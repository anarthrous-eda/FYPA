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




_HELP_TAB_BODY = """

<h2>Keyboard shortcuts</h2>

<h3>Heatmap tab</h3>
<table>
  <tr><th>Key</th><th>Action</th></tr>
  <tr><td><kbd>2</kbd></td><td>Switch to 2D mode <span class='muted'>(re-fits to data)</span></td></tr>
  <tr><td><kbd>3</kbd></td><td>Switch to 3D mode <span class='muted'>(re-fits to data)</span></td></tr>
  <tr><td><kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>2</kbd></td><td>Switch to 2D <i>keeping the current view</i></td></tr>
  <tr><td><kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>3</kbd></td><td>Switch to 3D <i>keeping the current view</i> (top-down entry)</td></tr>
  <tr><td><kbd>0</kbd></td><td>Reset the 3D view (top-down, refit to data) <span class='muted'>— 3D mode only</span></td></tr>
  <tr><td><kbd>O</kbd></td><td>Toggle <i>Show layer outlines</i></td></tr>
  <tr><td><kbd>R</kbd></td><td>Toggle <i>Show only rail net</i></td></tr>
  <tr><td><kbd>T</kbd></td><td>Toggle <i>Show cursor tooltip</i></td></tr>
  <tr><td><kbd>A</kbd></td><td>Toggle <i>Show current arrows</i></td></tr>
  <tr><td><kbd>V</kbd></td><td>Toggle <i>Heatmap vias/PTH</i> <span class='muted'>— 3D mode only</span></td></tr>
  <tr><td><kbd>M</kbd></td><td>Cycle <i>Mode</i> forward (Voltage &rarr; Voltage Drop &rarr; &hellip;)</td></tr>
  <tr><td><kbd>Shift</kbd>+<kbd>M</kbd></td><td>Cycle <i>Mode</i> backward</td></tr>
  <tr><td><kbd>H</kbd></td><td>Cycle <i>colour scheme</i> forward (Viridis &rarr; Blue&nbsp;&rarr;&nbsp;Red &rarr; &hellip;)</td></tr>
  <tr><td><kbd>Shift</kbd>+<kbd>H</kbd></td><td>Cycle <i>colour scheme</i> backward</td></tr>
  <tr><td><kbd>B</kbd></td><td>Collapse / expand the side panel</td></tr>
</table>
<p class='muted'>Shortcuts are window-scoped — they fire when the viewer
window has focus but defer to text inputs (e.g. the Min/Max boxes)
when one of those has focus. The editor-mode keys
(<kbd>E</kbd>, <kbd>S</kbd>, <kbd>L</kbd>, <kbd>Delete</kbd>, undo / redo)
are listed under <i>Mouse controls &rarr; Editor mode</i> below.</p>

<h2>Topology tab</h2>
<p>The <b>Topology</b> tab shows an abstract Flow diagram of the PDN
simulation model (not the PCB layout). Click a component box to jump
to its pad on the Heatmap.</p>
<table>
  <tr><th>Gesture / key</th><th>Action</th></tr>
  <tr><td>Mouse wheel</td><td>Scroll vertically</td></tr>
  <tr><td><kbd>Shift</kbd> + mouse wheel</td><td>Scroll horizontally</td></tr>
  <tr><td><kbd>Ctrl</kbd> + mouse wheel</td><td>Zoom in / out around the cursor</td></tr>
  <tr><td><kbd>Ctrl</kbd>+<kbd>+</kbd> / <kbd>Ctrl</kbd>+<kbd>&minus;</kbd></td>
      <td>Zoom in / out (viewport centre)</td></tr>
  <tr><td><kbd>Ctrl</kbd>+<kbd>0</kbd></td><td>Fit the whole diagram in view</td></tr>
  <tr><td><b>+</b> / <b>&minus;</b> / <b>Fit</b> toolbar buttons</td><td>Zoom in, zoom out, fit</td></tr>
  <tr><td>Middle-button drag</td><td>Pan</td></tr>
  <tr><td><kbd>Space</kbd> + left-button drag</td><td>Pan</td></tr>
  <tr><td>Arrow keys</td><td>Pan; <kbd>Shift</kbd> or <kbd>Ctrl</kbd> for larger steps</td></tr>
  <tr><td>Hover</td><td>Tooltip with port / component values</td></tr>
  <tr><td>Left click on a box</td><td>Jump to that component on the Heatmap</td></tr>
</table>
<p class='muted'>Topology shortcuts require the Topology tab to be
focused. The diagram is vector-rendered — zoom stays sharp.</p>

<h2>Mouse controls</h2>

<h3>Heatmap viewport (both modes)</h3>
<table>
  <tr><th>Gesture</th><th>Action</th></tr>
  <tr><td>Right-button drag</td><td>Pan the view</td></tr>
  <tr><td>Mouse wheel</td><td>Zoom in / out (around cursor in 2D, dolly camera in 3D)</td></tr>
  <tr><td>Middle-button drag &uarr;/&darr;</td><td>Exponential zoom — drag up = zoom in, down = zoom out</td></tr>
  <tr><td>Left click</td><td>Clear the yellow jump highlight (from a Vias/Nodes-tab Go)</td></tr>
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
  <tr><td><kbd>S</kbd></td><td>Drop a free <b>SOURCE</b> &mdash; same as the red triangle
      button at the top-left of the viewport. Then click copper to place it;
      press <kbd>S</kbd> again or <kbd>Esc</kbd> to cancel.</td></tr>
  <tr><td><kbd>L</kbd></td><td>Drop a free <b>SINK</b> &mdash; same as the blue triangle
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
of them &mdash; rows left showing <b>*</b> keep their per-sink values.
Current is per sink, so 2 A applied to four sinks adds 8 A of load. A
selection holding anything other than sinks offers no controls, because
no one set of properties applies across roles.</p>

<h3>3Dconnexion SpaceMouse</h3>
<ul>
  <li><b>Translation</b> &mdash; pan the view (2D and 3D)</li>
  <li><b>Translation Z</b> &mdash; zoom in / out</li>
  <li><b>Rotation</b> &mdash; orbit the camera (3D mode only)</li>
  <li><b>Menu / fit button</b> &mdash; fit board (2D) or reset 3D view</li>
  <li>Requires <b>3DxWare</b> on Windows/macOS
    (<code>uv sync --extra spacemouse</code>) or <b>spacenavd</b> on Linux
    (<code>sudo apt install spacenavd libspnav0</code>)</li>
  <li>Navigation stays active while FYPA is open so 3Dconnexion Settings
    keeps the FYPA profile selected</li>
</ul>

<h3>Voltage / Voltage Drop mode only <span class='muted'>(2D only)</span></h3>
<table>
  <tr><th>Gesture</th><th>Action</th></tr>
  <tr><td>Hold <kbd>Shift</kbd></td><td>Anchor a voltage probe at the cursor and draw a thin white
    line from there to the live mouse position. The probe bar gains a
    <code>Difference = X V</code> readout — the live cursor's voltage
    minus the anchor's. Press only takes effect when the cursor is
    over copper that has a voltage value; release <kbd>Shift</kbd> to
    clear the line.</td></tr>
</table>

<h2>Side-panel controls</h2>
<ul>
  <li><b>Physical layers</b> &mdash; tick checkboxes to stack multiple
    copper layers in the view. Each layer has its own swatch colour
    used by the outline overlay.</li>
  <li><b>Rails</b> &mdash; tick one or more bridged rail groups to
    show their copper (e.g. <code>+3V3</code> bundles <code>+3V3</code>
    + <code>3V3_SW</code> if a series resistor / inductor links them).
    Use the <i>All Rails</i> row to toggle every rail at once.</li>
  <li><b>Mode</b> &mdash; Voltage / Voltage Drop / Current Density /
    Power Density.</li>
  <li><b>Show only rail net</b> &mdash; hide bridged sibling nets so
    you see just each selected rail's primary net.</li>
  <li><b>Layer outlines</b> &mdash; the contour button on the
    <i>All Rails</i> row (hotkey <kbd>O</kbd>) traces each visible rail's
    copper polygons in the owning layer's swatch colour.</li>
  <li><b>Transparency control</b> &mdash; the white-disc icon sits on
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
  <li><b>Show cursor tooltip</b> &mdash; a small tooltip follows the
    mouse, showing the value of the current mode at that point along
    with the net and layer. Same info as the probe bar under the plot.</li>
  <li><b>3D view</b> &mdash; perspective view of the stacked layers
    with via cylinders.</li>
  <li><b>Heatmap vias/PTH</b> &mdash; colour via and plated-through-hole
    cylinders by the active mode instead of solid orange / light grey.
    Voltage / Voltage Drop interpolate along the barrel's length; Current
    Density and Power Density are constant per inter-layer segment. 3D
    mode only.</li>
  <li><b>Layer spacing slider</b> &mdash; 3D only. Scales both the
    inter-layer separation and the via cylinder length.</li>
  <li><b>Show current arrows</b> &mdash; white arrows on a regular
    pixel-spaced grid showing the direction of current flow; shaft
    length scales with &radic;|J| so weak and strong currents are
    both visible. Re-sampled on zoom / rotate. Works in 2D and 3D
    (in 3D each arrow rides the top face of its layer).</li>
  <li><b>Arrow spacing (px)</b> &mdash; pixels between adjacent
    arrows on screen. Smaller = denser arrows.</li>
  <li><b>Colour scale</b> &mdash; the gradient strip is overlaid on the
    viewer's bottom-left corner (with value ticks); drag its Min / Max
    handles, type exact values in the side-panel boxes, or click the
    <b>&#8634;</b> reset button to restore the data range.</li>
</ul>

<h2>Tabs</h2>
<ul>
  <li><b>Heatmap</b> &mdash; the interactive viewport.</li>
  <li><b>Setup</b> &mdash; HTML report of the solved problem: stackup,
    physics constants, parsed PDN_* directives (collapsible), solver
    diagnostics, warnings / errors.</li>
  <li><b>Nodes</b> &mdash; sortable table of every directive node with
    its voltage, drop, current density, and power density. Filter by
    role or rail. The <b>Go &#9654;</b> button jumps to that node in
    the Heatmap tab (enables its layer, zooms in, drops a yellow
    highlight ring &mdash; left-click anywhere to clear).</li>
  <li><b>Vias</b> &mdash; sortable table of every via with worst-segment
    current + power dissipation. The <b>Go &#9654;</b> button jumps to
    that via in the Heatmap tab (enables its layer, zooms in, drops a
    yellow highlight ring &mdash; left-click anywhere to clear).
    The tab title also shows a warning count if any via current
    reaches the threshold.</li>
  <li><b>Settings</b> &mdash; tunable physics, meshing, and display
    options, plus per-layer copper thicknesses. Edits apply on the next
    solve: <b>Re-run Solver</b> re-solves with the new settings and
    opens a fresh viewer.</li>
</ul>
"""




def _help_tab_html() -> str:
    """Render the Help tab — theme-aware <style> block + static body."""
    return _help_tab_style() + _HELP_TAB_BODY
