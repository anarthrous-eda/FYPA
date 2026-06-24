# 1. Sources and sinks — from Altium to FYPA

Before FYPA can solve your power-delivery network, it needs to know two
things about it:

- **Where the power enters the board** — the *source* (a connector,
  battery, regulator output, etc.).
- **Where the power is consumed** — the *sinks* (ICs, modules, LEDs,
  motors).

There are two ways to give FYPA this information:

- **In the Altium schematic** — add `PDN_*` parameters to the relevant
  components. The annotations live with the design and travel with the
  project files. This is the method covered in this section.
- **In the FYPA editor** — place sources and sinks directly on the board
  in the FYPA viewer, without touching the Altium schematic. The
  annotations are stored in a small `.fypa` project file beside the board.
  Covered in [Section 2 — Sources and sinks in the FYPA editor](02-sources-and-sinks-editor.md).

Use the schematic method when the PDN is a permanent part of the design
intent. Use the editor when you want to experiment, run quick "what-if"
sweeps, or annotate a board whose schematic you would rather not modify.

This section walks through annotating one source and one sink on a small
example, then importing the project into FYPA.

> **You will need:**
> - An Altium project (`.PrjPcb`) that compiles cleanly.
> - At least one component that supplies power to the board.
> - At least one component that consumes power from that supply.
> - FYPA installed (`python FYPA.py --help` should print a help screen).

## 1.1 The parameter shape

Every source or sink uses the same four parameters on one component:

| Parameter   | Purpose                                          | Example     |
|-------------|--------------------------------------------------|-------------|
| `PDN_ROLE`  | `SOURCE` or `SINK`                               | `SOURCE`    |
| `PDN_V` or `PDN_I` | Voltage (sources) or current (sinks)      | `5V` / `500mA` |
| `PDN_P_NET` | The positive / supply net name                   | `+5V`       |
| `PDN_N_NET` | The return / ground net name                     | `0V`        |

Net names must match the schematic exactly — same case, same punctuation.

> The walkthrough below adds the parameters to a placed instance, which
> is the right starting point. For parts you expect to reuse across
> designs (a regulator family, a recurring connector, an MCU), it is
> usually worth adding the `PDN_*` parameters to the **library symbol**
> instead — every future placement then arrives pre-annotated, and only
> the per-design net names need filling in. The mechanics are the same;
> you are just editing the part in the `.SchLib` rather than on a sheet.

## 1.2 Annotating a source

Example: a 5 V connector **J1**, with rails labelled `+5V` and `GND`.

**Step 1 — Select J1 in the schematic.**

Open the schematic sheet containing J1 and single-click the component body.

**Step 2 — Open the Parameters panel.**

Press `F11` (or *View > Workspace Panels > SCH > Properties*) to open the
**Properties** panel. Scroll to the **Parameters** section.

![Parameters panel highlighted](screenshots/01-source-params-panel.png)

**Step 3 — Add the four parameters.**

Click **Add** below the parameter list and fill in each row:

| Name        | Value     |
|-------------|-----------|
| `PDN_ROLE`  | `SOURCE`  |
| `PDN_V`     | `3V3`      |
| `PDN_P_NET` | `+3V3`     |
| `PDN_N_NET` | `GND`      |

> `PDN_V` accepts plain numbers (`5`, `3.3`) or numbers with a `V` suffix
> (`5V`, `3.3V`). `PDN_I` accepts `0.5`, `500mA`, `0.5A`. Either form
> works.

The parameters do not have to be visible, they will be hidden by default

![Parameters panel filled out](screenshots/02-source-params-panel_filled.png)

**Step 4 — Save the schematic** (`Ctrl+S`).

## 1.3 Annotating a sink

Example: a microcontroller **U1** drawing 500 mA from the `+5V` rail.

Select U1, open the Parameters panel, and add four rows:

| Name        | Value     |
|-------------|-----------|
| `PDN_ROLE`  | `SINK`    |
| `PDN_I`     | `500mA`   |
| `PDN_P_NET` | `+3V3`     |
| `PDN_N_NET` | `GND`      |

Save the schematic.

> FYPA solves a linear system, so `PDN_I` scales the result directly:
> doubling the current doubles the voltage drop. Use the typical current
> for normal-operation analysis, or the maximum current for worst-case
> headroom.

### Optional: `PDN_MIN_V` — minimum acceptable voltage at the sink

A sink can carry an extra `PDN_MIN_V` parameter giving the lowest
voltage the part is allowed to see at its P pins. FYPA does not change
the solve when this is set — it adds **Min V**, **Margin** and
**Status** columns to the viewer's *Nodes* tab and flags any pin whose
measured voltage falls below `PDN_MIN_V` in red (`Status = FAIL`). Sinks
without `PDN_MIN_V` show `—` in those columns.

Add it alongside the other four parameters:

| Name        | Value     |
|-------------|-----------|
| `PDN_ROLE`  | `SINK`    |
| `PDN_I`     | `500mA`   |
| `PDN_P_NET` | `+3V3`    |
| `PDN_N_NET` | `0V`      |
| `PDN_MIN_V` | `3.2`     |

The example above flags any `+3V3` pin on this sink that solves below
3.2 V. Pick the value from the part's datasheet — typically the minimum
supply voltage in the operating-conditions table.

> `PDN_MIN_V` accepts the same value forms as `PDN_V` — `3.2`, `3.2V`,
> etc. For multi-channel sinks (an IC with several independent supply
> rails on one part), use `PDN<n>_MIN_V` per channel, matching the
> `PDN<n>_I` numbering.

## 1.4 Multi-pin parts

For a two-pin part such as J1, FYPA infers which pad is which from
connectivity. For an IC with multiple supply and ground pins, FYPA
groups **every pad** on that component that connects to `PDN_P_NET` as
one terminal, and every pad on `PDN_N_NET` as the other. A BGA with
twelve `+5V` balls and twenty `0V` balls works without any pad
enumeration.

To override the inferred pad set (e.g. to exclude a thermal pad), use
the `PDN_P_PINS` / `PDN_N_PINS` parameters documented in the
[main README](../../README.md).

## 1.5 Pre-import checks

Before launching FYPA:

- **Compile the project in Altium** (*Project > Compile PCB Project*).
  FYPA reads the same files Altium does; compile errors block the read.
- **Verify the net names** match the names actually on the PCB. Hover
  over a wire on the schematic to see the net name in the status bar,
  or open *Design > Netlist > Edit Nets* on the PCB to see the
  authoritative list.
- **Save all files** (`File > Save All`). FYPA reads from disk, not
  from unsaved Altium buffers.

### Blanket / Parameter Set workflow

To attach the same `PDN_*` parameters to a group of components without
placing them on every symbol:

1. Draw a **Blanket** around the components on the schematic.
2. Place a **Parameter Set** on the blanket edge and add `PDN_ROLE`,
   `PDN_V` / `PDN_I`, and `PDN_P_NET` / `PDN_N_NET` (or `PDN_NET`).
3. **Compile the project** and **synchronise with the PCB** so Altium
   copies the parameters onto each component (`Project > Compile PCB
   Project`, then apply the ECO).
4. Launch FYPA — it reads the `PDN_*` values from the **PCB component
   parameters** (not from the blanket graphic itself).

### Local net names (hierarchical / reused sheets)

`PDN_P_NET`, `PDN_N_NET`, and `PDN_NET` may use the **local net name
as it appears on the schematic sheet** where the directive applies
(for example `+3V3` on a child sheet). FYPA compiles the schematic
netlist and resolves the name to the correct PCB pads per component
instance. Physical PCB net names still work unchanged — FYPA tries a
direct PCB match first, then falls back to local-name resolution.

> On hierarchical designs you no longer need to look up the flattened
> PCB net name manually when the local sheet name is unambiguous on
> that sheet. If resolution fails, check that the project compiles
> cleanly and that the component’s schematic designator matches the
> PCB `source_designator`.

## 1.6 Importing into FYPA

### Option A — From the terminal

Open a terminal in the FYPA install directory and run:

```sh
python FYPA.py gui path\to\YourBoard.PrjPcb
```

FYPA will:

1. Read the `.PrjPcb` and every `.PcbDoc` / `.SchDoc` it references.
2. Find every component carrying `PDN_*` parameters (on the schematic
   symbol or on the PCB after a Blanket/Parameter-Set sync).
3. Build a 2-D copper geometry per layer, per net.
4. Run the FEM solve.
5. Open the interactive viewer.

The first solve on a fresh project typically takes ten seconds to a
minute, depending on board size and layer count. Subsequent launches on
the same unchanged project are served from cache in under a second.

> Using the prebuilt executable? Run `FYPA.exe gui path\to\YourBoard.PrjPcb`
> from a terminal in the folder containing `FYPA.exe`, or drag the
> `.PrjPcb` file onto `FYPA.exe` in Explorer.

### Option B — From inside Altium

A DelphiScript launcher (`packaging/Run_FYPA.pas`) runs FYPA against the
currently focused project. Setup is documented in the
[main README](../../README.md#launching-directly-from-altium). Once
registered, right-clicking *Run* in the Script Editor opens a console
window and launches the viewer on the focused `.PrjPcb`.

## 1.7 What you should see

The viewer opens showing one copper layer shaded by voltage. The side
panel lists:

- Every layer in the stackup.
- Every **rail** FYPA inferred from the `PDN_P_NET` / `PDN_N_NET` pairs
  (in this example, `+5V → 0V`).
- A **Nodes** tab with one row per terminal pin and its solved voltage.

Switching the display mode from *Voltage* to *Current Density* shows
the current flow across the copper from source to sink.

## 1.8 Troubleshooting

| Message or symptom                                | Likely cause                                                        | Fix                                                                          |
|---------------------------------------------------|---------------------------------------------------------------------|------------------------------------------------------------------------------|
| `No PDN_* directives found`                        | No component carries `PDN_ROLE`.                                    | Re-check that the schematic was saved. Parameter names are case-sensitive.   |
| `Net "+5V" not found on PCB`                       | The net named in `PDN_P_NET` does not exist on the routed board.    | Check spelling against the PCB net list (see 1.5).                           |
| `Open loop on net …` / source without sink         | A rail has a source but no sink, or vice versa.                     | Add the missing end. Every source needs at least one sink on the same rail.  |
| Viewer opens but the layer is blank                | The selected layer has no copper on the selected rail.              | Switch layers or rails in the side panel. If all are blank, check copper net assignments on the PCB. |

For a faster diagnostic without running the full solve, use:

```sh
python FYPA.py load path\to\YourBoard.PrjPcb
```

This prints a solve-readiness report covering parameter parsing, net
resolution, and geometry extraction, then exits.

## Next steps

The same parameter pattern extends to the other directive types:

- Multiple rails — annotate a source and at least one sink per rail.
- On-board regulators (LDO, buck) — use `PDN_ROLE=REGULATOR`.
- Fuses, sense resistors, ferrites — use `PDN_ROLE=SERIES`.
- "What-if" analyses without editing the schematic — use editor mode in
  the viewer.

Each of these is covered in a later section.
