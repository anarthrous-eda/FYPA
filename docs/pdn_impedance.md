# How FYPA Models PDN Impedance Z(f)

This document describes the **Impedance** tab: what it computes, which inputs
it takes and where they come from, and what it deliberately does not model.

It is the third and last of the classic PDN parameters from TI application
note **SWPA222A**, after DC resistance
([via_resistance_model.md](via_resistance_model.md)) and capacitor loop
inductance ([cap_loop_inductance.md](cap_loop_inductance.md)). It is the one a
designer actually signs off on, and it is only meaningful *because* of the
other two: every capacitor's series resonance sits where its **mounted** loop
inductance puts it, not where its datasheet ESL alone would.

The implementation lives in [fypa/caploop/impedance.py](../fypa/caploop/impedance.py)
and [fypa/caploop/packages.py](../fypa/caploop/packages.py).

## The target mask (SWPA222A §4)

```
Z_target = V_rail · ripple% / I_transient
```

held from DC up to **F_MAX** — the frequency beyond which adding capacitors no
longer brings |Z| down, because plane spreading and package inductance
dominate. Both the ripple budget and the transient current are yours to
declare; FYPA reads the rail's nominal voltage from its `PDN_ROLE=SOURCE`
directive. A rail with no declared transient current has no mask (`Z_target` is
infinite) and the tab says so rather than inventing one.

## The model

Everything sits in parallel between the rail and its return, seen from the IC:

| Branch | Impedance | Sets |
|---|---|---|
| Each capacitor | `ESR + jω(ESL + L_mount) + 1/(jωC)` | the mid-band |
| The VRM | `R + jωL` | the DC floor |
| The plane pair | `1/(jωC_plane)` | the high-frequency tail |

```
Z(f) = 1 / Σ 1/Z_k
```

Two branches that are inductive and capacitive at the same frequency form a
parallel tank: the admittances nearly cancel and |Z| spikes. Those
**anti-resonances**, not the individual minima, are what a decoupling strategy
lives or dies by, so the tab finds them, marks the worst three, and colours any
that breach the mask.

On `ExampleDesigns/Imperial`'s +3V3 rail (53 capacitors, 197 µF) the model
reproduces the textbook shape: a 2 mΩ VRM floor, a VRM-versus-bulk peak near
157 kHz, a bulk-versus-ceramic peak near 6.7 MHz, and an inductive rise past
the last self-resonance.

### Where each input comes from

* **C** — parsed from the part's Altium parameters (the Capacitors tab's
  `C (µF)` column). A capacitor whose value can't be read is excluded, and
  said so.
* **L_mount** — the best tier FYPA has for that part: Tier 3 if the cavity
  solve has run, else Tier 2, else the Tier-1 closed form. Press **Compute
  Tier 2/3** on the Capacitors tab and the impedance plot redraws against the
  better numbers.
* **ESL, ESR** — the package library, overridable per part (below).
* **C_plane** — `ε0·εr·A/h` over the rail's dominant reference cavity, using
  the area of the *intersection* of the two planes and the **Dk extracted from
  the stackup**. When the stackup carries no Dk the tab falls back to 4.5 and
  labels the value as such rather than quietly pretending.
* **VRM R, L** — yours to declare, per rail.

Every input above the plot persists per rail in the `.fypa` project file.

## The SMD package library

A capacitor's own ESL and ESR are properties of the *component*, not the board,
so no amount of layout extraction can produce them. Rather than demand a vendor
S-parameter model per part, FYPA ships typical MLCC values per SMD case size,
shown in an editable table on the Impedance tab:

| Package | ESL (nH) | ESR (mΩ) | | Package | ESL (nH) | ESR (mΩ) |
|---|---|---|---|---|---|---|
| 01005 | 0.20 | 60 | | 1206 | 0.90 | 12 |
| 0201  | 0.30 | 40 | | 1210 | 1.00 | 10 |
| 0402  | 0.45 | 25 | | 1812 | 1.40 | 10 |
| 0603  | 0.60 | 20 | | 2220 | 1.80 | 10 |
| 0805  | 0.70 | 15 | | | | |

Reverse-geometry parts (0204, 0306, 0508, 0612) put the terminations on the
long edges and are listed with correspondingly lower ESL.

ESL grows with body length — the current has further to travel between
terminations — and ESR falls with electrode area. Editing a cell changes every
capacitor of that case size at once. Only the packages you actually edit are
written to the project file, so a project that never touched a value inherits
later revisions of the built-in default.

These are good enough to place a rail's anti-resonances within roughly a factor
of two, which is what a first-pass review needs. **For a sign-off number,
override the handful of capacitors that set the minimum** with values from the
vendor's model.

### Package detection, and what "SMD only" means

The case size is parsed from the footprint name, handling the three conventions
seen in the wild: a bare imperial code (`C_0402_SL`), an explicit metric one
(`C_0402_1005Metric`), and IPC-7351 land names (`CAPC1608X90N`, whose digits
are always metric). Note that `0603` is both an imperial code *and* the metric
code for an 0201; the metric reading is taken only when the name says so.

**Only SMD chip packages are supported.** A tantalum D-case brick, an
electrolytic, anything through-hole — none has a case-size code, and none has an
ESL a table could predict. Those capacitors are listed with `—` in the `Pkg`
column and **excluded from the impedance model with a stated reason**, never
silently defaulted.

### Per-part overrides

Double-click a capacitor's `ESL (nH)` or `ESR (mΩ)` cell on the Capacitors tab
to pin that part's value. Overridden cells are highlighted; clearing the field
falls back to the package default. Supplying **both** is also the way to admit a
non-SMD capacitor into the model — it is the only route in for a part the
library cannot classify.

Overrides live on the same per-designator record as the include and target
choices, so setting one never disturbs another.

## What is not modelled

* **Plane resonances.** This is a lumped model. It cannot see the board
  becoming a cavity a half-wavelength across, so trust it below the first plane
  resonance — a few hundred MHz on a typical board, lower for a large plane.
  (The Tier-2 FEM's port matrix is the groundwork for lifting this.)
* **Cap↔cap mutual inductance.** The
  [`CavityMatrix`](../fypa/caploop/tier2_fem.py) that Tier 2 produces holds the
  off-diagonal coupling terms, but the lumped rollup does not yet use them.
  They would raise the anti-resonance peaks slightly when many capacitors share
  one cavity.
* **Frequency-dependent ESR** (dielectric loss, skin effect) and the DC bias
  and temperature derating of MLCC capacitance — a 100 nF X5R at 80 % of its
  rated voltage is not 100 nF. Use a per-part override to model the effective
  value if it matters.
* **The IC's package and die capacitance**, which take over above F_MAX.
* **Multiple VRM phases** or a frequency-dependent regulator output impedance;
  the VRM is a single series R + L.

## Reading the plot

* The **blue trace** is |Z(f)| for the selected rail.
* The **red dashed line** is `Z_target`, drawn only as far as F_MAX (the dotted
  vertical).
* **Triangles** mark the worst three anti-resonances below F_MAX — red if they
  breach the mask, amber otherwise.
* **Show individual capacitors** overlays each branch faintly, which is how you
  find out *which* two parts are forming a given peak.

The summary line under the plot states the verdict. When the mask is missed it
reports the frequency at which |Z| **first** breaches it — the honest reading of
how far the decoupling actually holds — rather than only the worst peak.
