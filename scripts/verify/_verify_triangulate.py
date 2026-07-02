"""Verify the solid-fill 'show all copper' crash fix.

Root cause: the solid-fill path triangulates each copper polygon with
Shewchuk's Triangle (the C core behind the ``triangle`` package). Triangle
segfaults — silently, no traceback, killing the whole GUI — the instant
its vertex list contains a duplicate or non-finite coordinate. Real-board
copper pours are stored as float32 in ``all_copper`` records, which rounds
genuinely-distinct pour vertices onto the exact same coordinate. The
wire-mesh path never calls Triangle, which is why only solid mode crashed.

Run:  python _verify_triangulate.py
"""
import subprocess
import sys

import numpy as np


# --- 1. Confirm the raw, unrepaired Triangle call segfaults ------------
# Two identical, non-adjacent vertices in the input -> Windows 0xC0000005
# ACCESS_VIOLATION (returncode 3221225477), zero stdout, zero stderr.
RAW_CRASH_PROBE = r"""
import triangle as t
verts = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)]
segs = [(0, 1), (1, 2), (2, 3), (3, 4), (4, 0)]
t.triangulate({"vertices": verts, "segments": segs}, "pQ")
print("raw-ok")
"""


def probe_raw_crash() -> str:
    r = subprocess.run([sys.executable, "-c", RAW_CRASH_PROBE],
                        capture_output=True, text=True)
    if r.returncode == 0 and "raw-ok" in r.stdout:
        return "survived (this build of Triangle tolerates it)"
    return (f"CRASHED returncode={r.returncode} "
            f"(0x{r.returncode & 0xFFFFFFFF:08X}) "
            f"stdout={r.stdout.strip()!r} stderr={r.stderr.strip()!r}")


# --- 2. Drive the real PdnViewer triangulation methods -----------------
def make_viewer_stub():
    from fypa.altium_viewer import PdnViewer

    class _Stub:
        pass

    s = _Stub()
    s._poly_to_triangle_input = PdnViewer._poly_to_triangle_input
    s._triangulate_simple_polygon = (
        PdnViewer._triangulate_simple_polygon.__get__(s))
    s._triangulate_stub = PdnViewer._triangulate_stub.__get__(s)
    return s


def main() -> int:
    print("[1] raw Triangle, duplicate non-adjacent vertex:")
    print("    ", probe_raw_crash())

    s = make_viewer_stub()
    ok = True

    def check(label, arr, *, allow_empty=False):
        nonlocal ok
        good = arr.shape[0] % 3 == 0 and (allow_empty or arr.shape[0] >= 3)
        ok &= good
        print(f"    {label:34s} -> {arr.shape[0] // 3:5d} tris "
              f"{'OK' if good else 'FAIL'}")

    print("[2] PdnViewer._triangulate_stub on hostile geometry "
          "(no crash == fix works):")

    # A: clean square pour.
    check("clean square", s._triangulate_stub({
        "exterior": np.array([[0, 0], [20, 0], [20, 20], [0, 20]],
                             dtype=np.float32), "holes": []}))

    # B: ring carrying the exterior's closing duplicate vertex, exactly as
    #    altium_loader._build_all_copper_records stores it.
    check("ring with closing duplicate", s._triangulate_stub({
        "exterior": np.array([[0, 0], [20, 0], [20, 20], [0, 20], [0, 0]],
                             dtype=np.float32), "holes": []}))

    # C: two NON-ADJACENT vertices collapsed onto one coordinate — the
    #    float32-rounding case that segfaults raw Triangle (probe [1]).
    check("float32-collided vertices", s._triangulate_stub({
        "exterior": np.array([[0, 0], [20, 0], [20, 20], [0, 20], [20, 20]],
                             dtype=np.float32), "holes": []}))

    # D: self-touching (figure-8) ring -> invalid; buffer(0) repairs it.
    check("self-touching bowtie ring", s._triangulate_stub({
        "exterior": np.array([[0, 0], [10, 10], [0, 10], [10, 0]],
                             dtype=np.float32), "holes": []}),
          allow_empty=True)

    # E: pour with a thermal-relief-style hole.
    check("pour with one hole", s._triangulate_stub({
        "exterior": np.array([[0, 0], [30, 0], [30, 30], [0, 30]],
                             dtype=np.float32),
        "holes": [np.array([[10, 10], [20, 10], [20, 20], [10, 20]],
                            dtype=np.float32)]}))

    # F: a NaN coordinate -> would segfault Triangle; must degrade to empty.
    check("NaN vertex (must not crash)", s._triangulate_stub({
        "exterior": np.array([[0, 0], [20, 0], [np.nan, 20], [0, 20]],
                             dtype=np.float32), "holes": []}),
          allow_empty=True)

    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
