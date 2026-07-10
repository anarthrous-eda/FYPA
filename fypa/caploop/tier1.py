"""Tier 1 — closed-form mounted-inductance model.

Pure geometry over :class:`~fypa.caploop.identify.CapInstance`; no solve, so
the Capacitors tab can populate on project load. The mounted loop decomposes
per TI SWPA222A §3 / Fig. 6:

* **escape** — each pad's run to its escape vias, modelled as a trace over
  the nearest reference plane: ``L ≈ μ0 · h_d · len / w``.
* **via pair** — the anti-parallel barrel pair down to the reference cavity:
  ``L = (μ0/π) · h · acosh(s / 2r)``, derated for N parallel pairs by
  ``k_mutual / N`` (fully independent pairs would be ``1/N``; real adjacent
  pairs share flux).
* **spreading** — a radial closed-form stand-in for the plane-pair term,
  ``L = (μ0 · h_cav / π) · ln(r_far / r_port)``, replaced by the FEM value
  when Tier 2 has run (the closed form knows nothing about splits or
  perforations — that's the whole point of Tier 2).

  Note the ``1/π``, not ``1/2π``: current spreads radially *out* of the cap
  port and converges radially *into* the IC port, so both ports contribute a
  ``ln`` term. This makes the two-port cavity term share the via-pair's
  functional form, as it must — both are the same 2-D Laplace problem with
  two line sources. Validated against the Tier-2 FEM to within 0.5 % on an
  unbroken plane (see tests/test_caploop_tier2.py).

All inputs mm, all outputs henries (GUI converts to nH).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from fypa.caploop.constants import MU0_H_PER_MM, CapLoopSettings
from fypa.caploop.identify import CapInstance, EscapeVia

# acosh(s/2r) needs s > 2r; overlapping barrels are a geometry error, so the
# argument is clamped just above 1 (log-slow region — the exact clamp barely
# moves the result) rather than allowed to NaN.
_MIN_ACOSH_ARG = 1.02


@dataclass(frozen=True, slots=True)
class Tier1Result:
    """Mounted inductance with its per-term breakdown (henries)."""
    total_h: float
    escape_rail_h: float
    escape_return_h: float
    via_loop_h: float
    spread_cf_h: float
    # Effective geometry the terms were evaluated with — surfaced in the
    # table so a surprising L can be traced to its inputs.
    s_mm: float               # rail↔return escape-cluster centroid spacing
    r_eff_mm: float           # mean escape drill radius
    h_via_mm: float           # vertical run, mounting surface → cavity middle
    n_pairs: int
    is_fallback: bool = False


def via_pair_loop_h(h_mm: float, s_mm: float, r_mm: float) -> float:
    """Loop inductance of one anti-parallel round-barrel pair of length h."""
    if h_mm <= 0.0 or r_mm <= 0.0:
        return 0.0
    arg = max(s_mm / (2.0 * r_mm), _MIN_ACOSH_ARG)
    return (MU0_H_PER_MM / math.pi) * h_mm * math.acosh(arg)


def parallel_via_reduction(l_single_h: float, n_pairs: int,
                           k_mutual: float) -> float:
    """N parallel via pairs, derated for mutual coupling."""
    return l_single_h * k_mutual / max(n_pairs, 1)


def escape_h(len_mm: float, w_mm: float, h_d_mm: float) -> float:
    """Pad/trace escape over a reference plane at depth ``h_d``."""
    if len_mm <= 0.0 or h_d_mm <= 0.0:
        return 0.0
    return MU0_H_PER_MM * h_d_mm * len_mm / max(w_mm, 0.05)


def spreading_closed_form_h(h_cav_mm: float, r_port_mm: float,
                            r_far_mm: float) -> float:
    """Radial spreading between two ports in an unbroken plane-pair cavity.

    Both ports spread radially, hence ``1/π`` rather than ``1/2π`` — see the
    module docstring. Only valid on continuous planes; Tier 2 replaces it.
    """
    if h_cav_mm <= 0.0 or r_port_mm <= 0.0 or r_far_mm <= r_port_mm:
        return 0.0
    return (MU0_H_PER_MM * h_cav_mm / math.pi) \
        * math.log(r_far_mm / r_port_mm)


def _centroid(vias: tuple[EscapeVia, ...]) -> tuple[float, float]:
    return (sum(v.x_mm for v in vias) / len(vias),
            sum(v.y_mm for v in vias) / len(vias))


def mounted_inductance(
    cap: CapInstance,
    settings: CapLoopSettings | None = None,
) -> Tier1Result:
    """Tier-1 mounted inductance of one capacitor.

    Effective values: s = distance between the two sides' escape-cluster
    centroids, r = mean drill radius over all escapes, n = min(pairs per
    side), h = mounting surface to the middle of the reference cavity (both
    barrels together traverse the full depth to their respective planes, so
    the pair-average run is depth + h_cav/2). Each side's escape run is the
    *pad-edge* distance to its nearest via, over that side's own pad width,
    so a via-in-pad contributes no escape term at all. Degenerate geometry (a
    side with no escape at all, or no reference cavity) can't be modelled by
    the closed forms — those caps get the settings fallback value and
    ``is_fallback`` so the table shows an estimate, not a confident number.
    """
    settings = settings or CapLoopSettings()
    fallback = Tier1Result(
        total_h=settings.fallback_via_loop_nh * 1e-9,
        escape_rail_h=0.0, escape_return_h=0.0,
        via_loop_h=settings.fallback_via_loop_nh * 1e-9,
        spread_cf_h=0.0,
        s_mm=0.0, r_eff_mm=0.0, h_via_mm=0.0, n_pairs=0,
        is_fallback=True,
    )
    if not cap.vias_rail or not cap.vias_return or cap.cavity is None:
        return fallback

    cav = cap.cavity
    cx_rail = _centroid(cap.vias_rail)
    cx_return = _centroid(cap.vias_return)
    s_mm = math.hypot(cx_rail[0] - cx_return[0], cx_rail[1] - cx_return[1])
    all_escapes = cap.vias_rail + cap.vias_return
    r_eff_mm = 0.5 * sum(v.drill_mm for v in all_escapes) / len(all_escapes)
    if s_mm <= 0.0 or r_eff_mm <= 0.0:
        return fallback

    h_via_mm = cav.depth_mm + 0.5 * cav.h_cav_mm
    n_pairs = min(len(cap.vias_rail), len(cap.vias_return))
    via_loop = parallel_via_reduction(
        via_pair_loop_h(h_via_mm, s_mm, r_eff_mm),
        n_pairs, settings.mutual_coupling_factor)

    # Escape run per side: shortest pad-edge→via distance, over that side's
    # own pad width, at the depth of the nearer cavity plane.
    h_d_mm = cav.depth_mm
    escape_rail = escape_h(
        min(e.escape_mm for e in cap.vias_rail),
        cap.pad_width_rail_mm, h_d_mm)
    escape_return = escape_h(
        min(e.escape_mm for e in cap.vias_return),
        cap.pad_width_return_mm, h_d_mm)

    if cap.target_pins:
        tx = sum(p["x_mm"] for p in cap.target_pins) / len(cap.target_pins)
        ty = sum(p["y_mm"] for p in cap.target_pins) / len(cap.target_pins)
        px = 0.5 * (cx_rail[0] + cx_return[0])
        py = 0.5 * (cx_rail[1] + cx_return[1])
        r_far_mm = math.hypot(tx - px, ty - py)
    else:
        r_far_mm = settings.tier1_r_far_default_mm
    spread_cf = spreading_closed_form_h(
        cav.h_cav_mm, max(0.5 * s_mm, r_eff_mm), r_far_mm)

    return Tier1Result(
        total_h=escape_rail + escape_return + via_loop + spread_cf,
        escape_rail_h=escape_rail,
        escape_return_h=escape_return,
        via_loop_h=via_loop,
        spread_cf_h=spread_cf,
        s_mm=s_mm,
        r_eff_mm=r_eff_mm,
        h_via_mm=h_via_mm,
        n_pairs=n_pairs,
    )
