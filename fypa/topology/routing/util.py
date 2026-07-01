"""Routing helpers."""

from __future__ import annotations

from fypa.topology.constants import GND_NET
from fypa.topology.types import TopologyPort
from fypa.topology.util import truncate_label


def wire_display_label(ports: list[TopologyPort], net: str) -> str:
    if net == GND_NET:
        return "GND"
    for port in ports:
        if port.label and port.label != "?":
            return truncate_label(port.label)
    return truncate_label(net)
