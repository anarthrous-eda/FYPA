"""Worst-case supply voltages, derived from the one solve at nominal.

A SOURCE or REGULATOR output may carry a range (``v_min`` / ``v_max``, from
``PDN_V_TOL`` or ``PDN_V_MIN`` / ``PDN_V_MAX``). The solve still runs at the
nominal voltage; the checks then move its result to the ends of the range:

* **Low** - sinks draw a constant current, so lowering a supply by ΔV lowers
  every node on its rail by exactly ΔV and changes no current. The lowest a
  load can see is its solved voltage minus ``nominal - v_min``. This carries
  through series parts (their drop depends on current alone) and stops at a
  regulator, which holds its own output.
* **High** - the highest voltage is at light load, where nothing drops: the
  setpoint (or the solved voltage where remote sense lifts it above the
  setpoint) plus ``v_max - nominal``.

With several supplies on one rail the true shift is a weighted mean of
theirs, so the largest one is a safe bound.

Not modelled: an SMPS draws more input current when its input is low or its
output high, which adds roughly the tolerance's share to the upstream drop
that regulator causes (5 % tolerance -> about 5 % more of that drop).

Qt-free and solve-free: everything here reads the metadata directive dicts.
"""
from __future__ import annotations

from dataclasses import dataclass

from fypa.rail_currents import pin_nets, to_float

# SMPS topologies whose input need not sit above the output, so a dropout
# (input headroom over the output) says nothing about them.
NO_DROPOUT_TOPOLOGIES: frozenset[str] = frozenset({
    "BOOST", "BUCKBOOST", "INVERTER",
})


@dataclass(frozen=True)
class RailRange:
    """How far a rail's supplies can move it off the solved (nominal) result."""
    nominal: float       # the rail's setpoint: its highest supply voltage
    low: float           # volts the rail can sit below the solved result
    high: float          # volts above
    toleranced: bool     # a supply on the rail gave a range

    @property
    def v_min(self) -> float:
        return self.nominal - self.low

    @property
    def v_max(self) -> float:
        return self.nominal + self.high


def supply_shift(d: dict) -> tuple[float, float]:
    """``(low, high)`` volts a SOURCE / REGULATOR output can be off nominal."""
    nominal = to_float(d.get("value"))
    if nominal is None:
        return 0.0, 0.0
    v_min = to_float(d.get("v_min"))
    v_max = to_float(d.get("v_max"))
    return (max(0.0, nominal - v_min) if v_min is not None else 0.0,
            max(0.0, v_max - nominal) if v_max is not None else 0.0)


def rail_ranges(directives: list[dict],
                rail_to_members: dict[str, list[str]]) -> dict[str, RailRange]:
    """A :class:`RailRange` for every rail a SOURCE / REGULATOR drives."""
    net_to_rail = {n: rail for rail, members in rail_to_members.items()
                   for n in members}
    out: dict[str, RailRange] = {}
    for d in directives:
        term = {"SOURCE": "P", "REGULATOR": "OUT_P"}.get(d.get("role"))
        nominal = to_float(d.get("value"))
        if term is None or nominal is None:
            continue
        rail = next((net_to_rail[n] for n in sorted(pin_nets(d, term))
                     if n in net_to_rail), None)
        if rail is None:
            continue
        low, high = supply_shift(d)
        toleranced = d.get("v_min") is not None or d.get("v_max") is not None
        prev = out.get(rail)
        if prev is not None:
            nominal = max(nominal, prev.nominal)
            low, high = max(low, prev.low), max(high, prev.high)
            toleranced = toleranced or prev.toleranced
        out[rail] = RailRange(nominal, low, high, toleranced)
    return out


def lowest_voltage(v: float, rng: RailRange | None) -> float:
    """The solved voltage ``v`` with the rail's supply at its minimum."""
    return v if rng is None else v - rng.low


def highest_voltage(v: float, rng: RailRange | None) -> float:
    """The most a point solved at ``v`` can see: the rail lightly loaded and
    its supply at its maximum."""
    if rng is None:
        return v
    return max(v, rng.nominal) + rng.high


def regulator_input_minimum(d: dict) -> tuple[float, str] | None:
    """``(volts, why)`` - the lowest input a REGULATOR directive accepts, or
    ``None`` when it declares neither a dropout nor a minimum input.

    The dropout is taken above the output's *maximum*: a regulator sitting
    at the top of its range needs the most input.
    """
    cands: list[tuple[float, str]] = []
    v_out = to_float(d.get("value"))
    dropout = to_float(d.get("dropout"))
    topo = str(d.get("smps_topology") or "").upper()
    if (dropout is not None and v_out is not None and v_out > 0
            and topo not in NO_DROPOUT_TOPOLOGIES):
        v_hi = to_float(d.get("v_max"))
        v_hi = v_out if v_hi is None else max(v_hi, v_out)
        cands.append((v_hi + dropout,
                      f"{v_hi:.4g} V output + {dropout * 1e3:.4g} mV dropout"))
    vin_min = to_float(d.get("vin_min"))
    if vin_min is not None:
        cands.append((vin_min, "PDN_VIN_MIN"))
    return max(cands, key=lambda c: c[0]) if cands else None


def tolerance_text(d: dict) -> str:
    """The range of a SOURCE / REGULATOR as a short tolerance for a label:
    ``"±5%"`` or ``"+3/−5%"``; ``""`` when it has none (or a 0 V nominal)."""
    nominal = to_float(d.get("value"))
    if not nominal or (d.get("v_min") is None and d.get("v_max") is None):
        return ""
    low, high = supply_shift(d)
    lo_pct = 100.0 * low / abs(nominal)
    hi_pct = 100.0 * high / abs(nominal)
    if abs(lo_pct - hi_pct) < 5e-3:
        return f"±{hi_pct:.3g}%"
    return f"+{hi_pct:.3g}/−{lo_pct:.3g}%"


def limit_lines(d: dict) -> list[str]:
    """One line per check limit a directive carries, for tooltips."""
    role = d.get("role")
    lines: list[str] = []
    if role in ("SOURCE", "REGULATOR"):
        rng = range_text(d)
        if rng:
            lines.append(f"range {rng} ({tolerance_text(d)})"
                         if tolerance_text(d) else f"range {rng}")
    if role == "SINK":
        lo, hi = to_float(d.get("min_voltage")), to_float(d.get("max_voltage"))
        if lo is not None:
            lines.append(f"min {lo:.4g} V")
        if hi is not None:
            lines.append(f"max {hi:.4g} V")
    if role == "REGULATOR":
        need = regulator_input_minimum(d)
        if need is not None:
            lines.append(f"input min {need[0]:.4g} V ({need[1]})"
                         if need[1] != "PDN_VIN_MIN"
                         else f"input min {need[0]:.4g} V")
        vin_max = to_float(d.get("vin_max"))
        if vin_max is not None:
            lines.append(f"input max {vin_max:.4g} V")
    return lines


def range_text(d: dict) -> str:
    """``"3.135-3.465 V"`` for a SOURCE / REGULATOR with a range, else ``""``.
    A side that was not given shows the nominal."""
    nominal = to_float(d.get("value"))
    if nominal is None or (d.get("v_min") is None and d.get("v_max") is None):
        return ""
    low, high = supply_shift(d)
    return f"{nominal - low:.4g}–{nominal + high:.4g} V"
