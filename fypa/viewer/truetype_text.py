"""Outline Altium TrueType PCB text into board-space triangles.

Stroke-font text arrives from the loader as Altium's own single-stroke
polylines. TrueType text (Arial, etc.) carries only its string and font, so
it used to fall back to a screen-space label in the UI font, capped in pixel
size. This module instead lays the string out in its real face with Qt's
font engine and converts the glyph outlines to world-millimetre geometry,
so it scales with zoom, mirrors on the bottom side and reads exactly like
the manufactured silkscreen.

Altium sizes TrueType text by its character cell: the ``height`` is the
font's ascent + descent (GDI cell height; checked against Altium's stored
text-box size to 0.2 % on Consolas). Placement depends on the record:

* ``box_anchor`` (Altium's text-box mode, ``is_justification_valid``): the
  bottom-left corner of the glyphs' bounding box sits on the anchor, in the
  text's rotated frame, whatever the justification.
* otherwise (legacy text): the anchor is the bottom-left of the character
  cell, one descent below the baseline.
"""
from __future__ import annotations

import math

import numpy as np

from fypa.viewer import copper_tris as _copper_tris

# Layout pixel size. Large so the outline flattening is fine relative to the
# glyph; the result is rescaled to the real height in mm.
_LAYOUT_PX = 256


def _glyph_polygons(text: str, font_name: str, bold: bool, italic: bool):
    """Shapely polygons of ``text`` in font units (y up, baseline at 0),
    plus the font's ``ascent + descent`` and ``descent`` in the same units.
    ``None`` when nothing could be laid out."""
    from PySide6.QtGui import QFont, QFontMetricsF, QPainterPath
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    font = QFont(font_name)
    font.setPixelSize(_LAYOUT_PX)
    font.setBold(bold)
    font.setItalic(italic)
    fm = QFontMetricsF(font)
    path = QPainterPath()
    path.addText(0.0, 0.0, font, text)

    # TrueType outlines fill non-zero: outer contours run one way, counters
    # (the hole in "O") the other. Split rings by orientation, union each
    # group, and subtract - robust to letters that touch or overlap.
    rings = []
    for qpoly in path.toSubpathPolygons():
        pts = [(p.x(), -p.y()) for p in qpoly]  # Qt y is down
        if len(pts) < 3:
            continue
        try:
            ring = Polygon(pts)
        except Exception:
            continue
        if not ring.is_valid:
            ring = ring.buffer(0)
        if ring.is_empty or ring.area <= 0.0:
            continue
        # Signed area (shoelace) gives the winding direction.
        xs = np.asarray([p[0] for p in pts])
        ys = np.asarray([p[1] for p in pts])
        signed = 0.5 * float(np.dot(xs, np.roll(ys, -1))
                             - np.dot(ys, np.roll(xs, -1)))
        rings.append((signed, ring))
    if not rings:
        return None
    outer_sign = math.copysign(1.0, max(rings, key=lambda r: abs(r[0]))[0])
    outers = [r for s, r in rings if math.copysign(1.0, s) == outer_sign]
    holes = [r for s, r in rings if math.copysign(1.0, s) != outer_sign]
    try:
        shape = unary_union(outers)
        if holes:
            shape = shape.difference(unary_union(holes))
    except Exception:
        return None
    if shape.is_empty:
        return None
    return shape, fm.ascent() + fm.descent(), fm.descent()


def truetype_text_tris(rec: dict, *, solid: bool = True) -> np.ndarray:
    """Triangles (``(N*3, 2)`` float32, world mm) for one TrueType text
    record from the loader (``text``, ``x_mm``, ``y_mm``, ``height_mm``,
    ``rotation_deg``, ``font_name``, ``bold``, ``italic``, ``mirrored``).

    ``solid`` fills the glyphs; otherwise their outlines are traced (the
    wire-mesh fill style). Both variants are cached on the record."""
    cache_key = "_tt_tris" if solid else "_tt_tris_wire"
    cached = rec.get(cache_key)
    if cached is not None:
        return cached
    empty = np.empty((0, 2), dtype=np.float32)
    text = rec.get("text") or ""
    height = float(rec.get("height_mm", 0.0) or 0.0)
    if not text.strip() or height <= 0.0:
        rec[cache_key] = empty
        return empty
    laid = _glyph_polygons(text, rec.get("font_name") or "Arial",
                           bool(rec.get("bold")), bool(rec.get("italic")))
    if laid is None:
        rec[cache_key] = empty
        return empty
    shape, cell, descent = laid
    if cell <= 0.0:
        rec[cache_key] = empty
        return empty

    # Font units -> mm, place the anchor, mirror (bottom side), rotate CCW
    # about the anchor, translate to the board position.
    s = height / cell
    sx = -s if rec.get("mirrored") else s
    ang = math.radians(float(rec.get("rotation_deg", 0.0) or 0.0))
    c, si = math.cos(ang), math.sin(ang)
    ax = float(rec.get("x_mm", 0.0))
    ay = float(rec.get("y_mm", 0.0))
    if rec.get("box_anchor"):
        # Text-box placement: the glyphs' own bounding box sits with its
        # bottom-left corner on the anchor (any justification is already
        # folded into the stored position).
        min_x, min_y = shape.bounds[0], shape.bounds[1]
        off_x, off_y = -min_x, -min_y
    else:
        # Legacy placement: the bottom-left of the character cell, one
        # descent below the baseline.
        off_x, off_y = 0.0, descent

    def _xf(coords) -> np.ndarray:
        a = np.asarray(coords, dtype=np.float64)
        lx = (a[:, 0] + off_x) * sx
        ly = (a[:, 1] + off_y) * s
        out = np.empty((a.shape[0], 2), dtype=np.float32)
        out[:, 0] = ax + lx * c - ly * si
        out[:, 1] = ay + lx * si + ly * c
        return out

    parts = list(shape.geoms) if hasattr(shape, "geoms") else [shape]
    style = _copper_tris.SOLID if solid else _copper_tris.WIRE
    chunks: list[np.ndarray] = []
    for p in parts:
        if p.geom_type != "Polygon" or p.is_empty:
            continue
        tris = _copper_tris.build_polygon(
            style, _xf(p.exterior.coords),
            [_xf(h.coords) for h in p.interiors])
        if tris.size:
            chunks.append(tris)
    arr = np.concatenate(chunks, axis=0) if chunks else empty
    rec[cache_key] = arr
    return arr
