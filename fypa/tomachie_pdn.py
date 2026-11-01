# Copyright (c) 2026 Tomachie LLC.

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

NOT_A_LOAD = ("signal_pin_only", "not_a_supply")

SCHEMA = "tomachie-pdn"
SCHEMA_VERSION = 1
SOURCE_ROLES = ("board_input", "battery", "generator")
REGULATOR_TYPE = {"linear": "LDO", "switching": "SMPS", "isolated": "SMPS"}
NOT_FITTED_OHMS = 5e8
OPEN_CLASSES = ("capacitor", "device", "connector")
DC_RESISTANCE_SPECS = ("dcr", "dc_resistance", "resistance", "rds_on", "jumper_resistance",
                       "cold_resistance", "r_dc", "rdc", "on_resistance", "contact_resistance")
OHMS = {"ohm": 1.0, "ohms": 1.0, "Ω": 1.0, "mohm": 1e-3, "mΩ": 1e-3,
        "kohm": 1e3, "kΩ": 1e3}


class PdnImportError(ValueError):
    pass


def _number(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _amps(current) -> float | None:
    return _number(current.get("amps")) if isinstance(current, dict) else None


def _pads(refdes: str, pins: list[str]) -> list[str]:
    prefix = f"{refdes}_"
    return [p[len(prefix):] if p.startswith(prefix) else p.rpartition("_")[2] for p in pins]


def _return_part(load: dict) -> str | None:
    if load.get("role") == "resistive_path":
        path = load.get("path_parts") or []
        return path[-1] if path else None
    if load.get("role") == "pull_resistor":
        pin = load.get("pull_to") or ""
        return pin.rsplit("_", 1)[0] if "_" in pin else None
    return load["refdes"]


def _printed_ohms(fig: dict, column: str) -> float | None:
    unit = str(fig.get("unit", "")).strip()
    scale = OHMS.get(unit, OHMS.get(unit.lower()))
    try:
        value = float(str(fig.get(column, "")).strip().lstrip("<>~"))
    except ValueError:
        return None
    return None if scale is None else abs(value) * scale


JUMPER_SPECS = ("jumper_resistance",)


def _prefix(spec, names=DC_RESISTANCE_SPECS) -> str | None:
    spec = str(spec or "").lower()
    if spec in names:
        return ""
    for s in names:
        if s != "resistance" and spec.endswith("_" + s):
            return spec[:-len(s) - 1]
    return None


def _own_figures(element: dict, names=DC_RESISTANCE_SPECS) -> list[dict]:
    figs = [(f, _prefix(f.get("spec"), names)) for f in element.get("datasheet_figures", [])]
    figs = [(f, p) for f, p in figs if p is not None]
    if any(not p for _, p in figs):
        return [f for f, p in figs if not p]
    pn = str(element.get("part_number") or "").lower()
    if len({p for _, p in figs if p}) > 1 and pn:
        own = [(f, p) for f, p in figs if not p or p in pn or pn in p]
        figs = own if any(p for _, p in own) else figs
    return [f for f, _ in figs]


def dc_resistance(element: dict) -> tuple[float | None, float | None]:
    own = _number(element.get("resistance_ohm"))
    if own is not None:
        if own != 0:
            return own, None
        jumper = [v for f in _own_figures(element, JUMPER_SPECS) for c in ("max", "typ")
                  if (v := _printed_ohms(f, c)) is not None]
        return 0.0, (max(jumper) if jumper else None)
    figs = _own_figures(element)
    typs = [v for v in (_printed_ohms(f, "typ") for f in figs) if v is not None]
    maxs = [v for v in (_printed_ohms(f, "max") for f in figs) if v is not None]
    return (max(typs) if typs else None), (max(maxs) if maxs else None)


_FORWARD_V_SPEC = re.compile(r"(?i)^(v_?f\w*|v_?forward\w*|forward_voltage\w*)$")
_AMPS_PER_UNIT = {"a": 1.0, "ma": 1e-3, "ua": 1e-6, "µa": 1e-6}


def _printed_volts(fig: dict, column: str) -> float | None:
    try:
        return float(str(fig.get(column, "")).strip().lstrip("<>~"))
    except ValueError:
        return None


def _test_amps(text) -> float | None:
    m = re.match(r"\s*([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*([mµu]?A)\b", str(text or ""))
    return float(m.group(1)) * _AMPS_PER_UNIT[m.group(2).lower()] if m else None


_AT_25C = re.compile(r"(?<![\d.])25\s*°?\s*C\b", re.I)


def _curve(element: dict) -> dict | None:
    curves = [c for c in element.get("vf_curves") or [] if len(c.get("points") or []) >= 2]
    if not curves:
        return None
    return next((c for c in curves if _AT_25C.search(str(c.get("label", "")))), curves[0])


def curve_points(element: dict) -> list[tuple[float, float]] | None:
    curve = _curve(element)
    return None if curve is None else [(float(i), float(v)) for i, v in curve["points"]]


def _curve_fit(element: dict) -> tuple[float, float, str] | None:
    curve = _curve(element)
    if curve is None:
        return None
    pts = sorted((float(i), float(v)) for i, v in curve["points"])
    top = pts[-1][0]
    on = [(i, v) for i, v in pts if i >= 0.1 * top] or pts
    if len(on) < 2:
        on = pts[-2:]
    n = len(on)
    mi = sum(i for i, _ in on) / n
    mv = sum(v for _, v in on) / n
    sxx = sum((i - mi) ** 2 for i, _ in on)
    slope = max(sum((i - mi) * (v - mv) for i, v in on) / sxx, 0.0) if sxx > 0 else 0.0
    drop = mv - slope * mi
    label = f" ({curve['label']})" if curve.get("label") else ""
    page = f", page {curve['page']}" if curve.get("page") else ""
    how = (f"solved on its Vf-vs-If curve{label}{page} at the current it carries "
           f"(starting from {drop:.3g} V + {slope * 1e3:.3g} mOhm, fitted from "
           f"{on[0][0]:g} A to {on[-1][0]:g} A)")
    return drop, slope, how


def forward_drop(element: dict) -> tuple[float | None, float | None, float, str]:
    figs = [f for f in element.get("datasheet_figures", [])
            if _FORWARD_V_SPEC.match(str(f.get("spec", "")))
            and str(f.get("unit", "")).strip().upper() == "V"]
    cols = {c: [(_test_amps(f.get("if")), _printed_volts(f, c)) for f in figs]
            for c in ("typ", "max")}
    cols = {c: [(i, v) for i, v in pts if v is not None] for c, pts in cols.items()}
    fitted = _curve_fit(element)
    if fitted is not None:
        drop, slope, how = fitted
        vf_max = None
        if cols["max"]:
            i_m, v_m = max(cols["max"], key=lambda p: p[1])
            vf_max = max(v_m - (i_m or 0.0) * slope, drop)
        return drop, vf_max, slope, how
    if not cols["typ"] and not cols["max"]:
        return None, None, 0.0, ""
    fit_col = "typ" if cols["typ"] else "max"
    pts = sorted((i, v) for i, v in cols[fit_col] if i is not None)
    if len(pts) >= 2 and pts[-1][0] > pts[0][0]:
        (i1, v1), (i2, v2) = pts[0], pts[-1]
        slope = max((v2 - v1) / (i2 - i1), 0.0)
        drop = v1 - i1 * slope
        how = (f"forward drop {drop:.3g} V + {slope * 1e3:.3g} mOhm slope, from the "
               f"{fit_col} figures at {i1:g} A and {i2:g} A")
    else:
        i_at, drop = max(cols[fit_col], key=lambda p: p[1])
        slope = 0.0
        at = f" at {i_at:g} A" if i_at is not None else ""
        how = (f"forward drop {drop:g} V ({fit_col}{at}) as a constant: below that "
               "current it drops less, so the rail after it reads somewhat low")
    vf_max = None
    if fit_col == "typ" and cols["max"]:
        vf_max = drop + max(v for _, v in cols["max"]) - max(v for _, v in cols["typ"])
    return drop, vf_max, slope, how


def _board_ground(pdn: dict) -> str:
    for g in pdn.get("grounds") or []:
        if g.get("kind") == "reference" and g.get("net"):
            return g["net"]
    return pdn["ground_nets"][0]


class _Channels:

    def __init__(self):
        self.params: dict[str, dict[str, str]] = defaultdict(dict)
        self.count: dict[str, int] = defaultdict(int)
        self.keys: dict[str, set] = defaultdict(set)

    def add(self, designator: str, key: tuple, values: dict[str, str]) -> bool:
        if key in self.keys[designator]:
            return False
        self.keys[designator].add(key)
        self.count[designator] += 1
        n = self.count[designator]
        for k, v in values.items():
            self.params[designator][f"PDN{n}_{k}"] = v
        return True


class TomachiePdn:
    def __init__(self, pdn: dict, corner: str = "typ"):
        if pdn.get("schema") != SCHEMA or pdn.get("schema_version") != SCHEMA_VERSION:
            raise PdnImportError(f"Not a {SCHEMA} version {SCHEMA_VERSION} file")
        if corner not in ("typ", "max"):
            raise PdnImportError(f"corner must be 'typ' or 'max', not {corner!r}")
        self.pdn, self.corner = pdn, corner
        grounds = pdn.get("ground_nets") or []
        if not grounds:
            raise PdnImportError("The file names no ground net")
        self.gnd = _board_ground(pdn)
        self.grounds = list(grounds)
        if self.gnd not in self.grounds:
            self.grounds.insert(0, self.gnd)
        self.reg_inputs: list[tuple[str, str, str]] = []
        self.rails = {r["name"]: r for r in pdn.get("rails", [])}
        self.regs = {g["refdes"]: g for g in pdn.get("regulators", [])}
        self.ch = _Channels()
        self.notes: list[str] = []
        self.powered: set = set()
        self.pending_regs: list[tuple] = []
        self.pending_switches: list[tuple] = []
        self.freewheels: dict[tuple[str, str], tuple[str, dict[str, str]]] = {}
        self.closed_returns: set = set()
        self.reg_outputs: dict[tuple[str, str], str] = {}
        self.isolated: dict[str, list[str]] = {}
        self.pull_sinks: dict[str, str] = {}
        self.extra_inputs: dict[tuple[str, str], list] = {}
        self.source_outputs: dict[tuple[str, str], str] = {}

    def _source_volts(self, rail: dict) -> float | None:
        v = rail.get("voltage", {})
        lo = _number(v.get("min"))
        return lo if self.corner == "max" and lo is not None else _number(v.get("nominal"))

    def _supply_volts(self, rail_name: str | None) -> float | None:
        if self.corner != "max" or rail_name not in self.rails:
            return _number(self.rails.get(rail_name, {}).get("voltage", {}).get("nominal"))
        seen = set()
        while rail_name in self.rails and rail_name not in seen:
            seen.add(rail_name)
            r = self.rails[rail_name]
            role = (r.get("source") or {}).get("role")
            if role in SOURCE_ROLES:
                return self._source_volts(r)
            if role == "parent_rail" and r.get("parent_rail") in self.rails:
                rail_name = r["parent_rail"]
                continue
            if role == "regulator":
                return self._regulated_volts(r)
            return _number(r.get("voltage", {}).get("nominal"))
        return None

    def _regulated_volts(self, rail: dict) -> float | None:
        nominal = _number(rail.get("voltage", {}).get("nominal"))
        band = rail.get("regulator_band")
        if self.corner != "max" or not isinstance(band, dict) or nominal is None:
            return nominal
        for key in ("at_grade", "at_25c"):
            b = band.get(key)
            ends = [x for x in (_number(b.get("min_v")), _number(b.get("max_v")))
                    if x is not None] if isinstance(b, dict) else []
            if ends:
                return min(ends + [nominal], key=abs)
        return nominal

    def _sources(self):
        for name, r in self.rails.items():
            src = r["source"]
            v = self._source_volts(r)
            if src["role"] == "unresolved":
                self.notes.append(f"Rail {name}: no source traced in the schematic")
            elif src["role"] in SOURCE_ROLES:
                if v is None or not src.get("pins"):
                    self.notes.append(f"Rail {name}: source {src.get('refdes')} has no stated "
                                      "voltage or pins; no SOURCE")
                    continue
                for e in src.get("output_path") or []:
                    if e.get("net_in") and e.get("net_out"):
                        self._series(e)
                self._cable_note(name, src)
                out = (src["refdes"], src.get("output_net") or name)
                if out in self.source_outputs:
                    self.notes.append(f"Rail {name}: {src['refdes']}'s output, placed with "
                                      f"{self.source_outputs[out]}")
                    self.powered.add(name)
                    continue
                self.source_outputs[out] = name
                values = {
                    "ROLE": "SOURCE", "V": f"{v:g}", "P_NET": src.get("output_net") or name,
                    "N_NET": self.gnd, "P_PINS": ",".join(_pads(src["refdes"], src["pins"]))}
                ret = src.get("return")
                if isinstance(ret, dict) and ret.get("net"):
                    values["N_NET"] = ret["net"]
                    if ret.get("pins"):
                        values["N_PINS"] = ",".join(_pads(src["refdes"], ret["pins"]))
                    path = [e for e in ret.get("path") or []
                            if e.get("net_in") and e.get("net_out")]
                    for e in path:
                        self._series(e)
                    if ret["net"] == self.gnd or (path and ret.get("ground_net") == self.gnd):
                        self.closed_returns.add(src["refdes"].upper())
                if v != _number(r.get("voltage", {}).get("nominal")):
                    self.notes.append(f"Rail {name}: worst case, source {src['refdes']} at its "
                                      f"minimum {v:g} V (nominal {r['voltage']['nominal']:g} V)")
                self.ch.add(src["refdes"], ("SOURCE", name), values)
                self.powered.add(name)

    def _cable_note(self, rail: str, src: dict):
        cable = src.get("cable")
        ohms = _number(cable.get("resistance_ohm")) if isinstance(cable, dict) else None
        if ohms and self.corner == "max":
            self.notes.append(f"Rail {rail}: {src['refdes']}'s cable allowance {ohms * 1e3:g} mOhm "
                              "is not in the solve; the rail reads that much high")

    def _ideal_source(self, ref: str, rail: str, src: dict, v: float, why: str):
        self.notes.append(f"{ref} ({rail}): {why}; modeled as an ideal source, drawing "
                          "nothing from its input rail")
        self.ch.add(ref, ("SOURCE", rail), {
            "ROLE": "SOURCE", "V": f"{v:g}", "P_NET": src.get("output_net") or rail,
            "N_NET": self.gnd, "P_PINS": ",".join(_pads(ref, src["pins"]))})
        self.powered.add(rail)
        for e in src.get("output_path", []):
            if e.get("net_in") and e.get("net_out"):
                self._series(e)

    def _input(self, reg: dict) -> tuple[str | None, list[str], str | None]:
        for rail in reg.get("input_rails", []):
            for l in self.rails.get(rail, {}).get("loads", []):
                if l["role"] == "regulator" and l["refdes"] == reg["refdes"]:
                    return (l.get("input_net") or rail, _pads(l["refdes"], l.get("pins", [])),
                            rail)
        return None, [], None

    def _input_entries(self, reg: dict) -> list[tuple[str, dict]]:
        out = []
        for rail in reg.get("input_rails", []):
            for l in self.rails.get(rail, {}).get("loads", []):
                if l["role"] == "regulator" and l["refdes"] == reg["refdes"] and l.get("pins"):
                    out.append((rail, l))
                    break
        return out

    def _corner_amps(self, obj: dict, key: str) -> float | None:
        return (_amps(obj.get(key + "_max")) if self.corner == "max" else None)             or _amps(obj.get(key))

    def _split_inputs(self, reg: dict, rail: str, values: dict[str, str], kind: str,
                      v_out: float, eff: float | None, iq: float | None):
        ref = reg["refdes"]
        entries = self._input_entries(reg)
        if len(entries) < 2:
            return
        total = self._corner_amps(reg, "input_current")
        own = [self._corner_amps(l, "current") for _, l in entries]
        known = [a for a in own if a is not None]
        if total and len(known) == len(own) and abs(sum(known) - total) <= 0.01 * total:
            shares = [a / total for a in known]
            how = "as the file states"
        else:
            shares = [1.0 / len(entries)] * len(entries)
            how = "equally (the file states no split)"
        rails = [r for r, _ in entries]
        self.notes.append(f"{ref} ({rail}): {len(entries)} input rails ({', '.join(rails)}); "
                          f"its input current is shared {how}: "
                          + ", ".join(f"{r} {f:.3g}" for r, f in zip(rails, shares)))
        if kind == "LDO":
            gain = 1.0
        else:
            vin = self._supply_volts(rails[0])
            gain = v_out / (vin * (eff or 1.0)) if vin else None
        if gain is not None:
            values["GAIN"] = f"{gain * shares[0]:.6g}"
        else:
            self.notes.append(f"{ref} ({rail}): {rails[0]} voltage not stated; its share of "
                              "the input current is not set")
        if iq is not None:
            values["QUIESCENT"] = f"{iq * shares[0]:g}"
        if total is None:
            out = self._corner_amps(reg, "output_current")
            total = None if out is None or gain is None else gain * out + (iq or 0.0)
        extra = []
        for (r, l), f in zip(entries[1:], shares[1:]):
            net = l.get("input_net") or r
            if total is None:
                self.notes.append(f"{ref} on {r}: input current not known; no SINK for its "
                                  f"share, so {r}'s drop is a lower bound")
            else:
                extra.append((("SINK", net), {
                    "ROLE": "SINK", "I": f"{total * f:g}", "P_NET": net, "N_NET": self.gnd,
                    "P_PINS": ",".join(_pads(ref, l["pins"])), "RAIL": r}))
            if l.get("input_net") and l["input_net"] != r:
                extra.append((("FILTER", net, r), None))
        self.extra_inputs[(ref, rail)] = extra

    def _regulators(self):
        for name, r in self.rails.items():
            src = r["source"]
            if src["role"] != "regulator":
                continue
            reg = self.regs.get(src.get("refdes", ""))
            if reg is not None and reg.get("type") == "load_switch":
                self._load_switch(reg, name, src)
                continue
            v = _number(r.get("voltage", {}).get("nominal"))
            kind = REGULATOR_TYPE.get((reg or {}).get("type"))
            vin, vin_pins, vin_rail = self._input(reg) if reg else (None, [], None)
            if reg is not None and v is not None and src.get("pins") and (
                    kind is None or vin is None):
                self._ideal_source(reg["refdes"], name, src, v, "not known whether linear or "
                                   "switching" if kind is None else "input pins not found")
                continue
            if reg is None or v is None or kind is None or vin is None:
                why = ("no regulator entry" if reg is None else "no stated voltage" if v is None
                       else "not known whether linear or switching" if kind is None
                       else "its input pins are not in the file")
                self.notes.append(f"{src.get('refdes')} ({name}): {why}; no REGULATOR, so "
                                  f"{name} is not driven")
                continue
            v_set = self._regulated_volts(r)
            if v_set != v:
                self.notes.append(f"Rail {name}: worst case, regulator {reg['refdes']} at the "
                                  f"low end of its output band {v_set:.4g} V (set {v:g} V)")
            values = {"ROLE": "REGULATOR", "V": f"{v_set:g}", "REGULATOR_TYPE": kind,
                      "OUT_P_NET": src.get("output_net") or name, "OUT_N_NET": self.gnd,
                      "IN_P_NET": vin, "IN_N_NET": self.gnd,
                      "OUT_P_PINS": ",".join(_pads(reg["refdes"], src.get("pins", []))),
                      "IN_P_PINS": ",".join(vin_pins)}
            iq = _amps(reg.get("quiescent_current"))
            eff = _number(reg.get("efficiency"))
            if reg.get("type") == "isolated":
                self._isolated(reg, name, values, eff, iq)
                self.pending_regs.append((name, reg["refdes"], values, vin_rail,
                                          src.get("output_path", [])))
                continue
            if iq is not None:
                values["QUIESCENT"] = f"{iq:g}"
            if kind == "SMPS":
                if eff is None:
                    self.notes.append(f"{reg['refdes']} ({name}): efficiency not read from its "
                                      "datasheet; FYPA's default (1.0, lossless) is used, so "
                                      "its input current is understated")
                else:
                    values["REGULATOR_EFFICIENCY"] = f"{eff:g}"
            if kind == "SMPS":
                self._freewheel(reg, name, values, v, vin_rail)
            self._split_inputs(reg, name, values, kind, v, eff, iq)
            self.pending_regs.append((name, reg["refdes"], values, vin_rail,
                                      src.get("output_path", [])))

    def _isolated(self, reg: dict, rail: str, values: dict[str, str],
                  eff: float | None, iq: float | None):
        ref = reg["refdes"]
        self.isolated.setdefault(ref, []).append(rail)
        if eff is not None:
            values["REGULATOR_EFFICIENCY"] = f"{eff:g}"
        elif iq is not None:
            values["QUIESCENT"] = f"{iq:g}"
            self.notes.append(f"{ref} ({rail}): isolated converter, efficiency not in the file; "
                              f"input = no-load {iq:g} A + Pout / Vin - losses above no-load "
                              "are not included")
        else:
            values["GAIN"] = "0"
            self.notes.append(f"{ref} ({rail}): isolated converter, neither efficiency nor "
                              "no-load input current in the file; its input current is not "
                              f"known (drawn as 0), so {values['IN_P_NET']}'s drop is a lower "
                              "bound")

    def _pick(self, typ: float | None, mx: float | None) -> float | None:
        return mx if self.corner == "max" and mx is not None else (typ if typ is not None else mx)

    def _load_switch(self, reg: dict, rail: str, src: dict):
        ref = reg["refdes"]
        vin, vin_pins, vin_rail = self._input(reg)
        out_pins = _pads(ref, src.get("pins", []))
        if vin is None or not vin_pins or not out_pins:
            self.notes.append(f"{ref} ({rail}): load switch whose "
                              f"{'input' if vin is None or not vin_pins else 'output'} pins are "
                              f"not in the file; no SERIES, so {rail} is not driven")
            return
        typ, mx = dc_resistance({"datasheet_figures": reg.get("datasheet_resistances") or []})
        r = self._pick(typ, mx)
        if r is None or r <= 0:
            self.notes.append(f"{ref}: load switch, on-resistance not in the file; bridged at "
                              "1 mOhm")
            r = 0.001
        elif self.corner == "max" and mx is None:
            self.notes.append(f"{ref}: load switch, no maximum on-resistance printed; the "
                              "typical is used in the worst case")
        series = {"ROLE": "SERIES", "R": f"{r:g}", "P_NET": vin,
                  "N_NET": src.get("output_net") or rail,
                  "P_PINS": ",".join(vin_pins), "N_PINS": ",".join(out_pins)}
        iq = _amps(reg.get("quiescent_current"))
        sink = None if iq is None else {"ROLE": "SINK", "I": f"{iq:g}", "P_NET": vin,
                                        "N_NET": self.gnd, "P_PINS": ",".join(vin_pins)}
        self.pending_switches.append((rail, ref, series, vin_rail, sink,
                                      src.get("output_path", [])))

    def _freewheel(self, reg: dict, rail: str, values: dict[str, str], v_out: float,
                   vin_rail: str | None):
        sw = reg.get("switch_node")
        ref = reg["refdes"]
        if not isinstance(sw, dict) or sw.get("rectifier") != "diode":
            return
        if sw.get("net") and sw["net"] != values["OUT_P_NET"]:
            self.notes.append(f"{ref} ({rail}): switch node {sw['net']} is not the net its "
                              f"output pins sit on ({values['OUT_P_NET']}); no freewheel diode")
            return
        fw = sw.get("freewheel_diode") or {}
        d_ref = fw.get("refdes")
        anode, cathode = fw.get("anode_pins") or [], fw.get("cathode_pins") or []
        anode_net = fw.get("anode_net")
        v_in = self._supply_volts(vin_rail)
        missing = [k for k, ok in (("refdes", d_ref), ("anode_pins", anode),
                                   ("cathode_pins", cathode), ("anode_net", anode_net),
                                   (f"{vin_rail} voltage", v_in)) if not ok]
        if missing or d_ref is None or v_in is None or anode_net is None:
            self.notes.append(f"{ref} ({rail}): non-synchronous, but the file does not give the "
                              f"freewheel diode's {', '.join(missing)}; its switch pins carry "
                              "the whole output current")
            return
        duty = v_out / v_in
        if not 0.0 < duty < 1.0:
            self.notes.append(f"{ref} ({rail}): duty cycle {v_out:g} V / {v_in:g} V is not "
                              "between 0 and 1; no freewheel diode")
            return
        if sw.get("pins"):
            values["OUT_P_PINS"] = ",".join(_pads(ref, sw["pins"]))
        self.freewheels[(ref, rail)] = (d_ref, {
            "ROLE": "FREEWHEEL", "DUTY": f"{duty:.6g}", "SMPS_HOST": ref,
            "P_NET": anode_net, "N_NET": values["OUT_P_NET"],
            "P_PINS": ",".join(_pads(d_ref, anode)),
            "N_PINS": ",".join(_pads(d_ref, cathode))})

    def _power(self):
        changed = True
        while changed:
            changed = False
            for name, r in self.rails.items():
                if name not in self.powered and r.get("parent_rail") in self.powered:
                    self.powered.add(name)
                    changed = True
            for rail, *_rest in self.pending_regs + self.pending_switches:
                vin_rail = _rest[2]
                if rail not in self.powered and vin_rail in self.powered:
                    self.powered.add(rail)
                    changed = True
        for rail, ref, series, vin_rail, sink, path in self.pending_switches:
            if rail not in self.powered:
                self.notes.append(f"{ref} ({rail}): load switch; nothing in the file drives "
                                  f"its input ({vin_rail}); no SERIES")
                continue
            self.ch.add(ref, ("SERIES", series["P_NET"], series["N_NET"]), series)
            self.notes.append(f"{ref}: load switch, a SERIES of {series['R']} Ohm from "
                              f"{series['P_NET']} to {series['N_NET']} (not a regulator)")
            if sink is not None:
                self.ch.add(ref, ("SINK", sink["P_NET"]), sink)
            if series["P_NET"] != vin_rail:
                self.reg_inputs.append((ref, series["P_NET"], vin_rail))
            for e in path:
                if e.get("net_in") and e.get("net_out"):
                    self._series(e)
        for rail, ref, values, vin_rail, path in self.pending_regs:
            if rail not in self.powered:
                self.notes.append(f"{ref} ({rail}): nothing in the file drives its input "
                                  f"({vin_rail}); no REGULATOR")
                continue
            out_key = values["OUT_P_PINS"] or rail
            if self.ch.add(ref, ("REGULATOR", out_key), values):
                self.reg_outputs[(ref, out_key)] = rail
                for key, sink in self.extra_inputs.get((ref, rail), []):
                    if sink is None:
                        self.reg_inputs.append((ref, key[1], key[2]))
                    elif sink["RAIL"] in self.powered:
                        self.ch.add(ref, key, {k: v for k, v in sink.items() if k != "RAIL"})
                    else:
                        self.notes.append(f"{ref} on {sink['RAIL']}: nothing in the file drives "
                                          "that input rail; its share is not drawn")
                fw = self.freewheels.get((ref, rail))
                if fw is not None:
                    d_ref, fw_values = fw
                    self.ch.add(d_ref, ("FREEWHEEL", ref, rail), fw_values)
                    self.notes.append(f"{d_ref}: freewheel diode of {ref}; its switch pins carry "
                                      f"D = {fw_values['DUTY']} of {rail}'s current, {d_ref} the rest")
                if values["IN_P_NET"] != vin_rail:
                    self.reg_inputs.append((ref, values["IN_P_NET"], vin_rail))
            else:
                first_rail = self.reg_outputs[(ref, out_key)]
                self.notes.append(f"{ref} ({rail}): the same output pins as its REGULATOR for "
                                  f"{first_rail}; one element, {rail} reached through its "
                                  "output path")
            for e in path:
                if not (e.get("net_in") and e.get("net_out")):
                    continue
                if values["REGULATOR_TYPE"] == "SMPS":
                    self.notes.append(f"{e['refdes']}: inside {ref}'s regulation loop; bridged "
                                      "(its loss is in the efficiency)")
                    self.ch.add(e["refdes"], ("SERIES", e["net_in"], e["net_out"]), {
                        "ROLE": "SERIES", "R": "0.001", "P_NET": e["net_in"],
                        "N_NET": e["net_out"]})
                    continue
                self._series(e)

    def _series(self, e: dict):
        ref = e["refdes"]
        typ, mx = dc_resistance(e)
        r = mx if self.corner == "max" and mx is not None else (typ if typ is not None else mx)
        if e.get("class") == "diode":
            vf, vf_max, slope, how = forward_drop(e)
            values = {"ROLE": "SERIES", "R": f"{max(slope, 0.001):g}", "P_NET": e["net_in"],
                      "N_NET": e["net_out"]}
            if vf is None:
                self.notes.append(f"{ref}: series diode with no forward voltage in the file "
                                  "(bridged at 1 mOhm); the rail after it reads one forward "
                                  "drop high")
            else:
                pick = vf_max if self.corner == "max" and vf_max is not None else vf
                values["VF"] = f"{pick:g}"
                curve = curve_points(e)
                if curve:
                    values["VF_CURVE"] = ";".join(f"{i:g}:{v + pick - vf:g}"
                                                  for i, v in curve)
                self.notes.append(f"{ref}: series diode, {how}")
            self.ch.add(ref, ("SERIES", e.get("net_in"), e.get("net_out")), values)
            return
        if r is None:
            self.notes.append(f"{ref}: DC resistance not in the file; bridged at 1 mOhm")
            r = 0.001
        if r <= 0:
            return
        self.ch.add(ref, ("SERIES", e.get("net_in"), e.get("net_out")), {
            "ROLE": "SERIES", "R": f"{r:g}", "P_NET": e["net_in"], "N_NET": e["net_out"]})

    def _branches(self):
        for r in self.rails.values():
            if r["name"] not in self.powered:
                continue
            for e in r.get("series_from_parent", []):
                if e.get("net_in") and e.get("net_out"):
                    self._series(e)

    def _loads(self):
        not_supplied = []
        for name, r in self.rails.items():
            if name not in self.powered:
                drawn = [l["refdes"] for l in r.get("loads", []) if l["role"] != "regulator"]
                if drawn:
                    self.notes.append(f"Rail {name}: nothing in the file drives it; its loads "
                                      f"are left out: {', '.join(drawn)}")
                continue
            for l in r.get("loads", []):
                if l["role"] == "regulator":
                    continue
                current = l.get("current")
                if isinstance(current, dict) and current.get("basis") in NOT_A_LOAD:
                    not_supplied.append(f"{l['refdes']} ({name})")
                    continue
                self._sink(name, l)
            for pin in r.get("offboard_pins", []):
                self.notes.append(f"{name}: power leaves the board at {pin}; that load is not "
                                  "in the file, so the rail's drop is a lower bound")
        if not_supplied:
            self.notes.append("On a rail by a detect, enable or reference pin, not a supply pin, "
                              "so no SINK: " + ", ".join(not_supplied))

    def _sink_current(self, l: dict) -> float | None:
        pick = self._corner_or_max(l, "current")
        if l.get("load_dependent"):
            if pick is not None:
                self.notes.append(f"{l['refdes']}: draws what it drives "
                                  f"({l.get('load_dependent_reason', 'no reason given')}); its "
                                  f"own current {pick * 1e6:.4g} uA is used, so the rail's drop "
                                  "is a lower bound")
            return pick
        if pick is not None and l["role"] == "ic_with_regulator":
            reg = self._corner_or_max(l, "regulator_input")
            pick = None if reg is None else pick + reg
        return pick

    def _corner_or_max(self, l: dict, key: str) -> float | None:
        mx = _amps(l.get(f"{key}_max"))
        pick = (mx if self.corner == "max" else None) or _amps(l.get(key))
        if pick is None and mx is not None:
            self.notes.append(f"{l['refdes']}: only a maximum {key.replace('_', ' ')} is "
                              f"known; {mx * 1e3:g} mA is used")
            pick = mx
        return pick

    def _sink(self, rail: str, l: dict):
        for e in l.get("input_filter") or []:
            if e.get("net_in") and e.get("net_out"):
                self._series(e)
        amps = self._sink_current(l)
        if amps is None:
            why = l.get("load_dependent_reason") if l.get("load_dependent") else "current not known"
            self.notes.append(f"{l['refdes']} on {rail}: {why}; no SINK, so {rail}'s drop is a "
                              "lower bound")
            return
        net = l.get("input_net") or rail
        pins = ",".join(_pads(l["refdes"], l.get("pins", [])))
        values: dict[str, str] = {"ROLE": "SINK", "I": f"{amps:g}", "P_NET": net,
                                  "N_NET": self.gnd, "P_PINS": pins}
        driver = l.get("driver")
        if not isinstance(driver, dict):
            driver = {}
        drv_ref = driver.get("refdes")
        drv_return = _pads(drv_ref, driver.get("return_pins") or []) if drv_ref else []
        if l["role"] == "motor_output":
            supply = _pads(drv_ref, driver.get("supply_pins") or []) if drv_ref else []
            if not supply or not drv_ref:
                self.notes.append(f"{l['refdes']} on {rail}: motor with no driver supply pins in "
                                  f"the file; no SINK, so {rail}'s drop is a lower bound")
                return
            values["P_PINS"] = ",".join(supply)
            if drv_return:
                values["N_PINS"] = ",".join(drv_return)
            self.ch.add(drv_ref, ("SINK", net, l["refdes"]), values)
            return
        if l["role"] == "resistive_path" and drv_ref and drv_return:
            values["N_DES"] = drv_ref
            values["N_PINS"] = ",".join(drv_return)
        elif l["role"] in ("resistive_path", "pull_resistor"):
            ret = _return_part(l)
            if ret is None:
                self.notes.append(f"{l['refdes']} on {rail}: {l['role'].replace('_', ' ')} "
                                  "with no part listed it returns through; no SINK")
                return
            values["N_DES"] = ret
        sm = l.get("supply_min")
        if isinstance(sm, dict) and _number(sm.get("volts")) is not None:
            values["MIN_V"] = f"{_number(sm['volts']):g}"
        self._out_before_copper(rail, l)
        self.ch.add(l["refdes"], ("SINK", net), values)
        if l["role"] == "pull_resistor":
            self.pull_sinks[l["refdes"].upper()] = rail

    def _out_before_copper(self, rail: str, l: dict):
        cm = l.get("copper_margin")
        if not isinstance(cm, dict):
            return
        low, high = _number(cm.get("v")), _number(cm.get("high_room_v"))
        if low is not None and low < 0:
            self.notes.append(f"{l['refdes']} on {rail}: {-low * 1e3:.4g} mV below its minimum "
                              "supply before any copper (its regulator's output band against "
                              "its datasheet)")
        if high is not None and high < 0:
            self.notes.append(f"{l['refdes']} on {rail}: {-high * 1e3:.4g} mV above its maximum "
                              "supply at its rail's highest output")

    def series_elements(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for r in self.rails.values():
            parts = r.get("series_from_parent", []) + (r.get("source") or {}).get("output_path", [])
            for l in r.get("loads", []):
                parts = parts + (l.get("input_filter") or [])
            for e in parts:
                out.setdefault(e.get("refdes", ""), e)
        return out

    def place_on_board(self, params: dict[str, dict[str, str]], extracted) -> list[str]:
        notes: list[str] = []
        pads = defaultdict(set)
        for pad in extracted.pads:
            if pad.component_index < 0:
                continue
            comp = extracted.pcb_components[pad.component_index]
            des = (comp.source_designator or comp.designator).upper()
            pads[des].add(extracted.net_name(pad.net_index))
        grounds = set(self.grounds)
        known = {k.upper(): v for k, v in self.series_elements().items()}

        def ohms(des: str) -> tuple[float, str]:
            e = known.get(des)
            if e is not None:
                typ, mx = dc_resistance(e)
                r = mx if self.corner == "max" and mx is not None else (typ if typ is not None else mx)
                if r is not None and r > 0:
                    return r, ""
            return 0.001, f"{des}: DC resistance not in the file; bridged at 1 mOhm (above the 0.9 mOhm net-merge threshold)"

        def add(des: str, values: dict[str, str]):
            ch = params.setdefault(des, {})
            used = [int(k[3:].split("_", 1)[0]) for k in ch
                    if k.startswith("PDN") and k[3:].split("_", 1)[0].isdigit()]
            n = 1 + max(used or [0])
            for k, v in values.items():
                ch[f"PDN{n}_{k}"] = v

        for des, ch in list(params.items()):
            for k, v in list(ch.items()):
                if not (k.endswith("_ROLE") and v == "SOURCE"):
                    continue
                nk = k[:-len("ROLE")] + "N_NET"
                on = pads.get(ch.get(k[:-len("ROLE")] + "N_DES", des).upper(), set())
                if ch.get(nk) in on:
                    if ch[nk] == self.gnd or des.upper() in self.closed_returns:
                        continue
                    ret = [ch[nk]]
                else:
                    ret = sorted(on & grounds)
                    if not ret:
                        notes.append(f"{des}: no pad on any ground net of the file; return left as {ch.get(nk)}")
                        continue
                    ch[nk] = ret[0]
                    notes.append(f"{des}: source return is {ret[0]} (its pads), not {self.gnd}")
                if ret[0] == self.gnd:
                    continue
                choke = [c for c, nets in pads.items()
                         if c in known and ret[0] in nets and self.gnd in nets]
                if not choke:
                    notes.append(f"{des}: nothing the file lists joins {ret[0]} to {self.gnd}; "
                                 "the return is not closed")
                    continue
                r, why = ohms(choke[0])
                if why:
                    notes.append(why)
                add(choke[0], {"ROLE": "SERIES", "R": f"{r:g}", "P_NET": ret[0], "N_NET": self.gnd})
                notes.append(f"{choke[0]}: return winding {ret[0]} -> {self.gnd}, {r:g} Ohm")

        def passive(des: str) -> bool:
            return des[:1] in ("L", "R", "C", "F") or des.startswith("FB")

        edges = defaultdict(list)
        for des, nets in pads.items():
            if not passive(des):
                continue
            sig = sorted(n for n in nets if n not in grounds)
            if len(sig) == 2:
                edges[sig[0]].append((des, sig[1]))
                edges[sig[1]].append((des, sig[0]))
        placed = set()
        for ref, pin_net, rail in self.reg_inputs:
            prev = {pin_net: None}
            frontier = [pin_net]
            for _ in range(4):
                nxt = []
                for n in frontier:
                    for des, other in edges.get(n, []):
                        if other not in prev:
                            prev[other] = (des, n)
                            nxt.append(other)
                frontier = nxt
                if rail in prev:
                    break
            if rail not in prev:
                notes.append(f"{ref}: no passive path on the board from its input pins ({pin_net}) "
                             f"to {rail}")
                continue
            n = rail
            while (step := prev[n]) is not None:
                des, back = step
                if (des, n, back) in placed:
                    n = back
                    continue
                for par in [des] + sorted(d for d, other in edges.get(n, [])
                                          if other == back and d != des and d in known):
                    if (par, n, back) in placed and par != des:
                        continue
                    placed.add((par, n, back))
                    r, why = ohms(par)
                    if why:
                        notes.append(why)
                    add(par, {"ROLE": "SERIES", "R": f"{r:g}", "P_NET": n, "N_NET": back})
                    notes.append(f"{ref}: input filter {par} {n} -> {back}")
                n = back
        notes.extend(self._isolated_grounds(params, pads))
        notes.extend(self._unknown_pull_returns(params, pads))
        return notes

    def _unknown_pull_returns(self, params: dict[str, dict[str, str]], pads) -> list[str]:
        notes: list[str] = []
        for des, ch in params.items():
            rail = self.pull_sinks.get(des.upper())
            if rail is None:
                continue
            for k, role in list(ch.items()):
                if not (k.endswith("_ROLE") and role == "SINK"):
                    continue
                pre = k[:-len("ROLE")]
                ret, gnd = ch.get(pre + "N_DES", des), ch.get(pre + "N_NET")
                if gnd in pads.get(ret.upper(), set()):
                    continue
                for key in [x for x in ch if x.startswith(pre)]:
                    del ch[key]
                notes.append(f"{des}: pulls {ret}, which has no pad on {gnd}; its current "
                             f"returns through what drives {ret}, not known, so no SINK and "
                             f"{rail}'s drop is a lower bound")
        for des in [d for d, ch in params.items() if not ch]:
            del params[des]
        return notes

    def _isolated_domain(self, ref: str) -> set:
        rails = set(self.isolated.get(ref, []))
        changed = True
        while changed:
            changed = False
            for name, r in self.rails.items():
                if name not in rails and r.get("parent_rail") in rails:
                    rails.add(name)
                    changed = True
            for g in self.regs.values():
                if g["refdes"] in self.isolated or not set(g.get("input_rails", [])) & rails:
                    continue
                new = set(g.get("output_rails", [])) - rails
                if new:
                    rails |= new
                    changed = True
        nets = set(rails)
        for name in rails:
            r = self.rails.get(name, {})
            if (r.get("source") or {}).get("output_net"):
                nets.add(r["source"]["output_net"])
            nets |= {l["input_net"] for l in r.get("loads", []) if l.get("input_net")}
        return nets

    def _isolated_grounds(self, params: dict[str, dict[str, str]], pads) -> list[str]:
        notes: list[str] = []
        others = set(self.grounds) - {self.gnd}
        for ref in sorted(self.isolated):
            stated = {self.rails.get(rail, {}).get("ground") for rail in self.isolated[ref]}
            iso = sorted(stated & others & pads.get(ref.upper(), set()))
            if len(iso) != 1:
                iso = sorted(pads.get(ref.upper(), set()) & others)
            if len(iso) != 1:
                loads = {(_return_part(l) or l["refdes"]).upper() for rail in self.isolated[ref]
                         for l in self.rails.get(rail, {}).get("loads", [])}
                shared = set.intersection(*[pads.get(d, set()) & others for d in loads])                     if loads else set()
                iso = sorted(shared)
            if len(iso) != 1:
                notes.append(f"{ref}: isolated converter, its output-side ground was not found "
                             f"on the board ({', '.join(iso) or 'none'}); its output returns "
                             f"on {self.gnd}")
                continue
            gnd, domain = iso[0], self._isolated_domain(ref)
            notes.append(f"{ref}: isolated converter, output side returns on {gnd}")
            for des, ch in params.items():
                for k, role in list(ch.items()):
                    if not k.endswith("_ROLE"):
                        continue
                    pre = k[:-len("ROLE")]
                    sides = (("P_NET", "N_NET"),) if role == "SINK" else                         (("OUT_P_NET", "OUT_N_NET"), ("IN_P_NET", "IN_N_NET"))                         if role == "REGULATOR" else ()
                    for pk, nk in sides:
                        if ch.get(pre + pk) not in domain:
                            continue
                        ret_des = ch.get(pre + "N_DES", des) if pk == "P_NET" else des
                        if gnd not in pads.get(ret_des.upper(), set()):
                            notes.append(f"{des}: on {ch[pre + pk]} (powered by {ref}) but "
                                         f"{ret_des} has no pad on {gnd}; return left on "
                                         f"{ch.get(pre + nk)}")
                            continue
                        ch[pre + nk] = gnd
        return notes

    def parameters(self) -> tuple[dict[str, dict[str, str]], list[str]]:
        self._sources()
        self._regulators()
        self._power()
        self._branches()
        self._loads()
        self._not_fitted()
        self._switched_paths()
        return dict(self.ch.params), self.notes

    def _switched_paths(self):
        for sp in self.pdn.get("switched_paths") or []:
            what = f"Switched path {sp.get('net') or '?'}" + (
                f" ({sp['switched_by']})" if sp.get("switched_by") else "")
            inp, out, ret = sp.get("in"), sp.get("out"), sp.get("return")
            if sp.get("contact_only") or not isinstance(inp, dict) or not isinstance(out, dict):
                self.notes.append(f"{what}: contacts only, no source or load side on the "
                                  "board; not solved")
                continue
            if not isinstance(ret, dict) or not ret.get("terminals"):
                self.notes.append(f"{what}: the file names no return; not solved")
                continue
            volts = sp.get("voltage") if isinstance(sp.get("voltage"), dict) else {}
            v, amps = _number(volts.get("nominal")), _amps(sp.get("load_current_max"))
            if v is None or amps is None:
                self.notes.append(f"{what}: its {'voltage' if v is None else 'load current'} "
                                  "is not stated; not solved")
                continue
            src, dst = (inp.get("terminals") or [None])[0], (out.get("terminals") or [None])[0]
            if not src or not dst:
                self.notes.append(f"{what}: no terminal named at its "
                                  f"{'input' if not src else 'output'}; not solved")
                continue
            self.ch.add(src, ("SOURCE", inp["net"]), {
                "ROLE": "SOURCE", "V": f"{v:g}", "P_NET": inp["net"], "N_NET": ret["net"],
                "N_DES": ret["terminals"][0]})
            self.closed_returns.add(src.upper())
            for e in sp.get("series") or []:
                if e.get("net_in") and e.get("net_out"):
                    self._series(e)
            self.ch.add(dst, ("SINK", out["net"]), {
                "ROLE": "SINK", "I": f"{amps:g}", "P_NET": out["net"], "N_NET": ret["net"],
                "N_DES": ret["terminals"][-1]})
            self.notes.append(f"{what}: {inp['net']} -> {out['net']} at {v:g} V, {amps:g} A "
                              f"load, returning on {ret['net']} (the designer's figures)")

    def _not_fitted(self):
        opened, left = [], []
        for e in self.pdn.get("not_fitted") or []:
            ref, nets = e.get("refdes", ""), e.get("nets") or []
            if not ref:
                continue
            model = e.get("model_as") if isinstance(e.get("model_as"), dict) else None
            ohms = (_number(model.get("resistance_ohm")) if model is not None
                    else None if e.get("class") in OPEN_CLASSES else NOT_FITTED_OHMS)
            if "model_as" in e and model is None:
                ohms = None
            if ohms is None or len(nets) != 2 or nets[0] == nets[1]:
                left.append(ref)
                continue
            self.ch.add(ref, ("SERIES", nets[0], nets[1]), {"ROLE": "SERIES", "R": f"{ohms:g}"})
            opened.append(ref)
        if opened:
            self.notes.append(f"Not fitted, solved as open ({NOT_FITTED_OHMS:g} Ohm): "
                              + ", ".join(opened))
        if left:
            self.notes.append("Not fitted, left out: " + ", ".join(left))


def read_pdn_parameters(path, corner: str = "typ") -> tuple[dict[str, dict[str, str]], list[str]]:
    pdn = json.loads(Path(path).read_text(encoding="utf-8"))
    return TomachiePdn(pdn, corner).parameters()


def read_pdn_for_board(path, corner: str, extracted) -> tuple[dict[str, dict[str, str]], list[str]]:
    pdn = json.loads(Path(path).read_text(encoding="utf-8"))
    reader = TomachiePdn(pdn, corner)
    params, notes = reader.parameters()
    return params, notes + reader.place_on_board(params, extracted)


def _has_pdn(params: dict[str, str]) -> bool:
    return any(k.upper().startswith("PDN") for k in params)


def apply_to_project(extracted, params: dict[str, dict[str, str]]):
    notes: list[str] = []
    wanted = {d.upper(): (d, p) for d, p in params.items()}
    placed = set()
    comps = []
    for comp in extracted.sch_components:
        hit = wanted.get(comp.designator.upper())
        if hit is None:
            comps.append(comp)
            continue
        placed.add(comp.designator.upper())
        if _has_pdn(comp.parameters):
            notes.append(f"{comp.designator}: keeps its own schematic PDN_* parameters; the "
                         "file's are not applied")
            comps.append(comp)
            continue
        comps.append(replace(comp, parameters={**comp.parameters, **hit[1]}))
    for des_u, (des, _) in wanted.items():
        if des_u not in placed:
            notes.append(f"{des}: in the file but not in the schematic")
    return replace(extracted, sch_components=tuple(comps)), notes


def applied_designators(params: dict[str, dict[str, str]], extracted) -> set:
    own = {c.designator.upper() for c in extracted.sch_components if _has_pdn(c.parameters)}
    return {d.upper() for d in params} - own
