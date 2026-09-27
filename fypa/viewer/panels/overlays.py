"""The Overlays control: silkscreen / mask / outline layers over the heatmap."""
from __future__ import annotations

import math
import matplotlib
import matplotlib.colors
import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QHBoxLayout, QLabel, QListWidgetItem, QWidget

from fypa.viewer.diagnostics import (
    _MESH_FAILURE_MARKER_RADIUS_MM,
    _mesh_failure_marker_xy,
    _MESH_FAILURE_RING_RADIUS_MM,
)
from fypa.viewer.overlays import (
    _overlay_box_ring,
    _overlay_circle_ring,
    _OVERLAY_DEFAULT_BOTTOM_COLORS,
    _OVERLAY_DEFAULT_COLORS,
    _overlay_default_solid,
    _overlay_fan_tris,
    _overlay_hole_ring,
    _OVERLAY_LAYERS,
    _overlay_outline_tris,
    _overlay_ribbon_outline_tris,
    _OVERLAY_WIRE_HALF_MM,
)
from fypa.viewer.theme import _T
from fypa.viewer.widgets import (
    _transparency_alpha,
    EyeButton,
    FillToggleButton,
    OverlayColorButton,
    SplitButton,
    TransparencyButton,
)


class _OverlayPanelMixin:
    """The Overlays control: silkscreen / mask / outline layers over the heatmap."""

    # --- Overlays control ----------------------------------------------------

    def _init_overlay_state(self) -> None:
        """Build the Overlays control's state dict — the source of truth
        that :meth:`_build_overlay_list` renders rows from.

        One entry per overlay layer. ``split`` says whether the layer is
        shown as one merged row or separate Top / Bottom rows; each row
        variant (``both`` / ``top`` / ``bottom``) tracks ``vis`` — one of
        ``None`` (hidden), ``"rails"`` (visible on the selected rails only)
        or ``"all"`` (visible everywhere) — and ``solid`` (the fill style).

        Draw colour lives separately. ``self._overlay_colors`` holds the
        primary colour per layer — used by a merged row and the Top side of
        a split one. ``self._overlay_bottom_colors`` holds the distinct
        colour for the layer's Bottom side (``None`` == follow the primary);
        it applies whether the layer is split or merged. Both are seeded
        from :data:`_OVERLAY_DEFAULT_COLORS` /
        :data:`_OVERLAY_DEFAULT_BOTTOM_COLORS` and then overridden by
        anything the open .fypa project saved.
        """
        self._overlay_state: dict[str, dict] = {}
        for key, _label, has_sides in _OVERLAY_LAYERS:
            solid0 = _overlay_default_solid(key)
            # ``alpha_step`` is the TransparencyButton's step count (0..4);
            # 0 == 0% transparent == fully opaque. New rows start opaque.
            st: dict = {
                "split": False,
                "both": {"vis": None, "solid": solid0, "alpha_step": 0},
            }
            if has_sides:
                st["top"] = {"vis": None, "solid": solid0, "alpha_step": 0}
                st["bottom"] = {"vis": None, "solid": solid0, "alpha_step": 0}
            self._overlay_state[key] = st

        self._overlay_colors: dict[str, tuple[float, float, float]] = dict(
            _OVERLAY_DEFAULT_COLORS)
        # None == the Bottom side follows the primary colour; a tuple == a
        # distinct Bottom colour (a built-in default from
        # _OVERLAY_DEFAULT_BOTTOM_COLORS, or one the user later pins). It is
        # applied to the Bottom side whether the layer is split or merged.
        self._overlay_bottom_colors: dict[
            str, tuple[float, float, float] | None] = {
            key: _OVERLAY_DEFAULT_BOTTOM_COLORS.get(key)
            for key in _OVERLAY_DEFAULT_COLORS
        }
        self._load_overlay_colors_from_project()
        self._load_overlay_visibility_from_project()

    def _load_overlay_visibility_from_project(self) -> None:
        """Restore overlay visibility from ``viewer_settings["overlay_state"]``."""
        proj = getattr(self, "_project", None)
        if proj is None:
            return
        saved = (getattr(proj, "viewer_settings", None) or {}).get(
            "overlay_state")
        if not isinstance(saved, dict):
            return
        valid_vis = {None, "rails", "all"}
        for key, st in saved.items():
            if key not in self._overlay_state or not isinstance(st, dict):
                continue
            if "split" in st:
                self._overlay_state[key]["split"] = bool(st["split"])
            for variant in ("both", "top", "bottom"):
                src = st.get(variant)
                if variant not in self._overlay_state[key] or not isinstance(
                        src, dict):
                    continue
                dst = self._overlay_state[key][variant]
                vis = src.get("vis")
                if vis in valid_vis:
                    dst["vis"] = vis
                if "solid" in src:
                    dst["solid"] = bool(src["solid"])
                if "alpha_step" in src:
                    try:
                        dst["alpha_step"] = int(src["alpha_step"])
                    except (TypeError, ValueError):
                        pass

    def _load_overlay_colors_from_project(self) -> None:
        """Override the built-in overlay colours with any saved in the open
        .fypa project (``viewer_settings["overlay_colors"]``).

        Each entry is ``{"primary": [r, g, b], "bottom": [r, g, b]}`` —
        ``"bottom"`` present only when the user pinned a distinct Bottom
        colour. A bare ``[r, g, b]`` list is also accepted (the colours
        were stored that way before split colours existed). Unknown keys
        and malformed values are skipped so an older or hand-edited project
        still loads cleanly."""
        proj = getattr(self, "_project", None)
        if proj is None:
            return
        saved = (getattr(proj, "viewer_settings", None) or {}).get(
            "overlay_colors") or {}

        clamp = lambda c: min(1.0, max(0.0, c))

        def _rgb(v):
            try:
                return (clamp(float(v[0])), clamp(float(v[1])),
                        clamp(float(v[2])))
            except (TypeError, ValueError, IndexError):
                return None

        for key, val in saved.items():
            if key not in self._overlay_colors:
                continue
            if isinstance(val, dict):
                primary, bottom = _rgb(val.get("primary")), _rgb(
                    val.get("bottom"))
            else:
                primary, bottom = _rgb(val), None   # legacy flat form
            if primary is not None:
                self._overlay_colors[key] = primary
            if bottom is not None:
                self._overlay_bottom_colors[key] = bottom

    def _is_gerber_source(self) -> bool:
        """True when the open design was imported from Gerbers (rather than
        an Altium project). Gerber imports carry no component/pad/overlay/
        designator data, so those Board Features rows are suppressed."""
        return bool(self.metadata
                    and self.metadata.get("source_kind") == "gerber")

    def _build_overlay_list(self) -> None:
        """(Re)populate the Overlays QListWidget from ``self._overlay_state``.

        A split layer contributes two rows (Top / Bottom); a merged layer
        one. Called once at construction and again whenever a split toggle
        flips, so the list re-sizes to whatever the current state needs.

        Gerber-sourced designs have no component/pad/overlay/designator
        geometry, so only the "Board outline" row is shown for them."""
        self.overlay_list.clear()
        n_rows = 0
        gerber_only = self._is_gerber_source()
        for key, label, has_sides in _OVERLAY_LAYERS:
            # Gerber imports carry no component/pad/overlay/designator data,
            # so only rows derivable from copper + drill data are shown: the
            # board outline and non-plated through holes.
            if gerber_only and key not in ("board_outline", "npth"):
                continue
            if has_sides and self._overlay_state[key]["split"]:
                self._add_overlay_row(key, "top", f"Top {label}",
                                      splittable=True)
                self._add_overlay_row(key, "bottom", f"Bottom {label}",
                                      splittable=False)
                n_rows += 2
            else:
                self._add_overlay_row(key, "both", label,
                                      splittable=has_sides)
                n_rows += 1
        row_h = self.overlay_list.sizeHintForRow(0) or 24
        self.overlay_list.setFixedHeight(n_rows * row_h + 6)

    def _add_overlay_row(self, key: str, variant: str, label_text: str, *,
                         splittable: bool) -> None:
        """Append one overlay row widget to the list."""
        row = self._build_overlay_row_widget(key, variant, label_text,
                                             splittable=splittable)
        item = QListWidgetItem()
        item.setFlags(Qt.ItemIsEnabled)
        self.overlay_list.addItem(item)
        item.setSizeHint(row.sizeHint())
        self.overlay_list.setItemWidget(item, row)

    def _build_overlay_row_widget(self, key: str, variant: str,
                                  label_text: str, *,
                                  splittable: bool) -> QWidget:
        """Build one row of the Overlays control.

        Layout: ``[rails eye][all eye][colour] <label> … [split] [fill]``.
        The two eyes are a mutually-exclusive pair (selected-rails-only vs.
        everywhere), so the row is hidden / rails / all. The colour swatch
        opens a picker for the overlay's draw colour. ``splittable`` rows
        carry the split toggle just left of the fill toggle; on an
        already-split layer the Top row keeps it (acting as a merge button)
        while the Bottom row gets an aligning spacer in its place."""
        sub = self._overlay_state[key][variant]
        is_board = key == "board_outline"
        # Overlays with no per-net association get only the "show
        # everywhere" eye — a "show on the selected rails only" eye is
        # meaningless when nothing ties the overlay to a rail. That covers
        # the mechanical board outline and the silkscreen "Overlay" layer
        # (graphics-only). Every other overlay keeps the rails-only eye.
        has_rails_eye = key not in ("board_outline", "silkscreen", "npth")
        w = QWidget()
        layout = QHBoxLayout(w)
        layout.setContentsMargins(2, 1, 6, 1)
        layout.setSpacing(4)

        # The "show everywhere" eye is on every row. Its companion
        # "show on the selected rails only" eye sits to its left — except
        # on rows with no per-net association (see ``has_rails_eye``), which
        # get an equal-width spacer instead, keeping the "show everywhere"
        # eye in the same column as every other row. The two eyes (when both
        # present) are mutually exclusive: the row is hidden / rails /
        # everywhere.
        all_eye = EyeButton(
            visible=(sub["vis"] == "all"),
            tip_show="Show everywhere",
            tip_hide="Hide (shown everywhere)",
        )
        if not has_rails_eye:
            rails_eye = None
            rails_slot: QWidget = QLabel()
            rails_slot.setFixedSize(all_eye.width(), all_eye.height())
        else:
            rails_eye = EyeButton(
                visible=(sub["vis"] == "rails"),
                tip_show="Show on the selected rails only",
                tip_hide="Hide (shown on the selected rails only)",
            )
            rails_eye.toggled_visible.connect(
                lambda on: self._on_overlay_eye(key, variant, "rails", on,
                                                rails_eye, all_eye)
            )
            rails_slot = rails_eye
        all_eye.toggled_visible.connect(
            lambda on: self._on_overlay_eye(key, variant, "all", on,
                                            rails_eye, all_eye)
        )
        layout.addWidget(rails_slot)
        layout.addWidget(all_eye)

        # Colour swatch — opens a picker for this overlay's draw colour.
        # A merged row and the Top row share the layer's primary colour;
        # the Bottom row of a split layer carries its own swatch (its own
        # colour, or the primary while it is still following — see
        # _overlay_swatch_rgb / _on_overlay_color).
        colour_btn = OverlayColorButton(
            rgb=self._overlay_swatch_rgb(key, variant))
        colour_btn.colorChanged.connect(
            lambda rgb, k=key, v=variant: self._on_overlay_color(k, v, rgb)
        )
        layout.addWidget(colour_btn)

        name = QLabel(label_text)
        name.setStyleSheet(f"QLabel {{ color: {_T()['fg']}; }}")
        layout.addWidget(name)
        layout.addStretch(1)

        # Split / merge toggle — only on rows that own a Top + Bottom pair.
        # Sits just left of the fill toggle at the right of the row. Other
        # rows (vias, and the Bottom side of a split layer) get a spacer the
        # same width so the fill toggle still lines up across every row.
        if splittable:
            split_btn = SplitButton(split=self._overlay_state[key]["split"])
            split_btn.toggled_split.connect(
                lambda on: self._on_overlay_split(key, on)
            )
            layout.addWidget(split_btn)
        else:
            # Size off all_eye, not rails_eye — the board-outline row has
            # no rails_eye (it is None there).
            spacer = QLabel()
            spacer.setFixedSize(all_eye.width(), all_eye.height())
            layout.addWidget(spacer)

        # Transparency control — sits immediately left of the wire-mesh /
        # solid fill toggle on every row (including the board outline, which
        # still benefits from being able to fade its outline ribbon).
        transp_btn = TransparencyButton(step=int(sub.get("alpha_step", 0) or 0))
        transp_btn.toggled_transparency.connect(
            lambda step: self._on_overlay_transparency(key, variant, step)
        )
        layout.addWidget(transp_btn)

        # Wire-mesh / solid fill toggle, pinned to the far right of the row.
        # The board outline is always a fixed-width ribbon — solid vs
        # wire-mesh is meaningless — so that row gets an aligning spacer in
        # the fill toggle's place instead.
        if is_board:
            fill_slot: QWidget = QLabel()
            fill_slot.setFixedSize(all_eye.width(), all_eye.height())
            layout.addWidget(fill_slot)
        else:
            fill_btn = FillToggleButton(solid=sub["solid"])
            fill_btn.toggled_fill.connect(
                lambda on: self._on_overlay_fill(key, variant, on)
            )
            layout.addWidget(fill_btn)
        return w

    def _on_overlay_eye(self, key: str, variant: str, scope: str, on: bool,
                        rails_eye: EyeButton | None,
                        all_eye: EyeButton) -> None:
        """One of an overlay row's eyes toggled. When the row has both
        eyes they are a mutually-exclusive pair, so turning one on turns
        the other off — the row ends up hidden / shown-on-rails /
        shown-everywhere. The board-outline row has only the "everywhere"
        eye (``rails_eye`` is None), so there is no companion to clear."""
        sub = self._overlay_state[key][variant]
        if on:
            sub["vis"] = scope
            other = all_eye if scope == "rails" else rails_eye
            if other is not None:
                other.setVisibleState(False, emit=False)
        elif sub["vis"] == scope:
            sub["vis"] = None
        self._on_overlay_changed()

    def _on_overlay_fill(self, key: str, variant: str, solid: bool) -> None:
        """An overlay row's wire-mesh / solid fill toggle flipped."""
        self._overlay_state[key][variant]["solid"] = bool(solid)
        self._on_overlay_changed()

    def _on_overlay_transparency(self, key: str, variant: str,
                                  step: int) -> None:
        """An overlay row's transparency control cycled."""
        self._overlay_state[key][variant]["alpha_step"] = int(step)
        self._on_overlay_changed()

    def _overlay_swatch_rgb(self, key: str,
                            variant: str) -> tuple[float, float, float]:
        """Colour the swatch button on a ``(key, variant)`` row should show.

        The Bottom row of a split layer shows its own pinned colour, or the
        primary colour while it is still following it. Every other row (a
        merged ``both`` row, or the Top row) shows the primary colour."""
        if variant == "bottom":
            bot = self._overlay_bottom_colors.get(key)
            if bot is not None:
                return bot
        return self._overlay_colors[key]

    def _overlay_color_for(self, key: str,
                           side: str) -> tuple[float, float, float]:
        """RGB an overlay's geometry on ``side`` ("top" / "bottom") is drawn
        in. The Bottom side uses its own distinct colour whenever one is set
        — whether the layer is split or merged — so a merged row still draws
        its bottom-side geometry in the Bottom colour. The Top side, and the
        Bottom side when it has no distinct colour, use the primary."""
        if side == "bottom":
            bot = self._overlay_bottom_colors.get(key)
            if bot is not None:
                return bot
        return self._overlay_colors[key]

    def _on_overlay_color(self, key: str, variant: str,
                          rgb: tuple[float, float, float]) -> None:
        """A Board Features row's colour swatch picked a new colour.

        The Bottom row of a split layer pins its own colour
        (``self._overlay_bottom_colors``); a merged or Top row sets the
        layer's primary colour. The list is rebuilt (deferred a tick, as the
        picked swatch is mid-emit) so a Bottom swatch still following the
        primary repaints too. The overlay geometry is recoloured
        immediately, and the project is marked dirty so the new colour is
        written to the .fypa on the next save."""
        rgb = tuple(float(c) for c in rgb)
        if variant == "bottom":
            self._overlay_bottom_colors[key] = rgb
        else:
            self._overlay_colors[key] = rgb
        # Display-only change: it needs saving to the .fypa but does NOT make
        # the solve stale, so use _display_dirty rather than _project_dirty —
        # otherwise a cosmetic colour pick would light the ↻ Resolve button.
        self._display_dirty = True
        QTimer.singleShot(0, self._build_overlay_list)
        self._on_overlay_changed()

    def _on_overlay_split(self, key: str, split: bool) -> None:
        """An overlay row's split toggle flipped. Splitting seeds the fresh
        Top / Bottom rows from the merged row; merging keeps the Top row's
        state on the recombined row. The list is rebuilt so rows appear /
        disappear — deferred to the next event-loop tick because the rebuild
        deletes the SplitButton that is mid-emit right now."""
        st = self._overlay_state[key]
        if split == st["split"]:
            return
        st["split"] = split
        if split:
            for side in ("top", "bottom"):
                st[side]["vis"] = st["both"]["vis"]
                st[side]["solid"] = st["both"]["solid"]
                st[side]["alpha_step"] = st["both"].get("alpha_step", 0)
        else:
            st["both"]["vis"] = st["top"]["vis"]
            st["both"]["solid"] = st["top"]["solid"]
            st["both"]["alpha_step"] = st["top"].get("alpha_step", 0)
        QTimer.singleShot(0, self._build_overlay_list)
        self._on_overlay_changed()

    def _overlay_visibility(self) -> dict[str, dict]:
        """Current Overlays selection, keyed by layer then row variant —
        e.g. ``{"pads": {"top": {"vis": "rails", "solid": True}, …}}``.

        ``vis`` is ``None`` / ``"rails"`` / ``"all"``. For a merged layer
        only the ``"both"`` variant is populated; for a split layer the
        ``"top"`` and ``"bottom"`` variants are."""
        out: dict[str, dict] = {}
        for key, _label, has_sides in _OVERLAY_LAYERS:
            st = self._overlay_state[key]
            variants = (("top", "bottom") if has_sides and st["split"]
                        else ("both",))
            out[key] = {v: dict(st[v]) for v in variants}
        return out

    def _on_overlay_changed(self) -> None:
        """An overlay row's visibility, fill style or split state changed.

        Overlays are independent of the FEM heatmap, so this rebuilds just
        the overlay geometry rather than triggering a full :meth:`_render`.
        The board outline lives in its own GL batch, so it is refreshed
        alongside (its row is part of the same Overlays control)."""
        self._refresh_overlay_geometry(self._visible_rails())
        self._refresh_board_outline()

    def _overlay_side_states(self, key: str) -> dict:
        """Return ``{"top": (vis, solid), "bottom": (vis, solid)}`` for an
        overlay layer. When the layer is not split, the merged row's state
        applies to both sides."""
        st = self._overlay_state[key]
        if st.get("split"):
            return {"top": (st["top"]["vis"], st["top"]["solid"]),
                    "bottom": (st["bottom"]["vis"], st["bottom"]["solid"])}
        b = st["both"]
        return {"top": (b["vis"], b["solid"]),
                "bottom": (b["vis"], b["solid"])}

    def _overlay_side_alpha(self, key: str) -> dict[str, float]:
        """Per-side draw alpha (0..1) for an overlay layer, mirroring
        :meth:`_overlay_side_states`. Reads each row's TransparencyButton
        step (0 = opaque, 4 = invisible). A merged row's step applies to
        both sides."""
        st = self._overlay_state[key]
        if st.get("split"):
            return {
                "top": _transparency_alpha(st["top"].get("alpha_step", 0)),
                "bottom": _transparency_alpha(
                    st["bottom"].get("alpha_step", 0)),
            }
        a = _transparency_alpha(st["both"].get("alpha_step", 0))
        return {"top": a, "bottom": a}

    def _caps_overlay_signature(self) -> tuple:
        """Snapshot of the capacitor-overlay inputs, for
        :meth:`_overlay_geom_signature`'s identity-skip. Empty when the
        overlay is off, so toggling it always forces a rebuild."""
        box = getattr(self, "caps_overlay_box", None)
        if box is None or not box.isChecked():
            return ()
        rows = getattr(self, "_caps_rows_cache", None) or []
        return tuple(
            (r["designator"], round(self._cap_l_best_nh(r) or -1.0, 5))
            for r in rows if r.get("included", True)
        )

    def _emit_cap_inductance_overlay(self, _emit, labels, in_2d,
                                     feature_z) -> None:
        """Colour every included capacitor's pads by its loop inductance.

        Uses the best available tier (L3 > L2 > L1) on a log scale, because
        mounted inductance spans a decade across a board and a linear ramp
        would flatten everything but the worst offender. Silent no-op when
        the overlay is off, the rows were never computed, or the design
        info isn't loaded."""
        box = getattr(self, "caps_overlay_box", None)
        if box is None or not box.isChecked():
            return
        rows = [r for r in (getattr(self, "_caps_rows_cache", None) or [])
                if r.get("included", True)
                and self._cap_l_best_nh(r) is not None]
        if not rows:
            return
        loaded = getattr(self, "_loaded_project", None)
        extracted = getattr(loaded, "extracted", None)
        if extracted is None:
            return

        from fypa.altium_geometry import _pad_polygon

        values = [self._cap_l_best_nh(r) for r in rows]
        lo, hi = min(values), max(values)
        log_lo, log_hi = math.log10(max(lo, 1e-4)), math.log10(max(hi, 1e-3))
        span = max(log_hi - log_lo, 1e-6)
        cmap = matplotlib.colormaps["inferno"]

        for row, value in zip(rows, values):
            cap = row["cap"]
            t = (math.log10(max(value, 1e-4)) - log_lo) / span
            rgb = cmap(float(np.clip(t, 0.0, 1.0)))[:3]
            side = "bottom" if cap.mount_layer_id == 32 else "top"
            z = feature_z[side]
            for pi in cap.pads_rail + cap.pads_return:
                poly = _pad_polygon(extracted.pads[pi], cap.mount_layer_id)
                if poly is None or poly.is_empty:
                    continue
                # A through-hole pad's polygon can carry its bore as an
                # interior ring, or come back as a MultiPolygon; the fan
                # fill wants one convex outer ring, so take each part's
                # exterior and let the bore paint over.
                parts = ([poly] if poly.geom_type == "Polygon"
                         else list(getattr(poly, "geoms", [])))
                for part in parts:
                    if part.geom_type != "Polygon" or part.is_empty:
                        continue
                    ring = np.asarray(part.exterior.coords, dtype=np.float64)
                    _emit(_overlay_fan_tris(ring), z, rgb,
                          top=in_2d and side == "top", alpha=0.85)
            labels.append({
                "x": cap.center_xy[0],
                "y": cap.center_xy[1],
                "z": z,
                "text": f"{cap.designator} {value:.2f} nH",
                "color": "#ffffff",
                "height_mm": 0.4,
            })

    def _overlay_geom_signature(self, rails) -> tuple:
        """A hashable snapshot of every input :meth:`_refresh_overlay_geometry`
        reads to build the overlay-fill batch + labels. When two successive
        refreshes produce the same signature the previously-built batch is
        still valid, so the rebuild + GPU re-upload can be skipped.

        Stackup / layer-z / metadata-derived inputs (``self._physicals``, the
        ``md.get(...)`` records, ``_layer_z_for``) are NOT captured field by
        field — they only change on a solution swap, which resets
        ``_overlay_geom_sig`` via :meth:`_clear_solution_derived_caches`. This
        captures only the inputs that change *within* one solution."""
        def _freeze(o):
            if isinstance(o, dict):
                return tuple(sorted(((str(k), _freeze(v)) for k, v in o.items()),
                                    key=lambda kv: kv[0]))
            if isinstance(o, (list, tuple)):
                return tuple(_freeze(v) for v in o)
            return o

        in_2d = not self.view_3d_box.isChecked()
        rail_members = (tuple(sorted(self._effective_rail_members(rails)))
                        if rails else ())
        via_span = bool(getattr(self, "via_span_box", None) is not None
                        and self.via_span_box.isChecked())
        # Board-feature rows (silkscreen / components / pads / designators /
        # npth / board outline): all per-row vis / solid / alpha_step / split
        # state plus the overlay colours. Frozen generically so any field the
        # geometry reads is captured without structural assumptions.
        rows = (_freeze(self._overlay_state),
                _freeze(self._overlay_colors),
                _freeze(self._overlay_bottom_colors))
        # Per-layer all-copper (second eye): visibility, fill style, alpha,
        # swatch colour. _layer_render_z's only dynamic inputs are the
        # selected layer + 2D/3D, both captured below.
        fill_by_name = dict(getattr(self, "_layer_fill_buttons", []))
        transp_by_name = dict(getattr(self, "_layer_transparency_buttons", []))
        ac = []
        for name, eye2 in getattr(self, "_layer_eye2_buttons", []):
            fb = fill_by_name.get(name)
            tb = transp_by_name.get(name)
            ac.append((
                name,
                bool(eye2.isVisibleState()),
                bool(fb.isSolid()) if fb is not None else False,
                float(tb.alpha()) if tb is not None else 1.0,
                str(self._layer_color_for(name)),
            ))
        return (
            in_2d,
            rail_members,
            bool(self._editor_mode),
            frozenset(self._editor_highlight_nets),
            frozenset(self._editor_highlight_polys),
            # Net focus removes copper from the batch outright, so a focus
            # change is a geometry change — not just a colour one.
            frozenset(self._editor_focus_nets),
            frozenset(self._editor_focus_polys),
            self._selected_layer,
            via_span,
            tuple(rows),
            tuple(ac),
            self._caps_overlay_signature(),
        )

    def _refresh_overlay_geometry(self, rails) -> None:
        """Rebuild and push the Overlays control's GL geometry.

        Every visible overlay row contributes flat-shaded triangles — a
        solid fill, or a thin ribbon for wire-mesh outlines — to the GL
        viewer's overlay-fill batch; visible designator rows contribute
        text labels. Independent of the FEM heatmap, so it is safe to call
        regardless of the rail / layer selection.

        ``rails`` is the list of currently-visible rail names; it drives
        the per-row "show on the selected rails only" eye. Silkscreen has
        no per-net association, so either eye simply shows it."""
        gv = getattr(self, "_gl_viewer", None)
        if gv is None:
            return
        md = self.metadata
        if md is None:
            gv.clear_overlay_fills()
            gv.clear_overlay_labels()
            # Force a rebuild once metadata comes back.
            self._overlay_geom_sig = None
            return

        # Skip the (heavy) rebuild + GPU re-upload when nothing that feeds the
        # overlay batch has changed since the last build. set_overlay_fills /
        # clear_overlay_fills are only ever called from this method, so the GL
        # viewer still holds the correct batch when we skip. (Finding 3.8.)
        try:
            sig = self._overlay_geom_signature(rails)
        except Exception:
            sig = None  # never let signature trouble block a real refresh
        if sig is not None and sig == self._overlay_geom_sig:
            return
        # Invalidate now; only re-store after the build below completes, so a
        # build that raises part-way doesn't leave a stale sig marking a
        # half-uploaded batch as current.
        self._overlay_geom_sig = None

        rail_members = (set(self._effective_rail_members(rails))
                        if rails else set())
        # Per-side z for 3D placement. ``side_z`` is the outer copper plane
        # on each side — used by vias, which span the whole board. z=0 is
        # the top of the stackup; lower layers are negative.
        side_z = {"top": 0.0, "bottom": 0.0}
        if self._physicals:
            side_z["top"] = self._layer_z_for(self._physicals[0])
            side_z["bottom"] = self._layer_z_for(self._physicals[-1])
        # Board features (silkscreen / pads / components / designators /
        # NPTH) physically sit on the board's outer surfaces, so top-side
        # items are lifted ABOVE the topmost physical layer and bottom-side
        # items dropped BELOW the lowest, just far enough to read as
        # clearly separate from the copper in 3D.
        n_phys = len(self._physicals)
        if n_phys > 1:
            pitch = (side_z["top"] - side_z["bottom"]) / (n_phys - 1)
        else:
            pitch = self._LAYER_Z_SPACING_MM
        if pitch <= 0.0:
            pitch = self._LAYER_Z_SPACING_MM
        # Clamp the lift to a small offset. On a 2-layer board the "mean
        # layer pitch" is the *entire* board thickness, which would float
        # features (e.g. NPTH mounting-hole discs) a whole board-thickness
        # off each face — they should hug the surface instead.
        lift = min(pitch, self._FEATURE_LIFT_MM)
        feature_z = {"top": side_z["top"] + lift,
                     "bottom": side_z["bottom"] - lift}

        # Triangles split into three batches: ``under_*`` draw BEFORE the
        # heatmap mesh (2D only — bottom-side board features sit behind the
        # bottom copper), ``over_*`` draw after the mesh (vias, the per-
        # layer all-copper overlay), and ``top_*`` draw last (2D only —
        # top-side board features sit in front of all top copper / rails,
        # the all-copper overlay, and every other overlay batch). In 3D the
        # per-side feature z + depth test handle ordering, so everything
        # goes to ``over_*``.
        in_2d = not self.view_3d_box.isChecked()
        under_tri: list[np.ndarray] = []
        under_col: list[np.ndarray] = []
        over_tri: list[np.ndarray] = []
        over_col: list[np.ndarray] = []
        top_tri: list[np.ndarray] = []
        top_col: list[np.ndarray] = []
        labels: list[dict] = []

        def _emit(verts_xy: np.ndarray, z: float,
                  rgb: tuple[float, float, float], *,
                  under: bool = False, top: bool = False,
                  alpha: float = 1.0) -> None:
            if verts_xy.size == 0:
                return
            if under and top:
                raise ValueError("under and top are mutually exclusive")
            p = np.empty((verts_xy.shape[0], 3), dtype=np.float32)
            p[:, :2] = verts_xy
            p[:, 2] = z
            if under:
                tri, col = under_tri, under_col
            elif top:
                tri, col = top_tri, top_col
            else:
                tri, col = over_tri, over_col
            tri.append(p)
            rgba = np.empty((p.shape[0], 4), dtype=np.float32)
            rgba[:, 0] = float(rgb[0])
            rgba[:, 1] = float(rgb[1])
            rgba[:, 2] = float(rgb[2])
            rgba[:, 3] = float(alpha)
            col.append(rgba)

        # In 2D the top-side board features are routed to ``top_*`` so they
        # paint on top of the mesh AND the all-copper overlay. In 3D the
        # depth test handles ordering, so they stay in ``over_*``.
        def _side_buckets(side: str) -> dict[str, bool]:
            if not in_2d:
                return {}
            if side == "top":
                return {"top": True}
            if side == "bottom":
                return {"under": True}
            return {}

        def _shape_tris(ring, solid: bool) -> np.ndarray:
            # Solid = filled polygon; wire-mesh = thin mitered outline of
            # the closed ring (pad / via / component box).
            return (_overlay_fan_tris(ring) if solid
                    else _overlay_outline_tris(ring, _OVERLAY_WIRE_HALF_MM,
                                               closed=True))

        def _net_match(nets) -> bool:
            return bool(rail_members) and bool(set(nets) & rail_members)

        self._pump_busy_ui()
        # Overlay graphics — silkscreen tracks / arcs. Each is drawn at its
        # real track width with round end caps, so it matches Altium's
        # round-capped tracks and consecutive segments join smoothly. No
        # per-net data, so either eye ("selected rails" / "all") shows it.
        sides = self._overlay_side_states("silkscreen")
        alphas = self._overlay_side_alpha("silkscreen")
        for rec in md.get("silkscreen", []):
            self._pump_busy_ui()
            if rec.get("kind") == "text":
                continue  # legacy pickles only — text isn't an Overlay item
            side = rec.get("side", "top")
            vis, solid = sides.get(side, (None, False))
            if vis is None:
                continue
            a = alphas.get(side, 1.0)
            if a <= 0.0:
                continue
            rgb = self._overlay_color_for("silkscreen", side)
            poly = rec.get("polyline") or []
            if len(poly) < 2:
                continue
            # Real track width (Altium silkscreen is thin); a small floor
            # keeps a zero-width track from collapsing to nothing.
            half = max(0.5 * float(rec.get("width_mm", 0.0) or 0.0), 0.01)
            z = feature_z.get(side, 0.0)
            bucket = _side_buckets(side)
            if solid:
                _emit(_overlay_outline_tris(poly, half, closed=False), z,
                      rgb, alpha=a, **bucket)
                # Round end caps — interior joins are already mitered.
                for end in (poly[0], poly[-1]):
                    _emit(_overlay_fan_tris(_overlay_circle_ring(
                        float(end[0]), float(end[1]), half, 12)), z, rgb,
                        alpha=a, **bucket)
            else:
                # Wire-mesh: the track's hollow perimeter. The round caps
                # are part of the traced outline — no separate cap fills.
                _emit(_overlay_ribbon_outline_tris(
                    poly, half, _OVERLAY_WIRE_HALF_MM, closed=False),
                    z, rgb, alpha=a, **bucket)

        self._pump_busy_ui()
        # Components — axis-aligned bounding box per component.
        sides = self._overlay_side_states("components")
        alphas = self._overlay_side_alpha("components")
        for rec in md.get("components", []):
            self._pump_busy_ui()
            side = rec.get("side", "top")
            vis, solid = sides.get(side, (None, False))
            if vis is None:
                continue
            if vis == "rails" and not _net_match(rec.get("nets", [])):
                continue
            a = alphas.get(side, 1.0)
            if a <= 0.0:
                continue
            rgb = self._overlay_color_for("components", side)
            bbox = rec.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            _emit(_shape_tris(_overlay_box_ring(*bbox), solid),
                  feature_z[side], rgb, alpha=a, **_side_buckets(side))

        self._pump_busy_ui()
        # Pads — outline ring per pad, drawn on whichever board side(s)
        # the pad's copper layers include (through-hole pads → both).
        # Emitted AFTER components: the 2D overlay batch has no depth test,
        # so the last-emitted geometry wins — this puts pads visually on
        # top of the component bodies they sit on.
        enabled = md.get("enabled_copper_layer_ids") or []
        top_lid = enabled[0] if enabled else None
        bot_lid = enabled[-1] if enabled else None
        sides = self._overlay_side_states("pads")
        alphas = self._overlay_side_alpha("pads")
        for rec in md.get("pads", []):
            self._pump_busy_ui()
            ring = rec.get("outline")
            if not ring:
                continue
            lids = rec.get("layer_ids") or []
            # Pads with a per-layer pad stack carry per-layer outline rings;
            # draw each side with the shape its copper layer actually has.
            by_layer = rec.get("outline_by_layer") or {}
            for side, lid in (("top", top_lid), ("bottom", bot_lid)):
                if lid is None or lid not in lids:
                    continue
                vis, solid = sides[side]
                if vis is None:
                    continue
                if vis == "rails" and not _net_match([rec.get("net")]):
                    continue
                a = alphas.get(side, 1.0)
                if a <= 0.0:
                    continue
                side_ring = by_layer.get(str(lid), ring)
                rgb = self._overlay_color_for("pads", side)
                _emit(_shape_tris(side_ring, solid), feature_z[side], rgb,
                      alpha=a, **_side_buckets(side))

        self._pump_busy_ui()
        # Designators — drawn in Altium's actual single-stroke font: the
        # loader lays each one out into stroke polylines, which render as
        # thin round-capped ribbons (same path as silkscreen). TrueType-
        # font designators carry no polylines and fall back to a label.
        sides = self._overlay_side_states("designators")
        alphas = self._overlay_side_alpha("designators")
        for rec in md.get("designators", []):
            self._pump_busy_ui()
            side = rec.get("side", "top")
            vis, solid = sides.get(side, (None, False))
            if vis is None:
                continue
            if vis == "rails" and not _net_match(rec.get("nets", [])):
                continue
            a = alphas.get(side, 1.0)
            if a <= 0.0:
                continue
            rgb = self._overlay_color_for("designators", side)
            if rec.get("polylines"):
                _emit(self._designator_stroke_tris(rec, solid=solid),
                      feature_z[side], rgb, alpha=a, **_side_buckets(side))
            else:
                # Text labels honour the row's transparency by riding the
                # colour's alpha channel through #rrggbbaa.
                qc = QColor.fromRgbF(rgb[0], rgb[1], rgb[2], a)
                labels.append({
                    "x": float(rec.get("x_mm", 0.0)),
                    "y": float(rec.get("y_mm", 0.0)),
                    "z": feature_z[side],
                    "text": rec.get("text", ""),
                    "color": qc.name(QColor.HexArgb),
                    "height_mm": float(rec.get("height_mm", 1.0) or 1.0),
                    "rotation_deg": float(rec.get("rotation_deg", 0.0) or 0.0),
                })

        self._pump_busy_ui()
        # Non-plated through holes — a drilled hole through the whole board
        # (mounting / mechanical hole) with no net. Drawn on both faces as a
        # filled disc or a ring outline per the row's fill toggle, so it
        # reads regardless of which side is in view. No per-net association,
        # so only the "show everywhere" eye gates it.
        npth_recs = md.get("npth") or []
        if npth_recs:
            sides = self._overlay_side_states("npth")
            alphas = self._overlay_side_alpha("npth")
            for side in ("top", "bottom"):
                vis, solid = sides.get(side, (None, False))
                if vis is None:
                    continue
                a = alphas.get(side, 1.0)
                if a <= 0.0:
                    continue
                rgb = self._overlay_color_for("npth", side)
                z = feature_z[side]
                bucket = _side_buckets(side)
                for rec in npth_recs:
                    self._pump_busy_ui()
                    # Slot-aware: an obround drill when the record carries
                    # slot fields, otherwise a plain circle at diameter_mm.
                    ring = _overlay_hole_ring(rec)
                    if ring.shape[0] < 3:
                        continue
                    _emit(_shape_tris(ring, solid), z, rgb, alpha=a, **bucket)

        self._pump_busy_ui()
        # Physical-layer "all copper" — the per-layer second eye. For every
        # layer whose all-copper eye is on, draw all of that layer's copper
        # that ISN'T part of a selected rail (the rails are already the
        # heatmap; this is the rest), in the layer's swatch colour, as a
        # wire-mesh outline or a solid fill per that row's fill toggle.
        # The row's TransparencyButton fades that geometry without affecting
        # the heatmap itself.
        eye2_buttons = getattr(self, "_layer_eye2_buttons", [])
        if eye2_buttons and md.get("all_copper"):
            ac_by_layer: dict[int, list] = {}
            for rec in md["all_copper"]:
                ac_by_layer.setdefault(rec.get("layer_id"), []).append(rec)
            fill_by_name = dict(getattr(self, "_layer_fill_buttons", []))
            transp_by_name = dict(
                getattr(self, "_layer_transparency_buttons", []))
            # Which nets the all-copper view skips because the heatmap mesh
            # already draws them. Outside editor mode the selected rail IS
            # the heatmap, so its copper is excluded to avoid double-drawing.
            # In editor mode the user wants the second eye to show ALL of a
            # layer's physical copper — including nets belonging to a
            # previously-solved rail — so we drop the filter entirely. The
            # solved-rail heatmap / geometry still paints on top: opaque
            # all-copper rides in the ``under_*`` bucket (drawn before the
            # mesh) in 2D, and depth ordering handles it in 3D.
            ac_rail_members: set[str] = (
                set() if self._editor_mode else rail_members)
            # Emit bottom-up: ``_layer_eye2_buttons`` is in panel order
            # (topmost first); sort by stackup rank descending so the
            # topmost layer is emitted last, mirroring the heatmap mesh's
            # phys_draw_order — that matches the GL_LEQUAL depth test the
            # under-mesh batch uses in 2D (same z → later draw wins, so
            # topmost-emitted-last still wins).
            # In 2D opaque all-copper rides in ``under_*`` so it paints
            # BEFORE the heatmap mesh with depth on — the rail mesh
            # (drawn next, same per-layer z) paints over its own layer's
            # all-copper, and bottom rail triangles are depth-rejected
            # where a higher layer's all-copper has already written a
            # closer z. That cross-layer z stacking only works because
            # the under-mesh pass also WRITES depth, which would equally
            # depth-reject the rail mesh underneath a partially-
            # transparent layer — turning the transparent layer into a
            # tinted hole instead of letting the heatmap show through.
            # So a layer at alpha < 1.0 is routed to ``over_*`` instead:
            # drawn AFTER the mesh, no depth interference, free to blend
            # with the mesh (or other opaque all-copper) below. 3D
            # always uses ``over_*`` — global per-vertex z + depth
            # handles ordering there.
            sel_layer = self._selected_layer
            # In 2D the selected layer is forced to emit LAST so it paints
            # on top of the dimmed others (painter's order). In 3D the
            # per-vertex z + depth test already place each layer correctly,
            # and depth-write-on transparency needs a strict back-to-front
            # (stackup) draw order: forcing the selected layer last there
            # would depth-reject the layers sitting behind it, making an
            # unselected transparent layer read as opaque. So apply the
            # selection priority only in 2D.
            for name, eye2 in sorted(
                eye2_buttons,
                key=lambda ne: (
                    (0 if ne[0] == sel_layer else 1) if in_2d else 0,
                    self._phys_stackup_rank.get(ne[0], 1 << 30),
                ),
                reverse=True,
            ):
                if not eye2.isVisibleState():
                    continue
                recs = ac_by_layer.get(self._phys_name_to_layer_id.get(name))
                if not recs:
                    continue
                # Per-layer pump — for a 16-layer board with all-copper on,
                # this gives ~16 marquee-bar ticks during the heaviest
                # section of the overlay refresh.
                self._pump_busy_ui()
                fb = fill_by_name.get(name)
                solid = bool(fb.isSolid()) if fb is not None else False
                tb = transp_by_name.get(name)
                layer_alpha = tb.alpha() if tb is not None else 1.0
                if layer_alpha <= 0.0:
                    continue
                # Opaque layers stay in the under-mesh batch to keep the
                # cross-layer z stacking; transparent layers fall to
                # ``over_*`` so the mesh underneath isn't depth-rejected.
                ac_bucket = ({"under": True}
                             if (in_2d and layer_alpha >= 1.0)
                             else {})
                qc = QColor(self._layer_color_for(name))
                lrgb = (qc.redF(), qc.greenF(), qc.blueF())
                # When a layer is selected, every OTHER layer's all-copper
                # is blended toward the dim target and (optionally) faded
                # so the selected layer reads clearly above the rest.
                if sel_layer is not None and name != sel_layer:
                    lrgb = self._selected_layer_dim_rgb(lrgb)
                    layer_alpha *= max(
                        0.0, min(1.0, self._SELECTED_LAYER_UNSEL_ALPHA))
                    if layer_alpha <= 0.0:
                        continue
                    # Partial alpha after the multiplier needs the over-
                    # mesh bucket so the rail mesh underneath blends
                    # through instead of being depth-rejected (mirrors the
                    # existing transparency routing logic above).
                    if in_2d and layer_alpha < 1.0:
                        ac_bucket = {}
                z = self._layer_render_z(name)
                # Solid + partial transparency: route through a per-layer
                # merged triangulation so overlapping source polygons
                # blend the layer's alpha exactly once per pixel (rather
                # than once per overlapping polygon, which cumulatively
                # pushes the area to opacity). The merge collapses
                # per-net distinction, so editor-mode connectivity dim
                # falls back to a per-rec emit on this path — the wire-
                # mesh fill keeps the precise per-net dim anyway.
                editor_dim_active = (self._editor_mode and (
                    self._editor_highlight_nets
                    or self._editor_highlight_polys))
                # Net focus drops polygons, so it needs the same per-rec
                # path for the same reason: the merge collapses the per-net
                # distinction the skip test depends on.
                if (solid and layer_alpha < 1.0
                        and not editor_dim_active
                        and not self._editor_focus_active()):
                    merged = self._merged_solid_all_copper_tris(
                        name, recs, ac_rail_members)
                    if merged is not None:
                        _emit(merged, z, lrgb, alpha=layer_alpha,
                              **ac_bucket)
                    continue
                focus_on = self._editor_focus_active()
                for rec in recs:
                    net = rec.get("net")
                    if net in ac_rail_members:
                        continue
                    rec_layer_id = rec.get("layer_id")
                    # Net focus: a record whose net is focused is wholly in,
                    # and one whose net is a *different* real net is wholly
                    # out — only the "(none)" sentinel needs the per-polygon
                    # test below, so skip the whole record when we can.
                    if (focus_on and net and net != "(none)"
                            and net not in self._editor_focus_nets):
                        continue
                    # Per-rec pump — a single layer on Corvette can have
                    # hundreds of all-copper records; without a pump in
                    # this loop the marquee freezes for the duration of
                    # one layer's emit. Throttled, so overhead stays
                    # near-zero.
                    self._pump_busy_ui()
                    # Editor-mode connectivity focus: copper not connected
                    # to the current selection recedes toward the
                    # background (mirrors the heatmap mesh dimming). The
                    # dim choice is per-polygon — a record can hold both
                    # highlighted and dimmed pieces when the user picked
                    # one piece of unnamed copper but other disjoint
                    # ``"(none)"`` pieces share the same record.
                    for poly in rec.get("polygons", []):
                        if focus_on and self._editor_focus_hides(
                                net, rec_layer_id, poly):
                            continue
                        rgb = self._editor_dim_rgb(
                            lrgb, net, rec_layer_id, poly)
                        if solid:
                            _emit(self._triangulate_stub(poly), z, rgb,
                                  alpha=layer_alpha, **ac_bucket)
                        else:
                            _emit(self._copper_poly_wire(poly), z, rgb,
                                  alpha=layer_alpha, **ac_bucket)

        # FEM mesh failures — bright marker at the reported problem site.
        # Drawn on top of everything so it stays visible even when the
        # solve aborted and there is no heatmap mesh (stub viewer).
        for rec in md.get("mesh_failures") or []:
            center = _mesh_failure_marker_xy(rec)
            if center is None:
                continue
            cx, cy = center
            lid = rec.get("layer_id", -1)
            z = 0.0
            if isinstance(lid, int) and lid >= 0:
                for phys, plid in self._phys_name_to_layer_id.items():
                    if plid == lid:
                        z = self._layer_z_for(phys)
                        break
            if not in_2d:
                bucket: dict[str, bool] = {}
            else:
                bucket = {"top": True}
            _emit(
                _overlay_fan_tris(
                    _overlay_circle_ring(cx, cy, _MESH_FAILURE_MARKER_RADIUS_MM, 24),
                ),
                z, (1.0, 0.15, 0.15), alpha=0.85, **bucket,
            )
            _emit(
                _overlay_outline_tris(
                    _overlay_circle_ring(cx, cy, _MESH_FAILURE_RING_RADIUS_MM, 32),
                    0.2, closed=True,
                ),
                z, (1.0, 1.0, 0.0), alpha=1.0, **bucket,
            )
            # Crosshair arms so the spot is obvious at any zoom.
            arm = _MESH_FAILURE_RING_RADIUS_MM * 1.6
            for seg in (
                [(cx - arm, cy), (cx + arm, cy)],
                [(cx, cy - arm), (cx, cy + arm)],
            ):
                _emit(
                    _overlay_outline_tris(seg, 0.15, closed=False),
                    z, (1.0, 1.0, 0.0), alpha=1.0, **bucket,
                )

        # Capacitor loop-inductance overlay (Capacitors tab → "Show on
        # heatmap"). A feature overlay like the via cylinders — it colours
        # cap footprints by their loop L and does NOT touch the heatmap's
        # own scalar field or its ScaleController.
        self._emit_cap_inductance_overlay(_emit, labels, in_2d, feature_z)

        # Concatenate under-mesh chunks first so the GL viewer can draw
        # that leading slice before the heatmap mesh.
        # ``under_*`` (drawn before the mesh, 2D only): bottom-side board
        # features + every layer's all-copper geometry — having all-copper
        # paint before the mesh lets the rail mesh sit visually on top of
        # both its own layer's other-net copper and any lower-layer
        # all-copper. ``over_*`` (drawn after the mesh): vias and, in 3D,
        # every feature (depth test handles ordering there). ``top_*`` (2D
        # only): top-side board features so they sit on top of all copper.
        all_tri = under_tri + over_tri + top_tri
        if all_tri:
            under_count = sum(c.shape[0] for c in under_tri)
            gv.set_overlay_fills(
                np.concatenate(all_tri, axis=0),
                np.concatenate(under_col + over_col + top_col, axis=0),
                under_mesh_count=under_count)
        else:
            gv.clear_overlay_fills()
        if getattr(self, "via_span_box", None) is not None \
                and self.via_span_box.isChecked():
            labels.extend(self._build_via_span_labels(rail_members))
        if labels:
            gv.set_overlay_labels(labels)
        else:
            gv.clear_overlay_labels()
        # Build succeeded and the batch is uploaded — record its signature so
        # the next identical refresh is an identity-skip (finding 3.8).
        self._overlay_geom_sig = sig

    def _copper_poly_wire(self, poly: dict) -> np.ndarray:
        """Mitered wire-mesh outline triangles for one all-copper polygon
        (its exterior ring plus any holes). Cached on the polygon dict so
        repeated renders just re-upload the prebuilt array."""
        cached = poly.get("_wire_cache")
        if cached is not None:
            return cached
        chunks: list[np.ndarray] = []
        ext = poly.get("exterior")
        if ext is not None and len(ext) >= 2:
            chunks.append(_overlay_outline_tris(
                ext, _OVERLAY_WIRE_HALF_MM, closed=True))
        for hole in poly.get("holes", []) or []:
            if hole is not None and len(hole) >= 2:
                chunks.append(_overlay_outline_tris(
                    hole, _OVERLAY_WIRE_HALF_MM, closed=True))
        arr = (np.concatenate(chunks, axis=0) if chunks
               else np.empty((0, 2), dtype=np.float32))
        poly["_wire_cache"] = arr
        return arr

    def _designator_stroke_tris(self, rec: dict, *,
                                solid: bool = True) -> np.ndarray:
        """Triangles for one designator's stroke-font geometry — each glyph
        stroke as a thin round-capped ribbon at the text's stroke width.

        ``solid`` draws each stroke as a filled ribbon; otherwise each
        stroke is traced as its hollow perimeter (the wire-mesh fill
        style). Both variants are cached separately on the record dict
        (the layout is static for the session)."""
        cache_key = "_des_tris" if solid else "_des_tris_wire"
        cached = rec.get(cache_key)
        if cached is not None:
            return cached
        half = max(0.5 * float(rec.get("stroke_width_mm", 0.0) or 0.0), 0.01)
        chunks: list[np.ndarray] = []
        for pl in rec.get("polylines") or []:
            if len(pl) < 2:
                continue
            if solid:
                # Circular-pen model: a round nib of radius `half` swept
                # along the glyph. Each segment is its own straight quad and
                # every vertex carries a full disc, so joins and ends are
                # round. A single mitered ribbon along the whole polyline
                # left whisker spikes at the near-hairpin corners of the
                # angular Default stroke font; per-segment + per-vertex discs
                # reproduce exactly how Altium strokes the font.
                for i in range(len(pl) - 1):
                    chunks.append(_overlay_outline_tris(
                        (pl[i], pl[i + 1]), half, closed=False))
                for vx, vy in pl:
                    chunks.append(_overlay_fan_tris(_overlay_circle_ring(
                        float(vx), float(vy), half, 8)))
            else:
                chunks.append(_overlay_ribbon_outline_tris(
                    pl, half, _OVERLAY_WIRE_HALF_MM, closed=False))
        arr = (np.concatenate(chunks, axis=0) if chunks
               else np.empty((0, 2), dtype=np.float32))
        rec[cache_key] = arr
        return arr
