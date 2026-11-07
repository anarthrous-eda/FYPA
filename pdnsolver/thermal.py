"""Board thermal model for the coupled electro-thermal solve.

The default electro-thermal model (``ThermalConfig`` with no ``board``) is
local: each triangle sheds its own heat straight to ambient, so a hot neck
never warms its neighbours and a pour never acts as the heatsink it is. That
over-states the rise of small hot features many times over.

This module adds lateral spreading. The board is a stack of copper-layer
sheets on a square finite-difference grid over the board outline:

* **In plane**, each layer's sheet conducts ``k_cu·t_cu·coverage`` — the
  fraction of the cell covered by copper of *any* net, since all copper
  spreads heat — plus half of each touching laminate's ``k_xy·t``.
* **Through the board**, neighbouring layers couple through the laminate
  between them (``k_z·A/t``, FR-4 being ~2.7x worse through-plane than in
  plane) and through plated barrels (``k_cu·A_wall/dz``) wherever a via or
  plated hole spans both.
* **To ambient**, the two outer layers convect, each at half the combined
  both-faces coefficient the user gives (so one setting means the same as in
  the local model).

Heat comes from the copper's I²R (triangle by triangle, binned into the cell
holding each triangle's centroid) and from lumped parts the solver marks
(via barrels, series parts, regulator conversion loss), at their pads.

The conductance matrix does not depend on temperature, so it is factorised
once; each electro-thermal iteration is one back-substitution. A trace much
narrower than a cell reads its cell's mean temperature, so the hottest
sliver of a thin neck reads low by however much it is hotter than its cell —
the trade KiPI (whose design notes this follows) also makes.

Units: millimetres, watts, kelvin. Conductivities are given in W/(m·K) and
heat-transfer coefficients in W/(m²·K), and converted here.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

import numpy as np
import scipy.sparse
import scipy.sparse.linalg
import scipy.spatial

log = logging.getLogger(__name__)

K_COPPER_W_PER_MK = 390.0
# FR-4: about 0.8 W/(m·K) in plane, 0.3 through the glass weave.
K_LAMINATE_XY_W_PER_MK = 0.8
K_LAMINATE_Z_W_PER_MK = 0.3
# Grid: cells per layer aimed for, and the finest / coarsest cell allowed.
TARGET_CELLS_PER_LAYER = 40_000
# Whole-stack unknowns allowed: PARDISO factorises 640k in ~3 s; SuperLU
# needed >12 GB for the same matrix, so it gets a far smaller grid.
MAX_UNKNOWNS_PARDISO = 700_000
MAX_UNKNOWNS_SUPERLU = 150_000
MIN_CELL_MM = 0.1
MAX_CELL_MM = 3.0
# Supersampling per cell side when rasterising copper coverage.
_COVERAGE_SUPERSAMPLE = 3


@dataclass
class ThermalLayer:
    """One copper layer of the stack, top to bottom."""
    copper_thickness_mm: float
    # Every net's copper on this layer (any shapely areal geometry).
    copper: object
    # Laminate between this layer and the next one down (0 for the last).
    dielectric_below_mm: float = 0.0


@dataclass
class ThermalVia:
    """A plated barrel conducting heat between layers ``top`` and ``bottom``
    (indices into the layer list, inclusive)."""
    x_mm: float
    y_mm: float
    top: int
    bottom: int
    # Copper cross-section of the barrel wall (plus any conductive fill
    # expressed as copper), mm².
    copper_area_mm2: float


@dataclass
class ThermalBoard:
    """A factorised board thermal model; see the module docstring.

    Build it with :func:`build_thermal_board`. ``problem_layer_map[i]`` is
    the thermal layer of the solver's ``prob.layers[i]`` (``-1`` when it is
    unknown — its heat is then dropped, and the loop warns)."""
    x0: float
    y0: float
    dx: float
    nx: int
    ny: int
    n_layers: int
    # Flat index of each (layer, cell) unknown, -1 outside the board.
    unknown_of: np.ndarray            # (n_layers, ny, nx) int64
    n_unknowns: int
    problem_layer_map: list[int] = field(default_factory=list)
    _factor: object = None
    # Called by :meth:`close` to release the factorisation (PARDISO keeps
    # it in MKL memory the garbage collector cannot see).
    _release: object = None
    # Summary for the log / tests.
    info: dict = field(default_factory=dict)

    def close(self) -> None:
        """Free the factorisation. Safe to call more than once."""
        release, self._release = self._release, None
        self._factor = None
        if release is not None:
            try:
                release()
            except Exception:       # pragma: no cover - best effort
                pass

    # --- lookup --------------------------------------------------------

    def cell_of(self, x: np.ndarray, y: np.ndarray
                ) -> tuple[np.ndarray, np.ndarray]:
        """Grid (row, column) of points, clamped onto the grid."""
        col = np.clip(((np.asarray(x) - self.x0) / self.dx).astype(np.int64),
                      0, self.nx - 1)
        row = np.clip(((np.asarray(y) - self.y0) / self.dx).astype(np.int64),
                      0, self.ny - 1)
        return row, col

    def unknown_at(self, layer: np.ndarray, x: np.ndarray, y: np.ndarray
                   ) -> np.ndarray:
        """Unknown index at (layer, x, y); points off the board snap to the
        nearest on-board cell of that layer (copper at the very edge of a
        coarse grid), and ``-1`` stays only for an unknown layer."""
        layer = np.asarray(layer, dtype=np.int64)
        row, col = self.cell_of(x, y)
        out = np.full(layer.shape, -1, dtype=np.int64)
        ok = (layer >= 0) & (layer < self.n_layers)
        out[ok] = self.unknown_of[layer[ok], row[ok], col[ok]]
        miss = ok & (out < 0)
        if np.any(miss):
            out[miss] = self._nearest_inside(layer[miss], row[miss], col[miss])
        return out

    def _nearest_inside(self, layer, row, col) -> np.ndarray:
        inside_r, inside_c = np.nonzero(self.unknown_of[0] >= 0)
        if inside_r.size == 0:
            return np.full(layer.shape, -1, dtype=np.int64)
        tree = scipy.spatial.cKDTree(np.column_stack([inside_r, inside_c]))
        _d, j = tree.query(np.column_stack([row, col]))
        return self.unknown_of[layer, inside_r[j], inside_c[j]]

    # --- solve ---------------------------------------------------------

    def solve(self, heat_w: np.ndarray) -> np.ndarray:
        """Temperature rise (K) of every unknown for a heat input (W) per
        unknown."""
        return np.asarray(self._factor(np.asarray(heat_w, dtype=np.float64)))

    def rise_for(
        self,
        tri_layer: np.ndarray, tri_cx: np.ndarray, tri_cy: np.ndarray,
        tri_power_w: np.ndarray,
        point_layer: np.ndarray | None = None,
        point_x: np.ndarray | None = None,
        point_y: np.ndarray | None = None,
        point_power_w: np.ndarray | None = None,
    ) -> np.ndarray:
        """Temperature rise of each triangle (its cell's) for the given
        triangle and point heat sources. Layers are *thermal* layers."""
        heat = np.zeros(self.n_unknowns, dtype=np.float64)
        u_tri = self.unknown_at(tri_layer, tri_cx, tri_cy)
        ok = u_tri >= 0
        np.add.at(heat, u_tri[ok], np.asarray(tri_power_w)[ok])
        if point_power_w is not None and len(point_power_w):
            u_pt = self.unknown_at(point_layer, point_x, point_y)
            okp = u_pt >= 0
            np.add.at(heat, u_pt[okp], np.asarray(point_power_w)[okp])
        t = self.solve(heat)
        rise = np.zeros(u_tri.shape, dtype=np.float64)
        rise[ok] = t[u_tri[ok]]
        return rise


def _coverage(copper, x0, y0, dx, nx, ny) -> np.ndarray:
    """Fraction of each cell covered by ``copper`` (rows = y, cols = x),
    rasterised with Pillow at ``_COVERAGE_SUPERSAMPLE``² samples per cell.
    Each polygon is drawn into its own bounding-box mask (outline filled,
    holes cleared) and OR-ed in, so one net's antipads never erase another
    net's copper."""
    from PIL import Image, ImageDraw

    ss = _COVERAGE_SUPERSAMPLE
    w, h = nx * ss, ny * ss
    img = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(img)
    scale = ss / dx

    def _px(coords):
        c = np.asarray(coords, dtype=np.float64)
        return [(float(px), float(py)) for px, py in
                zip((c[:, 0] - x0) * scale, (c[:, 1] - y0) * scale)]

    if copper is None or getattr(copper, "is_empty", True):
        return np.zeros((ny, nx), dtype=np.float64)
    # Flatten fully: the loader hands over a GeometryCollection of
    # per-net MultiPolygons, and get_parts unwraps one level only.
    polys = []
    stack = [copper]
    while stack:
        g = stack.pop()
        if g.geom_type == "Polygon":
            polys.append(g)
        elif hasattr(g, "geoms"):
            stack.extend(g.geoms)
    for poly in polys:
        if poly.is_empty or poly.geom_type != "Polygon":
            continue
        ext = _px(poly.exterior.coords)
        if len(ext) < 3:
            continue
        if not poly.interiors:
            draw.polygon(ext, fill=255)
            continue
        xs = [p[0] for p in ext]
        ys = [p[1] for p in ext]
        bx0, by0 = max(int(math.floor(min(xs))), 0), max(int(math.floor(min(ys))), 0)
        bx1, by1 = min(int(math.ceil(max(xs))) + 1, w), min(int(math.ceil(max(ys))) + 1, h)
        if bx1 <= bx0 or by1 <= by0:
            continue
        mask = Image.new("L", (bx1 - bx0, by1 - by0), 0)
        md = ImageDraw.Draw(mask)
        md.polygon([(px - bx0, py - by0) for px, py in ext], fill=255)
        for ring in poly.interiors:
            hole = _px(ring.coords)
            if len(hole) >= 3:
                md.polygon([(px - bx0, py - by0) for px, py in hole], fill=0)
        img.paste(255, (bx0, by0, bx1, by1), mask)
    a = np.asarray(img, dtype=np.float64).reshape(ny, ss, nx, ss)
    return a.mean(axis=(1, 3)) / 255.0


def build_thermal_board(
    outline,
    layers: list[ThermalLayer],
    vias: list[ThermalVia],
    heat_transfer_w_per_m2k: float,
    cell_mm: float | None = None,
    k_copper: float = K_COPPER_W_PER_MK,
    k_laminate_xy: float = K_LAMINATE_XY_W_PER_MK,
    k_laminate_z: float = K_LAMINATE_Z_W_PER_MK,
) -> ThermalBoard:
    """Assemble and factorise the board model.

    ``outline`` is the board's shapely polygon (cells whose centre lies in
    it are board). ``heat_transfer_w_per_m2k`` is the *combined* both-faces
    coefficient, as in the local model; each outer face gets half."""
    import shapely

    if not layers:
        raise ValueError("thermal board needs at least one copper layer")
    minx, miny, maxx, maxy = outline.bounds
    width, height = maxx - minx, maxy - miny
    if cell_mm is None:
        # Cells per layer, held down so the whole stack stays factorisable:
        # SuperLU's fill on a layered grid makes it the binding limit.
        cap = MAX_UNKNOWNS_PARDISO if _have_pardiso() else MAX_UNKNOWNS_SUPERLU
        per_layer = min(TARGET_CELLS_PER_LAYER, cap // max(len(layers), 1))
        cell_mm = math.sqrt(max(outline.area, 1e-6) / max(per_layer, 1))
    dx = float(min(max(cell_mm, MIN_CELL_MM), MAX_CELL_MM))
    nx = max(1, int(math.ceil(width / dx)))
    ny = max(1, int(math.ceil(height / dx)))
    n_l = len(layers)

    cx = minx + (np.arange(nx) + 0.5) * dx
    cy = miny + (np.arange(ny) + 0.5) * dx
    gx, gy = np.meshgrid(cx, cy)
    inside = shapely.contains_xy(outline, gx, gy)
    if not np.any(inside):
        raise ValueError("thermal board outline contains no grid cell")
    n_in = int(np.count_nonzero(inside))
    cell_index = np.full((ny, nx), -1, dtype=np.int64)
    cell_index[inside] = np.arange(n_in)
    unknown_of = np.full((n_l, ny, nx), -1, dtype=np.int64)
    for li in range(n_l):
        unknown_of[li][inside] = li * n_in + cell_index[inside]
    n_unk = n_l * n_in

    k_cu = k_copper * 1e-3              # W/(mm·K)
    k_xy = k_laminate_xy * 1e-3
    k_z = k_laminate_z * 1e-3
    area = dx * dx

    rows: list[np.ndarray] = []
    cols: list[np.ndarray] = []
    vals: list[np.ndarray] = []

    def _couple(a: np.ndarray, b: np.ndarray, g: np.ndarray) -> None:
        rows.extend((a, b, a, b))
        cols.extend((a, b, b, a))
        vals.extend((g, g, -g, -g))

    copper_cover = []
    for li, lay in enumerate(layers):
        cov = _coverage(lay.copper, minx, miny, dx, nx, ny)
        copper_cover.append(float(cov[inside].mean()))
        lam_above = 0.5 * layers[li - 1].dielectric_below_mm if li > 0 else 0.0
        lam_below = 0.5 * lay.dielectric_below_mm if li < n_l - 1 else 0.0
        sheet = (k_cu * lay.copper_thickness_mm * cov
                 + k_xy * (lam_above + lam_below))       # W/K per square
        u = unknown_of[li]
        # x-neighbours, then y-neighbours: series of two half cells.
        for ua, ub, ga, gb in (
                (u[:, :-1], u[:, 1:], sheet[:, :-1], sheet[:, 1:]),
                (u[:-1, :], u[1:, :], sheet[:-1, :], sheet[1:, :])):
            ok = (ua >= 0) & (ub >= 0)
            g = 2.0 * ga[ok] * gb[ok] / np.maximum(ga[ok] + gb[ok], 1e-30)
            _couple(ua[ok], ub[ok], g)

    # Laminate between neighbouring layers, plus barrels.
    barrel_g = np.zeros((max(n_l - 1, 0), ny, nx), dtype=np.float64)
    for v in vias:
        if v.copper_area_mm2 <= 0.0:
            continue
        r, c = (int(np.clip((v.y_mm - miny) / dx, 0, ny - 1)),
                int(np.clip((v.x_mm - minx) / dx, 0, nx - 1)))
        for li in range(max(v.top, 0), min(v.bottom, n_l - 1)):
            dz = (layers[li].dielectric_below_mm
                  + 0.5 * (layers[li].copper_thickness_mm
                           + layers[li + 1].copper_thickness_mm))
            barrel_g[li, r, c] += k_cu * v.copper_area_mm2 / max(dz, 1e-6)
    for li in range(n_l - 1):
        t = max(layers[li].dielectric_below_mm, 1e-6)
        g = k_z * area / t + barrel_g[li]
        a, b = unknown_of[li][inside], unknown_of[li + 1][inside]
        _couple(a, b, g[inside])

    # Convection from the outer faces (each half the combined coefficient).
    h_face = 0.5 * max(heat_transfer_w_per_m2k, 0.0) * 1e-6     # W/(mm²·K)
    faces = [0] if n_l == 1 else [0, n_l - 1]
    for li in faces:
        # A single layer has both its faces exposed.
        share = 2.0 if n_l == 1 else 1.0
        u = unknown_of[li][inside]
        rows.append(u)
        cols.append(u)
        vals.append(np.full(u.shape, share * h_face * area))

    A = scipy.sparse.coo_matrix(
        (np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))),
        shape=(n_unk, n_unk)).tocsc()
    if h_face <= 0.0:
        # No path to ambient makes A singular; tie it weakly so the solve
        # stays defined (the caller warns about a zero coefficient).
        A = A + scipy.sparse.identity(n_unk, format="csc") * 1e-12
    factor, release, method = _factorize_spd(A)
    board = ThermalBoard(
        x0=minx, y0=miny, dx=dx, nx=nx, ny=ny, n_layers=n_l,
        unknown_of=unknown_of, n_unknowns=n_unk, _factor=factor,
        _release=release,
        info={"cell_mm": dx, "cells_per_layer": n_in, "layers": n_l,
              "unknowns": n_unk, "vias": len(vias),
              "copper_cover": copper_cover, "factor": method},
    )
    log.info("Thermal board: %d x %d cells of %.3g mm (%d on the board) x %d "
             "layer(s), %d plated barrel(s), %d unknowns (%s)", nx, ny, dx,
             n_in, n_l, len(vias), n_unk, method)
    return board


def _have_pardiso() -> bool:
    try:
        import pypardiso  # noqa: F401
        return True
    except Exception:
        return False


def _factorize_spd(A) -> tuple[object, object, str]:
    """Factorise the (symmetric positive-definite) board matrix once.

    MKL PARDISO when available, in a solver instance of its own so the
    electrical solve's cached factorisation is left alone, in SPD mode on
    the upper triangle (2.6 s for 640k unknowns on a 16-layer board).
    SuperLU otherwise — whose fill on a layered 3-D grid grows fast, which
    is why :func:`build_thermal_board` coarsens the grid when PARDISO is
    missing. Returns ``(solve(b), release(), method)``."""
    if _have_pardiso():
        try:
            import pypardiso
            upper = scipy.sparse.triu(A, format="csr")
            upper.sort_indices()
            ps = pypardiso.PyPardisoSolver(mtype=2)
            ps.factorize(upper)

            def _solve(b):
                return ps.solve(upper, b)
            return _solve, ps.free_memory, "pardiso"
        except Exception as exc:    # pragma: no cover - MKL specific
            log.warning("Thermal board: PARDISO failed (%s); using SuperLU.",
                        exc)
    return scipy.sparse.linalg.factorized(A.tocsc()), None, "superlu"

