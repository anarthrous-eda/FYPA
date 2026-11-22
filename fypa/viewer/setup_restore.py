"""Viewer side of the setup sidecar (:mod:`fypa.setup_sidecar`).

* :func:`adopt_restored_setup` - after an Altium import, take the entries the
  solve worker restored as the viewer's own project entries, and tell the
  user about any that were held back or that the schematic now defines.
* :func:`open_setup_review` - the review dialog for entries held back, and
  :func:`confirm_solve_with_held` - asked before a solve while any are.
* :func:`schedule_setup_save` - called on every setup edit; writes the
  sidecar shortly after (edits come in bursts: a marker drag, a batch undo).
  :func:`flush_setup_save` writes a pending save straight away, before the
  window swaps to another design or closes.
"""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QTimer

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
    what is shown here is the more useful message then. Entries that need a
    decision open the review dialog; changes that were applied without one
    (a re-annotated part, provably the same) are listed in a notice."""
    from fypa.setup_sidecar import RestoreReport, describe

    raw = (metadata or {}).pop("setup_restore", None) if isinstance(
        metadata, dict) else None
    if not raw or not hasattr(window, "_ensure_project"):
        return False
    report = RestoreReport.from_metadata(raw)
    if report.held and not report.review:
        # Matched exactly only (no second look): each held entry is simply
        # not found - still the user's to decide on.
        from fypa.setup_sidecar import REVIEW_MISSING, ReviewItem
        report.review = [ReviewItem(h, REVIEW_MISSING, []) for h in report.held]
    proj = window._ensure_project()
    proj.editor_directives = list(report.applied)
    have = {c.id for c in proj.copper_names}
    proj.copper_names = list(proj.copper_names) + [
        c for c in report.copper_names if c.id not in have]
    # Held entries stay in the sidecar (tried again on the next import);
    # superseded ones are dropped - the schematic defines those parts now.
    window._setup_held = list(report.held)
    window._setup_review = list(report.review)
    window._setup_held_ack = False

    stub = bool(getattr(solution, "solver_info", {}).get("stub"))
    if stub and report.applied:
        # Not solved with yet (load-only import, or still no source): the
        # topology preview and the Solve button must pick them up.
        window._initial_solve_stale = True
    _refresh_after_setup_change(window)
    save_setup_now(window)
    stamp_held(window)

    name = report.path.name if report.path else "the saved setup"
    by_id = {d.id: d for d in report.applied}
    auto = [f"{describe(by_id[i])}: {'; '.join(ch)}"
            for i, ch in report.changes.items()
            if i in by_id and not all(c.startswith("moved ") for c in ch)]
    superseded = [f"{describe(h.directive)} - {h.reason}"
                  for h in report.superseded]
    stranded = [
        f"{describe(by_id[i])} - has an ideal return, but {by_id[i].p_net} "
        "is supplied through copper, so it is not solved: give it "
        + (" or ".join(g) if g else "a ground net")
        + " as its ground (Board, or Manual setup)"
        for i, g in report.stranded.items() if i in by_id]
    if report.review:
        open_setup_review(window, applied_notes=auto + superseded + stranded)
        return True
    if not auto and not superseded and not stranded:
        window.statusBar().showMessage(
            f"Restored {_counted(report.applied)} from {name}.", 15000)
        return True

    from fypa.viewer.list_dialog import show_list_notice

    intro = (f"Restored {_counted(report.applied)} from <b>{name}</b>. The "
             "design has changed since it was saved:" if auto or superseded
             else f"Restored {_counted(report.applied)} from <b>{name}</b>. "
             "Some entries need a change before they are solved:")
    outro = ("Parts that were re-annotated were followed to their new "
             "designator - their Altium unique ids, or their footprint, value "
             "and position, show they are the same part. Entries the "
             "schematic now defines are dropped: its PDN parameters take "
             "precedence.")
    if not auto and not superseded:
        outro = ("A sink with an ideal return closes its loop through a "
                 "source with an ideal return on the same rail; on a rail "
                 "supplied through return copper it needs that ground net.")
    title = ("Saved setup restored with changes" if auto or superseded
             else "Saved setup restored - check these entries")
    show_list_notice(window, title, intro,
                     auto + superseded + stranded, outro)
    return True


def _refresh_after_setup_change(window) -> None:
    for hook in ("_refresh_solve_stale_overlay", "_update_pending_rails",
                 "_refresh_editor_markers", "_invalidate_topology",
                 "_sync_placeholder_tabs"):
        fn = getattr(window, hook, None)
        if fn is not None:
            try:
                fn()
            except Exception:  # noqa: BLE001 - cosmetic refresh
                log.debug("Refresh %s after setup change failed", hook,
                          exc_info=True)


# --- review ------------------------------------------------------------------

def open_setup_review(window, *, applied_notes: list[str] | None = None) -> None:
    """Open the review dialog for the window's held entries and apply what
    the user decides. Says so when there is nothing to review."""
    items = list(getattr(window, "_setup_review", None) or [])
    if not items:
        window.statusBar().showMessage(
            "Nothing to review - every saved setup entry is applied.", 8000)
        return
    decisions = open_review_dialog(window, items, applied_notes=applied_notes)
    if decisions is not None:
        apply_review_decisions(window, decisions)


def open_review_dialog(window, items, *, applied_notes=None,
                       importing: str | None = None, extracted=None):
    """Show the review dialog for ``items`` over ``window``; its decisions,
    or ``None`` when the user backed out. ``importing`` (the source's name)
    words it for an import from another design."""
    from fypa.viewer.setup_review import SetupReviewDialog

    if extracted is None:
        loaded = getattr(window, "_loaded_project", None)
        extracted = getattr(loaded, "extracted", None)
    if extracted is None and hasattr(window, "_topology_loaded_for_setup"):
        # Opened from the solve cache: no design in memory. Fetch it (the
        # design-info cache, usually) so a part can be picked by hand.
        loaded = window._topology_loaded_for_setup("Review saved setup")
        extracted = getattr(loaded, "extracted", None)
    designators = sorted(
        str(c.designator)
        for c in getattr(extracted, "pcb_components", ()) or ())

    def _retarget(item, designator):
        from fypa.setup_sidecar import retarget_to
        proj = window._ensure_project()
        return retarget_to(item.entry, extracted, designator,
                           proj.copper_names)

    def _show(xy, _designator):
        jump = getattr(window, "_jump_to_xy", None)
        if jump is not None and xy:
            jump(xy[0], xy[1], [])

    dlg = SetupReviewDialog(
        window, items, applied_notes=applied_notes,
        designators=designators,
        retarget=_retarget if extracted is not None else None,
        show_on_board=_show if hasattr(window, "_jump_to_xy") else None,
        importing=importing)
    return dlg.decisions() if dlg.exec() else None


def apply_review_decisions(window, decisions) -> None:
    """Carry out the review: accepted entries join the setup (rewritten for
    their new part), removed ones are dropped from the setup file, kept ones
    stay held."""
    from fypa.viewer.setup_review import ACCEPT, REMOVE

    proj = window._ensure_project()
    done: set[str] = set()
    accepted = 0
    for item, action, cand in decisions:
        did = item.entry.directive.id
        if action == ACCEPT and cand is not None:
            proj.editor_directives = [
                d for d in proj.editor_directives if d.id != cand.directive.id]
            proj.editor_directives.append(cand.directive)
            done.add(did)
            accepted += 1
        elif action == REMOVE:
            done.add(did)
    if not done:
        return
    window._setup_held = [
        h for h in (getattr(window, "_setup_held", None) or [])
        if h.directive.id not in done]
    window._setup_review = [
        r for r in (getattr(window, "_setup_review", None) or [])
        if r.entry.directive.id not in done]
    window._setup_held_ack = False
    stamp_held(window)
    if accepted:
        # A setup edit like any other: needs a solve, and saves the sidecar.
        window._mark_project_dirty()
        _refresh_after_setup_change(window)
    else:
        save_setup_now(window)
    removed = len(done) - accepted
    parts = []
    if accepted:
        parts.append(f"{accepted} moved to their new part")
    if removed:
        parts.append(f"{removed} removed")
    left = len(window._setup_held)
    msg = "Saved setup: " + ", ".join(parts)
    if left:
        msg += f"; {left} still held"
    window.statusBar().showMessage(msg + ".", 10000)


def stamp_held(window) -> None:
    """Record the held entries in the window's metadata, where the design
    report reads them (a solve that ran without them must say so)."""
    from fypa.setup_sidecar import describe

    md = getattr(window, "metadata", None)
    if not isinstance(md, dict):
        return
    held = getattr(window, "_setup_held", None) or []
    if held:
        md["setup_held"] = [f"{describe(h.directive)} - {h.reason}"
                            for h in held]
    else:
        md.pop("setup_held", None)


def confirm_solve_with_held(window) -> bool:
    """Before a solve: when saved entries are still held, ask whether to
    review them first. ``True`` = go ahead with the solve."""
    held = getattr(window, "_setup_held", None) or []
    if not held or getattr(window, "_setup_held_ack", False):
        return True
    from PySide6.QtWidgets import QMessageBox

    from fypa.setup_sidecar import describe

    n = len(held)
    shown = "".join(f"<li>{describe(h.directive)} - {h.reason}</li>"
                    for h in held[:6])
    more = f"<li>… and {n - 6} more</li>" if n > 6 else ""
    box = QMessageBox(window)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle("Saved setup entries not applied")
    box.setTextFormat(Qt.RichText)
    box.setText(
        f"{n} saved setup {'entry is' if n == 1 else 'entries are'} not "
        f"applied, so the solve will run without "
        f"{'it' if n == 1 else 'them'}:<ul>{shown}{more}</ul>"
        "A load left out makes the results look better than they are.")
    review = box.addButton("Review…", QMessageBox.ButtonRole.AcceptRole)
    solve = box.addButton("Solve without them",
                          QMessageBox.ButtonRole.DestructiveRole)
    box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(review)
    box.exec()
    clicked = box.clickedButton()
    if clicked is review:
        open_setup_review(window)
        return False
    if clicked is solve:
        window._setup_held_ack = True
        return True
    return False


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
    "adopt_restored_setup", "apply_review_decisions", "open_review_dialog",
    "confirm_solve_with_held", "flush_setup_save", "open_setup_review",
    "save_setup_now", "schedule_setup_save", "stamp_held",
]
