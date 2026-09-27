"""Editor-mode net focus and the net inventory table."""
from __future__ import annotations

import logging
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QTableWidgetItem

from fypa.viewer.tabs.messages import _MessagesSortItem
from fypa.viewer.widgets import _esc, _focus_pixmap


# Item-data role carrying the net table's per-row payload ({name, area,
# poly_keys, ...}) on the row's first cell. Stashing it on the item — rather
# than indexing a parallel list by visual row — keeps click / rename handlers
# correct after the user re-sorts the table by clicking a column header.
_NET_TABLE_ROW_ROLE = int(Qt.UserRole) + 1


class _EditorNetsMixin:
    """Editor-mode net focus and the net inventory table."""

    # --- Editor mode: net focus ("show only this net") ----------------------

    # Class-level "no focus held" defaults, so the render / panel paths that
    # read them stay safe on a viewer built with ``__new__`` (the test stubs,
    # which skip ``__init__``). Frozen, because every write replaces the set
    # rather than mutating it — an in-place mutation of a shared class
    # attribute would leak one viewer's focus into the next, so make it fail
    # loudly instead.
    _editor_focus_net: str | None = None
    _editor_focus_nets: frozenset | set = frozenset()
    _editor_focus_polys: frozenset | set = frozenset()

    def _editor_focus_active(self) -> bool:
        """True while a net focus is held — i.e. copper outside the focused
        net is being suppressed. Requires editor mode: focus is an editor
        control and must not leak into the viewer-mode heatmap."""
        return bool(
            self._editor_mode
            and (self._editor_focus_nets or self._editor_focus_polys)
        )

    def _editor_focus_hides(self, net: str | None,
                            layer_id: int | None = None,
                            poly: dict | None = None) -> bool:
        """Whether net focus suppresses this piece of copper entirely.

        The counterpart of :meth:`_editor_dim_rgb`'s dim test, but a hard
        skip rather than a colour blend — focus means the other nets are
        gone, not faded. ``net`` alone settles it for real nets; unnamed /
        synthetic copper shares the ``"(none)"`` sentinel, so the
        polygon-identity set is what keeps the focused region drawn while
        disjoint unnamed pieces disappear.
        """
        if not self._editor_focus_active():
            return False
        if net and net in self._editor_focus_nets:
            return False
        if (poly is not None and layer_id is not None
                and (int(layer_id), id(poly)) in self._editor_focus_polys):
            return False
        return True

    def _focus_blocks_pick(self, net: str | None,
                           world_x: float | None = None,
                           world_y: float | None = None,
                           layer_id: int | None = None) -> bool:
        """Whether a click at this point should be treated as landing on bare
        substrate because net focus has hidden the copper there.

        Losing a hard-won view to a stray click is the problem focus exists to
        solve, so a click that resolves to copper the user can no longer see
        must not select it. For the ``"(none)"`` sentinel the net name proves
        nothing, so the polygon under the cursor is resolved and tested by
        identity; without a point to resolve, an unnamed pick is let through
        rather than silently swallowed."""
        if not self._editor_focus_active():
            return False
        if net and net != "(none)":
            return net not in self._editor_focus_nets
        if world_x is None or world_y is None or layer_id is None:
            return False
        poly = self._copper_poly_under_point(
            float(world_x), float(world_y), int(layer_id))
        if poly is None:
            return False
        return (int(layer_id), id(poly)) not in self._editor_focus_polys

    def _focus_row_is_focused(self, row: dict) -> bool:
        """Whether ``row`` (a :meth:`_compute_net_table_rows` payload) is the
        one currently focused. Matched on name so the flag survives the table
        rebuild a rename triggers."""
        return (self._editor_focus_net is not None
                and row.get("name") == self._editor_focus_net)

    def _set_editor_focus(self, row: dict | None) -> None:
        """Focus the net described by ``row`` — from here on only its copper
        draws — or release the focus when ``row`` is ``None``. Passing the
        already-focused row toggles it off, so the crosshair is its own
        release.

        Selection is deliberately left alone: the crosshair lives in the net
        table, and the table is the panel's idle view (see
        :meth:`_update_editor_panel`), so selecting the row from here would
        hide the very control the user needs to click again.
        """
        if row is None or self._focus_row_is_focused(row):
            self._clear_editor_focus()
            return
        name = row.get("name") or ""
        # A real net focuses by name so SERIES-bridged copper comes along;
        # synthetic / unnamed copper has no usable name and focuses by
        # polygon identity alone.
        nets = self._connected_nets(name) if row.get("real") else set()
        self._editor_focus_net = name
        self._editor_focus_nets = set(nets)
        self._editor_focus_polys = set(row.get("poly_keys") or ())
        self._sync_net_focus_column()
        self._update_editor_focus_chip()
        self._render()
        self.statusBar().showMessage(
            f"Focused on {name}. Click the crosshair again (or press F) "
            "to show everything.", 6000)

    def _clear_editor_focus(self, *, render: bool = True) -> None:
        """Release the net focus; all copper comes back."""
        had = bool(self._editor_focus_nets or self._editor_focus_polys
                   or self._editor_focus_net)
        self._editor_focus_net = None
        self._editor_focus_nets = set()
        self._editor_focus_polys = set()
        if not had:
            return
        self._sync_net_focus_column()
        self._update_editor_focus_chip()
        if render:
            self._render()

    def _hotkey_toggle_net_focus(self) -> None:
        """F — release an active net focus, or focus the net the current
        editor selection sits on. A no-op outside editor mode, and on a
        selection with no single net to focus."""
        if not self._editor_mode:
            return
        if self._editor_focus_net:
            self._clear_editor_focus()
            self.statusBar().showMessage("Net focus released.", 3000)
            return
        row = self._focus_row_for_selection()
        if row is None:
            self.statusBar().showMessage(
                "Select a net (or a component / marker on one) first, then "
                "press F to show only that net's copper.", 4000)
            return
        self._set_editor_focus(row)

    def _focus_row_for_selection(self) -> dict | None:
        """The net-table row the current editor selection implies, or ``None``
        when the selection names no single net. Lets the F hotkey focus what
        is already selected instead of making the user find the row."""
        sel = self._editor_selection
        name: str | None = None
        if sel:
            kind = sel.get("kind")
            if kind == "copper":
                name = sel.get("net")
            elif kind == "free":
                d = (self._project.directive_by_id(sel.get("id") or "")
                     if self._project is not None else None)
                name = getattr(d, "p_net", None) if d is not None else None
            elif kind == "component":
                nets = {n for n in (sel.get("nets") or []) if n}
                # Only an unambiguous single-net component can focus; a part
                # bridging two rails has no one net to show.
                if len(nets) == 1:
                    name = next(iter(nets))
        if not name or name == "(none)":
            # Fall back to the highlighted table row — the user may have
            # clicked a row rather than the canvas.
            return self._selected_net_table_row()
        for r in getattr(self, "_net_table_rows", ()) or ():
            if r.get("name") == name:
                return r
        return None

    def _selected_net_table_row(self) -> dict | None:
        """The row payload for the net table's currently highlighted row, or
        ``None`` when no row is selected / the table isn't built."""
        table = getattr(self, "_net_table", None)
        if table is None:
            return None
        i = table.currentRow()
        if i < 0:
            return None
        item = table.item(i, self._NET_COL_NAME)
        return item.data(_NET_TABLE_ROW_ROLE) if item is not None else None

    def _sync_net_focus_column(self) -> None:
        """Repaint the net table's crosshair column from the focus state."""
        table = getattr(self, "_net_table", None)
        if table is None:
            return
        prev = self._net_table_populating
        self._net_table_populating = True
        try:
            for i in range(table.rowCount()):
                cell = table.item(i, self._NET_COL_FOCUS)
                name_item = table.item(i, self._NET_COL_NAME)
                if cell is None or name_item is None:
                    continue
                r = name_item.data(_NET_TABLE_ROW_ROLE) or {}
                self._paint_focus_cell(cell, self._focus_row_is_focused(r))
        finally:
            self._net_table_populating = prev

    def _paint_focus_cell(self, cell, on: bool) -> None:
        """Set one crosshair cell's icon, tooltip and sort key."""
        cell.setIcon(QIcon(_focus_pixmap(on)))
        # Sort key: a click on this column's header floats the focused net to
        # the top rather than shuffling rows by an empty display string.
        cell.setData(Qt.UserRole, 0 if on else 1)
        cell.setToolTip(
            "Showing only this net — click to show all copper again (F)"
            if on else
            "Show only this net's copper, hiding every other net until "
            "clicked again. Keyboard: F"
        )

    def _update_editor_focus_chip(self) -> None:
        """Sync the "focused on <net>" release chip.

        The chip covers exactly the state the net table cannot: a
        selection hides the table (see :meth:`_update_editor_panel`),
        taking the header's release link with it. While the table IS up
        the header carries the link and the chip stays down, so focusing
        never inserts a widget above the table and never shifts the rows
        under the cursor."""
        chip = getattr(self, "_editor_focus_chip", None)
        if chip is None:
            return
        self._update_net_table_label()
        table = getattr(self, "_net_table", None)
        table_up = table is not None and not table.isHidden()
        name = self._editor_focus_net
        show = bool(self._editor_mode and name and not table_up)
        if show:
            chip.setText(
                f"◉ Focused on <b>{_esc(name)}</b>"
                f" &nbsp;<a href='#release'>Show all</a>"
            )
        chip.setVisible(show)

    def _on_editor_focus_chip_link(self, _href: str) -> None:
        """"Show all" in the focus chip — release the focus."""
        self._clear_editor_focus()

    # --- Editor mode: net inventory table -----------------------------------

    # Net-table column order. The focus crosshair sits first, matching the
    # layer panel's eye-on-the-left convention, so the name column keeps the
    # stretchy slot it had when the table had two columns.
    _NET_COL_FOCUS = 0
    _NET_COL_NAME = 1
    _NET_COL_AREA = 2

    def _all_copper_poly_maps(self) -> tuple[dict, dict]:
        """``(shape_by_key, net_by_key)`` over every ``all_copper`` polygon,
        keyed by the same ``(layer_id, id(poly_dict))`` the connected-
        components index uses. ``shape_by_key`` re-uses the Shapely polygons
        already built for :meth:`_layer_strtrees` (no rebuild); ``net_by_key``
        carries each polygon's record-level net name (``"(none)"`` for
        unnamed copper)."""
        strt = self._layer_strtrees()
        shape_by_key: dict[tuple[int, int], object] = {}
        for lid, d in strt.items():
            for shp, poly in zip(d["shapes"], d["polys"]):
                shape_by_key[(int(lid), id(poly))] = shp
        net_by_key: dict[tuple[int, int], str | None] = {}
        for rec in (self.metadata or {}).get("all_copper") or []:
            lid = rec.get("layer_id")
            if lid is None:
                continue
            net = rec.get("net")
            for poly in rec.get("polygons", []):
                net_by_key[(int(lid), id(poly))] = net
        return shape_by_key, net_by_key

    def _copper_name_root_map(self, cc: dict | None = None) -> dict:
        """``{component_root: name}`` for every :class:`CopperName` rename,
        resolving each anchor to the connected-copper component it lands in.
        Lets the net table show a user-given (or auto-minted ``$NET_xxx``)
        name even before the next solve folds it into ``all_copper``."""
        if cc is None:
            cc = self._connected_components_data()
        component_of = cc["component_of"]
        out: dict[tuple[int, int], str] = {}
        if self._project is None:
            return out
        for c in self._project.copper_names:
            if not c.name:
                continue
            p = self._copper_poly_under_point(
                float(c.anchor_xy[0]), float(c.anchor_xy[1]), int(c.layer_id))
            if p is None:
                continue
            root = component_of.get((int(c.layer_id), id(p)))
            if root is not None:
                out[root] = c.name
        return out

    def _auto_name_isolated_regions(self) -> None:
        """Mint a stable ``$NET_xxx`` name for each electrically-isolated
        copper region that has no net. This is every region on a Gerber
        import (which carries no nets at all) and the no-net ``"(none)"``
        copper on an Altium board (pours / fills the schematic never
        assigned a net). Each region becomes one :class:`CopperName`
        anchored at an interior point of its largest polygon, so the name
        persists to the ``.fypa`` and survives re-solve / reload.

        The number is zero-padded to the width of the region count: 5
        regions → ``$NET_1``…``$NET_5``; 150 regions → ``$NET_001``…
        ``$NET_150``. Idempotent — a region already named (real net or an
        existing CopperName) is skipped, so re-entering editor mode is a
        no-op."""
        md = self.metadata or {}
        if not md.get("all_copper"):
            return
        from fypa.project_file import CopperName
        shape_by_key, net_by_key = self._all_copper_poly_maps()
        cc = self._connected_components_data()
        members_of = cc["members_of"]
        named_roots = set(self._copper_name_root_map(cc).keys())
        existing_names = {n for n in self._all_net_names()
                          if n and n != "(none)"}

        candidates: list[tuple[float, tuple, set]] = []
        for root, members in members_of.items():
            if root in named_roots:
                continue
            if any(net_by_key.get(k) not in (None, "(none)") for k in members):
                continue
            area = sum(shape_by_key[k].area
                       for k in members if k in shape_by_key)
            candidates.append((area, root, members))
        if not candidates:
            return
        # Largest region gets the lowest number, matching the table order.
        candidates.sort(key=lambda t: -t[0])
        width = max(len(str(len(candidates))), 1)
        proj = self._ensure_project()
        n = 0
        for _area, _root, members in candidates:
            best_key = max((k for k in members if k in shape_by_key),
                           key=lambda k: shape_by_key[k].area, default=None)
            if best_key is None:
                continue
            shp = shape_by_key[best_key]
            try:
                rp = shp.representative_point()
                ax, ay = float(rp.x), float(rp.y)
            except Exception:
                c = shp.centroid
                ax, ay = float(c.x), float(c.y)
            lid = int(best_key[0])
            n += 1
            name = f"$NET_{n:0{width}d}"
            while name in existing_names:
                n += 1
                name = f"$NET_{n:0{width}d}"
            proj.upsert_copper_name(
                CopperName(anchor_xy=(ax, ay), layer_id=lid, name=name))
            existing_names.add(name)
        self._all_net_names_cache = None
        self._mark_project_dirty()

    def _compute_net_table_rows(self) -> list[dict]:
        """Per-net rows for the editor net table — one entry per effective
        net name with its total copper area (mm²) summed across every
        connected polygon (both layers of a two-layer pour count). Each row
        carries the polygon-identity set used to drive the click highlight
        and a ``renameable`` flag (true for synthetic / user-named copper,
        false for a board's intrinsic nets). Ordered by area, descending."""
        md = self.metadata or {}
        if not md.get("all_copper"):
            return []
        shape_by_key, net_by_key = self._all_copper_poly_maps()
        cc = self._connected_components_data()
        members_of = cc["members_of"]
        name_by_root = self._copper_name_root_map(cc)

        agg: dict[str, dict] = {}
        for root, members in members_of.items():
            real_net = None
            for k in members:
                nn = net_by_key.get(k)
                if nn and nn != "(none)":
                    real_net = nn
                    break
            if real_net is not None:
                name, real = real_net, True
            else:
                nm = name_by_root.get(root)
                name, real = (nm if nm else "(none)"), False
            area = sum(shape_by_key[k].area
                       for k in members if k in shape_by_key)
            bucket = agg.setdefault(
                name, {"area": 0.0, "poly_keys": set(), "real": False})
            bucket["area"] += area
            bucket["poly_keys"].update(members)
            bucket["real"] = bucket["real"] or real

        rows = [
            {"name": nm, "area": d["area"], "poly_keys": d["poly_keys"],
             "real": d["real"],
             "renameable": (not d["real"]) and nm != "(none)"}
            for nm, d in agg.items()
        ]
        rows.sort(key=lambda r: -r["area"])
        return rows

    def _refresh_net_table(self) -> None:
        """Rebuild the editor net table from the current geometry + names."""
        table = getattr(self, "_net_table", None)
        if table is None:
            return
        try:
            rows = self._compute_net_table_rows()
        except Exception:
            logging.getLogger(__name__).exception("net table build failed")
            rows = []
        self._net_table_rows = rows
        # Remember the current sort so a rebuild (e.g. after a rename) keeps
        # the column / direction the user clicked, defaulting to area-desc.
        hdr = table.horizontalHeader()
        sort_col = hdr.sortIndicatorSection()
        sort_order = hdr.sortIndicatorOrder()
        if sort_col not in (self._NET_COL_FOCUS, self._NET_COL_NAME,
                            self._NET_COL_AREA):
            sort_col, sort_order = self._NET_COL_AREA, Qt.DescendingOrder
        self._net_table_populating = True
        try:
            # Sorting OFF while writing cells, else each setItem re-sorts the
            # half-built table and rows land in the wrong place.
            table.setSortingEnabled(False)
            table.clearContents()
            table.setRowCount(len(rows))
            for i, r in enumerate(rows):
                name_item = QTableWidgetItem(r["name"])
                flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable
                if r["renameable"]:
                    flags |= Qt.ItemIsEditable
                name_item.setFlags(flags)
                # Carry the row payload on the item so click / rename stay
                # correct after the user re-sorts by a header click.
                name_item.setData(_NET_TABLE_ROW_ROLE, r)
                table.setItem(i, self._NET_COL_NAME, name_item)
                # Focus crosshair. Enabled (so it takes clicks) but NOT
                # selectable: clicking it must not select the row, because
                # _update_editor_panel hides the whole table as soon as
                # anything is selected.
                focus_item = _MessagesSortItem("")
                focus_item.setFlags(Qt.ItemIsEnabled)
                focus_item.setTextAlignment(Qt.AlignCenter)
                self._paint_focus_cell(
                    focus_item, self._focus_row_is_focused(r))
                table.setItem(i, self._NET_COL_FOCUS, focus_item)
                area_item = _MessagesSortItem(f"{r['area']:,.2f}")
                area_item.setData(Qt.UserRole, float(r["area"]))
                area_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                area_item.setTextAlignment(
                    Qt.AlignRight | Qt.AlignVCenter)
                table.setItem(i, self._NET_COL_AREA, area_item)
            table.setSortingEnabled(True)
            table.sortItems(sort_col, sort_order)
        finally:
            self._net_table_populating = False
        # Re-apply the active name filter (and refresh the count label).
        # A rebuild resets per-row hidden state, so a live filter would
        # otherwise reveal the rows it was hiding.
        self._apply_net_table_filter()

    def _on_net_table_filter_changed(self, _text: str) -> None:
        """Slot for the net-table filter edit — re-run the row filter as
        the user types."""
        self._apply_net_table_filter()

    def _apply_net_table_filter(self) -> None:
        """Hide net rows whose name doesn't contain the filter text
        (case-insensitive substring), and update the count label to show
        how many rows match."""
        table = getattr(self, "_net_table", None)
        if table is None:
            return
        edit = getattr(self, "_net_table_filter", None)
        needle = edit.text().strip().lower() if edit is not None else ""
        visible = 0
        for i in range(table.rowCount()):
            item = table.item(i, self._NET_COL_NAME)
            name = item.text().lower() if item is not None else ""
            hide = bool(needle) and needle not in name
            table.setRowHidden(i, hide)
            if not hide:
                visible += 1
        self._update_net_table_label(visible=visible, filtered=bool(needle))

    def _update_net_table_label(self, *, visible: int | None = None,
                                filtered: bool | None = None) -> None:
        """Rewrite the net table's header line — the row count, plus the focus
        release link while a focus is held.

        The link lives here rather than in a banner of its own because the
        header is already present, at a fixed single-line height, immediately
        above the table. A banner above the table would instead push it down at
        the exact moment the user is reaching back to the crosshair to undo a
        mis-click, which is how they would end up focusing a second wrong net.
        """
        label = getattr(self, "_net_table_label", None)
        if label is None:
            return
        total = len(getattr(self, "_net_table_rows", ()))
        if visible is None or filtered is None:
            edit = getattr(self, "_net_table_filter", None)
            filtered = bool(edit is not None and edit.text().strip())
            visible = total
            table = getattr(self, "_net_table", None)
            if filtered and table is not None:
                visible = sum(not table.isRowHidden(i)
                              for i in range(table.rowCount()))
        head = (f"<b>Nets</b> ({visible} of {total})" if filtered
                else f"<b>Nets</b> ({total})")
        name = self._editor_focus_net
        if name:
            # Elided: the panel is a fixed 300 px and this label does not
            # wrap, so a long net name would push the release link off the
            # edge instead of growing the line.
            shown = name if len(name) <= 16 else name[:15] + "\u2026"
            head += (
                f" &nbsp;\u00b7&nbsp; <a href='#release'"
                f" title='Show all copper again'>"
                f"\u25c9 {_esc(shown)} \u2715</a>"
            )
        label.setText(head)

    def _on_net_table_selection(self) -> None:
        """Light up the clicked net's copper in the viewport (dim the rest),
        mirroring a copper click in the canvas."""
        if self._net_table_populating or not self._editor_mode:
            return
        table = getattr(self, "_net_table", None)
        if table is None:
            return
        row = table.currentRow()
        if row < 0:
            return
        name_item = table.item(row, self._NET_COL_NAME)
        r = name_item.data(_NET_TABLE_ROW_ROLE) if name_item else None
        if not r:
            return
        nets = self._connected_nets(r["name"]) if r["real"] else set()
        self._apply_editor_highlight(nets, polys=r["poly_keys"])

    def _on_net_table_cell_clicked(self, row: int, col: int) -> None:
        """Toggle net focus when the click landed in the crosshair column."""
        if col != self._NET_COL_FOCUS or not self._editor_mode:
            return
        table = getattr(self, "_net_table", None)
        if table is None:
            return
        name_item = table.item(row, self._NET_COL_NAME)
        r = name_item.data(_NET_TABLE_ROW_ROLE) if name_item else None
        if r:
            self._set_editor_focus(r)

    def _on_net_table_item_changed(self, item) -> None:
        """Commit an in-place net rename from the table. Only synthetic /
        user-named copper is editable; the new name is written back to the
        backing :class:`CopperName`(s) so it persists to the project."""
        if (self._net_table_populating or item is None
                or item.column() != self._NET_COL_NAME):
            return
        r = item.data(_NET_TABLE_ROW_ROLE)
        if not r:
            return
        old = r["name"]
        new = item.text().strip()
        if new == old:
            return

        def _revert() -> None:
            self._net_table_populating = True
            try:
                item.setText(old)
            finally:
                self._net_table_populating = False

        if not r["renameable"]:
            _revert()
            return
        if not new or new.lower() in {"none", "(none)"}:
            self.statusBar().showMessage("Enter a valid net name.", 4000)
            _revert()
            return
        if new in {n for n in self._all_net_names() if n != old}:
            self.statusBar().showMessage(
                f"'{new}' is already a net on this board — pick a "
                "different name.", 4000)
            _revert()
            return
        proj = self._ensure_project()
        renamed = 0
        for c in proj.copper_names:
            if c.name == old:
                c.name = new
                renamed += 1
        if not renamed:
            _revert()
            return
        self._all_net_names_cache = None
        self._mark_project_dirty()
        # Focus is tracked by display name; carry it over so a rename of the
        # focused net doesn't silently orphan the crosshair and the chip.
        if self._editor_focus_net == old:
            self._editor_focus_net = new
            self._update_editor_focus_chip()
        self._refresh_net_table()
        self.statusBar().showMessage(f"Renamed net to {new}.", 3000)
