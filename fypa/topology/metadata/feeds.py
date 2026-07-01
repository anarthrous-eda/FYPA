"""External power feed stubs for topology wiring."""

from __future__ import annotations

from fypa.topology.constants import (
    EXTERNAL_STUB_END_INSET,
    EXTERNAL_STUB_EXTEND,
    EXTERNAL_STUB_LABEL_X_OFFSET,
    EXTERNAL_STUB_LABEL_Y_OFFSET,
    GND_NET,
    PORT_R,
    PORT_WIRE_STUB,
)
from fypa.topology.metadata.nets import canonical_net
from fypa.topology.types import TopologyPort, TopologyWire


def external_feed_wires(
    ports: list[TopologyPort],
    driven_nets: set[str],
    net_to_rail: dict[str, str],
) -> list[TopologyWire]:
    """Dashed stubs for power inputs with no upstream PDN driver on that net."""
    wires: list[TopologyWire] = []
    for p in ports:
        if not p.is_power_input or p.net == GND_NET:
            continue
        cnet = canonical_net(p.net, net_to_rail) or p.net
        if cnet in driven_nets:
            continue
        stub_x = p.x - PORT_WIRE_STUB - EXTERNAL_STUB_EXTEND
        wires.append(TopologyWire(
            net=p.net,
            path_d=(
                f"M {stub_x:.1f},{p.y:.1f} H {p.x - PORT_R - EXTERNAL_STUB_END_INSET:.1f}"
            ),
            label="extern",
            label_x=stub_x + EXTERNAL_STUB_LABEL_X_OFFSET,
            label_y=p.y + EXTERNAL_STUB_LABEL_Y_OFFSET,
            dashed=True,
            dst_node=p.node_id,
            dst_terminal=p.terminal,
            routing_kind="external_stub",
        ))
    return wires
