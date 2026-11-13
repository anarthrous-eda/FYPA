"""Turn a :class:`DesignView` (plus optional datasheets) into PDN suggestions.

The rules, in order:

1. **Regulators** — ICs / modules with input pins (VIN, IN, PVIN…) and
   output pins (VOUT, OUT…) on different non-ground nets; or input pins and
   a switch pin (SW / LX) whose inductor leads to the output rail. LDO vs
   SMPS comes from the datasheet when there is one, else from the pins (a
   SW / BOOT pin means a switcher; BYP / NR means an LDO), else from the
   part's description, else from the voltage ratio. Output voltage comes
   from the output rail's name (``+1V8`` → 1.8 V), else from a datasheet's
   fixed output. A part whose input and output rails carry the same voltage
   and no switch pin is a load switch: a SERIES element.
2. **Sources** — a connector pin on a rail that nothing on the board drives
   (no regulator output, no existing source, not reachable through a
   SERIES bridge from one). One per rail: the connector with most pins on it.
3. **Sinks** — load ICs (not regulators, connectors or passives) with a
   supply-named pin (VCC, VDD, VS, VCC(A)…) on a rail; one per part and
   rail. Current from the datasheet's supply / quiescent current, else a
   flagged estimate.
4. **Series** — resistors under 1 Ω and ferrite beads / inductors joining
   two rails (current-sense and filter elements). 0 Ω links are left to the
   automatic bridging.

Parts already set up (schematic PDN_* parameters or FYPA editor
directives) are skipped and listed, never re-suggested.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from fypa.autosetup.datasheets import DatasheetFacts, DatasheetLibrary
from fypa.autosetup.design import DesignView, Part
from fypa.autosetup.nets import (
    BOOT_PINS,
    CONNECTOR_PREFIXES,
    IC_PREFIXES,
    LDO_HINT_PINS,
    PASSIVE_PREFIXES,
    REG_IN_PINS,
    REG_OUT_PINS,
    SW_PINS,
    has_control_token,
    is_supply_pin,
    looks_like_rail,
    parse_resistance,
    pin_base,
    rail_voltage,
)

# Fallbacks when neither the design nor a datasheet gives a value. Each use is
# flagged as an estimate in the suggestion so the review screen shows it.
DEFAULT_SMPS_EFFICIENCY = 0.90
DEFAULT_SINK_CURRENT = 0.010          # A
DEFAULT_FERRITE_DCR = 0.05            # Ω
DEFAULT_SWITCH_RON = 0.05             # Ω
SENSE_RESISTOR_MAX = 1.0              # Ω

_SMPS_WORDS = ("BUCK", "BOOST", "STEP-DOWN", "STEP DOWN", "STEP-UP", "DC/DC",
               "DCDC", "DC-DC", "CONVERTER", "SWITCHING", "POWER MODULE", "SMPS")
_LDO_WORDS = ("LDO", "LINEAR", "LOW DROPOUT", "LOW-DROPOUT")


@dataclass
class Suggestion:
    """One proposed PDN directive, with the reasoning behind it."""

    role: str                         # SOURCE | SINK | REGULATOR | SERIES
    designator: str
    part_number: str = ""
    p_net: str | None = None          # REGULATOR: output net
    n_net: str | None = None          # ground / return (REGULATOR: shared)
    in_p_net: str | None = None       # REGULATOR only
    single_net: bool = False
    voltage: float | None = None
    current: float | None = None
    resistance: float | None = None
    regulator_type: str | None = None
    efficiency: float | None = None
    smps_topology: str | None = None
    confidence: float = 0.5
    reasons: list[str] = field(default_factory=list)
    estimated: set[str] = field(default_factory=set)   # field names guessed
    datasheet: Path | None = None
    accepted: bool = True
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])

    @property
    def value_field(self) -> str:
        return {"SINK": "current", "SERIES": "resistance"}.get(self.role, "voltage")

    @property
    def value(self) -> float | None:
        return getattr(self, self.value_field)

    def missing(self) -> list[str]:
        """Fields that must be filled before this can be applied."""
        out = []
        if self.value is None:
            out.append(self.value_field)
        if not self.p_net:
            out.append("net")
        if self.role == "REGULATOR" and not self.in_p_net:
            out.append("input net")
        if (self.role in ("SERIES", "REGULATOR") or not self.single_net) and not self.n_net:
            out.append("return net" if self.role != "SERIES" else "second net")
        return out

    @property
    def complete(self) -> bool:
        return not self.missing()

    def nets_text(self) -> str:
        if self.role == "REGULATOR":
            return f"{self.in_p_net or '?'} → {self.p_net or '?'}"
        if self.role == "SERIES":
            return f"{self.p_net or '?'} ↔ {self.n_net or '?'}"
        return self.p_net or "?"

    def to_editor_directive(self):
        from fypa.project_file import EditorDirective

        two_net = self.role in ("SERIES", "REGULATOR") or not self.single_net
        return EditorDirective(
            kind="component",
            role=self.role,
            designator=self.designator,
            single_net=not two_net,
            p_net=self.p_net,
            n_net=self.n_net if two_net else None,
            voltage=self.voltage if self.role in ("SOURCE", "REGULATOR") else None,
            current=self.current if self.role == "SINK" else None,
            resistance=self.resistance if self.role == "SERIES" else None,
            in_p_net=self.in_p_net if self.role == "REGULATOR" else None,
            in_n_net=self.n_net if self.role == "REGULATOR" else None,
            regulator_type=self.regulator_type if self.role == "REGULATOR" else None,
            efficiency=(self.efficiency if self.role == "REGULATOR"
                        and self.regulator_type == "SMPS" else None),
            smps_topology=(self.smps_topology if self.role == "REGULATOR"
                           and self.regulator_type == "SMPS" else None),
            origin="autosetup",
        )


@dataclass
class AutoSetupResult:
    suggestions: list[Suggestion]
    already_configured: list[tuple[str, str]]   # (designator, what)
    datasheet_matches: dict[str, Path]          # designator → pdf
    without_datasheet: list[str]                # ICs with no datasheet found
    datasheets_used: bool
    notes: list[str] = field(default_factory=list)


# --- helpers -----------------------------------------------------------------


class _Rails:
    """Union of nets joined by SERIES bridges, and which groups are driven."""

    def __init__(self, view: DesignView) -> None:
        self._parent: dict[str, str] = {}
        for a, b in view.bridges:
            self.union(a, b)
        self._driven = {self.find(n) for n in view.driven}

    def find(self, n: str) -> str:
        n = n.upper()
        self._parent.setdefault(n, n)
        while self._parent[n] != n:
            self._parent[n] = self._parent[self._parent[n]]
            n = self._parent[n]
        return n

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra

    def drive(self, net: str) -> None:
        self._driven.add(self.find(net))

    def driven(self, net: str) -> bool:
        return self.find(net) in self._driven


def _pin_bases(part: Part) -> dict[str, set[str]]:
    """pin base name → nets."""
    out: dict[str, set[str]] = defaultdict(set)
    for p in part.pins:
        if p.net:
            out[pin_base(p.name)].add(p.net)
    return out


def _words(part: Part) -> str:
    return f"{part.mpn} {part.description} {part.param('part name')}".upper()


def _fact_reason(label: str, fact) -> str:
    return f"{label} (datasheet: “{fact.evidence}”)"


# --- regulators ----------------------------------------------------------------


def _regulators(view: DesignView, lib: DatasheetLibrary | None,
                facts_of: Callable[[Part], DatasheetFacts | None],
                rails: _Rails) -> list[Suggestion]:
    out: list[Suggestion] = []
    for part in view.parts:
        if part.designator.upper() in view.configured:
            continue
        if part.prefix not in IC_PREFIXES and part.prefix not in ("Q",):
            continue
        bases = _pin_bases(part)
        gnd = view.gnd_net_of(part)

        def rail_nets(names: frozenset[str]) -> set[str]:
            nets: set[str] = set()
            for b, ns in bases.items():
                if b in names:
                    nets |= {n for n in ns if not view.is_gnd(n)}
            return nets

        ins = rail_nets(REG_IN_PINS)
        outs = rail_nets(REG_OUT_PINS) - ins
        sw_nets = rail_nets(SW_PINS)
        has_sw = bool(sw_nets) or any(b in BOOT_PINS for b in bases)
        via_inductor = ""
        if ins and not outs and sw_nets:
            # External inductor: the output rail is the far side of the
            # inductor on the switch node.
            for other in view.parts:
                if other.prefix not in ("L",) or not (other.nets() & sw_nets):
                    continue
                far = {n for n in other.nets() - sw_nets if not view.is_gnd(n)}
                if far:
                    outs = far
                    via_inductor = other.designator
                    break
        if not ins or not outs:
            continue
        facts = facts_of(part)
        in_net = max(ins, key=lambda n: (len(part.pins_on(n)), n))
        for out_net in sorted(outs):
            sug = _regulator_suggestion(view, part, in_net, out_net, gnd, bases,
                                        has_sw, via_inductor, facts)
            if sug is not None:
                out.append(sug)
                if sug.role == "REGULATOR":
                    rails.drive(out_net)
                else:  # load switch → bridge
                    rails.union(in_net, out_net)
    return out


def _regulator_suggestion(view, part, in_net, out_net, gnd, bases, has_sw,
                          via_inductor, facts) -> Suggestion | None:
    reasons = [f"{part.designator}: input pins on {in_net}, output on {out_net}"
               + (f" (through inductor {via_inductor})" if via_inductor else "")]
    vin = rail_voltage(in_net)
    vout = rail_voltage(out_net)
    words = _words(part)
    is_switch = (facts is not None and facts.is_switch is not None
                 and facts.kind is None)
    if not has_sw and (is_switch or (vin is not None and vout is not None
                                     and abs(vin - vout) < 1e-6)):
        # Same voltage in and out, no switch node: a load switch / eFuse.
        sug = Suggestion(role="SERIES", designator=part.designator,
                         part_number=part.mpn, p_net=in_net, n_net=out_net,
                         confidence=0.55)
        if facts is not None and facts.resistance is not None:
            sug.resistance = float(facts.resistance.value)
            sug.reasons.append(_fact_reason("on-resistance", facts.resistance))
            sug.confidence = 0.75
        else:
            sug.resistance = DEFAULT_SWITCH_RON
            sug.estimated.add("resistance")
        sug.reasons = reasons + ["same voltage in and out and no switch pin: "
                                 "a load switch, modelled as a series resistance"] + sug.reasons
        if is_switch:
            sug.reasons.append(_fact_reason("load switch", facts.is_switch))
        sug.datasheet = facts.path if facts else None
        return sug

    sug = Suggestion(role="REGULATOR", designator=part.designator,
                     part_number=part.mpn, p_net=out_net, n_net=gnd,
                     in_p_net=in_net, confidence=0.6, reasons=list(reasons))
    # Type: datasheet > pins > description > voltage ratio.
    if facts is not None and facts.kind is not None:
        sug.regulator_type = str(facts.kind.value)
        sug.reasons.append(_fact_reason(f"{sug.regulator_type}", facts.kind))
        sug.confidence += 0.2
    elif has_sw:
        sug.regulator_type = "SMPS"
        sug.reasons.append("has a switch-node pin (SW / LX / BOOT): a switcher")
        sug.confidence += 0.15
    elif any(b in LDO_HINT_PINS for b in bases):
        sug.regulator_type = "LDO"
        sug.reasons.append("has a bypass / noise-reduction pin and no switch pin: an LDO")
        sug.confidence += 0.1
    elif any(w in words for w in _LDO_WORDS):
        sug.regulator_type = "LDO"
        sug.reasons.append(f"part description reads as an LDO ({part.description or part.mpn})")
    elif any(w in words for w in _SMPS_WORDS):
        sug.regulator_type = "SMPS"
        sug.reasons.append(f"part description reads as a switcher ({part.description or part.mpn})")
    else:
        sug.regulator_type = ("SMPS" if vin and vout and vin / max(vout, 1e-9) > 2.5
                              else "LDO")
        sug.estimated.add("regulator_type")
        sug.reasons.append("type guessed from the voltage ratio — check it")
        sug.confidence -= 0.15

    # Output voltage: rail name > datasheet fixed output.
    if vout is not None:
        sug.voltage = vout
        sug.reasons.append(f"output voltage {vout:g} V from the rail name {out_net}")
    elif facts is not None and facts.vout_fixed is not None:
        sug.voltage = float(facts.vout_fixed.value)
        sug.reasons.append(_fact_reason(f"fixed output {sug.voltage:g} V", facts.vout_fixed))
    else:
        sug.reasons.append(f"no output voltage in the rail name {out_net} — enter it")
        sug.confidence -= 0.2

    if sug.regulator_type == "SMPS":
        if facts is not None and facts.topology is not None:
            sug.smps_topology = str(facts.topology.value)
            sug.reasons.append(_fact_reason(f"{sug.smps_topology} topology", facts.topology))
        elif vin is not None and sug.voltage is not None:
            sug.smps_topology = "BUCK" if vin > sug.voltage else "BOOST"
            sug.estimated.add("smps_topology")
        if facts is not None and facts.efficiency is not None:
            # Datasheets quote peak efficiency; derate a little for a
            # realistic operating point.
            peak = float(facts.efficiency.value)
            sug.efficiency = round(max(0.5, peak - 0.03), 3)
            sug.reasons.append(_fact_reason(
                f"efficiency {sug.efficiency:.0%} (peak {peak:.0%} less 3 points)",
                facts.efficiency))
        else:
            sug.efficiency = DEFAULT_SMPS_EFFICIENCY
            sug.estimated.add("efficiency")
            sug.reasons.append(f"efficiency assumed {DEFAULT_SMPS_EFFICIENCY:.0%}")
    if facts is not None and facts.vin_range is not None and vin is not None:
        lo, hi = facts.vin_range.value
        if not (lo <= vin <= hi):
            sug.reasons.append(f"⚠ input rail {vin:g} V is outside the datasheet's "
                               f"{lo:g}–{hi:g} V range")
            sug.confidence -= 0.2
    if gnd is None:
        sug.reasons.append("no ground pin found — pick the return net")
    sug.datasheet = facts.path if facts else None
    sug.confidence = max(0.05, min(0.98, sug.confidence))
    return sug


# --- sources -------------------------------------------------------------------


def _sources(view: DesignView, rails: _Rails, rail_set: set[str]) -> list[Suggestion]:
    best: dict[str, tuple[int, Part, str]] = {}
    for part in view.parts:
        if part.prefix not in CONNECTOR_PREFIXES:
            continue
        if part.designator.upper() in view.configured:
            continue
        for net in part.nets():
            if net not in rail_set or view.is_gnd(net) or rails.driven(net):
                continue
            n = len(part.pins_on(net))
            key = rails.find(net)
            if key not in best or n > best[key][0]:
                best[key] = (n, part, net)
    out = []
    for _key, (n, part, net) in sorted(best.items(), key=lambda kv: kv[1][2]):
        v = rail_voltage(net)
        gnd = view.gnd_net_of(part)
        sug = Suggestion(role="SOURCE", designator=part.designator,
                         part_number=part.mpn, p_net=net, n_net=gnd,
                         single_net=gnd is None, voltage=v, confidence=0.7)
        sug.reasons.append(f"connector {part.designator} has {n} pin(s) on {net}, "
                           "which nothing on the board drives")
        if v is not None:
            sug.reasons.append(f"{v:g} V from the rail name")
        else:
            sug.reasons.append("no voltage in the rail name — enter it")
            sug.confidence -= 0.25
        if gnd is not None:
            sug.reasons.append(f"return through its {gnd} pins")
        out.append(sug)
        rails.drive(net)
    return out


# --- sinks ---------------------------------------------------------------------


def _sinks(view: DesignView, regulator_ids: set[str], rails: _Rails,
           facts_of: Callable[[Part], DatasheetFacts | None]) -> list[Suggestion]:
    out = []
    for part in view.parts:
        des = part.designator.upper()
        if des in view.configured or des in regulator_ids:
            continue
        if part.prefix in CONNECTOR_PREFIXES or part.prefix in PASSIVE_PREFIXES:
            continue
        supply: dict[str, list[str]] = defaultdict(list)
        for p in part.pins:
            if p.net and not view.is_gnd(p.net) and is_supply_pin(p.name) \
                    and not has_control_token(p.net):
                supply[p.net].append(p.name)
        if not supply:
            continue
        facts = facts_of(part)
        gnd = view.gnd_net_of(part)
        n_rails = len(supply)
        for net, names in sorted(supply.items()):
            sug = Suggestion(role="SINK", designator=part.designator,
                             part_number=part.mpn, p_net=net, n_net=gnd,
                             single_net=gnd is None, confidence=0.6)
            sug.reasons.append(f"supply pin(s) {', '.join(sorted(set(names))[:4])} on {net}")
            if not rails.driven(net):
                sug.reasons.append(f"⚠ nothing drives {net} yet")
                sug.confidence -= 0.1
            fact = None
            if facts is not None:
                fact = facts.supply_current or facts.quiescent
            if fact is not None:
                # A part fed from several rails splits its quoted current.
                sug.current = float(fact.value) / n_rails
                sug.reasons.append(_fact_reason(
                    f"supply current {_fmt_a(float(fact.value))}"
                    + (f", split over {n_rails} rails" if n_rails > 1 else ""), fact))
                sug.confidence += 0.15
                sug.datasheet = facts.path
            else:
                sug.current = DEFAULT_SINK_CURRENT
                sug.estimated.add("current")
                sug.reasons.append(f"current estimated at {_fmt_a(DEFAULT_SINK_CURRENT)} — "
                                   "replace with the datasheet value")
            out.append(sug)
    return out


def _fmt_a(a: float) -> str:
    if a >= 1:
        return f"{a:g} A"
    if a >= 1e-3:
        return f"{a * 1e3:g} mA"
    return f"{a * 1e6:g} µA"


# --- series --------------------------------------------------------------------


def _series(view: DesignView, rail_set: set[str],
            facts_of: Callable[[Part], DatasheetFacts | None],
            rails: _Rails) -> list[Suggestion]:
    out = []
    for part in view.parts:
        if part.designator.upper() in view.configured:
            continue
        if part.prefix not in ("R", "FB", "L", "FL"):
            continue
        nets = sorted(n for n in part.nets() if not view.is_gnd(n))
        if len(nets) != 2 or len(part.nets()) != 2:
            continue
        a, b = nets
        if a not in rail_set or b not in rail_set:
            continue
        if part.prefix == "R":
            r = parse_resistance(part.value_text)
            if r is None or r <= 0 or r > SENSE_RESISTOR_MAX:
                continue  # 0 Ω links are auto-bridged; big values aren't in the path
            sug = Suggestion(role="SERIES", designator=part.designator,
                             part_number=part.mpn, p_net=a, n_net=b,
                             resistance=r, confidence=0.75)
            sug.reasons.append(f"{r * 1e3:g} mΩ resistor between rails {a} and {b} "
                               "(current sense / shunt)")
        else:
            facts = facts_of(part)
            sug = Suggestion(role="SERIES", designator=part.designator,
                             part_number=part.mpn, p_net=a, n_net=b, confidence=0.6)
            dcr = parse_resistance(part.param("dc resistance", "dcr", "rdc"))
            if dcr:
                sug.resistance = dcr
                sug.reasons.append(f"DC resistance {dcr * 1e3:g} mΩ from the part parameters")
            elif facts is not None and facts.resistance is not None:
                sug.resistance = float(facts.resistance.value)
                sug.reasons.append(_fact_reason("DC resistance", facts.resistance))
                sug.datasheet = facts.path
            else:
                sug.resistance = DEFAULT_FERRITE_DCR
                sug.estimated.add("resistance")
                sug.reasons.append(f"DC resistance assumed {DEFAULT_FERRITE_DCR * 1e3:g} mΩ")
            sug.reasons.insert(0, f"{'ferrite bead' if part.prefix in ('FB', 'FL') else 'inductor'} "
                               f"between rails {a} and {b}")
        rails.union(a, b)
        out.append(sug)
    return out


# --- rail set ------------------------------------------------------------------


def _rail_nets(view: DesignView) -> set[str]:
    """Nets that carry power: named like a rail, drawn with a power port, or
    on a regulator / supply pin."""
    rails: set[str] = set()
    for name in view.net_names:
        if not name or view.is_gnd(name):
            continue
        if looks_like_rail(name) or name.upper() in view.power_ports \
                or name.upper() in view.driven:
            rails.add(name)
    for part in view.parts:
        for p in part.pins:
            if not p.net or view.is_gnd(p.net) or has_control_token(p.net):
                continue
            b = pin_base(p.name)
            if (part.prefix in IC_PREFIXES and (b in REG_IN_PINS or b in REG_OUT_PINS)) \
                    or (part.prefix not in CONNECTOR_PREFIXES and is_supply_pin(p.name)):
                rails.add(p.net)
    return rails


# --- entry point ---------------------------------------------------------------


def suggest(view: DesignView, *, datasheets: DatasheetLibrary | None = None,
            progress: Callable[[float, str], None] | None = None,
            cancelled: Callable[[], bool] | None = None) -> AutoSetupResult:
    """Classify the design and return reviewed-ready suggestions."""
    def tick(frac: float, msg: str) -> None:
        if progress is not None:
            progress(frac, msg)
        if cancelled is not None and cancelled():
            raise InterruptedError

    matches: dict[str, Path] = {}
    facts_cache: dict[str, DatasheetFacts | None] = {}
    candidates = [p for p in view.parts
                  if p.prefix in IC_PREFIXES or p.prefix in ("FB", "FL", "L", "Q")]

    def facts_of(part: Part) -> DatasheetFacts | None:
        key = part.designator.upper()
        if key in facts_cache:
            return facts_cache[key]
        result = None
        if datasheets is not None and len(datasheets):
            pns = [part.mpn, part.param("comment"), part.param("part name"),
                   part.param("value")]
            path = datasheets.find([pn for pn in pns if pn])
            if path is not None:
                matches[part.designator] = path
                result = datasheets.facts(path)
        facts_cache[key] = result
        return result

    if datasheets is not None and len(datasheets):
        todo = [p for p in candidates if p.designator.upper() not in view.configured]
        for i, part in enumerate(todo):
            tick(0.1 + 0.7 * i / max(1, len(todo)),
                 f"Reading datasheet for {part.designator} ({part.mpn or '?'})")
            facts_of(part)

    tick(0.82, "Classifying regulators")
    rails = _Rails(view)
    rail_set = _rail_nets(view)
    regs = _regulators(view, datasheets, facts_of, rails)
    tick(0.88, "Finding series elements")
    series = _series(view, rail_set, facts_of, rails)
    tick(0.92, "Finding sources")
    rail_set |= {s.p_net for s in regs if s.p_net} | {s.in_p_net for s in regs if s.in_p_net}
    sources = _sources(view, rails, rail_set)
    tick(0.96, "Finding sinks")
    reg_ids = {s.designator.upper() for s in regs}
    sinks = _sinks(view, reg_ids, rails, facts_of)

    suggestions = sources + regs + series + sinks
    for s in suggestions:
        s.accepted = s.complete and s.confidence >= 0.5

    without = sorted(
        p.designator for p in candidates
        if p.designator.upper() not in view.configured
        and p.designator not in matches
        and p.designator.upper() in {s.designator.upper() for s in suggestions}
    ) if datasheets is not None and len(datasheets) else []
    result = AutoSetupResult(
        suggestions=suggestions,
        already_configured=sorted(
            (p.designator, view.configured[p.designator.upper()])
            for p in view.parts if p.designator.upper() in view.configured),
        datasheet_matches=matches,
        without_datasheet=without,
        datasheets_used=datasheets is not None and len(datasheets) > 0,
    )
    unreadable = getattr(datasheets, "unreadable", None) or {}
    if unreadable:
        listed = "; ".join(f"{p.name} ({why})" for p, why in sorted(unreadable.items())[:6])
        more = f" and {len(unreadable) - 6} more" if len(unreadable) > 6 else ""
        result.notes.append(
            f"Couldn't read {len(unreadable)} datasheet(s): {listed}{more}. "
            "Parts matched to them were classified from the design alone.")
    if not result.datasheets_used:
        result.notes.append(
            "No datasheets: types, efficiencies and currents come from pin "
            "names, net names and part descriptions only. Values marked "
            "“estimate” need checking.")
    tick(1.0, "Done")
    return result
