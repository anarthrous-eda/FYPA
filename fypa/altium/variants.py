"""Altium assembly variants: which parts a build leaves off, and which it swaps.

A ``.PrjPcb`` lists its variants as ``[ProjectVariantN]`` sections. Each
names a build (``Description=Bringup``) and the parts that differ from the
base design:

* ``Kind=1`` - **Not Fitted**. The footprint's pads are still on the board,
  but nothing is soldered to them, so the part conducts nothing. This is the
  one that matters most to FYPA: an unfitted 0 Ω link or option strap must
  not be bridged, or rails get merged together (or into ground).
* ``Kind=2`` - **Alternate part**, with ``ParamVariationN`` rows overriding
  the part's parameters (``Comment``, ``Value``, …). A 0 Ω swapped for a
  10 kΩ stops being a bridge; a feedback resistor shows its real value.

``[Design] CurrentVariant=`` records the variant the designer last had
selected in Altium. FYPA follows it unless the project (``.fypa``) picks
another one - see :func:`resolve_variant`.

Parsing is altium_monkey's (``AltiumPrjPcb.variants``); this module turns it
into what the loader needs and applies it to an extracted project.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from pathlib import Path

log = logging.getLogger(__name__)

# The ``.fypa`` value meaning "the base design, every part fitted" - Altium's
# own "[No Variations]". ``None`` (the default) means "whatever Altium has
# selected".
NO_VARIATIONS = ""
NO_VARIATIONS_LABEL = "[No Variations]"


@dataclass(frozen=True)
class Variant:
    name: str
    not_fitted: frozenset[str] = frozenset()                 # designators, upper-case
    # designator (upper) → {parameter name: value}
    param_overrides: dict[str, dict[str, str]] = field(default_factory=dict)
    # designator (upper) as written in the variant → the part's Altium
    # UniqueId path (upper), e.g. ``\FLYIJELO\LKLKDIDM\CVVWDYKP``. Altium
    # binds an entry by this, not by the designator: after a re-annotation
    # the entry keeps its old designator but still means the same part.
    unique_ids: dict[str, str] = field(default_factory=dict)
    # Entries rebound by :func:`bind_to_board`: (designator in the variant,
    # designator on the board now).
    renamed: tuple[tuple[str, str], ...] = ()
    # Entries whose part is no longer in the design (Altium ignores them).
    orphans: tuple[str, ...] = ()

    @property
    def alternates(self) -> frozenset[str]:
        return frozenset(self.param_overrides)


@dataclass(frozen=True)
class ProjectVariants:
    current: str | None                    # Altium's CurrentVariant, None = none
    variants: dict[str, Variant] = field(default_factory=dict)

    @property
    def names(self) -> list[str]:
        return list(self.variants)

    def get(self, name: str | None) -> Variant | None:
        if not name:
            return None
        if name in self.variants:
            return self.variants[name]
        low = name.lower()
        return next((v for n, v in self.variants.items() if n.lower() == low), None)


def read_variants(prjpcb_path: str | Path | None) -> ProjectVariants:
    """The project's variants and Altium's current selection. Empty (no
    variants, no current) for anything that isn't a readable ``.PrjPcb``.

    A KiCad board or project gives its KiCad 10 design variants instead (see
    :mod:`fypa.kicad.variants`); KiCad stores no current selection."""
    if prjpcb_path is None:
        return ProjectVariants(current=None)
    from fypa.kicad import is_kicad_path
    if is_kicad_path(prjpcb_path):
        from fypa.kicad.variants import read_kicad_variants
        return read_kicad_variants(prjpcb_path)
    path = Path(prjpcb_path)
    if path.suffix.lower() != ".prjpcb" or not path.is_file():
        return ProjectVariants(current=None)
    try:
        from altium_monkey.altium_prjpcb import AltiumPrjPcb

        prj = AltiumPrjPcb(path)
        raw = prj.variants or {}
        current = (prj.get_current_variant() or "").strip() or None
    except Exception:  # noqa: BLE001 - a bad variant section must not stop a load
        log.warning("Could not read assembly variants from %s", path, exc_info=True)
        return ProjectVariants(current=None)
    out: dict[str, Variant] = {}
    for name, data in raw.items():
        name = (name or "").strip()
        if not name:
            continue
        dnp = frozenset((d or "").strip().upper() for d in (data.get("DNP") or ())
                        if (d or "").strip())
        overrides = {
            (des or "").strip().upper(): {str(k): str(v) for k, v in (params or {}).items()}
            for des, params in (data.get("parameter_overrides") or {}).items()
            if (des or "").strip()
        }
        uids = {}
        for row in data.get("variations") or ():
            des = (row.get("Designator") or "").strip().upper()
            uid = (row.get("UniqueId") or "").strip().upper()
            if des and uid:
                uids[des] = uid
        out[name] = Variant(name=name, not_fitted=dnp, param_overrides=overrides,
                            unique_ids=uids)
    return ProjectVariants(current=current, variants=out)


def bind_to_board(variant: Variant | None, pcb_components) -> Variant | None:
    """``variant`` with every entry pointed at the part Altium means.

    Altium identifies a variant's entries by the part's UniqueId path, and
    keeps the designator only as a label. Re-annotate the board after
    setting up a variant and the labels go stale: the entry written as
    ``R101_VCC_HDIO`` now means the part called ``R103_VCC_HDIO``, and
    ``R101_VCC_HDIO`` is a different, fitted part. So match by UniqueId
    (the PCB component's ``source_unique_id``) first; fall back to the
    designator only for an entry with no UniqueId, or a board without them.
    An entry whose UniqueId matches nothing is dropped, as Altium drops it.
    """
    if variant is None:
        return None
    by_uid: dict[str, str] = {}
    on_board: set[str] = set()
    for c in pcb_components or ():
        des = (getattr(c, "designator", "") or "").strip()
        if not des:
            continue
        on_board.add(des.upper())
        uid = (getattr(c, "source_unique_id", "") or "").strip().upper()
        if uid:
            by_uid.setdefault(uid, des.upper())
    if not by_uid:
        return variant          # nothing to bind against: designators as written

    renamed: list[tuple[str, str]] = []
    orphans: list[str] = []

    def bind(des: str) -> str | None:
        uid = variant.unique_ids.get(des)
        if not uid:
            return des if des in on_board else None
        now = by_uid.get(uid)
        if now is None:
            orphans.append(des)
        elif now != des:
            renamed.append((des, now))
        return now

    not_fitted = frozenset(d for d in (bind(x) for x in sorted(variant.not_fitted)) if d)
    overrides: dict[str, dict[str, str]] = {}
    for des, params in sorted(variant.param_overrides.items()):
        now = bind(des)
        if now:
            overrides[now] = params
    return replace(variant, not_fitted=not_fitted, param_overrides=overrides,
                   renamed=tuple(dict.fromkeys(renamed)),
                   orphans=tuple(dict.fromkeys(orphans)))


@dataclass(frozen=True)
class VariantChoice:
    """What a load applied: the project's request and what it resolved to."""

    requested: str | None          # the .fypa value: None = follow Altium
    variant: Variant | None        # None = base design (no variations)
    altium_current: str | None     # CurrentVariant in the .PrjPcb
    available: tuple[str, ...] = ()
    note: str = ""                 # set when the request couldn't be honoured

    @property
    def name(self) -> str:
        return self.variant.name if self.variant else NO_VARIATIONS

    @property
    def label(self) -> str:
        return self.variant.name if self.variant else NO_VARIATIONS_LABEL

    @property
    def not_fitted(self) -> frozenset[str]:
        return self.variant.not_fitted if self.variant else frozenset()


def resolve_variant(variants: ProjectVariants, requested: str | None) -> VariantChoice:
    """Pick the variant a load uses.

    ``requested`` is the project's setting: ``None`` follows Altium's
    ``CurrentVariant``; :data:`NO_VARIATIONS` (``""``) is the base design; a
    name picks that variant. A name the ``.PrjPcb`` no longer defines falls
    back to Altium's current one, with a note saying so."""
    avail = tuple(variants.names)
    note = ""
    if requested is None:
        chosen = variants.get(variants.current)
        if variants.current and chosen is None:
            note = (f"Altium's current variant {variants.current!r} is not defined "
                    "in the project; using the base design")
    elif requested == NO_VARIATIONS:
        chosen = None
    else:
        chosen = variants.get(requested)
        if chosen is None:
            chosen = variants.get(variants.current)
            note = (f"Variant {requested!r} is not in the project any more; using "
                    + (f"Altium's current variant {chosen.name!r}" if chosen
                       else "the base design"))
    return VariantChoice(requested=requested, variant=chosen,
                         altium_current=variants.current, available=avail, note=note)


_UNRECORDED = object()


def variant_reload_needed(loaded, requested: str | None) -> bool:
    """True when ``loaded`` was built for a different variant request than
    ``requested`` (the project's ``variant``), so only a reload applies it -
    Not Fitted parts act while annotations are parsed, before the merge.

    A design loaded before variants were supported (an old design-info
    cache) has no record; it is reloaded only if it came from an Altium
    ``.PrjPcb`` that defines variants. Gerber / KiCad designs never are."""
    if loaded is None:
        return False
    have = getattr(loaded, "variant_applied", _UNRECORDED)
    if have is _UNRECORDED:
        ex = getattr(loaded, "extracted", None)
        prj = getattr(ex, "prjpcb_path", None)
        return bool(prj) and bool(read_variants(prj).variants)
    return have != requested


def variant_choice_of(loaded) -> VariantChoice | None:
    """The variant ``loaded`` was built with, or ``None`` if not recorded."""
    return getattr(loaded, "variant_choice", None)


def variant_metadata(loaded) -> dict | None:
    """JSON-safe record of the variant ``loaded`` was built with, for the
    solve metadata. ``None`` when the load didn't record one (Gerber, or a
    design loaded before variants were supported)."""
    choice = variant_choice_of(loaded)
    if choice is None:
        return None
    on_board = {c.designator.upper()
                for c in getattr(getattr(loaded, "extracted", None),
                                 "pcb_components", ()) or ()}
    from fypa.kicad import is_kicad_path
    prj = getattr(getattr(loaded, "extracted", None), "prjpcb_path", None)
    return {
        # The design's tool, for the picker's wording ("KiCad default").
        "tool": "KiCad" if is_kicad_path(prj) else "Altium",
        "requested": choice.requested,          # None = follow Altium
        "name": choice.name,                    # "" = base design
        "label": choice.label,
        "altium_current": choice.altium_current,
        "available": list(choice.available),
        "not_fitted": sorted(choice.not_fitted & on_board) if on_board
        else sorted(choice.not_fitted),
        "alternates": sorted(choice.variant.alternates) if choice.variant else [],
        # Entries whose designator is stale (pre-re-annotation): [old, now].
        "renamed": [list(p) for p in choice.variant.renamed] if choice.variant else [],
        "note": choice.note,
    }


def describe_request(requested: str | None, altium_current: str | None,
                     tool: str = "Altium") -> str:
    """How the Properties picker labels a request. *tool* names the design's
    tool ("KiCad" records no current variant, so its default is the base)."""
    if requested is None:
        return (f"{tool} default: {altium_current}" if altium_current
                else f"{tool} default: {NO_VARIATIONS_LABEL}")
    if requested == NO_VARIATIONS:
        return NO_VARIATIONS_LABEL
    return requested


def apply_param_overrides(extracted, variant: Variant | None):
    """``extracted`` with the variant's alternate-part parameters applied to
    the matching schematic and PCB components (by physical designator)."""
    if variant is None or not variant.param_overrides:
        return extracted
    over = variant.param_overrides

    def patched(comps):
        out, changed = [], False
        for c in comps:
            hit = over.get((c.designator or "").upper())
            if hit:
                out.append(replace(c, parameters={**(c.parameters or {}), **hit}))
                changed = True
            else:
                out.append(c)
        return tuple(out), changed

    sch, sch_changed = patched(extracted.sch_components)
    pcb, pcb_changed = patched(extracted.pcb_components)
    if not (sch_changed or pcb_changed):
        return extracted
    return replace(extracted, sch_components=sch, pcb_components=pcb)
