# FYPA - FEM Y-parameter Power Analyser

Power-Delivery-Network (PDN) analysis for Altium and Gerber PCB designs.
Extracts copper geometry directly from a `.PrjPcb` project or from a Gerber
fabrication package, runs a 2-D FEM Laplace solver per copper layer to compute
voltage drop and current density across power rails, and visualises the result
in a custom OpenGL viewer with per-vertex shading.

**Name:** *FYPA* stands for **F**EM **Y**-parameter **P**ower **A**nalyser —
the tool extracts the admittance (Y-parameter) matrix of each copper layer
via a finite-element solve, then uses it to compute steady-state voltage,
current density, and power dissipation across the PDN.

Based on [padne](https://github.com/atx/padne) (the FEM solver) and uses
[altium_monkey](https://github.com/wavenumber-eng/altium_monkey) to read the
Altium project files.

> **New user?** Start with the [User Guide](docs/user-guide/README.md) for
> a step-by-step walkthrough of common workflows.

![Heatmap viewer](screenshots/viewer.png)
> **Disclaimer:** FYPA is a design-aid tool. Treat its results as guidance
> and validate against measurement.
## What it does

You annotate components in your Altium schematic with a small set of
`PDN_*` parameters (which net is the input, which is the output, the current
or voltage), point this tool at the `.PrjPcb`, and it:

1. Extracts every copper feature (tracks, arcs, regions, pads, vias) along
   with the net assignments.
2. Builds a per-(physical-layer, net) 2-D geometry from the Shapely union of
   all copper for that net on that layer.
3. Triangulates each layer and runs a sparse-direct FEM Laplace solve.
4. Solves for the voltage field, current density `|J|`, and power-density
   on every node.
5. Opens an interactive GPU-accelerated viewer where you can:
   - Switch layers / rails / display modes (Voltage / Voltage Drop /
     Current Density / Power Density)
   - Pan and zoom natively on the GPU (no rasterisation; pixel-sharp at
     every zoom level)
   - Drag handles or type values to clamp the colour scale
   - Hover for a probe readout
   - Cross-check per-pin voltages and per-via currents in sortable tables
   - Switch to **editor mode** to place sources / sinks and re-solve
     without editing the Altium schematic (see below)

## Download (prebuilt Windows binary)

If you just want to run the tool — no Python install, no git clone — grab the
latest packaged build from the
[Releases page](https://github.com/cutreedesigns/FYPA/releases/latest):

1. Download `FYPA_v<version>.zip` from the latest release's *Assets*.
2. Extract it anywhere permanent (e.g. `C:\Tools\FYPA\`).
3. Double-click `FYPA.exe` inside the extracted folder.

On first launch Windows SmartScreen will show a blue warning — click
**"More info"** then **"Run anyway"** (the exe is unsigned; this is a
one-time prompt per machine).

> **Keep `FYPA.exe` and the `_internal\` folder together** — moving the exe
> alone will break it. Create a desktop shortcut rather than moving the exe.

To build the executable yourself, see
[Building a standalone executable](#building-a-standalone-executable).

## Installation (from source)

FYPA uses [uv](https://docs.astral.sh/uv/) for dependency and Python-version
management. Install uv once (Windows):

```sh
winget install astral-sh.uv          # or: pip install uv
```

Then clone and sync:

```sh
git clone git@github.com:cutreedesigns/FYPA.git
cd FYPA
uv sync
```

That's it. `uv sync` reads `pyproject.toml` + `uv.lock`, fetches Python 3.12
if it isn't already available (the version is pinned in `.python-version`),
creates a `.venv\` inside the repo, and installs every runtime + dev
dependency — including `altium_monkey`, which uv pulls from
[upstream](https://github.com/wavenumber-eng/altium_monkey) at the tag pinned
in `pyproject.toml`'s `[tool.uv.sources]`.

> **Python version: 3.12 only.** Both FYPA and `altium_monkey` pin
> `requires-python = ">=3.12,<3.13"`. `.python-version` selects 3.12, which
> uv fetches automatically.

Day-to-day commands:

```sh
uv run python FYPA.py gui path\to\YourBoard.PrjPcb   # run the app
uv run pytest -m "not slow"                          # fast test suite
uv run ruff check .                                  # lint
```

`uv run` automatically syncs the venv if it's out of date, so you don't need
to `activate` first. If you prefer the classic flow, `.venv\Scripts\activate`
still works.

`PyOpenGL` and `PyOpenGL_accelerate` are required for the GPU viewer.

## Usage

Annotate your Altium schematic with `PDN_*` parameters on the components that
define the power-delivery topology:

| Role         | Required parameters                                                                                     | What it means                                                         |
|--------------|---------------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------|
| `SOURCE`     | `PDN_V`, `PDN_P_NET`, `PDN_N_NET` — *or* `PDN_V`, `PDN_NET`                                              | Voltage source (e.g. a connector pin or regulator)            |
| `SINK`       | `PDN_I`, `PDN_P_NET`, `PDN_N_NET` — *or* `PDN_I`, `PDN_NET`                                              | Current sink  (e.g. an IC load)                   |
| `SERIES`     | `PDN_R`, `PDN_P_NET`\*, `PDN_N_NET`\*                                                                   | Series resistance / fuse / ferrite / inductor DCR (rail bridge)       |
| `REGULATOR`  | `PDN_V`, `PDN_REGULATOR_TYPE`, `PDN_REGULATOR_EFFICIENCY`, optional `PDN_QUIESCENT` — *or* `PDN_GAIN`, plus `PDN_OUT_*` / `PDN_IN_*` nets | On-board regulator (LDO / buck) — models BOTH input and output rails  |

\* `PDN_P_NET` and `PDN_N_NET` are optional for `SERIES` on a 2-pin part — the
tool auto-infers them from the component's pad connectivity.

Add `PDN_ROLE` on the component (e.g. `PDN_ROLE=SOURCE`) and the required parameters for that role. 
For 2-pin parts the tool can auto-infer `PDN_P_NET` /
`PDN_N_NET` from connectivity; for ICs you'll need to set them explicitly.

### `SINK` — minimum-voltage check (`PDN_MIN_V`)

A `SINK` can carry an optional `PDN_MIN_V` parameter giving the **minimum
acceptable rail voltage at the part's P pins**. The viewer's *Nodes* tab adds
*Min V*, *Margin* and *Status* columns: margin is the per-pin measured voltage
minus `PDN_MIN_V`, and rows with a negative margin are highlighted in red
(`Status = FAIL`). Sinks without `PDN_MIN_V` show `—` in those columns; the
check is purely a post-solve report, it doesn't change what the solver does.

```text
U5 (3V3 load):
  PDN_ROLE  = SINK
  PDN_I     = 500mA
  PDN_P_NET = +3V3
  PDN_N_NET = 0V
  PDN_MIN_V = 3.2        # fail if any +3V3 pin drops below 3.2 V
```

Indexed multi-channel sinks support `PDN<n>_MIN_V` per channel, the same way
`PDN<n>_I` works.

### Single-net (point-to-point) check — `PDN_NET`

Sometimes a net has no return reference at all — tracing power from a
connector to a high-side switch, say, where the switch has only a supply
pin. For that case give a `SOURCE`/`SINK` a single `PDN_NET` instead of the
`PDN_P_NET` / `PDN_N_NET` pair:

```text
J1 (connector feeding VBATT):     U3 (high-side switch input):
  PDN_ROLE = SOURCE                 PDN_ROLE = SINK
  PDN_V    = 12V                    PDN_I    = 5A
  PDN_NET  = VBATT                  PDN_NET  = VBATT
```

The directive then has one terminal on PCB copper; its other terminal is an
ideal 0 Ω return, so the result reflects only that net's copper voltage drop
— the point-to-point IR drop you're after. (`PDN_PINS` overrides the pad set,
the way `PDN_P_PINS` / `PDN_N_PINS` do for two-terminal directives.)

Rules:

- `PDN_NET` and `PDN_P_NET` / `PDN_N_NET` are mutually exclusive on one
  directive — giving both, or neither, is an error.
- A single-net analysis still needs a closed loop: at least one `SOURCE`
  **and** one `SINK` on the same net (a lone one is an "open loop" error).
- Single-net mode is `SOURCE` / `SINK` only — `SERIES` bridges two nets and
  `REGULATOR` has four terminals.
- Every `SOURCE` and `SINK` that shares a net must use the same mode; a group
  can't mix single-net and two-terminal directives. Independent groups may
  use different modes in the same solve.


### Multi-channel directives

`SOURCE`, `SINK`, `REGULATOR`, and `SERIES` roles support multiple
independent channels on a single part. Channels are addressed by appending
a positive integer to `PDN` in the parameter prefix:

| Channel | SOURCE / REGULATOR | SINK | SERIES | Net params (examples) |
|---------|-------------------|------|--------|------------------------|
| legacy  | `PDN_V` | `PDN_I` | `PDN_R` | `PDN_P_NET`, `PDN_OUT_P_NET`, … |
| 1       | `PDN1_V` | `PDN1_I` | `PDN1_R` | `PDN1_P_NET`, `PDN1_OUT_P_NET`, … |
| 2       | `PDN2_V` | `PDN2_I` | `PDN2_R` | `PDN2_P_NET`, … |
| …       | … | … | … | … |

REGULATOR channels also require `PDN<n>_GAIN` and the four `PDN<n>_OUT_*` /
`PDN<n>_IN_*` net (or pin) parameters per channel.

The legacy unindexed channel and any number of indexed channels coexist
as independent directives. Indices are sparse — gaps are allowed (e.g.
just `PDN_V` + `PDN2_V`). A channel is "present" iff its value parameter
is set; the per-channel `*_NET` and `*_PINS` parameters use the matching
index. The part-wide `PDN_ROLE` is the **default** role for every channel —
a channel can override it with `PDN<n>_ROLE` (see [Mixed-role
parts](#mixed-role-parts-a-source-and-a-sink-on-one-component) below).

Example — a SINK with three independent supply rails:

```text
U7 (multi-rail IC load):
  PDN_ROLE   = SINK
  PDN_I      = 500mA     PDN_P_NET  = +3V3   PDN_N_NET  = GND
  PDN1_I     = 250mA     PDN1_P_NET = +1V8   PDN1_N_NET = GND
  PDN2_I     = 50mA      PDN2_P_NET = +5V    PDN2_N_NET = GND
```

Example — a PMIC with two regulator outputs on one symbol:

```text
U4:
  PDN_ROLE       = REGULATOR
  PDN_V          = 3.3       PDN_GAIN = 0.9
  PDN_OUT_P_NET  = +3V3      PDN_OUT_N_NET = GND
  PDN_IN_P_NET   = +5V       PDN_IN_N_NET  = GND
  PDN1_V         = 1.8       PDN1_GAIN = 0.85
  PDN1_OUT_P_NET = +1V8      PDN1_OUT_N_NET = GND
  PDN1_IN_P_NET  = +5V       PDN1_IN_N_NET  = GND
```

The Setup tab and the Nodes-tab table label indexed channels as
`U7#1`, `U7#2` so they're easy to tell apart from the legacy `U7` channel.

For `SERIES`, auto-inferred P/N nets (2-pin parts) apply only when the
part carries a single channel; multi-channel SERIES requires explicit
`PDNn_P_NET` / `PDNn_N_NET` or `PDNn_P_PINS` / `PDNn_N_PINS` per channel.

On **repeated schematic sheets** (Altium `REPEAT`), `PDN_*_NET` may use
the local child-sheet net label; FYPA maps each PCB instance to its slot
net via pin connectivity (not via `ChannelDesignatorFormatString`). See
[User guide — Local net names](docs/user-guide/01-sources-and-sinks.md#local-net-names-hierarchical--reused-sheets).

### Mixed-role parts (a source and a sink on one component)

`PDN_ROLE` is the part-wide **default** role, but any channel may override
it with `PDN<n>_ROLE`. That lets a single physical part carry channels of
different roles — the case where **one component is both a source and a
sink**.

The motivating example is a DAC: its supply pins **sink** current from the
power rails, while its outputs **source** current into downstream loads.
Put the part's majority role on `PDN_ROLE` and override only the channels
that differ:

```text
U7 (DAC):
  PDN_ROLE   = SINK                                    # part-wide default
  PDN_I      = 80mA    PDN_P_NET  = AVDD      PDN_N_NET  = GND
  PDN1_I     = 20mA    PDN1_P_NET = DVDD      PDN1_N_NET = GND
  PDN2_ROLE  = SOURCE                                  # this channel overrides
  PDN2_V     = 2.5     PDN2_P_NET = DAC_OUT0  PDN2_N_NET = GND
  PDN3_ROLE  = SOURCE
  PDN3_V     = 1.8     PDN3_P_NET = DAC_OUT1  PDN3_N_NET = GND
```

Here channels 0–1 are sinks (AVDD, DVDD supplies) and channels 2–3 are
sources (the two DAC outputs). Each channel's effective role decides which
value parameter marks it present (`PDN<n>_V` for a SOURCE/REGULATOR channel,
`PDN<n>_I` for SINK, `PDN<n>_R` for SERIES) and which net/pin parameters it
reads. `PDN<n>_ROLE` must be one of `SOURCE`, `SINK`, `SERIES`, `REGULATOR`.

Notes and pitfalls:

- **Uniform parts need nothing new.** Two sinks are still just
  `PDN_ROLE = SINK` + `PDN_I` / `PDN1_I` — you only write `PDN<n>_ROLE` for
  the channels that diverge from the default. Existing designs are
  unaffected.
- **Mix roles across *different* nets.** A SOURCE and a SINK on the *same*
  net of one part simply feed current straight back into each other. The
  mixed-role scheme is for channels on distinct nets (e.g. a supply rail vs
  a DAC output).
- **A genuine input→output converter is a `REGULATOR`, not a mixed part.**
  If the part draws on an input rail and supplies a *derived* output rail
  (with a current gain between them), model it with a single `REGULATOR`
  channel — see below — rather than a hand-paired SOURCE + SINK.
- **Current-output loads:** a `SOURCE` fixes voltage, not current. To impose
  a known DAC output current, set it on the **load** `SINK` at the far end
  of the output net; the DAC-output SOURCE then supplies exactly that
  current through the output copper, and the trace IR drop shows on that
  rail.

### `SOURCE` vs `REGULATOR` — when to use which

A `SOURCE` represents a supply *edge* — the voltage just appears on the
specified net and the solver assumes whatever is upstream of it is infinite.
Use it for a connector pin, a battery terminal, or any spot where you don't
care what's upstream.

A `REGULATOR` represents an *on-board regulator* (LDO, buck, etc.) — it
pins its output net at `PDN_V` AND pulls current from its input net
proportional to the output current. Use it when both the input AND the
output rail are part of the PDN you're analyzing, so you can see the
voltage drop the regulator causes on its input side too.

Set `PDN_REGULATOR_TYPE` (`LDO` or `SMPS`) and optionally
`PDN_REGULATOR_EFFICIENCY` to auto-compute gain, or set `PDN_GAIN` manually.
Use **Adaptive SMPS gain** in the viewer (or `--adaptive-regulator-gain` on
the CLI) to refine SMPS gain from the solved input voltage.

| Regulator type            | Gain                                              |
|---------------------------|---------------------------------------------------|
| LDO                       | `1.0`                                             |
| SMPS (initial)            | `Vout / (Vin_nom × efficiency)`                   |
| SMPS (adaptive re-solve)  | `Vout / (Vin_solved × efficiency)`                |

Worked example — a board with a 5V connector input feeding a 3V3 LDO:

```text
J1 (input connector):
  PDN_ROLE   = SOURCE
  PDN_V      = 5
  PDN_P_NET  = +5V
  PDN_N_NET  = 0V

U2 (3V3 LDO):
  PDN_ROLE           = REGULATOR
  PDN_REGULATOR_TYPE = LDO
  PDN_V              = 3.3
  PDN_OUT_P_NET = +3V3
  PDN_OUT_N_NET = 0V
  PDN_IN_P_NET  = +5V
  PDN_IN_N_NET  = 0V

U5 (3V3 load — your IC):
  PDN_ROLE  = SINK
  PDN_I     = 500mA
  PDN_P_NET = +3V3
  PDN_N_NET = 0V
```

With this setup the solver models 500 mA flowing from `+3V3` into U5
(showing drop on the +3V3 copper between U2 and U5) AND 500 mA being
drawn from `+5V` into U2 (showing drop on the +5V copper between J1
and U2). Replacing U2 with a `SOURCE` would zero out that second drop
because no current would flow through the +5V copper.

If every rail on your board comes in pre-regulated (no on-board
regulators), you'll never need `REGULATOR` — `SOURCE` for inputs and
`SINK` for loads is the whole story.

Then run the all-in-one solve + viewer:

```sh
python FYPA.py gui path\to\YourBoard.PrjPcb
```

Or use the individual subcommands:

```sh
python FYPA.py extract     YourBoard.PrjPcb           # raw record summary
python FYPA.py annotations YourBoard.PrjPcb           # parsed PDN_* directives
python FYPA.py geometry    YourBoard.PrjPcb           # per-layer copper summary
python FYPA.py load        YourBoard.PrjPcb           # full pipeline, readiness report
python FYPA.py solve       YourBoard.PrjPcb out.pkl   # solve + pickle
python FYPA.py show        out.pkl                    # open viewer on a saved pickle
python FYPA.py paraview    out.pkl out_dir\           # export to ParaView VTK (writes into a folder)
python FYPA.py gerber-gui  path\to\gerber_folder      # import a board from Gerber + Excellon files
```

### Editor mode — sources / sinks without touching the schematic

You don't have to round-trip through the Altium schematic to set up an
analysis. The viewer has an **editor mode** for placing the PDN directly on
the board:

- Click a component to attach a `SOURCE` or `SINK` to it, or drop a *free
  marker* on any copper to load a net with no component in the way.
- Set the role, voltage / current, and net(s) in the side panel — single-net
  or two-net, the same choices the `PDN_*` parameters give you.
- A component that already carries schematic `PDN_*` directives can be
  *unlocked* and overridden, for quick "what-if" values without editing
  Altium.
- Hit **Resolve** to re-run the solver with your edits applied — it reuses
  the cached design extraction, so it's far quicker than a cold load.

Editor edits are stored in a small `.fypa` project file beside the board, so
they (and the linked solve) survive a save / reopen and never modify the
Altium source files. A step-by-step walkthrough will follow in a separate
user guide.

### Solve cache

On the first run it solves the
FEM and saves the result to `FYPA/.cache/<project>_<hash>.pkl`
along with a fingerprint of every input that affects the solve:

* The `.PrjPcb` plus every `.PcbDoc` / `.SchDoc` it references
  (parsed from the `DocumentPath=` lines)
* The tool's own solver-affecting source files (`fypa/altium/extract.py`,
  `fypa/altium/annotations.py`, `fypa/altium/loader.py`,
  `fypa/gerber/extract.py`, `fypa/gerber/loader.py`,
  `fypa/altium_geometry.py`, `fypa/cli.py`, `fypa/lean_solution.py`, and the
  modified `pdnsolver/` modules — `problem.py`, `mesh.py`, `solver.py`)

On subsequent runs the fingerprint is recomputed and compared. If
nothing has changed the cached solution is reused — typical startup
drops from ~10–60 s (full solve) to under a second. Editing any
referenced PCB / Sch file or any of the tool's source files
automatically invalidates the cache.

To force a fresh solve, pass `--no-cache`:

```sh
python FYPA.py gui YourBoard.PrjPcb --no-cache
```

To clear all cached solutions, delete the `.cache/` directory inside
the tool folder — they'll regenerate on the next run.

### Launching directly from Altium

A small DelphiScript ([Run_FYPA.pas](packaging/Run_FYPA.pas)) is bundled
that fires the tool against whichever `.PrjPcb` is focused in Altium —
no need to copy paths to a console. One-time setup:

1. **Point the script at this directory.** Open `packaging/Run_FYPA.pas`
   in a text editor and check the `SCRIPT_DIR` constant near the top:
   ```pascal
   SCRIPT_DIR = 'C:\path\to\FYPA';
   ```
   Change it to wherever you cloned the repo. The script expects to find
   `<SCRIPT_DIR>\.venv\Scripts\python.exe` and
   `<SCRIPT_DIR>\FYPA.py`, so the venv from
   [Installation](#installation) needs to live inside that directory.

2. **Optionally change the subcommand** via the `SUBCOMMAND` constant.
   Defaults to `gui` (solve + open viewer). Set to `load` for a
   solve-readiness report, `annotations` to dump the parsed `PDN_*`
   directives, `extract` for a raw-record summary, etc.

3. **Register the script in Altium.** *DXP > Scripting System > Script
   Projects*, add a new script project (or open an existing one) and
   add `packaging/Run_FYPA.pas` to it.

4. **Run it.** With a `.PrjPcb` open and focused in the *Projects*
   panel, right-click the `Run` procedure in the Script Editor and
   choose *"Run Script"*. A console window opens and runs:
   ```
   <SCRIPT_DIR>\.venv\Scripts\python.exe FYPA.py gui <FocusedPrjPcb>
   ```
   The console stays open after Python exits (uses `cmd /K`) so any
   tracebacks are readable. Close the console window when you're done.

The script will surface a clear `ShowError` dialog if any prerequisite
is missing — wrong path, no project open, project unsaved, or the
focused project isn't a `.PrjPcb`.

## Building a standalone executable

The project can be packaged into a self-contained Windows folder using
[PyInstaller](https://pyinstaller.org). The recipient needs no Python
installation — they just extract a zip and double-click the `.exe`.

### Prerequisites

PyInstaller is in the opt-in `build` dependency group (not installed by
default `uv sync`) because it is only needed by whoever is doing the build,
not by users of the tool. Pull it in once:

```sh
uv sync --group build
```

### Building

From the project root, double-click **`packaging\build_dist.bat`** (or run
it from a terminal). It will:

1. Activate the venv
2. Auto-install PyInstaller if it is not already present
3. Wipe any previous `build/` and `dist/` folders
4. Run `pyinstaller packaging\FYPA.spec`
5. Copy `README.md` into the staged output so it travels with the bundle
6. Delete the intermediate `build/` folder
7. Zip the staged `dist\FYPA\` folder into a single distributable archive
8. Delete the unzipped staging folder so only the zip remains in `dist\`

The first build takes 3–5 minutes while PyInstaller scans all packages.
Subsequent builds are quicker. The final artefact is:

```
dist\
  FYPA.zip          ← share this single file
```

Inside the zip:

```
FYPA\
  FYPA.exe
  README.md
  _internal\
    [DLLs, .pyd files, Python runtime, ...]
    assets\
```

### Distributing

The standard distribution channel is **GitHub Releases** — tag a version
(e.g. `v0.02`), draft a release on GitHub, and upload `dist\FYPA.zip`
(rename it to include the version, e.g. `FYPA_v0.02.zip`) as a release
asset. Users follow the
[Download (prebuilt Windows binary)](#download-prebuilt-windows-binary)
instructions above.

The zip can also be sent directly if you don't want to publish a release.

Cached solves (`.cache\`) are written next to `FYPA.exe`, so they survive
re-extracting a new build over the old folder.

### Customising the build

All PyInstaller settings live in **`packaging\FYPA.spec`**. Notable options:

| Setting | Default | Notes |
|---|---|---|
| `console=True` | `True` | Shows a terminal window — keeps CLI subcommands usable and errors visible on crash. Set to `False` for a GUI-only distribution with no console. |
| `datas` | `assets` | Add extra data files/folders here if needed. |
| `hiddenimports` | PyOpenGL, PySide6 GL, scipy, matplotlib | Extend if the app crashes on launch with `ModuleNotFoundError` — add the missing module name here. |
| `excludes` | `tkinter`, `cadquery`/`OCP`/`vtk`/`casadi`, unused PySide6 modules | Trims ~400 MB by dropping the `altium_monkey` STEP-bounds 3D stack (only used by write-side helpers FYPA never calls) and QML/PDF Qt modules. Extend if you find more bloat. |

After editing the spec, re-run `packaging\build_dist.bat` to rebuild.

## Architecture

```
FYPA.py                  Thin launcher shim (repo root)
fypa/                    Application package:
  cli.py                 CLI entry; orchestrates the pipeline
  altium/                Altium project front-end:
    extract.py             altium_monkey adapter → typed dataclasses (mm-based)
    annotations.py         Parses PDN_* parameters, resolves terminal pins
    loader.py              Orchestrator; assembles the padne Problem
  gerber/                Gerber + Excellon front-end:
    extract.py             Gerber/drill parser → copper geometry
    loader.py              Assembles the padne Problem from Gerber input
    import_ui.py           Layer-mapping / stackup import dialogs
  altium_geometry.py     Builds per-(layer, net) Shapely MultiPolygons
  _clipper_fuse.py       Clipper2-based polygon fuse helper (fast union path)
  editor_directives.py   Editor-mode SOURCE / SINK / SERIES directives
  project_file.py        Read / write the sidecar .fypa project file
  altium_viewer.py       Qt viewer (side panel, tabs, scale controller)
  gl_mesh_viewer.py      Custom QOpenGLWidget — mesh-on-GPU heatmap canvas
  paraview_export.py     ParaView VTU export
  spacemouse_nav.py      3Dconnexion SpaceMouse integration
  navlib_camera.py       NavLib camera adapter for the SpaceMouse
  log_buffer.py          In-memory log capture for the Messages tab
  lean_solution.py       Compact numeric solution (cache / pickle payload)
pdnsolver/               Vendored fork of padne (FEM solver + mesher)
packaging/               PyInstaller spec, build script, Altium launcher
```

## How the GL viewer works

The FEM triangle mesh (vertices, indices, per-vertex scalar values) lives
in OpenGL vertex / index / value buffers. The vertex shader applies a
model-view-projection matrix and normalises the per-vertex value into
`[0, 1]` using current `vmin`/`vmax` uniforms; the fragment shader looks
up the colour from a 1-D RGBA8 LUT texture (sampled from matplotlib's
`viridis`).

Pan / zoom are pure matrix uniform updates. Colour-scale drag is a uniform
update. Layer / rail / mode changes are a re-upload of the value VBO only
(positions and indices are unchanged within a layer/rail). The CPU keeps a
matplotlib `LinearTriInterpolator` alongside for the hover-probe lookup and
Voltage Drop reference computation, but never touches the GPU rendering
path.

**3Dconnexion SpaceMouse:** optional support via the official NavLib on
Windows/macOS (`uv sync --extra spacemouse` plus [3DxWare](https://3dconnexion.com/service/drivers.html))
or `spacenavd` on Linux (`sudo apt install spacenavd libspnav0`). Translation
pans; push/pull zooms; rotation orbits in 3D mode. Active while the FYPA
window is open so 3Dconnexion Settings keeps the FYPA profile selected.

Marker overlays (SOURCE / SINK / SERIES / REGULATOR / VIA) and the title /
legend chips are drawn via `QPainter` inside `paintGL` so they share the
GL paint engine — no Qt raster-fallback compositor.

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE) and [NOTICE](NOTICE) for upstream
attributions. The vendored padne fork's modifications are documented in
[pdnsolver/CHANGES.md](pdnsolver/CHANGES.md).
