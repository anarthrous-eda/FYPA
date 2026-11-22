"""The setup sidecar: sources, sinks and regulators set up in FYPA, saved
beside the design so the next import of that design gets them back.

Setups made in FYPA (on the board, with Auto setup or Manual setup) live in
the viewer's in-memory project until the user saves a ``.fypa``. A fresh
import of the same design used to start empty. The sidecar closes that gap:

* **Save** - :func:`save_setup` writes ``<Board>.fypa-setup.json`` next to the
  ``.PcbDoc`` whenever the setup changes. When that folder can't be written
  (read-only checkout, network share) it falls back to FYPA's per-user cache
  folder for the project. Each component-bound entry carries a fingerprint of
  the part it was placed on (Altium unique ids, footprint, comment, position,
  pins), recorded from the design it was set up on.
* **Restore** - :func:`load_setup` reads it back and :func:`match_exact`
  checks every entry against the design being imported. An entry whose part,
  pins and nets are all still there is applied; one that no longer fits is
  *held* (not applied, kept in the sidecar and tried again next time); one on
  a part that has since gained ``PDN_*`` parameters in the schematic is
  *superseded* - Altium wins for anything it defines.

Nothing here guesses: a part that was renamed or swapped is held, not
re-pointed. Values (currents, voltages, limits) are never touched - only
whether each entry's anchor still holds.

No Qt and no Altium parser imports, so the CLI and the solve worker can use
it freely.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fypa.project_file import CopperName, EditorDirective

log = logging.getLogger(__name__)

SIDECAR_SUFFIX = ".fypa-setup.json"
SCHEMA_VERSION = 1

# Fallback file name inside the per-user project cache folder.
_FALLBACK_NAME = "setup.json"

# Roles the schematic can define for a part. A FYPA entry with one of these
# roles on a part that now carries schematic parameters is superseded.
_SCHEMATIC_ROLES = ("SOURCE", "SINK", "REGULATOR")
_SCHEMATIC_SPEC_CLASSES = {"SourceSpec", "SinkSpec", "RegulatorSpec"}

# Every net an entry can name, and every designator list it can reference.
_NET_FIELDS = ("p_net", "n_net", "in_p_net", "in_n_net")
_DES_FIELDS = ("p_des", "n_des", "sense_des", "sense_n_des")
_PIN_FIELDS = ("p_pins", "n_pins", "in_p_pins", "in_n_pins")


# --- where it lives ----------------------------------------------------------

def sidecar_path(prjpcb_path: Path, pcbdoc_path: Path | None) -> Path:
    """``<Board>.fypa-setup.json`` beside the PcbDoc (beside the PrjPcb when
    the PcbDoc isn't known)."""
    anchor = Path(pcbdoc_path) if pcbdoc_path else Path(prjpcb_path)
    return anchor.with_name(anchor.stem + SIDECAR_SUFFIX)


def fallback_path(prjpcb_path: Path, pcbdoc_path: Path | None) -> Path:
    """The per-user copy, used when the design folder can't be written."""
    from fypa.cli import _project_cache_dir
    return _project_cache_dir(
        Path(prjpcb_path), Path(pcbdoc_path) if pcbdoc_path else None,
    ) / _FALLBACK_NAME


# --- what it holds -----------------------------------------------------------

@dataclass
class HeldEntry:
    """A saved entry that wasn't applied, and why (shown to the user)."""

    directive: EditorDirective
    reason: str
    anchor: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"directive": self.directive.to_dict(), "reason": self.reason,
                "anchor": self.anchor}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> HeldEntry:
        return cls(EditorDirective.from_dict(d.get("directive") or {}),
                   str(d.get("reason") or ""), d.get("anchor") or None)


@dataclass
class SavedSetup:
    """The sidecar's contents. ``anchors`` maps a directive id to the
    fingerprint of the part it was placed on."""

    directives: list[EditorDirective] = field(default_factory=list)
    copper_names: list[CopperName] = field(default_factory=list)
    anchors: dict[str, dict[str, Any]] = field(default_factory=dict)
    held: list[HeldEntry] = field(default_factory=list)
    design: str = ""
    path: Path | None = None

    def is_empty(self) -> bool:
        return not (self.directives or self.copper_names or self.held)


def _to_doc(setup: SavedSetup) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "fypa-setup",
        "design": setup.design,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "directives": [
            {**d.to_dict(), "anchor": setup.anchors.get(d.id)}
            for d in setup.directives
        ],
        "copper_names": [c.to_dict() for c in setup.copper_names],
        "held": [h.to_dict() for h in setup.held],
    }


def _from_doc(doc: dict[str, Any], path: Path) -> SavedSetup:
    ver = int(doc.get("schema_version", 0))
    if ver > SCHEMA_VERSION:
        raise ValueError(
            f"{path.name} was written by a newer version of FYPA "
            f"(schema {ver}; this build understands {SCHEMA_VERSION}).")
    directives: list[EditorDirective] = []
    anchors: dict[str, dict[str, Any]] = {}
    for raw in doc.get("directives") or []:
        if not isinstance(raw, dict):
            continue
        d = EditorDirective.from_dict(raw)
        directives.append(d)
        if isinstance(raw.get("anchor"), dict):
            anchors[d.id] = raw["anchor"]
    return SavedSetup(
        directives=directives,
        copper_names=[CopperName.from_dict(c)
                      for c in doc.get("copper_names") or []
                      if isinstance(c, dict)],
        anchors=anchors,
        held=[HeldEntry.from_dict(h) for h in doc.get("held") or []
              if isinstance(h, dict)],
        design=str(doc.get("design") or ""),
        path=path,
    )


# --- fingerprints ------------------------------------------------------------

def _components_by_designator(extracted) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for i, c in enumerate(getattr(extracted, "pcb_components", ()) or ()):
        out.setdefault(str(c.designator).strip().upper(), []).append(i)
    return out


def _pins_by_component(extracted) -> dict[int, dict[str, str]]:
    """``{component_index: {pad designator: net name}}``."""
    nets = getattr(extracted, "nets", ()) or ()
    out: dict[int, dict[str, str]] = {}
    for pad in getattr(extracted, "pads", ()) or ():
        ci = int(getattr(pad, "component_index", -1))
        if ci < 0:
            continue
        ni = int(getattr(pad, "net_index", -1))
        net = nets[ni].name if 0 <= ni < len(nets) else ""
        out.setdefault(ci, {})[str(pad.designator)] = net
    return out


def component_fingerprint(extracted, designator: str,
                          pins: dict[int, dict[str, str]] | None = None
                          ) -> dict[str, Any] | None:
    """What identifies the part ``designator`` on this design, or ``None``
    when it isn't on the board (or appears more than once)."""
    idx = _components_by_designator(extracted).get(
        str(designator).strip().upper(), [])
    if len(idx) != 1:
        return None
    c = extracted.pcb_components[idx[0]]
    if pins is None:
        pins = _pins_by_component(extracted)
    params = getattr(c, "parameters", None) or {}
    centre = c.center
    if hasattr(centre, "x"):                     # extract.Pt2D
        x, y = float(centre.x), float(centre.y)
    else:
        x, y = float(centre[0]), float(centre[1])
    return {
        "designator": str(c.designator),
        "unique_id": str(getattr(c, "unique_id", "") or ""),
        "source_unique_id": str(getattr(c, "source_unique_id", "") or ""),
        "footprint": str(getattr(c, "footprint", "") or ""),
        "comment": str(params.get("Comment") or params.get("COMMENT") or ""),
        "xy": [round(x, 4), round(y, 4)],
        "layer": str(getattr(c, "layer_name", "") or ""),
        "pins": dict(sorted(pins.get(idx[0], {}).items())),
    }


# --- save / load -------------------------------------------------------------

def save_setup(prjpcb_path: Path, pcbdoc_path: Path | None,
               directives: list[EditorDirective],
               copper_names: list[CopperName], *,
               extracted=None,
               held: list[HeldEntry] | None = None,
               previous: SavedSetup | None = None) -> Path | None:
    """Write the sidecar and return where it went (``None`` if nowhere could
    be written, or when there is nothing to save and no file to clear).

    Fingerprints come from ``extracted`` (the design the setup was made on);
    without it - a viewer opened from the solve cache holds no design - each
    entry keeps the fingerprint ``previous`` recorded for it. ``held``
    entries are written back untouched so a restore that couldn't place them
    doesn't lose them.

    With nothing left to save (every entry removed) the file is deleted
    rather than left to bring old entries back on the next import."""
    held = list(held or [])
    beside = sidecar_path(prjpcb_path, pcbdoc_path)
    targets = [beside]
    try:
        targets.append(fallback_path(prjpcb_path, pcbdoc_path))
    except Exception:  # noqa: BLE001 - no cache dir is not fatal
        pass

    if not directives and not copper_names and not held:
        for p in targets:
            try:
                if p.exists():
                    p.unlink()
            except OSError as e:
                log.warning("Couldn't remove the setup sidecar %s (%s).", p, e)
        return None

    pins = _pins_by_component(extracted) if extracted is not None else None
    anchors: dict[str, dict[str, Any]] = {}
    for d in directives:
        if d.kind != "component" or not d.designator:
            continue
        fp = (component_fingerprint(extracted, d.designator, pins)
              if extracted is not None else None)
        if fp is None and previous is not None:
            fp = previous.anchors.get(d.id)
        if fp is not None:
            anchors[d.id] = fp
    setup = SavedSetup(
        directives=list(directives), copper_names=list(copper_names),
        anchors=anchors, held=held,
        design=Path(pcbdoc_path).name if pcbdoc_path else Path(prjpcb_path).name,
    )
    text = json.dumps(_to_doc(setup), indent=2)
    for i, path in enumerate(targets):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
        except OSError as e:
            log.info("Couldn't write the setup sidecar to %s (%s)%s.", path, e,
                     "; trying the per-user copy" if i + 1 < len(targets) else "")
            continue
        # Written beside the design: drop a stale per-user copy so the two
        # can't disagree on the next load.
        if i == 0:
            for stale in targets[1:]:
                try:
                    if stale.exists():
                        stale.unlink()
                except OSError:
                    pass
        return path
    log.warning("The setup couldn't be saved beside the design or in the "
                "per-user cache; it is only kept in this session.")
    return None


def load_setup(prjpcb_path: Path, pcbdoc_path: Path | None) -> SavedSetup | None:
    """The saved setup for this design, or ``None``. The file beside the
    design wins over the per-user copy. An unreadable file is logged and
    treated as absent - a broken sidecar must never stop a design opening."""
    candidates = [sidecar_path(prjpcb_path, pcbdoc_path)]
    try:
        candidates.append(fallback_path(prjpcb_path, pcbdoc_path))
    except Exception:  # noqa: BLE001
        pass
    for path in candidates:
        if not path.is_file():
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(doc, dict):
                raise ValueError("not a JSON object")
            return _from_doc(doc, path)
        except Exception as e:  # noqa: BLE001
            log.warning("Ignoring the setup sidecar %s: %s", path, e)
    return None


# --- restore -----------------------------------------------------------------

@dataclass
class RestoreReport:
    """The outcome of matching a saved setup against a design."""

    applied: list[EditorDirective] = field(default_factory=list)
    copper_names: list[CopperName] = field(default_factory=list)
    held: list[HeldEntry] = field(default_factory=list)
    superseded: list[HeldEntry] = field(default_factory=list)
    path: Path | None = None

    def to_metadata(self) -> dict[str, Any]:
        """Plain-JSON form carried to the viewer in the solve metadata."""
        return {
            "path": str(self.path) if self.path else None,
            "applied": [d.to_dict() for d in self.applied],
            "copper_names": [c.to_dict() for c in self.copper_names],
            "held": [h.to_dict() for h in self.held],
            "superseded": [h.to_dict() for h in self.superseded],
        }

    @classmethod
    def from_metadata(cls, d: dict[str, Any]) -> RestoreReport:
        return cls(
            applied=[EditorDirective.from_dict(x) for x in d.get("applied") or []],
            copper_names=[CopperName.from_dict(x)
                          for x in d.get("copper_names") or []],
            held=[HeldEntry.from_dict(x) for x in d.get("held") or []],
            superseded=[HeldEntry.from_dict(x)
                        for x in d.get("superseded") or []],
            path=Path(d["path"]) if d.get("path") else None,
        )


def schematic_designators(directives) -> set[str]:
    """Upper-cased designators the schematic sets up as a source, sink or
    regulator (auto-bridges and series parts don't count: the schematic
    didn't *define* those)."""
    return {
        str(getattr(d, "designator", "") or "").strip().upper()
        for d in directives or ()
        if type(d).__name__ in _SCHEMATIC_SPEC_CLASSES
    }


def describe(d: EditorDirective) -> str:
    """Short label for an entry: ``SINK U12 on +3V3``."""
    where = d.designator if d.kind == "component" and d.designator else "marker"
    net = d.p_net or d.in_p_net or ""
    return f"{d.role} {where}" + (f" on {net}" if net else "")


def _identity_problem(fp: dict[str, Any] | None,
                      now: dict[str, Any]) -> str | None:
    """Why the part now carrying the designator isn't the part the entry was
    placed on, or ``None`` when it is (or there's nothing to compare)."""
    if not fp:
        return None
    des = now["designator"]
    for key in ("source_unique_id", "unique_id"):
        a, b = fp.get(key) or "", now.get(key) or ""
        if a and b:
            if a == b:
                return None        # the same placed part - ids are decisive
            if key == "source_unique_id":
                return f"{des} is now a different schematic part"
    a, b = fp.get("footprint") or "", now.get("footprint") or ""
    if a and b and a != b:
        return f"{des}'s footprint changed from {a} to {b}"
    a, b = fp.get("comment") or "", now.get("comment") or ""
    if a and b and a != b:
        return f"{des} changed from {a} to {b}"
    return None


def match_exact(saved: SavedSetup, extracted, schematic_directives=()
                ) -> RestoreReport:
    """Check each saved entry (and each previously held one) against
    ``extracted``. Applied entries keep their ids and values unchanged."""
    report = RestoreReport(copper_names=list(saved.copper_names),
                           path=saved.path)
    by_des = _components_by_designator(extracted)
    pins = _pins_by_component(extracted)
    nets = {n.name for n in getattr(extracted, "nets", ()) or ()}
    nets.update(c.name for c in saved.copper_names)
    from_schematic = schematic_designators(schematic_directives)

    candidates = [(d, saved.anchors.get(d.id)) for d in saved.directives]
    candidates += [(h.directive, h.anchor) for h in saved.held]
    for d, anchor in candidates:
        problem: str | None = None
        if d.kind == "component" and d.designator:
            des = d.designator.strip().upper()
            idx = by_des.get(des, [])
            if not idx:
                problem = f"{d.designator} is not on this board"
            elif len(idx) > 1:
                problem = f"{d.designator} appears more than once on this board"
            else:
                now = component_fingerprint(extracted, d.designator, pins)
                problem = _identity_problem(anchor, now) if now else None
                if problem is None:
                    have = set(pins.get(idx[0], {}))
                    for f in _PIN_FIELDS:
                        gone = [p for p in getattr(d, f) or [] if p not in have]
                        if gone:
                            problem = (f"{d.designator} has no pin "
                                       f"{', '.join(gone)} any more")
                            break
        if problem is None:
            for f in _NET_FIELDS:
                net = getattr(d, f)
                if net and net not in nets:
                    problem = f"net {net} no longer exists"
                    break
        if problem is None:
            for f in _DES_FIELDS:
                gone = [x for x in getattr(d, f) or []
                        if x.strip().upper() not in by_des]
                if gone:
                    problem = f"{', '.join(gone)} is not on this board"
                    break
        if problem is not None:
            report.held.append(HeldEntry(d, problem, anchor))
            continue
        if (d.kind == "component" and d.role in _SCHEMATIC_ROLES
                and not d.overrides_designator
                and (d.designator or "").strip().upper() in from_schematic):
            report.superseded.append(HeldEntry(
                d, f"{d.designator} now has PDN parameters in the schematic, "
                   "which take precedence", anchor))
            continue
        report.applied.append(d)
    return report


__all__ = [
    "HeldEntry", "RestoreReport", "SIDECAR_SUFFIX", "SavedSetup",
    "component_fingerprint", "describe", "fallback_path", "load_setup",
    "match_exact", "save_setup", "schematic_designators", "sidecar_path",
]
