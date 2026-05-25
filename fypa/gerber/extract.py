"""Gerber + Excellon → :class:`~fypa.altium.extract.ExtractedProject` adapter.

Produces an :class:`ExtractedProject` from a set of RS-274X Gerber files and
NC-Drill (Excellon) files, populating only the fields needed downstream:

* :attr:`shape_based_regions` — one per connected copper component per layer.
  Every Gerber primitive (flashed apertures, drawn tracks, arcs, regions)
  rasterises to Shapely; the polarity-aware union per layer is split into
  connected components, and each component is encoded as a single
  :class:`RawShapeBasedRegion` with straight outline + holes. Tracks / pads /
  fills / regions / arcs all stay empty tuples — the downstream geometry
  builder accepts a project where copper lives entirely in
  ``shape_based_regions``.

* :attr:`vias` — one per Excellon drill hit. ``hole_diameter_mm`` is exact;
  ``diameter_mm`` is ``hole_diameter_mm + 0.3`` (a coarse annular-ring
  heuristic, since Gerber/Excellon doesn't carry pad-vs-drill annulus info).

* :attr:`stackup` — the user-supplied :class:`RawStackupLayer` list, chained
  Top → Bottom via ``next_layer_id`` so
  :meth:`ExtractedProject.enabled_copper_layer_ids` works.

* :attr:`board_outline` — the largest exterior ring of the optional outline
  Gerber, or (failing that) the bounding box of unioned copper.

Everything else (nets, pcb_components, sch_components, pads, texts, …) is
empty. The user adds source / sink directives via editor mode
(:mod:`fypa.editor_directives`); the
:class:`~fypa.project_file.CopperName` flow names individual copper islands.

Layer IDs follow the Altium convention used everywhere else in FYPA:
``1 = Top``, ``32 = Bottom``, ``2..31 = Inner 1..30``, ``33 / 34 = silk``.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import shapely
import shapely.affinity
import shapely.geometry
import shapely.ops

from fypa.altium.extract import (
    NO_NET,
    NO_POLYGON,
    ExtractedProject,
    Pt2D,
    RawRegionVertex,
    RawShapeBasedRegion,
    RawStackupLayer,
    RawVia,
)

log = logging.getLogger(__name__)


# Special "layer id" sentinels for the file-classifier UI. Negative + >32 are
# reserved by the existing pipeline, so we use small negatives here.
LAYER_ID_OUTLINE: int = -10
LAYER_ID_DRILL: int = -11
LAYER_ID_IGNORE: int = -12
LAYER_ID_SILK_TOP: int = 33
LAYER_ID_SILK_BOT: int = 34

LAYER_ID_TOP: int = 1
LAYER_ID_BOTTOM: int = 32
MAX_INNER_LAYERS: int = 30          # inner ids run 2..31

# Heuristic annular-ring extension applied to each drill hit when we have no
# pad data. Pads on a real board are typically drill + 0.3-0.5 mm; 0.3 mm is
# the smaller end of the IPC-2221 minimum for via barrels and keeps the
# pad-area FEM coupling region small enough that it doesn't over-bridge
# adjacent copper.
VIA_ANNULAR_RING_HEURISTIC_MM: float = 0.30

# Discretisation tolerance for arcs / circles when rasterising to Shapely.
# Matches the value used in altium_geometry.ARC_CHORD_TOLERANCE_MM so the
# Gerber and Altium paths produce comparable polygon-edge densities.
ARC_CHORD_TOLERANCE_MM: float = 0.025


# --- filename classification --------------------------------------------------

# Regex list — first match wins. Each entry is (compiled_re, layer_id) or
# (compiled_re, layer_id, inner_group) where ``inner_group`` is the regex
# group holding the inner-layer number (1-based; mapped to id = 1 + n).
def _re(p: str) -> re.Pattern[str]:
    return re.compile(p, re.IGNORECASE)


_CLASSIFIER_RULES: list[tuple[re.Pattern[str], int, int | None]] = [
    # Top copper — Altium .GTL / .CMP, KiCad F.Cu / F_Cu, generic top.cu
    (_re(r"\.gtl$"), LAYER_ID_TOP, None),
    (_re(r"\.cmp$"), LAYER_ID_TOP, None),
    (_re(r"[._-]F[._-]?Cu[._-]?(?:gbr|ger|gtl)?$"), LAYER_ID_TOP, None),
    (_re(r"(?:^|[._-])top[._-]?(?:copper|layer|cu|signal)\b"), LAYER_ID_TOP, None),
    (_re(r"_copper_signal_top"), LAYER_ID_TOP, None),
    # Bottom copper
    (_re(r"\.gbl$"), LAYER_ID_BOTTOM, None),
    (_re(r"\.sol$"), LAYER_ID_BOTTOM, None),
    (_re(r"[._-]B[._-]?Cu[._-]?(?:gbr|ger|gbl)?$"), LAYER_ID_BOTTOM, None),
    (_re(r"(?:^|[._-])bot(?:tom)?[._-]?(?:copper|layer|cu|signal)\b"), LAYER_ID_BOTTOM, None),
    (_re(r"_copper_signal_bot"), LAYER_ID_BOTTOM, None),
    # Outline — Altium .GKO / .GM1, KiCad Edge.Cuts
    (_re(r"\.gko$"), LAYER_ID_OUTLINE, None),
    (_re(r"\.gm1$"), LAYER_ID_OUTLINE, None),
    (_re(r"edge[._-]?cuts?"), LAYER_ID_OUTLINE, None),
    (_re(r"(?:^|[._-])outline\b"), LAYER_ID_OUTLINE, None),
    (_re(r"(?:^|[._-])board[._-]?outline\b"), LAYER_ID_OUTLINE, None),
    (_re(r"keep[._-]?out"), LAYER_ID_OUTLINE, None),
    # Drill — Excellon
    (_re(r"\.drl$"), LAYER_ID_DRILL, None),
    (_re(r"\.xln$"), LAYER_ID_DRILL, None),
    (_re(r"\.tap$"), LAYER_ID_DRILL, None),
    (_re(r"\.nc$"), LAYER_ID_DRILL, None),
    # Silk
    (_re(r"\.gto$"), LAYER_ID_SILK_TOP, None),
    (_re(r"\.gbo$"), LAYER_ID_SILK_BOT, None),
    (_re(r"[._-]F[._-]?SilkS"), LAYER_ID_SILK_TOP, None),
    (_re(r"[._-]B[._-]?SilkS"), LAYER_ID_SILK_BOT, None),
    # Inner copper — Altium .G1 / .G2 ..., KiCad In1.Cu / In2.Cu,
    # generic "innerN" / "L<N>".
    (_re(r"\.g(\d+)$"), 0, 1),                  # Altium inner; group 1 = N
    (_re(r"In(\d+)[._-]?Cu"), 0, 1),
    (_re(r"(?:^|[._-])inner[._-]?(\d+)\b"), 0, 1),
    (_re(r"(?:^|[._-])L(\d+)\b"), 0, 1),
    (_re(r"_copper_signal_(\d+)\b"), 0, 1),
]


def classify_file(path: Path) -> int:
    """Best-guess layer id for ``path`` based on its filename.

    Returns one of :data:`LAYER_ID_TOP` / :data:`LAYER_ID_BOTTOM` / a
    1-based inner id (2..31) / :data:`LAYER_ID_SILK_TOP` /
    :data:`LAYER_ID_SILK_BOT` / :data:`LAYER_ID_OUTLINE` /
    :data:`LAYER_ID_DRILL` / :data:`LAYER_ID_IGNORE`.

    The user always sees the auto-classification in the layer-mapping
    dialog and can reassign anything that came out wrong.
    """
    name = path.name
    for pat, layer_id, inner_group in _CLASSIFIER_RULES:
        m = pat.search(name)
        if not m:
            continue
        if inner_group is not None:
            try:
                n = int(m.group(inner_group))
            except (IndexError, ValueError):
                continue
            if 1 <= n <= MAX_INNER_LAYERS:
                return 1 + n        # Inner1 -> id 2, Inner2 -> id 3, ...
            continue                # out-of-range inner number; try next pattern
        return layer_id
    return LAYER_ID_IGNORE


@dataclass(frozen=True)
class ClassifiedFiles:
    """Result of running every picked path through :func:`classify_file`."""
    by_layer_id: dict[int, list[Path]]   # layer_id -> picked file(s)
    drill_files: list[Path]
    outline_files: list[Path]
    ignored: list[Path]


def classify_files(paths: Iterable[Path]) -> ClassifiedFiles:
    by_layer: dict[int, list[Path]] = {}
    drills: list[Path] = []
    outlines: list[Path] = []
    ignored: list[Path] = []
    for p in paths:
        lid = classify_file(p)
        if lid == LAYER_ID_DRILL:
            drills.append(p)
        elif lid == LAYER_ID_OUTLINE:
            outlines.append(p)
        elif lid == LAYER_ID_IGNORE:
            ignored.append(p)
        else:
            by_layer.setdefault(lid, []).append(p)
    return ClassifiedFiles(
        by_layer_id=by_layer,
        drill_files=drills,
        outline_files=outlines,
        ignored=ignored,
    )


# --- gerbonara primitive → Shapely -------------------------------------------

def _circle_to_polygon(x: float, y: float, r: float) -> shapely.geometry.Polygon:
    # Use shapely's ``buffer`` on a point — gives a uniform-edge circle with
    # quad_segs controlling vertex density. We size quad_segs from the radius
    # so very large copper flashes still have smooth edges.
    if r <= 0:
        return shapely.geometry.Polygon()
    n = max(8, int(2 * 3.14159265 * r / max(ARC_CHORD_TOLERANCE_MM, 1e-6)))
    quad = max(2, min(n // 4, 64))
    return shapely.geometry.Point(x, y).buffer(r, quad_segs=quad)


def _rectangle_to_polygon(x: float, y: float, w: float, h: float,
                          rotation: float = 0.0) -> shapely.geometry.Polygon:
    # gerbonara Rectangle's (x,y) is the CENTRE; rotation is degrees about it.
    poly = shapely.geometry.box(x - w / 2.0, y - h / 2.0,
                                x + w / 2.0, y + h / 2.0)
    if rotation:
        poly = shapely.affinity.rotate(poly, rotation, origin=(x, y),
                                       use_radians=False)
    return poly


def _line_to_polygon(x1: float, y1: float, x2: float, y2: float,
                     width: float) -> shapely.geometry.Polygon:
    # Round-capped stroke — matches Gerber's round-aperture stroking when
    # the aperture is a circle, which is by far the common case. Square /
    # rectangular apertures along a draw would need a flat-cap buffer, but
    # gerbonara collapses those into a sequence of Rectangle primitives
    # anyway when rasterising, so the round cap here is the right choice
    # for the Line primitives we actually get.
    if width <= 0:
        return shapely.geometry.Polygon()
    return shapely.geometry.LineString([(x1, y1), (x2, y2)]).buffer(
        width / 2.0, cap_style="round", join_style="round",
    )


def _arc_to_polygon(x1: float, y1: float, x2: float, y2: float,
                    cx: float, cy: float, clockwise: bool,
                    width: float) -> shapely.geometry.Polygon:
    """Discretise the arc to a polyline and buffer to a round-capped stroke.

    Gerber encodes a **full circle** as an arc whose start point equals
    its end point — both atan2 angles are then identical and a naive
    sweep computation gives 0, collapsing the LineString to a single
    point and rendering as a tiny disc the size of the pen tip instead
    of the intended stroked circle. We detect that case (p1 == p2 to
    within a small tolerance) and force a full ±2π sweep. The result of
    stroking a full circle with a pen wider than its radius (≤ r) is a
    solid disc (donut hole closes); a thinner pen produces an annulus.
    Both fall out of the LineString.buffer call below.
    """
    import math
    if width <= 0:
        return shapely.geometry.Polygon()
    r = math.hypot(x1 - cx, y1 - cy)
    if r <= 0:
        return shapely.geometry.Polygon()
    # Full-circle detection: Gerber draws closed circles as arcs with
    # p1 == p2. atan2(0, 0) would be undefined and the sweep would be
    # 0, so handle separately.
    epsilon = max(1e-6, r * 1e-9)
    is_full_circle = (abs(x2 - x1) < epsilon and abs(y2 - y1) < epsilon)
    a0 = math.atan2(y1 - cy, x1 - cx)
    if is_full_circle:
        a1 = a0 - 2.0 * math.pi if clockwise else a0 + 2.0 * math.pi
    else:
        a1 = math.atan2(y2 - cy, x2 - cx)
        # Sweep direction — counter-clockwise is standard math (positive
        # delta); Gerber uses clockwise=True for negative-direction arcs.
        if clockwise:
            if a1 > a0:
                a1 -= 2 * math.pi
        else:
            if a1 < a0:
                a1 += 2 * math.pi
    # Number of steps to keep chord error under ARC_CHORD_TOLERANCE_MM.
    # err ≈ r * (1 - cos(dθ/2)); solve for dθ: dθ ≈ 2 * acos(1 - err/r)
    err_ratio = max(min(ARC_CHORD_TOLERANCE_MM / r, 0.99), 1e-6)
    dtheta_max = 2.0 * math.acos(1.0 - err_ratio)
    sweep = abs(a1 - a0)
    n = max(2, int(math.ceil(sweep / dtheta_max)) + 1)
    pts = []
    for i in range(n):
        t = a0 + (a1 - a0) * (i / (n - 1))
        pts.append((cx + r * math.cos(t), cy + r * math.sin(t)))
    return shapely.geometry.LineString(pts).buffer(
        width / 2.0, cap_style="round", join_style="round",
    )


def _arcpoly_to_polygon(outline, arc_centers) -> shapely.geometry.Polygon:
    """Region (filled polygon) — straight + arc segments.

    ``outline`` is a list of ``(x, y)`` vertices defining the closed ring.
    ``arc_centers`` parallels it: each entry is either ``None`` (straight
    segment to next vertex) or ``(cx, cy)`` for an arc whose centre is at
    those coordinates, sweeping the short way (Gerber spec is sign-based
    in the file but gerbonara normalises to ``None`` / centre coordinates).
    Today we discretise arc segments to chords with the same tolerance as
    line arcs above.
    """
    import math
    if len(outline) < 3:
        return shapely.geometry.Polygon()
    pts: list[tuple[float, float]] = []
    n = len(outline)
    for i in range(n):
        x0, y0 = outline[i]
        x1, y1 = outline[(i + 1) % n]
        pts.append((x0, y0))
        ac = arc_centers[i] if arc_centers and i < len(arc_centers) else None
        if ac is None or ac[0] is None or ac[1] is None:
            continue
        cx, cy = ac
        r = math.hypot(x0 - cx, y0 - cy)
        if r <= 0:
            continue
        a0 = math.atan2(y0 - cy, x0 - cx)
        a1 = math.atan2(y1 - cy, x1 - cx)
        # Gerber region arc: short way around (sweep < pi). Normalise so the
        # smaller absolute sweep is taken.
        delta = a1 - a0
        while delta > math.pi:
            delta -= 2 * math.pi
        while delta < -math.pi:
            delta += 2 * math.pi
        err_ratio = max(min(ARC_CHORD_TOLERANCE_MM / r, 0.99), 1e-6)
        dtheta_max = 2.0 * math.acos(1.0 - err_ratio)
        steps = max(2, int(math.ceil(abs(delta) / dtheta_max)))
        for k in range(1, steps):
            t = a0 + delta * (k / steps)
            pts.append((cx + r * math.cos(t), cy + r * math.sin(t)))
    # Close ring implicitly; let shapely figure out validity.
    poly = shapely.geometry.Polygon(pts)
    if not poly.is_valid:
        poly = shapely.make_valid(poly)
        # ``make_valid`` may return a MultiPolygon or GeometryCollection;
        # caller will handle that by accepting any (multi)polygon.
    return poly


def _primitive_to_polygon(prim):
    """Dispatch one gerbonara graphic_primitive → Shapely polygon."""
    import gerbonara.graphic_primitives as gp
    if isinstance(prim, gp.Circle):
        return _circle_to_polygon(prim.x, prim.y, prim.r)
    if isinstance(prim, gp.Rectangle):
        return _rectangle_to_polygon(prim.x, prim.y, prim.w, prim.h,
                                     getattr(prim, "rotation", 0.0))
    if isinstance(prim, gp.Line):
        return _line_to_polygon(prim.x1, prim.y1, prim.x2, prim.y2, prim.width)
    if isinstance(prim, gp.Arc):
        return _arc_to_polygon(prim.x1, prim.y1, prim.x2, prim.y2,
                               prim.cx, prim.cy, prim.clockwise, prim.width)
    if isinstance(prim, gp.ArcPoly):
        return _arcpoly_to_polygon(prim.outline, prim.arc_centers)
    log.debug("Unhandled gerbonara primitive type: %s", type(prim).__name__)
    return shapely.geometry.Polygon()


def render_gerber_to_shapely(gerber_path: Path) -> shapely.geometry.base.BaseGeometry:
    """Open ``gerber_path`` with gerbonara and rasterise every object into one
    Shapely (Multi)Polygon. Polarity-aware: dark primitives are unioned,
    clear primitives are subtracted in stream order (mirrors how a Gerber
    photoplotter would resolve a layer).

    Returns ``Polygon`` / ``MultiPolygon`` / empty ``GeometryCollection``.
    """
    from gerbonara import GerberFile
    from gerbonara.utils import MM

    gf = GerberFile.open(str(gerber_path))
    # Stream the objects in file order, batching consecutive same-polarity
    # primitives into one unary_union per batch (much faster than unioning
    # one-by-one). When polarity flips, apply the accumulated dark batch
    # to the running shape with ``union``, or the clear batch with
    # ``difference``.
    accumulated: shapely.geometry.base.BaseGeometry = shapely.geometry.Polygon()
    batch: list[shapely.geometry.base.BaseGeometry] = []
    batch_dark = True
    for obj in gf.objects:
        for prim in obj.to_primitives(MM):
            poly = _primitive_to_polygon(prim)
            if poly.is_empty:
                continue
            is_dark = bool(prim.polarity_dark)
            if is_dark != batch_dark and batch:
                merged = shapely.ops.unary_union(batch)
                accumulated = (accumulated.union(merged)
                               if batch_dark
                               else accumulated.difference(merged))
                batch = []
            batch_dark = is_dark
            batch.append(poly)
    if batch:
        merged = shapely.ops.unary_union(batch)
        accumulated = (accumulated.union(merged)
                       if batch_dark
                       else accumulated.difference(merged))
    if not accumulated.is_valid:
        accumulated = shapely.make_valid(accumulated)
    return accumulated


def render_outline_to_shapely(gerber_path: Path
                              ) -> shapely.geometry.base.BaseGeometry:
    """Outline / mech layers are typically strokes, not flooded copper. We
    render them the same way (round-stroked Lines / Arcs), then take the
    convex hull as a fallback if the strokes form an open curve."""
    # The same per-primitive rasteriser already handles stroked lines/arcs
    # as round-capped buffers; the resulting MultiPolygon outlines the
    # board boundary. The caller takes the largest ring's exterior.
    return render_gerber_to_shapely(gerber_path)


# --- copper layer → RawShapeBasedRegion list ---------------------------------

def _polygons_in(geom: shapely.geometry.base.BaseGeometry
                 ) -> list[shapely.geometry.Polygon]:
    """Flatten a (Multi)Polygon / GeometryCollection to a list of Polygons."""
    if geom.is_empty:
        return []
    if isinstance(geom, shapely.geometry.Polygon):
        return [geom]
    if isinstance(geom, shapely.geometry.MultiPolygon):
        return list(geom.geoms)
    if isinstance(geom, shapely.geometry.GeometryCollection):
        out: list[shapely.geometry.Polygon] = []
        for g in geom.geoms:
            out.extend(_polygons_in(g))
        return out
    return []


def _polygon_to_shape_based_region(
    poly: shapely.geometry.Polygon, layer_id: int,
) -> RawShapeBasedRegion:
    """One connected copper polygon → one RawShapeBasedRegion record.

    Vertices come back as straight ``RawRegionVertex`` (no arc info) since
    Shapely flattens everything to polylines after the rasteriser pass.
    """
    outline = tuple(
        RawRegionVertex(pos=Pt2D(float(x), float(y)))
        for x, y in poly.exterior.coords[:-1]   # drop the closing dup
    )
    holes: list[tuple[Pt2D, ...]] = []
    for ring in poly.interiors:
        holes.append(tuple(Pt2D(float(x), float(y)) for x, y in ring.coords[:-1]))
    return RawShapeBasedRegion(
        outline=outline,
        holes=tuple(holes),
        layer_id=layer_id,
        net_index=NO_NET,
        kind=0,
        is_polygon_outline=False,
        is_keepout=False,
        is_board_cutout=False,
        polygon_index=NO_POLYGON,
    )


# --- drill (Excellon) → RawVia ----------------------------------------------

def _excellon_to_vias(drill_paths: list[Path],
                      top_layer_id: int, bottom_layer_id: int
                      ) -> tuple[tuple[RawVia, ...], list[str]]:
    """Parse every drill file → one ``RawVia`` per plated drill hit.

    Returns ``(vias, warnings)``; warnings describe files we couldn't open.
    """
    from gerbonara import ExcellonFile

    vias: list[RawVia] = []
    warnings: list[str] = []
    for path in drill_paths:
        try:
            ef = ExcellonFile.open(str(path))
        except Exception as e:  # SyntaxError, OSError, …
            warnings.append(
                f"Couldn't parse drill file {path.name} ({type(e).__name__}: "
                f"{e}); skipping."
            )
            continue
        # gerbonara's ExcellonFile.objects is a flat list of drill hits.
        for d in ef.objects:
            try:
                # gerbonara reports each ExcellonDrill in its native unit; we
                # need mm. ``unit`` is "mm" or "inch"; convert ``diameter``
                # accordingly via the .converted() method on the wrapper.
                # For simplicity we read the resolved fields directly.
                x = float(d.x)
                y = float(d.y)
                if str(d.unit) == "inch":
                    x *= 25.4
                    y *= 25.4
                tool = d.tool        # ExcellonTool
                diam_mm = float(tool.diameter)
                if str(tool.unit) == "inch":
                    diam_mm *= 25.4
            except Exception as e:
                warnings.append(
                    f"Skipping malformed drill record in {path.name} "
                    f"({type(e).__name__}: {e})."
                )
                continue
            if diam_mm <= 0:
                continue
            # Skip explicitly non-plated drills (mechanical mounting holes,
            # etc.) — they have no electrical role.
            if hasattr(tool, "plated") and tool.plated is False:
                continue
            outer_mm = diam_mm + VIA_ANNULAR_RING_HEURISTIC_MM
            vias.append(RawVia(
                center=Pt2D(x, y),
                diameter_mm=outer_mm,
                hole_diameter_mm=diam_mm,
                layer_start=top_layer_id,
                layer_end=bottom_layer_id,
                net_index=NO_NET,
            ))
    return tuple(vias), warnings


# --- public entry point ------------------------------------------------------

@dataclass(frozen=True)
class GerberStackupLayer:
    """One row in the user's stackup dialog.

    A thin transport dataclass; converted to :class:`RawStackupLayer` inside
    :func:`extract_gerber_project`.
    """
    layer_id: int                       # 1 = Top, 32 = Bottom, 2..31 = inner
    name: str
    copper_thickness_mm: float
    # Thickness of the dielectric SITTING BELOW this copper layer (between
    # this layer and the next copper layer down the stack). 0.0 for Bottom.
    dielectric_thickness_mm: float


def _build_stackup(layers: list[GerberStackupLayer],
                   ordered_layer_ids: list[int]
                   ) -> tuple[RawStackupLayer, ...]:
    """Convert the dialog's stackup spec to ``RawStackupLayer`` records,
    chained via ``next_layer_id`` in the order the user specified.

    ``ordered_layer_ids`` is the active copper stack Top→Bottom (extracted
    from the layer assignments). ``layers`` carries the per-id thickness
    info — must have one entry per id in ``ordered_layer_ids``.
    """
    by_id = {L.layer_id: L for L in layers}
    out: list[RawStackupLayer] = []
    for i, lid in enumerate(ordered_layer_ids):
        L = by_id.get(lid)
        if L is None:
            # Shouldn't happen if the dialog populated correctly, but
            # default to 1 oz copper + 0.2 mm dielectric so we still
            # produce a usable record.
            L = GerberStackupLayer(
                layer_id=lid, name=f"L{i + 1}",
                copper_thickness_mm=0.035,
                dielectric_thickness_mm=0.2,
            )
        next_id = ordered_layer_ids[i + 1] if i + 1 < len(ordered_layer_ids) else 0
        out.append(RawStackupLayer(
            layer_id=L.layer_id,
            name=L.name,
            copper_thickness_mm=L.copper_thickness_mm,
            dielectric_thickness_mm=(
                L.dielectric_thickness_mm
                if next_id != 0
                else 0.0
            ),
            next_layer_id=next_id,
            is_plane=False,
            plane_net_name=None,
            mech_enabled=True,
        ))
    return tuple(out)


def _outline_points(outline_geom: shapely.geometry.base.BaseGeometry
                    ) -> tuple[Pt2D, ...]:
    """Take the largest polygon's exterior ring as the board outline."""
    polys = _polygons_in(outline_geom)
    if not polys:
        return ()
    biggest = max(polys, key=lambda p: p.area)
    return tuple(Pt2D(float(x), float(y))
                 for x, y in biggest.exterior.coords[:-1])


def extract_gerber_project(
    *,
    copper_files: dict[int, Path],
    drill_files: list[Path],
    outline_file: Path | None,
    stackup: list[GerberStackupLayer],
    pseudo_prjpcb_path: Path,
) -> tuple[ExtractedProject, list[str]]:
    """Build an :class:`ExtractedProject` from a set of Gerber + Excellon files.

    Parameters
    ----------
    copper_files
        ``{layer_id: gerber_path}`` for each copper layer to import. ``layer_id``
        follows Altium convention (1 = Top, 32 = Bottom, 2..31 = inner).
        Multiple files per layer are not supported in this version — pass the
        already-merged copper if you have positive + clearance plots.
    drill_files
        List of NC-Drill (Excellon) paths. May be empty; missing drill data
        produces a warning but otherwise leaves the board solvable for
        single-layer rails or via free editor markers.
    outline_file
        Optional path to a board-outline / mechanical Gerber. ``None`` falls
        back to the bounding box of the unioned copper.
    stackup
        One :class:`GerberStackupLayer` per copper layer in ``copper_files``.
    pseudo_prjpcb_path
        Synthetic Altium-style project path used as the "project identity"
        key downstream (cache dir naming, metadata round-trip). Typically the
        ``.fypa`` file's path or a placeholder next to the gerbers; it does
        not need to exist on disk.

    Returns
    -------
    ``(extracted_project, warnings)`` — ``warnings`` is a list of
    human-readable strings to surface in the viewer's Messages tab.
    """
    warnings: list[str] = []

    # 1. Rasterise each copper layer → list of connected-component
    #    RawShapeBasedRegions.
    sbr_records: list[RawShapeBasedRegion] = []
    all_copper: list[shapely.geometry.base.BaseGeometry] = []
    for layer_id, path in copper_files.items():
        log.info("Gerber: rendering layer %d (%s)", layer_id, path.name)
        try:
            geom = render_gerber_to_shapely(path)
        except Exception as e:
            warnings.append(
                f"Couldn't render Gerber {path.name} for layer {layer_id} "
                f"({type(e).__name__}: {e}); skipping this layer."
            )
            continue
        for poly in _polygons_in(geom):
            if poly.area <= 0:
                continue
            sbr_records.append(_polygon_to_shape_based_region(poly, layer_id))
        if not geom.is_empty:
            all_copper.append(geom)

    # 2. Drill → Vias. Layer span = top..bottom of the stackup the user
    #    actually provided; if they only imported one layer we use the same
    #    id for both ends (the loader will treat it as a non-bridging via,
    #    which is the right behaviour for that case).
    ordered_ids = sorted(copper_files.keys(), key=lambda i: (i == LAYER_ID_BOTTOM, i))
    # Sort so Top=1 first, inner ids next ascending, Bottom=32 last.
    # The composite key puts Bottom last; the inner ids sort naturally.
    if ordered_ids:
        top_id = ordered_ids[0]
        bot_id = ordered_ids[-1]
    else:
        top_id = LAYER_ID_TOP
        bot_id = LAYER_ID_BOTTOM
    if drill_files:
        vias, drill_warnings = _excellon_to_vias(drill_files, top_id, bot_id)
        warnings.extend(drill_warnings)
    else:
        vias = ()
        warnings.append(
            "No drill (Excellon) file provided. Vias / through-hole pads "
            "won't be reconstructed; multi-layer rails will need editor "
            "directives or copper names to bridge layers."
        )

    # 3. Board outline. Prefer an explicit outline file; fall back to
    #    bounding box of unioned copper.
    board_outline: tuple[Pt2D, ...] = ()
    if outline_file is not None:
        try:
            outline_geom = render_outline_to_shapely(outline_file)
            board_outline = _outline_points(outline_geom)
        except Exception as e:
            warnings.append(
                f"Couldn't render outline {outline_file.name} "
                f"({type(e).__name__}: {e}); using copper bounding box."
            )
    if not board_outline and all_copper:
        union = shapely.ops.unary_union(all_copper)
        if not union.is_empty:
            minx, miny, maxx, maxy = union.bounds
            board_outline = (
                Pt2D(minx, miny), Pt2D(maxx, miny),
                Pt2D(maxx, maxy), Pt2D(minx, maxy),
                Pt2D(minx, miny),
            )

    # 4. Stackup, chained Top → Bottom in the order the user imported.
    if not stackup:
        warnings.append(
            "Stackup is empty; downstream conductance will be zero. "
            "This board won't solve."
        )
        stackup_records: tuple[RawStackupLayer, ...] = ()
    else:
        stackup_records = _build_stackup(stackup, ordered_ids)

    # 5. Assemble. Nets / pads / pcb_components / sch_components / tracks /
    #    arcs / fills / regions all stay empty — the geometry builder
    #    handles a project where copper lives entirely in
    #    ``shape_based_regions``.
    project = ExtractedProject(
        prjpcb_path=pseudo_prjpcb_path,
        pcbdoc_path=pseudo_prjpcb_path,
        tracks=(),
        arcs=(),
        vias=vias,
        pads=(),
        regions=(),
        shape_based_regions=tuple(sbr_records),
        fills=(),
        pcb_components=(),
        nets=(),
        stackup=stackup_records,
        sch_components=(),
        board_origin_mm=Pt2D(0.0, 0.0),
        board_outline=board_outline,
        texts=(),
    )
    log.info(
        "Gerber extract complete: %d layers, %d SBR polygons, %d vias, "
        "outline_pts=%d",
        len(copper_files), len(sbr_records), len(vias), len(board_outline),
    )
    return project, warnings
