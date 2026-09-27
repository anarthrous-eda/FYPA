"""Physical-layer and Rails lists in the sidebar, and the active layer."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QListWidgetItem, QToolButton, QWidget

from fypa.viewer.theme import _T
from fypa.viewer.widgets import _qt_widget_alive, EyeButton, FillToggleButton, TransparencyButton

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fypa.viewer.widgets import OutlineToggleButton


class _LayerPanelMixin:
    """Physical-layer and Rails lists in the sidebar, and the active layer."""

    # --- Layer-list helpers -------------------------------------------------

    # Fixed colour palette for the layer-list swatches. Matches Altium's
    # default "Signal And Plane Layers" palette so the swatches in this
    # viewer line up with what the user sees in Altium's Layers dialog.
    # Top/Bottom get dedicated colours; inner layers index into a cycle
    # by their position in the stackup (1st inner = cycle[0], etc.).
    _LAYER_SWATCH_COLOURS: dict[str, str] = {
        "top":    "#ff0000",
        "bottom": "#0000ff",
    }
    _INNER_LAYER_CYCLE: tuple[str, ...] = (
        "#bc8e00",  # 1st inner (L2)
        "#70dbfa",  # 2nd inner (L3)
        "#00cc66",  # 3rd inner (L4)
        "#9966ff",  # 4th inner (L5)
        "#00ffff",  # 5th inner (L6)
        "#800080",  # 6th inner (L7)
        "#ff00ff",  # 7th inner (L8)
        "#808000",  # 8th inner (L9)
        "#ffff00",  # 9th inner (L10)
        "#808080",  # 10th inner (L11)
        "#ffffff",  # 11th inner (L12)
        "#800080",  # 12th inner (L13)
        "#008080",  # 13th inner (L14)
        "#c0c0c0",  # 14th inner (L15)
    )

    # --- Selected (active) physical layer ----------------------------------
    # When the user clicks a row in the Physical layers control, that layer
    # becomes the "selected" / "active" layer. Only 0 or 1 layer is selected
    # at a time. The selected layer is lifted above every other physical
    # layer in the GPU draw order and the unselected layers' all-copper
    # overlay is dimmed toward ``_SELECTED_LAYER_DIM_RGB`` by the blend
    # factor below (0.0 = no change, 1.0 = fully replaced by the dim
    # target). ``_SELECTED_LAYER_UNSEL_ALPHA`` additionally scales the
    # unselected all-copper alpha (1.0 = no extra transparency). The row
    # widget's background uses ``_SELECTED_LAYER_BG`` from the active
    # theme when ``None``.
    _SELECTED_LAYER_DIM_FACTOR: float = 0.75
    _SELECTED_LAYER_DIM_RGB: tuple[float, float, float] = (0.22, 0.22, 0.22)
    _SELECTED_LAYER_UNSEL_ALPHA: float = 1.0
    _SELECTED_LAYER_BG: str | None = "#7b7b7b"  # None → theme's bg_selection

    def _layer_color_for(self, phys: str) -> str:
        # Colour is keyed purely on the layer's position in the stackup
        # ordering — not its name or id. Board layer names vary ("Top Layer"
        # vs "L1"), so a name check mis-colours designs like Corvette whose
        # layers are "L1".."L16". Ordering is reliable: the first layer in
        # the stackup is always red, the last always blue.
        rank = self._phys_stackup_rank.get(phys, 0)
        if rank == 0:
            return self._LAYER_SWATCH_COLOURS["top"]
        if rank == len(self._physicals) - 1:
            return self._LAYER_SWATCH_COLOURS["bottom"]
        # Inner layer — pick deterministically by stackup position so the
        # same layer keeps the same colour across re-renders. The 1st inner
        # layer has rank 1; subtract 1 to map it to cycle[0] (the cyan that
        # Altium uses for Mid Layer 1).
        idx = max(0, rank - 1)
        return self._INNER_LAYER_CYCLE[idx % len(self._INNER_LAYER_CYCLE)]

    def _build_layer_row_widget(self, eye: EyeButton, *,
                                  swatch_color: str | None,
                                  label_text: str, bold: bool,
                                  second_eye: EyeButton | None = None,
                                  fill_btn: FillToggleButton | None = None,
                                  outline_btn: OutlineToggleButton | None = None,
                                  transparency_btn: TransparencyButton | None = None,
                                  ) -> QWidget:
        """Build a single row for the layer list: eye(s) + swatch + name.

        ``second_eye``, when given, sits immediately right of the primary
        eye — the Physical layers control uses it for the per-layer "show
        all copper on this layer" toggle. ``fill_btn``, when given, is
        pinned to the far right — the wire-mesh / solid fill style for that
        all-copper view. ``transparency_btn``, when given, sits immediately
        left of ``fill_btn`` — the per-row transparency cycler that fades
        the all-copper overlay. ``outline_btn``, when given, sits just left
        of ``transparency_btn`` — the layer-outline toggle on the "All
        Rails" row (outlines only trace rail copper, so it belongs there).
        Per-rail and per-layer rows pass none of these and keep the
        original single-eye layout."""
        w = QWidget()
        # Object name + WA_StyledBackground let the selected-layer
        # highlight in _apply_layer_selection_highlight reliably paint a
        # solid background; a plain QWidget inside a stylesheet-styled
        # QListWidget otherwise renders transparent under setPalette.
        w.setObjectName("layerRow")
        w.setAttribute(Qt.WA_StyledBackground, True)
        layout = QHBoxLayout(w)
        layout.setContentsMargins(2, 1, 6, 1)
        layout.setSpacing(6)
        layout.addWidget(eye)
        if second_eye is not None:
            layout.addWidget(second_eye)
        if swatch_color is not None:
            swatch = QLabel()
            pix = QPixmap(14, 14)
            pix.fill(QColor(swatch_color))
            swatch.setPixmap(pix)
            swatch.setFixedSize(14, 14)
            layout.addWidget(swatch)
        else:
            # Keep the name label aligned with rows that have a swatch.
            spacer = QLabel()
            spacer.setFixedSize(14, 14)
            layout.addWidget(spacer)
        name = QLabel(label_text)
        _fg = _T()["fg"]
        name.setStyleSheet(
            f"QLabel {{ color: {_fg}; font-weight: bold; }}"
            if bold else f"QLabel {{ color: {_fg}; }}"
        )
        layout.addWidget(name)
        layout.addStretch(1)
        if outline_btn is not None:
            layout.addWidget(outline_btn)
        if transparency_btn is not None:
            layout.addWidget(transparency_btn)
        if fill_btn is not None:
            layout.addWidget(fill_btn)
        return w

    # --- Rails list (expandable subnet rows) ---------------------------------

    # Indent per tree depth (px). Kept modest so deep SERIES chains stay
    # readable in the default sidebar width.
    _RAIL_SUBNET_INDENT_PX = 10

    def _init_rail_list_state(self) -> None:
        """Allocate per-rail / per-subnet visibility state containers."""
        self._rail_eye_buttons: list[tuple[str, EyeButton]] = []
        self._subnet_eye_buttons: dict[str, dict[str, EyeButton]] = {}
        # Hidden parent keeps subnet EyeButtons alive while their list rows
        # are collapsed/removed — otherwise takeItem() deletes the row widget
        # and the C++ EyeButton along with it.
        self._subnet_eye_holder = QWidget(self)
        self._subnet_eye_holder.setFixedSize(0, 0)
        self._subnet_eye_holder.hide()
        self._rail_expand_buttons: dict[str, QToolButton] = {}
        self._subnet_expand_buttons: dict[tuple[str, str], QToolButton] = {}
        self._rail_subnet_items: dict[str, list[QListWidgetItem]] = {}
        self._rail_expanded: dict[str, bool] = {}
        # (rail, net) → whether that tree node's children are shown.
        self._subnet_node_expanded: dict[tuple[str, str], bool] = getattr(
            self, "_subnet_node_expanded", {},
        )
        self._rail_list_items: dict[str, QListWidgetItem] = {}
        self._rail_to_trees: dict = getattr(self, "_rail_to_trees", {})
        self._pending_rail_items: list = []
        self._pending_rail_list_items: dict[str, QListWidgetItem] = {}
        self._pending_rail_expand_buttons: dict[str, QToolButton] = {}
        self._pending_subnet_expand_buttons: dict[tuple[str, str], QToolButton] = {}
        self._pending_rail_subnet_items: dict[str, list[QListWidgetItem]] = {}
        self._pending_rail_expanded: dict[str, bool] = {}
        self._pending_subnet_node_expanded: dict[tuple[str, str], bool] = getattr(
            self, "_pending_subnet_node_expanded", {},
        )
        self._pending_rail_trees: dict = {}

    def _ground_rail_names(self) -> set[str]:
        return {"0v", "gnd", "ground", "vss"}

    def _default_rail_visible(self, rail: str) -> bool:
        if self._no_pdn_visibility():
            return False
        return rail.lower() not in self._ground_rail_names()

    def _make_expand_tool_button(
        self,
        *,
        expanded: bool,
        tip: str,
        on_toggled,
    ) -> QToolButton:
        btn = QToolButton()
        btn.setCheckable(True)
        btn.setChecked(expanded)
        btn.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        btn.setFixedSize(14, 14)
        btn.setAutoRaise(True)
        btn.setFocusPolicy(Qt.NoFocus)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setToolTip(tip)
        btn.toggled.connect(on_toggled)
        return btn

    def _build_rail_row_widget(
        self,
        eye: EyeButton,
        *,
        expand_btn: QToolButton | None = None,
        label_text: str,
        bold: bool = False,
        indent: int = 0,
    ) -> QWidget:
        """Build a row for the Rails list: eye + name, expand on the right."""
        w = QWidget()
        w.setObjectName("layerRow")
        w.setAttribute(Qt.WA_StyledBackground, True)
        layout = QHBoxLayout(w)
        layout.setContentsMargins(2 + indent, 1, 6, 1)
        layout.setSpacing(4)
        layout.addWidget(eye)
        name_spacer = QLabel()
        name_spacer.setFixedSize(14, 14)
        layout.addWidget(name_spacer)
        name = QLabel(label_text)
        _fg = _T()["fg"]
        name.setStyleSheet(
            f"QLabel {{ color: {_fg}; font-weight: bold; }}"
            if bold else f"QLabel {{ color: {_fg}; }}"
        )
        layout.addWidget(name)
        layout.addStretch(1)
        if expand_btn is not None:
            layout.addWidget(expand_btn)
        return w

    def _detach_subnet_eyes(self, rail: str) -> None:
        """Reparent subnet eyes off a list row before the row is destroyed."""
        holder = getattr(self, "_subnet_eye_holder", None)
        if holder is None:
            return
        for eye in self._subnet_eye_buttons.get(rail, {}).values():
            if _qt_widget_alive(eye):
                eye.setParent(holder)

    def _silence_rail_list_widgets(self) -> None:
        """Block signals on rail-list widgets about to be destroyed."""
        for _name, eye in getattr(self, "_rail_eye_buttons", []):
            if _qt_widget_alive(eye):
                eye.blockSignals(True)
        for btn in getattr(self, "_rail_expand_buttons", {}).values():
            if _qt_widget_alive(btn):
                btn.blockSignals(True)
        for btn in getattr(self, "_subnet_expand_buttons", {}).values():
            if _qt_widget_alive(btn):
                btn.blockSignals(True)
        for nets in getattr(self, "_subnet_eye_buttons", {}).values():
            for eye in nets.values():
                if _qt_widget_alive(eye):
                    eye.blockSignals(True)

    def _populate_rail_list(
        self,
        *,
        preserve_visibility: bool = False,
        saved_rails: dict[str, bool] | None = None,
        saved_subnets: dict[tuple[str, str], bool] | None = None,
        saved_expanded: dict[str, bool] | None = None,
        saved_subnet_expanded: dict[tuple[str, str], bool] | None = None,
    ) -> None:
        """Rebuild solved-rail rows (not including the 'All Rails' header)."""
        self._silence_rail_list_widgets()
        for rail in list(self._subnet_eye_buttons.keys()):
            self._detach_subnet_eyes(rail)
        while self.rail_list.count() > 1:
            self.rail_list.takeItem(self.rail_list.count() - 1)
        self._rail_eye_buttons.clear()
        self._subnet_eye_buttons.clear()
        self._rail_expand_buttons.clear()
        self._subnet_expand_buttons.clear()
        self._rail_subnet_items.clear()
        self._rail_list_items.clear()

        saved_rails = saved_rails or {}
        saved_subnets = saved_subnets or {}
        if saved_expanded is not None:
            self._rail_expanded = {
                r: bool(saved_expanded[r])
                for r in self._rails
                if r in saved_expanded
            }
        else:
            self._rail_expanded = {}
        if saved_subnet_expanded is not None:
            self._subnet_node_expanded = {
                k: bool(v) for k, v in saved_subnet_expanded.items()
                if k[0] in self._rails
            }

        for rail in self._rails:
            members = self._rail_to_members.get(rail, [rail])
            has_subnets = len(members) > 1

            eye = EyeButton(visible=False, shift_isolatable=True)
            eye.toggled_visible.connect(
                lambda on, r=rail: self._on_rail_eye_toggled(r, on),
            )
            eye.shift_clicked.connect(
                lambda r=rail: self._on_rail_eye_shift_clicked(r),
            )

            expand_btn = None
            expanded = bool(self._rail_expanded.get(rail, False))
            if has_subnets:
                expand_btn = self._make_expand_tool_button(
                    expanded=expanded,
                    tip="Show/hide subnet nets",
                    on_toggled=lambda exp, r=rail: self._on_rail_expand_toggled(
                        r, exp,
                    ),
                )
                self._rail_expand_buttons[rail] = expand_btn
                self._rail_expanded[rail] = expanded

            row = self._build_rail_row_widget(
                eye,
                expand_btn=expand_btn,
                label_text=rail,
                bold=False,
            )
            item = QListWidgetItem()
            item.setFlags(Qt.ItemIsEnabled)
            self.rail_list.addItem(item)
            item.setSizeHint(row.sizeHint())
            self.rail_list.setItemWidget(item, row)
            self._rail_eye_buttons.append((rail, eye))
            self._rail_list_items[rail] = item

            if has_subnets:
                self._subnet_eye_buttons[rail] = {}
                for net in members:
                    subnet_eye = EyeButton(
                        parent=self._subnet_eye_holder,
                        visible=False,
                        tip_show=(
                            f"Show {net} copper\n"
                            f"Ctrl+Click: include SERIES subtree"
                        ),
                        tip_hide=(
                            f"Hide {net} copper\n"
                            f"Ctrl+Click: include SERIES subtree"
                        ),
                        shift_isolatable=True,
                        partial_is_badge=True,
                    )
                    subnet_eye.toggled_visible.connect(
                        lambda on, r=rail, n=net: self._on_subnet_eye_toggled(
                            r, n, on,
                        ),
                    )
                    subnet_eye.shift_clicked.connect(
                        lambda r=rail, n=net: self._on_subnet_eye_shift_clicked(
                            r, n,
                        ),
                    )
                    subnet_eye.ctrl_clicked.connect(
                        lambda r=rail, n=net: self._on_subnet_eye_ctrl_clicked(
                            r, n,
                        ),
                    )
                    self._subnet_eye_buttons[rail][net] = subnet_eye
                    key = (rail, net)
                    if preserve_visibility and key in saved_subnets:
                        vis = saved_subnets[key]
                    elif preserve_visibility and rail in saved_rails:
                        vis = saved_rails[rail]
                    else:
                        vis = self._default_rail_visible(rail)
                    subnet_eye.setVisibleState(vis, emit=False)
                self._sync_rail_eye_from_subnets(rail)
                self._sync_rail_tree_node_partials(rail)
            else:
                if preserve_visibility and rail in saved_rails:
                    vis = saved_rails[rail]
                else:
                    vis = self._default_rail_visible(rail)
                eye.setVisibleState(vis, emit=False)

            if has_subnets and expanded:
                self._insert_subnet_rows(rail, after_item=item)

        self._sync_all_rails_eye()
        self._update_rail_list_height()

    def _insert_subnet_rows(
        self, rail: str, *, after_item: QListWidgetItem, pending: bool = False,
    ) -> None:
        """Insert indented subnet rows for the currently expanded tree nodes.

        Serves both rail panes. The solved pane shows the rail's live per-net
        eyes; the pending pane shows a throwaway disabled eye on a muted
        italic row. Everything else — the tree walk, the indent, the per-node
        expand buttons, the item bookkeeping — is identical, and the two
        copies this replaces had already drifted apart.
        """
        items_by_rail = (
            self._pending_rail_subnet_items if pending
            else self._rail_subnet_items
        )
        if items_by_rail.get(rail):
            return
        node_expanded = (
            self._pending_subnet_node_expanded if pending
            else self._subnet_node_expanded
        )
        expand_buttons = (
            self._pending_subnet_expand_buttons if pending
            else self._subnet_expand_buttons
        )
        members = (
            self._pending_rails.get(rail, [rail]) if pending
            else self._rail_to_members.get(rail, [rail])
        )
        trees = getattr(
            self, "_pending_rail_trees" if pending else "_rail_to_trees", {},
        )
        subnets = {} if pending else self._subnet_eye_buttons.get(rail, {})
        muted_qss = (
            f"color: {_T()['fg_muted']}; font-style: italic;" if pending
            else ""
        )
        items: list[QListWidgetItem] = []
        row_idx = self.rail_list.row(after_item) + 1
        indent_px = self._RAIL_SUBNET_INDENT_PX
        for net, depth, has_children in self._subnet_rows_for_rail(
            rail, members, trees, node_expanded=node_expanded,
        ):
            if pending:
                eye = EyeButton(visible=False)
                eye.setEnabled(False)
            else:
                eye = subnets.get(net)
                if eye is None or not _qt_widget_alive(eye):
                    continue
            is_primary = net == rail
            expand_btn = None
            if has_children:
                key = (rail, net)
                node_exp = node_expanded.get(key, is_primary)
                node_expanded[key] = node_exp
                expand_btn = self._make_expand_tool_button(
                    expanded=node_exp,
                    tip=f"Show/hide children of {net}",
                    on_toggled=lambda exp, r=rail, n=net, p=pending: (
                        self._on_subnet_node_expand_toggled(r, n, exp, pending=p)
                    ),
                )
                expand_buttons[key] = expand_btn
            subnet_row = self._build_rail_row_widget(
                eye,
                expand_btn=expand_btn,
                label_text=net,
                bold=is_primary,
                indent=indent_px * depth,
            )
            if muted_qss:
                subnet_row.setStyleSheet(muted_qss)
            if pending:
                tip = (
                    f"Unsolved subnet of rail {rail} — press Resolve to "
                    f"compute it."
                )
            elif is_primary:
                tip = f"Primary net of rail {rail}"
            else:
                tip = f"Subnet of rail {rail} — joined via a SERIES bridge"
            subnet_row.setToolTip(tip)
            item = QListWidgetItem()
            item.setFlags(Qt.ItemIsEnabled)
            self.rail_list.insertItem(row_idx, item)
            item.setSizeHint(subnet_row.sizeHint())
            self.rail_list.setItemWidget(item, subnet_row)
            items.append(item)
            row_idx += 1
        items_by_rail[rail] = items

    def _remove_subnet_rows(self, rail: str, *, pending: bool = False) -> None:
        if not pending:
            # Solved rows borrow the rail's persistent per-net eyes and must
            # hand them back before the row widgets go; pending rows own
            # throwaway ones that die with the row.
            self._detach_subnet_eyes(rail)
        expand_buttons = (
            self._pending_subnet_expand_buttons if pending
            else self._subnet_expand_buttons
        )
        items_by_rail = (
            self._pending_rail_subnet_items if pending
            else self._rail_subnet_items
        )
        for key in [k for k in list(expand_buttons) if k[0] == rail]:
            btn = expand_buttons.pop(key)
            if _qt_widget_alive(btn):
                btn.blockSignals(True)
        for item in items_by_rail.pop(rail, []):
            row = self.rail_list.row(item)
            if row >= 0:
                self.rail_list.takeItem(row)

    def _update_rail_list_height(self) -> None:
        if not hasattr(self, "rail_list"):
            return
        approx = self.rail_list.sizeHintForRow(0) or 22
        self.rail_list.setFixedHeight(self.rail_list.count() * approx + 6)

    def _fan_out_rail_eye_to_subnets(self, rail: str, on: bool) -> None:
        for eye in self._subnet_eye_buttons.get(rail, {}).values():
            if _qt_widget_alive(eye):
                eye.setVisibleState(on, partial=False, emit=False)

    def _subnet_tree_for_rail(self, rail: str):
        trees = getattr(self, "_rail_to_trees", {}) or {}
        return trees.get(rail)

    def _fan_out_subnet_subtree(
        self, rail: str, net: str, on: bool,
    ) -> None:
        """Set visibility for ``net`` and all SERIES descendants."""
        from fypa.rail_groups import subtree_net_names

        tree = self._subnet_tree_for_rail(rail)
        names = subtree_net_names(tree, net)
        if not names:
            names = [net]
        subnets = self._subnet_eye_buttons.get(rail, {})
        for name in names:
            eye = subnets.get(name)
            if eye is not None and _qt_widget_alive(eye):
                eye.setVisibleState(on, partial=False, emit=False)

    def _sync_rail_tree_node_partials(self, rail: str) -> None:
        """Update node partial badges from subtree visibility.

        Does **not** change each node's own on/off (that is copper for that
        net). The badge means "my state does not describe this whole branch"
        — some descendant differs from this node — and is information only:
        a plain click still toggles this net, Ctrl+Click still owns the
        branch. See :func:`rail_tree_node_partial_flags`.
        """
        from fypa.rail_groups import rail_tree_node_partial_flags

        tree = self._subnet_tree_for_rail(rail)
        if tree is None:
            return
        subnets = self._subnet_eye_buttons.get(rail, {})
        if not subnets:
            return
        visible = {
            name: eye.isVisibleState()
            for name, eye in subnets.items()
            if _qt_widget_alive(eye)
        }
        for name, partial in rail_tree_node_partial_flags(tree, visible).items():
            eye = subnets.get(name)
            if eye is None or not _qt_widget_alive(eye):
                continue
            eye.setVisibleState(
                eye.isVisibleState(),
                partial=partial,
                emit=False,
            )

    def _sync_rail_eye_from_subnets(self, rail: str) -> None:
        subnets = self._subnet_eye_buttons.get(rail, {})
        if not subnets:
            return
        alive = [
            eye for eye in subnets.values()
            if _qt_widget_alive(eye)
        ]
        if not alive:
            return
        on_count = sum(eye.isVisibleState() for eye in alive)
        all_on = on_count == len(alive)
        all_off = on_count == 0
        partial = not all_on and not all_off
        for name, eye in self._rail_eye_buttons:
            if name == rail and _qt_widget_alive(eye):
                eye.setVisibleState(
                    not all_off, partial=partial, emit=False,
                )
                break

    def _refresh_rail_visibility_after_subnet_change(self, rail: str) -> None:
        self._sync_rail_tree_node_partials(rail)
        self._sync_rail_eye_from_subnets(rail)
        self._sync_all_rails_eye()
        self._sync_rail_only_visibility()
        self._render_with_busy_popup()

    def _on_rail_expand_toggled(self, rail: str, expanded: bool) -> None:
        self._rail_expanded[rail] = expanded
        btn = self._rail_expand_buttons.get(rail)
        if btn is not None and _qt_widget_alive(btn):
            btn.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        parent_item = self._rail_list_items.get(rail)
        if parent_item is None:
            return
        if expanded:
            self._insert_subnet_rows(rail, after_item=parent_item)
        else:
            self._remove_subnet_rows(rail)
        self._update_rail_list_height()

    def _on_subnet_node_expand_toggled(
        self, rail: str, net: str, expanded: bool, *, pending: bool = False,
    ) -> None:
        node_expanded = (
            self._pending_subnet_node_expanded if pending
            else self._subnet_node_expanded
        )
        expand_buttons = (
            self._pending_subnet_expand_buttons if pending
            else self._subnet_expand_buttons
        )
        node_expanded[(rail, net)] = expanded
        btn = expand_buttons.get((rail, net))
        if btn is not None and _qt_widget_alive(btn):
            btn.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        # Defer the rebuild: this slot runs on the expand button's toggled
        # signal, and rebuilding destroys that button.
        QTimer.singleShot(
            0, lambda r=rail, p=pending: self._rebuild_subnet_rows(
                r, pending=p,
            ),
        )

    def _rebuild_subnet_rows(self, rail: str, *, pending: bool = False) -> None:
        # Deferred by one event-loop turn (see the caller), so the list may
        # already have been torn down by a viewer retire in between — the
        # Python-side item dicts outlive the C++ widget.
        if not _qt_widget_alive(getattr(self, "rail_list", None)):
            return
        list_items = (
            self._pending_rail_list_items if pending else self._rail_list_items
        )
        expanded = (
            self._pending_rail_expanded if pending else self._rail_expanded
        )
        parent_item = list_items.get(rail)
        if parent_item is None or not expanded.get(rail):
            return
        self._remove_subnet_rows(rail, pending=pending)
        self._insert_subnet_rows(rail, after_item=parent_item, pending=pending)
        self._update_rail_list_height()

    def _on_subnet_eye_toggled(self, rail: str, net: str, _on: bool) -> None:
        eye = self._subnet_eye_buttons.get(rail, {}).get(net)
        if eye is None or not _qt_widget_alive(eye):
            return
        self._refresh_rail_visibility_after_subnet_change(rail)

    def _on_subnet_eye_ctrl_clicked(self, rail: str, net: str) -> None:
        """Ctrl+Click: apply this net's own toggle to its SERIES subtree.

        The target mirrors what a plain click on this node would do — visible
        → hide the branch, hidden → show it — so the gesture round-trips. The
        alternative ("anything mixed → show all") cannot hide a partly-hidden
        branch at all without first rendering every net in it.
        """
        from fypa.rail_groups import subtree_toggle_target

        eye = self._subnet_eye_buttons.get(rail, {}).get(net)
        if eye is None or not _qt_widget_alive(eye):
            return
        self._fan_out_subnet_subtree(
            rail, net, subtree_toggle_target(eye.isVisibleState()),
        )
        self._refresh_rail_visibility_after_subnet_change(rail)

    def _add_layer_row(self, phys: str) -> tuple[EyeButton, EyeButton]:
        """Build, wire and append one physical-layer row. Returns (eye, eye2).

        Shared by ``_build_ui`` and ``_rebuild_layer_rail_lists`` — the two
        used to carry byte-identical copies 400 lines apart, and had already
        drifted over the default-copper-visibility fallback.
        """
        eye = EyeButton(
            visible=True,
            tip_show="Show this layer's analysed rails (rail copper only)",
            tip_hide="Hide this layer's analysed rails (rail copper only)",
            shift_isolatable=True,
        )
        eye.toggled_visible.connect(self._on_layer_eye_toggled)
        eye.shift_clicked.connect(
            lambda p=phys: self._on_layer_eye_shift_clicked(p),
        )
        eye2 = EyeButton(
            visible=False,
            tip_show="Show all copper on this layer",
            tip_hide="Hide all copper on this layer",
            shift_isolatable=True,
        )
        eye2.toggled_visible.connect(self._on_layer_eye2_toggled)
        eye2.shift_clicked.connect(
            lambda p=phys: self._on_layer_eye2_shift_clicked(p),
        )
        fill = FillToggleButton(solid=True)
        fill.toggled_fill.connect(self._on_layer_fill_toggled)
        transp = TransparencyButton(step=0)
        transp.toggled_transparency.connect(
            self._on_layer_transparency_toggled,
        )
        row = self._build_layer_row_widget(
            eye, swatch_color=self._layer_color_for(phys),
            label_text=phys, bold=False,
            second_eye=eye2, fill_btn=fill, transparency_btn=transp,
        )
        item = QListWidgetItem()
        item.setFlags(Qt.ItemIsEnabled)
        self.layer_list.addItem(item)
        item.setSizeHint(row.sizeHint())
        self.layer_list.setItemWidget(item, row)
        self._layer_eye_buttons.append((phys, eye))
        self._layer_eye2_buttons.append((phys, eye2))
        self._layer_fill_buttons.append((phys, fill))
        self._layer_transparency_buttons.append((phys, transp))
        self._layer_list_items[phys] = item
        return eye, eye2

    def _ensure_default_copper_visibility(self) -> None:
        """Keep the viewport non-empty on a board with no analysed rails.

        A Gerber import before any directives are placed — or an unsolved stub
        loaded without auto-solve — starts with every eye closed, so turn on
        the top physical layer's all-copper eye. Only when nothing else is
        already showing, so a restored session keeps its own choice.
        """
        if not (self._no_pdn_visibility() and self._layer_eye2_buttons):
            return
        if any(eye.isVisibleState() for _, eye in self._layer_eye2_buttons):
            return
        self._layer_eye2_buttons[0][1].setVisibleState(True, emit=False)

    def _apply_isolate_or_invert(
        self,
        entries: list[tuple[object, EyeButton]],
        targets: set,
        isolate_key: object,
    ) -> bool:
        """Show only *targets*, or invert when this same click already did.

        ``entries`` pairs an arbitrary hashable key with its eye; ``targets``
        is the subset of keys the clicked item covers (one entry for a layer or
        subnet, the whole group for a bridged rail). ``isolate_key`` identifies
        the click for the invert-on-repeat check.

        Returns True when the visibility state was changed.
        """
        alive = [(key, eye) for key, eye in entries if _qt_widget_alive(eye)]
        if not alive:
            return False
        target_keys = {key for key, _eye in alive if key in targets}
        if not target_keys:
            # The clicked item has no live eye. Isolating on it would switch
            # every other eye off and leave the viewport blank.
            return False
        visible = {key for key, eye in alive if eye.isVisibleState()}
        # Invert only on a repeat of the click that isolated, and only while
        # that isolation still holds — a manual toggle in between means the
        # user is narrowing down again, not asking for the complement.
        invert = visible == target_keys and self._isolated_key == isolate_key
        if invert and len(target_keys) == len(alive):
            # Nothing to swap to; inverting would hide everything.
            return False
        for key, eye in alive:
            eye.setVisibleState(
                (key in targets) != invert, partial=False, emit=False,
            )
        self._isolated_key = None if invert else isolate_key
        return True

    def _iter_rail_visibility_entries(
        self,
    ) -> list[tuple[str, str | None, EyeButton]]:
        """Return (rail, net_or_none, eye) for every isolatable rail/subnet eye."""
        entries: list[tuple[str, str | None, EyeButton]] = []
        for rail, eye in self._rail_eye_buttons:
            subnets = [
                (net, seye)
                for net, seye in self._subnet_eye_buttons.get(rail, {}).items()
                if _qt_widget_alive(seye)
            ]
            if subnets:
                entries.extend((rail, net, seye) for net, seye in subnets)
            elif _qt_widget_alive(eye):
                # Aliveness is checked per eye, not per rail: a rail whose
                # subnet eyes were destroyed with their row still has a live
                # parent eye, and dropping it from the list would let everyone
                # else's isolate switch it off with no way back.
                entries.append((rail, None, eye))
        return entries

    def _after_rail_visibility_change(self) -> None:
        """Re-sync the rail eye hierarchy after a bulk visibility change."""
        for rail in self._subnet_eye_buttons:
            self._sync_rail_eye_from_subnets(rail)
        self._sync_all_rails_eye()
        self._sync_rail_only_visibility()

    def _apply_rail_group_isolate_or_invert(self, rail: str) -> bool:
        """Isolate a rail (all its subnets) or invert when already sole group."""
        entries = self._iter_rail_visibility_entries()
        targets = {(r, n) for r, n, _eye in entries if r == rail}
        return self._apply_isolate_or_invert(
            [((r, n), eye) for r, n, eye in entries],
            targets,
            ("rail", rail),
        )

    def _apply_rail_entry_isolate_or_invert(
        self, rail: str, net: str | None,
    ) -> bool:
        """Isolate one rail or subnet entry, or invert when already sole visible."""
        entries = self._iter_rail_visibility_entries()
        return self._apply_isolate_or_invert(
            [((r, n), eye) for r, n, eye in entries],
            {(rail, net)},
            ("entry", rail, net),
        )

    def _apply_eye_isolate_or_invert(
        self,
        items: list[tuple[str, EyeButton]],
        target: str,
        *,
        kind: str = "layer",
    ) -> bool:
        """Show only *target*, or invert on a repeat click."""
        return self._apply_isolate_or_invert(
            list(items), {target}, (kind, target),
        )

    def _on_layer_eye_shift_clicked(self, phys: str) -> None:
        if not self._apply_eye_isolate_or_invert(
            self._layer_eye_buttons, phys, kind="layer",
        ):
            return
        self._sync_all_layers_eye()
        self._on_layer_visibility_changed()

    def _on_layer_eye2_shift_clicked(self, phys: str) -> None:
        if not self._apply_eye_isolate_or_invert(
            self._layer_eye2_buttons, phys, kind="layer2",
        ):
            return
        self._sync_all_layers_eye2()
        rails = self._visible_rails()
        self._run_with_busy_popup(
            lambda: self._refresh_after_copper_eye(rails))

    def _on_rail_eye_shift_clicked(self, rail: str) -> None:
        # Branch on the same map _iter_rail_visibility_entries builds from.
        # _rail_to_members is maintained separately (_init_solution_indices),
        # and if the two disagree the isolate matches no entry at all and
        # switches every eye off.
        if self._subnet_eye_buttons.get(rail):
            changed = self._apply_rail_group_isolate_or_invert(rail)
        else:
            changed = self._apply_rail_entry_isolate_or_invert(rail, None)
        if changed:
            self._after_rail_visibility_change()
            self._render_with_busy_popup()

    def _on_subnet_eye_shift_clicked(self, rail: str, net: str) -> None:
        if self._apply_rail_entry_isolate_or_invert(rail, net):
            self._after_rail_visibility_change()
            self._render_with_busy_popup()

    def _on_layer_eye_toggled(self, _on: bool) -> None:
        """An individual layer's eye was clicked."""
        self._sync_all_layers_eye()
        self._on_layer_visibility_changed()

    def _on_all_layers_toggled(self, on: bool) -> None:
        """The "All Layers" eye was clicked — show or hide every layer."""
        for _name, eye in self._layer_eye_buttons:
            eye.setVisibleState(on, emit=False)
        self._on_layer_visibility_changed()

    def _sync_all_layers_eye(self) -> None:
        """Reflect "any layer visible" in the All Layers eye state."""
        any_visible = any(eye.isVisibleState()
                          for _n, eye in self._layer_eye_buttons)
        self._all_layers_eye.setVisibleState(any_visible, emit=False)

    def _set_layer_visible(self, name: str, on: bool, *, emit: bool = True) -> None:
        """Programmatically toggle a named layer's visibility."""
        for nm, eye in self._layer_eye_buttons:
            if nm == name:
                eye.setVisibleState(on, emit=emit)
                break
        self._sync_all_layers_eye()

    def _on_layer_visibility_changed(self, _item: object = None) -> None:
        """Layer eye toggled in the layer list → re-render."""
        self._render_with_busy_popup()

    def _visible_layers(self) -> list[str]:
        """Names of the physical layers currently visible (eye open),
        in stackup order (top first)."""
        return [name for name, eye in self._layer_eye_buttons
                if eye.isVisibleState()]

    # --- Selected (active) physical layer ----------------------------------

    def _on_layer_item_clicked(self, item: QListWidgetItem) -> None:
        """A click on a Physical layers row that landed outside the row's
        embedded buttons. Toggle that layer as the selected layer; clicking
        the already-selected layer clears the selection."""
        clicked: str | None = None
        for phys, it in self._layer_list_items.items():
            if it is item:
                clicked = phys
                break
        if clicked is None:
            return  # "All Layers" header row or an unmapped item
        self._selected_layer = None if self._selected_layer == clicked else clicked
        self._apply_layer_selection_highlight()
        self._render_with_busy_popup()

    def _apply_layer_selection_highlight(self) -> None:
        """Repaint each Physical layers row to reflect the selected layer:
        the selected row gets the configured selection background, all
        others revert to no styled background. The ``#layerRow`` selector
        scopes the rule to the row widget itself so child labels stay
        transparent and inherit the highlight."""
        sel = self._selected_layer
        bg = self._SELECTED_LAYER_BG or _T()["bg_selection"]
        for phys, item in self._layer_list_items.items():
            w = self.layer_list.itemWidget(item)
            if w is None:
                continue
            if phys == sel:
                w.setStyleSheet(
                    f"QWidget#layerRow {{ background-color: {bg}; }}")
            else:
                w.setStyleSheet("")

    def _selected_layer_dim_rgb(
        self, rgb: tuple[float, float, float],
    ) -> tuple[float, float, float]:
        """Blend an unselected layer's swatch RGB toward the dim target
        when a layer is selected. No-op when no layer is selected."""
        f = max(0.0, min(1.0, float(self._SELECTED_LAYER_DIM_FACTOR)))
        if f <= 0.0:
            return rgb
        tr, tg, tb = self._SELECTED_LAYER_DIM_RGB
        return (
            (1.0 - f) * rgb[0] + f * tr,
            (1.0 - f) * rgb[1] + f * tg,
            (1.0 - f) * rgb[2] + f * tb,
        )

    # --- Physical-layer "all copper" (second eye + fill toggle) -------------

    def _on_layer_eye2_toggled(self, _on: bool) -> None:
        """A layer's 'all copper' eye was clicked. The all-copper view is
        independent of the FEM heatmap, so only the overlay geometry +
        via overlays need rebuilding — no full re-render. Via markers
        (2D) and via cylinders (3D) gate on the second eye too (see
        :meth:`_push_via_cylinders`), so they refresh here as well."""
        self._sync_all_layers_eye2()
        rails = self._visible_rails()
        self._run_with_busy_popup(
            lambda: self._refresh_after_copper_eye(rails))

    def _on_all_layers_eye2_toggled(self, on: bool) -> None:
        """The "All Layers" all-copper eye — show/hide all copper on every
        layer at once."""
        for _name, eye2 in self._layer_eye2_buttons:
            eye2.setVisibleState(on, emit=False)
        rails = self._visible_rails()
        self._run_with_busy_popup(
            lambda: self._refresh_after_copper_eye(rails))

    def _refresh_after_copper_eye(self, rails) -> None:
        """Refresh the overlays plus the via markers / cylinders that
        depend on second-eye visibility. Keeps the FEM heatmap untouched
        (the all-copper eye doesn't change which solution is solved)."""
        self._refresh_overlay_geometry(rails)
        phys_list = self._visible_layers()
        self._update_markers_and_legend(phys_list, rails)
        if self.view_3d_box.isChecked():
            mode = self.mode_combo.currentText()
            self._push_via_cylinders(phys_list, rails, mode=mode)

    def _sync_all_layers_eye2(self) -> None:
        """Reflect "any layer's all-copper shown" in the All Layers eye."""
        any_on = any(eye2.isVisibleState()
                     for _n, eye2 in self._layer_eye2_buttons)
        self._all_layers_eye2.setVisibleState(any_on, emit=False)

    def _on_layer_fill_toggled(self, _solid: bool) -> None:
        """A layer's all-copper wire-mesh / solid fill toggle flipped."""
        # Route through the busy-popup guard so a toggle landing inside another
        # pumping refresh can't interleave two overlay builds reading
        # half-old/half-new GL state (see :meth:`_run_with_busy_popup`).
        self._run_with_busy_popup(
            lambda: self._refresh_overlay_geometry(self._visible_rails()))

    def _on_all_layers_fill_toggled(self, solid: bool) -> None:
        """The "All Layers" fill toggle — set every layer's all-copper fill
        style at once."""
        for _name, fill in self._layer_fill_buttons:
            fill.setSolid(solid, emit=False)
        self._run_with_busy_popup(
            lambda: self._refresh_overlay_geometry(self._visible_rails()))

    def _on_layer_transparency_toggled(self, _step: int) -> None:
        """A layer's transparency was cycled. Fades both the heatmap mesh
        (per-vertex alpha) and the all-copper overlay for that layer."""
        def _work() -> None:
            self._push_mesh_alpha()
            self._refresh_overlay_geometry(self._visible_rails())
        self._run_with_busy_popup(_work)

    def _on_all_layers_transparency_toggled(self, step: int) -> None:
        """The "All Layers" transparency cycler — fan out to every per-
        layer button. Updates the mesh alpha + all-copper overlay once
        after the fan-out so the GPU only sees a single re-push."""
        for _name, transp in self._layer_transparency_buttons:
            transp.setStep(step, emit=False)
        def _work() -> None:
            self._push_mesh_alpha()
            self._refresh_overlay_geometry(self._visible_rails())
        self._run_with_busy_popup(_work)

    def _on_rail_eye_toggled(self, rail: str, on: bool) -> None:
        """An individual rail's eye was clicked."""
        if not any(
            name == rail and _qt_widget_alive(eye)
            for name, eye in self._rail_eye_buttons
        ):
            return
        self._fan_out_rail_eye_to_subnets(rail, on)
        self._refresh_rail_visibility_after_subnet_change(rail)

    def _on_all_rails_toggled(self, on: bool) -> None:
        """The "All Rails" eye was clicked — show or hide every rail."""
        for name, eye in self._rail_eye_buttons:
            if _qt_widget_alive(eye):
                eye.setVisibleState(on, partial=False, emit=False)
            self._fan_out_rail_eye_to_subnets(name, on)
            self._sync_rail_tree_node_partials(name)
        self._sync_all_rails_eye()
        self._sync_rail_only_visibility()
        self._render_with_busy_popup()

    def _sync_all_rails_eye(self) -> None:
        """Reflect "any rail visible" in the All Rails eye state."""
        any_visible = False
        any_hidden = False
        for _n, eye in self._rail_eye_buttons:
            if not _qt_widget_alive(eye):
                continue
            if eye.isVisibleState():
                any_visible = True
            else:
                any_hidden = True
        partial = any_visible and any_hidden
        if _qt_widget_alive(self._all_rails_eye):
            self._all_rails_eye.setVisibleState(
                any_visible, partial=partial, emit=False,
            )

    def _visible_rails(self) -> list[str]:
        """Names of the rails currently visible (eye open), in the
        sort order they were registered (matches the rail list UI)."""
        out: list[str] = []
        for name, eye in self._rail_eye_buttons:
            subnets = self._subnet_eye_buttons.get(name)
            if subnets:
                if any(
                    e.isVisibleState()
                    for e in subnets.values()
                    if _qt_widget_alive(e)
                ):
                    out.append(name)
            elif _qt_widget_alive(eye) and eye.isVisibleState():
                out.append(name)
        return out

    def _rail_only_meaningful(self) -> bool:
        """True iff any currently-visible rail has SERIES-bridged sibling
        nets — i.e. the "Show only rail net" filter would actually hide
        something. When no visible rail has siblings, toggling the box
        changes nothing, so the control is hidden by
        :meth:`_sync_rail_only_visibility`."""
        for name in self._visible_rails():
            if len(self._rail_to_members.get(name, [name])) > 1:
                return True
        return False

    def _sync_rail_only_visibility(self) -> None:
        """Show / hide the "Show only rail net" checkbox so it only appears
        when at least one visible rail is bridged to siblings via a
        RESISTOR / SERIES directive. The box's checked state is preserved
        across hides, so toggling a bridged rail back on restores the
        previous filter."""
        box = getattr(self, "rail_only_box", None)
        if box is None:
            return
        box.setVisible(self._rail_only_meaningful())
