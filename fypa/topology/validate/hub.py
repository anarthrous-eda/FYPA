"""Hub-specific topology validation."""

from __future__ import annotations

from fypa.topology.geometry import SchematicGeometry, point_on_segment
from fypa.topology.issues import make_issue
from fypa.topology.placement import port_stub_x
from fypa.topology.types import TopologyModel


def hub_routed_nets(model: TopologyModel) -> set[str]:
    """Nets that use hub routing (trunk, row bus, or row tap)."""
    return {wire.net for wire in model.wires if wire.net and wire.routing_kind.startswith("hub")}


def hub_net_port_count(model: TopologyModel, net: str) -> int:
    return sum(1 for node in model.nodes for port in node.ports if port.net == net)


def hub_net_ports_connected(
    model: TopologyModel,
    geo: SchematicGeometry,
    net: str,
) -> bool:
    """True when every port on *net* lies in one wire-graph component.

    Each port body is treated as connected to its stub column (``wire_x``), so
    wires that meet only the stub tip still count as reaching the port.
    """
    anchor_pts: list[tuple[float, float]] = []
    parent: dict[tuple[float, float], tuple[float, float]] = {}

    def find(pt: tuple[float, float]) -> tuple[float, float]:
        parent.setdefault(pt, pt)
        if parent[pt] != pt:
            parent[pt] = find(parent[pt])
        return parent[pt]

    def union(a: tuple[float, float], b: tuple[float, float]) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for node in model.nodes:
        for port in node.ports:
            if port.net != net:
                continue
            body = (round(port.x, 1), round(port.y, 1))
            stub = (round(port_stub_x(port), 1), round(port.y, 1))
            anchor_pts.append(body)
            union(body, stub)

    if len(anchor_pts) <= 1:
        return True

    for seg in geo.segments:
        if seg.net != net:
            continue
        a = (round(seg.x1, 1), round(seg.y1, 1))
        b = (round(seg.x2, 1), round(seg.y2, 1))
        union(a, b)

    for jx, jy in geo.junctions:
        on_net: list[tuple[float, float]] = []
        for seg in geo.segments:
            if seg.net != net:
                continue
            if point_on_segment(seg, jx, jy):
                on_net.append((round(seg.x1, 1), round(seg.y1, 1)))
                on_net.append((round(seg.x2, 1), round(seg.y2, 1)))
        if not on_net:
            continue
        hub = (round(jx, 1), round(jy, 1))
        for pt in on_net:
            union(hub, pt)

    roots = {find(pt) for pt in anchor_pts}
    return len(roots) == 1


def check_hub_net_disconnected(
    model: TopologyModel,
    geo: SchematicGeometry,
) -> list[dict]:
    """Flag hub-routed nets whose ports are not all in one wire-graph component.

    Applies to any net with ``hub`` / ``hub_row`` / ``hub_tap`` wires when two or
    more ports share the net (e.g. row-to-trunk feed failed or taps isolated).
    """
    issues: list[dict] = []
    for net in sorted(hub_routed_nets(model)):
        if hub_net_port_count(model, net) <= 1:
            continue
        if hub_net_ports_connected(model, geo, net):
            continue
        hub_wires = [
            wi
            for wi, wire in enumerate(model.wires)
            if wire.net == net and wire.routing_kind.startswith("hub")
        ]
        row_wires = [
            wi
            for wi, wire in enumerate(model.wires)
            if wire.net == net and wire.routing_kind == "hub_row"
        ]
        issues.append(
            make_issue(
                "hub_net_disconnected",
                (
                    f"Not all ports on {net!r} share one connected wire component "
                    f"on hub routing (hub wires {hub_wires}"
                    f"{f', row wires {row_wires}' if row_wires else ''})"
                ),
                net=net,
                wire_id=row_wires[0] if row_wires else (hub_wires[0] if hub_wires else None),
            )
        )
    return issues
