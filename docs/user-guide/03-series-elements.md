# 3. Series elements — fuses, ferrites, sense resistors

A `SERIES` directive tells FYPA that a real part bridges two nets with a
known resistance. Common examples:

- A **fuse** (DC resistance from the datasheet).
- A **ferrite bead** (DC resistance — FYPA solves DC, not AC).
- A **sense / shunt resistor** (the value printed on the part).
- A **0 Ω jumper** (use the actual measured resistance, not zero — see
  the note below).
- An **inductor's DCR** (the DC resistance of the windings).

Without a `SERIES` directive, FYPA sees two unconnected nets and the
solver cannot push current between them. With a `SERIES` directive, the
two nets are bridged by a lumped resistance, current flows, and the
voltage drop across the part shows up in the solve.

> Read [Section 1](01-sources-and-sinks.md) first if you have not — the
> parameter mechanics (where to add them, hiding them, library-symbol
> defaults) are the same.

## 3.1 The parameter shape

| Parameter   | Purpose                                                  | Example   |
|-------------|----------------------------------------------------------|-----------|
| `PDN_ROLE`  | `SERIES`                                                 | `SERIES`  |
| `PDN_R`     | The DC resistance in ohms (must be > 0)                  | `0.01`    |
| `PDN_P_NET` | The net on one side of the part — *optional for 2-pin parts* | `+5V_RAW` |
| `PDN_N_NET` | The net on the other side — *optional for 2-pin parts*   | `+5V`     |

Which side is `P` and which is `N` is arbitrary for a SERIES element;
the model is symmetric. The labels matter only when reading the result
in the viewer ("voltage on the `P` side of F1 is X V").

> A SERIES resistance of zero is rejected — the solver would treat it as
> a short and merge the two nets, which defeats the purpose of having a
> SERIES directive. For a true 0 Ω jumper, enter a small but realistic
> value (the link's measured resistance, typically a few milliohms).

## 3.2 Annotating a 2-pin part

Example: a 10 mΩ sense resistor **R7** between `+5V_RAW` (from the
input connector) and `+5V` (downstream of the resistor).

Select R7, open the Parameters panel, and add:

| Name        | Value     |
|-------------|-----------|
| `PDN_ROLE`  | `SERIES`  |
| `PDN_R`     | `0.01`    |

The P / N nets are inferred from the two pads' connectivity — FYPA sees
R7's pad 1 on `+5V_RAW`, pad 2 on `+5V`, and uses those automatically.

Save the schematic. R7 is now a bridge between the two rails, and FYPA
will report the IR drop across it (≈ 5 mV at 500 mA) in the solve.

## 3.3 Annotating a 3+ pin part

For a part with more than two pads (e.g. a fused power switch, a
common-mode choke), the auto-inference cannot pick the right two
terminals. Add `PDN_P_NET` and `PDN_N_NET` explicitly:

| Name        | Value          |
|-------------|----------------|
| `PDN_ROLE`  | `SERIES`       |
| `PDN_R`     | `0.05`         |
| `PDN_P_NET` | `+12V_IN`      |
| `PDN_N_NET` | `+12V_FUSED`   |

FYPA will then collect every pad on the part that is on `+12V_IN` as
the P terminal, every pad on `+12V_FUSED` as the N terminal. The
remaining pins (e.g. an enable line on a power-switch IC) are
ignored.

## 3.3.1 Multi-channel SERIES

A part with several independent series paths (e.g. a 4-channel ferrite
bead array) uses indexed parameters, the same way multi-rail SINKs use
`PDN1_I` / `PDN1_P_NET`:

| Channel | Value | Net / pin params |
|---------|-------|------------------|
| legacy  | `PDN_R` | `PDN_P_NET`, `PDN_N_NET` (auto-infer OK on 2-pin) |
| 1       | `PDN1_R` | `PDN1_P_NET`, `PDN1_N_NET` or `PDN1_P_PINS` / `PDN1_N_PINS` |
| 2       | `PDN2_R` | … |

Auto-inference from pad connectivity applies only when the part carries
**one** SERIES channel. With two or more channels you must name each
channel's nets or pin overrides explicitly.

Example — a 4-pad ferrite array bridging two net pairs per channel:

```text
FB1:
  PDN_ROLE    = SERIES
  PDN1_R      = 0.1
  PDN1_P_PINS = 1
  PDN1_N_PINS = 2
  PDN2_R      = 0.1
  PDN2_P_PINS = 3
  PDN2_N_PINS = 4
```

The viewer labels indexed channels `FB1#1`, `FB1#2`, and so on.

## 3.4 What changes in the viewer

Once a SERIES directive is in place:

- The two rails it bridges are treated as **one rail** in the side
  panel — the solver propagates current from a source on one side to a
  sink on the other through the lumped resistor.
- A SERIES marker is drawn on the board at the part location.
- The Nodes tab shows the voltage on each side of the SERIES part, and
  hovering over the marker reports the current through it.

> SERIES is also available in editor mode — see
> [Section 2.3](02-sources-and-sinks-editor.md#23-attaching-a-source-or-sink-to-a-component).
> Editor SERIES is component-bound only (it bridges two real pads on
> the same part) and uses an SI-units value (`0.01` for 10 mΩ).

## 3.4 Repeated sheets and local net names

On hierarchical designs with **repeated sheet symbols** (Altium
`REPEAT(...)`), use the **local** net label from the child sheet in
`PDN_P_NET` / `PDN_N_NET` — not the flattened PCB name (`VCC_EFUSE.4`).
FYPA maps each PCB instance to its slot net via schematic pin
connectivity. See [Section 1.5 — Local net names](01-sources-and-sinks.md#local-net-names-hierarchical--reused-sheets)
for the full explanation, VIP example, and troubleshooting table.

Auto-inference of P/N nets on 2-pin SERIES parts (Section 3.2) works per
PCB instance the same way: each repeated `R63.N` infers from its own
pads.

## 3.5 Troubleshooting

| Message or symptom                                       | Likely cause                                                                | Fix                                                                          |
|----------------------------------------------------------|------------------------------------------------------------------------------|------------------------------------------------------------------------------|
| `PDN_R must be positive`                                  | `PDN_R = 0` or a negative value.                                            | Enter a small positive resistance (milliohms is fine).                       |
| `SERIES on R7: ambiguous nets — please set PDN_P_NET / PDN_N_NET` | A 3+ pin part with auto-inference, or a 2-pin part with both pads on the same net. | Add `PDN_P_NET` and `PDN_N_NET` explicitly.                                  |
| `multi-channel SERIES requires explicit PDNn_P_NET / …` | Two or more `PDNn_R` channels without per-channel nets or pin overrides. | Add `PDNn_P_NET` / `PDNn_N_NET` or `PDNn_P_PINS` / `PDNn_N_PINS` for each channel. |
| `component … has no pad on net …` on a `SERIES` part | `PDN_P_NET` / `PDN_N_NET` name a net none of this part's pads sit on. | Match the parameter to the PCB net on each pad, or use a [local net name](01-sources-and-sinks.md#local-net-names-hierarchical--reused-sheets) on repeated sheets. |
| `net … could not be resolved as a local schematic net` on a repeated sheet | Flattened PCB name used instead of the local child-sheet label. | Use the local name (e.g. `VCC_EFUSE`, not `VCC_EFUSE.4`). |
| The two nets are still not connected in the solve         | The SERIES part was placed on the schematic but does not actually bridge the two nets on the PCB. | Check the PCB netlist: each pad of the SERIES part must be on the corresponding net. |

## Next steps

- For on-board regulators (LDO / buck) that pin an output rail's
  voltage while pulling current from an input rail, see
  [Section 4 — Regulators](04-regulators.md).
- For a full tour of the viewer panels, tables, and modes, see
  [Section 5 — The viewer tour](05-viewer-tour.md).
