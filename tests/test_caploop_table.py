"""Capacitors-tab Qt rendering: cell text, sort keys, toggles, filters.

These exercise the parts of the tab that only fail once Qt is in the loop —
notably that ``QTableWidgetItem`` aliases ``Qt.EditRole`` onto
``Qt.DisplayRole``, so the conventional "set the sort key on EditRole"
pattern silently replaces "Go ▶" with "0" and "0.5295" with
"0.5294781066094738".
"""
from __future__ import annotations

import os
import time
import types

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QMainWindow,
    QTabWidget,
)

import fypa.altium_viewer as av  # noqa: E402
from fypa.caploop.tier3 import Tier3Result  # noqa: E402
from fypa.project_file import ProjectFile  # noqa: E402
from tests.test_caploop_identify import (  # noqa: E402
    RAILS,
    _directives,
    _standard_cap_project,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class _Viewer(av.PdnViewer):
    """PdnViewer with just the state the Capacitors tab touches.

    ``QMainWindow.__init__`` (rather than ``PdnViewer.__init__``, which wants a
    solved board) makes this a real widget, so it can parent the busy dialog
    and worker thread that :meth:`_ensure_cap_rows_async` creates.
    """

    def __init__(self):
        QMainWindow.__init__(self)
        self.tabs = QTabWidget()
        self._rails = ["+3V3", "GND"]
        self._rail_to_members = RAILS
        self._project = ProjectFile()
        self._caploop_settings_obj = None
        self._caps_rows_cache = None
        self._caps_table_populated = True
        self._caps_tab_index = -1
        self._display_dirty = False
        self._caps_identity_cache = None
        self._caps_shapes_cache = None
        self._loaded_project = types.SimpleNamespace(
            extracted=_standard_cap_project())
        self.metadata = {"directives": _directives()}
        self._rendered = 0

    def _render(self):
        self._rendered += 1


@pytest.fixture
def viewer(qapp):
    v = _Viewer()
    v._caps_tab_index = v.tabs.addTab(v._build_capacitors_tab(), "Capacitors")
    v._populate_caps_table()
    return v


def _cell(v, col):
    """Cell of row 0 by column *label* — indices shift whenever a column is
    inserted, and a stale index silently asserts against the wrong column."""
    if isinstance(col, str):
        col = av._CAPS_COL[col]
    return v.caps_table.item(0, col)


def test_action_and_numeric_cells_keep_their_display_text(viewer):
    """The EditRole/DisplayRole aliasing regression."""
    assert _cell(viewer, viewer._CAPS_ACTION_COL).text() == "Go ▶"
    # L1 is formatted to 4 significant figures, not a raw float repr.
    l1 = _cell(viewer, "L1 (nH)").text()
    assert l1 not in ("", "—")
    assert len(l1) <= 6 and float(l1) > 0.0


def test_numeric_cells_sort_by_value_not_by_string(viewer):
    """Sort keys live on Qt.UserRole, read by _MessagesSortItem.__lt__."""
    l1_item = _cell(viewer, "L1 (nH)")
    assert isinstance(l1_item, av._MessagesSortItem)
    key = l1_item.data(Qt.UserRole)
    assert isinstance(key, float)
    assert key == pytest.approx(float(l1_item.text()), rel=1e-3)


def test_row_identity_survives_alongside_the_sort_key(viewer):
    action = _cell(viewer, viewer._CAPS_ACTION_COL)
    assert action.data(av._CAPS_TABLE_ROW_ROLE) == 0
    assert action.data(Qt.UserRole) == 0.0


def test_row_shows_the_resolved_target_and_design_voltage(viewer):
    assert _cell(viewer, "Designator").text() == "C1"
    assert _cell(viewer, "Rail").text() == "+3V3"
    assert _cell(viewer, "Design V").text() == "3.3"       # design V from the SOURCE
    assert _cell(viewer, "Target").text() == "U5"        # largest-current SINK
    assert _cell(viewer, "Vias").text() == "2+2"       # escape vias per side
    assert "↔" in _cell(viewer, "Cavity").text()        # cavity pair
    assert _cell(viewer, "L2 (nH)").text() == "—"        # L2 unsolved
    assert _cell(viewer, "L3 (nH)").text() == "—"        # L3 unsolved


# --- part parasitics (impedance model) ----------------------------------------


def test_package_column_and_library_defaults(viewer):
    assert _cell(viewer, "Pkg").text() == "0402"     # from the 0402 footprint
    lib = viewer._caploop_package_library()
    assert _cell(viewer, "ESL (nH)").text() == \
        f"{lib.get('0402').esl_nh:.4g}"
    assert _cell(viewer, "ESR (mΩ)").text() == \
        f"{lib.get('0402').esr_mohm:.4g}"
    tip = _cell(viewer, "ESL (nH)").toolTip()
    assert "Typical" in tip and "0402" in tip


def test_unrecognised_package_shows_no_defaults(qapp):
    import dataclasses
    from tests.test_caploop_identify import _comp, _standard_cap_project

    v = _Viewer()
    v._loaded_project = types.SimpleNamespace(
        extracted=_standard_cap_project(
            pcb_components=(dataclasses.replace(
                _comp("C1"), footprint="FP-TCJD-MFG"),)))
    v._caps_tab_index = v.tabs.addTab(v._build_capacitors_tab(), "Capacitors")
    v._populate_caps_table()
    assert _cell(v, "Pkg").text() == "—"
    assert _cell(v, "ESL (nH)").text() == "—"
    assert _cell(v, "ESR (mΩ)").text() == "—"
    assert "not a recognised SMD chip package" in _cell(v, "Pkg").toolTip()


def test_per_part_parasitic_override_persists_and_shows(viewer):
    viewer._set_cap_override("C1", esl_h=0.2e-9, esr_ohm=3e-3)
    esls, esrs = viewer._project.cap_parasitic_overrides()
    assert esls == {"C1": 0.2e-9} and esrs == {"C1": 3e-3}
    assert _cell(viewer, "ESL (nH)").text() == "0.2"
    assert _cell(viewer, "ESR (mΩ)").text() == "3"
    assert "Per-part override" in _cell(viewer, "ESL (nH)").toolTip()


def test_clearing_a_parasitic_override_falls_back_to_the_package(viewer):
    viewer._set_cap_override("C1", esl_h=0.2e-9)
    assert _cell(viewer, "ESL (nH)").text() == "0.2"
    viewer._set_cap_override("C1", esl_h=None)
    lib = viewer._caploop_package_library()
    assert _cell(viewer, "ESL (nH)").text() == f"{lib.get('0402').esl_nh:.4g}"
    assert viewer._project.cap_overrides == []


def test_parasitic_override_survives_an_include_toggle(viewer):
    """The two overrides live on one CapOverride record; setting one must not
    wipe the other."""
    viewer._set_cap_override("C1", esr_ohm=3e-3)
    _cell(viewer, viewer._CAPS_USE_COL).setCheckState(Qt.Unchecked)
    only = viewer._project.cap_overrides[0]
    assert only.esr_ohm == 3e-3 and only.include is False


def test_tier1_tooltip_breaks_the_inductance_down(viewer):
    tip = _cell(viewer, "L1 (nH)").toolTip()
    for term in ("escape (rail)", "escape (return)", "via pair",
                 "spreading (closed form)"):
        assert term in tip


def test_unchecking_use_persists_an_override_and_repopulates(viewer):
    _cell(viewer, viewer._CAPS_USE_COL).setCheckState(Qt.Unchecked)
    includes, _ = viewer._project.cap_override_maps()
    assert includes == {"C1": False}
    assert "0 included" in viewer.caps_summary_label.text()
    assert _cell(viewer, viewer._CAPS_USE_COL).checkState() == Qt.Unchecked


def test_rechecking_an_autodetected_cap_clears_the_override(viewer):
    use = _cell(viewer, viewer._CAPS_USE_COL)
    use.setCheckState(Qt.Unchecked)
    _cell(viewer, viewer._CAPS_USE_COL).setCheckState(Qt.Checked)
    # Back to the auto-detected verdict: no override is stored at all, so the
    # .fypa doesn't accumulate no-op entries.
    assert viewer._project.cap_overrides == []
    assert "1 included" in viewer.caps_summary_label.text()


def test_rail_filter_hides_other_rails(viewer):
    viewer.caps_rail_combo.setCurrentText("GND")
    assert viewer.caps_table.isRowHidden(0)
    viewer.caps_rail_combo.setCurrentText("+3V3")
    assert not viewer.caps_table.isRowHidden(0)


def test_flagged_only_filter(viewer):
    viewer.caps_flagged_only_box.setChecked(True)
    assert viewer.caps_table.isRowHidden(0)     # this cap has no flags
    viewer.caps_flagged_only_box.setChecked(False)
    assert not viewer.caps_table.isRowHidden(0)


def test_included_only_filter(viewer):
    _cell(viewer, viewer._CAPS_USE_COL).setCheckState(Qt.Unchecked)
    viewer.caps_included_only_box.setChecked(True)
    assert viewer.caps_table.isRowHidden(0)


def test_tier23_results_merge_into_the_table_and_rollup(viewer):
    """The worker's finished_ok payload lands in the L2/L3 columns, and the
    per-rail parallel rollup appears in the summary line."""
    cap = viewer._caps_rows_cache[0]["cap"]
    t2 = types.SimpleNamespace(spread_h=0.34e-9, reason="")
    t3 = Tier3Result(total_h=1.2e-9, escape_h=0.1e-9, via_loop_cap_h=0.2e-9,
                     spread_h=0.34e-9, via_loop_ic_h=0.56e-9, ic_pairs=2)
    viewer._on_cap_tier23_done({cap.designator: t2}, [],
                               {cap.designator: t3})

    assert _cell(viewer, "L2 (nH)").text() == "0.34"
    assert _cell(viewer, "L3 (nH)").text() == "1.2"
    assert "cavity spreading (FEM)" in _cell(viewer, "L3 (nH)").toolTip()
    assert "1 capacitor(s) solved" in viewer.caps_progress_label.text()
    # The headline number must show under the default "All rails" filter, not
    # only once the user narrows to a rail.
    assert viewer.caps_rail_combo.currentText() == "All rails"
    assert "+3V3: 1 cap(s) in parallel = 1.2 nH" in \
        viewer.caps_summary_label.text()
    viewer.caps_rail_combo.setCurrentText("+3V3")
    assert "in parallel = 1.2 nH" in viewer.caps_summary_label.text()


def test_partial_tier3_is_marked_as_a_lower_bound(viewer):
    cap = viewer._caps_rows_cache[0]["cap"]
    t2 = types.SimpleNamespace(spread_h=0.34e-9, reason="")
    t3 = Tier3Result(total_h=0.64e-9, escape_h=0.1e-9, via_loop_cap_h=0.2e-9,
                     spread_h=0.34e-9, via_loop_ic_h=0.0, ic_pairs=0,
                     is_partial=True, reason="target via geometry unknown")
    viewer._on_cap_tier23_done({cap.designator: t2}, [],
                               {cap.designator: t3})
    assert _cell(viewer, "L3 (nH)").text().startswith("≥")
    assert "lower bound" in _cell(viewer, "L3 (nH)").toolTip()


def test_skipped_tier2_explains_itself_in_the_tooltip(viewer):
    cap = viewer._caps_rows_cache[0]["cap"]
    viewer._caploop_requested = {cap.designator}
    t2 = types.SimpleNamespace(spread_h=None,
                               reason="no cavity path to target (split plane)")
    viewer._on_cap_tier23_done({cap.designator: t2}, [],
                               {cap.designator: None})
    assert _cell(viewer, "L2 (nH)").text() == "—"
    assert "split plane" in _cell(viewer, "L2 (nH)").toolTip()
    # The L3 cell has no total *because* of that, and says so.
    assert "split plane" in _cell(viewer, "L3 (nH)").toolTip()
    # The reason is named in the progress line too, not only in a tooltip.
    label = viewer.caps_progress_label.text()
    assert "1 skipped" in label and "1× no cavity path to target" in label


def test_an_excluded_cap_is_not_counted_as_skipped(viewer):
    """The worker is only given the included capacitors. Reporting an excluded
    one as "skipped" sends the user hunting for a reason in a tooltip that,
    before the fix, still read "press Compute Tier 2/3"."""
    _cell(viewer, viewer._CAPS_USE_COL).setCheckState(Qt.Unchecked)
    viewer._caploop_requested = set()          # nothing was submitted
    viewer._on_cap_tier23_done({}, [], {})

    assert "0 capacitor(s) solved" in viewer.caps_progress_label.text()
    assert "skipped" not in viewer.caps_progress_label.text()
    assert "Excluded from the analysis" in _cell(viewer, "L2 (nH)").toolTip()
    assert "Compute Tier 2/3" not in _cell(viewer, "L2 (nH)").toolTip()


def test_every_row_gets_a_reason_after_a_solve(viewer):
    """The progress line tells the user to hover the L2 cell, so no row may be
    left with the pre-solve hint."""
    cap = viewer._caps_rows_cache[0]["cap"]
    viewer._caploop_requested = {cap.designator}
    viewer._on_cap_tier23_done({}, [], {})     # worker returned nothing for it
    tip = _cell(viewer, "L2 (nH)").toolTip()
    assert "no result" in tip
    assert "1 skipped" in viewer.caps_progress_label.text()


def test_requested_set_is_cleared_when_the_rows_are_rebuilt(viewer):
    viewer._caploop_requested = {"C1"}
    viewer._invalidate_caps_cache(repopulate=False)
    assert viewer._caploop_requested is None


def test_overlay_toggle_triggers_a_render(viewer):
    before = viewer._rendered
    viewer.caps_overlay_box.setChecked(True)
    assert viewer._rendered > before
    assert viewer._caps_overlay_signature() != ()


# --- responsiveness -----------------------------------------------------------
#
# The first row build unions every (layer, net) copper shape and walks every
# component — 7.5 s on a 16-layer board. Doing it on the GUI thread made the
# tab look hung. These pin the fixes: the cold build goes to a worker, and the
# warm paths never redo it.


def _pump(qapp, predicate, timeout_s=30.0):
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    return predicate()


def test_cold_row_build_runs_off_the_gui_thread(qapp):
    v = _Viewer()
    v._caps_tab_index = v.tabs.addTab(v._build_capacitors_tab(), "Capacitors")
    done: list[bool] = []

    v._ensure_cap_rows_async(lambda: done.append(True))
    # Returned immediately, with the build in flight — the GUI stays live.
    assert v._caps_rows_pending
    assert not done

    assert _pump(qapp, lambda: bool(done)), "worker never finished"
    assert not v._caps_rows_pending
    assert v._caps_rows_cache is not None and len(v._caps_rows_cache) == 1


def test_warm_rows_skip_the_worker_entirely(viewer):
    """A re-entered tab must not spawn a thread or flash a dialog."""
    called: list[int] = []
    viewer._ensure_cap_rows_async(lambda: called.append(1))
    assert called == [1]                       # ran inline
    assert not getattr(viewer, "_caps_rows_pending", False)


def test_a_request_arriving_mid_build_is_queued_not_dropped(qapp,
                                                            monkeypatch):
    """The Capacitors tab, the overlay and the Impedance tab all want the same
    rows. Whoever asks while a build is running must still get its callback —
    dropping it leaves that view blank forever."""
    v = _Viewer()
    v._caps_tab_index = v.tabs.addTab(v._build_capacitors_tab(), "Capacitors")

    builds: list[int] = []
    real = av.PdnViewer._compute_cap_report

    def _counted(self):
        builds.append(1)
        return real(self)

    monkeypatch.setattr(av.PdnViewer, "_compute_cap_report", _counted)

    first, second = [], []
    v._ensure_cap_rows_async(lambda: first.append(1))
    v._ensure_cap_rows_async(lambda: second.append(1))   # arrives mid-build

    assert _pump(qapp, lambda: bool(first) and bool(second))
    assert first == [1] and second == [1]
    assert len(builds) == 1                    # …and only one worker ran
    assert not v._caps_rows_pending
    assert v._caps_rows_waiters == []


def test_copper_shapes_are_built_once_and_shared(viewer):
    """The Tier-2/3 button used to rebuild the whole copper union on the GUI
    thread. It now reads the same cache the table filled."""
    extracted = viewer._loaded_project.extracted
    first = viewer._cap_net_layer_shapes(extracted)
    assert viewer._cap_net_layer_shapes(extracted) is first


def test_override_toggle_reuses_the_identification(viewer):
    """An exclude or a retarget changes no geometry, so identification — the
    slow half — must not run again."""
    identity = viewer._caps_identity_cache
    assert identity is not None
    viewer._set_cap_override("C1", include=False)
    assert viewer._caps_identity_cache is identity
    assert viewer._caps_rows_cache[0]["included"] is False


def test_a_settings_change_does_rebuild_the_identification(viewer):
    """Settings feed escape clustering and cavity selection, so they must
    invalidate it — the cache is keyed on them."""
    from fypa.caploop.constants import CapLoopSettings

    assert viewer._caps_identity_cache is not None
    viewer._caploop_settings_obj = CapLoopSettings(escape_via_search_mm=0.01)
    viewer._invalidate_caps_cache(repopulate=False, heavy=True)
    assert viewer._caps_identity_cache is None
    rows = viewer._get_or_compute_cap_rows()
    assert "no-escape-via" in rows[0]["flags"]
