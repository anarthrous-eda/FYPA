"""Small reusable widgets: sidebar toggle buttons, icon pixmaps, tooltips."""
from __future__ import annotations

from PySide6.QtCore import QMetaMethod, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QApplication,
    QColorDialog,
    QFrame,
    QLabel,
    QSizePolicy,
    QToolButton,
    QWidget,
)

from fypa.viewer.theme import _T, current_theme, current_theme_mode


# Hover-probe throttle. Mouse-move callbacks fire up to ~125 Hz on a
# high-DPI mouse; we don't need to recompute the probe more often than
# the eye can track. 33 ms ≈ 30 Hz feels live and removes a chunky
# fraction of mouse-event CPU during slow pans / hover-over moves.
HOVER_THROTTLE_S: float = 0.033


# Faster throttle used ONLY while the "Show cursor tooltip" option is
# on — the tooltip follows the cursor visibly, so the eye notices any
# stutter the bottom probe-label hides. Lower = smoother but more CPU
# spent on shapely.contains + _FastTriSampler query per move event.
# Set to 0.0 to disable throttling entirely (re-probe on every event).
CURSOR_TOOLTIP_THROTTLE_S: float = 0.008




# --- Altium-style "eye" visibility icons -----------------------------------

_EYE_PIXMAP_CACHE: dict[tuple[str, bool, bool, int], QPixmap] = {}



# Partial visibility: open-eye glyph, muted between eye_open and eye_closed.
_EYE_PARTIAL_BLEND: float = 0.32




def _eye_icon_color(theme: dict, *, open_: bool, partial: bool) -> QColor:
    if partial:
        keyed = theme.get("eye_partial")
        if keyed:
            return QColor(keyed)
        a, b = QColor(theme["eye_open"]), QColor(theme["eye_closed"])
        w = _EYE_PARTIAL_BLEND
        return QColor(
            int(a.red() * (1 - w) + b.red() * w),
            int(a.green() * (1 - w) + b.green() * w),
            int(a.blue() * (1 - w) + b.blue() * w),
        )
    if open_:
        return QColor(theme["eye_open"])
    return QColor(theme["eye_closed"])




def _make_eye_pixmap(open_: bool, *, partial: bool = False, size: int = 16) -> QPixmap:
    """Draw an Altium-style eye icon.

    ``open_`` drives the silhouette: an open eye, or a slashed one when this
    item's own copper is hidden. ``partial`` only mutes the colour, so all
    four combinations stay distinguishable — in particular hidden-with-
    visible-descendants keeps its slash instead of reading as visible.
    """
    px = QPixmap(size, size)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing, True)

    t = current_theme()
    color = _eye_icon_color(t, open_=open_, partial=partial)
    pen = QPen(color)
    pen.setWidthF(max(1.0, size * 0.09))
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)

    pad = size * 0.10
    cy = size / 2.0
    bulge = size * 0.42
    path = QPainterPath()
    path.moveTo(pad, cy)
    path.quadTo(size / 2.0, cy - bulge, size - pad, cy)
    path.quadTo(size / 2.0, cy + bulge, pad, cy)
    p.drawPath(path)

    p.setPen(Qt.NoPen)
    p.setBrush(color)
    r = size * 0.17
    p.drawEllipse(QPointF(size / 2.0, cy), r, r)

    if not open_:
        slash = QPen(color)
        slash.setWidthF(max(1.2, size * 0.11))
        slash.setCapStyle(Qt.RoundCap)
        p.setPen(slash)
        p.setBrush(Qt.NoBrush)
        m = size * 0.12
        p.drawLine(QPointF(m, size - m), QPointF(size - m, m))

    p.end()
    return px




def _eye_pixmap(open_: bool, size: int = 16, *, partial: bool = False) -> QPixmap:
    key = (current_theme_mode(), open_, partial, size)
    cached = _EYE_PIXMAP_CACHE.get(key)
    if cached is None:
        cached = _make_eye_pixmap(open_, partial=partial, size=size)
        _EYE_PIXMAP_CACHE[key] = cached
    return cached




# --- Editor net-focus ("show only this net") icon ---------------------------

_FOCUS_PIXMAP_CACHE: dict[tuple[str, bool, int], QPixmap] = {}




def _make_focus_pixmap(on: bool, *, size: int = 16) -> QPixmap:
    """Draw the net-table focus toggle — a crosshair / target glyph.

    Deliberately NOT an eye. The layer eyes are additive, per-layer and
    independent of each other; this control is exclusive and sticky — while
    it is on, every net but its own is gone from the viewport and no click
    in the canvas can release it. A different silhouette keeps the two from
    reading as the same kind of switch.

    ``on`` fills the bullseye and switches to the accent colour so the
    focused row is obvious; off is a hollow ring in the muted eye grey.
    """
    px = QPixmap(size, size)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing, True)

    t = current_theme()
    color = QColor(t["accent"] if on else t["eye_closed"])
    pen = QPen(color)
    pen.setWidthF(max(1.0, size * (0.12 if on else 0.09)))
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)

    c = size / 2.0
    r = size * 0.30
    p.drawEllipse(QPointF(c, c), r, r)
    # Four crosshair arms from the ring out to the icon edge.
    arm_in = r + size * 0.06
    arm_out = size * 0.47
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        p.drawLine(QPointF(c + dx * arm_in, c + dy * arm_in),
                   QPointF(c + dx * arm_out, c + dy * arm_out))

    p.setPen(Qt.NoPen)
    p.setBrush(color)
    dot = size * (0.15 if on else 0.09)
    p.drawEllipse(QPointF(c, c), dot, dot)

    p.end()
    return px




def _focus_pixmap(on: bool, size: int = 16) -> QPixmap:
    key = (current_theme_mode(), on, size)
    cached = _FOCUS_PIXMAP_CACHE.get(key)
    if cached is None:
        cached = _make_focus_pixmap(on, size=size)
        _FOCUS_PIXMAP_CACHE[key] = cached
    return cached




def _floating_tooltip_qss(font_family: str | None = None,
                          font_size: str = "9pt",
                          padding: str = "4px 8px") -> str:
    t = _T()
    family = f" font-family: {font_family};" if font_family else ""
    return (
        "QLabel {"
        f" background-color: {t['bg']};"
        f" color: {t['fg']};"
        f" border: 1px solid {t['border']};"
        f" padding: {padding};"
        f"{family}"
        f" font-size: {font_size};"
        "}"
    )




def _make_floating_tooltip(parent, *, font_family: str | None = None,
                           font_size: str = "9pt",
                           padding: str = "4px 8px") -> QLabel:
    """A frameless, non-focusing tooltip label positioned in GLOBAL coordinates.

    ``Qt.ToolTip`` makes the label a top-level window, so it is never clipped
    to its parent's rect and is placed from :meth:`QCursor.pos`. Going through
    the cursor also sidesteps two traps in putting a label over a matplotlib
    canvas: mpl mouse events are bottom-origin where ``QWidget.move`` is
    top-origin, and they carry physical device pixels where ``move`` takes
    logical ones — so using ``event.x``/``event.y`` directly mirrors the label
    about the canvas midline and displaces it by the device-pixel ratio.

    ``WA_TransparentForMouseEvents`` stops the label stealing the very hover
    events that drive it when it slides under the cursor.
    """
    label = QLabel(parent, Qt.ToolTip | Qt.FramelessWindowHint)
    label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
    label.setAttribute(Qt.WA_ShowWithoutActivating, True)
    label.setFocusPolicy(Qt.NoFocus)
    label.setStyleSheet(_floating_tooltip_qss(font_family, font_size, padding))
    label.hide()
    return label




def _move_tooltip_to_cursor(label) -> None:
    """Anchor below-right of the cursor (Windows convention), clamped to the
    current screen so the label stays fully visible near an edge."""
    gx = QCursor.pos().x() + 16
    gy = QCursor.pos().y() + 20
    screen = label.screen() or QApplication.primaryScreen()
    if screen is not None:
        geo = screen.availableGeometry()
        gx = min(gx, geo.right() - label.width() - 2)
        gy = min(gy, geo.bottom() - label.height() - 2)
        gx = max(gx, geo.left() + 2)
        gy = max(gy, geo.top() + 2)
    label.move(gx, gy)




def _qt_widget_alive(widget) -> bool:
    """True when ``widget`` is a live Qt C++ object (not already deleted)."""
    if widget is None:
        return False
    try:
        from shiboken6 import isValid
        return bool(isValid(widget))
    except Exception:
        return False




class EyeButton(QToolButton):
    """Altium-style eye-icon toggle for layer visibility."""

    toggled_visible = Signal(bool)
    shift_clicked = Signal()
    # Ctrl+Click — the rail-tree subnet eyes use this for SERIES-subtree
    # fan-out. Opt-in like Shift: an eye whose owner never connects it keeps
    # the plain toggle rather than turning Ctrl+Click into a dead click.
    ctrl_clicked = Signal()

    _SHIFT_TIP = (
        "\nShift+Click: show only this item; again to invert (hide this, show others)"
    )
    # Appended to a badge eye's own tip so the muted icon has an explanation.
    _MIXED_TIP = "\nThis SERIES subtree is partly hidden"

    def __init__(self, parent=None, *, visible: bool = True,
                 icon_size: int = 16,
                 tip_show: str = "Show layer",
                 tip_hide: str = "Hide layer",
                 shift_isolatable: bool = False,
                 partial_is_badge: bool = False) -> None:
        super().__init__(parent)
        self._visible = bool(visible)
        self._partial = False
        self._icon_size = icon_size
        self._tip_show = tip_show
        self._tip_hide = tip_hide
        # Only eyes whose owner connects ``shift_clicked`` may claim Shift: on
        # any other eye, swallowing the modifier turns a Shift+click into a
        # dead click and the advertised tip would be a lie.
        self._shift_isolatable = bool(shift_isolatable)
        shift_tip = self._SHIFT_TIP if self._shift_isolatable else ""
        # Two kinds of eye share this class:
        #
        # * Aggregate ("All layers", a rail row) — owns no copper of its own,
        #   so partial means "some children on" and a click means "show them
        #   all".
        # * Badge (a rail-tree subnet node) — owns this net's copper. Partial
        #   is information *about its descendants*; it must never veto the
        #   plain toggle, or the node's own copper cannot be hidden while any
        #   descendant differs. Subtree fan-out is Ctrl+Click's job.
        self._partial_is_badge = bool(partial_is_badge)
        self._tip_partial = (
            "Some subnet nets visible — click to show all" + shift_tip
        )
        self._shift_tip = shift_tip
        # Set by the owner after construction (an unsolved rail explaining why
        # it is disabled, say). Once set, state changes must not overwrite it.
        self._custom_tip = False
        self._setting_own_tip = False
        self._press_mods: Qt.KeyboardModifiers = Qt.NoModifier
        self.setAutoRaise(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setIconSize(QSize(icon_size, icon_size))
        self.setFixedSize(icon_size + 6, icon_size + 6)
        self.setFocusPolicy(Qt.NoFocus)
        self._apply_icon()
        self.clicked.connect(self._on_clicked)

    def isVisibleState(self) -> bool:
        return self._visible

    def isPartialState(self) -> bool:
        return self._partial

    def setVisibleState(
        self, on: bool, *, partial: bool = False, emit: bool = True,
    ) -> None:
        on = bool(on)
        # Partial may sit on an off badge eye (this net hidden, descendants
        # shown). An aggregate eye has no such state — its owner only ever
        # passes partial together with on.
        partial = bool(partial)
        if on == self._visible and partial == self._partial:
            return
        self._visible = on
        self._partial = partial
        self._apply_icon()
        if emit:
            self.toggled_visible.emit(self._visible)

    def setToolTip(self, tip: str) -> None:
        if not self._setting_own_tip:
            self._custom_tip = True
        super().setToolTip(tip)

    def _set_own_tooltip(self, tip: str) -> None:
        self._setting_own_tip = True
        try:
            self.setToolTip(tip)
        finally:
            self._setting_own_tip = False

    def _apply_icon(self) -> None:
        # The slash tracks this eye's *own* state; partial only mutes the
        # colour. An off badge eye whose descendants are on must not borrow
        # the open silhouette, or hidden copper looks exactly like drawn
        # copper.
        self.setIcon(QIcon(_eye_pixmap(
            self._visible, self._icon_size, partial=self._partial,
        )))
        if self._custom_tip:
            return
        if self._partial and not self._partial_is_badge:
            self._set_own_tooltip(self._tip_partial)
            return
        base = self._tip_hide if self._visible else self._tip_show
        if self._partial:
            base += self._MIXED_TIP
        self._set_own_tooltip(base + self._shift_tip)

    def mousePressEvent(self, event) -> None:
        # Capture modifiers at press time — clicked fires on release and
        # QApplication.keyboardModifiers() may already be cleared.
        self._press_mods = event.modifiers()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        # ``clicked`` is emitted from inside the base implementation, so
        # ``_on_clicked`` still sees the press modifiers. Clearing afterwards
        # stops a press that never produced a click (drag off the button, a
        # cancelled press) from leaking Shift/Ctrl into the next click —
        # including programmatic ``click()`` and accessibility activation,
        # which never go through ``mousePressEvent`` at all.
        try:
            super().mouseReleaseEvent(event)
        finally:
            self._press_mods = Qt.NoModifier

    def _signal_connected(self, signal) -> bool:
        return self.isSignalConnected(QMetaMethod.fromSignal(signal))

    def _on_clicked(self) -> None:
        mods = self._press_mods
        self._press_mods = Qt.NoModifier
        if self._shift_isolatable and mods & Qt.ShiftModifier:
            self.shift_clicked.emit()
            return
        if (mods & Qt.ControlModifier) and self._signal_connected(
            self.ctrl_clicked,
        ):
            self.ctrl_clicked.emit()
            return
        if self._partial and not self._partial_is_badge:
            # Aggregate eye: partial → show every child.
            self.setVisibleState(True, partial=False)
            return
        # Badge eye (and any plain eye): toggle this eye's own copper. The
        # partial badge is recomputed by the owner from the new subtree
        # state, so it is carried across rather than cleared here.
        self.setVisibleState(not self._visible, partial=self._partial)




# --- Wire-mesh / solid fill toggle icons -----------------------------------

_FILL_PIXMAP_CACHE: dict[tuple[str, bool, int], QPixmap] = {}




def _make_fill_pixmap(solid: bool, *, size: int = 16) -> QPixmap:
    """Draw a fill-style icon for an overlay row.

    ``solid`` = True  → a solid-filled square (overlay drawn as solid fill).
    ``solid`` = False → an outlined square (overlay drawn as a wire mesh).
    """
    px = QPixmap(size, size)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing, True)

    color = QColor(current_theme()["eye_open"])
    inset = size * 0.20
    rect = QRectF(inset, inset, size - 2 * inset, size - 2 * inset)
    pen = QPen(color)
    pen.setWidthF(max(1.2, size * 0.11))
    pen.setJoinStyle(Qt.MiterJoin)
    p.setPen(pen)
    p.setBrush(color if solid else Qt.NoBrush)
    p.drawRect(rect)

    p.end()
    return px




def _fill_pixmap(solid: bool, size: int = 16) -> QPixmap:
    key = (current_theme_mode(), solid, size)
    cached = _FILL_PIXMAP_CACHE.get(key)
    if cached is None:
        cached = _make_fill_pixmap(solid, size=size)
        _FILL_PIXMAP_CACHE[key] = cached
    return cached




class FillToggleButton(QToolButton):
    """Outline-vs-solid fill toggle for an overlay row.

    Mirrors :class:`EyeButton` — a small auto-raise tool button whose icon
    flips between an outlined square (wire mesh) and a solid square."""

    toggled_fill = Signal(bool)  # True == solid fill

    def __init__(self, parent=None, *, solid: bool = True,
                 icon_size: int = 16) -> None:
        super().__init__(parent)
        self._solid = bool(solid)
        self._icon_size = icon_size
        self.setAutoRaise(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setIconSize(QSize(icon_size, icon_size))
        self.setFixedSize(icon_size + 6, icon_size + 6)
        self.setFocusPolicy(Qt.NoFocus)
        self._apply_icon()
        self.clicked.connect(self._on_clicked)

    def isSolid(self) -> bool:
        return self._solid

    def setSolid(self, on: bool, *, emit: bool = True) -> None:
        on = bool(on)
        if on == self._solid:
            return
        self._solid = on
        self._apply_icon()
        if emit:
            self.toggled_fill.emit(self._solid)

    def _apply_icon(self) -> None:
        self.setIcon(QIcon(_fill_pixmap(self._solid, self._icon_size)))
        self.setToolTip(
            "Solid fill — click for wire mesh" if self._solid
            else "Wire mesh — click for solid fill"
        )

    def _on_clicked(self) -> None:
        self.setSolid(not self._solid)




# --- Layer-outline toggle icons --------------------------------------------

_OUTLINE_PIXMAP_CACHE: dict[tuple[str, bool, int], QPixmap] = {}




def _make_outline_pixmap(on: bool, *, size: int = 16) -> QPixmap:
    """Draw a layer-outline toggle icon.

    ``on``  = True  → a muted square wrapped by a bright traced contour
                      (layer outlines shown).
    ``on``  = False → just the muted square with a faint dotted contour
                      (layer outlines hidden).
    """
    px = QPixmap(size, size)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing, True)

    t = current_theme()
    body = QColor(t["eye_closed"])
    trace = QColor(t["eye_open"] if on else t["eye_closed"])

    # Inner filled square — the copper polygon being traced.
    inset = size * 0.32
    inner = QRectF(inset, inset, size - 2 * inset, size - 2 * inset)
    p.setPen(Qt.NoPen)
    p.setBrush(body)
    p.drawRect(inner)

    # Outer square outline — the traced layer boundary.
    o = size * 0.14
    outer = QRectF(o, o, size - 2 * o, size - 2 * o)
    pen = QPen(trace)
    pen.setJoinStyle(Qt.MiterJoin)
    if on:
        pen.setWidthF(max(1.2, size * 0.11))
    else:
        pen.setWidthF(max(1.0, size * 0.075))
        pen.setStyle(Qt.DotLine)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawRect(outer)

    p.end()
    return px




def _outline_pixmap(on: bool, size: int = 16) -> QPixmap:
    key = (current_theme_mode(), on, size)
    cached = _OUTLINE_PIXMAP_CACHE.get(key)
    if cached is None:
        cached = _make_outline_pixmap(on, size=size)
        _OUTLINE_PIXMAP_CACHE[key] = cached
    return cached




class OutlineToggleButton(QToolButton):
    """Layer-outline visibility toggle for the "All Rails" row.

    Mirrors :class:`FillToggleButton` — a small auto-raise tool button,
    the same size as the wire-mesh / solid toggle. Its icon flips between
    a bright traced contour (outlines shown) and a faint dotted one
    (outlines hidden). Drives the same state the old "Show layer
    outlines" checkbox used to. Lives on the rails row because only the
    currently-visible rails' copper gets traced."""

    toggled_outline = Signal(bool)  # True == layer outlines shown

    def __init__(self, parent=None, *, on: bool = False,
                 icon_size: int = 16) -> None:
        super().__init__(parent)
        self._on = bool(on)
        self._icon_size = icon_size
        self.setAutoRaise(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setIconSize(QSize(icon_size, icon_size))
        self.setFixedSize(icon_size + 6, icon_size + 6)
        self.setFocusPolicy(Qt.NoFocus)
        self._apply_icon()
        self.clicked.connect(self._on_clicked)

    def isOn(self) -> bool:
        return self._on

    def setOn(self, on: bool, *, emit: bool = True) -> None:
        on = bool(on)
        if on == self._on:
            return
        self._on = on
        self._apply_icon()
        if emit:
            self.toggled_outline.emit(self._on)

    def toggle(self) -> None:
        self.setOn(not self._on)

    def _apply_icon(self) -> None:
        self.setIcon(QIcon(_outline_pixmap(self._on, self._icon_size)))
        self.setToolTip(
            "Layer outlines shown (O) — click to hide" if self._on
            else "Layer outlines hidden (O) — click to show"
        )

    def _on_clicked(self) -> None:
        self.setOn(not self._on)




# --- Split / merge toggle icons --------------------------------------------

_SPLIT_PIXMAP_CACHE: dict[tuple[str, bool, int], QPixmap] = {}




def _make_split_pixmap(split: bool, *, size: int = 16) -> QPixmap:
    """Draw a split / merge icon for overlay rows with a top + bottom side.

    ``split`` = False → one square with a dashed mid-line ("click to split").
    ``split`` = True  → two stacked squares with a gap ("click to merge").
    """
    px = QPixmap(size, size)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing, True)

    color = QColor(current_theme()["eye_open"])
    pen = QPen(color)
    pen.setWidthF(max(1.0, size * 0.09))
    pen.setJoinStyle(Qt.MiterJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)

    inset = size * 0.20
    w = size - 2 * inset
    if split:
        # Two separate halves with a clear gap — the result of a split.
        gap = size * 0.14
        h = (w - gap) / 2.0
        p.drawRect(QRectF(inset, inset, w, h))
        p.drawRect(QRectF(inset, inset + h + gap, w, h))
    else:
        # One box with a dashed line marking where it would be cut.
        p.drawRect(QRectF(inset, inset, w, w))
        dash = QPen(color)
        dash.setWidthF(max(1.0, size * 0.09))
        dash.setStyle(Qt.DashLine)
        p.setPen(dash)
        cy = size / 2.0
        p.drawLine(QPointF(inset, cy), QPointF(inset + w, cy))

    p.end()
    return px




def _split_pixmap(split: bool, size: int = 16) -> QPixmap:
    key = (current_theme_mode(), split, size)
    cached = _SPLIT_PIXMAP_CACHE.get(key)
    if cached is None:
        cached = _make_split_pixmap(split, size=size)
        _SPLIT_PIXMAP_CACHE[key] = cached
    return cached




class SplitButton(QToolButton):
    """Toggle that splits an overlay row into separate Top / Bottom rows."""

    toggled_split = Signal(bool)

    def __init__(self, parent=None, *, split: bool = False,
                 icon_size: int = 16) -> None:
        super().__init__(parent)
        self._split = bool(split)
        self._icon_size = icon_size
        self.setAutoRaise(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setIconSize(QSize(icon_size, icon_size))
        self.setFixedSize(icon_size + 6, icon_size + 6)
        self.setFocusPolicy(Qt.NoFocus)
        self._apply_icon()
        self.clicked.connect(self._on_clicked)

    def isSplit(self) -> bool:
        return self._split

    def setSplit(self, on: bool, *, emit: bool = True) -> None:
        on = bool(on)
        if on == self._split:
            return
        self._split = on
        self._apply_icon()
        if emit:
            self.toggled_split.emit(self._split)

    def _apply_icon(self) -> None:
        self.setIcon(QIcon(_split_pixmap(self._split, self._icon_size)))
        self.setToolTip(
            "Merge the Top / Bottom rows back into one" if self._split
            else "Split into separate Top / Bottom rows"
        )

    def _on_clicked(self) -> None:
        self.setSplit(not self._split)




# --- Transparency control --------------------------------------------------

# Nine-step transparency grid (12.5% per step): 0, 12.5, 25, 37.5, 50, 62.5,
# 75, 87.5, 100 %. Stored as an integer step count [0..8] on the button —
# the alpha applied to the row's geometry is (8 - step) / 8 (0%
# transparency == fully opaque == step 0). A plain click moves by the
# coarse delta (two steps == 25 %) and wraps around; an alt-click moves by
# the fine delta (one step == 12.5 %) and clamps at 0 / 100 %. Shift
# reverses the direction (alone or with alt).
_TRANSPARENCY_STEPS = 9


_TRANSPARENCY_COARSE_DELTA = 2


_TRANSPARENCY_FINE_DELTA = 1


_TRANSPARENCY_PIXMAP_CACHE: dict[tuple[str, int, int], QPixmap] = {}




def _make_transparency_pixmap(step: int, *, size: int = 16) -> QPixmap:
    """Pie-chart icon for the per-row transparency control.

    ``step`` is the number of eighth-slices removed from a white disc;
    coarse clicks remove two eighths (a quarter) at a time, alt-clicks
    remove one eighth (12.5 %). 0 = full disc (0% transparent), 8 = empty
    (100% transparent — only the faint reference outline remains so the
    button stays visible)."""
    px = QPixmap(size, size)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing, True)

    t = current_theme()
    # Faint reference circle so the button still reads as a control at
    # 100% transparency (when the white pie has fully vanished).
    inset = size * 0.16
    rect = QRectF(inset, inset, size - 2 * inset, size - 2 * inset)
    ring = QPen(QColor(t["eye_closed"]))
    ring.setWidthF(max(1.0, size * 0.08))
    p.setPen(ring)
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(rect)

    remaining = max(0, _TRANSPARENCY_STEPS - 1 - int(step))
    if remaining > 0:
        # Start the pie at 12 o'clock (90°) and sweep counter-clockwise.
        # As ``remaining`` decreases the trailing end retracts back toward
        # 12 along the clockwise direction, so each click visually removes
        # the next eighth slice clockwise (upper-right first, then sweeping
        # round to the upper-left as transparency rises).
        span_deg = remaining * (360.0 / (_TRANSPARENCY_STEPS - 1))
        path = QPainterPath()
        path.moveTo(rect.center())
        path.arcTo(rect, 90.0, float(span_deg))
        path.closeSubpath()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#ffffff"))
        p.drawPath(path)
        # Re-stroke the reference outline so the white pie doesn't bleed
        # over the edge after antialiasing.
        p.setPen(ring)
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(rect)

    p.end()
    return px




def _transparency_pixmap(step: int, size: int = 16) -> QPixmap:
    key = (current_theme_mode(), int(step), size)
    cached = _TRANSPARENCY_PIXMAP_CACHE.get(key)
    if cached is None:
        cached = _make_transparency_pixmap(step, size=size)
        _TRANSPARENCY_PIXMAP_CACHE[key] = cached
    return cached




def _transparency_percent(step: int) -> float:
    """Percentage the icon currently advertises for a given step count."""
    return 100.0 * step / (_TRANSPARENCY_STEPS - 1)




def _transparency_alpha(step: int) -> float:
    """Alpha (0..1) that the row's geometry should be drawn at."""
    return (_TRANSPARENCY_STEPS - 1 - int(step)) / float(_TRANSPARENCY_STEPS - 1)




class TransparencyButton(QToolButton):
    """Cycles a row's transparency through 0..100 % in 12.5 % increments.

    Click behaviour
    ---------------
    * Plain click       — +25 % (coarse), wraps 100 % → 0 %.
    * Shift+click       — -25 % (coarse), wraps 0 % → 100 %.
    * Right-click       — -25 % (coarse), wraps 0 % → 100 %.
    * Alt+click         — +12.5 % (fine), clamped at 100 %.
    * Alt+Shift+click   — -12.5 % (fine), clamped at 0 %.
    * Alt+Right-click   — -12.5 % (fine), clamped at 0 %.
    """

    toggled_transparency = Signal(int)  # 0..(_TRANSPARENCY_STEPS-1)

    def __init__(self, parent=None, *, step: int = 0,
                 icon_size: int = 16) -> None:
        super().__init__(parent)
        self._step = int(step) % _TRANSPARENCY_STEPS
        self._icon_size = icon_size
        self._press_mods: Qt.KeyboardModifiers = Qt.NoModifier
        self._press_button: Qt.MouseButton = Qt.NoButton
        self.setAutoRaise(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setIconSize(QSize(icon_size, icon_size))
        self.setFixedSize(icon_size + 6, icon_size + 6)
        self.setFocusPolicy(Qt.NoFocus)
        self._apply_icon()
        self.clicked.connect(self._on_clicked)

    def step(self) -> int:
        return self._step

    def alpha(self) -> float:
        return _transparency_alpha(self._step)

    def setStep(self, step: int, *, emit: bool = True) -> None:
        s = int(step) % _TRANSPARENCY_STEPS
        if s == self._step:
            return
        self._step = s
        self._apply_icon()
        if emit:
            self.toggled_transparency.emit(self._step)

    def _apply_icon(self) -> None:
        self.setIcon(QIcon(_transparency_pixmap(self._step, self._icon_size)))
        pct = _transparency_percent(self._step)
        # Drop the trailing zero on the 25 %-grid values so the common
        # multiples-of-25 read short; fine-grid (12.5 / 37.5 / ...) keeps
        # the half.
        pct_text = (f"{pct:.1f}".rstrip("0").rstrip(".")) + "%"
        self.setToolTip(
            f"Transparency: {pct_text}\n"
            "Click: +25 %    Shift+Click / Right-Click: -25 %    (wrap)\n"
            "Alt+Click: +12.5 %    Alt+Shift+Click / Alt+Right-Click: "
            "-12.5 %    (stop at 0 % / 100 %)"
        )

    def mousePressEvent(self, event) -> None:
        # Capture the modifiers at press time — QToolButton.clicked fires
        # on release, and QApplication.keyboardModifiers() at that moment
        # would race the user lifting Alt / Shift before the mouse button.
        self._press_mods = event.modifiers()
        self._press_button = event.button()
        if event.button() == Qt.RightButton:
            # QAbstractButton.clicked only fires for the left button, so
            # handle the right-button press directly without forwarding.
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.RightButton:
            mods = self._press_mods
            pressed_right = self._press_button == Qt.RightButton
            self._press_mods = Qt.NoModifier
            self._press_button = Qt.NoButton
            # Treat as a click only if the press also started on this
            # widget and the release is still inside its bounds.
            if pressed_right and self.rect().contains(event.position().toPoint()):
                self._apply_step_delta(reverse=True,
                                       fine=bool(mods & Qt.AltModifier))
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _on_clicked(self) -> None:
        mods = self._press_mods
        self._press_mods = Qt.NoModifier
        self._press_button = Qt.NoButton
        self._apply_step_delta(reverse=bool(mods & Qt.ShiftModifier),
                               fine=bool(mods & Qt.AltModifier))

    def _apply_step_delta(self, *, reverse: bool, fine: bool) -> None:
        delta = (_TRANSPARENCY_FINE_DELTA if fine
                 else _TRANSPARENCY_COARSE_DELTA)
        if reverse:
            delta = -delta
        max_step = _TRANSPARENCY_STEPS - 1
        if fine:
            # Fine clicks stop at the boundaries so the user can hold Alt
            # and nudge precisely without overshooting past 0 % / 100 %.
            new_step = max(0, min(max_step, self._step + delta))
        else:
            # Coarse clicks "snap, then wrap": land on the boundary first,
            # and only the next click *past* the boundary jumps to the
            # opposite end. Preserves the original "click at 100 % returns
            # to 0 %" feel even when the current step is off the coarse
            # grid (e.g. after the user has nudged with Alt).
            if reverse:
                if self._step == 0:
                    new_step = max_step
                else:
                    new_step = max(0, self._step + delta)
            else:
                if self._step == max_step:
                    new_step = 0
                else:
                    new_step = min(max_step, self._step + delta)
        if new_step == self._step:
            return
        self.setStep(new_step)




# --- Board-feature colour swatch -------------------------------------------

# Side (px) of the colour swatch. Matches the physical-layer list swatch
# (the flat square in _build_layer_row) so the two controls look identical.
_OVERLAY_SWATCH_PX = 14




def _make_swatch_pixmap(rgb: tuple[float, float, float], *,
                        size: int = _OVERLAY_SWATCH_PX) -> QPixmap:
    """A flat, solid-filled colour square — drawn exactly like the physical-
    layer list's swatch (a plain ``QPixmap.fill``: no border, no rounding,
    no antialiasing) so a Board Features row reads identically."""
    px = QPixmap(size, size)
    px.fill(QColor.fromRgbF(*[min(1.0, max(0.0, c)) for c in rgb]))
    return px




class OverlayColorButton(QToolButton):
    """Colour-swatch button for a Board Features row.

    Looks identical to the physical-layer list's colour box — a flat solid
    square, borderless and the same size — but is clickable: clicking opens
    a colour picker; choosing a colour repaints the swatch and emits
    :attr:`colorChanged` so the viewer can recolour the overlay and mark
    the project dirty."""

    colorChanged = Signal(object)  # the new (r, g, b) tuple, channels 0..1

    def __init__(self, parent=None, *,
                 rgb: tuple[float, float, float] = (1.0, 1.0, 1.0),
                 size: int = _OVERLAY_SWATCH_PX) -> None:
        super().__init__(parent)
        self._rgb = tuple(float(c) for c in rgb)
        self._size = size
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setIconSize(QSize(size, size))
        self.setFixedSize(size, size)
        # Borderless / transparent so only the solid swatch shows — no
        # tool-button frame or hover tint, matching the plain QLabel swatch.
        self.setStyleSheet(
            "QToolButton { border: none; padding: 0px; margin: 0px;"
            " background: transparent; }"
        )
        self._apply_icon()
        self.clicked.connect(self._on_clicked)

    def colorRgb(self) -> tuple[float, float, float]:
        return self._rgb

    def setColorRgb(self, rgb: tuple[float, float, float], *,
                    emit: bool = True) -> None:
        rgb = tuple(float(c) for c in rgb)
        if rgb == self._rgb:
            return
        self._rgb = rgb
        self._apply_icon()
        if emit:
            self.colorChanged.emit(self._rgb)

    def _apply_icon(self) -> None:
        self.setIcon(QIcon(_make_swatch_pixmap(self._rgb, size=self._size)))
        self.setToolTip("Pick this overlay's colour")

    def _on_clicked(self) -> None:
        chosen = QColorDialog.getColor(
            QColor.fromRgbF(*[min(1.0, max(0.0, c)) for c in self._rgb]),
            self, "Overlay colour",
        )
        if chosen.isValid():
            self.setColorRgb((chosen.redF(), chosen.greenF(), chosen.blueF()))




class SidebarToggleButton(QToolButton):
    """Slim vertical splitter between the heatmap side panel and the plot.

    * **Click** — collapse / expand the side panel (hotkey ``B``).
    * **Drag horizontally** — resize the side panel width.
    """

    # Delta (px) from the press position while dragging; positive = wider panel.
    resizedBy = Signal(int)

    _DRAG_THRESHOLD_PX = 4

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._collapsed = False
        self._press_global_x: float | None = None
        self._dragging = False
        self.setAutoRaise(True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.SizeHorCursor)
        self.setFixedWidth(14)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)

    def setCollapsed(self, collapsed: bool) -> None:
        collapsed = bool(collapsed)
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        self.setCursor(
            Qt.PointingHandCursor if collapsed else Qt.SizeHorCursor,
        )
        self.update()

    def isCollapsed(self) -> bool:
        return self._collapsed

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._press_global_x = float(event.globalPosition().x())
            self._dragging = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if (
            self._press_global_x is not None
            and event.buttons() & Qt.LeftButton
            and not self._collapsed
        ):
            dx = int(round(float(event.globalPosition().x()) - self._press_global_x))
            if not self._dragging and abs(dx) >= self._DRAG_THRESHOLD_PX:
                self._dragging = True
            if self._dragging:
                self.resizedBy.emit(dx)
                event.accept()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        was_drag = event.button() == Qt.LeftButton and self._dragging
        self._press_global_x = None
        self._dragging = False
        if was_drag:
            # Swallow the click (a drag must not also toggle the panel) but
            # still let QAbstractButton finish its press: returning early
            # here left its internal ``down`` flag latched and ``released``
            # never emitted.
            self.setDown(False)
            self.blockSignals(True)
            try:
                super().mouseReleaseEvent(event)
            finally:
                self.blockSignals(False)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        t = _T()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        rect = self.rect()
        # Track background. underMouse() drives the hover tint; QToolButton's
        # default styled-look fights the custom paint, so we draw it ourselves.
        bg = QColor(t["bg_hover"] if self.underMouse() else t["bg_alt"])
        p.fillRect(rect, bg)
        p.setPen(QColor(t["border"]))
        p.drawLine(rect.topLeft(), rect.bottomLeft())
        p.drawLine(rect.topRight(), rect.bottomRight())
        # Triangle: points RIGHT (▶) when collapsed (click expands the panel
        # outward); points LEFT (◀) when expanded (click pulls it back in).
        cx = rect.width() / 2.0
        cy = rect.height() / 2.0
        half_w = 3.0
        half_h = 5.0
        if self._collapsed:
            tri = QPolygonF([
                QPointF(cx - half_w, cy - half_h),
                QPointF(cx - half_w, cy + half_h),
                QPointF(cx + half_w, cy),
            ])
        else:
            tri = QPolygonF([
                QPointF(cx + half_w, cy - half_h),
                QPointF(cx + half_w, cy + half_h),
                QPointF(cx - half_w, cy),
            ])
        fill = QColor(t["fg"] if self.underMouse() else t["fg_muted"])
        p.setPen(Qt.NoPen)
        p.setBrush(fill)
        p.drawPolygon(tri)

    def enterEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().leaveEvent(event)




class _ClickAbsorbingPanel(QWidget):
    """QWidget that accepts (and thus swallows) mouse press / release /
    double-click events on its empty space.

    Why: the editor / copper-properties side panel is parented onto the
    GL viewer so it floats over the viewport. Qt's default
    QWidget.mousePressEvent ignores the event, which propagates up to
    the GL viewer and fires its empty-space click handler — deselecting
    the current copper. Absorbing the events here keeps clicks on the
    panel's blank areas from reaching the viewport.

    WA_StyledBackground is required so the panel's stylesheet
    ``background-color`` actually paints — without it, QWidget subclasses
    leave their background transparent and the GL viewport shows
    through.
    """

    # Proposed new panel width (px), emitted while the user drags the
    # left edge. The owner clamps and applies it.
    leftEdgeResized = Signal(int)

    _GRIP_W: int = 6  # width (px) of the left-edge resize hot zone

    def __init__(self, *args, enable_left_edge_resize: bool = True,
                 **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._resizing = False
        self._resize_start_global_x = 0.0
        self._resize_start_w = 0
        # Left-edge drag-to-resize is only wanted on the editor side panel.
        # Pure click-absorbers (e.g. the editor button backdrop) pass
        # enable_left_edge_resize=False so they show no grip line and offer
        # no resize cursor / behaviour.
        self._left_edge_resizable = enable_left_edge_resize
        # Thin visual hint of the draggable edge. Transparent to the mouse
        # so press / move events still reach the panel's own handlers, and
        # parked inside the left content margin so it never overlaps the
        # form widgets. Geometry tracked in :meth:`resizeEvent`.
        self._grip_line: QFrame | None = None
        # Invisible hot zone over the grip that owns the resize cursor, so
        # Qt picks the cursor by the widget under the mouse. Setting it on
        # the panel itself (and unsetting on a later panel mouseMove) left
        # it stuck: children inherit the panel cursor, and when the mouse
        # jumped from the grip straight onto a child that consumes move
        # events the reset never ran. The zone ignores mouse events, so
        # press / move / release still propagate up to the panel handlers.
        self._grip_zone: QWidget | None = None
        if self._left_edge_resizable:
            self._grip_zone = QWidget(self)
            self._grip_zone.setCursor(Qt.SizeHorCursor)
            self._grip_line = QFrame(self)
            self._grip_line.setStyleSheet(
                f"background-color: {_T()['border']};"
            )
            self._grip_line.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def _in_grip(self, x: float) -> bool:
        return self._left_edge_resizable and 0 <= x <= self._GRIP_W

    def resizeEvent(self, ev) -> None:  # noqa: N802 (Qt naming)
        super().resizeEvent(ev)
        # Pin the visual grip line to the left edge, full height.
        if self._grip_line is not None:
            self._grip_line.setGeometry(2, 0, 2, self.height())
            self._grip_line.raise_()
        if self._grip_zone is not None:
            self._grip_zone.setGeometry(0, 0, self._GRIP_W + 1, self.height())
            self._grip_zone.raise_()

    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.LeftButton and self._in_grip(ev.position().x()):
            self._resizing = True
            self._resize_start_global_x = ev.globalPosition().x()
            self._resize_start_w = self.width()
        ev.accept()

    def mouseMoveEvent(self, ev) -> None:
        if self._resizing:
            # Right edge is pinned; dragging the left edge leftwards
            # (smaller global x) widens the panel.
            delta = self._resize_start_global_x - ev.globalPosition().x()
            self.leftEdgeResized.emit(
                int(round(self._resize_start_w + delta)))
            ev.accept()
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev) -> None:
        if self._resizing and ev.button() == Qt.LeftButton:
            self._resizing = False
        ev.accept()

    def mouseDoubleClickEvent(self, ev) -> None:
        ev.accept()




def _esc(s) -> str:
    """Minimal HTML escape for user-supplied strings going into the Setup tab."""
    return (str(s)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))




def _contrasting_text_color(hex_color: str) -> str:
    """Return ``#000000`` or ``#ffffff`` — whichever gives better contrast
    against ``hex_color`` (#RRGGBB). Uses the YIQ luma approximation
    (0.299 R + 0.587 G + 0.114 B); threshold 0.5 puts mid-grey on black."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return "#000000"
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return "#000000"
    luma = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
    return "#000000" if luma > 0.5 else "#ffffff"
