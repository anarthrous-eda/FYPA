#!/usr/bin/env python3
"""Dump a topology wiring analysis report as JSON (for routing debug / LLM review).

Usage:
  uv run python tools/dump_topology_wiring.py path/to/solve.pkl
  uv run python tools/dump_topology_wiring.py path/to/solve.pkl -o wiring.json
  uv run python tools/dump_topology_wiring.py --stdin   # metadata pickle on stdin
"""

from __future__ import annotations

import argparse
import json
import pickle
import sys
from pathlib import Path

# Allow `python tools/dump_topology_wiring.py` from anywhere — repo root on path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fypa.topology import build_topology_model, topology_wiring_report  # noqa: E402


def _extract_metadata(data: object, path: str | None) -> dict | None:
    """Pull the metadata dict from any supported solve/pickle layout."""
    if isinstance(data, dict) and "metadata" in data:
        return data["metadata"]
    if isinstance(data, dict) and "directives" in data:
        return data
    if isinstance(data, dict) and not any(
        k in data for k in ("directives", "metadata", "solution")
    ):
        return None
    if isinstance(data, dict) and "solution" in data:
        # Wrapped solve output — needs the CLI loader, not the bare dict.
        data = None
    # Split-cache / solution-wrapped solve pickles: defer to the CLI loader,
    # which understands every on-disk solve format. Only metadata is needed.
    if path is not None:
        try:
            from fypa.cli import _load_solution_pickle

            _sol, meta = _load_solution_pickle(path, lean_ify=False)
            return meta
        except Exception as exc:  # noqa: BLE001
            print(f"CLI loader failed: {exc}", file=sys.stderr)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "pickle",
        nargs="?",
        help="Solve pickle containing metadata (uses embedded metadata dict)",
    )
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read pickle bytes from stdin instead of a file",
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        help="Write JSON to this file (default: stdout)",
    )
    parser.add_argument(
        "--issues-only",
        action="store_true",
        help="Print only summary and issues list",
    )
    args = parser.parse_args()

    if args.stdin:
        data = pickle.load(sys.stdin.buffer)
    elif args.pickle:
        with open(args.pickle, "rb") as f:
            data = pickle.load(f)
    else:
        parser.error("provide a pickle path or --stdin")

    metadata = _extract_metadata(data, args.pickle)
    if metadata is None:
        print(
            "Could not find a metadata dict in the pickle (tried dict, "
            "'metadata' key, and solve-cache formats).",
            file=sys.stderr,
        )
        return 1

    report = topology_wiring_report(build_topology_model(metadata))
    if args.issues_only:
        out = {"summary": report["summary"], "issues": report["issues"]}
    else:
        out = report

    text = json.dumps(out, indent=2)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
