from pathlib import Path
from types import SimpleNamespace

import pytest

from fypa.cli import _pdn_fingerprint, _project_fingerprint
from fypa.viewer.tomachie_menu import pdn_choice


def _window(prjpcb, chosen=None, project=None):
    return SimpleNamespace(metadata={"prjpcb_path": str(prjpcb)}, _tomachie_pdn=chosen,
                           _project=project)


def test_the_solve_fingerprint_follows_the_pdn_file_and_corner(tmp_path):
    prj = tmp_path / "a.PrjPcb"
    prj.write_text("x")
    pdn = tmp_path / "a.pdn.json"
    pdn.write_text("{}")
    plain = _project_fingerprint(prj)
    typ = _project_fingerprint(prj, pdn_json=pdn, pdn_corner="typ")
    assert plain["pdn_json"] is None and typ != plain
    assert _project_fingerprint(prj, pdn_json=pdn, pdn_corner="max") != typ
    pdn.write_text('{"x": 1}')
    assert _project_fingerprint(prj, pdn_json=pdn, pdn_corner="typ") != typ
    assert _pdn_fingerprint(tmp_path / "gone.pdn.json", "typ")["sha1"] is None


def test_the_choice_belongs_to_its_project(tmp_path):
    a, b = tmp_path / "a.PrjPcb", tmp_path / "b.PrjPcb"
    a.write_text("x")
    pdn = tmp_path / "a.pdn.json"
    win = _window(a, (pdn, "max", a))
    assert pdn_choice(win) == (pdn, "max")
    assert pdn_choice(win, b) == (None, "typ")
    assert win._tomachie_pdn is None


def test_a_project_file_for_another_design_supplies_no_pdn(tmp_path):
    a, b = tmp_path / "a.PrjPcb", tmp_path / "b.PrjPcb"
    project = SimpleNamespace(pdn_json=str(tmp_path / "a.pdn.json"), pdn_corner="typ",
                              prjpcb_path=str(a))
    assert pdn_choice(_window(a, project=project))[0] == Path(project.pdn_json)
    assert pdn_choice(_window(b, project=project), b)[0] is None


def test_a_broken_pdn_file_does_not_stop_the_load(tmp_path, monkeypatch):
    import fypa.altium.loader as loader
    import fypa.tomachie_pdn as tp

    class Reached(Exception):
        pass

    def broken(*_a, **_k):
        raise tp.PdnImportError("Not a pdn.json version 1 file")

    def annotations(*_a, **_k):
        raise Reached

    monkeypatch.setattr(tp, "read_pdn_for_board", broken)
    monkeypatch.setattr(loader, "parse_annotations", annotations)
    monkeypatch.setattr(loader, "extract_project",
                        lambda *a, **k: SimpleNamespace(enabled_copper_layer_ids=list))
    with pytest.raises(Reached):
        loader.load_project(tmp_path / "a.PrjPcb", pdn_json=tmp_path / "a.pdn.json")


def test_a_pdn_file_of_the_wrong_shape_does_not_stop_the_load(tmp_path):
    """A JSON array at the top level raises AttributeError, not one of the
    errors the reader expects: the design still loads, without the file."""
    import fypa.altium.loader as loader

    pdn = tmp_path / "a.pdn.json"
    pdn.write_text("[1, 2, 3]")
    seen = _load_until_annotations(loader, tmp_path, pdn)
    assert seen["extracted"] is seen["original"]


def test_a_failure_applying_the_file_leaves_the_design_unchanged(tmp_path, monkeypatch):
    """The file reads, but merging its parameters into the project fails: the
    annotations are parsed from the project as extracted, not a half-applied one."""
    import fypa.altium.loader as loader
    import fypa.tomachie_pdn as tp

    def apply_fails(*_a, **_k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(tp, "read_pdn_for_board", lambda *a, **k: ({"U1": {}}, []))
    monkeypatch.setattr(tp, "applied_designators", lambda *a, **k: {"U1"})
    monkeypatch.setattr(tp, "apply_to_project", apply_fails)
    seen = _load_until_annotations(loader, tmp_path, tmp_path / "a.pdn.json",
                                   monkeypatch=monkeypatch)
    assert seen["extracted"] is seen["original"]


def _load_until_annotations(loader, tmp_path, pdn, monkeypatch=None) -> dict:
    """Run load_project as far as annotation parsing, recording which project
    it was handed, then stop."""
    class Reached(Exception):
        pass

    original = SimpleNamespace(enabled_copper_layer_ids=list)
    seen = {"original": original}

    def annotations(extracted, *_a, **_k):
        seen["extracted"] = extracted
        raise Reached

    mp = monkeypatch or pytest.MonkeyPatch()
    try:
        mp.setattr(loader, "parse_annotations", annotations)
        mp.setattr(loader, "extract_project", lambda *a, **k: original)
        with pytest.raises(Reached):
            loader.load_project(tmp_path / "a.PrjPcb", pdn_json=pdn)
    finally:
        if monkeypatch is None:
            mp.undo()
    return seen
