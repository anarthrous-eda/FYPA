"""The Vias tab and its Go jump action."""
from __future__ import annotations



class _ViasTabMixin:
    """The Vias tab and its Go jump action."""

    # --- Vias tab ------------------------------------------------------------

    # Columns of the Vias-tab table. (display label, numeric?)
    # Column 0 is the per-row "Go" jump button (populated via
    # ``setCellWidget``); the rest are normal text/numeric cells.
    _VIAS_TABLE_COLUMNS: tuple[tuple[str, bool], ...] = (
        ("",                 False),
        ("Net",              False),
        ("Layer span",       False),
        ("X (mm)",           True),
        ("Y (mm)",           True),
        ("Diameter (mm)",    True),
        # IPC-4761 protection / fill — e.g. "—" (none / unprotected),
        # "V (fill) · Copper", "VII (fill + cap) · Silver Epoxy".
        # A "·" suffix marks vias treated as conductively filled (their
        # per-hop R uses the parallel wall+fill model).
        ("IPC-4761 fill",    False),
        ("V top (V)",        True),
        ("V bottom (V)",     True),
        ("|ΔV| (mV)",        True),
        ("|I| max (A)",      True),
        ("Power (mW)",       True),
    )

    # --- Vias-tab "Go" jump action -----------------------------------------

    # Default world half-width of the zoom-in view when the user jumps to
    # a via, in mm. Picked to comfortably show the via + its immediate
    # surroundings on a typical-density board.
    _JUMP_HALF_WIDTH_MM: float = 5.0

    def _jump_to_via(self, row: dict) -> None:
        """Switch to the Heatmap tab, ensure at least one of the via's
        spanning physical layers is visible, zoom in on the via, and
        drop a yellow highlight ring at its location."""
        self._jump_to_xy(row.get("x_mm"), row.get("y_mm"),
                         row.get("layer_ids"))

    def _jump_to_node(self, row: dict) -> None:
        """Nodes-tab "Go" action — same as :meth:`_jump_to_via` but for a
        directive pin, which sits on a single physical layer."""
        lid = row.get("layer_id")
        self._jump_to_xy(row.get("x_mm"), row.get("y_mm"),
                         [lid] if lid is not None else [])

    def _jump_to_xy(self, x, y, layer_ids) -> None:
        """Switch to the Heatmap tab, ensure at least one of the given
        physical layers is visible, zoom in on ``(x, y)``, and drop a
        yellow highlight ring at that location. Shared by the Vias-tab
        and Nodes-tab "Go" actions."""
        if x is None or y is None:
            return

        # Make sure at least one of the location's physical layers is
        # checked in the layer list. If none are, tick the topmost in
        # the span (typically the user wants to see the layer the trace
        # enters from).
        layer_ids = layer_ids or []
        if layer_ids:
            id_to_phys = {v: k for k, v in self._phys_name_to_layer_id.items()}
            phys_in_span = [id_to_phys[lid] for lid in layer_ids
                            if lid in id_to_phys]
            if phys_in_span:
                visible = set(self._visible_layers())
                if not (visible & set(phys_in_span)):
                    # Topmost in stackup wins (lowest rank). emit=False to
                    # suppress the eye's signal — we render explicitly below.
                    choice = min(phys_in_span,
                                  key=lambda p: self._phys_stackup_rank.get(p, 0))
                    self._set_layer_visible(choice, True, emit=False)

        # Compute zoom: pick mm/pixel so the highlighted region spans
        # the smaller widget dimension comfortably.
        widget_w = max(1, self._gl_viewer.width() if self._gl_viewer else 800)
        widget_h = max(1, self._gl_viewer.height() if self._gl_viewer else 600)
        half = self._JUMP_HALF_WIDTH_MM
        mm_per_pixel = (2.0 * half) / min(widget_w, widget_h)

        # Stash the highlight, switch tabs, re-render, then set view.
        # Re-render first so the layer change + markers are applied,
        # THEN move the view so the GL viewer is sized correctly.
        self._highlight_via_xy = (float(x), float(y))
        self.tabs.setCurrentIndex(self._heatmap_tab_index)
        self._render()
        self._gl_viewer.set_view_center_scale(float(x), float(y), mm_per_pixel)
