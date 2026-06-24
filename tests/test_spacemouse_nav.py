"""Tests for 3Dconnexion NavLib camera bridge and SpaceMouse controller."""
from __future__ import annotations

import math
from unittest.mock import MagicMock, patch

import pytest

from fypa.navlib_camera import (
    apply_view_extents_2d,
    camera_matrix_2d,
    camera_matrix_3d,
    camera_position_3d,
    model_extents_from_bounds,
    orbital_from_camera_position,
    parse_camera_center_2d,
    parse_camera_matrix_3d,
    NAVLIB_FIT_PADDING,
    perspective_fit_distance_mm,
    perspective_frustum_at_near,
    view_extents_2d,
    zoom_mpp_from_view_extents_2d,
)
from fypa.spacemouse_nav import FypaNavlibClient, SpaceMouseController

# The NavLib client class only exists when the optional `pynavlib` backend is
# installed (Windows/macOS + `uv sync --extra spacemouse`). CI syncs without
# the extra, so guard the tests that poke the client directly.
_needs_navlib = pytest.mark.skipif(
    FypaNavlibClient is None,
    reason="pynavlib backend not installed (uv sync --extra spacemouse)",
)


class TestNavlibCamera2D:
    WIDTH = 800
    HEIGHT = 600

    def test_view_extents_roundtrip(self):
        cx, cy, mpp = 12.5, -3.0, 0.05
        pmin, pmax = view_extents_2d(cx, cy, mpp, self.WIDTH, self.HEIGHT)
        cx2, cy2, mpp2 = apply_view_extents_2d(
            pmin, pmax, self.WIDTH, self.HEIGHT,
        )
        assert cx2 == pytest.approx(cx)
        assert cy2 == pytest.approx(cy)
        assert mpp2 == pytest.approx(mpp)

    def test_camera_matrix_preserves_center(self):
        cx, cy, mpp = 100.0, 50.0, 0.1
        m = camera_matrix_2d(cx, cy, mpp, self.WIDTH, self.HEIGHT)
        cx2, cy2 = parse_camera_center_2d(m)
        assert cx2 == pytest.approx(cx)
        assert cy2 == pytest.approx(cy)

    def test_ortho_navlib_split_pan_and_zoom(self):
        """Pan via camera matrix, zoom via view extents — must not overwrite."""
        from unittest.mock import MagicMock

        from fypa.gl_mesh_viewer import GLMeshViewer

        viewer = MagicMock()
        viewer._view_mode = "2d"
        viewer._view_center_x = 100.0
        viewer._view_center_y = 50.0
        viewer._mm_per_pixel = 0.1
        viewer.width.return_value = self.WIDTH
        viewer.height.return_value = self.HEIGHT
        viewer.navlib_scene_center_radius.return_value = (
            (100.0, 50.0, 0.0), 50.0,
        )

        panned = camera_matrix_2d(120.0, 70.0, 0.1, self.WIDTH, self.HEIGHT)
        GLMeshViewer.apply_navlib_camera_matrix(viewer, panned)
        assert viewer._view_center_x == pytest.approx(120.0)
        assert viewer._view_center_y == pytest.approx(70.0)

        pmin, pmax = view_extents_2d(120.0, 70.0, 0.05, self.WIDTH, self.HEIGHT)
        GLMeshViewer.apply_navlib_view_extents(viewer, pmin, pmax)
        viewer.set_view_center_scale.assert_called_once_with(120.0, 70.0, 0.05)

        stale = camera_matrix_2d(100.0, 50.0, 0.2, self.WIDTH, self.HEIGHT)
        GLMeshViewer.apply_navlib_camera_matrix(viewer, stale)
        assert viewer._view_center_x == pytest.approx(100.0)
        assert viewer._view_center_y == pytest.approx(50.0)
        viewer.set_view_center_scale.assert_called_once()

    def test_zoom_mpp_from_view_extents(self):
        cx, cy, mpp = 0.0, 0.0, 0.08
        pmin, pmax = view_extents_2d(cx, cy, mpp, self.WIDTH, self.HEIGHT)
        assert zoom_mpp_from_view_extents_2d(
            pmin, pmax, self.WIDTH, self.HEIGHT,
        ) == pytest.approx(mpp)

    def test_model_extents_padding(self):
        bounds = (0.0, 100.0, 0.0, 50.0)
        pmin, pmax = model_extents_from_bounds(bounds, z_max=0.0)
        assert pmax[0] - pmin[0] == pytest.approx(100.0 * NAVLIB_FIT_PADDING)
        assert pmax[1] - pmin[1] == pytest.approx(50.0 * NAVLIB_FIT_PADDING)

    def test_model_extents_3d_diagonal_margin(self):
        bounds = (0.0, 100.0, 0.0, 50.0)
        pmin, pmax = model_extents_from_bounds(
            bounds, z_max=0.0, perspective_3d=True,
        )
        half_w = 100.0 * 0.5 * NAVLIB_FIT_PADDING
        half_h = 50.0 * 0.5 * NAVLIB_FIT_PADDING
        half = math.hypot(half_w, half_h)
        assert pmax[0] - pmin[0] == pytest.approx(2.0 * half)
        assert pmax[1] - pmin[1] == pytest.approx(2.0 * half)

    def test_perspective_fit_distance(self):
        dist = perspective_fit_distance_mm(100.0, 50.0, 35.0, 4.0 / 3.0)
        # Width-limited for a 4:3 viewport and wide board.
        fov_h_half = math.radians(35.0) * 0.5
        tan_w = math.tan(math.atan(math.tan(fov_h_half) * (4.0 / 3.0)))
        expected = (100.0 * NAVLIB_FIT_PADDING * 0.5) / tan_w
        assert dist == pytest.approx(expected)

    def test_perspective_frustum_at_near_matches_projection(self):
        near = 12.5
        left, right, bottom, top, near_d, far_d = perspective_frustum_at_near(
            35.0, 16.0 / 9.0, near, far=5000.0,
        )
        fov_h_half = math.radians(35.0) * 0.5
        assert top - bottom == pytest.approx(2.0 * near * math.tan(fov_h_half))
        assert right - left == pytest.approx(
            (top - bottom) * (16.0 / 9.0),
        )
        assert near_d == near
        assert far_d == 5000.0


class TestNavlibCamera3D:
    TARGET = (10.0, 20.0, 0.0)

    def test_orbital_roundtrip(self):
        yaw, pitch, dist = 30.0, 45.0, 500.0
        m = camera_matrix_3d(self.TARGET, yaw, pitch, dist)
        yaw2, pitch2, dist2 = parse_camera_matrix_3d(m, self.TARGET)
        assert yaw2 == pytest.approx(yaw, abs=1e-6)
        assert pitch2 == pytest.approx(pitch, abs=1e-6)
        assert dist2 == pytest.approx(dist, abs=1e-3)

    def test_camera_position_matches_spherical(self):
        yaw, pitch, dist = 0.0, 89.0, 200.0
        pos = camera_position_3d(self.TARGET, yaw, pitch, dist)
        yaw2, pitch2, dist2 = orbital_from_camera_position(self.TARGET, pos)
        assert yaw2 == pytest.approx(yaw, abs=1e-4)
        assert pitch2 == pytest.approx(pitch, abs=1e-3)
        assert dist2 == pytest.approx(dist, abs=1e-3)


class TestSpaceMouseController:
    def test_unavailable_without_backends(self):
        viewer = MagicMock()
        with patch("fypa.spacemouse_nav._PYNAVLIB_AVAILABLE", False), patch(
            "fypa.spacemouse_nav.sys.platform", "win32",
        ), patch("fypa.spacemouse_nav._LinuxSpnavPoller") as mock_linux:
            mock_linux.return_value.available.return_value = False
            ctrl = SpaceMouseController(viewer, lambda: None)
            assert not ctrl.available()

    def test_window_activate_enables_navlib(self):
        top = MagicMock()
        ctrl = SpaceMouseController.__new__(SpaceMouseController)
        ctrl._top_level = top
        ctrl._navlib_client = MagicMock()
        ctrl._linux = None
        ctrl._active = False
        ctrl.set_active = SpaceMouseController.set_active.__get__(ctrl)

        from PySide6.QtCore import QEvent
        activate = MagicMock()
        activate.type.return_value = QEvent.Type.WindowActivate
        SpaceMouseController.eventFilter(ctrl, top, activate)
        assert ctrl._active
        ctrl._navlib_client.enable_navigation.assert_called_once_with(True)


class TestNavlib3DPan:
    def test_pivot_before_matrix_preserves_pan_center(self):
        from fypa.gl_mesh_viewer import GLMeshViewer
        from fypa.navlib_camera import camera_matrix_3d

        viewer = MagicMock()
        viewer._view_mode = "3d"
        viewer._cam_target = (0.0, 0.0, 0.0)
        viewer._cam_yaw_deg = 0.0
        viewer._cam_pitch_deg = 89.0
        viewer._cam_distance = 500.0

        target = (10.0, 20.0, 0.0)
        m = camera_matrix_3d(target, 0.0, 89.0, 500.0)

        GLMeshViewer.apply_navlib_3d_pose(viewer, target, m)
        assert viewer._cam_target == (10.0, 20.0, 0.0)
        assert viewer._cam_distance == pytest.approx(500.0, rel=1e-3)

    def test_matrix_does_not_override_pivot_when_navlib_disagrees(self):
        """Pivot wins when NavLib sends a new pivot but matrix still references old center."""
        from fypa.gl_mesh_viewer import GLMeshViewer
        from fypa.navlib_camera import camera_matrix_3d, parse_camera_pose_3d

        viewer = MagicMock()
        viewer._view_mode = "3d"
        viewer._cam_yaw_deg = 30.0
        viewer._cam_pitch_deg = 45.0
        viewer._cam_distance = 200.0

        old_center = (0.0, 0.0, 0.0)
        new_pivot = (50.0, -25.0, 0.0)
        m = camera_matrix_3d(old_center, 30.0, 45.0, 200.0)
        _, _, _, ray_target = parse_camera_pose_3d(m)
        assert ray_target == pytest.approx(old_center)

        GLMeshViewer.apply_navlib_3d_pose(viewer, new_pivot, m)
        assert viewer._cam_target == new_pivot
    @_needs_navlib
    def test_key_press_does_not_trigger_app_fit(self):
        client = MagicMock()
        client._gui = MagicMock()
        from fypa.spacemouse_nav import FypaNavlibClient, _V3DK_FIT

        FypaNavlibClient.set_key_press(client, _V3DK_FIT)
        client._gui.fit_requested.emit.assert_not_called()

    @_needs_navlib
    def test_active_command_does_not_trigger_fit(self):
        client = MagicMock()
        client._gui = MagicMock()
        from fypa.spacemouse_nav import FypaNavlibClient

        FypaNavlibClient.set_active_command(client, "ViewFrame")
        FypaNavlibClient.set_active_command(client, "Navigation.Frame")
        client._gui.fit_requested.emit.assert_not_called()
