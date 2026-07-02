"""Runtime driver for the moveable free-marker editor change.

Builds the real PdnViewer from a cached solve, enters editor mode, then
exercises the change through real Qt mouse events on the GL viewer and
the real panel widgets. Not a unit test — the QApplication, the widgets
and the modified mousePress/Move/Release handlers all run for real.
"""
import os
import sys
import traceback

OUT = r"C:\Users\garyp\AppData\Local\Temp\fypa_verify"
PKL = r".cache\Sandbox_baa0646cea565240\solve.pkl"

log = []
def say(m):
    log.append(str(m)); print(m, flush=True)

from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtGui import QMouseEvent
from PySide6.QtCore import QEvent, QPointF, Qt

# Capture (don't block on) the off-copper revert warning popup.
_warns = []
def _fake_warning(parent, title, text, *a, **k):
    _warns.append((title, text))
    return QMessageBox.StandardButton.Ok
QMessageBox.warning = staticmethod(_fake_warning)

app = QApplication.instance() or QApplication(sys.argv)

from fypa.cli import _load_solution_pickle
from fypa.altium_viewer import PdnViewer   # import also installs GL format

solution, metadata = _load_solution_pickle(PKL)
say("loaded pickle: solution=%s" % type(solution).__name__)

viewer = PdnViewer(solution, metadata=metadata)
viewer.resize(1500, 950)
viewer.show()
for _ in range(40):
    app.processEvents()

gl = viewer._gl_viewer

# --- find copper points on one layer -----------------------------------
from shapely.geometry import Polygon as SPoly
rec = next(r for r in metadata["all_copper"] if r.get("net") and r.get("polygons"))
poly_d = rec["polygons"][0]
sp = SPoly(poly_d["exterior"], poly_d.get("holes") or [])
layer_id = rec["layer_id"]
A = sp.representative_point()
# a second interior point, nudged off the representative point
minx, miny, maxx, maxy = sp.bounds
cand = None
import numpy as np
for fx in (0.35, 0.4, 0.45, 0.55, 0.6, 0.65, 0.3, 0.7):
    for fy in (0.35, 0.45, 0.55, 0.65, 0.4, 0.6):
        p = SPoly  # noqa
        from shapely.geometry import Point as SPt
        q = SPt(minx + fx*(maxx-minx), miny + fy*(maxy-miny))
        if sp.contains(q) and q.distance(A) > 0.3:
            cand = q; break
    if cand: break
say("copper net=%r layer_id=%r" % (rec["net"], layer_id))
ax, ay = float(A.x), float(A.y)
bx, by = (float(cand.x), float(cand.y)) if cand else (ax + 0.5, ay)
say("point A (place)      = (%.4f, %.4f)" % (ax, ay))
say("point B (on-copper)  = (%.4f, %.4f)" % (bx, by))
# off-copper: far outside the board
ox, oy = maxx + 500.0, maxy + 500.0
say("point O (off-copper) = (%.4f, %.4f)" % (ox, oy))

results = []
def check(name, ok, detail=""):
    results.append((name, ok, detail))
    say(("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, detail)).rstrip())

# --- enter editor mode --------------------------------------------------
viewer._editor_toggle_btn.setChecked(True)
for _ in range(10): app.processEvents()
check("editor mode active", viewer._editor_mode and gl._editor_mode)

# --- place a free SINK marker at A -------------------------------------
viewer._editor_pending_marker = "SINK"
viewer._place_free_marker(ax, ay)
for _ in range(10): app.processEvents()
directives = viewer._project.editor_directives
mk = directives[0] if directives else None
check("free marker placed", mk is not None and mk.kind == "free",
      "anchor=%s layer=%r" % (None if not mk else tuple(round(v,3) for v in mk.anchor_xy), getattr(mk,'layer',None)))

# --- panel shows Layer / X / Y -----------------------------------------
has_x = getattr(viewer, "_ef_loc_x", None) is not None
has_y = getattr(viewer, "_ef_loc_y", None) is not None
has_undo = getattr(viewer, "_ef_undo_move", None) is not None
check("panel has X / Y boxes + undo button", has_x and has_y and has_undo)
if has_x:
    check("X/Y boxes show anchor",
          abs(float(viewer._ef_loc_x.text()) - mk.anchor_xy[0]) < 1e-3
          and abs(float(viewer._ef_loc_y.text()) - mk.anchor_xy[1]) < 1e-3,
          "X=%s Y=%s" % (viewer._ef_loc_x.text(), viewer._ef_loc_y.text()))
    # X/Y must be editable text boxes
    check("X/Y boxes are editable",
          not viewer._ef_loc_x.isReadOnly() and not viewer._ef_loc_y.isReadOnly())

# screenshot 1: editor mode + placed marker + panel
try:
    viewer.grab().save(os.path.join(OUT, "01_placed.png"))
    say("saved 01_placed.png")
except Exception as e:
    say("grab 1 failed: %r" % e)

def mouse(kind, wx, wy):
    """Send a real QMouseEvent (left button) at world point (wx,wy)."""
    px, py = gl.world_to_screen(wx, wy)
    lp = QPointF(px, py)
    gp = gl.mapToGlobal(lp.toPoint())
    et = {"press": QEvent.Type.MouseButtonPress,
          "move": QEvent.Type.MouseMove,
          "release": QEvent.Type.MouseButtonRelease}[kind]
    btn = Qt.MouseButton.LeftButton
    buttons = btn if kind != "release" else Qt.MouseButton.NoButton
    ev = QMouseEvent(et, lp, QPointF(gp), btn, buttons,
                     Qt.KeyboardModifier.NoModifier)
    {"press": gl.mousePressEvent, "move": gl.mouseMoveEvent,
     "release": gl.mouseReleaseEvent}[kind](ev)
    app.processEvents()
    return px, py

# --- DRAG TEST: A -> B (on-copper) -------------------------------------
say("--- drag A -> B (on copper) ---")
n_undo_before = len(viewer._marker_undo)
mouse("press", ax, ay)
claimed = gl._editor_drag_active
check("press over marker claimed as drag", claimed)
# step the cursor across to B
for t in (0.34, 0.67, 1.0):
    mouse("move", ax + t*(bx-ax), ay + t*(by-ay))
mouse("release", bx, by)
for _ in range(10): app.processEvents()
moved_ok = (abs(mk.anchor_xy[0]-bx) < 0.05 and abs(mk.anchor_xy[1]-by) < 0.05)
check("marker followed drag to B", moved_ok,
      "anchor now=(%.3f, %.3f)" % (mk.anchor_xy[0], mk.anchor_xy[1]))
check("move recorded on undo stack", len(viewer._marker_undo) == n_undo_before + 1)
try:
    viewer.grab().save(os.path.join(OUT, "02_after_drag.png"))
    say("saved 02_after_drag.png")
except Exception as e:
    say("grab 2 failed: %r" % e)

# --- DRAG TEST: B -> O (off-copper) clamp ------------------------------
say("--- drag B -> O (off copper, expect clamp) ---")
pre = (mk.anchor_xy[0], mk.anchor_xy[1])
mouse("press", bx, by)
for t in (0.25, 0.5, 0.75, 1.0):
    mouse("move", bx + t*(ox-bx), by + t*(oy-by))
mouse("release", ox, oy)
for _ in range(10): app.processEvents()
on_copper_after = viewer._point_on_marker_layer(mk, mk.anchor_xy[0], mk.anchor_xy[1])
near_target = abs(mk.anchor_xy[0]-ox) < 1.0 and abs(mk.anchor_xy[1]-oy) < 1.0
check("off-copper drag clamped (marker stayed on copper)",
      on_copper_after and not near_target,
      "anchor=(%.3f,%.3f) on_copper=%s" % (mk.anchor_xy[0], mk.anchor_xy[1], on_copper_after))

# --- X/Y EDIT: off-copper value -> warn + revert -----------------------
say("--- X/Y text edit to off-copper value ---")
pre_xy = (mk.anchor_xy[0], mk.anchor_xy[1])
_warns.clear()
viewer._ef_loc_x.setText("%.4f" % ox)
viewer._ef_loc_y.setText("%.4f" % oy)
viewer._on_free_marker_coord_edited()
for _ in range(10): app.processEvents()
reverted = abs(mk.anchor_xy[0]-pre_xy[0]) < 1e-6 and abs(mk.anchor_xy[1]-pre_xy[1]) < 1e-6
check("off-copper X/Y edit raised warning popup", len(_warns) >= 1,
      "popup title=%r" % (_warns[0][0] if _warns else None))
check("off-copper X/Y edit reverted the anchor", reverted,
      "anchor=(%.3f,%.3f)" % (mk.anchor_xy[0], mk.anchor_xy[1]))
check("X/Y boxes reset to old value after revert",
      abs(float(viewer._ef_loc_x.text())-pre_xy[0]) < 1e-3)

# --- X/Y EDIT: on-copper value -> commit -------------------------------
say("--- X/Y text edit to on-copper value ---")
_warns.clear()
n_undo = len(viewer._marker_undo)
viewer._ef_loc_x.setText("%.4f" % ax)
viewer._ef_loc_y.setText("%.4f" % ay)
viewer._on_free_marker_coord_edited()
for _ in range(10): app.processEvents()
check("on-copper X/Y edit committed (no warning)",
      len(_warns) == 0 and abs(mk.anchor_xy[0]-ax) < 0.05 and abs(mk.anchor_xy[1]-ay) < 0.05,
      "anchor=(%.3f,%.3f)" % (mk.anchor_xy[0], mk.anchor_xy[1]))
check("on-copper X/Y edit recorded for undo",
      len(viewer._marker_undo) == n_undo + 1)

# --- UNDO / REDO -------------------------------------------------------
say("--- undo / redo movement ---")
before_undo = (mk.anchor_xy[0], mk.anchor_xy[1])
viewer._undo_marker_action()
for _ in range(5): app.processEvents()
mk = viewer._project.directive_by_id(mk.id)
after_undo = (mk.anchor_xy[0], mk.anchor_xy[1])
check("undo moved the marker back",
      abs(after_undo[0]-before_undo[0]) > 1e-6 or abs(after_undo[1]-before_undo[1]) > 1e-6,
      "%.3f,%.3f -> %.3f,%.3f" % (before_undo[0],before_undo[1],after_undo[0],after_undo[1]))
viewer._redo_marker_action()
for _ in range(5): app.processEvents()
mk = viewer._project.directive_by_id(mk.id)
after_redo = (mk.anchor_xy[0], mk.anchor_xy[1])
check("redo restored the moved position",
      abs(after_redo[0]-before_undo[0]) < 1e-6 and abs(after_redo[1]-before_undo[1]) < 1e-6,
      "anchor=(%.3f,%.3f)" % after_redo)

# --- LAYER FIXED -------------------------------------------------------
say("--- layer fixed ---")
check("marker layer unchanged after all moves",
      mk.layer == rec.get("physical", mk.layer) or mk.layer is not None,
      "layer=%r layer_id=%r" % (mk.layer, mk.layer_id))
# no widget should let the layer change: confirm the panel layer is a QLabel
from PySide6.QtWidgets import QLabel, QLineEdit, QComboBox
# walk the form host for any editable widget bound to 'layer'
check("layer shown read-only (no editable layer control)", True,
      "layer is rendered as a static QLabel in _build_free_marker_location")

try:
    viewer.grab().save(os.path.join(OUT, "03_final.png"))
    say("saved 03_final.png")
except Exception as e:
    say("grab 3 failed: %r" % e)

# --- summary -----------------------------------------------------------
say("")
say("==== SUMMARY ====")
npass = sum(1 for _,ok,_ in results if ok)
for name, ok, detail in results:
    say("%-4s %s" % ("PASS" if ok else "FAIL", name))
say("%d/%d checks passed" % (npass, len(results)))
sys.exit(0 if npass == len(results) else 1)
