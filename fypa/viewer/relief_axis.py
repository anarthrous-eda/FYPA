"""Tick values for the relief axis - the vertical ruler drawn beside the
copper in the ignoring-layers relief view (GLMeshViewer.set_relief_axis).

The ruler runs from the bottom to the top of the colour scale's Min / Max
window, because that is what the relief height follows. Ticks sit on round
values inside the window rather than at even fractions of it, so a height
reads off as a number worth reading.
"""
from __future__ import annotations

import math

# Aim for about this many ticks on a linear ruler.
_TARGET_TICKS: int = 5


def _nice_step(span: float, target: int = _TARGET_TICKS) -> float:
    """The 1 / 2 / 5 x 10^k step nearest to ``span / target`` - about 4 to
    9 ticks across ``span`` for the default target."""
    raw = span / max(1, target)
    mag = 10.0 ** math.floor(math.log10(raw))
    frac = raw / mag
    if frac < 1.5:
        return mag
    if frac < 3.5:
        return 2.0 * mag
    if frac < 7.5:
        return 5.0 * mag
    return 10.0 * mag


def _decimals_for(value: float) -> int:
    """Decimal places that print a 1 / 2 / 5 x 10^k number exactly."""
    return max(0, -int(math.floor(math.log10(abs(value)) + 1e-9)))


def _fmt(value: float, decimals: int) -> str:
    if decimals > 9:
        # Only reachable without an SI prefix to carry the magnitude.
        return "0" if value == 0.0 else f"{value:.3g}"
    text = f"{value:.{decimals}f}"
    return "0" if float(text) == 0.0 else text


def _linear_ticks(lo: float, hi: float) -> list[tuple[float, str]]:
    span = hi - lo
    step = _nice_step(span)
    decimals = _decimals_for(step)
    first = math.ceil(lo / step - 1e-9)
    last = math.floor(hi / step + 1e-9)
    ticks: list[tuple[float, str]] = []
    for k in range(int(first), int(last) + 1):
        value = k * step
        t = min(1.0, max(0.0, (value - lo) / span))
        ticks.append((t, _fmt(value, decimals)))
    return ticks


def _log_ticks(lo: float, hi: float) -> list[tuple[float, str]]:
    l_lo, l_hi = math.log10(lo), math.log10(hi)
    span = l_hi - l_lo

    def _at(mantissas: tuple[float, ...]) -> list[tuple[float, str]]:
        out: list[tuple[float, str]] = []
        for exp in range(int(math.floor(l_lo)), int(math.ceil(l_hi)) + 1):
            for m in mantissas:
                value = m * 10.0 ** exp
                if not lo * (1.0 - 1e-9) <= value <= hi * (1.0 + 1e-9):
                    continue
                t = min(1.0, max(0.0, (math.log10(value) - l_lo) / span))
                out.append((t, _fmt(value, _decimals_for(value))))
        return out

    # Whole decades when the window spans enough of them; otherwise fill
    # in 2 and 5 so a narrow window still gets a few ticks.
    ticks = _at((1.0,))
    if len(ticks) < 3:
        ticks = _at((1.0, 2.0, 5.0))
    return ticks


def relief_axis_ticks(vmin: float, vmax: float, *, log: bool = False,
                      divisor: float = 1.0) -> list[tuple[float, str]]:
    """Ticks for a relief axis spanning the colour window ``vmin..vmax``.

    Returns ``[(t, text), ...]`` bottom to top: ``t`` is the 0..1 position
    along the ruler (0 = ``vmin``, 1 = ``vmax``; log-spaced when ``log``)
    and ``text`` the value there divided by ``divisor`` - the colour bar's
    SI-prefix divisor, so the ruler reads in the unit the colour bar's
    title names. Empty when the window is degenerate or not finite.
    """
    if not (math.isfinite(vmin) and math.isfinite(vmax)
            and math.isfinite(divisor)) or divisor <= 0.0:
        return []
    lo, hi = vmin / divisor, vmax / divisor
    if not hi > lo:
        return []
    if log and lo > 0.0:
        ticks = _log_ticks(lo, hi)
        if len(ticks) >= 2:
            return ticks
        # Too narrow for round log values: round linear values, placed
        # on the log ruler.
        span = math.log10(hi) - math.log10(lo)
        return [(min(1.0, max(0.0, (math.log10(lo + t * (hi - lo))
                                    - math.log10(lo)) / span)), text)
                for t, text in _linear_ticks(lo, hi)]
    if (hi - lo) <= 1e-12 * max(abs(lo), abs(hi), 1e-300):
        return []       # below float resolution: no round values to show
    return _linear_ticks(lo, hi)
