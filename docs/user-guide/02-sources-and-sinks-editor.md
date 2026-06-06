# 2. Sources and sinks in the FYPA editor

The FYPA viewer has an **editor mode** that lets you place sources and
sinks directly on the board, without modifying the Altium schematic. This
is useful for:

- Running "what-if" current or voltage sweeps without round-tripping
  through Altium.
- Annotating a board whose schematic you do not want (or are not allowed)
  to modify.
- Overriding a single schematic `PDN_*` directive for one analysis,
  while leaving the original in place on disk.

Editor edits are stored in a small JSON project file (`.fypa`) that sits
beside the board. The Altium source files are never written to.

> Read [Section 1](01-sources-and-sinks.md) first if you have not. The
> underlying concepts — sources, sinks, P / N nets, single-net vs two-net
> — are the same. Only the entry method differs.

## 2.1 Schematic or editor — which to use

Both methods produce identical solver inputs. Choose by intent:

| You want…                                                         | Use this   |
|-------------------------------------------------------------------|------------|
| The PDN to be part of the design intent, versioned with the project | Schematic  |
| The same annotations to apply on every future build of this board | Schematic  |
| Quick "what-if" sweeps without touching the schematic             | Editor     |
| To annotate someone else's board (read-only schematic)            | Editor     |
| To override one schematic directive for a single analysis         | Editor (unlock — see 2.5) |

The two can also be mixed: a board can carry schematic `PDN_*` on most
parts and editor directives on a few extras, and the solver will treat
them identically.

## 2.2 Entering editor mode

Open the project in the viewer as usual (see Section 1.6 for the launch
command), then either:

- Click the **edit-mode toggle** button at the top-left of the
  viewport, **or**
- Press the **`E`** hotkey.

![Editor toggle button in the top-left of the viewport](screenshots/02-editor-toggle.png)

Three things change when editor mode is active:

1. The viewport background dims slightly, and copper not connected to
   the current selection is shown faded — so the rail you are about to
   touch is the visible one.
2. Two extra buttons appear next to the toggle: a red up-triangle
   (**SOURCE**) and a blue down-triangle (**SINK**). These are the free
   marker buttons, covered in 2.4.
3. The **PDN Editor** panel slides in on the right-hand side.

Press `E` again — or click the toggle off — to leave editor mode. Any
edits you applied remain in the project; only the transient selection
is cleared.

## 2.3 Attaching a source or sink to a component

This is the most common path: pick a real PCB component, choose its
role, and fill in the form.

**Step 1 — Click the component on the board.**

In editor mode, single-click the component's footprint in the viewport.
The form on the right populates with the component designator at the
top.

![Component selected, form populated](screenshots/02-component-selected.png)

> If the component already carries `PDN_*` parameters from the
> schematic, the form opens in read-only mode with an **🔓 Unlock to
> edit** button instead. Jump to 2.5 for that flow.

**Step 2 — Pick the role.**

The **PDN role** dropdown offers `SOURCE`, `SINK`, and `SERIES` (the
last is only available when a component is selected, since a series
element bridges two nets and needs a physical part to anchor to).

**Step 3 — Enter the value.**

The value field's label changes with the role:

| Role     | Field label       | Example   |
|----------|-------------------|-----------|
| `SOURCE` | Voltage (V)       | `5`       |
| `SINK`   | Current (A)       | `0.5`     |
| `SERIES` | Resistance (Ω)    | `0.01`    |

Values are plain numbers in SI units — no suffixes (`mA`, `kΩ`) here, in
contrast to the schematic parameters. Use `0.5` for 500 mA, `0.01` for
10 mΩ.

**Step 4 — Choose the current model.**

Two radio buttons control how the directive closes its current loop:

- **Single net (point-to-point)** — the directive's return is an
  ideal 0 V node, so the solve reports only the IR drop on the chosen
  net. Use this when there is no real return reference on the board
  (e.g. tracing power on a single rail to a high-side switch).
- **Two nets (full current-path loop)** — current flows through real
  copper on both the supply net and the return net. Use this for a
  normal supply-rail analysis.

`SERIES` always bridges two real nets, so its model is locked to
two-net.

**Step 5 — Pick the net(s).**

- In two-net mode, both **P net** and **N net** dropdowns are shown.
  P net lists the nets this component is connected to (when bound to a
  component) or every net on the board (free marker); N net always
  lists every net on the board.
- In single-net mode, the second dropdown is hidden and the remaining
  one is just labelled **Net**.

**Step 6 — (SINK only) Optional Min V.**

If the role is `SINK`, a **Min V (V)** field appears. This is the
editor-mode equivalent of the schematic `PDN_MIN_V` parameter — the
minimum acceptable rail voltage at this sink's P pins. Leave it blank
to skip the check; fill it in to have the Nodes table flag any pin
below this voltage in red after the solve.

**Step 7 — Click Apply.**

The status line at the bottom of the form turns green: *Applied — press
Resolve to re-solve.* A marker appears on the viewport showing the new
directive, the connected copper is highlighted, and the project is
marked dirty.

To delete the directive again, select the same component and click
**Remove**.

## 2.4 Dropping a free marker on copper

When the rail you want to load has no convenient component to anchor to
(a test pad, mid-track point, a stub of copper), drop a **free marker**
instead. A free marker is a SOURCE or SINK that lives at a specific
(x, y) point on a specific layer, not attached to any component.

**Step 1 — Click the red triangle (SOURCE) or blue triangle (SINK)
button.**

The button highlights to show it is armed. The status bar reminds you:
*Click copper to drop a free source / sink.*

![Free marker buttons in the toolbar](screenshots/02-free-marker-buttons.png)

**Step 2 — Click any copper area in the viewport.**

A marker is placed at the click point. The right-hand form populates
the same way as for a component selection.

**Step 3 — Fill in the form and Apply.**

The flow is identical to 2.3, with two differences:

- **SERIES is not offered** — a free marker is a single point on one
  net, which cannot express a two-net bridge. Use a component for
  SERIES.
- A **location** sub-form appears at the top of the panel with the
  marker's layer (read-only) and editable X / Y boxes — you can also
  drag the marker on the board with the mouse, and undo / redo the
  most recent moves with the buttons there.

> A free marker can be moved later but not changed to a different
> layer. To re-place it on another layer, **Remove** it and drop a new
> one.

## 2.5 Overriding a schematic directive

Selecting a component that already has `PDN_*` parameters from the
schematic opens the form in read-only mode with a summary of the
schematic values and an **🔓 Unlock to edit** button. You can select
the component either by clicking its footprint or by clicking the
SOURCE / SINK marker the schematic directive draws on the board — the
marker is the easier target, and it works even when the footprint sits
on a side whose copper is hidden.

![Read-only schematic info with unlock button](screenshots/02-unlock.png)

Clicking **Unlock** swaps the read-only view for the editable form,
pre-populated from the schematic values. From here it behaves like a
fresh editor directive — change whatever you need, click **Apply**,
then **Resolve**.

A status line in the form turns orange while unlocked: *Unlocked —
Apply replaces the schematic directive on the next resolve.* After
Apply it changes to: *Overrides the schematic directive.*

> The schematic file on disk is **never modified**. The override lives
> only in the `.fypa` project file. To revert to the schematic value,
> select the component and click **Remove** — the schematic directive
> takes over again on the next resolve.

## 2.6 Re-solving and saving

Editor edits do not run the solver themselves — they queue up changes
that the **Resolve** button then applies.

**Resolve.** A green **↻ Resolve** button appears at the top-left of
the viewport whenever there are unsolved edits. Click it to re-run the
solver with the current editor directives applied. The resolve reuses
the cached design extraction (geometry, net lookup, mesh) so it is
significantly faster than a cold load — typically a few seconds rather
than the tens of seconds of the first launch.

![Resolve button visible after an edit](screenshots/02-resolve-button.png)

**Save.** Editor edits and the resolved solution are not written to
disk until you save. Press **`Ctrl+S`** to bring up the save dialog:

- **Project only** (`S`) — writes the `.fypa` file with your editor
  directives.
- **Project + solution** (`A`) — also writes the latest solver run as
  a sibling pickle, so reopening the project will skip the re-solve.

The first time you save, a file picker asks where to put the `.fypa`.
On subsequent saves it overwrites the existing file silently. *File >
Save Project As…* writes to a new location.

> Closing the viewer with unsaved edits prompts before discarding
> them. The Resolve action itself does not save — you can resolve as
> many times as you like during one session and only save when you
> are happy with the result.

## 2.7 The `.fypa` project file

The `.fypa` is a small human-readable JSON document. It contains:

- The path to the Altium `.PrjPcb` and `.PcbDoc` it was derived from.
- Paths to the design-info and solution pickles (stored relative to
  the `.fypa` when they share its drive, so a project folder is
  portable).
- The list of editor directives — one entry per source / sink /
  series element you placed.
- Viewer settings (current layer, rail, display mode, overlay
  colours).

Opening a `.fypa` from *File > Open Project File…* (or by passing it
on the command line in place of a `.PrjPcb`) restores the viewer
exactly as you left it, including all editor edits and — if you saved
"Project + solution" — the most recent solved result, without
re-running the FEM.

> The pickles are referenced, not embedded. If you move a `.fypa` to
> a different machine, copy the sibling `_solve.pkl` and the cached
> `design-info.pkl` (under the FYPA `.cache/` folder, named after the
> project) along with it. The Altium source files only need to be
> present if you intend to **re-solve** on the new machine; pure
> viewing works from the pickles alone.

## 2.8 Troubleshooting

| Message or symptom                                                           | Likely cause                                                                 | Fix                                                                          |
|------------------------------------------------------------------------------|------------------------------------------------------------------------------|------------------------------------------------------------------------------|
| Resolve button never appears                                                  | No editor edits are pending, or none have been Applied yet.                  | Edits must be **Applied** (not just typed into the form) to mark the project dirty. |
| `Editor rail '+5V': single-net SINK(s) with no SOURCE — no current can flow` | A single-net sink was placed on a rail with no single-net source.            | Add a single-net SOURCE on the same rail, or switch the sink to two-net mode. |
| `P net '+5V' not found on the board; skipped`                                | The chosen net name does not exist in the current PCB extraction.            | Re-check the net name against the dropdown options — only nets present on the PCB are listed. |
| Form opens read-only with an Unlock button even though I want a fresh directive | The component already has a schematic `PDN_*` directive.                   | Click **Unlock** — see 2.5.                                                  |
| `SERIES` option missing from the role dropdown                                | A free marker is selected (SERIES is component-bound only).                  | Click the component instead, or pick a different role.                       |
| Marker dropped on the wrong layer                                             | Free markers are placed on the currently active layer.                       | Select the marker and use the **Layer** drop-down in its Location block to move it to another layer carrying the same net at that spot. If the net only exists on one layer there, remove the marker, switch the active layer in the side panel, and drop a new one. |

## Next steps

The editor covers SOURCE, SINK and SERIES directives. REGULATOR
directives, with their four terminals and gain factor, are still
schematic-only — see the [main README](../../README.md#source-vs-regulator--when-to-use-which)
for that flow.

Once you have the PDN set up — schematic, editor, or a mix — the rest
of the workflow is reading and acting on the results in the viewer.
The viewer tour (layers, rails, display modes, the Nodes and Vias
tables, colour-scale clamping, hover probes) is covered in a later
section.
