# 8. Generating a design report

Once a design is solved, FYPA can write up the assessment as a **design
report**: one document a reviewer can read top to bottom, with every rail's
result, every warning, and the setup the numbers came from. Use it to
record a design review, to attach to a release, or to compare one board
revision with the next.

## 8.1 Generating one

1. Solve the design (see [the viewer tour](05-viewer-tour.md)).
2. Choose **File ▸ Export ▸ Report…** (<kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>R</kbd>).
3. Pick the format, content and limits (below), then **Generate…** and
   choose where to save it.

The report is built in the background; it opens when it is saved unless you
untick **Open the report when it's saved**. Your choices are remembered in
the `.fypa` project file for next time.

### HTML or PDF?

| Format   | Best for |
|----------|----------|
| **HTML** | Working reviews. One self-contained file (images embedded) that opens in any browser, with links from the summary to each rail's section. |
| **PDF**  | Formal records: design reviews, archiving, sending to customers. A4 pages with page numbers, and table headers repeated across page breaks. |

Tick **Also save the numbers as JSON** to write `<name>.json` next to the
report — the same results as data, for comparing two revisions or checking
them in a script.

## 8.2 How the report reads

**1 · Executive summary.** A banner states the outcome plainly — *1 of 5
rails failed*, *All 5 rails passed*, and so on — and says what drove it.
Below it, one row per rail: nominal voltage, load current, lowest load
voltage, worst drop, minimum margin, maximum via current, peak current
density, impedance against target, and status. The cell that caused a
failure is highlighted, not just the row. Then **Issues to resolve** lists
every failure and warning, each linked to its rail.

**2 · Power tree.** The Topology tab's diagram.

**One section per rail**, always in the same order so a reviewer knows where
to look:

| Section | What it shows |
|---------|---------------|
| Headline numbers | Lowest load, worst drop, load, max via current, copper loss, impedance |
| Findings | This rail's failures, warnings and unchecked results |
| Power path | Source and loads with the solved voltage at each load |
| DC voltage at loads | Voltage, drop (and how much of it is in the return path), minimum, margin, status |
| Heatmaps | Voltage drop and current density on the layers that matter, worst points circled |
| Copper by layer | Voltage spread, peak current density and copper loss per layer |
| Vias | The vias carrying the most current, and any over the limit |
| Series elements & regulators | Current, voltage drop and loss in each series part; regulator input / output |
| Decoupling & PDN impedance | \|Z(f)\| against the target mask, and the capacitor summary |
| Thermal | Temperature rise, when the solve ran electrothermal |

A section with nothing to show says why (*Not analysed. Electrothermal
coupling was off for this solve.*) rather than disappearing.

**A · Appendix.** Stackup, solve settings, the pass rules applied, every
load pin, every capacitor, and the warnings from the Messages tab. The
report ends with a sign-off block.

## 8.3 Reading the status

| Status | Meaning |
|--------|---------|
| ✖ **FAIL** | A result is outside its limit. |
| ▲ **WARN** | Within limits but worth a look (small margin, flagged capacitor, annotation warning). |
| ✔ **PASS** | Checked against a limit and within it. |
| – **UNCHECKED** | Computed, but there was **no limit** to check it against. |

UNCHECKED matters: a sink with no `PDN_MIN_V` still gets its voltage
calculated, but nothing says whether that voltage is good enough — so the
report shows it grey, never green. Status is always a symbol and a word, so
a black-and-white printout still reads.

## 8.4 The rules

| Check | Result |
|-------|--------|
| Load voltage below its `PDN_MIN_V` | FAIL |
| Load meets `PDN_MIN_V` by less than the margin setting (default 1 % of nominal) | WARN |
| Drop above the drop budget (optional, % of nominal) | FAIL |
| A via carrying more than the via current limit | FAIL |
| Peak current density above the limit (optional, A/mm) | WARN |
| \|Z\| above the target mask anywhere up to F_MAX | FAIL |
| Capacitor flagged, or its loop inductance over the warning level | WARN |
| Annotation errors, mesh failures, copper connectivity breaks | FAIL |
| Annotation warnings, unannotated bridges, unsolved rails | WARN |
| The design was edited after the last solve | WARN, plus a banner |

The via limit starts from **Via current warning level** in the Settings tab.
The impedance target and VRM model are the ones set per rail in the
Impedance tab.

### How the numbers are defined

- **Voltage at a load** is the load's lowest supply pin minus its highest
  return pin — the worst differential voltage the part can see.
- **Drop** is the rail's setpoint (its source voltage or regulator output)
  minus that voltage, so it includes the return path. The report shows how
  much of the drop is in the return.
- **Load current** is the sum of the rail's sinks plus the estimated input
  current of any regulator it feeds (gain × output current + quiescent).
- **Peak current density** is the 99.9th percentile over the mesh, so a
  single-vertex spike at a pin doesn't stand in for the copper.

> The decoupling section uses the loop inductances already computed in the
> Capacitors tab. Run **Tier 2/3** there first if you want FEM values in the
> report rather than the Tier 1 estimate.

## 8.5 From the command line

```sh
python FYPA.py solve  YourBoard.PrjPcb out.pkl
python FYPA.py report out.pkl report.pdf --revision B --author "A. Engineer"
```

`report.html` or `report.pdf` picks the format. Useful options:

| Option | Effect |
|--------|--------|
| `--json` | Also write the numbers to `report.json` |
| `--detail failing\|all\|summary` | How much of each rail to print |
| `--via-limit A`, `--margin-warn-pct`, `--drop-budget-pct`, `--j-limit` | The limits from 8.4 |
| `--fail-on fail\|warn` | Exit with status 1 on a failure (or a warning) — for CI |

The command-line report leaves out the decoupling analysis, which needs the
viewer's capacitor identification; use **File ▸ Export ▸ Report…** for that.
