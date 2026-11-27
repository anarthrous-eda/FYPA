# 9. What-If scenarios

A What-If lets you try a change to the board and see whether it helps or
hurts, **without losing the current result**. The current result is the
*base*. Every What-If is solved separately and compared only with the base,
never with another What-If.

Typical uses:

- **Cost-down:** would 1 oz inner layers (instead of 2 oz) still meet every
  load's minimum voltage?
- **Margin:** how much does 2 oz outer copper buy on the worst rail?
- **Environment:** what happens in a 70 °C enclosure, or with the
  electro-thermal model switched on?
- **Fabrication:** what if the fab plates vias to IPC Class 2 instead of
  Class 3, or the vias are filled with conductive paste?
- **More vias:** would six more stitching vias next to the regulator fix
  the worst load? (See 9.3, Punching vias.)

## 9.1 Opening the panel

The **What-If** button at the top-left of the heatmap, next to **↻
Solve**, shows and hides the panel on the right of the heatmap (hotkey
**W** does the same). The panel is always hidden when a design loads.

What-Ifs are a viewer-mode tool: in editor mode the button is hidden, and
entering editor mode closes the panel. Leave editor mode and press the
button to open it again.

The design has to be solved first: a What-If is always compared with the
base result.

## 9.2 Making a What-If

Press **New…**. Every field shows the base value greyed out; leave a field
blank to keep the base value, and fill in only what you want to try:

- **Physics and vias:** board temperature, copper resistivity and its
  temperature coefficient, plating thickness for through, blind, buried and
  micro vias, via fill mode and fill resistivity, the coupled
  electro-thermal solve, its heat transfer coefficient and heat spreading.
- **Stackup:** each copper layer's thickness in µm, with ½ / 1 / 1½ / 2 /
  3 oz quick picks (and **Set every layer to…**), and its resistivity.

A What-If is solved with the same design, the same directives and the same
mesh settings as the base, so the comparison reflects only your change.
Load currents, source voltages and copper shapes can't be changed in a
What-If. Vias can be added - see 9.3.

**Save** keeps it; **Save and Run** also solves it straight away. A What-If
can be saved with nothing changed, to add vias to it afterwards (9.3), but
it can't be run until it changes something. Pressing **Punch vias** with no
What-If selected makes a vias-only one (`Vias 1`) for you.

## 9.3 Punching vias

The Fixes tab can also make a vias What-If for you: **Try as what-if** on
its **Add via** fixes (section 5).

**Punch vias** in the panel adds vias to the selected What-If by clicking
the Board. It works in 2D on the base view: pressing it switches to both.

- **Click** on copper to add one via on that copper's net.
- **Drag** a box to add a grid of vias at the **Pitch** set beside the
  button, all on the net under the corner you started the drag from.
- **Drag a via you added** to move it, the same way as a SOURCE or SINK
  marker in Edit Mode. This works whenever the panel shows the What-If on
  the 2D Board's base view, whether or not **Punch vias** is on. It keeps
  its net, size and span, follows the cursor while the spot passes the
  rules below, and stops at the last spot that did. Moving a via clears
  the What-If's old result, so it shows **not run** until you run it again.
- **Click a via you added** while **Punch vias** is on to remove it; with
  it off, a click picks the via out in the list. **Shift-drag** a box to
  remove every added via inside it. **Clear (n)** removes them all.
- **Esc**, the button again, switching to 3D or to another view stops it.

**Undo** / **Redo** (or **Ctrl+Z** / **Ctrl+Y** while the panel is open,
outside Edit Mode) step back through the What-If's via changes: a click,
a whole grid, a move, a box removal or **Clear** is one step each. Undoing
a change made after a run brings that run's result back, as long as the
What-If hasn't been run again or had its clearance changed since.

The **list** under the panel buttons shows each added via: its net, X / Y,
size and span, and after a run its current. Click a row to zoom to the via
on the Board. Double-click X or Y to type an exact position; a position
that breaks the placement rules is refused, with the reason in a toast.

**Export** copies the list to the clipboard (paste it into a spreadsheet)
or saves it as CSV, with each via's net, X / Y in mm and mil, drill, pad,
start and end layer, kind and fill. X / Y are the coordinates Altium shows,
measured from the board origin, so the vias can be placed at exactly those
spots in the layout.

**Via** picks what is punched:

- **Nearest on net (auto)**, the default: each new via copies the drill,
  pad, layer span and fill (IPC-4761 type and material) of the nearest via
  already on that net. If the net
  has no vias it copies the board's most common via, and on a board with no
  vias it is a 0.3 / 0.6 mm through via.
- **One of the board's via types**, listed by drill / pad size with their
  kind, fill and how many the board has, most used first, for example
  `0.3 / 0.6 mm through (1234)` or `0.1 / 0.25 mm micro (56)`. A filled
  and an unfilled via of the same size are listed as two types, because a
  conductive fill lowers the via's resistance in the solve.

A type that doesn't run through every layer (blind, buried or micro) shows
a **Span** choice beside it: the layer pairs the board uses that type
between, such as `Top → L2` or `L7 → Bottom`. Only spans the board already
uses are offered; drill, pad and span can't be edited.

The line under the choice says exactly what the next via will be, the
toast after each click repeats it, and hovering an added via shows its
net, size and span.

A via may go through another net's pour or internal plane, such as a
ground plane poured across the whole board. The What-If does what the
layout tool does when it repours: every pour and plane of another net that
the via passes through gets a clearance hole around it. So a row of power
vias through a ground pour helps the power rail and perforates the ground,
and the comparison shows both.

**Clearance** sets the gap between an added via's pad and the copper
around it, so the hole is the pad diameter plus twice the clearance. It
starts at the board's PlaneClearance rule (20 mil when the board has
none), and each What-If keeps its own value. **Board rule** goes back to
the rule. Changing it clears the What-If's old result. On an internal
plane the hole is never smaller than the board's rule, because the plane
already cuts that much.

A via is refused, with the reason in a toast, when:

- its net has copper on fewer than two of the layers it spans, so it would
  carry no current;
- its pad would touch another net's tracks, pads, fills or regions that
  aren't part of a pour, which would be a short a repour can't clear;
- it would overlap another net's via or through-hole pad, or sit inside a
  via already on its net.

On a Gerber import nothing records which copper is a pour, so every
filled area counts as pour; tracks and pads still refuse.

Added vias are drawn as orange pads with a dark drill and an orange ring
at the clearance while the panel is open, and exist only in that What-If.
With **Show: What-if** the Vias tab lists them with the board's own vias,
tinted orange, and **Added by the what-if only** filters to just them. In
the Base and Difference views (which show the base result) they aren't in
the Vias tab. Its Run solves the board with them;
the base, the report and the solve caches never see them. Adding or
removing a via clears the What-If's old result. A What-If with added vias
can't be **made the base** (9.6), because the vias aren't on the board: add
them in the layout and re-import instead.

## 9.4 Running and viewing

Select a What-If and press **Run**. The solve takes about as long as the
base solve, and the base result is untouched. When it finishes, the viewer
switches to **Difference**.

The **Show** buttons pick what the whole viewer displays:

| Show           | What you see |
|----------------|--------------|
| **Base**       | The design's own result: the normal view. |
| **What-if**    | The selected What-If's result. Every tab (Nodes, Vias, Topology, Properties report, …) shows it, and an orange banner on the heatmap says so. |
| **Difference** | The base copper coloured by *what-if − base* for the current heatmap mode: Voltage, Voltage Drop, Current Density or Power Density. **Red is worse, blue is better**, and white is no change. The hover probe and the side-panel summary read the change too. |

When every change goes the same way, such as a hotter board, which can only
lower voltages, the colour key runs from the largest change to 0 using only
that half of the palette (for example −36 → 0 mV, red to white). When the
change goes both ways, the key is symmetric around 0. Opposite-sign changes
smaller than 1 % of the largest are treated as solver noise and drawn white.

In the Difference view, Voltage and Voltage Drop both show the change in
each net's voltage measured from its source return. Via Current and Copper
ROI can't be shown as a difference; the banner says so and the base is
drawn instead. Current arrows, series-part bars and via cylinders are hidden
in the Difference view, because they would show base values on a
difference scale. Hovering a via still reads its current in the bottom bar:
the base value for the board's own vias, and the What-If's value, labelled
**Added via (what-if)**, for a via the What-If added.

While a What-If is on screen, a chip on the menu bar, right of **Global
Focus**, says so on every tab: **VIEWING WHAT-IF** *name* when every tab
shows the What-If, and **WHAT-IF DIFFERENCE** *name − base* in the
Difference view, where the heatmap shows the difference and the other tabs
show the base result. Click the chip to open the What-If panel on the
Board; its **×** goes back to Base.

Editing directives, saving, exporting a report or running a solve always
switches the viewer back to **Base** first.

## 9.5 Reading the comparison

The panel's top table compares the headline numbers, base against What-If:

- **Worst sink drop**: the largest drop at any load pin.
- **Worst margin to min V**: the closest any load comes to its `PDN_MIN_V`
  (negative means it fails).
- **Min-V failures**: the number of load pins below their `PDN_MIN_V`.
- **Copper I²R loss**: total power dissipated in the copper.
- **Max temperature rise**: shown only when the electro-thermal model ran.

The **Rail** column says which rail each headline is about: the rail where
the What-If's worst case sits, with the base's in brackets when the worst
case has moved to another rail (hover for the exact pin), every rail with
a failure, or *All rails* for the copper loss and temperature. Each Δ is
marked **▲ better** or **▼ worse**.

The node table below lists every directive pin by the chosen measure
(**Drop**, **Voltage**, **Margin to min V**, **|J| at pin** or **Power
density**), worst change first. **Changed only** hides pins that didn't
move. For *Voltage*, a rise counts as better on a supply pin and worse on a
return (N) pin.

## 9.6 Managing What-Ifs

- **Edit…** (or double-click) changes a What-If. Changing its inputs clears
  its old result.
- **More ▾ › Duplicate** copies a What-If, for example to try ½ oz after
  1 oz.
- **More ▾ › Delete** removes it.
- **More ▾ › Make this the base…** copies its changes into the Properties
  tab's **Base properties** and makes it the base. If its result is
  current, that result becomes the base result straight away (no
  re-solve); if it has no result or is out of date, the design is
  re-solved. The previous base result is replaced, so save the project
  first if you want to keep it.

A What-If run against an older base is marked **out of date**. That happens
when the design, the directives or the base properties have changed
since it ran. Press **Run again** to compare it against the current base.
If the design has editor changes that aren't solved yet, Solve first so
that the base and the What-If come from the same design.

## 9.7 Saving

What-Ifs are saved in the project file (`.fypa`). Their results are copied
into a `<project>_whatifs` folder beside it, so the project folder stays
portable. Before the project is first saved, results are kept in the
project's cache folder.
