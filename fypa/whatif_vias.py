"""Vias a What-If scenario adds to the board ("punched" vias).

A punched via exists only in its scenario: it is appended to the
ExtractedProject's ``vias`` for that scenario's solve and nowhere else, so
the base design, its report and its caches never see it. Once appended it
is an ordinary :class:`~fypa.altium.extract.RawVia` - the copper bucketing
gives it a pad disc on every layer it spans, plane sheets cut an anti-pad
for it, and the coupling network gives it a barrel - so nothing downstream
needs to know it was added.

Vias are stored by net *name*, not index: net indices are not stable
across a reload of the design, names are.

Qt-free so it can be tested headless.
"""
from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from typing import Any

# Metadata key the solve worker sets on a what-if result that added vias:
# ``{"count": n, "unconnected": k}``. The added vias are the last ``count``
# entries of ``metadata["vias"]``; ``unconnected`` of them joined copper on
# fewer than two layers and so carry no current.
METADATA_KEY = "whatif_added_vias"


@dataclass(frozen=True)
class AddedVia:
    """One punched via. Lengths in mm, layers as Altium layer ids."""

    x_mm: float
    y_mm: float
    net: str
    layer_start: int
    layer_end: int
    diameter_mm: float
    hole_diameter_mm: float
    # IPC-4761 protection / fill, copied from the via it was modelled on:
    # a conductive fill shunts the barrel, so it changes the solve.
    ipc4761_via_type: int = 0
    fill_material: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "x_mm": float(self.x_mm),
            "y_mm": float(self.y_mm),
            "net": str(self.net),
            "layer_start": int(self.layer_start),
            "layer_end": int(self.layer_end),
            "diameter_mm": float(self.diameter_mm),
            "hole_diameter_mm": float(self.hole_diameter_mm),
            "ipc4761_via_type": int(self.ipc4761_via_type),
            "fill_material": str(self.fill_material),
        }

    @classmethod
    def from_dict(cls, d: Any) -> AddedVia | None:
        """The via ``d`` describes, or ``None`` when it is malformed
        (missing field, non-finite number, empty net, hole not inside the
        pad)."""
        if not isinstance(d, dict):
            return None
        try:
            v = cls(
                x_mm=float(d["x_mm"]),
                y_mm=float(d["y_mm"]),
                net=str(d.get("net") or "").strip(),
                layer_start=int(d["layer_start"]),
                layer_end=int(d["layer_end"]),
                diameter_mm=float(d["diameter_mm"]),
                hole_diameter_mm=float(d["hole_diameter_mm"]),
                ipc4761_via_type=int(d.get("ipc4761_via_type") or 0),
                fill_material=str(d.get("fill_material") or ""),
            )
        except (KeyError, TypeError, ValueError):
            return None
        return v if not v.problem() else None

    def problem(self) -> str:
        """Why this via can't be used, or ``""``. Checks the via on its own;
        :func:`resolve_added_vias` checks it against the board."""
        nums = (self.x_mm, self.y_mm, self.diameter_mm, self.hole_diameter_mm)
        if not all(math.isfinite(n) for n in nums):
            return "a coordinate or size is not a number"
        if not self.net:
            return "no net"
        if self.hole_diameter_mm <= 0.0:
            return "drill must be larger than zero"
        if self.diameter_mm <= self.hole_diameter_mm:
            return "pad must be larger than the drill"
        return ""


def vias_from_dicts(raw: Any) -> list[AddedVia]:
    """The well-formed vias in ``raw`` (a list of dicts); others dropped."""
    out: list[AddedVia] = []
    for d in raw or []:
        v = AddedVia.from_dict(d)
        if v is not None:
            out.append(v)
    return out


def describe(vias: list[AddedVia],
             clearance_mm: float | None = None) -> list[str]:
    """``["Added vias: 3 on GND, 1 on +3V3 (clearance 0.3 mm)"]`` (empty
    when none), nets in the order first used. ``clearance_mm`` None reads
    "board clearance"."""
    if not vias:
        return []
    counts: dict[str, int] = {}
    for v in vias:
        counts[v.net] = counts.get(v.net, 0) + 1
    parts = [f"{n} on {net}" for net, n in counts.items()]
    clr = ("board clearance" if clearance_mm is None
           else f"clearance {clearance_mm:.3g} mm")
    return [f"Added vias: {', '.join(parts)} ({clr})"]


# --- repour: clearance holes in other nets' copper ----------------------------
#
# A via dropped into another net's pour makes the layout tool repour that
# pour with a clearance hole around it. The what-if solve does the same: each
# punched via cuts a disc of (pad radius + clearance) out of every OTHER net's
# copper on the layers it spans - pours and internal-plane sheets alike. The
# punch tool refuses a via whose pad touches another net's tracks, pads or
# fixed regions, so the disc only ever bites into copper a repour would move.

# Altium's default PlaneClearance (20 mil): used when the board carries no
# value (a Gerber import, or a result saved before the rule was recorded).
DEFAULT_CLEARANCE_MM = 0.508


def board_clearance_mm(metadata_or_extracted) -> float:
    """The board's PlaneClearance rule in mm (``metadata["plane_clearance_mm"]``
    or ``ExtractedProject.plane_clearance_mm``), else the Altium default."""
    if isinstance(metadata_or_extracted, dict):
        v = metadata_or_extracted.get("plane_clearance_mm")
    else:
        v = getattr(metadata_or_extracted, "plane_clearance_mm", None)
    if v is None:
        return DEFAULT_CLEARANCE_MM
    try:
        f = float(v)
    except (TypeError, ValueError):
        return DEFAULT_CLEARANCE_MM
    return f if f > 0.0 and math.isfinite(f) else DEFAULT_CLEARANCE_MM


@dataclass(frozen=True)
class ViaCutout:
    """One punched via's clearance hole: centre, the via's net (index) and
    the hole radius, on the layers it spans."""

    x_mm: float
    y_mm: float
    net_index: int
    radius_mm: float
    layer_ids: tuple[int, ...]


def cut_clearances(layers, cutouts) -> list:
    """``layers`` (GeometryLayers) with each cutout's disc removed from every
    OTHER net's copper on the cutout's layers. Layers it doesn't touch are
    returned as the same objects; the input list is not changed."""
    import shapely
    import shapely.geometry as sg

    from fypa.altium_geometry import _ensure_multipolygon

    by_layer: dict[int, list[tuple[object, int]]] = {}
    for c in cutouts or ():
        disc = sg.Point(c.x_mm, c.y_mm).buffer(c.radius_mm, 32)
        for lid in c.layer_ids:
            by_layer.setdefault(int(lid), []).append((disc, int(c.net_index)))
    out = []
    for gl in layers:
        holes = [d for d, net in by_layer.get(int(gl.layer_id), ())
                 if net != gl.net_index]
        if not holes or gl.shape is None or gl.shape.is_empty:
            out.append(gl)
            continue
        cut = shapely.union_all(holes)
        if not gl.shape.intersects(cut):
            out.append(gl)
            continue
        out.append(dataclasses.replace(
            gl, shape=_ensure_multipolygon(gl.shape.difference(cut))))
    return out


class CutFuture:
    """Stand-in for a future of GeometryLayers whose ``result()`` has the
    clearance holes cut (the non-active nets' display copper)."""

    def __init__(self, future, cutouts) -> None:
        self._future = future
        self._cutouts = cutouts

    def result(self, timeout=None):
        return cut_clearances(self._future.result(timeout), self._cutouts)

    def done(self) -> bool:
        return self._future.done()


def _span_positions(v: AddedVia, enabled: list[int]) -> tuple[int, int] | None:
    pos = {lid: i for i, lid in enumerate(enabled)}
    a, b = pos.get(int(v.layer_start)), pos.get(int(v.layer_end))
    if a is None or b is None:
        return None
    return (a, b) if a <= b else (b, a)


def resolve_added_vias(extracted, vias: list[AddedVia]):
    """``(raw_vias, warnings)``: each usable via as a ``RawVia`` on
    ``extracted``'s nets and layers, plus one warning per via skipped.

    A via is skipped when its net isn't on the board, an end layer isn't an
    enabled copper layer, or it spans a single layer (no barrel)."""
    from fypa.altium.extract import Pt2D, RawVia

    index_of: dict[str, int] = {}
    for i, n in enumerate(extracted.nets):
        index_of.setdefault(n.name, i)   # first index wins, as elsewhere
    enabled = list(extracted.enabled_copper_layer_ids())
    raw: list = []
    warnings: list[str] = []
    for v in vias:
        where = f"Added via at ({v.x_mm:.3f}, {v.y_mm:.3f}) mm on {v.net or '?'}"
        why = v.problem()
        if not why and v.net not in index_of:
            why = "that net is not on the board"
        span = None if why else _span_positions(v, enabled)
        if not why:
            if span is None:
                why = "an end layer is not an enabled copper layer"
            elif span[0] == span[1]:
                why = "it starts and ends on the same layer"
        if why:
            warnings.append(f"{where} skipped: {why}.")
            continue
        raw.append(RawVia(
            center=Pt2D(float(v.x_mm), float(v.y_mm)),
            diameter_mm=float(v.diameter_mm),
            hole_diameter_mm=float(v.hole_diameter_mm),
            layer_start=int(v.layer_start),
            layer_end=int(v.layer_end),
            net_index=index_of[v.net],
            ipc4761_via_type=int(v.ipc4761_via_type),
            fill_material=str(v.fill_material),
        ))
    return raw, warnings


def apply_added_vias(loaded, vias: list[AddedVia],
                     clearance_mm: float | None = None):
    """``(new_loaded, warnings, n_added)``: ``loaded`` with the usable vias
    appended to its extract's ``vias``, and their repour clearance holes
    recorded as ``new_loaded.whatif_via_cutouts`` (cut by ``build_problem``,
    see :func:`cut_clearances`). ``clearance_mm`` None = the board's
    PlaneClearance rule.

    ``loaded`` itself is left untouched (a fresh ExtractedProject and
    LoadedProject are built), so the pristine design the viewer keeps is
    never changed. The new vias go at the END of ``vias``: the per-net
    geometry cache relies on that to rebuild only the nets they touch
    (see :func:`fypa.cli.incremental_per_net_geometry`), and the result's
    ``metadata["vias"]`` lists them last."""
    raw, warnings = resolve_added_vias(loaded.extracted, vias)
    if not raw:
        return loaded, warnings, 0
    from fypa.altium.loader import replace_loaded
    ext = loaded.extracted
    clr = (board_clearance_mm(ext) if clearance_mm is None
           else max(0.0, float(clearance_mm)))
    enabled = list(ext.enabled_copper_layer_ids())
    cutouts = tuple(
        ViaCutout(x_mm=float(v.center.x), y_mm=float(v.center.y),
                  net_index=int(v.net_index),
                  radius_mm=float(v.diameter_mm) / 2.0 + clr,
                  layer_ids=tuple(span_layer_ids(
                      v.layer_start, v.layer_end, enabled)))
        for v in raw)
    new_extracted = dataclasses.replace(
        ext, vias=tuple(ext.vias) + tuple(raw))
    out = replace_loaded(loaded, extracted=new_extracted)
    # A plain attribute, not a LoadedProject field: only a what-if solve's
    # copy ever carries it (build_problem reads it with getattr), and
    # replace_loaded carries it across.
    setattr(out, "whatif_via_cutouts", cutouts)
    return out, warnings, len(raw)


# --- placing ("punching") -----------------------------------------------------
#
# The viewer's punch tool turns a click into an AddedVia: it copies the size
# and span of the nearest via already on that net, surveys the copper under
# the pad on every layer the via would pass through, and refuses a via that
# would carry no current or touch another net. These are the rules, kept
# here so they can be tested without a viewer.

# Used when the board has no via to copy: a common through via.
DEFAULT_DIAMETER_MM = 0.6
DEFAULT_HOLE_MM = 0.3
# A drag that would punch more vias than this is refused (a slip of the
# pitch box, not a design).
MAX_GRID_VIAS = 400


@dataclass(frozen=True)
class ViaTemplate:
    """Size and span a punched via takes, and where they came from."""

    layer_start: int
    layer_end: int
    diameter_mm: float
    hole_diameter_mm: float
    source: str     # human-readable, e.g. "nearest via on GND"
    ipc4761_via_type: int = 0
    fill_material: str = ""

    def make(self, x_mm: float, y_mm: float, net: str) -> AddedVia:
        return AddedVia(x_mm=float(x_mm), y_mm=float(y_mm), net=net,
                        layer_start=self.layer_start,
                        layer_end=self.layer_end,
                        diameter_mm=self.diameter_mm,
                        hole_diameter_mm=self.hole_diameter_mm,
                        ipc4761_via_type=self.ipc4761_via_type,
                        fill_material=self.fill_material)


def _md_fill(r: dict) -> tuple[int, str]:
    """``(ipc4761_via_type, fill_material)`` of a ``metadata["vias"]``
    record (0 / "" when it carries none)."""
    try:
        t = int(r.get("ipc4761_via_type") or 0)
    except (TypeError, ValueError):
        t = 0
    return t, str(r.get("fill_material") or "")


def fill_text(ipc4761_via_type: int, fill_material: str = "") -> str:
    """``""`` for an unprotected via, else its IPC-4761 label (e.g.
    ``"V (fill) · Copper"``) - the Vias tab's wording."""
    if not int(ipc4761_via_type or 0):
        return ""
    from fypa.altium.loader import ipc4761_label
    return ipc4761_label(int(ipc4761_via_type), str(fill_material or ""))


def _usable_md_via(r: dict, enabled: list[int]) -> bool:
    try:
        d, h = float(r["diameter_mm"]), float(r["hole_diameter_mm"])
        a, b = int(r["layer_start"]), int(r["layer_end"])
    except (KeyError, TypeError, ValueError):
        return False
    return d > h > 0.0 and a in enabled and b in enabled and a != b


def via_template(md_vias: list[dict], net: str, x_mm: float, y_mm: float,
                 enabled: list[int]) -> ViaTemplate:
    """The via to punch at ``(x, y)`` on ``net``: a copy of the nearest via
    already on that net; else the board's most common via; else a
    0.3 / 0.6 mm through via. ``md_vias`` is ``metadata["vias"]``."""
    usable = [r for r in md_vias or [] if _usable_md_via(r, enabled)]
    same = [r for r in usable if r.get("net") == net]
    if same:
        r = min(same, key=lambda r: (float(r["x_mm"]) - x_mm) ** 2
                + (float(r["y_mm"]) - y_mm) ** 2)
        return ViaTemplate(int(r["layer_start"]), int(r["layer_end"]),
                           float(r["diameter_mm"]),
                           float(r["hole_diameter_mm"]),
                           f"copied from the nearest via on {net}",
                           *_md_fill(r))
    if usable:
        counts: dict[tuple, int] = {}
        for r in usable:
            k = (int(r["layer_start"]), int(r["layer_end"]),
                 round(float(r["diameter_mm"]), 4),
                 round(float(r["hole_diameter_mm"]), 4)) + _md_fill(r)
            counts[k] = counts.get(k, 0) + 1
        k = max(counts, key=lambda k: (counts[k], -k[3]))
        return ViaTemplate(k[0], k[1], k[2], k[3],
                           f"the board's most common via ({net} has none)",
                           k[4], k[5])
    return ViaTemplate(enabled[0], enabled[-1], DEFAULT_DIAMETER_MM,
                       DEFAULT_HOLE_MM, "a 0.3 / 0.6 mm through via "
                       "(the board has no vias to copy)")


# --- the board's via types (the punch tool's Via / Span choice) ---------------
#
# A board usually carries a handful of via types: a through via, perhaps a
# smaller one, and on an HDI board blind / buried / micro vias that run only
# between certain layers. The punch tool offers them as a choice: the TYPE is
# the drill + pad size, and for a type that doesn't run through every layer
# the SPAN (which layers it joins) is a sub-choice - the spans that type is
# actually used with on the board.


@dataclass(frozen=True)
class ViaSpan:
    """One span a via type is used with: its end layers (top first), how
    many of the board's vias use it, and its kind (through / blind / ...)."""

    layer_start: int
    layer_end: int
    count: int
    kind: str


@dataclass(frozen=True)
class ViaType:
    """One drill + pad size used by the board's vias, with its spans
    (most used first)."""

    diameter_mm: float
    hole_diameter_mm: float
    count: int
    spans: tuple[ViaSpan, ...]
    # IPC-4761 fill: a filled and an unfilled via of one size are two types.
    ipc4761_via_type: int = 0
    fill_material: str = ""

    @property
    def key(self) -> tuple:
        return (round(self.hole_diameter_mm, 4), round(self.diameter_mm, 4),
                int(self.ipc4761_via_type), self.fill_material)

    @property
    def needs_span_choice(self) -> bool:
        """True when some span doesn't run through every layer - the
        punch tool then shows the Span sub-choice."""
        return any(sp.kind != "through" for sp in self.spans)

    def template(self, span: ViaSpan) -> ViaTemplate:
        return ViaTemplate(span.layer_start, span.layer_end,
                           self.diameter_mm, self.hole_diameter_mm,
                           f"the chosen {span.kind} via type",
                           self.ipc4761_via_type, self.fill_material)


def board_via_types(md_vias: list[dict], enabled: list[int]
                    ) -> list[ViaType]:
    """The board's via types from ``metadata["vias"]``, most used first.
    Vias on layers that aren't enabled copper are left out."""
    from fypa.via_kinds import via_record_kind
    pos = {lid: i for i, lid in enumerate(enabled)}
    sizes: dict[tuple, dict] = {}
    for r in md_vias or []:
        if not _usable_md_via(r, enabled):
            continue
        a, b = int(r["layer_start"]), int(r["layer_end"])
        if pos[a] > pos[b]:
            a, b = b, a                       # top first
        d, h = float(r["diameter_mm"]), float(r["hole_diameter_mm"])
        fill = _md_fill(r)
        entry = sizes.setdefault((round(h, 4), round(d, 4)) + fill,
                                 {"d": d, "h": h, "n": 0, "spans": {},
                                  "fill": fill})
        entry["n"] += 1
        sp = entry["spans"].setdefault(
            (a, b), [0, via_record_kind(r, enabled)])
        sp[0] += 1
    out = []
    for e in sizes.values():
        spans = sorted(
            (ViaSpan(a, b, n, kind) for (a, b), (n, kind) in e["spans"].items()),
            key=lambda sp: (-sp.count, pos[sp.layer_start]))
        out.append(ViaType(e["d"], e["h"], e["n"], tuple(spans),
                           *e["fill"]))
    out.sort(key=lambda t: (-t.count, t.hole_diameter_mm))
    return out


def via_type_label(t: ViaType) -> str:
    """``"0.3 / 0.6 mm through (1234)"`` - drill / pad, the kinds it is
    used as, and how many the board has."""
    kinds: list[str] = []
    for sp in t.spans:
        if sp.kind not in kinds:
            kinds.append(sp.kind)
    fill = fill_text(t.ipc4761_via_type, t.fill_material)
    return (f"{t.hole_diameter_mm:.3g} / {t.diameter_mm:.3g} mm "
            f"{'/'.join(kinds)}{', ' + fill if fill else ''} ({t.count})")


def span_label(layer_start: int, layer_end: int,
               names: dict[int, str]) -> str:
    """``"Top → L2"`` from layer ids."""
    return (f"{names.get(layer_start, f'L{layer_start}')} → "
            f"{names.get(layer_end, f'L{layer_end}')}")


def via_props_text(via: AddedVia, enabled: list[int],
                   names: dict[int, str]) -> str:
    """``"0.3 / 0.6 mm, Top → Bottom (through)"`` for one via."""
    from fypa.via_kinds import classify_via, via_layer_span
    a, b = via.layer_start, via.layer_end
    pos = {lid: i for i, lid in enumerate(enabled)}
    if a in pos and b in pos and pos[a] > pos[b]:
        a, b = b, a
    kind = classify_via(a, b, via_layer_span(a, b, list(enabled)),
                        list(enabled), via.hole_diameter_mm)
    fill = fill_text(via.ipc4761_via_type, via.fill_material)
    return (f"{via.hole_diameter_mm:.3g} / {via.diameter_mm:.3g} mm, "
            f"{span_label(a, b, names)} ({kind}"
            f"{', ' + fill if fill else ''})")


# --- export (to add the vias in the layout tool) --------------------------------

EXPORT_COLUMNS: tuple[str, ...] = (
    "#", "Net", "X (mm)", "Y (mm)", "X (mil)", "Y (mil)", "Drill (mm)",
    "Pad (mm)", "Start layer", "End layer", "Kind", "Fill")


def export_rows(vias: list[AddedVia], enabled: list[int],
                names: dict[int, str]) -> list[list[str]]:
    """One row per via, :data:`EXPORT_COLUMNS` order. X / Y are board
    coordinates as Altium shows them (relative to the board origin), so the
    vias can be placed at exactly those spots in the layout."""
    from fypa.via_kinds import classify_via, via_layer_span
    pos = {lid: i for i, lid in enumerate(enabled)}
    out = []
    for i, v in enumerate(vias, 1):
        a, b = v.layer_start, v.layer_end
        if a in pos and b in pos and pos[a] > pos[b]:
            a, b = b, a
        kind = classify_via(a, b, via_layer_span(a, b, list(enabled)),
                            list(enabled), v.hole_diameter_mm)
        out.append([
            str(i), v.net, f"{v.x_mm:.4f}", f"{v.y_mm:.4f}",
            f"{v.x_mm / 0.0254:.2f}", f"{v.y_mm / 0.0254:.2f}",
            f"{v.hole_diameter_mm:.4g}", f"{v.diameter_mm:.4g}",
            names.get(a, f"L{a}"), names.get(b, f"L{b}"), kind,
            fill_text(v.ipc4761_via_type, v.fill_material) or "none",
        ])
    return out


def export_text(vias: list[AddedVia], enabled: list[int],
                names: dict[int, str], sep: str = ",") -> str:
    """The vias as CSV (``sep=","``) or tab-separated text (pastes into a
    spreadsheet as columns), with a header row."""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=sep, lineterminator="\n")
    w.writerow(EXPORT_COLUMNS)
    w.writerows(export_rows(vias, enabled, names))
    return buf.getvalue()


def vias_in_box(vias: list[AddedVia], x0: float, y0: float, x1: float,
                y1: float) -> list[int]:
    """Indices of the vias whose centre lies in the box (any corner order)."""
    lo_x, hi_x = sorted((x0, x1))
    lo_y, hi_y = sorted((y0, y1))
    return [i for i, v in enumerate(vias)
            if lo_x <= v.x_mm <= hi_x and lo_y <= v.y_mm <= hi_y]


def via_short_text(via: AddedVia, enabled: list[int]) -> str:
    """``"0.3/0.6 through"`` (``+fill`` when filled) - for a narrow column;
    :func:`via_props_text` has the layers and fill in full."""
    from fypa.via_kinds import classify_via, via_layer_span
    a, b = via.layer_start, via.layer_end
    kind = classify_via(a, b, via_layer_span(a, b, list(enabled)),
                        list(enabled), via.hole_diameter_mm)
    fill = " +fill" if via.ipc4761_via_type else ""
    return (f"{via.hole_diameter_mm:.3g}/{via.diameter_mm:.3g} {kind}"
            f"{fill}")


def span_layer_ids(layer_start: int, layer_end: int,
                   enabled: list[int]) -> list[int]:
    """Enabled layers a barrel from ``layer_start`` to ``layer_end``
    passes through, top first (by stack position, not layer id)."""
    pos = {lid: i for i, lid in enumerate(enabled)}
    a, b = pos.get(int(layer_start)), pos.get(int(layer_end))
    if a is None or b is None:
        return []
    lo, hi = min(a, b), max(a, b)
    return list(enabled[lo:hi + 1])


def punch_problem(via: AddedVia, survey: dict[int, tuple[bool, set]],
                  enabled: list[int], plane_layer_ids: set[int],
                  layer_names: dict[int, str],
                  other_vias=()) -> str:
    """Why ``via`` can't be punched, or ``""``.

    ``survey`` maps each layer in the via's span to ``(own, fixed)`` or
    ``(own, fixed, pour)``: whether ``via.net``'s copper covers the via
    centre there, the other nets whose FIXED copper (tracks, arcs, pads,
    fills, regions not part of a pour) the pad would touch, and the other
    nets whose pour it would land in. ``other_vias`` are
    ``(x, y, net, diameter_mm)`` of the vias and through-hole pads already
    there (the board's and earlier punched ones).

    Refused: a via reaching its net's copper on fewer than two layers (it
    would carry no current); a pad touching another net's fixed copper on
    a signal layer - a real short a repour wouldn't clear; a pad
    overlapping another net's via; a centre inside a via already on the
    same net. Another net's pour or internal plane is fine: the solve
    repours it with a clearance hole (:func:`cut_clearances`)."""
    why = via.problem()
    if why:
        return why
    span = span_layer_ids(via.layer_start, via.layer_end, enabled)
    if len(span) < 2:
        return "its layers aren't enabled copper layers"
    r_new = via.diameter_mm / 2.0
    for ox, oy, onet, odia in other_vias:
        d2 = (ox - via.x_mm) ** 2 + (oy - via.y_mm) ** 2
        if onet != via.net:
            if d2 < (r_new + float(odia) / 2.0) ** 2:
                return f"it would overlap a via on {onet}"
        elif d2 < (max(via.diameter_mm, float(odia)) / 2.0) ** 2:
            return "there is already a via there"
    for lid in span:
        if lid in plane_layer_ids:
            continue
        fixed = survey.get(lid, (False, set()))[1]
        if fixed:
            name = layer_names.get(lid, f"layer {lid}")
            return (f"its pad would touch {', '.join(sorted(fixed))} "
                    f"on {name}")
    reach = [lid for lid in span if survey.get(lid, (False, set()))[0]]
    if len(reach) < 2:
        where = (f"only {layer_names.get(reach[0], 'one layer')}"
                 if reach else "no layer")
        return (f"{via.net} has copper there on {where} - the via would "
                "carry no current")
    return ""


def grid_points(x0: float, y0: float, x1: float, y1: float,
                pitch_mm: float) -> list[tuple[float, float]]:
    """Points at ``pitch_mm`` filling the rectangle, centred in it, rows
    top first. A side shorter than the pitch gets one point at its middle.
    Raises ``ValueError`` past :data:`MAX_GRID_VIAS` points or for a
    non-positive pitch."""
    if not (pitch_mm > 0.0 and math.isfinite(pitch_mm)):
        raise ValueError("the grid pitch must be larger than zero")
    lo_x, hi_x = sorted((float(x0), float(x1)))
    lo_y, hi_y = sorted((float(y0), float(y1)))
    nx = int(math.floor((hi_x - lo_x) / pitch_mm + 1e-9)) + 1
    ny = int(math.floor((hi_y - lo_y) / pitch_mm + 1e-9)) + 1
    if nx * ny > MAX_GRID_VIAS:
        raise ValueError(
            f"that would be {nx * ny} vias (the limit is {MAX_GRID_VIAS}) - "
            "drag a smaller area or use a larger pitch")
    cx, cy = (lo_x + hi_x) / 2.0, (lo_y + hi_y) / 2.0
    xs = [cx + (i - (nx - 1) / 2.0) * pitch_mm for i in range(nx)]
    ys = [cy + (j - (ny - 1) / 2.0) * pitch_mm for j in range(ny)]
    return [(x, y) for y in reversed(ys) for x in xs]


# --- "beside" an existing via (the Fixes tab's Try as what-if) -----------------

# Gap between the pads of a via and one punched beside it.
BESIDE_GAP_MM = 0.2


def beside_points(x_mm: float, y_mm: float, pad_mm: float,
                  rings: int = 3) -> list[tuple[float, float]]:
    """Candidate centres for a via beside the one at ``(x, y)``: ring ``k``
    is ``k`` pitches out (pitch = pad + :data:`BESIDE_GAP_MM`) with ``8k``
    points, nearest ring first, each ring starting due east and going
    anticlockwise."""
    pitch = float(pad_mm) + BESIDE_GAP_MM
    out: list[tuple[float, float]] = []
    for k in range(1, rings + 1):
        n = 8 * k
        for j in range(n):
            a = 2.0 * math.pi * j / n
            out.append((x_mm + k * pitch * math.cos(a),
                        y_mm + k * pitch * math.sin(a)))
    return out


def added_via_report(metadata: dict, n_added: int) -> dict[str, int]:
    """``{"count", "unconnected"}`` for the last ``n_added`` entries of
    ``metadata["vias"]``: an added via with no coupling segments reached
    copper of its net on fewer than two layers and carries no current."""
    rows = list((metadata or {}).get("vias") or [])
    tail = rows[len(rows) - n_added:] if 0 < n_added <= len(rows) else []
    unconnected = sum(1 for r in tail if not r.get("segments"))
    return {"count": int(n_added), "unconnected": int(unconnected)}
