"""The Copper ROI mode: target load, ranked fixes and their board highlights."""
from __future__ import annotations

import logging

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from fypa import copper_roi
from fypa.solution_sampling import face_to_vertex_average
from fypa.viewer.display import _COPPER_ROI_MODE
from fypa.viewer.theme import _T
from fypa.viewer.widgets import _qt_widget_alive

log = logging.getLogger(__name__)

# Label shown in the fix list per kind.
_KIND_LABELS = {"widen": "Widen", "parallel": "Add layer", "via": "Add via"}


class _CopperRoiMixin:
    """The Copper ROI mode: target load, ranked fixes and board highlights."""

    # Class-level defaults so the render and panel paths work before the
    # mode is ever entered (and on a viewer built without the panel).
    _roi_panel: QWidget | None = None
    _roi_key: str | None = None
    _roi_scores: list | None = None
    _roi_result = None
    _roi_selected: int = -1

    # --- panel ----------------------------------------------------------------

    def _build_roi_panel(self) -> QWidget:
        """Sidebar block shown under the Mode combo in Copper ROI mode: the
        target load, what its budget is, and the ranked fixes."""
        t = _T()
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.setSpacing(4)

        lay.addWidget(QLabel("<b>Target load</b>"))
        self._roi_target_combo = QComboBox()
        self._roi_target_combo.setToolTip(
            "The load whose voltage the map ranks copper for. Loads are "
            "listed by how much of their drop budget they use: the drop "
            "from the rail's setpoint over (nominal − PDN_MIN_V) where "
            f"PDN_MIN_V is set, otherwise over "
            f"{copper_roi.DEFAULT_BUDGET_PCT:g} % of nominal. The worst "
            "is chosen by default.")
        self._roi_target_combo.currentIndexChanged.connect(
            self._on_roi_target_changed)
        lay.addWidget(self._roi_target_combo)

        self._roi_summary = QLabel("")
        self._roi_summary.setWordWrap(True)
        self._roi_summary.setStyleSheet(f"QLabel {{ color: {t['fg_muted']}; }}")
        lay.addWidget(self._roi_summary)

        lay.addWidget(QLabel("<b>Best fixes</b>"))
        self._roi_list = QListWidget()
        self._roi_list.setWordWrap(True)
        self._roi_list.setMinimumHeight(150)
        self._roi_list.setToolTip(
            "Click a fix to zoom to it. Widen: push that copper edge out. "
            "Add layer: a stitched parallel copy of the copper, same "
            "weight, on the named free layer. Add via: another via beside "
            "this one. Greyed fixes are blocked by other copper.")
        self._roi_list.currentRowChanged.connect(self._on_roi_fix_selected)
        lay.addWidget(self._roi_list)

        note = QLabel(
            "Estimates are first order: good for modest changes, "
            "optimistic for large ones. Re-solve to confirm a fix.")
        note.setWordWrap(True)
        note.setStyleSheet(
            f"QLabel {{ color: {t['fg_muted']}; font-size: 8pt; }}")
        lay.addWidget(note)

        panel.setVisible(False)
        self._roi_panel = panel
        return panel

    def _roi_available(self) -> bool:
        return bool(getattr(getattr(self, "solution", None),
                            "sensitivity", None))

    def _reset_copper_roi(self) -> None:
        """Forget every ROI result — a new solution invalidates them. If the
        mode is showing, rebuild it for the new solution once the swap is
        done; it only rebuilds on a mode change otherwise, so a re-solve
        made from inside the mode would keep showing the old answer."""
        self._roi_key = None
        self._roi_scores = None
        self._roi_result = None
        self._roi_selected = -1
        gl = getattr(self, "_gl_viewer", None)
        if gl is not None:
            gl.set_roi_highlights(None)
        combo = getattr(self, "mode_combo", None)
        if (combo is not None and _qt_widget_alive(combo)
                and combo.currentText() == _COPPER_ROI_MODE):
            QTimer.singleShot(0, self._refresh_roi_for_new_solution)

    def _refresh_roi_for_new_solution(self) -> None:
        if (not _qt_widget_alive(self.mode_combo)
                or self.mode_combo.currentText() != _COPPER_ROI_MODE):
            return
        self._on_mode_changed_for_roi(_COPPER_ROI_MODE)
        self._render_with_busy_popup()

    # --- mode + target --------------------------------------------------------

    def _on_mode_changed_for_roi(self, mode: str) -> None:
        """Runs before the re-render the mode change triggers, so the value
        field is ready when the heatmap asks for it."""
        on = mode == _COPPER_ROI_MODE
        if self._roi_panel is not None and _qt_widget_alive(self._roi_panel):
            self._roi_panel.setVisible(on)
        if not on:
            self._gl_viewer.set_roi_highlights(None)
            return
        self._ensure_roi_scores()
        if self._roi_key is None:
            target = copper_roi.default_target(self._roi_scores or [])
            if target is not None:
                self._set_roi_target(target.label, render=False)
        else:
            self._refresh_roi_highlights()

    def _ensure_roi_scores(self) -> None:
        """Rank the loads (once per solution) and fill the target combo."""
        if self._roi_scores is not None:
            return
        combo = self._roi_target_combo
        combo.blockSignals(True)
        combo.clear()
        if not self._roi_available():
            self._roi_scores = []
            combo.setEnabled(False)
            self._roi_summary.setText(
                "This solution has no copper-sensitivity data — it was solved "
                "before Copper ROI existed, or has no SINK loads. Re-solve to "
                "see where copper would help.")
            self._roi_list.clear()
            combo.blockSignals(False)
            return
        try:
            scores = copper_roi.rank_loads(self.solution, self.metadata or {})
        except Exception:
            log.exception("Copper ROI: ranking the loads failed")
            scores = []
        self._roi_scores = scores
        for s in scores:
            if not s.has_field:
                continue
            # Short enough for the sidebar; the basis is in the summary line.
            combo.addItem(f"{s.label} · {s.rail} · {s.used:.0%}", s.label)
            combo.setItemData(
                combo.count() - 1,
                f"{s.label} on {s.rail}: {s.used:.0%} of its drop budget "
                f"({s.basis_text})", Qt.ToolTipRole)
        combo.setEnabled(combo.count() > 0)
        combo.blockSignals(False)
        if not combo.count():
            self._roi_summary.setText("No solved SINK loads to rank copper for.")

    def _on_roi_target_changed(self, index: int) -> None:
        key = self._roi_target_combo.itemData(index)
        if key and key != self._roi_key:
            self._set_roi_target(key)

    def _roi_score(self, key: str | None):
        return next((s for s in (self._roi_scores or []) if s.label == key),
                    None)

    def _set_roi_target(self, key: str, *, render: bool = True) -> None:
        """Analyse the copper for one load, show it, and list its fixes."""
        self._roi_key = key
        combo = self._roi_target_combo
        idx = combo.findData(key)
        if idx >= 0 and combo.currentIndex() != idx:
            combo.blockSignals(True)
            combo.setCurrentIndex(idx)
            combo.blockSignals(False)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self._roi_result = copper_roi.analyse(
                self.solution, self.metadata or {}, key,
                copper_by_layer=self._roi_copper_by_layer())
        except Exception:
            log.exception("Copper ROI: analysing %s failed", key)
            self._roi_result = None
        finally:
            QApplication.restoreOverrideCursor()
        self._roi_selected = -1
        self._show_roi_rails()
        self._fill_roi_summary()
        self._fill_roi_list()
        self._refresh_roi_highlights()
        if render:
            self._render_with_busy_popup()

    def _roi_copper_by_layer(self) -> dict | None:
        """Every net's copper per physical layer, for the blocked-widening and
        free-layer checks. None falls back to the solved nets only."""
        try:
            shape_by_key, net_by_key = self._all_copper_poly_maps()
        except Exception:
            log.debug("Copper ROI: no all-copper geometry", exc_info=True)
            return None
        id_to_phys = {v: k for k, v in self._phys_name_to_layer_id.items()}
        out: dict[str, list] = {}
        for key, shp in shape_by_key.items():
            phys = id_to_phys.get(key[0])
            if phys is not None and shp is not None:
                out.setdefault(phys, []).append(
                    (net_by_key.get(key) or "(none)", shp))
        return out or None

    def _show_roi_rails(self) -> None:
        """Make sure the target's rail — and the rails its best fixes sit on,
        e.g. the return — are visible, without hiding anything."""
        want = set()
        score = self._roi_score(self._roi_key)
        if score is not None:
            want.add(score.rail)
        net_to_rail = {n: r for r, members in self._rail_to_members.items()
                       for n in members}
        for o in getattr(self._roi_result, "opportunities", [])[:5]:
            rail = net_to_rail.get(o.net)
            if rail:
                want.add(rail)
        changed = False
        for name, eye in self._rail_eye_buttons:
            if name in want and _qt_widget_alive(eye) \
                    and not eye.isVisibleState():
                eye.setVisibleState(True, partial=False, emit=False)
                self._fan_out_rail_eye_to_subnets(name, True)
                self._sync_rail_tree_node_partials(name)
                changed = True
        if changed:
            self._sync_all_rails_eye()
            self._sync_rail_only_visibility()

    # --- summary, list, highlights --------------------------------------------

    def _fill_roi_summary(self) -> None:
        s = self._roi_score(self._roi_key)
        if s is None:
            self._roi_summary.setText("")
            return
        status = "over budget" if s.used > 1 else "within budget"
        self._roi_summary.setText(
            f"{s.label} sees {s.drop_v * 1e3:.3g} mV of drop against a "
            f"{s.budget_v * 1e3:.3g} mV budget ({s.basis_text}) — "
            f"{s.used:.0%}, {status}. The heatmap shows where parallel "
            "copper would raise its voltage most.")

    def _fill_roi_list(self) -> None:
        lst = self._roi_list
        lst.blockSignals(True)
        lst.clear()
        opps = getattr(self._roi_result, "opportunities", None) or []
        muted = _T()["fg_muted"]
        for rank, o in enumerate(opps, 1):
            item = QListWidgetItem(
                f"{rank}. {_KIND_LABELS.get(o.kind, o.kind)}: "
                f"+{o.value_v * 1e3:.2g} mV\n{o.title}\n{o.detail}")
            item.setToolTip(f"{o.title}\n{o.detail}")
            if o.blocked:
                item.setForeground(QColor(muted))
            lst.addItem(item)
        if not opps and self._roi_result is not None:
            lst.addItem(QListWidgetItem(
                "No copper change here would move this load's voltage "
                "noticeably."))
        lst.blockSignals(False)

    def _refresh_roi_highlights(self) -> None:
        gl = getattr(self, "_gl_viewer", None)
        if gl is None:
            return
        opps = getattr(self._roi_result, "opportunities", None) or []
        if self.mode_combo.currentText() != _COPPER_ROI_MODE or not opps:
            gl.set_roi_highlights(None)
            return
        gl.set_roi_highlights([
            {"kind": o.kind, "coords": o.coords, "rings": o.rings,
             "x": o.x_mm, "y": o.y_mm,
             "rank": i + 1, "selected": i == self._roi_selected,
             "blocked": o.blocked}
            for i, o in enumerate(opps)])

    def _on_roi_fix_selected(self, row: int) -> None:
        """Zoom to a fix and emphasise it on the board."""
        opps = getattr(self._roi_result, "opportunities", None) or []
        if not 0 <= row < len(opps):
            return
        self._roi_selected = row
        o = opps[row]
        x0, y0, x1, y1 = o.bbox
        # Frame the fix with room to see what it connects to.
        pad = max(3.0, 0.3 * max(x1 - x0, y1 - y0))
        self._gl_viewer.fit_to_bounds(x0 - pad, x1 + pad, y0 - pad, y1 + pad)
        self._refresh_roi_highlights()

    def _show_copper_roi_for(self, label: str) -> None:
        """Nodes-tab entry point: switch to Copper ROI on this load."""
        self._reset_roi_target_only()
        self._ensure_roi_scores()
        if self._roi_score(label) is None and self._roi_scores:
            # A pin row names the designator; a channel label may add "#n".
            label = next((s.label for s in self._roi_scores
                          if s.label.split("#")[0] == label), label)
        self._roi_key = label
        self.tabs.setCurrentIndex(self._heatmap_tab_index)
        if self.mode_combo.currentText() == _COPPER_ROI_MODE:
            self._set_roi_target(label)
        else:
            # The mode change re-renders; analyse first so it has the field.
            self._set_roi_target(label, render=False)
            self.mode_combo.setCurrentText(_COPPER_ROI_MODE)

    def _reset_roi_target_only(self) -> None:
        self._roi_key = None
        self._roi_result = None
        self._roi_selected = -1

    # --- render hook ------------------------------------------------------------

    def _roi_vertex_values(self, layer_index: int, geom: dict) -> list:
        """Per kept mesh, the target's value density averaged onto vertices
        (V/mm², clipped at 0 — copper that would lower the voltage is rare
        and reads as no value). Zeros until a target is analysed."""
        per_mesh = (getattr(self._roi_result, "density", None) or {}
                    ).get(layer_index)
        out = []
        for (tris_local, _pot, _pd, n), mesh_i in zip(
                geom["_kept"], geom["_kept_mesh_idx"]):
            dens = (per_mesh[mesh_i]
                    if per_mesh is not None and mesh_i < len(per_mesh)
                    else None)
            if dens is None or not dens.size:
                out.append(np.zeros(n, dtype=np.float64))
                continue
            out.append(face_to_vertex_average(
                tris_local, np.maximum(dens, 0.0), n))
        return out
