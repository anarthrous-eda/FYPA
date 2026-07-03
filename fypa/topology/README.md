# PDN Topology (`fypa.topology`)

Abstract PDN flow diagram: components as column layout, orthogonal wire routing,
junction points and bridge symbols, net labels, and SVG rendering for the FYPA viewer.

The viewer rebuilds the diagram live while editor-mode changes are pending (before
Resolve), using :func:`fypa.topology.preview.metadata_for_topology`.

``fypa.topology`` uses **lazy exports** in ``__init__.py`` so importing
``fypa.topology.constants`` or ``fypa.topology.net_aliases`` does not load the
full build pipeline (avoids circular imports with ``fypa.rail_groups``).

## Pipeline

```
metadata (directives, net_canonical)
    │
    ▼
placement.plan_signal_buses()   deterministic bus_x (shared by layout + routing)
    │
    ▼
layout.build_node_layout()      columns, nodes, ports, per-gap width from BusPlan
    │
    ▼
routing.build_wires(bus_plan)   signal + GND paths (routing/ subpackage)
    │
    ▼
metadata.external_feed_wires()  dashed "external" stubs
    │
    ▼
labels.finalize_wire_labels()  one label per net (explicit search space)
    │
    ▼
TopologyModel  ──►  render.render_topology_svg()
                 ──►  report.topology_wiring_report()
                 ──►  validate.validate_topology()
```

Single-pass: layout gap widths come from `placement.plan_signal_buses()` — no
measure-then-rebuild routing pass.

## Metadata contract

Topology consumes a **subset** of the solve metadata dict produced by
`build_solve_metadata()` — not the full viewer/FEM bundle (~30 top-level keys).

Typed shapes live in `metadata_schema.py`:

| Type | Purpose |
|------|---------|
| `TerminalPinDict` | Pin record inside a directive terminal |
| `TerminalDict` | Terminal with optional `ideal_return`, `pins` |
| `DirectiveDict` | One SOURCE/SINK/REGULATOR/… directive |
| `NodeSpec` | One layout component: `port_defs`, `terms`, `directives`, … |
| `PortDef` | Port layout tuple: `(terminal_name, side, sort_key)` |
| `JumpRowDict` | Footprint pin row for editor jump targets on a node |
| `TopologyMetadata` | Pipeline input: `directives`, `net_canonical`, `annotation_errors` |

Placement and bus-plan keys live in `placement/types.py`:

| Type | Purpose |
|------|---------|
| `ColumnSideKey` | `(column_x, "left" \| "right")` — stack hub lane |
| `GutterSpanKey` | `(x_lo, x_hi)` — column gap shared by gutter nets |
| `GapRoutingKey` | `("gap", x_lo, x_hi)` — 2-port gutter routing group |
| `StackBusKey` | `(column_x, side, net)` — planned bus beside a stack column |
| `GndTrunkKey` | `(trunk_x, side)` — planned GND column trunk |

`build_topology_model()`, `build_node_layout()`, and `compute_rail_groups()` accept
`TopologyMetadata | None`. Fields are optional (`total=False`) to match real pickles
and trimmed JSON fixtures.

Dev/CI validation: `assert_topology_metadata(data)` — lightweight shape check used by
`tests/test_topology_metadata_schema.py` on every fixture under
`tests/fixtures/topology/*.json`.

Extract a fixture from a pickle:

```python
import pickle, json
md = pickle.load(open("front.pkl", "rb"))["metadata"]
trim = {k: md[k] for k in ("directives", "net_canonical", "annotation_errors") if k in md}
json.dump(trim, open("tests/fixtures/topology/my_case.json", "w"), indent=2)
```


| Module | Responsibility |
|--------|----------------|
| `types.py` | `TopologyNode`, `TopologyPort`, `TopologyWire`, `TopologyModel` |
| `constants.py` | Layout/routing/label constants |
| `placement/` | `ports.py`, `bus_grid.py`, `hub_planning.py`, `classify.py`, `types.py`, `plan_types.py`, `plan_lookup.py`, `plan_gnd.py`, `plan_pairs.py`, `plan_hubs.py`, `plan.py` |
| `metadata/` | `layout_bridge`, `nets`, `specs`, `tooltips`, `feeds` — directive parsing |
| `metadata_schema.py` | `TopologyMetadata`, `NodeSpec`, `DirectiveDict`, … TypedDict contract |
| `terminal_roles.py` | `is_power_input_port`, `is_output_port` (shared role rules) |
| `net_aliases.py` | `GND_ALIASES`, `is_gnd_alias()` (import-light, shared with `rail_groups`) |
| `layout/` | `columns.py`, `vertical_align.py`, `stubs.py`; `build_node_layout()` in `__init__.py` |
| `layout_result.py` | `LayoutResult` dataclass returned by `build_node_layout()` |
| `routing/` | Wire routing: `context`, `paths`, `obstacles`, `hub`, `pair`, `gnd`, `build` |
| `geometry.py` | Path parser, segments, junctions, `solid_wire_index_maps()` |
| `labels.py` | Label placement via documented candidate search |
| `issues.py` | Shared `Issue` / `make_issue()` for validate + report |
| `render.py` | SVG drawing (wires, nodes, GND symbol, legend) |
| `validate/` | `segments.py`, `wires.py`, `labels.py`, `stubs.py`, `hub.py`, `util.py`; `validate_topology()` in `__init__.py` |
| `report.py` | `topology_wiring_report()` — JSON debug output |
| `hit_test.py` | `find_component_at`, `find_port_at`, `find_wire_at`, `topology_net_at` |
| `builder.py` | `build_topology_model()` — orchestrates the pipeline |
| `preview.py` | `metadata_for_topology()` — live editor preview metadata |
| `util.py` | Formatting (Ω, mA, V), `truncate_label()` |
| `svg_testutil.py` | Test-only SVG normalization + fixed theme |

## Routing

### Strategy by Port Count

| Ports per net | Strategy | `routing_kind` |
|---------------|----------|----------------|
| 2 | Gutter (column gap) or stack (stacked nodes) | `gutter`, `stack_column` |
| ≥ 3, same column | Hub bus beside the column | `hub`, `hub_row`, `hub_tap` |
| ≥ 3, across columns | Hub with vertical trunk in the gutter | `hub`, `hub_row`, `hub_tap` |

**Hub routing** (`routing/hub.py::route_hub`) models a net as a **tree**:

1. **Row buses** — collinear groups of two or more ports on the same Y become a
   horizontal `hub_row` wire (`paths.hub_row_path`, stub span from
   `paths.hub_row_stub_columns`). Obstacles may push the row downward via
   `obstacle_detour_y` (a *detoured* row keeps its nominal port Y for vertical
   drops onto the bus).
2. **Trunk** — when taps meet the hub column at different Y values, one vertical
   `hub` wire at `bus_x` spans `min(tap_y)…max(tap_y)`.
3. **Taps** — each port reaches a row or the trunk via `hub_tap` wires: straight
   vertical onto a row, horizontal feed from a row edge to `bus_x`, escape-column
   detour when a foreign vertical blocks the stub column, or a direct trunk tap for
   singletons.

Row-to-trunk attachment (`_connect_row_to_bus`) retries horizontal feed Y values from
`obstacle_detour_y_candidates`, reserving vertical/horizontal bands and checking
`trunk_vertical_clear` plus foreign-segment blocking before emitting a feed wire.

The same function serves gutter and beside-column buses — only `bus_x` differs.
Keeping each segment minimal and non-overlapping (instead of a single retracing
polyline) is exactly what junction detection relies on: interior taps meeting the
trunk give a dot, the top/bottom taps are corners. A single-row hub with no trunk
feed may have no `hub` wire — collinear row/tap geometry forms the bus.

### RoutingContext

Reserves channels **during** path generation (bands only — bus x comes from `BusPlan`):

- **Horizontal bands** — occupied y ranges for collision avoidance
- **Vertical bands** — occupied x ranges (`reserve_vertical`)
- **`obstacle_detour_y()`** — first feasible Y below obstacles and reserved bands
  (net-aware: same-net bands may share a row; foreign bands force a push downward)
- **`obstacle_detour_y_candidates()`** — ordered Y list for retry loops (row feeds,
  detoured hub rows); deduplicates within `WIRE_EPS`

Bus positions are planned in `placement.plan_signal_buses()` using
`allocate_bus_x()` on a `MIN_PARALLEL_GAP` grid (deterministic, no iterative bumping).
`placement/plan_lookup.py` mirrors the routing lookup order for CI parity tests
(`pair_buses` → `hub_buses` → `stack_buses`; GND trunks via path geometry).

Signal wires store the planned vertical at `TopologyWire.bus_x`. GND routes do
**not** set `bus_x` — trunk positions come from `BusPlan.gnd_trunks` and
`gnd_trunk` wire paths instead.

### GND (routing only)

GND uses the same junction rules as every other net. Routing is a **tree** per column:

- Horizontal return rail below all nodes (`gnd_rail`)
- One vertical **trunk** per column (`gnd_trunk`) from the rail up to the highest port
- One horizontal **tap** per port (`gnd_tap`) from the port to the trunk
- Column x comes from `gnd_drop_x` (node obstacles) then trunk placement at the
  GND stub column. Interior taps where trunk meets the rail (3 directions) get a
  junction dot; rail endpoints stay corners (2 directions).

### Spacing rules (enforced in routing + validation)

1. **No two foreign vertical segments on the same x** (same-net trunks may share x).
2. **No two foreign horizontal segments on the same y with overlapping x** (same-net
   collinear bus taps may share y).
3. **GND junction dots** — interior taps on the rail only (≥3 directions), not rail
   endpoints.

### Obstacle avoidance (orthogonal detours)

Signal and hub routes push **horizontally** around node bodies via
`obstacle_detour_y` (Y shifted downward). GND drops use the same idea on the
**outward X axis** in `gnd_drop_x`.

## Geometry

Orthogonal SVG paths (`M` / `H` / `V`) are decomposed into segments. Rules apply per net, identically for GND and signals.

- **Junction** — count the distinct directions (up/down/left/right) in which same-net wire leaves a point; **3 or more** → dot. Two directions are a straight pass-through or a 90° corner (no dot). Overlapping collinear segments merge into one direction, so this is fully net-agnostic (GND taps, hub taps, and signal Ts all use the same rule).
- **Bridge** — different nets → semicircle arc on the vertical

**Single source of truth:** `geometry.compute_schematic_geometry(wires)` decomposes
the wires once and returns segments, junctions, and bridge crossings in a
`SchematicGeometry`. Rendering, the report, and label placement all consume this
result, so the drawn SVG, the JSON report, and validation can never diverge.

`find_junctions(segments)` is a **low-level helper** on raw segments only (wire
geometry, 3+ same-net directions). It does **not** include GND-symbol junctions
that `compute_schematic_geometry` adds via `_gnd_symbol_junctions`. For report or
render parity, always use `compute_schematic_geometry(...).junctions`.

## Validation (`validate_topology`)

| Code | Check |
|------|-------|
| `segment_through_foreign_node` | Horizontal **or vertical** segment intersects foreign node |
| `parallel_vertical_gap` | Vertical buses in the **same gutter span** closer than `MIN_PARALLEL_GAP` (16 px) |
| `signal_vs_gnd_drop_gap` | Signal bus too close to GND trunk x |
| `duplicate_vertical_x` | Foreign (or duplicate same-net) vertical segments share x |
| `duplicate_horizontal_y` | Foreign horizontal segments share y with overlapping x |
| `junction_near_bridge` | Junction dot within bridge arc clearance on same vertical |
| `label_not_at_origin` | Label at (0, 0) |
| `label_anchor_distance` | Label too far from anchor segment |
| `open_gnd_stub` | GND port stub end not connected (`validate/stubs`) |
| `open_signal_stub` | Signal port stub end not connected (`validate/stubs`) |
| `dangling_wire_endpoint` | Wire path end not at port, GND symbol, or junction |
| `hub_net_disconnected` | A hub-routed net (`hub` / `hub_row` / `hub_tap`) with 2+ ports has them in more than one wire-graph component (`validate/hub.py`) |
| `foreign_wire_crossing` | Foreign horizontal/vertical bus segments cross in a shared gutter (`validate/segments`; hub vs pair nets; trunk verticals + horizontals only) |
| `canvas_width_reasonable` | Canvas wider than `MAX_CANVAS_WIDTH` (2400 px) |
| `vertical_under_node` | Vertical segment runs under a node body (**warning** — does not increment `summary.issues`; no src/dst skip unlike `segment_through_foreign_node`) |

**Severity:** `topology_wiring_report()` counts only non-warning issues in
`summary.issues`. Warnings (e.g. `vertical_under_node`) appear in the full
`issues` list but do not fail the zero-issues gate.

### Stacked port stubs

Outgoing (right): top signal port longest stub, bottom shortest.
Incoming (left): top shortest, bottom longest.
GND ports: always `GND_PORT_WIRE_STUB` (12 px), excluded from stagger.

### Label search order

1. Carrier wire by `routing_kind` priority (`hub_row` → `hub` → `gutter` → …)
2. **Horizontal** on the widest run at `bus_x` (source-side gutter row first)
3. Other long / short horizontal segments
4. Beside vertical at `bus_x` (only when no qualifying horizontal run)
5. Fallbacks (`LABEL_FALLBACK_OFFSETS`)

Offsets start at `LABEL_WIRE_OFFSET` (5 px). Junction dots use `geo.junctions` only;
bridge hops are checked separately via `_clear_of_bridges` (same-net horizontal
labels may sit beside a crossing on the wire row).

Additionally, `report._analyze_wire_issues()` checks per wire: dangling ends,
port mismatch, backtrack. Trunk/rail/tap kinds (`hub`, `hub_row`, `hub_tap`,
`gnd_rail`, `gnd_trunk`, `gnd_tap`) terminate on a bus rather than a port, so
their non-port ends are expected and not flagged.

`topology_wiring_report()` returns `summary.issues == 0` when everything is OK.

## Public API

Import via `fypa.topology`:

```python
from fypa.topology import (
    TopologyMetadata,
    assert_topology_metadata,
    build_topology_model,
    compute_schematic_geometry,
    find_wire_at,
    merge_validation_issues,
    parse_topology_directives,
    render_topology_svg,
    topology_wiring_report,
    validate_topology,
    find_component_at,
    topology_tooltip_at,
)
```

Display labels are truncated consistently via `util.truncate_label` (22 chars).

Debug report as JSON:

```bash
uv run python tools/dump_topology_wiring.py front.pkl -o wiring.json
uv run python tools/dump_topology_wiring.py front.pkl --issues-only
```

## Tests & Fixtures

Committed metadata fixtures (CI, no pickle required):

```
tests/fixtures/topology/
  front_like.json              small front layout (J1, U2, U1, R1)
  front_hub_vdd.json           full front design (hub VDD, D1, LEDs)
  column_gnd_feedback.json     REGULATOR OUT_N / GND column bug
  gutter_parallel_four_nets.json  parallel LED buses (D1/U4)
  gnd_junction_tap.json        two sinks on return rail (junction regression)
  hub_gutter_row_detour.json   hub row pushed below obstacle + row-to-trunk feed
  hub_escape_vertical_branch.json  escape-column tap when stub vertical blocked
  sandbox_subset.json          trimmed sandbox metadata
  svg/                         golden SVG snapshots (visual regression)
    front_hub_vdd.svg
    front_like.svg
    column_gnd_feedback.svg
```

Helper: `tests/topology_fixtures.py` — `load_topology_fixture(name)`.

```python
from tests.topology_fixtures import load_topology_fixture
model = build_topology_model(load_topology_fixture("front_hub_vdd"))
```

Test files:

| File | Content |
|------|---------|
| `test_topology_invariants.py` | parametrized: all fixtures → `issues == 0`, full bus-plan parity |
| `test_topology_regressions.py` | named tests per historical bug |
| `test_topology_layout.py` | columns, compactness, hit test |
| `test_topology_geometry.py` | junction/bridge classification |
| `test_topology_labels.py` | label placement |
| `test_topology_preview.py` | live editor metadata preview |
| `test_topology_metadata_schema.py` | fixture shape validation |
| `test_topology_svg_snapshots.py` | rendered SVG golden files |
| `test_terminal_roles.py` | power input / output port classification |
| `test_placement.py` | stub keys, gutter span, GND trunk |
| `test_metadata_specs.py` | directive → component spec parsing |
| `test_hit_test.py` | `find_port_at` |
| `test_svg_testutil.py` | SVG normalizer |
| `test_validate_merge.py` | `merge_validation_issues` |
| `test_validate_stubs.py` | geometry-based stub end connectivity |
| `test_pdn_topology.py` | routing scenarios, sandbox, report |
| `test_routing_hub.py` | hub row/trunk/tap unit tests (`route_hub`, stub columns) |
| `test_hub_routing_regressions.py` | `hub_gutter_row_detour`, `hub_escape_vertical_branch` fixtures |
| `test_validate_codes.py` | validation issue codes including `hub_net_disconnected` |

Run:

```bash
uv run python -m pytest tests/test_topology_invariants.py \
  tests/test_topology_regressions.py tests/test_topology_layout.py \
  tests/test_topology_geometry.py tests/test_topology_labels.py \
  tests/test_topology_preview.py tests/test_topology_metadata_schema.py \
  tests/test_topology_svg_snapshots.py tests/test_terminal_roles.py \
  tests/test_placement.py tests/test_metadata_specs.py tests/test_hit_test.py \
  tests/test_svg_testutil.py tests/test_validate_merge.py \
  tests/test_validate_stubs.py tests/test_validate_codes.py \
  tests/test_routing_hub.py tests/test_hub_routing_regressions.py \
  tests/test_pdn_topology.py -q
```

Or via `scripts/test-pdn-topology.ps1` (pytest before GUI start).

### SVG regression

Snapshot tests compare normalized SVG from `render_topology_svg()` against golden
files in `tests/fixtures/topology/svg/`. Tests use a fixed theme
(`svg_testutil.DEFAULT_TEST_THEME`) — not the viewer's `current_theme()`.

Regenerate goldens after intentional layout/render changes:

```bash
# PowerShell
$env:UPDATE_TOPOLOGY_SVG="1"; uv run pytest tests/test_topology_svg_snapshots.py -q

# bash
UPDATE_TOPOLOGY_SVG=1 uv run pytest tests/test_topology_svg_snapshots.py -q
```

After updating, also verify `topology_wiring_report()` / wiring.json if routing changed.

## Key Constants

| Constant | Value | Meaning |
|----------|-------|---------|
| `MIN_PARALLEL_GAP` | 16 px | Minimum spacing of parallel vertical buses |
| `PORT_WIRE_STUB` | 20 px | Stub length at port |
| `LABEL_WIRE_OFFSET` | 5 px | Label offset from wire |
| `MAX_CANVAS_WIDTH` | 2400 px | Warning threshold for column feedback bugs |
| `COL_GAP` | 100 px | Default column spacing (widened per-gap only where gutter buses need it) |

## Extension

`layout/` and `routing/` share net classification via `placement.classify_signal_nets()`
and gutter spans via `placement.plan_signal_buses()` / `placement.net_gutter_key` —
layout never imports routing.

New routing rules → `routing/` (+ fixture + invariant + regression test).

New invariant → appropriate module under `validate/`, covered in `test_topology_invariants.py`.

New fixture from Altium project — see **Metadata contract** above for the trim keys.

```python
# One-time: extract metadata from pickle (same trim as fixture convention)
import pickle, json
md = pickle.load(open("front.pkl", "rb"))["metadata"]
trim = {k: md[k] for k in ("directives", "net_canonical", "annotation_errors") if k in md}
json.dump(trim, open("tests/fixtures/topology/my_case.json", "w"), indent=2)
```
