"""TrueType PCB text is outlined into board-space geometry: it scales with
its height in mm (not a capped screen pixel size), mirrors on the bottom
side, rotates about its anchor and sits one descent above it."""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

pytest.importorskip("PySide6")


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if sys.platform == "win32":
        os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


def _rec(**kw):
    base = {"text": "TEST", "x_mm": 10.0, "y_mm": 20.0, "height_mm": 2.0,
            "rotation_deg": 0.0, "font_name": "Arial", "bold": False,
            "italic": False, "mirrored": False}
    base.update(kw)
    return base


def _tris(**kw):
    from fypa.viewer.truetype_text import truetype_text_tris
    return truetype_text_tris(_rec(**kw))


def test_scales_with_height_in_mm():
    small, big = _tris(height_mm=1.0), _tris(height_mm=10.0)
    assert small.size and big.size
    w_small = np.ptp(small[:, 0])
    w_big = np.ptp(big[:, 0])
    assert w_big == pytest.approx(10.0 * w_small, rel=1e-3)


# Glyphs may overhang their origin slightly (a negative left side bearing),
# and the face Qt resolves varies by machine, so anchor checks allow 2% of
# the 2 mm text height.
_TOL = 0.04


def test_sits_above_anchor_within_cell():
    a = _tris()
    assert a[:, 0].min() >= 10.0 - _TOL           # starts at the anchor x
    assert a[:, 1].min() > 20.0                   # baseline above anchor
    assert a[:, 1].max() <= 20.0 + 2.0 + _TOL     # inside the 2 mm cell


def test_mirrored_flips_about_anchor():
    a = _tris(mirrored=True)
    assert a[:, 0].max() <= 10.0 + _TOL


def test_rotation_90_turns_text_upward():
    a = _tris(rotation_deg=90.0)
    assert np.ptp(a[:, 1]) > np.ptp(a[:, 0])      # runs along +y
    assert a[:, 0].max() <= 10.0 + _TOL


def test_wire_and_solid_cached_separately():
    from fypa.viewer.truetype_text import truetype_text_tris
    r = _rec()
    solid = truetype_text_tris(r, solid=True)
    wire = truetype_text_tris(r, solid=False)
    assert solid.size and wire.size
    assert r["_tt_tris"] is solid and r["_tt_tris_wire"] is wire


def test_blank_text_is_empty():
    assert _tris(text="   ").shape == (0, 2)


def test_box_anchor_puts_glyph_box_corner_on_anchor():
    # Altium text-box mode: the glyphs' bounding box has its bottom-left
    # (in the text frame) exactly on the anchor - no descent gap.
    a = _tris(box_anchor=True)
    assert a[:, 0].min() == pytest.approx(10.0, abs=1e-4)
    assert a[:, 1].min() == pytest.approx(20.0, abs=1e-4)
    legacy = _tris()
    assert legacy[:, 1].min() > a[:, 1].min() + 0.1   # descent gap


def test_box_anchor_rotated_90():
    # Rotated 90 deg CCW the text frame's bottom edge faces +x, so the
    # glyph box's right / bottom world edges sit on the anchor.
    a = _tris(box_anchor=True, rotation_deg=90.0)
    assert a[:, 0].max() == pytest.approx(10.0, abs=1e-4)
    assert a[:, 1].min() == pytest.approx(20.0, abs=1e-4)
