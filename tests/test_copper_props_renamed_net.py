"""Viewer-mode copper properties show the editor-mode copper name.

Naming unnamed copper in editor mode stores a ``CopperName`` on the
project; the primitive metadata keeps net ``"(none)"`` until a Resolve
rebuilds it. Clicking that copper in viewer mode must show the user's
name, not "(none)" (Gerber import of the Hermes Mezz showed "(none)").
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
    v.metadata = {"all_copper": [
        {"layer_id": 1, "net": "+12V", "polygons": [_sq(0, 0, 10, 10)]},
        {"layer_id": 1, "net": "(none)", "polygons": [_sq(20, 20, 22, 22)]},
    ]}
    v._phys_name_to_layer_id = {"L1": 1}
    v._project = ProjectFile()
    return v


def _hit(x, y, net="(none)"):
    return {"kind": "region", "record": {"net": net}, "layer_id": 1,
            "net": net, "click_xy": (x, y)}


def test_unnamed_copper_without_rename_stays_none(viewer):
    assert viewer._display_net_for_hit(_hit(21, 21)) == "(none)"


def test_renamed_copper_shows_the_user_name(viewer):
    from fypa.project_file import CopperName
    viewer._project.upsert_copper_name(
        CopperName(anchor_xy=(21.0, 21.0), layer_id=1, name="VDD_MEZZ"))
    assert viewer._display_net_for_hit(_hit(21.5, 20.5)) == (
        "VDD_MEZZ  [pending resolve]")


def test_real_net_is_unchanged(viewer):
    assert viewer._display_net_for_hit(_hit(5, 5, "+12V")) == "+12V"
