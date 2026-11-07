# Modifications relative to upstream `padne`

This directory is a **modified fork** of [padne](https://github.com/atx/padne)
vendored into FYPA. Upstream is GPL-3.0; see
[LICENSE.upstream](LICENSE.upstream) for the licence text. All modifications
below are © 2025 CuTree Designs and licensed under AGPL-3.0-or-later (matching
the umbrella project's licence).

The motivation for vendoring rather than depending on upstream was a chain
of small porting changes — primarily the mesher and a couple of Python-3.11
compatibility fixes — that haven't been upstreamed yet.

## Summary of changes

### `solver.py` — ground-node selection fix and debug logging

`find_best_ground_node_index()` previously iterated over **all** networks
(`prob.networks`) while the `NodeIndexer` was built only from the
**filtered** networks.  If the highest-voltage `VoltageSource` happened to
be in a dropped network, the function would `KeyError`; even when it
survived, it could select a reference node that was inconsistent with the
active FEM system, producing a large spurious ground-clamp current.

Changes:

* `find_best_ground_node_index` now accepts `networks: list[Network]`
  instead of the whole `Problem`; `solve()` passes `filtered_networks`.
  This guarantees every candidate index exists in `NodeIndexer`.
* Ground selection now votes: it counts how many `VoltageSource`s share
  each GND mesh vertex and picks the most-shared one (i.e. the common
  power-supply GND bus).  Ties are broken by highest source voltage —
  same behaviour as before for single-supply boards, more robust for
  multi-rail boards.
* `_log_network_breakdown()` helper logs a per-element-type count of
  active vs dropped networks at DEBUG level, plus an INFO line if any
  networks were dropped.
* `solve()` logs total active sink current (sum of all `CurrentSource`
  currents in `filtered_networks`) and the ground-vertex mesh coordinates.
* The `SolverWarning` message now includes the imbalance as a percentage
  of total sink current and describes the most likely root causes
  (isolated GND regions, no via path to reference).

### `mesh.py` — meshing backend ported from CGAL to `triangle`

Upstream uses a CGAL-based 2-D mesher exposed via a compiled `padne._cgal`
extension. CGAL wouldn't build on Windows on the development machine, so the
mesher was re-implemented on top of the pure-Python
[`triangle`](https://pypi.org/project/triangle/) PyPI package (a wrapper for
Jonathan Shewchuk's `triangle` C library).

* `_prepare_polygon_for_triangle()` converts a Shapely `Polygon` /
  `MultiPolygon` to the `vertices` / `segments` / `holes` dict that
  `triangle.triangulate` expects.
* `_build_triangle_switches()` builds the `pq{angle}a{area}Q` switch string
  matching the quality + max-area constraints upstream's CGAL mesher used.
* The resulting `Mesh` / `Vertex` / `Face` objects preserve upstream's
  public shape so the rest of the solver is untouched.

### `solver.py` — orphan-vertex guards

The Laplace operator construction (`laplace_operator()`) crashed on meshes
whose `triangle` output contained vertices with no incident half-edges
(`vertex.out is None`). Such vertices appear at sliver-triangle boundaries
on real-world boards.

* `laplace_operator()` now **skips** orphan vertices when accumulating the
  cotangent sum.
* The matrix's diagonal for skipped vertices is **pinned to `1.0`** so the
  linear system stays non-singular. The orphan's voltage is then driven to
  whatever the right-hand side dictates (typically zero).
* `NodeIndexer._construct_kdtrees()` **excludes** orphan vertices from the
  per-layer KDtree used to snap `Connection` points onto mesh vertices.
  Without this, a Connection whose `(x, y)` exactly matched an orphan's
  position (e.g. a SINK pin whose seed point landed FP-just-outside the
  polygon) would snap to the orphan and its CurrentSource / coupling-
  Resistor stamps would attach to the v=0-pinned node. That dumped the
  injected current into the pin term (`σ·v = I`) instead of through real
  copper, dragging any via-coupled copper on the other layer to `≈ I/σ`
  (a few mV) — visible in the viewer as a multi-volt fake "drop" the
  moment the affected layer was enabled.

### `solver.py` — Steiner-ring refinement at current-injection vertices

The 2-D FEM has a log singularity at every point-current source / sink. A
single mesh vertex carrying the injected current produces a voltage that
depends on the area of the triangles incident to it: bigger surrounding
triangles regularise the singularity over a larger area, suppressing the
voltage rise at the injection vertex and biasing the solved end-to-end
resistance **low**. The error grows with conductor length (the share of
total resistance accounted for by the spreading region falls, so the
constant per-pin error becomes a larger fraction of what's left).

Symptoms observed before the fix on a 1 mm wide, 35.56 µm thick copper
trace at the default 0.6 mm mesh size:

| Length | V_FEM | V_theory | Ratio |
|--------|-------|----------|-------|
| 10 mm  | 3.57 mV | 4.73 mV | 0.755 |
| 20 mm  | 6.50 mV | 9.45 mV | 0.688 |
| 100 mm | 25.73 mV | 47.25 mV | 0.545 |

Fix: at the point where each `Connection`'s seed point is confirmed inside
a geometry, also append a small ring of Steiner seed points (8 points,
25 µm radius) around it. The Triangle mesher preserves every input
vertex, so the ring forces fine triangles around the injection regardless
of the global `maximum_size`. Ring points that fall outside the containing
polygon are dropped (handles connections at the very tip of a thin track
or right on its boundary).

After the fix, the same length sweep at 0.6 mm global mesh size barely
moved (the singularity-at-the-injection-vertex was *not* the dominant
error source — see next section). The ring refinement was kept anyway
because it's cheap and is the standard way to regularise 2-D point
sources; it may matter on geometries other than the long thin strip used
for the regression test.

### `mesh.py` / `solver.py` — per-polygon adaptive `maximum_size`

Follow-up to the Steiner-ring change. Re-running the length sweep with
the ring alone showed the bias was nearly unchanged — meaning the FEM
under-estimation on thin conductors comes from the **transverse mesh
resolution across the strip width**, not from the singularity at the
injection vertices. A 1 mm trace meshed at the default 0.6 mm `maximum_size`
only gets ~2-3 vertices across after q20 quality refinement, and the
cotangent Laplacian on such a mesh is biased low for elongated domains.

Fix: derive a per-polygon `max_size` from a local-width estimator and
clamp it to the user-configured global cap:

```
characteristic_width ≈ 2 × polygon.area / polygon.length    # ≈ width for thin shapes
effective_max_size   = min(config_max_size, characteristic_width / 5)
```

A 1 mm × 20 mm trace gets ~0.19 mm automatically; a 50×50 mm pour stays at
the global 0.6 mm. Helper lives in `Mesher.polygon_adaptive_max_size` and
the FEM-bound `generate_meshes_for_problem` path now builds one Triangle
switches string per polygon. The display-only `generate_disconnected_meshes`
path still uses one shared switches string — those meshes aren't solved on.

Plumbing changes:
* `Mesher._build_triangle_switches` now accepts an optional
  `max_size_override` so callers can drop in the adaptive value without
  mutating the shared `Mesher.Config`.
* `_mesh_polygons_in_parallel` now takes `switches: list[str]` (one per
  polygon) instead of a single shared string. Callers that don't need
  per-polygon variation broadcast: `[s] * len(polys)`.

Limitations:
* The estimator averages over the whole polygon. **Composite polygons
  (e.g. a large pour with a thin spur on the same net) get the wide
  pour's averaged width and the spur is still under-refined.** Fixing
  that requires local-width sampling (medial axis) or per-region max-area
  attributes — deferred until real-board profiling shows it matters.
* Holes in the polygon inflate `polygon.length` and shrink the
  characteristic width estimate. For typical via-perforated pours this
  over-refines slightly; for pathologically perforated copper it could
  noticeably slow meshing.
* The refinement factor (5) is hardcoded. Make it a `Mesher.Config`
  field if users start asking for it.

### Correction: the "~5 % under-estimate" is gone

The two sections above (written before the signed-cotangent fix in
`_half_cotangent`) attribute a residual low bias on thin strips to
transverse mesh resolution. P1 elements reproduce a linear field exactly on
any mesh, so a uniform strip's interior cannot be biased by resolution: the
bias came from the unsigned cotangent, which over-conducted obtuse
triangles. Measured now, the interior slope of the strip fixtures matches
ρ/(W·t) to better than 1e-6 at every mesh size, and `tests/test_analytical.py`
holds it to 1e-5. The per-polygon `maximum_size` is still useful (it
resolves current crowding at bends and pads), just not for the reason given.

### `solver.py` / `problem.py` — drill-circle via contacts and mesh grading

Via hops used to land on a single point at the via centre (plus the
25 µm Steiner ring). A point contact in 2-D is a log singularity, so a via's
spreading resistance into a plane came out +80 % high and grew as the mesh
was refined. A hop now lands on the **drill circle**: `Connection(region=…,
barrel=True)` ties every vertex inside the drill to one node, as the plated
wall does. Barrel patches are claimed after pad patches (a stable sort on
`barrel`), so a via inside a terminal pad joins the pad's group.

Each patch also grades the mesh around it: rings of Steiner seeds at
r·1.4ᵏ (up to 5), spaced to keep triangles near equilateral, stopping at the
global element size or half-way to the next patch. Pads get the same, as
offsets of their outline. Error on a 0.3 mm via into a plane at the default
mesh: point contact +78.5 % → patch −4.1 % → patch + grading −0.02 %; two
pads on a disc (method of images) −1.0 % → −0.5 %. Grading seeds are
generated only for patches on meshed copper and tested in one vectorised
STRtree query per layer, which made seed placement *faster* than before on
boards with many signal vias. `barrel` is part of the mesh-assembly
fingerprint.

### `solver.py` / `problem.py` — remote sense

`VoltageSource` and `VoltageRegulator` take optional `sense_p` / `sense_n`
nodes: the constraint row reads them (`V(sense_p) − V(sense_n) = V`) while
the current column stays on the output pins. The system is then
asymmetric, which `matrix_is_symmetric` now accounts for (PARDISO's
unsymmetric path, LGMRES fallback; the adjoint already solves Lᵀ).

### `thermal.py` — board thermal model

A layered finite-difference board model (every net's copper on every
layer, laminate and plated barrels between layers, convection from both
faces) that the electro-thermal loop uses when `ThermalConfig.board` is
set, in place of the local `p / h`. `Resistor.heat_source` marks real
dissipating parts (vias, series parts); regulators add their conversion
loss. See the module docstring and `tests/test_thermal_board.py`.

### `ui.py` — Python-3.11 compatibility + minor robustness

* Replaced PEP 695 class-level generic syntax
  (`class Foo[T: HasIndex]: ...`) with explicit `TypeVar` /
  `Generic[T]` declarations so the module loads under Python 3.11.
* Same change applied to other files using PEP 695 generics
  (`mesh.py`, `solver.py`).

> **NB:** The upstream `ui.py` viewer is not currently used by FYPA —
> we ship a custom OpenGL viewer ([../gl_mesh_viewer.py](../gl_mesh_viewer.py))
> driven from [../altium_viewer.py](../altium_viewer.py) instead. `ui.py`
> remains in the tree for now but is unused.

### Other files

Unchanged from upstream beyond the PEP-695 generic-syntax port. Consult
[upstream's commit history](https://github.com/atx/padne/commits) for the
pre-port behaviour.
