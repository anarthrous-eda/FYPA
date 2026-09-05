"""Shared VTK scalar-field helpers for ParaView export."""

from __future__ import annotations

import base64
import re
from collections.abc import Iterable

import numpy as np

# VTK XML type name -> numpy dtype. Explicitly little-endian to match the
# ``byte_order="LittleEndian"`` the writers declare on <VTKFile>.
VTK_DTYPES: dict[str, np.dtype] = {
    "Float32": np.dtype("<f4"),
    "Float64": np.dtype("<f8"),
    "Int8": np.dtype("i1"),
    "UInt8": np.dtype("u1"),
    "Int32": np.dtype("<i4"),
    "UInt32": np.dtype("<u4"),
    "Int64": np.dtype("<i8"),
    "UInt64": np.dtype("<u8"),
}

# Width of the byte-count header that prefixes every inline binary DataArray.
# Must match the ``header_type`` attribute the writers set on <VTKFile>; VTK's
# reader defaults to UInt32 when the attribute is absent, so UInt32 is the
# maximally-compatible choice.
VTK_HEADER_TYPE: str = "UInt32"
_HEADER_DTYPE: np.dtype = VTK_DTYPES[VTK_HEADER_TYPE]


def vtk_binary_data(values: Iterable[int | float] | np.ndarray,
                    data_type: str) -> str:
    """Encode ``values`` as the text body of a ``format="binary"`` DataArray.

    VTK's inline binary encoding is *two independently base64-encoded
    blocks concatenated*: first a single ``header_type`` word holding the
    byte count of the payload, then the raw little-endian array data. (The
    header is encoded on its own, so it carries its own base64 padding —
    this is what ``vtkBase64OutputStream`` emits and what ParaView expects
    for uncompressed inline data.)

    Roughly 8x faster than the per-value ``str()``/``repr()`` joins this
    replaces, produces ~half the bytes, and is exact: the doubles are
    written bit-for-bit rather than round-tripped through decimal text.
    """
    dtype = VTK_DTYPES[data_type]
    if isinstance(values, np.ndarray):
        arr = np.ascontiguousarray(values.reshape(-1), dtype=dtype)
    else:
        arr = np.asarray(list(values), dtype=dtype).reshape(-1)
    payload = arr.tobytes()
    header = np.array([arr.nbytes], dtype=_HEADER_DTYPE).tobytes()
    return (base64.b64encode(header).decode("ascii")
            + base64.b64encode(payload).decode("ascii"))


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
    """Average face-defined values onto vertices (each vertex gets the mean
    of the values of its incident faces).

    Kept on ``np.add.at`` deliberately. A ``np.bincount``-with-weights
    formulation (as in ``fypa.altium_viewer._face_to_vertex_average``, whose
    docstring claims 5-10x) was faster only on numpy < 1.24, before
    ``np.add.at`` grew its fast scatter path. Measured on numpy 2.2.3 it is
    1.4-2.3x *slower* here (2M faces: 72 ms add.at vs 103 ms bincount) and,
    because it accumulates in per-face rather than per-column order, its
    sums differ from these by up to ~6 ULP. Not worth trading exactness for
    a slowdown.
    """
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
    """Return the maximum vertex potential across all layer mesh components.

    The peak is seeded from the data, not floored at zero, so an all-negative
    solution returns its true (negative) max — matching the per-island
    fallback in :func:`per_vertex_fields`. Returns ``0.0`` only when there is
    no data at all.
    """
    peak: float | None = None
    for pots in layer_potentials:
        arr = np.asarray(pots, dtype=np.float64)
        if arr.size:
            layer_peak = float(arr.max())
            peak = layer_peak if peak is None else max(peak, layer_peak)
    return 0.0 if peak is None else peak
