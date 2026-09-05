"""
ParaView VTK XML export functionality for FEM simulation results.

This module provides functions to export padne's FEM simulation results to the
VTK XML UnstructuredGrid format, compatible with ParaView and other VTK-based
visualization tools.
"""

import logging
from pathlib import Path
from collections.abc import Iterable

import lxml.etree
import numpy as np
from lxml.etree import Element, SubElement

from . import mesh, solver
from .vtu_fields import VTK_HEADER_TYPE, sanitize_filename, vtk_binary_data

log = logging.getLogger(__name__)


def create_data_array(
    parent: Element,
    data_type: str,
    values: Iterable[int | float] | np.ndarray,
    name: str | None = None,
    number_of_components: int | None = None
) -> Element:
    """Create a DataArray element with specified type and values.

    The payload is written as inline base64 binary (``format="binary"``)
    rather than whitespace-separated decimal text: ~8x faster to produce,
    about half the bytes on disk, and lossless for Float64 (no decimal
    round trip at all). See :func:`pdnsolver.vtu_fields.vtk_binary_data`.

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
    data_array.set("format", "binary")

    if name is not None:
        data_array.set("Name", name)

    if number_of_components is not None:
        data_array.set("NumberOfComponents", str(number_of_components))

    data_array.text = vtk_binary_data(values, data_type)

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
    # Width of the byte-count header prefixing each inline binary DataArray.
    root.set("header_type", VTK_HEADER_TYPE)
    return root


def create_point_data(potentials: mesh.ZeroForm) -> Element:
    """Create PointData element with voltage scalar field values.

    Args:
        potentials: ZeroForm containing scalar values at mesh vertices

    Returns:
        PointData element containing the voltage field data
    """
    point_data = Element("PointData")
    point_data.set("Scalars", "voltage")

    # Extract values in vertex index order. ZeroForm.values already *is*
    # that array, so use it directly rather than materialising every lazy
    # Vertex stub just to index back into it.
    vertex_values = getattr(potentials, "values", None)
    if vertex_values is None:
        vertex_values = [potentials[vertex] for vertex in potentials.mesh.vertices]

    create_data_array(point_data, "Float64", vertex_values, name="voltage")
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

    # Extract coordinates in vertex index order with Y-axis negated.
    # _source_xys is the same (N, 2) data in the same order, so use it when
    # present instead of materialising every lazy Vertex stub.
    num_points = len(mesh_obj.vertices)
    xys = getattr(mesh_obj, "_source_xys", None)
    if xys is not None and xys.shape[0] == num_points:
        coordinates = np.zeros((num_points, 3), dtype=np.float64)
        coordinates[:, 0] = xys[:, 0]
        coordinates[:, 1] = -xys[:, 1]
    else:
        coordinates = []
        for vertex in mesh_obj.vertices:
            coordinates.extend([vertex.p.x, -vertex.p.y, 0.0])

    create_data_array(points, "Float64", coordinates, number_of_components=3)
    return points


def _extract_triangle_connectivity(mesh_obj: mesh.Mesh) -> np.ndarray:
    """Extract triangle connectivity as an ``(M, 3)`` int array.

    A mesh built by ``Mesh.from_triangle_soup`` / ``from_triangle_arrays``
    already carries exactly this array as ``_source_tris`` (one row per
    interior face, in ``mesh.faces`` order, indexing ``mesh.vertices``), so
    return it directly. The half-edge walk below is the fallback for older
    or hand-built meshes that have no source arrays: it materialises every
    lazy Vertex/Face stub and builds a dict keyed on Vertex objects, which
    costs seconds on a large board.

    Args:
        mesh_obj: Mesh object with source arrays or half-edge topology

    Returns:
        ``(M, 3)`` array of (v0, v1, v2) vertex indices
    """
    tris = getattr(mesh_obj, "_source_tris", None)
    if tris is not None and tris.shape[0] > 0:
        return tris

    triangles = []
    vertex_to_index = {vertex: i for i, vertex in enumerate(mesh_obj.vertices)}

    for face in mesh_obj.faces:
        if face.is_boundary:
            continue

        # Extract vertices from face edges
        face_vertices = []
        for edge in face.edges:
            vertex_idx = vertex_to_index[edge.origin]
            face_vertices.append(vertex_idx)

        # Ensure we have exactly 3 vertices for a triangle
        if len(face_vertices) == 3:
            triangles.append(tuple(face_vertices))
        else:
            log.warning(f"Non-triangular face with {len(face_vertices)} vertices, skipping")

    if not triangles:
        return np.empty((0, 3), dtype=np.int64)
    return np.asarray(triangles, dtype=np.int64)


def create_cells(mesh_obj: mesh.Mesh) -> Element:
    """Create Cells element with triangle connectivity, offsets, and types.

    Args:
        mesh_obj: Mesh object containing triangular faces

    Returns:
        Cells element with connectivity, offsets, and types arrays
    """
    cells = Element("Cells")
    triangles = _extract_triangle_connectivity(mesh_obj)
    num_cells = int(triangles.shape[0])

    # Connectivity array
    create_data_array(cells, "Int32", triangles.reshape(-1), name="connectivity")

    # Offsets array
    offset_values = np.arange(3, 3 * num_cells + 1, 3, dtype=np.int64)
    create_data_array(cells, "Int32", offset_values, name="offsets")

    # Types array (all triangles = type 5)
    type_values = np.full(num_cells, 5, dtype=np.uint8)
    create_data_array(cells, "UInt8", type_values, name="types")

    return cells


def create_piece(mesh_obj: mesh.Mesh, potentials: mesh.ZeroForm) -> Element:
    """Create a Piece element representing one triangular mesh with voltage data.

    Args:
        mesh_obj: Triangular mesh object
        potentials: Scalar field values at mesh vertices

    Returns:
        Piece element containing mesh geometry and voltage field
    """
    # Modern meshes are built with build_halfedges=False (the solver reads the
    # flat _source_xys / _source_tris arrays directly) and this exporter now
    # reads those same arrays. Only a mesh without them — older or hand-built
    # — needs the half-edge graph materialised for the fallback walk in
    # _extract_triangle_connectivity. _build_halfedges is idempotent, but it
    # is also expensive, so skip it entirely on the fast path.
    source_tris = getattr(mesh_obj, "_source_tris", None)
    have_source_tris = source_tris is not None and source_tris.shape[0] > 0
    if not have_source_tris:
        mesh_obj._build_halfedges()

    num_points = len(mesh_obj.vertices)
    if have_source_tris:
        # mesh.faces holds one Face per source triangle and none of them are
        # boundary faces (those live in mesh.boundaries), so this is the same
        # count the filtered walk produces — without building the stubs.
        num_cells = int(source_tris.shape[0])
    else:
        num_cells = len([f for f in mesh_obj.faces if not f.is_boundary])

    piece = Element("Piece")
    piece.set("NumberOfPoints", str(num_points))
    piece.set("NumberOfCells", str(num_cells))

    # Add sub-elements
    piece.append(create_point_data(potentials))
    piece.append(create_points(mesh_obj))
    piece.append(create_cells(mesh_obj))

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

        # Skip layers with no meshes
        meshes_and_potentials = [
            (mesh_obj, potential)
            for mesh_obj, potential in
            zip(layer_solution.meshes, layer_solution.potentials)
        ]

        if not meshes_and_potentials:
            log.warning(f"Skipping layer '{layer_name}' - no non-empty meshes")
            continue

        # Generate sanitized filename
        filename = sanitize_filename(layer_name, used_names)
        output_file = output_dir / f"{filename}.vtu"

        # Create root structure for this layer
        root = create_vtk_root()
        unstructured_grid = SubElement(root, "UnstructuredGrid")

        # Add all meshes in this layer as pieces
        layer_pieces = 0
        for mesh_obj, potential in meshes_and_potentials:
            piece = create_piece(mesh_obj, potential)
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
