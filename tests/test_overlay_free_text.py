"""The viewer's Overlay row: free-standing text (special strings expanded)
and filled regions / fills on the Top / Bottom Overlay layers reach it;
designators and component comments do not."""
import os
from types import SimpleNamespace

from fypa.altium import loader
from fypa.altium.extract import NO_POLYGON, Pt2D, RawFill, RawRegion, RawText


def _text(text, layer_id, **kw):
    base = {"text": text, "center": Pt2D(1.0, 2.0), "height_mm": 1.0,
                "rotation_deg": 0.0, "layer_id": layer_id, "component_index": -1,
                "is_designator": False, "is_comment": False, "is_mirrored": False,
                "is_stroke": True, "stroke_kind": 1, "stroke_width_mm": 0.15}
    base.update(kw)
    return RawText(**base)


def _square(x0, y0, s):
    return (Pt2D(x0, y0), Pt2D(x0 + s, y0), Pt2D(x0 + s, y0 + s),
            Pt2D(x0, y0 + s))


def _region(layer_id, outline, **kw):
    base = {"outline": outline, "holes": (), "layer_id": layer_id, "net_index": -1,
                "kind": 0, "is_polygon_outline": False, "is_keepout": False,
                "is_board_cutout": False, "polygon_index": NO_POLYGON}
    base.update(kw)
    return RawRegion(**base)


def _proj(tmp_path, **kw):
    prj = tmp_path / "x.PrjPcb"
    prj.write_text("[Parameter1]\nName=Revision\nValue=B2\n", encoding="utf-8")
    pcb = tmp_path / "x.PcbDoc"
    pcb.write_bytes(b"")
    os.utime(pcb, (1_700_000_000, 1_700_000_000))
    base = {"tracks": (), "arcs": (), "pads": (), "pcb_components": (), "vias": (),
                "nets": (), "texts": (), "regions": (), "shape_based_regions": (),
                "fills": (), "prjpcb_path": prj, "pcbdoc_path": pcb}
    base.update(kw)
    return SimpleNamespace(**base)


def test_free_overlay_text_is_included(tmp_path):
    proj = _proj(tmp_path, texts=(
        _text("REV 1.0", 33),
        _text("BOT", 34, is_mirrored=True),
        _text("TT", 33, is_stroke=False),
        _text("R1", 33, is_designator=True),
        _text("10k", 33, is_comment=True),
        _text("COPPER", 1),
    ))
    recs = loader._build_overlay_records(proj, str)
    txt = {r["text"]: r for r in recs["silkscreen"] if r["kind"] == "text"}
    assert set(txt) == {"REV 1.0", "BOT", "TT"}
    assert txt["BOT"]["side"] == "bottom"
    assert txt["REV 1.0"]["polylines"]          # stroke font laid out
    assert txt["TT"]["polylines"] == []         # TrueType -> label fallback


def test_special_strings_are_expanded(tmp_path):
    proj = _proj(tmp_path, texts=(
        _text(".ModifiedDate", 33),
        _text(".Revision", 33),
        _text("='REV ' + Revision", 33),
        _text(".NoSuchThing", 33),
    ))
    recs = loader._build_overlay_records(proj, str)
    txt = [r["text"] for r in recs["silkscreen"] if r["kind"] == "text"]
    assert txt[0] == loader._windows_short_date(1_700_000_000)
    assert txt[0] != ".ModifiedDate"
    assert txt[1:] == ["B2", "REV B2", ".NoSuchThing"]


def test_overlay_regions_and_fills(tmp_path):
    sq = _square(0.0, 0.0, 2.0)
    proj = _proj(
        tmp_path,
        # Same region in the legacy stream only -> used.
        regions=(_region(33, sq), _region(1, _square(5, 5, 1)),
                 _region(34, _square(9, 9, 1), is_keepout=True)),
        fills=(RawFill(10.0, 0.0, 11.0, 1.0, 0.0, 34, -1, False),),
    )
    recs = [r for r in loader._build_overlay_records(proj, str)["silkscreen"]
            if r["kind"] == "region"]
    assert sorted(r["side"] for r in recs) == ["bottom", "top"]
    top = next(r for r in recs if r["side"] == "top")
    xs = top["exterior"][:, 0]
    assert xs.min() == 0.0 and xs.max() == 2.0
