"""Hub bus geometry, gutter slot order, and separation from assigned buses."""

from __future__ import annotations

from fypa.topology.constants import MIN_PARALLEL_GAP, WIRE_EPS
from fypa.topology.placement.ports import (
    bus_outward,
    gutter_bus_x_bounds,
    port_stub_x,
)
from fypa.topology.types import TopologyPort


def hub_bus_outward(
    ports: list[TopologyPort],
    bus_x: float,
    bus_lo: float,
    bus_hi: float,
) -> float:
    """Sink inputs keep hub trunks west of the stub column; sources keep them east."""
    anchor = hub_destination_anchor(ports)
    if anchor.side == "left":
        return -1.0
    if anchor.side == "right":
        return 1.0
    return bus_outward(bus_x, bus_lo, bus_hi)


def separate_from_assigned_buses(
    bus_x: float,
    assigned_bus: list[float],
    *,
    outward: float,
    bus_lo: float,
    bus_hi: float,
) -> float:
    """Keep ``bus_x`` at least MIN_PARALLEL_GAP from buses already in this gutter."""
    x = bus_x
    for prev in sorted(assigned_bus):
        if abs(x - prev) < MIN_PARALLEL_GAP - WIRE_EPS:
            x = prev - MIN_PARALLEL_GAP if outward < 0 else prev + MIN_PARALLEL_GAP
    return min(bus_hi, max(bus_lo, x))


def _connector_family(designator: str) -> str | None:
    """J2.1 / J2.2 → ``J2``; plain designators → ``None``."""
    if "." not in designator:
        return None
    return designator.rsplit(".", 1)[0]


def is_connector_hub_net(ports: list[TopologyPort]) -> bool:
    anchor = hub_destination_anchor(ports)
    return _connector_family(anchor.node_id) is not None


def hub_destination_anchor(ports: list[TopologyPort]) -> TopologyPort:
    """Rightmost column, topmost port — first downstream destination."""
    max_x = max(p.x for p in ports)
    dests = [p for p in ports if abs(p.x - max_x) < WIRE_EPS]
    return min(dests, key=lambda p: (p.y, p.terminal))


def hub_bus_nominal_x(
    ports: list[TopologyPort],
    bus_lo: float,
    bus_hi: float,
) -> float:
    """Hub trunk x at the destination stub (minimize incoming feed length)."""
    del bus_hi  # upper bound comes from ``hub_bus_channel_bounds``
    stub = port_stub_x(hub_destination_anchor(ports))
    return max(bus_lo, stub)


def hub_bus_channel_bounds(
    ports: list[TopologyPort],
) -> tuple[float, float]:
    """Gutter channel for a hub net, extended to the destination stub column."""
    stubs = [port_stub_x(p) for p in ports]
    lo, hi = gutter_bus_x_bounds(stubs)
    anchor_stub = port_stub_x(hub_destination_anchor(ports))
    return lo, max(hi, anchor_stub)


def sorted_gutter_hub_items(
    gutter_hub_nets: dict[
        tuple[float, float],
        list[tuple[str, list[TopologyPort]]],
    ],
) -> list[tuple[tuple[float, float], str, list[TopologyPort]]]:
    """Gutter hub nets in deterministic slot order (connector hubs first)."""
    items: list[tuple[tuple[float, float], str, list[TopologyPort]]] = []
    for gkey, net_groups in gutter_hub_nets.items():
        for net, ports in net_groups:
            items.append((gkey, net, ports))
    items.sort(
        key=lambda item: (
            0 if is_connector_hub_net(item[2]) else 1,
            item[0],
            port_stub_x(hub_destination_anchor(item[2])),
            item[1],
        ),
    )
    return items
