"""ParaView VTU export for the lean in-memory solution.

The viewer holds a :class:`fypa.lean_solution.LeanSolution` — flat numpy
arrays of per-vertex coordinates, per-vertex voltages, and per-triangle
vertex indices. The upstream :mod:`pdnsolver.paraview` exporter walks
padne's half-edge :class:`pdnsolver.mesh.Mesh`, which the lean format no
longer carries, so File > Export > ParaView uses this writer instead.

The output is the same VTK XML UnstructuredGrid format the CLI's
``paraview`` subcommand produces — one ``.vtu`` per copper layer, with
per-vertex scalar fields for every viewer heatmap mode (voltage,
voltage drop, current density, power density). Y is negated to match the
orientation convention used by the upstream exporter so files from
either path render identically in ParaView.

When ``via_rows`` is supplied (the viewer passes its cached via report),
an additional ``vias.vtu`` point cloud is written with a ``via_current``
scalar at each via location.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np

from pdnsolver.vtu_fields import (
    global_voltage_max,
    per_vertex_fields,
    sanitize_filename,
)

log = logging.getLogger(__name__)

# VTK cell type for a single vertex (used by vias.vtu).
_VTK_VERTEX: int = 1


def _write_scalar_arrays(parent, fields: dict[str, np.ndarray],
                       default_scalar: str = "voltage") -> None:
    from lxml.etree import SubElement

    point_data = SubElement(parent, "PointData")
    point_data.set("Scalars", default_scalar)
    for name, values in fields.items():
        arr = SubElement(point_data, "DataArray")
        arr.set("type", "Float64")
        arr.set("format", "ascii")
        arr.set("Name", name)
        arr.text = " ".join(repr(float(v)) for v in values)


def _write_points(parent, xys: np.ndarray) -> None:
    from lxml.etree import SubElement

    num_points = int(xys.shape[0])
    points = SubElement(parent, "Points")
    coords = SubElement(points, "DataArray")
    coords.set("type", "Float64")
    coords.set("format", "ascii")
    coords.set("NumberOfComponents", "3")
    flat = np.empty(num_points * 3, dtype=np.float64)
    flat[0::3] = xys[:, 0]
    flat[1::3] = -xys[:, 1]
    flat[2::3] = 0.0
    coords.text = " ".join(repr(float(v)) for v in flat)


def _write_triangle_cells(parent, tris: np.ndarray) -> None:
    from lxml.etree import SubElement

    num_cells = int(tris.shape[0])
    cells = SubElement(parent, "Cells")
    conn = SubElement(cells, "DataArray")
    conn.set("type", "Int32")
    conn.set("format", "ascii")
    conn.set("Name", "connectivity")
    conn.text = " ".join(str(int(v)) for v in tris.reshape(-1))

    offsets = SubElement(cells, "DataArray")
    offsets.set("type", "Int32")
    offsets.set("format", "ascii")
    offsets.set("Name", "offsets")
    offsets.text = " ".join(str(3 * (i + 1)) for i in range(num_cells))

    types = SubElement(cells, "DataArray")
    types.set("type", "UInt8")
    types.set("format", "ascii")
    types.set("Name", "types")
    types.text = " ".join("5" for _ in range(num_cells))


def _write_piece(xys: np.ndarray, tris: np.ndarray,
                 fields: dict[str, np.ndarray]) -> Any:
    """Build one ``<Piece>`` (one connected component)."""
    from lxml.etree import Element

    num_points = int(xys.shape[0])
    num_cells = int(tris.shape[0])

    piece = Element("Piece")
    piece.set("NumberOfPoints", str(num_points))
    piece.set("NumberOfCells", str(num_cells))
    _write_scalar_arrays(piece, fields)
    _write_points(piece, xys)
    _write_triangle_cells(piece, tris)
    return piece


def _write_vtu(root_children: list, out_path: Path) -> None:
    import lxml.etree
    from lxml.etree import Element, SubElement

    root = Element("VTKFile")
    root.set("type", "UnstructuredGrid")
    root.set("version", "0.1")
    root.set("byte_order", "LittleEndian")
    ug = SubElement(root, "UnstructuredGrid")
    for child in root_children:
        ug.append(child)
    tree = lxml.etree.ElementTree(root)
    tree.write(str(out_path), xml_declaration=True, encoding="utf-8",
               pretty_print=True)


def _export_via_points(via_rows: list[dict], output_dir: Path) -> bool:
    """Write ``vias.vtu`` — one vertex per via with ``via_current`` (A)."""
    from lxml.etree import Element, SubElement

    xs: list[float] = []
    ys: list[float] = []
    currents: list[float] = []
    for row in via_rows:
        cur = row.get("current")
        if cur is None:
            continue
        cur_f = float(cur)
        if not np.isfinite(cur_f):
            continue
        xs.append(float(row.get("x_mm", 0.0)))
        ys.append(float(row.get("y_mm", 0.0)))
        currents.append(cur_f)
    if not xs:
        return False

    n = len(xs)
    xys = np.column_stack([xs, ys])
    piece = Element("Piece")
    piece.set("NumberOfPoints", str(n))
    piece.set("NumberOfCells", str(n))

    point_data = SubElement(piece, "PointData")
    point_data.set("Scalars", "via_current")
    arr = SubElement(point_data, "DataArray")
    arr.set("type", "Float64")
    arr.set("format", "ascii")
    arr.set("Name", "via_current")
    arr.text = " ".join(repr(v) for v in currents)

    points = SubElement(piece, "Points")
    coords = SubElement(points, "DataArray")
    coords.set("type", "Float64")
    coords.set("format", "ascii")
    coords.set("NumberOfComponents", "3")
    flat = np.empty(n * 3, dtype=np.float64)
    flat[0::3] = xys[:, 0]
    flat[1::3] = -xys[:, 1]
    flat[2::3] = 0.0
    coords.text = " ".join(repr(float(v)) for v in flat)

    cells = SubElement(piece, "Cells")
    conn = SubElement(cells, "DataArray")
    conn.set("type", "Int32")
    conn.set("format", "ascii")
    conn.set("Name", "connectivity")
    conn.text = " ".join(str(i) for i in range(n))

    offsets = SubElement(cells, "DataArray")
    offsets.set("type", "Int32")
    offsets.set("format", "ascii")
    offsets.set("Name", "offsets")
    offsets.text = " ".join(str(i + 1) for i in range(n))

    types = SubElement(cells, "DataArray")
    types.set("type", "UInt8")
    types.set("format", "ascii")
    types.set("Name", "types")
    types.text = " ".join(str(_VTK_VERTEX) for _ in range(n))

    _write_vtu([piece], output_dir / "vias.vtu")
    return True


def export_lean_solution(
    solution,
    output_dir: Path,
    *,
    via_rows: list[dict] | None = None,
    voltage_drop_reference: float | None = None,
) -> int:
    """Write one ``.vtu`` per padne layer of ``solution`` into ``output_dir``.

    Each layer file carries per-vertex ``voltage``, ``voltage_drop``,
    ``current_density`` and ``power_density`` arrays. When ``via_rows`` is
    given, an extra ``vias.vtu`` point cloud is written.

    ``voltage_drop`` is measured against ``voltage_drop_reference`` (pass the
    viewer's drop reference so the export matches the interactive heatmap).
    When unset it defaults to the global max potential across all layers — note
    that on a multi-rail board this references every layer to the single
    highest rail, so prefer passing the viewer reference when exporting a
    specific rail.

    Returns the number of files written. Layers with no mesh components
    are skipped.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if voltage_drop_reference is None:
        all_pots = [
            pots
            for layer_solution in solution.layer_solutions
            for pots in layer_solution.potentials
        ]
        voltage_drop_reference = global_voltage_max(all_pots)

    used_names: set[str] = set()
    total_files = 0
    total_pieces = 0

    for layer_idx, layer_solution in enumerate(solution.layer_solutions):
        layer_name = solution.problem.layers[layer_idx].name
        conductance = float(solution.problem.layers[layer_idx].conductance)
        pds_src = layer_solution.power_densities
        components = list(zip(
            layer_solution.vertex_xys,
            layer_solution.triangles,
            layer_solution.potentials,
            pds_src if pds_src else [None] * len(layer_solution.vertex_xys),
        ))
        if not components:
            log.warning("Skipping layer %r — no non-empty meshes", layer_name)
            continue

        stem = sanitize_filename(layer_name, used_names)
        pieces = []
        for xys, tris, pots, pd in components:
            fields = per_vertex_fields(
                tris, pots, pd, conductance,
                voltage_drop_reference=voltage_drop_reference,
            )
            pieces.append(_write_piece(xys, tris, fields))

        _write_vtu(pieces, output_dir / f"{stem}.vtu")
        total_files += 1
        total_pieces += len(pieces)

    if via_rows and _export_via_points(via_rows, output_dir):
        total_files += 1

    log.info("Exported %d mesh pieces across %d layer files to %s",
             total_pieces, total_files, output_dir)
    return total_files
