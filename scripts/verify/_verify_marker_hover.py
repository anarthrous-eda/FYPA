"""Runtime driver for the editor-mode marker-hover staleness fix.

Reproduces the reported bug: after an Altium-defined component is unlocked
and given a new value in editor mode, the bottom-bar marker hover kept
showing the *old* solved value until a Resolve. Builds the real PdnViewer
from a cached solve, applies an editor override exactly as the Apply button
does, then hit-tests the hover index the bottom bar uses.

Not a unit test — the QApplication, the viewer and _update_markers_and_legend
all run for real.
"""
import os
import sys

OUT = r"C:\Users\garyp\AppData\Local\Temp\fypa_verify"
PKL = r".cache\Sandbox_baa0646cea565240\solve.pkl"
os.makedirs(OUT, exist_ok=True)

log = []
def say(m):
    log.append(str(m)); print(m, flush=True)

from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication(sys.argv)

from fypa.cli import _load_solution_pickle
from fypa.altium_viewer import PdnViewer
from fypa.project_file import EditorDirective

solution, metadata = _load_solution_pickle(PKL)
say("loaded pickle: %d directives" % len(metadata.get("directives", [])))

viewer = PdnViewer(solution, metadata=metadata)
viewer.resize(1500, 950)
viewer.show()
for _ in range(40):
    app.processEvents()

results = []
def check(name, ok, detail=""):
    results.append((name, ok, detail))
    say(("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, detail)).rstrip())

# --- enter editor mode -------------------------------------------------
viewer._editor_toggle_btn.setChecked(True)
for _ in range(10):
    app.processEvents()
check("editor mode active", viewer._editor_mode)

# --- rail helpers -------------------------------------------------------
def rail_of(net):
    for r, members in viewer._rail_to_members.items():
        if net in members:
            return r
    return None

def p_net_of(d):
    t = (d.get("terminals") or {}).get("P")
    if not t:
        return None
    if t.get("requested_net"):
        return t["requested_net"]
    for p in t.get("pins", []) or []:
        if p.get("net"):
            return p["net"]
    return None

def n_net_of(d):
    t = (d.get("terminals") or {}).get("N")
    if not t:
        return None
    if t.get("requested_net"):
        return t["requested_net"]
    for p in t.get("pins", []) or []:
        if p.get("net"):
            return p["net"]
    return None

def pad_centroid(designator, net):
    """Centre of the named component's pad on ``net`` — matches the point
    _component_pad_points / _editor_marker_hover_rows resolve to."""
    import numpy as np
    prefix = f"{designator}-"
    for rec in metadata.get("pads", []):
        des = rec.get("designator") or ""
        if not des.startswith(prefix) or rec.get("net") != net:
            continue
        ring = rec.get("outline")
        if not ring:
            continue
        arr = np.asarray(ring, dtype=np.float64)
        return (0.5 * (arr[:, 0].min() + arr[:, 0].max()),
                0.5 * (arr[:, 1].min() + arr[:, 1].max()))
    return None

# --- pick a schematic SINK to unlock + override ------------------------
sink_d = next(d for d in metadata["directives"] if d.get("role") == "SINK")
sink_des = sink_d.get("designator")
sink_p = p_net_of(sink_d)
sink_n = n_net_of(sink_d)
old_current = float(sink_d.get("value"))
NEW_CURRENT = round(old_current + 1.5, 3)
say("chosen SINK %s: p_net=%s n_net=%s old=%g A -> new=%g A"
    % (sink_des, sink_p, sink_n, old_current, NEW_CURRENT))

px, py = pad_centroid(sink_des, sink_p)
say("hover point (P pad centroid) = (%.4f, %.4f)" % (px, py))

viewer._render()
for _ in range(8):
    app.processEvents()

# --- baseline: SOURCE rail-load current BEFORE the editor edit ---------
sink_rail = rail_of(sink_p)
source_d = None
for cand in metadata["directives"]:
    if cand.get("role") != "SOURCE":
        continue
    if rail_of(p_net_of(cand)) == sink_rail:
        source_d = cand
        break
src_before = (None if source_d is None
              else viewer._directive_current_for_hover(source_d))
say("baseline SOURCE %s rail-load current = %s A"
    % (source_d.get("designator") if source_d else "(none)", src_before))

# --- apply an editor override (what the Apply button does) -------------
d = EditorDirective()
d.kind = "component"
d.role = "SINK"
d.designator = sink_des
d.single_net = False
d.p_net = sink_p
d.n_net = sink_n
d.current = NEW_CURRENT
d.overrides_designator = sink_des
viewer._ensure_project().upsert_directive(d)
say("upserted editor override for %s" % sink_des)

viewer._render()
for _ in range(8):
    app.processEvents()

# --- THE BUG: hover after the edit, still in editor mode ---------------
row = viewer._pick_hovered_marker(px, py)
check("after edit: marker still hoverable", row is not None)
if row is not None:
    shown = row.get("directive_current_a")
    check("hover reports the NEW current, not the stale one",
          shown is not None and abs(shown - NEW_CURRENT) < 1e-6,
          "shows %g A (expected %g, stale was %g)"
          % (shown if shown is not None else -1, NEW_CURRENT, old_current))
    check("hover row is flagged pending", bool(row.get("pending")))
    text = viewer._format_marker_hover_text(row)
    say("    hover text: %r" % text)
    check("hover text carries the new value", ("%g" % NEW_CURRENT) in text
          or f"{NEW_CURRENT:.4g}" in text)
    check("hover text tagged as pending", "pending" in text.lower())
    tip = viewer._format_marker_tooltip_lines(row)
    check("tooltip lines tagged as pending",
          any("pending" in ln.lower() for ln in tip), " | ".join(tip))

# --- source current must track the changed sink load ------------------
# KCL: editing the sink swaps the schematic load out for the editor
# value, so the source rail-load current shifts by exactly the delta.
if source_d is None:
    check("found a SOURCE on the edited sink's rail", False,
          "no source shares rail %r" % sink_rail)
else:
    src_des = source_d.get("designator")
    src_after = viewer._directive_current_for_hover(source_d)
    delta = (None if (src_before is None or src_after is None)
             else src_after - src_before)
    check("SOURCE %s rail-load current tracks the edited sink" % src_des,
          delta is not None and abs(delta - (NEW_CURRENT - old_current))
          < 1e-6,
          "source %s A -> %s A (delta %s, expected %+g)"
          % (src_before, src_after, delta, NEW_CURRENT - old_current))

try:
    viewer.grab().save(os.path.join(OUT, "marker_hover.png"))
    say("saved marker_hover.png")
except Exception as e:
    say("grab failed: %r" % e)

# --- summary -----------------------------------------------------------
say("")
say("==== SUMMARY ====")
npass = sum(1 for _, ok, _ in results if ok)
for name, ok, _detail in results:
    say("%-4s %s" % ("PASS" if ok else "FAIL", name))
say("%d/%d checks passed" % (npass, len(results)))
sys.exit(0 if npass == len(results) else 1)
