#!/usr/bin/env python3
"""Debug column assignment from a solve pickle or metadata JSON."""
from __future__ import annotations

import json
import pickle
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fypa.topology import build_topology_model, topology_wiring_report
from fypa.topology.metadata.layout_bridge import parse_topology_directives


def load_metadata(path: Path) -> dict:
    if path.suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if "directives" in data:
            return data
        raise ValueError("JSON has no directives key")
    with path.open("rb") as f:
        data = pickle.load(f)
    if isinstance(data, dict) and "metadata" in data:
        return data["metadata"]
    if isinstance(data, dict) and "directives" in data:
        return data
    from fypa.cli import _load_solution_pickle
    _sol, meta = _load_solution_pickle(str(path), lean_ify=False)
    return meta


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: debug_topology_columns.py <metadata.json|solve.pkl>", file=sys.stderr)
        return 1
    path = Path(sys.argv[1])
    meta = load_metadata(path)
    parsed = parse_topology_directives(meta)
    print("columns:")
    for spec in sorted(parsed.node_specs, key=lambda s: parsed.columns.get(s["node_id"], 99)):
        nid = spec["node_id"]
        print(f"  {nid:8} col={parsed.columns.get(nid, '?'):3} role={spec['role']}")
    model = build_topology_model(meta)
    report = topology_wiring_report(model)
    print(f"\ncanvas: {report['canvas']['width']} x {report['canvas']['height']}")
    print(f"issues: {report['summary']['issues']}")
    for issue in report["issues"]:
        print(f"  [{issue['severity']}] {issue['code']}: {issue['message']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
