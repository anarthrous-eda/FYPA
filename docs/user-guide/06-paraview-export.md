# 6. Exporting to ParaView

FYPA's built-in viewer is tuned for fast design iteration — switch
layers, tweak a sink current, resolve, look at the heatmap. For deeper
post-processing — 3-D layer-stackup views, slicing through the voltage
field at arbitrary planes, publication-quality figures, comparison
against measured data — FYPA can export the solve to **ParaView**.

[ParaView](https://www.paraview.org/) is a free, open-source 3-D
scientific-visualisation tool from Kitware, widely used in
finite-element and CFD work. FYPA writes its solved voltage field as
standard VTK files that ParaView reads natively.

## 6.1 Why use ParaView (and when not to)

| You want to…                                                                | Use         |
|------------------------------------------------------------------------------|-------------|
| Iterate on the PDN (change a sink, re-solve, check the result)               | FYPA viewer |
| See all copper layers stacked in 3-D with realistic z-separation             | ParaView    |
| Slice the voltage field at an arbitrary plane                                | ParaView    |
| Apply a custom / log-scale / branded colormap                                | ParaView    |
| Overlay against external data (thermal measurements, probed voltages)        | ParaView    |
| Produce a high-resolution figure for a report or paper                       | ParaView    |
| Script batch post-processing across many solves                              | ParaView    |

For most design work you will not need ParaView. Reach for it when the
built-in viewer cannot give you the angle, slice or comparison you need.

## 6.2 What FYPA exports

FYPA writes one **VTU file** (VTK Unstructured Grid, XML format) per
enabled copper layer. Each file contains:

- The FEM triangle mesh for that layer — vertices and triangle
  connectivity.
- The solved **voltage** at every vertex (in volts).

Files are named after the layer name (`Top.vtu`, `L2.vtu`,
`GND_plane.vtu`, etc., with spaces and special characters replaced).
You then open the set in ParaView and stack / colour / slice as you
need.

> Only the voltage field is exported. Current density and power
> density are derivable from voltage and the per-layer copper
> thickness inside ParaView via the Gradient and Calculator filters
> — or recompute them yourself from the raw mesh and voltage data.

## 6.3 Exporting from the viewer

Once a project is open in the viewer, **File > Export > ParaView…**
writes the currently-loaded solution straight out. A directory chooser
opens (defaulting to the project's cache folder); pick a destination
and one `.vtu` file is written per copper layer. The status bar shows
how many files were written.

This is the quickest path — no extra `solve` step, no pickle handling,
and any in-editor changes you have already resolved are reflected in
the export. Use it whenever you just want to look at the current solve
in ParaView.

> The export needs the `lxml` Python package. The prebuilt Windows
> `.exe` bundles it already; for source installs run `pip install
> lxml` into the FYPA venv if the menu reports it missing.

## 6.4 Exporting from the command line

For batch / scripted workflows there is also a CLI export that reads a
**solve pickle** — the `.pkl` file produced by the `solve` subcommand.
The lean pickle used by the GUI cache will not work for this path: it
omits the per-triangle topology that the CLI exporter expects.

If you have not got a solve pickle yet, run:

```sh
python FYPA.py solve path\to\YourBoard.PrjPcb solution.pkl
```

This runs the full pipeline (extract → geometry → annotations →
solve) and writes the result to `solution.pkl`. On a board you have
already opened in the GUI, the design extraction is served from cache
so this is much faster than a cold run.

Then export:

```sh
python FYPA.py paraview solution.pkl paraview_out\
```

Arguments:

- `solution.pkl` — the pickle produced by `solve`.
- `paraview_out\` — the output directory. Created if missing. One
  `.vtu` file is written per copper layer.

The command prints `ParaView export complete: paraview_out` on
success.

> The prebuilt Windows executable bundles the export the same way —
> from a terminal in the folder containing `FYPA.exe`, run
> `FYPA.exe paraview solution.pkl paraview_out\`.

## 6.5 Opening the result in ParaView

1. Install ParaView from [paraview.org](https://www.paraview.org/download/)
   if you do not have it already.
2. Launch ParaView and choose **File > Open…**. Select **all** the
   `.vtu` files in the output directory (Ctrl/Shift+click) and open
   them as a single group, or open them one at a time as separate
   sources.
3. In the **Pipeline Browser**, click **Apply** for each loaded source.
4. In the **Properties** panel, set **Coloring** to **voltage** to
   shade by the solved field. The default greyscale becomes a colour
   gradient.
5. For a 3-D stackup view, select each layer's source in turn and use
   the **Transform** filter (Filters > Alphabetical > Transform) to
   translate it by the correct z-offset for the stackup. ParaView's
   layout view then shows all layers stacked.

The standard ParaView tutorials (Filters, Slice, Contour, Calculator)
all apply unchanged — FYPA's output is plain VTK from that point on.

## 6.6 Troubleshooting

| Message or symptom                                                                 | Likely cause                                                                  | Fix                                                                          |
|------------------------------------------------------------------------------------|--------------------------------------------------------------------------------|------------------------------------------------------------------------------|
| `` `paraview` needs the `lxml` package `` (CLI) or **ParaView export unavailable** dialog (viewer) | The `lxml` Python package is not installed.                                   | `pip install lxml` into the FYPA venv. (The prebuilt `.exe` bundles it already.) |
| `` `paraview` can't export from a lean-format pickle ``                            | The pickle was produced by the GUI cache rather than the `solve` subcommand. Only affects the CLI path; the **File > Export > ParaView…** menu reads the in-memory solution and is not subject to this. | Re-run `python FYPA.py solve YourBoard.PrjPcb solution.pkl` to produce a full pickle, or use the viewer's menu instead. |
| ParaView opens a file but the **Coloring** dropdown has no `voltage` option        | The `.vtu` loaded as geometry only (e.g. the **Apply** button was not clicked). | Click **Apply** in the Properties panel after opening the file.              |
| All layers are drawn in the same plane in 3-D view                                  | VTU files have no inherent z-offset — each is a 2-D mesh.                     | Use the **Transform** filter on each source to translate it to its physical z-position. |

## Next steps

- Looking for the bigger picture of what each `PDN_*` parameter does
  and how the FEM works under the hood? See the
  [main README](../../README.md) and the
  [via resistance model](../via_resistance_model.md).
