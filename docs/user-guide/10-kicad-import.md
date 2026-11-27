# 10. Importing a KiCad design (beta)

FYPA reads KiCad designs directly: the board (`.kicad_pcb`) for the copper,
nets, stackup and parts, and the schematic (`.kicad_sch`) beside it for
symbol fields, Do-not-populate marks and power symbols. After the import,
everything works as it does for an Altium design: editor mode, Auto and
Manual setup, What-If, reports, Reload Design.

> **Beta.** KiCad import is new. It has been checked against KiCad's own
> Gerber output (copper area within 0.02 % per layer, same position) on a
> KiCad 7 board, and on a KiCad 10 board for connectivity (every net one
> piece of copper, no two nets touching) and a full solve, but not yet on
> many designs. Check the copper
> on the **Board** tab against KiCad, and report anything that differs. The
> **Design issues** panel carries a reminder note on every KiCad design.

## 10.1 Which KiCad versions

| Board file from | What happens |
|-----------------|--------------|
| KiCad 7, 8, 9, 10 | Read normally, including KiCad 10's new features (nets named in place, via protection and fill, design variants). |
| KiCad 6         | Read, with a warning: open and save it in KiCad 7 or newer if the copper looks wrong. |
| KiCad 5 or older | Refused: open the board in KiCad 7 or newer and save it first. |
| Newer than KiCad 10 | Read, with a warning that FYPA doesn't know that format yet. |

No KiCad installation is needed: FYPA reads the files itself.

## 10.2 Before you import

In KiCad:

1. **Fill all zones**: in the PCB editor, **Edit > Fill All Zones**
   (**B**). FYPA reads a zone's *filled* copper, which KiCad saves only
   after a fill. An unfilled zone is reported in Design issues and its
   copper is missing from the solve.
2. If you set up sources and sinks in the schematic (see 10.4), run
   **Tools > Update PCB from Schematic** (**F8**) so the fields reach the
   board.
3. **Save** the board and the schematic.

## 10.3 Importing

**File > Import KiCad Design (beta)...** (`Ctrl+K`), in the start window or
an open viewer. Pick the project (`.kicad_pro`) or the board
(`.kicad_pcb`): either works, and FYPA remembers the design by its board.

**File > Import KiCad Design (beta, Clean)...** (`Ctrl+Shift+K`) does the
same while ignoring any cached design info or solution and setting aside
the setup saved beside the board, like the Altium clean import.

The import then behaves like an Altium one: it solves straight away if
**Settings > General options > Solve automatically on Altium / KiCad
import** is ticked, and it appears in **Recent** with a **KiCad** tag.

**File > Reload Design** (`Ctrl+R`) re-reads the KiCad files, and the
**DESIGN CHANGED** chip on the menu bar appears when you save the board or
schematic in KiCad after FYPA has read them.

From the command line, pass the KiCad file wherever a `.PrjPcb` goes:

```sh
python FYPA.py gui   path\to\Board.kicad_pro        # import + viewer
python FYPA.py solve path\to\Board.kicad_pcb out.pkl
python FYPA.py load  path\to\Board.kicad_pcb        # readiness report
```

## 10.4 Defining sources and sinks in KiCad

The `PDN_*` parameters are the same as in Altium (see
[Section 1](01-sources-and-sinks.md) for the full list). In KiCad they are
**symbol fields**:

1. Open a symbol's properties (**E**) in the schematic.
2. Add a field with **+** for each parameter: name `PDN_ROLE`, value
   `SINK`, and so on. Untick **Show** to keep them off the sheet.
3. For a part you reuse, add the fields to the library symbol instead.
4. Run **Tools > Update PCB from Schematic** (**F8**): KiCad copies the
   fields onto the footprints, which is where FYPA reads them. Fields typed
   directly on a footprint in the PCB editor work too.
5. Save, then **File > Reload Design** in FYPA.

A `PDN_*_NET` field can name the net as the board does (as shown in
KiCad's PCB editor, for example `+3V3` or `/Power/VBUS`) or as the
schematic does where the part sits: a local label (`VBUS` on the Power
sheet), a hierarchical label (on a sheet used twice, each use resolves to
the rail its parent sheet connects), a global label or a power symbol. FYPA
traces the schematic's wires and labels to find the board net; the Messages
tab log lists each name it translated. A name it can't place is reported in
Design issues as before.

Example, a 500 mA load on `+3V3`:

| Field       | Value  |
|-------------|--------|
| `PDN_ROLE`  | `SINK` |
| `PDN_I`     | `500mA` |
| `PDN_P_NET` | `+3V3` |
| `PDN_N_NET` | `GND`  |

You don't have to use fields at all: the FYPA editor, Auto setup and
Manual setup work the same on a KiCad design (Help > **Set up sources and
sinks...**). Their setup is saved beside the board as
`<Board>.fypa-setup.json` and restored on the next import.

## 10.5 What FYPA reads

| From KiCad | Used as |
|------------|---------|
| Tracks, arcs, vias (through, blind, buried, micro) | Copper and layer connections |
| Via **remove unused pads** settings (*Remove unconnected*, *Keep start and end*, *Start and end only*) | A via has no pad, and no connection, on the layers KiCad removes its pad from |
| Via protection (KiCad 10: tenting, covering, plugging, filling, capping, per via or the board default) | The IPC-4761 type shown on the Vias tab and used for the fill model |
| Filled zones | Copper pours (What-If via punching treats them as pours) |
| Copper drawn as shapes: board shapes on a copper layer, footprint shapes, custom pad shapes | Copper. A footprint shape takes the net of the pad it touches. |
| Pads (including trapezoid and chamfered), plated and non-plated holes and slots | Pads, barrels, holes, at their exact outline |
| Board stackup (**Board Setup > Physical Stackup**) | Copper and dielectric thickness, Dk and Df. Without one: 1 oz copper on a 1.6 mm board. |
| `Edge.Cuts` | Board outline |
| Footprint fields (`PDN_*`, Value) | Directives and part values |
| Do not populate (footprint attribute or schematic symbol) | The part is left out, as an Altium variant's Not Fitted part is |
| Design variants (KiCad 10) | The **Assembly variant** picker on the Properties tab, as for Altium (see 10.6) |
| Schematic wires, labels, sheets and power symbols | Which board net a `PDN_*_NET` name means (see 10.4) |
| Net-tie footprints | Joined as net ties |
| Pad pin names, schematic power symbols | Auto setup's pin and rail recognition |

## 10.6 Design variants (KiCad 10)

KiCad 10's design variants appear in the **Assembly variant** picker on the
Properties tab, exactly as an Altium project's do: a variant's Do not
populate parts are left out (never bridged), and its field overrides
replace the base values, so a variant can change a `PDN_I` or `PDN_V`.
KiCad doesn't save which variant you last selected, so **KiCad default**
is the base design; pick a variant to analyse it. FYPA reads the variants
from the board, so run **Update PCB from Schematic** after editing them.

## 10.7 Coordinates

KiCad's Y axis points down the page; FYPA's points up, as Altium's does.
FYPA turns the board the right way up when it reads it, so the board looks
as it does in KiCad, but a Y coordinate FYPA shows is KiCad's with the sign
changed.

The **What-If > punched vias** table, its typed X / Y, and its clipboard
and CSV export use KiCad's own coordinates (Y down), so they can be placed
in the layout as they are. Other coordinate readouts (probe, go-to, editor
markers) show FYPA's Y.

## 10.8 Known limits in the beta

- **Custom pads** connect sources and sinks at their anchor pad; the
  shapes drawn on the pad are copper on its net. Trapezoid and chamfered
  pads likewise connect at the rectangle inside their outline.
- **Footprint copper shapes** that touch no pad, or pads of two nets, have
  no net. They show as unnamed copper, which you can name in editor mode.
- **Via fill material**: KiCad records that a via is filled, not what with,
  so a filled via is modelled as non-conductive fill unless
  **Settings > Conductive via fill > Fill mode** says otherwise.
- **Per-layer via sizes** (KiCad 9 via stacks) use the front-layer size on
  every layer; **unused through-hole pad copper** removed by KiCad is kept
  (it touches nothing, so it carries no current either way).
