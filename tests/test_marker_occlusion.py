"""Markers always on top (Settings > Display options) and the GL marker pass.

Markers are instanced GL quads shaded from a signed-distance glyph. When
not always on top they are depth-tested against the scene, so copper drawn
above a marker's layer hides the part of it that copper covers. These pin
the instance rows the pass is fed, which groups take part in the depth
test, and - where a GL context is available - the cut itself.
"""
from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("PySide6")


@pytest.fixture(scope="module")
def qapp():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def gl(qapp):
    from fypa.gl_mesh_viewer import GLMeshViewer
    v = GLMeshViewer()
    v.resize(400, 300)
    v._view_mode = "2d"
    yield v
    v.deleteLater()


def _group(xs, zs=None, **kw):
    from fypa.gl_mesh_viewer import MarkerGroup
    xs = np.asarray(xs, dtype=float)
    kw.setdefault("color", "#ff0000")
    kw.setdefault("symbol", "o")
    kw.setdefault("size", 10)
    return MarkerGroup(xs=xs, ys=np.zeros(xs.size),
                       zs=None if zs is None else np.asarray(zs, float), **kw)


def test_instances_cull_offscreen_and_keep_order(gl):
    from fypa.gl_mesh_viewer import _MARKER_SHAPES, _MARKER_STRIDE
    cx = gl._view_center_x
    gl._markers = [_group([cx, cx + 1e6], [0.0, 0.0]),
                   _group([cx], symbol="tri_up")]
    inst = gl._build_marker_instances()
    assert inst.shape == (2, _MARKER_STRIDE)
    assert inst[0, 6] == _MARKER_SHAPES["o"]
    assert inst[1, 6] == _MARKER_SHAPES["tri_up"]
    assert inst[0, 7] == pytest.approx(5.0)          # r = size / 2
    # Occludable only with a per-marker z (and the group flag).
    assert inst[0, 25] == 1.0 and inst[1, 25] == 0.0


def test_instances_ring_box_and_slot(gl):
    from fypa.gl_mesh_viewer import _MARKER_SHAPES
    cx = gl._view_center_x
    gl._markers = [
        _group([cx, cx], [0.0, 0.0], ring_colors=["#00ff00", None],
               ring_width=2.5),
        _group([cx], [0.0], symbol="tri_up_box", size=20,
               color="transparent", occludable=False),
        _group([cx], [0.0], world_diameters_mm=np.array([1.0]),
               world_obrounds=[(4.0, 1.0, 0.0, True)]),
    ]
    inst = gl._build_marker_instances()
    ring, no_ring, box, slot = inst
    assert ring[21] == pytest.approx(2.5) and tuple(ring[16:20]) == (
        0.0, 1.0, 0.0, 1.0)
    assert no_ring[21] == 0.0
    # tri_up_box: an off-centre rect hugging the triangle, fill clear.
    assert box[6] == _MARKER_SHAPES["_rect"] and box[11] == 0.0
    assert box[4] < 0.0                                  # shifted up
    assert box[25] == 0.0                                # not occludable
    # Rounded slot: a capsule 4 x 1 mm on screen, long axis along x.
    assert slot[6] == _MARKER_SHAPES["_capsule"]
    mpp = gl._mm_per_pixel
    assert slot[22] == pytest.approx(2.0 / mpp, rel=1e-3)
    assert slot[23] == pytest.approx(0.5 / mpp, rel=1e-3)


def test_needs_depth_follows_setting_program_and_groups(gl):
    gl._marker_program = object()           # stand-in: linked program
    assert gl.markers_always_on_top() is False
    gl._markers = [_group([0.0])]           # no z: never hidden
    assert not gl._markers_need_depth()
    gl._markers = [_group([0.0], [0.0], occludable=False)]
    assert not gl._markers_need_depth()
    gl._markers = [_group([0.0], [0.0])]
    assert gl._markers_need_depth()
    gl.set_markers_always_on_top(True)
    assert not gl._markers_need_depth()
    gl.set_markers_always_on_top(False)
    gl._marker_program = None               # QPainter fallback
    assert not gl._markers_need_depth()


def _render(qapp, on_top: bool, ss: bool):
    """Top copper over x in [0, 30]; a bottom-layer disc centred on its
    edge at x = 0. Returns (image, viewer) or None without a GL context."""
    from fypa.gl_mesh_viewer import GLMeshViewer
    v = GLMeshViewer()
    v.resize(320, 240)
    v.show()
    v.set_supersampling(ss)
    xs = np.array([0., 30., 0., 30., -30.])
    ys = np.array([-20., -20., 20., 20., 0.])
    tris = np.array([[0, 1, 2], [1, 3, 2]], dtype=np.int32)
    v.set_mesh(xs, ys, tris, zs=np.zeros(5, np.float32))
    v.fit_to_data()
    v.set_markers([_group([0.0], [-1.6], color="#0000ff", edge_width=0.0,
                          world_diameters_mm=np.array([10.0]))])
    v.set_markers_always_on_top(on_top)
    for _ in range(3):
        qapp.processEvents()
    img = v.grabFramebuffer()
    if v._marker_program is None:
        return None
    return img, v


@pytest.mark.parametrize("ss", [False, True])
def test_copper_above_cuts_the_marker_partially(qapp, ss):
    got = _render(qapp, on_top=False, ss=ss)
    if got is None:
        pytest.skip("no GL marker program in this environment")
    img, v = got
    sx, sy = v.world_to_screen(0.0, 0.0)
    off = 2.0 / v._mm_per_pixel             # 2 mm either side of the edge
    dpr = img.devicePixelRatio()            # image is in device pixels

    def px(image, x, y):
        return image.pixelColor(int(x * dpr), int(y * dpr))

    left = px(img, sx - off, sy)
    right = px(img, sx + off, sy)
    assert left.blue() > 200 and left.red() < 60     # uncovered half shows
    assert right.blue() < 200                        # covered half hidden
    img_top, _v_top = _render(qapp, on_top=True, ss=ss)
    assert px(img_top, sx + off, sy).blue() > 200    # always on top: whole
