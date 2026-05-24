"""FYPA project files (``.fypa``).

A project file is a small JSON document that ties together everything FYPA
needs to reopen a board exactly as the user left it:

* the Altium sources it came from (``.PrjPcb`` / ``.PcbDoc``),
* the two cache pickles produced by the solve pipeline — ``design-info.pkl``
  (extraction + geometry, see :func:`fypa.cli._design_info_cache_path`) and
  ``solve.pkl`` (the FEM solution, see :func:`fypa.cli._solve_cache_path`),
* any **editor-mode directives** the user has placed by hand (sources / sinks
  dropped on components or free on copper), and
* a reserved ``net_renames`` map for a future gerber-export mode.

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
SCHEMA_VERSION = 1

# Roles an editor directive may carry. Mirrors ``VALID_ROLES`` in
# :mod:`fypa.altium_annotations`; kept as a local copy so this module has no
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
        return d

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
            voltage=(None if d.get("voltage") is None else float(d["voltage"])),
            current=(None if d.get("current") is None else float(d["current"])),
            resistance=(None if d.get("resistance") is None
                        else float(d["resistance"])),
            min_voltage=(None if d.get("min_voltage") is None
                         else float(d["min_voltage"])),
            overrides_designator=d.get("overrides_designator"),
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
    net_renames: dict[str, str] = field(default_factory=dict)   # reserved
    viewer_settings: dict[str, Any] = field(default_factory=dict)

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
            "net_renames": dict(self.net_renames),
            "viewer_settings": dict(self.viewer_settings),
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
            net_renames=dict(doc.get("net_renames", {})),
            viewer_settings=dict(doc.get("viewer_settings", {})),
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
