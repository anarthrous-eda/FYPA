"""Relief view - the 3D copper lifted by its heatmap value.

The mesh is lifted on the GPU (vertex shader); everything CPU-side that must
land on that surface - the hover ray-pick and the SOURCE / SINK markers -
goes through ``_relief_lift_for_value``. These tests pin that the CPU mirror
matches the shader's mapping and that the ray-pick finds the lifted surface,
not the flat layer plane underneath it.
"""
from __future__ import annotations

import math

import numpy as np

import pytest

pytest.importorskip("PySide6")


@pytest.fixture(scope="module")
def qapp():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


class _FakeGL:
    """Stands in for GLMeshViewer: a fixed relief span and a straight
    oblique camera ray x = x0 + k * z, y = 0."""

    def __init__(self, span: float, x0: float = 0.0, k: float = 1.0):
        self.span = span
        self.x0 = x0
        self.k = k

    def relief_z_span(self) -> float:
        return self.span

    def screen_to_world_at_z(self, _px, _py, z):
        return self.x0 + self.k * z, 0.0


@pytest.fixture
def viewer(qapp):
    from PySide6.QtWidgets import QMainWindow
    import fypa.altium_viewer as V

    v = V.PdnViewer.__new__(V.PdnViewer)
    QMainWindow.__init__(v)
    v._vmin, v._vmax = 0.0, 10.0
    v._log_active = False
    v._log_floor = 1e-12
    v._gl_viewer = _FakeGL(span=2.0)
    return v


def test_gl_relief_span_is_displayed_height_over_exaggeration(qapp):
    from fypa.gl_mesh_viewer import GLMeshViewer

    gl = GLMeshViewer()
    gl.set_relief_height(30.0)
    # 2D: never lifted, whatever the height.
    assert gl.relief_z_span() == 0.0
    gl._view_mode = "3d"
    gl.set_vertical_exaggeration(10.0)
    assert gl.relief_z_span() == pytest.approx(3.0)
    # Layer spacing changes the pre-exaggeration span, not the displayed
    # height (span * exaggeration stays 30 mm).
    gl.set_vertical_exaggeration(20.0)
    assert gl.relief_z_span() * 20.0 == pytest.approx(30.0)
    gl.set_relief_height(0.0)
    assert gl.relief_z_span() == 0.0
    gl.set_relief_height(float("nan"))
    assert gl.relief_z_span() == 0.0


def test_lift_mirrors_shader_normalisation_and_clamp(viewer):
    # span 2.0 over levels 0..10 → 0.2 per unit, clamped at both ends
    # exactly like the shader's clamp(v_norm, 0, 1).
    assert viewer._relief_lift_for_value(0.0) == pytest.approx(0.0)
    assert viewer._relief_lift_for_value(5.0) == pytest.approx(1.0)
    assert viewer._relief_lift_for_value(10.0) == pytest.approx(2.0)
    assert viewer._relief_lift_for_value(-3.0) == 0.0
    assert viewer._relief_lift_for_value(42.0) == pytest.approx(2.0)
    assert viewer._relief_lift_for_value(float("nan")) == 0.0
    viewer._gl_viewer.span = 0.0
    assert viewer._relief_lift_for_value(5.0) == 0.0


def test_lift_follows_log_scale(viewer):
    viewer._log_active = True
    viewer._log_floor = 1e-6
    viewer._vmin, viewer._vmax = 1.0, 100.0
    # log10: 10 is halfway between 1 and 100.
    assert viewer._relief_lift_for_value(10.0) == pytest.approx(1.0)


def test_ray_pick_hits_lifted_surface_not_layer_plane(viewer):
    # Field v(x) = x on copper 0 <= x <= 10 (levels 0..10, span 2 →
    # surface z = 0.2 x). Ray x = 4 + z meets it at z = 0.2 (4 + z)
    # → z = 1.0, x = 5. The flat-plane answer would be x = 4.
    viewer._gl_viewer = _FakeGL(span=2.0, x0=4.0, k=1.0)

    def sample(_lp, x, _y):
        return float(x) if 0.0 <= x <= 10.0 else None

    viewer._sample_layer_probe = sample
    hit = viewer._relief_ray_hit({}, 0, 0, z_base=0.0, span=2.0)
    assert hit is not None
    v, x, _y, z = hit
    assert z == pytest.approx(1.0, abs=1e-3)
    assert x == pytest.approx(5.0, abs=1e-3)
    assert v == pytest.approx(5.0, abs=1e-3)


def test_ray_pick_respects_layer_base_z(viewer):
    # Same geometry on a layer whose plane sits at z = -1.
    viewer._gl_viewer = _FakeGL(span=2.0, x0=4.0, k=1.0)
    viewer._sample_layer_probe = (
        lambda _lp, x, _y: float(x) if 0.0 <= x <= 10.0 else None)
    hit = viewer._relief_ray_hit({}, 0, 0, z_base=-1.0, span=2.0)
    assert hit is not None
    # z = -1 + 0.2 (4 + z) → z = -0.25, x = 3.75.
    assert hit[1] == pytest.approx(3.75, abs=1e-3)


def test_ray_pick_misses_off_copper(viewer):
    viewer._gl_viewer = _FakeGL(span=2.0, x0=50.0, k=1.0)
    viewer._sample_layer_probe = (
        lambda _lp, x, _y: float(x) if 0.0 <= x <= 10.0 else None)
    assert viewer._relief_ray_hit({}, 0, 0, z_base=0.0, span=2.0) is None


def test_ray_pick_takes_nearest_crossing(viewer):
    # A ridge (v = 10 on 2 <= x <= 3, else 0) in front of a flat floor.
    # Ray x = 2.5 + 0.1 z from above: crosses the ridge top (z = 2) at
    # x ≈ 2.7 before it would ever reach the floor.
    viewer._gl_viewer = _FakeGL(span=2.0, x0=2.5, k=0.1)

    def sample(_lp, x, _y):
        if not 0.0 <= x <= 10.0:
            return None
        return 10.0 if 2.0 <= x <= 3.0 else 0.0

    viewer._sample_layer_probe = sample
    hit = viewer._relief_ray_hit({}, 0, 0, z_base=0.0, span=2.0)
    assert hit is not None
    assert hit[0] == pytest.approx(10.0)
    assert math.isclose(hit[1], 2.7, abs_tol=0.05)


def _relief_widgets(viewer):
    from PySide6.QtWidgets import QCheckBox, QComboBox, QSlider
    from fypa.viewer.display import _VIA_CURRENT_MODE

    viewer.view_3d_box = QCheckBox()
    viewer.relief_combo = QComboBox()
    viewer.relief_combo.addItems(["Off", "Per layer", "Ignoring layers"])
    viewer.relief_height_slider = QSlider()
    viewer.relief_height_slider.setRange(1, 100)
    viewer.relief_height_slider.setValue(25)
    viewer.mode_combo = QComboBox()
    viewer.mode_combo.addItems(["Voltage", _VIA_CURRENT_MODE])
    viewer._data_bounds = (0.0, 80.0, 0.0, 40.0)
    return _VIA_CURRENT_MODE


def test_relief_height_off_in_via_current_and_2d(viewer):
    via_current = _relief_widgets(viewer)

    viewer.relief_combo.setCurrentIndex(1)
    assert viewer._relief_height_mm() == 0.0          # still 2D
    viewer.view_3d_box.setChecked(True)
    assert viewer._relief_height_mm() == pytest.approx(20.0)  # 25% of 80
    viewer.relief_combo.setCurrentIndex(2)            # same height
    assert viewer._relief_height_mm() == pytest.approx(20.0)
    viewer.mode_combo.setCurrentText(via_current)
    assert viewer._relief_height_mm() == 0.0
    viewer.mode_combo.setCurrentText("Voltage")
    viewer.relief_combo.setCurrentIndex(0)
    assert viewer._relief_height_mm() == 0.0


def test_hotkey_cycles_relief_modes(viewer):
    _relief_widgets(viewer)
    viewer._hotkey_toggle_relief()      # from 2D: 3D + per layer
    assert viewer.view_3d_box.isChecked()
    assert viewer._relief_mode() == viewer._RELIEF_PER_LAYER
    viewer._hotkey_toggle_relief()
    assert viewer._relief_mode() == viewer._RELIEF_IGNORE_LAYERS
    viewer._hotkey_toggle_relief()
    assert viewer._relief_mode() == viewer._RELIEF_OFF
    viewer._hotkey_toggle_relief()
    assert viewer._relief_mode() == viewer._RELIEF_PER_LAYER


def test_gl_ignoring_layers_collapses_stack_onto_top_layer(qapp):
    from fypa.gl_mesh_viewer import GLMeshViewer

    gl = GLMeshViewer()
    gl._view_mode = "3d"
    gl.set_vertical_exaggeration(10.0)
    gl.set_relief_height(30.0)                 # span 3.0 pre-exaggeration
    # Per layer: layer planes untouched.
    assert gl.relief_layer_z(-1.2) == pytest.approx(-1.2)
    assert gl.relief_flat_z(-1.2) == pytest.approx(-1.2)
    assert not gl.relief_ignores_layers()

    # Stack from z = 0 (top) to z = -1.6 (bottom).
    gl.set_relief_layers_ignored(True, pivot_z=0.0, stack_depth=1.6)
    assert gl.relief_ignores_layers()
    top = gl.relief_layer_z(0.0)
    mid = gl.relief_layer_z(-0.8)
    bot = gl.relief_layer_z(-1.6)
    assert top == pytest.approx(0.0)
    # Whole stack squeezed into the tie-break sliver (2% of the span),
    # still in stackup order so equal values don't z-fight.
    assert top - bot == pytest.approx(
        GLMeshViewer._RELIEF_TIEBREAK_FRAC * 3.0)
    assert top > mid > bot
    # The sliver is far smaller than a whole-scale step, so a bottom-layer
    # point 10% up the scale is drawn above a top-layer point at 0%.
    assert bot + 0.1 * gl.relief_z_span() > top
    # Value-less stub copper sits on the common base.
    assert gl.relief_flat_z(-1.6) == pytest.approx(0.0)

    # 2D or relief off: no squash.
    gl.set_relief_height(0.0)
    assert gl.relief_layer_z(-1.6) == pytest.approx(-1.6)
    assert not gl.relief_ignores_layers()


class _FakeGLLayers(_FakeGL):
    """_FakeGL plus the pieces _probe_at_point_3d needs: a camera far up
    the ray and an ignoring-layers style base map."""

    def __init__(self, bases, **kw):
        super().__init__(**kw)
        self.bases = bases

    def relief_layer_z(self, layer_z):
        return self.bases[layer_z]

    def vertical_exaggeration(self):
        return 1.0

    def _camera_position(self):
        zc = 100.0
        return self.x0 + self.k * zc, 0.0, zc


def test_hover_picks_nearest_surface_not_top_layer(viewer):
    # Top layer at 2 V, bottom layer at 8 V (levels 0..10, span 2). With
    # layer positions ignored the bottom layer's surface (z ≈ 1.6) sits
    # above the top layer's (z ≈ 0.4); the cursor must report it even
    # though the probe walk lists the top layer first.
    viewer._gl_viewer = _FakeGLLayers(
        bases={0.0: 0.0, -1.0: -0.04}, span=2.0, x0=5.0, k=0.2)
    top = {"physical": "Top", "prepared_shape": object(), "v": 2.0}
    bot = {"physical": "Bot", "prepared_shape": object(), "v": 8.0}
    viewer._layer_probes = [top, bot]
    viewer._layer_z_for = lambda phys: {"Top": 0.0, "Bot": -1.0}[phys]
    viewer._sample_layer_probe = (
        lambda lp, x, _y: lp["v"] if 0.0 <= x <= 10.0 else None)
    hit = viewer._probe_at_point_3d(0, 0)
    assert hit is not None
    v, lp = hit
    assert lp is bot
    assert v == pytest.approx(8.0)
    # Hit point on the bottom surface: z = 1.56 → x = 5 + 0.2 * 1.56.
    assert viewer._last_probe_3d_xy[0] == pytest.approx(5.312, abs=1e-3)


def _square_with_hole_mesh():
    """A 3x3 grid of unit squares (2 triangles each) with the centre
    square removed: outer boundary 12 edges, hole boundary 4 edges."""
    xs, ys = np.meshgrid(np.arange(4.0), np.arange(4.0))
    xs = xs.ravel()
    ys = ys.ravel()
    tris = []
    for j in range(3):
        for i in range(3):
            if (i, j) == (1, 1):
                continue
            a = j * 4 + i
            b, c, d = a + 1, a + 4, a + 5
            tris += [(a, b, d), (a, d, c)]
    return xs, ys, np.array(tris, dtype=np.int32)


def test_boundary_edges_are_outer_edge_and_holes(viewer):
    xs, ys, tris = _square_with_hole_mesh()
    viewer._layer_geometry = lambda _i: geom
    geom = {"xs": xs, "ys": ys, "tris": tris}
    edges = viewer._layer_boundary_edges(0)
    assert edges.shape == (16, 2)
    # Every boundary edge is a unit-length grid edge, none is a diagonal.
    lengths = np.hypot(xs[edges[:, 0]] - xs[edges[:, 1]],
                       ys[edges[:, 0]] - ys[edges[:, 1]])
    assert np.allclose(lengths, 1.0)
    # The hole's four edges are in there.
    hole = {(5, 6), (6, 10), (9, 10), (5, 9)}
    assert hole <= {tuple(e) for e in edges.tolist()}
    # Cached on the geometry entry.
    assert viewer._layer_boundary_edges(0) is edges


def test_relief_outline_values_match_mesh_values(viewer):
    xs, ys, tris = _square_with_hole_mesh()
    geom = {"xs": xs, "ys": ys, "tris": tris}
    viewer._layer_geometry = lambda _i: geom
    vs = xs * 10.0 + ys                      # distinct value per vertex
    lp = {"layer_index": 0, "values": vs, "value_offset": 2.5}
    segs, vals = viewer._relief_outline_segments(lp)
    assert segs.shape == (32, 2) and vals.shape == (32,)
    # Each endpoint carries its own vertex's (shifted) value, so the
    # shader lifts the outline exactly as it lifts the copper there.
    expect = segs[:, 0] * 10.0 + segs[:, 1] - 2.5
    assert np.allclose(vals, expect)
    # Log scale: values go through the same transform as the mesh's.
    viewer._log_active = True
    viewer._log_floor = 1e-6
    _, log_vals = viewer._relief_outline_segments(lp)
    assert np.allclose(log_vals, np.log10(np.maximum(expect, 1e-6)),
                       atol=1e-6)
    # Values that don't line up with the mesh → drawn flat, not wrong.
    lp_bad = dict(lp, values=vs[:-1])
    _, none_vals = viewer._relief_outline_segments(lp_bad)
    assert none_vals is None
