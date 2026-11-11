"""The per-layer all-copper view (the Physical layers control's second eye).

On a large board every toggle used to rebuild and re-upload one buffer holding
all the visible layers' copper — seconds per click. Each layer now owns a GPU
buffer that is uploaded once and then only drawn or skipped, so these tests
pin the properties that make a toggle free:

* a layer's geometry is handed to the GL viewer exactly once, however often
  its eye, colour, transparency or selection changes;
* what is drawn still follows the old rules — copper on a visible rail is
  left to the heatmap, net focus removes the other nets, a selection
  highlight dims them;
* a layer whose triangles are still being prepared in the background shows
  up when they arrive, and the background result is the same triangles the
  in-process path makes.
"""
from __future__ import annotations

import time

import numpy as np
import pytest

pytest.importorskip("PySide6")

from fypa.viewer import copper_tris as CT  # noqa: E402
from fypa.viewer.copper_layers import (  # noqa: E402
    _CopperLayersMixin,
    _LayerCopper,
    _ranges,
)


@pytest.fixture(scope="module")
def qapp():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


# --- a board small enough to reason about -----------------------------------

def _square(x: float, y: float, size: float = 1.0) -> dict:
    ring = np.array([[x, y], [x + size, y], [x + size, y + size],
                     [x, y + size], [x, y]], dtype=np.float32)
    return {"exterior": ring, "holes": []}


def _board() -> dict:
    """Two layers. Layer 1 carries GND (2 squares), +3V3 (1) and two pieces
    of unnamed copper; layer 2 carries GND only."""
    return {"all_copper": [
        {"layer_id": 1, "net": "GND",
         "polygons": [_square(0, 0), _square(2, 0)]},
        {"layer_id": 1, "net": "+3V3", "polygons": [_square(4, 0)]},
        {"layer_id": 1, "net": "(none)",
         "polygons": [_square(6, 0), _square(8, 0)]},
        {"layer_id": 2, "net": "GND", "polygons": [_square(0, 3, 5.0)]},
    ]}


class _Eye:
    def __init__(self, on: bool) -> None:
        self.on = on

    def isVisibleState(self) -> bool:
        return self.on


class _Fill:
    def __init__(self, solid: bool = True) -> None:
        self.solid = solid

    def isSolid(self) -> bool:
        return self.solid


class _Transp:
    def __init__(self, a: float = 1.0) -> None:
        self.a = a

    def alpha(self) -> float:
        return self.a


class _Box:
    def __init__(self, checked: bool = False) -> None:
        self.checked = checked

    def isChecked(self) -> bool:
        return self.checked


class _StatusBar:
    def __init__(self) -> None:
        self.message = ""

    def showMessage(self, text: str, *_a) -> None:
        self.message = text

    def clearMessage(self) -> None:
        self.message = ""


class _FakeGL:
    """Stands in for GLMeshViewer's copper-buffer API and counts uploads."""

    def __init__(self) -> None:
        self.bufs: dict = {}
        self.uploads: list = []
        self.draws: list = []

    def has_copper_geometry(self, key, version=None) -> bool:
        return key in self.bufs and self.bufs[key][0] == version

    def set_copper_geometry(self, key, xy, version=None) -> None:
        self.bufs[key] = (version, np.asarray(xy))
        self.uploads.append(key)

    def retain_copper_geometry(self, keep) -> None:
        for key in [k for k in self.bufs if k not in keep]:
            del self.bufs[key]

    def clear_copper_geometry(self) -> None:
        self.bufs.clear()
        self.draws = []

    def set_copper_draws(self, draws) -> None:
        self.draws = list(draws)

    def drawn_vertices(self, key) -> int:
        n = self.bufs[key][1].shape[0]
        total = 0
        for d in self.draws:
            if d.key != key:
                continue
            for _rgba, firsts, counts in d.passes:
                total += n if firsts is None else int(np.sum(counts))
        return total


def _host(qapp, metadata=None):
    from PySide6.QtCore import QObject
    from fypa.viewer.editor.nets import _EditorNetsMixin
    from fypa.viewer.editor.selection import _EditorSelectionMixin
    from fypa.viewer.markers import _MarkerOverlayMixin
    from fypa.viewer.panels.overlays import _OverlayPanelMixin

    class Host(_CopperLayersMixin, QObject):
        _SELECTED_LAYER_UNSEL_ALPHA = 1.0
        _COPPER_BG_MIN_POLYGONS = 10 ** 9   # in-process unless a test opts in
        _triangulate_stub = _MarkerOverlayMixin._triangulate_stub
        _merged_solid_all_copper_tris = (
            _MarkerOverlayMixin._merged_solid_all_copper_tris)
        _triangulate_simple_polygon = (
            _MarkerOverlayMixin._triangulate_simple_polygon)
        _merged_copper_rings = staticmethod(
            _MarkerOverlayMixin._merged_copper_rings)
        _copper_poly_wire = _OverlayPanelMixin._copper_poly_wire
        _editor_dim_rgb = _EditorSelectionMixin._editor_dim_rgb
        _editor_focus_active = _EditorNetsMixin._editor_focus_active

        def __init__(self) -> None:
            super().__init__()
            self.metadata = metadata if metadata is not None else _board()
            self._gl_viewer = _FakeGL()
            self._phys_name_to_layer_id = {"Top": 1, "Bottom": 2}
            self._phys_stackup_rank = {"Top": 0, "Bottom": 1}
            self._layer_eye2_buttons = [("Top", _Eye(True)),
                                        ("Bottom", _Eye(False))]
            self._layer_fill_buttons = [("Top", _Fill()), ("Bottom", _Fill())]
            self._layer_transparency_buttons = [("Top", _Transp()),
                                                ("Bottom", _Transp())]
            self.view_3d_box = _Box(False)
            self._selected_layer = None
            self._editor_mode = False
            self._editor_highlight_nets = set()
            self._editor_highlight_polys = set()
            self._editor_focus_nets = frozenset()
            self._editor_focus_polys = frozenset()
            self._merged_all_copper_cache = {}
            self._rail_nets: list[str] = []
            self._status = _StatusBar()

        def _visible_rails(self):
            return ["rail"] if self._rail_nets else []

        def _effective_rail_members(self, _rails):
            return list(self._rail_nets)

        def _layer_color_for(self, name):
            return {"Top": "#ff0000", "Bottom": "#0000ff"}[name]

        def _layer_render_z(self, name):
            return {"Top": 0.0, "Bottom": -1.0}[name]

        def _selected_layer_dim_rgb(self, rgb):
            return tuple(0.5 * c for c in rgb)

        def _pump_busy_ui(self):
            pass

        def _run_with_busy_popup(self, work):
            work()

        def statusBar(self):
            return self._status

    return Host()


def _eye(host, name):
    return dict(host._layer_eye2_buttons)[name]


# --- the point of the change: a toggle uploads nothing ----------------------

def test_a_layers_geometry_is_uploaded_once_however_often_it_is_toggled(qapp):
    host = _host(qapp)
    gl = host._gl_viewer
    host._refresh_copper_layers()
    assert gl.uploads == [("Top", CT.SOLID)]
    assert [d.key for d in gl.draws] == [("Top", CT.SOLID)]

    for on in (False, True, False, True):
        _eye(host, "Top").on = on
        host._refresh_copper_layers()
    assert gl.uploads == [("Top", CT.SOLID)], "a toggle re-uploaded geometry"
    assert [d.key for d in gl.draws] == [("Top", CT.SOLID)]


def test_a_hidden_layer_keeps_its_buffer_and_is_simply_not_drawn(qapp):
    host = _host(qapp)
    gl = host._gl_viewer
    host._refresh_copper_layers()
    _eye(host, "Top").on = False
    host._refresh_copper_layers()
    assert gl.draws == []
    assert ("Top", CT.SOLID) in gl.bufs


def test_selection_recolours_without_touching_geometry(qapp):
    host = _host(qapp)
    gl = host._gl_viewer
    _eye(host, "Bottom").on = True
    host._refresh_copper_layers()
    uploads = list(gl.uploads)
    # Bottom-up so the top layer paints last.
    assert [d.key[0] for d in gl.draws] == ["Bottom", "Top"]

    host._selected_layer = "Bottom"
    host._refresh_copper_layers()
    assert gl.uploads == uploads
    # The selected layer is forced last (on top) in 2D, the other is dimmed.
    assert [d.key[0] for d in gl.draws] == ["Top", "Bottom"]
    top, bottom = gl.draws
    assert top.passes[0][0][:3] == (0.5, 0.0, 0.0)
    assert bottom.passes[0][0][:3] == (0.0, 0.0, 1.0)


def test_3d_changes_only_the_pass_not_the_buffers(qapp):
    host = _host(qapp)
    gl = host._gl_viewer
    host._refresh_copper_layers()
    assert gl.draws[0].under is True      # 2D opaque: before the mesh
    host.view_3d_box.checked = True
    host._refresh_copper_layers()
    assert gl.draws[0].under is False
    assert gl.uploads == [("Top", CT.SOLID)]


def test_switching_fill_style_swaps_the_buffer(qapp):
    host = _host(qapp)
    gl = host._gl_viewer
    host._refresh_copper_layers()
    dict(host._layer_fill_buttons)["Top"].solid = False
    host._refresh_copper_layers()
    assert [d.key for d in gl.draws] == [("Top", CT.WIRE)]
    # The style no longer in use is freed rather than held alongside.
    assert ("Top", CT.SOLID) not in gl.bufs


def test_transparent_solid_fill_uses_the_merged_buffer_after_the_mesh(qapp):
    host = _host(qapp)
    gl = host._gl_viewer
    dict(host._layer_transparency_buttons)["Top"].a = 0.5
    host._refresh_copper_layers()
    (draw,) = gl.draws
    assert draw.key == ("Top", "merged")
    assert draw.under is False
    assert draw.passes[0][0][3] == 0.5


# --- what is drawn still follows the old per-polygon rules ------------------

def _top_counts(host):
    """Vertex count of each net's copper on the top layer."""
    lc = host._copper_layer_index()[1]
    off = lc.offsets[CT.SOLID]
    out: dict = {}
    for i, ni in enumerate(lc.poly_net.tolist()):
        out[lc.nets[ni]] = out.get(lc.nets[ni], 0) + int(off[i + 1] - off[i])
    return out


def test_copper_on_a_visible_rail_is_left_to_the_heatmap(qapp):
    host = _host(qapp)
    gl = host._gl_viewer
    host._refresh_copper_layers()
    per_net = _top_counts(host)
    total = sum(per_net.values())
    assert gl.drawn_vertices(("Top", CT.SOLID)) == total

    host._rail_nets = ["GND"]
    host._refresh_copper_layers()
    assert gl.drawn_vertices(("Top", CT.SOLID)) == total - per_net["GND"]
    assert gl.uploads == [("Top", CT.SOLID)]

    # Editor mode shows all the physical copper, rails included.
    host._editor_mode = True
    host._refresh_copper_layers()
    assert gl.drawn_vertices(("Top", CT.SOLID)) == total


def test_net_focus_draws_only_the_focused_copper(qapp):
    host = _host(qapp)
    gl = host._gl_viewer
    host._refresh_copper_layers()
    per_net = _top_counts(host)
    host._editor_mode = True
    host._editor_focus_nets = frozenset({"+3V3"})
    host._refresh_copper_layers()
    assert gl.drawn_vertices(("Top", CT.SOLID)) == per_net["+3V3"]

    # Unnamed copper is focused by polygon identity: one of the two pieces.
    unnamed = host.metadata["all_copper"][2]["polygons"]
    host._editor_focus_nets = frozenset()
    host._editor_focus_polys = frozenset({(1, id(unnamed[0]))})
    host._refresh_copper_layers()
    assert (gl.drawn_vertices(("Top", CT.SOLID))
            == per_net["(none)"] // 2)

    # A real net's polygon is never rescued by identity alone.
    gnd = host.metadata["all_copper"][0]["polygons"]
    host._editor_focus_polys = frozenset({(1, id(gnd[0]))})
    host._refresh_copper_layers()
    assert gl.draws == []


def test_selection_highlight_dims_the_other_copper(qapp):
    host = _host(qapp)
    gl = host._gl_viewer
    host._refresh_copper_layers()
    per_net = _top_counts(host)
    total = sum(per_net.values())
    host._editor_mode = True
    host._editor_highlight_nets = {"+3V3"}
    host._refresh_copper_layers()
    (draw,) = gl.draws
    dim, lit = draw.passes
    assert lit[0][:3] == (1.0, 0.0, 0.0)
    assert dim[0][:3] != lit[0][:3]
    assert int(np.sum(lit[2])) == per_net["+3V3"]
    assert int(np.sum(dim[2])) == total - per_net["+3V3"]
    assert gl.uploads == [("Top", CT.SOLID)]


def test_ranges_merge_neighbouring_polygons():
    offsets = np.array([0, 3, 9, 9, 12, 18])
    firsts, counts = _ranges(
        np.array([True, True, False, True, False]), offsets)
    assert firsts.tolist() == [0, 9]
    assert counts.tolist() == [9, 3]
    firsts, counts = _ranges(np.zeros(5, dtype=bool), offsets)
    assert firsts.size == 0 and counts.size == 0


def test_polygons_are_grouped_by_net():
    recs = _board()["all_copper"][:3]
    lc = _LayerCopper(1, [recs[1], recs[0], recs[2]])
    names = [lc.nets[i] for i in lc.poly_net.tolist()]
    assert names == sorted(names)
    assert lc.unnamed[lc.net_index["(none)"]]
    assert not lc.unnamed[lc.net_index["GND"]]


# --- a re-solve keeps the triangles ------------------------------------------

def test_a_resolve_reuses_triangles_for_unchanged_copper(qapp):
    host = _host(qapp)
    host._refresh_copper_layers()
    old = host.metadata["all_copper"][0]["polygons"][0]["_tris_cache"]

    fresh = _board()
    fresh["all_copper"][1]["polygons"][0] = _square(4, 0, 2.0)   # +3V3 moved
    host.metadata = fresh
    host._copper_layer_index()
    assert fresh["all_copper"][0]["polygons"][0]["_tris_cache"] is old
    assert "_tris_cache" not in fresh["all_copper"][1]["polygons"][0]


# --- background triangulation -------------------------------------------------

def test_a_chunk_matches_the_in_process_builders():
    polys = [_square(0, 0), _square(3, 1, 2.0),
             {"exterior": np.empty((0, 2), dtype=np.float32), "holes": []}]
    hole = np.array([[1, 1], [2, 1], [2, 2], [1, 2], [1, 1]],
                    dtype=np.float32)
    polys.append({"exterior": _square(10, 0, 4.0)["exterior"],
                  "holes": [hole + 10 * np.array([1, 0], dtype=np.float32)]})
    for kind in (CT.SOLID, CT.WIRE):
        xy, counts = CT.build_chunk(
            kind, [(p["exterior"], p["holes"]) for p in polys])
        parts = [CT.build_polygon(kind, p["exterior"], p["holes"])
                 for p in polys]
        assert counts.tolist() == [a.shape[0] for a in parts]
        assert np.array_equal(xy, np.concatenate(parts))
        assert xy.shape[0] % 3 == 0


def test_a_layer_still_being_prepared_appears_when_it_is_ready(qapp):
    """End to end through real worker processes: the layer is not drawn
    while its triangles are outstanding, says so in the status bar, and is
    drawn — with the same triangles the in-process path makes — once the
    workers deliver."""
    host = _host(qapp)
    host._COPPER_BG_MIN_POLYGONS = 1
    gl = host._gl_viewer
    try:
        host._refresh_copper_layers()
        assert gl.draws == []
        assert host._copper_waiting == ("Top",)
        assert "Top" in host._status.message

        deadline = time.monotonic() + 120
        while host._copper_waiting and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert host._copper_waiting == ()
        assert host._status.message == ""
        assert [d.key for d in gl.draws] == [("Top", CT.SOLID)]

        # The hidden layer was prepared too, without ever being asked for.
        while (host._copper_triangulator.busy()
               and time.monotonic() < deadline):
            qapp.processEvents()
            time.sleep(0.01)
        for rec in host.metadata["all_copper"]:
            for poly in rec["polygons"]:
                want = CT.triangulate_rings(poly["exterior"], poly["holes"])
                assert np.array_equal(poly["_tris_cache"], want)

        # A transparent solid fill needs the layer's copper merged into one
        # shape — slow on a real board, so it is handed to the workers too.
        # Until it arrives the layer is drawn from its ordinary buffer
        # rather than disappearing.
        dict(host._layer_transparency_buttons)["Top"].a = 0.5
        host._refresh_copper_layers()
        assert host._copper_waiting == ("Top",)
        assert [d.key for d in gl.draws] == [("Top", CT.SOLID)]
        assert gl.draws[0].under is False
        while host._copper_waiting and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert host._copper_waiting == ()
        assert [d.key for d in gl.draws] == [("Top", "merged")]
        rings = [(p["exterior"], p["holes"])
                 for rec in host.metadata["all_copper"][:3]
                 for p in rec["polygons"]]
        assert np.array_equal(gl.bufs[("Top", "merged")][1],
                              CT.merged_triangles(rings))
    finally:
        host._shutdown_copper_layers()
    assert host._copper_triangulator is None


def test_a_dead_worker_pool_falls_back_to_in_process(qapp):
    host = _host(qapp)
    host._COPPER_BG_MIN_POLYGONS = 1
    gl = host._gl_viewer
    try:
        host._refresh_copper_layers()
        assert host._copper_waiting == ("Top",)
        host._copper_triangulator._fail(RuntimeError("worker crashed"))
        assert host._copper_waiting == ()
        assert [d.key for d in gl.draws] == [("Top", CT.SOLID)]
    finally:
        host._shutdown_copper_layers()


# --- via markers ---------------------------------------------------------------

def test_via_positions_match_the_per_via_filter(qapp):
    """The per-layer via filter is array masks over a column index now; it
    must select exactly what the per-via loop did."""
    from fypa.viewer.viewport import _ViewportMixin

    rng = np.random.default_rng(1)
    nets = ["GND", "+3V3", "(none)", None]
    vias = []
    for i in range(400):
        a, b = sorted(rng.integers(1, 9, size=2).tolist())
        if i % 7 == 0:
            a, b = b, a                       # reversed spans occur
        vias.append({
            "layer_start": a, "layer_end": b,
            "x_mm": float(rng.random()), "y_mm": float(rng.random()),
            "hole_diameter_mm": float(rng.choice([0.0, 0.2, 0.3])),
            "diameter_mm": float(rng.choice([0.0, 0.5])),
            "net": nets[i % len(nets)],
        })
    vias.append({"layer_start": None, "layer_end": 3, "net": "GND"})

    class Stub(_ViewportMixin):
        _VIA_MARKER_FALLBACK_DIAM_MM = 0.3
        _editor_mode = False
        _editor_focus_nets = frozenset()
        _editor_focus_polys = frozenset()

        def _editor_focus_active(self):
            return bool(self._editor_mode and self._editor_focus_nets)

    s = Stub()
    s.metadata = {"vias": vias}

    def reference(layer, members, scoped):
        xs, ys, ds = [], [], []
        for v in vias:
            ls, le = v.get("layer_start"), v.get("layer_end")
            if ls is None or le is None:
                continue
            lo, hi = (ls, le) if ls <= le else (le, ls)
            if not (lo <= layer <= hi):
                continue
            if scoped and v.get("net") not in members:
                continue
            if s._focus_hides_net(v.get("net")):
                continue
            xs.append(v.get("x_mm", 0.0))
            ys.append(v.get("y_mm", 0.0))
            ds.append(s._via_marker_diameter_mm(v))
        return xs, ys, ds

    for focus in (frozenset(), frozenset({"+3V3"})):
        s._editor_mode = bool(focus)
        s._editor_focus_nets = focus
        for layer in (1, 4, 8):
            for members, scoped in ((set(), False), ({"GND"}, True),
                                    (set(), True)):
                got = s._collect_via_positions(
                    layer, members, rail_scoped=scoped)
                assert got == reference(layer, members, scoped)
