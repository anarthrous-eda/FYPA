"""Lean numeric-arrays representation of a padne ``Solution``.

padne's :class:`pdnsolver.solver.Solution` carries the full half-edge mesh
data structure (Vertex / Face / HalfEdge objects with cross-references).
That's exactly the right shape for the solver and the original padne UI,
but pickling it is wildly wasteful — a 71-vertex mesh balloons to ~42 KB
of pickle metadata when the actual data is ~2 KB of (x, y) floats and
triangle indices.

This module defines a parallel "lean" representation that stores only
the numpy arrays the viewer actually reads, and an adapter
:func:`to_lean_solution` that converts a padne Solution into it. Cache
pickles go from ~240 MB → ~3 MB on a typical 8-layer mid-sized board,
and load times drop from seconds to ~100 ms.

The viewer was refactored to consume the lean format directly, so all
new pickles are lean. The load path also accepts legacy padne pickles
and converts them on the fly for backward compatibility.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any

import numpy as np


@dataclass
class LeanLayer:
    """Per-(physical_layer, net) layer descriptor used by the viewer.

    All fields are pickle-cheap: name + numeric scalars + a Shapely
    Polygon/MultiPolygon (which Shapely serialises compactly via WKB)."""
    name: str
    conductance: float
    shape: Any                       # shapely Polygon | MultiPolygon
    layer_id: int = 0
    is_plane: bool = False
    plane_net_name: str | None = None


@dataclass
class LeanLayerSolution:
    """Per-padne-layer solve output as flat numpy arrays.

    The lists are per *connected component* of the layer's mesh (which
    padne builds one Mesh per component). All four lists have the same
    length and the i-th entry of each describes the i-th component.

    * ``vertex_xys[i]``     — ``(N_i, 2)`` float64
    * ``triangles[i]``      — ``(M_i, 3)`` int32 (indices into vertex_xys[i])
    * ``potentials[i]``     — ``(N_i,)`` float64 (per-vertex voltage, V)
    * ``power_densities[i]``— ``(M_i,)`` float64 (per-face W/mm²) OR
                              ``None`` if the solver didn't compute them
    """
    vertex_xys: list[np.ndarray]
    triangles: list[np.ndarray]
    potentials: list[np.ndarray]
    power_densities: list[np.ndarray | None]
    # Per-triangle sheet conductance per mesh; empty unless the
    # electro-thermal loop ran (otherwise the layer's ``conductance``).
    tri_conductances: list[np.ndarray] = field(default_factory=list)
    # Copper-sensitivity adjoint fields: key -> per-mesh float32 deltas plus
    # the per-mesh float64 offsets (pdnsolver.sensitivity.mesh_adjoint).
    # Pickles from before these existed lack the attributes entirely — read
    # them with getattr(..., None).
    adjoints: dict[str, list[np.ndarray]] = field(default_factory=dict)
    adjoint_offsets: dict[str, list[float]] = field(default_factory=dict)


@dataclass
class LeanProblem:
    """Subset of padne's :class:`Problem` actually consumed by the viewer.
    Networks / connections are deliberately omitted — they're solver
    inputs, not viewer inputs, and they pickle large."""
    layers: list[LeanLayer]
    project_name: str | None = None


@dataclass
class LeanSolution:
    problem: LeanProblem
    layer_solutions: list[LeanLayerSolution]
    solver_info: dict = field(default_factory=dict)
    # key -> {"objective_v", "current_a"} per solved sensitivity target (see
    # pdnsolver.solver.Solution.sensitivity). Absent on older pickles.
    sensitivity: dict = field(default_factory=dict)


def to_lean_solution(padne_solution) -> LeanSolution:
    """Convert a padne :class:`Solution` into the lean numeric form.

    Walks the half-edge structure once and packs every mesh into flat
    numpy arrays. After this returns, the padne Solution can be
    discarded — the viewer no longer needs it.
    """
    # --- Lean Problem (just the per-layer descriptors) ---
    lean_layers: list[LeanLayer] = []
    for L in padne_solution.problem.layers:
        lean_layers.append(LeanLayer(
            name=getattr(L, "name", ""),
            conductance=float(getattr(L, "conductance", 0.0)),
            shape=getattr(L, "shape", None),
            layer_id=int(getattr(L, "layer_id", 0)),
            is_plane=bool(getattr(L, "is_plane", False)),
            plane_net_name=getattr(L, "plane_net_name", None),
        ))
    lean_problem = LeanProblem(
        layers=lean_layers,
        project_name=getattr(padne_solution.problem, "project_name", None),
    )

    # --- Lean layer solutions: flatten each Mesh half-edge to arrays ---
    #
    # Fast path: each Mesh built by Mesher.poly_to_mesh retains the source
    # triangle-soup arrays it was constructed from (_source_xys + _source_tris)
    # — vertex coordinates and triangle indices in the *exact* order required
    # by the lean format. Read them directly instead of walking the half-edge
    # graph in Python (used to be O(V + F) per mesh of pure interpreter work).
    # Slow path (legacy/handbuilt meshes without those attributes) falls back
    # to the original iteration so we never lose data.
    lean_layer_solutions: list[LeanLayerSolution] = []
    for layer_i, ls in enumerate(padne_solution.layer_solutions):
        # Drop the GIL between layers so the GUI thread's progress-dialog
        # timer can fire — without this, a large multi-layer board can
        # monopolise the GIL through the whole flatten loop and trigger
        # Windows' "Not Responding" watchdog.
        if layer_i:
            time.sleep(0.001)
        n_meshes = len(ls.meshes)
        pds_src = ls.power_densities if ls.power_densities else [None] * n_meshes
        xys_list: list[np.ndarray] = []
        tris_list: list[np.ndarray] = []
        pots_list: list[np.ndarray] = []
        pds_list: list[np.ndarray | None] = []
        for mesh_i, (m, pot, pd) in enumerate(zip(ls.meshes, ls.potentials, pds_src)):
            if mesh_i and (mesh_i & 0x3F) == 0:
                time.sleep(0.001)
            n_v = len(m.vertices)
            src_xys = getattr(m, "_source_xys", None)
            src_tris = getattr(m, "_source_tris", None)
            if (src_xys is not None and src_tris is not None
                    and src_xys.shape[0] == n_v):
                xys = np.asarray(src_xys, dtype=np.float64)
                tris = np.asarray(src_tris, dtype=np.int32)
            else:
                xys = np.empty((n_v, 2), dtype=np.float64)
                for vt in m.vertices:
                    xys[vt.i, 0] = vt.p.x
                    xys[vt.i, 1] = vt.p.y
                # Triangles — skip any face that isn't a triangle (defensive;
                # padne's mesher only ever emits triangles).
                tri_rows: list[tuple[int, int, int]] = []
                for face in m.faces:
                    verts = list(face.vertices)
                    if len(verts) == 3:
                        tri_rows.append((verts[0].i, verts[1].i, verts[2].i))
                tris = (np.asarray(tri_rows, dtype=np.int32)
                        if tri_rows else np.empty((0, 3), dtype=np.int32))
            xys_list.append(xys)
            tris_list.append(tris)
            pots_list.append(np.asarray(pot.values, dtype=np.float64))
            if pd is not None:
                pds_list.append(np.asarray(pd.values, dtype=np.float64))
            else:
                pds_list.append(None)
        lean_layer_solutions.append(LeanLayerSolution(
            vertex_xys=xys_list,
            triangles=tris_list,
            potentials=pots_list,
            power_densities=pds_list,
            tri_conductances=[np.asarray(c, dtype=np.float64) for c in
                              (getattr(ls, "tri_conductances", None) or [])],
            adjoints={k: list(v) for k, v in
                      (getattr(ls, "adjoints", None) or {}).items()},
            adjoint_offsets={k: list(v) for k, v in
                             (getattr(ls, "adjoint_offsets", None) or {}).items()},
        ))

    # solver_info on padne is a SolverInfo dataclass; on lean we store
    # it as a plain dict (pickle-cheap, no padne import required to read).
    raw_info = getattr(padne_solution, "solver_info", None)
    if raw_info is None:
        info_dict: dict = {}
    elif is_dataclass(raw_info):
        info_dict = asdict(raw_info)
    elif isinstance(raw_info, dict):
        info_dict = dict(raw_info)
    else:
        info_dict = {k: getattr(raw_info, k) for k in dir(raw_info)
                     if not k.startswith("_") and not callable(getattr(raw_info, k))}
    return LeanSolution(
        problem=lean_problem,
        layer_solutions=lean_layer_solutions,
        solver_info=info_dict,
        sensitivity=dict(getattr(padne_solution, "sensitivity", None) or {}),
    )
