"""Manual setup: the logic behind the Topology tab's Manual setup wizard.

The wizard edits a working list of FYPA editor directives — supplies
(SOURCE / REGULATOR) and loads (SINK) bound to parts — the way KiPI's PI
setup panel edits VRMs and loads: pick a net, then a part on it (busiest
first), then optionally its pins; empty fields are filled from the design
(voltage from the net name, ground from the part, a regulator's input from
its one other rail). Nothing here touches Qt or the project; the dialog
owns that.
"""

from __future__ import annotations

import copy
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from fypa.autosetup.design import DesignView, Part
from fypa.autosetup.nets import (
    CONNECTOR_PREFIXES,
    IC_PREFIXES,
    is_supply_pin,
    looks_like_rail,
    rail_voltage,
)
from fypa.project_file import EditorDirective

IDEAL_RETURN = "(ideal return)"
SUPPLY_TYPES = ("Source", "LDO", "Switcher")
SUPPLY_ROLES = ("SOURCE", "REGULATOR")


# --- entries ---------------------------------------------------------------------


@dataclass
class Entry:
    """One row of the wizard: an editor directive, or a locked schematic one."""

    directive: EditorDirective
    locked: bool = False            # defined in the schematic, not editable
    schematic: dict | None = None   # the metadata directive, when locked

    @property
    def is_supply(self) -> bool:
        return self.directive.role in SUPPLY_ROLES

    def title(self) -> str:
        d = self.directive
        kind = supply_type(d) if self.is_supply else "Load"
        net = d.p_net or "?"
        val = ""
        if d.role == "SINK" and d.current is not None:
            val = f" · {_fmt(d.current, 'A')}"
        elif d.voltage is not None:
            val = f" · {_fmt(d.voltage, 'V')}"
        lock = "🔒 " if self.locked else ""
        return f"{lock}{d.designator or '?'}  {kind}  {net}{val}"


def _fmt(v: float, unit: str) -> str:
    a = abs(v)
    if a >= 1 or a == 0:
        return f"{v:.4g} {unit}"
    if a >= 1e-3:
        return f"{v * 1e3:.4g} m{unit}"
    return f"{v * 1e6:.4g} µ{unit}"


def supply_type(d: EditorDirective) -> str:
    if d.role == "SOURCE":
        return "Source"
    return "Switcher" if (d.regulator_type or "").upper() == "SMPS" else "LDO"


def set_supply_type(d: EditorDirective, kind: str) -> None:
    """Switch a supply between Source / LDO / Switcher, keeping its output."""
    if kind == "Source":
        d.role = "SOURCE"
        d.in_p_net = d.in_n_net = None
        d.in_p_pins = d.in_n_pins = None
        d.regulator_type = d.efficiency = d.smps_topology = None
    else:
        d.role = "REGULATOR"
        d.single_net = False
        d.regulator_type = "SMPS" if kind == "Switcher" else "LDO"
        if kind == "LDO":
            d.efficiency = None
            d.smps_topology = None


def new_supply() -> EditorDirective:
    return EditorDirective(kind="component", role="SOURCE", single_net=False)


def new_load() -> EditorDirective:
    return EditorDirective(kind="component", role="SINK", single_net=False)


def duplicate_keep_id(d: EditorDirective) -> EditorDirective:
    """Working copy of an existing directive (same id, so Apply updates it)."""
    return copy.deepcopy(d)


def duplicate(d: EditorDirective) -> EditorDirective:
    out = copy.deepcopy(d)
    out.id = EditorDirective().id
    out.overrides_designator = None
    return out


def set_ground(d: EditorDirective, gnd: str | None) -> None:
    """Ground pick: a net, or IDEAL_RETURN (single-net, sources / loads only)."""
    if gnd == IDEAL_RETURN and d.role != "REGULATOR":
        d.single_net = True
        d.n_net = None
    else:
        d.single_net = False
        d.n_net = gnd or None
        if d.role == "REGULATOR":
            d.in_n_net = gnd or None


def ground_of(d: EditorDirective) -> str | None:
    if d.single_net and d.role != "REGULATOR":
        return IDEAL_RETURN
    return d.n_net


# --- pickers ---------------------------------------------------------------------


def net_choices(view: DesignView) -> list[str]:
    """Every net with a part pin on it: rails first (named like one, drawn
    with a power port, or on a supply pin), then the rest; grounds last."""
    used: set[str] = set()
    supply_nets: set[str] = set()
    for part in view.parts:
        for p in part.pins:
            if p.net:
                used.add(p.net)
                if is_supply_pin(p.name):
                    supply_nets.add(p.net)

    def rank(n: str) -> tuple:
        if view.is_gnd(n):
            return (2, n)
        railish = (looks_like_rail(n) or n.upper() in view.power_ports
                   or n in supply_nets)
        return (0 if railish else 1, n)

    return sorted(used, key=rank)


def parts_on_net(view: DesignView, net: str, *, prefer: str = "") -> list[tuple[str, str]]:
    """``(designator, label)`` for parts with a pin on ``net``, busiest first
    (``U4  TPS62130  (3)``). ``prefer`` ("supply" / "load") floats the likely
    kind of part to the top among equals."""
    rows = []
    for part in view.parts_on(net):
        n = len(part.pins_on(net))
        if prefer == "supply":
            kind = 0 if part.prefix in IC_PREFIXES | CONNECTOR_PREFIXES else 1
        elif prefer == "load":
            kind = 0 if part.prefix in IC_PREFIXES else 1
        else:
            kind = 0
        label = f"{part.designator}  {part.mpn or part.value_text}".rstrip() + f"  ({n})"
        rows.append(((kind, -n, part.designator), part.designator, label))
    rows.sort()
    return [(des, label) for _k, des, label in rows]


def ground_choices(view: DesignView, part: Part | None) -> list[str]:
    """Ground nets, the part's own first, then any other ground-named net."""
    seen: list[str] = []
    if part is not None:
        g = view.gnd_net_of(part)
        if g:
            seen.append(g)
        for n in sorted(part.nets()):
            if view.is_gnd(n) and n not in seen:
                seen.append(n)
    for n in sorted(view.net_names):
        if n and view.is_gnd(n) and n not in seen:
            seen.append(n)
    return seen


def pins_of(part: Part | None, net: str | None) -> list[tuple[str, str]]:
    """``[(pad, pin name)]`` of ``part`` on ``net``, in natural pad order."""
    if part is None or not net:
        return []
    import re

    def key(pad: str) -> list:
        return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", pad.upper())]

    seen: dict[str, str] = {}
    for p in part.pins_on(net):
        seen.setdefault(p.pad, p.name)
    return [(pad, seen[pad]) for pad in sorted(seen, key=key)]


def input_net_choices(view: DesignView, part: Part | None, out_net: str | None) -> list[str]:
    """A regulator's possible inputs: the part's own nets, not ground and not
    its output — rails first (named like one / drawn with a power port), then
    the rest, so a feedback / enable net is still pickable if it really is
    the input."""
    if part is None:
        return []
    nets = [n for n in part.nets() if n != out_net and not view.is_gnd(n)]

    def rank(n: str) -> tuple:
        railish = looks_like_rail(n) or n.upper() in view.power_ports
        return (0 if railish else 1, -len(part.pins_on(n)), n)

    return sorted(nets, key=rank)


def other_rails(view: DesignView, part: Part, net: str) -> list[str]:
    """The part's other power rails — named like a rail or drawn with a power
    port — so a bypass / feedback / enable net doesn't count (falling back to
    every non-ground net when none of them reads like a rail)."""
    nets = [n for n in part.nets() if n != net and not view.is_gnd(n)]
    rails = [n for n in nets if looks_like_rail(n) or n.upper() in view.power_ports]
    return sorted(rails or nets)


# --- autofill ----------------------------------------------------------------------


def autofill(d: EditorDirective, view: DesignView) -> list[str]:
    """Fill the directive's empty fields from the design; returns what changed.

    Only empty fields are touched, so anything the user typed stays."""
    filled: list[str] = []
    net = d.p_net
    if not net:
        return filled
    if d.role in SUPPLY_ROLES and d.voltage is None:
        v = rail_voltage(net)
        if v is not None:
            d.voltage = v
            filled.append("voltage")
    if not d.designator:
        prefer = "supply" if d.role in SUPPLY_ROLES else "load"
        cands = parts_on_net(view, net, prefer=prefer)
        if len(cands) == 1 or (cands and d.role == "SINK"):
            d.designator = cands[0][0]
            filled.append("part")
    part = view.part(d.designator) if d.designator else None
    if part is None:
        return filled
    if not d.n_net and not d.single_net:
        g = view.gnd_net_of(part)
        if g:
            set_ground(d, g)
            filled.append("ground")
    if d.role == "REGULATOR" and not d.in_p_net:
        # The one net on input-named pins (VIN_1…VIN_14 on a module), else the
        # part's one other rail.
        from fypa.autosetup.nets import REG_IN_PINS, pin_base

        by_name = {p.net for p in part.pins
                   if p.net and p.net != net and not view.is_gnd(p.net)
                   and pin_base(p.name) in REG_IN_PINS}
        others = sorted(by_name) if by_name else other_rails(view, part, net)
        if len(others) == 1:
            d.in_p_net = others[0]
            filled.append("input")
    return filled


def split_load(view: DesignView, designator: str, total: float,
               ground: str | None = None) -> list[EditorDirective]:
    """One load per supply net of a part, sharing ``total`` amps by how many
    supply pins sit on each net (KiPI's "Split across nets…")."""
    part = view.part(designator)
    if part is None:
        return []
    counts: dict[str, int] = defaultdict(int)
    for p in part.pins:
        if p.net and not view.is_gnd(p.net) and is_supply_pin(p.name):
            counts[p.net] += 1
    if not counts:   # no supply-named pins: every non-ground rail it touches
        for n in other_rails(view, part, ""):
            counts[n] = len(part.pins_on(n))
    pins = sum(counts.values()) or 1
    gnd = ground or view.gnd_net_of(part)
    out = []
    for net, n in sorted(counts.items()):
        d = new_load()
        d.designator = part.designator
        d.p_net = net
        d.current = total * n / pins
        set_ground(d, gnd or IDEAL_RETURN)
        out.append(d)
    return out


# --- schematic directives ---------------------------------------------------------


def _term_net(term: dict | None) -> str | None:
    if not term or term.get("ideal_return"):
        return None
    if term.get("requested_net"):
        return term["requested_net"]
    for p in term.get("pins") or []:
        if p.get("net"):
            return p["net"]
    return None


def _term_pads(term: dict | None) -> list[str] | None:
    pads = sorted({str(p["pad"]) for p in (term or {}).get("pins") or []
                   if p.get("pad") and not str(p["pad"]).startswith("(")})
    return pads or None


def from_schematic(md: dict) -> EditorDirective | None:
    """Editable copy of a schematic SOURCE / SINK / REGULATOR (metadata
    dict) that overrides it on the next resolve — the wizard's Override."""
    role = md.get("role")
    terms = md.get("terminals") or {}
    des = md.get("designator")
    if role == "SOURCE" or role == "SINK":
        n_term = terms.get("N")
        single = n_term is None or bool((n_term or {}).get("ideal_return"))
        d = EditorDirective(kind="component", role=role, designator=des,
                            single_net=single, p_net=_term_net(terms.get("P")),
                            n_net=None if single else _term_net(n_term),
                            p_pins=_term_pads(terms.get("P")),
                            n_pins=None if single else _term_pads(n_term))
        if role == "SOURCE":
            d.voltage = md.get("value")
        else:
            d.current = md.get("value")
            d.min_voltage = md.get("min_voltage")
    elif role == "REGULATOR":
        d = EditorDirective(kind="component", role="REGULATOR", designator=des,
                            single_net=False,
                            p_net=_term_net(terms.get("OUT_P")),
                            n_net=_term_net(terms.get("OUT_N")),
                            in_p_net=_term_net(terms.get("IN_P")),
                            in_n_net=_term_net(terms.get("IN_N")),
                            p_pins=_term_pads(terms.get("OUT_P")),
                            in_p_pins=_term_pads(terms.get("IN_P")),
                            voltage=md.get("value"),
                            regulator_type=(md.get("regulator_type") or "LDO").upper(),
                            efficiency=md.get("efficiency"),
                            smps_topology=md.get("smps_topology"))
    else:
        return None
    d.overrides_designator = des
    return d


def schematic_entries(metadata: dict | None, editor_directives) -> list[Entry]:
    """Locked rows for schematic supplies / loads not already overridden."""
    overridden = {ed.overrides_designator for ed in editor_directives
                  if getattr(ed, "overrides_designator", None)}
    out = []
    for md in (metadata or {}).get("directives") or []:
        if md.get("schdoc") in ("(editor)", "(auto-bridge)"):
            continue
        if md.get("role") not in ("SOURCE", "SINK", "REGULATOR"):
            continue
        if md.get("designator") in overridden:
            continue
        d = from_schematic(md)
        if d is not None:
            out.append(Entry(directive=d, locked=True, schematic=md))
    return out


# --- validation -------------------------------------------------------------------


@dataclass
class Issue:
    level: str          # "error" blocks Apply; "warning" doesn't
    entry_id: str
    text: str


def validate(entries: list[Entry], view: DesignView,
             bridges: Sequence[tuple[str, str]] = ()) -> list[Issue]:
    issues: list[Issue] = []
    seen: dict[tuple, str] = {}
    for e in entries:
        if e.locked:
            continue
        d = e.directive
        name = d.designator or "new entry"

        def err(text: str, _id=d.id, _name=name) -> None:
            issues.append(Issue("error", _id, f"{_name}: {text}"))

        if not d.p_net:
            err("pick a net")
            continue
        if not d.designator:
            err("pick the part")
            continue
        part = view.part(d.designator)
        if part is None:
            err(f"{d.designator} isn't on the board")
            continue
        if not part.pins_on(d.p_net):
            err(f"has no pin on {d.p_net}")
        _check_pins(err, part, d.p_net, d.p_pins, "pins")
        if d.role in SUPPLY_ROLES:
            if d.voltage is None or d.voltage == 0:
                err("enter the output voltage")
        elif d.current is None or d.current <= 0:
            err("enter the current")
        gnd = d.n_net
        if not d.single_net:
            if not gnd:
                err("pick the ground / return net")
            elif gnd == d.p_net:
                err("ground and supply are the same net")
            elif not part.pins_on(gnd):
                issues.append(Issue("warning", d.id,
                                    f"{name}: has no pin on {gnd} — the return "
                                    "couples at the part's centre"))
        if d.role == "REGULATOR":
            if not d.in_p_net:
                err("pick the input net")
            elif d.in_p_net == d.p_net:
                err("input and output are the same net")
            else:
                _check_pins(err, part, d.in_p_net, d.in_p_pins, "input pins")
                if not part.pins_on(d.in_p_net):
                    err(f"has no pin on its input {d.in_p_net}")
            if (d.regulator_type or "") == "SMPS" and d.efficiency is not None \
                    and not (0 < d.efficiency <= 1):
                err("efficiency must be between 0 and 100 %")
        key = (d.designator, d.role, d.p_net)
        if key in seen:
            err(f"duplicates another {d.role.lower()} on {d.p_net}")
        seen[key] = d.id

    # Loads on rails nothing supplies (warning: the solve skips open rails).
    parent: dict[str, str] = {}

    def find(n: str) -> str:
        n = n.upper()
        parent.setdefault(n, n)
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    for a, b in list(bridges) + list(view.bridges):
        parent[find(a)] = find(b)
    # The rows (locked schematic ones included) are the whole supply side —
    # not view.driven, which still counts editor supplies being removed here.
    supplied = {find(e.directive.p_net) for e in entries
                if e.is_supply and e.directive.p_net}
    for e in entries:
        d = e.directive
        if e.locked or d.role != "SINK" or not d.p_net:
            continue
        if find(d.p_net) not in supplied:
            issues.append(Issue("warning", d.id,
                                f"{d.designator or 'load'}: nothing supplies "
                                f"{d.p_net} yet"))
    return issues


def _check_pins(err, part: Part, net: str, pins: list[str] | None, what: str) -> None:
    if pins is None:
        return   # all of its pins on the net
    if not pins:
        err(f"tick at least one of its {what} on {net} (or tick All)")
        return
    have = {p.pad.upper() for p in part.pins_on(net)}
    missing = [p for p in pins if p.upper() not in have]
    if missing:
        err(f"{what} {', '.join(missing)} aren't on {net}")
