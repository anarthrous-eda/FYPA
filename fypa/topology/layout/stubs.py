"""Port stub lengths and shared edge wire columns."""

from __future__ import annotations

from collections import defaultdict

from fypa.topology.constants import (
    GND_NET,
    GND_PORT_WIRE_STUB,
    PORT_WIRE_STUB,
    PORT_WIRE_STUB_MIN,
    WIRE_EPS,
)
from fypa.topology.placement import port_stub_length
from fypa.topology.terminal_roles import is_power_input_port
from fypa.topology.types import TopologyPort


def assign_stacked_stub_lengths(ports: list[TopologyPort]) -> None:
    """Stagger horizontal stubs: shortest at the bottom, longest at the top."""
    by_side: dict[str, list[TopologyPort]] = defaultdict(list)
    for port in ports:
        by_side[port.side].append(port)
    span = PORT_WIRE_STUB - PORT_WIRE_STUB_MIN
    for side, group in by_side.items():
        del side  # length rank depends on y only; side sets stub direction in port_stub_x
        for port in group:
            if port.net == GND_NET:
                port.stub_length = GND_PORT_WIRE_STUB
        signal_ports = [p for p in group if p.net != GND_NET]
        if len(signal_ports) < 2:
            for port in signal_ports:
                if port.stub_length < WIRE_EPS:
                    port.stub_length = PORT_WIRE_STUB
            continue
        ordered = sorted(signal_ports, key=lambda p: p.y)
        n = len(ordered)
        for i, port in enumerate(ordered):
            rank_from_bottom = (n - 1) - i
            port.stub_length = PORT_WIRE_STUB_MIN + rank_from_bottom / max(n - 1, 1) * span


def _port_vertical_bias(port: TopologyPort, role: str) -> str:
    """How a port's wire leaves the symbol edge: up, down, or horizontally."""
    if port.net == GND_NET:
        return "down"
    if role == "REGULATOR" and port.side == "left" and is_power_input_port(role, port.terminal):
        return "up"
    return "horizontal"


def _outer_wire_x(ports: list[TopologyPort], side: str) -> float:
    """Outermost routing column for ports sharing one edge."""
    px = ports[0].x
    reach = max(port_stub_length(p) for p in ports)
    if side == "left":
        return px - reach
    return px + reach


def _may_share_power_gnd_column(
    up: list[TopologyPort],
    down: list[TopologyPort],
    all_ports: list[TopologyPort],
) -> bool:
    """Share one wire column only when power feeds up and GND drops down without overlap."""
    power_top = min(p.y for p in up)
    gnd_top = min(p.y for p in down)
    if power_top >= gnd_top - WIRE_EPS:
        return False
    power_net = up[0].net
    net_ports = [p for p in all_ports if p.net == power_net]
    if not net_ports:
        return True
    min_y = min(p.y for p in net_ports)
    max_y = max(p.y for p in net_ports)
    if power_top > min_y + WIRE_EPS and max_y > gnd_top + WIRE_EPS:
        return True
    return False


def assign_edge_wire_columns(
    ports: list[TopologyPort],
    role: str,
    all_ports: list[TopologyPort],
) -> None:
    """Share one routing column when feeds split vertically without crossing."""
    by_side: dict[str, list[TopologyPort]] = defaultdict(list)
    for port in ports:
        by_side[port.side].append(port)
    for side, group in by_side.items():
        down = [p for p in group if _port_vertical_bias(p, role) == "down"]
        up = [p for p in group if _port_vertical_bias(p, role) == "up"]
        if not down or not up:
            continue
        if not _may_share_power_gnd_column(up, down, all_ports):
            continue
        column_x = _outer_wire_x(down + up, side)
        for port in down + up:
            port.wire_x = column_x
