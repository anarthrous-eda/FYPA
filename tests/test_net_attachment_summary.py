"""Editor-mode copper selection: the "attached PDN elements" summary.

Clicking a copper net in editor mode lists the sources / sinks / series
elements that couple into its connected group, with the values the solve
uses. The list is assembled from two directive lists at once (the last
solve's metadata and the project's editor directives), so these tests pin
the de-dupe rules it shares with the rail-load sum: an editor directive
that survived a re-solve sits in BOTH lists, and a schematic directive an
editor directive overrides must give way to the editor one.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fypa.altium_viewer import PdnViewer  # noqa: E402

_EDITOR_SCHDOC = "(editor)"


class _SummaryViewerStub:
    _ATTACH_ROLE_ORDER = PdnViewer._ATTACH_ROLE_ORDER
    _ATTACH_TERMINAL_ORDER = PdnViewer._ATTACH_TERMINAL_ORDER
    _ATTACH_MAX_ROWS = PdnViewer._ATTACH_MAX_ROWS
    _ATTACH_ROLE_WORDS = PdnViewer._ATTACH_ROLE_WORDS
    _ROLE_MARKER_STYLE = PdnViewer._ROLE_MARKER_STYLE
    _LEGEND_GLYPHS = PdnViewer._LEGEND_GLYPHS

    _connected_nets = PdnViewer._connected_nets
    # Rebind as a staticmethod: the class attribute above hands back the
    # plain function, which would otherwise pick up the stub's ``self``.
    _terminal_nets = staticmethod(PdnViewer._terminal_nets)
    _overridden_designators = PdnViewer._overridden_designators
    _rail_sink_load = PdnViewer._rail_sink_load
    _component_record = PdnViewer._component_record
    _net_attachment_rows = PdnViewer._net_attachment_rows
    _net_summary_html = PdnViewer._net_summary_html

    def __init__(self, metadata_directives=(), editor_directives=None,
                 components=()) -> None:
        self._rail_to_members = {"+12V": ["+12V"], "GND": ["GND"]}
        self.metadata = {"directives": list(metadata_directives),
                         "components": list(components)}
        self._project = (
            None if editor_directives is None
            else SimpleNamespace(editor_directives=list(editor_directives))
        )


def _editor(role, **kw):
    spec = {"id": "abcdef012345", "kind": "component", "designator": None,
            "role": role, "p_net": "+12V", "n_net": "GND", "single_net": False,
            "voltage": None, "current": None, "resistance": None,
            "min_voltage": None, "overrides_designator": None}
    spec.update(kw)
    return SimpleNamespace(**spec)


def _solved(role, designator, value, unit, schdoc="power.SchDoc",
            p_net="+12V", n_net="GND", **kw):
    d = {
        "role": role,
        "designator": designator,
        "label": designator,
        "schdoc": schdoc,
        "value": value,
        "unit": unit,
        "terminals": {
            "P": {"requested_net": p_net, "pins": [{"net": p_net}]},
            "N": {"requested_net": n_net, "pins": [{"net": n_net}]},
        },
    }
    d.update(kw)
    return d


def _labels(rows):
    return [(r["role"], r["label"]) for r in rows]


def test_lists_solved_sources_and_sinks_on_the_clicked_net():
    v = _SummaryViewerStub([
        _solved("SOURCE", "J1", 12.0, "V"),
        _solved("SINK", "U7", 0.25, "A"),
    ])
    rows = v._net_attachment_rows("+12V")
    assert _labels(rows) == [("SOURCE", "J1"), ("SINK", "U7")]
    assert rows[0]["value"] == "12 V"
    assert rows[1]["value"] == "250 mA"
    assert rows[0]["terminals"] == ["P"]


def test_elements_on_another_rail_are_not_listed():
    v = _SummaryViewerStub([
        _solved("SINK", "U7", 0.25, "A"),
        _solved("SINK", "U9", 0.5, "A", p_net="+3V3", n_net="GND"),
    ])
    assert _labels(v._net_attachment_rows("+12V")) == [("SINK", "U7")]


def test_return_terminal_is_listed_on_the_ground_net():
    """Clicking GND finds the same sink through its N terminal."""
    v = _SummaryViewerStub([_solved("SINK", "U7", 0.25, "A")])
    rows = v._net_attachment_rows("GND")
    assert rows[0]["terminals"] == ["N"]


def test_single_net_editor_sink_does_not_appear_on_ground():
    """Its return is an ideal 0 V node, not copper — GND owns nothing."""
    ed = _editor("SINK", designator="U7", current=0.25, single_net=True)
    v = _SummaryViewerStub([], [ed])
    assert v._net_attachment_rows("+12V")
    assert v._net_attachment_rows("GND") == []


def test_resolved_editor_directive_is_listed_once():
    """It sits in both lists after a re-solve; the editor list owns it."""
    ed = _editor("SINK", designator=None, kind="free", current=4.0,
                 single_net=True)
    solved = [_solved("SINK", "EDIT_abcdef012345", 4.0, "A",
                      schdoc=_EDITOR_SCHDOC)]
    rows = _SummaryViewerStub(solved, [ed])._net_attachment_rows("+12V")
    assert len(rows) == 1
    assert rows[0]["role"] == "SINK"
    assert rows[0]["pending"] is False


def test_pending_editor_directive_is_flagged():
    ed = _editor("SINK", designator="U7", current=4.0, single_net=True)
    rows = _SummaryViewerStub([], [ed])._net_attachment_rows("+12V")
    assert rows[0]["pending"] is True


def test_editor_directives_survive_without_a_project():
    """A solve bundle opened on its own still lists its editor markers."""
    solved = [_solved("SINK", "EDIT_abcdef012345", 4.0, "A",
                      schdoc=_EDITOR_SCHDOC)]
    rows = _SummaryViewerStub(solved, None)._net_attachment_rows("+12V")
    assert len(rows) == 1


def test_overridden_schematic_directive_gives_way_to_the_editor_one():
    ed = _editor("SINK", designator="U7", current=4.0, single_net=True,
                 overrides_designator="U7")
    solved = [_solved("SINK", "U7", 0.25, "A")]
    rows = _SummaryViewerStub(solved, [ed])._net_attachment_rows("+12V")
    assert len(rows) == 1
    assert rows[0]["value"] == "4 A"


def test_schematic_series_is_shown_under_the_editor_role_name():
    v = _SummaryViewerStub([
        _solved("RESISTOR", "FB1", 0.01, "Ohm", p_net="+12V", n_net="+12V_F"),
    ])
    rows = v._net_attachment_rows("+12V")
    assert rows[0]["role"] == "SERIES"
    assert "10" in rows[0]["value"]


def test_series_bridged_rail_lists_both_sides():
    """_connected_nets spans an editor SERIES, so the summary does too."""
    bridge = _editor("SERIES", designator="FB1", resistance=0.01,
                     p_net="+12V", n_net="+12V_F")
    solved = [_solved("SINK", "U7", 0.25, "A", p_net="+12V_F")]
    rows = _SummaryViewerStub(solved, [bridge])._net_attachment_rows("+12V")
    assert ("SINK", "U7") in _labels(rows)


def test_sinks_sort_after_sources():
    v = _SummaryViewerStub([
        _solved("SINK", "U7", 0.25, "A"),
        _solved("SOURCE", "J1", 12.0, "V"),
        _solved("REGULATOR", "U2", 3.3, "V", p_net="+12V"),
    ])
    assert [r["role"] for r in v._net_attachment_rows("+12V")] == [
        "SOURCE", "REGULATOR", "SINK"]


def test_summary_html_reports_values_and_total_load():
    v = _SummaryViewerStub([
        _solved("SOURCE", "J1", 12.0, "V"),
        _solved("SINK", "U7", 0.25, "A", min_voltage=11.4),
        _solved("SINK", "U8", 0.75, "A"),
    ])
    html = v._net_summary_html("+12V")
    assert "1 source" in html and "2 sinks" in html
    assert "12 V" in html and "250 mA" in html
    assert "11.4" in html               # PDN_MIN_V on the sink row
    assert "Total sink load" in html and "1 A" in html


def test_summary_html_links_only_real_components():
    v = _SummaryViewerStub(
        [_solved("SINK", "U7", 0.25, "A"),
         _solved("SINK", "U8", 0.25, "A")],
        components=[{"designator": "U7"}],
    )
    html = v._net_summary_html("+12V")
    assert "sel:component:U7" in html
    assert "sel:component:U8" not in html


def test_summary_html_caps_long_lists():
    solved = [_solved("SINK", f"U{i}", 0.1, "A") for i in range(20)]
    html = _SummaryViewerStub(solved)._net_summary_html("+12V")
    assert f"+ {20 - PdnViewer._ATTACH_MAX_ROWS} more not shown" in html


def test_summary_html_without_attachments():
    html = _SummaryViewerStub([])._net_summary_html("+12V")
    assert "None" in html


def test_summary_html_skips_unnamed_copper():
    assert _SummaryViewerStub([])._net_summary_html("(none)") == ""
