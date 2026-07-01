"""Facade between topology metadata and node layout."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from fypa.topology.constants import GND_NET, RETURN_PORT_SORT_BASE
from fypa.topology.metadata.nets import (
    canonical_net,
    is_ideal_return,
    net_to_rail_map,
    port_display_net,
    terminal_net,
    wire_net,
)
from fypa.topology.metadata.specs import (
    directives_to_component_specs,
    driven_power_nets,
    jump_row_for_directive,
    natural_sort_key,
)
from fypa.topology.metadata.tooltips import port_tooltip
from fypa.topology.metadata_schema import NodeSpec, TerminalDict, TopologyMetadata
from fypa.topology.terminal_roles import is_output_port
from fypa.topology.util import truncate_label


@dataclass(frozen=True)
class ResolvedPort:
    wnet: str
    plabel: str
    tooltip: str


@dataclass(frozen=True)
class ParsedLayoutInput:
    node_specs: list[NodeSpec]
    net_to_rail: dict[str, str]
    driven_nets: set[str]
    needs_gnd: bool
    columns: dict[str, int]


def _column_flow_net(term: TerminalDict | None) -> str | None:
    """Physical net for column placement (GND collapsed; no rail-group merge).

    Rail merging is for the solver/viewer dropdown — using it here creates
    feedback cycles when SERIES bridges join upstream and downstream nets
    (e.g. VDD_3V3 ↔ VDD_IMU) onto one canonical name.
    """
    if not term or is_ideal_return(term):
        return None
    return wire_net(terminal_net(term))


def _column_net(
    role: str,
    term: TerminalDict | None,
    net_to_rail: dict[str, str],
) -> str | None:
    """Net key for the column-placement graph.

    SERIES / RESISTOR ports use physical wire names so bridged downstream
    nets (VDD_IMU, LED_R, …) do not collapse onto the upstream rail and
    create propagation cycles.  Other roles keep rail-canonical names so
    parallel loads on a shared rail stay aligned.
    """
    if not term or is_ideal_return(term):
        return None
    if role in ("RESISTOR", "SERIES"):
        return _column_flow_net(term)
    return canonical_net(terminal_net(term), net_to_rail)


def assign_columns(
    node_specs: list[NodeSpec],
    net_to_rail: dict[str, str],
) -> dict[str, int]:
    """Place nodes in columns by propagating from SOURCE outputs along nets."""
    col: dict[str, int] = {}
    role_by_id = {s["node_id"]: s["role"] for s in node_specs}

    sources = [s for s in node_specs if s["role"] in ("SOURCE",)]
    for s in node_specs:
        if s["role"] == "REGULATOR" and not sources:
            sources.append(s)
    if not sources:
        sources = node_specs[:1] if node_specs else []

    for s in sources:
        col[s["node_id"]] = 0

    outputs_by_net: dict[str, list[str]] = defaultdict(list)
    inputs_by_net: dict[str, list[str]] = defaultdict(list)
    inputs_by_canonical: dict[str, list[str]] = defaultdict(list)
    for s in node_specs:
        nid = s["node_id"]
        for pname, side, _ in s["port_defs"]:
            term = (s["terms"] or {}).get(pname)
            if is_ideal_return(term):
                continue
            flow_net = _column_net(s["role"], term, net_to_rail)
            if not flow_net or flow_net == GND_NET:
                continue
            if is_output_port(s["role"], pname, side):
                outputs_by_net[flow_net].append(nid)
            else:
                inputs_by_net[flow_net].append(nid)
            if not is_output_port(s["role"], pname, side):
                cn = canonical_net(terminal_net(term), net_to_rail)
                if cn and cn != GND_NET:
                    inputs_by_canonical[cn].append(nid)

    changed = True
    guard = 0
    while changed and guard < len(node_specs) + 5:
        guard += 1
        changed = False
        for s in node_specs:
            nid = s["node_id"]
            base = col.get(nid, 0)
            for pname, side, _ in s["port_defs"]:
                if not is_output_port(s["role"], pname, side):
                    continue
                term = (s["terms"] or {}).get(pname)
                if is_ideal_return(term):
                    continue
                flow_net = _column_net(s["role"], term, net_to_rail)
                if not flow_net or flow_net == GND_NET:
                    continue
                for other in inputs_by_net.get(flow_net, []):
                    if other == nid:
                        continue
                    new_c = base + 1
                    if new_c > col.get(other, -1):
                        col[other] = new_c
                        changed = True

    for s in node_specs:
        nid = s["node_id"]
        if nid not in col:
            col[nid] = max(col.values(), default=0) + 1

    for s in node_specs:
        if s["role"] not in ("RESISTOR", "SERIES"):
            continue
        nid = s["node_id"]
        peer_cols: list[int] = []
        for pname, term in (s["terms"] or {}).items():
            if not term or is_ideal_return(term):
                continue
            flow_net = _column_flow_net(term)
            if not flow_net:
                continue
            for pid in outputs_by_net.get(flow_net, []):
                if pid != nid:
                    peer_cols.append(col.get(pid, 0))
            for pid in inputs_by_net.get(flow_net, []):
                if pid != nid:
                    peer_cols.append(col.get(pid, 0))
        if peer_cols:
            col[nid] = max(min(peer_cols), col.get(nid, 0))

    # Parallel taps on the P-side rail sit to the right of the bridge (not
    # downstream loads on the N-side nets).
    for s in node_specs:
        if s["role"] not in ("RESISTOR", "SERIES"):
            continue
        nid = s["node_id"]
        rcol = col.get(nid, 0)
        downstream: set[str] = set()
        for pname, term in (s["terms"] or {}).items():
            if not term or is_ideal_return(term) or not pname.startswith("N"):
                continue
            n_net = _column_flow_net(term)
            if not n_net:
                continue
            for other in inputs_by_net.get(n_net, []):
                if other != nid:
                    downstream.add(other)
        for pname, term in (s["terms"] or {}).items():
            if not term or is_ideal_return(term):
                continue
            if not pname.startswith("P"):
                continue
            rail = canonical_net(terminal_net(term), net_to_rail)
            if not rail or rail == GND_NET:
                continue
            for other in inputs_by_canonical.get(rail, []):
                if other == nid or other in downstream:
                    continue
                if role_by_id.get(other) in ("RESISTOR", "SERIES"):
                    continue
                if col.get(other, 0) <= rcol:
                    col[other] = max(col[other], rcol + 1)

    changed = True
    guard = 0
    while changed and guard < len(node_specs) + 5:
        guard += 1
        changed = False
        for s in node_specs:
            if s["role"] in ("RESISTOR", "SERIES"):
                continue
            nid = s["node_id"]
            base = col.get(nid, 0)
            for pname, side, _ in s["port_defs"]:
                if not is_output_port(s["role"], pname, side):
                    continue
                term = (s["terms"] or {}).get(pname)
                if is_ideal_return(term):
                    continue
                flow_net = _column_net(s["role"], term, net_to_rail)
                if not flow_net or flow_net == GND_NET:
                    continue
                for other in inputs_by_net.get(flow_net, []):
                    if other == nid:
                        continue
                    new_c = base + 1
                    if new_c > col.get(other, -1):
                        col[other] = new_c
                        changed = True

    if col:
        sink_col = max(col.values())
        for s in node_specs:
            if s["role"] == "SINK":
                col[s["node_id"]] = sink_col

    return col


def specs_by_column(
    node_specs: list[NodeSpec],
    columns: dict[str, int],
) -> tuple[dict[int, list[NodeSpec]], int]:
    """Group component specs by column index (insertion order within each column)."""
    by_col: dict[int, list[NodeSpec]] = defaultdict(list)
    for spec in node_specs:
        by_col[columns.get(spec["node_id"], 0)].append(spec)
    max_col = max(by_col.keys(), default=0)
    return by_col, max_col


def _enrich_resolved_ports(spec: NodeSpec, net_to_rail: dict[str, str]) -> None:
    resolved: dict[str, ResolvedPort] = {}
    port_directives = spec.get("port_directives") or {}
    terms = spec.get("terms") or {}
    for pname, _, _ in spec["port_defs"]:
        term = terms.get(pname)
        raw = terminal_net(term)
        cnet = canonical_net(raw, net_to_rail) or "?"
        wnet = wire_net(raw)
        if not wnet:
            continue
        plabel = truncate_label(port_display_net(term, cnet))
        resolved[pname] = ResolvedPort(
            wnet=wnet,
            plabel=plabel,
            tooltip=port_tooltip(plabel, port_directives.get(pname), pname),
        )
    spec["resolved_ports"] = resolved


def parse_topology_directives(metadata: TopologyMetadata) -> ParsedLayoutInput:
    """Parse metadata into layout-ready component specs and rail maps."""
    # Deferred: rail_groups imports topology.constants; eager import here
    # would cycle with metadata/__init__ → layout_bridge during package init.
    from fypa.rail_groups import compute_rail_groups

    _, rail_to_members = compute_rail_groups(metadata)
    net_to_rail = net_to_rail_map(rail_to_members)
    errors = list(metadata.get("annotation_errors") or [])
    directives = sorted(
        metadata.get("directives") or [],
        key=lambda d: natural_sort_key(str(d.get("designator") or d.get("label", ""))),
    )
    node_specs = directives_to_component_specs(directives, errors, net_to_rail)
    needs_gnd = False
    for spec in node_specs:
        _enrich_resolved_ports(spec, net_to_rail)
        for pname, _, _ in spec["port_defs"]:
            term = (spec["terms"] or {}).get(pname)
            if canonical_net(terminal_net(term), net_to_rail) == GND_NET:
                needs_gnd = True
    columns = assign_columns(node_specs, net_to_rail)
    return ParsedLayoutInput(
        node_specs=node_specs,
        net_to_rail=net_to_rail,
        driven_nets=driven_power_nets(node_specs, net_to_rail),
        needs_gnd=needs_gnd,
        columns=columns,
    )


def is_return_port_row(sort_key: int) -> bool:
    return sort_key >= RETURN_PORT_SORT_BASE


__all__ = [
    "ParsedLayoutInput",
    "ResolvedPort",
    "assign_columns",
    "is_return_port_row",
    "jump_row_for_directive",
    "parse_topology_directives",
    "specs_by_column",
]
