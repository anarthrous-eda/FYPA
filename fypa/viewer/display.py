"""Display modes, per-vertex field helpers and heatmap colormaps."""
from __future__ import annotations

import math
import matplotlib
import matplotlib.cm as _mpl_cm
import matplotlib.colors
import numpy as np


# Modes the viewer offers. Each is (label, unit, derive_fn). The derive_fn
# takes (tris, potentials, power_density, conductance, n_verts) and
# returns a numpy array of values per vertex.
#   tris            — (M, 3) int32, vertex indices into the mesh
#   potentials      — (N,) float64, per-vertex voltage (V)
#   power_density   — (M,) float64 or None, per-face power density (W/mm²)
#   conductance     — sheet conductance (S) of the layer
#   n_verts         — vertex count of the mesh (== potentials.size)
def _voltage_per_vertex(tris, potentials, power_density, conductance, n_verts):
    return potentials.copy()




def _power_density_per_vertex(tris, potentials, power_density, conductance, n_verts):
    if power_density is None:
        return np.zeros(n_verts)
    return _face_to_vertex_average(tris, power_density, n_verts)




def _current_density_per_vertex(tris, potentials, power_density, conductance, n_verts):
    # |J| = sqrt(power_density * sheet_conductance). power_density is W/mm^2;
    # sheet_conductance is S (= A/V); product is A^2/mm^2; sqrt → A/mm.
    p = _power_density_per_vertex(tris, potentials, power_density, conductance, n_verts)
    return np.sqrt(np.maximum(p * conductance, 0.0))




def _copper_roi_per_vertex(tris, potentials, power_density, conductance, n_verts):
    # Placeholder identity for the Copper ROI mode: the render path spots it
    # and reads the target's value density instead (it needs the mesh index
    # and the selected load, which this signature doesn't carry).
    return np.zeros(n_verts)


_MODES = [
    ("Voltage",         "V",     _voltage_per_vertex),
    ("Voltage Drop",    "V",     _voltage_per_vertex),  # values are shifted in _render
    ("Current Density", "A/mm",  _current_density_per_vertex),
    ("Power Density",   "W/mm^2", _power_density_per_vertex),
    # Via Current: copper renders neutral grey (the derive_fn output is
    # ignored — see :meth:`PdnViewer._render`), and the heatmap belongs
    # to the via cylinders / markers. Scale range = (min, max) of every
    # visible via's max-segment |I| on the selected rails.
    ("Via Current",     "A",     _voltage_per_vertex),
]

# The Fixes tab's value map: where more copper raises the selected load's
# voltage most (V per mm² of parallel copper). Not in the Mode combo — the
# tab's "Show value map" box turns it on (see PdnViewer._current_selection).
# Its values come from the load's adjoint field, not from this function —
# see PdnViewer._layer_arrays and fypa.copper_roi.
_VALUE_MAP_MODE_ENTRY = ("Copper ROI", "V/mm^2", _copper_roi_per_vertex)



# Mode label that triggers the "shift values so the worst SINK on the rail
# reads 0 V" post-processing in _render. Kept as a constant so the same
# string is used in both the mode list and the special-case check.
_VOLTAGE_DROP_MODE: str = "Voltage Drop"



# Mode label that disables the per-vertex copper heatmap and uses via
# currents as the heatmapped quantity instead. Same string is used in
# the mode list and every special-case branch.
_VIA_CURRENT_MODE: str = "Via Current"



# Modes whose values blow up at point constraints (the FEM has a
# logarithmic gradient singularity at any pinned-voltage vertex such as a
# SOURCE/SINK pin). The default colour-scale clamp uses a percentile rather
# than the absolute max so the rest of the board isn't crushed to black.
# The full data range is still exposed on the scale controller so the user
# can drag/type up to the real max if they want to see the spike.
# Mode label of the copper return-on-investment view.
_COPPER_ROI_MODE: str = "Copper ROI"

_SPIKE_PRONE_MODES: frozenset[str] = frozenset(
    {"Current Density", "Power Density", _COPPER_ROI_MODE})



# Heatmap modes whose data spans many decades, so a logarithmic colour
# scale earns its keep: current density (J spikes hard at copper
# constrictions), power density (~ J²·ρ — roughly the square of that
# dynamic range), and via current (long-tailed: most vias near 0 A, a
# handful near a regulator). The linear/log toggle is enabled only for
# these. Voltage sits in a narrow band and Voltage Drop is signed, so a
# log scale is useless or undefined for them.
_LOG_ELIGIBLE_MODES: frozenset[str] = frozenset(
    {"Current Density", "Power Density", _VIA_CURRENT_MODE, _COPPER_ROI_MODE}
)



# A log colour scale spans at most this many decades below the window
# maximum. Values under the resulting floor — including the exact zeros
# of no-current copper — clamp to the bottom of the LUT instead of
# diverging to log(0) = -inf.
_LOG_SCALE_DECADES: float = 6.0



# Per-via segment current that earns a warning highlight in the Vias tab.
# 1 A through a single 0.6 mm plated through-hole on 1 oz copper is roughly
# where IPC-2152 derating starts to bite (~30°C rise depending on plating
# thickness). Tune to taste.
_VIA_CURRENT_WARN_A: float = 1.0



# Conductive-fill override mode → human-readable phrase for the Physics-
# constants report (mirrors loader.SolveSettings.conductive_fill_mode).
_FILL_MODE_REPORT_LABELS: dict[str, str] = {
    "auto": "auto — per via from the IPC-4761 FILLING material",
    "all": "override: all vias forced filled",
    "none": "override: no vias filled",
}



# Percentile used to clip outliers in the default display range. P99 means
# the worst 1% of vertices are above the auto-clamp top. Most FEM spikes
# affect << 1% of vertices, so this gives a useful default scale on the
# rest of the board.
_DISPLAY_PERCENTILE_HIGH: float = 99.0



# Percentile used to cap the *slider's full extent* (data_max) on spike-prone
# modes. The raw vertex maximum can be 10–100× the physically meaningful range
# because of the FEM logarithmic gradient singularity at pinned-voltage
# vertices (SOURCE/SINK pins), which makes the slider's "Max" label
# misleading. P99.9 still sits well above the P99 default-selection clamp so
# users can drag up to inspect outliers, just not the singularity itself.
_SLIDER_CAP_PERCENTILE: float = 99.9




def _slider_data_max(vs_arr: np.ndarray, mode: str, raw_max: float) -> float:
    """Effective ``data_max`` for the scale-controller slider.

    On spike-prone modes (Current Density / Power Density), clip the raw
    maximum to P99.9 so the FEM singularity at pinned-voltage vertices
    doesn't blow up the slider's upper bound. On all other modes (and on
    very small meshes) returns ``raw_max`` unchanged.
    """
    if mode not in _SPIKE_PRONE_MODES or len(vs_arr) < 100:
        return raw_max
    capped = float(np.percentile(vs_arr, _SLIDER_CAP_PERCENTILE))
    return min(raw_max, capped) if math.isfinite(capped) else raw_max




def _face_to_vertex_average(tris: np.ndarray, face_values: np.ndarray,
                            n_verts: int) -> np.ndarray:
    """Average face-defined values onto vertices (each vertex gets the mean
    of the values of its incident faces). Vectorised via ``np.bincount``.

    A note on the "bincount is faster than np.add.at" claim this docstring
    used to make: it is no longer true. numpy grew a fast scatter path for
    ``np.add.at`` in 1.24, and measured on numpy 2.2 the ``add.at`` form is
    1.4-2.3x FASTER here (65 ms vs 103 ms on a 2M-triangle mesh). The two
    also differ by up to ~6 ULP because they accumulate in different orders.
    Kept as bincount because this is the viewer's hot path and changing the
    accumulation order would shift rendered values; see
    ``pdnsolver/vtu_fields.py`` for the same measurement written down where
    the export path made the same choice in reverse."""
    if tris.size == 0:
        return np.zeros(n_verts, dtype=np.float64)
    # tris.ravel() is [f0v0, f0v1, f0v2, f1v0, …]; each vertex slot's weight is
    # its face's value, so repeat face_values 3× to match that order.
    flat = tris.ravel()
    totals = np.bincount(
        flat, weights=np.repeat(face_values, 3), minlength=n_verts,
    )[:n_verts]
    counts = np.bincount(flat, minlength=n_verts)[:n_verts].astype(np.float64)
    counts[counts == 0] = 1.0
    return totals / counts




def _split_composite_name(name: str) -> tuple[str, str]:
    if "|" in name:
        phys, rail = name.split("|", 1)
        return phys, rail
    return name, ""




def _build_cmap_lut(name: str = "viridis") -> np.ndarray:
    """Sample a matplotlib colormap into a (256, 4) uint8 RGBA LUT for
    upload to the GLMeshViewer's 1-D texture."""
    cmap = _mpl_cm.get_cmap(name)
    samples = cmap(np.linspace(0.0, 1.0, 256))
    return (samples * 255.0).astype(np.uint8)




# Flat grey the copper renders as in Via Current mode. Kept dark so the
# orange / light-grey via cylinders (which own the heatmap in that mode)
# stand out clearly against the context copper. Tweak here to taste.
_VIA_CURRENT_COPPER_RGBA: tuple[int, int, int, int] = (110, 110, 110, 255)




def _build_neutral_cmap_lut(
    rgba: tuple[int, int, int, int] = _VIA_CURRENT_COPPER_RGBA,
) -> np.ndarray:
    """Flat-grey 256-entry RGBA LUT. Pushed to the GL viewer in
    Via Current mode so the copper renders as a single neutral shade
    regardless of the per-vertex values: the heatmap "belongs" to the
    via cylinders in that mode, and the copper is just context."""
    return np.tile(np.asarray(rgba, dtype=np.uint8), (256, 1))




# --- Heatmap colour schemes -------------------------------------------------
#
# The heatmap looks up per-vertex colours through a 256-entry RGBA LUT
# (see :func:`_build_cmap_lut`). The scheme is user-selectable from a
# dropdown next to the colour-scale bar — :data:`_HEATMAP_COLORMAPS` is the
# ordered ``(display name, matplotlib colormap name)`` menu; index 0 is the
# default applied on first render.
#
# "Acton" and "Bam" are Scientific Colour Maps (Crameri — the same schemes
# JuliaPlots ships). matplotlib doesn't bundle them, so
# :func:`_register_custom_colormaps` builds them from anchor colours and
# registers them under ``fypa_``-prefixed names that ``get_cmap`` resolves
# like any built-in. Everything downstream (LUT build, gradient strip)
# only ever sees a colormap name string, so custom and built-in schemes
# travel the exact same path.

# Anchor colours (evenly spaced, RGB 0..1) for the schemes matplotlib
# doesn't ship. Sampled to follow the published Scientific Colour Map ramps.
_CUSTOM_CMAP_ANCHORS: dict[str, list[tuple[float, float, float]]] = {
    # Sequential: dark indigo -> mauve -> light pink.
    "fypa_acton": [
        (0.176, 0.125, 0.302),
        (0.318, 0.235, 0.408),
        (0.486, 0.318, 0.494),
        (0.682, 0.404, 0.557),
        (0.812, 0.541, 0.643),
        (0.871, 0.671, 0.733),
        (0.902, 0.792, 0.804),
    ],
    # Diverging: magenta <-> green through a near-white centre.
    "fypa_bam": [
        (0.396, 0.078, 0.353),
        (0.620, 0.247, 0.541),
        (0.812, 0.510, 0.706),
        (0.937, 0.776, 0.886),
        (0.969, 0.969, 0.969),
        (0.737, 0.871, 0.722),
        (0.435, 0.682, 0.435),
        (0.184, 0.451, 0.243),
    ],
}




def _register_custom_colormaps() -> None:
    """Register the non-matplotlib colour schemes so :func:`get_cmap`
    resolves them by name like any built-in. ``force=True`` makes a
    repeat call (e.g. after a module reload) harmless."""
    from matplotlib.colors import LinearSegmentedColormap
    for name, anchors in _CUSTOM_CMAP_ANCHORS.items():
        cmap = LinearSegmentedColormap.from_list(name, anchors, N=256)
        matplotlib.colormaps.register(cmap, name=name, force=True)




_register_custom_colormaps()




# Ordered ``(display name, matplotlib colormap name)`` for the colour-scale
# dropdown. Index 0 is the default. To add a scheme, append a row — the
# combo, gradient strip and LUT build all read this tuple.
_HEATMAP_COLORMAPS: tuple[tuple[str, str], ...] = (
    ("Viridis (default)", "viridis"),
    ("Blue → Red",   "RdBu_r"),
    ("Heat",              "YlOrRd"),
    ("Bam",               "fypa_bam"),
    ("Acton",             "fypa_acton"),
    ("Inferno",           "inferno"),
    ("Turbo",             "turbo"),
    ("Grayscale",         "gray"),
)


_DEFAULT_CMAP_NAME: str = _HEATMAP_COLORMAPS[0][1]
