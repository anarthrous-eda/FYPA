"""The Topology tab."""
from __future__ import annotations

from pathlib import Path
from PySide6.QtCore import QByteArray, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (
    QFileDialog,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QMessageBox,
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
    """Zoomable/pannable topology diagram — SVG rendered as vector graphics."""

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
        self.setResizeAnchor(QGraphicsView.AnchorUnderMouse)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
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
        # C++ object then raises RuntimeError. Ignore it — nothing to fit.
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
        prev_anchor = self.transformationAnchor()
        self.setTransformationAnchor(QGraphicsView.NoAnchor)
        scene_before = self._map_view_to_scene(anchor)
        self.scale(factor, factor)
        scene_after = self._map_view_to_scene(anchor)
        delta = scene_before - scene_after
        self.translate(delta.x(), delta.y())
        self.setTransformationAnchor(prev_anchor)

    def zoom_in(self) -> None:
        self._zoom_by(_TOPOLOGY_ZOOM_STEP)

    def zoom_out(self) -> None:
        self._zoom_by(1.0 / _TOPOLOGY_ZOOM_STEP)

    def fit_in_view(self) -> None:
        if not self._diagram_loaded:
            return
        self.resetTransform()
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
        """Instant clear — used on diagram reload / theme rebuild."""
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
            event.buttons() & Qt.MiddleButton
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
        if event.button() == Qt.MiddleButton or (
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
        if event.button() in (Qt.MiddleButton, Qt.LeftButton):
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
        """Build the Topology tab — abstract PDN simulation schematic."""
        widget = QWidget(self.tabs)
        outer = QVBoxLayout(widget)
        outer.setContentsMargins(8, 8, 8, 8)

        intro = QLabel(
            "Flow diagram of the PDN simulation model — each "
            "directive is a box with input/output ports wired by net. "
            "Click a box to jump to its pad on the Heatmap. "
            "Ctrl+wheel to zoom; wheel to scroll; Shift+wheel horizontal; "
            "middle-drag or Space+drag to pan; arrow keys to scroll."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"QLabel {{ color: {_T()['fg_muted']}; }}")
        outer.addWidget(intro)

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
        toolbar.addStretch()
        outer.addLayout(toolbar)

        self._topology_view = _TopologyView(self, widget)
        self._topology_view.set_background(_t["bg"])
        outer.addWidget(self._topology_view, 1)

        self._topology_hint = QLabel("")
        self._topology_hint.setStyleSheet(
            f"QLabel {{ color: {_t['fg_dim']}; font-size: 9pt; }}"
        )
        outer.addWidget(self._topology_hint)

        self._topology_model = None
        self._install_topology_shortcuts(widget)
        return widget

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
            # Without a source or sink there's no power flow to draw — at
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

        model = build_topology_model(preview_md)
        self._topology_model = model
        svg = render_topology_svg(model, theme=current_theme())
        self._topology_view.set_diagram_svg(QByteArray(svg.encode("utf-8")))
        n_dir = len((preview_md or {}).get("directives") or [])
        n_nodes = sum(1 for n in model.nodes if n.role != "GND")
        n_wires = len(model.wires)
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
                "No setup metadata is loaded — solve or open a project first.",
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
        """Jump to the pad under the clicked schematic symbol."""
        from fypa.topology import find_component_at

        model = getattr(self, "_topology_model", None)
        view = getattr(self, "_topology_view", None)
        if model is None or view is None:
            return
        x, y = view.diagram_pos(event.position())
        comp = find_component_at(model, x, y)
        if comp is not None and comp.jump_row:
            self._jump_to_node(comp.jump_row)

    def _on_setup_anchor_clicked(self, url) -> None:
        """Handle clicks on toggle-anchors in the Setup tab."""
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
        self._refresh_setup_html()
