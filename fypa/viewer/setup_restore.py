"""Viewer side of the setup sidecar (:mod:`fypa.setup_sidecar`).

* :func:`adopt_restored_setup` - after an Altium import, take the entries the
  solve worker restored as the viewer's own project entries, and tell the
  user about any that were held back or that the schematic now defines.
* :func:`schedule_setup_save` - called on every setup edit; writes the
  sidecar shortly after (edits come in bursts: a marker drag, a batch undo).
  :func:`flush_setup_save` writes a pending save straight away, before the
  window swaps to another design or closes.
"""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QTimer

log = logging.getLogger(__name__)

_SAVE_DELAY_MS = 800

_ROLE_WORDS = {"SOURCE": "source", "SINK": "sink", "REGULATOR": "regulator",
               "SERIES": "series part"}


def _design_paths(window) -> tuple[Path, Path | None] | None:
    """``(prjpcb, pcbdoc)`` of the window's design when it is an Altium one
    the sidecar applies to, else ``None``."""
    md = getattr(window, "metadata", None) or {}
    if md.get("source_kind") == "gerber" or not md.get("prjpcb_path"):
        return None
    pcbdoc = md.get("pcbdoc_path")
    return Path(md["prjpcb_path"]), Path(pcbdoc) if pcbdoc else None


def _counted(directives) -> str:
    """``2 sinks and 1 source``."""
    counts: dict[str, int] = {}
    for d in directives:
        counts[d.role] = counts.get(d.role, 0) + 1
    parts = [f"{n} {_ROLE_WORDS.get(r, r.lower())}{'' if n == 1 else 's'}"
             for r, n in counts.items()]
    if len(parts) > 1:
        return ", ".join(parts[:-1]) + " and " + parts[-1]
    return parts[0] if parts else "nothing"


# --- restore -----------------------------------------------------------------

def adopt_restored_setup(window, metadata, solution) -> bool:
    """Adopt what the solve worker restored (``metadata["setup_restore"]``).

    Returns ``True`` when a saved setup was found for this design - even if
    none of it could be applied - so the caller skips the "no setup" chooser:
    the notice shown here is the more useful message then."""
    from fypa.setup_sidecar import RestoreReport, describe

    raw = (metadata or {}).pop("setup_restore", None) if isinstance(
        metadata, dict) else None
    if not raw or not hasattr(window, "_ensure_project"):
        return False
    report = RestoreReport.from_metadata(raw)
    proj = window._ensure_project()
    proj.editor_directives = list(report.applied)
    have = {c.id for c in proj.copper_names}
    proj.copper_names = list(proj.copper_names) + [
        c for c in report.copper_names if c.id not in have]
    # Held entries stay in the sidecar (tried again on the next import);
    # superseded ones are dropped - the schematic defines those parts now.
    window._setup_held = list(report.held)

    stub = bool(getattr(solution, "solver_info", {}).get("stub"))
    if stub and report.applied:
        # Not solved with yet (load-only import, or still no source): the
        # topology preview and the Solve button must pick them up.
        window._initial_solve_stale = True
    for hook in ("_refresh_solve_stale_overlay", "_update_pending_rails",
                 "_refresh_editor_markers", "_invalidate_topology",
                 "_sync_placeholder_tabs"):
        fn = getattr(window, hook, None)
        if fn is not None:
            try:
                fn()
            except Exception:  # noqa: BLE001 - cosmetic refresh
                log.debug("Refresh %s after setup restore failed", hook,
                          exc_info=True)
    save_setup_now(window)

    name = report.path.name if report.path else "the saved setup"
    left_out = report.held + report.superseded
    if not left_out:
        window.statusBar().showMessage(
            f"Restored {_counted(report.applied)} from {name}.", 15000)
        return True

    from fypa.viewer.list_dialog import show_list_notice

    items = [f"{describe(h.directive)} - {h.reason}" for h in report.held]
    items += [f"{describe(h.directive)} - {h.reason}"
              for h in report.superseded]
    if report.applied:
        intro = (f"Restored {_counted(report.applied)} from <b>{name}</b>. "
                 f"{len(left_out)} saved "
                 f"{'entry was' if len(left_out) == 1 else 'entries were'} "
                 "not applied because the design has changed:")
    else:
        intro = (f"<b>{name}</b> has a saved setup for this design, but none "
                 "of it could be applied because the design has changed:")
    outro = ("Entries on parts that are missing or changed are kept in the "
             "file and tried again each time this design is imported - place "
             "them again on the board if the part was renamed. Entries the "
             "schematic now defines are dropped: its PDN parameters take "
             "precedence.")
    show_list_notice(window, "Saved setup restored with changes", intro,
                     items, outro, warning=True)
    return True


# --- save --------------------------------------------------------------------

def _used_copper_names(proj) -> list:
    """Copper names some entry actually uses. Names minted for no-net copper
    that nothing sits on are left out - they would make a sidecar for a
    design with no setup at all."""
    used: set[str] = set()
    for d in proj.editor_directives:
        for f in ("p_net", "n_net", "in_p_net", "in_n_net"):
            if getattr(d, f, None):
                used.add(getattr(d, f))
    return [c for c in proj.copper_names if c.name in used]


def save_setup_now(window) -> Path | None:
    """Write the window's setup to its design's sidecar now."""
    from fypa.setup_sidecar import load_setup, save_setup

    timer = getattr(window, "_setup_save_timer", None)
    if timer is not None:
        timer.stop()
    window._setup_save_pending = None
    paths = _design_paths(window)
    if paths is None:
        return None
    proj = getattr(window, "_project", None)
    directives = list(getattr(proj, "editor_directives", None) or [])
    copper = _used_copper_names(proj) if proj is not None else []
    held = list(getattr(window, "_setup_held", None) or [])
    loaded = getattr(window, "_loaded_project", None)
    extracted = getattr(loaded, "extracted", None)
    try:
        previous = load_setup(*paths) if extracted is None else None
        return save_setup(*paths, directives, copper, extracted=extracted,
                          held=held, previous=previous)
    except Exception:  # noqa: BLE001 - saving must never break an edit
        log.warning("Couldn't save the setup sidecar", exc_info=True)
        return None


def schedule_setup_save(window) -> None:
    """Save the sidecar shortly; repeated calls within the delay coalesce.
    The design is captured now, so a save that fires after the window has
    swapped to another design is dropped instead of written to the wrong
    one (the swap flushes it first - see :func:`flush_setup_save`)."""
    paths = _design_paths(window)
    if paths is None:
        return
    window._setup_save_pending = paths
    timer = getattr(window, "_setup_save_timer", None)
    if timer is None:
        timer = QTimer(window)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: _fire_save(window))
        window._setup_save_timer = timer
    timer.start(_SAVE_DELAY_MS)


def _fire_save(window) -> None:
    pending = getattr(window, "_setup_save_pending", None)
    if pending is None:
        return
    if _design_paths(window) != pending:
        window._setup_save_pending = None
        return
    save_setup_now(window)


def flush_setup_save(window) -> None:
    """Write a pending save now (before a design swap or close)."""
    if getattr(window, "_setup_save_pending", None) is not None:
        _fire_save(window)


__all__ = [
    "adopt_restored_setup", "flush_setup_save", "save_setup_now",
    "schedule_setup_save",
]
