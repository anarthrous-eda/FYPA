"""ParaView VTU export for the lean in-memory solution.

The viewer holds a :class:`fypa.lean_solution.LeanSolution` — flat numpy
arrays of per-vertex coordinates, per-vertex voltages, and per-triangle
vertex indices. The upstream :mod:`pdnsolver.paraview` exporter walks
padne's half-edge :class:`pdnsolver.mesh.Mesh`, which the lean format no
longer carries, so File > Export > ParaView uses this writer instead.

The output is the same VTK XML UnstructuredGrid format the CLI's
``paraview`` subcommand produces — one ``.vtu`` per copper layer, with
the per-vertex ``voltage`` scalar field. Y is negated to match the
orientation convention used by the upstream exporter so files from
either path render identically in ParaView.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


def _sanitize_filename(name: str, used_names: set[str],
                       fallback_prefix: str = "layer") -> str:
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


def _write_piece(parent, xys: np.ndarray, tris: np.ndarray,
                 pots: np.ndarray) -> None:
    """Append one ``<Piece>`` (one connected component) to ``parent``."""
    from lxml.etree import SubElement

    num_points = int(xys.shape[0])
    num_cells = int(tris.shape[0])

    piece = SubElement(parent, "Piece")
    piece.set("NumberOfPoints", str(num_points))
    piece.set("NumberOfCells", str(num_cells))

    point_data = SubElement(piece, "PointData")
    point_data.set("Scalars", "voltage")
    voltage = SubElement(point_data, "DataArray")
    voltage.set("type", "Float64")
    voltage.set("format", "ascii")
    voltage.set("Name", "voltage")
    voltage.text = " ".join(repr(float(v)) for v in pots)

    points = SubElement(piece, "Points")
    coords = SubElement(points, "DataArray")
    coords.set("type", "Float64")
    coords.set("format", "ascii")
    coords.set("NumberOfComponents", "3")
    # Negate Y to match the upstream exporter's ParaView orientation.
    flat = np.empty(num_points * 3, dtype=np.float64)
    flat[0::3] = xys[:, 0]
    flat[1::3] = -xys[:, 1]
    flat[2::3] = 0.0
    coords.text = " ".join(repr(float(v)) for v in flat)

    cells = SubElement(piece, "Cells")
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


def export_lean_solution(solution, output_dir: Path) -> int:
    """Write one ``.vtu`` per padne layer of ``solution`` into ``output_dir``.

    Returns the number of files written. Layers with no mesh components
    are skipped.
    """
    import lxml.etree
    from lxml.etree import Element, SubElement

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    used_names: set[str] = set()
    total_files = 0
    total_pieces = 0

    for layer_idx, layer_solution in enumerate(solution.layer_solutions):
        layer_name = solution.problem.layers[layer_idx].name
        components = list(zip(
            layer_solution.vertex_xys,
            layer_solution.triangles,
            layer_solution.potentials,
        ))
        if not components:
            log.warning("Skipping layer %r — no non-empty meshes", layer_name)
            continue

        stem = _sanitize_filename(layer_name, used_names)
        out_path = output_dir / f"{stem}.vtu"

        root = Element("VTKFile")
        root.set("type", "UnstructuredGrid")
        root.set("version", "0.1")
        root.set("byte_order", "LittleEndian")
        ug = SubElement(root, "UnstructuredGrid")

        layer_pieces = 0
        for xys, tris, pots in components:
            _write_piece(ug, xys, tris, pots)
            layer_pieces += 1

        tree = lxml.etree.ElementTree(root)
        tree.write(str(out_path), xml_declaration=True, encoding="utf-8",
                   pretty_print=True)

        total_files += 1
        total_pieces += layer_pieces

    log.info("Exported %d mesh pieces across %d layer files to %s",
             total_pieces, total_files, output_dir)
    return total_files
