# 5. The viewer tour

Once FYPA has solved your PDN, the viewer is where you spend the rest
of your time. This section walks through the layout, the tabs on the
side panel, the display modes, and the interactions worth knowing.

> The first four sections all assume you have a solved project open in
> the viewer. If you have not got that far yet, start with
> [Section 1.6](01-sources-and-sinks.md#16-importing-into-fypa).

## 5.1 Layout overview

The viewer has three main areas:

- **Viewport (centre / left)** — the OpenGL canvas. Shows the current
  layer's copper, shaded by the current display mode. Pan with the
  mouse, zoom with the wheel.
- **Side panel (right)** — a tabbed panel with the Setup, Nodes,
  Vias, Messages, Settings and Help tabs.
- **Status bar (bottom)** — short hints, hover-probe readouts and
  recently-applied actions.

A small **colour-scale strip** is pinned to the bottom-left of the
viewport, with two draggable handles for clamping the display range.

![Viewer layout overview](screenshots/05-layout.png)

## 5.2 The Setup tab

The Setup tab is the control surface for what the viewport shows.
From top to bottom:

- **Layer picker** — every copper layer in the stackup. Click to
  switch which layer the viewport renders.
- **Rail picker** — every rail FYPA inferred from your `PDN_P_NET` /
  `PDN_N_NET` pairs. Click to switch which rail's voltage / current is
  shown.
- **Mode dropdown** — *Voltage*, *Voltage Drop*, *Current Density* or
  *Power Density* (see 5.3).
- **Colour-scheme picker** — the matplotlib colormap used to shade the
  mesh (default `viridis`).
- **Min / Max boxes** — type a value to clamp the colour scale, or
  drag the handles on the scale strip in the viewport.
- **Per-layer transparency buttons** — fade individual layers to see
  underlying copper.

> Use the **R** hotkey to toggle "rail-only" mode — hides everything
> except the selected rail's copper. Useful on a busy board where
> you want to see one rail without the visual noise of all the
> others.

## 5.3 The four display modes

Switching the mode dropdown (or pressing **M** to cycle, **Shift+M**
to cycle back) changes what each mesh vertex's colour represents:

| Mode               | Units    | What it shows                                                                                  |
|--------------------|----------|------------------------------------------------------------------------------------------------|
| **Voltage**        | V        | Absolute potential at every node. The hottest colour is the supply, the coldest is the return. |
| **Voltage Drop**   | V        | Signed drop relative to the rail's source. Useful for "how much voltage have I lost between J1 and U5?" |
| **Current Density**| A / mm   | Magnitude of the current density vector `|J|` at every node. Highlights bottlenecks. |
| **Power Density**  | W / mm²  | Resistive power dissipation per unit area. Highlights hot spots — where copper will warm up. |

> Current Density and Power Density tend to spike sharply at narrow
> tracks and pad corners. The colour scale auto-clips the top end on
> these modes so a single 10× spike does not flatten the rest of the
> board into one colour. Use the Min / Max boxes to override.

## 5.4 The Nodes tab

One row per terminal pin (every pad on every SOURCE / SINK /
REGULATOR / SERIES directive). Columns:

- **Designator / Pad** — which part, which pad.
- **Net** — the net this pin connects to.
- **V** — the solved voltage at this pin.
- **Min V**, **Margin**, **Status** — only populated for SINK pins
  that carry a `PDN_MIN_V` value
  (see [Section 1.3](01-sources-and-sinks.md#optional-pdn_min_v--minimum-acceptable-voltage-at-the-sink)).
  *Margin* is the measured voltage minus the minimum; rows with a
  negative margin are highlighted red with `Status = FAIL`.

The table is sortable — click a column header to sort by it. Sorting
by Margin ascending is the fastest way to find which pin is closest
to falling below its limit.

## 5.5 The Vias tab

One row per via on the board, with the current flowing through it.
Columns:

- **Net** — which net the via belongs to.
- **From / To** — the two layers it connects.
- **Current (A)** — the solved current through the via.

Sort by Current descending to spot vias that are carrying too much —
a 0.3 mm hole carrying 2 A is a future failure. The
[via resistance model](../via_resistance_model.md) covers how those
currents are computed.

## 5.6 The Messages tab

Warnings and diagnostics from the loader and solver — anything that
did not abort the run but is worth knowing about. Examples:

- Net names referenced in `PDN_*` parameters that were not found on
  the PCB (and were therefore skipped).
- Editor directives that could not be applied (skipped, not aborted —
  see [Section 2.8](02-sources-and-sinks-editor.md#28-troubleshooting)).
- Solver warnings about near-singular matrices, suspicious gradients,
  etc.

Worth a glance after every solve. If everything is healthy, the tab
shows a short "no issues" message.

## 5.7 The Settings and Help tabs

- **Settings** — viewer-wide preferences (default colormap, marker
  visibility, hover-probe behaviour, theme).
- **Help** — built-in cheatsheet of hotkeys and gestures.

## 5.8 The Heatmap tab

The viewport itself lives in a tab called *Heatmap*. You will rarely
click away from it during normal use, but FYPA can also open
single-rail / single-layer comparison views in additional tabs.

## 5.9 Interaction reference

| Action                          | How                                                                  |
|---------------------------------|----------------------------------------------------------------------|
| Pan                             | Left-click and drag in the viewport.                                 |
| Zoom                            | Mouse wheel.                                                         |
| Hover-probe a value             | Move the mouse over copper — the value (in the current mode's units) appears in the status bar, along with the net and layer. |
| Clamp the colour scale          | Drag the two handles on the colour strip, or type values into the **Min** / **Max** boxes on the Setup tab. |
| Click a piece of copper         | Selects it — the bottom bar reports the net, the area, and (where applicable) the layer-local current. |
| Click a marker                  | Reports the directive value (source voltage, sink current, etc.) in the bottom bar. |
| Resolve after editor edits      | Click the green **↻ Resolve** button at the top-left of the viewport (see [Section 2.6](02-sources-and-sinks-editor.md#26-re-solving-and-saving)). |

## 5.10 Hotkeys worth remembering

| Key          | Action                                                                |
|--------------|-----------------------------------------------------------------------|
| **M / Shift+M** | Cycle the display mode forward / backward.                         |
| **H / Shift+H** | Cycle the colour scheme forward / backward.                        |
| **R**        | Toggle rail-only mode (hide other rails' copper).                     |
| **O**        | Toggle copper outlines.                                               |
| **I**        | Toggle SOURCE / SINK / SERIES / via markers.                          |
| **A**        | Toggle current-flow arrows on the heatmap.                            |
| **V**        | Toggle via overlay on the heatmap.                                    |
| **T**        | Toggle the hover-probe tooltip.                                       |
| **2 / 3**    | Switch the viewport to 2-D / 3-D mode.                                |
| **0**        | Reset the 3-D camera.                                                 |
| **E**        | Toggle editor mode (see [Section 2.2](02-sources-and-sinks-editor.md#22-entering-editor-mode)). |
| **Ctrl+S**   | Save the project (the dialog offers project-only or project+solution). |

The Help tab inside the viewer has the authoritative, always-current
list — if a hotkey here looks wrong, that tab is the source of truth.

## Next steps

- For exporting your solve to a tool that does 3-D stackup
  visualisation, slicing, and publication-quality plots, see
  [Section 6 — Exporting to ParaView](06-paraview-export.md).
