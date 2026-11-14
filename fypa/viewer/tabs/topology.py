"""The Topology tab."""
from __future__ import annotations

from pathlib import Path
from PySide6.QtCore import QByteArray, QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QKeySequence, QPainter, QShortcut
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
        self.setStyleSheet(
            f"QGraphicsView {{ background-color: {color}; border: none; }}"
        )

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

    def set_diagram_svg(self, svg_data: QByteArray) -> None:
        from PySide6.QtSvg import QSvgRenderer

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
        from PySide6.QtWidgets import QToolTip

        from fypa.topology import topology_tooltip_at
        from fypa.topology.hit_test import topology_net_at

        model = getattr(self._viewer, "_topology_model", None)
        if model is not None and self._diagram_loaded:
            x, y = self.diagram_pos(event.position())
            tip = topology_tooltip_at(model, x, y)
            if tip:
                QToolTip.showText(event.globalPosition().toPoint(), tip, self)
            else:
                QToolTip.hideText()
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
                )
                if clickable:
                    self.setCursor(Qt.PointingHandCursor)
                else:
                    self.unsetCursor()
            self._set_highlighted_net(topology_net_at(model, x, y))
        else:
            self._clear_net_highlight()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        from PySide6.QtWidgets import QToolTip

        QToolTip.hideText()
        self._set_highlighted_net(None)
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
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
        # form back to the Heatmap panel before its host disappears.
        self._topology_form_release()
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
            load_topology_layout,
            load_topology_show_add_handles,
            load_topology_show_bridges,
        )

        toggle_qss = btn_qss + (
            f"QToolButton:checked {{ background-color: {_t['bg_hover']};"
            f"  border-color: {_t['accent']}; }}"
        )
        self._topology_layout = load_topology_layout()
        self._topology_show_bridges = load_topology_show_bridges()
        self._topology_show_add_handles = load_topology_show_add_handles()
        toolbar.addSpacing(12)
        self._topology_classic_btn = QToolButton()
        self._topology_classic_btn.setCheckable(True)
        self._topology_classic_btn.setChecked(self._topology_layout == "columns")
        self._sync_topology_classic_btn()
        self._topology_classic_btn.setCursor(Qt.PointingHandCursor)
        # Plain style: the label already says what a click does, so a
        # "checked" highlight would read as the wrong layout being on.
        self._topology_classic_btn.setStyleSheet(btn_qss)
        self._topology_classic_btn.toggled.connect(self._on_topology_classic_toggled)
        toolbar.addWidget(self._topology_classic_btn)
        self._topology_bridges_btn = QToolButton()
        self._topology_bridges_btn.setText("Bridges")
        self._topology_bridges_btn.setToolTip(
            "List 0 Ω auto-bridges on nets no source, sink or regulator uses "
            "(pull-up links, signal jumpers…)."
        )
        self._topology_bridges_btn.setCheckable(True)
        self._topology_bridges_btn.setChecked(self._topology_show_bridges)
        self._topology_bridges_btn.setEnabled(self._topology_layout == "rows")
        self._topology_bridges_btn.setCursor(Qt.PointingHandCursor)
        self._topology_bridges_btn.setStyleSheet(toggle_qss)
        self._topology_bridges_btn.toggled.connect(self._on_topology_bridges_toggled)
        toolbar.addWidget(self._topology_bridges_btn)
        self._topology_add_btn = QToolButton()
        self._topology_add_btn.setText("Add handles")
        self._topology_add_btn.setToolTip(
            "Show a dashed \"+ SINK (n)\" tap under each rail's last load when "
            "more parts on the rail (ICs, connectors) could be a load. Click one "
            "to add a SINK there in Manual setup. Needs the Altium design loaded."
        )
        self._topology_add_btn.setCheckable(True)
        self._topology_add_btn.setChecked(self._topology_show_add_handles)
        self._topology_add_btn.setEnabled(self._topology_layout == "rows")
        self._topology_add_btn.setCursor(Qt.PointingHandCursor)
        self._topology_add_btn.setStyleSheet(toggle_qss)
        self._topology_add_btn.toggled.connect(self._on_topology_add_handles_toggled)
        toolbar.addWidget(self._topology_add_btn)
        toolbar.addStretch()
        outer.addLayout(toolbar)

        # Setup + Resolve, stacked in a left-aligned column under the view
        # controls. Same look as the Heatmap's Resolve / What-If buttons
        # (6 px padding, 6 px radius, bold, badge icon left of the label);
        # the column is as wide as its widest button, so all three match.
        from fypa.viewer.assets import (
            _autosetup_icon,
            _manual_setup_icon,
            _resolve_icon,
        )

        def _big_btn_qss(color: str) -> str:
            return (
                "QPushButton {{ border: 1px solid {c}; border-radius: 6px;"
                "  padding: 6px 12px; font-weight: bold; text-align: left;"
                "  background-color: {bg}; color: {c}; }}"
                "QPushButton:hover:enabled {{ background-color: {hover}; }}"
                "QPushButton:disabled {{ color: {dim}; border-color: {dim}; }}"
                .format(c=color, bg=_t["bg"], hover=_t["bg_hover"],
                        dim=_t["fg_dim"])
            )

        def _big_btn(text: str, icon, color: str, tip: str, slot) -> QPushButton:
            b = QPushButton(text)
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
            "Add and edit sources, regulators and loads by hand: pick a net, "
            "the part on it and its pins, in one list per kind.",
            self._topology_run_manual_setup)
        # Same green and refresh badge as the Heatmap's Resolve button.
        self._topology_resolve_btn = _big_btn(
            "Resolve", _resolve_icon("#2ca92c", _t["bg"], _t["fg_dim"]),
            "#2ca92c",
            "Re-run the solver with the current editor changes applied",
            self._on_topology_resolve)

        setup_col = QVBoxLayout()
        setup_col.setSpacing(4)
        setup_col.setContentsMargins(0, 4, 0, 4)
        for b in (self._topology_autosetup_btn, self._topology_manual_btn,
                  self._topology_resolve_btn):
            setup_col.addWidget(b)
        setup_row = QHBoxLayout()
        setup_row.addLayout(setup_col)
        setup_row.addStretch()
        outer.addLayout(setup_row)

        self._topology_split = QSplitter(Qt.Horizontal, widget)
        self._topology_view = _TopologyView(self, self._topology_split)
        self._topology_view.set_background(_t["bg"])
        self._topology_split.addWidget(self._topology_view)
        self._topology_sidebar = self._build_topology_sidebar()
        self._topology_split.addWidget(self._topology_sidebar)
        self._topology_split.setStretchFactor(0, 1)
        self._topology_split.setStretchFactor(1, 0)
        self._topology_sidebar.hide()
        outer.addWidget(self._topology_split, 1)
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
        same form as the Heatmap's editor panel (see
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
        self._topology_side_jump = QPushButton("⌖  Show on Heatmap")
        self._topology_side_jump.setToolTip("Switch to the Heatmap and zoom to this part")
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
        its widgets are shared ``self._ef_*`` attributes - so the Heatmap
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
        awaiting = getattr(self, "_awaiting_first_solve", False)
        btn.setText("Solve" if awaiting else "Resolve")

    def _on_topology_resolve(self) -> None:
        self._topology_close_sidebar()
        self._on_resolve_clicked()

    # --- Auto setup / click-to-add --------------------------------------------

    def _topology_loaded_for_setup(self):
        """The loaded design, or ``None`` after telling the user why not.

        A viewer opened through the solve cache (or from a bare solve
        pickle) carries no design in memory. Fetch it the way a Resolve
        does - the project's design-info cache, else a fresh extract - and
        if the Altium project isn't where the solve says it was, ask the
        user to locate the .PrjPcb rather than sending them to re-import."""
        loaded = getattr(self, "_loaded_project", None)
        if loaded is None or getattr(loaded, "extracted", None) is None:
            loaded = self._topology_fetch_design()
            if loaded is None:
                return None
        ex = loaded.extracted
        if getattr(ex, "compiled_netlist", None) is None:
            QMessageBox.information(
                self, "Auto setup",
                "This design has no schematic netlist (e.g. a Gerber import), so "
                "pin names are unknown and parts can't be classified. Add "
                "directives by hand in the PDN editor instead.")
            return None
        return loaded

    def _topology_fetch_design(self):
        """Load the design behind this solve; ``None`` if the user cancels or
        it can't be read (the user has been told why)."""
        md = self.metadata or {}
        if md.get("source_kind") == "gerber":
            QMessageBox.information(
                self, "Auto setup",
                "This design was imported from Gerber files, which carry no "
                "schematic, so parts can't be classified. Add directives by "
                "hand in the PDN editor instead.")
            return None
        prj = Path(md["prjpcb_path"]) if md.get("prjpcb_path") else None
        pcb = Path(md["pcbdoc_path"]) if md.get("pcbdoc_path") else None
        if prj is None or not prj.is_file():
            prj, pcb = self._topology_ask_for_project(prj, pcb)
            if prj is None:
                return None
        loaded, error = self._topology_load_design_busy(prj, pcb)
        if loaded is None:
            QMessageBox.warning(
                self, "Auto setup",
                f"Could not load the Altium project\n{prj}\n\n{error}")
            return None
        # Keep it: the next Resolve reuses it instead of reloading (it still
        # checks the bridge / auto-SERIES settings and reloads on a change).
        self._loaded_project = loaded
        return loaded

    def _topology_ask_for_project(self, prj: Path | None, pcb: Path | None):
        """Ask for the .PrjPcb when the solve's own path is missing or gone.
        Returns ``(prjpcb, pcbdoc)``; ``(None, None)`` if cancelled."""
        where = (f"The Altium project this solve was made from isn't at\n{prj}\n"
                 "(moved, renamed, or on another machine)." if prj is not None
                 else "This solve doesn't record which Altium project it came from.")
        answer = QMessageBox.question(
            self, "Locate the Altium project",
            f"{where}\n\nAuto setup reads the design itself - pin names, nets "
            "and part parameters. Locate the project's .PrjPcb file?",
            QMessageBox.Open | QMessageBox.Cancel, QMessageBox.Open)
        if answer != QMessageBox.Open:
            return None, None
        start = str(prj.parent) if prj is not None and prj.parent.is_dir() else self._menu_start_dir()
        path_str, _ = QFileDialog.getOpenFileName(
            self, "Locate the Altium project", start,
            "Altium project (*.PrjPcb);;All files (*)",
            options=_file_dialog_options())
        if not path_str:
            return None, None
        new_prj = Path(path_str)
        # Keep the same board in a multi-PCB project when it's beside it.
        new_pcb = None
        if pcb is not None and (new_prj.parent / pcb.name).is_file():
            new_pcb = new_prj.parent / pcb.name
        return new_prj, new_pcb

    def _topology_load_design_busy(self, prj: Path, pcb: Path | None):
        """Design-info cache, else a full extract, on a worker thread behind a
        busy dialog. Returns ``(loaded | None, error text)``."""
        import threading

        from PySide6.QtCore import QEventLoop
        from PySide6.QtWidgets import QApplication, QProgressDialog

        no_bridge = set(getattr(self._project, "no_auto_bridge", None) or ()) or None
        out: dict = {}

        def work() -> None:
            try:
                from fypa.cli import _project_cache_dir, _try_load_cached_design_info

                loaded = None
                try:
                    loaded = _try_load_cached_design_info(prj, None, pcbdoc_path=pcb)
                except Exception:  # noqa: BLE001 - a bad cache just means re-extract
                    loaded = None
                if loaded is not None and no_bridge is not None and (
                        {d.upper() for d in no_bridge}
                        != set(getattr(loaded, "no_auto_bridge_applied", ()) or ())):
                    loaded = None
                if loaded is None:
                    from fypa.altium.loader import load_project

                    cache_dir = (_project_cache_dir(prj, pcb)
                                 if pcb is not None and prj.suffix.lower() == ".prjpcb"
                                 else None)
                    loaded = load_project(prj, pcbdoc_selector=pcb,
                                          no_auto_bridge=no_bridge,
                                          cache_dir=cache_dir)
                out["loaded"] = loaded
            except Exception as e:  # noqa: BLE001 - reported to the user
                import logging
                logging.getLogger(__name__).exception("Loading %s failed", prj)
                out["error"] = f"{type(e).__name__}: {e}"

        dlg = QProgressDialog(f"Reading the design…\n{prj.name}", None, 0, 0, self)
        dlg.setWindowTitle("Auto setup")
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(300)
        dlg.setValue(0)
        thread = threading.Thread(target=work, name="topology-design-load", daemon=True)
        thread.start()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            while thread.is_alive():
                QApplication.processEvents(QEventLoop.AllEvents, 50)
                thread.join(0.05)
        finally:
            QApplication.restoreOverrideCursor()
            dlg.close()
        return out.get("loaded"), out.get("error", "")

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
        stale and refresh the Heatmap markers and the diagram."""
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
            if role == "SINK":
                # A new load on a rail is a *different* part: never preset
                # one already drawn on it (the existing sink, its source…).
                drawn = self._topology_parts_on_net(net)
                cands = [c for c in cands if c.designator.upper() not in drawn]
            pick = cands[0] if cands and cands[0].confidence >= 0.5 else None
            preset = M.preset(view, net, role, pick)
        dlg = ManualSetupDialog(self, view=view,
                                editor_directives=list(project.editor_directives),
                                metadata=self.metadata, preset=preset)
        if not dlg.exec():
            return
        # Supplies and loads bound to parts are what the dialog owns; series
        # elements (Bridges tab) and free Heatmap markers are kept as-is.
        owned = ("SOURCE", "SINK", "REGULATOR")
        before = [d.to_dict() for d in project.editor_directives]
        keep = [d for d in project.editor_directives
                if not (d.kind == "component" and d.role in owned)]
        project.editor_directives = keep + dlg.result_directives()
        if [d.to_dict() for d in project.editor_directives] != before:
            self._topology_close_sidebar()
            self._topology_directives_changed()
            self.statusBar().showMessage(
                "Manual setup applied - press Resolve to solve.", 8000)

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
        loaded = self._topology_loaded_for_setup()
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

    def _sync_topology_classic_btn(self) -> None:
        """The layout button names the layout it switches *to*: "Classic"
        while the new rows layout is showing, "New" while classic is."""
        btn = self._topology_classic_btn
        if btn.isChecked():
            btn.setText("New")
            btn.setToolTip(
                "Switch to the new layout: one row per source / regulator, "
                "with tags where a rail continues on another row.")
        else:
            btn.setText("Classic")
            btn.setToolTip(
                "Switch to the classic layout: every part in one left-to-right "
                "chain with the nets wired between them.")

    def _on_topology_classic_toggled(self, checked: bool) -> None:
        from fypa.viewer.prefs import save_topology_layout

        self._sync_topology_classic_btn()
        self._topology_layout = "columns" if checked else "rows"
        save_topology_layout(self._topology_layout)
        self._topology_bridges_btn.setEnabled(not checked)
        self._topology_add_btn.setEnabled(not checked)
        self._refresh_topology_live()

    def _on_topology_bridges_toggled(self, checked: bool) -> None:
        from fypa.viewer.prefs import save_topology_show_bridges

        self._topology_show_bridges = bool(checked)
        save_topology_show_bridges(self._topology_show_bridges)
        self._refresh_topology_live()

    def _on_topology_add_handles_toggled(self, checked: bool) -> None:
        from fypa.viewer.prefs import save_topology_show_add_handles

        self._topology_show_add_handles = bool(checked)
        save_topology_show_add_handles(self._topology_show_add_handles)
        self._refresh_topology_live()

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
        if getattr(self, "_topology_layout", "rows") != "rows":
            return None
        view = self._topology_view_in_memory()
        if view is None:
            return None
        cache = getattr(self, "_topology_cands_cache", None)
        if cache is not None and cache[0] is view:
            return cache[1]
        from fypa.autosetup.nets import CONNECTOR_PREFIXES, IC_PREFIXES

        likely = IC_PREFIXES | CONNECTOR_PREFIXES
        out: dict[str, set[str]] = {}
        for part in view.parts:
            if part.prefix not in likely:
                continue
            for net in part.nets():
                if not view.is_gnd(net):
                    out.setdefault(net.upper(), set()).add(part.designator.upper())
        self._topology_cands_cache = (view, out)
        return out

    def _topology_preview_metadata(self) -> dict | None:
        """Metadata dict shown on the Topology tab (incl. pending editor edits)."""
        from fypa.topology.preview import metadata_for_topology

        if self.metadata is None:
            return None
        return metadata_for_topology(
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

    def _populate_topology(self) -> None:
        """Render the topology SVG from setup metadata."""
        from fypa.topology import build_topology_model, render_topology_svg

        preview_md = self._topology_preview_metadata()
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
            self._topology_view.set_empty_message(
                "No sources or sinks set up yet.\n\n"
                "Place SOURCE and SINK directives in the PDN editor (or "
                "annotate them on the schematic)\n"
                "to see the PDN topology."
            )
            self._topology_hint.setText("")
            self._topology_model = None
            return

        self._topology_refresh_resolve_btn()
        model = build_topology_model(
            preview_md,
            layout=getattr(self, "_topology_layout", "rows"),
            show_bridges=getattr(self, "_topology_show_bridges", False),
            add_candidates=self._topology_add_candidates(),
        )
        self._topology_model = model
        svg = render_topology_svg(model, theme=current_theme())
        self._topology_view.set_diagram_svg(QByteArray(svg.encode("utf-8")))
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

    def _refresh_topology_live(self) -> None:
        """Rebuild the topology diagram from current editor + metadata state."""
        if getattr(self, "_topology_view", None) is None:
            return
        self._topology_populated = True
        self._populate_topology()

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

        * a part's ⌖ icon → jump to it on the Heatmap;
        * a tag that continues elsewhere → pan to the other end;
        * an undriven / no-load tag → menu to add a source, regulator or sink;
        * a "+ SINK (n)" handle → Manual setup on a new load for that rail;
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
        self._topology_close_sidebar()

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
