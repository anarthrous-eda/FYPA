"""A series diode solved on its datasheet's forward-voltage curve (``PDN_VF_CURVE``).

FYPA stamps a diode as a resistor with ``Vf / R`` carried back across it: the straight
line V = Vf + I x R. A Schottky's curve is far from straight over its range: a line
fitted from 2 A to 20 A reads about 0.27 V high at 80 mA. A diode with a curve has its
line moved, between solves, onto the curve's tangent at the current it carried: R the
curve's slope there and Vf what puts the line on the curve. Its resistor and current
source are patched in the built problem, which keeps its nodes, so nothing is rebuilt;
the solve repeats until the line meets the curve at the solved current.

The current comes from the voltage across the diode's pads less its forward drop, over
its slope. On the tangent the slope is the diode's own (0.6 ohm at 80 mA on an SK510A),
so the pad voltages fix the current well.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Callable, Sequence

from pdnsolver import problem as _pp

# A tangent never goes below this: the stamp divides by R.
MIN_SLOPE_OHM = 1e-3
# Converged when every diode's line is within this of its curve at its current.
CURVE_TOL_V = 1e-4
OFF_OHM = 1e6


def _points(curve: Sequence[Sequence[float]]) -> list[tuple[float, float]]:
    pts: dict[float, float] = {}
    for i, v in curve:
        if float(i) > 0:
            pts.setdefault(float(i), float(v))
    return sorted(pts.items())


def _segment(pts: list[tuple[float, float]], amps: float) -> int:
    """The index k of the segment pts[k-1]..pts[k] holding ``amps`` (the end segments
    beyond the curve's ends)."""
    return next((n for n in range(1, len(pts)) if pts[n][0] >= amps), len(pts) - 1)


def curve_volts(curve: Sequence[Sequence[float]], amps: float) -> float:
    """The curve's forward voltage at ``amps``: straight lines in log current between its
    points (a diode's voltage grows with the log of its current), and beyond its ends the
    line through its last two points the same way."""
    pts = _points(curve)
    if len(pts) == 1:
        return pts[0][1]
    k = _segment(pts, amps)
    (i1, v1), (i2, v2) = pts[k - 1], pts[k]
    x = math.log(max(amps, 1e-12))
    return max(v1 + (v2 - v1) * (x - math.log(i1)) / (math.log(i2) - math.log(i1)), 0.0)


def tangent(curve: Sequence[Sequence[float]], amps: float) -> tuple[float, float]:
    """(Vf, R) of the line touching the curve at ``amps``: dV/dI there, and the drop
    that puts the line through the curve's point."""
    pts = _points(curve)
    if len(pts) == 1:
        return pts[0][1], MIN_SLOPE_OHM
    k = _segment(pts, amps)
    (i1, v1), (i2, v2) = pts[k - 1], pts[k]
    slope = max((v2 - v1) / (math.log(i2) - math.log(i1)) / max(amps, 1e-12),
                MIN_SLOPE_OHM)
    return curve_volts(curve, amps) - slope * amps, slope


def has_curve(d) -> bool:
    """Whether a directive is a SERIES diode that carries a curve."""
    return bool(getattr(d, "forward_v", None) and getattr(d, "vf_curve", None))


def _mean(values: list) -> float | None:
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def stamp_current(spec) -> float:
    """The Vf / R current carried back across a diode's resistor; none when it is open."""
    if getattr(spec, "reverse_open", False):
        return 0.0
    return spec.forward_v / spec.resistance


def _patch(network: _pp.Network, spec) -> bool:
    """Put the spec's resistance and forward drop into its built network: the main
    resistor (first element) and the current source carrying Vf / R back across it."""
    main = network.elements[0]
    if not isinstance(main, _pp.Resistor):
        return False
    for n, e in enumerate(network.elements):
        if isinstance(e, _pp.CurrentSource) and e.f is main.b and e.t is main.a:
            network.elements[n] = dataclasses.replace(e, current=stamp_current(spec))
            network.elements[0] = dataclasses.replace(main, resistance=spec.resistance)
            return True
    return False


def is_diode(d) -> bool:
    return bool(getattr(d, "forward_v", None))


def _on_specs(loaded) -> dict:
    return loaded.__dict__.setdefault("diode_on_specs", {})


def assess(solution, loaded, networks: dict, label: Callable,
           sample: Callable) -> tuple[float, dict, list]:
    gap, solved, moves = 0.0, {}, []
    on_specs = _on_specs(loaded)
    for n, d in enumerate(loaded.annotations.directives):
        net = networks.get(label(d)) if is_diode(d) else None
        if net is None:
            continue
        vp = _mean([sample(solution, loaded, p) for p in d.p.pins])
        vn = _mean([sample(solution, loaded, p) for p in d.n.pins])
        if vp is None or vn is None:
            continue
        key = label(d)
        volts = vp - vn
        if key in on_specs:
            on = on_specs[key]
            if volts - on.forward_v > CURVE_TOL_V:
                gap = max(gap, volts - on.forward_v)
                moves.append((n, net, on, key, True))
            continue
        if volts - d.forward_v < -CURVE_TOL_V:
            gap = max(gap, d.forward_v - volts)
            moves.append((n, net, dataclasses.replace(d, resistance=OFF_OHM,
                                                         reverse_open=True), key, False))
            continue
        amps = (volts - d.forward_v) / d.resistance
        if not has_curve(d) or not amps > 0:
            continue
        on_curve = curve_volts(d.vf_curve, amps)
        gap = max(gap, abs(on_curve - (d.forward_v + d.resistance * amps)))
        vf, r = tangent(d.vf_curve, amps)
        solved[key] = (amps, on_curve)
        moves.append((n, net, dataclasses.replace(d, forward_v=max(vf, 0.0) or 1e-6,
                                                  resistance=r), key, None))
    return gap, solved, moves


def advance(loaded, moves: list) -> None:
    on_specs = _on_specs(loaded)
    directives = list(loaded.annotations.directives)
    for n, net, new, key, turn_on in moves:
        if not _patch(net, new):
            continue
        if turn_on is False:
            on_specs[key] = directives[n]
        elif turn_on is True:
            on_specs.pop(key, None)
        directives[n] = new
    loaded.annotations.directives = directives


def off(loaded) -> list[str]:
    return sorted(loaded.__dict__.get("diode_on_specs", {}))


def retune(solution, loaded, networks: dict, label: Callable,
           sample: Callable) -> tuple[float, dict]:
    gap, solved, moves = assess(solution, loaded, networks, label, sample)
    advance(loaded, moves)
    return gap, solved
