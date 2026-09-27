"""The colour-scale gradient bar and the side-panel scale controller."""
from __future__ import annotations

import math
import matplotlib.cm as _mpl_cm
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPolygonF
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from fypa.viewer.display import _HEATMAP_COLORMAPS
from fypa.viewer.numeric import _numeric_validator, _parse_numeric_text
from fypa.viewer.theme import _T, current_theme


class _GradientBar(QWidget):
    """Horizontal colormap strip with two draggable handles for vmin/vmax,
    designed to sit as an overlay on the heatmap viewer's bottom-left
    corner.

    Paints its own dark chip background, a title, the gradient strip with
    draggable handles, and a row of value tick labels along the bottom.
    Emits :attr:`rangeChanged(low, high)` whenever the user drags a
    handle. The handles render as small triangle pointers above the
    strip; clicking on the strip near a handle starts a drag. ``low`` is
    always kept ≤ ``high`` with at least a tiny gap so the heatmap
    doesn't collapse.

    Colours are fixed rather than theme-driven — the chip sits on the
    heatmap (not in the themed side panel), so it always uses a dark
    background with light text, matching the GL viewer's legend chip.
    """

    rangeChanged = Signal(float, float)

    # --- Fixed overlay colours (theme-independent — chip sits on the GL
    # viewer, like the legend chip) ---
    _CHIP_BG = QColor(30, 30, 30)
    _CHIP_BORDER = QColor("#666666")
    _STRIP_BORDER = QColor("#888888")
    _MASK = QColor(0, 0, 0, 150)        # darkens the strip outside [low, high]
    _TEXT = QColor("#e6e6e6")
    _TICK = QColor("#aaaaaa")
    _HANDLE_FILL = QColor("#f0f0f0")
    _HANDLE_EDGE = QColor("#1a1a1a")

    # --- Pixel geometry ---
    _CHIP_W: int = 300
    _CHIP_PAD: int = 8       # padding between chip edge and content
    _MARGIN_X: int = 8       # extra inset so handle triangles aren't clipped
    _TITLE_H: int = 15       # title text band
    _MARGIN_TOP: int = 9     # handle-triangle space above the strip
    _STRIP_HEIGHT: int = 15
    _TICK_GAP: int = 3       # strip bottom → tick mark
    _TICK_MARK: int = 4      # tick mark length
    _TICK_LABEL_H: int = 13  # tick label text band
    _HANDLE_HALF_W: int = 6
    _N_TICKS: int = 5
    # Cap on total significant figures per tick label. Keeps a
    # near-constant field around a large value (e.g. ~100 V) from
    # forcing many decimals onto a multi-digit integer part, which
    # would overflow the label box and collide along the axis.
    _MAX_SIG_FIGS: int = 5

    # Engineering SI prefixes (powers of 1000), exponent → symbol. One
    # prefix is picked per render from the data magnitude and folded into
    # the axis title so the tick labels can stay plain numbers.
    _SI_PREFIXES: dict[int, str] = {
        -15: "f", -12: "p", -9: "n", -6: "µ", -3: "m",
        0: "", 3: "k", 6: "M", 9: "G", 12: "T",
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Data bounds (the absolute extents the slider spans) and user-
        # selected clamp values. Kept separately so the user can clamp
        # tighter than the data range without losing the auto-detected
        # extents.
        self._data_min: float = 0.0
        self._data_max: float = 1.0
        self._low: float = 0.0
        self._high: float = 1.0
        self._cmap_name: str = "viridis"
        self._cmap_gradient: object | None = None
        # LUT colour stops + clamp end-colours, filled by _rebuild_gradient.
        self._cmap_stops: list[tuple[float, QColor]] = []
        self._clamp_lo_color: QColor = QColor(0, 0, 0)
        self._clamp_hi_color: QColor = QColor(255, 255, 255)
        self._dragging: str | None = None  # 'low', 'high', or None
        # Metric name + unit shown above the strip, e.g. "Voltage" / "V".
        # Kept separate so an engineering SI prefix (chosen per render
        # from the data range) can be folded into the unit at paint time
        # — e.g. a ~1 mV axis renders its title as "Voltage [mV]".
        self._label: str = ""
        self._unit: str = ""
        # Log-spaced value axis. When True the handle ↔ value mapping is
        # logarithmic; the data range must be strictly positive for it to
        # take effect (callers floor it — see PdnViewer._render).
        self._log: bool = False
        height = (self._CHIP_PAD + self._TITLE_H + self._MARGIN_TOP
                  + self._STRIP_HEIGHT + self._TICK_GAP + self._TICK_MARK
                  + self._TICK_LABEL_H + self._CHIP_PAD)
        self.setFixedSize(self._CHIP_W, height)
        self.setMouseTracking(True)
        self._rebuild_gradient()

    def setTitle(self, label: str, unit: str) -> None:
        """Set the heading shown above the strip, e.g. ``Voltage`` / ``V``.

        The unit gets an SI prefix folded in at paint time (see
        :meth:`_display_title` / :meth:`_si_scale`), so the rendered
        heading may read ``Voltage [mV]`` even though ``unit`` is ``V``.
        """
        self._label = label or ""
        self._unit = unit or ""
        self.update()

    def setColormap(self, cmap_name: str) -> None:
        self._cmap_name = cmap_name
        self._rebuild_gradient()
        self.update()

    def setLogScale(self, on: bool) -> None:
        """Switch the strip between a linear and a log-spaced value axis.
        Log needs a strictly positive data range; :meth:`_value_to_t`
        falls back to linear if that doesn't hold."""
        on = bool(on)
        if on != self._log:
            self._log = on
            self.update()

    def setDataRange(self, data_min: float, data_max: float) -> None:
        """Set the absolute data extents (full slider range)."""
        if not math.isfinite(data_min) or not math.isfinite(data_max):
            return
        if data_max <= data_min:
            data_max = data_min + 1e-12
        self._data_min = float(data_min)
        self._data_max = float(data_max)
        # Clamp the current selection to the new bounds.
        self._low = max(self._data_min, min(self._low, self._data_max))
        self._high = min(self._data_max, max(self._high, self._data_min))
        if self._high <= self._low:
            self._high = self._low + (self._data_max - self._data_min) * 1e-6
        self.update()

    def setSelectedRange(self, low: float, high: float,
                         emit: bool = False) -> None:
        """Programmatically set the clamp values. Used to reset on a fresh
        render so the next mode starts with full-range coverage."""
        if not (math.isfinite(low) and math.isfinite(high)):
            return
        if high <= low:
            high = low + (self._data_max - self._data_min) * 1e-6
        self._low = float(low)
        self._high = float(high)
        self.update()
        if emit:
            self.rangeChanged.emit(self._low, self._high)

    def selectedRange(self) -> tuple[float, float]:
        return self._low, self._high

    def dataRange(self) -> tuple[float, float]:
        return self._data_min, self._data_max

    # --- Painting -----------------------------------------------------------

    def _rebuild_gradient(self) -> None:
        """Pre-compute the colormap LUT stops and the clamp end-colors. Cached
        so paintEvent doesn't re-sample matplotlib on every repaint (drags fire
        many repaints per second). The QLinearGradient geometry itself is built
        per-paint because it must span the *clamp window* [low, high] — not the
        whole data-range strip — to match how the GPU maps the LUT (full LUT
        stretched across the window, flat clamp colours outside)."""
        cmap = _mpl_cm.get_cmap(self._cmap_name)
        n_stops = 32
        self._cmap_stops = []
        for i in range(n_stops):
            t = i / (n_stops - 1)
            r, g, b, a = cmap(t)
            self._cmap_stops.append((t, QColor.fromRgbF(r, g, b, a)))
        # Clamp colours: the GPU renders values below `low` at LUT[0] and above
        # `high` at LUT[1], so the bar's out-of-window regions use these.
        self._clamp_lo_color = self._cmap_stops[0][1]
        self._clamp_hi_color = self._cmap_stops[-1][1]
        # Retained for any external reader; geometry is rebuilt per paint.
        self._cmap_gradient = True

    def _strip_rect(self) -> QRectF:
        left = self._CHIP_PAD + self._MARGIN_X
        top = self._CHIP_PAD + self._TITLE_H + self._MARGIN_TOP
        return QRectF(
            left,
            top,
            max(1.0, self.width() - 2.0 * left),
            self._STRIP_HEIGHT,
        )

    def _log_ok(self) -> bool:
        """True when the log axis can actually be used — it needs a
        strictly positive, non-degenerate data range."""
        return (self._log and self._data_min > 0.0
                and self._data_max > self._data_min)

    def _value_to_t(self, value: float) -> float:
        """Normalised 0..1 position of ``value`` along the strip — log-
        spaced when the log axis is active, linear otherwise."""
        if self._log_ok():
            lo = math.log10(self._data_min)
            hi = math.log10(self._data_max)
            v = math.log10(max(value, self._data_min))
            t = (v - lo) / max(hi - lo, 1e-30)
        else:
            span = max(self._data_max - self._data_min, 1e-30)
            t = (value - self._data_min) / span
        return min(1.0, max(0.0, t))

    def _value_to_x(self, value: float) -> float:
        rect = self._strip_rect()
        return rect.left() + self._value_to_t(value) * rect.width()

    def _x_to_value(self, x: float) -> float:
        rect = self._strip_rect()
        if rect.width() <= 0:
            return self._data_min
        t = min(1.0, max(0.0, (x - rect.left()) / rect.width()))
        if self._log_ok():
            lo = math.log10(self._data_min)
            hi = math.log10(self._data_max)
            return 10.0 ** (lo + t * (hi - lo))
        return self._data_min + t * (self._data_max - self._data_min)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        # Chip background — an opaque dark rect + light border, matching
        # the GL viewer's legend chip so the strip reads on top of the
        # heatmap regardless of the colours underneath.
        p.fillRect(self.rect(), self._CHIP_BG)
        p.setPen(self._CHIP_BORDER)
        p.setBrush(Qt.NoBrush)
        p.drawRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5))

        # Title ("Voltage [mV]" etc. — SI prefix folded in per render).
        title = self._display_title()
        if title:
            f = p.font()
            f.setPointSizeF(9.0)
            f.setBold(True)
            p.setFont(f)
            p.setPen(self._TEXT)
            p.drawText(
                QRectF(self._CHIP_PAD, self._CHIP_PAD,
                       self.width() - 2 * self._CHIP_PAD, self._TITLE_H),
                Qt.AlignLeft | Qt.AlignVCenter, title,
            )

        # Gradient strip + border. The full LUT is stretched across the clamp
        # window [low, high] — exactly what the GPU does — with flat clamp
        # colours outside it (values below `low` render at LUT[0], above `high`
        # at LUT[1]). Previously the LUT spanned the whole data-range strip, so
        # a given hue on the bar sat at a different value than on the board
        # whenever the window ≠ the data range (the out-of-the-box state for
        # Current/Power Density) — users mis-read every saturated region.
        strip = self._strip_rect()
        low_x = self._value_to_x(self._low)
        high_x = self._value_to_x(self._high)
        if getattr(self, "_cmap_stops", None):
            # Flat clamp colours outside the window.
            if low_x > strip.left():
                p.fillRect(QRectF(strip.left(), strip.top(),
                                  low_x - strip.left(), strip.height()),
                           self._clamp_lo_color)
            if high_x < strip.right():
                p.fillRect(QRectF(high_x, strip.top(),
                                  strip.right() - high_x, strip.height()),
                           self._clamp_hi_color)
            # Full LUT across the window, in screen (pixel) coordinates. The
            # LUT-to-screen map is linear between low_x and high_x in BOTH
            # linear and log modes (the endpoints already carry the mode's
            # spacing), so linear stops are correct either way.
            if high_x > low_x:
                grad = QLinearGradient(low_x, 0.0, high_x, 0.0)
                for t, color in self._cmap_stops:
                    grad.setColorAt(t, color)
                p.fillRect(QRectF(low_x, strip.top(),
                                  high_x - low_x, strip.height()), grad)
        p.setPen(self._STRIP_BORDER)
        p.setBrush(Qt.NoBrush)
        p.drawRect(strip)

        # Faintly dim the out-of-window regions so the active clamp window is
        # still visually obvious on top of the (now correct-hue) clamp colours.
        p.fillRect(QRectF(strip.left(), strip.top(),
                          low_x - strip.left(), strip.height()), self._MASK)
        p.fillRect(QRectF(high_x, strip.top(),
                          strip.right() - high_x, strip.height()), self._MASK)

        # Tick marks + value labels along the bottom edge.
        self._draw_ticks(p, strip)

        # Handle triangles (one pointing down at each clamp position).
        self._draw_handle(p, low_x)
        self._draw_handle(p, high_x)

    def _draw_handle(self, p: QPainter, x: float) -> None:
        top_y = self._strip_rect().top() - 1
        tri = QPolygonF([
            QPointF(x, top_y + 8),
            QPointF(x - self._HANDLE_HALF_W, top_y - 2),
            QPointF(x + self._HANDLE_HALF_W, top_y - 2),
        ])
        p.setPen(self._HANDLE_EDGE)
        p.setBrush(self._HANDLE_FILL)
        p.drawPolygon(tri)

    def _draw_ticks(self, p: QPainter, strip: QRectF) -> None:
        """Draw ``_N_TICKS`` evenly-spaced tick marks under the strip,
        each labelled with the data value at that position. Values are
        divided by the axis SI prefix's divisor so the labels stay plain
        numbers (the prefix lives in the title — see :meth:`_si_scale`)."""
        divisor, _prefix = self._si_scale(self._data_min, self._data_max,
                                          self._unit)
        f = p.font()
        f.setPointSizeF(8.0)
        f.setBold(False)
        p.setFont(f)
        n = self._N_TICKS
        mark_top = strip.bottom() + self._TICK_GAP
        mark_bot = mark_top + self._TICK_MARK
        label_top = mark_bot
        # Pre-compute every tick value so each label can be formatted
        # against its local spacing — constant in linear mode, but
        # varies along the axis in log mode.
        values: list[float] = []
        for i in range(n):
            t = i / (n - 1)
            x = strip.left() + t * strip.width()
            values.append(self._x_to_value(x) / divisor)
        for i in range(n):
            t = i / (n - 1)
            x = strip.left() + t * strip.width()
            value = values[i]
            if n >= 2:
                if i == 0:
                    step = abs(values[1] - values[0])
                elif i == n - 1:
                    step = abs(values[-1] - values[-2])
                else:
                    step = min(abs(values[i + 1] - values[i]),
                               abs(values[i] - values[i - 1]))
            else:
                step = 0.0
            p.setPen(self._TICK)
            p.drawLine(QPointF(x, mark_top), QPointF(x, mark_bot))
            text = self._fmt_tick(value, step)
            # Keep the end labels inside the chip — left-align the first,
            # right-align the last, centre the rest.
            if i == 0:
                box = QRectF(x, label_top, 90.0, self._TICK_LABEL_H)
                align = Qt.AlignLeft | Qt.AlignVCenter
            elif i == n - 1:
                box = QRectF(x - 90.0, label_top, 90.0, self._TICK_LABEL_H)
                align = Qt.AlignRight | Qt.AlignVCenter
            else:
                box = QRectF(x - 45.0, label_top, 90.0, self._TICK_LABEL_H)
                align = Qt.AlignHCenter | Qt.AlignVCenter
            p.setPen(self._TEXT)
            p.drawText(box, align, text)

    @classmethod
    def _si_scale(cls, data_min: float, data_max: float,
                  unit: str) -> tuple[float, str]:
        """Pick one engineering SI prefix for the whole axis.

        Returns ``(divisor, prefix_symbol)``: a tick value divided by
        ``divisor`` prints as a plain number, and ``prefix_symbol`` is
        prepended to the unit in the title (e.g. ``V`` → ``mV``). Falls
        back to ``(1.0, "")`` when there is no unit or the data
        magnitude is zero / non-finite.
        """
        mag = max(abs(data_min), abs(data_max))
        if not unit or not math.isfinite(mag) or mag <= 0.0:
            return 1.0, ""
        exp = int(math.floor(math.log10(mag) / 3.0) * 3)
        exp = max(min(cls._SI_PREFIXES), min(max(cls._SI_PREFIXES), exp))
        return 10.0 ** exp, cls._SI_PREFIXES[exp]

    def _display_title(self) -> str:
        """Heading shown above the strip, with the active SI prefix
        folded into the unit — e.g. ``Voltage [mV]``."""
        if not self._unit:
            return self._label
        _divisor, prefix = self._si_scale(
            self._data_min, self._data_max, self._unit)
        return f"{self._label} [{prefix}{self._unit}]"

    @staticmethod
    def _fmt_tick(value: float, step: float = 0.0) -> str:
        """Format an already-SI-scaled tick value as a plain number.

        No scientific notation — the SI prefix in the axis title
        carries the magnitude. Precision comes from ``step`` (the
        spacing to the neighbouring tick): each label shows ~2 sig
        figs of the step, so the whole axis reads at a consistent
        scale and values smaller than the step's resolution collapse
        to ``"0"`` instead of printing noise digits (e.g. -0.000331
        on a 0..2.66 axis → ``0``). When ``step`` is unavailable, falls
        back to ~3 sig figs of the value itself.

        Decimals are also capped against the value's own magnitude
        (``_MAX_SIG_FIGS`` total significant figures): a near-constant
        field around a large value (e.g. ≈100 V) would otherwise force
        many decimals onto a multi-digit integer part, producing long
        labels that collide along the axis. Larger numbers therefore
        show fewer decimal places."""
        if not math.isfinite(value):
            return "—"
        if step > 0.0 and math.isfinite(step):
            digits = max(0, 1 - int(math.floor(math.log10(step))))
        elif value == 0.0:
            return "0"
        else:
            digits = max(0, 2 - int(math.floor(math.log10(abs(value)))))
        # Trim decimals so the whole number never exceeds _MAX_SIG_FIGS
        # significant figures — i.e. the bigger the integer part, the
        # fewer decimals it keeps (values < 1 are left untouched).
        int_digits = (int(math.floor(math.log10(abs(value)))) + 1
                      if abs(value) >= 1.0 else 0)
        digits = min(digits, max(0, _GradientBar._MAX_SIG_FIGS - int_digits))
        digits = min(6, digits)
        # Round first so values within half a ULP of zero render as
        # plain "0" rather than "-0.00".
        rounded = round(value, digits)
        if rounded == 0.0:
            return "0"
        s = f"{rounded:.{digits}f}"
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return s or "0"

    # --- Mouse input --------------------------------------------------------

    def _hit_handle(self, x: float) -> str | None:
        """Pick whichever handle is closest to ``x`` within the grip radius."""
        low_x = self._value_to_x(self._low)
        high_x = self._value_to_x(self._high)
        grip = self._HANDLE_HALF_W + 4
        d_low = abs(x - low_x)
        d_high = abs(x - high_x)
        if d_low <= grip and d_low <= d_high:
            return "low"
        if d_high <= grip:
            return "high"
        return None

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton:
            return
        pos = event.position()
        strip = self._strip_rect()
        # Only the strip and the handle band just above it are draggable —
        # clicks on the title or the tick labels must not move a handle.
        if not (strip.top() - 11 <= pos.y() <= strip.bottom() + 2):
            return
        self._dragging = self._hit_handle(pos.x())
        if self._dragging is None:
            # Click on bare strip → move the nearest handle to that point.
            v = self._x_to_value(pos.x())
            if abs(v - self._low) <= abs(v - self._high):
                self._dragging = "low"
            else:
                self._dragging = "high"
            self._update_from_drag(pos.x())

    def mouseMoveEvent(self, event) -> None:
        if self._dragging is not None:
            self._update_from_drag(event.position().x())

    def mouseReleaseEvent(self, event) -> None:
        self._dragging = None

    def _update_from_drag(self, x_pixel: float) -> None:
        v = self._x_to_value(x_pixel)
        # Don't let the handles cross — leave at least 1e-6 of the data span
        # between them so the heatmap retains a tiny colour range.
        gap = max((self._data_max - self._data_min) * 1e-6, 1e-30)
        if self._dragging == "low":
            self._low = min(v, self._high - gap)
        elif self._dragging == "high":
            self._high = max(v, self._low + gap)
        self.update()
        self.rangeChanged.emit(self._low, self._high)




class ScaleController(QWidget):
    """Compound widget: title + gradient bar + Min/Max textboxes + Reset.

    Wraps :class:`_GradientBar` with editable text fields so users can
    enter precise clamp values OR drag the handles. Mirrors the
    "Scope Controller" panel commonly seen in CAD post-processors.
    """

    rangeChanged = Signal(float, float)
    # Emitted (with the matplotlib colormap name) when the user picks a
    # different colour scheme from the dropdown. Programmatic
    # :meth:`setColormap` calls are signal-blocked and do NOT emit this.
    colormapChanged = Signal(str)
    # Emitted (True = logarithmic) when the user changes the Scale
    # dropdown. Programmatic :meth:`setLogActive` does NOT emit it.
    scaleTypeChanged = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._unit: str = ""
        self._label: str = "Value"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.title_label = QLabel("<b>Colour scale</b>")
        layout.addWidget(self.title_label)

        # Colour-scheme picker — recolours the heatmap live. Sits directly
        # under the title so it reads as part of the colour-scale panel.
        cmap_row = QHBoxLayout()
        cmap_row.setContentsMargins(0, 0, 0, 0)
        cmap_row.setSpacing(6)
        self.cmap_label = QLabel("Scheme")
        self.cmap_label.setStyleSheet(
            f"QLabel {{ color: {_T()['fg_muted']}; font-size: 8pt; }}"
        )
        self.cmap_combo = QComboBox()
        for display_name, _mpl_name in _HEATMAP_COLORMAPS:
            self.cmap_combo.addItem(display_name)
        self.cmap_combo.setToolTip(
            "Colour scheme for the heatmap. Changing it recolours the "
            "viewport immediately — no re-solve needed."
        )
        self.cmap_combo.currentIndexChanged.connect(self._on_cmap_combo_changed)
        cmap_row.addWidget(self.cmap_label, 0)
        cmap_row.addWidget(self.cmap_combo, 1)
        layout.addLayout(cmap_row)

        # Linear / logarithmic value axis. Hidden by the viewer for
        # modes where a log scale is meaningless (Voltage) or undefined
        # (the signed Voltage Drop) — see PdnViewer._render.
        scale_row = QHBoxLayout()
        scale_row.setContentsMargins(0, 0, 0, 0)
        scale_row.setSpacing(6)
        self.scale_label = QLabel("Scale")
        self.scale_label.setStyleSheet(
            f"QLabel {{ color: {_T()['fg_muted']}; font-size: 8pt; }}"
        )
        self.scale_combo = QComboBox()
        self.scale_combo.addItems(["Linear", "Logarithmic"])
        self.scale_combo.setToolTip(
            "Linear or logarithmic colour scale. A log scale spreads the "
            "low end so the bulk of the board and the spikes are both "
            "readable at once — available for Current Density, Power "
            "Density and Via Current (those span many decades). Hidden "
            "for Voltage / Voltage Drop."
        )
        self.scale_combo.currentIndexChanged.connect(self._on_scale_combo_changed)
        scale_row.addWidget(self.scale_label, 0)
        scale_row.addWidget(self.scale_combo, 1)
        layout.addLayout(scale_row)

        # The gradient strip itself is NOT placed in this side panel — it
        # lives as an overlay on the heatmap viewer's bottom-left corner
        # (PdnViewer._build_ui reparents it onto the GL viewer). It is
        # created here, parentless, so this controller can keep driving
        # its colormap / range / title and relay its drag events; the
        # data-extent tick labels are drawn on the strip itself. This
        # panel keeps only the Scheme / Scale pickers + Min/Max editors.
        _t = _T()
        self.bar = _GradientBar()
        self.bar.rangeChanged.connect(self._on_bar_range_changed)

        edits_row = QHBoxLayout()
        edits_row.setContentsMargins(0, 0, 0, 0)
        edits_row.setSpacing(6)

        min_col = QVBoxLayout()
        min_col.setSpacing(2)
        min_lbl = QLabel("Min")
        min_lbl.setStyleSheet(
            f"QLabel {{ color: {_t['fg_muted']}; font-size: 8pt; }}"
        )
        self.min_edit = QLineEdit()
        self.min_edit.setValidator(_numeric_validator(self, bottom=None))
        self.min_edit.editingFinished.connect(self._on_edits_committed)
        min_col.addWidget(min_lbl)
        min_col.addWidget(self.min_edit)
        edits_row.addLayout(min_col, 1)

        max_col = QVBoxLayout()
        max_col.setSpacing(2)
        max_lbl = QLabel("Max")
        max_lbl.setStyleSheet(
            f"QLabel {{ color: {_t['fg_muted']}; font-size: 8pt; }}"
        )
        self.max_edit = QLineEdit()
        self.max_edit.setValidator(_numeric_validator(self, bottom=None))
        self.max_edit.editingFinished.connect(self._on_edits_committed)
        max_col.addWidget(max_lbl)
        max_col.addWidget(self.max_edit)
        edits_row.addLayout(max_col, 1)

        # Reset-to-data-range button — a small square button sitting to
        # the right of the Max box. The Min/Max columns carry a label
        # above the edit, so AlignBottom drops the button level with the
        # input boxes (not the taller label+edit column). The "↺" glyph
        # is the button's *text*, so it's painted in the themed text
        # colour and tracks light/dark mode — a QStyle standard icon
        # would not. Min/Max stay at equal stretch (1 each) so they keep
        # the same width; the button column is fixed-width (stretch 0).
        self.reset_button = QPushButton("↺")
        self.reset_button.setToolTip(
            "Reset to data range — restore Min / Max to the auto-detected "
            "data range for the current layer, rail, and mode."
        )
        _reset_font = self.reset_button.font()
        if _reset_font.pointSize() > 0:
            _reset_font.setPointSize(_reset_font.pointSize() + 2)
            self.reset_button.setFont(_reset_font)
        self.reset_button.clicked.connect(self._on_reset_clicked)
        edits_row.addWidget(self.reset_button, 0, Qt.AlignBottom)

        layout.addLayout(edits_row)

        # Theme-driven styling for the inputs so they don't fight the rest
        # of the UI. Colours come from the active theme dict so both dark
        # and light modes look right.
        self.setStyleSheet(
            f"QLineEdit {{ background-color: {_t['bg_input']}; color: {_t['fg']};"
            f"            border: 1px solid {_t['border']}; padding: 2px 4px; }}"
            f"QPushButton {{ background-color: {_t['bg_hover']}; color: {_t['fg']};"
            f"              border: 1px solid {_t['border']}; padding: 3px; }}"
            f"QPushButton:hover {{ background-color: {_t['bg_hover_strong']}; }}"
        )

        # Square the reset button to the Min/Max input height. Done after
        # the stylesheet is set so the line edit's size hint already
        # includes the themed padding + border.
        self.min_edit.ensurePolished()
        _edit_h = self.min_edit.sizeHint().height()
        self.reset_button.setFixedSize(_edit_h, _edit_h)

    def apply_theme(self) -> None:
        """Re-style the controller and its children to match the active
        theme. Called by :meth:`PdnViewer._refresh_inline_theme` after the
        user picks a new theme in the Settings tab."""
        t = current_theme()
        # The "Min" / "Max" / "Scheme" / "Scale" header labels weren't
        # captured as instance attributes; find them by their plain text
        # so we can re-style. (The gradient strip itself is a fixed-colour
        # overlay on the GL viewer — it has no theme to follow.)
        for lbl in self.findChildren(QLabel):
            if lbl is self.title_label:
                continue
            if lbl.text().strip() in ("Min", "Max", "Scheme", "Scale"):
                lbl.setStyleSheet(
                    f"QLabel {{ color: {t['fg_muted']}; font-size: 8pt; }}"
                )
        self.setStyleSheet(
            f"QLineEdit {{ background-color: {t['bg_input']}; color: {t['fg']};"
            f"            border: 1px solid {t['border']}; padding: 2px 4px; }}"
            f"QPushButton {{ background-color: {t['bg_hover']}; color: {t['fg']};"
            f"              border: 1px solid {t['border']}; padding: 3px; }}"
            f"QPushButton:hover {{ background-color: {t['bg_hover_strong']}; }}"
        )

    def setColormap(self, cmap_name: str) -> None:
        """Set the active colour scheme (matplotlib colormap name). Syncs
        both the gradient strip and the dropdown selection WITHOUT emitting
        :attr:`colormapChanged` — used for programmatic sync from the
        viewer (the dropdown itself is the only thing that emits)."""
        self.bar.setColormap(cmap_name)
        for i, (_display, mpl_name) in enumerate(_HEATMAP_COLORMAPS):
            if mpl_name == cmap_name and self.cmap_combo.currentIndex() != i:
                self.cmap_combo.blockSignals(True)
                self.cmap_combo.setCurrentIndex(i)
                self.cmap_combo.blockSignals(False)
                break

    def _on_cmap_combo_changed(self, index: int) -> None:
        """User picked a scheme from the dropdown — repaint the gradient
        strip and let the viewer recolour the heatmap."""
        if 0 <= index < len(_HEATMAP_COLORMAPS):
            mpl_name = _HEATMAP_COLORMAPS[index][1]
            self.bar.setColormap(mpl_name)
            self.colormapChanged.emit(mpl_name)

    def setLogActive(self, on: bool) -> None:
        """Set whether the gradient strip uses a log-spaced value axis.
        This is the *effective* state (the viewer forces linear for
        ineligible modes); the dropdown selection is left untouched so
        the user's preference survives a trip through such a mode."""
        self.bar.setLogScale(on)

    def setLogVisible(self, visible: bool) -> None:
        """Show or hide the Scale (Linear / Logarithmic) dropdown for the
        current mode. Hidden — not merely greyed — for modes where a log
        axis is meaningless (Voltage) or undefined (the signed Voltage
        Drop), so the panel only ever shows controls that do something."""
        self.scale_combo.setVisible(visible)
        self.scale_label.setVisible(visible)

    def _on_scale_combo_changed(self, index: int) -> None:
        """User switched the Linear / Logarithmic dropdown."""
        self.scaleTypeChanged.emit(index == 1)

    def setLabelUnit(self, label: str, unit: str) -> None:
        # The metric name + unit are shown as the title on the overlaid
        # strip; the side-panel header stays the static "Colour scale".
        self._label = label
        self._unit = unit
        self.bar.setTitle(label, unit)

    def setRange(self, data_min: float, data_max: float,
                 sel_min: float | None = None,
                 sel_max: float | None = None,
                 reset_selection: bool = True) -> None:
        """Set the data extents (full slider range) and optionally the
        initial Min/Max selection.

        ``sel_min`` / ``sel_max`` default to the data extents — pass a
        narrower window when the auto-detected data range contains
        outliers (e.g. FEM spikes at pinned-voltage vertices) so the
        default heatmap isn't crushed to one corner of the colour scale.
        The slider can still be dragged out to ``data_max`` to inspect
        the outlier.
        """
        self.bar.setDataRange(data_min, data_max)
        if reset_selection:
            initial_min = data_min if sel_min is None else sel_min
            initial_max = data_max if sel_max is None else sel_max
            self.bar.setSelectedRange(initial_min, initial_max, emit=False)
        # Always refresh the text boxes so they reflect the (possibly
        # re-clamped) selection.
        low, high = self.bar.selectedRange()
        self._set_edit_values(low, high)

    def selectedRange(self) -> tuple[float, float]:
        return self.bar.selectedRange()

    # --- Internal slots -----------------------------------------------------

    def _on_bar_range_changed(self, low: float, high: float) -> None:
        self._set_edit_values(low, high)
        self.rangeChanged.emit(low, high)

    def _on_edits_committed(self) -> None:
        try:
            low = _parse_numeric_text(self.min_edit.text())
            high = _parse_numeric_text(self.max_edit.text())
        except ValueError:
            return
        if high <= low:
            return
        self.bar.setSelectedRange(low, high, emit=False)
        self.rangeChanged.emit(low, high)

    def _on_reset_clicked(self) -> None:
        d_min, d_max = self.bar.dataRange()
        self.bar.setSelectedRange(d_min, d_max, emit=False)
        self._set_edit_values(d_min, d_max)
        self.rangeChanged.emit(d_min, d_max)

    def _set_edit_values(self, low: float, high: float) -> None:
        # ``blockSignals`` is the simple way to suppress editingFinished
        # while we update the text — otherwise typing in one box would
        # trigger a re-render mid-edit when focus moves.
        self.min_edit.blockSignals(True)
        self.max_edit.blockSignals(True)
        self.min_edit.setText(self._fmt(low))
        self.max_edit.setText(self._fmt(high))
        self.min_edit.blockSignals(False)
        self.max_edit.blockSignals(False)

    @staticmethod
    def _fmt(value: float) -> str:
        if not math.isfinite(value):
            return "—"
        if value == 0:
            return "0"
        if abs(value) < 1e-3 or abs(value) >= 1e5:
            return f"{value:.3e}"
        return f"{value:.4g}"
