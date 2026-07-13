"""Per-capacitor override persistence in the .fypa project file.

The Capacitors tab's include / target choices round-trip through
``ProjectFile.cap_overrides`` as an additive key — older .fypa files (which
lack it entirely) must still load at the unchanged schema version.
"""
from __future__ import annotations

from fypa.project_file import SCHEMA_VERSION, CapOverride, ProjectFile


def _saved(tmp_path, proj: ProjectFile) -> ProjectFile:
    path = tmp_path / "p.fypa"
    proj.save(path)
    return ProjectFile.load(path)


def test_cap_override_round_trip(tmp_path):
    proj = ProjectFile()
    proj.upsert_cap_override("C1", include=False)
    proj.upsert_cap_override("C2", target_label="U9")
    loaded = _saved(tmp_path, proj)

    by_designator = {c.designator: c for c in loaded.cap_overrides}
    assert by_designator["C1"].include is False
    assert by_designator["C1"].target_label is None
    assert by_designator["C2"].include is None
    assert by_designator["C2"].target_label == "U9"


def test_upsert_merges_fields_on_one_override():
    proj = ProjectFile()
    proj.upsert_cap_override("C1", include=False)
    proj.upsert_cap_override("C1", target_label="U5")
    assert len(proj.cap_overrides) == 1
    only = proj.cap_overrides[0]
    assert only.include is False and only.target_label == "U5"


def test_upsert_leaves_unpassed_field_untouched():
    proj = ProjectFile()
    proj.upsert_cap_override("C1", include=False, target_label="U5")
    proj.upsert_cap_override("C1", target_label="U9")   # include not passed
    assert proj.cap_overrides[0].include is False
    assert proj.cap_overrides[0].target_label == "U9"


def test_clearing_both_fields_removes_the_override():
    proj = ProjectFile()
    proj.upsert_cap_override("C1", include=False)
    proj.upsert_cap_override("C1", include=None)
    assert proj.cap_overrides == []


def test_no_op_override_is_never_created():
    proj = ProjectFile()
    proj.upsert_cap_override("C1", include=None, target_label=None)
    assert proj.cap_overrides == []


def test_cap_override_maps_shape():
    proj = ProjectFile()
    proj.upsert_cap_override("C1", include=False)
    proj.upsert_cap_override("C2", include=True, target_label="U9")
    proj.upsert_cap_override("C3", target_label="U5")
    includes, targets = proj.cap_override_maps()
    assert includes == {"C1": False, "C2": True}
    assert targets == {"C2": "U9", "C3": "U5"}


def test_legacy_project_without_cap_overrides_loads(tmp_path):
    # A .fypa written before this feature: no "cap_overrides" key at all.
    path = tmp_path / "old.fypa"
    path.write_text(
        f'{{"schema_version": {SCHEMA_VERSION}, "source_kind": "altium"}}',
        encoding="utf-8")
    assert ProjectFile.load(path).cap_overrides == []


def test_cap_overrides_do_not_bump_schema_version(tmp_path):
    import json
    proj = ProjectFile()
    proj.upsert_cap_override("C1", include=False)
    path = tmp_path / "p.fypa"
    proj.save(path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["schema_version"] == SCHEMA_VERSION


def test_cap_override_ids_are_stable_across_save(tmp_path):
    proj = ProjectFile()
    proj.cap_overrides.append(CapOverride(designator="C1", include=False,
                                          id="abc123"))
    assert _saved(tmp_path, proj).cap_overrides[0].id == "abc123"


# --- per-part parasitics (impedance model) ------------------------------------


def test_parasitic_overrides_round_trip(tmp_path):
    proj = ProjectFile()
    proj.upsert_cap_override("C1", esl_h=0.2e-9, esr_ohm=3e-3)
    loaded = _saved(tmp_path, proj)
    only = loaded.cap_overrides[0]
    assert only.esl_h == 0.2e-9 and only.esr_ohm == 3e-3
    assert loaded.cap_parasitic_overrides() == ({"C1": 0.2e-9},
                                                {"C1": 3e-3})


def test_parasitics_merge_with_include_and_target_on_one_override():
    proj = ProjectFile()
    proj.upsert_cap_override("C1", include=False)
    proj.upsert_cap_override("C1", esr_ohm=5e-3)
    proj.upsert_cap_override("C1", target_label="U9")
    assert len(proj.cap_overrides) == 1
    only = proj.cap_overrides[0]
    assert (only.include, only.target_label, only.esr_ohm) == \
        (False, "U9", 5e-3)
    assert only.esl_h is None


def test_clearing_every_field_removes_the_override():
    proj = ProjectFile()
    proj.upsert_cap_override("C1", esl_h=1e-9, esr_ohm=1e-3)
    proj.upsert_cap_override("C1", esl_h=None)
    assert proj.cap_overrides                      # esr still set
    proj.upsert_cap_override("C1", esr_ohm=None)
    assert proj.cap_overrides == []


def test_an_esl_only_override_is_kept():
    """One parasitic may be overridden while the other falls back to the
    package library — the ESL is the one that usually needs a vendor value."""
    proj = ProjectFile()
    proj.upsert_cap_override("C1", esl_h=0.2e-9)
    esls, esrs = proj.cap_parasitic_overrides()
    assert esls == {"C1": 0.2e-9} and esrs == {}


def test_legacy_override_without_parasitics_loads(tmp_path):
    import json
    path = tmp_path / "old.fypa"
    path.write_text(json.dumps({
        "schema_version": SCHEMA_VERSION,
        "cap_overrides": [{"designator": "C1", "include": False}],
    }), encoding="utf-8")
    only = ProjectFile.load(path).cap_overrides[0]
    assert only.include is False
    assert only.esl_h is None and only.esr_ohm is None


def test_unparseable_parasitics_degrade_to_none(tmp_path):
    import json
    path = tmp_path / "bad.fypa"
    path.write_text(json.dumps({
        "schema_version": SCHEMA_VERSION,
        "cap_overrides": [{"designator": "C1", "include": False,
                           "esl_h": "banana", "esr_ohm": None}],
    }), encoding="utf-8")
    only = ProjectFile.load(path).cap_overrides[0]
    assert only.esl_h is None and only.include is False
