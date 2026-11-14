"""The Help tab content."""
from __future__ import annotations

from fypa.viewer.theme import current_theme


def _help_tab_style() -> str:
    """Build the Help-tab inline <style> block using the active theme."""
    t = current_theme()
    return (
        "<style>"
        f"  body {{ font-family: Segoe UI, sans-serif; font-size: 11pt;"
        f"         color: {t['fg']}; background-color: {t['bg']}; }}"
        f"  h2 {{ margin-top: 18px; color: {t['fg_strong']};"
        f"       border-bottom: 1px solid {t['border']}; padding-bottom: 2px; }}"
        f"  h3 {{ margin-top: 14px; color: {t['accent']}; }}"
        f"  p, li {{ color: {t['fg']}; }}"
        f"  table {{ border-collapse: collapse; margin: 6px 0;"
        f"          color: {t['fg']}; background-color: {t['bg']}; }}"
        f"  th, td {{ border: 1px solid {t['border']}; padding: 4px 10px;"
        f"           text-align: left; vertical-align: top; }}"
        f"  th {{ background-color: {t['bg_header']}; color: {t['fg_strong']}; font-weight: 600; }}"
        f"  kbd {{ background-color: {t['bg_input']}; color: {t['code']};"
        f"        border: 1px solid {t['border']}; border-radius: 3px;"
        f"        padding: 1px 6px; font-family: Consolas, monospace; font-size: 10pt; }}"
        f"  .muted {{ color: {t['fg_dim']}; }}"
        "</style>"
    )




# Top-level sections, one per viewer tab in tab-strip order. Each renders
# as a collapsible heading - see :func:`_help_tab_html`.
_HELP_SECTIONS: tuple[tuple[str, str], ...] = (
    ("Heatmap", """
<p>The interactive viewport.</p>

<h3>Keyboard shortcuts</h3>
<table>
  <tr><th>Key</th><th>Action</th></tr>
  <tr><td><kbd>2</kbd></td><td>Switch to 2D mode <span class='muted'>(re-fits to data)</span></td></tr>
  <tr><td><kbd>3</kbd></td><td>Switch to 3D mode <span class='muted'>(re-fits to data)</span></td></tr>
  <tr><td><kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>2</kbd></td><td>Switch to 2D <i>keeping the current view</i></td></tr>
  <tr><td><kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>3</kbd></td><td>Switch to 3D <i>keeping the current view</i> (top-down entry)</td></tr>
  <tr><td><kbd>0</kbd></td><td>Reset the 3D view (top-down, refit to data) <span class='muted'>- 3D mode only</span></td></tr>
  <tr><td><kbd>Z</kbd></td><td>Cycle <i>Relief view</i>: off &rarr; per layer &rarr; ignoring layers <span class='muted'>(from 2D, switches to 3D with per-layer relief)</span></td></tr>
  <tr><td><kbd>O</kbd></td><td>Toggle <i>Show layer outlines</i></td></tr>
  <tr><td><kbd>T</kbd></td><td>Toggle <i>Show cursor tooltip</i></td></tr>
  <tr><td><kbd>A</kbd></td><td>Toggle <i>Show current arrows</i></td></tr>
  <tr><td><kbd>V</kbd></td><td>Toggle <i>Heatmap vias/PTH</i> <span class='muted'>- 3D mode only</span></td></tr>
  <tr><td><kbd>M</kbd></td><td>Cycle <i>Mode</i> forward (Voltage &rarr; Voltage Drop &rarr; &hellip;)</td></tr>
  <tr><td><kbd>Shift</kbd>+<kbd>M</kbd></td><td>Cycle <i>Mode</i> backward</td></tr>
  <tr><td><kbd>H</kbd></td><td>Cycle <i>colour scheme</i> forward (Viridis &rarr; Blue&nbsp;&rarr;&nbsp;Red &rarr; &hellip;)</td></tr>
  <tr><td><kbd>Shift</kbd>+<kbd>H</kbd></td><td>Cycle <i>colour scheme</i> backward</td></tr>
  <tr><td><kbd>B</kbd></td><td>Collapse / expand the side panel</td></tr>
</table>
<p class='muted'>Shortcuts are window-scoped - they fire when the viewer
window has focus but defer to text inputs (e.g. the Min/Max boxes)
when one of those has focus. The editor-mode keys
(<kbd>E</kbd>, <kbd>S</kbd>, <kbd>L</kbd>, <kbd>Delete</kbd>, undo / redo)
are listed under <i>Editor mode</i> below.</p>

<h3>Mouse controls</h3>
<table>
  <tr><th>Gesture</th><th>Action</th></tr>
  <tr><td>Right-button drag</td><td>Pan the view</td></tr>
  <tr><td>Mouse wheel</td><td>Zoom in / out (around cursor in 2D, dolly camera in 3D)</td></tr>
  <tr><td>Middle-button drag &uarr;/&darr;</td><td>Exponential zoom - drag up = zoom in, down = zoom out</td></tr>
  <tr><td>Left click</td><td>Clear the yellow jump highlight (from a Vias/Nodes-tab Go)</td></tr>
</table>

<h3>Voltage / Voltage Drop mode only <span class='muted'>(2D only)</span></h3>
<table>
  <tr><th>Gesture</th><th>Action</th></tr>
  <tr><td>Hold <kbd>Shift</kbd></td><td>Anchor a voltage probe at the cursor and draw a thin white
    line from there to the live mouse position. The probe bar gains a
    <code>Difference = X V</code> readout - the live cursor's voltage
    minus the anchor's. Press only takes effect when the cursor is
    over copper that has a voltage value; release <kbd>Shift</kbd> to
    clear the line.</td></tr>
</table>

<h3>3D mode only</h3>
<table>
  <tr><th>Gesture</th><th>Action</th></tr>
  <tr><td><kbd>Shift</kbd> + right-button drag</td><td>Rotate (orbit the camera around the board centre)</td></tr>
</table>

<h3>Editor mode <span class='muted'>(2D only)</span></h3>
<table>
  <tr><th>Gesture / key</th><th>Action</th></tr>
  <tr><td><kbd>E</kbd></td><td>Enter / leave editor mode</td></tr>
  <tr><td><kbd>S</kbd></td><td>Drop a free <b>SOURCE</b> - same as the red triangle
      button at the top-left of the viewport. Then click copper to place it;
      press <kbd>S</kbd> again or <kbd>Esc</kbd> to cancel.</td></tr>
  <tr><td><kbd>L</kbd></td><td>Drop a free <b>SINK</b> - same as the blue triangle
      button at the top-left of the viewport. Then click copper to place it;
      press <kbd>L</kbd> again or <kbd>Esc</kbd> to cancel.</td></tr>
  <tr><td>Left click</td><td>Select the component, marker or copper under the cursor</td></tr>
  <tr><td>Left drag on a marker</td><td>Move that free marker (constrained to copper)</td></tr>
  <tr><td>Left drag on empty board</td><td>Rubber-band select every PDN marker <i>fully</i> inside the box. Copper and roleless parts are never selected.</td></tr>
  <tr><td><kbd>Shift</kbd> + drag / click</td><td>Add to the selection</td></tr>
  <tr><td><kbd>Ctrl</kbd> + drag / click</td><td>Remove from the selection</td></tr>
  <tr><td><kbd>Delete</kbd> / <kbd>Backspace</kbd></td><td>Remove the selected marker(s)</td></tr>
  <tr><td><kbd>Ctrl</kbd>+<kbd>Z</kbd> / <kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>Z</kbd></td><td>Undo / redo the last editor edit. A multi-sink Apply or Remove undoes as one step.</td></tr>
</table>
<p class='muted'>Select two or more markers that are all SINKs and the
side panel shows the ordinary sink form, with <b>*</b> in every row the
selected sinks disagree on. Apply writes only the rows you edit, to all
of them - rows left showing <b>*</b> keep their per-sink values.
Current is per sink, so 2 A applied to four sinks adds 8 A of load. A
selection holding anything other than sinks offers no controls, because
no one set of properties applies across roles.</p>

<h3>3Dconnexion SpaceMouse</h3>
<ul>
  <li><b>Translation</b> - pan the view (2D and 3D)</li>
  <li><b>Translation Z</b> - zoom in / out</li>
  <li><b>Rotation</b> - orbit the camera (3D mode only)</li>
  <li><b>Menu / fit button</b> - fit board (2D) or reset 3D view</li>
  <li>Requires <b>3DxWare</b> on Windows/macOS
    (<code>uv sync --extra spacemouse</code>) or <b>spacenavd</b> on Linux
    (<code>sudo apt install spacenavd libspnav0</code>)</li>
  <li>Navigation stays active while FYPA is open so 3Dconnexion Settings
    keeps the FYPA profile selected</li>
</ul>

<h3>Side-panel controls</h3>
<ul>
  <li><b>Physical layers</b> - tick checkboxes to stack multiple
    copper layers in the view. Each layer has its own swatch colour
    used by the outline overlay.</li>
  <li><b>Rails</b> - tick one or more bridged rail groups to
    show their copper (e.g. <code>+3V3</code> bundles <code>+3V3</code>
    + <code>3V3_SW</code> if a series resistor / inductor links them).
    Use the <i>All Rails</i> row to toggle every rail at once.</li>
  <li><b>Mode</b> - Voltage / Voltage Drop / Current Density /
    Power Density.</li>
  <li><b>Layer outlines</b> - the contour button on the
    <i>All Rails</i> row (hotkey <kbd>O</kbd>) traces each visible rail's
    copper polygons in the owning layer's swatch colour.</li>
  <li><b>Transparency control</b> - the white-disc icon sits on
    every Physical layers row and every Board Features row; the
    <i>All Layers</i> row carries a master copy that fans its setting
    out to every physical layer at once. Each click removes a
    quarter-slice of the disc clockwise as the row's transparency
    rises. For a physical layer the transparency fades both the rail
    heatmap mesh AND the per-layer all-copper overlay; for a Board
    Features row it fades that overlay (silkscreen / pads / components
    / vias / designators / board outline).
    <table>
      <tr><th>Click</th><th>Effect</th></tr>
      <tr><td>Click</td>
          <td>+25 %  <span class='muted'>(coarse, wraps 100&nbsp;%&nbsp;&rarr;&nbsp;0&nbsp;%)</span></td></tr>
      <tr><td><kbd>Shift</kbd>+click <i>or</i> right-click</td>
          <td>&minus;25 %  <span class='muted'>(reverse, wraps 0&nbsp;%&nbsp;&rarr;&nbsp;100&nbsp;%)</span></td></tr>
      <tr><td><kbd>Alt</kbd>+click</td>
          <td>+12.5 %  <span class='muted'>(fine, stops at 100&nbsp;%)</span></td></tr>
      <tr><td><kbd>Alt</kbd>+<kbd>Shift</kbd>+click <i>or</i> <kbd>Alt</kbd>+right-click</td>
          <td>&minus;12.5 %  <span class='muted'>(fine reverse, stops at 0&nbsp;%)</span></td></tr>
    </table></li>
  <li><b>Show cursor tooltip</b> - a small tooltip follows the
    mouse, showing the value of the current mode at that point along
    with the net and layer. Same info as the probe bar under the plot.</li>
  <li><b>3D view</b> - perspective view of the stacked layers
    with via cylinders.</li>
  <li><b>Heatmap vias/PTH</b> - colour via and plated-through-hole
    cylinders by the active mode instead of solid orange / light grey.
    Voltage / Voltage Drop interpolate along the barrel's length; Current
    Density and Power Density are constant per inter-layer segment. 3D
    mode only.</li>
  <li><b>Layer spacing slider</b> - 3D only. Scales both the
    inter-layer separation and the via cylinder length.</li>
  <li><b>Relief view</b> - 3D only. Raises each point of the copper
    by its value on the colour scale, so the higher the voltage the
    higher it is drawn and voltage drop reads as a slope. The height
    follows the colour bar, so clamping Min / Max clamps it too. The
    <i>Relief height</i> slider sets the full-scale height as a percentage
    of the board's larger side. Two variants:
    <ul>
      <li><b>Per layer</b> - each layer rises from its own plane, so
        layers keep their stackup order.</li>
      <li><b>Ignoring layers</b> (relief view ignoring layer positions)
        - every layer rises from one base at the top copper layer, so
        a higher voltage is always drawn higher whatever layer it is on.
        Layers at the same value keep a tiny stackup-order offset so they
        don't flicker through each other. <i>Layer spacing</i> is hidden in
        this variant, and no-current stub copper sits on the base. The
        hover readout still names the layer.</li>
    </ul>
    Layer outlines (<kbd>O</kbd>) follow the raised copper edge, so the
    outline colour still shows which layer each piece of copper is on
    - the main cue in the ignoring-layers variant. Via barrels,
    current arrows and the mesh wireframe are hidden while relief is on
    (they sit on the flat layer planes); not available in Via Current
    mode.</li>
  <li><b>Show current arrows</b> - white arrows on a regular
    pixel-spaced grid showing the direction of current flow; shaft
    length scales with &radic;|J| so weak and strong currents are
    both visible. Re-sampled on zoom / rotate. Works in 2D and 3D
    (in 3D each arrow rides the top face of its layer).</li>
  <li><b>Arrow spacing (px)</b> - pixels between adjacent
    arrows on screen. Smaller = denser arrows.</li>
  <li><b>Colour scale</b> - the gradient strip is overlaid on the
    viewer's bottom-left corner (with value ticks); drag its Min / Max
    handles, type exact values in the side-panel boxes, or click the
    <b>&#8634;</b> reset button to restore the data range.</li>
</ul>
"""),
    ("Properties", """
<p>Everything that defines this design's solve. <b>Left - Base
properties:</b> what the real board is - physics (temperature, copper
resistivity, via plating, electro-thermal), conductive via fill and
per-layer copper thickness and resistivity, with mesh accuracy and the
capacitor loop-inductance knobs under a collapsed <b>Advanced</b> section.
Edited fields are outlined; <b>Re-run Solver</b> (or Resolve) replaces the
base result with one solved from the new values. To try alternatives
without changing the base, use What-Ifs. <b>Right:</b> a read-only
report of what the solver was given: extracted records, stackup, physics
constants, parsed PDN_* directives (collapsible), active nets, solver
diagnostics, warnings / errors.</p>
"""),
    ("Topology", """
<p>The power tree of the PDN simulation model (not the PCB layout). Each
source starts a row; each regulator gets its own row under the row that
feeds it, indented one step per conversion stage. Within a row power flows
left to right. A rail that continues on another row ends in a <b>tag</b>
instead of a long wire - click a tag to jump to the other end. Dashed
amber tags mark rails nothing drives; grey tags mark rails with no loads.
Each part has its own ground symbol.</p>
<p><b>Bridges</b> lists 0&nbsp;&Omega; auto-bridges on nets no source, sink
or regulator uses. <b>Classic</b> switches back to the single-chain column
layout.</p>
<p>Click a component box to open its PDN properties in the sidebar (the
same form as the Heatmap's editor panel); its &#8982; icon jumps to it on
the Heatmap. Click an amber or grey tag to add a source, regulator or sink
on that rail. <b>Auto setup&hellip;</b> proposes a whole setup from the
design and, optionally, a folder of datasheets, for you to review and apply.
<b>Manual setup&hellip;</b> lists every source, regulator and load to add or
edit by hand: pick the net, the part on it and its pins.
<b>&#8635; Resolve</b> re-solves with your changes.</p>

<h3>Keyboard shortcuts</h3>
<table>
  <tr><th>Key</th><th>Action</th></tr>
  <tr><td><kbd>Ctrl</kbd>+<kbd>+</kbd> / <kbd>Ctrl</kbd>+<kbd>&minus;</kbd></td>
      <td>Zoom in / out (viewport centre)</td></tr>
  <tr><td><kbd>Ctrl</kbd>+<kbd>0</kbd></td><td>Fit the whole diagram in view</td></tr>
  <tr><td>Arrow keys</td><td>Pan; <kbd>Shift</kbd> or <kbd>Ctrl</kbd> for larger steps</td></tr>
</table>

<h3>Mouse controls</h3>
<table>
  <tr><th>Gesture</th><th>Action</th></tr>
  <tr><td>Mouse wheel</td><td>Scroll vertically</td></tr>
  <tr><td><kbd>Shift</kbd> + mouse wheel</td><td>Scroll horizontally</td></tr>
  <tr><td><kbd>Ctrl</kbd> + mouse wheel</td><td>Zoom in / out around the cursor</td></tr>
  <tr><td><b>+</b> / <b>&minus;</b> / <b>Fit</b> toolbar buttons</td><td>Zoom in, zoom out, fit</td></tr>
  <tr><td>Middle-button drag</td><td>Pan</td></tr>
  <tr><td><kbd>Space</kbd> + left-button drag</td><td>Pan</td></tr>
  <tr><td>Hover</td><td>Tooltip with port / component values</td></tr>
  <tr><td>Left click on a box</td><td>Open its PDN properties in the sidebar</td></tr>
  <tr><td>Left click on a box's &#8982; icon</td><td>Jump to that component on the Heatmap</td></tr>
  <tr><td>Left click on a red / magenta tag</td><td>Jump to where that rail continues</td></tr>
  <tr><td>Left click on an amber / grey tag</td><td>Add a source, regulator or sink on that rail</td></tr>
</table>
<p class='muted'>Topology shortcuts require the Topology tab to be
focused. The diagram is vector-rendered - zoom stays sharp.</p>
"""),
    ("Nodes", """
<p>Sortable table of every directive node with
its voltage, drop, current density, and power density. Filter by
role or rail. The <b>Go &#9654;</b> button jumps to that node in
the Heatmap tab (enables its layer, zooms in, drops a yellow
highlight ring - left-click anywhere to clear).</p>
"""),
    ("Vias", """
<p>Sortable table of every via with worst-segment
current + power dissipation. The <b>Go &#9654;</b> button jumps to
that via in the Heatmap tab (enables its layer, zooms in, drops a
yellow highlight ring - left-click anywhere to clear).
The tab title also shows a warning count if any via current
reaches the threshold.</p>
<p class='muted'>Click a column heading to sort. Click the funnel at the
right of a heading (or right-click the heading) to filter that column: tick
the values to keep, or give a Min / Max on a numeric column. Filtered columns
show a filled funnel; <i>Clear column filters</i> removes them all. The
Bridges and Capacitors tables work the same way.</p>
"""),
    ("Bridges", """
<p>Every part that joins two nets, and what FYPA did with it:
modelled as a SERIES element, shorted automatically (Net Ties, 0&nbsp;&Omega;
parts), or not modelled at all. Click a part's designator to jump to it on
the Heatmap.</p>
<p>To model a part as a resistance: select its row, type its real DC
resistance into <i>Selected part</i>, and click <b>Model as SERIES</b>.
<b>Disable auto-bridge</b> leaves an automatically shorted part open at DC;
<b>Remove</b> undoes either. Changes are saved to the .fypa and take effect
on the next Resolve.</p>
<p class='muted'>Filter to <i>Affects a solved rail</i> first - those
parts join a solved rail to copper that is not in the solve and are under
100&nbsp;&Omega; (or of unknown value), so they can carry current the solve
misses and the rail's resistance can read high. Parts of 100&nbsp;&Omega; or
more (pull-ups, dividers) show as <i>ignored (high &Omega;)</i>: the current
they carry is too small to matter.</p>
"""),
    ("Capacitors", """
<p>Every decoupling capacitor with its mounted loop inductance,
sorted worst first. The <b>Go &#9654;</b> button jumps to the part on the
Heatmap; untick <b>Use</b> to leave a part out of the analysis. <i>Show on
heatmap</i> colours each included capacitor's pads by its loop inductance.</p>

<h3>Mouse controls</h3>
<table>
  <tr><th>Gesture</th><th>Action</th></tr>
  <tr><td>Double-click <b>C (&micro;F)</b></td><td>Override the capacitance. A part with no readable value is left out of the impedance model until you set one.</td></tr>
  <tr><td>Double-click <b>Pkg</b></td><td>Pin the case size, which picks the default ESL / ESR</td></tr>
  <tr><td>Double-click <b>ESL</b> / <b>ESR</b></td><td>Override the part's own ESL / ESR</td></tr>
  <tr><td>Click <b>Target</b></td><td>Choose which sink the loop closes to (default: the largest-current sink on the rail)</td></tr>
</table>
<p class='muted'>Leave an override field empty to go back to the value read
from the part or the package default.</p>

<h3>Tiers</h3>
<p><b>L1</b> is a closed-form estimate, filled in straight away.
<b>Compute Tier 2/3</b> runs a 2-D FEM of the plane-pair spreading
inductance (L2) and assembles the full capacitor &rarr; plane &rarr; IC loop
(L3). It is slower, but accurate on split and perforated planes where L1
is not.</p>
"""),
    ("Impedance", """
<p>A rail's |Z(f)| - regulator, decoupling capacitors and
plane-pair capacitance in parallel - plotted against a target mask of
Z<sub>target</sub> = V &middot; ripple&nbsp;% / I<sub>transient</sub> up to
F_MAX. Set the ripple, transient current, F_MAX and regulator R / L in the
left panel, then click <b>Apply &amp; Recompute</b>; the values are saved in
the .fypa per rail. The package library below sets the default ESL / ESR
for each case size.</p>
<p><i>Show individual capacitors</i> draws each part's own curve faintly;
hover a curve to highlight it and show its designator. The capacitors come
from the Capacitors tab, including its <b>Use</b> ticks and overrides.</p>
"""),
    ("Messages", """
<p>Every log message since FYPA started (loader, solver, editor),
filterable by level. <b>Clear</b> empties the list without touching
<code>fypa.log</code>.</p>
"""),
    ("Settings", """
<p>Application preferences - how FYPA looks and behaves: appearance,
display options, general behaviour, performance and experimental options.
They persist across launches and never change a solve result. The design's
base properties are on the Properties tab.</p>
"""),
    ("What-Ifs", """
<p>Try a change and see whether it helps or hurts, without losing the
current (base) result. The <b>What-If</b> button at the top-left of the
heatmap, next to Resolve, shows and hides the panel on the right (<b>W</b>
does the same). It is not available in editor mode: the button is hidden
there, and entering editor mode closes the panel. Drag the panel's left
edge to make it wider if the tables don't fit.</p>
<ul>
<li><b>New…</b> makes a what-if: change any of board temperature, copper
resistivity, via plating, via fill, the electro-thermal model, or a layer's
copper thickness (with ½-3 oz quick picks) or resistivity. Blank fields keep
the base value.</li>
<li><b>Run</b> solves the design with those changes. The base result is
untouched; each what-if keeps its own result.</li>
<li><b>Show:</b> <i>Base</i>, <i>What-if</i> (every tab then shows the
what-if, with a banner on the heatmap) or <i>Difference</i> - a per-vertex
map of what-if minus base for the current mode, red where it got worse and
blue where it got better.</li>
<li>The table compares each node (drop, voltage, margin, |J|, power) and the
headline numbers (worst drop, failures, copper I&sup2;R loss) against base,
marked better / worse.</li>
<li>Only one what-if is compared with the base at a time. A what-if run
against an older base (the design, directives or base inputs changed since)
is marked <i>out of date</i>; Run it again to refresh.</li>
<li><b>Make this the base</b> copies a what-if's changes into the Properties
tab's Base properties and makes its result the base result (re-solving if
the result is missing or out of date).</li>
<li>What-ifs and their results are saved with the project (.fypa).</li>
</ul>
"""),
)




def _help_tab_html(expanded: set[str]) -> str:
    """Render the Help tab - theme-aware <style> block + one collapsible
    section per tab. Only the titles in *expanded* show their body; each
    heading is a ``toggle:<title>`` anchor the browser's anchorClicked
    handler flips. ``toggle:*`` expands everything, ``toggle:-`` collapses it."""
    t = current_theme()
    link = f"color:{t['accent']}; text-decoration:none;"
    parts = [
        _help_tab_style(),
        f"<p class='muted'>"
        f"<a href='toggle:*' style='{link}'>Expand all</a> &nbsp;&middot;&nbsp; "
        f"<a href='toggle:-' style='{link}'>Collapse all</a></p>",
    ]
    for title, body in _HELP_SECTIONS:
        is_open = title in expanded
        arrow = "&#9662;" if is_open else "&#9656;"  # ▼ / ▶
        parts.append(
            f"<h2><a href='toggle:{title}' style='color:{t['fg_strong']};"
            f" text-decoration:none;'>{arrow} {title}</a></h2>")
        if is_open:
            parts.append(body)
    return "".join(parts)
