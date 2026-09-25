"""Editor-mode net focus — the net table's crosshair column.

The editor is worked one net at a time: pick a net, place its source and
sinks, move on. Selecting a net to see where its copper runs used to be the
only way to isolate it, and any stray click in the canvas threw that away.
Focus makes it sticky — and hides the other nets outright rather than fading
them — so these tests pin the two properties the feature lives or dies by:

* nothing that clears a *selection* may clear the focus, and
* focused-out copper is gone from the geometry, not dimmed to 10%.

The panel and its table are the real widgets, driven through a stubbed
:class:`~fypa.altium_viewer.PdnViewer` (its ``__init__`` opens a whole
board), so the crosshair column's click routing and its no-select behaviour
are exercised rather than described.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")


@pytest.fixture(scope="module")
def qapp():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


# Two named nets and one synthetic (Gerber-style) region whose record-level
# net name is the "(none)" sentinel — the case net names alone cannot tell
# apart, so it focuses by polygon identity instead.
V3_POLYS = {(1, 101), (2, 102)}
GND_POLYS = {(1, 201)}
NONAME_POLYS = {(1, 301)}


def _rows():
    return [
        {"name": "GND", "area": 900.0, "poly_keys": set(GND_POLYS),
         "real": True, "renameable": False},
        {"name": "+3V3", "area": 120.0, "poly_keys": set(V3_POLYS),
         "real": True, "renameable": False},
        {"name": "$NET_001", "area": 30.0, "poly_keys": set(NONAME_POLYS),
         "real": False, "renameable": True},
    ]


@pytest.fixture
def viewer(qapp):
    from PySide6.QtWidgets import QMainWindow
    import fypa.altium_viewer as V

    v = V.PdnViewer.__new__(V.PdnViewer)
    QMainWindow.__init__(v)
    v.metadata = {}
    v._project = None
    v._editor_mode = True
    v._editor_selection = None
    v._editor_multi = []
    v._editor_pending_marker = None
    v._marker_drag = None
    v._copper_selection = None
    v._editor_highlight_nets = set()
    v._editor_highlight_polys = set()
    v._editor_focus_net = None
    v._editor_focus_nets = set()
    v._editor_focus_polys = set()
    v._net_table_rows = []
    v._net_table_populating = False
    v._selected_layer = None
    v._layer_transparency_buttons = []

    v.renders = 0

    def _render():
        v.renders += 1

    v._render = _render
    v._connected_nets = lambda net: ({net} if net else set())
    v._compute_net_table_rows = _rows
    v._design_has_named_copper = lambda: True
    v._populate_editor_form = lambda: None
    v._net_summary_html = lambda net: ""
    v._is_copper_name_selection = lambda sel: False
    v._on_editor_panel_resized = lambda *a, **k: None
    v._editor_add_source_btn = None
    v._editor_add_sink_btn = None

    class _GL:
        def set_primitive_selection_outline(self, rings):
            pass

    v._gl_viewer = _GL()
    v._editor_panel_width = V.PdnViewer._EDITOR_PANEL_DEFAULT_W
    # Hold a reference: the panel is parentless here, so dropping it would
    # have Python collect it and its child widgets out from under us.
    v._test_panel = V.PdnViewer._build_editor_panel(v)
    v._editor_panel = v._test_panel
    v._refresh_net_table()
    # The panel's idle state: editor mode, nothing selected, so the net table
    # is up. Everything about layout stability is measured from here.
    v._update_editor_panel()
    v._test_panel.resize(V.PdnViewer._EDITOR_PANEL_DEFAULT_W, 700)
    v._test_panel.show()
    return v


def _row(v, name: str) -> dict:
    return next(r for r in v._net_table_rows if r["name"] == name)


def _table_row_index(v, name: str) -> int:
    for i in range(v._net_table.rowCount()):
        item = v._net_table.item(i, v._NET_COL_NAME)
        if item is not None and item.text() == name:
            return i
    raise AssertionError(f"{name} not in the net table")


def _click_crosshair(v, name: str) -> None:
    """What the user's click on the crosshair cell reaches."""
    v._on_net_table_cell_clicked(_table_row_index(v, name),
                                 v._NET_COL_FOCUS)


# --- the crosshair is its own release ---------------------------------------

def test_crosshair_click_focuses_the_net(viewer):
    _click_crosshair(viewer, "+3V3")
    assert viewer._editor_focus_net == "+3V3"
    assert viewer._editor_focus_active() is True


def test_a_second_crosshair_click_releases_the_focus(viewer):
    _click_crosshair(viewer, "+3V3")
    _click_crosshair(viewer, "+3V3")
    assert viewer._editor_focus_net is None
    assert viewer._editor_focus_active() is False


def test_focusing_another_net_moves_the_focus(viewer):
    _click_crosshair(viewer, "+3V3")
    _click_crosshair(viewer, "GND")
    assert viewer._editor_focus_net == "GND"
    assert viewer._editor_focus_nets == {"GND"}


def test_a_click_on_the_name_column_does_not_focus(viewer):
    """Only the crosshair column focuses; clicking the name still just
    highlights, as it always did."""
    viewer._on_net_table_cell_clicked(
        _table_row_index(viewer, "+3V3"), viewer._NET_COL_NAME)
    assert viewer._editor_focus_net is None


def test_the_crosshair_cell_is_not_selectable(viewer):
    """The table hides the moment anything is selected, so a click that
    selected the row would take the crosshair off screen with it."""
    from PySide6.QtCore import Qt
    cell = viewer._net_table.item(
        _table_row_index(viewer, "+3V3"), viewer._NET_COL_FOCUS)
    assert bool(cell.flags() & Qt.ItemIsEnabled)
    assert not bool(cell.flags() & Qt.ItemIsSelectable)


# --- stickiness: the whole point -------------------------------------------

def test_clearing_the_selection_highlight_keeps_the_focus(viewer):
    """A stray click on bare substrate clears the selection highlight. That
    is exactly the accident focus exists to survive."""
    _click_crosshair(viewer, "+3V3")
    viewer._apply_editor_highlight({"GND"})
    viewer._clear_editor_highlight()
    assert viewer._editor_focus_net == "+3V3"
    assert viewer._editor_focus_active() is True


def test_selecting_a_different_net_keeps_the_focus(viewer):
    _click_crosshair(viewer, "+3V3")
    viewer._select_copper("GND")
    assert viewer._editor_focus_net == "+3V3"


def test_leaving_editor_mode_releases_the_focus(viewer):
    """There is no focus control outside editor mode, so leaving must not
    strand the user with most of the board missing."""
    _click_crosshair(viewer, "+3V3")
    viewer._editor_mode = False
    viewer._clear_editor_focus(render=False)
    assert viewer._editor_focus_net is None


def test_focus_is_inert_outside_editor_mode(viewer):
    """Even with the sets populated, viewer mode must draw every net."""
    _click_crosshair(viewer, "+3V3")
    viewer._editor_mode = False
    assert viewer._editor_focus_active() is False
    assert viewer._editor_focus_hides("GND", 1, None) is False


# --- hidden, not dimmed ----------------------------------------------------

def test_focus_hides_every_other_net(viewer):
    _click_crosshair(viewer, "+3V3")
    assert viewer._editor_focus_hides("GND") is True
    assert viewer._editor_focus_hides("+3V3") is False


def test_no_focus_hides_nothing(viewer):
    assert viewer._editor_focus_hides("GND") is False
    assert viewer._editor_focus_hides(None) is False


def test_dim_and_hide_are_separate(viewer):
    """The old behaviour was a blend toward the background; focus must take
    the copper out of the batch instead. Both may be live at once (the user
    can select one net while focused on another), so the dim blend staying
    unchanged is part of the contract."""
    _click_crosshair(viewer, "+3V3")
    viewer._apply_editor_highlight({"+3V3"})
    dimmed = viewer._editor_dim_rgb((1.0, 1.0, 1.0), "GND")
    assert dimmed != (1.0, 1.0, 1.0)          # still a blend, not a skip
    assert viewer._editor_focus_hides("GND") is True


def test_unnamed_copper_focuses_by_polygon_identity(viewer):
    """A synthetic region's record-level net is the "(none)" sentinel every
    other unnamed piece shares, so the name can prove nothing."""
    _click_crosshair(viewer, "$NET_001")
    assert viewer._editor_focus_nets == set()
    lid, pid = next(iter(NONAME_POLYS))

    class _Poly:
        pass

    keep = _Poly()
    drop = _Poly()
    viewer._editor_focus_polys = {(lid, id(keep))}
    assert viewer._editor_focus_hides("(none)", lid, keep) is False
    assert viewer._editor_focus_hides("(none)", lid, drop) is True


# --- the heatmap mesh goes to zero, not to 10% -----------------------------

def _probes():
    return [
        {"physical": "Top Layer", "net": "+3V3", "n_vertices": 3},
        {"physical": "Top Layer", "net": "GND", "n_vertices": 2},
    ]


def test_mesh_alpha_is_zero_outside_the_focused_net(viewer):
    _click_crosshair(viewer, "+3V3")
    arr = viewer._combined_mesh_alpha_array(_probes(), 5)
    assert arr is not None
    # layer_probes is top-first, the mesh is built bottom-first, so the
    # array comes back reversed: GND's two vertices then +3V3's three.
    assert list(arr) == [0.0, 0.0, 1.0, 1.0, 1.0]


def test_mesh_alpha_without_focus_still_dims_to_ten_percent(viewer):
    viewer._apply_editor_highlight({"+3V3"})
    arr = viewer._combined_mesh_alpha_array(_probes(), 5)
    assert arr is not None
    assert list(arr) == pytest.approx([0.1, 0.1, 1.0, 1.0, 1.0])


def test_focus_wins_over_a_conflicting_highlight(viewer):
    """Focused on +3V3 while GND is selected: GND must still be gone, not
    lit. The selection is transient, the focus is what the user asked for."""
    _click_crosshair(viewer, "+3V3")
    viewer._apply_editor_highlight({"GND"})
    arr = viewer._combined_mesh_alpha_array(_probes(), 5)
    assert list(arr) == [0.0, 0.0, 1.0, 1.0, 1.0]


def test_focus_on_synthetic_copper_blanks_the_solved_mesh(viewer):
    """No solved rail is the copper the user asked to see, so none of the
    mesh may stay on screen."""
    _click_crosshair(viewer, "$NET_001")
    arr = viewer._combined_mesh_alpha_array(_probes(), 5)
    assert list(arr) == [0.0, 0.0, 0.0, 0.0, 0.0]


# --- clicks can't land on copper the focus hid -----------------------------

def test_a_pick_on_hidden_copper_reads_as_bare_substrate(viewer):
    _click_crosshair(viewer, "+3V3")
    assert viewer._focus_blocks_pick("GND") is True
    assert viewer._focus_blocks_pick("+3V3") is False


def test_picks_are_unaffected_without_a_focus(viewer):
    assert viewer._focus_blocks_pick("GND") is False


def test_an_unnamed_pick_with_no_point_is_let_through(viewer):
    """Better to select something than to swallow the click silently."""
    _click_crosshair(viewer, "$NET_001")
    assert viewer._focus_blocks_pick("(none)") is False


# --- releasing the focus: the layout must not move --------------------------

def _table_top(v) -> int:
    v._test_panel.layout().activate()
    return v._net_table.y()


def test_focusing_does_not_move_the_net_table(viewer):
    """The whole bug: a banner above the table pushed it down, so the second
    click meant to undo a mis-focus landed on a different net's crosshair."""
    before = _table_top(viewer)
    _click_crosshair(viewer, "+3V3")
    assert _table_top(viewer) == before


def test_the_row_under_a_given_position_is_unchanged_by_focusing(viewer):
    """Same invariant from the user's end: the crosshair they just clicked is
    still the crosshair under the cursor."""
    row_i = _table_row_index(viewer, "+3V3")
    rect = viewer._net_table.visualItemRect(
        viewer._net_table.item(row_i, viewer._NET_COL_FOCUS))
    point = viewer._net_table.viewport().mapTo(
        viewer._test_panel, rect.center())
    _click_crosshair(viewer, "+3V3")
    viewer._test_panel.layout().activate()
    again = viewer._net_table.viewport().mapTo(
        viewer._test_panel,
        viewer._net_table.visualItemRect(
            viewer._net_table.item(row_i, viewer._NET_COL_FOCUS)).center())
    assert again == point


def test_the_header_carries_the_release_link_while_the_table_is_up(viewer):
    _click_crosshair(viewer, "+3V3")
    text = viewer._net_table_label.text()
    assert "#release" in text
    assert "+3V3" in text


def test_the_header_link_releases_the_focus(viewer):
    _click_crosshair(viewer, "+3V3")
    viewer._on_editor_focus_chip_link("#release")
    assert viewer._editor_focus_net is None
    assert "#release" not in viewer._net_table_label.text()


def test_the_header_still_reports_the_row_count(viewer):
    _click_crosshair(viewer, "+3V3")
    assert "(3)" in viewer._net_table_label.text()
    viewer._net_table_filter.setText("3v3")
    assert "1 of 3" in viewer._net_table_label.text()


def test_a_long_net_name_is_elided_so_the_link_survives(viewer):
    """The label does not wrap and the panel is a fixed width, so an
    un-elided name would push the release link off the edge."""
    long_name = "VERY_LONG_POWER_NET_NAME_THAT_RUNS_ON"
    viewer._compute_net_table_rows = lambda: [
        {"name": long_name, "area": 10.0, "poly_keys": set(),
         "real": True, "renameable": False}]
    viewer._refresh_net_table()
    _click_crosshair(viewer, long_name)
    text = viewer._net_table_label.text()
    assert "#release" in text
    assert long_name not in text
    assert "\u2026" in text


# --- the chip covers the one state the header cannot ------------------------

def test_the_chip_stays_down_while_the_table_is_up(viewer):
    """Two release controls at once would be noise — and the chip is the one
    that moves the table."""
    _click_crosshair(viewer, "+3V3")
    assert viewer._net_table.isHidden() is False
    assert viewer._editor_focus_chip.isHidden() is True


def test_the_chip_takes_over_when_a_selection_hides_the_table(viewer):
    """A component selection hides the table — and with it the header link —
    so the chip is the only release left. It has to be showing."""
    _click_crosshair(viewer, "+3V3")
    viewer._editor_selection = {"kind": "component", "designator": "U1",
                                "nets": ["+3V3"], "bbox": None,
                                "unlocked": False}
    viewer._update_editor_panel()
    assert viewer._net_table.isHidden() is True
    assert viewer._editor_focus_chip.isHidden() is False
    assert "+3V3" in viewer._editor_focus_chip.text()


def test_a_marquee_selection_also_gets_the_chip(viewer):
    _click_crosshair(viewer, "+3V3")
    viewer._editor_multi = [{"kind": "component", "id": "U1", "label": "U1"}]
    viewer._update_editor_panel()
    assert viewer._editor_focus_chip.isHidden() is False


def test_clearing_the_selection_hands_the_link_back_to_the_header(viewer):
    _click_crosshair(viewer, "+3V3")
    viewer._editor_selection = {"kind": "component", "designator": "U1",
                                "nets": ["+3V3"], "bbox": None,
                                "unlocked": False}
    viewer._update_editor_panel()
    viewer._editor_selection = None
    viewer._update_editor_panel()
    assert viewer._editor_focus_chip.isHidden() is True
    assert "#release" in viewer._net_table_label.text()


def test_no_focus_means_no_release_control_anywhere(viewer):
    assert "#release" not in viewer._net_table_label.text()
    assert viewer._editor_focus_chip.isHidden() is True


# --- the crosshair column tracks the state --------------------------------

def test_the_focused_row_carries_the_lit_icon_sort_key(viewer):
    from PySide6.QtCore import Qt
    _click_crosshair(viewer, "+3V3")
    lit = viewer._net_table.item(
        _table_row_index(viewer, "+3V3"), viewer._NET_COL_FOCUS)
    dark = viewer._net_table.item(
        _table_row_index(viewer, "GND"), viewer._NET_COL_FOCUS)
    assert lit.data(Qt.UserRole) == 0
    assert dark.data(Qt.UserRole) == 1


def test_the_icon_survives_a_table_rebuild(viewer):
    from PySide6.QtCore import Qt
    _click_crosshair(viewer, "+3V3")
    viewer._refresh_net_table()
    lit = viewer._net_table.item(
        _table_row_index(viewer, "+3V3"), viewer._NET_COL_FOCUS)
    assert lit.data(Qt.UserRole) == 0


def test_the_area_column_still_sorts_by_area_not_text(viewer):
    """Regression guard for the column shift: the area cell's numeric sort
    key has to have moved with it."""
    from PySide6.QtCore import Qt
    item = viewer._net_table.item(
        _table_row_index(viewer, "GND"), viewer._NET_COL_AREA)
    assert item.data(Qt.UserRole) == pytest.approx(900.0)


def test_the_filter_still_reads_the_name_column(viewer):
    viewer._net_table_filter.setText("3v3")
    shown = [i for i in range(viewer._net_table.rowCount())
             if not viewer._net_table.isRowHidden(i)]
    assert len(shown) == 1
    assert viewer._net_table.item(
        shown[0], viewer._NET_COL_NAME).text() == "+3V3"


# --- F, the keyboard twin --------------------------------------------------

def test_f_focuses_the_selected_net(viewer):
    viewer._editor_selection = {"kind": "copper", "net": "+3V3"}
    viewer._hotkey_toggle_net_focus()
    assert viewer._editor_focus_net == "+3V3"


def test_f_releases_an_active_focus(viewer):
    _click_crosshair(viewer, "+3V3")
    viewer._hotkey_toggle_net_focus()
    assert viewer._editor_focus_net is None


def test_f_declines_a_component_that_bridges_two_nets(viewer):
    """A part with a pin on each of two rails names no single net to show."""
    viewer._editor_selection = {"kind": "component", "designator": "U1",
                                "nets": ["+3V3", "GND"], "bbox": None,
                                "unlocked": False}
    viewer._hotkey_toggle_net_focus()
    assert viewer._editor_focus_net is None


def test_f_focuses_a_single_net_component(viewer):
    viewer._editor_selection = {"kind": "component", "designator": "C1",
                                "nets": ["GND", "GND"], "bbox": None,
                                "unlocked": False}
    viewer._hotkey_toggle_net_focus()
    assert viewer._editor_focus_net == "GND"


def test_f_is_inert_outside_editor_mode(viewer):
    viewer._editor_mode = False
    viewer._editor_selection = {"kind": "copper", "net": "+3V3"}
    viewer._hotkey_toggle_net_focus()
    assert viewer._editor_focus_net is None


# --- focus is geometry, so the overlay cache must notice -------------------

def test_a_focus_change_invalidates_the_overlay_signature(viewer):
    """Focus removes polygons from the batch. If the signature missed it the
    viewport would keep the stale geometry and nothing would appear to
    happen."""
    viewer._overlay_state = {}
    viewer._overlay_colors = {}
    viewer._overlay_bottom_colors = {}
    viewer._layer_fill_buttons = []
    viewer._layer_eye2_buttons = []
    viewer._effective_rail_members = lambda rails: []
    viewer._caps_overlay_signature = lambda: ()
    viewer.via_span_box = None

    class _Box:
        def isChecked(self):
            return False

    viewer.view_3d_box = _Box()

    before = viewer._overlay_geom_signature([])
    _click_crosshair(viewer, "+3V3")
    assert viewer._overlay_geom_signature([]) != before


# --- a rename must not orphan the focus -----------------------------------

def test_renaming_the_focused_net_carries_the_focus_over(viewer):
    _click_crosshair(viewer, "$NET_001")
    row_i = _table_row_index(viewer, "$NET_001")
    item = viewer._net_table.item(row_i, viewer._NET_COL_NAME)

    from fypa.project_file import CopperName, ProjectFile
    viewer._project = ProjectFile()
    viewer._project.copper_names.append(
        CopperName(anchor_xy=(0.0, 0.0), layer_id=1, name="$NET_001"))
    viewer._ensure_project = lambda: viewer._project
    viewer._mark_project_dirty = lambda: None
    viewer._all_net_names = lambda: ["GND", "+3V3", "$NET_001"]
    viewer._compute_net_table_rows = lambda: [
        dict(r, name=("VDD_CORE" if r["name"] == "$NET_001" else r["name"]))
        for r in _rows()
    ]

    item.setText("VDD_CORE")
    assert viewer._editor_focus_net == "VDD_CORE"
    assert "VDD_CORE" in viewer._net_table_label.text()


# --- markers: only the focused net's sources and sinks -----------------------

def _directives(v):
    """Give the stub a project holding one free SINK per net, plus one on
    unnamed copper."""
    from fypa.project_file import EditorDirective, ProjectFile
    proj = ProjectFile()
    for net, x in (("+3V3", 10.0), ("GND", 20.0), ("(none)", 30.0)):
        proj.upsert_directive(EditorDirective(
            id=f"d_{net}", kind="free", role="SINK", p_net=net,
            single_net=True, current=0.5, anchor_xy=(x, 10.0),
            layer="Top Layer", layer_id=1))
    v._project = proj
    # The "(none)" marker's anchor resolves to a real polygon that is in no
    # focus set, so focusing a named net drops it. (The unresolvable case —
    # where the marker stays put — has tests of its own below.)
    v._unowned_poly = object()
    v._copper_poly_under_point = lambda x, y, lid: v._unowned_poly
    return proj


def _marker_nets(v) -> set[str]:
    return {d.p_net for d in v._project.editor_directives
            if v._directive_rail_visible(d)}


@pytest.fixture
def marker_viewer(viewer):
    _directives(viewer)
    # Strip the rail half of the gate: no solve here, so every directive is
    # "not on any solved rail" and passes it anyway — this just makes that
    # explicit so a failure points at the focus half.
    viewer._effective_rail_members = lambda rails: []
    viewer._visible_rails = list
    viewer._rail_to_members = {}
    return viewer


def test_without_a_focus_every_nets_markers_show(marker_viewer):
    assert _marker_nets(marker_viewer) == {"+3V3", "GND", "(none)"}


def test_focus_hides_other_nets_markers(marker_viewer):
    _click_crosshair(marker_viewer, "+3V3")
    assert _marker_nets(marker_viewer) == {"+3V3"}


def test_focus_moves_with_the_crosshair(marker_viewer):
    _click_crosshair(marker_viewer, "GND")
    assert _marker_nets(marker_viewer) == {"GND"}


def test_releasing_the_focus_brings_the_markers_back(marker_viewer):
    _click_crosshair(marker_viewer, "+3V3")
    marker_viewer._clear_editor_focus()
    assert _marker_nets(marker_viewer) == {"+3V3", "GND", "(none)"}


def test_an_unresolved_marker_matches_on_the_copper_under_its_anchor(
        marker_viewer):
    """A marker still on "(none)" copper has no net to test, so its anchor
    polygon decides. One inside the focused region stays, one outside goes."""
    class _Poly:
        pass

    inside, outside = _Poly(), _Poly()
    marker_viewer._editor_focus_net = "$NET_001"
    marker_viewer._editor_focus_nets = set()
    marker_viewer._editor_focus_polys = {(1, id(inside))}

    d = next(x for x in marker_viewer._project.editor_directives
             if x.p_net == "(none)")
    marker_viewer._copper_poly_under_point = lambda x, y, lid: inside
    assert marker_viewer._directive_focus_visible(d) is True
    marker_viewer._copper_poly_under_point = lambda x, y, lid: outside
    assert marker_viewer._directive_focus_visible(d) is False


def test_a_marker_whose_anchor_wont_resolve_stays_visible(marker_viewer):
    """Better one extra glyph than a marker the user just dropped
    disappearing with no explanation."""
    _click_crosshair(marker_viewer, "$NET_001")
    d = next(x for x in marker_viewer._project.editor_directives
             if x.p_net == "(none)")
    marker_viewer._copper_poly_under_point = lambda x, y, lid: None
    assert marker_viewer._directive_focus_visible(d) is True
    d.anchor_xy = None
    assert marker_viewer._directive_focus_visible(d) is True


def test_a_hidden_marker_is_not_clickable(marker_viewer):
    """The glyph is gone, so the hit-test must not still find it — otherwise
    a click on empty board would select an invisible sink."""
    _click_crosshair(marker_viewer, "+3V3")
    assert marker_viewer._free_marker_at(10.0, 10.0) is not None   # focused
    assert marker_viewer._free_marker_at(20.0, 10.0) is None       # GND


# --- markers show when the rail OR its copper is on screen -------------------
#
# Editing is done one net at a time, and that net usually has no solved rail
# yet — so gating markers purely on rail visibility hid the sources and sinks
# being placed. The rule is now: visible rail, or visible copper.

@pytest.fixture
def rail_viewer(viewer):
    """One free SINK on +3V3, which IS a member of a solved rail. Whether its
    marker draws is then purely a visibility question."""
    from fypa.project_file import EditorDirective, ProjectFile
    proj = ProjectFile()
    proj.upsert_directive(EditorDirective(
        id="d1", kind="free", role="SINK", p_net="+3V3", single_net=True,
        current=0.5, anchor_xy=(10.0, 10.0), layer="Top Layer", layer_id=1))
    viewer._project = proj
    viewer._rail_to_members = {"+3V3": ["+3V3"]}
    viewer._effective_rail_members = lambda rails: (
        ["+3V3"] if "+3V3" in list(rails) else [])
    viewer._visible_rails = list           # rail eye off
    viewer._visible_all_copper_layer_ids = dict  # no all-copper either
    viewer._copper_poly_under_point = lambda x, y, lid: None
    return viewer


def _drawn(v) -> bool:
    return v._directive_rail_visible(v._project.directive_by_id("d1"))


def test_a_marker_on_a_hidden_rail_with_no_copper_stays_hidden(rail_viewer):
    assert _drawn(rail_viewer) is False


def test_a_visible_rail_draws_the_marker(rail_viewer):
    rail_viewer._visible_rails = lambda: ["+3V3"]
    assert _drawn(rail_viewer) is True


def test_visible_copper_draws_the_marker_with_the_rail_off(rail_viewer):
    """The case that prompted this: working a net from the all-copper view,
    rail eye off. The copper is right there, so the marker must be too."""
    rail_viewer._visible_all_copper_layer_ids = lambda: {1: "Top Layer"}
    assert _drawn(rail_viewer) is True


def test_copper_on_another_layer_does_not_draw_the_marker(rail_viewer):
    """All-copper is on, but not for the layer the marker is pinned to."""
    rail_viewer._visible_all_copper_layer_ids = lambda: {2: "Bottom Layer"}
    assert _drawn(rail_viewer) is False


def test_visible_copper_does_not_rescue_the_marker_outside_editor_mode(
        rail_viewer):
    """In the viewer the markers annotate the heatmap, so the rail eye is
    still the only thing that governs them."""
    rail_viewer._editor_mode = False
    rail_viewer._visible_all_copper_layer_ids = lambda: {1: "Top Layer"}
    assert _drawn(rail_viewer) is False


def test_focus_still_wins_over_visible_copper(rail_viewer):
    """Visible copper is a reason to show a marker, not a way around the
    focus — a GND sink must not reappear because GND copper is on screen."""
    rail_viewer._visible_all_copper_layer_ids = lambda: {1: "Top Layer"}
    d = rail_viewer._project.directive_by_id("d1")
    d.p_net = "GND"
    rail_viewer._editor_focus_net = "+3V3"
    rail_viewer._editor_focus_nets = {"+3V3"}
    assert _drawn(rail_viewer) is False


def test_a_component_directive_follows_its_pads_layers(rail_viewer):
    from fypa.project_file import EditorDirective
    rail_viewer._project.upsert_directive(EditorDirective(
        id="d1", kind="component", role="SINK", designator="U1",
        p_net="+3V3", single_net=True, current=0.5))
    seen = {}

    def _pads(des, nets, visible_layer_ids=None, with_layer_color=False,
              pin_filter=None):
        seen["ids"] = visible_layer_ids
        return [(1.0, 2.0)] if visible_layer_ids == {1} else []

    rail_viewer._component_pad_points = _pads
    rail_viewer._visible_all_copper_layer_ids = lambda: {1: "Top Layer"}
    assert _drawn(rail_viewer) is True
    assert seen["ids"] == {1}
    rail_viewer._visible_all_copper_layer_ids = lambda: {7: "Inner 3"}
    assert _drawn(rail_viewer) is False


# --- via / PTH dots follow the focus too ------------------------------------

def _vias(v):
    v.metadata = {"vias": [
        {"x_mm": 1.0, "y_mm": 1.0, "net": "+3V3",
         "layer_start": 1, "layer_end": 4},
        {"x_mm": 2.0, "y_mm": 2.0, "net": "GND",
         "layer_start": 1, "layer_end": 4},
    ]}
    v._via_marker_diameter_mm = lambda rec: 0.3
    return v


def test_vias_of_other_nets_go_with_their_copper(viewer):
    """Left in, they would float as dots over bare substrate."""
    _vias(viewer)
    _click_crosshair(viewer, "+3V3")
    xs, _ys, _d = viewer._collect_via_positions(1, set(), rail_scoped=False)
    assert xs == [1.0]


def test_vias_are_untouched_without_a_focus(viewer):
    _vias(viewer)
    xs, _ys, _d = viewer._collect_via_positions(1, set(), rail_scoped=False)
    assert xs == [1.0, 2.0]


def test_vias_are_untouched_outside_editor_mode(viewer):
    _vias(viewer)
    _click_crosshair(viewer, "+3V3")
    viewer._editor_mode = False
    xs, _ys, _d = viewer._collect_via_positions(1, set(), rail_scoped=False)
    assert xs == [1.0, 2.0]
