"""The Setup tab."""
from __future__ import annotations

from PySide6.QtWidgets import QTextBrowser, QVBoxLayout, QWidget

from fypa.viewer.display import _FILL_MODE_REPORT_LABELS
from fypa.viewer.tabs.help import _HELP_SECTIONS, _help_tab_html
from fypa.viewer.theme import _T, current_theme
from fypa.viewer.widgets import _contrasting_text_color, _esc


# --- Setup-tab HTML formatter (module-level, no Qt deps) ---------------------

def _format_setup_html(solution, metadata: dict | None,
                       expanded_directives: set[str] | frozenset[str] = frozenset(),
                       *,
                       phys_color_fn=None,
                       ) -> str:
    """Render the metadata bundle as a single HTML document for QTextBrowser.

    ``expanded_directives`` is the set of channel-aware directive labels
    ("U5" for legacy single-channel, "U5#1" for indexed multi-channel)
    whose terminal-pin tables should be shown. Any directive NOT in this
    set is rendered collapsed (heading only, click to expand).

    Falls back to a "metadata not available — re-solve with the current
    version" notice when ``metadata is None`` (legacy pickle).
    """
    from fypa.viewer.window import PdnViewer
    if metadata is None:
        return (
            "<h2>Setup metadata not available</h2>"
            "<p>This solution pickle was saved with an older version of the "
            "tool that did not bundle setup metadata. To populate this tab, "
            "re-run the solver:</p>"
            "<pre>python FYPA.py solve YOUR.PrjPcb output.pkl</pre>"
            "<p>and then re-open the result with <code>show</code>.</p>"
        )

    parts: list[str] = []
    # Self-contained styling — colours come from the active theme dict so
    # the Setup tab tracks dark / light mode the same as the rest of the UI.
    _t = current_theme()
    parts.append(
        "<style>"
        f"body {{ font-family: Segoe UI, sans-serif; font-size: 11pt;"
        f"       color: {_t['fg']}; background-color: {_t['bg']}; }}"
        f"h2 {{ margin-top: 18px; color: {_t['fg_strong']};"
        f"     border-bottom: 1px solid {_t['border']}; padding-bottom: 2px; }}"
        f"h3 {{ margin-top: 14px; color: {_t['accent']}; }}"
        f"p  {{ color: {_t['fg']}; }}"
        f"table {{ border-collapse: collapse; margin: 6px 0;"
        f"        color: {_t['fg']}; background-color: {_t['bg']}; }}"
        f"th, td {{ border: 1px solid {_t['border']}; padding: 4px 8px;"
        f"         text-align: left; color: {_t['fg']}; }}"
        f"th {{ background-color: {_t['bg_header']}; color: {_t['fg_strong']};"
        f"     font-weight: 600; }}"
        f"td.num {{ text-align: right;"
        f"         font-family: Consolas, monospace; color: {_t['fg']}; }}"
        f"code {{ background-color: {_t['bg_input']}; color: {_t['code']};"
        f"       padding: 1px 4px; border-radius: 3px; }}"
        f".muted {{ color: {_t['fg_dim']}; }}"
        f".warn  {{ color: {_t['warn']}; }}"
        f".err   {{ color: {_t['err']}; }}"
        f"li {{ color: {_t['fg']}; }}"
        "</style>"
    )

    parts.append("<h2>Project</h2>")
    parts.append(f"<p><b>{_esc(metadata.get('project_name', '?'))}</b><br>"
                 f"<span class='muted'>{_esc(metadata.get('prjpcb_path', ''))}</span></p>")

    ex = metadata.get("extraction_summary", {})
    if ex:
        parts.append("<h3>Extracted records</h3>")
        parts.append("<table>"
                     f"<tr><th>tracks</th><td class='num'>{ex.get('tracks', 0):,}</td>"
                     f"<th>arcs</th><td class='num'>{ex.get('arcs', 0):,}</td>"
                     f"<th>vias</th><td class='num'>{ex.get('vias', 0):,}</td></tr>"
                     f"<tr><th>pads</th><td class='num'>{ex.get('pads', 0):,}</td>"
                     f"<th>regions</th><td class='num'>{ex.get('regions', 0):,}</td>"
                     f"<th>nets</th><td class='num'>{ex.get('nets', 0):,}</td></tr>"
                     f"<tr><th>pcb components</th><td class='num'>{ex.get('pcb_components', 0):,}</td>"
                     f"<th>sch components</th><td class='num'>{ex.get('sch_components', 0):,}</td>"
                     f"<th>enabled cu layers</th><td class='num'>{len(metadata.get('enabled_copper_layer_ids', []))}</td></tr>"
                     "</table>")

    # Stackup
    stackup = metadata.get("stackup", [])
    if stackup:
        parts.append("<h2>Copper stackup</h2>")
        parts.append("<p class='muted'>Conductance is computed per layer as "
                     "<code>copper_thickness_mm &times; conductivity_S_per_mm</code>.</p>")
        parts.append("<table>"
                     "<tr><th>id</th><th>Name</th>"
                     "<th>Cu thickness</th><th>(mil)</th><th>(oz)</th>"
                     "<th>Dielectric below</th>"
                     "<th>Sheet conductance</th><th>Sheet resistance</th>"
                     "<th>Notes</th></tr>")
        for row in stackup:
            notes = []
            if row.get("is_plane"):
                notes.append(f"PLANE on net {row.get('plane_net_name') or '?'}")
            diel_mm = row.get("dielectric_thickness_mm", 0.0) or 0.0
            diel_cell = (f"{diel_mm*1000:.1f} µm" if diel_mm > 0
                         else "<span class='muted'>—</span>")
            # Tint the id cell with the physical-layer swatch used in the
            # Heatmap tab so users can cross-reference at a glance.
            bg = phys_color_fn(row["name"]) if phys_color_fn else None
            if bg:
                fg = _contrasting_text_color(bg)
                id_cell = (f"<td class='num' style='background-color:{bg};"
                           f" color:{fg}; font-weight:bold;'>{row['layer_id']}</td>")
            else:
                id_cell = f"<td class='num'>{row['layer_id']}</td>"
            parts.append("<tr>"
                         f"{id_cell}"
                         f"<td>{_esc(row['name'])}</td>"
                         f"<td class='num'>{row['copper_thickness_mm']*1000:.3f} µm</td>"
                         f"<td class='num'>{row['copper_thickness_mil']:.3f}</td>"
                         f"<td class='num'>{row['copper_thickness_oz']:.3f}</td>"
                         f"<td class='num'>{diel_cell}</td>"
                         f"<td class='num'>{row['sheet_conductance_S']:.3f} S/sq</td>"
                         f"<td class='num'>{row['sheet_resistance_milliohm_per_sq']:.4f} mΩ/sq</td>"
                         f"<td>{_esc(', '.join(notes))}</td>"
                         "</tr>")
        parts.append("</table>")

    # Physics constants
    phys = metadata.get("physics_constants", {})
    if phys:
        # Per-hop via R varies; summarise the distribution from each via's
        # segments list so users can see the actual range the FEM used.
        seg_rs: list[float] = []
        cond_fill_count = 0
        for v in metadata.get("vias", []):
            if v.get("is_conductive_fill"):
                cond_fill_count += 1
            for seg in v.get("segments") or []:
                r = seg.get("resistance_ohm")
                if r is not None and r > 0.0:
                    seg_rs.append(float(r))
        if seg_rs:
            r_min = min(seg_rs) * 1000.0
            r_max = max(seg_rs) * 1000.0
            r_mean = (sum(seg_rs) / len(seg_rs)) * 1000.0
            via_r_cell = (f"min {r_min:.3f} / mean {r_mean:.3f} / "
                          f"max {r_max:.3f} mΩ "
                          f"<span class='muted'>(over {len(seg_rs)} segment(s))</span>")
        else:
            via_r_cell = (f"<span class='muted'>(no via segments; fallback "
                          f"= {phys.get('fallback_via_resistance_ohm', 0)*1000:.3f} mΩ)</span>")
        parts.append("<h2>Physics constants</h2>")
        parts.append("<table>"
                     f"<tr><th>Copper conductivity</th>"
                     f"<td class='num'>{phys.get('copper_conductivity_S_per_mm', 0):.3e} S/mm</td>"
                     f"<td class='muted'>= {phys.get('copper_resistivity_microohm_cm', 0):.4f} µΩ·cm "
                     f"= {phys.get('copper_resistivity_ohm_m', 0):.3e} Ω·m</td></tr>"
                     f"<tr><th>Plating thickness</th>"
                     f"<td class='num'>{phys.get('plating_thickness_mm', 0)*1000:.1f} µm</td>"
                     f"<td class='muted'>Standard plated-through-hole copper "
                     f"wall thickness (IPC-A-600 Class 2).</td></tr>"
                     f"<tr><th>Via barrel resistance (per hop)</th>"
                     f"<td>{via_r_cell}</td>"
                     f"<td class='muted'>{_esc(phys.get('note_via_resistance', ''))}</td></tr>"
                     f"<tr><th>Conductive fill resistivity</th>"
                     f"<td class='num'>{phys.get('conductive_fill_resistivity_ohm_mm', 0)*1.0e3:.3g} mΩ·mm</td>"
                     f"<td class='muted'>Applied as a parallel rod inside the "
                     f"plated barrel for filled vias "
                     f"({_esc(_FILL_MODE_REPORT_LABELS.get(phys.get('conductive_fill_mode', 'auto'), 'auto'))}) — "
                     f"<b>{cond_fill_count}</b> via(s) on this board.</td></tr>"
                     f"<tr><th>Multi-pin coupling resistance</th>"
                     f"<td class='num'>{phys.get('coupling_resistance_ohm', 0)*1000:.3f} mΩ</td>"
                     f"<td class='muted'>{_esc(phys.get('note_coupling_resistance', ''))}</td></tr>"
                     f"<tr><th>Area-weighted pin coupling</th>"
                     f"<td class='num'>"
                     f"{'on' if phys.get('area_weighted_pin_coupling') else 'off'}"
                     f"</td>"
                     f"<td class='muted'>When on, each multi-pin star R scales "
                     f"as R ∝ 1/pad area (supply and GND).</td></tr>"
                     "</table>")

    # Directives — each heading is a clickable toggle (collapsed by default).
    # Synthetic AUTO_BRIDGE records are excluded: they carry no annotation
    # the user wrote, and the "Bridged / shorted nets" table above already
    # lists each one. Counting them here inflates the number people read as
    # "how many parts did I annotate".
    directives = [d for d in metadata.get("directives", [])
                  if d.get("role") != "AUTO_BRIDGE"]
    parts.append(f"<h2>PDN directives <span class='muted'>({len(directives)} — click a heading to expand)</span></h2>")
    if not directives:
        parts.append("<p class='warn'>No directives parsed — nothing to solve.</p>")
    for d in directives:
        desig = d.get("designator", "?")
        # ``label`` (e.g. "U5#1") disambiguates multi-channel SOURCE/SINK
        # so two channels on the same part get independent expand-state.
        toggle_key = str(d.get("label") or desig)
        is_open = toggle_key in expanded_directives
        arrow = "&#9662;" if is_open else "&#9656;"  # ▼ / ▶
        # The heading itself is an anchor; the viewer's anchorClicked handler
        # intercepts ``toggle:<label>`` URLs and re-renders.
        parts.append(
            f"<h3 style='margin: 8px 0;'>"
            f"<a href='toggle:{_esc(toggle_key)}' "
            f"style='color:{_t['accent']}; text-decoration:none; font-weight:600;'>"
            f"{arrow} {_esc(d.get('role','?'))} on {_esc(toggle_key)}"
            f"</a> &nbsp;"
            f"<span class='muted'>({_esc(d.get('schdoc',''))})</span> &nbsp;"
            f"<span style='color:{_t['fg']};'>{_esc(d.get('value_str',''))}</span>"
            f"</h3>"
        )
        if not is_open:
            continue
        terms = d.get("terminals", {})
        if not terms:
            continue
        parts.append("<table>"
                     "<tr><th>Terminal</th><th>Pin</th><th>Net</th>"
                     "<th>Layer</th><th>X (mm)</th><th>Y (mm)</th></tr>")
        for term_name, term in terms.items():
            pins = term.get("pins", [])
            if not pins:
                parts.append(f"<tr><td>{_esc(term_name)}</td>"
                             "<td colspan='5' class='warn'>(no pins resolved)</td></tr>")
            else:
                # Show the net the directive named (PDN_*_NET). When local sheet
                # label resolution maps the name to pads on a different PCB net,
                # keep the named net as the headline and note the actual pad net.
                req_net = term.get("requested_net")
                for i, pin in enumerate(pins):
                    actual_net = pin.get('net', '')
                    if req_net and actual_net and actual_net != req_net:
                        net_cell = (f"<code>{_esc(req_net)}</code> "
                                    f"<span class='muted'>(via "
                                    f"{_esc(actual_net)})</span>")
                    else:
                        net_cell = f"<code>{_esc(req_net or actual_net)}</code>"
                    parts.append("<tr>"
                                 f"<td>{_esc(term_name) if i == 0 else ''}</td>"
                                 f"<td>{_esc(PdnViewer._pin_display_pad(pin))}</td>"
                                 f"<td>{net_cell}</td>"
                                 f"<td class='num'>{pin.get('layer_id','')}</td>"
                                 f"<td class='num'>{pin.get('x_mm', 0):.3f}</td>"
                                 f"<td class='num'>{pin.get('y_mm', 0):.3f}</td>"
                                 "</tr>")
        parts.append("</table>")

    # Active nets list
    active = metadata.get("active_nets", [])
    if active:
        parts.append("<h2>Active nets</h2>")
        parts.append("<p class='muted'>Only these nets have PDN parameter data set; "
                     "other nets are excluded from the FEM.</p>")
        parts.append("<p>" + ", ".join(f"<code>{_esc(n)}</code>" for n in active) + "</p>")

    # FEM stats
    fem = metadata.get("fem_stats", {}) or {}
    mesher = metadata.get("mesher_config") or {}
    solver = metadata.get("solver_stats") or {}
    if fem or mesher or solver:
        parts.append("<h2>FEM &amp; solver</h2>")
        parts.append("<table>")
        if mesher:
            parts.append(f"<tr><th>Mesher min angle</th><td class='num'>{mesher.get('minimum_angle_deg', 0):.1f}°</td></tr>"
                         f"<tr><th>Mesher max size</th><td class='num'>{mesher.get('maximum_size_mm', 0):.3f} mm</td></tr>")
        if fem:
            parts.append(f"<tr><th>Layers</th><td class='num'>{fem.get('padne_layer_count', 0)}</td></tr>"
                         f"<tr><th>Networks (total)</th><td class='num'>{fem.get('padne_network_count', 0)}</td></tr>"
                         f"<tr><th>Via coupling networks</th><td class='num'>{fem.get('via_coupling_network_count', 0)}</td></tr>")
        if solver:
            res = solver.get("residual_norm", 0.0)
            gnd = solver.get("ground_node_current_A", 0.0)
            res_flag = " class='err'" if res > 1.0 else (" class='warn'" if res > 1e-3 else "")
            gnd_flag = " class='err'" if abs(gnd) > 0.1 else (" class='warn'" if abs(gnd) > 1e-3 else "")
            parts.append(f"<tr><th>Solver residual ‖L·v − r‖</th><td{res_flag} class='num'>{res:.3e}</td></tr>"
                         f"<tr><th>Ground-node current</th><td{gnd_flag} class='num'>{gnd*1000:.4f} mA "
                         f"<span class='muted'>(should be ≈ 0 for a well-posed problem)</span></td></tr>")
        parts.append("</table>")

    # Bridged / shorted nets. These parts are electrically a piece of metal
    # (a Net Tie, a 0 Ω resistor, a wire jumper, or a SERIES the user gave a
    # sub-milliohm value), so the loader merges the two nets into one rail and
    # re-inserts the physical link as a same-net bridge resistor. That merge
    # is invisible everywhere else — after it, both pads report the surviving
    # net name — so this is the one place the user can see which rails were
    # tied together and by what.
    bridges = metadata.get("merged_bridges") or []
    if bridges:
        parts.append("<h2>Bridged / shorted nets</h2>")
        parts.append(
            "<p class='muted'>These parts join two nets with (near-)zero "
            "resistance, so FYPA solves them as a single rail and models the "
            "link itself as a bridge resistor at the pads below. A part here "
            "was <b>not</b> annotated by you \u2014 it was inferred from its "
            "Altium ComponentKind, value or footprint. To model a real "
            "resistance instead, annotate it with "
            "<code>PDN_ROLE=SERIES</code> and <code>PDN_R</code>.</p>")
        parts.append(
            "<table><tr><th>Part</th><th>Shorted nets</th>"
            "<th>Solved as</th><th>Bridge R</th><th>Location</th></tr>")
        for b in bridges:
            p_net, n_net = b.get("p_net", "?"), b.get("n_net", "?")
            shorted = (f"{_esc(p_net)} \u2194 {_esc(n_net)}"
                       if p_net != n_net else _esc(p_net))
            parts.append(
                f"<tr><th>{_esc(b.get('designator', '?'))}</th>"
                f"<td>{shorted}</td>"
                f"<td>{_esc(b.get('canonical_net', '?'))}</td>"
                f"<td class='num'>{_esc(b.get('resistance_str', ''))}</td>"
                f"<td class='num'>({b.get('p_x_mm', 0.0):.3f}, "
                f"{b.get('p_y_mm', 0.0):.3f}) mm</td></tr>")
        parts.append("</table>")

    # Warnings + errors
    warnings = metadata.get("annotation_warnings") or []
    errors = metadata.get("annotation_errors") or []
    if warnings or errors:
        parts.append("<h2>Annotation log</h2>")
        if errors:
            parts.append("<h3 class='err'>Errors</h3><ul>")
            for e in errors:
                parts.append(f"<li class='err'>{_esc(e)}</li>")
            parts.append("</ul>")
        if warnings:
            parts.append("<h3 class='warn'>Warnings</h3><ul>")
            for w in warnings:
                parts.append(f"<li class='warn'>{_esc(w)}</li>")
            parts.append("</ul>")

    return "".join(parts)


class _SetupTabMixin:
    """The Setup tab."""

    # --- Setup tab ----------------------------------------------------------

    def _build_help_tab(self) -> QWidget:
        """Static HTML reference, one collapsible section per tab. Clicking a
        heading re-renders with that section toggled in / out of
        :attr:`_help_expanded` (all collapsed to start)."""
        widget = QWidget(self.tabs)
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(8, 8, 8, 8)

        self._help_expanded: set[str] = set()
        self.help_browser = QTextBrowser()
        self.help_browser.setOpenExternalLinks(False)
        self.help_browser.setOpenLinks(False)
        self.help_browser.anchorClicked.connect(self._on_help_anchor_clicked)
        _t = _T()
        self.help_browser.setStyleSheet(
            f"QTextBrowser {{ background-color: {_t['bg']}; color: {_t['fg']}; }}"
        )
        self.help_browser.setHtml(_help_tab_html(self._help_expanded))
        layout.addWidget(self.help_browser)
        return widget

    def _on_help_anchor_clicked(self, url) -> None:
        """Toggle a Help section (or all of them) and re-render in place."""
        href = url.toString()
        prefix = "toggle:"
        if not href.startswith(prefix):
            return
        key = href[len(prefix):]
        if key == "*":
            self._help_expanded = {title for title, _ in _HELP_SECTIONS}
        elif key == "-":
            self._help_expanded.clear()
        else:
            self._help_expanded ^= {key}
        scroll_bar = self.help_browser.verticalScrollBar()
        scroll_pos = scroll_bar.value()
        self.help_browser.setHtml(_help_tab_html(self._help_expanded))
        scroll_bar.setValue(scroll_pos)

    def _build_setup_tab(self) -> QWidget:
        """Build the Setup tab — a scrollable HTML view of everything the
        FEM was given. Helps users verify that copper thickness, conductivity,
        directive values, etc. match what they entered in Altium.

        Directive blocks render as collapsible sections; clicking the
        heading re-renders the HTML with that designator toggled into / out
        of :attr:`_expanded_directives`.
        """
        widget = QWidget(self.tabs)
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(8, 8, 8, 8)

        self.setup_browser = QTextBrowser()
        self.setup_browser.setOpenExternalLinks(False)
        # `setOpenLinks(False)` lets `anchorClicked` fire without QTextBrowser
        # trying to navigate to a (nonexistent) URL.
        self.setup_browser.setOpenLinks(False)
        self.setup_browser.anchorClicked.connect(self._on_setup_anchor_clicked)
        # Pin the widget palette to the active theme so scrollbar gutter
        # etc. don't clash with the HTML body's background.
        _t = _T()
        self.setup_browser.setStyleSheet(
            f"QTextBrowser {{ background-color: {_t['bg']}; color: {_t['fg']}; }}"
        )
        self._refresh_setup_html()
        layout.addWidget(self.setup_browser)
        return widget

    def _refresh_setup_html(self) -> None:
        """Re-render the Setup tab HTML, preserving the scroll position so
        toggling a directive doesn't jump the view."""
        scroll_bar = self.setup_browser.verticalScrollBar()
        scroll_pos = scroll_bar.value()
        self.setup_browser.setHtml(_format_setup_html(
            self.solution, self.metadata, self._expanded_directives,
            phys_color_fn=self._layer_color_for,
        ))
        scroll_bar.setValue(scroll_pos)
