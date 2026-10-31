# Copyright (c) 2026 Tomachie LLC.
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


def _points(curve: Sequence[Sequence[float]]) -> list[tuple[float, float]]:
    return sorted((float(i), float(v)) for i, v in curve if float(i) > 0)


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


def _patch(network: _pp.Network, spec) -> bool:
    """Put the spec's resistance and forward drop into its built network: the main
    resistor (first element) and the current source carrying Vf / R back across it."""
    main = network.elements[0]
    if not isinstance(main, _pp.Resistor):
        return False
    for n, e in enumerate(network.elements):
        if isinstance(e, _pp.CurrentSource) and e.f is main.b and e.t is main.a:
            network.elements[n] = dataclasses.replace(
                e, current=spec.forward_v / spec.resistance)
            network.elements[0] = dataclasses.replace(main, resistance=spec.resistance)
            return True
    return False


def retune(solution, loaded, networks: dict, label: Callable,
           sample: Callable) -> tuple[float, dict]:
    """Move each curve diode's line onto its curve's tangent at the current it carried
    in ``solution``: its spec in ``loaded`` and its elements in ``networks`` (label ->
    network). Returns (the largest gap between a line and its curve at its current,
    volts; {label: (amps, volts on the curve)})."""
    gap, solved = 0.0, {}
    updated = []
    for d in loaded.annotations.directives:
        net = networks.get(label(d)) if has_curve(d) else None
        if net is None:
            updated.append(d)
            continue
        vp = _mean([sample(solution, loaded, p) for p in d.p.pins])
        vn = _mean([sample(solution, loaded, p) for p in d.n.pins])
        amps = None if vp is None or vn is None else (vp - vn - d.forward_v) / d.resistance
        if amps is None or not amps > 0:
            updated.append(d)              # not conducting, or not sampled: as it is
            continue
        on_curve = curve_volts(d.vf_curve, amps)
        gap = max(gap, abs(on_curve - (d.forward_v + d.resistance * amps)))
        vf, r = tangent(d.vf_curve, amps)
        new = dataclasses.replace(d, forward_v=max(vf, 0.0) or 1e-6, resistance=r)
        if not _patch(net, new):
            updated.append(d)
            continue
        solved[label(d)] = (amps, on_curve)
        updated.append(new)
    loaded.annotations.directives = updated
    return gap, solved
