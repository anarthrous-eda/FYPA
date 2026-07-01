"""Wire endpoint connectivity validation."""

from __future__ import annotations

from collections import defaultdict

from fypa.topology.constants import GND_NET, WIRE_EPS
from fypa.topology.geometry import SchematicGeometry, parse_wire_path
from fypa.topology.issues import make_issue
from fypa.topology.types import TopologyModel


def check_dangling_wire_endpoints(
    model: TopologyModel,
    geo: SchematicGeometry,
) -> list[dict]:
    """Every wire path end must meet a port, GND symbol, or another wire segment."""
    issues: list[dict] = []
    junction_pts = {(round(x, 1), round(y, 1)) for x, y in geo.junctions}

    port_pts: set[tuple[float, float, str]] = set()
    for node in model.nodes:
        for port in node.ports:
            if port.net and port.net != "?":
                port_pts.add((round(port.x, 1), round(port.y, 1), port.net))

    gnd_symbol_pt: tuple[float, float] | None = None
    if model.gnd_symbol_x is not None and model.gnd_bus_y is not None:
        gnd_symbol_pt = (round(model.gnd_symbol_x, 1), round(model.gnd_bus_y, 1))

    endpoint_hits: dict[tuple[str, float, float], int] = defaultdict(int)
    for seg in geo.segments:
        for x, y in ((seg.x1, seg.y1), (seg.x2, seg.y2)):
            endpoint_hits[(seg.net, round(x, 1), round(y, 1))] += 1

    def _endpoint_connected(net: str, px: float, py: float) -> bool:
        pt = (round(px, 1), round(py, 1))
        if (pt[0], pt[1], net) in port_pts:
            return True
        if net == GND_NET and gnd_symbol_pt is not None:
            if (
                abs(pt[0] - gnd_symbol_pt[0]) < WIRE_EPS
                and abs(pt[1] - gnd_symbol_pt[1]) < WIRE_EPS
            ):
                return True
        if pt in junction_pts:
            return True
        if endpoint_hits[(net, pt[0], pt[1])] >= 2:
            return True
        return False

    for wi, wire in enumerate(model.wires):
        if wire.dashed or not wire.net:
            continue
        points = parse_wire_path(wire.path_d)
        if len(points) < 2:
            continue
        for end_name, (px, py) in (("start", points[0]), ("end", points[-1])):
            if _endpoint_connected(wire.net, px, py):
                continue
            issues.append(
                make_issue(
                    "dangling_wire_endpoint",
                    (
                        f"Wire {wi} ({wire.net}, {wire.routing_kind}) {end_name} "
                        f"at ({px:.1f},{py:.1f}) is not connected to a port, "
                        f"GND symbol, or another wire"
                    ),
                    wire_id=wi,
                    net=wire.net,
                    routing_kind=wire.routing_kind,
                    end=end_name,
                    x=round(px, 1),
                    y=round(py, 1),
                )
            )

    return issues
