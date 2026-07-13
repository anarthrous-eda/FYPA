"""FYPA project files (``.fypa``).

A project file is a small JSON document that ties together everything FYPA
needs to reopen a board exactly as the user left it:

* the design source it came from — either an Altium project (``.PrjPcb`` +
  ``.PcbDoc``) or a set of Gerber + Excellon files (``gerber_files`` +
  ``drill_files`` + an optional ``outline_file``),
* the two cache pickles produced by the solve pipeline — ``design-info.pkl``
  (extraction + geometry, see :func:`fypa.cli._design_info_cache_path`) and
  ``solve.pkl`` (the FEM solution, see :func:`fypa.cli._solve_cache_path`),
* any **editor-mode directives** the user has placed by hand (sources / sinks
  dropped on components or free on copper),
* a reserved ``net_renames`` map for a future gerber-export mode, and
* (Gerber projects only) the per-file ``layer_assignments`` and per-layer
  ``gerber_stackup`` the user confirmed in the import dialogs, so reopening
  the project skips those dialogs.

The pickles are *referenced*, not embedded — they can be large (tens to
hundreds of MB). Paths are stored relative to the ``.fypa`` file when both sit
on the same drive, so a project folder can be moved or shared as a unit.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

# Bumped whenever the on-disk schema changes incompatibly. ``load`` tolerates
# older minor additions (missing keys fall back to defaults); a hard mismatch
# raises so the user gets a clear error rather than silently-wrong state.
SCHEMA_VERSION = 2

# Roles an editor directive may carry. Mirrors ``VALID_ROLES`` in
# :mod:`fypa.altium.annotations`; kept as a local copy so this module has no
# import dependency on the (heavy) annotation stack.
EDITOR_ROLES = ("SOURCE", "SINK", "REGULATOR", "SERIES")

PROJECT_FILE_SUFFIX = ".fypa"


@dataclass
class EditorDirective:
    """One source / sink / regulator / series element placed in editor mode.

    A directive is either **component-bound** (``kind == "component"``,
    attached to a real PCB component by ``designator``) or a **free marker**
    (``kind == "free"``, dropped at ``anchor_xy`` on a copper layer).

    ``single_net`` chooses the current model: ``True`` is a point-to-point
    single-net directive (the ``n`` terminal is an ideal 0 V return);
    ``False`` is a full two-net current-path loop using both ``p_net`` and
    ``n_net``. ``voltage`` is meaningful for SOURCE / REGULATOR, ``current``
    for SINK, ``resistance`` for SERIES; the unused ones are ``None``.

    A SERIES directive always bridges two real nets (a ferrite bead, sense
    resistor, 0 Ω jumper, …), so it is inherently two-net — ``single_net``
    is ``False`` and both ``p_net`` and ``n_net`` are required.
    """

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    kind: str = "component"               # "component" | "free"
    role: str = "SINK"                    # one of EDITOR_ROLES
    designator: str | None = None         # component-bound
    anchor_xy: tuple[float, float] | None = None   # free marker, world mm
    layer: str | None = None              # physical layer name of the marker
    layer_id: int | None = None           # Altium copper layer id of the marker
    single_net: bool = True
    p_net: str | None = None
    n_net: str | None = None
    # Optional pin restriction (the editor-mode equivalent of the schematic
    # ``PDN_PINS`` / ``PDN_P_PINS`` / ``PDN_N_PINS`` parameters). Each is a
    # list of pad designators (e.g. ``["1"]``) the terminal couples to;
    # ``None`` means *every* pad of the component that sits on the terminal's
    # net — the default for a freshly placed marker. Only meaningful for a
    # component-bound directive; ignored for a free marker. ``p_pins`` pairs
    # with ``p_net`` (PDN_PINS in single-net mode), ``n_pins`` with ``n_net``.
    p_pins: list[str] | None = None
    n_pins: list[str] | None = None
    voltage: float | None = None
    current: float | None = None
    resistance: float | None = None       # SERIES only, ohms
    # Optional minimum acceptable rail voltage at the SINK's P pins
    # (the editor-mode equivalent of the schematic ``PDN_MIN_V`` parameter).
    # The viewer's Nodes table compares the measured per-pin voltage against
    # this limit and flags pass / fail. Meaningful for SINK only; ``None`` to
    # disable the check or for non-SINK roles.
    min_voltage: float | None = None
    # When set, this editor directive replaces (overrides) the Altium
    # schematic directive carrying this designator — the re-solve drops the
    # schematic one so they don't both stamp a lumped element. ``None`` for
    # an ordinary editor directive that simply adds a new source / sink.
    overrides_designator: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.anchor_xy is not None:
            d["anchor_xy"] = [float(self.anchor_xy[0]), float(self.anchor_xy[1])]
        d["p_pins"] = list(self.p_pins) if self.p_pins is not None else None
        d["n_pins"] = list(self.n_pins) if self.n_pins is not None else None
        return d

    @staticmethod
    def _coerce_pins(raw: Any) -> list[str] | None:
        """Normalise a stored pin list to ``list[str]`` (or ``None``).

        Drops blanks / whitespace; an empty result collapses to ``None`` so
        "no restriction" and "explicitly empty" are the same thing."""
        if not raw:
            return None
        pins = [str(p).strip() for p in raw if str(p).strip()]
        return pins or None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EditorDirective:
        anchor = d.get("anchor_xy")
        return cls(
            id=str(d.get("id") or uuid.uuid4().hex[:12]),
            kind=str(d.get("kind", "component")),
            role=str(d.get("role", "SINK")).upper(),
            designator=d.get("designator"),
            anchor_xy=(float(anchor[0]), float(anchor[1])) if anchor else None,
            layer=d.get("layer"),
            layer_id=(None if d.get("layer_id") is None
                      else int(d["layer_id"])),
            single_net=bool(d.get("single_net", True)),
            p_net=d.get("p_net"),
            n_net=d.get("n_net"),
            p_pins=cls._coerce_pins(d.get("p_pins")),
            n_pins=cls._coerce_pins(d.get("n_pins")),
            voltage=(None if d.get("voltage") is None else float(d["voltage"])),
            current=(None if d.get("current") is None else float(d["current"])),
            resistance=(None if d.get("resistance") is None
                        else float(d["resistance"])),
            min_voltage=(None if d.get("min_voltage") is None
                         else float(d["min_voltage"])),
            overrides_designator=d.get("overrides_designator"),
        )


@dataclass
class CopperName:
    """A user-given name for a single piece of unnamed copper.

    Altium copper without a net assignment surfaces as the sentinel string
    ``"(none)"`` everywhere downstream, which collapses every disjoint
    unnamed copper piece on the board onto one rail — the solver then
    can't tell them apart, and any source / sink placed on one piece is
    silently dropped (no rail of that name in the solution).

    A ``CopperName`` lets the user point at a specific polygon (anchor
    point + copper layer) and give it a real net name. At solve time
    :func:`fypa.editor_directives.apply_copper_names` walks the extracted
    project, finds the no-net region whose geometry contains
    ``anchor_xy`` on ``layer_id``, adds a synthetic net carrying
    ``name``, and re-points that region at it — so only THIS piece gets
    the new name (the other disjoint unnamed pieces stay ``"(none)"``).
    """

    anchor_xy: tuple[float, float]
    layer_id: int
    name: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "anchor_xy": [float(self.anchor_xy[0]), float(self.anchor_xy[1])],
            "layer_id": int(self.layer_id),
            "name": self.name,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CopperName:
        a = d["anchor_xy"]
        return cls(
            id=str(d.get("id") or uuid.uuid4().hex[:12]),
            anchor_xy=(float(a[0]), float(a[1])),
            layer_id=int(d["layer_id"]),
            name=str(d["name"]),
        )


@dataclass
class CapOverride:
    """Per-capacitor user override for the Capacitors (loop inductance) tab.

    Keyed by the physical (PCB) designator — unique per board, stable across
    re-extractions. ``include`` overrides the auto-detection verdict
    (``True`` forces a structurally-valid cap into the analysis, ``False``
    drops a detected one); ``None`` leaves detection alone. ``target_label``
    repoints the loop-measurement endpoint at another directive's label
    ("U5" / "U5#1"); ``None`` keeps the default (largest-current SINK on the
    cap's rail).

    ``esl_h`` / ``esr_ohm`` override the part's own parasitics for the
    impedance model, which otherwise reads them from the SMD package library
    by case size. They are the only way to model a capacitor whose footprint
    carries no case-size code — a tantalum brick, an electrolytic — since no
    table can predict those.

    An override with every field ``None`` is meaningless and is dropped on
    upsert.
    """

    designator: str
    include: bool | None = None
    target_label: str | None = None
    esl_h: float | None = None
    esr_ohm: float | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def is_empty(self) -> bool:
        return (self.include is None and self.target_label is None
                and self.esl_h is None and self.esr_ohm is None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "designator": self.designator,
            "include": self.include,
            "target_label": self.target_label,
            "esl_h": self.esl_h,
            "esr_ohm": self.esr_ohm,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CapOverride:
        def _float(key: str) -> float | None:
            v = d.get(key)
            if v is None:
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        return cls(
            id=str(d.get("id") or uuid.uuid4().hex[:12]),
            designator=str(d["designator"]),
            include=(None if d.get("include") is None else bool(d["include"])),
            target_label=(None if d.get("target_label") is None
                          else str(d["target_label"])),
            esl_h=_float("esl_h"),
            esr_ohm=_float("esr_ohm"),
        )


@dataclass
class ProjectFile:
    """In-memory model of a ``.fypa`` document.

    Pickle paths are kept absolute in memory; :meth:`save` rewrites them
    relative to the ``.fypa`` location where possible.
    """

    prjpcb_path: str | None = None
    pcbdoc_path: str | None = None
    design_info_pickle: str | None = None
    solve_pickle: str | None = None
    editor_directives: list[EditorDirective] = field(default_factory=list)
    copper_names: list[CopperName] = field(default_factory=list)
    cap_overrides: list[CapOverride] = field(default_factory=list)
    net_renames: dict[str, str] = field(default_factory=dict)   # reserved
    viewer_settings: dict[str, Any] = field(default_factory=dict)

    # ---- Gerber-import fields (schema v2) --------------------------------
    # Populated when the project was imported from Gerber files instead of
    # an Altium .PrjPcb. ``source_kind`` is the discriminator the viewer +
    # cli consult when deciding which extract path to use on re-open.
    source_kind: str = "altium"                                  # "altium" | "gerber"
    gerber_files: list[str] = field(default_factory=list)        # rel to .fypa
    drill_files: list[str] = field(default_factory=list)
    outline_file: str | None = None
    # basename -> Altium-convention layer id (1 Top, 32 Bottom, 2..31 inner,
    # 33/34 silk; see fypa.gerber.extract for sentinels).
    layer_assignments: dict[str, int] = field(default_factory=dict)
    # Serialised GerberStackupLayer list — each dict carries layer_id /
    # name / copper_thickness_mm / dielectric_thickness_mm. We round-trip
    # plain dicts so that loading a v2 .fypa doesn't force a hard import
    # of fypa.gerber.extract (PySide6 may not be needed for a CLI load).
    gerber_stackup: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def save(self, path: str | Path) -> None:
        """Write this project to ``path`` as JSON. Pickle / source paths are
        stored relative to ``path`` when they share its drive, so a project
        folder stays portable."""
        path = Path(path)
        base = path.parent

        doc: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "prjpcb_path": _rel(self.prjpcb_path, base),
            "pcbdoc_path": _rel(self.pcbdoc_path, base),
            "design_info_pickle": _rel(self.design_info_pickle, base),
            "solve_pickle": _rel(self.solve_pickle, base),
            "editor_directives": [d.to_dict() for d in self.editor_directives],
            "copper_names": [c.to_dict() for c in self.copper_names],
            "cap_overrides": [c.to_dict() for c in self.cap_overrides],
            "net_renames": dict(self.net_renames),
            "viewer_settings": dict(self.viewer_settings),
            "source_kind": self.source_kind,
            "gerber_files": [_rel(p, base) for p in self.gerber_files],
            "drill_files": [_rel(p, base) for p in self.drill_files],
            "outline_file": _rel(self.outline_file, base),
            "layer_assignments": dict(self.layer_assignments),
            "gerber_stackup": [dict(d) for d in self.gerber_stackup],
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        tmp.replace(path)   # atomic-ish: don't leave a half-written .fypa

    @classmethod
    def load(cls, path: str | Path) -> ProjectFile:
        """Read a ``.fypa`` document; resolve every stored path back to an
        absolute path relative to the file's own location."""
        path = Path(path)
        base = path.parent
        doc = json.loads(path.read_text(encoding="utf-8"))

        ver = int(doc.get("schema_version", 0))
        if ver > SCHEMA_VERSION:
            raise ValueError(
                f"{path.name} was written by a newer version of FYPA "
                f"(schema {ver}; this build understands {SCHEMA_VERSION}). "
                "Please update FYPA."
            )

        return cls(
            prjpcb_path=_abs(doc.get("prjpcb_path"), base),
            pcbdoc_path=_abs(doc.get("pcbdoc_path"), base),
            design_info_pickle=_abs(doc.get("design_info_pickle"), base),
            solve_pickle=_abs(doc.get("solve_pickle"), base),
            editor_directives=[
                EditorDirective.from_dict(d)
                for d in doc.get("editor_directives", [])
            ],
            copper_names=[
                CopperName.from_dict(c)
                for c in doc.get("copper_names", [])
            ],
            # Additive (older files simply lack the key), so the schema
            # version is unchanged.
            cap_overrides=[
                CapOverride.from_dict(c)
                for c in doc.get("cap_overrides", [])
            ],
            net_renames=dict(doc.get("net_renames", {})),
            viewer_settings=dict(doc.get("viewer_settings", {})),
            source_kind=str(doc.get("source_kind", "altium")),
            gerber_files=[_abs(p, base) for p in doc.get("gerber_files", [])
                          if p],
            drill_files=[_abs(p, base) for p in doc.get("drill_files", [])
                         if p],
            outline_file=_abs(doc.get("outline_file"), base),
            layer_assignments=dict(doc.get("layer_assignments", {})),
            gerber_stackup=[dict(d) for d in doc.get("gerber_stackup", [])],
        )

    # ------------------------------------------------------------------
    # Editor-directive helpers
    # ------------------------------------------------------------------

    def directive_by_id(self, directive_id: str) -> EditorDirective | None:
        for d in self.editor_directives:
            if d.id == directive_id:
                return d
        return None

    def upsert_directive(self, directive: EditorDirective) -> None:
        """Replace the directive with the same ``id`` if present, else append."""
        for i, d in enumerate(self.editor_directives):
            if d.id == directive.id:
                self.editor_directives[i] = directive
                return
        self.editor_directives.append(directive)

    def remove_directive(self, directive_id: str) -> bool:
        before = len(self.editor_directives)
        self.editor_directives = [
            d for d in self.editor_directives if d.id != directive_id
        ]
        return len(self.editor_directives) != before

    # ------------------------------------------------------------------
    # Copper-name helpers
    # ------------------------------------------------------------------

    def copper_name_for(self, anchor_xy: tuple[float, float],
                        layer_id: int) -> CopperName | None:
        """The :class:`CopperName` whose anchor and layer match (within a
        small epsilon to absorb float-stringification round-trips through
        the project-file JSON). Returns ``None`` when no rename applies."""
        ax, ay = float(anchor_xy[0]), float(anchor_xy[1])
        for c in self.copper_names:
            if int(c.layer_id) != int(layer_id):
                continue
            if (abs(c.anchor_xy[0] - ax) < 1e-6
                    and abs(c.anchor_xy[1] - ay) < 1e-6):
                return c
        return None

    def upsert_copper_name(self, copper_name: CopperName) -> None:
        """Replace the entry with the same ``id`` if present, else append."""
        for i, c in enumerate(self.copper_names):
            if c.id == copper_name.id:
                self.copper_names[i] = copper_name
                return
        self.copper_names.append(copper_name)

    def remove_copper_name(self, copper_name_id: str) -> bool:
        before = len(self.copper_names)
        self.copper_names = [
            c for c in self.copper_names if c.id != copper_name_id
        ]
        return len(self.copper_names) != before

    # ------------------------------------------------------------------
    # Capacitor-override helpers
    # ------------------------------------------------------------------

    def cap_override_for(self, designator: str) -> CapOverride | None:
        for c in self.cap_overrides:
            if c.designator == designator:
                return c
        return None

    def upsert_cap_override(
        self,
        designator: str,
        *,
        include: bool | None = ...,
        target_label: str | None = ...,
        esl_h: float | None = ...,
        esr_ohm: float | None = ...,
    ) -> None:
        """Merge the given fields into the designator's override (one
        override per designator). A field passed as the ``...`` sentinel is
        left untouched, so the include toggle, the target picker and the two
        parasitic editors each update their own part independently. An
        override whose fields are all back at ``None`` is removed entirely —
        the .fypa doesn't accumulate no-op entries."""
        existing = self.cap_override_for(designator)
        merged = existing or CapOverride(designator=designator)
        if include is not ...:
            merged.include = include
        if target_label is not ...:
            merged.target_label = target_label
        if esl_h is not ...:
            merged.esl_h = esl_h
        if esr_ohm is not ...:
            merged.esr_ohm = esr_ohm
        if merged.is_empty():
            if existing is not None:
                self.cap_overrides = [
                    c for c in self.cap_overrides
                    if c.designator != designator
                ]
            return
        if existing is None:
            self.cap_overrides.append(merged)

    def cap_override_maps(self) -> tuple[dict[str, bool], dict[str, str]]:
        """(include_overrides, target_overrides) in the shape
        :func:`fypa.caploop.identify.identify_capacitors` consumes."""
        includes: dict[str, bool] = {}
        targets: dict[str, str] = {}
        for c in self.cap_overrides:
            if c.include is not None:
                includes[c.designator] = c.include
            if c.target_label is not None:
                targets[c.designator] = c.target_label
        return includes, targets

    def cap_parasitic_overrides(
        self,
    ) -> tuple[dict[str, float], dict[str, float]]:
        """(esl_overrides, esr_overrides) keyed by designator, for the
        impedance model's per-part parasitics."""
        esls: dict[str, float] = {}
        esrs: dict[str, float] = {}
        for c in self.cap_overrides:
            if c.esl_h is not None:
                esls[c.designator] = c.esl_h
            if c.esr_ohm is not None:
                esrs[c.designator] = c.esr_ohm
        return esls, esrs


# --- path helpers -------------------------------------------------------------


def _rel(p: str | None, base: Path) -> str | None:
    """Best-effort path relative to ``base``; falls back to the absolute path
    when the two live on different drives (Windows) or ``relative_to`` fails."""
    if not p:
        return None
    ap = Path(p).resolve()
    try:
        return str(_relpath(ap, base.resolve()))
    except (ValueError, OSError):
        return str(ap)


def _abs(p: str | None, base: Path) -> str | None:
    """Resolve a stored (possibly relative) path against ``base``."""
    if not p:
        return None
    pp = Path(p)
    if pp.is_absolute():
        return str(pp)
    return str((base / pp).resolve())


def _relpath(target: Path, base: Path) -> Path:
    """``os.path.relpath``-style relative path; raises ``ValueError`` across
    drives so :func:`_rel` can fall back to an absolute path."""
    import os
    return Path(os.path.relpath(target, base))
