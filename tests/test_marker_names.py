"""Free-marker names: shown instead of the ``EDIT_<id>`` key the solve uses."""

from __future__ import annotations

from fypa.project_file import (
    EditorDirective,
    ProjectFile,
    clean_marker_name,
    ensure_marker_names,
    with_marker_names,
)
from fypa.topology import build_topology_model, render_topology_svg
from tests.topology_fixtures import project_b_compact_metadata


def _free(role="SOURCE", name=None, id_=None):
    d = EditorDirective(kind="free", role=role, p_net="VIN", name=name)
    if id_:
        d.id = id_
    return d


def test_markers_get_numbered_names_per_role_in_list_order():
    ds = [_free("SOURCE"), _free("SINK"), _free("SINK"), _free("SOURCE"),
          EditorDirective(kind="component", designator="U1")]
    assert ensure_marker_names(ds)
    assert [d.name for d in ds] == ["SRC1", "SNK1", "SNK2", "SRC2", None]
    assert not ensure_marker_names(ds)          # stable once named


def test_new_marker_takes_the_next_free_number():
    ds = [_free("SINK", "SNK1"), _free("SINK", "SNK3"), _free("SINK")]
    ensure_marker_names(ds)
    assert ds[2].name == "SNK2"


def test_typed_names_are_kept_and_automatic_ones_follow_the_role():
    typed = _free("SINK", "FPGA core")
    auto = _free("SINK", "SRC4")                # was a source, now a sink
    ds = [typed, auto]
    ensure_marker_names(ds)
    assert typed.name == "FPGA core"
    assert auto.name == "SNK1"


def test_names_are_trimmed_and_blank_means_automatic():
    assert clean_marker_name("  Bench   PSU ") == "Bench PSU"
    assert clean_marker_name("   ") is None
    assert len(clean_marker_name("x" * 80)) == 24


def test_name_round_trips_and_old_files_get_names_on_load(tmp_path):
    p = ProjectFile(editor_directives=[_free("SOURCE", "Bench PSU")])
    d = EditorDirective.from_dict(p.editor_directives[0].to_dict())
    assert d.name == "Bench PSU"
    # A file written before markers had names.
    old = p.editor_directives[0].to_dict()
    del old["name"]
    path = tmp_path / "old.fypa"
    import json
    path.write_text(json.dumps({"schema_version": 2,
                                "editor_directives": [old, dict(old, id="b")]}),
                    encoding="utf-8")
    loaded = ProjectFile.load(path)
    assert [d.name for d in loaded.editor_directives] == ["SRC1", "SRC2"]


def test_upsert_names_a_new_marker():
    p = ProjectFile()
    d = _free("SINK")
    p.upsert_directive(d)
    assert d.name == "SNK1"


def _marker_metadata(ed):
    """project_b_compact with J1 replaced by the marker ``ed``."""
    md = project_b_compact_metadata()
    for d in md["directives"]:
        if d["designator"] == "J1":
            d["designator"] = d["label"] = f"EDIT_{ed.id}"
    return md


def test_metadata_gets_display_names_but_keeps_its_keys():
    ed = _free("SOURCE", "Bench PSU", id_="abc123")
    md = with_marker_names(_marker_metadata(ed), [ed])
    (d,) = [d for d in md["directives"] if d.get("marker")]
    assert d["display_name"] == "Bench PSU"
    assert d["designator"] == d["label"] == "EDIT_abc123"
    others = [d for d in md["directives"] if not d.get("marker")]
    assert others and all("display_name" not in d for d in others)


def test_topology_shows_the_name_and_draws_the_marker_dashed():
    ed = _free("SOURCE", "Bench PSU", id_="abc123")
    md = with_marker_names(_marker_metadata(ed), [ed])
    model = build_topology_model(md, layout="rows")
    (node,) = [n for n in model.nodes if n.marker]
    assert node.label == "Bench PSU" and node.designator == "EDIT_abc123"
    assert "copper" in node.tooltip
    svg = render_topology_svg(model)
    assert ">Bench PSU</text>" in svg and "EDIT_" not in svg
    assert 'stroke-dasharray="5 3"' in svg
    # Without names, nothing is drawn dashed.
    plain = render_topology_svg(build_topology_model(project_b_compact_metadata(),
                                                     layout="rows"))
    assert 'stroke-dasharray="5 3"' not in plain
