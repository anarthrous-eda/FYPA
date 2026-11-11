"""A Bridges-tab change must be appliable even with nothing to solve yet.

Disabling an auto-bridge (or changing the auto-SERIES limit) changes which
nets exist, and only a rebuild of the design applies it. Resolve used to stay
greyed out whenever the design had no SOURCE + SINK rail, so the Bridges tab
said "press Resolve" while offering no way to — and a transistor whose drain
had been merged into its source could never be set up. Now:

* Resolve is enabled while such a change is pending, solveable or not;
* that click rebuilds the design without solving (load-only);
* a load-only run opens the rebuilt design even when editor directives exist
  but do not form a closed rail, instead of failing "not solveable".
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from fypa.project_file import EditorDirective, ProjectFile  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


class _Loaded:
    def __init__(self, opted_out=(), limit=0.1):
        self.no_auto_bridge_applied = frozenset(opted_out)
        self.auto_series_max_ohm_applied = limit


@pytest.fixture
def viewer(qapp):
    from PySide6.QtWidgets import QMainWindow
    import fypa.altium_viewer as V
    v = V.PdnViewer.__new__(V.PdnViewer)
    QMainWindow.__init__(v)
    v._project = ProjectFile()
    v._loaded_project = _Loaded()
    v._awaiting_first_solve = False
    v._solve_stale = True            # an edit was made
    v._adaptive_gain_check = None
    v._has_solveable_directives = lambda: False    # no SOURCE + SINK rail
    return v


def _enabled(v) -> bool:
    import fypa.altium_viewer as V
    return V.PdnViewer._resolve_button_enabled(v)


def _pending(v) -> bool:
    import fypa.altium_viewer as V
    return V.PdnViewer._design_reload_pending(v)


def test_nothing_to_solve_and_no_bridge_change_stays_disabled(viewer):
    assert not _pending(viewer)
    assert not _enabled(viewer)


def test_a_disabled_auto_bridge_enables_resolve_with_nothing_to_solve(viewer):
    viewer._project.no_auto_bridge = ["R1"]
    assert _pending(viewer)
    assert _enabled(viewer)


def test_a_changed_auto_series_limit_enables_resolve(viewer):
    viewer._project.auto_series_max_ohm = 0.0
    assert _pending(viewer)
    assert _enabled(viewer)


def test_an_applied_opt_out_is_not_pending(viewer):
    """Once the rebuilt design carries the opt-out, nothing is left to do."""
    viewer._project.no_auto_bridge = ["r1"]
    viewer._loaded_project = _Loaded(opted_out={"R1"})
    assert not _pending(viewer)
    assert not _enabled(viewer)


def test_an_up_to_date_solve_is_not_re_enabled(viewer):
    viewer._project.no_auto_bridge = ["R1"]
    viewer._solve_stale = False
    assert not _enabled(viewer)


def test_no_in_memory_design_is_never_reported_pending(viewer):
    viewer._project.no_auto_bridge = ["R1"]
    viewer._loaded_project = None
    assert not _pending(viewer)


# --- the worker: load-only opens the design whatever the directives --------

def _board():
    from fypa.altium.annotations import parse_annotations
    from fypa.altium.extract import (
        ExtractedProject, Pt2D, RawNet, RawPad, RawPcbComponent,
        RawStackupLayer,
    )
    from fypa.altium.loader import LoadedProject

    def pad(pin, net, x):
        return RawPad(center=Pt2D(x, 0), width_mm=1.0, height_mm=1.0,
                      hole_mm=0, shape=2, rotation_deg=0, layer_id=1,
                      net_index=net, designator=pin, component_index=0,
                      is_through_hole=False, is_smt=True)

    proj = ExtractedProject(
        prjpcb_path=Path("nowhere/t.PrjPcb"),
        pcbdoc_path=Path("nowhere/t.PcbDoc"),
        tracks=(), arcs=(), vias=(), regions=(), shape_based_regions=(),
        fills=(),
        stackup=(RawStackupLayer(
            layer_id=1, name="Top", copper_thickness_mm=0.035,
            dielectric_thickness_mm=0.0, next_layer_id=0, is_plane=False,
            plane_net_name=None, mech_enabled=True),),
        compiled_netlist=None,
        nets=(RawNet("+12V"), RawNet("GND")),
        sch_components=(),
        pcb_components=(RawPcbComponent(
            designator="J1", center=Pt2D(0.5, 0), rotation_deg=0.0,
            layer_name="TOP", footprint="HDR2", source_designator="J1"),),
        pads=(pad("1", 0, 0.0), pad("2", 1, 1.0)),
    )
    ann = parse_annotations(proj, enabled_layers=[1])
    return LoadedProject(extracted=proj, annotations=ann)


def _run_worker(qapp, *, load_only):
    from fypa.altium.loader import SolveSettings
    from fypa.viewer.solve_worker import _SolveWorker
    # A SOURCE with no SINK: editor directives that form no closed rail.
    source = EditorDirective(
        kind="component", role="SOURCE", designator="J1", single_net=False,
        p_net="+12V", n_net="GND", voltage=12.0)
    w = _SolveWorker(Path("nowhere/t.PrjPcb"), SolveSettings(),
                     use_design_cache=False, editor_directives=[source],
                     loaded_project=_board(), load_only=load_only)
    got = {}
    w.finished_ok.connect(lambda sol, md, ld: got.update(ok=(sol, md)))
    w.failed.connect(lambda msg: got.update(failed=msg))
    w.run()          # in-process, synchronously
    return got


def test_load_only_with_an_open_rail_opens_the_design(qapp):
    got = _run_worker(qapp, load_only=True)
    assert "failed" not in got, got.get("failed")
    sol, md = got["ok"]
    assert sol.solver_info.get("stub")
    assert "bridge_candidates" in md


def test_a_real_resolve_with_an_open_rail_still_fails_loudly(qapp):
    """Unchanged: asking to SOLVE an incomplete rail explains why not."""
    got = _run_worker(qapp, load_only=False)
    assert "failed" in got and "ok" not in got
