#!/usr/bin/env python3
"""Generate a minimal Altium project that reproduces the sheet-entry hotspot bug.

Writes Altium files and a GitHub-ready bug report under ``_probe/sheet-entry-hotspot-repro/``.
Run from the FYPA repo root::

    uv run python scripts/generate_sheet_entry_hotspot_repro.py
"""
from __future__ import annotations

import textwrap
from pathlib import Path

from altium_monkey import (
    AltiumPrjPcbBuilder,
    AltiumSchDoc,
    ColorValue,
    LineWidth,
    PortIOType,
    PortStyle,
    SchFontSpec,
    SchHorizontalAlign,
    SchPointMils,
    SchRectMils,
    SchSheetEntryIOType,
    SchSheetSymbolType,
    SheetEntrySide,
    TextJustification,
    TextOrientation,
    make_sch_file_name,
    make_sch_port,
    make_sch_sheet_entry,
    make_sch_sheet_name,
    make_sch_sheet_symbol,
    make_sch_wire,
)
from altium_monkey.altium_netlist_options import NetlistOptions
from altium_monkey.altium_netlist_single_sheet import AltiumNetlistSingleSheetCompiler
from altium_monkey.altium_prjpcb import NetIdentifierScope
from altium_monkey import AltiumSchDoc

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = REPO_ROOT / "_probe" / "sheet-entry-hotspot-repro"

# Geometry mirrors a hierarchical hub sheet with two adjacent left-side entries whose
# fractional DistanceFromTop values place wires at native Y=566 and Y=555 (mils * 10).
SYM_LEFT_MILS = 3240.0
SYM_TOP_MILS = 6020.0
SYM_RIGHT_MILS = 4890.0
SYM_BOTTOM_MILS = 5430.0
WIRE_A_Y_MILS = 5660.0
WIRE_D_Y_MILS = 5550.0
WIRE_LEFT_MILS = 3010.0
PORT_X_MILS = 2060.0

# Fractional offsets that differ from distance_from_top * 10 (see make_sch_sheet_entry).
ENTRY_A_MILS = 354.3306
ENTRY_D_MILS = 472.4408


def _port(name: str, y_mils: float):
    return make_sch_port(
        location_mils=SchPointMils.from_mils(PORT_X_MILS, y_mils),
        name=name,
        width_mils=940,
        height_mils=100,
        io_type=PortIOType.UNSPECIFIED,
        style=PortStyle.LEFT,
        font=SchFontSpec(name="Arial", size=10),
        border_color=ColorValue.from_hex("#000000"),
        fill_color=ColorValue.from_hex("#D9EAF7"),
        text_color=ColorValue.from_hex("#073763"),
        alignment=SchHorizontalAlign.LEFT,
        border_width=LineWidth.SMALL,
        auto_size=False,
        show_net_name=True,
    )


def _wire(y_mils: float):
    return make_sch_wire(
        points_mils=[
            SchPointMils.from_mils(WIRE_LEFT_MILS, y_mils),
            SchPointMils.from_mils(SYM_LEFT_MILS, y_mils),
        ],
        color=ColorValue.from_hex("#000080"),
        line_width=LineWidth.SMALL,
    )


def _build_hub_sheet() -> AltiumSchDoc:
    schdoc = AltiumSchDoc()
    symbol = make_sch_sheet_symbol(
        bounds_mils=SchRectMils.from_corners_mils(
            SYM_LEFT_MILS,
            SYM_TOP_MILS,
            SYM_RIGHT_MILS,
            SYM_BOTTOM_MILS,
        ),
        border_width=LineWidth.MEDIUM,
        symbol_type=SchSheetSymbolType.NORMAL,
    )
    symbol.add_entry(
        make_sch_sheet_entry(
            name="VRAIL_A",
            side=SheetEntrySide.LEFT,
            io_type=SchSheetEntryIOType.UNSPECIFIED,
            distance_from_top_mils=ENTRY_A_MILS,
            font=SchFontSpec(name="Arial", size=10),
        )
    )
    symbol.add_entry(
        make_sch_sheet_entry(
            name="VRAIL_D",
            side=SheetEntrySide.LEFT,
            io_type=SchSheetEntryIOType.UNSPECIFIED,
            distance_from_top_mils=ENTRY_D_MILS,
            font=SchFontSpec(name="Arial", size=10),
        )
    )
    symbol.set_sheet_name(
        make_sch_sheet_name(
            text="LEAF",
            location_mils=SchPointMils.from_mils(SYM_LEFT_MILS + 100, SYM_TOP_MILS + 100),
            font=SchFontSpec(name="Arial", size=12, bold=True),
            orientation=TextOrientation.DEGREES_0,
            justification=TextJustification.BOTTOM_LEFT,
        )
    )
    symbol.set_file_name(
        make_sch_file_name(
            text="leaf.SchDoc",
            location_mils=SchPointMils.from_mils(SYM_LEFT_MILS + 100, SYM_BOTTOM_MILS - 100),
            font=SchFontSpec(name="Arial", size=10, italic=True),
            orientation=TextOrientation.DEGREES_0,
            justification=TextJustification.TOP_LEFT,
        )
    )
    schdoc.add_object(symbol)
    schdoc.add_object(_wire(WIRE_A_Y_MILS))
    schdoc.add_object(_wire(WIRE_D_Y_MILS))
    schdoc.add_object(_port("VRAIL_A", WIRE_A_Y_MILS))
    schdoc.add_object(_port("VRAIL_D", WIRE_D_Y_MILS))
    return schdoc


def _build_leaf_sheet() -> AltiumSchDoc:
    schdoc = AltiumSchDoc()
    schdoc.add_object(_port("VRAIL_A", 4000.0))
    schdoc.add_object(_port("VRAIL_D", 3800.0))
    return schdoc


def _compile_vrail_nets(hub_path: Path) -> list[tuple[str, list[str], list[str]]]:
    """Single-sheet compile on hub — shows mis-attached port UIDs."""
    hub = AltiumSchDoc(hub_path)
    opts = NetlistOptions()
    opts.net_identifier_scope = NetIdentifierScope.HIERARCHICAL
    nl = AltiumNetlistSingleSheetCompiler(hub, options=opts).generate()
    uid_to_name = {p.unique_id: p.name for p in hub.get_ports() if p.unique_id}
    rows = []
    for net in nl.nets:
        if "VRAIL" not in net.name:
            continue
        port_names = [uid_to_name.get(uid, "?") for uid in net.graphical.ports]
        rows.append((net.name, port_names, list(net.graphical.sheet_entries)))
    return rows


def _write_bug_report(rows_unpatched: list[tuple[str, list[str], list[str]]]) -> None:
    actual = "\n".join(
        f"{name} port_names= {ports} sheet_entries= {entries}"
        for name, ports, entries in rows_unpatched
    ) or "(no VRAIL nets found)"
    body = textwrap.dedent(
        f"""\
## Summary

`_extract_sheet_entries()` in `altium_netlist_single_sheet.py` computes the connection hotspot of a sheet-symbol entry as `entry.distance_from_top * 10`, ignoring the fractional part `distance_from_top_frac1`. For entries not placed on an exact 100-mil step, the computed hotspot drifts by several drawing units. Combined with the default connection tolerance of `5`, a drifted entry can snap onto the wire of an **adjacent** entry/port. This silently cross-connects two electrically distinct nets and, in hierarchical designs, merges them through the sheet-entry ↔ port bridge.

The SVG renderers already compute this offset correctly via `entry._distance_from_top_native_units()` (= `distance_from_top*10 + distance_from_top_frac1/100000`), so only the netlist compiler is affected.

The same truncation exists for harness entries in `altium_netlist_multi_sheet.py`, `_expand_harness_entries()` (`entry.distance_from_top * 10`).

This is a different root cause from [#9](https://github.com/wavenumber-eng/altium_monkey/issues/9) (wire crossing without a junction); here the wires are separate and the entry hotspot geometry is wrong.

## Version And Environment

- Altium Monkey version: `v2026.5.20` (also present on current `main`)
- Python version: 3.12
- Operating system: Windows
- Altium Designer version, if relevant: n/a (reproduced via altium_monkey netlist compiler only)

## File Type

Which file type is involved?

- [x] `.SchDoc`
- [ ] `.SchLib`
- [ ] `.PcbDoc`
- [ ] `.PcbLib`
- [x] `.PrjPcb` (optional; single-sheet compile on `hub.SchDoc` is sufficient)
- [ ] `.OutJob`
- [ ] Other:

## Reproduction

Minimal proof that the fractional offset is dropped (no schematic needed):

```python
from altium_monkey import make_sch_sheet_entry, SheetEntrySide

e = make_sch_sheet_entry(
    name="VRAIL_D",
    side=SheetEntrySide.LEFT,
    distance_from_top_mils=470,
)
print(e.distance_from_top, e.distance_from_top_frac1)  # -> 4 700000
print(e._distance_from_top_native_units())             # -> 47.0   (correct; used by SVG)
print(e.distance_from_top * 10)                        # -> 40     (used by the netlist compiler)
```

Single-sheet compile on the attached `hub.SchDoc`:

```python
from altium_monkey import AltiumSchDoc
from altium_monkey.altium_netlist_single_sheet import AltiumNetlistSingleSheetCompiler
from altium_monkey.altium_netlist_options import NetlistOptions
from altium_monkey.altium_prjpcb import NetIdentifierScope

hub = AltiumSchDoc("hub.SchDoc")
opts = NetlistOptions()
opts.net_identifier_scope = NetIdentifierScope.HIERARCHICAL
nl = AltiumNetlistSingleSheetCompiler(hub, options=opts).generate()
uid_to_name = {{p.unique_id: p.name for p in hub.get_ports() if p.unique_id}}
for net in nl.nets:
    if "VRAIL" in net.name:
        ports = [uid_to_name.get(u, "?") for u in net.graphical.ports]
        print(net.name, "port_names=", ports, "sheet_entries=", net.graphical.sheet_entries)
```

## Minimal Reproduction

Attached in this folder (generic names, no customer data):

- `hub.SchDoc` — sheet symbol with two adjacent left-side entries (`VRAIL_A`, `VRAIL_D`) at fractional `DistanceFromTop`, wires, and matching ports
- `leaf.SchDoc`, `min.PrjPcb` — optional hierarchy context

Regenerate with:

```powershell
uv run python scripts/generate_sheet_entry_hotspot_repro.py
```

## Expected Behavior

Each net carries the port UID and sheet entry that match its name:

```
VRAIL_A port_names= ['VRAIL_A', 'VRAIL_A'] sheet_entries= ['…_VRAIL_A']
VRAIL_D port_names= ['VRAIL_D', 'VRAIL_D'] sheet_entries= ['…_VRAIL_D']
```

In multi-sheet designs, hierarchy links should connect parent entries to child ports of the same name without spurious net aliases.

## Actual Behavior

On `v2026.5.20`, `VRAIL_D`'s sheet entry snaps to `VRAIL_A`'s wire; the net named `VRAIL_D` receives `VRAIL_A` port UIDs, while `VRAIL_A` has no ports:

```
{actual}
```

In multi-sheet designs this mis-attachment propagates into spurious net aliases and incorrect hierarchy bridges.

---

### Proposed fix (for maintainers)

`altium_netlist_single_sheet.py`, `_extract_sheet_entries()`:

```diff
-        dist = (
-            entry.distance_from_top * 10
-        )  # Convert to CoordPoint (10-mil) units
+        dist = round(entry._distance_from_top_native_units())
```

`altium_netlist_multi_sheet.py`, `_expand_harness_entries()`:

```diff
-                    entry_y = connector.location.y - entry.distance_from_top * 10
+                    entry_y = connector.location.y - round(
+                        entry._distance_from_top_native_units()
+                    )
```

Backward compatible: when `distance_from_top_frac1 == 0`, the result equals `distance_from_top * 10`.
"""
    ).strip()
    (OUT_DIR / "BUGREPORT.md").write_text(body + "\n", encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    hub_path = OUT_DIR / "hub.SchDoc"
    leaf_path = OUT_DIR / "leaf.SchDoc"
    prj_path = OUT_DIR / "min.PrjPcb"

    _build_hub_sheet().save(hub_path)
    _build_leaf_sheet().save(leaf_path)

    (
        AltiumPrjPcbBuilder(
            "sheet-entry-hotspot-repro",
            net_identifier_scope=NetIdentifierScope.HIERARCHICAL,
        )
        .add_schdoc(hub_path.name)
        .add_schdoc(leaf_path.name)
        .save(prj_path)
    )

    rows = _compile_vrail_nets(hub_path)
    _write_bug_report(rows)

    print(f"Wrote repro project to {OUT_DIR}")
    print("Single-sheet VRAIL nets on hub.SchDoc (unpatched altium_monkey):")
    for name, ports, entries in rows:
        print(f"  {name!r} port_names={ports} sheet_entries={entries}")
    print(f"Bug report: {OUT_DIR / 'BUGREPORT.md'}")


if __name__ == "__main__":
    main()
