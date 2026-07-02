"""Wire routing for the PDN topology schematic."""

from __future__ import annotations

from fypa.topology.constants import MIN_PARALLEL_GAP
from fypa.topology.placement import port_stub_length, port_stub_x
from fypa.topology.routing.build import build_signal_wires, build_wires
from fypa.topology.routing.context import RoutingContext
from fypa.topology.routing.hub import route_hub

__all__ = [
    "MIN_PARALLEL_GAP",
    "RoutingContext",
    "build_signal_wires",
    "build_wires",
    "port_stub_length",
    "port_stub_x",
    "route_hub",
]
