"""Report figures, drawn with matplotlib's Agg backend (no Qt, no pyplot).

Every function returns PNG bytes, so the same image goes into the HTML report
(base64-embedded) and the PDF. Heatmaps are drawn from the solved mesh rather
than grabbed from the OpenGL view: the picture is then the same whatever the
viewer's zoom, theme or colour-scale handles were, and it can be drawn from
the command line.
"""
from __future__ import annotations

import io
import math

import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.patches import FancyBboxPatch
from matplotlib.tri import Triangulation

# Palette shared with the HTML/PDF renderers.
INK = "#1d1d1f"
MUTED = "#5f6670"
BORDER = "#dde1e6"
ACCENT = "#1a5fbf"
STATUS_COLORS = {
    "FAIL": ("#b3261e", "#fdecea"),
    "WARN": ("#8a5a00", "#fff4dc"),
    "PASS": ("#1e6b3a", "#e7f5ec"),
    "UNCHECKED": ("#5f6670", "#eef0f3"),
}
DPI = 150


def _png(fig: Figure) -> bytes:
    FigureCanvasAgg(fig)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=DPI, bbox_inches="tight",
                facecolor="white")
    return buf.getvalue()


def _fmt_v(v: float | None) -> str:
    return "—" if v is None else f"{v:.3f} V"


# --- power path ---------------------------------------------------------------

def power_path_png(source_label: str, source_sub: str, load_a: float | None,
                   loads: list[dict], max_rows: int = 14) -> bytes:
    """Source box on the left feeding a column of load boxes.

    ``loads``: dicts with ``label``, ``value`` (text), ``status`` and an
    optional ``via`` (series parts on the way, e.g. ``"FB3"``).
    """
    shown = loads[:max_rows]
    extra = len(loads) - len(shown)
    n = max(len(shown) + (1 if extra else 0), 1)
    row_h = 0.62
    height = max(1.6, n * row_h + 0.5)
    fig = Figure(figsize=(7.4, height))
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_xlim(0, 10)
    ax.set_ylim(0, height)
    ax.axis("off")

    mid = height / 2
    ax.add_patch(FancyBboxPatch(
        (0.1, mid - 0.42), 2.5, 0.84, boxstyle="round,pad=0.02,rounding_size=0.08",
        facecolor="white", edgecolor=INK, linewidth=1.2))
    ax.text(1.35, mid + 0.12, source_label, ha="center", va="center",
            fontsize=9, fontweight="bold", color=INK)
    ax.text(1.35, mid - 0.18, source_sub, ha="center", va="center",
            fontsize=7.5, color=MUTED)

    bus_x = 4.1
    ax.plot([2.6, bus_x], [mid, mid], color=INK, linewidth=2.2)
    if load_a is not None:
        ax.text((2.6 + bus_x) / 2, mid + 0.12, f"{load_a:.3g} A", ha="center",
                va="bottom", fontsize=8, color=MUTED)
    # Centre the load column on the source so a single load lines up.
    top = mid + (n - 1) * row_h / 2
    ys = [top - i * row_h for i in range(n)]
    if len(ys) > 1:
        ax.plot([bus_x, bus_x], [ys[-1], ys[0]], color=INK, linewidth=2.2)
    for y, load in zip(ys, shown):
        fg, bg = STATUS_COLORS.get(load.get("status", "PASS"),
                                   STATUS_COLORS["PASS"])
        via = load.get("via")
        ax.plot([bus_x, 6.0], [y, y], color=INK, linewidth=1.4)
        if via:
            ax.add_patch(FancyBboxPatch(
                (4.45, y - 0.15), 1.2, 0.3,
                boxstyle="round,pad=0.01,rounding_size=0.04",
                facecolor="white", edgecolor=INK, linewidth=0.9))
            ax.text(5.05, y, via, ha="center", va="center", fontsize=7,
                    color=INK)
        ax.add_patch(FancyBboxPatch(
            (6.0, y - 0.24), 3.9, 0.48,
            boxstyle="round,pad=0.01,rounding_size=0.06",
            facecolor=bg, edgecolor=fg, linewidth=1.0))
        word = {"FAIL": "FAIL", "WARN": "WARN", "PASS": "OK",
                "UNCHECKED": "no limit"}.get(load.get("status"), "")
        ax.text(6.12, y, load["label"], ha="left", va="center", fontsize=8,
                color=fg, fontweight="bold")
        ax.text(9.8, y, f"{load.get('value', '')}  {word}", ha="right",
                va="center", fontsize=7.5, color=fg)
    if extra:
        ax.text(6.1, ys[len(shown)], f"+ {extra} more (see the load table)",
                ha="left", va="center", fontsize=7.5, color=MUTED)
    return _png(fig)


# --- heatmaps -------------------------------------------------------------------

def mesh_map_png(parts: list[tuple[np.ndarray, np.ndarray, np.ndarray]],
                 *, title: str, unit_label: str, cmap: str,
                 vmin: float | None = None, vmax: float | None = None,
                 marker: tuple[float, float, str] | None = None,
                 pins: list[tuple[float, float, str]] | None = None,
                 scale: float = 1.0) -> bytes:
    """Flat-shaded map of per-face values over one layer's rail copper.

    ``parts``: ``(vertex_xys, triangles, face_values)`` per mesh component.
    ``marker``: ``(x, y, text)`` circled and labelled (the worst point).
    ``pins``: small ticks for the source / load pins on this layer.
    ``scale`` multiplies values for display (e.g. 1e3 for mV).
    """
    xs, ys, tris, vals = [], [], [], []
    offset = 0
    for xys, tri, fv in parts:
        xys = np.asarray(xys)
        tri = np.asarray(tri)
        if xys.shape[0] == 0 or tri.size == 0:
            continue
        xs.append(xys[:, 0])
        ys.append(xys[:, 1])
        tris.append(tri + offset)
        vals.append(np.asarray(fv, dtype=np.float64) * scale)
        offset += xys.shape[0]
    fig = Figure(figsize=(6.2, 4.4))
    ax = fig.add_axes((0.02, 0.16, 0.96, 0.78))
    ax.set_aspect("equal")
    ax.axis("off")
    if not tris:
        ax.text(0.5, 0.5, "No copper on this layer", ha="center",
                transform=ax.transAxes, color=MUTED)
        return _png(fig)
    x = np.concatenate(xs)
    y = np.concatenate(ys)
    t = np.concatenate(tris)
    v = np.concatenate(vals)
    v = np.where(np.isfinite(v), v, np.nan)
    lo = np.nanmin(v) if vmin is None else vmin * scale
    hi = np.nanmax(v) if vmax is None else vmax * scale
    if not math.isfinite(lo) or not math.isfinite(hi) or hi <= lo:
        hi = lo + 1e-12 if math.isfinite(lo) else 1.0
        lo = lo if math.isfinite(lo) else 0.0
    tri = Triangulation(x, y, t)
    coll = ax.tripcolor(tri, facecolors=np.clip(v, lo, hi), cmap=cmap,
                        vmin=lo, vmax=hi, shading="flat", rasterized=True)
    for px, py, _label in pins or []:
        ax.plot(px, py, marker="s", markersize=3.5, markerfacecolor="white",
                markeredgecolor=INK, markeredgewidth=0.7, linestyle="none")
    if marker is not None:
        mx, my, text = marker
        ax.plot(mx, my, marker="o", markersize=13, markerfacecolor="none",
                markeredgecolor=INK, markeredgewidth=1.8, linestyle="none")
        ax.plot(mx, my, marker="o", markersize=15, markerfacecolor="none",
                markeredgecolor="white", markeredgewidth=0.8, linestyle="none")
        ax.annotate(text, (mx, my), xytext=(10, 10),
                    textcoords="offset points", fontsize=10, color=INK,
                    bbox={"boxstyle": "round,pad=0.25", "fc": "white",
                          "ec": BORDER, "lw": 0.8})
    cax = fig.add_axes((0.2, 0.07, 0.6, 0.035))
    cb = fig.colorbar(coll, cax=cax, orientation="horizontal")
    cb.set_label(unit_label, fontsize=10, color=MUTED)
    cb.ax.tick_params(labelsize=9, colors=MUTED)
    for spine in cb.ax.spines.values():
        spine.set_edgecolor(BORDER)
    ax.set_title(title, fontsize=11, color=INK, loc="left")
    return _png(fig)


# --- impedance ------------------------------------------------------------------

def impedance_png(freqs_hz: np.ndarray, z_mag: np.ndarray,
                  target_ohm: float | None, f_max_hz: float | None,
                  worst: tuple[float, float] | None = None) -> bytes:
    """|Z(f)| on log-log axes against the flat target up to F_MAX, with the
    band where the target is breached shaded."""
    fig = Figure(figsize=(6.2, 3.6))
    ax = fig.add_axes((0.12, 0.15, 0.84, 0.78))
    f = np.asarray(freqs_hz, dtype=np.float64)
    z = np.asarray(z_mag, dtype=np.float64)
    ax.loglog(f, z * 1e3, color=ACCENT, linewidth=1.6, label="|Z|")
    if target_ohm is not None and math.isfinite(target_ohm):
        f_hi = f_max_hz or f[-1]
        ax.plot([f[0], f_hi], [target_ohm * 1e3] * 2, color="#b3261e",
                linestyle="--", linewidth=1.2, label="Target")
        over = (z > target_ohm) & (f <= f_hi)
        if over.any():
            ax.fill_between(f, 1e-9, 1e9, where=[bool(o) for o in over],
                            color="#b3261e",
                            alpha=0.10, linewidth=0, step="mid")
    if f_max_hz:
        ax.axvline(f_max_hz, color=MUTED, linewidth=0.8, linestyle=":")
    if worst is not None:
        wf, wz = worst
        ax.plot(wf, wz * 1e3, marker="o", color="#b3261e", markersize=5)
        ax.annotate(f"{wz * 1e3:.3g} mΩ @ {_fmt_hz(wf)}", (wf, wz * 1e3),
                    xytext=(6, 6), textcoords="offset points", fontsize=8,
                    color="#b3261e")
    finite = z[np.isfinite(z)]
    if finite.size:
        lo = max(float(finite.min()) * 1e3 / 3, 1e-4)
        hi = float(finite.max()) * 1e3 * 3
        if target_ohm is not None and math.isfinite(target_ohm):
            lo = min(lo, target_ohm * 1e3 / 3)
            hi = max(hi, target_ohm * 1e3 * 3)
        ax.set_ylim(lo, hi)
    ax.set_xlim(f[0], f[-1])
    ax.set_xlabel("Frequency (Hz)", fontsize=8, color=MUTED)
    ax.set_ylabel("|Z| (mΩ)", fontsize=8, color=MUTED)
    ax.tick_params(labelsize=7, colors=MUTED)
    ax.grid(True, which="major", color=BORDER, linewidth=0.6)
    for s in ax.spines.values():
        s.set_edgecolor(BORDER)
    ax.legend(fontsize=7, frameon=False, loc="upper left")
    return _png(fig)


def _fmt_hz(f: float) -> str:
    for scale, unit in ((1e9, "GHz"), (1e6, "MHz"), (1e3, "kHz")):
        if f >= scale:
            return f"{f / scale:.3g} {unit}"
    return f"{f:.3g} Hz"
