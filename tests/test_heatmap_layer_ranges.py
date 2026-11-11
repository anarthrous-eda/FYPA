"""Heatmap layer toggles draw a different part of one uploaded mesh.

The heatmap batch used to hold only the visible layers, so every layer-eye
click rebuilt it and re-uploaded millions of vertices. It now holds every
layer of the selected rails; a click only changes which index runs are drawn.
These tests pin that, plus the two 3D helpers that were rewritten for speed
and must produce exactly what they did before.
"""
from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("PySide6")

from fypa.viewer import mesh_geometry as MG  # noqa: E402
from fypa.viewer.render import _RenderMixin  # noqa: E402


class _Box:
    def __init__(self, checked: bool = False) -> None:
        self.checked = checked

    def isChecked(self) -> bool:
        return self.checked


def _entry(n: int, seed: int) -> dict:
    rng = np.random.default_rng(seed)
    xs, ys = rng.random(n), rng.random(n)
    # A strip of triangles that leaves the last vertex unreferenced (an
    # "orphan", as the solver produces).
    tris = np.array([[i, i + 1, i + 2] for i in range(n - 3)], dtype=np.int32)
    return {
        "xs": xs, "ys": ys, "tris": tris, "vs": rng.random(n),
        "no_current_mask": np.zeros(n, dtype=np.float32),
        "used_indices": np.unique(tris),
        "bounds": (float(xs.min()), float(xs.max()),
                   float(ys.min()), float(ys.max())),
        "triangulation": None, "interpolator": None,
        "prepared_shape": None, "outline_segments": None,
    }


class _Stub(_RenderMixin):
    _COPPER_THICKNESS_MM = 0.035

    def __init__(self) -> None:
        self.solution = object()
        names = ["L1", "L2", "L3"]
        self._layer_eye_buttons = [(n, None) for n in names]
        self._phys_stackup_rank = {n: i for i, n in enumerate(names)}
        self._phys_name_to_layer_id = {n: i + 1 for i, n in enumerate(names)}
        self._index_by_pair = {(n, "VCC"): i for i, n in enumerate(names)}
        self._entries = {0: _entry(8, 0), 1: _entry(6, 1), 2: _entry(10, 2)}
        self._selected_layer = None
        self._rail_geom_cache = None
        self.view_3d_box = _Box(False)

    def _effective_rail_members(self, _rails):
        return ["VCC"]

    def _merged_hidden_sections(self):
        return {}

    def _layer_render_z(self, phys):
        return -float(self._phys_stackup_rank[phys])

    def _pump_busy_ui(self):
        pass

    def _layer_arrays(self, layer_index, _derive_fn):
        return self._entries[layer_index]

    def _layer_cache_key(self, layer_index, derive_fn):
        return (layer_index, id(derive_fn))


def _build(stub, visible):
    return stub._build_rail_arrays(visible, ["rail"], None)


def test_hiding_a_layer_reuses_the_uploaded_geometry():
    stub = _Stub()
    xs, ys, zs, vs, tris, probes, nc = _build(stub, ["L1", "L2", "L3"])
    assert xs.size == 8 + 6 + 10          # every layer is in the batch
    assert [p["physical"] for p in probes] == ["L1", "L2", "L3"]

    xs2, ys2, zs2, vs2, tris2, probes2, nc2 = _build(stub, ["L1", "L3"])
    # The very same arrays: _render_impl's identity check then skips the
    # GPU upload, which is the whole saving.
    assert xs2 is xs and ys2 is ys and zs2 is zs and tris2 is tris
    assert vs2.size == xs.size
    assert [p["physical"] for p in probes2] == ["L1", "L3"]
    assert [(b["physical"], b["visible"]) for b in stub._mesh_blocks] == [
        ("L3", True), ("L2", False), ("L1", True)]


def test_blocks_locate_each_layer_in_the_batch():
    stub = _Stub()
    xs, ys, zs, vs, tris, _probes, _nc = _build(stub, ["L2"])
    by_name = {b["physical"]: b for b in stub._mesh_blocks}
    # Drawn bottom-first: L3, L2, L1.
    assert [b["v0"] for b in stub._mesh_blocks] == [0, 10, 16]
    assert [b["t0"] for b in stub._mesh_blocks] == [0, 7, 10]
    b = by_name["L2"]
    block_tris = tris[b["t0"]:b["t0"] + b["n_tris"]]
    assert block_tris.min() >= b["v0"]
    assert block_tris.max() < b["v0"] + b["n_vertices"]
    # Used vertices are the block's own, minus the orphan.
    assert np.array_equal(b["used"] + b["v0"], np.unique(block_tris))
    assert "used" not in by_name["L1"]      # hidden: not part of the stats
    assert np.all(zs[b["v0"]:b["v0"] + b["n_vertices"]] == -1.0)


def test_3d_blocks_cover_both_faces_and_reuse_the_prism():
    stub = _Stub()
    stub.view_3d_box.checked = True
    xs, ys, zs, vs, tris, _probes, _nc = _build(stub, ["L1", "L2", "L3"])
    assert xs.size == 2 * (8 + 6 + 10)
    b = next(b for b in stub._mesh_blocks if b["physical"] == "L1")
    block_tris = tris[b["t0"]:b["t0"] + b["n_tris"]]
    assert np.array_equal(b["used"] + b["v0"], np.unique(block_tris))
    cached = dict(stub._prism_tris_cache)
    # A selection change rebuilds the batch (draw order changes) but not
    # the per-layer prism topology.
    stub._selected_layer = "L2"
    _build(stub, ["L1", "L2", "L3"])
    assert all(stub._prism_tris_cache[k] is v for k, v in cached.items())


def test_gl_viewer_draw_ranges_gate_the_mesh(monkeypatch):
    from fypa.gl_mesh_viewer import GLMeshViewer

    class Bare:
        _n_indices = 30
        _mesh_ranges = None
        update = staticmethod(lambda: None)
        set_mesh_draw_ranges = GLMeshViewer.set_mesh_draw_ranges
        _mesh_has_visible = GLMeshViewer._mesh_has_visible

    gv = Bare()
    assert gv._mesh_has_visible()
    gv.set_mesh_draw_ranges(np.array([0, 18]), np.array([9, 12]))
    assert gv._mesh_ranges == ([0, 18], [9, 12])
    assert gv._mesh_has_visible()
    empty = np.empty(0, dtype=np.int64)
    gv.set_mesh_draw_ranges(empty, empty)
    assert not gv._mesh_has_visible()       # kept uploaded, drawn as nothing
    gv.set_mesh_draw_ranges(None, None)
    assert gv._mesh_has_visible()


# --- rewritten for speed: must match the originals exactly -------------------

def _prism_triangles_reference(tris, n):
    top = tris.astype(np.int64, copy=False)
    bot = tris[:, [0, 2, 1]].astype(np.int64) + n
    m = tris.shape[0]
    edges = np.empty((3 * m, 2), dtype=np.int64)
    edges[0::3] = np.sort(tris[:, [0, 1]], axis=1)
    edges[1::3] = np.sort(tris[:, [1, 2]], axis=1)
    edges[2::3] = np.sort(tris[:, [2, 0]], axis=1)
    _, inverse, counts = np.unique(edges, axis=0, return_inverse=True,
                                   return_counts=True)
    bedges = edges[counts[inverse.ravel()] == 1]
    a, b = bedges[:, 0], bedges[:, 1]
    return np.concatenate(
        [top, bot, np.column_stack([a, b, b + n]),
         np.column_stack([a, b + n, a + n])], axis=0).astype(np.int32)


def test_prism_triangles_match_the_original_edge_grouping():
    from matplotlib.tri import Triangulation
    rng = np.random.default_rng(3)
    for _ in range(10):
        n = int(rng.integers(6, 200))
        tris = Triangulation(rng.random(n), rng.random(n)).triangles
        tris = tris[rng.random(tris.shape[0]) > 0.3].astype(np.int32)
        if not tris.size:
            continue
        got = MG._prism_triangles(tris, n)
        assert got.dtype == np.int32
        assert np.array_equal(got, _prism_triangles_reference(tris, n))


def test_round_barrel_matches_the_general_construction():
    rng = np.random.default_rng(4)
    for _ in range(30):
        x, y = float(rng.random() * 50), float(rng.random() * 50)
        z_top, z_bot = -float(rng.random()), -1.0 - float(rng.random())
        r = 0.1 + float(rng.random())
        n_seg = int(rng.integers(3, 20))
        rgb = tuple(rng.random(3))
        pos, col = MG._generate_via_cylinder(
            x, y, z_top, z_bot, r, rgb, n_segments=n_seg)
        fp = MG._drill_footprint_xy(x, y, r, None, n_seg)
        nxt = np.roll(fp, -1, axis=0)
        want = np.empty((n_seg * 6, 3), dtype=np.float32)
        for k, (ring, z) in enumerate(((fp, z_top), (fp, z_bot),
                                       (nxt, z_top), (nxt, z_top),
                                       (fp, z_bot), (nxt, z_bot))):
            want[k::6, :2] = ring
            want[k::6, 2] = z
        assert np.array_equal(pos, want)
        assert np.array_equal(
            col, np.broadcast_to(np.asarray(rgb, dtype=np.float32),
                                 (n_seg * 6, 3)))
