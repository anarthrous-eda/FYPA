"""Pure camera-matrix helpers for 3Dconnexion NavLib integration.

NavLib exchanges 4×4 row-major camera pose matrices (camera-to-world).
FYPA's :class:`~fypa.gl_mesh_viewer.GLMeshViewer` stores either 2D
orthographic state (centre + mm/pixel) or a 3D orbital camera.  These
functions convert between the two representations without Qt or OpenGL.
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fypa.gl_mesh_viewer import GLMeshViewer

Matrix4 = list[list[float]]

# Margin around the board for Fit (2D mpp fit, 3D distance, NavLib model extents).
NAVLIB_FIT_PADDING: float = 1.15


def _identity() -> Matrix4:
    return [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]


def _translation(x: float, y: float, z: float) -> Matrix4:
    m = _identity()
    m[0][3] = x
    m[1][3] = y
    m[2][3] = z
    return m


def _look_at_camera_to_world(
    eye: tuple[float, float, float],
    center: tuple[float, float, float],
    up: tuple[float, float, float],
) -> Matrix4:
    """Build a camera-to-world matrix matching Qt ``lookAt`` conventions."""
    ex, ey, ez = eye
    cx, cy, cz = center
    ux, uy, uz = up

    fx, fy, fz = cx - ex, cy - ey, cz - ez
    flen = math.hypot(fx, fy, fz)
    if flen < 1e-12:
        return _translation(ex, ey, ez)
    fx, fy, fz = fx / flen, fy / flen, fz / flen

    # right = forward × up
    rx = fy * uz - fz * uy
    ry = fz * ux - fx * uz
    rz = fx * uy - fy * ux
    rlen = math.hypot(rx, ry, rz)
    if rlen < 1e-12:
        return _translation(ex, ey, ez)
    rx, ry, rz = rx / rlen, ry / rlen, rz / rlen

    # true up = right × forward
    ux2 = ry * fz - rz * fy
    uy2 = rz * fx - rx * fz
    uz2 = rx * fy - ry * fx

    # View matrix rows are [right, up, -forward, translation_in_view_space]
    # Camera-to-world is the inverse: columns are right, up, -forward, eye.
    return [
        [rx, ux2, -fx, ex],
        [ry, uy2, -fy, ey],
        [rz, uz2, -fz, ez],
        [0.0, 0.0, 0.0, 1.0],
    ]


def camera_matrix_2d(
    center_x: float,
    center_y: float,
    mm_per_pixel: float,
    width_px: int,
    height_px: int,
) -> Matrix4:
    """Camera-to-world matrix for the 2D orthographic top-down view."""
    half_w = max(1, width_px) * mm_per_pixel * 0.5
    half_h = max(1, height_px) * mm_per_pixel * 0.5
    # Eye above the board centre, looking down −Z with world +Z up.
    eye = (center_x, center_y, max(half_w, half_h, 1.0))
    center = (center_x, center_y, 0.0)
    return _look_at_camera_to_world(eye, center, (0.0, 1.0, 0.0))


def parse_camera_matrix_2d(
    matrix: Matrix4,
    width_px: int,
    height_px: int,
) -> tuple[float, float, float]:
    """Recover (center_x, center_y, mm_per_pixel) from a 2D camera matrix."""
    cx, cy = parse_camera_center_2d(matrix)
    # Distance along view axis encodes scale; use eye Z as a fallback scale hint.
    eye_z = abs(matrix[2][3])
    w_px = max(1, width_px)
    h_px = max(1, height_px)
    # Derive mpp from the horizontal extent implied by eye height and FOV-like
    # factor — when NavLib only pans, mpp is unchanged; when it zooms via
    # view_extents we rely on zoom_mpp_from_view_extents_2d instead.
    mpp = max(eye_z / max(w_px, h_px) * 2.0, 1e-9)
    return float(cx), float(cy), float(mpp)


def parse_camera_center_2d(matrix: Matrix4) -> tuple[float, float]:
    """Board-plane (z=0) look-at point from a NavLib orthographic camera matrix."""
    ex, ey, ez = matrix[0][3], matrix[1][3], matrix[2][3]
    fx, fy, fz = -matrix[0][2], -matrix[1][2], -matrix[2][2]
    if abs(fz) < 1e-9:
        return float(ex), float(ey)
    t = -ez / fz
    return ex + t * fx, ey + t * fy


def adjust_ortho_navlib_camera(
    matrix: Matrix4,
    scene_center: tuple[float, float, float],
    scene_radius: float,
) -> Matrix4:
    """Shift orthographic eye along view axis (3DxWare / Cura workaround)."""
    m = [list(row) for row in matrix]
    dx, dy, dz = -m[0][2], -m[1][2], -m[2][2]
    dlen = math.hypot(dx, dy, dz)
    if dlen < 1e-12:
        return m
    dx, dy, dz = dx / dlen, dy / dlen, dz / dlen

    ex, ey, ez = m[0][3], m[1][3], m[2][3]
    scx, scy, scz = scene_center
    vx, vy, vz = scx - ex, scy - ey, scz - ez
    vlen = math.hypot(vx, vy, vz)
    if vlen < 1e-12:
        return m
    cos_value = (dx * vx + dy * vy + dz * vz) / vlen

    offset = 0.0
    if vlen < scene_radius and cos_value > 0.0:
        offset = scene_radius
    elif vlen < scene_radius and cos_value < 0.0:
        offset = 2.0 * scene_radius
    elif vlen > scene_radius and cos_value < 0.0:
        offset = 2.0 * vlen

    m[0][3] -= offset * dx
    m[1][3] -= offset * dy
    m[2][3] -= offset * dz
    return m


def view_extents_2d(
    center_x: float,
    center_y: float,
    mm_per_pixel: float,
    width_px: int,
    height_px: int,
    *,
    z_pad: float = 9001.0,
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """NavLib orthographic view box (min, max) in world mm."""
    half_w = max(1, width_px) * mm_per_pixel * 0.5
    half_h = max(1, height_px) * mm_per_pixel * 0.5
    pmin = (center_x - half_w, center_y - half_h, -z_pad)
    pmax = (center_x + half_w, center_y + half_h, z_pad)
    return pmin, pmax


def apply_view_extents_2d(
    pmin: tuple[float, float, float],
    pmax: tuple[float, float, float],
    width_px: int,
    height_px: int,
) -> tuple[float, float, float]:
    """Convert NavLib view extents back to (center_x, center_y, mm_per_pixel)."""
    w_px = max(1, width_px)
    h_px = max(1, height_px)
    cx = (pmin[0] + pmax[0]) * 0.5
    cy = (pmin[1] + pmax[1]) * 0.5
    mpp = zoom_mpp_from_view_extents_2d(pmin, pmax, w_px, h_px)
    return cx, cy, mpp


def zoom_mpp_from_view_extents_2d(
    pmin: tuple[float, float, float],
    pmax: tuple[float, float, float],
    width_px: int,
    height_px: int,
) -> float:
    """Recover mm/pixel from NavLib orthographic view extents (zoom only)."""
    w_px = max(1, width_px)
    h_px = max(1, height_px)
    half_w = (pmax[0] - pmin[0]) * 0.5
    half_h = (pmax[1] - pmin[1]) * 0.5
    mpp_w = half_w / (w_px * 0.5)
    mpp_h = half_h / (h_px * 0.5)
    return max((mpp_w + mpp_h) * 0.5, 1e-9)


def camera_position_3d(
    target: tuple[float, float, float],
    yaw_deg: float,
    pitch_deg: float,
    distance: float,
) -> tuple[float, float, float]:
    """World-space camera position (matches GLMeshViewer spherical model)."""
    tx, ty, tz = target
    yaw = math.radians(yaw_deg)
    pitch = math.radians(pitch_deg)
    cosp = math.cos(pitch)
    x = tx + distance * cosp * math.sin(yaw)
    y = ty - distance * cosp * math.cos(yaw)
    z = tz + distance * math.sin(pitch)
    return x, y, z


def orbital_from_camera_position(
    target: tuple[float, float, float],
    cam_pos: tuple[float, float, float],
) -> tuple[float, float, float]:
    """Derive (yaw_deg, pitch_deg, distance) from target and camera position."""
    tx, ty, tz = target
    dx = cam_pos[0] - tx
    dy = cam_pos[1] - ty
    dz = cam_pos[2] - tz
    dist = math.hypot(dx, dy, dz)
    if dist < 1e-9:
        return 0.0, 89.0, 1.0
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, dz / dist))))
    yaw = math.degrees(math.atan2(dx, -dy))
    return yaw, pitch, dist


def camera_matrix_3d(
    target: tuple[float, float, float],
    yaw_deg: float,
    pitch_deg: float,
    distance: float,
) -> Matrix4:
    """Camera-to-world matrix for the 3D orbital camera."""
    eye = camera_position_3d(target, yaw_deg, pitch_deg, distance)
    return _look_at_camera_to_world(eye, target, (0.0, 0.0, 1.0))


def parse_camera_matrix_3d(
    matrix: Matrix4,
    target: tuple[float, float, float],
) -> tuple[float, float, float]:
    """Recover (yaw_deg, pitch_deg, distance) given a fixed look-at target."""
    cam_pos = (matrix[0][3], matrix[1][3], matrix[2][3])
    return orbital_from_camera_position(target, cam_pos)


def look_at_on_plane_from_camera_matrix(
    matrix: Matrix4,
    plane_z: float = 0.0,
) -> tuple[float, float, float]:
    """World look-at where the camera forward ray hits ``z = plane_z``."""
    ex, ey, ez = matrix[0][3], matrix[1][3], matrix[2][3]
    fx, fy, fz = -matrix[0][2], -matrix[1][2], -matrix[2][2]
    if abs(fz) < 1e-9:
        return float(ex), float(ey), float(plane_z)
    t = (plane_z - ez) / fz
    return ex + t * fx, ey + t * fy, float(plane_z)


def parse_camera_pose_3d(
    matrix: Matrix4,
    *,
    plane_z: float = 0.0,
) -> tuple[float, float, float, tuple[float, float, float]]:
    """Recover (yaw, pitch, distance, look_at) from a NavLib camera matrix."""
    target = look_at_on_plane_from_camera_matrix(matrix, plane_z)
    yaw, pitch, dist = parse_camera_matrix_3d(matrix, target)
    return yaw, pitch, dist, target


def perspective_frustum_at_near(
    fov_deg: float,
    aspect: float,
    near: float,
    *,
    far: float = 1e7,
) -> tuple[float, float, float, float, float, float]:
    """NavLib frustum planes at the near clip (world mm), KiCad-style.

  ``left/right/top/bottom`` are half-extents on the near plane, not raw
  ``tan(fov/2)`` slopes — NavLib Fit uses these to pick camera distance.
    """
    fov_h_half = math.radians(fov_deg) * 0.5
    half_h = near * math.tan(fov_h_half)
    half_w = half_h * max(aspect, 1e-9)
    return (-half_w, half_w, -half_h, half_h, near, far)


def perspective_fit_distance_mm(
    board_w: float,
    board_h: float,
    fov_deg: float,
    aspect: float,
    *,
    padding: float = NAVLIB_FIT_PADDING,
) -> float:
    """Camera distance to frame a board rectangle in perspective (top-down)."""
    fov_h_half = math.radians(fov_deg) * 0.5
    tan_h = math.tan(fov_h_half)
    tan_w = math.tan(math.atan(tan_h * max(aspect, 1e-9)))
    dist_h = (board_h * padding * 0.5) / max(tan_h, 1e-9)
    dist_w = (board_w * padding * 0.5) / max(tan_w, 1e-9)
    return max(dist_h, dist_w, 1.0)


def view_extents_3d(
    target: tuple[float, float, float],
    distance: float,
    fov_deg: float,
    width_px: int,
    height_px: int,
    *,
    z_pad: float = 9001.0,
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """NavLib view box for a perspective camera (visible frustum at target)."""
    tx, ty, tz = target
    aspect = max(width_px, 1) / max(height_px, 1)
    fov_h_half = math.radians(fov_deg) * 0.5
    half_h = distance * math.tan(fov_h_half)
    half_w = half_h * aspect
    return (tx - half_w, ty - half_h, tz - z_pad), (tx + half_w, ty + half_h, tz + z_pad)


def model_extents_from_bounds(
    bounds: tuple[float, float, float, float] | None,
    z_max: float = 0.0,
    *,
    z_pad: float = 1.0,
    padding: float = NAVLIB_FIT_PADDING,
    perspective_3d: bool = False,
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """NavLib model AABB from FYPA bounds (x_min, x_max, y_min, y_max).

    ``padding`` inflates the XY box so NavLib's built-in Fit command leaves
    the same margin as :meth:`~fypa.gl_mesh_viewer.GLMeshViewer.fit_to_bounds`.

    When ``perspective_3d`` is true the XY box is expanded to the bounding
    sphere of the padded board rectangle so NavLib's perspective Fit frames
    the full board at any orbit angle.
    """
    if bounds is None:
        b = (-10.0, 10.0, -10.0, 10.0)
    else:
        b = bounds
    x_min, x_max, y_min, y_max = b
    cx = (x_min + x_max) * 0.5
    cy = (y_min + y_max) * 0.5
    board_w = max(x_max - x_min, 1e-9)
    board_h = max(y_max - y_min, 1e-9)
    half_w = board_w * 0.5 * padding
    half_h = board_h * 0.5 * padding
    if perspective_3d:
        half = math.hypot(half_w, half_h)
        half_w = half_h = half
    z_top = max(float(z_max), z_pad)
    return (cx - half_w, cy - half_h, -z_pad), (cx + half_w, cy + half_h, z_top)


def snapshot_navlib_state(viewer: GLMeshViewer) -> dict:
    """Capture viewer camera state for round-trip / tests."""
    if viewer.view_mode() == "3d":
        tx, ty, tz = viewer._cam_target  # noqa: SLF001 — test helper
        return {
            "mode": "3d",
            "target": (tx, ty, tz),
            "yaw": viewer._cam_yaw_deg,
            "pitch": viewer._cam_pitch_deg,
            "distance": viewer._cam_distance,
        }
    return {
        "mode": "2d",
        "center_x": viewer._view_center_x,
        "center_y": viewer._view_center_y,
        "mm_per_pixel": viewer._mm_per_pixel,
    }
