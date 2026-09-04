"""Altium project extractor for FYPA.

Loads a `.PrjPcb` via altium_monkey and produces typed, mm-normalised raw record
dataclasses for downstream geometry meshing (fypa.altium_geometry) and FEM annotation
parsing (fypa.altium.annotations).

Conventions
-----------
- All spatial values are millimetres (mm). All angles are degrees.
- Layer identifiers are the integer Altium `layer_id` (1=Top, 32=Bottom on the
  classic numbering; the same integer that appears in `pcb.board.layer_stackup`
  and on each PCB primitive's `.layer` field).
- Net identifiers are integer indices into `ExtractedProject.nets`. Use the
  module-level sentinel `NO_NET = -1` for unassigned. `NO_POLYGON = 65535` is
  the sentinel returned by altium_monkey on tracks that are not part of a
  polygon outline.
- A primitive owned by a polygon pour inherits that polygon's net when it
  carries none of its own — Altium keeps the net on the `Polygons6` record for
  poured copper (regions for a solid fill, tracks/arcs for a hatched one).
  Tracks and arcs also record whether that parent pour is hatched, because a
  hatched pour's perimeter is real copper rather than boundary artwork.

Public entry: :func:`extract_project`.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from altium_monkey import AltiumDesign

if TYPE_CHECKING:
    from altium_monkey.altium_netlist_model import Netlist


log = logging.getLogger(__name__)


MIL_TO_MM: float = 0.0254
REGION_RAW_PER_MIL: float = 10000.0
NO_POLYGON: int = 65535
NO_NET: int = -1

_MIL_STRING_RE = re.compile(r"^\s*(-?[\d.eE+\-]+)\s*mil\s*$")


def mils_to_mm(x: float) -> float:
    return float(x) * MIL_TO_MM


def region_raw_to_mm(x: float) -> float:
    """Region vertices are exposed in Altium's internal integer unit (10000/mil)."""
    return float(x) * MIL_TO_MM / REGION_RAW_PER_MIL


def parse_mil_string(s: str) -> float:
    """Parse strings like ``'11500.7mil'`` (used by AltiumPcbComponent.x/y)."""
    m = _MIL_STRING_RE.match(str(s))
    if not m:
        raise ValueError(f"Cannot parse mil string: {s!r}")
    return float(m.group(1)) * MIL_TO_MM


def parse_rotation_string(s: str) -> float:
    """Parse rotation strings like ``' 2.70000000000000E+0002'`` (degrees)."""
    return float(str(s).strip())


# --- typed dataclasses --------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Pt2D:
    """2D point in millimetres."""
    x: float
    y: float


@dataclass(frozen=True, slots=True)
class RawTrack:
    a: Pt2D
    b: Pt2D
    width_mm: float
    layer_id: int
    net_index: int            # NO_NET if unassigned
    polygon_index: int        # NO_POLYGON if not part of a polygon
    is_polygon_outline: bool
    component_index: int      # -1 if not part of a component
    is_keepout: bool
    # True when the parent polygon pour is *not* solid-filled (any hatch
    # style, or "outlines only"). A hatched pour's copper IS its tracks —
    # including the perimeter — so `is_polygon_outline` must not exclude
    # them the way it does for a solid pour. See `_polygon_lookup`.
    polygon_hatched: bool = False


@dataclass(frozen=True, slots=True)
class RawArc:
    center: Pt2D
    radius_mm: float
    start_angle_deg: float
    end_angle_deg: float
    width_mm: float
    layer_id: int
    net_index: int
    is_keepout: bool
    # An arc that forms part of a *solid* polygon-pour outline (flags1 & 0x02)
    # is boundary artwork, not copper — the poured copper is the region/fill.
    # Like is_polygon_outline tracks, these must be excluded from the copper
    # geometry or a rounded-corner pour gains a spurious band of copper along
    # its outline. A hatched pour is the exception: see `polygon_hatched`.
    is_polygon_outline: bool = False
    polygon_index: int = NO_POLYGON
    polygon_hatched: bool = False


@dataclass(frozen=True, slots=True)
class RawVia:
    center: Pt2D
    diameter_mm: float
    hole_diameter_mm: float
    layer_start: int          # Altium layer_id of via top layer
    layer_end: int            # Altium layer_id of via bottom layer
    net_index: int
    # IPC-4761 fill / protection metadata. ``ipc4761_via_type`` is the raw
    # Altium enum integer (0 = NONE / unprotected, 9–12 = fill variants).
    # ``fill_material`` is the free-text material string from the FILLING
    # IPC-4761 feature row (e.g. "", "Copper", "Silver Epoxy", "Polymer");
    # empty when no fill row exists. FYPA's via-barrel resistance model
    # consults these to decide whether to model a conductive-fill shunt
    # in parallel with the plated wall.
    ipc4761_via_type: int = 0
    fill_material: str = ""


@dataclass(frozen=True, slots=True)
class RawHole:
    """A non-plated through hole (NPTH) — a mechanical / mounting hole with
    no copper barrel, no net and no layer span. It carries no electrical
    role, so it is never meshed; it is surfaced purely so the viewer can
    draw it as the "Non Plated TH" Board Features overlay. ``diameter_mm``
    is the drilled hole diameter."""
    center: Pt2D
    diameter_mm: float


@dataclass(frozen=True, slots=True)
class RawPad:
    center: Pt2D
    width_mm: float
    height_mm: float
    hole_mm: float            # 0.0 for SMT
    shape: int                # Altium pad shape code (1=round, 2=rect, 3=octagonal, ...)
    rotation_deg: float
    layer_id: int             # 74 = Multi-Layer (through-hole), 1 = TOP, 32 = BOTTOM
    net_index: int
    designator: str           # pin number/name, e.g. '1', 'A2'
    component_index: int      # index into pcb_components, -1 if free-standing
    is_through_hole: bool
    is_smt: bool
    corner_radius_pct: int = 0  # 0-100; percentage of min(w,h)/2 used as corner radius
    is_plated: bool = True      # False for NPTH mounting / mechanical holes
    # Drill-hole shape. ``hole_shape`` is Altium's raw code (0=round, 1=square,
    # 2=slot). For a slot the drill is an obround: width = ``hole_mm`` (the
    # short axis), length = ``slot_length_mm`` (long axis), rotated by
    # ``slot_rotation_deg`` *relative to* the pad's own ``rotation_deg``.
    # A slot is only "real" when ``hole_shape == 2`` and the slot is longer
    # than the bore (``slot_length_mm > hole_mm``) — see :func:`is_slot_hole`.
    hole_shape: int = 0
    slot_length_mm: float = 0.0
    slot_rotation_deg: float = 0.0
    # Per-copper-layer pad-stack variations, for pads whose shape/size differs
    # across layers (Altium "Top-Middle-Bottom" or "Full Stack" pad modes).
    # Each entry is ``(layer_id, shape, width_mm, height_mm, corner_radius_pct)``
    # and lists only copper layers that differ from the top-level
    # ``shape`` / ``width_mm`` / ``height_mm`` / ``corner_radius_pct`` values.
    # Empty for ordinary uniform pads (the top-level fields then apply on every
    # copper layer the pad touches).
    layer_variations: tuple[tuple[int, int, float, float, int], ...] = ()


def slot_hole_geometry(pad) -> tuple[str, float, float, float] | None:
    """Non-round drill geometry of a pad, or ``None`` for a plain round bore.

    Returns ``(kind, length_mm, width_mm, rotation_deg)`` where ``width_mm``
    is the drilled bore (short axis), ``length_mm`` the long axis and
    ``rotation_deg`` is absolute (the slot rotation composed with the pad's
    own rotation). ``kind`` is:

    * ``"rect"`` — Altium ``hole_shape == 1``: a rectangular / square-cornered
      hole (a rectangular slot when ``slot_size`` adds length, a plain square
      when it does not).
    * ``"obround"`` — Altium ``hole_shape == 2``: a rounded-end slot. Only
      counts when genuinely longer than the bore; a zero-length obround is
      just a round hole, so this returns ``None`` (matching altium_monkey).

    Accepts any object exposing ``hole_shape`` / ``hole_mm`` /
    ``slot_length_mm`` / ``slot_rotation_deg`` / ``rotation_deg`` (a
    :class:`RawPad`, or a metadata dict via ``types.SimpleNamespace``)."""
    hole_shape = int(getattr(pad, "hole_shape", 0) or 0)
    if hole_shape not in (1, 2):
        return None
    width = float(getattr(pad, "hole_mm", 0.0) or 0.0)
    if width <= 0.0:
        return None
    length = float(getattr(pad, "slot_length_mm", 0.0) or 0.0)
    if hole_shape == 2:
        # Rounded slot: needs real extra length, else it's a round hole.
        if length <= width + 1e-9:
            return None
        kind = "obround"
    else:
        # Rectangular hole: a square when no slot length is set.
        length = max(length, width)
        kind = "rect"
    rot = (float(getattr(pad, "slot_rotation_deg", 0.0) or 0.0)
           + float(getattr(pad, "rotation_deg", 0.0) or 0.0))
    return (kind, length, width, rot)


@dataclass(frozen=True, slots=True)
class RawRegion:
    """A filled copper region (from Altium's Regions6 stream).

    `outline` is the closed boundary; `holes` is a tuple of inner boundaries.
    `kind == 0` is normal copper; non-zero kinds (board cutout, polygon cutout)
    are still surfaced here so callers can filter.

    ``polygon_index`` links a polygon-pour-rendered region back to the
    parent ``Polygons6`` record it was generated from
    (:data:`NO_POLYGON` = 65535 means "not part of a polygon"). Modern
    Altium dual-stores polygon-pour output in BOTH ``Regions6`` and
    ``ShapeBasedRegions6``; the geometry layer skips the ``Regions6`` copy
    when a matching ``ShapeBasedRegions6`` record exists for the same
    polygon, since the latter carries the arc-edge / thermal-relief
    detail.
    """
    outline: tuple[Pt2D, ...]
    holes: tuple[tuple[Pt2D, ...], ...]
    layer_id: int
    net_index: int
    kind: int
    is_polygon_outline: bool
    is_keepout: bool
    is_board_cutout: bool
    polygon_index: int = NO_POLYGON


@dataclass(frozen=True, slots=True)
class RawRegionVertex:
    """One vertex of a shape-based region outline.

    A shape-based region's outline is a closed sequence of these vertices.
    The segment from vertex ``i`` to vertex ``i+1`` is:

    * a straight line, when ``is_arc`` is False;
    * a circular arc from ``pos`` to the next vertex's ``pos``, with the
      arc centred at ``center`` with radius ``radius_mm``, sweeping from
      ``start_angle_deg`` to ``end_angle_deg`` (degrees), when ``is_arc``
      is True.

    Straight-line vertices leave the arc fields at their zero defaults.
    """
    pos: Pt2D
    is_arc: bool = False
    center: Pt2D = Pt2D(0.0, 0.0)
    radius_mm: float = 0.0
    start_angle_deg: float = 0.0
    end_angle_deg: float = 0.0


@dataclass(frozen=True, slots=True)
class RawShapeBasedRegion:
    """A filled copper region from Altium's ``ShapeBasedRegions6`` stream.

    Same role as :class:`RawRegion` but the outline can contain circular-arc
    segments — these come from manually-placed "Place > Region" objects with
    arc edges and from polygon pours rendered with thermal-relief spokes /
    rounded clearances. Holes remain simple polylines (Altium stores them as
    double-precision vertices with no arc info).

    ``polygon_index`` is the parent ``Polygons6`` record this region was
    generated from (:data:`NO_POLYGON` = 65535 if standalone). Used by the
    geometry layer to deduplicate against legacy ``Regions6`` copies of
    the same polygon-pour output.
    """
    outline: tuple[RawRegionVertex, ...]
    holes: tuple[tuple[Pt2D, ...], ...]
    layer_id: int
    net_index: int
    kind: int
    is_polygon_outline: bool
    is_keepout: bool
    is_board_cutout: bool
    polygon_index: int = NO_POLYGON


@dataclass(frozen=True, slots=True)
class RawFill:
    """A rectangular copper fill (from Altium's Fills6 stream).

    Altium's "Place > Fill" primitive: an axis-aligned rectangle defined
    by opposite corners ``(x1, y1)`` and ``(x2, y2)``, optionally rotated
    by ``rotation_deg`` about the rectangle's geometric centre. Coordinates
    are millimetres, already shifted by the project origin.
    """
    x1_mm: float
    y1_mm: float
    x2_mm: float
    y2_mm: float
    rotation_deg: float
    layer_id: int
    net_index: int
    is_keepout: bool


@dataclass(frozen=True, slots=True)
class RawText:
    """A PCB text string (from Altium's ``Texts6`` stream).

    Covers free-standing text as well as the per-component reference
    designator and comment strings. ``component_index`` links the latter
    back to :attr:`ExtractedProject.pcb_components` (-1 if free-standing).
    ``layer_id`` 33 / 34 are the Top / Bottom Overlay (silkscreen) layers.
    """
    text: str
    center: Pt2D              # text anchor point, origin-corrected mm
    height_mm: float          # character height
    rotation_deg: float
    layer_id: int
    component_index: int      # -1 if not part of a component
    is_designator: bool       # the component's reference designator
    is_comment: bool          # the component's comment / value string
    is_mirrored: bool         # placed on a bottom-side layer (reads mirrored)
    # Font: Altium PCB text is drawn either with one of three built-in
    # single-stroke vector fonts or a TrueType face. ``is_stroke`` is True
    # for the stroke fonts; ``stroke_kind`` then selects which, using
    # Altium's native ``stroke_font_type`` convention (1 = Default,
    # 2 = Sans Serif, 3 = Serif; 0 / unknown fall back to Default).
    # ``stroke_width_mm`` is the stroke pen width. ``font_name`` /
    # ``is_bold`` / ``is_italic`` describe the TrueType case.
    is_stroke: bool = True
    stroke_kind: int = 0
    stroke_width_mm: float = 0.0
    font_name: str = ""
    is_bold: bool = False
    is_italic: bool = False


@dataclass(frozen=True, slots=True)
class RawPcbComponent:
    designator: str           # physical (PCB) designator, e.g. 'C144_PWR_SW13'
    center: Pt2D
    rotation_deg: float
    layer_name: str           # 'TOP' or 'BOTTOM'
    footprint: str
    # Schematic (logical) designator, from the PCB record's SOURCEDESIGNATOR
    # field, e.g. 'C118'. In a multi-channel design Altium re-bases the
    # physical designator, so this is the only reliable link back to the
    # schematic component a PDN_* directive is authored on. Empty for a
    # component with no schematic origin (hand-placed on the PCB).
    source_designator: str = ""
    # Component parameters from PrimitiveParameters/Data (populated after a
    # schematic→PCB ECO; carries Blanket/Parameter-Set directives among others).
    parameters: dict[str, str] = field(default_factory=dict)
    unique_id: str = ""
    # Altium ``ComponentKind`` as stored on the PCB record (COMPONENTKIND /
    # VERSION2 / VERSION3). Same encoding as
    # :attr:`RawSchComponent.component_kind`. Captured on both sides because a
    # Net Tie can exist only on the PCB — added by ECO, or a board opened
    # without its schematics — and the auto-bridge has to see it there too.
    component_kind: int = 0
    # Hierarchy path of UniqueIDs from root sheet symbol(s) down to the
    # schematic component (PCB ``SOURCEUNIQUEID``, e.g. ``\XOZXOXGE\QJGQOCLZ``).
    # Primary key for binding sheet-symbol ``PDN_<Des>_*`` overrides.
    source_unique_id: str = ""
    # Human-readable hierarchy (PCB ``SOURCEHIERARCHICALPATH``), e.g.
    # ``main\CON-AUX``. Used as fallback when ``source_unique_id`` is empty:
    # sheet-symbol ``sheet_name`` is matched against path segments.
    source_hierarchical_path: str = ""


@dataclass(frozen=True, slots=True)
class RawNet:
    name: str                 # index into ExtractedProject.nets is the net_index


@dataclass(frozen=True, slots=True)
class RawStackupLayer:
    """One entry from `pcb.board.layer_stackup`.

    `next_layer_id == 0` marks the end of the enabled chain. Walk
    :meth:`ExtractedProject.enabled_copper_layer_ids` starting from id=1 (Top)
    to get the in-order enabled copper stack.
    """
    layer_id: int
    name: str
    copper_thickness_mm: float
    # Thickness of the dielectric sitting BELOW this copper layer (i.e. between
    # this layer and the one with id == next_layer_id). 0.0 for the bottom-most
    # copper layer or when the .PcbDoc didn't store a value.
    dielectric_thickness_mm: float
    next_layer_id: int
    is_plane: bool
    plane_net_name: str | None
    mech_enabled: bool
    # Distance an internal plane is pulled back from the board outline
    # (Altium ``PLANE<n>PULLBACK``), in mm. 0.0 for signal layers and for
    # planes that don't define a pullback.
    plane_pullback_mm: float = 0.0
    # Relative permittivity (Dk) and loss tangent (Df) of the dielectric
    # BELOW this copper layer (the same gap ``dielectric_thickness_mm``
    # measures). A multi-ply gap (core + prepreg) is thickness-weighted.
    # ``None`` when the .PcbDoc doesn't store a value — consumers fall back
    # to their own default. Not used by the DC solve (conductance is
    # Dk-independent); carried for the PDN inductance/impedance analyses.
    dielectric_dk: float | None = None
    dielectric_df: float | None = None


@dataclass(frozen=True, slots=True)
class RawSchComponent:
    designator: str
    # Project-relative SchDoc path (forward slashes, original casing preserved
    # for diagnostics), e.g. ``Power.SchDoc`` or ``mod/Child.SchDoc``. Absolute
    # path string when the file sits outside the ``.PrjPcb`` tree.
    schdoc_name: str
    parameters: dict[str, str]  # name -> text (case-preserved keys)
    pin_designators: tuple[str, ...]
    # Altium schematic ComponentKind (see altium_monkey.ComponentKind).
    # 3 = Net Tie (BOM), 4 = Net Tie (No BOM); 0 = Standard.
    component_kind: int = 0
    # Pin designators (upper-cased) marked PDN_IGNORE on the schematic pin
    # itself. Empty when no pin-level ignore parameters were found.
    ignored_pins: frozenset[str] = frozenset()


def _component_kind_value(comp) -> int:
    """Return Altium ``ComponentKind`` as an int (0 = Standard).

    Works for schematic and PCB components alike: altium_monkey exposes the
    field as a ``ComponentKind`` enum on both, and older builds may not expose
    it at all.
    """
    kind = getattr(comp, "component_kind", None)
    if kind is None:
        return 0
    try:
        return int(getattr(kind, "value", kind) or 0)
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True, slots=True)
class RawSchSheetSymbol:
    """One hierarchical sheet symbol placement (parent sheet → child SchDoc).

    Carries optional ``PDN_<Designator>_*`` parameters that override child
    component PDN directives for PCB instances whose ``SOURCEUNIQUEID`` path
    contains this symbol's :attr:`unique_id`.
    """
    parent_schdoc: str        # filename of the sheet that hosts the symbol
    sheet_name: str           # display name (may be ``REPEAT(...)``)
    child_filename: str       # referenced child schematic filename
    unique_id: str            # Altium UniqueID of the sheet symbol
    parameters: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExtractedProject:
    prjpcb_path: Path
    pcbdoc_path: Path           # which .PcbDoc was actually loaded (multi-PCB projects)
    tracks: tuple[RawTrack, ...]
    arcs: tuple[RawArc, ...]
    vias: tuple[RawVia, ...]
    pads: tuple[RawPad, ...]
    regions: tuple[RawRegion, ...]
    shape_based_regions: tuple[RawShapeBasedRegion, ...]
    fills: tuple[RawFill, ...]
    pcb_components: tuple[RawPcbComponent, ...]
    nets: tuple[RawNet, ...]
    stackup: tuple[RawStackupLayer, ...]
    sch_components: tuple[RawSchComponent, ...]
    # Sheet symbols across all SchDocs (for per-instance PDN overrides).
    # Empty default so older callers / Gerber extracts keep working.
    sch_sheet_symbols: tuple[RawSchSheetSymbol, ...] = ()
    # Compiled schematic netlist (multi-sheet aware). Used to translate local
    # sheet net names in PDN_*_NET parameters to per-instance PCB connectivity.
    compiled_netlist: Any | None = None
    # Absolute SchDoc paths keyed by project-relative lowercase path (forward
    # slashes). When a basename is unique in the project it is also registered
    # as an alias key (``"child.schdoc"``) so callers that only know the
    # filename still resolve. Used for lazy per-sheet netlist compiles.
    schdoc_paths: dict[str, str] = field(default_factory=dict)
    # Lazily filled single-sheet netlists, keyed like :attr:`schdoc_paths`.
    # Empty until a child-sheet local-net fallback needs a sheet, and dropped
    # by ``__getstate__`` so it never reaches the design-info pickle (see
    # there) — it is a rebuildable cache, not project data.
    sheet_netlists: dict[str, Any] = field(default_factory=dict)
    # altium_monkey ≥ 2026.7 maps netlist ``source_sheets`` to physical page
    # ids (``physical:0:logical:0:main.SchDoc:child:…``). This tuple maps each
    # physical page id → logical schematic file name (``power.SchDoc``) so
    # local-net sheet matching stays compatible with sch_components.
    physical_sheet_names: tuple[tuple[str, str], ...] = ()
    # User-defined Altium origin (Board6/ORIGINX,ORIGINY), in mm. Every
    # Pt2D produced above has already had this subtracted, so coordinates
    # match what Altium displays when the user has set a custom origin.
    # Retained here for traceability and so downstream code can reconstruct
    # absolute (file) coordinates if needed: absolute = relative + origin.
    board_origin_mm: Pt2D = Pt2D(0.0, 0.0)
    # Closed polyline of the PCB's mechanical outline (the layer tagged
    # Layer Type = Board), in mm, origin-corrected. Arc segments have
    # been discretised. Empty tuple when the project carries no outline.
    board_outline: tuple[Pt2D, ...] = ()
    # PCB text strings (designators, comments, free-standing text). Optional
    # with an empty default so older callers that build ExtractedProject
    # without texts keep working.
    texts: tuple[RawText, ...] = ()
    # Non-plated through holes (mounting / mechanical holes). Empty default
    # so older callers that build ExtractedProject without them keep working.
    npth_holes: tuple[RawHole, ...] = ()
    # Internal-plane modelling rules, sourced from the Altium design rules.
    # ``plane_clearance_mm`` is the anti-pad gap punched around a foreign-net
    # through feature (PlaneClearance). The ``plane_relief_*`` fields describe
    # the thermal relief that connects a same-net through feature to the plane
    # (PlaneConnect): a ``plane_relief_air_gap_mm`` annular gap bridged by
    # ``plane_relief_entries`` spokes of width ``plane_relief_conductor_width_mm``.
    # All default to 0 / 4 so non-plane boards and the Gerber path are unaffected.
    plane_clearance_mm: float = 0.0
    plane_relief_air_gap_mm: float = 0.0
    plane_relief_conductor_width_mm: float = 0.0
    plane_relief_entries: int = 4

    def __getstate__(self) -> dict:
        """Drop :attr:`sheet_netlists` from the pickle.

        ``parse_annotations`` fills it lazily during ``load_project``, i.e.
        *before* the CLI pickles the whole ``LoadedProject`` into the
        design-info cache. On a project with many repeated child sheets that
        would write one fully compiled netlist per sheet on top of the
        project-wide ``compiled_netlist``, inflating both the cache write and
        every subsequent cache-hit load. Each entry is recomputed on demand.
        """
        state = {
            name: getattr(self, name)
            for cls in type(self).__mro__
            for name in getattr(cls, "__slots__", ())
            if hasattr(self, name)
        }
        state["sheet_netlists"] = {}
        return state

    def __setstate__(self, state: dict) -> None:
        # frozen=True blocks setattr, and slots=True means there is no
        # __dict__ to update — set each slot directly.
        for name, value in state.items():
            object.__setattr__(self, name, value)

    def enabled_copper_layer_ids(self) -> list[int]:
        """Layer ids forming the actually-enabled copper stack, in Top→Bottom order.

        Walks `next_layer_id` linkage from id=1. Falls back to "all layer_ids
        present in tracks/regions" if the linkage is broken.
        """
        by_id = {s.layer_id: s for s in self.stackup}
        ordered: list[int] = []
        cur = 1
        seen: set[int] = set()
        while cur and cur in by_id and cur not in seen:
            ordered.append(cur)
            seen.add(cur)
            cur = by_id[cur].next_layer_id
        # A well-formed chain terminates with next_layer_id == 0. A single-layer
        # flex board is a legitimate 1-long chain (Top → 0) — trust it. Only
        # fall back to the artwork scan when the linkage is actually broken:
        # empty (id=1 absent), a dangling next_layer_id, or a cycle (cur != 0).
        if not ordered or cur != 0:
            used: set[int] = set()
            for t in self.tracks:
                used.add(t.layer_id)
            for a in self.arcs:
                used.add(a.layer_id)
            for p in self.pads:
                used.add(p.layer_id)
            for r in self.regions:
                used.add(r.layer_id)
            for r in self.shape_based_regions:
                used.add(r.layer_id)
            for f in self.fills:
                used.add(f.layer_id)
            ordered = sorted(i for i in used if i in by_id)
        return ordered

    def net_name(self, net_index: int) -> str:
        if net_index is None or net_index == NO_NET:
            return ""
        if 0 <= net_index < len(self.nets):
            return self.nets[net_index].name
        return ""


# --- altium_monkey adapters ---------------------------------------------------

def _net_index(raw) -> int:
    return NO_NET if raw is None else int(raw)


def _component_index(raw) -> int:
    return -1 if raw is None else int(raw)


# HATCHSTYLE values Altium writes for a *solid* pour, lower-cased. Everything
# else ("45degree", "90degree", "horizontal", "vertical", "none") means the
# pour's copper is drawn as tracks/arcs rather than poured as regions.
_SOLID_HATCH_STYLES: frozenset[str] = frozenset({"solid", ""})


def _polygon_net_of(poly) -> int | None:
    """One polygon's net index, or ``None`` when it carries no net.

    Deliberately does *not* trust ``polygon.net`` alone: altium_monkey parses a
    missing ``NET`` field as ``int(record.get('NET', 0))``, so a net-less
    polygon is indistinguishable from one genuinely on net index 0. Inheriting
    that would silently attach pour copper to whichever net happens to sit at
    index 0 — 27 of Corvette's 134 polygons carry no ``NET`` field at all, and
    were landing on ``PWR_I2C.SDA``. The raw record is consulted so "absent"
    stays absent; a polygon built programmatically has no raw record, and there
    ``poly.net`` is all we have.
    """
    raw = getattr(poly, "_raw_record", None) or {}
    if raw and not str(raw.get("NET") or "").strip():
        return None
    try:
        value = int(poly.net)
    except (AttributeError, TypeError, ValueError):
        return None
    return None if value < 0 else value


def _polygon_is_hatched(poly) -> bool:
    """True when a pour is not solid-filled, so its copper is tracks and arcs.

    Note the two distinct "None"s: a *missing* ``hatch_style`` attribute
    (Python ``None``) means solid — that is altium_monkey's own default —
    whereas the *string* ``'None'`` is Altium's "outlines only" fill, whose
    copper really is just the perimeter tracks.
    """
    style = str(getattr(poly, "hatch_style", None) or "Solid").strip()
    return style.lower() not in _SOLID_HATCH_STYLES


def _polygon_lookup(pcb):
    """Return ``(net_of, hatched_of)`` resolvers mapping a primitive's
    ``polygon_index`` to facts about its parent ``Polygons6`` record.

    Both are resolved once per polygon up front — a board has a hundred or so
    pours but tens of thousands of primitives asking about them.
    """
    polygons = list(getattr(pcb, "polygons", None) or ())
    nets = [_polygon_net_of(p) for p in polygons]
    hatched = [_polygon_is_hatched(p) for p in polygons]

    # 65535 is the documented "no polygon" sentinel; split-plane and
    # board-outline tracks carry 65534. Both land outside the record list, so
    # one range check covers every sentinel Altium writes.
    def net_of(idx: int) -> int | None:
        return nets[idx] if 0 <= idx < len(nets) else None

    def hatched_of(idx: int) -> bool:
        return hatched[idx] if 0 <= idx < len(hatched) else False

    return net_of, hatched_of


def _pt_from_mils(x_mils: float, y_mils: float,
                  ox_mm: float = 0.0, oy_mm: float = 0.0) -> Pt2D:
    return Pt2D(mils_to_mm(x_mils) - ox_mm, mils_to_mm(y_mils) - oy_mm)


def _pad_height_mm(pad) -> float:
    """Pad height isn't exposed as `_mils` in all altium_monkey versions;
    fall back to the raw integer (10000 per mil) when needed."""
    if hasattr(pad, "height_mils"):
        return mils_to_mm(pad.height_mils)
    return float(pad.height) * MIL_TO_MM / REGION_RAW_PER_MIL


def _extract_tracks(pcb, ox_mm: float, oy_mm: float) -> tuple[RawTrack, ...]:
    """Extract ``Tracks6`` records, inheriting the parent polygon's net.

    A hatched (or outlines-only) pour renders its copper as tracks, and Altium
    leaves those tracks' own ``net_index`` unlinked (0xFFFF) because the net
    assignment lives on the parent ``Polygons6`` record — the same split
    :func:`_extract_regions` already handles for solid pours. Without this the
    hatch lines arrive as NO_NET and drop out of the per-net pipeline.
    """
    poly_net, poly_hatched = _polygon_lookup(pcb)
    out: list[RawTrack] = []
    for t in pcb.tracks:
        raw_net = t.net_index
        poly_idx = int(t.polygon_index)
        if raw_net is None:
            raw_net = poly_net(poly_idx)
        out.append(RawTrack(
            a=_pt_from_mils(t.start_x_mils, t.start_y_mils, ox_mm, oy_mm),
            b=_pt_from_mils(t.end_x_mils, t.end_y_mils, ox_mm, oy_mm),
            width_mm=mils_to_mm(t.width_mils),
            layer_id=int(t.layer),
            net_index=_net_index(raw_net),
            polygon_index=poly_idx,
            is_polygon_outline=bool(t.is_polygon_outline),
            component_index=_component_index(t.component_index),
            is_keepout=bool(t.is_keepout),
            polygon_hatched=poly_hatched(poly_idx),
        ))
    return tuple(out)


def _extract_arcs(pcb, ox_mm: float, oy_mm: float) -> tuple[RawArc, ...]:
    """Extract ``Arcs6`` records, inheriting the parent polygon's net exactly
    as :func:`_extract_tracks` does — a hatched pour's rounded corners and
    curved perimeter arrive as polygon-owned arcs with no net of their own."""
    poly_net, poly_hatched = _polygon_lookup(pcb)
    out: list[RawArc] = []
    for a in pcb.arcs:
        raw_net = a.net_index
        poly_idx = int(getattr(a, "polygon_index", NO_POLYGON))
        if raw_net is None:
            raw_net = poly_net(poly_idx)
        out.append(RawArc(
            center=_pt_from_mils(a.center_x_mils, a.center_y_mils, ox_mm, oy_mm),
            radius_mm=mils_to_mm(a.radius_mils),
            start_angle_deg=float(a.start_angle),
            end_angle_deg=float(a.end_angle),
            width_mm=mils_to_mm(a.width_mils),
            layer_id=int(a.layer),
            net_index=_net_index(raw_net),
            is_keepout=bool(a.is_keepout),
            is_polygon_outline=bool(getattr(a, "is_polygon_outline", False)),
            polygon_index=poly_idx,
            polygon_hatched=poly_hatched(poly_idx),
        ))
    return tuple(out)


def _extract_texts(pcb, ox_mm: float, oy_mm: float) -> tuple[RawText, ...]:
    """Extract every PCB text string from the ``Texts6`` stream.

    Unicode text is stored out-of-line in the wide-strings table; fall
    back to the inline ``text_content`` for the common ASCII case (and
    when no wide-strings table is exposed by this altium_monkey build)."""
    out: list[RawText] = []
    wst = getattr(pcb, "widestrings_table", None)
    for t in pcb.texts:
        content = ""
        if wst is not None and hasattr(t, "resolve_text_content"):
            try:
                content = t.resolve_text_content(wst) or ""
            except Exception:
                content = ""
        if not content:
            content = str(getattr(t, "text_content", "") or "")
        # Font: ``font_type`` 0 == one of Altium's built-in stroke fonts;
        # ``stroke_font_type`` then picks the face (1 = Default,
        # 2 = Sans Serif, 3 = Serif).
        font_type = int(getattr(t, "font_type", 0) or 0)
        out.append(RawText(
            text=content,
            center=_pt_from_mils(t.x_mils, t.y_mils, ox_mm, oy_mm),
            height_mm=mils_to_mm(float(getattr(t, "height_mils", 0.0) or 0.0)),
            rotation_deg=float(getattr(t, "rotation", 0.0) or 0.0),
            layer_id=int(getattr(t, "layer", 0) or 0),
            component_index=_component_index(getattr(t, "component_index", None)),
            is_designator=bool(getattr(t, "is_designator", False)),
            is_comment=bool(getattr(t, "is_comment", False)),
            is_mirrored=bool(getattr(t, "is_mirrored", False)),
            is_stroke=(font_type == 0),
            stroke_kind=int(getattr(t, "stroke_font_type", 0) or 0),
            stroke_width_mm=mils_to_mm(
                float(getattr(t, "stroke_width_mils", 0.0) or 0.0)),
            font_name=str(getattr(t, "font_name", "") or ""),
            is_bold=bool(getattr(t, "is_bold", False)),
            is_italic=bool(getattr(t, "is_italic", False)),
        ))
    return tuple(out)


def _extract_vias(pcb, ox_mm: float, oy_mm: float) -> tuple[RawVia, ...]:
    out: list[RawVia] = []
    for v in pcb.vias:
        out.append(RawVia(
            center=_pt_from_mils(v.x_mils, v.y_mils, ox_mm, oy_mm),
            diameter_mm=mils_to_mm(v.diameter_mils),
            hole_diameter_mm=mils_to_mm(v.hole_size_mils),
            layer_start=int(v.layer_start),
            layer_end=int(v.layer_end),
            net_index=_net_index(v.net_index),
            ipc4761_via_type=int(getattr(v, "ipc4761_via_type", 0) or 0),
            fill_material=_via_fill_material(v),
        ))
    return tuple(out)


# IPC-4761 FILLING feature type enum value (PcbViaStructureFeatureType.FILLING).
# Repeated here so fypa.altium.extract has no hard import dependency on
# altium_monkey enums — the value is part of the on-disk Altium format.
_IPC4761_FEATURE_FILLING: int = 3


def _via_fill_material(v) -> str:
    """Return the IPC-4761 FILLING feature row's material string for this via.

    Altium stores per-feature material strings on the via_structure side-table
    record (see ``altium_pcb_via_structure.AltiumPcbViaStructure``). A via
    with no structure attached (most commonly because it has IPC-4761 type
    NONE) returns the empty string. The material text is free-form — Altium
    surfaces it verbatim in the Via dialog — and downstream code is expected
    to do case-insensitive substring matching ("copper", "silver", etc.) to
    classify it.
    """
    structure = getattr(v, "via_structure", None)
    if structure is None:
        return ""
    try:
        feature = structure.get_feature(_IPC4761_FEATURE_FILLING)
    except Exception as exc:
        log.debug("via_structure.get_feature(FILLING) failed, treating via "
                  "as non-conductive-fill: %s", exc)
        return ""
    if feature is None:
        return ""
    return str(getattr(feature, "material", "") or "")


# Copper layer ids that a pad stack can vary over: 1 = TOP, 2..31 =
# MID1..MID30, 32 = BOTTOM (the PcbLayer enum's signal-layer values).
_PAD_COPPER_LAYER_IDS: tuple[int, ...] = tuple(range(1, 33))


def _pad_layer_variations(
    p, shape: int, width_mm: float, height_mm: float, corner_pct: int,
) -> tuple[tuple[int, int, float, float, int], ...]:
    """Per-copper-layer ``(layer_id, shape, width_mm, height_mm, corner_pct)``
    for a pad whose stack varies across layers (Altium top-mid-bot / full-stack
    pad modes). Returns ``()`` for uniform pads so ordinary pads carry no extra
    payload. Only layers that differ from the supplied top-level values are
    emitted; the geometry side falls back to those for any missing layer.

    Uses altium_monkey's per-layer resolvers (``_layer_shape`` / ``_layer_size``
    / per-layer ``corner_radius``), which already collapse simple / top-mid-bot
    / full-stack modes into a single per-layer answer."""
    if not getattr(p, "pad_mode", 0):
        return ()
    try:
        from altium_monkey.altium_pcb_enums import PcbLayer
    except Exception:
        return ()
    to_iu = getattr(p, "_from_internal_units", None)
    corner_list = list(getattr(p, "corner_radius", None) or [])
    out: list[tuple[int, int, float, float, int]] = []
    for lid in _PAD_COPPER_LAYER_IDS:
        try:
            layer = PcbLayer(lid)
            l_shape = int(p._layer_shape(layer))
            sx_iu, sy_iu = p._layer_size(layer)
            l_w = mils_to_mm(to_iu(sx_iu)) if to_iu else mils_to_mm(sx_iu)
            l_h = mils_to_mm(to_iu(sy_iu)) if to_iu else mils_to_mm(sy_iu)
        except Exception:
            continue
        l_cr = int(corner_list[lid - 1]) if lid - 1 < len(corner_list) else corner_pct
        # Skip layers identical to the top-level (uniform) values — the
        # geometry builder falls back to those, so storing them is redundant.
        if (l_shape == shape and l_cr == corner_pct
                and abs(l_w - width_mm) < 1e-6 and abs(l_h - height_mm) < 1e-6):
            continue
        out.append((lid, l_shape, l_w, l_h, l_cr))
    return tuple(out)


def _extract_pads(pcb, ox_mm: float, oy_mm: float) -> tuple[RawPad, ...]:
    out: list[RawPad] = []
    for p in pcb.pads:
        shape = int(getattr(p, 'effective_top_shape', p.shape))
        width_mm = mils_to_mm(p.width_mils)
        height_mm = _pad_height_mm(p)
        corner_pct = int(getattr(p, 'corner_radius_percentage', 0))
        out.append(RawPad(
            center=_pt_from_mils(p.x_mils, p.y_mils, ox_mm, oy_mm),
            width_mm=width_mm,
            height_mm=height_mm,
            hole_mm=mils_to_mm(p.hole_size_mils),
            shape=shape,
            rotation_deg=float(p.rotation),
            layer_id=int(p.layer),
            net_index=_net_index(p.net_index),
            designator=str(p.designator),
            component_index=_component_index(p.component_index),
            is_through_hole=bool(p.is_through_hole),
            is_smt=bool(p.is_smt),
            corner_radius_pct=corner_pct,
            is_plated=bool(getattr(p, 'is_plated', True)),
            hole_shape=int(getattr(p, 'hole_shape', 0) or 0),
            # slot_size is in Altium internal units (10000/mil), like region
            # vertices — reuse region_raw_to_mm for the conversion.
            slot_length_mm=region_raw_to_mm(
                float(getattr(p, 'slot_size', 0) or 0)),
            slot_rotation_deg=float(getattr(p, 'slot_rotation', 0.0) or 0.0),
            layer_variations=_pad_layer_variations(
                p, shape, width_mm, height_mm, corner_pct),
        ))
    return tuple(out)


def _vertex_to_pt(v, ox_mm: float = 0.0, oy_mm: float = 0.0) -> Pt2D:
    """Region vertices use Altium internal integer units (10000/mil)."""
    return Pt2D(region_raw_to_mm(v.x_raw) - ox_mm,
                region_raw_to_mm(v.y_raw) - oy_mm)


def _split_holes(hole_vertices, hole_count: int,
                 ox_mm: float = 0.0, oy_mm: float = 0.0,
                 ) -> tuple[tuple[Pt2D, ...], ...]:
    """altium_monkey returns hole_vertices either as a flat list (one big sequence)
    or as a list of vertex lists, depending on version. Handle both."""
    if hole_count == 0 or not hole_vertices:
        return ()
    # Case 1: list of lists (preferred shape if altium_monkey already split them)
    if hole_vertices and isinstance(hole_vertices[0], (list, tuple)):
        return tuple(
            tuple(_vertex_to_pt(v, ox_mm, oy_mm) for v in ring)
            for ring in hole_vertices
        )
    # Case 2: flat list. Altium closes each hole ring (its last vertex repeats
    # its first), so split on ring closure rather than assuming equal vertex
    # counts per ring. Equal division silently produces garbage rings for holes
    # of differing vertex counts (e.g. 10 + 6 vertices split into two 8-vertex
    # rings, which make_valid then "repairs" into wrong copper).
    def _closed(a, b) -> bool:
        return a.x_raw == b.x_raw and a.y_raw == b.y_raw

    rings: list[list] = []
    cur: list = []
    for v in hole_vertices:
        cur.append(v)
        # A ring needs ≥3 distinct vertices before its closing repeat, so only
        # treat a start-vertex match as closure once we have enough points.
        if len(cur) >= 4 and _closed(v, cur[0]):
            rings.append(cur)
            cur = []
    if cur:
        rings.append(cur)

    if len(rings) == hole_count:
        return tuple(
            tuple(_vertex_to_pt(v, ox_mm, oy_mm) for v in ring)
            for ring in rings
        )

    # Closure split disagreed with hole_count — fall back to equal division if
    # it divides evenly, else dump as a single ring. Either way, warn: the
    # flat-list shape is not what we expected and the result may be wrong.
    total = len(hole_vertices)
    log.warning(
        "Region has %d hole_vertices with hole_count=%d but closure-split "
        "found %d ring(s); geometry may be wrong.",
        total, hole_count, len(rings),
    )
    if total % hole_count != 0:
        return (tuple(_vertex_to_pt(v, ox_mm, oy_mm) for v in hole_vertices),)
    step = total // hole_count
    return tuple(
        tuple(_vertex_to_pt(v, ox_mm, oy_mm) for v in hole_vertices[i * step:(i + 1) * step])
        for i in range(hole_count)
    )


def _extract_regions(pcb, ox_mm: float, oy_mm: float) -> tuple[RawRegion, ...]:
    """Extract Regions6 records, inheriting the parent polygon's net when the
    region itself carries no net.

    Altium's Regions6 records (the filled output of polygon pours) often have
    ``net_index = None`` because the net assignment lives on the parent
    Polygons6 record. Without this inheritance, the largest copper pours on
    the board come out unassigned — wreaking havoc on per-net-aware FEM.
    """
    _polygon_net, _ = _polygon_lookup(pcb)

    out: list[RawRegion] = []
    for r in pcb.regions:
        outline = tuple(_vertex_to_pt(v, ox_mm, oy_mm) for v in r.outline_vertices)
        holes = _split_holes(r.hole_vertices, int(r.hole_count), ox_mm, oy_mm)
        raw_net = r.net_index
        poly_idx = int(r.polygon_index)
        if raw_net is None and poly_idx != NO_POLYGON:
            raw_net = _polygon_net(poly_idx)
        out.append(RawRegion(
            outline=outline,
            holes=holes,
            layer_id=int(r.layer),
            net_index=_net_index(raw_net),
            kind=int(r.kind),
            is_polygon_outline=bool(r.is_polygon_outline),
            is_keepout=bool(r.is_keepout),
            is_board_cutout=bool(r.is_board_cutout),
            polygon_index=poly_idx,
        ))
    return tuple(out)


def _shape_based_vertex(v, ox_mm: float, oy_mm: float) -> RawRegionVertex:
    """Convert one ``PcbExtendedVertex`` to a :class:`RawRegionVertex`.

    Extended vertices store position and (optional) arc-centre + radius in
    Altium's internal integer unit (10000 per mil) — same scaling as
    Regions6 vertices. Arc start/end angles are in degrees.
    """
    is_arc = bool(getattr(v, "is_round", False)) and float(getattr(v, "radius", 0) or 0) > 0
    if is_arc:
        return RawRegionVertex(
            pos=Pt2D(region_raw_to_mm(v.x) - ox_mm,
                     region_raw_to_mm(v.y) - oy_mm),
            is_arc=True,
            center=Pt2D(region_raw_to_mm(v.center_x) - ox_mm,
                        region_raw_to_mm(v.center_y) - oy_mm),
            radius_mm=region_raw_to_mm(v.radius),
            start_angle_deg=float(v.start_angle),
            end_angle_deg=float(v.end_angle),
        )
    return RawRegionVertex(
        pos=Pt2D(region_raw_to_mm(v.x) - ox_mm,
                 region_raw_to_mm(v.y) - oy_mm),
    )


def _shape_based_hole(hole, ox_mm: float, oy_mm: float) -> tuple[Pt2D, ...]:
    """Convert one ShapeBasedRegion hole ring (``list[PcbSimpleVertex]``)
    to a tuple of :class:`Pt2D`. Simple vertices store ``x``/``y`` as
    doubles in raw internal units (10000 per mil).
    """
    return tuple(Pt2D(region_raw_to_mm(sv.x) - ox_mm,
                      region_raw_to_mm(sv.y) - oy_mm)
                 for sv in hole)


def _extract_shape_based_regions(pcb, ox_mm: float, oy_mm: float,
                                  ) -> tuple[RawShapeBasedRegion, ...]:
    """Extract ``ShapeBasedRegions6`` records.

    Polygon pours are rendered into this stream by Altium (with thermal
    reliefs / clearance gaps already applied), and manually-placed regions
    with arc edges land here too. Net inheritance from the parent polygon
    follows the same rule as :func:`_extract_regions` — if the region
    record itself has no net but is owned by a polygon, take the polygon's
    net so polygon-pour copper isn't silently dropped from the per-net
    pipeline.
    """
    shape_based = getattr(pcb, "shapebased_regions", None)
    if not shape_based:
        return ()
    _polygon_net, _ = _polygon_lookup(pcb)

    out: list[RawShapeBasedRegion] = []
    for r in shape_based:
        # The ShapeBasedRegions6 stream stores ``count+1`` outline vertices
        # with the last one repeating the first to close the ring. Drop it
        # so downstream consumers see one entry per logical corner.
        verts = list(r.outline)
        if (len(verts) >= 2
                and int(verts[0].x) == int(verts[-1].x)
                and int(verts[0].y) == int(verts[-1].y)):
            verts = verts[:-1]
        outline = tuple(_shape_based_vertex(v, ox_mm, oy_mm) for v in verts)
        holes = tuple(_shape_based_hole(h, ox_mm, oy_mm) for h in r.holes)
        raw_net = r.net_index
        poly_idx = int(getattr(r, "polygon_index", NO_POLYGON))
        # ShapeBasedRegion sets net_index = 0xFFFF for "unassigned" rather
        # than Python None, so coerce both representations to "missing"
        # before reaching for the polygon's net.
        if (raw_net is None or raw_net == 0xFFFF) and poly_idx != NO_POLYGON:
            raw_net = _polygon_net(poly_idx)
        # A standalone unassigned SBR (0xFFFF with no parent polygon, or a
        # polygon that is itself unassigned) must become NO_NET, not the phantom
        # net 65535 — otherwise the editor-mode path treats 0xFFFF as a real
        # active net. Regions6 already maps its sentinel to None; do the same.
        if raw_net == 0xFFFF:
            raw_net = None
        # ShapeBasedRegion.kind is a ``PcbRegionKind`` enum (COPPER=0,
        # BOARD_CUTOUT=1, POLYGON_CUTOUT=2). Store the int so downstream
        # filters can do plain ``kind != 0`` to keep only copper.
        kind_value = int(getattr(r.kind, "value", r.kind))
        out.append(RawShapeBasedRegion(
            outline=outline,
            holes=holes,
            layer_id=int(r.layer),
            net_index=_net_index(raw_net),
            kind=kind_value,
            is_polygon_outline=bool(getattr(r, "is_polygon_outline", False)),
            is_keepout=bool(r.is_keepout),
            is_board_cutout=kind_value == 1,
            polygon_index=poly_idx,
        ))
    return tuple(out)


def _extract_fills(pcb, ox_mm: float, oy_mm: float) -> tuple[RawFill, ...]:
    """Extract ``Fills6`` records (Altium "Place > Fill" rectangles).

    Fills are rectangular copper primitives separate from Regions; their
    net assignment is direct (no polygon inheritance needed). Coordinates
    come in mils via ``pos1_x_mils`` / ``pos2_x_mils``.
    """
    fills = getattr(pcb, "fills", None)
    if not fills:
        return ()
    out: list[RawFill] = []
    for f in fills:
        out.append(RawFill(
            x1_mm=mils_to_mm(f.pos1_x_mils) - ox_mm,
            y1_mm=mils_to_mm(f.pos1_y_mils) - oy_mm,
            x2_mm=mils_to_mm(f.pos2_x_mils) - ox_mm,
            y2_mm=mils_to_mm(f.pos2_y_mils) - oy_mm,
            rotation_deg=float(getattr(f, "rotation", 0.0) or 0.0),
            layer_id=int(f.layer),
            net_index=_net_index(f.net_index),
            is_keepout=bool(getattr(f, "is_keepout", False)),
        ))
    return tuple(out)


def _normalise_pcb_parameters(raw: dict | None) -> dict[str, str]:
    if not raw:
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        if key is None:
            continue
        name = str(key).strip()
        if not name:
            continue
        out[name] = str(value).strip() if value is not None else ""
    return out


def _extract_pcb_components(pcb, ox_mm: float, oy_mm: float,
                            ) -> tuple[RawPcbComponent, ...]:
    out: list[RawPcbComponent] = []
    for c in pcb.components:
        # One corrupt/hand-edited component record (bad mil string, malformed
        # rotation, etc.) must not abort the entire project load — warn and
        # skip it. A dropped component only loses its designator overlay /
        # any PDN annotations it carried; the copper geometry is unaffected.
        try:
            rr = getattr(c, "raw_record", None) or {}
            out.append(RawPcbComponent(
                designator=str(c.designator),
                center=Pt2D(parse_mil_string(c.x) - ox_mm,
                            parse_mil_string(c.y) - oy_mm),
                rotation_deg=parse_rotation_string(c.rotation),
                layer_name=str(c.layer),
                footprint=str(c.footprint),
                source_designator=str(
                    rr.get("SOURCEDESIGNATOR", "") or ""),
                parameters=_normalise_pcb_parameters(
                    getattr(c, "parameters", None)),
                unique_id=str(getattr(c, "unique_id", "") or ""),
                component_kind=_component_kind_value(c),
                source_unique_id=str(rr.get("SOURCEUNIQUEID", "") or ""),
                source_hierarchical_path=str(
                    rr.get("SOURCEHIERARCHICALPATH", "") or ""),
            ))
        except Exception as exc:
            desig = getattr(c, "designator", "?")
            log.warning("Skipping malformed PCB component %s: %s", desig, exc)
    return tuple(out)


def _extract_nets(pcb) -> tuple[RawNet, ...]:
    return tuple(RawNet(name=str(n.name)) for n in pcb.nets)


def _extract_board_outline(pcb, ox_mm: float, oy_mm: float) -> tuple[Pt2D, ...]:
    """Return the PCB's mechanical board outline as a closed polyline in mm.

    altium_monkey parses the outline (sourced from the mechanical layer
    tagged Layer Type = Board, or the legacy Board6/Data VX/VY fields)
    into :class:`AltiumBoardOutline`. Each vertex begins either a line
    segment or an arc segment to the next vertex; arcs are discretised
    here into chordal samples (~0.1 mm per chord) so downstream consumers
    can treat the outline uniformly as a closed polyline.
    """
    outline = getattr(getattr(pcb, "board", None), "outline", None)
    verts = list(getattr(outline, "vertices", ()) or ())
    n = len(verts)
    if n < 3:
        return ()
    pts: list[Pt2D] = []
    for i, v in enumerate(verts):
        nxt = verts[(i + 1) % n]
        x0 = mils_to_mm(v.x_mils) - ox_mm
        y0 = mils_to_mm(v.y_mils) - oy_mm
        pts.append(Pt2D(x0, y0))
        if not v.is_arc:
            continue
        r_mm = mils_to_mm(v.radius_mils)
        if r_mm <= 0.0:
            continue
        from altium_monkey.altium_board import resolve_outline_arc_segment
        clockwise, sweep_deg = resolve_outline_arc_segment(v, nxt)
        if sweep_deg <= 0.0:
            continue
        cx = mils_to_mm(v.center_x_mils) - ox_mm
        cy = mils_to_mm(v.center_y_mils) - oy_mm
        start_ang = math.atan2(y0 - cy, x0 - cx)
        sweep_rad = math.radians(sweep_deg)
        if clockwise:
            sweep_rad = -sweep_rad
        # ~0.1 mm chord length, at least 4 samples per arc.
        steps = max(4, int(abs(sweep_rad) * r_mm / 0.1))
        for k in range(1, steps):
            t = sweep_rad * (k / steps)
            ang = start_ang + t
            pts.append(Pt2D(cx + r_mm * math.cos(ang),
                            cy + r_mm * math.sin(ang)))
    return tuple(pts)


def _v9_dielectric_gaps(pcb) -> dict[str, tuple[float | None, float | None]]:
    """Per-copper-layer (Dk, Df) of the dielectric gap below it, keyed by the
    copper layer's lower-cased display name.

    Walks the V9 physical stack (the only view carrying ``diel_constant`` /
    ``diel_loss_tangent`` per dielectric ply) top→bottom: for each copper
    layer, the plies down to the next copper layer form its gap; a multi-ply
    gap (core + prepreg) is thickness-weighted. Plies without a stored Dk/Df
    (0.0 in the record) are excluded from that average; a gap with no data at
    all yields ``None``. Boards without a V9 stack return an empty dict and
    callers fall back to the legacy per-layer ``diel_constant``.
    """
    v9 = list(getattr(pcb.board, "v9_stack", ()) or ())
    if not v9:
        return {}
    v9.sort(key=lambda l: int(getattr(l, "stack_index", 0)))
    out: dict[str, tuple[float | None, float | None]] = {}
    for i, layer in enumerate(v9):
        if not getattr(layer, "is_copper", False):
            continue
        dk_wsum = df_wsum = dk_h = df_h = 0.0
        for ply in v9[i + 1:]:
            if getattr(ply, "is_copper", False):
                break
            h = float(getattr(ply, "diel_height", 0.0) or 0.0)
            if h <= 0.0:
                continue
            dk = float(getattr(ply, "diel_constant", 0.0) or 0.0)
            df = float(getattr(ply, "diel_loss_tangent", 0.0) or 0.0)
            if dk > 0.0:
                dk_wsum += dk * h
                dk_h += h
            if df > 0.0:
                df_wsum += df * h
                df_h += h
        out[str(layer.name).strip().lower()] = (
            dk_wsum / dk_h if dk_h > 0.0 else None,
            df_wsum / df_h if df_h > 0.0 else None,
        )
    return out


def _extract_stackup(pcb) -> tuple[RawStackupLayer, ...]:
    # ``plane_net_names_by_index`` is keyed by the *internal-plane index*
    # (1..16, parsed from the ``PLANE<n>NETNAME`` board records), NOT by the
    # stackup ``layer_id``. Looking it up directly with a stackup layer_id
    # (Top=1, Mid=2..31, Bottom=32) mis-flags signal layers whose id happens
    # to collide with a plane index as planes — see issue #4. Map each plane
    # index into the legacy internal-plane layer-id space (Internal Plane 1 ==
    # PcbLayer.INTERNAL_PLANE_1 == 39) so it only matches a stackup entry that
    # is genuinely an internal plane.
    from altium_monkey.altium_record_types import PcbLayer
    plane_index_map = getattr(pcb.board, "plane_net_names_by_index", {}) or {}
    internal_plane_1 = int(PcbLayer.INTERNAL_PLANE_1.value)
    plane_map = {
        internal_plane_1 + (int(idx) - 1): name
        for idx, name in plane_index_map.items()
    }
    diel_gaps = _v9_dielectric_gaps(pcb)
    out: list[RawStackupLayer] = []
    for ls in pcb.board.layer_stackup:
        layer_id = int(ls.layer_id)
        plane_name = plane_map.get(layer_id)
        # Dk/Df of the gap below: prefer the V9 physical stack (thickness-
        # weighted across plies, carries Df); fall back to the legacy record's
        # single per-layer diel_constant (no Df there).
        v9_dk, v9_df = diel_gaps.get(str(ls.name).strip().lower(), (None, None))
        legacy_dk = float(getattr(ls, "diel_constant", 0.0) or 0.0)
        out.append(RawStackupLayer(
            layer_id=layer_id,
            name=str(ls.name),
            copper_thickness_mm=mils_to_mm(ls.copper_thickness),
            dielectric_thickness_mm=mils_to_mm(getattr(ls, "diel_height", 0.0) or 0.0),
            next_layer_id=int(ls.layer_next),
            is_plane=plane_name is not None,
            plane_net_name=str(plane_name) if plane_name is not None else None,
            mech_enabled=bool(ls.mech_enabled),
            dielectric_dk=v9_dk if v9_dk is not None
            else (legacy_dk if legacy_dk > 0.0 else None),
            dielectric_df=v9_df,
        ))

    # Internal-plane layers (legacy ids 39-54) are never present in the legacy
    # ``LAYER1..32`` stackup above — they live only in the resolved physical
    # stack. When the board assigns planes to nets, splice them in so the
    # enabled-copper walk (which follows ``next_layer_id``, already pointing at
    # the plane ids) traverses them and downstream geometry can model them.
    # Boards with no plane assignments take the early return and are byte-for-
    # byte unchanged.
    if plane_map:
        out = _splice_plane_layers(pcb, out, plane_map)
    return tuple(out)


def _parse_mil_value(s) -> float:
    """Parse an Altium dimension string like ``'20mil'`` to mm; 0.0 on blank
    / unparseable input (rules legitimately carry empty strings)."""
    if s is None:
        return 0.0
    txt = str(s).strip()
    if not txt:
        return 0.0
    try:
        return parse_mil_string(txt)
    except ValueError:
        try:
            return mils_to_mm(float(txt))
        except (TypeError, ValueError):
            return 0.0


def _extract_plane_rules(pcb) -> dict[str, float]:
    """Read the PlaneClearance (anti-pad) and PlaneConnect (thermal relief)
    design rules into a small dict of mm values.

    Falls back to Altium's defaults (20 mil clearance, 10 mil relief air gap /
    conductor width, 4 spokes) when a rule is absent, so a board that relies on
    implicit defaults still gets sensible plane geometry.
    """
    clearance_mm = mils_to_mm(20.0)
    air_gap_mm = mils_to_mm(10.0)
    conductor_mm = mils_to_mm(10.0)
    entries = 4
    for r in getattr(pcb, "rules", ()) or ():
        if not getattr(r, "enabled", True):
            continue
        kind = str(getattr(r, "rule_kind", "") or "")
        if kind == "PlaneClearance":
            v = _parse_mil_value(getattr(r, "clearance", ""))
            if v > 0.0:
                clearance_mm = v
        elif kind == "PlaneConnect":
            settings = getattr(r, "connect_settings", None) or {}
            cs = settings.get("DEFAULT") or next(iter(settings.values()), None)
            if cs is not None:
                ag = _parse_mil_value(getattr(cs, "relief_air_gap", ""))
                cw = _parse_mil_value(getattr(cs, "relief_conductor_width", ""))
                if ag > 0.0:
                    air_gap_mm = ag
                if cw > 0.0:
                    conductor_mm = cw
                try:
                    entries = int(str(getattr(cs, "relief_entries", "") or 4))
                except (TypeError, ValueError):
                    entries = 4
    return {
        "clearance_mm": clearance_mm,
        "air_gap_mm": air_gap_mm,
        "conductor_mm": conductor_mm,
        "entries": float(entries),
    }


def _splice_plane_layers(
    pcb,
    legacy_rows: list[RawStackupLayer],
    plane_net_by_id: dict[int, str],
) -> list[RawStackupLayer]:
    """Insert internal-plane :class:`RawStackupLayer` rows into the conductive
    stack, ordered and dimensioned from the resolved physical layer stack.

    The resolved stack is the only altium_monkey view that includes internal
    planes (legacy ids 39-54) in physical order, with their copper thickness
    and the sub-dielectric thicknesses between adjacent conductors. We use it
    to rebuild the conductive chain (signal + plane) with correct
    ``next_layer_id`` linkage and ``dielectric_thickness_mm`` (the gap to the
    next conductor below). Per-layer copper thickness, display name and
    mech flag are preserved from the legacy row when one exists, so signal
    layers keep the exact values the legacy parser produced.
    """
    from altium_monkey.altium_record_types import PcbLayer
    from altium_monkey.altium_resolved_layer_stack import (
        resolved_layer_stack_from_pcbdoc,
    )

    ip1 = int(PcbLayer.INTERNAL_PLANE_1.value)
    ip16 = int(PcbLayer.INTERNAL_PLANE_16.value)
    board_record = getattr(pcb.board, "raw_record", {}) or {}

    def _plane_pullback_mm(layer_id: int) -> float:
        # PLANE<n>PULLBACK is keyed by the internal-plane index (1..16).
        index = layer_id - ip1 + 1
        return _parse_mil_value(board_record.get(f"PLANE{index}PULLBACK", ""))

    def _is_conductor(rl) -> bool:
        lid = rl.legacy_id
        return lid is not None and (1 <= lid <= 32 or ip1 <= lid <= ip16)

    resolved = list(resolved_layer_stack_from_pcbdoc(pcb).layers)

    # Walk the resolved stack top→bottom, collecting each conductor with the
    # summed dielectric thickness down to the next conductor.
    conductors: list[tuple[int, float, float, str]] = []  # id, cu_mils, diel_mils, name
    for i, rl in enumerate(resolved):
        if not _is_conductor(rl):
            continue
        diel = 0.0
        for nxt in resolved[i + 1:]:
            if _is_conductor(nxt):
                break
            diel += float(nxt.thickness_mils or 0.0)
        conductors.append(
            (int(rl.legacy_id), float(rl.thickness_mils or 0.0), diel,
             str(rl.display_name or "")))

    legacy_by_id = {r.layer_id: r for r in legacy_rows}
    diel_gaps = _v9_dielectric_gaps(pcb)

    rebuilt: list[RawStackupLayer] = []
    for k, (lid, cu_mils, diel_mils, disp_name) in enumerate(conductors):
        next_id = conductors[k + 1][0] if k + 1 < len(conductors) else 0
        plane_net = plane_net_by_id.get(lid)
        legacy = legacy_by_id.get(lid)
        if legacy is not None:
            # Signal layer already parsed: keep its copper/name/mech, only
            # re-link next_layer_id and dielectric to the resolved neighbour
            # (a no-op on plane-free boards, where the chain is unchanged).
            # The re-linked gap may differ from the legacy one (a plane was
            # spliced in between), so re-look-up its Dk/Df by name too.
            v9_dk, v9_df = diel_gaps.get(legacy.name.strip().lower(),
                                         (legacy.dielectric_dk,
                                          legacy.dielectric_df))
            rebuilt.append(RawStackupLayer(
                layer_id=lid,
                name=legacy.name,
                copper_thickness_mm=legacy.copper_thickness_mm,
                dielectric_thickness_mm=mils_to_mm(diel_mils),
                next_layer_id=next_id,
                is_plane=legacy.is_plane,
                plane_net_name=legacy.plane_net_name,
                mech_enabled=legacy.mech_enabled,
                dielectric_dk=v9_dk,
                dielectric_df=v9_df,
            ))
        else:
            # Internal-plane layer, sourced wholly from the resolved stack.
            name = disp_name or f"Internal Plane {lid - ip1 + 1}"
            v9_dk, v9_df = diel_gaps.get(name.strip().lower(), (None, None))
            rebuilt.append(RawStackupLayer(
                layer_id=lid,
                name=name,
                copper_thickness_mm=mils_to_mm(cu_mils),
                dielectric_thickness_mm=mils_to_mm(diel_mils),
                next_layer_id=next_id,
                is_plane=plane_net is not None,
                plane_net_name=plane_net,
                mech_enabled=False,
                plane_pullback_mm=_plane_pullback_mm(lid),
                dielectric_dk=v9_dk,
                dielectric_df=v9_df,
            ))

    # Preserve any legacy rows the resolved conductive walk didn't cover
    # (e.g. disabled / orphan layers) so nothing silently disappears.
    covered = {r.layer_id for r in rebuilt}
    for r in legacy_rows:
        if r.layer_id not in covered:
            rebuilt.append(r)
    return rebuilt


def _is_pdn_ignore_param(name: str | None, text: str | None) -> bool:
    """True when a schematic pin parameter means "exclude from PDN terminals".

    Accepts ``PDN_IGNORE`` with a truthy value (``1`` / ``TRUE`` / ``YES`` /
    ``IGNORE``) or the alias name ``PDN`` with value ``IGNORE``. Empty values
    do not count.
    """
    if name is None:
        return False
    n = str(name).strip().upper()
    v = str(text).strip().upper() if text is not None else ""
    if not v:
        return False
    if n == "PDN_IGNORE":
        return v in ("1", "TRUE", "YES", "IGNORE")
    if n == "PDN":
        return v == "IGNORE"
    return False


def _sch_component_pins(comp) -> list:
    """Typed pin objects for a schematic component (``pins`` or children)."""
    pins = list(getattr(comp, "pins", ()) or ())
    if pins:
        return pins
    return [
        c for c in (getattr(comp, "children", ()) or ())
        if type(c).__name__ == "AltiumSchPin"
    ]


def _is_pin_owned_parameter(obj) -> bool:
    """True for AltiumSchParameter or a duck-typed name/text/owner_index object."""
    cls = type(obj).__name__
    if cls == "AltiumSchPin":
        return False
    if cls == "AltiumSchParameter":
        return True
    return (
        hasattr(obj, "name")
        and hasattr(obj, "text")
        and hasattr(obj, "owner_index")
    )


def _sheet_ignored_pins_by_component(
    components: list,
    all_objects,
) -> list[frozenset[str]]:
    """One pass over ``all_objects``: ignored pin sets parallel to ``components``.

    Builds a sheet-wide pin-index → (component index, designator) map, then
    scans parameters once instead of re-scanning the sheet per component.

    Pin-owned ``PDN_IGNORE`` is discovered two ways (both match altium_monkey
    SchDoc layout): ``pin.pin_parameters`` when the library attached them, and
    sheet ``all_objects`` parameters whose ``owner_index`` equals the pin's
    ``_record_index``. Component-owned parameters (owner → component record)
    are ignored here — use ``PDN_IGNORE_PINS`` on the part when pin-level
    ownership cannot be resolved.
    """
    ignored: list[set[str]] = [set() for _ in components]
    pin_index_to_comp_des: dict[int, tuple[int, str]] = {}

    for ci, comp in enumerate(components):
        for pin in _sch_component_pins(comp):
            des = getattr(pin, "designator", None)
            if not des:
                continue
            des_s = str(des)
            idx = getattr(pin, "_record_index", None)
            if idx is not None:
                try:
                    pin_index_to_comp_des[int(idx)] = (ci, des_s)
                except (TypeError, ValueError):
                    pass
            for param in getattr(pin, "pin_parameters", ()) or ():
                if _is_pdn_ignore_param(
                    getattr(param, "name", None), getattr(param, "text", None),
                ):
                    ignored[ci].add(des_s.upper())

    if pin_index_to_comp_des:
        for obj in all_objects or ():
            if not _is_pin_owned_parameter(obj):
                continue
            owner = getattr(obj, "owner_index", None)
            if owner is None:
                continue
            try:
                owner_i = int(owner)
            except (TypeError, ValueError):
                continue
            hit = pin_index_to_comp_des.get(owner_i)
            if hit is None:
                continue
            if _is_pdn_ignore_param(
                getattr(obj, "name", None), getattr(obj, "text", None),
            ):
                ci, des_s = hit
                ignored[ci].add(des_s.upper())

    return [frozenset(s) for s in ignored]


def _ignored_pins_from_sch_component(comp, all_objects) -> frozenset[str]:
    """Collect pin designators with a pin-owned PDN_IGNORE parameter.

    Thin wrapper around :func:`_sheet_ignored_pins_by_component` for a single
    component (unit tests and callers that already have one part).
    """
    return _sheet_ignored_pins_by_component([comp], all_objects)[0]


def _extract_sch_component(
    comp, schdoc_name: str, ignored_pins: frozenset[str] | None = None,
) -> RawSchComponent | None:
    """Extract one component's designator + parameters + pin list from its children.

    Returns None if the component has no AltiumSchDesignator child (rare; usually
    means a non-instantiated symbol — safe to skip for PDN purposes).

    ``ignored_pins`` comes from the sheet-level batch in
    :func:`_extract_sch_components`; when omitted, pin-owned ignores are not
    resolved here (callers must pass them or use the batch path).
    """
    designator: str | None = None
    parameters: dict[str, str] = {}
    pins: list[str] = []
    for child in comp.children:
        cls_name = type(child).__name__
        if cls_name == "AltiumSchDesignator":
            designator = str(getattr(child, "text", ""))
        elif cls_name == "AltiumSchParameter":
            name = getattr(child, "name", None)
            if not name:
                continue
            parameters[str(name).strip()] = str(getattr(child, "text", ""))
        elif cls_name == "AltiumSchPin":
            pin_designator = getattr(child, "designator", None)
            if pin_designator:
                pins.append(str(pin_designator))
    if designator is None:
        return None
    # Prefer pin list from the typed ``pins`` collection when children
    # enumeration missed some (OwnerIndex hierarchy).
    if not pins:
        for pin in getattr(comp, "pins", ()) or ():
            des = getattr(pin, "designator", None)
            if des:
                pins.append(str(des))
    return RawSchComponent(
        designator=designator,
        schdoc_name=schdoc_name,
        parameters=parameters,
        pin_designators=tuple(pins),
        component_kind=_component_kind_value(comp),
        ignored_pins=ignored_pins if ignored_pins is not None else frozenset(),
    )


def _schdoc_storage_key(abs_path: Path, project_root: Path) -> str:
    """Stable lowercase dict key for one SchDoc path.

    Prefer a path relative to the ``.PrjPcb`` directory (lowercase, ``/``).
    Files outside that tree use the absolute path so two external sheets with
    the same basename do not collide.
    """
    return _schdoc_display_path(abs_path, project_root).lower()


def _schdoc_display_path(abs_path: Path, project_root: Path) -> str:
    """Case-preserving relative (or absolute) SchDoc path for annotations/UI."""
    abs_path = abs_path.resolve()
    root = project_root.resolve()
    try:
        return str(abs_path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(abs_path).replace("\\", "/")


def _extract_sch_components(
    design,
    prjpcb_path: Path,
) -> tuple[RawSchComponent, ...]:
    out: list[RawSchComponent] = []
    root = prjpcb_path.parent
    for sd in design.schdocs:
        if not getattr(sd, "filepath", None):
            continue
        # Preserve casing for diagnostics; lookups lower-case on compare.
        schdoc_name = _schdoc_display_path(Path(sd.filepath), root)
        all_objects = getattr(sd, "all_objects", None) or ()
        components = list(sd.components)
        ignored_sets = _sheet_ignored_pins_by_component(components, all_objects)
        for comp, ignored in zip(components, ignored_sets):
            rec = _extract_sch_component(comp, schdoc_name, ignored)
            if rec is not None:
                out.append(rec)
    return tuple(out)


def _parameters_from_sch_children(children) -> dict[str, str]:
    """Collect ``AltiumSchParameter`` name→text from a parent object's children."""
    parameters: dict[str, str] = {}
    for child in children or ():
        if type(child).__name__ != "AltiumSchParameter":
            continue
        name = getattr(child, "name", None)
        if not name:
            continue
        parameters[str(name).strip()] = str(getattr(child, "text", ""))
    return parameters


def _parameters_owned_by_index(schdoc, owner_index: int) -> dict[str, str]:
    """Parameters on ``schdoc.parameters`` whose ``owner_index`` matches.

    altium_monkey attaches sheet-symbol parameters this way (OwnerIndex →
    sheet symbol ``index_in_sheet``). They are *not* placed on
    ``symbol.children`` — that list only holds entries / name / filename.
    """
    parameters: dict[str, str] = {}
    for param in getattr(schdoc, "parameters", ()) or ():
        if getattr(param, "owner_index", None) != owner_index:
            continue
        name = getattr(param, "name", None)
        if not name:
            continue
        parameters[str(name).strip()] = str(getattr(param, "text", "") or "")
    return parameters


# Sheet-symbol PDN overrides are ``PDN_<DesignatorWithDigit>_*`` (e.g.
# ``PDN_J1_I``, ``PDN_U12A_I``). BOM / library params on the same symbol
# are dropped.
_PDN_SHEET_OVERRIDE_NAME_RE = re.compile(
    r"^PDN_[A-Za-z]*\d[A-Za-z0-9]*_", re.IGNORECASE,
)


def _filter_sheet_pdn_parameters(parameters: dict[str, str]) -> dict[str, str]:
    """Keep only designator-targeted ``PDN_<Des>_*`` sheet-symbol keys."""
    return {
        k: v for k, v in parameters.items()
        if _PDN_SHEET_OVERRIDE_NAME_RE.match(str(k).strip())
    }


def _extract_sch_sheet_symbol(
    info, parent_schdoc: str, schdoc=None,
) -> RawSchSheetSymbol | None:
    """Extract one sheet symbol's identity + parameters."""
    record = getattr(info, "record", None)
    if record is None:
        return None
    unique_id = str(getattr(info, "unique_id", "") or "").strip()
    if not unique_id:
        unique_id = str(getattr(record, "unique_id", "") or "").strip()
    child_filename = str(getattr(info, "file_name", "") or "").strip()
    if not child_filename:
        return None
    sheet_name = str(getattr(info, "designator", "") or "").strip()
    # Children rarely carry parameters for sheet symbols; OwnerIndex on the
    # schdoc's flat parameter list is the authoritative path.
    parameters = _parameters_from_sch_children(getattr(record, "children", ()))
    owner_index = getattr(record, "index_in_sheet", None)
    if schdoc is not None and owner_index is not None:
        parameters.update(_parameters_owned_by_index(schdoc, owner_index))
    parameters = _filter_sheet_pdn_parameters(parameters)
    return RawSchSheetSymbol(
        parent_schdoc=parent_schdoc,
        sheet_name=sheet_name,
        child_filename=child_filename,
        unique_id=unique_id,
        parameters=parameters,
    )


def _sheet_symbol_info_wrapper(record):
    """Adapt a bare ``AltiumSchSheetSymbol`` to the info-wrapper interface."""
    class _Info:
        def __init__(self, rec):
            self.record = rec

        @property
        def designator(self):
            sn = getattr(self.record, "sheet_name", None)
            return getattr(sn, "text", "") if sn is not None else ""

        @property
        def file_name(self):
            fn = getattr(self.record, "file_name", None)
            return getattr(fn, "text", "") if fn is not None else ""

        @property
        def unique_id(self):
            return getattr(self.record, "unique_id", "") or ""

    return _Info(record)


def _extract_sch_sheet_symbols(design) -> tuple[RawSchSheetSymbol, ...]:
    out: list[RawSchSheetSymbol] = []
    for sd in design.schdocs:
        parent_name = sd.filepath.name if getattr(sd, "filepath", None) else ""
        getter = getattr(sd, "get_sheet_symbols", None)
        symbols = getter() if callable(getter) else getattr(sd, "sheet_symbols", ())
        for info in symbols or ():
            if type(info).__name__ == "AltiumSchSheetSymbol":
                info = _sheet_symbol_info_wrapper(info)
            rec = _extract_sch_sheet_symbol(info, parent_name, schdoc=sd)
            if rec is not None:
                out.append(rec)
    return tuple(out)


# --- public entry -------------------------------------------------------------

def list_pcbdoc_paths(prjpcb_path: str | Path) -> list[Path]:
    """Return every ``.PcbDoc`` referenced by ``prjpcb_path``, in project order.

    Cheap: just opens the .PrjPcb to enumerate document paths; does not
    parse the PCB binary OR any SchDoc. Used by the GUI launcher / CLI to
    pick a board when the project contains more than one, and on the
    cache-hit fast path before the solve cache lookup — so it must not
    trigger AltiumDesign.from_prjpcb, which eagerly parses every SchDoc.
    """
    # AltiumPrjPcb is not exposed at altium_monkey's top level (its
    # __getattr__ lazy-loader only handles AltiumDesign / AltiumSchDoc /
    # AltiumPcbDoc / etc.) — import from the submodule directly.
    from altium_monkey.altium_prjpcb import AltiumPrjPcb
    prjpcb_path = Path(prjpcb_path)
    if not prjpcb_path.exists():
        raise FileNotFoundError(f"PrjPcb not found: {prjpcb_path}")
    return list(AltiumPrjPcb(prjpcb_path).get_pcbdoc_paths())


def _compile_schematic_netlist(
    design: AltiumDesign,
) -> tuple[Netlist | None, tuple[tuple[str, str], ...]]:
    """Compile the project schematic netlist for local-net name resolution.

    Returns ``(netlist, physical_sheet_names)`` where ``physical_sheet_names``
    maps compiled physical page ids to logical ``*.SchDoc`` names
    (altium_monkey ≥ 2026.7). The map is empty on releases without a
    compiled model, and callers must then degrade rather than guess a sheet
    from an unmapped page id.
    """
    if not design.schdocs:
        return None, ()

    netlist = None
    compiled = None
    if hasattr(design, "compile"):
        # >= 2026.7 exposes the compiled model, which carries the page map.
        try:
            compiled = design.compile()
            netlist = compiled.to_netlist()
        except Exception as exc:
            log.warning(
                "Could not compile schematic netlist via design.compile(): %s",
                exc,
            )
            compiled = None
    if netlist is None:
        # Older releases have no compile(). ``to_netlist()`` reaches the same
        # compiler through the design's OWN options, which carry the merged
        # per-sheet parameters that net labels substitute. Rebuilding options
        # with ``NetlistOptions.from_prjpcb()`` drops that merge -- only
        # ``AltiumDesign.from_prjpcb`` adds it -- which silently renames nets
        # on a project using =Parameter substitution rather than degrading.
        try:
            netlist = design.to_netlist()
        except Exception as exc:
            log.warning("Could not compile schematic netlist: %s", exc)
            return None, ()
    if netlist is None:
        return None, ()

    # Harvested separately from the compile: losing the page map must not
    # throw away a netlist that built cleanly.
    sheet_names: tuple[tuple[str, str], ...] = ()
    if compiled is not None:
        try:
            sheet_names = _harvest_physical_sheet_names(compiled)
        except Exception as exc:
            log.warning(
                "Could not read the compiled physical-page map (%s); local-net "
                "sheet matching falls back to alias resolution.", exc,
            )
    return netlist, sheet_names


def _harvest_physical_sheet_names(compiled) -> tuple[tuple[str, str], ...]:
    """``(physical page id, logical sheet name)`` pairs from a compiled design.

    Prefers ``source_path`` -- the project-relative path -- over ``file_name``,
    which is the bare leaf: harvesting the leaf collapses ``SubA/Power.SchDoc``
    and ``SubB/Power.SchDoc`` onto one name, the very directory collision
    :func:`~fypa.altium.annotations._sheet_name_matches` exists to keep apart.
    Falls back to ``file_name`` on a release exposing only that.
    """
    pairs: list[tuple[str, str]] = []
    for doc in (getattr(compiled, "physical_documents", None) or ()):
        doc_id = getattr(doc, "id", None)
        name = (getattr(doc, "source_path", None)
                or getattr(doc, "file_name", None))
        if doc_id and name:
            pairs.append((str(doc_id), str(name)))
    return tuple(pairs)


def _collect_schdoc_paths(
    design: AltiumDesign,
    prjpcb_path: Path,
) -> dict[str, str]:
    """Map unique SchDoc keys → absolute path strings for lazy sheet compiles.

    Primary key is :func:`_schdoc_storage_key` (project-relative, or absolute
    when outside the tree). When a basename is unique across the project it is
    also registered so callers that only know ``Child.SchDoc`` still resolve.
    """
    root = prjpcb_path.parent
    entries: list[tuple[str, str, str]] = []
    basename_counts: dict[str, int] = {}
    for sch in design.schdocs:
        if not getattr(sch, "filepath", None):
            continue
        abs_path = Path(sch.filepath).resolve()
        key = _schdoc_storage_key(abs_path, root)
        base = abs_path.name.lower()
        entries.append((key, str(abs_path), base))
        basename_counts[base] = basename_counts.get(base, 0) + 1

    out: dict[str, str] = {}
    for key, abs_s, base in entries:
        out[key] = abs_s
        if basename_counts.get(base, 0) == 1:
            out[base] = abs_s
    return out


def extract_project(prjpcb_path: str | Path,
                    pcbdoc_selector: str | Path | None = None,
                    ) -> ExtractedProject:
    """Parse a `.PrjPcb` and return an :class:`ExtractedProject` snapshot.

    The project's PCB document is loaded via :meth:`AltiumDesign.load_pcbdoc`;
    all schematic documents found in the project are scanned for component
    parameters (the source of ADNE_* annotations in the next pipeline stage).

    ``pcbdoc_selector`` chooses among multiple ``.PcbDoc`` files in the
    project (forwarded to ``AltiumDesign.load_pcbdoc``). Accepts an
    absolute path, a project-relative path, a filename, or a stem.
    ``None`` keeps altium_monkey's default (first PcbDoc).
    """
    prjpcb_path = Path(prjpcb_path)
    if not prjpcb_path.exists():
        raise FileNotFoundError(f"PrjPcb not found: {prjpcb_path}")

    log.info("Loading Altium project: %s", prjpcb_path)
    # Drop annotation memoization from any previous project so a reload of the
    # same path cannot reuse stale child-sheet / resolver state.
    from fypa.altium.annotations import clear_annotation_caches
    clear_annotation_caches()

    design = AltiumDesign.from_prjpcb(str(prjpcb_path))
    pcb = design.load_pcbdoc(selector=pcbdoc_selector)
    if pcb is None:
        raise RuntimeError(
            f"Project {prjpcb_path.name} does not reference a PcbDoc; "
            "FYPA needs a PCB document for power analysis."
        )
    pcbdoc_path = Path(pcb.filepath).resolve() if pcb.filepath else prjpcb_path

    # Altium PCB editor displays coordinates relative to the user-defined
    # origin (Board6/ORIGINX,ORIGINY, stored in mils). Subtracting it here
    # means every Pt2D — and therefore the viewer's cursor readout, the
    # Nodes/Vias tables, and the saved metadata — matches what Altium shows.
    origin_x_mils = float(getattr(pcb.board, "origin_x", 0.0) or 0.0)
    origin_y_mils = float(getattr(pcb.board, "origin_y", 0.0) or 0.0)
    ox_mm = mils_to_mm(origin_x_mils)
    oy_mm = mils_to_mm(origin_y_mils)

    compiled_netlist, physical_sheet_names = _compile_schematic_netlist(design)
    schdoc_paths = _collect_schdoc_paths(design, prjpcb_path)

    return ExtractedProject(
        prjpcb_path=prjpcb_path,
        pcbdoc_path=pcbdoc_path,
        tracks=_extract_tracks(pcb, ox_mm, oy_mm),
        arcs=_extract_arcs(pcb, ox_mm, oy_mm),
        vias=_extract_vias(pcb, ox_mm, oy_mm),
        pads=_extract_pads(pcb, ox_mm, oy_mm),
        texts=_extract_texts(pcb, ox_mm, oy_mm),
        regions=_extract_regions(pcb, ox_mm, oy_mm),
        shape_based_regions=_extract_shape_based_regions(pcb, ox_mm, oy_mm),
        fills=_extract_fills(pcb, ox_mm, oy_mm),
        pcb_components=_extract_pcb_components(pcb, ox_mm, oy_mm),
        nets=_extract_nets(pcb),
        stackup=_extract_stackup(pcb),
        sch_components=_extract_sch_components(design, prjpcb_path),
        sch_sheet_symbols=_extract_sch_sheet_symbols(design),
        compiled_netlist=compiled_netlist,
        schdoc_paths=schdoc_paths,
        sheet_netlists={},
        physical_sheet_names=physical_sheet_names,
        board_origin_mm=Pt2D(ox_mm, oy_mm),
        board_outline=_extract_board_outline(pcb, ox_mm, oy_mm),
        **_plane_rule_kwargs(pcb),
    )


def _plane_rule_kwargs(pcb) -> dict[str, float | int]:
    """Plane modelling rules as ExtractedProject keyword args (empty-safe)."""
    rules = _extract_plane_rules(pcb)
    return {
        "plane_clearance_mm": rules["clearance_mm"],
        "plane_relief_air_gap_mm": rules["air_gap_mm"],
        "plane_relief_conductor_width_mm": rules["conductor_mm"],
        "plane_relief_entries": int(rules["entries"]),
    }


# --- self-check ---------------------------------------------------------------

def _summarise(proj: ExtractedProject) -> str:
    enabled = proj.enabled_copper_layer_ids()
    enabled_str = ", ".join(f"{i}({proj.stackup[i-1].name})" if 1 <= i <= len(proj.stackup) else str(i) for i in enabled)
    return (
        f"Project: {proj.prjpcb_path.name}\n"
        f"  tracks       : {len(proj.tracks):>6}\n"
        f"  arcs         : {len(proj.arcs):>6}\n"
        f"  vias         : {len(proj.vias):>6}\n"
        f"  pads         : {len(proj.pads):>6}\n"
        f"  regions      : {len(proj.regions):>6}\n"
        f"  shape_based_regions: {len(proj.shape_based_regions):>6}\n"
        f"  fills        : {len(proj.fills):>6}\n"
        f"  pcb_components: {len(proj.pcb_components):>6}\n"
        f"  nets         : {len(proj.nets):>6}\n"
        f"  stackup rows : {len(proj.stackup):>6}\n"
        f"  sch_components: {len(proj.sch_components):>6}\n"
        f"  sch_sheet_symbols: {len(proj.sch_sheet_symbols):>6}\n"
        f"  enabled copper layers (Top->Bottom): {enabled_str}\n"
    )


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if len(sys.argv) != 2:
        print("usage: python -m fypa.altium.extract PATH_TO.PrjPcb", file=sys.stderr)
        sys.exit(2)
    proj = extract_project(sys.argv[1])
    print(_summarise(proj))
