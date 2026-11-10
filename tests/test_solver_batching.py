"""Batched forms of two per-item solver passes must reproduce the per-item
results exactly: which mesh vertices lie under each pad (node indexing), and
the per-triangle power density."""
from __future__ import annotations

import numpy as np
import scipy.spatial
import shapely
import shapely.geometry

from pdnsolver import solver as S


def _random_pads(rng, n):
    pads = []
    for _ in range(n):
        cx, cy = rng.uniform(5, 95, size=2)
        kind = rng.integers(0, 3)
        if kind == 0:       # drill / round pad
            pads.append(shapely.geometry.Point(cx, cy).buffer(
                rng.uniform(0.1, 1.5), quad_segs=8))
        elif kind == 1:     # rectangular SMT pad
            w, h = rng.uniform(0.2, 3.0, size=2)
            pads.append(shapely.geometry.box(cx - w, cy - h, cx + w, cy + h))
        else:               # two-part region
            pads.append(shapely.geometry.MultiPolygon([
                shapely.geometry.box(cx, cy, cx + 0.8, cy + 0.8),
                shapely.geometry.box(cx + 1.5, cy, cx + 2.3, cy + 0.8)]))
    return pads


def test_pad_members_batch_matches_the_per_pad_path(monkeypatch):
    rng = np.random.default_rng(7)
    xy = rng.uniform(0, 100, size=(60_000, 2))
    tree = scipy.spatial.KDTree(xy, leafsize=32)
    globals_arr = rng.permutation(xy.shape[0]).astype(np.int64)
    pads = _random_pads(rng, 700)
    pads.append(shapely.geometry.Polygon())                    # empty outline
    pads.append(shapely.geometry.box(500, 500, 501, 501))      # catches nothing
    points = [shapely.geometry.Point(
        *(p.centroid.coords[0] if not p.is_empty else (0.0, 0.0)))
        for p in pads]
    # Several slices, so the chunk seams are exercised too.
    monkeypatch.setattr(S, "_PAD_BATCH", 128)

    batch = S._pad_members_batch(tree, pads)
    assert len(batch) == len(pads)
    assert sum(len(b) for b in batch) > 1000        # the test has teeth

    claimed_a = np.zeros(xy.shape[0], dtype=bool)
    claimed_b = np.zeros(xy.shape[0], dtype=bool)
    for pad, pt, sel in zip(pads, points, batch):
        if pad.is_empty:
            assert sel.size == 0
            continue
        ref = S._vertices_under_pad(tree, globals_arr, pad, pt, claimed_a)
        got = S._vertices_under_pad(tree, globals_arr, pad, pt, claimed_b,
                                    sel_local=sel)
        # Same vertices, same order (representative first).
        assert np.array_equal(ref, got)
        claimed_a[ref] = True
        claimed_b[got] = True


def _reference_power_density(xys, tris, vals, conductivity):
    """The masked closed form compute_power_density used before blocking."""
    p0, p1, p2 = xys[tris[:, 0]], xys[tris[:, 1]], xys[tris[:, 2]]
    f0, f1, f2 = vals[tris[:, 0]], vals[tris[:, 1]], vals[tris[:, 2]]
    y23 = p1[:, 1] - p2[:, 1]
    x32 = p2[:, 0] - p1[:, 0]
    y31 = p2[:, 1] - p0[:, 1]
    x13 = p0[:, 0] - p2[:, 0]
    y12 = p0[:, 1] - p1[:, 1]
    x21 = p1[:, 0] - p0[:, 0]
    D = y23 * (p0[:, 0] - p2[:, 0]) + x32 * (p0[:, 1] - p2[:, 1])
    Ex = np.zeros_like(D)
    Ey = np.zeros_like(D)
    nz = D != 0
    invD = np.zeros_like(D)
    invD[nz] = 1.0 / D[nz]
    Ex[nz] = (y23[nz] * f0[nz] + y31[nz] * f1[nz] + y12[nz] * f2[nz]) * invD[nz]
    Ey[nz] = (x32[nz] * f0[nz] + x13[nz] * f1[nz] + x21[nz] * f2[nz]) * invD[nz]
    return conductivity * (Ex * Ex + Ey * Ey)


def test_power_density_kernel_is_bit_identical(monkeypatch):
    rng = np.random.default_rng(3)
    n_v, n_t = 5_000, 20_000
    xys = rng.uniform(0, 50, size=(n_v, 2))
    tris = rng.integers(0, n_v, size=(n_t, 3)).astype(np.int32)
    tris[::500, 1] = tris[::500, 0]                 # degenerate triangles
    vals = rng.uniform(0, 1, size=n_v)
    ref = _reference_power_density(xys, tris, vals, 2030.0)
    assert (ref[::500] == 0).all()

    # One block, many blocks, and blocks on threads all agree to the bit.
    assert np.array_equal(S._power_density_kernel(xys, tris, vals, 2030.0), ref)
    monkeypatch.setattr(S, "_POWER_DENSITY_BLOCK", 1024)
    assert np.array_equal(S._power_density_kernel(xys, tris, vals, 2030.0), ref)
    monkeypatch.setattr(S, "_POWER_DENSITY_PARALLEL_MIN", 2048)
    assert np.array_equal(S._power_density_kernel(xys, tris, vals, 2030.0), ref)

    # A non-finite potential must not leak into a degenerate triangle.
    bad = vals.copy()
    bad[tris[0, 0]] = np.nan
    assert S._power_density_kernel(xys, tris, bad, 2030.0)[0] == 0.0
