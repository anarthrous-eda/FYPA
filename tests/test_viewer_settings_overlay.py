"""Overlay visibility round-trip via viewer_settings on .fypa projects."""

from __future__ import annotations

from fypa.altium_viewer import PdnViewer
from fypa.project_file import ProjectFile


class _OverlayViewerStub:
    _init_overlay_state = PdnViewer._init_overlay_state
    _load_overlay_colors_from_project = PdnViewer._load_overlay_colors_from_project
    _load_overlay_visibility_from_project = (
        PdnViewer._load_overlay_visibility_from_project
    )
    _store_viewer_settings = PdnViewer._store_viewer_settings

    def __init__(self) -> None:
        self._project = ProjectFile()
        PdnViewer._init_overlay_state(self)


def test_load_overlay_visibility_from_project_restores_vis() -> None:
    viewer = _OverlayViewerStub()
    viewer._overlay_state["silkscreen"]["both"]["vis"] = "all"
    viewer._overlay_state["silkscreen"]["both"]["alpha_step"] = 2
    viewer._project.viewer_settings["overlay_state"] = {
        "silkscreen": {
            "split": False,
            "both": {"vis": "rails", "solid": True, "alpha_step": 3},
        },
    }
    PdnViewer._load_overlay_visibility_from_project(viewer)
    both = viewer._overlay_state["silkscreen"]["both"]
    assert both["vis"] == "rails"
    assert both["alpha_step"] == 3


def test_store_viewer_settings_saves_overlay_state_without_colors() -> None:
    viewer = _OverlayViewerStub()
    viewer._overlay_state["pads"]["both"]["vis"] = "all"
    viewer._overlay_colors = {}
    proj = ProjectFile()
    PdnViewer._store_viewer_settings(viewer, proj)
    saved = proj.viewer_settings.get("overlay_state")
    assert isinstance(saved, dict)
    assert saved["pads"]["both"]["vis"] == "all"
    assert "overlay_colors" not in proj.viewer_settings
