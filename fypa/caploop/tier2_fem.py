"""Tier 2 — plane-pair spreading inductance by FEM.

The closed form in :mod:`fypa.caploop.tier1` assumes an unbroken cavity. Real
power planes are split and perforated, which is exactly where that assumption
fails and where a solve pays for itself. This module reuses FYPA's existing
2-D cotangent-Laplacian FEM (:mod:`pdnsolver`) unchanged, exploiting the
duality between DC spreading *resistance* and magnetostatic spreading
*inductance*.

**The duality.** A plane pair of separation ``h`` carries equal and opposite
sheet currents. Its inter-plane potential ``Φ`` obeys
``∇·((1/(μ0·h))·∇Φ) = 0`` with current injected at ports — the same PDE the
DC solve applies to ``∇·(σ·t·∇V) = 0``. So a :class:`pdnsolver.problem.Layer`
whose ``conductance`` is set to ``1/(μ0·h)`` (units 1/H per square, not
siemens) and a 1 A :class:`~pdnsolver.problem.CurrentSource` between two
ports yield a solved "potential" difference that **is** the spreading
inductance in henries. No scaling anywhere else.

**Ports.** A capacitor's port is the place its escape vias carry current
between the two planes — a single port in the 2-D cavity field, not one port
per side. The cavity sheet (the intersection of the two planes' copper) has
anti-pad holes exactly there, so a port region covering the hole ties the
mesh vertices ringing it into one equipotential node: a finite-size port.

**The port matrix.** One solve per capacitor injects 1 A at that cap and
reads ``Φ`` at *every* port. That gives a column of the N-port transfer
inductance matrix ``L[i][j] = Φ_j(I_i = 1) − Φ_ic`` for free — diagonal
entries are each cap's self spreading inductance (what the Capacitors tab
shows), off-diagonals the cap↔cap coupling through the shared cavity that a
future PDN-impedance ``Z(f) = jωL + branch RLCs`` needs and which cannot be
recovered from scalars after the fact. Solves 2…N reuse the cached mesh +
Laplacian (the assembly fingerprint covers geometry and connection seeds,
never source magnitudes), so the extra columns are cheap.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

import numpy as np
import shapely
import shapely.geometry

import pdnsolver.problem as _pp
from pdnsolver import mesh as _mesh
from pdnsolver import solver as _solver

from fypa.caploop.constants import MU0_H_PER_MM, CapLoopSettings
from fypa.caploop.identify import (
    CapInstance,
    EscapeVia,
    associate_escape_vias,
)

log = logging.getLogger(__name__)

# Minimum port radius. A port smaller than this is numerically a point
# source (the log singularity swamps the mesh), and no real via cluster is.
_MIN_PORT_RADIUS_MM = 0.15
# Successive multipliers tried when a port region catches no cavity copper.
_PORT_GROW_FACTORS = (1.0, 2.0, 4.0, 8.0)


class Tier2Error(Exception):
    """Raised when a cavity problem cannot be posed at all."""


@dataclass(frozen=True)
class Tier2Result:
    """Per-capacitor Tier-2 outcome."""
    designator: str
    spread_h: float | None       # self spreading inductance (henries)
    cavity_key: str
    target_label: str
    reason: str = ""             # non-empty when spread_h is None


@dataclass
class CavityMatrix:
    """N-port transfer-inductance matrix for one cavity, referenced to the
    IC port. ``matrix[i][j]`` is the potential at port ``j`` (henries) when
    1 A is injected at port ``i`` and drawn from the IC port. The diagonal is
    each cap's self spreading inductance; reciprocity makes it symmetric.

    Persisted with the Capacitors-tab row cache so a later PDN-impedance
    analysis can compose ``Z(f) = jωL + branch RLCs`` without re-solving.
    """
    cavity_key: str
    rail_group: str
    target_label: str
    h_cav_mm: float
    labels: tuple[str, ...]                 # cap designators, matrix order
    matrix: np.ndarray = field(repr=False)  # (n, n) henries


@dataclass(frozen=True)
class _Port:
    """A cavity port: one equipotential node spread over a via cluster.

    ``region`` is the *union* of one disc per via — never their convex hull.
    The solver's equipotential-patch machinery claims each mesh vertex for the
    first connection that covers it, so a hull over a device's spread-out pins
    would silently swallow the ports of any capacitor mounted inside that
    device's footprint, tie them to the device's node, and report their
    spreading inductance as exactly zero. A union covers only the vias.

    It stays a *single* Connection because pdnsolver maps one node to one
    representative vertex; two Connections sharing a node raise. Shapely's
    MultiPolygon satisfies everything ``_vertices_under_pad`` asks of a
    region (bounds, buffer, contains).
    """
    label: str
    points: tuple[shapely.geometry.Point, ...]   # one per attached disc
    region: shapely.geometry.base.BaseGeometry   # their union
    discs: tuple[shapely.geometry.Polygon, ...]
    geom_index: int      # which cavity polygon it sits on


def sheet_coefficient(h_cav_mm: float) -> float:
    """Sheet inverse-inductance (1/H per square) for a cavity of height
    ``h_cav_mm`` — the value fed to ``Layer.conductance`` under the duality."""
    if h_cav_mm <= 0.0:
        raise Tier2Error(f"non-physical cavity height {h_cav_mm}")
    return 1.0 / (MU0_H_PER_MM * h_cav_mm)


# --- geometry -----------------------------------------------------------------

def _as_multipolygon(geom) -> shapely.geometry.MultiPolygon:
    if geom.is_empty:
        return shapely.geometry.MultiPolygon([])
    if geom.geom_type == "Polygon":
        return shapely.geometry.MultiPolygon([geom])
    if geom.geom_type == "MultiPolygon":
        return geom
    polys = [g for g in getattr(geom, "geoms", []) if g.geom_type == "Polygon"]
    return shapely.geometry.MultiPolygon(polys)


def build_cavity_sheet(
    layer_rail: int,
    layer_return: int,
    rail_net_indices: set[int],
    return_net_indices: set[int],
    net_layer_shapes: dict[tuple[int, int], shapely.geometry.base.BaseGeometry],
    h_cav_mm: float,
    name: str,
) -> _pp.Layer:
    """The 2-D cavity domain: where rail copper on one layer faces return
    copper on the other.

    Return current only exists where *both* planes do, so the intersection is
    the honest domain — it is what makes splits and anti-pad perforations
    change the answer. Both inputs already carry their anti-pads, pullback and
    thermal reliefs (``fypa.altium_geometry._plane_sheet_polygon``), so no
    extra anti-pad punching is needed on a normal Altium plane.
    """
    def _side(layer_id: int, nets: set[int]):
        parts = [net_layer_shapes[(layer_id, ni)]
                 for ni in nets if (layer_id, ni) in net_layer_shapes]
        parts = [p for p in parts if not p.is_empty]
        if not parts:
            return None
        return shapely.union_all(parts)

    rail = _side(layer_rail, rail_net_indices)
    ret = _side(layer_return, return_net_indices)
    if rail is None or ret is None:
        raise Tier2Error("cavity layer carries no copper for its net group")
    cavity = _as_multipolygon(rail.intersection(ret))
    if cavity.is_empty:
        raise Tier2Error("rail and return copper do not overlap")
    return _pp.Layer(shape=cavity, name=name,
                     conductance=sheet_coefficient(h_cav_mm))


def _make_port(label: str, xys: list[tuple[float, float]], radius_mm: float,
               cavity: shapely.geometry.MultiPolygon) -> _Port:
    """A finite-size port over a via cluster: one disc per via.

    Each via sits in an anti-pad hole, so its disc is grown until it catches
    cavity copper — the mesh vertices ringing the hole are what the solver
    ties into the port's equipotential node. Each disc's seed point is forced
    onto copper; a seed inside a hole reads as an off-copper terminal and gets
    the whole network dropped. Discs that find no copper even when fully grown
    are dropped individually; the port survives as long as one disc lands.
    """
    radius = max(radius_mm, _MIN_PORT_RADIUS_MM)
    points: list[shapely.geometry.Point] = []
    discs: list[shapely.geometry.Polygon] = []
    geom_index = -1

    for (x, y) in xys:
        centre = shapely.geometry.Point(x, y)
        for factor in _PORT_GROW_FACTORS:
            disc = centre.buffer(radius * factor)
            on_copper = disc.intersection(cavity)
            if not on_copper.is_empty:
                break
        else:
            continue
        points.append(on_copper.representative_point())
        discs.append(disc)
        if geom_index < 0:
            for i, g in enumerate(cavity.geoms):
                if g.intersects(on_copper):
                    geom_index = i
                    break

    if not points:
        raise Tier2Error(f"port {label!r} finds no cavity copper nearby")
    return _Port(label=label, points=tuple(points),
                 region=shapely.union_all(discs), discs=tuple(discs),
                 geom_index=geom_index)


def _via_radius(vias: tuple[EscapeVia, ...], settings: CapLoopSettings) -> float:
    drills = [v.drill_mm for v in vias if v.drill_mm > 0.0]
    r = 0.5 * (max(drills) if drills else 2.0 * _MIN_PORT_RADIUS_MM)
    return r + settings.plane_antipad_clearance_mm


def _min_separation(a: list[tuple[float, float]],
                    b: list[tuple[float, float]]) -> float:
    return min((math.hypot(ax - bx, ay - by)
                for (ax, ay) in a for (bx, by) in b), default=math.inf)


def _cluster_coincident(xys_per_port: list[list[tuple[float, float]]],
                        threshold_mm: float) -> list[int]:
    """Group ports whose vias sit within ``threshold_mm`` of each other.

    Two vias closer than an anti-pad clearance share the same hole in the
    plane: they are one port, not two, and the cavity cannot resolve a
    spreading inductance between them. Boards do this deliberately — a
    capacitor mounted right on an IC's via-in-pad — so it must be reported,
    not silently turned into a zero that looks computed. Returns a
    representative index per port (union-find style).
    """
    n = len(xys_per_port)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        for j in range(i + 1, n):
            if _min_separation(xys_per_port[i], xys_per_port[j]) < threshold_mm:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[max(ri, rj)] = min(ri, rj)
    return [find(i) for i in range(n)]


def _disjoint_radii(xys_per_port: list[list[tuple[float, float]]],
                    requested: list[float]) -> list[float]:
    """Shrink each port's disc radius so no two ports' discs can touch.

    Overlapping ports share mesh vertices, and the solver claims each vertex
    for the first connection that covers it. The loser then samples a vertex
    belonging to its neighbour's node, which breaks the reciprocity the port
    matrix must obey (``L[i][j] == L[j][i]``) and makes the extracted coupling
    depend on port order. Capping each radius at 45 % of the distance to the
    nearest foreign via keeps the discs disjoint.

    Decoupling capacitors legitimately sit within a millimetre of the device
    they serve, so this fires often; it is a numerical guard, not a warning
    about the layout. Ports whose vias genuinely coincide can't be separated —
    ``_warn_on_overlapping_ports`` reports those.
    """
    out: list[float] = []
    for i, mine in enumerate(xys_per_port):
        nearest = math.inf
        for j, other in enumerate(xys_per_port):
            if i == j:
                continue
            for (ax, ay) in mine:
                for (bx, by) in other:
                    nearest = min(nearest, math.hypot(ax - bx, ay - by))
        cap = 0.45 * nearest if math.isfinite(nearest) else math.inf
        out.append(max(min(requested[i], cap), 0.02))
    return out


# --- problem construction -------------------------------------------------------

def _port_connection(sheet: _pp.Layer, port: _Port) -> _pp.Connection:
    """One equipotential Connection covering all of a port's via discs.

    pdnsolver maps each node to a single representative vertex, so a port must
    be exactly one Connection; its ``region`` (the disc union) is what ties
    every vertex under any disc into that one node.
    """
    return _pp.Connection(layer=sheet, point=port.points[0],
                          region=port.region)


def build_cavity_problem(
    sheet: _pp.Layer,
    cap_ports: list[_Port],
    ic_port: _Port,
    currents: list[float],
) -> _pp.Problem:
    """One network: every cap port plus the shared IC port, with a current
    source from the IC to each cap. Only the magnitudes differ between the
    N solves, so the mesh + Laplacian assembly fingerprint is identical and
    solves 2…N hit the cache (a "value-only re-solve")."""
    ic_conn = _port_connection(sheet, ic_port)
    conns = [ic_conn]
    elements: list[_pp.BaseLumped] = []
    for port, current in zip(cap_ports, currents):
        conn = _port_connection(sheet, port)
        conns.append(conn)
        # A zero-current source is an open circuit: the inactive cap ports
        # stay passive equipotential patches (that is what makes them
        # measurable observation points) instead of shorting the cavity.
        elements.append(_pp.CurrentSource(f=ic_conn.node_id, t=conn.node_id,
                                          current=current))
    return _pp.Problem(
        layers=[sheet],
        networks=[_pp.Network(connections=conns, elements=elements)],
        project_name="caploop-cavity",
    )


# --- extraction --------------------------------------------------------------------

def _sample_potentials(solution, points: list[shapely.geometry.Point]
                       ) -> list[float]:
    """Potential at each point, from the nearest real mesh vertex.

    Orphan vertices (present in the array but in no triangle) are pinned to 0
    by the solver to keep the system non-singular; sampling one would read a
    confident zero. Filter them out — the same discipline
    ``_compute_via_report`` applies.
    """
    from scipy.spatial import cKDTree

    xs, vals = [], []
    for ls in solution.layer_solutions:
        for m, pot in zip(ls.meshes, ls.potentials):
            xys = getattr(m, "_source_xys", None)
            if xys is None or xys.shape[0] == 0:
                continue
            in_tri = getattr(m, "_in_triangle_mask", None)
            if in_tri is not None and in_tri.size == xys.shape[0]:
                keep = np.flatnonzero(in_tri)
            else:
                keep = np.arange(xys.shape[0])
            if keep.size == 0:
                continue
            xs.append(xys[keep])
            vals.append(pot.values[keep])
    if not xs:
        raise Tier2Error("solution carries no meshed vertices")
    tree = cKDTree(np.vstack(xs))
    allvals = np.concatenate(vals)
    _, idx = tree.query([(p.x, p.y) for p in points])
    return [float(allvals[i]) for i in np.atleast_1d(idx)]


def _sample_port(solution, port: _Port) -> float:
    """The port node's potential.

    Sampled at ``points[0]`` and nowhere else. That is the point handed to the
    Connection, so the mesher seeds a vertex there and ``_vertices_under_pad``
    picks the vertex nearest it as the node's representative — sampling there
    reads the node exactly.

    The port's other discs are *not* seeded, and once radii are clamped for
    disjointness they are often smaller than a mesh cell, so they contain no
    vertex at all. Sampling one reads whatever plain copper lies nearest and
    drags the answer toward the far field; averaging across discs (a median,
    say) then halves the port potential and destroys the matrix's reciprocity.
    """
    return _sample_potentials(solution, [port.points[0]])[0]


def _warn_on_overlapping_ports(cap_ports: list[_Port],
                               ic_port: _Port) -> None:
    """Two ports whose discs overlap share mesh vertices, and the solver's
    equipotential patches claim each vertex for whichever connection reaches
    it first. Electrically they are one port; the extracted coupling between
    them is meaningless. Report rather than silently return a plausible
    number."""
    all_ports = cap_ports + [ic_port]
    for i, a in enumerate(all_ports):
        for b in all_ports[i + 1:]:
            if a.region.intersects(b.region):
                log.warning(
                    "Cavity ports %s and %s overlap — their vias are within a "
                    "via diameter of each other, so they are electrically one "
                    "port; the coupling between them is not meaningful.",
                    a.label, b.label)


def solve_cavity_matrix(
    sheet: _pp.Layer,
    cap_ports: list[_Port],
    ic_port: _Port,
    mesher_config=None,
    progress_cb=None,
    cancel_event=None,
) -> np.ndarray:
    """The (n, n) port inductance matrix, one solve per cap port.

    ``matrix[i][j] = Φ_j − Φ_ic`` with 1 A injected at cap ``i``. Ports with
    no conduction path to the IC (a split plane between them) yield a NaN row.
    """
    n = len(cap_ports)
    matrix = np.full((n, n), np.nan, dtype=np.float64)
    _warn_on_overlapping_ports(cap_ports, ic_port)

    for i, port in enumerate(cap_ports):
        if cancel_event is not None and cancel_event.is_set():
            raise Tier2Error("cancelled")
        if port.geom_index != ic_port.geom_index:
            # Disconnected cavity islands: no return path through this
            # cavity at all. Left as NaN — the row is not "zero coupling".
            log.info("Cap port %s is on a cavity island isolated from the "
                     "target port — no spreading path", port.label)
            continue
        if progress_cb is not None:
            progress_cb(f"Cavity solve {i + 1}/{n}: {port.label}")
        currents = [0.0] * n
        currents[i] = 1.0
        prob = build_cavity_problem(sheet, cap_ports, ic_port, currents)
        solution = _solver.solve(prob, mesher_config)
        phi_ic = _sample_port(solution, ic_port)
        matrix[i, :] = [_sample_port(solution, p) - phi_ic
                        for p in cap_ports]
    return matrix


# --- top-level entry ------------------------------------------------------------------

def _cavity_key(cap: CapInstance) -> str:
    cav = cap.cavity
    return (f"{cav.name_rail}|{cav.name_return}|{cap.rail_group}"
            f"|{cap.target_label}")


def run_tier2(
    extracted,
    caps: list[CapInstance],
    net_layer_shapes: dict[tuple[int, int], shapely.geometry.base.BaseGeometry],
    rail_to_members: dict[str, list[str]],
    settings: CapLoopSettings | None = None,
    mesher_config=None,
    progress_cb=None,
    cancel_event=None,
) -> tuple[dict[str, Tier2Result], list[CavityMatrix]]:
    """Solve the plane-pair spreading inductance for every included capacitor.

    Caps are grouped by (cavity layer pair, rail group, target device): each
    group is one cavity domain with one shared reference port, hence one
    N-port matrix. Returns per-cap results keyed by designator, plus the
    matrices for downstream impedance analysis.
    """
    settings = settings or CapLoopSettings()
    if mesher_config is None:
        mesher_config = _mesh.Mesher.Config()

    enabled = extracted.enabled_copper_layer_ids()
    net_names = [n.name for n in extracted.nets]
    name_to_index = {n: i for i, n in enumerate(net_names)}

    results: dict[str, Tier2Result] = {}
    matrices: list[CavityMatrix] = []

    groups: dict[str, list[CapInstance]] = {}
    for cap in caps:
        if not cap.included:
            continue
        if cap.cavity is None:
            results[cap.designator] = Tier2Result(
                cap.designator, None, "", "", "no reference cavity")
            continue
        if not cap.target_label or not cap.target_pins:
            results[cap.designator] = Tier2Result(
                cap.designator, None, "", "", "no target device")
            continue
        if not cap.vias_rail or not cap.vias_return:
            results[cap.designator] = Tier2Result(
                cap.designator, None, "", cap.target_label,
                "no escape via")
            continue
        groups.setdefault(_cavity_key(cap), []).append(cap)

    for key, group in groups.items():
        first = group[0]
        cav = first.cavity
        rail_members = set(rail_to_members.get(first.rail_group,
                                               [first.rail_group]))
        rail_idx = {name_to_index[m] for m in rail_members
                    if m in name_to_index}
        return_idx = {name_to_index[c.return_net] for c in group
                      if c.return_net in name_to_index}
        try:
            sheet = build_cavity_sheet(
                cav.layer_rail, cav.layer_return, rail_idx, return_idx,
                net_layer_shapes, cav.h_cav_mm,
                name=f"cavity {cav.name_rail}/{cav.name_return}")

            # The IC port: escape vias of the target's pins, on the same net
            # groups — reuse the cap-side association so both ends of the
            # loop are modelled the same way.
            target_pads = _pads_at(extracted, first.target_pins)
            ic_layer = int(first.target_pins[0].get("layer_id") or 1)
            ic_vias = associate_escape_vias(
                target_pads, extracted, rail_idx, enabled, settings,
                ic_layer)
            if ic_vias:
                ic_xys = [(v.x_mm, v.y_mm) for v in ic_vias]
                ic_radius = _via_radius(ic_vias, settings)
            else:
                # Through-hole-less BGA on the cavity's own layer: fall back
                # to the pin locations themselves.
                ic_xys = [(p["x_mm"], p["y_mm"]) for p in first.target_pins]
                ic_radius = _MIN_PORT_RADIUS_MM \
                    + settings.plane_antipad_clearance_mm

            cap_xys = [
                [(v.x_mm, v.y_mm) for v in (c.vias_rail + c.vias_return)]
                for c in group
            ]
            # Ports whose vias coincide (within an anti-pad clearance) are one
            # port. The IC sits last, so a cap merged into its cluster shares
            # the target's own via and has no plane spreading to it at all.
            reps = _cluster_coincident(
                cap_xys + [ic_xys], settings.plane_antipad_clearance_mm)
            ic_rep = reps[-1]

            active: list[CapInstance] = []
            active_xys: list[list[tuple[float, float]]] = []
            merged_into: dict[str, int] = {}
            for i, c in enumerate(group):
                if reps[i] == ic_rep:
                    results[c.designator] = Tier2Result(
                        c.designator, 0.0, key, c.target_label or "",
                        "capacitor shares the target's via — "
                        "no plane spreading between them")
                elif reps[i] == i:
                    active.append(c)
                    active_xys.append(cap_xys[i])
                else:
                    merged_into[c.designator] = reps[i]
            if not active:
                continue

            # Size every port in the cavity together so their discs stay
            # disjoint — see _disjoint_radii.
            requested = [
                _via_radius(c.vias_rail + c.vias_return, settings)
                for c in active
            ] + [ic_radius]
            radii = _disjoint_radii(active_xys + [ic_xys], requested)

            cap_ports = [
                _make_port(c.designator, xys, r, sheet.shape)
                for c, xys, r in zip(active, active_xys, radii)
            ]
            ic_port = _make_port(first.target_label, ic_xys, radii[-1],
                                 sheet.shape)
            matrix = solve_cavity_matrix(
                sheet, cap_ports, ic_port, mesher_config,
                progress_cb, cancel_event)
        except Tier2Error as e:
            if str(e) == "cancelled":
                raise
            log.warning("Tier-2 cavity %s skipped: %s", key, e)
            for c in group:
                results[c.designator] = Tier2Result(
                    c.designator, None, key, c.target_label or "", str(e))
            continue

        # The matrix carries only the independent ports, so it stays
        # reciprocal and invertible for a downstream impedance solve.
        labels = tuple(c.designator for c in active)
        matrices.append(CavityMatrix(
            cavity_key=key, rail_group=first.rail_group,
            target_label=first.target_label or "",
            h_cav_mm=cav.h_cav_mm, labels=labels, matrix=matrix))
        for i, c in enumerate(active):
            self_l = matrix[i, i]
            if math.isnan(self_l):
                results[c.designator] = Tier2Result(
                    c.designator, None, key, c.target_label or "",
                    "no cavity path to target (split plane)")
            else:
                results[c.designator] = Tier2Result(
                    c.designator, float(self_l), key,
                    c.target_label or "")
        # Capacitors merged onto another capacitor's via inherit its result:
        # they are the same port, so their spreading term is identical.
        by_index = {i: c for i, c in enumerate(group)}
        for designator, rep_index in merged_into.items():
            rep = by_index[rep_index]
            rep_result = results.get(rep.designator)
            if rep_result is None or rep_result.spread_h is None:
                continue
            results[designator] = Tier2Result(
                designator, rep_result.spread_h, key,
                rep_result.target_label,
                f"shares a via with {rep.designator} — same cavity port")
    return results, matrices


def _pads_at(extracted, pins: tuple[dict, ...]) -> list:
    """Extracted pads matching the metadata pin records — matched by position,
    since pin dicts carry x/y/layer rather than a pad index."""
    if not pins:
        return []
    wanted = {(round(p["x_mm"], 4), round(p["y_mm"], 4)) for p in pins}
    return [pad for pad in extracted.pads
            if (round(pad.center.x, 4), round(pad.center.y, 4)) in wanted]
