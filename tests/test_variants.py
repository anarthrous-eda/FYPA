"""Altium assembly variants: reading, choosing, applying, and showing them."""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace as NS

import pytest

from fypa.altium.variants import (
    NO_VARIATIONS,
    ProjectVariants,
    Variant,
    apply_param_overrides,
    read_variants,
    resolve_variant,
    variant_metadata,
    variant_reload_needed,
)
from fypa.viewer import variant_ui as V

PRJPCB = """[Design]
Version=1.0
CurrentVariant=Bringup

[Document1]
DocumentPath=Main.SchDoc

[ProjectVariant1]
UniqueId=0bbadd81-387b-439b-aca1-8cd5015f69be
Description=Bringup
AllowFabrication=0
ParameterCount=0
VariationCount=3
Variation1=Designator=R207|UniqueId=\\PMFLWZNJ\\FWPUQIDR|Kind=1|AlternatePart=
Variation2=Designator=R101_VCC_HDIOC|UniqueId=\\FLYIJELO\\WENGULYG\\CVVWDYKP|Kind=1|AlternatePart=
Variation3=Designator=R94_1V0|UniqueId=\\FLYIJELO\\HBKIHRRJ\\RAEIXWFH|Kind=2|AlternatePart=RC0603FR-07158KL
ParamVariation1=ParameterName=Comment|VariantValue=RC0603FR-07158KL
ParamDesignator1=R94_1V0
ParamVariation2=ParameterName=Value|VariantValue=158k
ParamDesignator2=R94_1V0
ParamVariationCount=2

[ProjectVariant2]
UniqueId=1bbadd81-387b-439b-aca1-8cd5015f69be
Description=Production
AllowFabrication=1
ParameterCount=0
VariationCount=1
Variation1=Designator=R203|UniqueId=\\PMFLWZNJ\\AAAAAAAA|Kind=1|AlternatePart=
"""


@pytest.fixture
def prjpcb(tmp_path):
    p = tmp_path / "board.PrjPcb"
    p.write_text(PRJPCB, encoding="utf-8")
    return p


def test_reads_variants_and_altium_current(prjpcb) -> None:
    pv = read_variants(prjpcb)
    assert pv.current == "Bringup"
    assert pv.names == ["Bringup", "Production"]
    bring = pv.get("bringup")                      # case-insensitive
    assert bring.not_fitted == {"R207", "R101_VCC_HDIOC"}
    assert bring.param_overrides["R94_1V0"]["Value"] == "158k"
    assert pv.get("Production").not_fitted == {"R203"}


def test_missing_or_non_altium_project_has_no_variants(tmp_path) -> None:
    assert read_variants(None).variants == {}
    assert read_variants(tmp_path / "nope.PrjPcb").variants == {}
    assert read_variants(tmp_path / "x.fypa-gerber").current is None


@pytest.mark.parametrize("request_, name, note", [
    (None, "Bringup", ""),                      # follow Altium
    (NO_VARIATIONS, "", ""),                    # base design
    ("Production", "Production", ""),
    ("Gone", "Bringup", "not in the project"),  # falls back to Altium's
])
def test_resolve_variant(prjpcb, request_, name, note) -> None:
    choice = resolve_variant(read_variants(prjpcb), request_)
    assert choice.name == name
    assert (note in choice.note) if note else not choice.note
    assert choice.requested == request_


def test_follow_altium_with_no_current_is_the_base_design() -> None:
    pv = ProjectVariants(current=None, variants={"A": Variant("A")})
    choice = resolve_variant(pv, None)
    assert choice.variant is None and choice.label == "[No Variations]"


@dataclass(frozen=True)
class _Comp:
    designator: str
    parameters: dict = field(default_factory=dict)


@dataclass(frozen=True)
class _Ex:
    sch_components: tuple
    pcb_components: tuple


def test_alternate_part_parameters_replace_the_base_ones() -> None:
    ex = _Ex(sch_components=(_Comp("R94_1V0", {"Value": "100k", "Footprint": "0603"}),
                             _Comp("R1", {"Value": "0R"})),
             pcb_components=(_Comp("R94_1V0", {"Value": "100k"}),))
    v = Variant("Bringup", param_overrides={"R94_1V0": {"Value": "158k"}})
    out = apply_param_overrides(ex, v)
    assert out.sch_components[0].parameters == {"Value": "158k", "Footprint": "0603"}
    assert out.sch_components[1] is ex.sch_components[1]
    assert out.pcb_components[0].parameters["Value"] == "158k"
    assert apply_param_overrides(ex, None) is ex


def test_reload_needed_tracks_the_request(prjpcb) -> None:
    loaded = NS(variant_applied=None, extracted=NS(prjpcb_path=prjpcb))
    assert not variant_reload_needed(loaded, None)
    assert variant_reload_needed(loaded, "Production")
    assert variant_reload_needed(loaded, NO_VARIATIONS)
    assert not variant_reload_needed(None, "Production")


def test_old_cache_reloads_only_when_the_project_has_variants(prjpcb, tmp_path) -> None:
    # Loaded before variants were supported: no variant_applied attribute.
    assert variant_reload_needed(NS(extracted=NS(prjpcb_path=prjpcb)), None)
    plain = tmp_path / "plain.PrjPcb"
    plain.write_text("[Design]\nVersion=1.0\n", encoding="utf-8")
    assert not variant_reload_needed(NS(extracted=NS(prjpcb_path=plain)), None)
    gerber = NS(extracted=NS(prjpcb_path=tmp_path / "board.fypa-gerber"))
    assert not variant_reload_needed(gerber, None)


def _meta(prjpcb, request_):
    choice = resolve_variant(read_variants(prjpcb), request_)
    loaded = NS(variant_choice=choice,
                extracted=NS(pcb_components=[NS(designator="R207"),
                                             NS(designator="R101_VCC_HDIOC")]))
    return variant_metadata(loaded)


def test_metadata_records_the_loaded_variant(prjpcb) -> None:
    meta = _meta(prjpcb, None)
    assert meta["requested"] is None and meta["name"] == "Bringup"
    assert meta["not_fitted"] == ["R101_VCC_HDIOC", "R207"]
    assert meta["alternates"] == ["R94_1V0"]
    assert meta["available"] == ["Bringup", "Production"]
    assert variant_metadata(NS()) is None


def test_picker_entries_and_pending_state(prjpcb) -> None:
    meta = _meta(prjpcb, None)
    labels = [label for label, _ in V.combo_entries(meta)]
    assert labels == ["Altium default: Bringup", "[No Variations]", "Bringup", "Production"]
    # Choosing Bringup explicitly loads the same build: nothing pending.
    assert not V.is_pending("Bringup", meta)
    assert V.is_pending("Production", meta)
    assert V.is_pending(NO_VARIATIONS, meta)
    assert V.data_to_request(V.request_to_data(None)) is None
    assert V.data_to_request(V.request_to_data("")) == ""


def test_bridges_banner_names_the_active_variant(prjpcb) -> None:
    theme = {"fg_muted": "#888", "accent": "#0af", "warn": "#fa0", "fg_dim": "#777"}
    meta = _meta(prjpcb, None)
    html = V.banner_html(meta, None, theme)
    assert "Bringup" in html and "following Altium" in html
    assert "2 part(s) not fitted" in html and "next Resolve" not in html
    pending = V.banner_html(meta, "Production", theme)
    assert "Changes to Production on the next Resolve" in pending
    assert "no Altium variants" in V.banner_html(None, None, theme)
    assert "Will load [No Variations]" in V.picker_status_html(meta, "", theme)


def test_project_file_round_trips_the_variant(tmp_path) -> None:
    from fypa.project_file import ProjectFile

    for value in (None, "", "Bringup"):
        path = tmp_path / f"p{value!r}.fypa"
        ProjectFile(variant=value).save(path)
        assert ProjectFile.load(path).variant == value


# --- the viewer: Properties picker and Bridges banner --------------------------

pytest.importorskip("PySide6")


@pytest.fixture(scope="module")
def qapp():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


_VAR_META = {
    "requested": None, "name": "Bringup", "label": "Bringup",
    "altium_current": "Bringup", "available": ["Bringup", "Production"],
    "not_fitted": ["R101_VCC_HDIOC", "R207"], "alternates": ["R94_1V0"], "note": "",
}


def _rows() -> list[dict]:
    base = {"footprint": "0603", "pin_count": 2, "resistance_ohm": None,
            "impact": "", "touches_active_rail": False,
            "x_mm": 0.0, "y_mm": 0.0, "layer_id": 1, "value": "0R", "kind": "resistor"}
    return [
        {**base, "designator": "R207", "net_a": "FAN_PWMSTART0", "net_b": "GND",
         "state": "not_fitted", "why": "not fitted in variant 'Bringup' - left open"},
        {**base, "designator": "R203", "net_a": "+PL_3V3", "net_b": "FAN_PWMSTART0",
         "state": "auto", "resistance_ohm": 0.0005, "why": "value 0R reads as a link"},
    ]


@pytest.fixture
def viewer(qapp, monkeypatch):
    from PySide6.QtWidgets import QMainWindow

    import fypa.altium_viewer as AV
    from fypa.project_file import ProjectFile

    v = AV.PdnViewer.__new__(AV.PdnViewer)
    QMainWindow.__init__(v)
    v.metadata = {"bridge_candidates": _rows(), "variant": dict(_VAR_META)}
    v._project = ProjectFile()
    v._project_path = None
    v.tabs = None
    v._ensure_project = lambda: v._project
    v._dirty_calls = []
    v._mark_project_dirty = lambda: v._dirty_calls.append(1)
    v._update_pending_rails = lambda: None
    v._render = lambda: None
    v._test_bridges = AV.PdnViewer._build_bridges_tab(v)
    AV.PdnViewer._populate_bridges_table(v)
    v._test_picker = AV.PdnViewer._build_variant_picker(v)
    return v


def test_bridges_banner_and_not_fitted_rows(viewer) -> None:
    banner = viewer.bridges_variant_banner.text()
    assert "Bringup" in banner and "2 part(s) not fitted" in banner
    t = viewer.bridges_table
    states = {t.item(i, 0).text(): t.item(i, 5).text() for i in range(t.rowCount())}
    assert states["R207"] == "not fitted"
    assert t.item(0, 0).font().strikeOut() or t.item(1, 0).font().strikeOut()
    viewer.bridges_state_combo.setCurrentText("Not fitted")
    shown = [t.item(i, 0).text() for i in range(t.rowCount()) if not t.isRowHidden(i)]
    assert shown == ["R207"]


def test_picker_lists_variants_and_changes_the_project(viewer) -> None:
    combo = viewer.variant_combo
    labels = [combo.itemText(i) for i in range(combo.count())]
    assert labels == ["Altium default: Bringup", "[No Variations]", "Bringup", "Production"]
    assert combo.currentIndex() == 0                     # project.variant is None
    combo.setCurrentIndex(3)
    viewer._on_variant_chosen(3)
    assert viewer._project.variant == "Production" and viewer._dirty_calls
    assert "press Resolve" in viewer.variant_status_label.text()
    assert "Changes to Production on the next Resolve" in \
        viewer.bridges_variant_banner.text()
    viewer._on_variant_chosen(1)                         # [No Variations]
    assert viewer._project.variant == ""


def test_picker_is_disabled_without_variant_metadata(viewer) -> None:
    viewer.metadata = {"bridge_candidates": []}
    viewer._refresh_variant_picker()
    assert not viewer.variant_combo.isEnabled()
    assert "no Altium assembly variants" in viewer.variant_status_label.text()


# --- binding entries to parts the way Altium does (by UniqueId) -----------------


def _pcb(des, uid):
    return NS(designator=des, source_unique_id=uid)


def test_stale_designators_bind_by_unique_id() -> None:
    """The board was re-annotated after the variant was set up: the entry
    still reads R101_VCC_HDIO, but its UniqueId is now R103_VCC_HDIO's. Altium
    leaves R103 off and fits R101 - FYPA must do the same."""
    v = Variant("Bringup",
                not_fitted=frozenset({"R101_VCC_HDIO", "R207", "R999"}),
                param_overrides={"R94_1V0": {"Value": "158k"}},
                unique_ids={"R101_VCC_HDIO": r"\FLY\LKL\CVV", "R207": r"\PMF\FWP",
                            "R94_1V0": r"\FLY\HBK\RAE", "R999": r"\GONE\X"})
    board = [_pcb("R101_VCC_HDIO", r"\FLY\LKL\LIG"),     # a different part now
             _pcb("R103_VCC_HDIO", r"\FLY\LKL\CVV"),
             _pcb("R207", r"\PMF\FWP"),
             _pcb("R108_1V0", r"\FLY\HBK\RAE")]
    from fypa.altium.variants import bind_to_board
    b = bind_to_board(v, board)
    assert b.not_fitted == {"R103_VCC_HDIO", "R207"}
    assert "R101_VCC_HDIO" not in b.not_fitted
    assert b.param_overrides == {"R108_1V0": {"Value": "158k"}}
    assert ("R101_VCC_HDIO", "R103_VCC_HDIO") in b.renamed
    assert b.orphans == ("R999",)                         # Altium ignores it too


def test_board_without_unique_ids_keeps_designators() -> None:
    from fypa.altium.variants import bind_to_board
    v = Variant("A", not_fitted=frozenset({"R1"}), unique_ids={"R1": r"\X"})
    assert bind_to_board(v, [_pcb("R1", "")]) is v
    assert bind_to_board(None, []) is None


def test_reads_unique_ids_from_the_project(prjpcb) -> None:
    v = read_variants(prjpcb).get("Bringup")
    assert v.unique_ids["R101_VCC_HDIOC"] == r"\FLYIJELO\WENGULYG\CVVWDYKP"


def test_ui_explains_entries_matched_by_unique_id() -> None:
    theme = {"fg_muted": "#888", "accent": "#0af", "warn": "#fa0", "fg_dim": "#777"}
    meta = {**_VAR_META, "renamed": [["R101_VCC_HDIO", "R103_VCC_HDIO"]]}
    for html in (V.banner_html(meta, None, theme), V.picker_status_html(meta, None, theme)):
        assert "R101_VCC_HDIO → R103_VCC_HDIO" in html and "re-annotated" in html
