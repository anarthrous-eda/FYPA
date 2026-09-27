"""Copper return-on-investment: where would more copper help a load most?

Built on the adjoint fields the solver returns per SINK (see
:mod:`pdnsolver.sensitivity`). Everything here is Qt-free so the viewer, the
report and a future CLI check can share it.

Three kinds of fix are ranked, matching how PDN problems are fixed in
practice (widen first, add layers and vias, thicken last):

* **widen** — push a copper edge outward. The value density at the edge
  times the edge length is the gain per mm of widening; runs of high-value
  edge are reported per :data:`WIDEN_STEP_MM` of widening, small enough for
  the first-order estimate to hold.
* **parallel** — a stitched copy of the copper, same weight, on another
  layer over a region. The first-order value of doubling a region's
  conductance overstates the gain by 2x on a series path (halving a
  resistance saves half its drop, not all of it), so it is halved — exact
  for a series path, conservative-to-fair for spreading.
* **via** — one more via beside an existing one, halved the same way.

The target load is ranked by how much of its drop budget it uses: the drop
from the rail's setpoint over ``nominal - PDN_MIN_V`` when PDN_MIN_V is set,
else over :data:`DEFAULT_BUDGET_PCT` of nominal. Drops and margins come from
:func:`fypa.report.collect.build_report`, so the ranking always agrees with
the design report.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

import numpy as np

from fypa.solution_sampling import MATCH_TOL_MM, split_composite_name
from pdnsolver.sensitivity import mesh_adjoint, triangle_areas, triangle_values

log = logging.getLogger(__name__)

# Drop budget, as a share of the rail's nominal voltage, for a load with no
# PDN_MIN_V.
DEFAULT_BUDGET_PCT: float = 5.0
# Widening step the "widen" estimates are quoted for.
WIDEN_STEP_MM: float = 0.25
# An edge (or grid cell) joins a reported run / region when its value is at
# least this share of the target's best edge / cell.
_RUN_THRESHOLD: float = 0.25
_REGION_THRESHOLD: float = 0.2
# A layer counts as free for a parallel pour when at least this share of
# the region has no other net's copper on it.
_FREE_LAYER_SHARE: float = 0.7
# Widening counts as blocked when other copper covers this share of the
# band the widened edge would occupy.
_BLOCKED_SHARE: float = 0.3
# Series-path correction for "double the copper / add an identical via".
_DOUBLING_FACTOR: float = 0.5
# Hot edge runs separated by less than this much cooler edge are one run.
_RUN_GAP_MM: float = 1.5
# Most fixes of one kind listed, and the smallest worth listing relative to
# the best unblocked fix.
_PER_KIND: int = 4
_MIN_SHARE_OF_BEST: float = 0.05


# --- Target ranking ------------------------------------------------------------

@dataclass
class LoadScore:
    label: str
    rail: str
    nominal_v: float
    drop_v: float
    budget_v: float
    basis: str            # "PDN_MIN_V" | "default"
    used: float           # drop / budget; > 1 is over budget
    has_field: bool       # the solve returned an adjoint for it

    @property
    def basis_text(self) -> str:
        return ("PDN_MIN_V" if self.basis == "PDN_MIN_V"
                else f"{DEFAULT_BUDGET_PCT:g} % default")


def rank_loads(solution, metadata: dict,
               default_budget_pct: float = DEFAULT_BUDGET_PCT
               ) -> list[LoadScore]:
    """Every solved SINK, the most of its drop budget used first."""
    from fypa.report.collect import build_report

    report = build_report(solution, metadata, render_figures=False,
                          topology=False)
    have = set(getattr(solution, "sensitivity", None) or {})
    out: list[LoadScore] = []
    for rail in report.rails:
        nominal = float(rail.nominal_v or 0.0)
        if rail.kind != "supply" or nominal <= 0:
            continue
        for ld in rail.loads:
            if ld.kind != "sink" or ld.drop_v is None:
                continue
            if ld.min_v is not None and nominal - ld.min_v > 0:
                budget, basis = nominal - ld.min_v, "PDN_MIN_V"
            else:
                budget, basis = nominal * default_budget_pct / 100.0, "default"
            used = ld.drop_v / budget if budget > 0 else math.inf
            out.append(LoadScore(
                label=ld.label, rail=rail.name, nominal_v=nominal,
                drop_v=float(ld.drop_v), budget_v=budget, basis=basis,
                used=used, has_field=ld.label in have))
    out.sort(key=lambda s: (-s.used, s.label))
    return out


def default_target(scores: list[LoadScore]) -> LoadScore | None:
    """The worst-margin load that has a sensitivity field."""
    return next((s for s in scores if s.has_field), None)


# --- The value field ------------------------------------------------------------

def _mesh_sigma(ls, layer, mesh_i: int) -> float | np.ndarray:
    tc = getattr(ls, "tri_conductances", None) or []
    if mesh_i < len(tc) and tc[mesh_i] is not None and len(tc[mesh_i]):
        return np.asarray(tc[mesh_i], dtype=np.float64)
    return float(layer.conductance)


def value_density(solution, key: str) -> dict[int, list[np.ndarray | None]]:
    """Per layer index, per mesh: each triangle's value density (V/mm²) for
    ``key`` — the load-voltage gain per mm² of parallel copper. Layers with
    no field for ``key`` are absent."""
    out: dict[int, list[np.ndarray | None]] = {}
    for li, ls in enumerate(solution.layer_solutions):
        if key not in (getattr(ls, "adjoints", None) or {}):
            continue
        layer = solution.problem.layers[li]
        per_mesh: list[np.ndarray | None] = []
        for mi, (xys, tris, pot) in enumerate(
                zip(ls.vertex_xys, ls.triangles, ls.potentials)):
            lam = mesh_adjoint(ls, key, mi)
            tris = np.asarray(tris)
            if lam is None or tris.size == 0:
                per_mesh.append(None)
                continue
            xys = np.asarray(xys)
            vals = triangle_values(xys, tris, np.asarray(pot), lam,
                                   _mesh_sigma(ls, layer, mi))
            area = triangle_areas(xys, tris)
            dens = np.zeros_like(vals)
            np.divide(vals, area, out=dens, where=area > 0)
            per_mesh.append(dens)
        out[li] = per_mesh
    return out


# --- Opportunities --------------------------------------------------------------

@dataclass
class Opportunity:
    kind: str                          # "widen" | "parallel" | "via"
    value_v: float                     # estimated load-voltage gain (V)
    layer: str                         # physical layer name
    net: str
    x_mm: float                        # where to look
    y_mm: float
    bbox: tuple[float, float, float, float]
    title: str
    detail: str = ""
    # Drawing: widen -> the edge run polyline in ``coords``; parallel -> the
    # region's outline(s) in ``rings``; via -> neither (the point is x/y).
    coords: list[tuple[float, float]] = field(default_factory=list)
    rings: list[list[tuple[float, float]]] = field(default_factory=list)
    blocked: bool = False


@dataclass
class RoiResult:
    key: str
    density: dict[int, list[np.ndarray | None]]
    opportunities: list[Opportunity]
    max_density: float                 # for the colour scale (V/mm²)


def analyse(solution, metadata: dict, key: str, *,
            copper_by_layer: dict[str, list[tuple[str, object]]] | None = None,
            limit: int = 12) -> RoiResult:
    """Value field plus the ranked fixes for one target load.

    ``copper_by_layer`` maps physical layer name -> ``[(net, geometry), …]``
    for ALL copper (signal nets included), used to tell whether widening is
    blocked and which layers are free for a parallel pour. Without it only
    the solved nets' copper is known.
    """
    density = value_density(solution, key)
    copper = _Copper(copper_by_layer or _solved_copper(solution))
    opps: list[Opportunity] = []
    for kind_opps in (_widen_runs(solution, density, copper),
                      _parallel_regions(solution, metadata, density, copper),
                      _via_values(solution, metadata, key)):
        # Cap each kind so one can't crowd the others out of the list.
        kind_opps = sorted((o for o in kind_opps if o.value_v > 0),
                           key=lambda o: (o.blocked, -o.value_v))
        opps += kind_opps[:_PER_KIND]
    best = max((o.value_v for o in opps if not o.blocked), default=0.0)
    opps = [o for o in opps if o.value_v >= _MIN_SHARE_OF_BEST * best]
    opps.sort(key=lambda o: (o.blocked, -o.value_v))
    peak = 0.0
    for per_mesh in density.values():
        for d in per_mesh:
            if d is not None and d.size:
                peak = max(peak, float(np.percentile(np.maximum(d, 0), 99.5)))
    return RoiResult(key=key, density=density, opportunities=opps[:limit],
                     max_density=peak)


def _solved_copper(solution) -> dict[str, list[tuple[str, object]]]:
    out: dict[str, list[tuple[str, object]]] = {}
    for layer in solution.problem.layers:
        phys, net = split_composite_name(layer.name)
        if layer.shape is not None:
            out.setdefault(phys, []).append((net, layer.shape))
    return out


class _Copper:
    """Per-layer copper lookups with the (costly) unions cached."""

    def __init__(self, by_layer: dict[str, list[tuple[str, object]]]):
        self.by_layer = by_layer
        self._others: dict[tuple[str, str], object] = {}
        self._own: dict[tuple[str, str], object] = {}

    def layers(self) -> list[str]:
        return sorted(self.by_layer)

    @staticmethod
    def _union(geoms):
        from shapely.ops import unary_union

        geoms = [g for g in geoms if g is not None]
        return unary_union(geoms) if geoms else None

    def others(self, phys: str, net: str):
        """Every other net's copper on a layer (None when there is none)."""
        k = (phys, net)
        if k not in self._others:
            self._others[k] = self._union(
                g for n, g in self.by_layer.get(phys, []) if n != net)
        return self._others[k]

    def own(self, phys: str, net: str):
        k = (phys, net)
        if k not in self._own:
            self._own[k] = self._union(
                g for n, g in self.by_layer.get(phys, []) if n == net)
        return self._own[k]


def _fmt_len(mm: float) -> str:
    return f"{mm:.1f}" if mm < 10 else f"{mm:.0f}"


def _fmt_mv(v: float) -> str:
    mv = v * 1e3
    return f"{mv:.2g} mV" if abs(mv) < 10 else f"{mv:.0f} mV"


# --- widen ----------------------------------------------------------------------

def _boundary_edges(tris: np.ndarray):
    """(a, b, triangle) for every edge used by exactly one triangle."""
    n = tris.shape[0]
    e = np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]])
    owner = np.tile(np.arange(n), 3)
    lo = np.minimum(e[:, 0], e[:, 1]).astype(np.int64)
    hi = np.maximum(e[:, 0], e[:, 1]).astype(np.int64)
    # One int64 per undirected edge: a 1-D unique is far faster than a
    # row-wise one.
    key = lo * (int(tris.max()) + 1) + hi
    _, inv, counts = np.unique(key, return_inverse=True, return_counts=True)
    once = counts[inv] == 1
    return e[once], owner[once]


def _edge_loops(edges: np.ndarray) -> list[list[int]]:
    """Group boundary edges (by index) into chains that follow the outline."""
    by_vertex: dict[int, list[int]] = {}
    for i, (a, b) in enumerate(edges):
        by_vertex.setdefault(int(a), []).append(i)
        by_vertex.setdefault(int(b), []).append(i)
    seen = np.zeros(len(edges), dtype=bool)
    loops: list[list[int]] = []
    for start in range(len(edges)):
        if seen[start]:
            continue
        loop = [start]
        seen[start] = True
        cur = int(edges[start][1])
        while True:
            nxt = next((j for j in by_vertex.get(cur, ()) if not seen[j]), None)
            if nxt is None:
                break
            seen[nxt] = True
            loop.append(nxt)
            a, b = edges[nxt]
            cur = int(b) if int(a) == cur else int(a)
        loops.append(loop)
    return loops


def _runs(values: np.ndarray, threshold: float,
          lengths: np.ndarray | None = None,
          gap_mm: float = 0.0) -> list[tuple[int, int]]:
    """Maximal [start, end) spans of a closed loop with values >= threshold,
    joined across the loop's wrap-around. With ``lengths``, cool stretches
    shorter than ``gap_mm`` between two hot ones are absorbed, so an edge
    whose value dips briefly (a pad, a mesh artefact) stays one run."""
    hot = values >= threshold
    n = len(values)
    if lengths is not None and gap_mm > 0 and hot.any() and not hot.all():
        # Walk the loop from just after a hot edge so every cool gap is seen
        # whole, and fill the short ones.
        start = (int(np.flatnonzero(hot)[-1]) + 1) % n
        order = (np.arange(n) + start) % n
        i = 0
        while i < n:
            if hot[order[i]]:
                i += 1
                continue
            j = i
            while j < n and not hot[order[j]]:
                j += 1
            if j < n and lengths[order[i:j]].sum() < gap_mm:
                hot[order[i:j]] = True
            i = j
    if not hot.any():
        return []
    if hot.all():
        return [(0, n)]
    runs, i = [], 0
    while i < n:
        if hot[i]:
            j = i
            while j < n and hot[j]:
                j += 1
            runs.append((i, j))
            i = j
        else:
            i += 1
    if len(runs) > 1 and runs[0][0] == 0 and runs[-1][1] == n:
        s, _ = runs.pop()
        runs[0] = (s - n, runs[0][1])
    return runs


def _widen_runs(solution, density, copper) -> list[Opportunity]:
    from shapely.geometry import LineString

    cands = []  # (value per mm, phys, net, polyline, mesh id)
    per_mesh_edges = []
    # Meshes whose whole field is negligible can't hold a reportable run.
    floor = 1e-3 * max(
        (float(d.max()) for pm in density.values() for d in pm
         if d is not None and d.size), default=0.0)
    for li, per_mesh in density.items():
        ls = solution.layer_solutions[li]
        for mi, dens in enumerate(per_mesh):
            if dens is None or not dens.size or float(dens.max()) <= floor:
                continue
            tris = np.asarray(ls.triangles[mi])
            xys = np.asarray(ls.vertex_xys[mi])
            edges, owner = _boundary_edges(tris)
            if not len(edges):
                continue
            d = np.maximum(dens[owner], 0.0)
            lengths = np.linalg.norm(xys[edges[:, 0]] - xys[edges[:, 1]],
                                     axis=1)
            per_mesh_edges.append((li, mi, xys, edges, d, lengths))
    if not per_mesh_edges:
        return []
    # Threshold against a robust peak: current crowds into pad corners, and
    # one such edge would otherwise set the bar so high that a long,
    # uniformly valuable trace never qualifies.
    all_d = np.concatenate([p[4] for p in per_mesh_edges])
    all_len = np.concatenate([p[5] for p in per_mesh_edges])
    peak = _weighted_percentile(all_d, all_len, 98.0)
    if peak <= 0:
        return []
    for li, mi, xys, edges, d, lengths in per_mesh_edges:
        phys, net = split_composite_name(solution.problem.layers[li].name)
        for loop in _edge_loops(edges):
            idx = np.asarray(loop)
            for s, e in _runs(d[idx], _RUN_THRESHOLD * peak,
                              lengths[idx], _RUN_GAP_MM):
                for sel in _split_at_corners(idx[np.arange(s, e) % len(idx)],
                                             edges, xys):
                    value_per_mm = float((d[sel] * lengths[sel]).sum())
                    pts = _chain_points(edges[sel], xys)
                    cands.append((value_per_mm, phys, net, pts, (li, mi)))
    cands.sort(key=lambda c: -c[0])
    # One entry per stretch of copper: the two edges of a trace are one
    # suggestion ("widen either side"), and when only one of them is free
    # the free one is the suggestion.
    entries: list[dict] = []
    for value_per_mm, phys, net, pts, mesh_id in cands[:4 * _PER_KIND]:
        line = LineString(pts) if len(pts) > 1 else None
        if line is None:
            continue
        # First order is linear in the step; the copper's own width w sets
        # how far that holds. Widening by δ scales a series path's
        # conductance by (1 + δ/w), which saves 1/(1 + δ/w) of the linear
        # estimate — ~1 for a plane, ½ when the step doubles a trace.
        width = _local_width(copper.own(phys, net), line)
        gain = value_per_mm * WIDEN_STEP_MM / (1.0 + WIDEN_STEP_MM / width)
        blocked = _widen_blocked(copper, phys, net, line)
        new = {"mesh": mesh_id, "line": line, "width": width, "gain": gain,
               "blocked": blocked, "phys": phys, "net": net, "pts": pts,
               "sides": 1}
        twin = next((e for e in entries if e["mesh"] == mesh_id
                     and _same_stretch(line, e["line"],
                                       min(width, e["width"]))), None)
        if twin is None:
            entries.append(new)
            continue
        if twin["blocked"] and not blocked:
            twin.update(new)          # the free side is the suggestion
        elif not twin["blocked"] and not blocked:
            twin["sides"] = 2
    out = []
    for e in entries:
        line, gain = e["line"], e["gain"]
        mid = line.interpolate(0.5, normalized=True)
        x0, y0, x1, y1 = line.bounds
        where = " (either side of the trace)" if e["sides"] > 1 else ""
        out.append(Opportunity(
            kind="widen", value_v=gain, layer=e["phys"], net=e["net"],
            x_mm=mid.x, y_mm=mid.y, bbox=(x0, y0, x1, y1),
            title=f"Widen {e['net']} on {e['phys']} by {WIDEN_STEP_MM:g} mm",
            detail=(f"est. +{_fmt_mv(gain)} pushing out {line.length:.0f} mm "
                    f"of edge{where}"
                    + (" — blocked by other copper" if e["blocked"] else "")),
            coords=[(float(x), float(y)) for x, y in e["pts"]],
            blocked=e["blocked"]))
    return out


# A run turning more sharply than this at one vertex is split there, so each
# run is one side of the copper (a trace's two edges and its ends apart).
_CORNER_DEG: float = 50.0


def _split_at_corners(sel: np.ndarray, edges: np.ndarray,
                      xys: np.ndarray) -> list[np.ndarray]:
    """Split a run of consecutive boundary edges at its sharp corners."""
    if len(sel) < 2:
        return [sel]
    pts = np.asarray(_chain_points(edges[sel], xys), dtype=np.float64)
    seg = np.diff(pts, axis=0)
    ang = np.arctan2(seg[:, 1], seg[:, 0])
    turn = np.abs((np.diff(ang) + np.pi) % (2 * np.pi) - np.pi)
    cuts = np.flatnonzero(turn > math.radians(_CORNER_DEG)) + 1
    return [part for part in np.split(sel, cuts) if len(part)]


def _weighted_percentile(values: np.ndarray, weights: np.ndarray,
                         pct: float) -> float:
    if not values.size or float(weights.sum()) <= 0:
        return 0.0
    order = np.argsort(values)
    cum = np.cumsum(weights[order])
    k = int(np.searchsorted(cum, pct / 100.0 * cum[-1]))
    return float(values[order[min(k, len(order) - 1)]])


def _same_stretch(a, b, width: float, samples: int = 16) -> bool:
    """Run ``a`` faces the already-listed run ``b`` across one stretch of
    copper ``width`` wide — the other side of the same trace: every point of
    ``a`` is within about a width of ``b``. One-directional, so a piece of
    the far side still matches a whole near side."""
    if not math.isfinite(width):
        return False
    reach = 1.5 * width + 0.05
    return all(
        b.distance(a.interpolate((k + 0.5) / samples, normalized=True))
        <= reach
        for k in range(samples))


def _chain_points(edges: np.ndarray, xys: np.ndarray) -> list[tuple]:
    """Ordered points along a run of consecutive boundary edges."""
    if not len(edges):
        return []
    a, b = int(edges[0][0]), int(edges[0][1])
    if len(edges) > 1 and a in (int(edges[1][0]), int(edges[1][1])):
        a, b = b, a
    order = [a, b]
    for p, q in edges[1:]:
        p, q = int(p), int(q)
        order.append(q if p == order[-1] else p)
    return [tuple(xys[i]) for i in order]


def _local_width(own, line, samples: int = 5, reach_mm: float = 50.0) -> float:
    """Copper width across an edge run: from points along the run, the
    distance inward to the far side of the copper (median of a few rays)."""
    from shapely.geometry import LineString, Point

    if own is None or line.length <= 0:
        return math.inf
    boundary = own.boundary
    widths = []
    for k in range(samples):
        t = (k + 0.5) / samples
        p = line.interpolate(t, normalized=True)
        q = line.interpolate(min(1.0, t + 0.01), normalized=True)
        r = line.interpolate(max(0.0, t - 0.01), normalized=True)
        tx, ty = q.x - r.x, q.y - r.y
        norm = math.hypot(tx, ty)
        if norm == 0:
            continue
        nx, ny = -ty / norm, tx / norm
        if not own.contains(Point(p.x + nx * 1e-3, p.y + ny * 1e-3)):
            nx, ny = -nx, -ny
        ray = LineString([(p.x + nx * 1e-3, p.y + ny * 1e-3),
                          (p.x + nx * reach_mm, p.y + ny * reach_mm)])
        hit = ray.intersection(boundary)
        if hit.is_empty:
            continue
        d = min(Point(p.x, p.y).distance(g)
                for g in getattr(hit, "geoms", [hit]))
        if d > 1e-3:
            widths.append(d)
    return float(np.median(widths)) if widths else math.inf


def _widen_blocked(copper, phys: str, net: str, line) -> bool:
    other = copper.others(phys, net)
    if other is None:
        return False
    band = line.buffer(WIDEN_STEP_MM * 2, cap_style="flat")
    own = copper.own(phys, net)
    if own is not None:
        band = band.difference(own)
    if band.is_empty or band.area <= 0:
        return False
    return band.intersection(other).area / band.area >= _BLOCKED_SHARE


# --- parallel -------------------------------------------------------------------

def _parallel_regions(solution, metadata, density, copper) -> list[Opportunity]:
    from scipy import ndimage
    from shapely.geometry import box
    from shapely.ops import unary_union

    # Per slab: triangle centroids and values (V).
    slabs = []
    total_area = 0.0
    for li, per_mesh in density.items():
        ls = solution.layer_solutions[li]
        cs, vs = [], []
        for mi, dens in enumerate(per_mesh):
            if dens is None:
                continue
            xys = np.asarray(ls.vertex_xys[mi])
            tris = np.asarray(ls.triangles[mi])
            area = triangle_areas(xys, tris)
            total_area += float(area.sum())
            cs.append(xys[tris].mean(axis=1))
            vs.append(np.maximum(dens, 0.0) * area)
        if cs:
            slabs.append((li, np.concatenate(cs), np.concatenate(vs)))
    if not slabs or total_area <= 0:
        return []
    cell = float(np.clip(math.sqrt(total_area) / 40.0, 0.5, 5.0))

    grids = []
    peak = 0.0
    for li, cen, val in slabs:
        x0, y0 = cen.min(axis=0)
        ix = ((cen[:, 0] - x0) / cell).astype(int)
        iy = ((cen[:, 1] - y0) / cell).astype(int)
        grid = np.zeros((ix.max() + 1, iy.max() + 1))
        np.add.at(grid, (ix, iy), val)
        peak = max(peak, float(grid.max()))
        grids.append((li, x0, y0, grid))
    if peak <= 0:
        return []

    phys_order = [row.get("name") for row in metadata.get("stackup", [])
                  if row.get("name")]
    out = []
    for li, x0, y0, grid in grids:
        phys, net = split_composite_name(solution.problem.layers[li].name)
        own_shape = solution.problem.layers[li].shape
        labels, n = ndimage.label(grid >= _REGION_THRESHOLD * peak,
                                  structure=np.ones((3, 3)))
        for k in range(1, n + 1):
            cells = np.argwhere(labels == k)
            value = float(grid[labels == k].sum()) * _DOUBLING_FACTOR
            region = unary_union([
                box(x0 + i * cell, y0 + j * cell,
                    x0 + (i + 1) * cell, y0 + (j + 1) * cell)
                for i, j in cells])
            if own_shape is not None:
                region = region.intersection(own_shape)
            if region.is_empty or region.area <= 0:
                continue
            free = _free_layers(copper, phys_order, phys, net, region)
            # Label on the copper itself (a curved trace's centroid sits in
            # empty board), outline the copper the region actually covers.
            c = region.representative_point()
            bx0, by0, bx1, by1 = region.bounds
            where = (f"free on {', '.join(free)}" if free
                     else "no layer is free over this area")
            rings = [[(float(x), float(y)) for x, y in poly.exterior.coords]
                     for poly in getattr(region, "geoms", [region])
                     if hasattr(poly, "exterior")]
            out.append(Opportunity(
                kind="parallel", value_v=value, layer=phys, net=net,
                x_mm=c.x, y_mm=c.y, bbox=(bx0, by0, bx1, by1),
                title=(f"Parallel {net} path alongside {phys} "
                       f"({_fmt_len(bx1 - bx0)} × {_fmt_len(by1 - by0)} mm)"),
                detail=f"est. +{_fmt_mv(value)} if stitched in; {where}",
                rings=rings,
                blocked=not free))
    return out


def _free_layers(copper, phys_order, phys, net, region) -> list[str]:
    free = []
    for other_phys in phys_order or copper.layers():
        if other_phys == phys:
            continue
        own_there = copper.own(other_phys, net)
        taken = copper.others(other_phys, net)
        blocked_area = 0.0
        if taken is not None:
            blocked_area = region.intersection(taken).area
        already = (region.intersection(own_there).area
                   if own_there is not None else 0.0)
        # A layer that already carries this net here isn't a new path.
        if already / region.area > 0.5:
            continue
        if 1.0 - blocked_area / region.area >= _FREE_LAYER_SHARE:
            free.append(other_phys)
    return free


# --- vias -----------------------------------------------------------------------

class _FieldSampler:
    """Nearest-vertex (v, λ) lookup per (physical layer, net) for one key."""

    def __init__(self, solution, key: str):
        self.solution = solution
        self.key = key
        self.index_by_pair = {split_composite_name(L.name): i
                              for i, L in enumerate(solution.problem.layers)}
        self._trees: dict = {}

    def tree(self, phys: str, net: str):
        pair = (phys, net)
        if pair in self._trees:
            return self._trees[pair]
        from scipy.spatial import cKDTree

        li = self.index_by_pair.get(pair)
        res = None
        if li is not None:
            ls = self.solution.layer_solutions[li]
            pts, vs, lams = [], [], []
            for mi, (xys, tris, pot) in enumerate(
                    zip(ls.vertex_xys, ls.triangles, ls.potentials)):
                lam = mesh_adjoint(ls, self.key, mi)
                tris = np.asarray(tris)
                if lam is None or tris.size == 0:
                    continue
                used = np.unique(tris.ravel())
                pts.append(np.asarray(xys)[used])
                vs.append(np.asarray(pot)[used])
                lams.append(lam[used])
            if pts:
                res = (cKDTree(np.concatenate(pts)), np.concatenate(vs),
                       np.concatenate(lams))
        self._trees[pair] = res
        return res

    def sample(self, phys: str, net: str, x: float, y: float):
        t = self.tree(phys, net)
        if t is None:
            return None
        d, i = t[0].query((x, y))
        if d > MATCH_TOL_MM:
            return None
        return float(t[1][i]), float(t[2][i])


def _via_values(solution, metadata, key: str) -> list[Opportunity]:
    from pdnsolver.sensitivity import resistor_value

    sampler = _FieldSampler(solution, key)
    id_to_phys = {row["layer_id"]: row["name"]
                  for row in metadata.get("stackup", [])}
    scored = []
    for via in metadata.get("vias", []):
        net = via.get("net", "")
        segs = via.get("segments") or []
        if not net or not segs:
            continue
        x, y = float(via.get("x_mm", 0.0)), float(via.get("y_mm", 0.0))
        value = 0.0
        for seg in segs:
            r = float(seg.get("resistance_ohm") or 0.0)
            pa = id_to_phys.get(int(seg["layer_a"]))
            pb = id_to_phys.get(int(seg["layer_b"]))
            if r <= 0 or pa is None or pb is None:
                continue
            sa = sampler.sample(pa, net, x, y)
            sb = sampler.sample(pb, net, x, y)
            if sa is None or sb is None:
                continue
            value += resistor_value((sa[0] - sb[0]) / r, sa[1], sb[1])
        if value > 0:
            scored.append((value * _DOUBLING_FACTOR, net, x, y, via))
    scored.sort(key=lambda s: -s[0])
    out = []
    for value, net, x, y, via in scored[:8]:
        span = ""
        a, b = via.get("layer_start"), via.get("layer_end")
        if a in id_to_phys and b in id_to_phys:
            span = f" ({id_to_phys[a]} → {id_to_phys[b]})"
        out.append(Opportunity(
            kind="via", value_v=value, layer=id_to_phys.get(a, ""), net=net,
            x_mm=x, y_mm=y, bbox=(x - 1, y - 1, x + 1, y + 1),
            title=f"Add a {net} via beside ({x:.1f}, {y:.1f}){span}",
            detail=f"est. +{_fmt_mv(value)} per extra via"))
    return out
