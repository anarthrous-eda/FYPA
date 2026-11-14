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

## 9.1 Opening the panel

The **What-If** button at the top-left of the heatmap, next to **↻
Resolve**, shows and hides the panel on the right of the heatmap (hotkey
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
What-If.

**Save** keeps it; **Save and Run** also solves it straight away.

## 9.3 Running and viewing

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
difference scale.

Editing directives, saving, exporting a report or running a solve always
switches the viewer back to **Base** first.

## 9.4 Reading the comparison

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

## 9.5 Managing What-Ifs

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
If the design has editor changes that aren't solved yet, Resolve first so
that the base and the What-If come from the same design.

## 9.6 Saving

What-Ifs are saved in the project file (`.fypa`). Their results are copied
into a `<project>_whatifs` folder beside it, so the project folder stays
portable. Before the project is first saved, results are kept in the
project's cache folder.
