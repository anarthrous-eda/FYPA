"""
ParaView VTK XML export functionality for FEM simulation results.

This module provides functions to export padne's FEM simulation results to the
VTK XML UnstructuredGrid format, compatible with ParaView and other VTK-based
visualization tools.
"""

import logging
import re
from pathlib import Path
from collections.abc import Iterable

import lxml.etree
import numpy as np
from lxml.etree import Element, SubElement

from . import mesh, solver

log = logging.getLogger(__name__)


def _sanitize_filename(name: str, used_names: set[str], fallback_prefix: str = "layer") -> str:
    """Sanitize a layer name for use as a filename.

    Args:
        name: Original layer name
        used_names: Set of already used filenames to avoid duplicates
        fallback_prefix: Prefix to use if name is empty or invalid

    Returns:
        Sanitized filename (without extension)
    """
    # Handle empty or whitespace-only names
    if not name or not name.strip():
        base = fallback_prefix
    else:
        # Replace spaces with underscores, keep only alphanumeric, underscore, hyphen, dots
        base = re.sub(r'[^a-zA-Z0-9_.-]', '_', name.strip())
        # Remove multiple consecutive underscores
        base = re.sub(r'_+', '_', base)
        # Remove leading/trailing underscores (but keep dots)
        base = base.strip('_')
        # If nothing left after sanitization, use fallback
        if not base:
            base = fallback_prefix

    # Handle duplicates by appending counter
    if base not in used_names:
        used_names.add(base)
        return base

    counter = 2
    while f"{base}_{counter}" in used_names:
        counter += 1

    result = f"{base}_{counter}"
    used_names.add(result)
    return result


def create_data_array(
    parent: Element,
    data_type: str,
    values: Iterable[int | float],
    name: str | None = None,
    number_of_components: int | None = None
) -> Element:
    """Create a DataArray element with specified type and values.

    Args:
        parent: Parent element to attach the DataArray to
        data_type: VTK data type (e.g., "Float64", "Int32", "UInt8")
        values: Numeric values to store in the array
        name: Optional name attribute for the DataArray
        number_of_components: Optional NumberOfComponents attribute

    Returns:
        Created DataArray element
    """
    data_array = SubElement(parent, "DataArray")
    data_array.set("type", data_type)
    data_array.set("format", "ascii")

    if name is not None:
        data_array.set("Name", name)

    if number_of_components is not None:
        data_array.set("NumberOfComponents", str(number_of_components))

    # Convert all values to strings and join with spaces
    data_array.text = " ".join(str(value) for value in values)

    return data_array


def create_vtk_root() -> Element:
    """Create the root VTKFile element with standard attributes.

    Returns:
        Root VTKFile element configured for UnstructuredGrid format
    """
    root = Element("VTKFile")
    root.set("type", "UnstructuredGrid")
    root.set("version", "0.1")
    root.set("byte_order", "LittleEndian")
    return root


def _face_to_vertex_average(tris: np.ndarray, face_values: np.ndarray,
                            n_verts: int) -> np.ndarray:
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


def _per_vertex_fields(
    tris: np.ndarray,
    vertex_values: np.ndarray,
    face_power_density: np.ndarray | None,
    conductance: float,
) -> dict[str, np.ndarray]:
    voltage = np.asarray(vertex_values, dtype=np.float64)
    ref = float(voltage.max()) if voltage.size else 0.0
    voltage_drop = voltage - ref
    if face_power_density is None:
        pd_at_verts = np.zeros(voltage.shape[0], dtype=np.float64)
    else:
        pd_at_verts = _face_to_vertex_average(
            tris, np.asarray(face_power_density, dtype=np.float64), voltage.shape[0],
        )
    current_density = np.sqrt(np.maximum(pd_at_verts * conductance, 0.0))
    return {
        "voltage": voltage,
        "voltage_drop": voltage_drop,
        "current_density": current_density,
        "power_density": pd_at_verts,
    }


def create_point_data(fields: dict[str, np.ndarray]) -> Element:
    """Create PointData element with all viewer heatmap scalar fields."""
    point_data = Element("PointData")
    point_data.set("Scalars", "voltage")
    for name, values in fields.items():
        create_data_array(point_data, "Float64", values, name=name)
    return point_data


def create_points(mesh_obj: mesh.Mesh) -> Element:
    """Create Points element with vertex coordinates.

    Args:
        mesh_obj: Mesh object containing vertices

    Returns:
        Points element containing 3D coordinates (z=0 for 2D meshes)
        Note: Y coordinates are negated for ParaView orientation
    """
    points = Element("Points")

    # Extract coordinates in vertex index order with Y-axis negated
    coordinates = []
    for vertex in mesh_obj.vertices:
        coordinates.extend([vertex.p.x, -vertex.p.y, 0.0])

    create_data_array(points, "Float64", coordinates, number_of_components=3)
    return points


def _extract_triangle_connectivity(
    mesh_obj: mesh.Mesh,
    power_density: mesh.TwoForm | None = None,
) -> tuple[np.ndarray, np.ndarray | None]:
    """Extract triangle connectivity and per-face power density arrays."""
    tri_rows: list[tuple[int, int, int]] = []
    pd_rows: list[float] = []
    vertex_to_index = {vertex: i for i, vertex in enumerate(mesh_obj.vertices)}

    for face in mesh_obj.faces:
        if face.is_boundary:
            continue

        face_vertices = []
        for edge in face.edges:
            vertex_idx = vertex_to_index[edge.origin]
            face_vertices.append(vertex_idx)

        if len(face_vertices) == 3:
            tri_rows.append(tuple(face_vertices))
            if power_density is not None:
                pd_rows.append(float(power_density.values[face.i]))
        else:
            log.warning(
                "Non-triangular face with %d vertices, skipping",
                len(face_vertices),
            )

    tris = (np.asarray(tri_rows, dtype=np.int32)
            if tri_rows else np.empty((0, 3), dtype=np.int32))
    face_pd = (np.asarray(pd_rows, dtype=np.float64) if pd_rows else None)
    return tris, face_pd


def create_cells(tris: np.ndarray) -> Element:
    """Create Cells element with triangle connectivity, offsets, and types."""
    cells = Element("Cells")
    connectivity_values = tris.reshape(-1).tolist() if tris.size else []
    create_data_array(cells, "Int32", connectivity_values, name="connectivity")
    offset_values = [3 * (i + 1) for i in range(tris.shape[0])]
    create_data_array(cells, "Int32", offset_values, name="offsets")
    type_values = [5] * tris.shape[0]
    create_data_array(cells, "UInt8", type_values, name="types")
    return cells


def create_piece(
    mesh_obj: mesh.Mesh,
    potentials: mesh.ZeroForm,
    *,
    power_density: mesh.TwoForm | None = None,
    conductance: float = 0.0,
) -> Element:
    """Create a Piece element with geometry and all heatmap scalar fields."""
    tris, face_pd = _extract_triangle_connectivity(mesh_obj, power_density)
    vertex_values = np.asarray(
        [potentials[vertex] for vertex in potentials.mesh.vertices],
        dtype=np.float64,
    )
    fields = _per_vertex_fields(tris, vertex_values, face_pd, conductance)

    piece = Element("Piece")
    piece.set("NumberOfPoints", str(len(mesh_obj.vertices)))
    piece.set("NumberOfCells", str(tris.shape[0]))
    piece.append(create_point_data(fields))
    piece.append(create_points(mesh_obj))
    piece.append(create_cells(tris))
    return piece


def export_solution(solution: solver.Solution, output_dir: Path) -> None:
    """Export a complete Solution to VTK XML format as separate files per layer.

    Args:
        solution: Complete solution containing meshes and potential fields
        output_dir: Directory where VTU files should be written (one per layer)
    """
    log.info(f"Exporting solution to ParaView format: {output_dir}")

    # Create output directory if it doesn't exist
    output_dir.mkdir(parents=True, exist_ok=True)

    # Keep track of used filenames to handle duplicates
    used_names: set[str] = set()

    # Process each layer solution as a separate file
    total_files = 0
    total_pieces = 0

    for layer_idx, layer_solution in enumerate(solution.layer_solutions):
        # Get layer name from the problem
        layer_name = solution.problem.layers[layer_idx].name
        log.debug(f"Processing layer '{layer_name}' with {len(layer_solution.meshes)} meshes")

        conductance = float(solution.problem.layers[layer_idx].conductance)
        pds_src = (layer_solution.power_densities
                   if layer_solution.power_densities
                   else [None] * len(layer_solution.meshes))

        meshes_and_fields = list(zip(
            layer_solution.meshes,
            layer_solution.potentials,
            pds_src,
        ))

        if not meshes_and_fields:
            log.warning(f"Skipping layer '{layer_name}' - no non-empty meshes")
            continue

        # Generate sanitized filename
        filename = _sanitize_filename(layer_name, used_names)
        output_file = output_dir / f"{filename}.vtu"

        # Create root structure for this layer
        root = create_vtk_root()
        unstructured_grid = SubElement(root, "UnstructuredGrid")

        # Add all meshes in this layer as pieces
        layer_pieces = 0
        for mesh_obj, potential, pd in meshes_and_fields:
            piece = create_piece(
                mesh_obj, potential,
                power_density=pd,
                conductance=conductance,
            )
            unstructured_grid.append(piece)
            layer_pieces += 1

        log.debug(f"Layer '{layer_name}' -> {output_file} ({layer_pieces} pieces)")

        # Write XML to file
        tree = lxml.etree.ElementTree(root)
        tree.write(
            str(output_file),
            xml_declaration=True,
            encoding="utf-8",
            pretty_print=True
        )

        total_files += 1
        total_pieces += layer_pieces

    log.info(f"Exported {total_pieces} mesh pieces across {total_files} layer files to {output_dir}")
