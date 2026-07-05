"""KiCAD ``.kicad_pcb`` (+ optional ``.kicad_sch``) → :class:`ExtractedProject`.

Adapts a KiCAD 9 board (and, when present, its schematic) into the same frozen
:class:`~fypa.altium.extract.ExtractedProject` the Altium and Gerber paths
produce, so every downstream stage works unchanged. Parsing is done by the
self-contained reader in :mod:`fypa.kicad.sexpr`.

What maps where:

* copper **tracks / arcs / vias** ← ``(segment)`` / ``(arc)`` / ``(via)``;
* **pads** ← footprint ``(pad ...)`` (SMD on its copper layer, through-hole on
  layer id 74 = Multi-Layer);
* copper **pours** ← ``(zone ...)`` filled polygons, one
  :class:`RawShapeBasedRegion` per ``(filled_polygon)`` — mirroring the Gerber
  path. An **unfilled** zone (no ``filled_polygon``) is skipped with a warning;
* **components** ← ``(footprint ...)``; crucially, ``parameters`` = the
  footprint's custom ``(property "KEY" "VAL")`` fields — this is where the
  ``PDN_*`` directives live (KiCAD 7+ syncs schematic symbol fields down to
  footprint properties), so :func:`fypa.altium.annotations.parse_annotations`
  picks them up as PCB-sourced directives with no changes;
* **nets** ← the board's ``(net N "name")`` table. KiCAD net numbers are used
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
Coordinates are already millimetres, Y-down — no scale or flip is applied.
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


def _int_or_none(value: str | None, what: str) -> int | None:
    """Coerce a KiCAD token to ``int``, warning + returning ``None`` on garbage.

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
        log.warning("KiCAD: ignoring malformed %s %r", what, value)
        return None


def kicad_layer_to_fypa_id(name: str) -> int | None:
    """Map a KiCAD copper-layer name to FYPA's integer layer id.

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

    KiCAD angles are counter-clockwise on screen, but the file is Y-down, so a
    positive KiCAD angle is clockwise in raw (x-right, y-down) coordinates —
    hence the ``-deg`` in the standard rotation matrix::

        gx = px·cos(-θ) - py·sin(-θ)
        gy = px·sin(-θ) + py·cos(-θ)

    NOTE: rotation sign is the top field-verification risk for KiCAD import; it
    is exercised only by rotated footprints (the bundled example uses 0°).
    """
    a = math.radians(-deg)
    ca, sa = math.cos(a), math.sin(a)
    return (px * ca - py * sa, px * sa + py * ca)


def _arc_from_three_points(
    start: Pt2D, mid: Pt2D, end: Pt2D
) -> tuple[Pt2D, float, float, float] | None:
    """Convert a KiCAD start/mid/end arc to ``(center, radius, a_start, a_end)``.

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
        # mid is on the far side of the CCW start→end arc — traverse the
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
    apart so the stamped circles overlap into a continuous slot — mirroring the
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
def _build_nets(pcb: SNode) -> tuple[tuple[RawNet, ...], Callable[[int | None], int]]:
    """Build the ``nets`` tuple and a ``net_index(kicad_no) -> fypa_index`` fn.

    FYPA uses the KiCAD net number directly as the tuple index (net 0 kept as an
    empty-name placeholder so indices align). The returned function maps a raw
    KiCAD net number to a primitive's ``net_index``, collapsing net 0 (KiCAD's
    "unconnected") to :data:`NO_NET`.
    """
    names: dict[int, str] = {}
    for n in pcb.nodes("net"):
        num = _int_or_none(n.atom(0), "net number")
        if num is None:
            continue
        names[num] = n.atom(1, "") or ""
    max_no = max(names) if names else 0
    nets = tuple(RawNet(names.get(k, "")) for k in range(max_no + 1))

    def net_index(kicad_no: int | None) -> int:
        if kicad_no is None or kicad_no <= 0 or kicad_no > max_no:
            return NO_NET
        return kicad_no

    return nets, net_index


def _pad_net_no(pad: SNode) -> int | None:
    net = pad.node("net")
    return _int_or_none(net.atom(0), "pad net") if net is not None else None


# ---------------------------------------------------------------------------
# Pads / footprints
# ---------------------------------------------------------------------------
# KiCAD pad shape token → Altium shape code used by fypa.altium_geometry.
_PAD_SHAPE_CODE = {
    "circle": 1,
    "oval": 1,        # obround: rendered via width != height on the circle path
    "rect": 2,
    "roundrect": 4,
    "trapezoid": 2,   # approximated by its bounding rectangle in v1
    "custom": 2,      # approximated by its bounding rectangle in v1
}


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


def _parse_footprint(
    fp: SNode,
    comp_index: int,
    net_index,
) -> tuple[RawPcbComponent, list[RawVia], list[RawHole], list[dict]]:
    """Parse one ``(footprint ...)`` into a component + its pad-derived records.

    Returns the :class:`RawPcbComponent` plus lists of (npth holes) and pad
    dicts to be turned into :class:`RawPad`. (Vias list is always empty here —
    board vias are top-level — but kept for signature symmetry.)
    """
    at = fp.node("at")
    fx, fy = (at.f_at(0), at.f_at(1)) if at else (0.0, 0.0)
    frot = at.f_at(2) if at else 0.0
    layer = fp.s("layer", 0, "F.Cu") or "F.Cu"
    layer_name = "BOTTOM" if layer.startswith("B.") else "TOP"

    properties: dict[str, str] = {}
    for p in fp.nodes("property"):
        key = p.atom(0)
        val = p.atom(1, "")
        if key is not None:
            properties[key] = val or ""
    designator = properties.get("Reference", "")

    uid = fp.s("uuid") or fp.s("tstamp") or ""
    comp = RawPcbComponent(
        designator=designator,
        center=Pt2D(fx, fy),
        rotation_deg=frot,
        layer_name=layer_name,
        footprint=fp.atom(0, "") or "",
        source_designator=designator,   # no multi-channel re-basing in KiCAD
        parameters=properties,
        unique_id=uid,
    )

    npth: list[RawHole] = []
    pad_dicts: list[dict] = []
    fp_vias: list[RawVia] = []
    for pad in fp.nodes("pad"):
        ptype = pad.atom(1, "")
        pat = pad.node("at")
        lpx, lpy = (pat.f_at(0), pat.f_at(1)) if pat else (0.0, 0.0)
        # KiCAD stores a pad's `at` angle as its *absolute* board orientation
        # (the parent footprint's rotation is already baked in), whereas the pad
        # *position* is footprint-relative and must be rotated by `frot`. So the
        # pad's rotation is `prot` alone — adding `frot` again double-counts the
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
        corner_pct = 0
        if shape == 4:
            rratio = pad.f("roundrect_rratio")
            corner_pct = int(round(max(0.0, min(100.0, rratio * 200.0))))
        pad_net = net_index(_pad_net_no(pad))
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
    """Parse copper layers from a KiCAD ``(stackup ...)`` node, in board order.

    Dielectric (core / prepreg) thicknesses between two copper layers are folded
    into the ``dielectric_thickness_mm`` of the copper layer above them.
    """
    copper: list[tuple[int, str, float]] = []   # (fypa_id, name, copper_mm)
    ordered: list[list] = []   # rows we will finalise with next_layer_id
    for layer in stack.nodes("layer"):
        name = layer.atom(0, "") or ""
        ltype = (layer.s("type") or "").lower()
        thickness = layer.f("thickness")
        lid = kicad_layer_to_fypa_id(name)
        if lid is not None and (ltype == "copper" or name.endswith(".Cu")):
            copper.append((lid, name, thickness))
            ordered.append([lid, name, thickness, 0.0])   # dielectric filled below
        else:
            # dielectric / core / prepreg: attribute to the copper just above.
            if ordered:
                ordered[-1][3] += thickness
    if not copper:
        return ()
    rows: list[RawStackupLayer] = []
    for i, (lid, name, cu, diel) in enumerate(ordered):
        nxt = ordered[i + 1][0] if i + 1 < len(ordered) else 0
        rows.append(RawStackupLayer(
            layer_id=lid,
            name=name,
            copper_thickness_mm=cu or _DEFAULT_COPPER_MM,
            dielectric_thickness_mm=diel,
            next_layer_id=nxt,
            is_plane=False,
            plane_net_name=None,
            mech_enabled=True,
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
def _parse_schematic(sch_path: Path) -> tuple[RawSchComponent, ...]:
    """Parse a ``.kicad_sch`` (recursing hierarchical sub-sheets) into symbols.

    v1 scope: designator + field parameters + pin names. This mainly gives
    symbol-field *authority* — PDN_* directives already arrive via PCB footprint
    properties — so it degrades gracefully (an empty tuple) if anything is
    missing. No hierarchical net-alias resolution (``compiled_netlist`` stays
    ``None``).
    """
    try:
        root = sexpr.parse_file(sch_path)
    except (OSError, ValueError) as exc:
        log.warning("Could not read KiCAD schematic %s: %s", sch_path, exc)
        return ()

    out: list[RawSchComponent] = []
    seen_sheets: set[Path] = set()
    _collect_symbols(root, sch_path, out, seen_sheets)
    return tuple(out)


def _collect_symbols(root: SNode, path: Path,
                     out: list[RawSchComponent], seen: set[Path]) -> None:
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
        designator = props.get("Reference", "")
        if not designator or designator.startswith("#"):
            continue   # power / no-connect pseudo-symbols
        pins = tuple(
            pn.atom(0) or "" for pn in sym.nodes("pin") if pn.atom(0)
        )
        out.append(RawSchComponent(
            designator=designator,
            schdoc_name=sheet_name,
            parameters=props,
            pin_designators=pins,
        ))
    # Recurse hierarchical sheets referenced by file name.
    for sheet in root.nodes("sheet"):
        for p in sheet.nodes("property"):
            if p.atom(0) in ("Sheetfile", "Sheet file"):
                sub = path.parent / (p.atom(1, "") or "")
                if sub.exists():
                    try:
                        _collect_symbols(sexpr.parse_file(sub), sub, out, seen)
                    except (OSError, ValueError) as exc:
                        log.warning("Could not read sub-sheet %s: %s", sub, exc)


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------
def extract_kicad_project(
    kicad_pcb_path: str | Path,
    sch_path: str | Path | None = None,
) -> ExtractedProject:
    """Read a KiCAD board (+ optional schematic) into an :class:`ExtractedProject`.

    *sch_path* defaults to a sibling ``<board stem>.kicad_sch`` if it exists.
    """
    pcb_path = Path(kicad_pcb_path)
    pcb = sexpr.parse_file(pcb_path)
    if pcb.tag != "kicad_pcb":
        raise ValueError(f"{pcb_path} is not a .kicad_pcb (root tag {pcb.tag!r})")

    nets, net_index = _build_nets(pcb)
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
            net_index=net_index(_int_or_none(seg.s("net"), "segment net")),
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
            net_index=net_index(_int_or_none(arc.s("net"), "arc net")),
            is_keepout=False,
        ))

    # --- vias ---
    vias: list[RawVia] = []
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
            net_index=net_index(_int_or_none(via.s("net"), "via net")),
        ))

    # --- zones → shape-based regions (copper pours) ---
    shape_based_regions: list[RawShapeBasedRegion] = []
    for zone in pcb.nodes("zone"):
        znet = net_index(_int_or_none(zone.s("net"), "zone net"))
        filled = list(zone.nodes("filled_polygon"))
        if not filled:
            nm = zone.s("net_name") or "?"
            log.warning(
                "KiCAD zone on net %r has no filled_polygon — run 'Fill All "
                "Zones' in KiCAD before export, or its copper won't be modelled.",
                nm)
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
            ))

    # --- footprints → components + pads + npth holes ---
    pcb_components: list[RawPcbComponent] = []
    pads: list[RawPad] = []
    npth_holes: list[RawHole] = []
    for fp in pcb.nodes("footprint"):
        idx = len(pcb_components)
        comp, fp_vias, npth, pad_dicts = _parse_footprint(fp, idx, net_index)
        pcb_components.append(comp)
        npth_holes.extend(npth)
        vias.extend(fp_vias)   # plated-slot barrel chains, if any
        for pd in pad_dicts:
            if pd["layer_id"] not in (MULTI_LAYER_PAD_LAYER_ID,):
                used_copper_ids.add(pd["layer_id"])
            pads.append(RawPad(**pd))
    if any(p.is_through_hole for p in pads) or vias:
        used_copper_ids |= {LAYER_ID_TOP, LAYER_ID_BOTTOM}

    # --- stackup ---
    stackup = _parse_stackup(pcb, used_copper_ids)

    # --- board outline (Edge.Cuts) ---
    board_outline = _extract_board_outline(pcb)

    # --- schematic (optional) ---
    sch_components: tuple[RawSchComponent, ...] = ()
    if sch_path is None:
        candidate = pcb_path.with_suffix(".kicad_sch")
        if candidate.exists():
            sch_path = candidate
    if sch_path is not None:
        sch_components = _parse_schematic(Path(sch_path))

    log.info(
        "KiCAD extract: %d tracks, %d arcs, %d vias, %d zones→regions, "
        "%d pads, %d components, %d nets, %d stackup layers, %d sch symbols",
        len(tracks), len(arcs), len(vias), len(shape_based_regions),
        len(pads), len(pcb_components), len(nets), len(stackup),
        len(sch_components))

    return ExtractedProject(
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
        nets=nets,
        stackup=stackup,
        sch_components=sch_components,
        compiled_netlist=None,
        board_origin_mm=Pt2D(0.0, 0.0),
        board_outline=board_outline,
        texts=(),
        npth_holes=tuple(npth_holes),
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

    *tags* is the ``(line, arc, rect, circle, poly)`` element names — ``gr_*``
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
            "KiCAD: Edge.Cuts has %d graphic(s) but they don't form a closed "
            "board outline — the board contour may be incomplete.", n_graphics)
    return outline
