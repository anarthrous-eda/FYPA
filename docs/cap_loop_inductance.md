# How FYPA Models Capacitor Loop Inductance

This document describes how FYPA finds decoupling capacitors, how it
computes their mounted and full-loop inductance, which inputs go into each
term, and what is *not* modelled. It is aimed at users who want to
understand or sanity-check the numbers the tool reports in the
**Capacitors** tab and its heatmap overlay.

It is the inductance counterpart of
[via_resistance_model.md](via_resistance_model.md): where that document
covers the first classic PDN parameter (DC resistance / IR drop), this one
covers the second — per-capacitor loop inductance, per TI application note
**SWPA222A §3**, "Power Delivery Network Analysis".

The implementation lives in [fypa/caploop/](../fypa/caploop/).

## Why loop inductance

A decoupling capacitor only decouples if the loop from its plates, through
its pads, escape vias and the plane pair, and back to the IC's balls, is
short. Above a few megahertz that loop's inductance — not the capacitance —
sets the impedance the IC sees. TI's data manuals specify a **maximum loop
inductance per capacitor, excluding the part's own ESL**; for the OMAP4430
the budget is 0.7–1.0 nH.

TI's own extraction flow (SWPA222A §3) defines a Z-parameter port across the
capacitor's power and ground pads, extracts `Z`, and reports
`L_eff = Im(Z) / 2πf` at ~50 MHz. FYPA reaches the same quantity from
geometry, which is available before any board is built.

## The three tiers

| Tier | What it adds | Needs a solve? |
|---|---|---|
| 1 | Closed-form mounted inductance: pad escape + via pair + a radial spreading stand-in | No — populates on load |
| 2 | Plane-pair spreading inductance, solved on the real plane geometry by FEM | Yes (per cavity) |
| 3 | The full cap → plane → IC loop, adding the device's own via pair | Yes (uses Tier 2) |

Tier 1 is always shown. Tiers 2 and 3 fill in when the user presses
**Compute Tier 2/3**.

## Units and constants

All geometry is millimetres (the repo convention). All inductances are
computed and stored in **henries**; only the GUI converts to nH.

```
MU0_H_PER_MM = 4π × 10⁻¹⁰      # μ0 = 4π×10⁻⁷ H/m
```

See [fypa/caploop/constants.py](../fypa/caploop/constants.py) for the user
knobs (`CapLoopSettings`), which persist in the `.fypa` project file's
`viewer_settings` and are edited in the Settings tab. They are *analysis*
parameters: changing them never invalidates the voltage solve.

## Finding the capacitors

A decoupling capacitor is a two-terminal part whose designator starts with
`C` + a digit, connected across two power rails — where **GND counts as a
rail**. Rails come from `compute_rail_groups`
([fypa/rail_groups.py](../fypa/rail_groups.py)), i.e. from the `PDN_*`
annotations; a GND-alias net is admitted even on a board whose directives
never name it.

Detection is a heuristic, so every capacitor carries an override, stored per
physical designator in the project file (`ProjectFile.cap_overrides`):

* the **Use** checkbox forces a structurally-valid cap in, or drops a
  detected one;
* the **Target** cell repoints the loop's far end at another directive.

Capacitance and voltage rating are parsed from Altium component parameters
(`Capacitance` / `Value` / `Comment`, `Voltage` / `Voltage Rating`, …) with
plausibility bands. These columns are **informational only** — they are
never inputs to the inductance math — so an unparseable value shows blank
rather than raising.

### Escape vias

Escape vias are found by distance clustering around the cap's pads, subject
to two rules that matter more than they look:

1. **A via must reach the pad's own mounting layer.** A buried or far-side
   via can sit half a millimetre from a pad in *plan view* and carry none of
   its current. Without this rule a bottom-mounted 0402 on a 16-layer board
   "escapes" through a layer 2–15 buried via and is charged the full board
   thickness of escape inductance — an observed 20 nH of fiction.
2. **A stacked continuation via is not a parallel escape.** Boards routinely
   route pad → L15 with one via and L15 → L3 with another. Those barrels are
   in *series*. Counting the second as a second escape would inflate the
   parallel-pair count and understate the loop. `expand_reachable_layers`
   follows the chain for the purpose of finding reference planes, without
   adding it to the cluster.

A through-hole pad is its own escape via, at zero distance.

### The reference cavity

The cavity is the plane pair the loop closes through: rail copper on one
layer facing return copper on another. Candidates must be *reachable* (rule 2
above) and must look like a **sheet** locally — measured as the fraction of a
2 mm disc around the via cluster that the net's copper fills. This is
deliberately shape-based rather than keyed on Altium's `is_plane` flag,
because plenty of boards (including `ExampleDesigns/Imperial`) build their
power and ground references as pours on signal layers, which Altium never
marks as internal planes. The mounting layer itself is excluded: the loop
leaves the pads and goes *down*.

Candidates are ranked **depth first** (nearest to the mounting surface), then
by dielectric gap, then planes over pours. Ranking by gap first once selected
a pair on the far side of the board because its dielectric was 15 µm thinner
than the pair directly under the part.

## Tier 1 — closed forms

Implemented in [fypa/caploop/tier1.py](../fypa/caploop/tier1.py).

**Via pair** (the cap's escape barrels, anti-parallel currents):

```
L = (μ0 / π) · h · acosh(s / 2r)
```

`h` = mounting surface to the middle of the cavity, `s` = spacing between the
rail and return escape-cluster centroids, `r` = mean drill radius. For `N`
parallel pairs, `L_N = L · k / N` with `k` the mutual-coupling factor
(default 0.8; `k = 1` would mean fully independent pairs, which adjacent
barrels are not).

**Escape** (each pad's run to its vias, as a trace over the nearest reference
plane):

```
L ≈ μ0 · h_d · len / w
```

`len` is measured from the **pad edge**, not its centre — a via-in-pad
escapes in 0 mm, which is exactly TI's recommendation. `w` is that side's own
pad width. Measuring from the centre would charge a 1206 land ~0.8 mm of
escape run it never makes.

**Spreading** (radial stand-in for the cavity, replaced by Tier 2):

```
L = (μ0 · h_cav / π) · ln(r_far / r_port)
```

Note `1/π`, **not** `1/2π`: current spreads radially *out* of the cap port
and converges radially *into* the IC port, so both ports contribute a `ln`
term. This makes the two-port cavity term share the via-pair's functional
form, as it must — both are the same 2-D Laplace problem with two line
sources. Validated against the Tier-2 FEM to within 0.7 % on an unbroken
plane (`tests/test_caploop_tier2.py`).

Degenerate geometry (no escape via, or no reference cavity) can't be modelled
by these forms. Those capacitors get the settings fallback value, are marked
with a `~` prefix in the table, and are flagged — an estimate, never a
confident number.

## Tier 2 — FEM plane-pair spreading

Implemented in [fypa/caploop/tier2_fem.py](../fypa/caploop/tier2_fem.py).

The closed form assumes an unbroken cavity. Real planes are split and
perforated, and that is precisely where the assumption fails. Tier 2 reuses
FYPA's existing 2-D cotangent-Laplacian FEM (`pdnsolver`) **unchanged**, via
the duality between DC spreading resistance and magnetostatic spreading
inductance.

### The duality

A plane pair of separation `h` carries equal and opposite sheet currents. Its
inter-plane potential `Φ` obeys

```
∇·( (1 / (μ0·h)) ∇Φ ) = 0
```

which is the same PDE the DC solve applies to `∇·(σ·t·∇V) = 0`. So a
`pdnsolver.problem.Layer` whose `conductance` is set to `1 / (μ0·h)` — units
of 1/H per square, not siemens — plus a 1 A `CurrentSource` between two
ports, yields a solved "potential" difference that **is** the spreading
inductance in henries. There is no scaling anywhere else.

### The cavity domain

The sheet is the **intersection** of the rail copper on one layer with the
return copper on the other: return current only exists where both planes do.
That is what makes splits and anti-pad perforations change the answer. Both
inputs already carry their anti-pads, pullback and thermal reliefs from
`_plane_sheet_polygon` in
[fypa/altium_geometry.py](../fypa/altium_geometry.py), so no extra punching
is needed.

### Ports

A capacitor's port is a **single** port in the 2-D cavity field — the place
its vias carry current between the two planes — not one port per side. The
sheet has anti-pad holes exactly there, so the port region covers the hole
and ties the mesh vertices ringing it into one equipotential node: a
finite-size port. The port's seed point is forced onto copper, since a seed
inside a hole would read as an off-copper terminal and get the network
dropped.

Three details that are easy to get wrong, and each of which produced a
plausible-looking wrong answer before it was fixed:

* **A multi-via port is the *union* of one disc per via, never their convex
  hull.** The solver claims each mesh vertex for the first connection that
  covers it, so a hull over a device's spread-out pins swallows the ports of
  every capacitor mounted inside that footprint — tying them to the device's
  node and reporting their spreading inductance as exactly zero. The union is
  still one `Connection`, because pdnsolver maps one node to one
  representative vertex.
* **Port discs are shrunk so they never touch** (to 45 % of the distance to
  the nearest foreign via). Overlapping discs share vertices, the loser
  samples its neighbour's node, and the matrix loses its reciprocity.
* **The port's potential is sampled at its seed point and nowhere else.** The
  seed is the vertex the mesher plants and the solver elects as the node's
  representative. Averaging across a port's other discs — which after the
  disjointness clamp are often smaller than a mesh cell and contain no vertex
  at all — drags the reading toward the far field and halves it.

Two ports whose vias sit within an anti-pad clearance of each other are
**one port**, and no cavity can resolve a spreading inductance between them.
This happens by design: boards mount a capacitor directly on an IC's
via-in-pad. Such a capacitor is merged out of the matrix and reported with
`L2 = 0` **and an explicit reason**, rather than a bare zero that reads like a
computed value.

### The port matrix

One solve per capacitor injects 1 A at that cap and reads `Φ` at *every*
port, giving a column of the N-port transfer-inductance matrix:

```
L[i][j] = Φ_j(I_i = 1) − Φ_ic
```

Diagonal entries are each cap's self spreading inductance (the `L2` column).
Off-diagonals are the cap↔cap coupling through the shared cavity, which a
future PDN-impedance analysis (`Z(f) = jωL + branch RLCs`) needs and which
cannot be recovered from the scalars afterwards. Only independent ports enter
the matrix, so it is symmetric to numerical precision — reciprocity is the
self-check that caught every port bug listed above.

Inactive ports carry a **zero-current source**, i.e. an open circuit, so they
stay passive observation points rather than shorting the cavity. Solves 2…N
reuse the cached mesh and Laplacian (the assembly fingerprint covers geometry
and connection seeds, never source magnitudes), so extra columns are cheap.

A capacitor on a cavity island with no conduction path to the target yields a
**NaN** row — *unknown*, not "zero coupling" — and is reported as a split
plane.

## Tier 3 — the full loop

Implemented in [fypa/caploop/tier3.py](../fypa/caploop/tier3.py).

```
L_total = L_escape(both pads) + L_via(cap → cavity)
        + L_spread(FEM)      + L_via(cavity → IC)
```

All four are series elements of one current loop, so they add. The IC's via
pair is found with the same clustering (and the same mounting-layer rule) as
the capacitor's, using the target directive's power and return pins.

Plating thickness — which dominates the DC barrel resistance model in
`_barrel_segment_resistance_ohm`
([fypa/altium/loader.py](../fypa/altium/loader.py)) — is **irrelevant** here.
Loop inductance is set by the current's enclosed area, and the return current
rides the barrel's outer wall regardless of how thick the plating is.

When the target's via geometry can't be resolved (an ideal-return directive,
no vias), the IC term is taken as zero and the total is reported as a **lower
bound** (`≥` prefix), not silently as a complete answer.

The per-rail rollup combines each rail's included capacitors **in parallel** —
what the IC actually sees below the caps' series resonance.

## What is not modelled

* **The capacitor's own ESL.** TI specifies the loop budget *excluding* ESL,
  and FYPA reports the same quantity. Add the part's ESL from its datasheet
  to compare against a total.
* **ESR / frequency dependence.** This is a magnetostatic (low-frequency
  asymptote) model. It is valid below the first plane resonance — roughly
  700 MHz for a 100 mm plane in FR-4, lower for smaller pours.
* **Skin effect and proximity effect** in the barrels and planes.
* **Multiple cavities in parallel.** Each capacitor is assigned one reference
  cavity, the nearest it can reach.
* **Trace-following.** Escape vias are associated by distance, not by walking
  copper connectivity: at sub-3 mm length scales the accuracy gain does not
  pay for the geometry work.
* **Dielectric constant.** Inductance is Dk-independent. `Dk`/`Df` *are*
  extracted into `RawStackupLayer` (thickness-weighted across plies of a
  multi-ply gap) because the PDN-impedance analysis needs them for the
  plane-pair capacitance — see [pdn_impedance.md](pdn_impedance.md), which
  consumes the loop inductances this document describes.

## Performance

The expensive step is the per-(layer, net) copper union — 5–11 s on a real
board — followed by identification (the copper-coverage tests per capacitor),
another 2–5 s. Neither runs on the GUI thread:

* Opening the tab (or ticking **Show on heatmap**) builds the rows on a worker
  behind a modal busy dialog.
* **Compute Tier 2/3** reuses the copper shapes the row build already cached
  and runs the cavity solves on a worker behind a cancellable progress dialog
  (one tick per capacitor).

Both caches are keyed so that only the things that actually invalidate them do:
the copper shapes on the extracted design, and the identification on the design
*plus* the analysis settings *plus* the set of force-included capacitors. An
exclude or a retargeting changes no geometry, so
`apply_cap_overrides` re-derives just the affected fields — a checkbox click is
~0.03 s rather than a 7.5 s rebuild.

## Flags

| Flag | Meaning |
|---|---|
| `single-via` | One escape via on at least one side — the dominant, most fixable loop-L contributor |
| `long-escape` | The pad-edge-to-via run exceeds the warning distance |
| `far-plane` | The reference cavity is deeper than the warning depth below the mounting surface |
| `no-cavity` | No reachable plane pair; Tier 1 falls back and Tier 2 is skipped |
| `no-escape-via` | No via reaches this pad's mounting layer within the search radius |
| `no-target` | No SINK directive on this rail, so the loop has no far end |

## Design guidance (SWPA222A §3)

* Keep the power/ground plane pair close to the surface the capacitor mounts
  on.
* Use via-in-pad.
* Place vias as close to the IC's balls as possible.
* Avoid discontinuities in the power or ground planes — they break the return
  path, which is what Tier 2 measures and the closed form cannot see.
* Select capacitors with a small footprint to minimise ESL.
