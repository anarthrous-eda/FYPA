# 4. Regulators — LDOs and buck converters

A `REGULATOR` directive models an on-board voltage regulator: an LDO,
a buck converter, a boost, a switching module. Unlike a `SOURCE`
(which is a supply *edge* — what is upstream of it is assumed to be
infinite), a `REGULATOR` models **both rails it touches**:

- It pins its **output** net at the configured voltage.
- It pulls **input** current from its input net, proportional to the
  output current it delivers.

That second behaviour is the point of having a separate REGULATOR role:
it lets you see the IR drop on the *input* copper too, between the
upstream source and the regulator's input pin. With a `SOURCE` on the
regulator's output, that input-side copper carries no current in the
solve and the input rail looks artificially clean.

> Read [Section 1](01-sources-and-sinks.md) first if you have not — the
> parameter mechanics (where to add them, hiding them, library-symbol
> defaults) are the same.

## 4.1 SOURCE vs REGULATOR — when to use which

| Situation                                                                                  | Use         |
|--------------------------------------------------------------------------------------------|-------------|
| The rail enters the board pre-regulated (connector pin, battery, off-board supply)         | `SOURCE`    |
| The regulator's input rail is **not** part of the PDN you are analysing                    | `SOURCE`    |
| The regulator's input rail **is** part of the PDN, and you want to see drop on its copper  | `REGULATOR` |

If every rail on your board comes in pre-regulated and you do not have
on-board step-up / step-down conversion, you will never need
`REGULATOR` — `SOURCE` for inputs and `SINK` for loads is the whole
story.

## 4.2 The parameter shape

A REGULATOR uses output voltage, gain (manual or auto), regulator type,
and four nets (two output, two input):

| Parameter                  | Purpose                                                | Example   |
|----------------------------|--------------------------------------------------------|-----------|
| `PDN_ROLE`                 | `REGULATOR`                                            | `REGULATOR` |
| `PDN_V`                    | Output voltage (volts)                                 | `3.3`     |
| `PDN_REGULATOR_TYPE`       | `LDO` or `SMPS` — auto-computes gain when `PDN_GAIN` is omitted | `SMPS` |
| `PDN_REGULATOR_EFFICIENCY` | 0–1; SMPS only (default `1.0`)                       | `0.9`     |
| `PDN_GAIN`                 | Optional fixed override (disables auto-gain)             | `0.73`    |
| `PDN_QUIESCENT`            | Optional constant input current (Eigenverbrauch / Iq)    | `5mA`     |
| `PDN_OUT_P_NET`            | Output supply net                                      | `+3V3`    |
| `PDN_OUT_N_NET`            | Output return net                                      | `0V`      |
| `PDN_IN_P_NET`             | Input supply net                                       | `+5V`     |
| `PDN_IN_N_NET`             | Input return net                                       | `0V`      |

There is no auto-inference for REGULATOR — a regulator IC always has
more than two pads, so all four net names must be set explicitly.

### Multi-channel REGULATOR (PMIC)

A multi-output PMIC on one symbol uses indexed channels the same way
multi-rail SINKs use `PDN1_I`:

| Channel | Value params | Net params |
|---------|--------------|------------|
| legacy  | `PDN_V`, `PDN_REGULATOR_TYPE`, `PDN_REGULATOR_EFFICIENCY`, `PDN_QUIESCENT` — *or* `PDN_GAIN` | `PDN_OUT_P_NET`, `PDN_OUT_N_NET`, `PDN_IN_P_NET`, `PDN_IN_N_NET` |
| 1       | `PDN1_V`, `PDN1_REGULATOR_TYPE`, `PDN1_QUIESCENT`, … | `PDN1_OUT_P_NET`, … |
| 2       | `PDN2_V`, … | … |

Example — 3.3 V and 1.8 V outputs from a shared 5 V input:

```text
U4:
  PDN_ROLE                  = REGULATOR
  PDN_V                     = 3.3
  PDN_REGULATOR_TYPE        = SMPS
  PDN_REGULATOR_EFFICIENCY  = 0.9
  PDN_OUT_P_NET             = +3V3      PDN_OUT_N_NET = GND
  PDN_IN_P_NET              = +5V       PDN_IN_N_NET  = GND
  PDN1_V                    = 1.8
  PDN1_REGULATOR_TYPE       = SMPS
  PDN1_REGULATOR_EFFICIENCY = 0.85
  PDN1_OUT_P_NET            = +1V8      PDN1_OUT_N_NET = GND
  PDN1_IN_P_NET             = +5V       PDN1_IN_N_NET  = GND
```

Indexed channels appear as `U4#1`, `U4#2` in the viewer.

## 4.3 Auto-gain from regulator type

When `PDN_GAIN` is omitted, set `PDN_REGULATOR_TYPE`:

| Type   | Initial `PDN_GAIN` | Adaptive eligible |
|--------|--------------------|-------------------|
| `LDO`  | `1.0`              | No                |
| `SMPS` | `PDN_V / (Vin_nom × η)` | Yes          |

For `SMPS`, **Vin_nom** is inferred from an upstream `SOURCE` or
`REGULATOR` whose output is declared on the same net name as
`PDN_IN_P_NET` (exact name match — SERIES bridge groups are not expanded
for Vin lookup; pad resolution still uses them). Set `PDN_REGULATOR_EFFICIENCY` to the datasheet
value (default `1.0` = ideal).

Example — 3.3 V buck from a 5 V `SOURCE`:

```text
J1:
  PDN_ROLE  = SOURCE
  PDN_V     = 5
  PDN_P_NET = +5V
  PDN_N_NET = GND

U2:
  PDN_ROLE                  = REGULATOR
  PDN_REGULATOR_TYPE        = SMPS
  PDN_V                     = 3.3
  PDN_REGULATOR_EFFICIENCY  = 0.9
  PDN_OUT_P_NET             = +3V3
  PDN_OUT_N_NET             = GND
  PDN_IN_P_NET              = +5V
  PDN_IN_N_NET              = GND
```

Initial gain: `3.3 / (5 × 0.9) = 0.73`.

### Adaptive SMPS gain in the viewer

After the first solve, check **Adaptive SMPS gain** next to **↻ Resolve**
and re-solve. FYPA iterates the SMPS gain using the **solved** input
voltage at the regulator pads (includes SERIES drop and input-side copper
IR drop). LDO regulators stay at `PDN_GAIN = 1.0`.

Import with auto-solve always runs a **single** pass; use the checkbox for
iterative refinement. The CLI equivalent is `--adaptive-regulator-gain`.

### Quiescent current (`PDN_QUIESCENT`)

Regulators draw a small **standby current** from their input even when the
load is idle — LDO quiescent current (Iq) or an SMPS no-load draw. Set
optional `PDN_QUIESCENT` (or `PDNn_QUIESCENT` per channel) in the same
SI units as `PDN_I` on sinks (`5mA`, `0.005`, …). Omitted defaults to
zero.

The solver adds this as a **constant input current** on `PDN_IN_P_NET` /
`PDN_IN_N_NET`, in addition to the load-proportional `PDN_GAIN × I_out`
term. It increases IR drop on the input rail even when the output load
is zero.

## 4.4 Manual `PDN_GAIN` (advanced)

`PDN_GAIN` is the ratio of input current to output current. It depends
on the regulator topology:

| Regulator type            | `PDN_GAIN`                                       | Why                                                |
|---------------------------|--------------------------------------------------|----------------------------------------------------|
| **LDO** (linear)          | `1.0`                                            | Output current passes straight through from input. |
| **Buck, 100% efficient**  | `Vout / Vin`                                     | Power balance: `Vin · Iin = Vout · Iout`.          |
| **Buck, realistic**       | `(Vout / Vin) / efficiency`                      | Same, with efficiency divided in.                  |
| **Boost, 100% efficient** | `Vout / Vin`                                     | Same identity — input current is *higher* than output. |
| **Boost, realistic**      | `(Vout / Vin) / efficiency`                      | Same.                                              |

Worked numbers for the common cases:

- LDO any-to-any: `PDN_GAIN = 1.0`
- Ideal buck, 5 V → 3.3 V: `3.3 / 5 = 0.66`
- 90 % buck, 5 V → 3.3 V: `0.66 / 0.9 = 0.73`
- Ideal boost, 5 V → 12 V: `12 / 5 = 2.4`
- 85 % boost, 5 V → 12 V: `2.4 / 0.85 = 2.82`

When in doubt, the regulator's datasheet typically gives a typical or
worst-case efficiency curve — pick the operating point that matches
the load current you are analysing.

> `PDN_GAIN` is a **scalar**, not a function of operating point.
> FYPA's solve is linear, so the input/output current ratio is fixed
> for the whole solve. For a regulator whose efficiency varies
> significantly with load, run separate solves for each operating
> point with the corresponding `PDN_GAIN`.

## 4.5 A worked example

A board with a 5 V barrel-jack input feeding a 3.3 V LDO that supplies a
500 mA load:

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
  PDN_QUIESCENT      = 5mA
  PDN_OUT_P_NET = +3V3
  PDN_OUT_N_NET = 0V
  PDN_IN_P_NET  = +5V
  PDN_IN_N_NET  = 0V

U5 (3V3 load):
  PDN_ROLE  = SINK
  PDN_I     = 500mA
  PDN_P_NET = +3V3
  PDN_N_NET = 0V
```

What the solver does with this:

- 500 mA flows out of U2's `+3V3` output pin into U5's `+3V3` input
  pins, through whatever `+3V3` copper exists between them. The drop
  on that copper is visible in the `+3V3` rail's heatmap.
- 500 mA also flows out of J1's `+5V` pin into U2's `+5V` input pin
  (`PDN_GAIN = 1.0` because it is an LDO). The drop on that copper is
  visible in the `+5V` rail's heatmap.
- An additional constant **5 mA** (`PDN_QUIESCENT`) is drawn from the
  `+5V` input regardless of load — it adds a small extra drop on the
  input copper even when U5 is removed from the schematic.

Replacing U2's role with `SOURCE` would leave the `+5V` rail at a flat
5 V everywhere — no current is modelled on its copper, so no drop. The
`+3V3` rail's drop would still solve correctly.

## 4.6 Multi-regulator topologies

A board with several regulators in series (e.g. a 12 V input → 5 V
buck → 3.3 V LDO chain) just chains REGULATOR directives:

```text
U2: REGULATOR  +12V → +5V   (PDN_GAIN = 0.5 for an 85% efficient buck)
U3: REGULATOR  +5V  → +3V3  (PDN_GAIN = 1.0 for an LDO)
```

The two regulators are independent directives. The solver propagates
current from each downstream SINK back through every upstream
regulator, so a 500 mA `+3V3` load shows as 500 mA on the `+5V` rail
between U2 and U3, and 250 mA on the `+12V` rail between J1 and U2
(via the buck's gain).

## 4.7 Troubleshooting

| Message or symptom                                                 | Likely cause                                                                | Fix                                                                          |
|--------------------------------------------------------------------|------------------------------------------------------------------------------|------------------------------------------------------------------------------|
| `REGULATOR has four terminals, use PDN_OUT_P_NET / PDN_OUT_N_NET / PDN_IN_P_NET / PDN_IN_N_NET` | A `PDN_NET` or `PDN_P_NET` / `PDN_N_NET` was used on a REGULATOR.            | Use the four `PDN_OUT_*` / `PDN_IN_*` net parameters.                        |
| `REGULATOR on U2: missing PDN_GAIN or PDN_REGULATOR_TYPE`            | Neither manual gain nor regulator type was set.                               | Add `PDN_REGULATOR_TYPE=LDO` or `SMPS` (and `PDN_REGULATOR_EFFICIENCY` for SMPS), or set `PDN_GAIN` explicitly. |
| Input rail still solves to a flat voltage everywhere                | The regulator was added as a `SOURCE`, not a `REGULATOR`.                   | Change `PDN_ROLE` to `REGULATOR` and add the input-side net parameters.      |
| Output voltage in the solve is not `PDN_V`                          | A SINK on the output rail has its `PDN_P_NET` mis-spelled, so the rail has no closed loop and the solver falls back to a degenerate result. | Check spelling against the PCB netlist (see [1.5](01-sources-and-sinks.md#15-pre-import-checks)). |

## Next steps

- For series elements (fuses, ferrites, sense resistors) on a rail,
  see [Section 3 — Series elements](03-series-elements.md).
- For a full tour of the viewer panels, tables, and modes, see
  [Section 5 — The viewer tour](05-viewer-tour.md).

> Regulators are currently **schematic-only** — editor mode does not
> offer a REGULATOR role. To model an on-board regulator that is not
> in the schematic, place a SOURCE on the output rail and a SINK on
> the input rail with the expected input current.
