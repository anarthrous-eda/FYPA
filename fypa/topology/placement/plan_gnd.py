"""GND column trunk bus planning."""

from __future__ import annotations

from collections import defaultdict

from fypa.topology.constants import GND_NET
from fypa.topology.placement.bus_grid import gnd_column_trunk_x
from fypa.topology.placement.plan_types import BusPlan
from fypa.topology.placement.ports import port_stub_x
from fypa.topology.types import TopologyPort


def plan_gnd_trunks(
    plan: BusPlan,
    gnd_ports: list[TopologyPort],
    gnd_bus_y: float,
) -> None:
    """Reserve GND column trunks and record their x positions on ``plan``."""
    reserved = plan.reserved_verticals
    groups: dict[float, list[TopologyPort]] = defaultdict(list)
    for port in gnd_ports:
        nominal = port_stub_x(port)
        groups[round(nominal, 1)].append(port)
    for group in groups.values():
        trunk_x = gnd_column_trunk_x(group)
        top_y = min(p.y for p in group)
        y_lo, y_hi = min(gnd_bus_y, top_y), max(gnd_bus_y, top_y)
        sides = {p.side for p in group}
        side = "left" if sides == {"left"} else "right" if sides == {"right"} else "mixed"
        key = (round(trunk_x, 1), side)
        plan.gnd_trunks[key] = trunk_x
        reserved.append((trunk_x, y_lo, y_hi, GND_NET))
