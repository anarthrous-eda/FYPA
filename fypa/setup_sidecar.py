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
# A setup set aside by a clean import (see set_aside_setup).
BACKUP_SUFFIX = ".bak"
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
# Anchor key holding the fingerprints of the other parts an entry names in
# its designator lists ({upper designator: fingerprint}).
_RELATED = "related"


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
    if pins is None:
        pins = _pins_by_component(extracted)
    return _fingerprint_at(extracted, idx[0], pins)


def _fingerprint_at(extracted, index: int,
                    pins: dict[int, dict[str, str]]) -> dict[str, Any]:
    """The fingerprint of component ``index`` (see
    :func:`component_fingerprint`)."""
    c = extracted.pcb_components[index]
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
        "pins": dict(sorted(pins.get(index, {}).items())),
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
        old = previous.anchors.get(d.id) if previous is not None else None
        if fp is None:
            fp = dict(old) if old else None
        if fp is None:
            continue
        # The other parts the entry names (sense parts, P / N DES), so a
        # renamed one can be followed like the entry's own part.
        related = dict((old or {}).get(_RELATED) or {})
        for x in {x.strip().upper() for f in _DES_FIELDS
                  for x in getattr(d, f) or []}:
            rfp = (component_fingerprint(extracted, x, pins)
                   if extracted is not None else None)
            if rfp is not None:
                rfp.pop(_RELATED, None)
                related[x] = rfp
        wanted = {x.strip().upper() for f in _DES_FIELDS
                  for x in getattr(d, f) or []}
        related = {k: v for k, v in related.items() if k in wanted}
        if related:
            fp[_RELATED] = related
        else:
            fp.pop(_RELATED, None)
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


def setup_hash(setup: SavedSetup) -> str:
    """A key for the solve cache: changes whenever the restore could give a
    different answer, and only then.

    Covers what the matcher reads - the entries with their part
    fingerprints, the copper names and the held entries - and the source of
    the code that does the matching and applies the result, so a change to
    either re-solves. Leaves out what doesn't affect it: when the file was
    saved and where it lives (a design whose setup file moved from the
    per-user cache to beside the board still hits)."""
    import hashlib

    content = {
        "directives": [{**d.to_dict(), "anchor": setup.anchors.get(d.id)}
                       for d in setup.directives],
        "copper_names": [c.to_dict() for c in setup.copper_names],
        "held": [h.to_dict() for h in setup.held],
    }
    h = hashlib.sha1(json.dumps(content, sort_keys=True).encode("utf-8"))
    here = Path(__file__).resolve().parent
    for src in (here / "setup_sidecar.py", here / "editor_directives.py",
                here / "project_file.py"):
        try:
            h.update(src.read_bytes())
        except OSError:
            # A frozen build ships no .py sources; the version string below
            # stands in for them there.
            pass
    try:
        from fypa.cli import __version__
        h.update(str(__version__).encode("utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return h.hexdigest()[:20]


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


def set_aside_setup(prjpcb_path: Path, pcbdoc_path: Path | None) -> list[Path]:
    """Rename this design's saved setup (the file beside the board and the
    per-user copy) to ``<name>.bak``, replacing an older backup, so the next
    import starts without it - Import Altium Design (Clean). Returns the
    backups made; a file that can't be renamed is logged and left alone."""
    candidates = [sidecar_path(prjpcb_path, pcbdoc_path)]
    try:
        candidates.append(fallback_path(prjpcb_path, pcbdoc_path))
    except Exception:  # noqa: BLE001
        pass
    backups: list[Path] = []
    for path in candidates:
        if not path.is_file():
            continue
        bak = path.with_name(path.name + BACKUP_SUFFIX)
        try:
            path.replace(bak)
        except OSError as e:
            log.warning("Couldn't set the saved setup %s aside: %s", path, e)
            continue
        log.info("Saved setup set aside for a clean import: %s", bak)
        backups.append(bak)
    return backups


# --- restore -----------------------------------------------------------------

# Review statuses, worst-understood last. Only the review dialog changes
# anything for these: nothing below "renamed" is applied without the user.
REVIEW_NET_CHANGED = "net-changed"   # same part, a net it uses was renamed
REVIEW_LIKELY = "likely"             # one part fits well, but nothing proves it
REVIEW_AMBIGUOUS = "ambiguous"       # several parts fit about equally
REVIEW_MISSING = "missing"           # nothing plausible

REVIEW_LABELS = {
    REVIEW_NET_CHANGED: "Net changed",
    REVIEW_LIKELY: "Likely match",
    REVIEW_AMBIGUOUS: "Several matches",
    REVIEW_MISSING: "Not found",
}


@dataclass
class Candidate:
    """A part a held entry could move to, with the entry rewritten for it."""

    designator: str
    directive: EditorDirective
    why: str
    changes: list[str] = field(default_factory=list)
    score: float = 0.0
    xy: tuple[float, float] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"designator": self.designator,
                "directive": self.directive.to_dict(), "why": self.why,
                "changes": list(self.changes), "score": self.score,
                "xy": list(self.xy) if self.xy else None}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Candidate:
        xy = d.get("xy")
        return cls(str(d.get("designator") or ""),
                   EditorDirective.from_dict(d.get("directive") or {}),
                   str(d.get("why") or ""), list(d.get("changes") or []),
                   float(d.get("score") or 0.0),
                   (float(xy[0]), float(xy[1])) if xy else None)


@dataclass
class ReviewItem:
    """A held entry the user decides on: its status and the parts it could
    go to (best first; empty when nothing fits)."""

    entry: HeldEntry
    status: str
    candidates: list[Candidate] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"entry": self.entry.to_dict(), "status": self.status,
                "candidates": [c.to_dict() for c in self.candidates]}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ReviewItem:
        return cls(HeldEntry.from_dict(d.get("entry") or {}),
                   str(d.get("status") or REVIEW_MISSING),
                   [Candidate.from_dict(c) for c in d.get("candidates") or []])


@dataclass
class RestoreReport:
    """The outcome of matching a saved setup against a design.

    ``held`` is every entry not applied (kept in the sidecar); ``review``
    has one item per held entry saying why and what it could go to.
    ``changes`` notes, per applied entry id, what the restore changed about
    it (``"U12 → U14"``) - applied without asking because the part is
    provably the same. ``stranded`` maps applied SINKs with an ideal return
    on a rail supplied only through copper - the solve skips those (see
    :func:`fypa.editor_directives.stranded_ideal_sinks`) - to the ground
    nets to suggest for them."""

    applied: list[EditorDirective] = field(default_factory=list)
    copper_names: list[CopperName] = field(default_factory=list)
    held: list[HeldEntry] = field(default_factory=list)
    superseded: list[HeldEntry] = field(default_factory=list)
    review: list[ReviewItem] = field(default_factory=list)
    changes: dict[str, list[str]] = field(default_factory=dict)
    stranded: dict[str, list[str]] = field(default_factory=dict)
    path: Path | None = None

    def to_metadata(self) -> dict[str, Any]:
        """Plain-JSON form carried to the viewer in the solve metadata."""
        return {
            "path": str(self.path) if self.path else None,
            "applied": [d.to_dict() for d in self.applied],
            "copper_names": [c.to_dict() for c in self.copper_names],
            "held": [h.to_dict() for h in self.held],
            "superseded": [h.to_dict() for h in self.superseded],
            "review": [r.to_dict() for r in self.review],
            "changes": {k: list(v) for k, v in self.changes.items()},
            "stranded": {k: list(v) for k, v in self.stranded.items()},
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
            review=[ReviewItem.from_dict(x) for x in d.get("review") or []],
            changes={str(k): list(v)
                     for k, v in (d.get("changes") or {}).items()},
            stranded={str(k): list(v)
                      for k, v in (d.get("stranded") or {}).items()},
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


def _find_part(fp: dict[str, Any], now_fps: list[dict[str, Any]]) -> int | None:
    """Where the part ``fp`` describes is now: the one part with its Altium
    unique id (schematic link, else PCB id), else the one part with its
    footprint and value on its side, where it was. ``None`` when neither is
    unique."""
    for key in ("source_unique_id", "unique_id"):
        want = fp.get(key) or ""
        if want:
            idx = [i for i, now in enumerate(now_fps) if now.get(key) == want]
            if len(idx) == 1:
                return idx[0]
    spots = [i for i, now in enumerate(now_fps) if _score(fp, now)[2]]
    return spots[0] if len(spots) == 1 else None


def _follow_related(d: EditorDirective, anchor: dict[str, Any] | None,
                    by_des: dict[str, list[int]], now_fps: list[dict[str, Any]]
                    ) -> tuple[EditorDirective, dict[str, Any] | None,
                               list[str], str | None]:
    """``(d, anchor)`` with the other parts ``d`` names (sense parts, P / N
    DES) moved to their designators now, what changed, and why one couldn't
    be followed (``None`` when all were). The anchor is a copy whenever it
    changes, its fingerprints keyed by the new designators.

    A part still on its designator, and still the same part, stays. One
    that was renamed is found by :func:`_find_part` from the fingerprint
    saved with the entry. Without a fingerprint only the designator is
    checked (the caller reports it missing)."""
    related: dict[str, Any] = (anchor or {}).get(_RELATED) or {}
    rename: dict[str, str] = {}
    for f in _DES_FIELDS:
        for x in getattr(d, f) or []:
            key = x.strip().upper()
            if key in rename:
                continue
            fp = related.get(key)
            idx = by_des.get(key, [])
            if fp is None:
                continue
            if len(idx) == 1 and _identity_problem(fp, now_fps[idx[0]]) is None:
                continue
            hit = _find_part(fp, now_fps)
            if hit is None:
                why = (_identity_problem(fp, now_fps[idx[0]]) if len(idx) == 1
                       else None) or f"{x} is not on this board"
                return d, anchor, [], why
            new_des = now_fps[hit]["designator"]
            if new_des.strip().upper() != key:
                rename[key] = new_des
    if not rename:
        return d, anchor, [], None
    new = EditorDirective.from_dict(d.to_dict())     # same id, same values
    for f in _DES_FIELDS:
        lst = getattr(d, f)
        if lst:
            out: list[str] = []
            for x in lst:
                y = rename.get(x.strip().upper(), x)
                if y.upper() not in {z.upper() for z in out}:
                    out.append(y)
            setattr(new, f, out)
    # Each renamed part's fingerprint goes with it to its new designator.
    moved = {**(anchor or {}),
             _RELATED: {rename.get(k, k).strip().upper(): v
                        for k, v in related.items()}}
    changes = [f"{old} → {des} (a part it names)"
               for old, des in rename.items()]
    return new, moved, changes, None


def match_exact(saved: SavedSetup, extracted, schematic_directives=()
                ) -> RestoreReport:
    """Check each saved entry (and each previously held one) against
    ``extracted``. Applied entries keep their ids and values, except that a
    sense / DES part renamed since (found by its saved fingerprint, see
    :func:`_follow_related`) is followed to its new designator, noted in
    ``changes``."""
    return _prune_changes(_match_exact(saved, extracted, schematic_directives))


def _match_exact(saved: SavedSetup, extracted, schematic_directives=()
                 ) -> RestoreReport:
    """:func:`match_exact`, with change notes kept for held entries too (the
    held ones :func:`match_saved` places carry them on)."""
    report = RestoreReport(copper_names=list(saved.copper_names),
                           path=saved.path)
    by_des = _components_by_designator(extracted)
    pins = _pins_by_component(extracted)
    nets = {n.name for n in getattr(extracted, "nets", ()) or ()}
    nets.update(c.name for c in saved.copper_names)
    from_schematic = schematic_designators(schematic_directives)

    now_fps = [_fingerprint_at(extracted, i, pins)
               for i in range(len(getattr(extracted, "pcb_components", ()) or ()))]

    candidates = [(d, saved.anchors.get(d.id)) for d in saved.directives]
    candidates += [(h.directive, h.anchor) for h in saved.held]
    for d, anchor in candidates:
        problem: str | None = None
        # Renamed sense / DES parts first, so every check below (and the
        # entry's own part matching in match_saved) sees them where they are.
        d, anchor, followed, problem = _follow_related(d, anchor, by_des, now_fps)
        if followed:
            report.changes[d.id] = followed
        if problem is None and d.kind == "component" and d.designator:
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


def _prune_changes(report: RestoreReport) -> RestoreReport:
    """Keep change notes only for the entries actually applied."""
    ids = {d.id for d in report.applied}
    report.changes = {k: v for k, v in report.changes.items() if k in ids}
    return report


# --- matching parts that changed ---------------------------------------------

# A part within this distance of where the entry's part was counts as "the
# same spot" (mm) - re-annotation keeps parts put; a nudge is still close.
_SAME_SPOT_MM = 0.5
_NEAR_MM = 5.0
# A candidate below this score isn't offered as a match at all, and the best
# needs to beat the runner-up by this much to be "the" likely one.
_MIN_SCORE = 5.0
_CLEAR_LEAD = 1.0
_MAX_CANDIDATES = 5
_MOVED_MM = 0.5

# Each net field and the pin restriction / designator list that goes with it.
_NET_PIN = (("p_net", "p_pins", "p_des"), ("n_net", "n_pins", "n_des"),
            ("in_p_net", "in_p_pins", None), ("in_n_net", "in_n_pins", None))


def _xy(fp: dict[str, Any] | None) -> tuple[float, float] | None:
    xy = (fp or {}).get("xy")
    return (float(xy[0]), float(xy[1])) if xy else None


def _dist(a: dict[str, Any] | None, b: dict[str, Any] | None) -> float | None:
    pa, pb = _xy(a), _xy(b)
    if pa is None or pb is None:
        return None
    return ((pa[0] - pb[0]) ** 2 + (pa[1] - pb[1]) ** 2) ** 0.5


def _retarget(d: EditorDirective, anchor: dict[str, Any] | None,
              now: dict[str, Any], nets: set[str], by_des: dict[str, list[int]],
              now_fps: list[dict[str, Any]] | None = None
              ) -> tuple[EditorDirective, list[str], bool] | None:
    """``(d rewritten for the part now, what changed, whether a net had
    to be followed by pin)`` - or ``None`` when it can't go there (a pin it is restricted to
    is missing, or a net it needs can't be found on the part's pins).

    A net that no longer exists, or isn't on the new part, is followed by
    pin: the pads that carried it on the old part, read on the new one. It
    is only taken when those pads all agree on one net."""
    new = EditorDirective.from_dict(d.to_dict())     # same id, same values
    changes: list[str] = []
    nets_changed = False
    old_des, new_des = d.designator or "", now["designator"]
    if old_des.strip().upper() != new_des.strip().upper():
        new.designator = new_des
        changes.append(f"{old_des} → {new_des}")
        if (d.overrides_designator or "").strip().upper() == old_des.strip().upper():
            new.overrides_designator = new_des
        for f in _DES_FIELDS:
            lst = getattr(d, f)
            if lst:
                setattr(new, f, [new_des if x.strip().upper()
                                 == old_des.strip().upper() else x for x in lst])
    for f in _DES_FIELDS:
        if any(x.strip().upper() not in by_des for x in getattr(new, f) or []):
            return None
    # The parts it names must still be the parts it was set up with.
    if now_fps is not None and _follow_related(new, anchor, by_des, now_fps)[3]:
        return None
    pins_now: dict[str, str] = now.get("pins") or {}
    pins_old: dict[str, str] = (anchor or {}).get("pins") or {}
    for f in _PIN_FIELDS:
        if any(p not in pins_now for p in getattr(new, f) or []):
            return None
    for net_f, pin_f, des_f in _NET_PIN:
        net = getattr(new, net_f)
        if not net:
            continue
        on_part = net in set(pins_now.values())
        # A terminal drawn from other parts' pads (p_des / n_des) needn't be
        # on this part; it only has to exist.
        if des_f and getattr(new, des_f):
            if net in nets:
                continue
            return None
        if net in nets and on_part:
            continue
        pads = list(getattr(new, pin_f) or []) or [
            p for p, n in pins_old.items() if n == net]
        found = {pins_now[p] for p in pads if pins_now.get(p)}
        if len(found) != 1:
            return None
        repl = found.pop()
        if repl != net:
            setattr(new, net_f, repl)
            changes.append(f"net {net} → {repl}")
            nets_changed = True
    # Supply and return on one net is no load at all - the pins followed
    # don't belong to a part like this one.
    for a, b in (("p_net", "n_net"), ("in_p_net", "in_n_net")):
        if getattr(new, a) and getattr(new, a) == getattr(new, b):
            return None
    return new, changes, nets_changed


def _score(anchor: dict[str, Any], now: dict[str, Any]
           ) -> tuple[float, str, bool]:
    """How well part ``now`` fits the part ``anchor`` described: ``(score,
    why, same_spot)``. ``same_spot`` is the strict test for applying a
    rename without asking: same footprint, value and side, where it was."""
    why: list[str] = []
    score = 0.0
    fp_eq = bool(anchor.get("footprint")) and anchor["footprint"] == now.get("footprint")
    a_cm, n_cm = anchor.get("comment") or "", now.get("comment") or ""
    cm_eq = bool(a_cm) and a_cm == n_cm
    if fp_eq:
        score += 3
        why.append("same footprint")
    if cm_eq:
        score += 3
        why.append(f"same value ({a_cm})")
    same_side = bool(anchor.get("layer")) and anchor.get("layer") == now.get("layer")
    if same_side:
        score += 1
    d = _dist(anchor, now)
    if d is not None and d < _SAME_SPOT_MM:
        score += 4
        why.append("same position")
    elif d is not None and d < _NEAR_MM:
        score += 2
        why.append(f"{d:.1f} mm away")
    old_pins = anchor.get("pins") or {}
    new_pins = now.get("pins") or {}
    if old_pins:
        same = sum(1 for p, n in old_pins.items() if n and new_pins.get(p) == n)
        frac = same / len(old_pins)
        score += 3 * frac
        if frac >= 0.999:
            why.append("same pin nets")
        elif frac > 0:
            why.append(f"{same} of {len(old_pins)} pin nets the same")
    same_spot = (fp_eq and (cm_eq or not (a_cm or n_cm)) and same_side
                 and d is not None and d < _SAME_SPOT_MM)
    return score, ", ".join(why) or "nothing in common", same_spot


def match_saved(saved: SavedSetup, extracted, schematic_directives=()
                ) -> RestoreReport:
    """:func:`match_exact`, then a second look at what it held back.

    Entries placed on the same part are moved together, and no two parts'
    entries can claim the same new part. For each held part, in order:

    * **Same placed part, new designator** - its Altium unique id (schematic
      link, else PCB id) is on another designator: applied, the change
      noted. If a net had to be followed by pin as well, it goes to review.
    * **Same spot** - one part with the same footprint, value and side sits
      where it was, and its nets line up: applied as a rename, noted.
    * Otherwise it goes to review: **net changed** (the same part, a net it
      uses renamed - proposed, not applied), **likely** (one clear best
      part), **several matches** (no clear best) or **not found**.

    Nothing in review is applied; the dialog does that once the user
    agrees. Free markers and entries saved without a fingerprint are only
    ever matched exactly."""
    report = _match_exact(saved, extracted, schematic_directives)
    # Renamed sense / DES parts match_exact already followed, per entry id.
    followed = dict(report.changes)
    comps = getattr(extracted, "pcb_components", ()) or ()
    by_des = _components_by_designator(extracted)
    pins = _pins_by_component(extracted)
    nets = {n.name for n in getattr(extracted, "nets", ()) or ()}
    nets.update(c.name for c in saved.copper_names)
    from_schematic = schematic_designators(schematic_directives)
    now_fps = [_fingerprint_at(extracted, i, pins) for i in range(len(comps))]

    # Moved, but otherwise matched exactly: worth a note, nothing more.
    for d in report.applied:
        if d.kind == "component" and d.designator:
            idx = by_des.get(d.designator.strip().upper(), [])
            moved = (_dist(saved.anchors.get(d.id), now_fps[idx[0]])
                     if len(idx) == 1 else None)
            if moved is not None and moved >= _MOVED_MM:
                report.changes.setdefault(d.id, []).append(
                    f"moved {moved:.1f} mm")

    # Parts already spoken for: by an applied entry of that designator.
    claimed: dict[int, str] = {}
    for d in report.applied:
        if d.kind == "component" and d.designator:
            for i in by_des.get(d.designator.strip().upper(), []):
                claimed[i] = d.designator.strip().upper()

    # Group the held entries by the part they were placed on.
    groups: dict[str, list[HeldEntry]] = {}
    loose: list[HeldEntry] = []
    for h in report.held:
        d = h.directive
        if d.kind == "component" and d.designator and h.anchor:
            groups.setdefault(d.designator.strip().upper(), []).append(h)
        else:
            loose.append(h)
    report.held = []

    def _fit_all(group: list[HeldEntry], i: int):
        """Every entry of ``group`` retargeted to part ``i``, or ``None``."""
        out = []
        for h in group:
            r = _retarget(h.directive, h.anchor, now_fps[i], nets, by_des,
                          now_fps)
            if r is None:
                return None
            out.append(r)
        return out

    def _free(i: int, key: str) -> bool:
        return claimed.get(i, key) == key

    def _apply(group, fits, why: str) -> None:
        for h, (new, changes, _nets) in zip(group, fits):
            if (new.role in _SCHEMATIC_ROLES and not new.overrides_designator
                    and (new.designator or "").strip().upper() in from_schematic):
                report.superseded.append(HeldEntry(
                    new, f"{new.designator} now has PDN parameters in the "
                         "schematic, which take precedence", h.anchor))
                continue
            report.applied.append(new)
            notes = [*followed.get(new.id, []), *changes]
            if changes:
                notes.append(why)
            if notes:
                report.changes[new.id] = notes

    def _hold(group, status: str, options) -> None:
        """Hold every entry of ``group`` for review. ``options`` is a list
        of ``(part index, fits, why, score)``; each entry gets its own
        rewritten copy for each candidate part."""
        for k, h in enumerate(group):
            cands = [Candidate(now_fps[i]["designator"], fits[k][0], why,
                               [*followed.get(h.directive.id, []), *fits[k][1]],
                               score, _xy(now_fps[i]))
                     for i, fits, why, score in options]
            report.held.append(h)
            report.review.append(ReviewItem(h, status, cands))

    pending: dict[str, tuple] = {}

    # 1. Unique ids: the same placed part, wherever its designator went.
    for key, group in groups.items():
        anchor = group[0].anchor or {}
        hit = None
        for id_key in ("source_unique_id", "unique_id"):
            want = anchor.get(id_key) or ""
            if not want:
                continue
            idx = [i for i, fp in enumerate(now_fps) if fp.get(id_key) == want]
            if len(idx) == 1:
                hit = idx[0]
                break
        if hit is None or not _free(hit, key):
            continue
        fits = _fit_all(group, hit)
        if fits is None:
            pending[key] = ("blocked", hit)
            continue
        if any(nets_changed for _n, _ch, nets_changed in fits):
            # A net had to be followed by pin: the user confirms that.
            pending[key] = ("net", hit, fits)
            claimed[hit] = key
            continue
        claimed[hit] = key
        why = ("same schematic part (Altium unique id)"
               if hit != (by_des.get(key) or [None])[0] else "")
        _apply(group, fits, why or "same part")
        pending[key] = ("done",)

    # 2. Same spot, same footprint and value: a re-annotation.
    for key, group in groups.items():
        if key in pending:
            continue
        anchor = group[0].anchor or {}
        spots = []
        for i, fp in enumerate(now_fps):
            if not _free(i, key):
                continue
            score, why, same_spot = _score(anchor, fp)
            if same_spot:
                spots.append((i, why))
        if len(spots) != 1:
            continue
        i, why = spots[0]
        fits = _fit_all(group, i)
        if fits is None or any(nets_changed for _n, _c, nets_changed in fits):
            continue
        claimed[i] = key
        _apply(group, fits, why)
        pending[key] = ("done",)

    # 3. Everything else goes to review, with its best candidates.
    for key, group in groups.items():
        state = pending.get(key)
        if state and state[0] == "done":
            continue
        if state and state[0] == "net":
            _, i, fits = state
            _hold(group, REVIEW_NET_CHANGED,
                  [(i, fits, "same part; a net it uses was renamed", 99.0)])
            continue
        anchor = group[0].anchor or {}
        schematic_roles = any(h.directive.role in _SCHEMATIC_ROLES
                              and not h.directive.overrides_designator
                              for h in group)
        scored = []
        for i, fp in enumerate(now_fps):
            if not _free(i, key):
                continue
            # The schematic defines this part: the entry would only be
            # dropped there.
            if schematic_roles and fp["designator"].strip().upper() in from_schematic:
                continue
            score, why, _same = _score(anchor, fp)
            if score < _MIN_SCORE:
                continue
            fits = _fit_all(group, i)
            # A different part on different nets isn't a rename - offering
            # every look-alike connector on the board helps no one. (The
            # user can still pick one by hand.)
            if fits is None or any(nets_changed for _n, _c, nets_changed in fits):
                continue
            scored.append((score, i, why, fits))
        scored.sort(key=lambda t: (-t[0], now_fps[t[1]]["designator"]))
        options = [(i, fits, why, score)
                   for score, i, why, fits in scored[:_MAX_CANDIDATES]]
        if not options:
            status = REVIEW_MISSING
        elif len(scored) > 1 and scored[0][0] - scored[1][0] < _CLEAR_LEAD:
            status = REVIEW_AMBIGUOUS
        else:
            status = REVIEW_LIKELY
        _hold(group, status, options)

    # Two parts' entries whose best guess is the same part: neither is clear.
    best: dict[str, list[ReviewItem]] = {}
    for item in report.review:
        if item.status == REVIEW_LIKELY and item.candidates:
            best.setdefault(item.candidates[0].designator.upper(), []).append(item)
    for items in best.values():
        if len({it.entry.directive.designator.upper() for it in items
                if it.entry.directive.designator}) > 1:
            for it in items:
                it.status = REVIEW_AMBIGUOUS

    for h in loose:
        report.held.append(h)
        report.review.append(ReviewItem(h, REVIEW_MISSING, []))
    return _prune_changes(report)


# --- importing a setup from another design ----------------------------------

def load_setup_file(path: Path) -> SavedSetup | str:
    """A setup to import, read from ``path``: a ``.fypa-setup.json``, a
    ``.fypa`` project, or a ``.PcbDoc`` / ``.PrjPcb`` with a setup file
    beside it. Returns a sentence when there is nothing to import.

    A ``.fypa`` carries no part fingerprints, so its entries can only be
    matched by designator (and offered for review otherwise)."""
    path = Path(path)
    name = path.name.lower()
    if name.endswith((".pcbdoc", ".prjpcb")):
        beside = sidecar_path(path, path if name.endswith(".pcbdoc") else None)
        if not beside.is_file():
            return (f"{path.name} has no saved setup beside it "
                    f"({beside.name}). Set it up in FYPA first, or pick its "
                    ".fypa project.")
        path = beside
        name = path.name.lower()
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return f"{path.name} couldn't be read: {e}"
    if not isinstance(doc, dict):
        return f"{path.name} is not a FYPA setup or project file."
    try:
        if doc.get("kind") == "fypa-setup":
            setup = _from_doc(doc, path)
        elif "editor_directives" in doc:
            from fypa.project_file import ProjectFile
            proj = ProjectFile.load(path)
            setup = SavedSetup(directives=list(proj.editor_directives),
                               design=Path(proj.pcbdoc_path or path).name,
                               path=path)
        else:
            return f"{path.name} is not a FYPA setup or project file."
    except ValueError as e:
        return str(e)
    if not (setup.directives or setup.held):
        return f"{path.name} has no sources, sinks or regulators to import."
    return setup


def _same_setup(a: EditorDirective, b: EditorDirective) -> bool:
    return (a.kind == b.kind == "component"
            and (a.designator or "").strip().upper()
            == (b.designator or "").strip().upper()
            and a.role == b.role and (a.p_net or "") == (b.p_net or ""))


def match_import(source: SavedSetup, extracted, schematic_directives=(),
                 existing: list[EditorDirective] | None = None
                 ) -> tuple[RestoreReport, list[HeldEntry]]:
    """Match a setup from another design against this one.

    Returns ``(report, skipped)``: the usual :func:`match_saved` report
    (applied / review / superseded), plus the entries skipped outright -
    markers placed on copper (their position means nothing on another
    board) and parts this design already has set up the same way.
    Imported entries get fresh ids so they never collide with this
    design's own."""
    import uuid

    skipped: list[HeldEntry] = []
    directives: list[EditorDirective] = []
    anchors: dict[str, dict[str, Any]] = {}
    pool = [(d, source.anchors.get(d.id)) for d in source.directives]
    pool += [(h.directive, h.anchor) for h in source.held]
    for d, anchor in pool:
        if d.kind != "component" or not d.designator:
            skipped.append(HeldEntry(
                d, "placed on copper, not on a part - place it again on "
                   "this board"))
            continue
        new = EditorDirective.from_dict({**d.to_dict(),
                                         "id": uuid.uuid4().hex[:12]})
        directives.append(new)
        if anchor:
            anchors[new.id] = anchor
    report = match_saved(
        SavedSetup(directives=directives, anchors=anchors, path=source.path),
        extracted, schematic_directives)
    report.copper_names = []
    existing = list(existing or [])
    kept = []
    for d in report.applied:
        if any(_same_setup(d, e) for e in existing):
            skipped.append(HeldEntry(
                d, f"{d.designator} is already set up as a "
                   f"{d.role.lower()} on {d.p_net or 'this net'}"))
        else:
            kept.append(d)
    report.applied = kept
    return report, skipped


def retarget_to(entry: HeldEntry, extracted, designator: str,
                copper_names=()) -> Candidate | str:
    """Move a held entry to a part the user picked. Returns the
    :class:`Candidate`, or a sentence saying why it can't go there."""
    pins = _pins_by_component(extracted)
    by_des = _components_by_designator(extracted)
    idx = by_des.get(str(designator).strip().upper(), [])
    if len(idx) != 1:
        return f"{designator} is not on this board."
    d = entry.directive
    if d.kind != "component":
        return ("A marker placed on copper can't be moved to a part - place "
                "it again on the board.")
    now = _fingerprint_at(extracted, idx[0], pins)
    nets = {n.name for n in getattr(extracted, "nets", ()) or ()}
    nets.update(c.name for c in copper_names)
    r = _retarget(d, entry.anchor, now, nets, by_des)
    if r is None:
        return (f"{now['designator']} doesn't have the pins or nets this "
                f"{d.role.lower()} needs ({describe(d)}).")
    new, changes, _nets = r
    why = (_score(entry.anchor, now)[1] if entry.anchor else "picked by hand")
    return Candidate(now["designator"], new, why, changes, 0.0, _xy(now))


__all__ = [
    "Candidate", "HeldEntry", "REVIEW_AMBIGUOUS", "REVIEW_LABELS",
    "REVIEW_LIKELY", "REVIEW_MISSING", "REVIEW_NET_CHANGED", "RestoreReport",
    "ReviewItem", "SIDECAR_SUFFIX", "SavedSetup", "component_fingerprint",
    "describe", "fallback_path", "load_setup", "match_exact", "match_saved",
    "load_setup_file", "match_import", "retarget_to",
    "save_setup", "schematic_designators", "setup_hash", "sidecar_path",
]
