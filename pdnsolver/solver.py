

import collections
import ctypes
import hashlib
import itertools
import logging
import math
import os
import threading
import time
import numpy as np
import scipy.sparse
import scipy.sparse.linalg
import scipy.spatial
import shapely
import shapely.geometry
import shapely.wkb
import warnings

from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field

from . import problem, mesh

log = logging.getLogger(__name__)

# Optional fast direct solver. MKL PARDISO (via pypardiso) factorises the
# 2.5D-PDN MNA matrix several times faster than scipy's SuperLU and is
# multithreaded. When it isn't installed — or fails to import, e.g. an MKL
# runtime DLL problem — the solver falls back to SuperLU transparently.
try:
    import pypardiso as _pypardiso
    _HAVE_PARDISO = True
except Exception:  # ImportError, or an MKL runtime load failure
    _pypardiso = None
    _HAVE_PARDISO = False

# MKL PARDISO thread count. Sparse direct factorisation parallelises only
# so far: the elimination tree has limited width and memory bandwidth
# saturates. Measured on this MNA workload (factor+solve, best of 3):
#
#       threads     360k DOF     ~2M DOF
#       1            978 ms         --
#       MKL default  ~520 ms     2552 ms
#       8            ~390 ms     2260 ms
#       12             --        2214 ms
#       16 (all)     ~432 ms     2824 ms   <- worst: oversubscription
#
# The MKL default heuristic is ~13 % off the optimum and pinning to *all*
# logical cores is the slowest of all. 8 threads sits at the sweet spot at
# both sizes, and the real 2.5D-PDN matrix has less fill-in than the dense
# 2D-Laplacian proxy used to measure this, so its optimum is at or below 8.
# Cap at 8, never exceed the machine's core count; override via env.
_PARDISO_THREADS: int = int(
    os.environ.get("PDNSOLVER_PARDISO_THREADS", "0")
) or min(8, os.cpu_count() or 8)

_mkl_threads_configured = False


def _configure_mkl_threads() -> None:
    """Pin MKL's thread count for PARDISO. Idempotent and best-effort —
    a failure here just leaves MKL on its (suboptimal) default."""
    global _mkl_threads_configured
    if _mkl_threads_configured or not _HAVE_PARDISO:
        return
    _mkl_threads_configured = True  # set first: don't retry on failure
    try:
        # pypardiso already located and loaded mkl_rt; reuse that handle.
        libmkl = _pypardiso.PyPardisoSolver().libmkl
        libmkl.MKL_Set_Num_Threads(ctypes.c_int(_PARDISO_THREADS))
        log.debug("MKL PARDISO thread count pinned to %d", _PARDISO_THREADS)
    except Exception as e:  # environment-dependent — never fatal
        log.debug("Could not pin MKL thread count (%s); using MKL default.", e)


DTYPE = np.float64

# Below this work-item count, parallel meshing's pool-spawn overhead
# (~500 ms total for an 8-worker pool on Windows) exceeds the saving, so
# we stay serial. Tuned empirically; tweak via env if needed.
_MESH_PARALLEL_THRESHOLD: int = int(
    os.environ.get("PDNSOLVER_MESH_PARALLEL_THRESHOLD", "4"),
)
# Cap workers — meshing is CPU-bound and triangulate is single-threaded
# inside one call, so going above the physical core count just adds
# context-switching overhead. Use cpu_count() // 2 as a rough heuristic
# for "physical cores" on machines with SMT/hyperthreading; user can
# override with PDNSOLVER_MESH_MAX_WORKERS.
_MESH_MAX_WORKERS_DEFAULT = max(1, (os.cpu_count() or 1) // 2 or 1)
_MESH_MAX_WORKERS: int = int(
    os.environ.get("PDNSOLVER_MESH_MAX_WORKERS", str(_MESH_MAX_WORKERS_DEFAULT)),
)

# --- Linear-solver tolerances -----------------------------------------------
# Lifted out of inline literals in ``_solve_robust`` so the whole tolerance
# stack is discoverable and tunable from one place. The values are unchanged —
# see each use site in ``_solve_robust`` for the rationale behind them.
#
# Direct-solve residual acceptance: a direct solve is trusted only if
# ``||L·v - r|| <= max(_DIRECT_SOLVE_ABS_TOL_FLOOR, _DIRECT_SOLVE_REL_TOL·||r||)``;
# otherwise the solver falls back to MINRES.
_DIRECT_SOLVE_ABS_TOL_FLOOR: float = 1e-9
_DIRECT_SOLVE_REL_TOL: float = 1e-6
# Jacobi preconditioner diagonal floor: ``eps = max(_JACOBI_EPS_FLOOR,
# _JACOBI_EPS_REL·max|diag|)`` guards the 1/|diag| inversion against the
# zero-diagonal MNA Lagrange rows.
_JACOBI_EPS_FLOOR: float = 1e-12
_JACOBI_EPS_REL: float = 1e-10
# MINRES fallback: relative tolerance and iteration ceiling.
_MINRES_RTOL: float = 1e-10
_MINRES_MAXITER: int = 5000
# MINRES fallback wall-clock budget. A 1–2 M-variable symmetric-indefinite
# system with only a Jacobi preconditioner can need thousands of iterations;
# without a budget the solve looks frozen (no GUI feedback) and the user
# cancels it. On timeout the best iterate so far is kept. Override via env
# for very large boards.
_MINRES_TIME_BUDGET_S: float = float(
    os.environ.get("PDNSOLVER_MINRES_BUDGET_S", "180"),
)
# Log MINRES progress every N iterations so the GUI substage feed shows the
# iterative solve advancing instead of appearing hung.
_MINRES_PROGRESS_EVERY: int = 250
# When every direct solve fails, report this many worst-residual rows — they
# localise the near-floating copper region that drove the matrix singular.
_SINGULAR_DIAG_ROWS: int = 12
# Tikhonov ridge (last-resort regularisation): ``lambda = max(
# _RIDGE_LAMBDA_FLOOR, _RIDGE_LAMBDA_REL·max|diag|)``.
_RIDGE_LAMBDA_FLOOR: float = 1e-9
_RIDGE_LAMBDA_REL: float = 1e-9

# Steiner ring placed around every Connection point to force the mesher to
# produce small triangles around 2D point-current injection vertices. Without
# this, coarse triangles regularise the log singularity at each pin over too-
# large an area and the FEM systematically under-estimates IR drop on narrow
# conductors — the error grows with conductor length (verified empirically:
# a 1 mm × 100 mm trace at the default 0.6 mm mesh size came out at 25.7 mΩ
# vs the analytical 47.3 mΩ; the same trace with these ring seeds in place
# lands within ~5 % of theory at the same global mesh size).
#
# 25 µm (~1 mil) is smaller than any standard PCB trace width, so the ring
# fits inside even fine-line geometry. Ring points that fall outside the
# containing polygon are filtered by the caller.
_INJECTION_STEINER_RING_RADIUS_MM: float = 0.025
_INJECTION_STEINER_RING_POINTS: int = 8


def _injection_steiner_ring(
    center: shapely.geometry.Point,
    radius_mm: float = _INJECTION_STEINER_RING_RADIUS_MM,
    n: int = _INJECTION_STEINER_RING_POINTS,
) -> list[shapely.geometry.Point]:
    """Return n evenly-spaced points on a small circle around ``center``."""
    cx, cy = center.x, center.y
    step = 2.0 * math.pi / n
    return [
        shapely.geometry.Point(
            cx + radius_mm * math.cos(step * i),
            cy + radius_mm * math.sin(step * i),
        )
        for i in range(n)
    ]


# Equipotential-patch seeding. A directive terminal couples into its pad as
# an equipotential patch (see solve()'s contraction step): every mesh vertex
# under the pad is tied to one node. For that to be meaningful the mesher
# must actually place vertices around the pad, so every Connection carrying
# a ``region`` seeds points evenly along the pad outline. The spacing is
# fine enough that even a small SMT pad gets a ring of boundary vertices for
# current to spread through.
_PAD_SEED_SPACING_MM: float = 0.1
_PAD_SEED_MIN_POINTS: int = 8
_PAD_SEED_MAX_POINTS: int = 64

# Tolerance for "is this mesh vertex under the pad". Pad-outline seed
# vertices land exactly on the region boundary, where shapely's strict
# ``contains`` is False — so the membership test runs against the region
# buffered out by this epsilon. 5 µm is far below any pad/trace dimension.
_PAD_MEMBERSHIP_EPS_MM: float = 0.005


def _pad_seed_points(
    region: shapely.geometry.Polygon,
    point: shapely.geometry.Point,
) -> list[shapely.geometry.Point]:
    """Seed points for an equipotential-patch Connection: the pad centroid
    plus evenly-spaced samples around the pad outline. Points that fall off
    the copper are filtered later by the per-geometry containment test."""
    pts = [shapely.geometry.Point(point.x, point.y)]
    exterior = getattr(region, "exterior", None)
    if exterior is None or exterior.is_empty:
        return pts
    perimeter = exterior.length
    if perimeter <= 0.0:
        return pts
    n = int(perimeter / _PAD_SEED_SPACING_MM)
    n = max(_PAD_SEED_MIN_POINTS, min(_PAD_SEED_MAX_POINTS, n))
    for i in range(n):
        sample = exterior.interpolate(perimeter * i / n)
        pts.append(shapely.geometry.Point(sample.x, sample.y))
    return pts


def _vertices_under_pad(
    kdtree: scipy.spatial.KDTree,
    globals_arr: np.ndarray,
    region: shapely.geometry.Polygon,
    point: shapely.geometry.Point,
    claimed: set[int],
) -> np.ndarray:
    """Global indices of the mesh vertices that lie under ``region`` (a pad
    outline), with the vertex nearest ``point`` placed first as the group's
    representative.

    Vertices already in ``claimed`` are excluded so pad groups stay disjoint
    and the contraction in :func:`solve` is a clean partition. Returns an
    empty array when the pad catches no free vertex.

    The membership polygon is ``region`` buffered out by
    :data:`_PAD_MEMBERSHIP_EPS_MM` so pad-outline seed vertices — which sit
    exactly on the boundary, where strict ``contains`` is False — count.

    Candidates are pulled with a KDTree ball query over the pad's bounding
    circle — O(log n + k) — rather than an O(n) scan of every vertex on the
    layer, which is wasteful when many directives land on a million-vertex
    power/ground net.
    """
    data = kdtree.data  # (n, 2) array of vertex (x, y)
    if data.size == 0:
        return np.empty(0, dtype=np.int64)

    minx, miny, maxx, maxy = region.bounds
    eps = _PAD_MEMBERSHIP_EPS_MM
    cx, cy = 0.5 * (minx + maxx), 0.5 * (miny + maxy)
    radius = 0.5 * math.hypot(maxx - minx, maxy - miny) + eps
    cand_local = np.asarray(
        kdtree.query_ball_point((cx, cy), radius), dtype=np.int64,
    )
    if cand_local.size == 0:
        return np.empty(0, dtype=np.int64)

    member_region = region.buffer(eps)
    cand_pts = shapely.points(data[cand_local, 0], data[cand_local, 1])
    inside = shapely.contains(member_region, cand_pts)
    sel_local = cand_local[np.asarray(inside, dtype=bool)]
    if sel_local.size == 0:
        return np.empty(0, dtype=np.int64)

    sel_globals = globals_arr[sel_local].astype(np.int64, copy=False)
    if claimed:
        free = np.fromiter(
            (g not in claimed for g in sel_globals),
            dtype=bool, count=sel_globals.size,
        )
        sel_local = sel_local[free]
        sel_globals = sel_globals[free]
    if sel_globals.size == 0:
        return np.empty(0, dtype=np.int64)

    # Representative = the vertex nearest the nominal connection point.
    dx = data[sel_local, 0] - point.x
    dy = data[sel_local, 1] - point.y
    rep_pos = int(np.argmin(dx * dx + dy * dy))
    order = np.concatenate((
        [rep_pos],
        np.delete(np.arange(sel_globals.size), rep_pos),
    ))
    return sel_globals[order]


def _build_contraction(
    N: int, vertex_groups: list[np.ndarray],
) -> "tuple[np.ndarray, int] | None":
    """Build the index remap that collapses each equipotential-patch vertex
    group into a single variable.

    Returns ``(inverse, M)`` where ``inverse`` is a length-``N`` array
    mapping every original variable index to its reduced index in
    ``[0, M)``, or ``None`` when there is nothing to contract. Grouped
    vertices share their group's reduced index; everything else keeps a
    unique one. Original index order is preserved, so the ground node
    (original index ``N - 1``) stays last in the reduced system.

    Built with an O(N) cumulative-sum rank rather than ``np.unique`` (an
    O(N log N) sort): ``parent`` is ``arange(N)`` with only the handful of
    grouped members rewritten, so sorting it is wasted work on a
    multi-million-variable system.
    """
    groups = [g for g in vertex_groups if len(g) >= 2]
    if not groups:
        return None
    # parent[i] = the representative of i's group, or i itself.
    # removed[i] = True for a non-representative group member (dropped).
    parent = np.arange(N, dtype=np.int64)
    removed = np.zeros(N, dtype=bool)
    for group in groups:
        g = np.asarray(group, dtype=np.int64)
        parent[g[1:]] = int(g[0])
        removed[g[1:]] = True
    # Reduced index of a kept original index = its rank among kept indices.
    # parent only ever points at kept indices, so new_index[parent] gives
    # the reduced index of every original variable.
    new_index = np.cumsum(~removed, dtype=np.int64) - 1
    inverse = new_index[parent]
    return inverse, int(new_index[-1]) + 1


# Module-level handle on the meshing pool currently in flight. The GUI's
# abort path (see :func:`cancel_active_mesh_pool`) shuts this down so a
# user-cancelled solve doesn't leak worker processes that keep running
# their Triangle call to completion in the background.
_active_mesh_pool: ProcessPoolExecutor | None = None
_active_mesh_pool_lock = threading.Lock()


def cancel_active_mesh_pool() -> None:
    """Tear down any in-flight meshing pool. Safe to call from any thread
    and safe to call when no pool is active.

    Public API for the GUI's solve-cancel path. Internally calls
    ``pool.shutdown(cancel_futures=True, wait=False)`` — already-running
    Triangle calls in worker processes will still finish their current
    polygon (we can't kill a C library mid-call), but no further work is
    dispatched and the pool's queues are torn down so the workers exit
    once their current task returns.
    """
    with _active_mesh_pool_lock:
        pool = _active_mesh_pool
    if pool is not None:
        try:
            pool.shutdown(cancel_futures=True, wait=False)
        except Exception as e:
            log.warning(f"cancel_active_mesh_pool: shutdown failed ({e})")


class SolverWarning(Warning):
    """
    A warning that is raised by the solver when it encounters a problem
    that does not prevent it from solving the problem, but may indicate
    a potential issue with the problem definition.
    """


# A connection point farther than this many mesh cells (× the mesher's
# maximum edge length) from the nearest copper vertex on its net is treated
# as "off copper" and warned about — see NodeIndexer.create.
_OFF_COPPER_WARN_FACTOR = 3.0


@dataclass(frozen=True)
class SolverInfo:
    """Diagnostic information from the solver."""
    ground_node_current: float  # Should be ~0 for well-posed problems
    residual_norm: float        # ||L @ v - r||, should be ~0 for solved systems


@dataclass
class LayerSolution:
    meshes: list[mesh.Mesh]
    potentials: list[mesh.ZeroForm]
    power_densities: list[mesh.TwoForm] = field(default_factory=list)
    disconnected_meshes: list[mesh.Mesh] = field(default_factory=list)


@dataclass
class Solution:
    problem: problem.Problem
    layer_solutions: list[LayerSolution]
    solver_info: SolverInfo


def construct_strtrees_from_layers(layers: list[problem.Layer]
                                   ) -> list[shapely.strtree.STRtree]:
    """
    Construct STRtrees for each layer in the problem.

    Args:
        layers: List of layers to construct STRtrees for

    Returns:
        List of STRtrees, one for each layer
    """
    strtrees = []
    for layer in layers:
        strtree = shapely.strtree.STRtree(layer.geoms)
        strtrees.append(strtree)
    return strtrees


@dataclass
class ConnectivityGraph:
    nodes: list["Node"] = field(default_factory=list)

    @dataclass(eq=False)
    class Node:
        layer_i: int  # Index of the layer in the Problem
        geom_i: int   # Index of this particular polygon in the layer.geoms tuple
        is_root: bool = False
        neighbors: set["ConnectivityGraph.Node"] = field(default_factory=set)

    @classmethod
    def create_from_problem(cls,
                            problem: problem.Problem,
                            strtrees: list[shapely.strtree.STRtree]) -> "ConnectivityGraph":
        # First, we construct Node objects for ever layer geometry in the layers
        # that is, a list nodes_by_layers[layer_i][geom_i] gives us the
        # Node that coresponds to the layer_i-th layers geom_i-th geometry
        # object.
        nodes_by_layers = []
        for layer_i, layer in enumerate(problem.layers):
            nodes_by_layers.append(
                [cls.Node(layer_i=layer_i, geom_i=geom_i)
                 for geom_i, geom in enumerate(layer.geoms)]
            )

        # Pre-build id(layer) → index lookup so we don't pay O(L) per
        # connection in the inner loop. ``problem.layers.index(...)`` is
        # called once per connection per network — for boards with many
        # directives that's O(L · K) work for no reason. id() works as a
        # key because Layer is a frozen dataclass containing an unhashable
        # MultiPolygon (so hash() would fail) and the SAME Layer instance
        # is used in conn.layer everywhere.
        layer_to_index = {id(layer): i for i, layer in enumerate(problem.layers)}

        # And finally, we walk through each of the networks, figure out
        # which Nodes are connected to each of the Connection and then
        # consider those Nodes connected to each other.
        for network in problem.networks:
            nodes_in_this_network = []
            for conn in network.connections:
                # Find the layer index for this connection
                layer_i = layer_to_index[id(conn.layer)]
                kdtree = strtrees[layer_i]

                # Find the closest vertex to this connection
                candidates = kdtree.query(conn.point)

                for geom_i in candidates:
                    # Check if this connection is already in the index
                    if not conn.layer.geoms[geom_i].intersects(conn.point):
                        continue
                    intersecting_node = nodes_by_layers[layer_i][geom_i]
                    nodes_in_this_network.append(intersecting_node)

                    if network.has_source:
                        intersecting_node.is_root = True
            # Wire the nodes together
            for node_a, node_b in itertools.combinations(nodes_in_this_network, 2):
                node_a.neighbors.add(node_b)
                node_b.neighbors.add(node_a)

        # And finally flatten the list of nodes into a single list
        nodes = [
            node for xs in nodes_by_layers for node in xs
        ]

        return cls(nodes=nodes)

    def compute_connected_nodes(self) -> list[Node]:
        """
        Return a list of all nodes that are either root nodes themselves
        or are connected to a root node via any connection.
        """
        open_set = {n for n in self.nodes if n.is_root}
        closed_set = set()

        while open_set:
            node = open_set.pop()
            closed_set.add(node)
            for neighbor in node.neighbors:
                if neighbor not in closed_set:
                    open_set.add(neighbor)

        return list(closed_set)


def collect_seed_points(problem: problem.Problem, layer: problem.Layer) -> list[mesh.Point]:
    """
    Collect all seed points (component pads) that are on this layer.

    Args:
        problem: The entire problem containing all lumped elements
        layer: The specific layer to collect seed points for

    Returns:
        List of Points to be used as mesh seed points
    """
    seed_points = []
    for network in problem.networks:
        for conn in network.connections:
            # Check if this connection is on our layer
            if conn.layer == layer:
                seed_points.append(mesh.Point(conn.point.x, conn.point.y))
    return seed_points


# Clamp on |half-cotangent|. cot(θ) → ±∞ as θ → 0 or π, so a single sliver
# triangle can otherwise produce a ~1e18 matrix entry that dwarfs every real
# conductance and wrecks the factorisation. 5e3 corresponds to apex angles down
# to ~0.0057°; Triangle normally emits angles ≥ 20° (|cot| ≈ 2.75), so the clamp
# is only hit on degenerate output.
_MAX_HALF_COT = 5.0e3


def _half_cotangent(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """cot(θ) / 2 for the apex angle θ between an apex's two outgoing edges
    ``a`` and ``b``, computed for every triangle at once.

    cot θ = cos θ / sin θ = (a·b) / |a×b|. The apex angle of a triangle is
    always in (0, π), so sin θ > 0 and the cross magnitude carries no sign —
    the SIGN of cot θ comes solely from the dot product, so obtuse apices
    (θ > 90°, a·b < 0) correctly yield a NEGATIVE cotangent. Dropping that sign
    (taking |cot|) makes the linear-FEM stiffness weight (cot α + cot β)/2
    inconsistent: it over-conducts obtuse triangles, so solved trace resistance
    comes out low and does not converge under mesh refinement. The magnitude is
    clamped symmetrically at :data:`_MAX_HALF_COT` (see there)."""
    dot = a[:, 0] * b[:, 0] + a[:, 1] * b[:, 1]
    crs = a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]
    out = np.zeros_like(dot)
    mask = crs != 0
    out[mask] = dot[mask] / np.abs(crs[mask])
    np.clip(out, -_MAX_HALF_COT, _MAX_HALF_COT, out=out)
    return out * 0.5


def _mesh_source_arrays(msh: mesh.Mesh) -> tuple[np.ndarray, np.ndarray]:
    """Return ``(xys (N, 2) float, tris (T, 3) int64)`` for a mesh.

    Prefers the flat triangle-soup arrays retained by ``from_triangle_soup``
    (``_source_xys`` / ``_source_tris``); falls back to reconstructing them
    from the half-edge form for very old pickled or hand-built meshes."""
    N = len(msh.vertices)
    xys = getattr(msh, "_source_xys", None)
    tris = getattr(msh, "_source_tris", None)
    if xys is None or tris is None or (N > 0 and np.asarray(xys).size == 0):
        xys = np.empty((N, 2), dtype=DTYPE)
        for vt in msh.vertices:
            xys[vt.i, 0] = vt.p.x
            xys[vt.i, 1] = vt.p.y
        tri_rows: list[tuple[int, int, int]] = []
        for face in msh.faces:
            verts = list(face.vertices)
            if len(verts) == 3:
                tri_rows.append((verts[0].i, verts[1].i, verts[2].i))
        tris = (np.asarray(tri_rows, dtype=np.int64)
                if tri_rows else np.empty((0, 3), dtype=np.int64))
    return np.asarray(xys, dtype=DTYPE), np.asarray(tris, dtype=np.int64)


def laplace_operator(mesh: mesh.Mesh) -> scipy.sparse.coo_matrix:
    """Cotangent Laplacian for a single mesh as an (N, N) sparse COO matrix.

    Single-mesh reference implementation. The solve path builds every mesh's
    Laplacian at once through :func:`process_mesh_laplace_operators` (which
    shares the same :func:`_half_cotangent` kernel); this function is kept as
    the readable per-mesh form and is cross-checked against the batched path in
    the tests.

    The off-diagonal weight on each directed edge (i, k) is
    ``sum_t cot(opposite_apex_t) / 2`` over the (one or two) triangles t
    sharing the edge — the standard cotangent-Laplacian stiffness. Boundary
    edges get one half-cotangent (only one triangle touches them); interior
    edges get ``(cot α + cot β) / 2``. See :func:`_half_cotangent` for why the
    cotangent must be signed.

    Orphan vertices (in the vertex list but in no triangle — Triangle keeps
    input seed points even when FP drops them just outside the polygon) are
    pinned to v=0 with a ``1.0`` diagonal entry so the system stays
    non-singular.
    """
    N = len(mesh.vertices)
    xys, tris = _mesh_source_arrays(mesh)

    row_chunks: list[np.ndarray] = []
    col_chunks: list[np.ndarray] = []
    val_chunks: list[np.ndarray] = []

    if tris.shape[0] > 0:
        v0 = tris[:, 0]
        v1 = tris[:, 1]
        v2 = tris[:, 2]
        p0 = xys[v0]
        p1 = xys[v1]
        p2 = xys[v2]

        w_for_edge_v1_v2 = _half_cotangent(p1 - p0, p2 - p0)  # apex 0 ↔ (v1,v2)
        w_for_edge_v2_v0 = _half_cotangent(p2 - p1, p0 - p1)  # apex 1 ↔ (v2,v0)
        w_for_edge_v0_v1 = _half_cotangent(p0 - p2, p1 - p2)  # apex 2 ↔ (v0,v1)

        # Off-diagonal: L[i, k] += w on both directions of each edge.
        rows = np.concatenate([v1, v2, v2, v0, v0, v1])
        cols = np.concatenate([v2, v1, v0, v2, v1, v0])
        vals = np.concatenate([
            w_for_edge_v1_v2, w_for_edge_v1_v2,
            w_for_edge_v2_v0, w_for_edge_v2_v0,
            w_for_edge_v0_v1, w_for_edge_v0_v1,
        ])

        # Diagonal: L[i, i] -= sum of outgoing weights from i. np.add.at is
        # an atomic scatter — handles repeated row indices correctly.
        diag = np.zeros(N, dtype=DTYPE)
        np.add.at(diag, rows, -vals)

        row_chunks.append(rows)
        col_chunks.append(cols)
        val_chunks.append(vals)

        diag_idx = np.arange(N, dtype=np.int64)
        row_chunks.append(diag_idx)
        col_chunks.append(diag_idx)
        val_chunks.append(diag)

    # Pin orphan vertices to keep the matrix non-singular.
    if N > 0:
        used = np.zeros(N, dtype=bool)
        if tris.shape[0] > 0:
            used[tris.ravel()] = True
        orphans = np.where(~used)[0].astype(np.int64)
        if orphans.size > 0:
            row_chunks.append(orphans)
            col_chunks.append(orphans)
            val_chunks.append(np.ones(orphans.size, dtype=DTYPE))

    if row_chunks:
        rows_all = np.concatenate(row_chunks)
        cols_all = np.concatenate(col_chunks)
        vals_all = np.concatenate(val_chunks)
    else:
        rows_all = np.empty(0, dtype=np.int64)
        cols_all = np.empty(0, dtype=np.int64)
        vals_all = np.empty(0, dtype=DTYPE)

    return scipy.sparse.coo_matrix(
        (vals_all, (rows_all, cols_all)), shape=(N, N), dtype=DTYPE,
    )


@dataclass
class VertexIndexer:
    # Cumulative per-mesh vertex-count offsets, length ``len(meshes) + 1``:
    # mesh ``m`` owns the global vertex indices
    # ``[mesh_vertex_offsets[m], mesh_vertex_offsets[m + 1])``.
    #
    # This replaces the former per-vertex ``list[(mesh_idx, vertex_idx)]`` plus
    # a reverse ``dict`` — the two together built ~2N Python objects (≈400 MB
    # at 2M vertices) in an O(N) interpreter loop that showed up as the
    # multi-second "Vertex indexing" stage. The reverse dict was never read
    # anywhere. The forward map is only needed as (a) a total vertex count and
    # (b) a handful of single-index lookups outside any hot loop, both of which
    # the offsets array serves in O(1) / O(log meshes).
    mesh_vertex_offsets: np.ndarray = field(
        default_factory=lambda: np.zeros(1, dtype=np.int64)
    )

    @property
    def n_vertices(self) -> int:
        return int(self.mesh_vertex_offsets[-1])

    @classmethod
    def create(cls, meshes: list[mesh.Mesh]) -> "VertexIndexer":
        # Only the vertex COUNT per mesh is needed — never the stub objects —
        # so this stays O(meshes), not O(vertices).
        counts = np.fromiter(
            (len(msh.vertices) for msh in meshes),
            dtype=np.int64, count=len(meshes),
        )
        offsets = np.zeros(len(meshes) + 1, dtype=np.int64)
        np.cumsum(counts, out=offsets[1:])
        return cls(mesh_vertex_offsets=offsets)

    def to_mesh_vertex(self, global_index: int) -> tuple[int, int]:
        """Map a global vertex index back to ``(mesh_index, vertex_index)``."""
        mesh_i = int(np.searchsorted(
            self.mesh_vertex_offsets, global_index, side="right",
        ) - 1)
        return mesh_i, int(global_index - self.mesh_vertex_offsets[mesh_i])


def find_connected_layer_geom_indices(connectivity_graph: ConnectivityGraph
                                      ) -> set[tuple[int, int]]:
    connected_nodes = connectivity_graph.compute_connected_nodes()

    layer_mesh_pairs = set()
    for node in connected_nodes:
        layer_i = node.layer_i
        geom_i = node.geom_i
        layer_mesh_pairs.add((layer_i, geom_i))

    return layer_mesh_pairs


def _mesh_polygons_in_parallel(
    polys: list[shapely.geometry.Polygon],
    seed_xys: list[np.ndarray | None],
    switches: list[str],
    log_label: str,
    adaptive: tuple | None = None,
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Mesh a batch of polygons concurrently via a ProcessPoolExecutor.

    Inputs and outputs are kept in the cheapest cross-process form: WKB
    bytes in, raw ``(vertices, triangles)`` numpy arrays out — pickling
    the full :class:`mesh.Mesh` (which holds tens of thousands of tiny
    Vertex / Face Python stubs) would dominate runtime and erase the
    parallelism win.

    For < ``_MESH_PARALLEL_THRESHOLD`` polygons the pool is skipped and
    Triangle runs in-process — spawning workers for two or three pieces
    on a small board is a net loss.

    ``switches`` is one Triangle switches string *per polygon* — callers
    that don't need per-polygon variation should broadcast a single
    string ``[s] * len(polys)``. Per-polygon switches let the
    connected-mesh path apply :meth:`Mesher.polygon_adaptive_max_size`
    so narrow nets get finer meshes than wide pours.

    ``adaptive`` (when not None) switches every polygon to the
    variable-density two-pass mesher (:func:`mesh._triangulate_adaptive`);
    ``switches`` is then unused.

    Returns the per-polygon ``(out_vertices, out_triangles)`` in input
    order. Raises :class:`mesh.MeshingException` (the first one observed)
    if any worker fails.
    """
    n = len(polys)
    assert n == len(seed_xys), "polys / seed_xys length mismatch"
    assert n == len(switches), "polys / switches length mismatch"
    if n == 0:
        return []
    # Fall back to serial when the pool wouldn't pay for itself.
    if n < _MESH_PARALLEL_THRESHOLD:
        results: list[tuple[np.ndarray, np.ndarray]] = []
        for i, (poly, sxy, sw) in enumerate(zip(polys, seed_xys, switches), 1):
            if adaptive is not None:
                results.append(mesh._triangulate_adaptive(poly, sxy, adaptive))
            else:
                vertices, segments, holes = (
                    mesh._prepare_polygon_for_triangle_arrays(poly, sxy)
                )
                results.append(mesh._triangulate_arrays(
                    vertices, segments, holes, sw,
                ))
            if i == 1 or i == n or (i % 8 == 0):
                log.info(f"{log_label}: {i}/{n} pieces meshed (serial)")
        return results

    workers = min(n, _MESH_MAX_WORKERS)
    log.info(f"{log_label}: meshing {n} pieces across {workers} worker(s)")
    payloads = [shapely.wkb.dumps(p) for p in polys]
    results = [None] * n
    # spawn-mode pool on Windows; workers re-import pdnsolver.mesh and
    # pick up the top-level triangulate_worker by name.
    global _active_mesh_pool
    pool = ProcessPoolExecutor(max_workers=workers)
    with _active_mesh_pool_lock:
        _active_mesh_pool = pool
    try:
        future_to_idx = {
            pool.submit(
                mesh.triangulate_worker, payloads[i], seed_xys[i],
                switches[i], adaptive,
            ): i for i in range(n)
        }
        done = 0
        next_log = 1
        for fut in as_completed(future_to_idx):
            idx = future_to_idx[fut]
            try:
                results[idx] = fut.result()
            except mesh.MeshingException:
                # Propagate — workers' MeshingException already carries
                # the failed-input details. Other futures are cancelled
                # via the finally below.
                raise
            done += 1
            # Per-piece progress every doubling (1, 2, 4, 8, …) plus the
            # last one — gives a useful pulse without spamming for large
            # batches.
            if done >= next_log or done == n:
                log.info(f"{log_label}: {done}/{n} pieces meshed")
                next_log *= 2
    finally:
        with _active_mesh_pool_lock:
            _active_mesh_pool = None
        # cancel_futures=False on the success path: by here all futures
        # are already done. On the exception/abort path, futures may be
        # in flight; cancel_futures=True drains the input queue.
        pool.shutdown(cancel_futures=True, wait=True)
    return results  # type: ignore[return-value]


def generate_meshes_for_problem(prob: problem.Problem,
                                mesher: mesh.Mesher,
                                connected_layer_mesh_pairs: set[tuple[int, int]],
                                strtrees: list[shapely.strtree.STRtree]
                                ) -> tuple[list[mesh.Mesh], list[int]]:
    # Phase 1: assign seed points to geometries (per-layer, in-process).
    # Phase 2: collect every (layer, geom) to be meshed and the polygons /
    # seed arrays in deterministic order, then hand the whole batch to
    # the parallel mesher. Phase 3: rebuild Mesh stubs from the returned
    # arrays in the same order so mesh_index_to_layer_index is stable.

    # Variable-density (adaptive) meshing: when enabled, the connected
    # meshes use the two-pass graded mesher — fine near pins/vias/copper
    # edges, coarse in plane interiors. ``_adaptive`` is the picklable
    # parameter tuple handed to each Triangle worker; None = uniform.
    _cfg = mesher.config
    _adaptive = (
        (_cfg.minimum_angle, _cfg.maximum_size,
         _cfg.variable_size_maximum_factor,
         _cfg.variable_density_min_distance,
         _cfg.variable_density_max_distance)
        if _cfg.is_variable_density else None
    )

    # Pre-build a layer-id → seed-points map in ONE pass over all networks.
    # The previous code called ``collect_seed_points(prob, layer)`` inside
    # the per-layer loop, which made the total cost O(networks × layers).
    # On a board with 10k networks × 21 layers that loop alone took
    # ~60 s. Now we walk the network list exactly once.
    import shapely.prepared
    # Each entry is (seed_point, add_steiner_ring). A point Connection gets
    # an 8-point Steiner ring to refine the log singularity at its single
    # injection vertex; an equipotential-patch Connection (one carrying a
    # pad region) instead seeds the pad outline directly — the patch has no
    # point singularity, so no ring is needed.
    seed_points_by_layer_id: dict[int, list[tuple[shapely.geometry.Point, bool]]] = (
        collections.defaultdict(list)
    )
    for network in prob.networks:
        for conn in network.connections:
            if conn.layer is None:
                continue
            lid = id(conn.layer)
            if conn.region is not None:
                for sp in _pad_seed_points(conn.region, conn.point):
                    seed_points_by_layer_id[lid].append((sp, False))
            else:
                seed_points_by_layer_id[lid].append((
                    shapely.geometry.Point(conn.point.x, conn.point.y), True,
                ))

    polys_to_mesh: list[shapely.geometry.Polygon] = []
    seed_xys_to_mesh: list[np.ndarray | None] = []
    layer_indices: list[int] = []

    for layer_i, layer in enumerate(prob.layers):
        seed_points_in_layer = seed_points_by_layer_id.get(id(layer), [])

        geom_to_seed_points = collections.defaultdict(list)

        # Lazy-prepare each geometry the first time it gets a candidate
        # seed point. PreparedGeometry caches an internal edge RTree so
        # repeated contains/intersects calls drop from O(boundary_vertices)
        # to O(log n) — the difference is dramatic on large GND copper
        # (the 8682 mm² piece has 1000s of boundary vertices).
        prepared_by_geom_i: dict[int, shapely.prepared.PreparedGeometry] = {}

        for seed_point, add_ring in seed_points_in_layer:
            candidates = strtrees[layer_i].query(seed_point)

            for geom_i in candidates:
                if (layer_i, geom_i) not in connected_layer_mesh_pairs:
                    # This geometry is not even connected to any driven
                    # network, so we can just skip it.
                    continue
                prep = prepared_by_geom_i.get(geom_i)
                if prep is None:
                    prep = shapely.prepared.prep(layer.geoms[geom_i])
                    prepared_by_geom_i[geom_i] = prep
                if not prep.contains(seed_point):
                    continue

                # This seed point is inside the geometry, so we stick it in
                geom_to_seed_points[geom_i].append(seed_point)

                # For point Connections, augment with a small ring of
                # Steiner points around the injection vertex — forces fine
                # triangles there regardless of the global mesh size (see
                # _INJECTION_STEINER_RING_* constants for the why). Ring
                # members that fall outside the geometry are silently
                # dropped — happens when the Connection sits at the very tip
                # of a thin track or on its boundary. Equipotential-patch
                # Connections skip this: their pad-outline samples already
                # provide perimeter density and there is no singularity.
                if add_ring:
                    for ring_pt in _injection_steiner_ring(seed_point):
                        if prep.contains(ring_pt):
                            geom_to_seed_points[geom_i].append(ring_pt)

        for geom_i, geom in enumerate(layer.geoms):
            if (layer_i, geom_i) not in connected_layer_mesh_pairs:
                # This layer is not connected to any lumped elements, skip it
                continue
            # This layer is connected to at least one lumped element, so we need to mesh it

            # Beware! We are only including seed points that are _on the interior_
            # of the geometry. This is because otherwise the mesher
            # may attempt to fill in holes due to a seed point being on the boundary.
            # The rest of the stack _must_ ensure that any points that it needs
            # to use as Connection points that lie on the boundary should already
            # be included in the geometry.
            # TODO: The proper way to solve this is for the mesher to include
            # boundary points in the rings if it detects the case above,
            # but this is not yet implemented.
            # TODO: Add a warning here if we detect the case above
            seed_points_in_geom = geom_to_seed_points[geom_i]

            if seed_points_in_geom:
                seed_xy: np.ndarray | None = np.asarray(
                    [(p.x, p.y) for p in seed_points_in_geom],
                    dtype=np.float64,
                )
            else:
                seed_xy = None

            polys_to_mesh.append(layer.geoms[geom_i])
            seed_xys_to_mesh.append(seed_xy)
            layer_indices.append(layer_i)

    if not polys_to_mesh:
        return [], []

    # Per-polygon switches with width-aware max_size. Narrow nets get a
    # finer cap than the global config; wide pours keep the global value.
    # Without this, thin traces (where width < a couple of triangle edges)
    # systematically under-estimate end-to-end resistance because the
    # cotangent Laplacian needs several vertices across the conductor to
    # converge to the continuum solution.
    switches = [
        mesher._build_triangle_switches(
            max_size_override=mesh.Mesher.polygon_adaptive_max_size(
                poly, mesher.config.maximum_size,
            ),
        )
        for poly in polys_to_mesh
    ]
    arrays = _mesh_polygons_in_parallel(
        polys_to_mesh, seed_xys_to_mesh, switches,
        log_label="connected meshes", adaptive=_adaptive,
    )

    meshes: list[mesh.Mesh] = []
    mesh_index_to_layer_index: list[int] = list(layer_indices)
    for out_vertices, out_triangles in arrays:
        meshes.append(mesh.Mesh.from_triangle_arrays(out_vertices, out_triangles))

    return meshes, mesh_index_to_layer_index


def generate_disconnected_meshes(prob: problem.Problem,
                                 connected_layer_mesh_pairs: set[tuple[int, int]],
                                 ) -> list[list[mesh.Mesh]]:
    """
    Generate simple triangulations for disconnected copper regions.

    Args:
        prob: The Problem containing layers and geometry
        connected_layer_mesh_pairs: Set of (layer_i, geom_i) pairs that are electrically connected

    Returns:
        List of disconnected meshes per layer: disconnected_meshes_by_layer[layer_i] = [mesh1, mesh2, ...]
    """
    # Use relaxed mesher for fast triangulation without quality constraints
    relaxed_mesher = mesh.Mesher(mesh.Mesher.Config.RELAXED)
    disconnected_meshes_by_layer: list[list[mesh.Mesh]] = [[] for _ in prob.layers]

    polys_to_mesh: list[shapely.geometry.Polygon] = []
    seed_xys_to_mesh: list[np.ndarray | None] = []
    layer_indices: list[int] = []

    for layer_i, layer in enumerate(prob.layers):
        for geom_i, geom in enumerate(layer.geoms):
            if (layer_i, geom_i) in connected_layer_mesh_pairs:
                continue
            # This layer is not connected to any lumped elements
            # Triangulate it for display as disconnected copper
            polys_to_mesh.append(layer.geoms[geom_i])
            seed_xys_to_mesh.append(None)
            layer_indices.append(layer_i)

    if not polys_to_mesh:
        return disconnected_meshes_by_layer

    # Disconnected pieces are display-only (no FEM run on them), so skip
    # the per-polygon width-aware sizing — one relaxed switches string
    # for everything keeps these triangulations cheap.
    switches = [relaxed_mesher._build_triangle_switches()] * len(polys_to_mesh)
    arrays = _mesh_polygons_in_parallel(
        polys_to_mesh, seed_xys_to_mesh, switches,
        log_label="disconnected meshes",
    )

    for layer_i, (out_vertices, out_triangles) in zip(layer_indices, arrays):
        disconnected_meshes_by_layer[layer_i].append(
            mesh.Mesh.from_triangle_arrays(out_vertices, out_triangles)
        )

    return disconnected_meshes_by_layer


@dataclass
class NodeIndexer:
    node_to_global_index: dict[problem.NodeID, int] = field(default_factory=dict)
    extra_source_to_global_index: dict[problem.BaseLumped, int] = field(default_factory=dict)
    internal_node_count: int = 0
    # One entry per equipotential-patch pad: a numpy array of the global
    # vertex indices under that pad, representative first. solve() collapses
    # each group into a single variable so the pad behaves as an ideal
    # conductor. Empty when no Connection carries a pad region.
    vertex_groups: list[np.ndarray] = field(default_factory=list)

    @classmethod
    def _construct_kdtrees(cls,
                           prob: problem.Problem,
                           meshes: list[mesh.Mesh],
                           mesh_index_to_layer_index: list[int],
                           vindex: VertexIndexer
                           ) -> tuple[dict[int, scipy.spatial.KDTree],
                                      dict[int, np.ndarray]]:
        """
        Construct a KDTree per layer indexing every non-orphan mesh
        vertex in that layer.

        Orphan vertices — points Triangle preserved from the input that
        don't appear in any triangle (typically seed points that fell a
        hair outside the polygon due to FP) — are excluded.
        ``laplace_operator`` pins them to v=0 so the system stays
        non-singular, and a Connection that snapped onto one would dump
        its CurrentSource / coupling-Resistor stamps onto a hard-grounded
        node (e.g. J8 5A SINK landing on an orphan at exactly its pin
        (x, y) used to drag the bottom-layer +3V3 pad island to
        ~5/σ ≈ 2.4 mV). Membership uses the flat ``_in_triangle_mask``;
        legacy meshes without that mask fall back to the half-edge
        ``vertex.out is None`` test.

        Returns ``(layer_to_kdtree, layer_to_globals)`` where
        ``layer_to_globals[layer_i]`` is a 1-D int64 array such that
        ``layer_to_globals[layer_i][k]`` is the global vertex index of
        the k-th point fed into ``layer_to_kdtree[layer_i]``. Replaces
        the previous list-of-(global_idx, Point) tuples — that allocated
        ~2N Python objects per layer just to be discarded after the
        single ``kdtree.query`` call per connection.
        """
        layer_to_kdtree: dict[int, scipy.spatial.KDTree] = {}
        layer_to_globals: dict[int, np.ndarray] = {}

        # Bucket mesh indices by their owning layer once so we don't scan
        # every mesh for every layer (previously O(layers × meshes)).
        meshes_by_layer: dict[int, list[int]] = {}
        for mesh_i, layer_i in enumerate(mesh_index_to_layer_index):
            meshes_by_layer.setdefault(layer_i, []).append(mesh_i)

        # Per-mesh global-vertex offset — matches the order
        # VertexIndexer.create assigns indices (cumulative vertex count
        # of all earlier meshes).
        offsets = np.fromiter(
            (len(m.vertices) for m in meshes),
            dtype=np.int64, count=len(meshes),
        )
        offsets = np.concatenate(([0], np.cumsum(offsets[:-1]))) if offsets.size else \
                  np.empty(0, dtype=np.int64)

        for layer_i, mesh_indices in meshes_by_layer.items():
            xys_chunks: list[np.ndarray] = []
            globals_chunks: list[np.ndarray] = []
            for mesh_i in mesh_indices:
                msh = meshes[mesh_i]
                base = int(offsets[mesh_i])
                mask = getattr(msh, "_in_triangle_mask", None)
                if mask is None:
                    # Legacy mesh without source arrays / mask — walk the
                    # half-edge graph as before.
                    kept_xys: list[tuple[float, float]] = []
                    kept_globals: list[int] = []
                    for vertex_i, vertex in enumerate(msh.vertices):
                        if vertex.out is None:
                            continue
                        kept_xys.append((vertex.p.x, vertex.p.y))
                        kept_globals.append(base + vertex_i)
                    if kept_xys:
                        xys_chunks.append(
                            np.asarray(kept_xys, dtype=np.float64),
                        )
                        globals_chunks.append(
                            np.asarray(kept_globals, dtype=np.int64),
                        )
                else:
                    # Fast path: slice the flat source arrays in numpy.
                    keep = np.flatnonzero(mask)
                    if keep.size == 0:
                        continue
                    xys_chunks.append(msh._source_xys[keep])
                    globals_chunks.append((base + keep).astype(np.int64))

            if not xys_chunks:
                continue
            xys_layer = np.concatenate(xys_chunks, axis=0)
            globals_layer = np.concatenate(globals_chunks)

            layer_to_globals[layer_i] = globals_layer
            layer_to_kdtree[layer_i] = scipy.spatial.KDTree(
                xys_layer, leafsize=32,
            )

        return layer_to_kdtree, layer_to_globals

    @classmethod
    def create(cls,
               prob: problem.Problem,
               meshes: list[mesh.Mesh],
               mesh_index_to_layer_index: list[int],
               vindex: VertexIndexer,
               filtered_networks: list[problem.Network],
               layer_to_index: dict[int, int] | None = None,
               off_copper_threshold_mm: float | None = None) -> "NodeIndexer":

        layer_to_kdtree, layer_to_globals = cls._construct_kdtrees(
            prob,
            meshes,
            mesh_index_to_layer_index,
            vindex
        )

        if layer_to_index is None:
            layer_to_index = {id(layer): i for i, layer in enumerate(prob.layers)}

        # Contains both the Connection-related nodes and the
        # "virtual" nodes that only live inside a Network
        node_to_global_index = {}

        # First, we index the NodeIDs that are used in a Connection.
        #
        # A Connection carrying a pad ``region`` is an equipotential patch:
        # every mesh vertex under the pad outline is gathered into one group
        # (see ``vertex_groups``), and solve() collapses the group into a
        # single variable. The node maps to the group's representative
        # vertex. A Connection without a region (or a pad too small to catch
        # any mesh vertex) falls back to the single nearest vertex — the
        # original point-coupling behaviour.
        vertex_groups: list[np.ndarray] = []
        # Global vertex indices already assigned to a pad group. Keeping
        # groups disjoint guarantees the contraction in solve() is a clean
        # partition (no vertex pulled into two pads).
        claimed: set[int] = set()
        connections = [
            conn for network in filtered_networks for conn in network.connections
        ]
        for conn in connections:
            # A connection's layer may have no meshed copper at all — e.g. a
            # directive pin on a plane that has no copper reachable from a
            # driven source. _construct_kdtrees only builds a tree for layers
            # that produced connected meshes, so layer_to_kdtree[layer_i] is
            # absent for those. Indexing it directly used to raise KeyError and
            # abort the whole solve; instead, skip the connection with a warning
            # (its node falls through to the internal-node allocation below and
            # is simply left unattached to copper).
            layer_i = (layer_to_index.get(id(conn.layer))
                       if conn.layer is not None else None)
            kdtree = layer_to_kdtree.get(layer_i) if layer_i is not None else None
            if kdtree is None:
                warnings.warn(
                    f"Connection node {conn.node_id} is on a layer with no "
                    f"meshed copper near ({conn.point.x:.4g}, "
                    f"{conn.point.y:.4g}); its terminal is left unattached. "
                    f"Check that this pin lands on copper reachable from a "
                    f"source.",
                    SolverWarning, stacklevel=2,
                )
                continue
            # layer_to_globals[layer_i] is a flat int64 array — direct numpy
            # indexing returns the global vertex index, no tuple unpack.
            globals_arr = layer_to_globals[layer_i]

            vertex_global_idx: int | None = None
            if conn.region is not None:
                group = _vertices_under_pad(
                    kdtree, globals_arr, conn.region, conn.point, claimed,
                )
                if group.size:
                    vertex_global_idx = int(group[0])
                    claimed.update(int(g) for g in group)
                    if group.size >= 2:
                        vertex_groups.append(group)

            if vertex_global_idx is None:
                dist, vertex_idx_in_kdtree = kdtree.query(
                    (conn.point.x, conn.point.y), k=1,
                )
                vertex_global_idx = int(globals_arr[vertex_idx_in_kdtree])
                # If the nearest copper vertex is far from where the terminal
                # was placed, the pin isn't really on this net's copper — its
                # current then gets injected at a distant vertex, silently
                # skewing IR-drop. Warn but still attach (preserving the
                # long-standing behaviour); the threshold is a few mesh cells,
                # so normal on-copper pins never trip it.
                if (off_copper_threshold_mm is not None
                        and dist > off_copper_threshold_mm):
                    warnings.warn(
                        f"Connection node {conn.node_id} at "
                        f"({conn.point.x:.4g}, {conn.point.y:.4g}) is "
                        f"{dist:.3g} mm from the nearest copper vertex on its "
                        f"net (> {off_copper_threshold_mm:.3g} mm); its current "
                        f"is being injected at a distant vertex, so IR-drop "
                        f"near it may be wrong. Check the pin lands on copper.",
                        SolverWarning, stacklevel=2,
                    )

            node = conn.node_id

            # Check that we are not overwriting an existing node with different
            # vertex index. This should never happen in practice
            if node in node_to_global_index and node_to_global_index[node] != vertex_global_idx:
                raise ValueError("Duplicate connection vertices found, this should not happen.")
            node_to_global_index[node] = vertex_global_idx

        # Next, we allocate new indices for all the yet to be allocated nodes
        nodes = [
            node for network in filtered_networks for node in network.nodes
            if node not in node_to_global_index
        ]
        internal_node_count = len(nodes)
        i_at = vindex.n_vertices
        for node in nodes:
            node_to_global_index[node] = i_at
            i_at += 1

        # And finally we need to allocate indices for the voltage sources
        # (those need an extra variable)
        extra_sources = [
            elem for network in filtered_networks for elem in network.elements
        ]
        extra_source_to_global_index = {}
        for elem in extra_sources:

            if elem.extra_variable_count > 1:
                # TODO: Store a (elem, index) pair in the global index or something
                raise NotImplementedError("Extra variable count > 1 not supported yet")

            for _ in range(elem.extra_variable_count):
                extra_source_to_global_index[elem] = i_at
                i_at += 1

        return cls(
            node_to_global_index=node_to_global_index,
            extra_source_to_global_index=extra_source_to_global_index,
            internal_node_count=internal_node_count,
            vertex_groups=vertex_groups,
        )


def stamp_network_into_system(network: problem.Network,
                              node_indexer: NodeIndexer,
                              rows: list,
                              cols: list,
                              vals: list,
                              r: np.ndarray) -> None:
    """Append each network element's MNA stamp to the global COO row/col/val
    accumulators (in-place ``extend`` on the lists; ``r`` is a numpy array
    and is mutated directly).

    Switched from ``scipy.sparse.lil_matrix.__setitem__`` per element to flat
    COO accumulation: every ``L[i, j] += v`` / ``L[i, j] = v`` in the
    upstream lil-matrix version becomes ``rows.append(i); cols.append(j);
    vals.append(v)``. The final COO assembly in ``solve()`` sums duplicate
    (i, j) entries automatically — equivalent to the ``+=`` semantics. For
    the elements that originally wrote ``L[i, j] = const`` (e.g. the
    VoltageSource voltage-equation rows), each (i, j) is unique to that
    element so there's nothing to overwrite, and accumulation gives the
    same result.
    """
    for element in network.elements:
        match element:
            case problem.Resistor(a=a, b=b, resistance=resistance):
                i_a = node_indexer.node_to_global_index[a]
                i_b = node_indexer.node_to_global_index[b]
                g = 1.0 / resistance
                # (V_b - V_a) / R contribution at node a; mirror at node b.
                rows.extend((i_a, i_a, i_b, i_b))
                cols.extend((i_a, i_b, i_b, i_a))
                vals.extend((-g,   g,   -g,   g))
            case problem.CurrentSource(f=f, t=t, current=current):
                i_f = node_indexer.node_to_global_index[f]
                i_t = node_indexer.node_to_global_index[t]
                r[i_f] += current
                r[i_t] -= current
            case problem.VoltageSource(p=p, n=n, voltage=voltage):
                i_p = node_indexer.node_to_global_index[p]
                i_n = node_indexer.node_to_global_index[n]
                i_v = node_indexer.extra_source_to_global_index[element]
                # MNA voltage-source: extra current variable I_v couples
                # V_p − V_n = voltage and the I_v current threads through
                # the p / n KCL equations.
                rows.extend((i_v, i_v, i_p, i_n))
                cols.extend((i_p, i_n, i_v, i_v))
                vals.extend((1.0, -1.0, 1.0, -1.0))
                r[i_v] = voltage
            case problem.VoltageRegulator(v_p=v_p, v_n=v_n,
                                          s_f=s_f, s_t=s_t,
                                          voltage=voltage,
                                          gain=gain):
                i_v_p = node_indexer.node_to_global_index[v_p]
                i_v_n = node_indexer.node_to_global_index[v_n]
                i_s_f = node_indexer.node_to_global_index[s_f]
                i_s_t = node_indexer.node_to_global_index[s_t]
                i_v = node_indexer.extra_source_to_global_index[element]
                # Voltage-source half (identical to VoltageSource above).
                rows.extend((i_v, i_v, i_v_p, i_v_n))
                cols.extend((i_v_p, i_v_n, i_v, i_v))
                vals.extend((1.0, -1.0, 1.0, -1.0))
                r[i_v] += voltage
                # Mirror the output current at the input pair with gain.
                rows.extend((i_s_f, i_s_t))
                cols.extend((i_v, i_v))
                vals.extend((gain, -gain))
            case _:
                raise NotImplementedError(f"Unsupported node type {element}")


def setup_ground_node(i_gnd: int, N: int,
                      rows: list, cols: list, vals: list,
                      r: np.ndarray) -> None:
    """Append the ground-node stamp to the COO accumulators.

    Wires a 0 V virtual source from ``i_gnd`` to the implicit ground node
    that lives at global index ``N - 1`` (the last variable in the system).
    The upstream lil-matrix version used negative indexing (``L[-1, ...]``)
    — here we expand it to the explicit index since COO assembly takes
    absolute indices only.
    """
    last = N - 1
    rows.extend((last, i_gnd))
    cols.extend((i_gnd, last))
    vals.extend((1.0, 1.0))
    r[last] = 0  # Ground node voltage is 0


def setup_ground_nodes(ground_indices: list[int], N: int,
                       rows: list, cols: list, vals: list,
                       r: np.ndarray) -> None:
    """Pin one voltage reference per entry of ``ground_indices``.

    Generalises :func:`setup_ground_node` to several electrically-isolated
    subsystems (e.g. a PDN_NET single-net analysis solved alongside a normal
    one). Each reference gets its own implicit ground variable in the last
    ``len(ground_indices)`` slots of the system; a 0 V virtual source wires
    the reference index to it. With a single reference this is identical to
    ``setup_ground_node``.
    """
    n_g = len(ground_indices)
    for k, i_gnd in enumerate(ground_indices):
        last = N - n_g + k
        rows.extend((last, i_gnd))
        cols.extend((i_gnd, last))
        vals.extend((1.0, 1.0))
        r[last] = 0.0  # Each reference node's voltage is 0


def process_mesh_laplace_operators(
    meshes: list[mesh.Mesh],
    conductances: list[float],
    vindex: VertexIndexer,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Assemble every mesh's cotangent Laplacian at once, returning one
    concatenated ``(rows, cols, vals)`` triple in GLOBAL vertex indices, ready
    for the single COO→CSC assembly alongside the network and ground stamps.

    Batched: all meshes' retained triangle-soup arrays are concatenated into
    one global vertex array and one global triangle array (each mesh's
    triangles offset into its ``vindex`` global-index range), then the whole
    board's half-cotangent weights are computed in ONE vectorised
    :func:`_half_cotangent` pass. This replaces the previous per-mesh
    ``ThreadPoolExecutor.map(laplace_operator, meshes)`` — on a board with tens
    of thousands of small meshes the per-mesh Python overhead (task dispatch, a
    ``scipy.coo_matrix`` object built and torn down per mesh, GIL-bound small
    numpy) dominated; one global pass removes all of it.

    The result is **bit-identical** to the per-mesh path (cross-checked in
    ``tests/test_laplace_batched.py``): triangles are concatenated in mesh
    order, so each vertex lives in one contiguous triangle block and the
    global ``np.add.at`` accumulates its diagonal in the same order the
    per-mesh scatter did; the final CSC is identical because off-diagonal
    duplicates (≤2 per edge) sum commutatively.
    """
    if not meshes:
        return (np.empty(0, dtype=np.int64),
                np.empty(0, dtype=np.int64),
                np.empty(0, dtype=DTYPE))

    # vindex assigns global vertex indices in mesh-iteration order, so mesh m
    # owns [offsets[m], offsets[m+1]). Gather each mesh's (xys, tris), offset
    # its triangles into the global index range, and tag every triangle /
    # vertex with its mesh's conductance. Gathering is cheap (no compute); the
    # heavy cotangent math happens once, below.
    offsets = vindex.mesh_vertex_offsets
    n_total = int(offsets[-1])
    xys_list: list[np.ndarray] = []
    tris_list: list[np.ndarray] = []
    tri_cond_list: list[np.ndarray] = []
    vert_cond = np.empty(n_total, dtype=DTYPE)
    for mesh_i, msh in enumerate(meshes):
        base = int(offsets[mesh_i])
        end = int(offsets[mesh_i + 1])
        cond = conductances[mesh_i]
        vert_cond[base:end] = cond
        xys, tris = _mesh_source_arrays(msh)
        xys_list.append(xys)
        if tris.shape[0] > 0:
            tris_list.append(tris + base)  # local → global vertex indices
            tri_cond_list.append(np.full(tris.shape[0], cond, dtype=DTYPE))

    xys_all = (np.concatenate(xys_list, axis=0) if xys_list
               else np.empty((0, 2), dtype=DTYPE))

    row_chunks: list[np.ndarray] = []
    col_chunks: list[np.ndarray] = []
    val_chunks: list[np.ndarray] = []

    if tris_list:
        tris_all = np.concatenate(tris_list, axis=0)
        tri_cond = np.concatenate(tri_cond_list)
        v0 = tris_all[:, 0]
        v1 = tris_all[:, 1]
        v2 = tris_all[:, 2]
        p0 = xys_all[v0]
        p1 = xys_all[v1]
        p2 = xys_all[v2]

        w12 = _half_cotangent(p1 - p0, p2 - p0)  # apex 0 ↔ edge (v1, v2)
        w20 = _half_cotangent(p2 - p1, p0 - p1)  # apex 1 ↔ edge (v2, v0)
        w01 = _half_cotangent(p0 - p2, p1 - p2)  # apex 2 ↔ edge (v0, v1)

        rows_off = np.concatenate([v1, v2, v2, v0, v0, v1])
        cols_off = np.concatenate([v2, v1, v0, v2, v1, v0])
        vals_off = np.concatenate([w12, w12, w20, w20, w01, w01])

        # Diagonal (UNSCALED): L[i, i] = -Σ outgoing weights from i. Done before
        # conductance scaling so a vertex's diagonal is (Σ weights)·cond, matching
        # the per-mesh path exactly rather than Σ(weight·cond).
        diag = np.zeros(n_total, dtype=DTYPE)
        np.add.at(diag, rows_off, -vals_off)

        # Scale: each off-diagonal entry by its triangle's conductance (the
        # 6 edge sub-blocks all index the same triangle set), each diagonal by
        # its vertex's conductance.
        vals_off = vals_off * np.concatenate([tri_cond] * 6)
        diag = diag * vert_cond

        diag_idx = np.arange(n_total, dtype=np.int64)
        row_chunks += [rows_off, diag_idx]
        col_chunks += [cols_off, diag_idx]
        val_chunks += [vals_off, diag]

        used = np.zeros(n_total, dtype=bool)
        used[tris_all.ravel()] = True
    else:
        used = np.zeros(n_total, dtype=bool)

    # Pin orphan vertices (in no triangle) to keep the matrix non-singular —
    # value 1.0·conductance, matching the per-mesh path.
    orphans = np.where(~used)[0].astype(np.int64)
    if orphans.size > 0:
        row_chunks.append(orphans)
        col_chunks.append(orphans)
        val_chunks.append(vert_cond[orphans])

    if row_chunks:
        return (np.concatenate(row_chunks),
                np.concatenate(col_chunks),
                np.concatenate(val_chunks))
    return (np.empty(0, dtype=np.int64),
            np.empty(0, dtype=np.int64),
            np.empty(0, dtype=DTYPE))


def produce_layer_solutions(layers: list[problem.Layer],
                            vindex: VertexIndexer,
                            meshes: list[mesh.Mesh],
                            mesh_index_to_layer_index: list[int],
                            v: np.ndarray,
                            disconnected_meshes_by_layer: list[list[mesh.Mesh]]) -> list[LayerSolution]:
    """Pack the flat solution vector ``v`` back into per-layer LayerSolution
    objects.

    Vectorised: vertex global indices are contiguous within each mesh (the
    VertexIndexer hands them out in mesh-iteration order), so we just slice
    ``v[base:base + n]`` per mesh and assign it directly into the
    ZeroForm's underlying ``values`` array. No per-vertex Python loop, no
    half-edge walk. Same trick lets us also build per-mesh buckets in a
    single pass instead of an O(layers × meshes) outer-loop scan.
    """
    # Bucket mesh indices by layer once — replaces the O(L × M) inner
    # filter ``if mesh_index_to_layer_index[mesh_i] != layer_i``.
    meshes_by_layer: dict[int, list[int]] = {}
    for mesh_i, lid in enumerate(mesh_index_to_layer_index):
        meshes_by_layer.setdefault(lid, []).append(mesh_i)

    # Cumulative vertex offsets — VertexIndexer.create assigns globals in
    # iteration order, so mesh m's global indices are
    # [offsets[m], offsets[m] + len(meshes[m].vertices)).
    offsets = [0]
    for m in meshes:
        offsets.append(offsets[-1] + len(m.vertices))

    layer_solutions: list[LayerSolution] = []
    for layer_i, layer in enumerate(layers):
        layer_meshes: list[mesh.Mesh] = []
        layer_values: list[mesh.ZeroForm] = []
        layer_power_densities: list[mesh.TwoForm] = []
        for mesh_i in meshes_by_layer.get(layer_i, ()):
            msh = meshes[mesh_i]
            base = offsets[mesh_i]
            n_v = len(msh.vertices)
            # Direct slice into a ZeroForm — no per-vertex Python loop.
            vertex_values = mesh.ZeroForm(msh)
            vertex_values.values[:] = v[base:base + n_v]
            # Power density per triangle from the vectorised flat-array path.
            power_density = compute_power_density(vertex_values, layer.conductance)

            layer_values.append(vertex_values)
            layer_meshes.append(msh)
            layer_power_densities.append(power_density)

        layer_solutions.append(LayerSolution(
            meshes=layer_meshes,
            potentials=layer_values,
            power_densities=layer_power_densities,
            disconnected_meshes=disconnected_meshes_by_layer[layer_i]
        ))

    return layer_solutions


def network_has_a_dead_terminal(network: problem.Network,
                                prob: problem.Problem,
                                connected_layer_mesh_pairs: set[tuple[int, int]],
                                strtrees: list[shapely.strtree.STRtree],
                                layer_to_index: dict[int, int] | None = None,
                                ) -> bool:
    """
    Check if a network has any connection on a dead (disconnected) copper region.

    ``layer_to_index`` is an optional ``{id(layer): index}`` cache; pass it from
    the solve loop to avoid an O(L) ``prob.layers.index(...)`` per connection.
    """
    if layer_to_index is None:
        layer_to_index = {id(layer): i for i, layer in enumerate(prob.layers)}
    for conn in network.connections:
        layer_i = layer_to_index[id(conn.layer)]
        strtree = strtrees[layer_i]

        candidates = strtree.query(conn.point)
        for geom_i in candidates:
            if (layer_i, geom_i) in connected_layer_mesh_pairs:
                # Would have no effect on whether the network
                # has a dead terminal or not, do not even bother checking
                continue

            if not conn.layer.geoms[geom_i].intersects(conn.point):
                continue

            # Okay, at this point:
            # * We know that the connection is on (layer_i, geom_i)
            # * We know that the (layer_i, geom_i) pair got eliminated by
            # the connectivity graph check.
            # This means we eliminate the entire network. In practice,
            # it should not happen that a network has some dead
            # terminals and some live terminals (that would mean ConnectivityGraph
            # is broken). So it is enough to just find the first dead terminal
            # and bail.
            return True

    return False


def _log_network_breakdown(
    filtered: list[problem.Network],
    all_networks: list[problem.Network],
) -> None:
    """Log a per-element-type count for filtered vs dropped networks."""
    def _count_types(nets):
        ctr: collections.Counter[str] = collections.Counter()
        for net in nets:
            for elem in net.elements:
                ctr[type(elem).__name__] += 1
        return ctr

    filtered_ids = {id(n) for n in filtered}
    kept = _count_types(filtered)
    dropped = _count_types(n for n in all_networks if id(n) not in filtered_ids)
    log.debug(
        f"Active element types:  {dict(kept)}\n"
        f"Dropped element types: {dict(dropped)}"
    )
    n_dropped = len(all_networks) - len(filtered)
    if n_dropped:
        log.info(
            f"  {n_dropped} network(s) dropped (dead-copper terminal) — "
            f"their currents are excluded from the solve."
        )


def find_best_ground_node_index(
    networks: list[problem.Network],
    node_indexer: NodeIndexer,
) -> int:
    """Pick the GND reference mesh vertex.

    Votes each VoltageSource's n-terminal (GND pin) and selects the vertex
    shared by the most VoltageSources — the common power-supply GND bus.
    Ties are broken by the highest source voltage.  Only considers
    ``networks`` (the already-filtered list), so all returned indices are
    guaranteed to exist in ``node_indexer``.
    """
    vote_count: collections.Counter[int] = collections.Counter()
    max_voltage_by_vertex: dict[int, float] = {}

    for network in networks:
        for element in network.elements:
            if not isinstance(element, problem.VoltageSource):
                continue
            gnd_idx = node_indexer.node_to_global_index.get(element.n)
            if gnd_idx is None:
                continue
            vote_count[gnd_idx] += 1
            if element.voltage > max_voltage_by_vertex.get(gnd_idx, float('-inf')):
                max_voltage_by_vertex[gnd_idx] = element.voltage

    if not vote_count:
        log.warning(
            "No VoltageSource found in active networks — defaulting ground "
            "reference to vertex 0.  Solver results will be unreliable."
        )
        return 0

    # Most-shared GND vertex first; highest voltage breaks ties.
    best_idx = max(
        vote_count,
        key=lambda idx: (vote_count[idx], max_voltage_by_vertex[idx]),
    )
    log.debug(
        f"Ground node: global index {best_idx}, "
        f"shared by {vote_count[best_idx]} VoltageSource(s), "
        f"highest voltage {max_voltage_by_vertex[best_idx]:.3f} V"
    )
    return best_idx


def find_ground_node_indices(
    filtered_networks: list[problem.Network],
    node_indexer: NodeIndexer,
    vindex: VertexIndexer,
) -> list[int]:
    """Pick one voltage-reference index per electrically-isolated subsystem.

    The MNA system loses one degree of freedom for every connected component
    that has no fixed potential, so each component needs its own reference or
    the solve is rank-deficient. A board whose rails all share a GND plane
    forms one component and yields a single reference — identical to the old
    single-ground behaviour. A single-net (PDN_NET) analysis forms its own
    component with no GND copper, so it gets its own reference here.

    Within each component the reference is voted exactly as
    :func:`find_best_ground_node_index` does globally — the node shared by the
    most ``VoltageSource`` N-terminals, highest source voltage breaking ties.
    A component with no ``VoltageSource`` falls back to its lowest-indexed
    mesh vertex.
    """
    n_vert = vindex.n_vertices
    n2g = node_indexer.node_to_global_index

    # Union-find over "units": one per mesh (every vertex of a mesh is
    # mutually connected through that mesh's Laplacian) plus one per
    # network-internal node / extra-source variable.
    parent: dict[object, object] = {}

    def find(x):
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    def unit(gi: int):
        if gi < n_vert:
            return ("mesh", vindex.to_mesh_vertex(gi)[0])
        return ("node", gi)

    # Each element ties all of its terminals (and its extra current variable,
    # if any) into one component.
    for net in filtered_networks:
        for elem in net.elements:
            units = [unit(n2g[t]) for t in elem.terminals]
            ev = node_indexer.extra_source_to_global_index.get(elem)
            if ev is not None:
                units.append(unit(ev))
            for u in units[1:]:
                union(units[0], u)

    # Per component: VoltageSource N-terminal votes + a fallback mesh vertex.
    vote: dict[object, collections.Counter] = {}
    max_voltage: dict[tuple[object, int], float] = {}
    fallback_vertex: dict[object, int] = {}
    for net in filtered_networks:
        for elem in net.elements:
            comp = find(unit(n2g[elem.terminals[0]]))
            for t in elem.terminals:
                gi = n2g[t]
                if gi < n_vert:
                    cur = fallback_vertex.get(comp)
                    if cur is None or gi < cur:
                        fallback_vertex[comp] = gi
            if isinstance(elem, problem.VoltageSource):
                gnd_gi = n2g[elem.n]
                vote.setdefault(comp, collections.Counter())[gnd_gi] += 1
                key = (comp, gnd_gi)
                if elem.voltage > max_voltage.get(key, float("-inf")):
                    max_voltage[key] = elem.voltage

    ground_indices: list[int] = []
    for comp in set(fallback_vertex) | set(vote):
        counter = vote.get(comp)
        if counter:
            ground_indices.append(max(
                counter,
                key=lambda gi: (counter[gi], max_voltage[(comp, gi)]),
            ))
        else:
            ground_indices.append(fallback_vertex[comp])

    if not ground_indices:
        # No networks at all — keep the system pinnable (matches the legacy
        # fallback of grounding vertex 0).
        log.warning("No networks to ground — defaulting reference to vertex "
                    "0. Solver results will be unreliable.")
        return [0]
    ground_indices.sort()
    log.debug("Ground references (one per isolated subsystem): %s",
              ground_indices)
    return ground_indices


def compute_triangle_gradient(vertices: list[mesh.Vertex],
                              values: list[float]) -> mesh.Vector:
    """
    Compute the gradient of a function that is a linear interpolation of the
    values at the vertices of a triangle.
    """
    if len(vertices) != 3 or len(values) != 3:
        raise ValueError("Vertices and values must be of length 3 for a triangle")
    # Ugh. This is all veeeeery adhoc.
    # The magical keywords here are
    # * Finite Element Exterior Calculus
    # * Whitney Forms
    # * Nedelec elements
    # So, ultimately, this should all be implemented in mesh.py and we would just
    # like take the exterior derivative and have the interpolant etc.
    # However, for now, I want to get a simple solution and get the more
    # complicated stuff going later.
    v1, v2, v3 = vertices
    x1, y1 = v1.p.x, v1.p.y
    x2, y2 = v2.p.x, v2.p.y
    x3, y3 = v3.p.x, v3.p.y
    f1, f2, f3 = values

    def interpolate(x, y) -> float:
        # Barycentric coordinates
        D = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
        l1 = ((y2 - y3) * (x - x3) + (x3 - x2) * (y - y3)) / D
        l2 = ((y3 - y1) * (x - x3) + (x1 - x3) * (y - y3)) / D
        l3 = 1 - l1 - l2
        return l1 * f1 + l2 * f2 + l3 * f3

    # Since this is a linear interpolation, the gradient is just equal to the
    # difference quotient
    partial_x = interpolate(x1 + 1, y1) - f1
    partial_y = interpolate(x1, y1 + 1) - f1
    # TODO: mesh.Vector is semantically not quite the right type here
    return mesh.Vector(partial_x, partial_y)


def compute_power_density(voltage: mesh.ZeroForm, conductivity: float) -> mesh.TwoForm:
    """
    Compute the power density at the mesh faces.

    Vectorised: reads the mesh's flat triangle-soup arrays
    (``_source_xys`` + ``_source_tris``) plus the potential vector and
    computes p = σ · |∇V|² for every triangle in one numpy expression,
    instead of iterating ``mesh.faces`` in Python and calling
    ``compute_triangle_gradient`` per face.

    Falls back to the per-face loop only if the mesh predates source-array
    retention (very old pickled meshes); modern meshes never take that path.
    """
    msh = voltage.mesh
    power_density = mesh.TwoForm(msh)

    xys = getattr(msh, "_source_xys", None)
    tris = getattr(msh, "_source_tris", None)
    vals = np.asarray(voltage.values, dtype=DTYPE)
    if (xys is not None and tris is not None and tris.shape[0] > 0
            and vals.size == xys.shape[0]):
        # Vectorised path.
        p0 = xys[tris[:, 0]]
        p1 = xys[tris[:, 1]]
        p2 = xys[tris[:, 2]]
        f0 = vals[tris[:, 0]]
        f1 = vals[tris[:, 1]]
        f2 = vals[tris[:, 2]]
        # Barycentric gradient: signed double-area D = (y2−y3)(x1−x3) + (x3−x2)(y1−y3)
        y23 = p1[:, 1] - p2[:, 1]
        x32 = p2[:, 0] - p1[:, 0]
        y31 = p2[:, 1] - p0[:, 1]
        x13 = p0[:, 0] - p2[:, 0]
        y12 = p0[:, 1] - p1[:, 1]
        x21 = p1[:, 0] - p0[:, 0]
        D = y23 * (p0[:, 0] - p2[:, 0]) + x32 * (p0[:, 1] - p2[:, 1])
        # Linear-element gradient — closed form from the affine map:
        #   ∂V/∂x = (y23·f0 + y31·f1 + y12·f2) / D
        #   ∂V/∂y = (x32·f0 + x13·f1 + x21·f2) / D
        # On degenerate triangles (D == 0) ∇V is undefined — keep p = 0
        # (matches the original ``compute_triangle_gradient`` which would
        # raise; the per-face loop only continued past triangles that came
        # back from face.vertices with len != 3, so degenerate-but-3-vertex
        # triangles silently emitted NaN before — now they emit 0).
        Ex = np.zeros_like(D)
        Ey = np.zeros_like(D)
        nz = D != 0
        invD = np.zeros_like(D)
        invD[nz] = 1.0 / D[nz]
        Ex[nz] = (y23[nz] * f0[nz] + y31[nz] * f1[nz] + y12[nz] * f2[nz]) * invD[nz]
        Ey[nz] = (x32[nz] * f0[nz] + x13[nz] * f1[nz] + x21[nz] * f2[nz]) * invD[nz]
        # p = J · E = σ |E|².
        p_arr = conductivity * (Ex * Ex + Ey * Ey)
        # TwoForm.values is sized from len(mesh.faces); guarantee the same
        # length whether or not the mesh has lightweight Face stubs.
        if power_density.values.size != p_arr.size:
            power_density.values = p_arr.astype(np.float64, copy=False)
        else:
            np.copyto(power_density.values, p_arr)
        return power_density

    # Legacy per-face path (only kept for meshes without source arrays).
    for face in msh.faces:
        vertices = list(face.vertices)
        if len(vertices) != 3:
            continue
        E = compute_triangle_gradient(
            vertices,
            [voltage[v] for v in vertices]
        )
        J = E * conductivity
        p = J.dot(E)
        power_density[face] = p
    return power_density


class _MinresTimeout(Exception):
    """Raised from the MINRES callback when its wall-clock budget is spent."""


class _MinresProgress:
    """MINRES ``callback``: keeps the latest iterate, logs progress to the
    GUI substage feed, and raises :class:`_MinresTimeout` once the wall-clock
    budget is exhausted — so a multi-million-variable iterative solve reports
    progress instead of appearing frozen, and can never hang indefinitely."""

    def __init__(self, label: str, budget_s: float) -> None:
        self._label = label
        self._budget_s = budget_s
        self._t0 = time.monotonic()
        self.iterations = 0
        self.last_x: np.ndarray | None = None

    def __call__(self, xk: np.ndarray) -> None:
        self.iterations += 1
        self.last_x = xk
        elapsed = time.monotonic() - self._t0
        if self.iterations % _MINRES_PROGRESS_EVERY == 0:
            log.info(
                "MINRES (%s) fallback: iteration %d, elapsed %.0fs.",
                self._label, self.iterations, elapsed,
            )
        if elapsed > self._budget_s:
            raise _MinresTimeout()


# Persistent symmetric-indefinite PARDISO solver + a fingerprint of the matrix
# it currently holds factorised. Keeping the factorisation alive between
# solve() calls lets a re-solve whose stiffness matrix is unchanged — e.g. the
# GUI editor loop where only sink-current / source-voltage magnitudes change
# (those touch only the RHS, never L) — skip the ~seconds-to-minutes
# factorisation and run just the fast solve phase. The lock serialises access
# (solves never overlap in practice, but a shared factorisation must not be
# entered concurrently). ``free_pardiso_cache`` drops it; the GUI also calls
# that on cancel so a solve hard-killed mid-factorisation can never leave a
# corrupt factorisation to be reused.
_sym_solver = None
_sym_fingerprint: str | None = None
_sym_solver_lock = threading.Lock()


def _csr_fingerprint(m: "scipy.sparse.csr_matrix") -> str:
    """Content hash of a CSR matrix — changes iff the matrix (structure or
    values) changes, so an identical re-solve is detected and the cached
    factorisation reused."""
    return (hashlib.sha1(np.ascontiguousarray(m.indptr)).hexdigest()
            + hashlib.sha1(np.ascontiguousarray(m.indices)).hexdigest()
            + hashlib.sha1(np.ascontiguousarray(m.data)).hexdigest())


def free_pardiso_cache() -> None:
    """Release the cached symmetric factorisation and its solver.

    Bounds resident memory (a large factorisation can be gigabytes) and, called
    on solve-cancel, guarantees a solve that was hard-killed mid-factorisation
    can't leave an inconsistent factorisation for the next solve to reuse. The
    next solve simply re-factorises from scratch."""
    global _sym_solver, _sym_fingerprint
    with _sym_solver_lock:
        solver, _sym_solver, _sym_fingerprint = _sym_solver, None, None
    if solver is not None:
        try:
            solver.free_memory(everything=True)
        except Exception:  # pragma: no cover - best effort
            pass


def _pardiso_solve_sym(
    L_csc: "scipy.sparse.csc_matrix", r: np.ndarray,
) -> np.ndarray:
    """Symmetric-indefinite PARDISO solve (mtype -2), reusing the factorisation
    across identical-matrix re-solves.

    PARDISO factorises only the upper triangle here — markedly faster than
    the unsymmetric factorisation. Two requirements: it must be given just
    ``triu(L)`` (the full matrix crashes MKL), and the diagonal must be
    structurally complete — the MNA Lagrange rows (ground node, voltage
    sources) carry no diagonal entry, which PARDISO rejects as "input
    inconsistent", so the diagonal is materialised (missing entries become
    explicit zeros, numerically a no-op).

    A module-level solver is kept alive and re-factorised only when the matrix
    fingerprint changes; when it hasn't (a value-only re-solve), pypardiso runs
    the solve phase alone. On any error the cache is dropped and the exception
    re-raised so :func:`_solve_robust` falls back — and a stale/garbage result
    from a reused factorisation is caught anyway by that function's residual
    check. ``size_limit_storage=0`` makes pypardiso store only a hash of the
    factorised matrix (not a full copy), keeping resident memory down."""
    M = scipy.sparse.triu(L_csc, format="csr")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # scipy setdiag notice
        M.setdiag(M.diagonal())
    M.sort_indices()  # canonical form: stable fingerprint, and PARDISO needs it

    global _sym_solver, _sym_fingerprint
    with _sym_solver_lock:
        if _sym_solver is None:
            _sym_solver = _pypardiso.PyPardisoSolver(
                mtype=-2, size_limit_storage=0)
            _sym_fingerprint = None
        try:
            fp = _csr_fingerprint(M)
            if fp != _sym_fingerprint:
                # First solve, or the matrix changed: analyse + factorise once.
                _sym_solver.factorize(M)
                _sym_fingerprint = fp
            # solve() sees the stored factorisation and runs the solve phase
            # only — the cheap path that makes an identical re-solve fast.
            return _sym_solver.solve(M, r)
        except BaseException:
            try:
                _sym_solver.free_memory(everything=True)
            except Exception:
                pass
            _sym_solver = None
            _sym_fingerprint = None
            raise


def _log_singular_diagnostic(
    L_csc: "scipy.sparse.csc_matrix",
    r: np.ndarray,
    v: np.ndarray,
    row_describer: Callable[[int], str],
) -> None:
    """Log the equations the failed direct solve left unsatisfied.

    The rows with the largest ``|L·v - r|`` are where the factorisation's
    small pivots landed — i.e. the variables spanning the matrix's
    near-null-space. Mapped back through ``row_describer`` to a copper
    (layer, net) slab and location, they localise the near-floating region
    that drove the matrix near-singular.
    """
    try:
        resid_vec = np.abs(np.asarray(L_csc @ v - r, dtype=DTYPE))
        k = int(min(_SINGULAR_DIAG_ROWS, resid_vec.size))
        if k <= 0:
            return
        worst = np.argpartition(resid_vec, -k)[-k:]
        worst = worst[np.argsort(resid_vec[worst])[::-1]]
        lines = [
            f"      |L·v-r|={resid_vec[i]:.4g}  ->  {row_describer(int(i))}"
            for i in worst
        ]
        log.warning(
            "Worst-residual equations from the failed direct solve (these "
            "localise the near-singular region — inspect this copper for a "
            "missing or barely-connected return path):\n%s",
            "\n".join(lines),
        )
    except Exception as e:  # diagnostic only — never break the solve
        log.debug("Singular-region diagnostic failed: %s", e)


def _solve_robust(
    L_csc: "scipy.sparse.csc_matrix",
    r: np.ndarray,
    symmetric: bool = False,
    row_describer: Callable[[int], str] | None = None,
) -> tuple[np.ndarray, str, int, float]:
    """Solve ``L_csc @ v = r`` with staged automatic fallback when the direct
    solve fails.

    Returns ``(v, method_used, iterations, residual_norm)``. ``method_used``
    is the solver that produced ``v``: ``"pardiso-sym"`` / ``"pardiso"`` (MKL
    PARDISO, symmetric-indefinite or unsymmetric), ``"superlu"`` (scipy's
    direct solver), ``"minres"`` / ``"minres+ridge"`` (Jacobi-preconditioned
    iterative, the latter with Tikhonov regularisation), or
    ``"direct-best-effort"`` when nothing reaches tolerance and the least-bad
    direct solve is handed back. ``iterations`` is the iteration count for
    iterative methods (1 for direct), and ``residual_norm`` is ``||L·v - r||``
    against the original system — already needed for the fallback check, so
    it is handed back rather than recomputed by the caller.

    The MNA matrix assembled by this solver is **symmetric indefinite**:
    Laplacian + Resistor stamps contribute positive eigenvalues, and the
    VoltageSource and ground-constraint Lagrange rows contribute negative
    ones. A direct factorisation usually handles this without issue — but
    pathological topologies (small isolated meshes connected only by lumped
    elements, heavily fragmented power nets, weakly-coupled mesh components)
    push the matrix near-singular, at which point the factorisation gets
    small pivots and silently returns a solution with a huge residual. The
    Lagrange-multiplier rows (ground_node_current, VoltageSource currents)
    are particularly sensitive — they end up wildly wrong, propagating to
    nonsensical downstream voltages.

    Detection at every stage is the same check: the residual ``||L·v - r||``
    must fall below ``max(abs floor, rel_tol·||r||)``. Recovery is staged,
    cheapest first — a near-singular matrix can defeat one factorisation's
    pivoting but not another's, so a failed direct solve is retried with
    progressively more robust pivoting before the (slow) iterative fallback:

      1. PARDISO symmetric-indefinite (Bunch-Kaufman pivoting) — primary,
         when the matrix is symmetric and PARDISO is installed.
      2. PARDISO unsymmetric (full MKL pivoting) — stronger pivoting.
      3. SuperLU (partial pivoting).
      4. Jacobi-preconditioned MINRES, warm-started from the best direct
         solve and bounded by a wall-clock budget.
      5. MINRES with a Tikhonov ridge as a last resort.

    Each direct retry costs a few seconds — cheap insurance against a
    multi-minute MINRES run. The result returned is whichever candidate has
    the smallest residual, so a fallback never returns a worse answer than
    the direct solve it replaced. When ``row_describer`` is supplied and
    every direct solve fails, the worst-residual rows are logged through it
    to pinpoint the offending copper region.
    """
    r_norm = float(np.linalg.norm(r))
    # Tolerance: residual should be well below the RHS magnitude. 1e-6
    # is conservative — a well-conditioned direct solve gives ~1e-12.
    abs_tol = max(_DIRECT_SOLVE_ABS_TOL_FLOOR, _DIRECT_SOLVE_REL_TOL * r_norm)

    def _residual(vec: np.ndarray) -> float:
        return float(np.linalg.norm(L_csc @ vec - r))

    # --- Staged direct solve ---------------------------------------------
    # Build the attempt list cheapest/fastest first. Symmetric matrices get
    # the fast symmetric-indefinite PARDISO path as primary; the unsymmetric
    # PARDISO factorisation (stronger, full pivoting) and SuperLU follow as
    # progressively more robust retries. Each is checked by the same
    # residual test; the first that passes wins.
    attempts: list = []
    if _HAVE_PARDISO:
        _configure_mkl_threads()
        if symmetric:
            attempts.append(
                ("pardiso-sym", lambda: _pardiso_solve_sym(L_csc, r)))
        attempts.append(("pardiso", lambda: _pypardiso.spsolve(L_csc, r)))
    attempts.append(
        ("superlu", lambda: scipy.sparse.linalg.spsolve(L_csc, r)))

    best_v: np.ndarray | None = None
    best_res = math.inf
    best_method = "none"
    for label, run in attempts:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # singular-matrix notices
                v = run()
        except Exception as e:
            log.warning("Direct solve (%s) raised (%s) — trying next method.",
                        label, e)
            continue
        residual_norm = _residual(v)
        if residual_norm <= abs_tol:
            log.debug(
                "Direct solve (%s): residual=%.4g (<= tol=%.4g, ||r||=%.4g) "
                "— good.", label, residual_norm, abs_tol, r_norm,
            )
            return v, label, 1, residual_norm
        log.warning(
            "Direct solve (%s) returned residual=%.4g (> tol=%.4g, "
            "||r||=%.4g) — near-singular, trying a more robust method.",
            label, residual_norm, abs_tol, r_norm,
        )
        if residual_norm < best_res:
            best_v, best_res, best_method = v, residual_norm, label

    # --- Every direct solve failed ---------------------------------------
    log.warning(
        "All direct solves failed (best was %s, residual=%.4g >> tol=%.4g, "
        "||r||=%.4g). The matrix is near-singular — the factorisation has "
        "small pivots and the solution doesn't satisfy L·v=r. This is "
        "usually caused by a near-floating copper region: an isolated mesh "
        "connected only via lumped elements, or a fragmented power net with "
        "a barely-there return path. Note the matrix depends only on the "
        "board topology, not on the source/sink values — editing those "
        "changes which RHS directions expose the singularity, not the "
        "singularity itself. Falling back to MINRES with Jacobi "
        "preconditioning. See KNOWN_ISSUES.md.",
        best_method, best_res, abs_tol, r_norm,
    )
    if row_describer is not None and best_v is not None:
        _log_singular_diagnostic(L_csc, r, best_v, row_describer)

    # Jacobi preconditioner: M⁻¹ ≈ diag(1/|L_ii|). Cheap and effective for
    # symmetric matrices with widely-varying diagonal entries (our case —
    # copper Laplacian diagonals are O(100-1000 S) while Lagrange-row
    # diagonals are 0 by construction, so we floor by a small ε).
    diag = np.asarray(L_csc.diagonal(), dtype=DTYPE)
    diag_abs = np.abs(diag)
    eps = max(_JACOBI_EPS_FLOOR,
              _JACOBI_EPS_REL * float(diag_abs.max()) if diag_abs.size
              else _JACOBI_EPS_FLOOR)
    inv_diag = 1.0 / np.where(diag_abs > eps, diag_abs, eps)
    M_precond = scipy.sparse.diags(inv_diag, format="csc")

    # Warm-start MINRES from the best direct solve. Even a failed direct
    # solve is usually correct across most of the system — only the
    # near-null-space components are wrong — so it is a far better x0 than
    # zero and cuts the iteration count substantially.
    x0 = best_v

    # MINRES tolerance: scipy's default is 1e-5 (rtol). Tightened to 1e-10
    # since we're already in the fallback path. maxiter is a hard ceiling;
    # the wall-clock budget enforced by the callback is the practical bound
    # — without it a multi-million-variable solve appears to hang.
    progress = _MinresProgress("Jacobi", _MINRES_TIME_BUDGET_S)
    try:
        v, info = scipy.sparse.linalg.minres(
            L_csc, r, x0=x0, rtol=_MINRES_RTOL, maxiter=_MINRES_MAXITER,
            M=M_precond, callback=progress,
        )
    except _MinresTimeout:
        v = progress.last_x if progress.last_x is not None else (
            x0 if x0 is not None else np.zeros_like(r))
        info = -1
        log.warning(
            "MINRES hit its %.0fs wall-clock budget after %d iterations — "
            "keeping the best iterate so far and trying ridge regularisation.",
            _MINRES_TIME_BUDGET_S, progress.iterations,
        )
    residual_norm = _residual(v)
    if info == 0 and residual_norm <= abs_tol:
        log.info(
            "MINRES converged: residual=%.4g (<= tol=%.4g) in %d iterations.",
            residual_norm, abs_tol, progress.iterations,
        )
        return v, "minres", progress.iterations, residual_norm

    log.warning(
        "MINRES did not converge cleanly: info=%d, residual=%.4g "
        "(tol=%.4g). Retrying with Tikhonov ridge regularisation.",
        info, residual_norm, abs_tol,
    )

    # Last-resort fallback: add a small ridge λI to make the matrix
    # definitively non-singular. The ridge biases the solution toward
    # v=0, so λ must be small enough that the bias is negligible compared
    # to the natural variable magnitudes (voltages ~ source voltage).
    # Choose λ as a tiny fraction of the matrix's largest diagonal entry.
    lam = max(_RIDGE_LAMBDA_FLOOR,
              _RIDGE_LAMBDA_REL * float(diag_abs.max()) if diag_abs.size
              else _RIDGE_LAMBDA_FLOOR)
    L_ridge = L_csc + lam * scipy.sparse.identity(L_csc.shape[0], format="csc",
                                                  dtype=DTYPE)
    progress = _MinresProgress("ridge", _MINRES_TIME_BUDGET_S)
    try:
        v_ridge, info = scipy.sparse.linalg.minres(
            L_ridge, r, x0=(v if v is not None else x0), rtol=_MINRES_RTOL,
            maxiter=_MINRES_MAXITER, M=M_precond, callback=progress,
        )
    except _MinresTimeout:
        v_ridge = progress.last_x if progress.last_x is not None else v
        info = -1
        log.warning(
            "MINRES+ridge hit its %.0fs budget after %d iterations.",
            _MINRES_TIME_BUDGET_S, progress.iterations,
        )
    ridge_res = _residual(v_ridge)

    # Return whichever candidate has the smallest residual against the
    # original system — plain MINRES, MINRES+ridge, or the best direct
    # solve. A fallback must never hand back a worse answer than it started
    # with. NaN residuals (a singular direct solve) sort last.
    candidates = [("minres", v, residual_norm),
                  ("minres+ridge", v_ridge, ridge_res)]
    if best_v is not None:
        candidates.append(("direct-best-effort", best_v, best_res))
    method, v_final, res_final = min(
        candidates,
        key=lambda c: c[2] if math.isfinite(c[2]) else math.inf,
    )
    log.info(
        "MINRES+ridge (λ=%.4g): info=%d, residual=%.4g. Best available "
        "solution: method=%s, residual=%.4g (tol=%.4g)%s.",
        lam, info, ridge_res, method, res_final, abs_tol,
        "" if res_final <= abs_tol else " — STILL ABOVE TOLERANCE; results "
        "for the near-floating region are unreliable",
    )
    return v_final, method, progress.iterations, res_final


def _record_stage(timings: list, label: str, t0: float, extra: str = "") -> None:
    """Log a solve stage's duration and append ``(label, seconds)`` to
    ``timings`` so :func:`solve` can print a ranked breakdown at the end.

    ``t0`` is the ``time.monotonic()`` captured when the stage started;
    ``extra`` is an optional suffix for the log line (counts, sizes, …).
    """
    dt = time.monotonic() - t0
    timings.append((label, dt))
    log.info(f"{label} done in {dt:.2f}s{extra}")


def _log_timing_breakdown(timings: list, total: float) -> None:
    """Log every solve stage sorted slowest-first, each with its share of the
    total wall-clock time — the at-a-glance view of where the solve spends
    its time and which stages are worth optimising.

    An ``(other / untimed)`` row captures whatever total time was not
    attributed to a named stage (small glue code between stages); if it is
    ever large, a stage is missing a timer.
    """
    log.info("=== Solve timing breakdown (slowest stage first) ===")
    accounted = 0.0
    for label, dt in sorted(timings, key=lambda kv: kv[1], reverse=True):
        accounted += dt
        pct = 100.0 * dt / total if total > 0 else 0.0
        log.info(f"  {dt:8.2f}s  {pct:5.1f}%  {label}")
    other = total - accounted
    pct_other = 100.0 * other / total if total > 0 else 0.0
    log.info(f"  {other:8.2f}s  {pct_other:5.1f}%  (other / untimed)")
    log.info(f"  {total:8.2f}s  100.0%  TOTAL")


def solve(prob: problem.Problem, mesher_config: mesh.Mesher.Config | None = None) -> Solution:
    """
    Solve the given PCB problem to find voltage and current distribution.

    Args:
        problem: The Problem object containing layers and lumped elements
        mesher_config: Configuration for mesh generation, uses defaults if None

    Returns:
        A Solution object with the computed results
    """
    # References:
    # https://www.cs.cmu.edu/~kmcrane/Projects/DDG/paper.pdf
    # http://mobile.rodolphe-vaillant.fr/entry/101/definition-laplacian-matrix-for-triangle-meshes
    # Note that if mesher_config = None, default parameters are used.
    mesher = mesh.Mesher(mesher_config)

    # Per-stage timing: ``_t0`` is captured right before each stage and
    # ``_record_stage`` logs a "… done in Xs" line and appends the duration
    # to ``timings``. After the solve, ``_log_timing_breakdown`` prints every
    # stage ranked by cost — the at-a-glance view of where time goes and
    # which stages are worth optimising.
    _total_t0 = time.monotonic()
    timings: list[tuple[str, float]] = []

    # As a first step, we flatten the Layer-Mesh tree to get a flat list of meshes.
    # We also keep track of which layer each mesh belongs to.
    # This will be needed later when we construct the final solution object.
    _t0 = time.monotonic()
    log.info("Constructing connectivity graph and finding connected layers")
    strtrees = construct_strtrees_from_layers(prob.layers)
    # Cache id(layer)→index so the O(L) prob.layers.index(...) call sites
    # (connectivity graph, dead-terminal filter, node indexer) all share
    # one O(1) dict lookup instead of doing a linear search per connection.
    layer_to_index = {id(layer): i for i, layer in enumerate(prob.layers)}
    connectivity_graph = ConnectivityGraph.create_from_problem(prob, strtrees)
    connected_layer_mesh_pairs = find_connected_layer_geom_indices(connectivity_graph)
    _record_stage(timings, "Connectivity graph", _t0)

    _t0 = time.monotonic()
    log.info("Meshing the connected components")
    meshes, mesh_index_to_layer_index = \
        generate_meshes_for_problem(prob, mesher, connected_layer_mesh_pairs, strtrees)
    _record_stage(timings, "Connected meshing", _t0, f" ({len(meshes)} mesh(es))")

    _t0 = time.monotonic()
    log.info("Meshing the disconnected components")
    disconnected_meshes_by_layer = \
        generate_disconnected_meshes(prob, connected_layer_mesh_pairs)
    _n_disc = sum(len(m) for m in disconnected_meshes_by_layer)
    _record_stage(timings, "Disconnected meshing", _t0, f" ({_n_disc} mesh(es))")

    # In the next step, we assign a global index to each vertex in every mesh.
    # This is needed since we need to somehow map the vertex indices to the
    # matrix indices in the final system of equations
    _t0 = time.monotonic()
    log.info("Indexing vertices and connections")
    vindex = VertexIndexer.create(meshes)
    _record_stage(timings, "Vertex indexing", _t0,
                  f" ({vindex.n_vertices} vertices)")

    _t0 = time.monotonic()
    log.info("Processing lumped element networks")
    # Now we need to filter out the lumped element networks that are not connected
    # to any of the meshes that we are driving with a source.
    filtered_networks = [
        net
        for net in prob.networks
        if not network_has_a_dead_terminal(
            net, prob, connected_layer_mesh_pairs, strtrees, layer_to_index,
        )
    ]
    _record_stage(timings, "Network filtering", _t0,
                  f" ({len(filtered_networks)}/{len(prob.networks)} kept)")
    _log_network_breakdown(filtered_networks, prob.networks)
    # Next, we construct the _internal_ system of equations for each of the
    # network.
    _t0 = time.monotonic()
    log.info("Constructing node index for networks")
    node_indexer = NodeIndexer.create(
        prob, meshes, mesh_index_to_layer_index, vindex, filtered_networks,
        layer_to_index=layer_to_index,
        # A pin more than a few mesh cells from any copper vertex on its net
        # isn't really on that copper — flag it rather than silently snapping.
        off_copper_threshold_mm=_OFF_COPPER_WARN_FACTOR * mesher.config.maximum_size,
    )
    _record_stage(timings, "Node indexing", _t0)

    # One voltage reference per electrically-isolated subsystem. A board with
    # a shared GND plane has exactly one (→ identical to the old single
    # ground); a PDN_NET single-net analysis is its own subsystem and gets
    # its own. Computed here because N depends on how many there are.
    ground_indices = find_ground_node_indices(
        filtered_networks, node_indexer, vindex,
    )
    n_ground = len(ground_indices)

    # We are solving the equation L * v = r
    # where L is the "laplace operator",
    # v is the voltage vector and
    # r is the right-hand side "source" vector
    N = vindex.n_vertices + \
        node_indexer.internal_node_count + \
        len(node_indexer.extra_source_to_global_index) + \
        n_ground  # one implicit ground node per isolated subsystem
    log.info(f"System matrix size: {N}x{N} variables")
    r = np.zeros(N, dtype=DTYPE)

    # Flat-COO assembly: every stamp (mesh laplacians, network elements,
    # ground node) appends to a single (rows, cols, vals) accumulator. The
    # global L is built ONCE at the end via coo_matrix(...).tocsc(), which
    # sums duplicate (i, j) entries automatically — equivalent to the
    # upstream lil_matrix's ``L[i, j] +=`` semantics, but without paying
    # ``lil_matrix.__setitem__``'s Python-level overhead per write.
    _t0 = time.monotonic()
    log.info("Constructing the Laplace operators")
    mesh_conductances = [
        prob.layers[mesh_index_to_layer_index[i]].conductance
        for i in range(len(meshes))
    ]
    mesh_rows, mesh_cols, mesh_vals = process_mesh_laplace_operators(
        meshes, mesh_conductances, vindex,
    )
    _record_stage(timings, "Laplace operator construction", _t0,
                  f" ({len(mesh_vals)} mesh entries)")

    # Network stamps are small (a handful of entries per element) — plain
    # Python list .extend is fine; we'll concatenate once at the end.
    net_rows: list = []
    net_cols: list = []
    net_vals: list = []

    _t0 = time.monotonic()
    log.info("Processing networks")
    for network in filtered_networks:
        stamp_network_into_system(
            network, node_indexer, net_rows, net_cols, net_vals, r,
        )
    _record_stage(timings, "Network stamping", _t0)

    total_sink_current = sum(
        elem.current
        for net in filtered_networks
        for elem in net.elements
        if isinstance(elem, problem.CurrentSource)
    )
    log.info(f"Total active sink current: {total_sink_current:.4g} A")

    log.info(f"Grounding {n_ground} isolated subsystem(s)")
    for i_gnd in ground_indices:
        if i_gnd < vindex.n_vertices:
            _mesh_i, _v_i = vindex.to_mesh_vertex(i_gnd)
            _pt = meshes[_mesh_i].vertices.to_object(_v_i).p
            log.debug(f"  ground reference at vertex ({_pt.x:.4g}, {_pt.y:.4g})")
        else:
            log.debug(f"  ground reference at internal node {i_gnd}")
    setup_ground_nodes(ground_indices, N, net_rows, net_cols, net_vals, r)

    # --- COO assembly: stitch mesh + network stamps into one triple -------
    _t0 = time.monotonic()
    log.info("Assembling COO triples")
    all_rows = np.concatenate([
        mesh_rows,
        np.asarray(net_rows, dtype=np.int64),
    ])
    all_cols = np.concatenate([
        mesh_cols,
        np.asarray(net_cols, dtype=np.int64),
    ])
    all_vals = np.concatenate([
        mesh_vals,
        np.asarray(net_vals, dtype=DTYPE),
    ])
    _record_stage(timings, "COO assembly", _t0, f" ({len(all_vals)} entries)")

    # Equipotential-patch contraction. Each directive terminal couples into
    # its pad as an equipotential patch: node_indexer.vertex_groups lists,
    # per pad, the mesh vertices under the pad outline. Collapsing each group
    # into one variable makes the pad an ideal conductor — the terminal
    # current then crosses the pad boundary distributed by the surrounding
    # copper, instead of all flowing through one vertex (the old point
    # source, which produced a log voltage singularity). The contraction is
    # just an index remap on the assembled COO triples: coo_matrix sums
    # duplicate (i, j) entries, which is exactly the row/column merge a node
    # contraction needs; the RHS is summed the same way with bincount.
    _t0 = time.monotonic()
    contraction = _build_contraction(N, node_indexer.vertex_groups)
    if contraction is not None:
        inverse, M = contraction
        solve_rows = inverse[all_rows]
        solve_cols = inverse[all_cols]
        # Sum each original RHS entry into its reduced slot. bincount is the
        # vectorised form of this scatter-add — far faster than np.add.at,
        # which falls back to an unbuffered element-by-element loop.
        r_solve = np.bincount(
            inverse, weights=r, minlength=M,
        ).astype(DTYPE, copy=False)
        _record_stage(
            timings, "Equipotential-patch contraction", _t0,
            f" ({N} → {M} vars, "
            f"{len(node_indexer.vertex_groups)} pad group(s))",
        )
    else:
        inverse, M = None, N
        solve_rows, solve_cols, r_solve = all_rows, all_cols, r
        _record_stage(timings, "Equipotential-patch contraction", _t0,
                      " (none)")

    # --- Sparse matrix build: COO → CSC -----------------------------------
    # The single COO→CSC pass sums duplicate (i, j) entries — this is what
    # makes assembly dramatically faster than the previous lil_matrix path.
    _t0 = time.monotonic()
    log.info("Building sparse matrix (COO → CSC)")
    L_csc = scipy.sparse.coo_matrix(
        (all_vals, (solve_rows, solve_cols)), shape=(M, M), dtype=DTYPE,
    ).tocsc()
    _record_stage(timings, "Matrix assembly (COO→CSC)", _t0,
                  f" ({L_csc.nnz} nonzeros)")

    # --- Linear solve -----------------------------------------------------
    _t0 = time.monotonic()
    log.info("Solving the linear system")
    # The MNA matrix is symmetric unless a VoltageRegulator is present — its
    # gain term is the only asymmetric stamp (the contraction above preserves
    # symmetry, remapping rows and columns identically). When symmetric,
    # PARDISO can use its faster symmetric-indefinite factorisation.
    matrix_is_symmetric = not any(
        isinstance(e, problem.VoltageRegulator)
        for net in filtered_networks for e in net.elements
    )
    # Direct sparse LU first — fast (~1 s on a 400K matrix) and exact when
    # the system is well-conditioned, which it almost always is. The MNA
    # matrix here is symmetric indefinite: Laplacian (PSD) + lumped Resistor
    # stamps (PSD) + VoltageSource and ground-constraint Lagrange rows
    # (which contribute negative eigenvalues). SuperLU usually handles this
    # without issue.
    #
    # However, certain pathological topologies — small isolated meshes
    # connected only by lumped elements, heavily fragmented power nets
    # with many small mesh pieces each having ~1 via to the bottom plane,
    # weak coupling between mesh components — produce a near-singular
    # matrix that SuperLU silently mis-solves: it returns a "solution"
    # whose residual is many orders of magnitude larger than machine
    # precision. The Lagrange-multiplier outputs (ground_node_current,
    # VoltageSource currents) come out wrong, leading to nonsensical
    # downstream voltages.
    #
    # Detect this and fall back to MINRES, the iterative solver designed
    # for exactly this case (symmetric indefinite, ill-conditioned). With
    # a Jacobi preconditioner it converges reliably even when direct LU
    # cannot. See KNOWN_ISSUES.md for the test case that motivated this.
    # Diagnostic hook: when every direct solve fails, _solve_robust calls
    # this to turn a worst-residual (reduced) matrix row back into a
    # human-readable location — the copper (layer, net) slab and coordinates
    # — so the near-floating region that drove the matrix singular can be
    # pinpointed. The reduced→full index map is materialised lazily, only
    # if a failure actually occurs.
    _n_vert = vindex.n_vertices
    _n_internal = node_indexer.internal_node_count
    _n_extra = len(node_indexer.extra_source_to_global_index)
    _reduced_to_full: list = []

    def _describe_solver_row(reduced_idx: int) -> str:
        if inverse is None:
            full = int(reduced_idx)
        else:
            if not _reduced_to_full:
                r2f = np.empty(M, dtype=np.int64)
                r2f[inverse] = np.arange(N, dtype=np.int64)
                _reduced_to_full.append(r2f)
            full = int(_reduced_to_full[0][reduced_idx])
        if full < _n_vert:
            mesh_i, vtx_i = vindex.to_mesh_vertex(full)
            layer_name = prob.layers[mesh_index_to_layer_index[mesh_i]].name
            try:
                pt = meshes[mesh_i].vertices.to_object(vtx_i).p
                where = f" near ({pt.x:.3f}, {pt.y:.3f}) mm"
            except Exception:
                where = ""
            return (f"copper on (layer,net) slab '{layer_name}'{where} "
                    f"[mesh {mesh_i}]")
        full -= _n_vert
        if full < _n_internal:
            return "lumped-network internal node (no copper attachment)"
        full -= _n_internal
        if full < _n_extra:
            return "voltage-source current variable (MNA Lagrange row)"
        return "ground-reference variable (an isolated subsystem)"

    v_solve, solver_method, solver_iterations, residual_norm = _solve_robust(
        L_csc, r_solve, symmetric=matrix_is_symmetric,
        row_describer=_describe_solver_row,
    )
    # Expand the reduced solution back to the full N-variable space so every
    # downstream consumer (per-layer ZeroForms, diagnostics) is unchanged —
    # the vertices in a pad group all receive their patch's single solved
    # potential.
    v = v_solve[inverse] if inverse is not None else v_solve
    _record_stage(timings, "Linear solve", _t0,
                  f" (method={solver_method}, iter={solver_iterations}, N={M})")

    # --- Solver diagnostics ----------------------------------------------
    # The residual is measured against the system actually solved (reduced
    # when a contraction was applied). ``_solve_robust`` already computed
    # ``||L_csc·v_solve - r_solve||`` for its fallback check and handed it
    # back — reuse it rather than repeating that 2M-row sparse mat-vec.
    _t0 = time.monotonic()
    # The implicit ground variables are the last n_ground entries. A
    # well-posed solve drives every one to ~0 (each subsystem is balanced);
    # report the worst-balanced as the diagnostic.
    ground_currents = v[N - n_ground:] if n_ground else np.zeros(1)
    ground_node_current = float(
        ground_currents[np.argmax(np.abs(ground_currents))]
    )
    solver_info = SolverInfo(
        ground_node_current=ground_node_current,
        residual_norm=residual_norm,
    )
    _record_stage(timings, "Solver diagnostics", _t0)

    if not np.isclose(ground_node_current, 0):
        # This is a warning, but we still continue to produce the solution object
        # since it may still be useful for the user.
        fraction = (
            abs(ground_node_current / total_sink_current)
            if total_sink_current != 0 else float('inf')
        )
        warnings.warn(
            f"Ground node current is not zero ({ground_node_current:.4g} A, "
            f"{fraction:.1%} of total sink current {total_sink_current:.4g} A). "
            "Likely causes: isolated GND copper regions with no via path to the "
            "reference node, or GND return pins landing on copper not reachable "
            "from the chosen reference.  Relative voltage-drop patterns are still "
            "qualitatively correct, but absolute values are unreliable.",
            SolverWarning,
        )

    # And now we just grab the final solution vector and reconstruct it back
    # into a solution object for easier consumption by the caller.
    _t0 = time.monotonic()
    log.info("Producing the solution object")
    layer_solutions = produce_layer_solutions(
        prob.layers,
        vindex,
        meshes,
        mesh_index_to_layer_index,
        v,
        disconnected_meshes_by_layer
    )
    _record_stage(timings, "Solution object", _t0)

    _total = time.monotonic() - _total_t0
    log.info(f"Total solve time: {_total:.2f}s "
             f"(from inside pdnsolver.solver.solve)")
    _log_timing_breakdown(timings, _total)

    return Solution(problem=prob, layer_solutions=layer_solutions, solver_info=solver_info)
