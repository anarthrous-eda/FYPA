"""3Dconnexion SpaceMouse integration for the FYPA GL viewer.

Windows and macOS use the official NavLib via :mod:`pynavlib` (requires
3DxWare).  Linux polls ``libspnav`` through the ``spacenavd`` daemon and
applies axis motion directly to :class:`~fypa.gl_mesh_viewer.GLMeshViewer`.

Install the optional extra on Windows/macOS::

    uv sync --extra spacemouse

Linux additionally needs system packages::

    sudo apt install spacenavd libspnav0
"""
from __future__ import annotations

import ctypes
import logging
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QElapsedTimer, QEvent, QTimer, Signal

if TYPE_CHECKING:
    from fypa.gl_mesh_viewer import GLMeshViewer

_log = logging.getLogger(__name__)

_PYNAVLIB_AVAILABLE = False
_pynav = None

if sys.platform in ("win32", "darwin"):
    try:
        import pynavlib.pynavlib_interface as _pynav
        _PYNAVLIB_AVAILABLE = True
    except ImportError:
        _pynav = None


# ---------------------------------------------------------------------------
# Linux libspnav (ctypes)
# ---------------------------------------------------------------------------

class _SpnavMotionEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("z", ctypes.c_int),
        ("rx", ctypes.c_int),
        ("ry", ctypes.c_int),
        ("rz", ctypes.c_int),
        ("period", ctypes.c_uint),
    ]


class _SpnavButtonEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("bnum", ctypes.c_int),
        ("press", ctypes.c_int),
    ]


class _SpnavEvent(ctypes.Union):
    _fields_ = [
        ("motion", _SpnavMotionEvent),
        ("button", _SpnavButtonEvent),
    ]


class _SpnavEventStruct(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("u", _SpnavEvent),
    ]


_SPNAV_EVENT_MOTION = 1
_SPNAV_EVENT_BUTTON = 2
_SPNAV_MAX = 32767


def _load_libspnav():
    try:
        lib = ctypes.CDLL("libspnav.so.0")
    except OSError:
        try:
            lib = ctypes.CDLL("libspnav.so")
        except OSError:
            return None
    lib.spnav_open.restype = ctypes.c_int
    lib.spnav_close.restype = ctypes.c_int
    lib.spnav_poll_event.argtypes = [ctypes.POINTER(_SpnavEventStruct)]
    lib.spnav_poll_event.restype = ctypes.c_int
    return lib


class _LinuxSpnavPoller(QObject):
    """Poll ``spacenavd`` and forward normalised axis motion to the viewer."""

    def __init__(
        self,
        viewer: GLMeshViewer,
        on_fit: Callable[[], None],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._viewer = viewer
        self._on_fit = on_fit
        self._lib = _load_libspnav()
        self._open = False
        self._enabled = False
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._poll)
        self._elapsed = QElapsedTimer()

    def available(self) -> bool:
        return self._lib is not None

    def start(self) -> bool:
        if not self._lib or self._open:
            return self._open
        if self._lib.spnav_open() != 0:
            _log.info("SpaceMouse: spnav_open failed (is spacenavd running?)")
            return False
        self._open = True
        self._elapsed.start()
        _log.info("SpaceMouse: libspnav connected")
        return True

    def stop(self) -> None:
        self._timer.stop()
        if self._open and self._lib:
            self._lib.spnav_close()
        self._open = False

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        if enabled and self._open:
            self._elapsed.restart()
            self._timer.start()
        else:
            self._timer.stop()

    def _poll(self) -> None:
        if not self._enabled or not self._lib or not self._open:
            return
        dt = self._elapsed.restart() / 1000.0
        ev = _SpnavEventStruct()
        while self._lib.spnav_poll_event(ctypes.byref(ev)):
            if ev.type == _SPNAV_EVENT_MOTION:
                m = ev.u.motion
                scale = 1.0 / _SPNAV_MAX
                self._viewer.apply_spacemouse_motion(
                    m.x * scale, m.y * scale, m.z * scale,
                    m.rx * scale, m.ry * scale, m.rz * scale,
                    dt,
                )
            elif ev.type == _SPNAV_EVENT_BUTTON and ev.u.button.press:
                if ev.u.button.bnum == 0:
                    self._on_fit()


# ---------------------------------------------------------------------------
# NavLib client (Windows / macOS via pynavlib)
# ---------------------------------------------------------------------------

_V3DK_FIT = 2  # 3DxWare virtual key — menu button / palette Fit


class _NavlibGuiBridge(QObject):
    """Marshals NavLib callbacks (any thread) onto the Qt GUI thread."""

    camera_matrix_ready = Signal(list)
    view_extents_ready = Signal(object, object)
    pivot_ready = Signal(float, float, float)
    fit_requested = Signal()

    def __init__(
        self,
        viewer: GLMeshViewer,
        on_fit: Callable[[], None],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._viewer = viewer
        self._on_fit = on_fit
        self._pending_pivot: tuple[float, float, float] | None = None
        self._pending_matrix: list[list[float]] | None = None
        self._flush_scheduled = False
        self.camera_matrix_ready.connect(self._apply_camera_matrix)
        self.view_extents_ready.connect(self._apply_view_extents)
        self.pivot_ready.connect(self._apply_pivot)
        self.fit_requested.connect(self._on_fit)

    def _schedule_navlib_3d_flush(self) -> None:
        if self._flush_scheduled:
            return
        self._flush_scheduled = True
        QTimer.singleShot(0, self._flush_navlib_3d)

    def _flush_navlib_3d(self) -> None:
        self._flush_scheduled = False
        if self._viewer.view_mode() != "3d":
            return
        pivot = self._pending_pivot
        matrix = self._pending_matrix
        self._pending_pivot = None
        self._pending_matrix = None
        if pivot is None and matrix is None:
            return
        self._viewer.apply_navlib_3d_pose(pivot, matrix)

    def _apply_camera_matrix(self, matrix: list[list[float]]) -> None:
        if self._viewer.view_mode() == "3d":
            self._pending_matrix = matrix
            self._schedule_navlib_3d_flush()
            return
        self._viewer.apply_navlib_camera_matrix(matrix)

    def _apply_view_extents(
        self,
        pmin: tuple[float, float, float],
        pmax: tuple[float, float, float],
    ) -> None:
        self._viewer.apply_navlib_view_extents(pmin, pmax)

    def _apply_pivot(self, x: float, y: float, z: float) -> None:
        if self._viewer.view_mode() == "3d":
            self._pending_pivot = (x, y, z)
            self._schedule_navlib_3d_flush()
            return
        self._viewer.apply_navlib_pivot(x, y, z)


if _PYNAVLIB_AVAILABLE and _pynav is not None:

    class FypaNavlibClient(_pynav.NavlibNavigationModel):
        """NavLib adapter — camera sync with :class:`GLMeshViewer`."""

        def __init__(self, viewer: GLMeshViewer,
                     on_fit: Callable[[], None]) -> None:
            super().__init__(False, _pynav.NavlibOptions.RowMajorOrder)
            self._viewer = viewer
            self._on_fit = on_fit
            self._gui = _NavlibGuiBridge(viewer, on_fit)
            self.put_profile_hint("FYPA")
            self._scene_center = (0.0, 0.0, 0.0)
            self._scene_radius = 10.0

        def _vec(self, xyz: tuple[float, float, float]):
            return _pynav.NavlibVector(xyz[0], xyz[1], xyz[2])

        def _box(self, pmin, pmax):
            return _pynav.NavlibBox(self._vec(pmin), self._vec(pmax))

        def _matrix(self, m: list[list[float]]):
            return _pynav.NavlibMatrix(m)

        def get_pointer_position(self):
            px, py, pz = self._viewer.navlib_pointer_world()
            return self._vec((px, py, pz))

        def get_view_extents(self):
            return self._box(*self._viewer.navlib_view_extents())

        def set_view_extents(self, extents) -> None:
            pmin = (extents._min._x, extents._min._y, extents._min._z)
            pmax = (extents._max._x, extents._max._y, extents._max._z)
            self._gui.view_extents_ready.emit(pmin, pmax)

        def get_view_frustum(self):
            w_px = max(1, int(
                self._viewer.width() - self._viewer._legend_right_inset,
            ))
            h_px = max(1, self._viewer.height())
            if self._viewer.view_mode() == "3d":
                from fypa.navlib_camera import perspective_frustum_at_near

                near = max(self._viewer._cam_distance * 0.01, 0.1)
                far = max(self._viewer._cam_distance * 100.0, near + 1.0)
                left, right, bottom, top, near_d, far_d = (
                    perspective_frustum_at_near(
                        self._viewer._cam_fov_deg,
                        w_px / h_px,
                        near,
                        far=far,
                    )
                )
                return _pynav.NavlibFrustum(
                    left, right, bottom, top, near_d, far_d,
                )
            pmin, pmax = self._viewer.navlib_view_extents()
            half_w = (pmax[0] - pmin[0]) * 0.5
            half_h = (pmax[1] - pmin[1]) * 0.5
            return _pynav.NavlibFrustum(
                -half_w, half_w, -half_h, half_h, -9001.0, 9001.0,
            )

        def get_is_view_perspective(self) -> bool:
            return self._viewer.view_mode() == "3d"

        def get_is_view_rotatable(self) -> bool:
            return self._viewer.view_mode() == "3d"

        def get_view_fov(self) -> float:
            import math
            if self._viewer.view_mode() == "3d":
                return math.radians(self._viewer._cam_fov_deg)
            return 1.0

        def get_view_focus_distance(self) -> float:
            if self._viewer.view_mode() == "3d":
                return self._viewer._cam_distance
            return 1.0

        def get_camera_target(self):
            return self._vec(self._viewer.navlib_pivot_world())

        def get_floor_plane(self):
            return _pynav.NavlibPlane(self._vec((0.0, 0.0, 1.0)), 0.0)

        def get_view_construction_plane(self):
            return self.get_floor_plane()

        def get_selection_extents(self):
            return self.get_model_extents()

        def get_selection_transform(self):
            return self._matrix([
                [1, 0, 0, 0],
                [0, 1, 0, 0],
                [0, 0, 1, 0],
                [0, 0, 0, 1],
            ])

        def get_is_selection_empty(self) -> bool:
            return True

        def get_pivot_visible(self) -> bool:
            return False

        def get_camera_matrix(self):
            return self._matrix(self._viewer.navlib_camera_matrix())

        def set_camera_matrix(self, matrix) -> None:
            # NavLib may call from a worker thread; emit plain lists so Qt
            # can queue the slot on the GUI thread (invokeMethod cannot pass
            # NavlibMatrix to @Slot handlers).
            rows = [list(row) for row in matrix._matrix]
            self._gui.camera_matrix_ready.emit(rows)

        def get_coordinate_system(self):
            return self._matrix([
                [1, 0, 0, 0],
                [0, 1, 0, 0],
                [0, 0, 1, 0],
                [0, 0, 0, 1],
            ])

        def get_front_view(self):
            return self.get_coordinate_system()

        def get_model_extents(self):
            pmin, pmax = self._viewer.navlib_model_extents()
            cx = (pmin[0] + pmax[0]) * 0.5
            cy = (pmin[1] + pmax[1]) * 0.5
            cz = (pmin[2] + pmax[2]) * 0.5
            self._scene_center = (cx, cy, cz)
            dx = pmax[0] - cx
            dy = pmax[1] - cy
            dz = pmax[2] - cz
            self._scene_radius = max(
                (dx * dx + dy * dy + dz * dz) ** 0.5, 1.0,
            )
            return self._box(pmin, pmax)

        def get_pivot_position(self):
            return self._vec(self._viewer.navlib_pivot_world())

        def get_hit_look_at(self):
            return self._vec(self._viewer.navlib_pivot_world())

        def get_units_to_meters(self) -> float:
            return 0.001  # viewer works in mm

        def is_user_pivot(self) -> bool:
            return False

        def set_pivot_position(self, position) -> None:
            self._gui.pivot_ready.emit(
                position._x, position._y, position._z,
            )

        def set_camera_target(self, target) -> None:
            # KiCad returns function_not_supported — NavLib must not recenter
            # the look-at here; doing so snapped the view back after every pan.
            pass

        def set_key_press(self, vkey: int) -> None:
            # Fit is handled by NavLib via model_extents + set_camera_matrix.
            # Calling app fit here also fired after ordinary navigation.
            pass

        def set_active_command(self, commandId: str) -> None:
            # FYPA does not export custom palette commands.  NavLib may call
            # this with internal IDs during normal navigation — never treat
            # those as Fit (a loose "frame" substring match fired fit after
            # every pan/zoom).
            pass

        def set_motion_flag(self, motion: bool) -> None:
            pass

        def set_hit_selection_only(self, only_selection: bool) -> None:
            pass

else:
    FypaNavlibClient = None  # type: ignore[misc, assignment]


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class SpaceMouseController(QObject):
    """Attach SpaceMouse navigation to a :class:`GLMeshViewer`."""

    def __init__(
        self,
        viewer: GLMeshViewer,
        on_fit: Callable[[], None],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._viewer = viewer
        self._on_fit = on_fit
        self._navlib_client = None
        self._linux = None
        self._active = False
        self._logged_unavailable = False

        if _PYNAVLIB_AVAILABLE and FypaNavlibClient is not None:
            try:
                self._navlib_client = FypaNavlibClient(viewer, on_fit)
                self._navlib_client.enable_navigation(False)
                _log.info("SpaceMouse: pynavlib backend ready (3DxWare required)")
            except Exception as exc:
                _log.info("SpaceMouse: pynavlib init failed: %s", exc)
                self._navlib_client = None

        if sys.platform == "linux":
            self._linux = _LinuxSpnavPoller(viewer, on_fit, self)
            if self._linux.available() and self._linux.start():
                pass
            else:
                self._linux = None

        if not self._navlib_client and not self._linux:
            self._log_unavailable()

        # Track the top-level window, not GL-viewer widget focus.  Disabling
        # NavLib on FocusOut breaks 3DxWare: opening 3Dconnexion Settings (or
        # clicking elsewhere in FYPA) drops the FYPA profile and selects
        # Desktop instead.
        self._top_level = viewer.window()
        if self._top_level is not None:
            self._top_level.installEventFilter(self)
            if self._top_level.isActiveWindow():
                self.set_active(True)

    def available(self) -> bool:
        return self._navlib_client is not None or self._linux is not None

    def _log_unavailable(self) -> None:
        if self._logged_unavailable:
            return
        self._logged_unavailable = True
        if sys.platform in ("win32", "darwin"):
            _log.info(
                "SpaceMouse: not available — install 3DxWare and "
                "'uv sync --extra spacemouse' for pynavlib",
            )
        else:
            _log.info(
                "SpaceMouse: not available — install spacenavd + libspnav0",
            )

    def set_active(self, active: bool) -> None:
        if active == self._active:
            return
        self._active = active
        if self._navlib_client is not None:
            self._navlib_client.enable_navigation(active)
        if self._linux is not None:
            self._linux.set_enabled(active)

    def eventFilter(self, watched, event) -> bool:
        if watched is not self._top_level:
            return False
        if event.type() == QEvent.Type.WindowActivate:
            self.set_active(True)
        return False

    def shutdown(self) -> None:
        self.set_active(False)
        if self._navlib_client is not None:
            try:
                self._navlib_client.enable_navigation(False)
            except Exception:
                pass
        if self._linux is not None:
            self._linux.stop()
