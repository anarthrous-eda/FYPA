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
import os
import re
import time
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor
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
    RawHole,
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
    # Drill — Gerber X2 (Altium emits .GBR<n> with %TF.FileFunction,…,Drill*%
    # for PTH / NPTH / blind / buried / microvia drill data — these carry the
    # actual drill coordinates AND the layer span per file).
    (_re(r"\.gbr\d+$"), LAYER_ID_DRILL, None),
    # Drill *drawing* (.GD<n>) / *guide* (.GG<n>): graphical fab sheets only —
    # NonConductor symbols at hole positions, NOT machine-readable drill data.
    # The fallthrough would already ignore them; rules are explicit so a
    # future inner-layer regex can't accidentally pick them up as copper.
    (_re(r"\.gd\d+$"), LAYER_ID_IGNORE, None),
    (_re(r"\.gg\d+$"), LAYER_ID_IGNORE, None),
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


def _sniff_excellon_header(path: Path) -> bool:
    """Return True if ``path`` looks like an Excellon NC-drill program.

    An Excellon program opens with an ``M48`` header line. We sniff the first
    handful of lines (skipping leading comments / blank lines) for it. Used to
    rescue drill files with an ambiguous ``.txt`` extension, which no filename
    rule can safely claim (readmes, pick-and-place, and BOM exports share it).
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for _ in range(20):
                line = f.readline()
                if not line:
                    break
                if line.strip().upper().startswith("M48"):
                    return True
    except OSError:
        return False
    return False


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
    # `.txt` is ambiguous: Altium/KiCad NC-drill is commonly emitted as
    # `*.TXT`, but so are readmes / pick-and-place / BOM files. No filename
    # rule can tell them apart, so gate `.txt` on a positive content signal —
    # an Excellon `M48` header — before calling it a drill file. Everything
    # else falls through to the ordinary filename rules (→ Ignore).
    if path.suffix.lower() == ".txt" and _sniff_excellon_header(path):
        return LAYER_ID_DRILL
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
    # gerbonara Rectangle's (x,y) is the CENTRE; ``rotation`` is in
    # **radians** (per gerbonara.graphic_primitives.Rectangle's source —
    # "rotation around center in radians"). shapely.affinity.rotate
    # needs us to opt in via ``use_radians=True``; treating the value as
    # degrees would silently turn a 90° rotation (π/2 ≈ 1.57 rad) into
    # 1.57°, leaving the rectangle effectively un-rotated. That manifests
    # as side-of-chip pads drawn in their pre-rotation (horizontal)
    # orientation, overlapping into a solid blob.
    poly = shapely.geometry.box(x - w / 2.0, y - h / 2.0,
                                x + w / 2.0, y + h / 2.0)
    if rotation:
        poly = shapely.affinity.rotate(poly, rotation, origin=(x, y),
                                       use_radians=True)
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


def _tessellate_arcpoly_ring(outline, arc_centers) -> list[tuple[float, float]]:
    """Discretise a gerbonara ``ArcPoly`` (straight + arc segments) to a flat
    ring of ``(x, y)`` points.

    ``outline`` is the list of ring vertices (first and last implicitly
    connected). ``arc_centers`` parallels it: ``None`` for a straight segment,
    or a ``(clockwise, (cx, cy))`` tuple for an arc segment — that is
    gerbonara's own format (see ``gerbonara.graphic_primitives.ArcPoly``:
    "Arc segments have ``(clockwise, (cx, cy))`` tuple with cx, cy being
    absolute coords").

    Two things this gets right that the previous inline code did not:

    * **Unpacking.** Each entry is ``(clockwise, (cx, cy))``, not a bare
      ``(cx, cy)``; the old ``cx, cy = ac`` bound ``cx`` to the bool and
      ``cy`` to the centre tuple, so ``math.hypot`` raised ``TypeError`` on the
      first real arc. That exception propagated up to the per-layer guard in
      :func:`extract_gerber_project`, which dropped the *entire copper layer*
      with only a warning — any region with a rounded (arc) corner lost all
      its copper.
    * **Sweep direction.** Region arcs may sweep up to a full turn and the
      direction is significant (a 270° CW arc and a 90° CCW arc share
      endpoints but bound different areas), so we honour the ``clockwise``
      flag instead of always taking the short way around — the same convention
      :func:`_arc_to_polygon` uses for stroked arcs.
    """
    import math
    pts: list[tuple[float, float]] = []
    n = len(outline)
    for i in range(n):
        x0, y0 = outline[i]
        x1, y1 = outline[(i + 1) % n]
        pts.append((float(x0), float(y0)))
        ac = arc_centers[i] if arc_centers and i < len(arc_centers) else None
        if ac is None:
            continue  # straight segment
        clockwise, center = ac
        if (clockwise is None or center is None
                or center[0] is None or center[1] is None):
            continue  # gerbonara's straight-segment sentinel form
        cx, cy = center
        r = math.hypot(x0 - cx, y0 - cy)
        if r <= 0:
            continue
        a0 = math.atan2(y0 - cy, x0 - cx)
        epsilon = max(1e-6, r * 1e-9)
        if abs(x1 - x0) < epsilon and abs(y1 - y0) < epsilon:
            # Full-circle segment (start == end): force a whole turn in the
            # flagged direction rather than collapsing to a zero sweep.
            a1 = a0 - 2.0 * math.pi if clockwise else a0 + 2.0 * math.pi
        else:
            a1 = math.atan2(y1 - cy, x1 - cx)
            # clockwise=True is a negative-direction (CW) sweep in math coords.
            if clockwise:
                if a1 > a0:
                    a1 -= 2 * math.pi
            else:
                if a1 < a0:
                    a1 += 2 * math.pi
        delta = a1 - a0
        err_ratio = max(min(ARC_CHORD_TOLERANCE_MM / r, 0.99), 1e-6)
        dtheta_max = 2.0 * math.acos(1.0 - err_ratio)
        steps = max(2, int(math.ceil(abs(delta) / dtheta_max)))
        for k in range(1, steps):
            t = a0 + delta * (k / steps)
            pts.append((cx + r * math.cos(t), cy + r * math.sin(t)))
    return pts


def _arcpoly_to_polygon(outline, arc_centers) -> shapely.geometry.Polygon:
    """Region (filled polygon) — straight + arc segments.

    Discretises via :func:`_tessellate_arcpoly_ring` (which handles gerbonara's
    ``(clockwise, (cx, cy))`` arc format and arc direction) and lets shapely
    repair any self-intersection.
    """
    if len(outline) < 3:
        return shapely.geometry.Polygon()
    pts = _tessellate_arcpoly_ring(outline, arc_centers)
    # Close ring implicitly; let shapely figure out validity.
    poly = shapely.geometry.Polygon(pts)
    if not poly.is_valid:
        poly = shapely.make_valid(poly)
        # ``make_valid`` may return a MultiPolygon or GeometryCollection;
        # caller will handle that by accepting any (multi)polygon.
    return poly


def _primitive_to_polygon(prim):
    """Dispatch one gerbonara graphic_primitive → Shapely polygon.

    Reference single-primitive rasteriser. :func:`render_gerber_to_shapely`
    no longer calls this on its hot path (it batches strokes by width and
    caches flashes), but this keeps the per-primitive mapping in one readable
    place and is handy for tests / one-off conversions.
    """
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


def _flash_polygon_cached(prim, cache: dict) -> shapely.geometry.Polygon | None:
    """Rasterise one flashed aperture (``gp.Circle`` / ``gp.Rectangle``) to a
    Shapely polygon, caching the *unit* shape (built at the origin) keyed on
    its rounded aperture parameters and translating it to the flash position.

    Real boards flash the same aperture hundreds to thousands of times (every
    pad of a given size, every via land); building each one from scratch calls
    ``Point.buffer`` / ``box`` + ``affinity.rotate`` afresh, and gerbonara
    re-expands the aperture per flash. Rounding the shape params to the
    nanometre and reusing one buffered template per distinct aperture, then
    ``affinity.translate``-ing it, is geometrically identical (buffer / rotate
    are translation-invariant) but collapses the per-flash cost to a cheap
    coordinate shift.

    Returns ``None`` for a degenerate (zero-size) aperture or an unhandled
    primitive type so the caller can skip it.
    """
    import gerbonara.graphic_primitives as gp
    if isinstance(prim, gp.Circle):
        r = float(prim.r)
        if r <= 0:
            return None
        key = ("C", round(r, 6))
        unit = cache.get(key)
        if unit is None:
            unit = _circle_to_polygon(0.0, 0.0, r)
            cache[key] = unit
        return shapely.affinity.translate(unit, float(prim.x), float(prim.y))
    if isinstance(prim, gp.Rectangle):
        w = float(prim.w)
        h = float(prim.h)
        if w <= 0 or h <= 0:
            return None
        rot = float(getattr(prim, "rotation", 0.0))
        # Rotation is about the rectangle centre; building at the origin and
        # translating is equivalent to building in place (see _rectangle_to_
        # polygon: origin=(x, y) there, origin=(0, 0) here + translate).
        key = ("R", round(w, 6), round(h, 6), round(rot, 9))
        unit = cache.get(key)
        if unit is None:
            unit = _rectangle_to_polygon(0.0, 0.0, w, h, rot)
            cache[key] = unit
        return shapely.affinity.translate(unit, float(prim.x), float(prim.y))
    return None


# A dark↔clear polarity flip forces a flush (union / difference of the
# accumulated batch) that can't be avoided without breaking the photoplotter
# stream semantics. A file that interleaves polarity per pour degrades to
# O(flips × layer complexity); log a warning past this many flips so a
# pathologically-authored layer is identifiable from the Messages tab / log.
_POLARITY_FLIP_WARN_THRESHOLD: int = 50


def render_gerber_to_shapely(gerber_path: Path) -> shapely.geometry.base.BaseGeometry:
    """Open ``gerber_path`` with gerbonara and rasterise every object into one
    Shapely (Multi)Polygon. Polarity-aware: dark primitives are unioned,
    clear primitives are subtracted in stream order (mirrors how a Gerber
    photoplotter would resolve a layer).

    Returns ``Polygon`` / ``MultiPolygon`` / empty ``GeometryCollection``.
    """
    from gerbonara import GerberFile
    from gerbonara.utils import MM
    import gerbonara.graphic_primitives as gp

    gf = GerberFile.open(str(gerber_path))
    # Stream the objects in file order, batching consecutive same-polarity
    # primitives into one unary_union per batch (much faster than unioning
    # one-by-one). When polarity flips, apply the accumulated dark batch
    # to the running shape with ``union``, or the clear batch with
    # ``difference``.
    #
    # Within a batch, stroked primitives (Lines / Arcs) are grouped by pen
    # width and buffered ONCE per width as a single MultiLineString —
    # geometrically identical to buffering each stroke separately (buffer
    # distributes over union), but one GEOS call instead of N. Flashed
    # apertures (Circles / Rectangles) go through a per-aperture template
    # cache (see :func:`_flash_polygon_cached`).
    accumulated: shapely.geometry.base.BaseGeometry = shapely.geometry.Polygon()
    batch_polys: list[shapely.geometry.base.BaseGeometry] = []
    batch_lines_by_width: dict[float, list[shapely.geometry.LineString]] = {}
    batch_dark = True
    flash_cache: dict = {}
    flip_count = 0

    def _flush() -> None:
        nonlocal accumulated
        parts: list[shapely.geometry.base.BaseGeometry] = list(batch_polys)
        for width, lines in batch_lines_by_width.items():
            geom: shapely.geometry.base.BaseGeometry = (
                lines[0] if len(lines) == 1
                else shapely.geometry.MultiLineString(lines)
            )
            parts.append(geom.buffer(width / 2.0, cap_style="round",
                                     join_style="round"))
        if not parts:
            return
        merged = shapely.ops.unary_union(parts)
        accumulated = (accumulated.union(merged)
                       if batch_dark
                       else accumulated.difference(merged))

    for obj in gf.objects:
        for prim in obj.to_primitives(MM):
            is_dark = bool(prim.polarity_dark)
            if is_dark != batch_dark and (batch_polys or batch_lines_by_width):
                _flush()
                batch_polys = []
                batch_lines_by_width = {}
                flip_count += 1
            batch_dark = is_dark
            if isinstance(prim, gp.Line):
                w = float(prim.width)
                if w <= 0:
                    continue
                batch_lines_by_width.setdefault(round(w, 6), []).append(
                    shapely.geometry.LineString(
                        [(prim.x1, prim.y1), (prim.x2, prim.y2)]))
            elif isinstance(prim, gp.Arc):
                w = float(prim.width)
                if w <= 0:
                    continue
                pts = _discretise_arc_to_points(
                    prim.x1, prim.y1, prim.x2, prim.y2,
                    prim.cx, prim.cy, prim.clockwise)
                if len(pts) < 2:
                    continue
                batch_lines_by_width.setdefault(round(w, 6), []).append(
                    shapely.geometry.LineString(pts))
            elif isinstance(prim, (gp.Circle, gp.Rectangle)):
                poly = _flash_polygon_cached(prim, flash_cache)
                if poly is not None and not poly.is_empty:
                    batch_polys.append(poly)
            elif isinstance(prim, gp.ArcPoly):
                poly = _arcpoly_to_polygon(prim.outline, prim.arc_centers)
                if not poly.is_empty:
                    batch_polys.append(poly)
            else:
                log.debug("Unhandled gerbonara primitive type: %s",
                          type(prim).__name__)
    if batch_polys or batch_lines_by_width:
        _flush()
    if flip_count >= _POLARITY_FLIP_WARN_THRESHOLD:
        log.warning(
            "%s: %d dark/clear polarity flips — each forces a full-layer "
            "union/difference, so render time on this layer scales with the "
            "flip count. If this layer is slow, the source Gerber interleaves "
            "positive and clearance artwork per pour.",
            gerber_path.name, flip_count,
        )
    else:
        log.debug("%s: %d polarity flip(s)", gerber_path.name, flip_count)
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


def _discretise_arc_to_points(x1: float, y1: float, x2: float, y2: float,
                              cx: float, cy: float,
                              clockwise: bool) -> list[tuple[float, float]]:
    """Sample an arc to chord points at ARC_CHORD_TOLERANCE_MM. Returns
    points from (x1,y1) → (x2,y2) inclusive, with intermediate vertices.
    Empty list if radius is zero.
    """
    import math
    r = math.hypot(x1 - cx, y1 - cy)
    if r <= 0:
        return []
    epsilon = max(1e-6, r * 1e-9)
    is_full = (abs(x2 - x1) < epsilon and abs(y2 - y1) < epsilon)
    a0 = math.atan2(y1 - cy, x1 - cx)
    if is_full:
        a1 = a0 - 2.0 * math.pi if clockwise else a0 + 2.0 * math.pi
    else:
        a1 = math.atan2(y2 - cy, x2 - cx)
        if clockwise:
            if a1 > a0:
                a1 -= 2 * math.pi
        else:
            if a1 < a0:
                a1 += 2 * math.pi
    err_ratio = max(min(ARC_CHORD_TOLERANCE_MM / r, 0.99), 1e-6)
    dtheta_max = 2.0 * math.acos(1.0 - err_ratio)
    sweep = abs(a1 - a0)
    n = max(2, int(math.ceil(sweep / dtheta_max)) + 1)
    return [(cx + r * math.cos(a0 + (a1 - a0) * (i / (n - 1))),
             cy + r * math.sin(a0 + (a1 - a0) * (i / (n - 1))))
            for i in range(n)]


def render_outline_to_polyline(gerber_path: Path) -> tuple[Pt2D, ...]:
    """Fast path for board-outline gerbers.

    Skips the polygon-buffer-and-union pipeline used by
    :func:`render_gerber_to_shapely` (which can take ~10s on a stroked
    outline because every line gets a thick round-capped buffer and
    every flash gets unioned). Walks gerbonara primitives, collects
    segment endpoints (discretising arcs), and stitches them into
    closed rings by endpoint matching. Returns the largest-area closed
    ring as a ``Pt2D`` tuple matching the format
    :meth:`ExtractedProject.board_outline` expects.

    Falls back to ``render_outline_to_shapely`` + ``_outline_points`` if
    no closed ring can be stitched from the segments.
    """
    from gerbonara import GerberFile
    from gerbonara.utils import MM
    import gerbonara.graphic_primitives as gp

    gf = GerberFile.open(str(gerber_path))
    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    arcpoly_rings: list[list[tuple[float, float]]] = []
    for obj in gf.objects:
        for prim in obj.to_primitives(MM):
            if isinstance(prim, gp.Line):
                segments.append(((prim.x1, prim.y1), (prim.x2, prim.y2)))
            elif isinstance(prim, gp.Arc):
                pts = _discretise_arc_to_points(
                    prim.x1, prim.y1, prim.x2, prim.y2,
                    prim.cx, prim.cy, prim.clockwise,
                )
                for i in range(len(pts) - 1):
                    segments.append((pts[i], pts[i + 1]))
            elif isinstance(prim, gp.ArcPoly):
                # Each ArcPoly is already a closed region. A board-outline
                # layer routinely carries SEVERAL — the board contour plus
                # cutouts, mousebites, or fiducial keepouts — and there is no
                # guarantee the true contour comes first (a cutout or fiducial
                # can precede it). Collect them all and pick the largest by
                # area below, rather than returning whichever came first.
                # Shares the same correct arc handling as the copper path.
                ring = _tessellate_arcpoly_ring(prim.outline, prim.arc_centers)
                if len(ring) >= 3:
                    arcpoly_rings.append(ring)
            # Circles / rectangles on an outline layer are typically pad
            # flashes for fiducials etc; not part of the board boundary.

    if not segments and not arcpoly_rings:
        return ()

    # Stitch segments into rings by endpoint hashing. Greedy walk: from
    # each unused segment, follow connected endpoints (with float
    # tolerance) until the ring closes or no candidate remains.
    from collections import defaultdict

    def key(p: tuple[float, float]) -> tuple[int, int]:
        # 1e-6 mm quantisation tolerates the float noise gerbonara emits
        # when it converts inch/imperial coordinates.
        return (int(round(p[0] * 1e6)), int(round(p[1] * 1e6)))

    endpoint_map: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
    for i, (a, b) in enumerate(segments):
        endpoint_map[key(a)].append((i, 0))
        endpoint_map[key(b)].append((i, 1))

    used = [False] * len(segments)
    rings: list[list[tuple[float, float]]] = []
    for start in range(len(segments)):
        if used[start]:
            continue
        a, b = segments[start]
        ring: list[tuple[float, float]] = [a, b]
        used[start] = True
        start_key = key(a)
        while True:
            tail_key = key(ring[-1])
            if tail_key == start_key and len(ring) >= 4:
                break
            next_pick: tuple[int, tuple[float, float]] | None = None
            for cand_i, which_end in endpoint_map[tail_key]:
                if used[cand_i]:
                    continue
                cand_a, cand_b = segments[cand_i]
                next_pick = (cand_i, cand_b if which_end == 0 else cand_a)
                break
            if next_pick is None:
                break
            used[next_pick[0]] = True
            ring.append(next_pick[1])
        if len(ring) >= 4 and key(ring[0]) == key(ring[-1]):
            rings.append(ring)

    # Candidate rings come from two sources: stitched Line/Arc strokes and
    # standalone ArcPoly regions. Pick the largest-area ring across BOTH so a
    # small cutout / fiducial can never win over the real board contour.
    candidates = rings + arcpoly_rings
    if not candidates:
        # Couldn't stitch a closed loop — fall back to the slow path so
        # we still get *some* outline (convex-hull-ish via the buffered
        # union pipeline).
        outline_geom = render_outline_to_shapely(gerber_path)
        return _outline_points(outline_geom)

    # Absolute polygon area via the shoelace formula. Uses a modular index so
    # it is correct whether the ring is explicitly closed (stitched rings
    # carry a duplicated first==last vertex) or open (ArcPoly rings do not) —
    # the wrap-around closing edge is zero-length in the closed case.
    def shoelace(pts: list[tuple[float, float]]) -> float:
        n = len(pts)
        s = 0.0
        for i in range(n):
            x0, y0 = pts[i]
            x1, y1 = pts[(i + 1) % n]
            s += x0 * y1 - x1 * y0
        return 0.5 * abs(s)

    biggest = max(candidates, key=shoelace)
    # Drop the duplicated closing vertex to match _outline_points convention.
    if len(biggest) >= 2 and key(biggest[0]) == key(biggest[-1]):
        biggest = biggest[:-1]
    return tuple(Pt2D(float(x), float(y)) for x, y in biggest)


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


# --- drill (Excellon + Gerber X2) → RawVia ----------------------------------

def _is_gerber_x2_drill(path: Path) -> bool:
    """Sniff the first ~30 lines for a Gerber X2 ``%TF.FileFunction,…,Drill``
    (or ``…,Route`` / ``…,Mixed``) attribute. Gerber starts with ``%``;
    Excellon starts with ``M48`` / a comment / coordinates, so this is a safe
    discriminator regardless of the file extension the user picked.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            head = "".join(f.readline() for _ in range(30))
    except OSError:
        return False
    m = re.search(r"%TF\.FileFunction,([^*]+)\*%", head)
    if not m:
        return False
    last = m.group(1).rsplit(",", 1)[-1].strip().lower()
    return last in {"drill", "route", "mixed"}


def _x2_drill_span_to_layer_ids(
    file_function: tuple[str, ...] | None,
    ordered_layer_ids: list[int],
) -> tuple[int, int]:
    """Translate the X2 ``%TF.FileFunction`` span into FYPA layer ids.

    FileFunction is e.g. ``('Plated', '1', '16', 'PTH', 'Drill')`` — the two
    integers are **1-based physical layer positions** (1 = top, N = bottom)
    in the originating CAD. We map position k → ``ordered_layer_ids[k-1]``;
    out-of-range positions clamp to the nearest end of the imported stack so
    a drill file describing a 16-layer board still produces sensible vias
    when the user only imports a subset.
    """
    if not ordered_layer_ids:
        return (LAYER_ID_TOP, LAYER_ID_BOTTOM)
    n = len(ordered_layer_ids)
    start_pos = end_pos = None
    if file_function is not None:
        for token in file_function:
            try:
                v = int(token)
            except (TypeError, ValueError):
                continue
            if start_pos is None:
                start_pos = v
            else:
                end_pos = v
                break
    if start_pos is None or end_pos is None:
        return (ordered_layer_ids[0], ordered_layer_ids[-1])
    lo, hi = sorted((start_pos, end_pos))
    lo_idx = max(0, min(n - 1, lo - 1))
    hi_idx = max(0, min(n - 1, hi - 1))
    return (ordered_layer_ids[lo_idx], ordered_layer_ids[hi_idx])


def _gerber_drill_to_vias(
    path: Path,
    ordered_layer_ids: list[int],
) -> tuple[list[RawVia], list[RawHole], list[str]]:
    """Parse one Gerber X2 drill file → ``RawVia`` + ``RawHole`` records.

    Plated (PTH / blind / buried / microvia) files become vias: round
    flashes (``gp.Circle`` primitives) → one via each; routed slots
    (``gp.Line`` primitives — common for oval component-lead holes) get
    discretised into a chain of overlapping circular via stamps so they
    bridge the layer pair across the slot's full length in the FEM mesh.

    NonPlated files (mechanical / mounting holes) carry no electrical role,
    so each flash / slot becomes a ``RawHole`` instead — drawn as the
    "Non Plated TH" Board Features overlay but never meshed.
    """
    import math
    import gerbonara.graphic_primitives as gp
    from gerbonara import GerberFile
    from gerbonara.utils import MM

    vias: list[RawVia] = []
    npth: list[RawHole] = []
    warnings: list[str] = []
    try:
        gf = GerberFile.open(str(path))
    except Exception as e:
        warnings.append(
            f"Couldn't parse Gerber drill file {path.name} "
            f"({type(e).__name__}: {e}); skipping."
        )
        return vias, npth, warnings

    file_function = gf.file_attrs.get(".FileFunction") if gf.file_attrs else None
    # NonPlated = mechanical mounting holes, no electrical role — collect
    # them as NPTH holes (drawn, not meshed) rather than vias.
    is_npth = bool(file_function
                   and file_function[0].strip().lower() == "nonplated")

    layer_start, layer_end = _x2_drill_span_to_layer_ids(
        file_function, ordered_layer_ids,
    )

    for obj in gf.objects:
        for prim in obj.to_primitives(MM):
            if isinstance(prim, gp.Circle):
                diam = 2.0 * float(prim.r)
                if diam <= 0:
                    continue
                if is_npth:
                    npth.append(RawHole(
                        center=Pt2D(float(prim.x), float(prim.y)),
                        diameter_mm=diam,
                    ))
                    continue
                vias.append(RawVia(
                    center=Pt2D(float(prim.x), float(prim.y)),
                    diameter_mm=diam + VIA_ANNULAR_RING_HEURISTIC_MM,
                    hole_diameter_mm=diam,
                    layer_start=layer_start,
                    layer_end=layer_end,
                    net_index=NO_NET,
                ))
            elif isinstance(prim, gp.Line):
                # Routed slot (oblong hole): stamp circular vias along the
                # path so the bridge between layers spans the full slot.
                w = float(prim.width)
                if w <= 0:
                    continue
                dx = float(prim.x2) - float(prim.x1)
                dy = float(prim.y2) - float(prim.y1)
                length = math.hypot(dx, dy)
                step = max(w / 2.0, 1e-3)
                n_stamps = max(2, int(math.ceil(length / step)) + 1)
                for i in range(n_stamps):
                    t = i / (n_stamps - 1) if n_stamps > 1 else 0.0
                    cx = float(prim.x1) + dx * t
                    cy = float(prim.y1) + dy * t
                    if is_npth:
                        npth.append(RawHole(
                            center=Pt2D(cx, cy), diameter_mm=w))
                        continue
                    vias.append(RawVia(
                        center=Pt2D(cx, cy),
                        diameter_mm=w + VIA_ANNULAR_RING_HEURISTIC_MM,
                        hole_diameter_mm=w,
                        layer_start=layer_start,
                        layer_end=layer_end,
                        net_index=NO_NET,
                    ))
            # Arcs / regions in a drill file are uncommon; ignore quietly.
    return vias, npth, warnings


def _excellon_to_vias(
    drill_paths: list[Path],
    ordered_layer_ids: list[int],
) -> tuple[list[RawVia], list[RawHole], list[str]]:
    """Parse every Excellon drill file → ``RawVia`` + ``RawHole`` records.

    Plated hits become vias spanning the top↔bottom of the imported stack
    (Excellon carries no layer-span info). Explicitly non-plated tools
    become NPTH holes (drawn, not meshed) instead of being discarded.
    """
    from gerbonara import ExcellonFile
    from gerbonara.utils import MM

    vias: list[RawVia] = []
    npth: list[RawHole] = []
    warnings: list[str] = []
    top_layer_id = ordered_layer_ids[0] if ordered_layer_ids else LAYER_ID_TOP
    bottom_layer_id = (
        ordered_layer_ids[-1] if ordered_layer_ids else LAYER_ID_BOTTOM
    )
    for path in drill_paths:
        try:
            ef = ExcellonFile.open(str(path))
        except Exception as e:  # SyntaxError, OSError, …
            warnings.append(
                f"Couldn't parse drill file {path.name} ({type(e).__name__}: "
                f"{e}); skipping."
            )
            continue
        for d in ef.objects:
            try:
                # Let gerbonara handle inch/mm conversion. Its LengthUnit
                # ``__str__`` returns the shorthand ("in"), so the old
                # ``str(d.unit) == "inch"`` check never matched and inch drill
                # files came through 25.4x too small (all holes bunched at the
                # origin). ``convert_from`` is unit-aware and a no-op for
                # mm / unit-less files.
                x = float(MM.convert_from(d.unit, d.x))
                y = float(MM.convert_from(d.unit, d.y))
                tool = d.tool        # ExcellonTool
                diam_mm = float(tool.equivalent_width(MM))
            except Exception as e:
                warnings.append(
                    f"Skipping malformed drill record in {path.name} "
                    f"({type(e).__name__}: {e})."
                )
                continue
            if diam_mm <= 0:
                continue
            if hasattr(tool, "plated") and tool.plated is False:
                npth.append(RawHole(
                    center=Pt2D(x, y), diameter_mm=diam_mm))
                continue
            vias.append(RawVia(
                center=Pt2D(x, y),
                diameter_mm=diam_mm + VIA_ANNULAR_RING_HEURISTIC_MM,
                hole_diameter_mm=diam_mm,
                layer_start=top_layer_id,
                layer_end=bottom_layer_id,
                net_index=NO_NET,
            ))
    return vias, npth, warnings


def _drill_files_to_vias(
    drill_paths: list[Path],
    ordered_layer_ids: list[int],
) -> tuple[tuple[RawVia, ...], tuple[RawHole, ...], list[str]]:
    """Dispatch each drill file to the Gerber X2 or Excellon parser based on
    its actual content (filename is a hint, not authoritative). Returns
    ``(vias, npth_holes, warnings)``."""
    vias: list[RawVia] = []
    npth: list[RawHole] = []
    warnings: list[str] = []
    excellon_batch: list[Path] = []
    for path in drill_paths:
        if _is_gerber_x2_drill(path):
            v, h, w = _gerber_drill_to_vias(path, ordered_layer_ids)
            vias.extend(v)
            npth.extend(h)
            warnings.extend(w)
        else:
            excellon_batch.append(path)
    if excellon_batch:
        v, h, w = _excellon_to_vias(excellon_batch, ordered_layer_ids)
        vias.extend(v)
        npth.extend(h)
        warnings.extend(w)
    return tuple(vias), tuple(npth), warnings


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
    progress_cb=None,
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

    def _progress(stage=None, substage=None):
        if progress_cb is None:
            return
        try:
            progress_cb(stage, substage)
        except Exception:
            # Never let a UI callback failure abort the import.
            pass

    # 1. Rasterise each copper layer → list of connected-component
    #    RawShapeBasedRegions. The per-layer render_gerber_to_shapely
    #    call is pure-CPU (gerbonara parse + Shapely/GEOS booleans) and
    #    each layer is independent, so for >1 layer we fan out to a
    #    ProcessPoolExecutor and gather (Multi)Polygon results back.
    #    Windows uses spawn — keep the worker function module-level
    #    (render_gerber_to_shapely is) and pass plain Paths.
    sbr_records: list[RawShapeBasedRegion] = []
    all_copper: list[shapely.geometry.base.BaseGeometry] = []
    # Every entry in copper_files is a COPPER layer. A render failure here is
    # missing copper — a silently-wrong answer for a power-delivery tool — so
    # unlike a soft outline/drill failure we collect these and hard-fail the
    # whole import below rather than opening a viewer with layers quietly
    # absent (traced only by a Messages-tab line). Collected across all layers
    # first so the error names every failure, not just the first.
    failed_copper_layers: list[str] = []
    items = list(copper_files.items())
    # Cap workers: too many spawned Python processes on Windows just
    # thrash memory + I/O without speeding anything up.
    n_workers = min(os.cpu_count() or 1, len(items), 8)
    geom_by_layer: dict[int, shapely.geometry.base.BaseGeometry] = {}
    t_render0 = time.monotonic()
    _progress(stage=f"Rendering {len(items)} Gerber layers…",
              substage=f"0 / {len(items)} done")
    if n_workers > 1 and len(items) > 1:
        log.info("Gerber: rendering %d layers in parallel (workers=%d)",
                 len(items), n_workers)
        with ProcessPoolExecutor(max_workers=n_workers) as pool:
            fut_to_meta = {
                pool.submit(render_gerber_to_shapely, path): (layer_id, path)
                for layer_id, path in items
            }
            from concurrent.futures import as_completed
            done = 0
            for fut in as_completed(fut_to_meta):
                layer_id, path = fut_to_meta[fut]
                try:
                    geom_by_layer[layer_id] = fut.result()
                    log.info("Gerber: rendered layer %d (%s)",
                             layer_id, path.name)
                except Exception as e:
                    failed_copper_layers.append(
                        f"layer {layer_id} ({path.name}): "
                        f"{type(e).__name__}: {e}"
                    )
                done += 1
                _progress(substage=f"{done} / {len(items)} done "
                                   f"(latest: {path.name})")
    else:
        for idx, (layer_id, path) in enumerate(items, start=1):
            log.info("Gerber: rendering layer %d (%s)", layer_id, path.name)
            try:
                geom_by_layer[layer_id] = render_gerber_to_shapely(path)
            except Exception as e:
                failed_copper_layers.append(
                    f"layer {layer_id} ({path.name}): "
                    f"{type(e).__name__}: {e}"
                )
            _progress(substage=f"{idx} / {len(items)} done "
                               f"(latest: {path.name})")
    log.info("Gerber: per-layer render total %.2fs (%d layer(s))",
             time.monotonic() - t_render0, len(geom_by_layer))
    if failed_copper_layers:
        # Abort the import rather than proceed with missing copper. The caller
        # (_GerberImportWorker) surfaces this as a failure dialog; outline /
        # drill render failures stay soft (warnings) because a missing board
        # outline or drill still leaves the copper solve meaningful.
        raise RuntimeError(
            "Failed to render "
            f"{len(failed_copper_layers)} of {len(items)} copper layer(s); "
            "aborting import so the board isn't opened with copper missing:\n  "
            + "\n  ".join(failed_copper_layers)
        )
    # Iterate input order so sbr_records / all_copper are deterministic.
    _progress(stage="Building shape-based region records…", substage="")
    t_sbr0 = time.monotonic()
    for layer_id, _path in items:
        geom = geom_by_layer.get(layer_id)
        if geom is None:
            continue
        for poly in _polygons_in(geom):
            if poly.area <= 0:
                continue
            sbr_records.append(_polygon_to_shape_based_region(poly, layer_id))
        if not geom.is_empty:
            all_copper.append(geom)
    log.info("Gerber: SBR assembly took %.2fs (%d records)",
             time.monotonic() - t_sbr0, len(sbr_records))

    # 2. Drill → Vias. Excellon has no span info so its hits span the full
    #    top↔bottom of the imported stack; Gerber X2 drill files (.GBR<n>)
    #    carry per-file span in %TF.FileFunction so microvias / blind /
    #    buried vias come out with the correct layer pair.
    _progress(stage="Reading drill files…", substage="")
    t_drill0 = time.monotonic()
    ordered_ids = sorted(copper_files.keys(), key=lambda i: (i == LAYER_ID_BOTTOM, i))
    # Sort so Top=1 first, inner ids next ascending, Bottom=32 last.
    if not ordered_ids:
        ordered_ids = [LAYER_ID_TOP, LAYER_ID_BOTTOM]
    if drill_files:
        vias, npth_holes, drill_warnings = _drill_files_to_vias(
            drill_files, ordered_ids)
        warnings.extend(drill_warnings)
        if not vias and not npth_holes:
            warnings.append(
                "Drill file(s) supplied but produced no via records "
                f"({', '.join(p.name for p in drill_files)}). Multi-layer "
                "rails will need editor directives or copper names to "
                "bridge layers."
            )
    else:
        vias = ()
        npth_holes = ()
        warnings.append(
            "No drill file provided (Excellon .drl/.xln/.tap/.nc or Gerber X2 "
            ".GBR<n>). Vias / through-hole pads won't be reconstructed; "
            "multi-layer rails will need editor directives or copper names "
            "to bridge layers."
        )
    log.info("Gerber: drill/vias took %.2fs (%d via(s))",
             time.monotonic() - t_drill0, len(vias))

    # 3. Board outline. Prefer an explicit outline file; fall back to
    #    bounding box of unioned copper.
    _progress(stage="Building board outline…", substage="")
    t_outline0 = time.monotonic()
    board_outline: tuple[Pt2D, ...] = ()
    if outline_file is not None:
        try:
            board_outline = render_outline_to_polyline(outline_file)
        except Exception as e:
            warnings.append(
                f"Couldn't render outline {outline_file.name} "
                f"({type(e).__name__}: {e}); using copper bounding box."
            )
    if not board_outline and all_copper:
        # bbox(union(A,B,...)) == bbox of bbox-union, so skip the (very
        # expensive on big boards) unary_union call and just min/max
        # over each layer geometry's bounds.
        bounds = [g.bounds for g in all_copper if not g.is_empty]
        if bounds:
            minx = min(b[0] for b in bounds)
            miny = min(b[1] for b in bounds)
            maxx = max(b[2] for b in bounds)
            maxy = max(b[3] for b in bounds)
            board_outline = (
                Pt2D(minx, miny), Pt2D(maxx, miny),
                Pt2D(maxx, maxy), Pt2D(minx, maxy),
            )
    log.info("Gerber: board outline took %.2fs (%s, %d pts)",
             time.monotonic() - t_outline0,
             "from outline file" if outline_file is not None else "from copper bbox",
             len(board_outline))

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
        npth_holes=tuple(npth_holes),
    )
    log.info(
        "Gerber extract complete: %d layers, %d SBR polygons, %d vias, "
        "%d NPTH holes, outline_pts=%d",
        len(copper_files), len(sbr_records), len(vias), len(npth_holes),
        len(board_outline),
    )
    return project, warnings
