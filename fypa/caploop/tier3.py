"""Tier 3 — the full cap→plane→IC loop.

Tier 1 stops at the capacitor's own mount; Tier 2 solves the cavity between
the cap and the IC. Tier 3 closes the loop by adding the term at the far end
— the IC's own via pair climbing out of the cavity to its balls — and rolls
the per-cap totals up per rail:

``L_total = L_escape(both pads) + L_via(cap→cavity) + L_spread(FEM) +
L_via(cavity→IC)``

All four terms are series elements of one current loop, so they add. The
per-rail rollup combines the included caps in parallel, which is what the IC
actually sees at frequencies where every cap is still capacitive.

The via terms reuse :func:`fypa.caploop.tier1.via_pair_loop_h`. Plating
thickness — which dominates the DC barrel *resistance* model in
:func:`fypa.altium.loader._barrel_segment_resistance_ohm` — is irrelevant
here: loop inductance is set by the current's enclosed area, and the return
current rides the barrel's outer wall regardless of how thick the plating is.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

from fypa.caploop.constants import CapLoopSettings
from fypa.caploop.identify import CapInstance, EscapeVia, associate_escape_vias
from fypa.caploop.tier1 import (
    Tier1Result,
    parallel_via_reduction,
    via_pair_loop_h,
)


@dataclass(frozen=True, slots=True)
class IcGeometry:
    """The target device's via geometry at the far end of the loop."""
    vias_rail: tuple[EscapeVia, ...]
    vias_return: tuple[EscapeVia, ...]
    mount_layer_id: int
    z_mount_mm: float


@dataclass(frozen=True, slots=True)
class Tier3Result:
    """Full loop inductance with its series breakdown (henries)."""
    total_h: float
    escape_h: float          # both cap pads
    via_loop_cap_h: float
    spread_h: float
    via_loop_ic_h: float
    ic_pairs: int
    # True when a term could not be modelled and was taken as zero — the
    # total is then a lower bound, not an estimate.
    is_partial: bool = False
    reason: str = ""


@dataclass(frozen=True, slots=True)
class RailSummary:
    """Per-rail rollup over the included capacitors."""
    rail: str
    cap_count: int
    parallel_h: float        # what the IC sees: caps in parallel
    min_h: float
    median_h: float


def barrel_pair_loop_h(drill_mm: float, hop_mm: float, s_mm: float,
                       settings: CapLoopSettings | None = None) -> float:
    """Loop inductance of one via pair spanning ``hop_mm`` of stack.

    Degenerate geometry (missing drill / zero span) falls back to the
    settings value rather than producing a divide-by-zero or a 0 H short —
    the same discipline the DC via-resistance model applies.
    """
    settings = settings or CapLoopSettings()
    if drill_mm <= 0.0 or hop_mm <= 0.0 or s_mm <= 0.0:
        return settings.fallback_via_loop_nh * 1e-9
    return via_pair_loop_h(hop_mm, s_mm, 0.5 * drill_mm)


def build_ic_geometry(
    extracted,
    cap: CapInstance,
    rail_net_indices: set[int],
    return_net_indices: set[int],
    enabled_layers: list[int],
    z_centers: dict[int, float],
    settings: CapLoopSettings | None = None,
) -> IcGeometry | None:
    """Associate the target device's escape vias, both sides.

    Uses the same clustering as the capacitor side
    (:func:`~fypa.caploop.identify.associate_escape_vias`), so both ends of
    the loop are modelled consistently. ``None`` when the target has no
    return-side pins (an ideal-return directive) or no vias at all — the
    caller then reports a partial Tier-3 total.
    """
    settings = settings or CapLoopSettings()
    if not cap.target_pins or not cap.target_pins_n:
        return None

    def _pads_for(pins) -> list:
        wanted = {(round(p["x_mm"], 4), round(p["y_mm"], 4)) for p in pins}
        return [pad for pad in extracted.pads
                if (round(pad.center.x, 4), round(pad.center.y, 4)) in wanted]

    pads_rail = _pads_for(cap.target_pins)
    pads_return = _pads_for(cap.target_pins_n)
    if not pads_rail or not pads_return:
        return None

    layer_id = int(cap.target_pins[0].get("layer_id") or 1)
    z_mount = z_centers.get(layer_id)
    if z_mount is None:
        return None

    # Same mounting-layer rule as the capacitor side: a via that doesn't
    # reach the device's own layer can't take current off its balls.
    vias_rail = associate_escape_vias(
        pads_rail, extracted, rail_net_indices, enabled_layers, settings,
        layer_id)
    vias_return = associate_escape_vias(
        pads_return, extracted, return_net_indices, enabled_layers, settings,
        layer_id)
    if not vias_rail or not vias_return:
        return None

    return IcGeometry(vias_rail, vias_return, layer_id, z_mount)


def _centroid(vias: tuple[EscapeVia, ...]) -> tuple[float, float]:
    return (sum(v.x_mm for v in vias) / len(vias),
            sum(v.y_mm for v in vias) / len(vias))


def ic_via_loop_h(
    ic: IcGeometry,
    cavity_mid_z_mm: float,
    settings: CapLoopSettings | None = None,
) -> tuple[float, int]:
    """The IC's via-pair loop from the cavity up to its balls, and the pair
    count it was derated by."""
    settings = settings or CapLoopSettings()
    c_rail = _centroid(ic.vias_rail)
    c_return = _centroid(ic.vias_return)
    s_mm = math.hypot(c_rail[0] - c_return[0], c_rail[1] - c_return[1])
    all_vias = ic.vias_rail + ic.vias_return
    drill_mm = sum(v.drill_mm for v in all_vias) / len(all_vias)
    hop_mm = abs(cavity_mid_z_mm - ic.z_mount_mm)
    n_pairs = min(len(ic.vias_rail), len(ic.vias_return))
    single = barrel_pair_loop_h(drill_mm, hop_mm, s_mm, settings)
    return (parallel_via_reduction(single, n_pairs,
                                   settings.mutual_coupling_factor),
            n_pairs)


def total_loop(
    cap: CapInstance,
    tier1: Tier1Result,
    spread_h: float | None,
    ic: IcGeometry | None,
    settings: CapLoopSettings | None = None,
) -> Tier3Result | None:
    """Assemble the full loop for one capacitor.

    ``spread_h`` is the Tier-2 FEM cavity term; ``None`` (a split plane, a
    missing cavity) means there is no meaningful total and the caller shows
    "—" rather than a number built on a term it doesn't have. A missing
    ``ic`` yields a *partial* total (the IC via term taken as zero, so the
    result is a lower bound) rather than nothing, because the other three
    terms are still the bulk of the loop.
    """
    settings = settings or CapLoopSettings()
    if spread_h is None or cap.cavity is None or tier1.is_fallback:
        return None

    escape_h = tier1.escape_rail_h + tier1.escape_return_h
    cavity_mid_z = 0.5 * (cap.cavity.z_rail_mm + cap.cavity.z_return_mm)

    if ic is None:
        ic_h, ic_pairs = 0.0, 0
        is_partial, reason = True, "target via geometry unknown"
    else:
        ic_h, ic_pairs = ic_via_loop_h(ic, cavity_mid_z, settings)
        is_partial, reason = False, ""

    return Tier3Result(
        total_h=escape_h + tier1.via_loop_h + spread_h + ic_h,
        escape_h=escape_h,
        via_loop_cap_h=tier1.via_loop_h,
        spread_h=spread_h,
        via_loop_ic_h=ic_h,
        ic_pairs=ic_pairs,
        is_partial=is_partial,
        reason=reason,
    )


def rail_rollup(
    rows: list[tuple[str, str, float]],
) -> dict[str, RailSummary]:
    """Per-rail summary from ``(rail, designator, total_h)`` triples.

    The parallel combination is what the IC sees below the caps' series
    resonance — halving every cap's mounted L, or doubling the cap count,
    each halve it.
    """
    by_rail: dict[str, list[float]] = {}
    for rail, _designator, total_h in rows:
        if total_h and total_h > 0.0:
            by_rail.setdefault(rail, []).append(total_h)

    out: dict[str, RailSummary] = {}
    for rail, values in by_rail.items():
        inv = sum(1.0 / v for v in values)
        out[rail] = RailSummary(
            rail=rail,
            cap_count=len(values),
            parallel_h=(1.0 / inv) if inv > 0.0 else math.inf,
            min_h=min(values),
            median_h=statistics.median(values),
        )
    return out
