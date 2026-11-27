"""KiCad ``.kicad_pcb`` (+ optional ``.kicad_sch``) → :class:`ExtractedProject`.

Adapts a KiCad 9 board (and, when present, its schematic) into the same frozen
:class:`~fypa.altium.extract.ExtractedProject` the Altium and Gerber paths
produce, so every downstream stage works unchanged. Parsing is done by the
self-contained reader in :mod:`fypa.kicad.sexpr`.

What maps where:

* copper **tracks / arcs / vias** ← ``(segment)`` / ``(arc)`` / ``(via)``;
* **pads** ← footprint ``(pad ...)`` (SMD on its copper layer, through-hole on
  layer id 74 = Multi-Layer);
* copper **pours** ← ``(zone ...)`` filled polygons, one
  :class:`RawShapeBasedRegion` per ``(filled_polygon)`` - mirroring the Gerber
  path. An **unfilled** zone (no ``filled_polygon``) is skipped with a warning;
* **components** ← ``(footprint ...)``; crucially, ``parameters`` = the
  footprint's custom ``(property "KEY" "VAL")`` fields - this is where the
  ``PDN_*`` directives live (KiCad 7+ syncs schematic symbol fields down to
  footprint properties), so :func:`fypa.altium.annotations.parse_annotations`
  picks them up as PCB-sourced directives with no changes;
* **nets** ← the board's ``(net N "name")`` table. KiCad net numbers are used
  directly as FYPA net indices (net 0 = unconnected → ``NO_NET``); names are
  already resolved global names, so ``PDN_*_NET`` values match directly and
  ``compiled_netlist`` stays ``None``;
* **stackup** ← ``(setup (stackup ...))`` when present, else a synthesised
  default (1 oz copper, 1.6 mm total board split across dielectrics);
* **board outline** ← ``Edge.Cuts`` graphics: ``gr_line`` / ``gr_arc`` /
  ``gr_rect`` / ``gr_circle`` / ``gr_poly`` and any ``fp_*`` edge graphics
  drawn inside footprints.

Layer IDs follow the Altium convention used everywhere in FYPA: ``1 = Top``,
``32 = Bottom``, ``2..31 = Inner 1..30``, ``74 = Multi-Layer`` (through-hole).
Coordinates are millimetres. The file is Y-down; every record is built in that
frame (where footprint-local offsets compose) and mirrored to FYPA's Y-up frame
as the last step (:func:`_to_fypa_frame`), so a board reads ``(x, -y)``. That is
also where KiCad plots its Gerbers, so a KiCad import and a Gerber import of the
same board line up.
"""
from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable
from pathlib import Path

from fypa.altium.extract import (
    NO_NET,
    NO_POLYGON,
    ExtractedProject,
    Pt2D,
    RawArc,
    RawHole,
    RawNet,
    RawPad,
    RawPcbComponent,
    RawRegionVertex,
    RawSchComponent,
    RawShapeBasedRegion,
    RawStackupLayer,
    RawTrack,
    RawVia,
)
from fypa.kicad import sexpr
from fypa.kicad.sexpr import SNode

log = logging.getLogger(__name__)

# --- layer-id convention (shared with the Altium / Gerber paths) -----------
LAYER_ID_TOP: int = 1
LAYER_ID_BOTTOM: int = 32
MULTI_LAYER_PAD_LAYER_ID: int = 74

_INNER_RE = re.compile(r"^In(\d+)\.Cu$")

# Fallback stackup (used only when the board has no `(setup (stackup ...))`),
# matching the Gerber import defaults: 1 oz copper, 1.6 mm total board.
_DEFAULT_COPPER_MM: float = 0.035
_DEFAULT_BOARD_THICKNESS_MM: float = 1.6

# Endpoint-match tolerance when stitching the board outline (mm).
_OUTLINE_STITCH_TOL_MM: float = 0.01

# ``(version N)`` of the board file format: the date of the last format change
# in each KiCad release. KiCad 5 and older use a different footprint syntax
# (``module``) and are refused; KiCad 6 and anything newer than the newest
# release checked against this reader load with a warning.
KICAD6_BOARD_VERSION: int = 20211014
KICAD7_BOARD_VERSION: int = 20221018
# The last KiCad 10 format change (variant serialization). KiCad 10 boards are
# read to KiCad's own reader and writer code (nets by name, IPC-4761 via
# features, variants); checked on a real KiCad 10.0 board (every net one
# connected piece, no two nets overlapping, solves).
KICAD10_BOARD_VERSION: int = 20260206
NEWEST_TESTED_BOARD_VERSION: int = KICAD10_BOARD_VERSION


def board_format_note(version: int | None) -> str | None:
    """A warning for a board format this reader was not checked against, or
    ``None`` for KiCad 7 to 10. Raises :class:`ValueError` for KiCad 5 and older."""
    if version is None:
        return ("KiCad board file carries no format version - read as the "
                "current format; check the copper on the Board tab")
    if version < KICAD6_BOARD_VERSION:
        raise ValueError(
            f"KiCad board format {version} is from KiCad 5 or older, which FYPA "
            "can't read. Open the board in KiCad 7 or newer and save it.")
    if version < KICAD7_BOARD_VERSION:
        return (f"KiCad board format {version} is from KiCad 6, which FYPA has "
                "not been checked against - open and save the board in KiCad 7 "
                "or newer if the copper looks wrong")
    if version > NEWEST_TESTED_BOARD_VERSION:
        return (f"KiCad board format {version} is newer than KiCad 10, the "
                "newest format FYPA reads - check the copper on the Board tab")
    return None


def resolve_kicad_board(path: str | Path) -> Path:
    """The ``.kicad_pcb`` for a picked ``.kicad_pcb`` or ``.kicad_pro``.

    A KiCad project has one board, named after the project, beside the
    ``.kicad_pro``. Raises :class:`FileNotFoundError` when it isn't there.
    """
    p = Path(path)
    if p.suffix.lower() == ".kicad_pro":
        p = p.with_suffix(".kicad_pcb")
    if not p.is_file():
        raise FileNotFoundError(f"KiCad board not found: {p}")
    return p


def _int_or_none(value: str | None, what: str) -> int | None:
    """Coerce a KiCad token to ``int``, warning + returning ``None`` on garbage.

    The numeric accessors on :class:`SNode` (``f`` / ``f_at``) already swallow
    malformed floats, but net numbers and drill counts are read as raw ``str``
    atoms and coerced here; a corrupt token would otherwise abort the whole
    extract with a bare :class:`ValueError` traceback. Mirrors the forgiving
    ``f()`` pattern: warn once, degrade (an unparseable net → ``None`` →
    ``NO_NET``) rather than crash.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        log.warning("KiCad: ignoring malformed %s %r", what, value)
        return None


def kicad_layer_to_fypa_id(name: str) -> int | None:
    """Map a KiCad copper-layer name to FYPA's integer layer id.

    ``F.Cu → 1``, ``B.Cu → 32``, ``In{N}.Cu → 1+N`` (so ``In1.Cu → 2`` …
    ``In30.Cu → 31``). Returns ``None`` for any non-copper layer (silk, mask,
    ``Edge.Cuts`` …), which callers skip.
    """
    if name == "F.Cu":
        return LAYER_ID_TOP
    if name == "B.Cu":
        return LAYER_ID_BOTTOM
    m = _INNER_RE.match(name)
    if m:
        return 1 + int(m.group(1))
    return None


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------
def _xy(node: SNode | None) -> Pt2D:
    """``(tag x y ...)`` → :class:`Pt2D` (0,0 if absent)."""
    if node is None:
        return Pt2D(0.0, 0.0)
    return Pt2D(node.f_at(0), node.f_at(1))


def _rotate_local(px: float, py: float, deg: float) -> tuple[float, float]:
    """Rotate a footprint-local point by the footprint orientation *deg*.

    KiCad angles are counter-clockwise on screen, but the file is Y-down, so a
    positive KiCad angle is clockwise in raw (x-right, y-down) coordinates -
    hence the ``-deg`` in the standard rotation matrix::

        gx = px·cos(-θ) - py·sin(-θ)
        gy = px·sin(-θ) + py·cos(-θ)

    NOTE: rotation sign is the top field-verification risk for KiCad import; it
    is exercised only by rotated footprints (the bundled example uses 0°).
    """
    a = math.radians(-deg)
    ca, sa = math.cos(a), math.sin(a)
    return (px * ca - py * sa, px * sa + py * ca)


def _arc_from_three_points(
    start: Pt2D, mid: Pt2D, end: Pt2D
) -> tuple[Pt2D, float, float, float] | None:
    """Convert a KiCad start/mid/end arc to ``(center, radius, a_start, a_end)``.

    Angles are degrees in FYPA's convention: the consumer traces
    ``a_start → a_start + (a_end - a_start) mod 360`` with plain ``cos/sin``, so
    we orient ``a_start``/``a_end`` such that that CCW sweep passes through
    *mid*. Returns ``None`` for a degenerate (near-collinear) arc.
    """
    ax, ay = start.x, start.y
    bx, by = mid.x, mid.y
    cx, cy = end.x, end.y
    d = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return None
    a2 = ax * ax + ay * ay
    b2 = bx * bx + by * by
    c2 = cx * cx + cy * cy
    ux = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / d
    uy = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / d
    center = Pt2D(ux, uy)
    radius = math.hypot(ax - ux, ay - uy)
    a_start = math.atan2(ay - uy, ax - ux)
    a_mid = math.atan2(by - uy, bx - ux)
    a_end = math.atan2(cy - uy, cx - ux)
    two_pi = 2.0 * math.pi
    sweep_end = (a_end - a_start) % two_pi
    sweep_mid = (a_mid - a_start) % two_pi
    if sweep_mid > sweep_end:
        # mid is on the far side of the CCW start→end arc - traverse the
        # other way by swapping the endpoints.
        a_start, a_end = a_end, a_start
    return center, radius, math.degrees(a_start), math.degrees(a_end)


def _stitch_outline(segments: list[tuple[Pt2D, Pt2D]]) -> tuple[Pt2D, ...]:
    """Greedily chain unordered edge segments into a single ordered polyline.

    Returns the ordered vertices of the largest chain found (endpoints matched
    within :data:`_OUTLINE_STITCH_TOL_MM`); an empty tuple when there are no
    segments. Robust to arbitrary segment order; good enough for the common
    single closed outline.
    """
    if not segments:
        return ()
    tol2 = _OUTLINE_STITCH_TOL_MM ** 2

    def close(p: Pt2D, q: Pt2D) -> bool:
        return (p.x - q.x) ** 2 + (p.y - q.y) ** 2 <= tol2

    remaining = list(segments)
    chain: list[Pt2D] = list(remaining.pop(0))
    progressed = True
    while remaining and progressed:
        progressed = False
        for i, (p, q) in enumerate(remaining):
            if close(chain[-1], p):
                chain.append(q)
            elif close(chain[-1], q):
                chain.append(p)
            elif close(chain[0], q):
                chain.insert(0, p)
            elif close(chain[0], p):
                chain.insert(0, q)
            else:
                continue
            remaining.pop(i)
            progressed = True
            break
    return tuple(chain)


def _slot_stamp_centers(
    center: Pt2D, bore_mm: float, major_mm: float, angle_deg: float
) -> list[Pt2D]:
    """Sample an obround (oval) drill's centreline into overlapping stamp points.

    Returns the centres of a chain of ``bore_mm``-diameter circles laid along
    the slot's major axis (absolute board angle *angle_deg*), spaced half a bore
    apart so the stamped circles overlap into a continuous slot - mirroring the
    Gerber drill path (:func:`fypa.gerber.extract` routed-slot handling). Used
    for both a plated slot's layer-bridging via chain and a non-plated slot's
    NPTH holes, so neither collapses to a single small circle.
    """
    ux, uy = _rotate_local(1.0, 0.0, angle_deg)
    half = max(0.0, (major_mm - bore_mm) / 2.0)
    x1, y1 = center.x - ux * half, center.y - uy * half
    x2, y2 = center.x + ux * half, center.y + uy * half
    step = max(bore_mm / 2.0, 1e-3)
    n = max(2, int(math.ceil((2.0 * half) / step)) + 1)
    out: list[Pt2D] = []
    for i in range(n):
        t = i / (n - 1)
        out.append(Pt2D(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t))
    return out


def _discretize_arc(start: Pt2D, mid: Pt2D, end: Pt2D, step_deg: float = 15.0
                    ) -> list[Pt2D]:
    """Sample a start/mid/end arc into a straight polyline (for the outline)."""
    conv = _arc_from_three_points(start, mid, end)
    if conv is None:
        return [start, end]
    center, radius, a0, a1 = conv
    sweep = (a1 - a0) % 360.0 or 360.0
    n = max(2, int(math.ceil(sweep / step_deg)))
    pts: list[Pt2D] = []
    for k in range(n + 1):
        t = math.radians(a0 + sweep * k / n)
        pts.append(Pt2D(center.x + radius * math.cos(t),
                        center.y + radius * math.sin(t)))
    return pts


# ---------------------------------------------------------------------------
# Nets
# ---------------------------------------------------------------------------
class _NetTable:
    """Resolves a primitive's ``(net …)`` to a FYPA net index.

    KiCad 6 to 9 boards carry a numbered table, ``(net 3 "GND")`` at the top,
    and primitives refer to it by number (pads also repeat the name). Those
    numbers are kept as FYPA indices, so ``nets[3]`` is KiCad net 3; net 0 is
    KiCad's "unconnected" and maps to :data:`NO_NET`.

    A board may instead name its nets in place - ``(net "GND")``, with no
    table - as newer KiCad versions may write them. Names are then given
    indices as they are met. On a board that does have a table, a name that
    isn't in it is malformed and maps to ``NO_NET`` with a warning.
    """

    def __init__(self, pcb: SNode) -> None:
        names: dict[int, str] = {}
        for n in pcb.nodes("net"):
            num = _int_or_none(n.atom(0), "net number")
            if num is None:
                continue
            names[num] = n.atom(1, "") or ""
        self.numbered = bool(names)
        top = max(names) if names else 0
        self._names: list[str] = [names.get(k, "") for k in range(top + 1)]
        self._by_name: dict[str, int] = {}
        for i, nm in enumerate(self._names):
            if i > 0 and nm and nm not in self._by_name:
                self._by_name[nm] = i

    @property
    def nets(self) -> tuple[RawNet, ...]:
        return tuple(RawNet(nm) for nm in self._names)

    def by_number(self, kicad_no: int | None) -> int:
        if kicad_no is None or kicad_no <= 0 or kicad_no >= len(self._names):
            return NO_NET
        return kicad_no

    def by_name(self, name: str | None, what: str) -> int:
        if not name:
            return NO_NET
        idx = self._by_name.get(name)
        if idx is not None:
            return idx
        if self.numbered:
            log.warning("KiCad: ignoring malformed %s %r", what, name)
            return NO_NET
        self._names.append(name)
        self._by_name[name] = len(self._names) - 1
        return len(self._names) - 1

    def of(self, elem: SNode, what: str) -> int:
        """Net index of *elem*'s ``(net …)`` (a zone's ``net_name`` too)."""
        net = elem.node("net")
        if net is None:
            return self.by_name(elem.s("net_name"), what)
        first = net.atom(0)
        if first is None:
            return NO_NET
        try:
            num = int(first)
        except ValueError:
            return self.by_name(first, what)        # (net "GND")
        idx = self.by_number(num)
        if idx == NO_NET and num > 0:
            # A number past the table: fall back to the name pads carry.
            return self.by_name(net.atom(1) or elem.s("net_name"), what)
        return idx


def _build_nets(pcb: SNode) -> tuple[tuple[RawNet, ...], Callable[[int | None], int]]:
    """``(nets, net_index)`` from the board's numbered net table - see
    :class:`_NetTable`, which the extractor itself uses."""
    table = _NetTable(pcb)
    return table.nets, table.by_number


# ---------------------------------------------------------------------------
# Flags, via protection, unused via pads
# ---------------------------------------------------------------------------
def _flag(elem: SNode, tag: str) -> bool | None:
    """A boolean token in any of KiCad's spellings: ``(tag yes)`` / ``(tag no)``
    / ``(tag none)`` (KiCad 8+), a bare ``(tag)`` or ``tag`` (KiCad 6 / 7,
    meaning yes). ``None`` when absent or ``none``."""
    n = elem.node(tag)
    if n is not None:
        a = n.atom(0)
        if a is None or a in ("yes", "true"):
            return True
        if a in ("no", "false"):
            return False
        return None
    return True if tag in elem.atoms else None


def _front_back(elem: SNode, tag: str) -> tuple[bool | None, bool | None]:
    """``(tag (front yes) (back no))``, or the legacy bare ``(tenting front back)``."""
    n = elem.node(tag)
    if n is None:
        return None, None
    if n.node("front") is not None or n.node("back") is not None:
        return _flag(n, "front"), _flag(n, "back")
    atoms = n.atoms
    return (True if "front" in atoms else None, True if "back" in atoms else None)


def via_ipc4761_type(via: SNode, defaults: SNode | None = None) -> int:
    """KiCad 10's via protection flags as FYPA's IPC-4761 type number (the
    Altium enum - see fypa.altium.loader._IPC4761_TYPE_LABELS): 12 = VII
    fill + cap, 9-11 = V / VI fill (+ cover), 5-8 = III / IV plug (+ cover),
    3-4 = II tent + cover, 1-2 = I tent, 0 = none. "a" (one side) or "b"
    (both) follows the front / back flags.

    KiCad records no fill *material*, so FYPA treats a fill as non-conductive
    unless Settings says otherwise - the same as an Altium fill with no
    material named.

    *defaults* is the board's ``(setup …)``: KiCad 10 writes the board-wide
    protection there, and a via without a token of its own follows it. A
    per-via ``none`` also means "the board default"."""
    def flag(tag: str) -> bool:
        own = _flag(via, tag)
        if own is None and defaults is not None:
            own = _flag(defaults, tag)
        return own is True

    def sides(tag: str) -> list[bool]:
        own = _front_back(via, tag)
        base = (_front_back(defaults, tag) if defaults is not None
                else (None, None))
        return [(o if o is not None else b) is True for o, b in zip(own, base)]

    filled = flag("filling")
    capped = flag("capping")
    cover = sides("covering")
    plug = sides("plugging")
    tent = sides("tenting")
    if filled and capped:
        return 12
    if filled:
        return 11 if all(cover) else 10 if any(cover) else 9
    if any(plug):
        if any(cover):
            return 8 if all(plug) else 7
        return 6 if all(plug) else 5
    if any(cover):
        return 4 if all(cover) else 3
    if any(tent):
        return 2 if all(tent) else 1
    return 0


# How a via treats copper layers it doesn't connect to (KiCad's padstack
# "unconnected layer mode").
VIA_KEEP_ALL = "keep_all"
VIA_REMOVE_ALL = "remove_all"                 # pads only where connected
VIA_REMOVE_KEEP_ENDS = "remove_keep_ends"     # ... plus its start and end layers
VIA_START_END_ONLY = "start_end_only"         # pads on start and end layers only


def via_layer_mode(via: SNode) -> tuple[str, frozenset[str] | None]:
    """``(mode, zone layers)``. *zone layers* are the copper layer names KiCad
    recorded as zone-connected (``zone_layer_connections``), or ``None`` when
    the file doesn't say (then a same-net zone covering the via counts)."""
    zl_node = via.node("zone_layer_connections")
    zone_layers = frozenset(zl_node.atoms) if zl_node is not None else None
    if _flag(via, "start_end_only") is True:
        return VIA_START_END_ONLY, zone_layers
    if _flag(via, "remove_unused_layers") is True:
        keep_ends = _flag(via, "keep_end_layers") is True
        return (VIA_REMOVE_KEEP_ENDS if keep_ends else VIA_REMOVE_ALL), zone_layers
    return VIA_KEEP_ALL, zone_layers


def _unconnected_via_layers(vias: list, modes: list, stackup, tracks, arcs,
                            pads, regions) -> list:
    """Fill ``unconnected_layer_ids`` on vias whose unused pads KiCad removes.

    A layer of the via's span keeps its pad when copper of the via's net meets
    the via there: a track or arc ending on it, a pad over it, or a zone (the
    layers in ``zone_layer_connections`` when the file lists them, else a
    same-net filled zone covering the via). With no pad the drill wall has no
    copper on that layer, which FYPA models as no barrel coupling there.
    """
    if all(m == VIA_KEEP_ALL for m, _ in modes):
        return vias
    from dataclasses import replace

    from shapely.geometry import Point, Polygon
    from shapely.prepared import prep

    order = [row.layer_id for row in stackup]
    name_of = {row.layer_id: row.name for row in stackup}
    zones: dict[tuple[int, int], list] = {}
    for r in regions:
        if r.polygon_index != NO_POLYGON and len(r.outline) >= 3:
            poly = Polygon([(v.pos.x, v.pos.y) for v in r.outline])
            if not poly.is_valid:
                poly = poly.buffer(0)
            zones.setdefault((r.layer_id, r.net_index), []).append(prep(poly))

    def ends(layer: int, net: int):
        for t in tracks:
            if t.layer_id == layer and t.net_index == net:
                yield t.a
                yield t.b
        for a in arcs:
            if a.layer_id == layer and a.net_index == net:
                for ang in (a.start_angle_deg, a.end_angle_deg):
                    r = math.radians(ang)
                    yield Pt2D(a.center.x + a.radius_mm * math.cos(r),
                               a.center.y + a.radius_mm * math.sin(r))

    out = []
    for via, (mode, zone_layers) in zip(vias, modes):
        if mode == VIA_KEEP_ALL or via.layer_start not in order \
                or via.layer_end not in order:
            out.append(via)
            continue
        i0, i1 = sorted((order.index(via.layer_start), order.index(via.layer_end)))
        span = order[i0:i1 + 1]
        ends_ids = {span[0], span[-1]}
        c, r = via.center, max(via.diameter_mm / 2.0, 1e-3)

        def connected(layer: int, _via=via, _zl=zone_layers, _c=c, _r=r) -> bool:
            net = _via.net_index
            if net == NO_NET:
                return False
            if any(math.hypot(q.x - _c.x, q.y - _c.y) <= _r + 1e-6
                   for q in ends(layer, net)):
                return True
            if any(pd.net_index == net
                   and pd.layer_id in (layer, MULTI_LAYER_PAD_LAYER_ID)
                   and math.hypot(pd.center.x - _c.x, pd.center.y - _c.y)
                   <= max(pd.width_mm, pd.height_mm) / 2.0
                   for pd in pads):
                return True
            if _zl is not None:
                return name_of.get(layer) in _zl
            pt = Point(_c.x, _c.y)
            return any(z.contains(pt) for z in zones.get((layer, net), ()))

        if mode == VIA_START_END_ONLY:
            keep = ends_ids
        else:
            keep = {lid for lid in span if connected(lid)}
            if mode == VIA_REMOVE_KEEP_ENDS:
                keep |= ends_ids
        gone = tuple(lid for lid in span if lid not in keep)
        out.append(replace(via, unconnected_layer_ids=gone) if gone else via)
    return out


# ---------------------------------------------------------------------------
# Pad outlines FYPA's pad shapes can't draw (trapezoid, chamfered rectangle)
# ---------------------------------------------------------------------------
def trapezoid_corners(w: float, h: float, dx: float, dy: float
                      ) -> list[tuple[float, float]]:
    """A trapezoid pad's corners in its own frame, as KiCad builds them
    (``PAD::buildEffectiveShape``): ``rect_delta`` is the full delta; half of
    ``dx`` tilts the top and bottom edges, half of ``dy`` the sides."""
    hx, hy, tx, ty = w / 2.0, h / 2.0, dx / 2.0, dy / 2.0
    return [(-hx - ty, hy + tx), (hx + ty, hy - tx),
            (hx - ty, -hy + tx), (-hx + ty, -hy - tx)]


def chamfered_rect_outline(w: float, h: float, chamfer: float, corners: set[str],
                           radius: float = 0.0, step_deg: float = 15.0
                           ) -> list[tuple[float, float]]:
    """A rectangle with the named corners (``top_left`` … in KiCad's Y-down
    pad frame) cut at 45° by *chamfer*, the others rounded by *radius*."""
    hx, hy = w / 2.0, h / 2.0
    pts: list[tuple[float, float]] = []
    # corner name, corner point, (x, y) direction towards the corner
    spec = (("top_right", hx, -hy, 1, -1), ("bottom_right", hx, hy, 1, 1),
            ("bottom_left", -hx, hy, -1, 1), ("top_left", -hx, -hy, -1, -1))
    start = {"top_right": -90.0, "bottom_right": 0.0, "bottom_left": 90.0,
             "top_left": 180.0}
    for name, cx, cy, sx, sy in spec:
        if name in corners and chamfer > 0.0:
            if name in ("top_right", "bottom_left"):
                pts += [(cx - sx * chamfer, cy), (cx, cy - sy * chamfer)]
            else:
                pts += [(cx, cy - sy * chamfer), (cx - sx * chamfer, cy)]
        elif radius > 0.0:
            ox, oy = cx - sx * radius, cy - sy * radius
            n = max(2, int(math.ceil(90.0 / step_deg)))
            a0 = start[name]
            pts += [(ox + radius * math.cos(math.radians(a0 + 90.0 * k / n)),
                     oy + radius * math.sin(math.radians(a0 + 90.0 * k / n)))
                    for k in range(n + 1)]
        else:
            pts.append((cx, cy))
    return pts


# ---------------------------------------------------------------------------
# Copper drawn as graphics (board shapes, footprint shapes, custom pads)
# ---------------------------------------------------------------------------
# Arc / circle facet angle for copper shapes. Finer than the outline's 15° so
# a round copper feature keeps its area (a 10° polygon is within 0.5 %).
_COPPER_ARC_STEP_DEG: float = 10.0

# Graphic element names, in (line, arc, rect, circle, poly) order.
_SHAPE_KINDS = ("line", "arc", "rect", "circle", "poly")
_BOARD_SHAPES = ("gr_line", "gr_arc", "gr_rect", "gr_circle", "gr_poly")
_FOOTPRINT_SHAPES = ("fp_line", "fp_arc", "fp_rect", "fp_circle", "fp_poly")


def _stroke_width(g: SNode) -> float:
    """A shape's line width: ``(stroke (width w))`` (KiCad 7+) or ``(width w)``."""
    stroke = g.node("stroke")
    if stroke is not None and stroke.node("width") is not None:
        return stroke.f("width")
    return g.f("width")


def _is_filled(g: SNode, default: bool) -> bool:
    """``(fill solid)`` / ``(fill yes)`` → filled; ``none`` / ``no`` → not.
    Polygons default to filled (older files omit the token for them)."""
    fill = g.s("fill")
    if fill is None:
        return default
    return fill in ("solid", "yes")


def _pts_with_arcs(pts_node: SNode) -> list[Pt2D]:
    """A ``(pts …)`` list in order, with ``(arc (start)(mid)(end))`` items
    (KiCad 7+ polygons) faceted in place."""
    out: list[Pt2D] = []
    for it in pts_node.nodes():
        if it.tag == "xy":
            out.append(Pt2D(it.f_at(0), it.f_at(1)))
        elif it.tag == "arc":
            seg = _discretize_arc(_xy(it.node("start")), _xy(it.node("mid")),
                                  _xy(it.node("end")), _COPPER_ARC_STEP_DEG)
            if out and seg and math.hypot(out[-1].x - seg[0].x,
                                          out[-1].y - seg[0].y) < 1e-6:
                seg = seg[1:]
            out.extend(seg)
    return out


def _shape_geometry(g: SNode, kind: str
                    ) -> tuple[list[list[Pt2D]], list[tuple[Pt2D, Pt2D]], float]:
    """``(filled rings, stroke segments, stroke width)`` of one graphic shape in
    its container's frame; *kind* is one of :data:`_SHAPE_KINDS`. A filled
    shape's stroke still widens its edge, so with a stroke width its edge comes
    back as segments too."""
    w = _stroke_width(g)
    rings: list[list[Pt2D]] = []
    segs: list[tuple[Pt2D, Pt2D]] = []
    if kind == "line":
        segs.append((_xy(g.node("start")), _xy(g.node("end"))))
    elif kind == "arc":
        pts = _discretize_arc(_xy(g.node("start")), _xy(g.node("mid")),
                              _xy(g.node("end")), _COPPER_ARC_STEP_DEG)
        segs.extend((pts[i], pts[i + 1]) for i in range(len(pts) - 1))
    else:
        if kind == "rect":
            a, b = _xy(g.node("start")), _xy(g.node("end"))
            ring = [a, Pt2D(b.x, a.y), b, Pt2D(a.x, b.y)]
            filled = _is_filled(g, False)
        elif kind == "circle":
            ring = _circle_local_points(_xy(g.node("center")), _xy(g.node("end")),
                                        _COPPER_ARC_STEP_DEG)
            filled = _is_filled(g, False)
        else:
            pts_node = g.node("pts")
            ring = _pts_with_arcs(pts_node) if pts_node is not None else []
            filled = _is_filled(g, True)
        if len(ring) >= 3:
            if filled:
                rings.append(ring)
            segs.extend((ring[i], ring[(i + 1) % len(ring)])
                        for i in range(len(ring)))
    if w <= 0.0:
        segs = []
    return rings, segs, w


def _copper_layer_ids(g: SNode) -> list[int]:
    """Copper layers a shape is drawn on: its ``(layer …)``, or every copper
    name in a ``(layers …)`` list."""
    lid = kicad_layer_to_fypa_id(g.s("layer") or "")
    if lid is not None:
        return [lid]
    layers = g.node("layers")
    if layers is None:
        return []
    return [i for i in (kicad_layer_to_fypa_id(n) for n in layers.atoms)
            if i is not None]


class _CopperShapes:
    """Copper drawn as graphics, collected while the file is read and turned
    into regions (filled shapes) and tracks (stroked edges and lines).

    :attr:`ALL` as a layer stands for every copper layer (a through-hole custom
    pad's primitives); it is expanded once the stackup is known.
    """

    ALL = -1

    def __init__(self) -> None:
        self.items: list[tuple[int, int, list[list[Pt2D]],
                               list[tuple[Pt2D, Pt2D]], float, int]] = []
        self.netless = 0      # footprint shapes whose net couldn't be told

    def add(self, layer_id: int, net: int, rings, segs, width: float,
            component_index: int = -1) -> None:
        if rings or segs:
            self.items.append((layer_id, net, rings, segs, width, component_index))

    def emit(self, copper_ids: list[int], regions: list, tracks: list,
             used: set[int]) -> None:
        for lid, net, rings, segs, w, comp in self.items:
            for layer in (copper_ids if lid == self.ALL else [lid]):
                used.add(layer)
                for ring in rings:
                    regions.append(RawShapeBasedRegion(
                        outline=tuple(RawRegionVertex(pos=q) for q in ring),
                        holes=(), layer_id=layer, net_index=net, kind=0,
                        is_polygon_outline=False, is_keepout=False,
                        is_board_cutout=False))
                for a, b in segs:
                    tracks.append(RawTrack(
                        a=a, b=b, width_mm=w, layer_id=layer, net_index=net,
                        polygon_index=NO_POLYGON, is_polygon_outline=False,
                        component_index=comp, is_keepout=False))


def _shape_touches_pad(rings, segs, width: float, pad: dict) -> bool:
    """True when a footprint shape overlaps the middle of a pad (a disc of the
    pad's smaller half-size) - enough to say whose net the shape carries."""
    from shapely.geometry import LineString, Point, Polygon
    c = pad["center"]
    disc = Point(c.x, c.y).buffer(
        max(1e-3, min(pad["width_mm"], pad["height_mm"]) / 2.0), quad_segs=4)
    for ring in rings:
        poly = Polygon([(q.x, q.y) for q in ring])
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.intersects(disc):
            return True
    for a, b in segs:
        if LineString([(a.x, a.y), (b.x, b.y)]).buffer(
                max(width / 2.0, 1e-3)).intersects(disc):
            return True
    return False


# ---------------------------------------------------------------------------
# Pads / footprints
# ---------------------------------------------------------------------------
# KiCad pad shape token → Altium shape code used by fypa.altium_geometry.
_PAD_SHAPE_CODE = {
    "circle": 1,
    "oval": 1,        # obround: rendered via width != height on the circle path
    "rect": 2,
    "roundrect": 4,
    "trapezoid": 2,   # the rectangle inside it; the full outline is copper
    "custom": 2,      # the anchor pad; its primitives are added as copper
}

# Altium ``ComponentKind`` FYPA uses for a Net Tie (see
# fypa.altium.annotations.NET_TIE_COMPONENT_KINDS): the auto-bridge shorts its
# nets. A KiCad footprint is a net tie when it lists ``net_tie_pad_groups``
# (KiCad 7+) or comes from KiCad's ``NetTie`` library.
_NET_TIE_COMPONENT_KIND: int = 4


def _pad_copper_layer_id(pad: SNode) -> tuple[int | None, bool]:
    """Return ``(fypa_layer_id, is_through_hole)`` for a pad's copper layer.

    ``*.Cu`` (all copper) → Multi-Layer id 74, through-hole. Otherwise the first
    named copper layer. ``(None, _)`` for a pad that touches no copper.
    """
    layers = pad.node("layers")
    if layers is None:
        return None, False
    names = layers.atoms
    if any(n == "*.Cu" for n in names):
        return MULTI_LAYER_PAD_LAYER_ID, True
    for n in names:
        lid = kicad_layer_to_fypa_id(n)
        if lid is not None:
            return lid, False
    return None, False


def _is_net_tie(fp: SNode) -> bool:
    if fp.node("net_tie_pad_groups") is not None:
        return True
    lib = (fp.atom(0, "") or "").split(":", 1)[0]
    return lib.lower() == "nettie"


def footprint_properties(fp: SNode) -> dict[str, str]:
    """A footprint's fields. KiCad 8+ stores Reference / Value as properties;
    KiCad 6 / 7 as ``(fp_text reference "R1" ...)`` / ``(fp_text value ...)``
    with only the custom fields as properties, so the gaps are filled from
    ``fp_text``."""
    properties: dict[str, str] = {}
    for p in fp.nodes("property"):
        key = p.atom(0)
        if key is not None:
            properties[key] = p.atom(1, "") or ""
    for txt in fp.nodes("fp_text"):
        key = {"reference": "Reference", "value": "Value"}.get(txt.atom(0) or "")
        if key and not properties.get(key):
            properties[key] = txt.atom(1, "") or ""
    return properties


def footprint_reference(fp: SNode) -> str:
    """A footprint's designator (see :func:`footprint_properties`)."""
    return footprint_properties(fp).get("Reference", "")


def footprint_is_dnp(fp: SNode) -> bool:
    """``(attr … dnp)`` - a KiCad 8+ footprint marked Do not populate."""
    attr = fp.node("attr")
    return attr is not None and "dnp" in attr.atoms


def _parse_footprint(
    fp: SNode,
    comp_index: int,
    net_index: _NetTable,
    shapes: _CopperShapes | None = None,
) -> tuple[RawPcbComponent, list[RawVia], list[RawHole], list[dict]]:
    """Parse one ``(footprint ...)`` into a component + its pad-derived records.

    Returns the :class:`RawPcbComponent` plus lists of plated-slot vias, npth
    holes and pad dicts to be turned into :class:`RawPad`. Copper the
    footprint draws as graphics, and custom pads' primitives, go to *shapes*.
    """
    if shapes is None:
        shapes = _CopperShapes()
    at = fp.node("at")
    fx, fy = (at.f_at(0), at.f_at(1)) if at else (0.0, 0.0)
    frot = at.f_at(2) if at else 0.0
    layer = fp.s("layer", 0, "F.Cu") or "F.Cu"
    layer_name = "BOTTOM" if layer.startswith("B.") else "TOP"

    properties = footprint_properties(fp)
    designator = properties.get("Reference", "")

    uid = fp.s("uuid") or fp.s("tstamp") or ""
    comp = RawPcbComponent(
        designator=designator,
        center=Pt2D(fx, fy),
        rotation_deg=frot,
        layer_name=layer_name,
        footprint=fp.atom(0, "") or "",
        source_designator=designator,   # no multi-channel re-basing in KiCad
        parameters=properties,
        unique_id=uid,
        component_kind=_NET_TIE_COMPONENT_KIND if _is_net_tie(fp) else 0,
    )

    def fp_xf(q: Pt2D) -> Pt2D:
        rx, ry = _rotate_local(q.x, q.y, frot)
        return Pt2D(fx + rx, fy + ry)

    npth: list[RawHole] = []
    pad_dicts: list[dict] = []
    fp_vias: list[RawVia] = []
    for pad in fp.nodes("pad"):
        ptype = pad.atom(1, "")
        pat = pad.node("at")
        lpx, lpy = (pat.f_at(0), pat.f_at(1)) if pat else (0.0, 0.0)
        # KiCad stores a pad's `at` angle as its *absolute* board orientation
        # (the parent footprint's rotation is already baked in), whereas the pad
        # *position* is footprint-relative and must be rotated by `frot`. So the
        # pad's rotation is `prot` alone - adding `frot` again double-counts the
        # footprint rotation and mis-orients every non-square pad on a rotated
        # footprint (the historic rotation TODO).
        prot = pat.f_at(2) if pat else 0.0
        rx, ry = _rotate_local(lpx, lpy, frot)
        center = Pt2D(fx + rx, fy + ry)

        # drill: (drill d) round, or (drill oval dx dy). f_at() coerces each
        # atom forgivingly (a garbage token → 0.0, not a crash).
        drill = pad.node("drill")
        datoms = drill.atoms if drill is not None else []
        if datoms and datoms[0] == "oval":
            drill_dx = drill.f_at(1)
            drill_dy = drill.f_at(2) if len(datoms) >= 3 else drill_dx
        elif datoms:
            drill_dx = drill_dy = drill.f_at(0)
        else:
            drill_dx = drill_dy = 0.0
        bore_mm = min(drill_dx, drill_dy)          # obround short axis
        major_mm = max(drill_dx, drill_dy)         # obround long axis
        is_slot = bore_mm > 0.0 and (major_mm - bore_mm) > 1e-6
        hole_mm = bore_mm if is_slot else drill_dx
        # Major axis lies along the pad's local X when dx is the long side,
        # else its local Y (a 90° turn); composed with the pad's absolute
        # orientation this is the slot angle in board coordinates.
        slot_rot_rel = 0.0 if drill_dx >= drill_dy else 90.0

        if ptype == "np_thru_hole":
            # Non-plated mechanical hole/slot: surface as RawHole(s) only. A
            # slot stamps a chain so it isn't drawn as one small round hole.
            if is_slot:
                for c in _slot_stamp_centers(
                        center, bore_mm, major_mm, prot + slot_rot_rel):
                    npth.append(RawHole(center=c, diameter_mm=bore_mm))
            elif hole_mm > 0.0:
                npth.append(RawHole(center=center, diameter_mm=hole_mm))
            continue

        lid, is_tht = _pad_copper_layer_id(pad)
        if lid is None:
            continue
        size = pad.node("size")
        w = size.f_at(0) if size else 0.0
        h = size.f_at(1) if size else 0.0
        shape_tok = pad.atom(2, "rect") or "rect"
        shape = _PAD_SHAPE_CODE.get(shape_tok, 2)
        if shape_tok == "custom":
            # The anchor (rect or circle, the pad's size) is the pad; the
            # primitives drawn on it are more copper on the pad's net.
            options = pad.node("options")
            if options is not None and options.s("anchor") == "circle":
                shape = 1
        corner_pct = 0
        if shape == 4:
            rratio = pad.f("roundrect_rratio")
            corner_pct = int(round(max(0.0, min(100.0, rratio * 200.0))))
        pad_net = net_index.of(pad, "pad net")
        # Outlines FYPA's pad shapes can't draw: the pad record becomes a
        # rectangle wholly inside the outline (still the terminal), and the
        # exact outline is added as copper on the pad's net, so the union is
        # the real pad.
        outline: list[tuple[float, float]] | None = None
        rd = pad.node("rect_delta")
        chamfer_ratio = pad.f("chamfer_ratio")
        chamfer_node = pad.node("chamfer")
        if shape_tok == "trapezoid" and rd is not None:
            dx, dy = rd.f_at(0), rd.f_at(1)
            if dx or dy:
                outline = trapezoid_corners(w, h, dx, dy)
                w, h = max(w - abs(dy), 1e-3), max(h - abs(dx), 1e-3)
        elif chamfer_node is not None and chamfer_ratio > 0.0 and chamfer_node.atoms:
            cut = chamfer_ratio * min(w, h)
            radius = (pad.f("roundrect_rratio") * min(w, h)
                      if shape_tok == "roundrect" else 0.0)
            outline = chamfered_rect_outline(w, h, cut, set(chamfer_node.atoms),
                                             radius)
            inset = max(cut, radius)
            w, h = max(w - 2 * inset, 1e-3), max(h - 2 * inset, 1e-3)
            shape, corner_pct = 2, 0
        if outline is not None:
            def out_xf(q: tuple[float, float], _c=center, _r=prot) -> Pt2D:
                rx, ry = _rotate_local(q[0], q[1], _r)
                return Pt2D(_c.x + rx, _c.y + ry)
            shapes.add(_CopperShapes.ALL if is_tht else lid, pad_net,
                       [[out_xf(q) for q in outline]], [], 0.0, comp_index)
        prims = pad.node("primitives")
        if prims is not None:
            def pad_xf(q: Pt2D, _c=center, _r=prot) -> Pt2D:
                rx, ry = _rotate_local(q.x, q.y, _r)
                return Pt2D(_c.x + rx, _c.y + ry)
            pad_layer = _CopperShapes.ALL if is_tht else lid
            for kind, tag in zip(_SHAPE_KINDS, _BOARD_SHAPES):
                for g in prims.nodes(tag):
                    rings, segs, w = _shape_geometry(g, kind)
                    shapes.add(pad_layer, pad_net,
                               [[pad_xf(q) for q in r] for r in rings],
                               [(pad_xf(a), pad_xf(b)) for a, b in segs], w,
                               comp_index)
        pad_dicts.append({
            "center": center,
            "width_mm": w,
            "height_mm": h,
            "hole_mm": hole_mm,
            "shape": shape,
            "rotation_deg": prot,
            "layer_id": lid,
            "net_index": pad_net,
            "designator": pad.atom(0, "") or "",
            "pin_name": pad.s("pinfunction") or "",
            "component_index": comp_index,
            "is_through_hole": is_tht,
            "is_smt": not is_tht,
            "corner_radius_pct": corner_pct,
            # Preserve the slot geometry (obround: hole_shape 2) so the viewer
            # and metadata draw the real bore, not a round hole.
            "hole_shape": 2 if is_slot else 0,
            "slot_length_mm": major_mm if is_slot else 0.0,
            "slot_rotation_deg": slot_rot_rel if is_slot else 0.0,
        })
        # A plated slot must bridge layers across its full length, so stamp a
        # via chain along the major axis (as the Gerber path does). The chain's
        # stamps overlap by design; the pad's own single-circle barrel (added by
        # the loader for the plated TH pad) is one more overlapping stamp at the
        # centre, so this is a strict improvement over the single-circle bridge,
        # not a phantom parallel conductor.
        if is_slot and is_tht:
            for c in _slot_stamp_centers(
                    center, bore_mm, major_mm, prot + slot_rot_rel):
                fp_vias.append(RawVia(
                    center=c,
                    diameter_mm=bore_mm,
                    hole_diameter_mm=bore_mm,
                    layer_start=LAYER_ID_TOP,
                    layer_end=LAYER_ID_BOTTOM,
                    net_index=pad_net,
                ))

    # Copper the footprint draws as graphics (a heatsink tab, a net-tie bridge,
    # a shaped pad extension). It has no net of its own in the file: it takes
    # the net of the pads it touches when they all agree. Anything else -
    # touching no pad, or pads of two nets (a net tie's bridge, whose nets the
    # auto-bridge joins anyway) - is left without a net.
    for kind, tag in zip(_SHAPE_KINDS, _FOOTPRINT_SHAPES):
        for g in fp.nodes(tag):
            for glid in _copper_layer_ids(g):
                rings, segs, w = _shape_geometry(g, kind)
                rings = [[fp_xf(q) for q in r] for r in rings]
                segs = [(fp_xf(a), fp_xf(b)) for a, b in segs]
                touched = {pd["net_index"] for pd in pad_dicts
                           if pd["layer_id"] in (glid, MULTI_LAYER_PAD_LAYER_ID)
                           and pd["net_index"] != NO_NET
                           and _shape_touches_pad(rings, segs, w, pd)}
                net = touched.pop() if len(touched) == 1 else NO_NET
                if net == NO_NET and (rings or segs):
                    shapes.netless += 1
                shapes.add(glid, net, rings, segs, w, comp_index)
    return comp, fp_vias, npth, pad_dicts


# ---------------------------------------------------------------------------
# Stackup
# ---------------------------------------------------------------------------
def _parse_stackup(pcb: SNode, used_copper_ids: set[int]) -> tuple[RawStackupLayer, ...]:
    """Build the copper stackup from ``(setup (stackup ...))`` or a default."""
    setup = pcb.node("setup")
    stack = setup.node("stackup") if setup else None
    if stack is not None:
        rows = _stackup_from_node(stack)
        if rows:
            return rows
    return _default_stackup(used_copper_ids)


def _stackup_from_node(stack: SNode) -> tuple[RawStackupLayer, ...]:
    """Parse copper layers from a KiCad ``(stackup ...)`` node, in board order.

    Dielectric (core / prepreg) thicknesses between two copper layers are folded
    into the ``dielectric_thickness_mm`` of the copper layer above them, with
    Dk / Df thickness-weighted across the plies. Mask, paste and silk entries
    also appear in the stackup (mask with a thickness) but sit outside the
    copper, so they are not dielectric.

    A KiCad dielectric layer can hold several plies, written as repeated
    ``(thickness …)`` / ``(epsilon_r …)`` / ``(loss_tangent …)`` groups split
    by ``addsublayer``; every ply is counted.
    """
    copper: list[tuple[int, str, float]] = []   # (fypa_id, name, copper_mm)
    # Per copper row: [lid, name, cu_mm, diel_mm, dk_weighted, dk_mm, df_weighted, df_mm]
    ordered: list[list] = []
    for layer in stack.nodes("layer"):
        name = layer.atom(0, "") or ""
        ltype = (layer.s("type") or "").lower()
        lid = kicad_layer_to_fypa_id(name)
        if lid is not None and (ltype == "copper" or name.endswith(".Cu")):
            thickness = layer.f("thickness")
            copper.append((lid, name, thickness))
            ordered.append([lid, name, thickness, 0.0, 0.0, 0.0, 0.0, 0.0])
            continue
        if ltype not in ("core", "prepreg") or not ordered:
            continue
        # dielectric: attribute every ply to the copper just above.
        plies = [n.f_at(0) for n in layer.nodes("thickness")]
        dks = [n.f_at(0) for n in layer.nodes("epsilon_r")]
        dfs = [n.f_at(0) for n in layer.nodes("loss_tangent")]
        row = ordered[-1]
        for i, t in enumerate(plies):
            row[3] += t
            if i < len(dks) and dks[i] > 0.0:
                row[4] += dks[i] * t
                row[5] += t
            if i < len(dfs) and dfs[i] > 0.0:
                row[6] += dfs[i] * t
                row[7] += t
    if not copper:
        return ()
    rows: list[RawStackupLayer] = []
    for i, (lid, name, cu, diel, dkw, dkt, dfw, dft) in enumerate(ordered):
        nxt = ordered[i + 1][0] if i + 1 < len(ordered) else 0
        rows.append(RawStackupLayer(
            layer_id=lid,
            name=name,
            copper_thickness_mm=cu or _DEFAULT_COPPER_MM,
            dielectric_thickness_mm=diel if nxt else 0.0,
            next_layer_id=nxt,
            is_plane=False,
            plane_net_name=None,
            mech_enabled=True,
            dielectric_dk=(dkw / dkt) if nxt and dkt > 0.0 else None,
            dielectric_df=(dfw / dft) if nxt and dft > 0.0 else None,
        ))
    return tuple(rows)


def _default_stackup(used_copper_ids: set[int]) -> tuple[RawStackupLayer, ...]:
    """Synthesise a stackup when the board carries none.

    1 oz copper on every used layer; the 1.6 mm nominal board thickness is split
    evenly across the dielectric gaps. Always includes Top and Bottom so a plain
    2-layer board with only tracks/pads still solves.
    """
    ids = set(used_copper_ids) | {LAYER_ID_TOP, LAYER_ID_BOTTOM}
    ordered = sorted(ids)   # 1, 2..31, 32 == top → bottom
    gaps = max(1, len(ordered) - 1)
    diel = _DEFAULT_BOARD_THICKNESS_MM / gaps
    rows: list[RawStackupLayer] = []
    for i, lid in enumerate(ordered):
        nxt = ordered[i + 1] if i + 1 < len(ordered) else 0
        name = ("F.Cu" if lid == LAYER_ID_TOP
                else "B.Cu" if lid == LAYER_ID_BOTTOM
                else f"In{lid - 1}.Cu")
        rows.append(RawStackupLayer(
            layer_id=lid,
            name=name,
            copper_thickness_mm=_DEFAULT_COPPER_MM,
            dielectric_thickness_mm=(diel if nxt else 0.0),
            next_layer_id=nxt,
            is_plane=False,
            plane_net_name=None,
            mech_enabled=True,
        ))
    return tuple(rows)


# ---------------------------------------------------------------------------
# Schematic
# ---------------------------------------------------------------------------
def _parse_schematic(sch_path: Path
                     ) -> tuple[tuple[RawSchComponent, ...], frozenset[str],
                                tuple[str, ...]]:
    """Parse a ``.kicad_sch`` (recursing hierarchical sub-sheets) into symbols.

    Returns ``(symbols, dnp, power nets)``: designator + field parameters +
    pin names per placed symbol, the designators marked Do not populate, and
    the net names power symbols (``power:+3V3`` …) put on the schematic. ``PDN_*``
    directives arrive through the board's footprint fields (KiCad copies symbol
    fields onto footprints on Update PCB from Schematic), so this is a second
    source; it degrades gracefully (empty) if anything is missing. No
    hierarchical net-alias resolution (``compiled_netlist`` stays ``None``).

    A sheet used more than once (or a symbol in a sub-sheet) has a designator
    per use, kept in the symbol's ``(instances …)`` (KiCad 7+) or the root's
    ``(symbol_instances …)`` (KiCad 6) - not in its Reference field, which
    holds only one of them. Each use becomes its own row.
    """
    try:
        root = sexpr.parse_file(sch_path)
    except (OSError, ValueError) as exc:
        log.warning("Could not read KiCad schematic %s: %s", sch_path, exc)
        return (), frozenset(), ()

    # KiCad 6 keeps every instance's reference in one root table, keyed by a
    # path ending in the symbol's uuid.
    root_refs: dict[str, list[str]] = {}
    table = root.node("symbol_instances")
    if table is not None:
        for path in table.nodes("path"):
            uid = (path.atom(0, "") or "").rsplit("/", 1)[-1]
            ref = path.s("reference")
            if uid and ref:
                root_refs.setdefault(uid, []).append(ref)

    out: dict[tuple[str, str], RawSchComponent] = {}
    dnp: set[str] = set()
    power: dict[str, None] = {}
    seen_sheets: set[Path] = set()
    _collect_symbols(root, sch_path, out, dnp, seen_sheets, root_refs, power)
    return tuple(out.values()), frozenset(dnp), tuple(power)


def _symbol_references(sym: SNode, props: dict[str, str],
                       root_refs: dict[str, list[str]]) -> list[str]:
    """Every designator a placed symbol has (one per sheet instance)."""
    refs: list[str] = []
    inst = sym.node("instances")
    if inst is not None:
        for project in inst.nodes("project"):
            for path in project.nodes("path"):
                ref = path.s("reference")
                if ref and ref not in refs:
                    refs.append(ref)
    if not refs:
        refs = list(dict.fromkeys(root_refs.get(sym.s("uuid") or "", [])))
    if not refs and props.get("Reference"):
        refs = [props["Reference"]]
    return refs


def _collect_symbols(root: SNode, path: Path,
                     out: dict[tuple[str, str], RawSchComponent],
                     dnp: set[str], seen: set[Path],
                     root_refs: dict[str, list[str]],
                     power: dict[str, None]) -> None:
    resolved = path.resolve()
    if resolved in seen:
        return
    seen.add(resolved)
    sheet_name = path.name
    for sym in root.nodes("symbol"):
        props: dict[str, str] = {}
        for p in sym.nodes("property"):
            key = p.atom(0)
            if key is not None:
                props[key] = p.atom(1, "") or ""
        pins = tuple(
            pn.atom(0) or "" for pn in sym.nodes("pin") if pn.atom(0)
        )
        if (sym.s("lib_id") or "").lower().startswith("power:"):
            # A power symbol's Value is the net it names (PWR_FLAG is an ERC
            # marker, not a net).
            if props.get("Value") and not (sym.s("lib_id") or "").lower(
                    ).endswith(":pwr_flag"):
                power[props["Value"]] = None
            continue
        for designator in _symbol_references(sym, props, root_refs):
            if not designator or designator.startswith("#"):
                continue   # power / no-connect pseudo-symbols
            if sym.s("dnp") == "yes":
                dnp.add(designator)
            key = (designator, sheet_name)
            prev = out.get(key)
            if prev is not None:
                # Another unit of a multi-unit part: one row, all its pins.
                out[key] = RawSchComponent(
                    designator=designator, schdoc_name=sheet_name,
                    parameters={**props, **prev.parameters},
                    pin_designators=tuple(dict.fromkeys(
                        prev.pin_designators + pins)))
                continue
            out[key] = RawSchComponent(
                designator=designator,
                schdoc_name=sheet_name,
                parameters={**props, "Reference": designator},
                pin_designators=pins,
            )
    # Recurse hierarchical sheets referenced by file name.
    for sheet in root.nodes("sheet"):
        for p in sheet.nodes("property"):
            if p.atom(0) in ("Sheetfile", "Sheet file"):
                sub = path.parent / (p.atom(1, "") or "")
                if sub.exists():
                    try:
                        _collect_symbols(sexpr.parse_file(sub), sub, out, dnp,
                                         seen, root_refs, power)
                    except (OSError, ValueError) as exc:
                        log.warning("Could not read sub-sheet %s: %s", sub, exc)


# ---------------------------------------------------------------------------
# PDN_*_NET values written with the schematic's names
# ---------------------------------------------------------------------------
def _is_net_key(key: str) -> bool:
    k = key.upper()
    return k.startswith("PDN") and k.endswith("NET")


def _resolve_pdn_net_names(pcb_components: list, sch_components: tuple,
                           pads: list, nets: tuple, sch_path: Path | None):
    """Rewrite ``PDN_*_NET`` values that name a net as the schematic does into
    the board's name for it (see :mod:`fypa.kicad.schematic_nets`).

    A value that is already a board net name is left alone, as is one the
    schematic can't place (the annotation parser then reports it as before).
    The schematic is traced only when some value needs it.
    """
    from dataclasses import replace

    board_names = [n.name for n in nets]
    known = set(board_names)

    def unknown(comps) -> bool:
        return any(_is_net_key(k) and v.strip() and v.strip() not in known
                   for c in comps for k, v in (c.parameters or {}).items())

    if sch_path is None or not (unknown(pcb_components) or unknown(sch_components)):
        return pcb_components, sch_components
    from fypa.kicad.schematic_nets import build_schematic_nets, resolve_net_name
    pad_nets: dict[tuple[str, str], int] = {}
    for pd in pads:
        if 0 <= pd.component_index < len(pcb_components) and pd.net_index >= 0:
            des = pcb_components[pd.component_index].designator.upper()
            pad_nets.setdefault((des, pd.designator), pd.net_index)
    sn = build_schematic_nets(sch_path, pad_nets)
    renamed: list[str] = []

    def fix(c):
        params = dict(c.parameters or {})
        changed = False
        for k, v in list(params.items()):
            val = v.strip()
            if not (_is_net_key(k) and val) or val in known:
                continue
            hit = resolve_net_name(sn, c.designator, val, board_names)
            if hit and hit != val:
                params[k] = hit
                changed = True
                renamed.append(f"{c.designator} {k} {val!r} -> {hit!r}")
        return replace(c, parameters=params) if changed else c

    pcb_out = [fix(c) for c in pcb_components]
    sch_out = tuple(fix(c) for c in sch_components)
    if renamed:
        log.info("KiCad: %d PDN net name(s) given as the schematic names them, "
                 "read as the board's: %s", len(renamed),
                 "; ".join(dict.fromkeys(renamed)))
    return pcb_out, sch_out


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------
def extract_kicad_project(
    kicad_pcb_path: str | Path,
    sch_path: str | Path | None = None,
) -> ExtractedProject:
    """Read a KiCad board (+ optional schematic) into an :class:`ExtractedProject`.

    *kicad_pcb_path* may also be the project's ``.kicad_pro`` (see
    :func:`resolve_kicad_board`). *sch_path* defaults to a sibling
    ``<board stem>.kicad_sch`` if it exists.
    """
    pcb_path = resolve_kicad_board(kicad_pcb_path)
    pcb = sexpr.parse_file(pcb_path)
    if pcb.tag != "kicad_pcb":
        raise ValueError(f"{pcb_path} is not a .kicad_pcb (root tag {pcb.tag!r})")
    load_notes: list[str] = []
    note = board_format_note(_int_or_none(pcb.s("version"), "board version"))
    if note:
        log.warning("%s", note)
        load_notes.append(note)

    nets = _NetTable(pcb)
    shapes = _CopperShapes()
    used_copper_ids: set[int] = set()

    # --- tracks (segments) & arcs ---
    tracks: list[RawTrack] = []
    for seg in pcb.nodes("segment"):
        lid = kicad_layer_to_fypa_id(seg.s("layer") or "")
        if lid is None:
            continue
        used_copper_ids.add(lid)
        tracks.append(RawTrack(
            a=_xy(seg.node("start")),
            b=_xy(seg.node("end")),
            width_mm=seg.f("width"),
            layer_id=lid,
            net_index=nets.of(seg, "segment net"),
            polygon_index=NO_POLYGON,
            is_polygon_outline=False,
            component_index=-1,
            is_keepout=False,
        ))

    arcs: list[RawArc] = []
    for arc in pcb.nodes("arc"):
        lid = kicad_layer_to_fypa_id(arc.s("layer") or "")
        if lid is None:
            continue
        conv = _arc_from_three_points(
            _xy(arc.node("start")), _xy(arc.node("mid")), _xy(arc.node("end")))
        if conv is None:
            continue
        center, radius, a0, a1 = conv
        used_copper_ids.add(lid)
        arcs.append(RawArc(
            center=center,
            radius_mm=radius,
            start_angle_deg=a0,
            end_angle_deg=a1,
            width_mm=arc.f("width"),
            layer_id=lid,
            net_index=nets.of(arc, "arc net"),
            is_keepout=False,
        ))

    # --- vias ---
    vias: list[RawVia] = []
    via_modes: list[tuple[str, frozenset[str] | None]] = []
    for via in pcb.nodes("via"):
        layers = via.node("layers")
        lnames = layers.atoms if layers else []
        ls = kicad_layer_to_fypa_id(lnames[0]) if lnames else LAYER_ID_TOP
        le = kicad_layer_to_fypa_id(lnames[-1]) if lnames else LAYER_ID_BOTTOM
        vias.append(RawVia(
            center=_xy(via.node("at")),
            diameter_mm=via.f("size"),
            hole_diameter_mm=via.f("drill"),
            layer_start=ls if ls is not None else LAYER_ID_TOP,
            layer_end=le if le is not None else LAYER_ID_BOTTOM,
            net_index=nets.of(via, "via net"),
            ipc4761_via_type=via_ipc4761_type(via, pcb.node("setup")),
        ))
        via_modes.append(via_layer_mode(via))

    # --- zones → shape-based regions (copper pours) ---
    shape_based_regions: list[RawShapeBasedRegion] = []
    unfilled_nets: list[str] = []
    # Each copper zone is one pour: its filled polygons share a
    # ``polygon_index`` (the zone's ordinal), which is how FYPA tells pour
    # copper from fixed copper (What-If via punching, picking a pour).
    for zone_no, zone in enumerate(pcb.nodes("zone")):
        if zone.node("keepout") is not None:
            continue   # rule area: never filled, carries no copper
        znet = nets.of(zone, "zone net")
        filled = list(zone.nodes("filled_polygon"))
        if not filled:
            unfilled_nets.append(zone.s("net_name") or "(no net)")
            continue
        for fpoly in filled:
            lid = kicad_layer_to_fypa_id(fpoly.s("layer") or zone.s("layer") or "")
            if lid is None:
                continue
            pts_node = fpoly.node("pts")
            if pts_node is None:
                continue
            outline = tuple(
                RawRegionVertex(pos=Pt2D(xy.f_at(0), xy.f_at(1)))
                for xy in pts_node.nodes("xy")
            )
            if len(outline) < 3:
                continue
            used_copper_ids.add(lid)
            shape_based_regions.append(RawShapeBasedRegion(
                outline=outline,
                holes=(),
                layer_id=lid,
                net_index=znet,
                kind=0,
                is_polygon_outline=False,
                is_keepout=False,
                is_board_cutout=False,
                polygon_index=zone_no,
            ))

    # --- footprints → components + pads + npth holes ---
    pcb_components: list[RawPcbComponent] = []
    pads: list[RawPad] = []
    npth_holes: list[RawHole] = []
    dnp: set[str] = set()
    for fp in pcb.nodes("footprint"):
        idx = len(pcb_components)
        comp, fp_vias, npth, pad_dicts = _parse_footprint(fp, idx, nets, shapes)
        pcb_components.append(comp)
        if footprint_is_dnp(fp) and comp.designator:
            dnp.add(comp.designator)
        npth_holes.extend(npth)
        vias.extend(fp_vias)   # plated-slot barrel chains, if any
        via_modes.extend((VIA_KEEP_ALL, None) for _ in fp_vias)
        for pd in pad_dicts:
            if pd["layer_id"] not in (MULTI_LAYER_PAD_LAYER_ID,):
                used_copper_ids.add(pd["layer_id"])
            pads.append(RawPad(**pd))
    if any(p.is_through_hole for p in pads) or vias:
        used_copper_ids |= {LAYER_ID_TOP, LAYER_ID_BOTTOM}

    if unfilled_nets:
        shown = ", ".join(sorted(set(unfilled_nets)))
        msg = (f"{len(unfilled_nets)} KiCad zone(s) not filled ({shown}) - their "
               "copper is missing. Run Edit > Fill All Zones in KiCad, save, "
               "and reload")
        log.warning("%s", msg)
        load_notes.append(msg)

    # --- copper drawn as board graphics (gr_* on a copper layer) ---
    for kind, tag in zip(_SHAPE_KINDS, _BOARD_SHAPES):
        for g in pcb.nodes(tag):
            for glid in _copper_layer_ids(g):
                rings, segs, w = _shape_geometry(g, kind)
                shapes.add(glid, nets.of(g, "shape net") if g.node("net")
                           else NO_NET, rings, segs, w)

    # --- stackup ---
    for lid, *_ in shapes.items:
        if lid != _CopperShapes.ALL:
            used_copper_ids.add(lid)
    stackup = _parse_stackup(pcb, used_copper_ids)
    shapes.emit([row.layer_id for row in stackup], shape_based_regions,
                tracks, used_copper_ids)
    vias = _unconnected_via_layers(vias, via_modes, stackup, tracks, arcs,
                                   pads, shape_based_regions)
    if shapes.netless:
        log.info("KiCad: %d footprint copper shape(s) touch no pad, or pads of "
                 "more than one net, and are read without a net",
                 shapes.netless)

    # --- board outline (Edge.Cuts) ---
    board_outline = _extract_board_outline(pcb)

    # --- schematic (optional) ---
    sch_components: tuple[RawSchComponent, ...] = ()
    power_nets: tuple[str, ...] = ()
    if sch_path is None:
        candidate = pcb_path.with_suffix(".kicad_sch")
        if candidate.exists():
            sch_path = candidate
    if sch_path is not None:
        sch_components, sch_dnp, power_nets = _parse_schematic(Path(sch_path))
        dnp |= sch_dnp
    on_board = {c.designator for c in pcb_components}
    not_fitted = tuple(sorted(d for d in dnp if d in on_board))
    pcb_components, sch_components = _resolve_pdn_net_names(
        pcb_components, sch_components, pads, nets.nets,
        Path(sch_path) if sch_path is not None else None)

    log.info(
        "KiCad extract: %d tracks, %d arcs, %d vias, %d zones→regions, "
        "%d pads, %d components, %d nets, %d stackup layers, %d sch symbols",
        len(tracks), len(arcs), len(vias), len(shape_based_regions),
        len(pads), len(pcb_components), len(nets.nets), len(stackup),
        len(sch_components))

    return _to_fypa_frame(ExtractedProject(
        prjpcb_path=pcb_path,
        pcbdoc_path=pcb_path,
        tracks=tuple(tracks),
        arcs=tuple(arcs),
        vias=tuple(vias),
        pads=tuple(pads),
        regions=(),
        shape_based_regions=tuple(shape_based_regions),
        fills=(),
        pcb_components=tuple(pcb_components),
        nets=nets.nets,
        stackup=stackup,
        sch_components=sch_components,
        compiled_netlist=None,
        board_origin_mm=Pt2D(0.0, 0.0),
        board_outline=board_outline,
        texts=(),
        npth_holes=tuple(npth_holes),
        load_notes=tuple(load_notes),
        not_fitted_designators=not_fitted,
        power_port_nets=power_nets,
    ))


def _flip_pt(p: Pt2D) -> Pt2D:
    return Pt2D(p.x, -p.y)


def _flip_arc_angles(a0: float, a1: float) -> tuple[float, float]:
    """A CCW sweep ``a0 → a1`` mirrored in X is the CCW sweep ``-a1 → -a0``."""
    return -a1, -a0


def _to_fypa_frame(proj: ExtractedProject) -> ExtractedProject:
    """Mirror a KiCad-frame (Y-down) project into FYPA's Y-up frame.

    Everything above is computed in KiCad's own file coordinates (Y grows down
    the page), which is where footprint-local offsets compose correctly. FYPA,
    like Altium and Gerber, works Y-up, so this last pass maps every point
    ``(x, y) → (x, -y)``. Without it the board is drawn upside down, and any pad
    at an angle other than a multiple of 90° is turned the wrong way (a mirror
    reverses rotation).

    Angles need no change: a KiCad angle is counter-clockwise *as seen on
    screen*, which in a Y-up frame is the ordinary counter-clockwise angle FYPA
    uses for ``rotation_deg``. Arcs are swept CCW from start to end angle, so
    their endpoints swap (see :func:`_flip_arc_angles`).
    """
    from dataclasses import replace

    def vtx(v: RawRegionVertex) -> RawRegionVertex:
        if not v.is_arc:
            return replace(v, pos=_flip_pt(v.pos))
        a0, a1 = _flip_arc_angles(v.start_angle_deg, v.end_angle_deg)
        return replace(v, pos=_flip_pt(v.pos), center=_flip_pt(v.center),
                       start_angle_deg=a0, end_angle_deg=a1)

    def arc(a: RawArc) -> RawArc:
        a0, a1 = _flip_arc_angles(a.start_angle_deg, a.end_angle_deg)
        return replace(a, center=_flip_pt(a.center),
                       start_angle_deg=a0, end_angle_deg=a1)

    return replace(
        proj,
        tracks=tuple(replace(t, a=_flip_pt(t.a), b=_flip_pt(t.b))
                     for t in proj.tracks),
        arcs=tuple(arc(a) for a in proj.arcs),
        vias=tuple(replace(v, center=_flip_pt(v.center)) for v in proj.vias),
        pads=tuple(replace(p, center=_flip_pt(p.center)) for p in proj.pads),
        shape_based_regions=tuple(
            replace(r, outline=tuple(vtx(v) for v in r.outline),
                    holes=tuple(tuple(_flip_pt(p) for p in h) for h in r.holes))
            for r in proj.shape_based_regions),
        pcb_components=tuple(replace(c, center=_flip_pt(c.center))
                             for c in proj.pcb_components),
        board_outline=tuple(_flip_pt(p) for p in proj.board_outline),
        npth_holes=tuple(replace(h, center=_flip_pt(h.center))
                         for h in proj.npth_holes),
    )


def _on_edge_cuts(node: SNode) -> bool:
    return (node.s("layer") or "") == "Edge.Cuts"


def _circle_local_points(center: Pt2D, edge: Pt2D, step_deg: float = 15.0
                         ) -> list[Pt2D]:
    """Discretise a circle (given centre + a point on it) into a closed loop."""
    r = math.hypot(edge.x - center.x, edge.y - center.y)
    if r <= 0.0:
        return []
    n = max(8, int(math.ceil(360.0 / step_deg)))
    return [Pt2D(center.x + r * math.cos(2.0 * math.pi * k / n),
                 center.y + r * math.sin(2.0 * math.pi * k / n))
            for k in range(n)]


def _outline_segments_from(
    container: SNode,
    tags: tuple[str, str, str, str, str],
    xf,
    segments: list[tuple[Pt2D, Pt2D]],
) -> int:
    """Append *container*'s ``Edge.Cuts`` graphics to *segments*; return count.

    *tags* is the ``(line, arc, rect, circle, poly)`` element names - ``gr_*``
    for board graphics, ``fp_*`` for graphics drawn inside a footprint. *xf*
    maps a raw point to board coordinates (identity for board graphics; the
    footprint place+rotate transform for ``fp_*``). Shapes are built in the
    container's own frame and transformed vertex-by-vertex, so a rectangle /
    polygon on a rotated footprint stays correct.
    """
    ln, ar, rc, ci, po = tags
    n = 0
    for g in container.nodes(ln):
        if not _on_edge_cuts(g):
            continue
        n += 1
        segments.append((xf(_xy(g.node("start"))), xf(_xy(g.node("end")))))
    for g in container.nodes(ar):
        if not _on_edge_cuts(g):
            continue
        n += 1
        pts = [xf(p) for p in _discretize_arc(
            _xy(g.node("start")), _xy(g.node("mid")), _xy(g.node("end")))]
        segments.extend((pts[i], pts[i + 1]) for i in range(len(pts) - 1))
    for g in container.nodes(rc):
        if not _on_edge_cuts(g):
            continue
        n += 1
        s, e = _xy(g.node("start")), _xy(g.node("end"))
        local = [s, Pt2D(e.x, s.y), e, Pt2D(s.x, e.y)]
        c = [xf(p) for p in local]
        segments.extend((c[i], c[(i + 1) % 4]) for i in range(4))
    for g in container.nodes(ci):
        if not _on_edge_cuts(g):
            continue
        n += 1
        local = _circle_local_points(_xy(g.node("center")), _xy(g.node("end")))
        c = [xf(p) for p in local]
        if len(c) >= 3:
            segments.extend((c[i], c[(i + 1) % len(c)]) for i in range(len(c)))
    for g in container.nodes(po):
        if not _on_edge_cuts(g):
            continue
        n += 1
        pts_node = g.node("pts")
        if pts_node is None:
            continue
        local = [Pt2D(xy.f_at(0), xy.f_at(1)) for xy in pts_node.nodes("xy")]
        c = [xf(p) for p in local]
        if len(c) >= 2:
            segments.extend((c[i], c[(i + 1) % len(c)]) for i in range(len(c)))
    return n


def _extract_board_outline(pcb: SNode) -> tuple[Pt2D, ...]:
    """Stitch the ``Edge.Cuts`` graphics into a board-outline polyline.

    Handles board-level ``gr_line`` / ``gr_arc`` / ``gr_rect`` / ``gr_circle``
    / ``gr_poly`` (a rectangle- or circle-tool board is very common and would
    otherwise yield an empty outline) plus any ``Edge.Cuts`` graphics drawn
    inside footprints (``fp_*``), transformed into board coordinates. Warns when
    Edge.Cuts carries graphics but they don't close into a ring.
    """
    segments: list[tuple[Pt2D, Pt2D]] = []
    n_graphics = _outline_segments_from(
        pcb, ("gr_line", "gr_arc", "gr_rect", "gr_circle", "gr_poly"),
        lambda p: p, segments)

    for fp in pcb.nodes("footprint"):
        at = fp.node("at")
        fx, fy = (at.f_at(0), at.f_at(1)) if at else (0.0, 0.0)
        frot = at.f_at(2) if at else 0.0

        def xf(p: Pt2D, _fx=fx, _fy=fy, _fr=frot) -> Pt2D:
            rx, ry = _rotate_local(p.x, p.y, _fr)
            return Pt2D(_fx + rx, _fy + ry)

        n_graphics += _outline_segments_from(
            fp, ("fp_line", "fp_arc", "fp_rect", "fp_circle", "fp_poly"),
            xf, segments)

    outline = _stitch_outline(segments)
    closed = (len(outline) >= 4
              and (outline[0].x - outline[-1].x) ** 2
              + (outline[0].y - outline[-1].y) ** 2 <= _OUTLINE_STITCH_TOL_MM ** 2)
    if n_graphics and not closed:
        log.warning(
            "KiCad: Edge.Cuts has %d graphic(s) but they don't form a closed "
            "board outline - the board contour may be incomplete.", n_graphics)
    return outline
