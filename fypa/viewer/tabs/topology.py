"""The Topology tab."""
from __future__ import annotations

from pathlib import Path
from PySide6.QtCore import QByteArray, QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (
    QFileDialog,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from fypa.viewer.theme import _file_dialog_options, _T, current_theme
from fypa.viewer.widgets import _esc

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fypa.viewer.window import PdnViewer


_TOPOLOGY_ZOOM_STEP = 1.2


_TOPOLOGY_MIN_SCALE = 0.05


_TOPOLOGY_MAX_SCALE = 32.0


_TOPOLOGY_PAN_STEP = 48




class _TopologyView(QGraphicsView):
    """Zoomable/pannable topology diagram - SVG rendered as vector graphics."""

    def __init__(self, viewer: PdnViewer, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._viewer = viewer
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        from PySide6.QtSvgWidgets import QGraphicsSvgItem

        self._svg_item = QGraphicsSvgItem()
        self._scene.addItem(self._svg_item)
        self._highlight_item = QGraphicsSvgItem()
        self._highlight_item.setZValue(0.5)
        self._highlight_item.setOpacity(0.0)
        self._highlight_item.setVisible(False)
        self._scene.addItem(self._highlight_item)
        self._highlighted_net: str | None = None
        self._highlight_renderer = None
        # Net hover highlight fades in/out (~0.3s) rather than snapping on/off,
        # so sweeping the cursor across wires doesn't flash.
        from PySide6.QtCore import QEasingCurve, QPropertyAnimation

        self._highlight_anim = QPropertyAnimation(self._highlight_item, b"opacity", self)
        self._highlight_anim.setDuration(300)
        self._highlight_anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._highlight_anim.finished.connect(self._on_highlight_anim_finished)
        self._svg_renderer = None
        self._diagram_loaded = False
        self._empty_text = QGraphicsSimpleTextItem()
        self._scene.addItem(self._empty_text)
        self._empty_text.setZValue(1.0)
        # Widget shown over the viewport instead of a diagram (the "nothing
        # set up yet" panel) - see set_empty_panel / show_empty_panel.
        self._empty_panel: QWidget | None = None

        self.setRenderHint(QPainter.Antialiasing)
        self.setRenderHint(QPainter.TextAntialiasing)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        # Scroll bars always shown: if they appeared mid-zoom, the viewport
        # would shrink under the cursor-anchored zoom and the resize would
        # re-anchor the view, so the point under the cursor would drift.
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.setDragMode(QGraphicsView.NoDrag)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)

        self._space_pan = False
        self._panning = False
        self._pan_anchor: QPointF | None = None
        self._pan_scroll = (0, 0)
        self._click_anchor: QPointF | None = None

    def set_background(self, color: str) -> None:
        """Canvas colour; the diagram is drawn on it transparently, over a
        dot grid in a faint tint of the text colour."""
        from fypa.topology.style import mix

        self.setStyleSheet(
            f"QGraphicsView {{ background-color: {color}; border: none; }}"
        )
        self._grid_color = QColor(mix(current_theme()["fg"], color, 0.16))
        self.viewport().update()

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        """Dot grid behind the diagram, pinned to diagram coordinates so it
        pans and zooms with it; thinned out (every 2nd, 4th... dot) when
        zoomed far enough out that the dots would crowd."""
        super().drawBackground(painter, rect)
        color = getattr(self, "_grid_color", None)
        if color is None or not self._diagram_loaded:
            return
        from PySide6.QtGui import QPen, QPolygonF

        from fypa.topology.render import GRID_STEP

        scale = self._current_scale()
        if scale <= 0:
            return
        step = GRID_STEP
        while step * scale < 12.0:
            step *= 2
        x0 = (int(rect.left() // step)) * step + step / 2
        y0 = (int(rect.top() // step)) * step + step / 2
        pts = []
        y = y0
        while y <= rect.bottom():
            x = x0
            while x <= rect.right():
                pts.append(QPointF(x, y))
                x += step
            y += step
        if not pts:
            return
        pen = QPen(color)
        pen.setCosmetic(True)
        pen.setWidthF(max(1.2, min(2.0, 1.8 * scale)))
        pen.setCapStyle(Qt.RoundCap)
        painter.save()
        painter.setPen(pen)
        painter.drawPoints(QPolygonF(pts))
        painter.restore()

    def set_empty_message(self, message: str) -> None:
        self.resetTransform()
        self._diagram_loaded = False
        self._svg_renderer = None
        self._clear_net_highlight()
        self._svg_item.setVisible(False)
        self._empty_text.setText(message)
        self._empty_text.setBrush(QColor(current_theme()["fg_muted"]))
        self._empty_text.setVisible(bool(message))
        if message:
            self._scene.setSceneRect(self._empty_text.boundingRect().adjusted(
                -16, -16, 16, 16,
            ))
        else:
            self._scene.setSceneRect(QRectF(0, 0, 1, 1))

    def set_empty_panel(self, panel: QWidget) -> None:
        """Adopt ``panel`` as the widget :meth:`show_empty_panel` lays over
        the viewport. Hidden until then."""
        panel.setParent(self)
        panel.hide()
        self._empty_panel = panel

    def show_empty_panel(self, on: bool) -> None:
        panel = self._empty_panel
        if panel is None:
            return
        panel.setVisible(on)
        if on:
            self._place_empty_panel()
            # Keep the floating setup buttons clickable above it.
            for b in getattr(self._viewer, "_topology_setup_btns", ()):
                b.raise_()

    def _place_empty_panel(self) -> None:
        panel = self._empty_panel
        if panel is not None and panel.isVisible():
            panel.setGeometry(self.viewport().geometry())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place_empty_panel()

    def set_diagram_svg(self, svg_data: QByteArray, *, keep_view: bool = False) -> None:
        """Show a new diagram, fitted to the view - or, with ``keep_view``
        (a row collapsed/expanded), at the current zoom and scroll."""
        from PySide6.QtSvg import QSvgRenderer

        kept = None
        if keep_view and self._diagram_loaded:
            kept = (self.transform(), self.horizontalScrollBar().value(),
                    self.verticalScrollBar().value())
        self.resetTransform()
        self._clear_net_highlight()
        self._svg_renderer = QSvgRenderer(svg_data)
        if not self._svg_renderer.isValid():
            self.set_empty_message("Topology diagram could not be rendered.")
            return
        self._svg_item.setSharedRenderer(self._svg_renderer)
        self._svg_item.setVisible(True)
        self._diagram_loaded = True
        self._empty_text.setVisible(False)
        bounds = self._svg_renderer.viewBoxF()
        if bounds.width() < 1 or bounds.height() < 1:
            bounds = self._svg_item.boundingRect()
        self._scene.setSceneRect(bounds)
        if kept is not None:
            self.setTransform(kept[0])
            self.horizontalScrollBar().setValue(kept[1])
            self.verticalScrollBar().setValue(kept[2])
            return
        QTimer.singleShot(0, self._deferred_fit_in_view)

    def _deferred_fit_in_view(self) -> None:
        # The view can be torn down (e.g. a tab rebuild on theme toggle) between
        # scheduling this and the event loop firing it; touching the deleted
        # C++ object then raises RuntimeError. Ignore it - nothing to fit.
        try:
            self.fit_in_view()
        except RuntimeError:
            pass

    def _current_scale(self) -> float:
        return float(self.transform().m11())

    def _clamp_zoom_factor(self, factor: float) -> float:
        scale = self._current_scale()
        target = scale * factor
        if target < _TOPOLOGY_MIN_SCALE:
            return _TOPOLOGY_MIN_SCALE / scale
        if target > _TOPOLOGY_MAX_SCALE:
            return _TOPOLOGY_MAX_SCALE / scale
        return factor

    def _map_view_to_scene(self, view_pos: QPointF) -> QPointF:
        """Map a viewport position to scene coordinates (PySide6 needs QPoint)."""
        pt = view_pos.toPoint()
        return self.mapToScene(pt)

    def _zoom_by(self, factor: float, *, anchor: QPointF | None = None) -> None:
        factor = self._clamp_zoom_factor(factor)
        if abs(factor - 1.0) < 1e-9:
            return
        if anchor is None:
            anchor = QPointF(self.viewport().rect().center())
        # Zoom about ``anchor`` (the cursor for Ctrl+wheel): the diagram
        # point under it stays under it. A scrollable QGraphicsView positions
        # the scene with its scroll bars (translate() is ignored), and centres
        # a diagram smaller than the view, so neither can hold an arbitrary
        # point still. Instead work out the scene area the view must show -
        # the one with that point at the cursor - let the scene extend over
        # it (past the diagram's edge if need be, like any canvas app), and
        # centre on it. fit_in_view() shrinks the scene back to the diagram.
        prev_anchor = self.transformationAnchor()
        self.setTransformationAnchor(QGraphicsView.NoAnchor)
        scene_pt = self._map_view_to_scene(anchor)
        self.scale(factor, factor)
        s = self._current_scale()
        vp = self.viewport().rect()
        visible = QRectF(
            scene_pt.x() - anchor.x() / s,
            scene_pt.y() - anchor.y() / s,
            vp.width() / s,
            vp.height() / s,
        )
        self._scene.setSceneRect(self._diagram_bounds().united(visible))
        self.centerOn(visible.center())
        self.setTransformationAnchor(prev_anchor)

    def _diagram_bounds(self) -> QRectF:
        if self._svg_renderer is not None:
            vb = self._svg_renderer.viewBoxF()
            if vb.width() >= 1 and vb.height() >= 1:
                return vb
        return self._svg_item.boundingRect()

    def zoom_in(self) -> None:
        self._zoom_by(_TOPOLOGY_ZOOM_STEP)

    def zoom_out(self) -> None:
        self._zoom_by(1.0 / _TOPOLOGY_ZOOM_STEP)

    def go_to(self, point: tuple[float, float], *, net: str | None = None) -> None:
        """Pan so ``point`` is centred, keeping the zoom; highlight ``net``."""
        if not self._diagram_loaded:
            return
        if self._current_scale() < 0.7:
            self._zoom_by(0.9 / self._current_scale())
        self.centerOn(QPointF(point[0], point[1]))
        if net:
            self._highlighted_net = None
            self._set_highlighted_net(net)

    def fit_in_view(self) -> None:
        if not self._diagram_loaded:
            return
        self.resetTransform()
        # Drop any room a cursor-anchored zoom added past the diagram's edge.
        self._scene.setSceneRect(self._diagram_bounds())
        model = getattr(self._viewer, "_topology_model", None)
        if getattr(model, "layout", "") == "rows":
            # A rows diagram is tall and narrow: fitting it whole can shrink
            # it past legibility. Fit the width instead (never above 1.5×)
            # and start at the top; scroll down for the rest.
            rect = self._svg_item.boundingRect()
            vp = self.viewport().rect()
            if rect.width() > 0 and rect.height() > 0 and vp.width() > 0:
                whole = min(vp.width() / rect.width(), vp.height() / rect.height())
                if whole < 0.75:
                    scale = min(vp.width() / rect.width() * 0.98, 1.5)
                    self.scale(scale, scale)
                    self.centerOn(rect.center().x(), rect.top())
                    self.verticalScrollBar().setValue(self.verticalScrollBar().minimum())
                    return
        self.fitInView(self._svg_item, Qt.KeepAspectRatio)
        if self._current_scale() > _TOPOLOGY_MAX_SCALE:
            self._zoom_by(_TOPOLOGY_MAX_SCALE / self._current_scale())
        if self._current_scale() < _TOPOLOGY_MIN_SCALE:
            self._zoom_by(_TOPOLOGY_MIN_SCALE / self._current_scale())

    def pan_by(self, dx: int, dy: int) -> None:
        h = self.horizontalScrollBar()
        v = self.verticalScrollBar()
        h.setValue(h.value() - dx)
        v.setValue(v.value() - dy)

    def diagram_pos(self, view_pos: QPointF) -> tuple[float, float]:
        scene_pos = self._map_view_to_scene(view_pos)
        return float(scene_pos.x()), float(scene_pos.y())

    def _clear_net_highlight(self) -> None:
        """Instant clear - used on diagram reload / theme rebuild."""
        self._highlight_anim.stop()
        self._highlighted_net = None
        self._highlight_item.setOpacity(0.0)
        self._highlight_item.setVisible(False)

    def _fade_highlight_to(self, target: float) -> None:
        """Animate the highlight overlay's opacity toward ``target`` (0..1)."""
        self._highlight_anim.stop()
        if target > 0.0:
            self._highlight_item.setVisible(True)
        self._highlight_anim.setStartValue(self._highlight_item.opacity())
        self._highlight_anim.setEndValue(target)
        self._highlight_anim.start()

    def _on_highlight_anim_finished(self) -> None:
        if self._highlight_item.opacity() <= 0.0:
            self._highlight_item.setVisible(False)

    def _set_highlighted_net(self, net: str | None) -> None:
        if net == self._highlighted_net:
            return
        self._highlighted_net = net
        if net is None:
            self._fade_highlight_to(0.0)
            return
        from PySide6.QtSvg import QSvgRenderer

        from fypa.topology.render import render_net_highlight_svg

        model = getattr(self._viewer, "_topology_model", None)
        svg = (
            render_net_highlight_svg(model, net, theme=current_theme())
            if model is not None
            else ""
        )
        if not svg:
            self._highlighted_net = None
            self._fade_highlight_to(0.0)
            return
        renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        if not renderer.isValid():
            self._highlighted_net = None
            self._fade_highlight_to(0.0)
            return
        self._highlight_renderer = renderer
        self._highlight_item.setSharedRenderer(renderer)
        # Restart the fade from fully transparent. Sweeping the cursor
        # straight from one net to an adjacent net (without crossing empty
        # space) leaves the overlay already opaque from the previous net, so
        # animating "to 1.0" would be a no-op and the new net would snap on.
        # Reset to 0 first so the new net always fades in.
        self._highlight_item.setOpacity(0.0)
        self._fade_highlight_to(1.0)

    def wheelEvent(self, event) -> None:
        self._hide_tip()
        delta_y = event.angleDelta().y()
        delta_x = event.angleDelta().x()
        mods = event.modifiers()

        if mods & Qt.ControlModifier:
            delta = delta_y if delta_y else delta_x
            if delta == 0:
                return
            factor = pow(_TOPOLOGY_ZOOM_STEP, delta / 120.0)
            self._zoom_by(factor, anchor=event.position())
            event.accept()
            return

        if mods & Qt.ShiftModifier:
            delta = delta_y if delta_y else delta_x
            if delta == 0:
                return
            self.pan_by(int(-delta / 120.0 * _TOPOLOGY_PAN_STEP), 0)
            event.accept()
            return

        if delta_y != 0:
            super().wheelEvent(event)
            return
        if delta_x != 0:
            self.pan_by(int(-delta_x / 120.0 * _TOPOLOGY_PAN_STEP), 0)
            event.accept()

    def keyPressEvent(self, event) -> None:
        self._hide_tip()
        key = event.key()
        if key == Qt.Key_Space and not event.isAutoRepeat():
            self._space_pan = True
            self.setDragMode(QGraphicsView.ScrollHandDrag)
            self.setCursor(Qt.OpenHandCursor)
            event.accept()
            return
        step = _TOPOLOGY_PAN_STEP
        mods = event.modifiers()
        if mods & Qt.ControlModifier:
            step *= 3
        elif mods & Qt.ShiftModifier:
            step *= 2
        if key == Qt.Key_Left:
            self.pan_by(-step, 0)
            event.accept()
            return
        if key == Qt.Key_Right:
            self.pan_by(step, 0)
            event.accept()
            return
        if key == Qt.Key_Up:
            self.pan_by(0, -step)
            event.accept()
            return
        if key == Qt.Key_Down:
            self.pan_by(0, step)
            event.accept()
            return
        if key in (Qt.Key_Plus, Qt.Key_Equal):
            self.zoom_in()
            event.accept()
            return
        if key == Qt.Key_Minus:
            self.zoom_out()
            event.accept()
            return
        if key == Qt.Key_0:
            self.fit_in_view()
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:
        if event.key() == Qt.Key_Space and not event.isAutoRepeat():
            self._space_pan = False
            self.setDragMode(QGraphicsView.NoDrag)
            self.unsetCursor()
            event.accept()
            return
        super().keyReleaseEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._panning and (
            event.buttons() & (Qt.MiddleButton | Qt.RightButton)
            or (event.buttons() & Qt.LeftButton and self._space_pan)
        ):
            if self._pan_anchor is not None:
                delta = event.position() - self._pan_anchor
                h = self.horizontalScrollBar()
                v = self.verticalScrollBar()
                h.setValue(int(self._pan_scroll[0] - delta.x()))
                v.setValue(int(self._pan_scroll[1] - delta.y()))
            event.accept()
            return
        from fypa.topology import topology_tooltip_at
        from fypa.topology.hit_test import topology_net_at

        model = getattr(self._viewer, "_topology_model", None)
        if model is not None and self._diagram_loaded:
            x, y = self.diagram_pos(event.position())
            tip = topology_tooltip_at(model, x, y)
            from fypa.topology.hit_test import find_band_header_at

            band = None if tip else find_band_header_at(model, x, y)
            if band is not None:
                tip = ("Click to expand this row" if band.collapsed
                       else "Click to collapse this row")
            if tip:
                self._show_tip(tip)
            else:
                self._hide_tip()
            if not self._space_pan:
                from fypa.topology.hit_test import (
                    find_component_at,
                    find_jump_icon_at,
                    find_tag_at,
                )

                tag = find_tag_at(model, x, y)
                clickable = (
                    (tag is not None and (tag.target is not None
                                          or tag.kind in ("undriven", "end", "add")))
                    or find_jump_icon_at(model, x, y) is not None
                    or find_component_at(model, x, y) is not None
                    or band is not None
                )
                if clickable:
                    self.setCursor(Qt.PointingHandCursor)
                else:
                    self.unsetCursor()
            self._set_highlighted_net(topology_net_at(model, x, y))
        else:
            self._clear_net_highlight()
            self._hide_tip()
        super().mouseMoveEvent(event)

    # --- hover tooltip ------------------------------------------------------
    #
    # Not QToolTip: Qt closes that on any focus change anywhere in the app
    # (and after ~10 s), so a tooltip read while something else in the
    # window takes focus vanished after a second. This label stays up until
    # the mouse leaves the item or the view, or a click / wheel / key.

    def _show_tip(self, text: str) -> None:
        from fypa.viewer.widgets import (
            _make_floating_tooltip,
            _move_tooltip_to_cursor,
            _qt_widget_alive,
        )

        label = getattr(self, "_tip_label", None)
        if label is None or not _qt_widget_alive(label):
            label = self._tip_label = _make_floating_tooltip(self)
            # Mouse moves stop arriving once the pointer is over another
            # window (or another app is in front), so watch the pointer
            # itself rather than focus or activation.
            self._tip_watch = QTimer(self)
            self._tip_watch.setInterval(250)
            self._tip_watch.timeout.connect(self._hide_tip_if_pointer_left)
        if label.text() != text:
            label.setText(text)
            label.adjustSize()
        _move_tooltip_to_cursor(label)
        if not label.isVisible():
            label.show()
        if not self._tip_watch.isActive():
            self._tip_watch.start()

    def _hide_tip(self) -> None:
        from fypa.viewer.widgets import _qt_widget_alive

        watch = getattr(self, "_tip_watch", None)
        if watch is not None:
            watch.stop()
        label = getattr(self, "_tip_label", None)
        if label is not None and _qt_widget_alive(label):
            label.hide()

    def _hide_tip_if_pointer_left(self) -> None:
        vp = self.viewport()
        if not vp.rect().contains(vp.mapFromGlobal(QCursor.pos())):
            self._hide_tip()

    def hideEvent(self, event) -> None:
        self._hide_tip()   # tab switched away, window minimised
        super().hideEvent(event)

    def leaveEvent(self, event) -> None:
        self._hide_tip()
        self._set_highlighted_net(None)
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        self._hide_tip()
        if event.button() in (Qt.MiddleButton, Qt.RightButton) or (
            event.button() == Qt.LeftButton and self._space_pan
        ):
            self._panning = True
            self._pan_anchor = event.position()
            self._pan_scroll = (
                self.horizontalScrollBar().value(),
                self.verticalScrollBar().value(),
            )
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        if event.button() == Qt.LeftButton:
            self._click_anchor = event.position()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() in (Qt.MiddleButton, Qt.RightButton, Qt.LeftButton):
            self._panning = False
            if self._space_pan:
                self.setCursor(Qt.OpenHandCursor)
            else:
                self.unsetCursor()
        if (
            event.button() == Qt.LeftButton
            and not self._space_pan
            and self._click_anchor is not None
        ):
            moved = (event.position() - self._click_anchor).manhattanLength()
            if moved < 6.0:
                self._viewer._on_topology_view_clicked(event)
        self._click_anchor = None
        super().mouseReleaseEvent(event)


class _TopologyTabMixin:
    """The Topology tab."""

    # --- Topology tab -------------------------------------------------------

    def _build_topology_tab(self) -> QWidget:
        """Build the Topology tab - abstract PDN simulation schematic."""
        # A rebuild (theme switch) deletes the old sidebar: hand the editor
        # form back to the Board panel before its host disappears.
        self._topology_form_release()
        # Rows always open expanded (on start, on a design load, on a rebuild).
        self._topology_collapsed: set[str] = set()
        widget = QWidget(self.tabs)
        outer = QVBoxLayout(widget)
        outer.setContentsMargins(8, 8, 8, 8)

        _t = _T()
        toolbar = QHBoxLayout()
        toolbar.setSpacing(4)
        btn_qss = (
            f"QToolButton {{ border: 1px solid {_t['border']}; border-radius: 4px;"
            f"  padding: 2px 10px; background-color: {_t['bg_alt']}; }}"
            f"QToolButton:hover {{ background-color: {_t['bg_hover']}; }}"
        )
        for label, tip, slot in (
            ("+", "Zoom in (Ctrl++, +)", self._topology_zoom_in),
            ("−", "Zoom out (Ctrl+-, −)", self._topology_zoom_out),
            ("Fit", "Fit diagram (Ctrl+0)", self._topology_fit),
            ("Dump", "Write topology.pkl, wiring.json, topology.svg", self._topology_dump_debug),
        ):
            btn = QToolButton()
            btn.setText(label)
            btn.setToolTip(tip)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(btn_qss)
            btn.clicked.connect(slot)
            toolbar.addWidget(btn)

        from fypa.viewer.prefs import (
            load_topology_cascade,
            load_topology_show_add_handles,
            load_topology_show_bridges,
            load_topology_gauge_style,
            load_topology_port_voltages,
            load_topology_show_gauges,
        )

        self._topology_cascade = load_topology_cascade()
        self._topology_show_bridges = load_topology_show_bridges()
        self._topology_show_add_handles = load_topology_show_add_handles()
        self._topology_show_gauges = load_topology_show_gauges()
        self._topology_gauge_style = load_topology_gauge_style()
        self._topology_port_voltages = load_topology_port_voltages()
        toolbar.addSpacing(12)

        # View options: one drop-down holding the diagram's display toggles.
        view_btn = QToolButton()
        view_btn.setText("View options ▾")
        view_btn.setToolTip("Choose what the topology diagram shows")
        view_btn.setCursor(Qt.PointingHandCursor)
        view_btn.setPopupMode(QToolButton.InstantPopup)
        # The label carries its own arrow; hide Qt's indicator so the two
        # don't stack.
        view_btn.setStyleSheet(
            btn_qss + "QToolButton::menu-indicator { image: none; width: 0; }")
        view_menu = QMenu(view_btn)
        view_menu.setToolTipsVisible(True)
        from fypa.viewer.assets import _menu_check_indicator_qss

        view_menu.setStyleSheet(_menu_check_indicator_qss(
            _t["fg"], _t["accent"], _t["fg_dim"]))

        def _toggle(text: str, tip: str, checked: bool, slot):
            act = view_menu.addAction(text)
            act.setToolTip(tip)
            act.setCheckable(True)
            act.setChecked(checked)
            act.toggled.connect(slot)
            return act

        self._topology_cascade_act = _toggle(
            "Cascade",
            "Draw each regulator inline, wired straight off the rail that "
            "feeds it (J2 → R18 → U6 → J1 on one row), instead of on a row of "
            "its own linked by tags. Regulators on one rail stack in its "
            "column.",
            self._topology_cascade,
            self._on_topology_cascade_toggled)
        view_menu.addSeparator()
        self._topology_bridges_act = _toggle(
            "Bridges",
            "List 0 Ω auto-bridges on nets no source, sink or regulator uses "
            "(pull-up links, signal jumpers…).",
            self._topology_show_bridges,
            self._on_topology_bridges_toggled)
        self._topology_add_act = _toggle(
            "Add handles",
            "Show a dashed \"+ SINK (n)\" tap under each rail's last load when "
            "more parts on the rail (ICs, connectors) could be a load. Click one "
            "to add a SINK there in Manual setup. Needs the Altium design "
            "loaded.",
            self._topology_show_add_handles,
            self._on_topology_add_handles_toggled)
        view_menu.addSeparator()
        self._topology_gauges_act = _toggle(
            "Voltage gauges",
            "In each sink (and regulator input) with a minimum voltage: the "
            "voltage at its pins and its margin to the nearest limit. Pins "
            "are measured supply minus return, so ground rise counts. Hover "
            "a gauge for the full numbers.",
            self._topology_show_gauges,
            self._on_topology_gauges_toggled)
        self._topology_gauge_text_act = _toggle(
            "Gauges as text",
            "Show each gauge as two lines of text (voltage and margin, then "
            "the limits and supply) instead of the track: green zone "
            "allowed, red outside, the caret the supply before any drop, the "
            "dot the voltage at the pins.",
            self._topology_gauge_style == "text",
            self._on_topology_gauge_text_toggled)
        self._topology_gauge_text_act.setEnabled(self._topology_show_gauges)
        self._topology_port_v_act = _toggle(
            "Port voltages",
            "Show the solved voltage beside each source, regulator and sink "
            "supply port: its pins minus their return (a load's lowest pin, "
            "an output's average), so ground rise counts.",
            self._topology_port_voltages,
            self._on_topology_port_voltages_toggled)
        view_btn.setMenu(view_menu)
        self._topology_view_btn = view_btn
        toolbar.addWidget(view_btn)
        toolbar.addStretch()

        # Setup, What-If + Resolve: 34 x 34 icon-only squares (6 px radius,
        # bold, the Board's badge icons) that widen on hover to reveal their
        # label, like the Board's editor-mode column (hover_expand). The
        # icon is held left at its centred spot (1 + 8 + 16 + 8 + 1) so it
        # stays put while the button grows; [expanded] adds the right
        # padding for the label.
        from fypa.viewer.assets import (
            _autosetup_icon,
            _manual_setup_icon,
            _resolve_icon,
            _whatif_icon,
        )

        def _big_btn_qss(color: str) -> str:
            return (
                "QPushButton {{ border: 1px solid {c}; border-radius: 6px;"
                "  padding: 0px 0px 0px 8px; font-weight: bold;"
                "  text-align: left; background-color: {bg}; color: {c}; }}"
                "QPushButton[expanded=\"true\"] {{ padding: 0px 10px 0px 8px; }}"
                "QPushButton:hover:enabled {{ background-color: {hover}; }}"
                "QPushButton:disabled {{ color: {dim}; border-color: {dim}; }}"
                .format(c=color, bg=_t["bg"], hover=_t["bg_hover"],
                        dim=_t["fg_dim"])
            )

        def _big_btn(text: str, icon, color: str, tip: str, slot) -> QPushButton:
            # ``text`` is the hover label (see the expanders below); the
            # button itself starts blank.
            b = QPushButton()
            b.setIcon(icon)
            b.setIconSize(QSize(16, 16))
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(tip)
            b.setStyleSheet(_big_btn_qss(color))
            b.clicked.connect(slot)
            return b

        accent = _t["accent"]
        # Auto setup: pastel yellow, a touch deeper on the light theme so the
        # label still reads against near-white.
        from fypa.viewer.theme import current_theme_mode
        autosetup_yellow = ("#f2d675" if current_theme_mode() == "dark"
                            else "#c9a227")
        self._topology_autosetup_btn = _big_btn(
            "Auto setup…", _autosetup_icon(autosetup_yellow, _t["bg"]),
            autosetup_yellow,
            "Propose sources, regulators, sinks and series elements from the "
            "design (and a datasheet folder, if you have one), then review "
            "and apply them.",
            self._topology_run_autosetup)
        self._topology_manual_btn = _big_btn(
            "Manual setup…", _manual_setup_icon(accent, _t["bg"]), accent,
            "Add and edit sources, regulators and sinks by hand: pick a net, "
            "the part on it and its pins, in one list per kind.",
            self._topology_run_manual_setup)
        # Same pastel orange and badge as the Board's What-If button. Shows
        # the What-If panel on this tab's right-hand side, in place of the
        # part sidebar; open / closed is kept apart from the Board's.
        whatif_orange = (self._WHATIF_ORANGE_DARK
                         if current_theme_mode() == "dark"
                         else self._WHATIF_ORANGE_LIGHT)
        wbtn = QPushButton()
        wbtn.setIcon(_whatif_icon(whatif_orange, _t["bg"]))
        wbtn.setIconSize(QSize(16, 16))
        wbtn.setCursor(Qt.PointingHandCursor)
        wbtn.setCheckable(True)
        wbtn.setToolTip(
            "Show / hide the What-If panel (W): try a change - copper "
            "weight, temperature, via plating - and compare it with this "
            "result. Replaces the part sidebar while open; leaves editor "
            "mode if it is on.")
        wbtn.setStyleSheet(
            _big_btn_qss(whatif_orange)
            # ":hover:enabled" outranks a bare ":checked", so name the hover
            # case too: like the Board's button, it stays filled on hover.
            + "QPushButton:checked, QPushButton:checked:hover:enabled"
              " {{ background-color: {c}; color: {bg}; }}"
            .format(c=whatif_orange, bg=_t["bg"]))
        wbtn.setChecked(
            bool(getattr(self, "_whatif_open", {}).get("topology", False)))
        wbtn.toggled.connect(
            lambda on: self._set_whatif_panel_visible(on, where="topology"))
        self._topology_whatif_btn = wbtn
        # Same green and refresh badge as the Board's Resolve button.
        self._topology_resolve_btn = _big_btn(
            "Resolve", _resolve_icon("#2ca92c", _t["bg"], _t["fg_dim"]),
            "#2ca92c",
            "Re-run the solver with the current editor changes applied",
            self._on_topology_resolve)

        self._topology_split = QSplitter(Qt.Horizontal, widget)
        self._topology_view = _TopologyView(self, self._topology_split)
        from fypa.viewer.empty_state import EmptyStatePanel, run_empty_state_action
        self._topology_view.set_empty_panel(EmptyStatePanel(
            lambda what: run_empty_state_action(self, what)))
        # Setup, What-If + Resolve: a column of squares floating over the
        # diagram's top-left corner (like the Board's buttons), so the
        # diagram runs the tab's full width. Parented straight to the view
        # (not its viewport, so they stay put while the diagram scrolls, and
        # not a layout, which would hold a hovered button's width), placed
        # by hand.
        from fypa.viewer.editor.hover_expand import HoverExpander

        setup_btns = (self._topology_autosetup_btn, self._topology_manual_btn,
                      self._topology_whatif_btn, self._topology_resolve_btn)
        y = 8
        for b in setup_btns:
            b.setParent(self._topology_view)
            b.resize(34, 34)
            b.move(8, y)
            b.raise_()
            y += 34 + 4
        always = lambda: True  # noqa: E731
        self._topology_expanders = {
            self._topology_autosetup_btn: HoverExpander(
                self._topology_autosetup_btn, lambda: "Auto setup…", always),
            self._topology_manual_btn: HoverExpander(
                self._topology_manual_btn, lambda: "Manual setup…", always),
            wbtn: HoverExpander(wbtn, lambda: "What-If", always),
            self._topology_resolve_btn: HoverExpander(
                self._topology_resolve_btn,
                lambda: ("Solve" if getattr(self, "_awaiting_first_solve",
                                            False) else "Resolve"),
                always),
        }
        self._topology_setup_btns = setup_btns
        self._topology_view.set_background(_t["bg"])
        self._topology_split.addWidget(self._topology_view)
        self._topology_sidebar = self._build_topology_sidebar()
        self._topology_split.addWidget(self._topology_sidebar)
        self._topology_split.setStretchFactor(0, 1)
        self._topology_split.setStretchFactor(1, 0)
        self._topology_sidebar.hide()
        # View controls sit over the diagram only, so the What-If panel to
        # the right runs from the top of the tab.
        diagram_col = QVBoxLayout()
        diagram_col.setContentsMargins(0, 0, 0, 0)
        diagram_col.setSpacing(4)
        diagram_col.addLayout(toolbar)
        diagram_col.addWidget(self._topology_split, 1)
        # Diagram (+ part sidebar), with the What-If panel's Topology home
        # to its right: the panel moves here from the Board while this tab
        # shows (see _whatif_place_panel).
        split_row = QHBoxLayout()
        split_row.setContentsMargins(0, 0, 0, 0)
        split_row.setSpacing(0)
        split_row.addLayout(diagram_col, 1)
        self._topology_whatif_layout = split_row
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addLayout(split_row, 1)
        outer.addLayout(body, 1)
        if not getattr(self, "_topology_tabs_hooked", False):
            self.tabs.currentChanged.connect(self._on_tabs_changed_for_topology)
            self._topology_tabs_hooked = True

        self._topology_hint = QLabel("")
        self._topology_hint.setStyleSheet(
            f"QLabel {{ color: {_t['fg_dim']}; font-size: 9pt; }}"
        )
        outer.addWidget(self._topology_hint)

        self._topology_model = None
        self._install_topology_shortcuts(widget)
        return widget

    # --- Sidebar: the PDN editor form for a clicked part -------------------

    def _build_topology_sidebar(self) -> QWidget:
        """Right-hand panel showing the clicked part's PDN properties - the
        same form as the Board's editor panel (see
        :meth:`_topology_form_activate`)."""
        _t = _T()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(300)
        scroll.setMaximumWidth(520)
        scroll.setStyleSheet(
            f"QScrollArea {{ background-color: {_t['bg']};"
            f"  border-left: 1px solid {_t['border']}; }}")
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(8)
        head = QHBoxLayout()
        self._topology_side_title = QLabel("")
        self._topology_side_title.setTextFormat(Qt.RichText)
        self._topology_side_title.setWordWrap(True)
        head.addWidget(self._topology_side_title, 1)
        close = QToolButton()
        close.setText("✕")
        close.setToolTip("Close")
        close.setAutoRaise(True)
        close.clicked.connect(self._topology_close_sidebar)
        head.addWidget(close, 0, Qt.AlignTop)
        lay.addLayout(head)
        self._topology_side_jump = QPushButton("⌖  Show on Board")
        self._topology_side_jump.setToolTip("Switch to the Board and zoom to this part")
        self._topology_side_jump.clicked.connect(self._topology_side_jump_clicked)
        lay.addWidget(self._topology_side_jump)
        self._topology_side_hint = QLabel("")
        self._topology_side_hint.setWordWrap(True)
        self._topology_side_hint.setStyleSheet(f"color: {_t['fg_muted']};")
        lay.addWidget(self._topology_side_hint)
        # A part with several editor directives (one per rail): pick which.
        from PySide6.QtWidgets import QComboBox

        self._topology_side_pick = QComboBox()
        self._topology_side_pick.setToolTip(
            "This part has several PDN directives - choose the one to edit")
        self._topology_side_pick.currentIndexChanged.connect(
            self._on_topology_side_pick)
        self._topology_side_pick.hide()
        lay.addWidget(self._topology_side_pick)
        self._topology_form_host = QWidget()
        self._topology_form_layout = QVBoxLayout(self._topology_form_host)
        self._topology_form_layout.setContentsMargins(0, 0, 0, 0)
        self._topology_form_layout.setSpacing(6)
        lay.addWidget(self._topology_form_host)
        lay.addStretch(1)
        scroll.setWidget(panel)
        self._topology_side_node = None
        return scroll

    def _topology_form_activate(self) -> None:
        """Point the editor form at the Topology sidebar.

        The form code (``_populate_editor_form`` / ``_on_editor_apply`` …)
        builds into ``self._editor_form_layout``. Only one form may be live -
        its widgets are shared ``self._ef_*`` attributes - so the Board
        panel's copy is cleared while the sidebar owns it, and handed back
        by :meth:`_topology_form_release`."""
        if getattr(self, "_topology_form_owned", False):
            return
        if not hasattr(self, "_editor_form_layout"):
            return
        self._hm_form_host = self._editor_form_host
        self._hm_form_layout = self._editor_form_layout
        self._clear_layout(self._hm_form_layout)
        self._hm_form_host.hide()
        self._editor_form_host = self._topology_form_host
        self._editor_form_layout = self._topology_form_layout
        self._topology_form_owned = True

    def _topology_form_release(self) -> None:
        if not getattr(self, "_topology_form_owned", False):
            return
        self._clear_layout(self._topology_form_layout)
        self._editor_form_host = self._hm_form_host
        self._editor_form_layout = self._hm_form_layout
        self._topology_form_owned = False
        self._editor_selection = None
        try:
            self._update_editor_panel()
        except Exception:  # noqa: BLE001 - cosmetic panel sync
            pass

    def _topology_node_selection(self, node) -> dict | None:
        """Editor selection dict for a diagram node (component or free marker)."""
        des = node.designator or node.label or ""
        if des.startswith("EDIT_") and self._project is not None:
            ed = self._project.directive_by_id(des[len("EDIT_"):])
            if ed is not None and ed.kind == "free":
                return {"kind": "free", "id": ed.id}
        rec = self._component_record(des)
        if rec is None:
            return {"kind": "component", "designator": des,
                    "nets": sorted({p.net for p in node.ports
                                    if p.net and not p.net.startswith("__")}),
                    "bbox": None, "unlocked": False}
        return {"kind": "component", "designator": rec.get("designator"),
                "nets": list(rec.get("nets", [])), "bbox": rec.get("bbox"),
                "unlocked": False}

    def _topology_open_sidebar(self, node) -> None:
        """Show ``node``'s PDN properties in the sidebar (the editor form)."""
        sel = self._topology_node_selection(node)
        if sel is None:
            return
        # One right-hand panel at a time: the part form replaces What-If.
        # It edits the base design, so put the base back on screen.
        if getattr(self, "_whatif_open", {}).get("topology"):
            self._set_whatif_panel_visible(False, where="topology")
        ensure = getattr(self, "_whatif_ensure_base", None)
        if ensure is not None:
            ensure()
        self._topology_side_node = node
        role = "SERIES" if node.role in ("RESISTOR", "SERIES") else node.role
        self._topology_side_title.setText(
            f"<b>{_esc(node.label)}</b> &nbsp;<span style='color:{_T()['fg_muted']};'>"
            f"{_esc(role.replace('_', ' ').title())}</span>")
        self._topology_side_jump.setEnabled(bool(node.jump_row))
        self._topology_form_activate()
        self._editor_multi = []
        eds = [d for d in (self._project.editor_directives if self._project else [])
               if sel.get("kind") == "component" and d.kind == "component"
               and d.designator == sel.get("designator")]
        pick = self._topology_side_pick
        pick.blockSignals(True)
        pick.clear()
        for d in eds:
            pick.addItem(f"{d.role} on {d.p_net or '?'}", d.id)
        pick.blockSignals(False)
        pick.setVisible(len(eds) > 1)
        if len(eds) > 1:
            sel["directive_id"] = eds[0].id
        self._editor_selection = sel
        try:
            self._populate_editor_form()
        except Exception as e:  # noqa: BLE001 - keep the tab usable
            import logging
            logging.getLogger(__name__).exception("Topology sidebar form failed")
            self._topology_side_hint.setText(f"Could not open the form: {e}")
        if self._topology_form_layout.count():
            self._topology_side_hint.setText("")
        self._topology_form_host.show()
        self._topology_sidebar.show()

    def _on_topology_side_pick(self, index: int) -> None:
        sel = getattr(self, "_editor_selection", None)
        if index < 0 or not sel or not getattr(self, "_topology_form_owned", False):
            return
        sel["directive_id"] = self._topology_side_pick.itemData(index)
        self._populate_editor_form()

    def _topology_close_sidebar(self) -> None:
        self._topology_form_release()
        self._topology_side_node = None
        sidebar = getattr(self, "_topology_sidebar", None)
        if sidebar is not None:
            sidebar.hide()

    def _topology_side_jump_clicked(self) -> None:
        node = self._topology_side_node
        if node is not None and node.jump_row:
            self._topology_close_sidebar()
            self._jump_to_node(node.jump_row)

    def _on_tabs_changed_for_topology(self, index: int) -> None:
        place = getattr(self, "_whatif_place_panel", None)
        if place is not None:
            place()
        if index != getattr(self, "_topology_tab_index", -1):
            self._topology_close_sidebar()
        else:
            self._topology_refresh_resolve_btn()

    # --- Resolve ------------------------------------------------------------

    def _topology_refresh_resolve_btn(self) -> None:
        btn = getattr(self, "_topology_resolve_btn", None)
        if btn is None:
            return
        try:
            enabled = bool(self._resolve_button_enabled())
        except Exception:  # noqa: BLE001 - viewer without editor state
            enabled = False
        btn.setEnabled(enabled)
        # Icon-only; the label ("Solve" before the first solve, else
        # "Resolve") shows only while a hover has the button expanded.
        exp = getattr(self, "_topology_expanders", {}).get(btn)
        if exp is not None:
            btn.setText(exp.current_label())
        btn.setToolTip(
            "Set up a source and a sink first - there is nothing to solve yet."
            if getattr(self, "_topology_needs_setup", False)
            else "Re-run the solver with the current editor changes applied")

    def _topology_show_setup_panel(self, on: bool) -> None:
        """Show (or hide) the "nothing set up yet" panel over the diagram;
        Solve's tooltip says why it is greyed out. What-If stays selectable,
        as on the Board: its panel itself says a solve is needed first."""
        from fypa.viewer.empty_state import (
            ACTION_SETUP, SETUP_TITLE, EmptyStatePanel,
        )

        self._topology_needs_setup = bool(on)
        view = self._topology_view
        panel = getattr(view, "_empty_panel", None)
        if on and isinstance(panel, EmptyStatePanel):
            panel.set_state(
                SETUP_TITLE,
                "The topology shows how power flows from each source, "
                "through its regulators, to every load. Define at least one "
                "source and one sink to draw it.",
                ACTION_SETUP,
                has_schematic=(self.metadata or {}).get("source_kind")
                != "gerber")
        show = getattr(view, "show_empty_panel", None)
        if show is not None:
            show(bool(on))
        self._topology_refresh_resolve_btn()

    def _on_topology_resolve(self) -> None:
        self._topology_close_sidebar()
        self._on_resolve_clicked()


    # --- Auto setup / click-to-add --------------------------------------------

    def _topology_loaded_for_setup(self, title: str = "Auto setup"):
        """The loaded design, or ``None`` after telling the user why not
        (in a message box titled ``title`` - Auto setup, Manual setup or
        Import setup, whichever asked).

        A viewer opened through the solve cache (or from a bare solve
        pickle) carries no design in memory. Fetch it the way a Resolve
        does - the project's design-info cache, else a fresh extract - and
        if the Altium project isn't where the solve says it was, ask the
        user to locate the .PrjPcb rather than sending them to re-import."""
        loaded = self._design_in_memory()
        if loaded is None:
            if (self.metadata or {}).get("source_kind") == "gerber":
                QMessageBox.information(
                    self, title,
                    "This design was imported from Gerber files, which carry "
                    "no schematic, so FYPA can't list its parts and pins. "
                    "Place sources and sinks on the board instead: switch on "
                    "Edit, then click copper to drop a marker.")
                return None
            loaded = self._fetch_design_info(
                title, "pin names, nets and part parameters")
            if loaded is None:
                return None
        ex = loaded.extracted
        if getattr(ex, "compiled_netlist", None) is None:
            QMessageBox.information(
                self, title,
                "This design has no schematic netlist, so FYPA can't list its "
                "parts and pins. Place sources and sinks on the board "
                "instead: switch on Edit, then click copper to drop a marker.")
            return None
        return loaded

    def _topology_run_autosetup(self) -> None:
        from fypa.viewer.autosetup_dialog import AutoSetupWizard
        from fypa.viewer.prefs import load_autosetup_datasheet_dir, save_autosetup_datasheet_dir

        loaded = self._topology_loaded_for_setup()
        if loaded is None:
            return
        project = self._ensure_project()
        wiz = AutoSetupWizard(
            self, loaded=loaded,
            editor_directives=list(project.editor_directives),
            datasheet_dir=load_autosetup_datasheet_dir(),
        )
        ok = wiz.exec()
        save_autosetup_datasheet_dir(wiz.datasheet_dir())
        if wiz.result is not None:
            self._autosetup_last = (wiz.result, wiz.view)
        if not ok:
            return
        added, skipped = self._topology_apply_suggestions(wiz.chosen())
        msg = f"Auto setup added {added} directive(s)"
        if skipped:
            msg += f"; {skipped} skipped (already present)"
        self.statusBar().showMessage(msg + " - press Resolve to solve.", 10000)

    def _topology_apply_suggestions(self, suggestions) -> tuple[int, int]:
        """Turn accepted suggestions into editor directives (no duplicates)."""
        project = self._ensure_project()
        have = {(d.designator or "", d.role, d.p_net or "")
                for d in project.editor_directives if d.kind == "component"}
        added = skipped = 0
        for s in suggestions:
            key = (s.designator, s.role, s.p_net or "")
            if key in have:
                skipped += 1
                continue
            project.upsert_directive(s.to_editor_directive())
            have.add(key)
            added += 1
        if added:
            self._topology_directives_changed()
        self._topology_refresh_resolve_btn()
        return added, skipped

    def _topology_directives_changed(self) -> None:
        """Editor directives changed from the Topology tab: flag the solve
        stale and refresh the Board markers and the diagram."""
        self._mark_project_dirty()
        for hook in ("_update_pending_rails", "_refresh_editor_markers"):
            fn = getattr(self, hook, None)
            if fn is not None:
                try:
                    fn()
                except Exception:  # noqa: BLE001 - cosmetic refresh
                    pass
        self._schedule_topology_refresh()
        self._topology_refresh_resolve_btn()

    def _topology_run_manual_setup(self, *, net: str | None = None,
                                   role: str | None = None) -> None:
        """Open the Manual setup wizard. With ``net`` and ``role`` (SOURCE,
        REGULATOR or SINK - the click-to-add menu on an undriven / no-load
        tag) it opens on a new entry for that net, prefilled from the
        auto-setup classifier's pick when it has one."""
        from fypa.autosetup import manual as M
        from fypa.project_file import ensure_marker_names
        from fypa.viewer.manual_setup_dialog import ManualSetupDialog

        pair = self._topology_design_view()
        if pair is None:
            return
        result, view = pair
        project = self._ensure_project()
        preset = None
        if net and role:
            from fypa.autosetup import candidates_for_net

            cands = candidates_for_net(view, result, net, role)
            drawn: set[str] = set()
            if role == "SINK":
                # A new load on a rail is a *different* part: never preset
                # one already drawn on it (the existing sink, its source…).
                drawn = self._topology_parts_on_net(net)
                cands = [c for c in cands if c.designator.upper() not in drawn]
            pick = cands[0] if cands and cands[0].confidence >= 0.5 else None
            preset = M.preset(view, net, role, pick, taken=drawn)
        dlg = ManualSetupDialog(self, view=view,
                                editor_directives=list(project.editor_directives),
                                metadata=self.metadata, preset=preset)
        if not dlg.exec():
            return
        # The dialog owns supplies and loads bound to parts and the copper
        # source / sink markers; series elements (Bridges tab) are kept
        # as-is. Edited rows stay where they were in the project's list (so
        # an untouched setup compares equal), removed ones drop out and new
        # ones go at the end.
        owned = ("SOURCE", "SINK", "REGULATOR")
        before = [d.to_dict() for d in project.editor_directives]
        edited = {d.id: d for d in dlg.result_directives()}
        merged = []
        for d in project.editor_directives:
            mine = ((d.kind == "component" and d.role in owned)
                    or M.is_listed_free_marker(d))
            if not mine:
                merged.append(d)
            elif d.id in edited:
                merged.append(edited.pop(d.id))
        project.editor_directives = merged + list(edited.values())
        ensure_marker_names(project.editor_directives)   # names left blank
        if [d.to_dict() for d in project.editor_directives] != before:
            self._topology_close_sidebar()
            self._topology_directives_changed()
            self.statusBar().showMessage(
                "Manual setup applied - press Resolve to solve.", 8000)
        if dlg.locate_id:
            self._topology_show_marker_on_board(dlg.locate_id)

    def _topology_show_marker_on_board(self, directive_id: str) -> None:
        """Switch to the Board zoomed to a free marker (Manual setup's
        "Apply and show on Board"), selecting it when editor mode is on."""
        project = self._ensure_project()
        d = project.directive_by_id(directive_id)
        if d is None or d.anchor_xy is None:
            return
        self._topology_close_sidebar()
        self._jump_to_xy(float(d.anchor_xy[0]), float(d.anchor_xy[1]),
                         [d.layer_id] if d.layer_id is not None else [])
        if getattr(self, "_editor_mode", False):
            self._select_free_marker(d)
        else:
            self.statusBar().showMessage(
                "Turn on editor mode to move this marker or change its net.", 8000)

    def _topology_parts_on_net(self, net: str) -> set[str]:
        """Designators (upper) of parts drawn with a port on ``net``."""
        model = getattr(self, "_topology_model", None)
        if model is None:
            return set()
        return {
            (n.designator or "").split("#")[0].upper()
            for n in model.nodes
            if any(p.net == net for p in n.ports)
        }

    def _topology_design_view(self):
        """Design view for click-to-add - the last auto-setup run's, or a
        quick one without the power-port scan."""
        last = getattr(self, "_autosetup_last", None)
        if last is not None:
            return last
        loaded = self._topology_loaded_for_setup("Manual setup")
        if loaded is None:
            return None
        from fypa.autosetup import build_design_view, suggest

        project = self._ensure_project()
        view = build_design_view(loaded, editor_directives=project.editor_directives,
                                 scan_schdocs=False)
        return suggest(view), view

    def _topology_tag_menu(self, tag, global_pos) -> None:
        """Click on an undriven / no-load tag: offer to add what's missing."""
        menu = QMenu(self)
        net = tag.net
        if tag.kind == "undriven":
            menu.addAction(f"Add a SOURCE on {net}…",
                           lambda: self._topology_run_manual_setup(net=net, role="SOURCE"))
            menu.addAction(f"Add a REGULATOR feeding {net}…",
                           lambda: self._topology_run_manual_setup(net=net, role="REGULATOR"))
        menu.addAction(f"Add a SINK on {net}…",
                       lambda: self._topology_run_manual_setup(net=net, role="SINK"))
        if tag.target is not None:
            menu.addSeparator()
            menu.addAction("Jump to where it continues",
                           lambda: self._topology_view.go_to(tag.target, net=net))
        menu.exec(global_pos)

    def _install_topology_shortcuts(self, parent: QWidget) -> None:
        """Keyboard zoom shortcuts active while the Topology tab is focused."""
        for keys, slot in (
            ("Ctrl++", self._topology_zoom_in),
            ("Ctrl+=", self._topology_zoom_in),
            ("Ctrl+-", self._topology_zoom_out),
            ("Ctrl+0", self._topology_fit),
        ):
            sc = QShortcut(QKeySequence(keys), parent)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(slot)

    def _topology_zoom_in(self) -> None:
        view = getattr(self, "_topology_view", None)
        if view is not None:
            view.zoom_in()

    def _topology_zoom_out(self) -> None:
        view = getattr(self, "_topology_view", None)
        if view is not None:
            view.zoom_out()

    def _topology_fit(self) -> None:
        view = getattr(self, "_topology_view", None)
        if view is not None:
            view.fit_in_view()

    def _on_topology_cascade_toggled(self, checked: bool) -> None:
        from fypa.viewer.prefs import save_topology_cascade

        self._topology_cascade = bool(checked)
        save_topology_cascade(self._topology_cascade)
        self._refresh_topology_live(reuse_metadata=True)

    def _on_topology_bridges_toggled(self, checked: bool) -> None:
        from fypa.viewer.prefs import save_topology_show_bridges

        self._topology_show_bridges = bool(checked)
        save_topology_show_bridges(self._topology_show_bridges)
        self._topology_keep_view_once = True  # keep zoom / scroll, don't refit
        self._refresh_topology_live(reuse_metadata=True)

    def _on_topology_add_handles_toggled(self, checked: bool) -> None:
        from fypa.viewer.prefs import save_topology_show_add_handles

        self._topology_show_add_handles = bool(checked)
        save_topology_show_add_handles(self._topology_show_add_handles)
        self._topology_keep_view_once = True  # keep zoom / scroll, don't refit
        self._refresh_topology_live(reuse_metadata=True)

    def _on_topology_gauges_toggled(self, checked: bool) -> None:
        from fypa.viewer.prefs import save_topology_show_gauges

        self._topology_show_gauges = bool(checked)
        save_topology_show_gauges(self._topology_show_gauges)
        act = getattr(self, "_topology_gauge_text_act", None)
        if act is not None:
            act.setEnabled(self._topology_show_gauges)
        self._topology_keep_view_once = True  # keep zoom / scroll, don't refit
        self._refresh_topology_live(reuse_metadata=True)

    def _on_topology_port_voltages_toggled(self, checked: bool) -> None:
        from fypa.viewer.prefs import save_topology_port_voltages

        self._topology_port_voltages = bool(checked)
        save_topology_port_voltages(self._topology_port_voltages)
        self._topology_keep_view_once = True  # keep zoom / scroll, don't refit
        self._refresh_topology_live(reuse_metadata=True)

    def _on_topology_gauge_text_toggled(self, checked: bool) -> None:
        from fypa.viewer.prefs import save_topology_gauge_style

        self._topology_gauge_style = "text" if checked else "bar"
        save_topology_gauge_style(self._topology_gauge_style)
        self._topology_keep_view_once = True  # keep zoom / scroll, don't refit
        self._refresh_topology_live(reuse_metadata=True)

    def _topology_gauge_readings(self):
        """``(readings, ports)`` from the last solve (cached per solve):
        the voltage at every load's pins keyed by ``LoadResult.key``, and
        every supply port's voltage keyed ``"ROLE:label:TERM"``; ``None``
        with no solve, or if it fails."""
        solution, metadata = getattr(self, "solution", None), self.metadata
        if solution is None or metadata is None:
            return None
        key = (id(solution), id(metadata))
        cache = getattr(self, "_topology_gauge_cache", None)
        if cache is not None and cache[0] == key:
            return cache[1]
        try:
            from fypa.report.collect import topology_results
            from fypa.topology.gauges import readings_from_loads

            loads, ports = topology_results(solution, metadata)
            readings = (readings_from_loads(loads), ports)
        except Exception:  # noqa: BLE001 - gauges are optional
            import logging

            logging.getLogger(__name__).debug(
                "No voltage readings for the topology gauges", exc_info=True)
            readings = None
        self._topology_gauge_cache = (key, readings)
        return readings

    def _topology_view_in_memory(self):
        """Design view for the "+ SINK" handles, without prompting or loading
        anything: the last auto-setup run's, else one built (and cached) from
        the design already in memory. ``None`` when there's none (a solve
        opened without its design, a Gerber import): no handles are drawn."""
        last = getattr(self, "_autosetup_last", None)
        if last is not None:
            return last[1]
        loaded = getattr(self, "_loaded_project", None)
        ex = getattr(loaded, "extracted", None)
        if ex is None or getattr(ex, "compiled_netlist", None) is None:
            return None
        cache = getattr(self, "_topology_view_cache", None)
        if cache is not None and cache[0] is ex:
            return cache[1]
        try:
            from fypa.autosetup import build_design_view

            view = build_design_view(loaded, scan_schdocs=False)
        except Exception:  # noqa: BLE001 - handles are optional
            import logging

            logging.getLogger(__name__).debug(
                "No design view for topology add handles", exc_info=True)
            view = None
        self._topology_view_cache = (ex, view)
        return view

    def _topology_add_candidates(self) -> dict[str, set[str]] | None:
        """net (upper) -> ICs / connectors with a pin on it, for the rows
        layout's "+ SINK (n)" handles; ``None`` turns the handles off."""
        if not getattr(self, "_topology_show_add_handles", False):
            return None
        maps = self._topology_design_maps()
        return None if maps is None else maps[0]

    def _topology_premerge_parts(self) -> dict[str, set[str]] | None:
        """Pre-merge net -> parts on it, for rails merged through 0 Ω links:
        lets the rows layout draw a link's far side (``R106 → +ETH_2V5``)."""
        maps = self._topology_design_maps()
        return None if maps is None else maps[1]

    def _topology_design_maps(
        self,
    ) -> tuple[dict[str, set[str]] | None, dict[str, set[str]] | None] | None:
        """(load candidates, pre-merge parts) for the rows layout.

        From the design in memory when there is one (and saved beside the
        project's cache); otherwise - a project reopened from its solve
        cache - from that saved copy, so the diagram doesn't depend on how
        the project was opened."""
        from fypa.autosetup import manual as M

        view = self._topology_view_in_memory()
        if view is None:
            return self._topology_read_cands_sidecar()
        cache = getattr(self, "_topology_cands_cache", None)
        if cache is not None and cache[0] is view:
            return cache[1]
        # Same "likely load" rule as Manual setup's load picker, so the
        # handle's n is the number of parts that picker lists.
        out = (M.likely_loads_by_net(view), M.premerge_parts_by_net(view))
        self._topology_cands_cache = (view, out)
        path = self._topology_cands_sidecar_path()
        if path is not None:
            try:
                M.save_load_candidates(path, out[0], out[1])
            except OSError:
                import logging

                logging.getLogger(__name__).debug(
                    "Could not save %s", path, exc_info=True)
        return out

    def _topology_cands_sidecar_path(self) -> Path | None:
        """``load-candidates.json`` in this Altium project's cache folder."""
        md = self.metadata or {}
        prj = md.get("prjpcb_path")
        if not prj or md.get("source_kind") == "gerber":
            return None
        try:
            from fypa.autosetup.manual import LOAD_CANDIDATES_FILE
            from fypa.cli import _project_cache_dir

            pcb = md.get("pcbdoc_path")
            return _project_cache_dir(Path(prj), Path(pcb) if pcb else None) \
                / LOAD_CANDIDATES_FILE
        except Exception:  # noqa: BLE001 - handles are optional
            return None

    def _topology_read_cands_sidecar(
        self,
    ) -> tuple[dict[str, set[str]] | None, dict[str, set[str]] | None] | None:
        """The saved (candidates, pre-merge parts) maps (cached on path +
        mtime); ``None`` if this project has never been opened with its
        design in memory."""
        path = self._topology_cands_sidecar_path()
        if path is None:
            return None
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        cache = getattr(self, "_topology_sidecar_cache", None)
        if cache is not None and cache[0] == (path, mtime):
            return cache[1]
        from fypa.autosetup.manual import read_load_candidates, read_premerge_parts

        cands = read_load_candidates(path)
        out = None if cands is None else (cands, read_premerge_parts(path))
        self._topology_sidecar_cache = ((path, mtime), out)
        return out

    def _topology_preview_metadata(self) -> dict | None:
        """Metadata dict shown on the Topology tab (incl. pending editor edits)."""
        from fypa.project_file import with_marker_names
        from fypa.topology.preview import metadata_for_topology

        if self.metadata is None:
            return None
        md = metadata_for_topology(
            self.metadata,
            loaded=self._loaded_project,
            editor_directives=(
                list(self._project.editor_directives)
                if self._project is not None else None
            ),
            copper_names=(
                list(self._project.copper_names)
                if self._project is not None else None
            ),
            live_preview=self._topology_live_preview_needed(),
        )
        # Free markers show their names, not their EDIT_<id> keys.
        return with_marker_names(
            md, self._project.editor_directives if self._project is not None else None)

    def _populate_topology(self, *, reuse_metadata: bool = False) -> None:
        """Render the topology SVG from setup metadata.

        ``reuse_metadata``: only a view setting changed (a row collapsed,
        Bridges / + SINK / Cascade toggled), not the setup - reuse the last
        preview metadata rather than rebuild it. With pending edits that
        rebuild re-applies every editor directive to a copy of the design and
        re-derives the solve metadata, which takes seconds on a large board,
        while the diagram itself builds and renders in milliseconds."""
        from fypa.topology import build_topology_model, render_topology_svg

        preview_md = None
        cached = getattr(self, "_topology_md_cache", None)
        key = (id(self.metadata), id(self._loaded_project), id(self._project))
        if reuse_metadata and cached is not None and cached[0] == key:
            preview_md = cached[1]
        else:
            preview_md = self._topology_preview_metadata()
            self._topology_md_cache = (key, preview_md)
        # Global Focus: only the parts on the focused rail. Applied after the
        # cache so switching rail doesn't rebuild the preview metadata.
        preview_md = self._global_focus_metadata(preview_md)
        self._topology_show_setup_panel(False)
        if preview_md is None:
            self._topology_view.set_empty_message("No setup metadata available.")
            self._topology_hint.setText("")
            self._topology_model = None
            return
        if not any(
            d.get("role") in ("SOURCE", "SINK", "REGULATOR")
            for d in preview_md.get("directives") or []
        ):
            # Without a source or sink there's no power flow to draw - at
            # most a few orphan SERIES boxes, which reads as a broken diagram.
            # The setup panel says so and offers the ways to fix it.
            self._topology_view.set_empty_message("")
            self._topology_show_setup_panel(True)
            self._topology_hint.setText("")
            self._topology_model = None
            return

        self._topology_refresh_resolve_btn()
        model = build_topology_model(
            preview_md,
            layout="rows",
            show_bridges=getattr(self, "_topology_show_bridges", False),
            add_candidates=self._topology_add_candidates(),
            premerge_parts=self._topology_premerge_parts(),
            collapsed=getattr(self, "_topology_collapsed", ()),
            cascade=getattr(self, "_topology_cascade", False),
            gauges=(getattr(self, "_topology_gauge_style", "bar")
                    if getattr(self, "_topology_show_gauges", True) else ""),
            port_voltages=bool(getattr(self, "_topology_port_voltages", True)),
        )
        if model.port_voltages or any(n.gauges for n in model.nodes):
            from fypa.topology.gauges import apply_readings

            res = self._topology_gauge_readings()
            apply_readings(model, res[0] if res else None,
                           ports=res[1] if res else None,
                           stale=bool(getattr(self, "_solve_stale", False)))
        self._topology_model = model
        # Transparent: the view paints the canvas and its dot grid behind.
        svg = render_topology_svg(model, theme=current_theme(), background=False)
        keep = getattr(self, "_topology_keep_view_once", False)
        self._topology_keep_view_once = False
        self._topology_view.set_diagram_svg(
            QByteArray(svg.encode("utf-8")), keep_view=keep)
        n_dir = len((preview_md or {}).get("directives") or [])
        n_nodes = sum(1 for n in model.nodes if n.role != "GND")
        n_wires = len(model.wires)
        if model.layout == "rows":
            n_rows = sum(
                1 for b in model.bands
                if b.kind in ("source", "regulator", "undriven", "other")
            )
            n_undriven = sum(1 for b in model.bands if b.kind == "undriven")
            pending = self._topology_live_preview_needed() and bool(
                self._project_dirty
                or self._project.editor_directives
                or self._project.copper_names
            )
            bits = [f"{n_dir} directive(s)", f"{n_nodes} part(s)", f"{n_rows} row(s)"]
            if n_undriven:
                bits.append(f"{n_undriven} undriven rail(s)")
            self._topology_hint.setText(
                ", ".join(bits) + (" (pending edits)" if pending else "")
            )
            return
        # Not merely "live preview": a stub previews its own annotations
        # with no edits pending.
        pending = self._topology_live_preview_needed() and bool(
            self._project_dirty
            or self._project.editor_directives
            or self._project.copper_names
        )
        suffix = " (pending edits)" if pending else ""
        self._topology_hint.setText(
            f"{n_dir} directive(s), {n_nodes} node(s), {n_wires} wire(s){suffix}"
        )

    def _topology_live_preview_needed(self) -> bool:
        """True when editor state should override solve metadata: unsaved
        edits, or saved ones the loaded solve predates (a stub, or a project
        whose editor directives were never solved). Without the second case
        a saved-but-unsolved source reads as "no sources or sinks"."""
        if not (self._project_dirty
                or getattr(self, "_initial_solve_stale", False)):
            return False
        if self._project is None:
            return False
        return bool(
            self._project.editor_directives
            or self._project.copper_names
            or self._loaded_project is not None
        )

    def _schedule_topology_refresh(self) -> None:
        """Debounced live refresh while the Topology tab is visible."""
        view = getattr(self, "_topology_view", None)
        if view is None:
            return
        timer = getattr(self, "_topology_refresh_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._refresh_topology_live)
            self._topology_refresh_timer = timer
        timer.start(50)

    def _refresh_topology_live(self, *, reuse_metadata: bool = False) -> None:
        """Rebuild the topology diagram from current editor + metadata state
        (``reuse_metadata``: see :meth:`_populate_topology`)."""
        if getattr(self, "_topology_view", None) is None:
            return
        self._topology_populated = True
        self._populate_topology(reuse_metadata=reuse_metadata)

    def _topology_dump_debug(self) -> None:
        """Write topology.pkl, wiring.json, and topology.svg for offline debug."""
        preview_md = self._topology_preview_metadata()
        if preview_md is None:
            QMessageBox.information(
                self, "Nothing to dump",
                "No setup metadata is loaded - solve or open a project first.",
            )
            return

        start_dir = (
            getattr(self, "_topology_last_dump_dir", None)
            or self._project_cache_dir_str()
            or self._menu_start_dir()
        )
        out_dir_str = QFileDialog.getExistingDirectory(
            self,
            "Choose folder for topology dump",
            start_dir,
            options=_file_dialog_options() | QFileDialog.Option.ShowDirsOnly,
        )
        if not out_dir_str:
            return

        from fypa.topology.dump import (
            TOPOLOGY_PKL,
            TOPOLOGY_SVG,
            WIRING_JSON,
            dump_topology_debug,
        )

        try:
            model = getattr(self, "_topology_model", None)
            paths = dump_topology_debug(
                Path(out_dir_str),
                preview_md,
                model=model,
                theme=current_theme(),
            )
        except Exception as e:
            QMessageBox.critical(
                self, "Topology dump failed",
                f"Failed to write debug files to {out_dir_str}:\n\n"
                f"{type(e).__name__}: {e}",
            )
            return

        self._topology_last_dump_dir = out_dir_str
        names = ", ".join((TOPOLOGY_PKL, WIRING_JSON, TOPOLOGY_SVG))
        self.statusBar().showMessage(
            f"Wrote {names} to {out_dir_str}", 8000,
        )
        self._topology_hint.setText(
            f"Dumped {names} → {_esc(out_dir_str)}"
        )
        QMessageBox.information(
            self, "Topology dump",
            "Wrote:\n\n"
            f"  {paths[0]}\n"
            f"  {paths[1]}\n"
            f"  {paths[2]}\n\n"
            f"Analyze with:\n"
            f"  uv run python tools/dump_topology_wiring.py {paths[0]}",
        )

    def _on_topology_view_clicked(self, event) -> None:
        """Click on the diagram:

        * a part's ⌖ icon → jump to it on the Board;
        * a tag that continues elsewhere → pan to the other end;
        * an undriven / no-load tag → menu to add a source, regulator or sink;
        * a "+ SINK (n)" handle → Manual setup on a new load for that rail;
        * a row's header → collapse / expand that row;
        * a part → its PDN properties in the sidebar (the editor form);
        * empty space → close the sidebar."""
        from fypa.topology import find_component_at
        from fypa.topology.hit_test import find_jump_icon_at, find_tag_at

        model = getattr(self, "_topology_model", None)
        view = getattr(self, "_topology_view", None)
        if model is None or view is None:
            return
        x, y = view.diagram_pos(event.position())
        icon_node = find_jump_icon_at(model, x, y)
        if icon_node is not None:
            self._topology_close_sidebar()
            self._jump_to_node(icon_node.jump_row)
            return
        tag = find_tag_at(model, x, y)
        if tag is not None:
            if tag.kind == "add" and tag.net:
                self._topology_close_sidebar()
                self._topology_run_manual_setup(net=tag.net, role="SINK")
            elif tag.kind in ("undriven", "end") and tag.net:
                self._topology_tag_menu(tag, event.globalPosition().toPoint())
            elif tag.target is not None:
                view.go_to(tag.target, net=tag.net or None)
            return
        comp = find_component_at(model, x, y)
        if comp is not None:
            self._topology_open_sidebar(comp)
            return
        from fypa.topology.hit_test import find_band_header_at

        band = find_band_header_at(model, x, y)
        if band is not None:
            self._topology_toggle_row(band.key)
            return
        self._topology_close_sidebar()

    def _topology_toggle_row(self, key: str) -> None:
        """Collapse / expand one row, keeping the current zoom and scroll."""
        collapsed = getattr(self, "_topology_collapsed", None)
        if collapsed is None:
            collapsed = self._topology_collapsed = set()
        collapsed.symmetric_difference_update({key})
        self._topology_keep_view_once = True
        self._populate_topology(reuse_metadata=True)

    def _on_properties_anchor_clicked(self, url) -> None:
        """Handle clicks on toggle-anchors in the Properties tab."""
        href = url.toString()
        prefix = "toggle:"
        if not href.startswith(prefix):
            return
        # Key is the channel-aware label ("U5" or "U5#1") so each indexed
        # SOURCE/SINK channel toggles independently.
        toggle_key = href[len(prefix):]
        if toggle_key in self._expanded_directives:
            self._expanded_directives.discard(toggle_key)
        else:
            self._expanded_directives.add(toggle_key)
        self._refresh_properties_html()
