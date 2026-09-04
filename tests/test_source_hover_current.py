"""SOURCE hover current on a rail carrying editor-placed sinks.

Regression for issue #51: after a re-solve, ``apply_editor_directives`` has
appended each editor directive to ``loaded.annotations.directives`` (and so to
``metadata["directives"]``) while the project still holds the same directive in
``editor_directives``. Summing both lists reported twice the rail's true load.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fypa.altium_viewer import PdnViewer  # noqa: E402

_EDITOR_SCHDOC = "(editor)"


class _HoverViewerStub:
    _rail_sink_load = PdnViewer._rail_sink_load
    _overridden_designators = PdnViewer._overridden_designators
    _rail_load_for_nets = PdnViewer._rail_load_for_nets
    _editor_directive_current = PdnViewer._editor_directive_current
    _source_rail_load_current = PdnViewer._source_rail_load_current
    _directive_current_for_hover = PdnViewer._directive_current_for_hover

    def __init__(self, metadata_directives, editor_directives) -> None:
        self._rail_to_members = {"+12V": ["+12V"], "GND": ["GND"]}
        self.metadata = {"directives": list(metadata_directives)}
        self._project = (
            None if editor_directives is None
            else SimpleNamespace(editor_directives=list(editor_directives))
        )


def _editor(role, **kw):
    spec = {"role": role, "p_net": "+12V", "n_net": "GND", "current": None,
            "voltage": None, "overrides_designator": None}
    spec.update(kw)
    return SimpleNamespace(**spec)


def _solved_sink(designator, amps, schdoc):
    return {
        "role": "SINK",
        "designator": designator,
        "schdoc": schdoc,
        "value": amps,
        "terminals": {
            "P": {"pins": [{"net": "+12V"}]},
            "N": {"pins": [{"net": "GND"}]},
        },
    }


_SOURCE = _editor("SOURCE", voltage=12.0)
_SINKS = [_editor("SINK", current=4.0), _editor("SINK", current=3.0)]


def test_pending_editor_sinks_sum_once():
    """Markers placed but not yet resolved: only the editor list has them."""
    v = _HoverViewerStub([], [_SOURCE] + _SINKS)
    assert v._editor_directive_current(_SOURCE) == pytest.approx(7.0)


def test_resolved_editor_sinks_are_not_double_counted():
    """Issue #51: 4 A + 3 A must read 7 A, not 14 A, after a re-solve."""
    solved = [_solved_sink("EDIT_1", 4.0, _EDITOR_SCHDOC),
              _solved_sink("EDIT_2", 3.0, _EDITOR_SCHDOC)]
    v = _HoverViewerStub(solved, [_SOURCE] + _SINKS)
    assert v._editor_directive_current(_SOURCE) == pytest.approx(7.0)


def test_schematic_sinks_still_counted_alongside_editor_sinks():
    """Dropping the editor-origin copies must not drop real schematic ones."""
    solved = [_solved_sink("U7", 2.0, "power.SchDoc"),
              _solved_sink("EDIT_1", 4.0, _EDITOR_SCHDOC),
              _solved_sink("EDIT_2", 3.0, _EDITOR_SCHDOC)]
    v = _HoverViewerStub(solved, [_SOURCE] + _SINKS)
    assert v._editor_directive_current(_SOURCE) == pytest.approx(9.0)


def test_editor_sinks_kept_when_no_project_holds_them():
    """A solve bundle opened without a project has only the solved list."""
    solved = [_solved_sink("EDIT_1", 4.0, _EDITOR_SCHDOC),
              _solved_sink("EDIT_2", 3.0, _EDITOR_SCHDOC)]
    v = _HoverViewerStub(solved, None)
    assert v._source_rail_load_current(
        {"terminals": {"P": {"pins": [{"net": "+12V"}]}}}
    ) == pytest.approx(7.0)


def test_solved_source_marker_hover_matches_editor_marker_hover():
    """Both hover entry points report the same KCL figure."""
    solved = [_solved_sink("EDIT_1", 4.0, _EDITOR_SCHDOC),
              _solved_sink("EDIT_2", 3.0, _EDITOR_SCHDOC)]
    v = _HoverViewerStub(solved, [_SOURCE] + _SINKS)
    marker = {
        "role": "SOURCE",
        "designator": "EDIT_0",
        "schdoc": _EDITOR_SCHDOC,
        "terminals": {"P": {"pins": [{"net": "+12V"}]},
                      "N": {"pins": [{"net": "GND"}]}},
    }
    assert (v._directive_current_for_hover(marker)
            == v._editor_directive_current(_SOURCE)
            == pytest.approx(7.0))
