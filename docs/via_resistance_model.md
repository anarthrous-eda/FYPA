# How FYPA Models Vias

This document describes how FYPA represents vias and plated through-hole
(PTH) pads in the FEM solve, how their inter-layer (barrel) resistance is
calculated, which inputs go into that calculation, and what is *not*
modelled. It is aimed at users who want to understand or sanity-check the
numbers the tool reports in the **Vias** tab and the per-segment current
overlay.

The authoritative implementation lives in
[fypa/altium/loader.py](../fypa/altium/loader.py); section anchors in this
document point at the relevant ranges.

## Where vias fit into the FEM

The FEM is per-(physical-layer, net) - every enabled copper layer is
triangulated as an independent 2-D Laplace problem. Vias and PTH pads are
the only objects that couple voltages between layers. For each via, FYPA
inserts a chain of small `Resistor` elements (one per layer-to-layer
"hop") into the padne `Problem`. Each end of a hop lands on the **drill
circle** of the via on that layer: every mesh vertex inside the drill is
tied to one potential (an equipotential patch), because the plated wall
shorts the whole hole edge together. Current therefore spreads into the
plane from the drill radius, as it does in copper.

The via copper on each layer is a solid disc (the hole is not cut out), so
the patch catches the disc's centre vertex plus seeds placed on the drill
circle. Around each patch the mesh is graded: rings of Steiner seeds at
1.4×, 1.4²×, … the drill radius, spaced to keep the triangles near
equilateral, stopping at the global element size or half-way to the next
via. The field just outside a barrel falls off as 1/r, so this is where the
via's spreading resistance lives.

Why it matters (a 0.3 mm via into a 10 mm plane, against the analytical
`Rs/(2π)·ln(R/r)`; see `tests/test_validation.py`):

| Contact model | 0.6 mm mesh (default) | 0.3 mm | 0.1 mm |
|---|---|---|---|
| Point at the via centre (before) | +78.5 % | +80.8 % | +84.3 % - diverges |
| Drill-circle patch, no grading | −4.1 % | −2.5 % | +0.3 % |
| Drill-circle patch + grading (now) | −0.02 % | | |

A point contact is a log singularity: its "spreading resistance" depends on
the mesh, grows without bound as the mesh is refined, and added about
0.26 mΩ per via end per 1 oz layer - about 40 % on a typical through via.

A via that sits inside a terminal pad (via-in-pad) joins that pad's
equipotential group rather than carving its own patch out of it: pads are
claimed first, and the via's drill vertices are then already taken.

See `_coupling_networks` in
[fypa/altium/loader.py](../fypa/altium/loader.py) for the element
construction, and `_barrel_grading_points` / `_add_grading_seeds` in
[pdnsolver/solver.py](../pdnsolver/solver.py) for the grading.

## Per-hop resistance formula

For one layer-to-layer hop, the barrel resistance is

```
R_wall = ρ_cu(T) * L_hop / A_annulus

A_annulus = π * ((d/2)² − ((d − 2t)/2)²)
```

(the cross-section of the plated barrel - drill diameter minus the
unplated centre void).

If the via has an IPC-4761 fill **with a conductive material** (copper
paste, silver epoxy, electroplated-copper fill, etc. - see
[Conductive-fill model](#conductive-fill-model)), a second rod of fill
material is added in parallel:

```
R_fill = ρ_fill * L_hop / A_inner

A_inner = π * ((d − 2t)/2)²

1/R_hop = 1/R_wall + 1/R_fill
```

For a plain (or non-conductively-filled) via the fill branch is skipped and
`R_hop = R_wall`.

If the drill is smaller than twice the plating (e.g. via-in-pad with full
plating closure), the annulus formula gives a non-positive area and the
barrel collapses to a **solid copper rod**:

```
A = π * (d/2)²
```

This branch keeps the formula valid for tiny micro-vias whose plating
effectively fills the hole. (There is no separate fill term in this case -
the void no longer exists.) See `_barrel_segment_resistance_ohm` in
[fypa/altium/loader.py](../fypa/altium/loader.py).

A through-hole pad with a 0 mm hole, or a missing stackup, or any other
degenerate input falls back to a fixed per-hop value (see
[Fallback behaviour](#fallback-behaviour)).

## Conductive-fill model

FYPA reads the IPC-4761 protection record on every via and classifies the
fill as **conductive** or **non-conductive** before solving:

* A via must have one of the IPC-4761 *FILLING* protection types
  (V, VIa, VIb, VII - enum values 9, 10, 11, 12; value 5 is also accepted as
  a synonym for the V *filling* type, for resilience against `altium_monkey`
  enum reorderings). Tenting, plugging, and capping are not fills and leave
  the barrel cross-section unchanged.
* The FILLING feature row's material string (free text in Altium, e.g.
  `Copper`, `Silver Epoxy`, `Polymer`, `Non-Conductive Epoxy`) is matched
  case-insensitively against the keywords *conductive*, *copper*, *Cu*,
  *silver*, *Ag*. A leading "non-conductive" prefix disqualifies the row
  even if it contains "copper" downstream.
* A filled via whose material is empty or unrecognised is treated as
  **non-conductive** (the epoxy default). This is conservative - it leaves
  the resistance unchanged rather than silently shunting current down an
  invisible fill rod.

When classified as conductive, `R_fill` is computed from a single
user-tunable bulk resistivity:

| Setting                          | Default     | Equivalent                                |
|----------------------------------|-------------|-------------------------------------------|
| Conductive fill resistivity      | 5×10⁻³ Ω·mm | 5×10⁻⁴ Ω·cm = 500 µΩ·cm (~300× copper)    |

The default approximates a typical silver-loaded thermosetting epoxy.
Pure electroplated copper-fill closure ≈ copper itself (1.68 µΩ·cm) -
drop the setting to ~2×10⁻⁵ Ω·mm in that case.

The classifier and per-hop helper live in
[fypa/altium/loader.py](../fypa/altium/loader.py) (`_is_conductive_fill`
and `_barrel_segment_resistance_ohm`).

### How much does it actually move R?

For a 0.3 mm drill, 1 mm hop, 25 µm plating:

| Configuration                    | R_hop                   |
|----------------------------------|-------------------------|
| Plain (no fill)                  | 0.778 mΩ                |
| Silver-epoxy fill (5×10⁻³ Ω·mm)  | 0.772 mΩ (~0.7 % lower) |
| Copper-paste fill (~8×10⁻⁵ Ω·mm) | ~0.4 mΩ                 |
| Pure copper fill (1.7×10⁻⁵ Ω·mm) | ~0.13 mΩ                |

The silver-epoxy result is barely measurable - the fill is ~300× more
resistive than the copper wall, so almost all current still flows in the
plating. Conductive fills only meaningfully change `R_hop` when their
resistivity is within an order of magnitude of copper.

## What goes into the calculation

| Input                            | Where it comes from                                                          | User-editable?                                              |
|----------------------------------|------------------------------------------------------------------------------|-------------------------------------------------------------|
| `d` - drill diameter             | Altium `RawVia.hole_diameter_mm` / `RawPad.hole_mm`                          | No (set in Altium PCB)                                      |
| `L_hop` - hop length             | Distance between layer **centres** in the Altium stackup                     | No directly, but follows from stackup edits in the viewer   |
| `t` - plating thickness          | Settings tab → *Via plating* per via type (see [Plating by via type](#plating-by-via-type)) | **Yes**                                      |
| `ρ_cu(T)` - copper resistivity   | Derived from `ρ₂₀`, `α`, `T`: `ρ(T) = ρ₂₀ * (1 + α*(T − 20))`               | **Yes** - via the three fields below                        |
| `ρ₂₀` - resistivity at 20 °C     | Settings tab → *Copper resistivity (at 20 °C)* (default 1.68 µΩ·cm)          | **Yes**                                                     |
| `α` - temperature coefficient    | Settings tab → *Copper temperature coefficient α* (default 0.00393 /°C)      | **Yes**                                                     |
| `T` - board temperature          | Settings tab → *Board temperature* (default 20 °C)                           | **Yes**                                                     |
| Fallback `R` (per hop)           | Settings tab → *Fallback via resistance* (default 1.0 mΩ)                    | **Yes**                                                     |
| IPC-4761 fill type               | Altium via record (`ipc4761_via_type`)                                       | No (set in Altium PCB)                                      |
| Fill material string             | Altium via-structure FILLING row (`material`)                                | No (set in Altium PCB)                                      |
| `ρ_fill` - conductive fill ρ    | Settings tab → *Conductive fill resistivity* (default 5×10⁻³ Ω·mm)           | **Yes**                                                     |

Concretely, all of these answers are "yes":

* **Distance between the layers it hops** - yes. `L_hop` is the absolute
  z-difference between the *centres* of the two copper layers, computed
  from the Altium stackup's `copper_thickness_mm` and
  `dielectric_thickness_mm`. See `_layer_z_centers_mm` in
  [fypa/altium/loader.py:638-669](../fypa/altium/loader.py#L638-L669).
  Edits to the stackup made through the viewer feed straight into this.

* **Diameter of the via** - yes. The drill diameter (`hole_diameter_mm`)
  is taken directly from the Altium via record. (Note: this is the hole,
  not the via pad diameter - the pad diameter does not enter the model.)

* **Copper resistivity setup** - yes. The 20 °C reference resistivity is
  a user setting, as is the temperature coefficient α used to slide it.

* **Temperature setting** - yes. Changing *Board temperature* recomputes
  `ρ_cu(T)` for every hop on the next solve. The same temperature-
  corrected `ρ` is also applied to the plane-sheet conductivity on every
  copper layer - so via resistance and copper sheet resistance stay
  consistent.

The Settings-tab field schema and the matching parameter docs live in
`_SETTINGS_FIELDS` at
[fypa/viewer/settings_tab.py:78-142](../fypa/viewer/settings_tab.py#L78-L142).

## Plating by via type

Plating grows inward from the drilled wall, and how much a hole gets
depends on how it is made: through holes and blind vias are plated with the
outer layers, buried vias with their core before lamination, and laser
microvias get a thin layer of their own. Each via is classified from its
declared end layers, and takes the plating for its type:

| Type | Rule | Default (IPC-6012 Class 3) | Class 2 |
|---|---|---|---|
| Through / PTH | both ends on the outer layers; component plated holes | 25 µm | 20 µm |
| Blind | one end on an outer layer | 25 µm | 20 µm |
| Buried | neither end on an outer layer | 15 µm | 13 µm |
| Microvia | blind or buried over one dielectric, drill ≤ 0.15 mm (IPC-2226) | 12 µm | 12 µm |

Altium carries no microvia flag on a via, hence the span-and-drill rule.
The defaults are the minimum *average* plating of IPC-6012 Class 3; set the
values your fabricator states. Each hop records its type and plating
(`via_kind`, `plating_mm`) in the solve metadata, and the viewer's barrel
current density uses the same wall. A solve saved before per-type plating
reopens with its single value applied to every type.

## Where each hop "lands"

For a multi-layer via, FYPA only chains the via through layers where the
via's nominal net **actually has copper covering the via's (x, y)**. This
matters in two ways:

* Inner layers that the net does *not* reach (the via just passes by) are
  skipped - they get no drill patch, so no phantom current can flow
  there.
* If fewer than two layers in the via's `layer_start..layer_end` span
  actually carry net copper at the via, the via is dropped from the FEM
  entirely (it has no closed-loop electrical role).

The reasoning is documented inline at
[fypa/altium/loader.py:846-976](../fypa/altium/loader.py#L846-L976).

Through-hole **pads** are treated the same way as vias, but with an
assumed span of *every enabled copper layer* (Altium pads do not carry an
explicit blind/buried span). See `_via_through_holes` at
[fypa/altium/loader.py:977-1012](../fypa/altium/loader.py#L977-L1012).

## Does Altium report whether the via is filled?

Yes - Altium stores quite a lot of "what is in / around the hole"
information in its PCB records, including:

* **IPC-4761 fill type** (`ipc4761_via_type`) and per-feature
  side/material rows: `Ia`, `Ib`, `IIa/IIb`, `IIIa/IIIb`,
  `IVa/IVb`, `Va/Vb`, `VI`, `VII` - i.e. tented, plugged, filled-and-
  plated-over, etc. Parsed by
  [altium_monkey/.../altium_record_pcb__via.py](../altium_monkey/src/py/altium_monkey/altium_record_pcb__via.py).
* **Soldermask tenting** flags (`is_tent_top`, `is_tent_bottom`).
* **Backdrill parameters** (`backdrill_params`) and a `via_mode` byte that
  distinguishes through, blind, and buried geometry.

**What FYPA does with that information today:** the PCB extractor
([fypa/altium/extract.py](../fypa/altium/extract.py)) propagates the
IPC-4761 type and the FILLING row's material string onto `RawVia` so the
resistance model can apply the conductive-fill shunt described above.
Tenting, backdrill, and the remaining IPC-4761 feature rows (covering,
plugging, capping) are still discarded - they do not change the conducting
cross-section of the barrel, so leaving them out of the model is exact for
DC resistance.

Per-via the extractor surfaces:

```
center, diameter_mm, hole_diameter_mm, layer_start, layer_end, net_index,
ipc4761_via_type, fill_material
```

The viewer's Vias tab shows the IPC-4761 fill column for every via;
conductively-filled rows carry a "·" suffix so it is visually obvious
which vias' `R_hop` includes the parallel-shunt model.

### Behaviour by IPC-4761 type

* A **non-conductive fill** (the most common IPC-4761 case for via-in-pad
  designs - epoxy, polymer, "Non-Conductive Paste") changes the barrel
  cross-section by adding a non-conducting rod inside the void. DC
  resistance is unchanged. FYPA's classifier rejects the row and falls
  back to the wall-only formula, which matches reality.
* A **conductive** (copper-paste / silver-paste / electroplated-copper)
  fill adds a second conducting rod in parallel with the plated wall.
  FYPA models this with the parallel-shunt formula above; the magnitude
  of the change depends on `ρ_fill` (see the worked numbers under
  [Conductive-fill model](#conductive-fill-model)).
* **Tented / covered / plugged / capped** vias just close the ends. The
  barrel itself is unchanged, so DC resistance is unchanged and FYPA
  treats them as unfilled vias.
* **Backdrilled** vias have the unused stub removed. Removing the stub
  has no DC effect on the current that flows between the layers it still
  connects, but a backdrilled via that the tool *thinks* still spans the
  full board might also have its (x, y) "covered" by the net on a layer
  that, post-backdrill, is no longer physically connected. In the
  pathological case this could overstate the connectivity. If you rely on
  backdrills, treat FYPA's via current as an upper bound on the affected
  net.

## Fallback behaviour

A fixed per-hop resistance (`fallback_via_resistance_ohm`, default
1.0 mΩ) is substituted whenever the physical model cannot produce a
sensible number:

* The drill diameter is missing or 0 mm (e.g. an Altium PTH pad with no
  hole size set).
* The stackup has no z-data for one of the two layers being hopped, so
  `L_hop` cannot be computed.
* The plated-barrel cross-section comes out non-positive after the solid-
  rod fallback. In practice this only happens when both `d` and `t` are
  zero.

Most boards never trigger the fallback. When they do, every affected
segment is logged at DEBUG level (`Skipped …` lines from
`_coupling_networks`) and shown with `R_hop = fallback` in the per-via
segment records that the viewer's Vias tab consumes.

This degrades gracefully - the solve still produces a result; vias whose
geometry is unknown just look like 1 mΩ short hops rather than failing
the solve outright.

## Assumptions and limitations

1. **DC-only.** The model is pure DC resistance. Skin effect, frequency-
   dependent inductive coupling, and high-frequency loss are not
   represented.
2. **Hollow plated cylinders.** Every via and PTH pad is a plated
   cylinder whose wall thickness is set per via type (through / PTH,
   blind, buried, microvia - see [Plating by via type](#plating-by-via-type)),
   and its `layer_start..layer_end` span is respected when chaining hops.
   A stacked microvia column is a chain of separate microvias.
3. **One plating per via type.** Each type has one wall thickness for the
   whole board; a design whose plating varies within a type (heavy-copper
   outer vias next to standard ones of the same type) is not represented.
4. **Limited fill / cap / tent modelling.** Capping, tenting, and
   plugging are ignored - they do not change the conducting cross-section
   of the barrel and are exact for DC. **Conductive fills** (copper /
   silver paste, electroplated-copper closure) *are* modelled as a
   parallel rod inside the plated wall (see
   [Conductive-fill model](#conductive-fill-model)). Non-conductive
   epoxies and polymer fills are deliberately ignored - they do not
   change DC R.
5. **No backdrill awareness.** A backdrilled stub is still treated as
   electrically connected up to `layer_end`. Use with care on backdrilled
   designs.
6. **Layer centres, not surfaces.** `L_hop` is measured between the
   *centres* of two copper layers - i.e. it includes half of each copper
   thickness plus the full dielectric between them. This is the physically
   reasonable choice (current must traverse the bulk of each copper layer
   on its way to the in-plane mesh), but a thicker copper layer therefore
   contributes more to the barrel length than a thinner one with the
   same dielectric below it.
7. **Pad diameter is irrelevant.** Only the drill is used for the barrel
   cross-section. The annular ring on each layer does not enter
   `R_hop` - it is captured separately as part of that layer's 2-D copper
   mesh.
8. **Fallback is not an error.** A via that hits the fallback path
   produces a plausible 1 mΩ-per-hop result; the solve does not crash.
   The DEBUG log is the only signal that the physical model was bypassed
   - check it if a board has unexpectedly low IR drop.

## Editable settings - quick reference

All of the following live on the viewer's **Settings** tab. Editing them
does not trigger an immediate re-solve; press **Re-run Solver** to apply.

| Setting                                | Default          | Affects                                                              |
|----------------------------------------|------------------|----------------------------------------------------------------------|
| Board temperature                      | 20 °C            | `ρ_cu(T)` → both via R and copper sheet conductivity                 |
| Copper resistivity (at 20 °C)          | 1.68 µΩ·cm       | `ρ₂₀` reference                                                       |
| Copper temperature coefficient α       | 0.00393 /°C      | Slope of `ρ(T)` vs temperature                                       |
| Via plating: through / PTH             | 0.025 mm         | `t` of through vias and component plated holes                       |
| Via plating: blind                     | 0.025 mm         | `t` of blind vias                                                    |
| Via plating: buried                    | 0.015 mm         | `t` of buried vias                                                   |
| Via plating: microvia                  | 0.012 mm         | `t` of microvias (one dielectric, drill ≤ 0.15 mm)                   |
| Fallback via resistance                | 1.0 mΩ           | Per-hop R substituted when geometry is missing                        |
| Conductive fill resistivity            | 5×10⁻³ Ω·mm      | `ρ_fill` for the parallel rod inside conductively-filled vias        |
| Multi-pin coupling resistance          | 100 mΩ           | Star-couples pins within one terminal; not part of the barrel model  |
| Weight multi-pin coupling by pad area  | off              | When on, each star R scales as `R ∝ 1/A` (supply and GND terminals) |

Solver-side constants (`PLATING_THICKNESS_MM`, `PLATING_BLIND_MM`,
`PLATING_BURIED_MM`, `PLATING_MICROVIA_MM`, `COPPER_RESISTIVITY_OHM_MM`,
`FALLBACK_VIA_RESISTANCE_OHM`) are patched by
`SolveSettings.apply_to_modules` in
[fypa/altium/loader.py](../fypa/altium/loader.py) when
the user re-runs the solver from the Settings tab, so the next solve
picks up the new values without code changes.
