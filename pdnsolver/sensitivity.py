"""Copper sensitivity from the adjoint fields the solver returns.

For a target load the solver defines ``J = V(f) - V(t)`` — the voltage the
load actually receives — and, alongside the forward potentials ``v``, solves
the adjoint system ``Lᵀλ = e_f - e_t`` (see ``Problem.sensitivity_targets``).
With the system matrix assembled as ``L = Σ_t σ_t·K_t`` (triangle ``t``'s
cotangent stiffness ``K_t`` scaled by its sheet conductance ``σ_t``)::

    dJ/dσ_t = -λᵀ K_t v = Σ_edges w_e · Δλ_e · Δv_e

where ``w_e`` is the edge's half-cotangent weight — the same weights the
solver assembles with, so the derivative is exact for the discrete model.

``σ_t·dJ/dσ_t`` is the first-order rise in the load's voltage if triangle
``t`` carried twice the copper — which is what a parallel, stitched copy of
the same copper weight on another layer does. Divided by the triangle's
area it becomes a density (V per mm² of parallel copper) that is also the
right quantity at the copper's edge: pushing an edge out by ``δ`` over a
length ``ℓ`` adds ``ℓ·δ`` of copper at that density.

A lumped resistor carrying ``I`` from ``a`` to ``b`` gets the same
treatment: ``G·dJ/dG = I·(λ_a - λ_b)`` — the gain from a second identical
resistor (a via) in parallel.

Everything here is a first-order estimate: accurate for modest changes,
indicative for large ones, which need a re-solve.
"""
from __future__ import annotations

import numpy as np

from pdnsolver.solver import _half_cotangent


def mesh_adjoint(layer_solution, key: str, mesh_i: int) -> np.ndarray | None:
    """The float64 adjoint field of ``key`` on one mesh, or None.

    Works on a solver ``LayerSolution`` and on the viewer's lean form alike:
    both store float32 deltas in ``adjoints[key][i]`` and the per-mesh
    float64 offset in ``adjoint_offsets[key][i]``.
    """
    deltas = (getattr(layer_solution, "adjoints", None) or {}).get(key)
    if deltas is None or mesh_i >= len(deltas):
        return None
    offsets = (getattr(layer_solution, "adjoint_offsets", None) or {}).get(key)
    off = float(offsets[mesh_i]) if offsets and mesh_i < len(offsets) else 0.0
    return np.asarray(deltas[mesh_i], dtype=np.float64) + off


def triangle_areas(xys: np.ndarray, tris: np.ndarray) -> np.ndarray:
    """Unsigned area of each triangle (mm²)."""
    if tris.shape[0] == 0:
        return np.zeros(0, dtype=np.float64)
    p0, p1, p2 = xys[tris[:, 0]], xys[tris[:, 1]], xys[tris[:, 2]]
    d1, d2 = p1 - p0, p2 - p0
    return 0.5 * np.abs(d1[:, 0] * d2[:, 1] - d1[:, 1] * d2[:, 0])


def triangle_values(
    xys: np.ndarray,
    tris: np.ndarray,
    v: np.ndarray,
    lam: np.ndarray,
    sigma: float | np.ndarray,
) -> np.ndarray:
    """``σ_t·dJ/dσ_t`` per triangle (V): the rise in the target load's voltage
    if this triangle's copper were doubled. ``sigma`` is the layer's sheet
    conductance, or one value per triangle."""
    if tris.shape[0] == 0:
        return np.zeros(0, dtype=np.float64)
    i0, i1, i2 = tris[:, 0], tris[:, 1], tris[:, 2]
    p0, p1, p2 = xys[i0], xys[i1], xys[i2]
    w12 = _half_cotangent(p1 - p0, p2 - p0)  # apex 0 ↔ edge (1, 2)
    w20 = _half_cotangent(p2 - p1, p0 - p1)  # apex 1 ↔ edge (2, 0)
    w01 = _half_cotangent(p0 - p2, p1 - p2)  # apex 2 ↔ edge (0, 1)
    s = (w12 * (lam[i1] - lam[i2]) * (v[i1] - v[i2])
         + w20 * (lam[i2] - lam[i0]) * (v[i2] - v[i0])
         + w01 * (lam[i0] - lam[i1]) * (v[i0] - v[i1]))
    return np.asarray(sigma, dtype=np.float64) * s


def triangle_value_density(
    xys: np.ndarray,
    tris: np.ndarray,
    v: np.ndarray,
    lam: np.ndarray,
    sigma: float | np.ndarray,
) -> np.ndarray:
    """:func:`triangle_values` per unit area (V/mm²). Zero-area slivers get 0."""
    vals = triangle_values(xys, tris, v, lam, sigma)
    area = triangle_areas(xys, tris)
    out = np.zeros_like(vals)
    np.divide(vals, area, out=out, where=area > 0)
    return out


def resistor_value(current_a: float, lam_a: float, lam_b: float) -> float:
    """``G·dJ/dG`` for a resistor carrying ``current_a`` from a to b (V): the
    rise in the load's voltage from a second identical one in parallel."""
    return float(current_a) * (float(lam_a) - float(lam_b))
