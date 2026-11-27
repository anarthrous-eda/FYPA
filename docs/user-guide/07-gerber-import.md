# 7. Importing a board from Gerber files

FYPA can analyse boards that did not originate in Altium. If all you have
is a fabrication Gerber set - RS-274X copper layers plus an Excellon
drill file - you can import them directly. The rest of FYPA (meshing,
solver, viewer, editor mode, project file) works exactly the same; only
the input changes.

## 7.1 When to use Gerber import

- You did not author the board and have no Altium project for it.
- You are analysing someone else's reference design or a fab-house
  output set.
- You have an Altium project but want to sanity-check FYPA against a
  pure-Gerber view of the same board.

If you do have an Altium project, prefer [Section 1](01-sources-and-sinks.md).
The Altium path carries net names, pads, components, and any schematic
`PDN_*` directives - Gerber carries none of those.

## 7.2 What you need

| File                                  | Required | Purpose                                                       |
|---------------------------------------|----------|---------------------------------------------------------------|
| Copper Gerber for every layer         | Yes      | Top, Bottom, and any inner copper                             |
| NC-Drill (Excellon) file              | No       | Vias / through-holes; without it, multi-layer rails cannot bridge across layers |
| Board outline Gerber (`.GKO`, `Edge.Cuts`, etc.) | No       | Sets the board boundary on the viewer; falls back to copper bounding box |
| Approximate stackup                   | Yes      | Total board thickness + copper weight, used to compute conductance |

FYPA recognises Altium (`.GTL` / `.GBL` / `.G<n>` / `.GKO` / `.GTO`),
KiCad (`F.Cu` / `B.Cu` / `In<n>.Cu` / `Edge.Cuts`), and several common
generic naming conventions automatically. You can correct any
miscategorised file in the import window's first step.

## 7.3 The import flow

**File → Import Gerber Files…** (`Ctrl+G`), in either the launcher window
or an already-open viewer, opens the **Import Gerber Files** window. The
steps are listed down the left; the current step is on the right.

- **< Back** always works, and nothing you entered is lost by going back.
- **Next >** only goes on once the current step has no problems. Problems
  are listed in red under the step and update as you edit; notes that don't
  block the import are shown in orange.
- You can click any step in the list that you have already reached. Steps
  you haven't reached yet, and steps after one that now has a problem, are
  greyed out.
- **Cancel** (or Esc, or closing the window) asks before throwing away the
  files you listed.

The steps:

1. **Files & roles.** Click **Add Files...** and multi-select every Gerber
   and drill file, or drop them onto the window. Click **Add Files...**
   again to bring in files from another folder; files already in the list
   are skipped. Select rows and click **Remove** to take files out. Each
   file's role is worked out from its name; change any that came out wrong:
   - **Top** / **Inner 1..N** / **Bottom** - copper layers (must have
     exactly one Top and one Bottom; inner layers must be numbered from
     Inner 1 with no gaps).
   - **Top Silk** / **Bottom Silk** - accepted but currently unused.
   - **Outline** - the board boundary (only the first such file is
     used).
   - **Drill (Excellon)** - every Excellon file is parsed. Without one,
     the step notes that copper on different layers won't connect.
   - **Ignore** - file is dropped from the import.

   Files outside the recognised extensions can still be added - they
   start as "Ignore" and you can re-assign them.
2. **Stackup.** One row per copper layer from step 1, pre-filled with:
   - 1.6 mm total board thickness.
   - 1 oz copper (0.035 mm per layer).
   - Dielectric split evenly across the gaps.

   Edit the per-layer copper / dielectric thicknesses; click
   **"Apply weight + re-distribute dielectric"** to reset after a
   change. The values feed directly into the FEM conductance for each
   layer, so they don't have to be exact - they just have to be in the
   right ballpark. If the layers don't add up to the total thickness you
   get a note, not a block (asymmetric stackups are common). If you go
   back and change the copper layers, the values you typed are kept for
   the layers that are still there.
3. **Review & import.** A summary of the copper layers and their files,
   the drill and outline files, how many files are ignored, the total
   thickness, and anything worth checking. Click **Import**. The import
   runs in the window with its progress shown on the page; **Cancel
   import** stops it and leaves you on this step. If the import fails, the
   error is shown here and you can go back, fix the files or stackup, and
   click **Try again**.

When the import finishes, the window closes and the **viewer opens** with
a blank heatmap (no solve has run yet) and the copper visible on every
layer.

FYPA remembers the files, layer mapping and stackup of each successful
import. To re-import the same set later (for example after re-exporting
the Gerbers), pick it from the start window's **Recent** panel: the
import re-runs straight away without the import window. If any copper,
drill or outline file has gone, FYPA lists the missing files and drops the
entry. To change the mapping or stackup, use **File → Import Gerber
Files…** again.

## 7.4 Adding sources and sinks

Gerber files have no schematic information, so the **only** way to add
PDN directives is the editor mode. The full editor walk-through is
[Section 2](02-sources-and-sinks-editor.md); the key differences for a
Gerber-imported board are:

- **Every copper island starts unnamed.** Gerber carries no nets, so
  every disjoint piece of copper appears as `(none)` until you give it
  a name. Use the **"Name copper"** flow (click on an island, give it a
  net name) before dropping a SOURCE / SINK / SERIES on it. A directive
  on un-named copper is silently dropped at solve time.
- **No "component-bound" directives.** The component picker in the
  editor panel is empty because Gerber has no PCB components - use
  free markers (the red SOURCE / blue SINK triangles) on copper islands
  instead.
- **Vias are reconstructed from the Excellon drill data only.**
  If you didn't supply a drill file, multi-layer rails will need either
  a SERIES directive bridging the two layers or a second source / sink
  on each layer.

Press **Resolve** to solve. The first Resolve replaces the
blank stub solution with a real heatmap.

## 7.5 Saving the project

`Ctrl+S` saves a `.fypa` project file alongside the Gerbers. The file
records:

- The Gerber and drill files used (paths relative to the `.fypa`).
- Your confirmed layer assignments.
- The stackup you entered.
- Every editor directive and copper-name rename you placed.

Opening the `.fypa` later via **File → Open Project File…** re-imports
the Gerbers using the saved settings - the import window is skipped.

## 7.6 Limitations

- **Via diameter is a heuristic.** Gerber + Excellon carry only the
  drill diameter, not the pad annulus, so FYPA models each via barrel
  as drill + 0.3 mm. If your design uses unusually large or small
  annular rings, expect a few-percent error in IR drop.
- **No IPC-4761 fill metadata.** All vias are modelled as plated
  through-holes with a hollow barrel; conductive-fill shunts (which
  the Altium path can detect and model) are not available here.
- **No automatic SERIES element detection.** Without a schematic,
  there's no way to know that two named copper islands are connected
  through a ferrite or sense resistor - you must place a SERIES editor
  directive between them explicitly.
- **No component-bound directives.** As above, every editor directive
  on a Gerber project is a free marker.

If a board hits any of these limits hard enough to matter, the
recommended path is to re-create the board in Altium and use the
schematic-based flow.
