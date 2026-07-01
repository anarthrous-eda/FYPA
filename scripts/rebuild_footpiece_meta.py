"""Better smart_footpiece metadata from wiring.json ports."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fypa.rail_groups import compute_rail_groups
from fypa.topology import build_topology_model, topology_wiring_report
from fypa.topology.metadata.layout_bridge import parse_topology_directives

w = json.loads(Path("wiring.json").read_text(encoding="utf-8"))
ports = w["ports"]

ROLE_BY_NODE = {
    "J3": "SOURCE",
    "U1": "SINK",
    "U3": "REGULATOR",
    "U4": "REGULATOR",
    "J2.1": "SINK",
    "J2.2": "SINK",
    "L4": "RESISTOR",
    "L2": "RESISTOR",
    "L3": "RESISTOR",
    "U2": "SINK",
    "U5": "SINK",
    "U6": "SINK",
}

def make_directive(nid: str, role: str) -> dict:
    d: dict = {
        "role": role,
        "designator": nid.split(".")[0] if "." in nid else nid,
        "label": nid,
        "terminals": {},
    }
    if "." in nid:
        d["channel_index"] = int(nid.split(".")[1])
    return d

by_nid: dict[str, dict] = {}
for p in ports:
    nid = p["node_id"]
    role = ROLE_BY_NODE[nid]
    if nid not in by_nid:
        by_nid[nid] = make_directive(nid, role)
    d = by_nid[nid]
    t = p["terminal"]
    net = "GND" if p["net"] == "__GND__" else p["net"]
    d["terminals"][t] = {
        "requested_net": net,
        "pins": [{"net": net, "pad": "1"}],
    }

directives = list(by_nid.values())
meta = {"directives": directives, "net_canonical": {}, "annotation_errors": []}
Path("scripts/smart_footpiece_approx.json").write_text(
    json.dumps(meta, indent=2), encoding="utf-8",
)

_, rail_to_members = compute_rail_groups(meta)
print("rail groups:", {k: v for k, v in rail_to_members.items() if len(v) > 1})
parsed = parse_topology_directives(meta)
print("columns:")
for spec in sorted(parsed.node_specs, key=lambda s: parsed.columns.get(s["node_id"], 99)):
    nid = spec["node_id"]
    print(f"  {nid:8} col={parsed.columns.get(nid, '?')}")
model = build_topology_model(meta)
report = topology_wiring_report(model)
print("canvas", report["canvas"]["width"], "issues", report["summary"]["issues"])
for issue in report["issues"]:
    print(f"  {issue['code']}: {issue['message'][:100]}")
