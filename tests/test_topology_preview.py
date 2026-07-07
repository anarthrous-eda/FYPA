"""Tests for live topology metadata preview."""

from fypa.project_file import EditorDirective
from fypa.topology import build_topology_model
from fypa.topology.preview import metadata_for_topology

from tests.topology_fixtures import project_b_compact_metadata


def test_metadata_for_topology_unchanged_without_live_preview():
    md = project_b_compact_metadata()
    out = metadata_for_topology(md, live_preview=False)
    assert out is md


def test_metadata_for_topology_merges_pending_free_source():
    md = {"directives": [], "net_canonical": {}}
    ed = EditorDirective(
        kind="free",
        role="SOURCE",
        anchor_xy=(10.0, 20.0),
        layer_id=1,
        single_net=True,
        p_net="VDD_3V3",
        voltage=3.3,
    )
    out = metadata_for_topology(
        md,
        editor_directives=[ed],
        live_preview=True,
    )
    assert len(out["directives"]) == 1
    d = out["directives"][0]
    assert d["role"] == "SOURCE"
    assert d["schdoc"] == "(editor)"
    model = build_topology_model(out)
    assert any(n.role == "SOURCE" for n in model.nodes)


def test_metadata_for_topology_drops_overridden_schematic_directive():
    md = project_b_compact_metadata()
    n_before = len(md["directives"])
    j1 = next(d for d in md["directives"] if d["designator"] == "J1")
    ed = EditorDirective(
        kind="component",
        role="SOURCE",
        designator="J1",
        overrides_designator="J1",
        single_net=False,
        p_net="VDD_3V3_PWR",
        n_net="GND",
        voltage=5.0,
    )
    out = metadata_for_topology(
        md,
        editor_directives=[ed],
        live_preview=True,
    )
    designators = {d["designator"] for d in out["directives"]}
    assert "J1" in designators
    assert len(out["directives"]) == n_before
    j1_out = next(d for d in out["directives"] if d["designator"] == "J1")
    assert j1_out["value"] == 5.0
    assert j1_out["schdoc"] == "(editor)"
    assert j1.get("schdoc") != "(editor)"
