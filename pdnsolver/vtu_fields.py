"""Shared VTK scalar-field helpers for ParaView export."""

from __future__ import annotations

import re

import numpy as np


def sanitize_filename(
    name: str,
    used_names: set[str],
    fallback_prefix: str = "layer",
) -> str:
    """Map a layer name to a safe, unique filename stem."""
    if not name or not name.strip():
        base = fallback_prefix
    else:
        base = re.sub(r"[^a-zA-Z0-9_.-]", "_", name.strip())
        base = re.sub(r"_+", "_", base).strip("_")
        if not base:
            base = fallback_prefix
    if base not in used_names:
        used_names.add(base)
        return base
    counter = 2
    while f"{base}_{counter}" in used_names:
        counter += 1
    result = f"{base}_{counter}"
    used_names.add(result)
    return result


def face_to_vertex_average(
    tris: np.ndarray,
    face_values: np.ndarray,
    n_verts: int,
) -> np.ndarray:
    """Average face-defined values onto vertices."""
    totals = np.zeros(n_verts, dtype=np.float64)
    counts = np.zeros(n_verts, dtype=np.float64)
    if tris.size == 0:
        return totals
    np.add.at(totals, tris[:, 0], face_values)
    np.add.at(totals, tris[:, 1], face_values)
    np.add.at(totals, tris[:, 2], face_values)
    np.add.at(counts, tris[:, 0], 1.0)
    np.add.at(counts, tris[:, 1], 1.0)
    np.add.at(counts, tris[:, 2], 1.0)
    counts[counts == 0] = 1.0
    return totals / counts


def per_vertex_fields(
    tris: np.ndarray,
    pots: np.ndarray,
    power_density: np.ndarray | None,
    conductance: float,
    *,
    voltage_drop_reference: float | None = None,
) -> dict[str, np.ndarray]:
    """Compute viewer heatmap quantities at mesh vertices."""
    n_verts = int(pots.shape[0])
    voltage = np.asarray(pots, dtype=np.float64)
    if voltage_drop_reference is None:
        ref = float(voltage.max()) if voltage.size else 0.0
    else:
        ref = float(voltage_drop_reference)
    voltage_drop = voltage - ref

    if power_density is None:
        pd_at_verts = np.zeros(n_verts, dtype=np.float64)
    else:
        pd_at_verts = face_to_vertex_average(
            tris, np.asarray(power_density, dtype=np.float64), n_verts,
        )
    current_density = np.sqrt(np.maximum(pd_at_verts * conductance, 0.0))

    return {
        "voltage": voltage,
        "voltage_drop": voltage_drop,
        "current_density": current_density,
        "power_density": pd_at_verts,
    }


def global_voltage_max(layer_potentials) -> float:
    """Return the maximum vertex potential across all layer mesh components."""
    peak = 0.0
    for pots in layer_potentials:
        arr = np.asarray(pots, dtype=np.float64)
        if arr.size:
            peak = max(peak, float(arr.max()))
    return peak
