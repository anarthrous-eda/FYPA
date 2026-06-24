"""Benchmark + correctness-check the copper-fuse backends on a real board.

Runs the *actual* per-(layer, net) union path
(:func:`fypa.altium_geometry._parallel_union_buckets`) under each backend and
reports timing + per-bucket area agreement, so you can qualify the optional
Clipper2 backend (``fypa._clipper_fuse``) on your own boards before enabling it.

Usage::

    uv run python tools/bench_fuse.py path/to/Board.PrjPcb

The Clipper2 rows need the optional backend installed (``uv sync --extra
fast-fuse``); without it they are skipped with a note.

Backends compared:
  * shapely  — the default (threaded ``shapely.union_all``).
  * clipper  — in-process Clipper2 (run serially; it is GIL-bound).
  * verify   — runs both and keeps shapely wherever the areas disagree
               (this is the safe per-board qualification mode).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# Allow `python tools/bench_fuse.py` from anywhere — put the repo root (this
# file's parent's parent) on sys.path so `import fypa` resolves.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fypa.altium.extract import extract_project  # noqa: E402
from fypa import altium_geometry as ag  # noqa: E402
from fypa import _clipper_fuse  # noqa: E402


def _run(buckets, backend: str):
    os.environ["FYPA_FUSE_BACKEND"] = backend
    t = time.monotonic()
    res = ag._parallel_union_buckets(buckets)
    return res, time.monotonic() - t


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("prjpcb", type=Path, help="Altium .PrjPcb to fuse")
    ap.add_argument("--tol", type=float, default=1e-3,
                    help="per-bucket area mismatch tolerance, mm^2")
    args = ap.parse_args()

    proj = extract_project(args.prjpcb)
    enabled = proj.enabled_copper_layer_ids()
    buckets = ag._build_net_layer_buckets(proj, enabled, include_vias=True)
    n_prims = sum(len(v) for v in buckets.values())
    print(f"{args.prjpcb.name}: {len(buckets)} (layer,net) buckets, "
          f"{n_prims} primitives")

    res_sh, t_sh = _run(buckets, "shapely")
    print(f"  shapely (threaded)  {t_sh:7.2f}s")

    if not _clipper_fuse.clipper_available():
        print("  clipper             SKIPPED — pyclipr not installed "
              "(uv sync --extra fast-fuse)")
        return 0

    res_cl, t_cl = _run(buckets, "clipper")
    print(f"  clipper (serial)    {t_cl:7.2f}s   ({t_sh / t_cl:.2f}x)")

    # correctness: compare clipper vs shapely per bucket
    worst = 0.0
    mism = 0
    for k in buckets:
        d = abs(res_sh[k].area - res_cl[k].area)
        worst = max(worst, d)
        if d > args.tol:
            mism += 1
    tot_sh = sum(g.area for g in res_sh.values())
    tot_cl = sum(g.area for g in res_cl.values())
    print(f"  area: shapely={tot_sh:.3f}  clipper={tot_cl:.3f}  "
          f"(diff {abs(tot_sh - tot_cl):.4f} mm^2)")
    print(f"  per-bucket: worst diff {worst:.2e} mm^2, "
          f"buckets over tol({args.tol:g}) = {mism}/{len(buckets)}")
    if mism == 0:
        print("  => clipper matches shapely within tolerance — safe to enable "
              "(FYPA_FUSE_BACKEND=clipper).")
    else:
        print("  => some buckets differ; inspect before trusting 'clipper' "
              "(verify mode keeps shapely for those).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
