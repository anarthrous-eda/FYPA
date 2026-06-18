"""Tests for 3Dconnexion NavLib camera bridge and SpaceMouse controller."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from fypa.navlib_camera import (
    apply_view_extents_2d,
    camera_matrix_2d,
    camera_matrix_3d,
    camera_position_3d,
    orbital_from_camera_position,
    parse_camera_matrix_2d,
    parse_camera_matrix_3d,
    view_extents_2d,
)
from fypa.spacemouse_nav import SpaceMouseController


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
        cx2, cy2, _ = parse_camera_matrix_2d(m, self.WIDTH, self.HEIGHT)
        assert cx2 == pytest.approx(cx)
        assert cy2 == pytest.approx(cy)


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
