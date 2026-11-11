"""No-net copper drawn over netted copper on the same layer.

Altium lets a free region ("Place > Region") with no net sit on top of a
netted fill or pour. It's physically the same copper, so the editor must
treat it as part of that net: one connected component, the net's name on
a click, no auto-minted ``$NET_xxx`` for it, and an old project's stale
auto-name for it pruned with its markers moved onto the real net.
(Mangala: a no-net region over the HS_SNS fill came up as ``$NET_0041``.)
"""
from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("PySide6")


def _sq(x0, y0, x1, y1):
    return {"exterior": np.array(
        [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]], dtype=np.float32),
        "holes": []}


@pytest.fixture
def viewer():
    import fypa.altium_viewer as V
    from fypa.project_file import ProjectFile

    v = V.PdnViewer.__new__(V.PdnViewer)
    pour = _sq(0, 0, 10, 10)
    overlay = _sq(4, 4, 6, 6)          # no-net region inside the pour
    island = _sq(20, 20, 22, 22)       # genuinely isolated no-net copper
    v.metadata = {"all_copper": [
        {"layer_id": 1, "net": "+12V", "polygons": [pour]},
        {"layer_id": 1, "net": "(none)", "polygons": [overlay, island]},
    ]}
    v._phys_name_to_layer_id = {"L1": 1}
    v._project = ProjectFile()
    v._ensure_project = lambda: v._project
    v._mark_project_dirty = lambda: None
    v._all_net_names = lambda: ["+12V"]
    v._polys = {"pour": pour, "overlay": overlay, "island": island}
    return v


def test_overlapping_no_net_copper_joins_the_netted_component(viewer):
    cc = viewer._connected_components_data()
    p = viewer._polys
    assert (cc["component_of"][(1, id(p["overlay"]))]
            == cc["component_of"][(1, id(p["pour"]))])
    assert (cc["component_of"][(1, id(p["island"]))]
            != cc["component_of"][(1, id(p["pour"]))])
    assert cc["real_net_of"][cc["component_of"][(1, id(p["pour"]))]] == "+12V"


def test_click_on_the_overlap_reports_the_real_net(viewer):
    import shapely.geometry as sg
    assert viewer._named_copper_hit_on_layer(1, sg.Point(5, 5)) == "+12V"
    assert viewer._named_copper_hit_on_layer(1, sg.Point(21, 21)) == "(none)"
    assert viewer._connected_real_net(5, 5, 1) == "+12V"


def test_auto_naming_skips_the_overlap_and_names_the_island(viewer):
    viewer._auto_name_isolated_regions()
    names = viewer._project.copper_names
    assert len(names) == 1
    assert 20 <= names[0].anchor_xy[0] <= 22


def test_stale_auto_name_is_pruned_and_markers_follow(viewer):
    from fypa.project_file import CopperName, EditorDirective
    proj = viewer._project
    proj.copper_names.append(
        CopperName(anchor_xy=(5.0, 5.0), layer_id=1, name="$NET_0041"))
    proj.copper_names.append(
        CopperName(anchor_xy=(5.0, 5.0), layer_id=1, name="MY_NAME"))
    proj.upsert_directive(EditorDirective(
        id="d1", kind="free", role="SINK", p_net="$NET_0041",
        single_net=True, current=1.0, anchor_xy=(5.0, 5.0),
        layer="L1", layer_id=1))

    # The name must not stick to the netted pour, even before pruning.
    assert viewer._copper_name_at(2.0, 2.0, 1) is None

    viewer._auto_name_isolated_regions()
    kept = {c.name for c in proj.copper_names}
    assert "$NET_0041" not in kept
    assert "MY_NAME" in kept             # user-typed names are left alone
    assert proj.editor_directives[0].p_net == "+12V"
