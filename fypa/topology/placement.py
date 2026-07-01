"""Port-based placement keys and deterministic bus planning (no routing)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from fypa.topology.constants import (
    COLUMN_BUS_PAD,
    GND_NET,
    MIN_PARALLEL_GAP,
    NODE_W,
    PORT_WIRE_STUB,
    PORT_WIRE_STUB_MIN,
    WIRE_EPS,
    WIRE_STAGGER,
)
from fypa.topology.types import TopologyPort

__all__ = [
    "BusPlan",
    "allocate_bus_x",
    "gnd_column_trunk_x",
    "gutter_bus_span_from_plan",
    "gutter_groups",
    "group_ports_by_net",
    "net_gutter_key",
    "plan_signal_buses",
    "port_stub_length",
    "port_stub_x",
    "ports_all_share_column",
    "port_column_x",
    "stacked_routing_order",
    "stacked_wire_bus_side",
    "wire_routing_key",
]


def port_stub_length(port: TopologyPort) -> float:
    """Horizontal stub length for this port (stacked edges use staggered lengths)."""
    if port.stub_length > WIRE_EPS:
        return port.stub_length
    return PORT_WIRE_STUB


def port_stub_x(port: TopologyPort) -> float:
    """Wire exit point offset outward from the node edge."""
    length = port_stub_length(port)
    if port.side == "right":
        return port.x + length
    return port.x - length


def port_column_x(port: TopologyPort) -> float:
    """Left edge x of the node column this port belongs to."""
    return port.x if port.side == "left" else port.x - NODE_W


def ports_share_column(a: TopologyPort, b: TopologyPort) -> bool:
    if a.node_id == b.node_id:
        return False
    return abs(port_column_x(a) - port_column_x(b)) < WIRE_EPS


def ports_all_share_column(ports: list[TopologyPort]) -> bool:
    if len(ports) < 2:
        return False
    col = round(port_column_x(ports[0]), 1)
    return all(abs(port_column_x(p) - col) < WIRE_EPS for p in ports[1:])


def wire_routing_key(a: TopologyPort, b: TopologyPort) -> tuple:
    if ports_share_column(a, b):
        return ("stack", *sorted((a.node_id, b.node_id)))
    x_lo = min(a.x, b.x)
    x_hi = max(a.x, b.x)
    return ("gap", round(x_lo, 1), round(x_hi, 1))


def net_gutter_key(ports: list[TopologyPort]) -> tuple | None:
    """Gutter span key for a signal net (``(x_lo, x_hi)`` rounded)."""
    if len(ports) < 2:
        return None
    if len(ports) == 2:
        key = wire_routing_key(ports[0], ports[1])
        if key[0] == "gap":
            return key[1:]
        return None
    if ports_all_share_column(ports):
        return None
    xs = [p.x for p in ports]
    return (round(min(xs), 1), round(max(xs), 1))


def gutter_groups(all_ports: list[TopologyPort]) -> dict[tuple, set[str]]:
    """Map each column-gap gutter span ``(x_lo, x_hi)`` to the nets sharing it."""
    by_net: dict[str, list[TopologyPort]] = defaultdict(list)
    for p in all_ports:
        if p.net:
            by_net[p.net].append(p)
    gutter_nets: dict[tuple, set[str]] = defaultdict(set)
    for net, group in by_net.items():
        if net == GND_NET or len(group) != 2:
            continue
        gkey = net_gutter_key(group)
        if gkey is not None:
            gutter_nets[gkey].add(net)
    return gutter_nets


def group_ports_by_net(ports: list[TopologyPort]) -> dict[str, list[TopologyPort]]:
    by_net: dict[str, list[TopologyPort]] = defaultdict(list)
    for p in ports:
        if p.net:
            by_net[p.net].append(p)
    return by_net


def stacked_routing_order(
    a: TopologyPort,
    b: TopologyPort,
) -> tuple[TopologyPort, TopologyPort]:
    if a.side == "right" and b.side == "left":
        return a, b
    if a.side == "left" and b.side == "right":
        return b, a
    if a.y <= b.y:
        return a, b
    return b, a


def stacked_wire_bus_side(ports: list[TopologyPort]) -> str:
    if any(p.side == "right" for p in ports):
        return "right"
    return "left"


def column_bus_x(
    col_x: float,
    side: str,
    *,
    lane: int,
    n_lanes: int,
) -> float:
    stagger = (lane - (n_lanes - 1) / 2) * max(WIRE_STAGGER, MIN_PARALLEL_GAP)
    if side == "right":
        return col_x + NODE_W + PORT_WIRE_STUB + COLUMN_BUS_PAD + stagger
    return col_x - PORT_WIRE_STUB - COLUMN_BUS_PAD - stagger


def gutter_bus_x_bounds(stubs: list[float]) -> tuple[float, float]:
    lo, hi = min(stubs), max(stubs)
    inner_lo = lo + MIN_PARALLEL_GAP
    inner_hi = hi - MIN_PARALLEL_GAP
    if inner_hi - inner_lo < MIN_PARALLEL_GAP - WIRE_EPS:
        mid = (lo + hi) / 2
        return mid - MIN_PARALLEL_GAP / 2, mid + MIN_PARALLEL_GAP / 2
    return inner_lo, inner_hi


def bus_outward(bus_x: float, x_lo: float, x_hi: float) -> float:
    mid = (x_lo + x_hi) / 2
    return 1.0 if bus_x >= mid else -1.0


def gnd_column_trunk_x(group: list[TopologyPort]) -> float:
    gnd_ports = [p for p in group if p.net == GND_NET]
    if gnd_ports:
        return port_stub_x(gnd_ports[0])
    stubs = [port_stub_x(p) for p in group]
    if all(p.side == "left" for p in group):
        return min(stubs)
    if all(p.side == "right" for p in group):
        return max(stubs)
    return sum(stubs) / len(stubs)


def _vertical_blocks_x(
    x: float,
    y_lo: float,
    y_hi: float,
    reserved: list[tuple[float, float, float, str]],
    net: str,
) -> bool:
    lo, hi = min(y_lo, y_hi), max(y_lo, y_hi)
    for vx, vy_lo, vy_hi, vnet in reserved:
        if vnet == net and abs(vx - x) < WIRE_EPS:
            continue
        if abs(vx - x) >= MIN_PARALLEL_GAP - WIRE_EPS:
            continue
        if hi > vy_lo + WIRE_EPS and lo < vy_hi + WIRE_EPS:
            return True
    return False


def _shift_for_blockers(
    x: float,
    y_lo: float,
    y_hi: float,
    reserved: list[tuple[float, float, float, str]],
    net: str,
    *,
    outward: float,
) -> float:
    lo, hi = min(y_lo, y_hi), max(y_lo, y_hi)
    for vx, vy_lo, vy_hi, vnet in reserved:
        if vnet == net and abs(vx - x) < WIRE_EPS:
            continue
        if vnet == GND_NET and abs(vx - x) < MIN_PARALLEL_GAP - WIRE_EPS:
            return vx + outward * MIN_PARALLEL_GAP if outward >= 0 else vx - MIN_PARALLEL_GAP
        if (abs(vx - x) < MIN_PARALLEL_GAP - WIRE_EPS
                and hi > vy_lo + WIRE_EPS
                and lo < vy_hi + WIRE_EPS):
            return vx + outward * MIN_PARALLEL_GAP if outward >= 0 else vx - MIN_PARALLEL_GAP
    return x


def allocate_bus_x(
    nominal: float,
    y_lo: float,
    y_hi: float,
    bus_lo: float,
    bus_hi: float,
    reserved_verticals: list[tuple[float, float, float, str]],
    net: str,
    *,
    outward: float,
    assigned_in_group: list[float] | None = None,
) -> float:
    """Pick the first valid bus x on the MIN_PARALLEL_GAP grid inside [bus_lo, bus_hi]."""
    assigned = assigned_in_group or []
    n_slots = max(
        int((bus_hi - bus_lo) / MIN_PARALLEL_GAP) + 1,
        len(assigned) + 1,
        8,
    )
    candidates: list[float] = [nominal]
    for k in range(n_slots + 1):
        candidates.append(bus_lo + k * MIN_PARALLEL_GAP)
    candidates.append((bus_lo + bus_hi) / 2)
    seen: set[float] = set()
    ordered: list[float] = []
    for c in candidates:
        r = round(c, 1)
        if r not in seen:
            seen.add(r)
            ordered.append(c)

    for candidate in ordered:
        x = max(bus_lo, min(bus_hi, candidate))
        for prev in assigned:
            if x < prev + MIN_PARALLEL_GAP - WIRE_EPS:
                x = prev + MIN_PARALLEL_GAP
        x = max(bus_lo, min(bus_hi, x))
        if _vertical_blocks_x(x, y_lo, y_hi, reserved_verticals, net):
            x = _shift_for_blockers(
                x, y_lo, y_hi, reserved_verticals, net, outward=outward,
            )
            x = max(bus_lo, min(bus_hi, x))
            for prev in assigned:
                if x < prev + MIN_PARALLEL_GAP - WIRE_EPS:
                    x = min(bus_hi, prev + MIN_PARALLEL_GAP)
        if not _vertical_blocks_x(x, y_lo, y_hi, reserved_verticals, net):
            return x
    return max(bus_lo, min(bus_hi, nominal))


@dataclass
class BusPlan:
    """Precomputed vertical bus positions for signal routing."""

    pair_buses: dict[str, float] = field(default_factory=dict)
    hub_buses: dict[str, float] = field(default_factory=dict)
    stack_buses: dict[tuple[float, str, str], float] = field(default_factory=dict)
    gnd_trunks: dict[tuple[float, str], float] = field(default_factory=dict)
    reserved_verticals: list[tuple[float, float, float, str]] = field(
        default_factory=list,
    )
    gutter_spans: dict[tuple[float, float], list[float]] = field(
        default_factory=dict,
    )


def plan_signal_buses(
    by_net: dict[str, list[TopologyPort]],
    *,
    gnd_ports: list[TopologyPort] | None = None,
    gnd_bus_y: float | None = None,
) -> BusPlan:
    """Deterministic bus-x plan mirroring ``build_signal_wires`` slot order."""
    plan = BusPlan()
    reserved = plan.reserved_verticals

    if gnd_ports and gnd_bus_y is not None:
        groups: dict[float, list[TopologyPort]] = defaultdict(list)
        for port in gnd_ports:
            nominal = port_stub_x(port)
            groups[round(nominal, 1)].append(port)
        for group in groups.values():
            trunk_x = gnd_column_trunk_x(group)
            top_y = min(p.y for p in group)
            y_lo, y_hi = min(gnd_bus_y, top_y), max(gnd_bus_y, top_y)
            key = (round(trunk_x, 1), "left" if group[0].side == "left" else "right")
            plan.gnd_trunks[key] = trunk_x
            reserved.append((trunk_x, y_lo, y_hi, GND_NET))

    two_port_pairs: list[tuple[TopologyPort, TopologyPort]] = []
    gutter_hub_nets: dict[tuple, list[tuple[str, list[TopologyPort]]]] = defaultdict(list)
    stack_hub_nets: dict[tuple, list[tuple[str, list[TopologyPort]]]] = defaultdict(list)

    for net, group in by_net.items():
        if net == GND_NET or len(group) < 2:
            continue
        ordered = sorted(group, key=lambda p: (p.x, p.y))
        if len(ordered) == 2:
            two_port_pairs.append((ordered[0], ordered[1]))
        elif ports_all_share_column(ordered):
            col = round(port_column_x(ordered[0]), 1)
            side = stacked_wire_bus_side(ordered)
            stack_hub_nets[(col, side)].append((net, ordered))
        else:
            gkey = net_gutter_key(ordered)
            if gkey is not None:
                gutter_hub_nets[gkey].append((net, ordered))

    for (col, side), net_groups in stack_hub_nets.items():
        net_groups.sort(key=lambda t: t[0])
        n_lanes = len(net_groups)
        for lane, (net, ports) in enumerate(net_groups):
            bus_x = column_bus_x(col, side, lane=lane, n_lanes=n_lanes)
            y_lo = min(p.y for p in ports)
            y_hi = max(p.y for p in ports)
            outward = 1.0 if side == "right" else -1.0
            bus_x = allocate_bus_x(
                bus_x, y_lo, y_hi, bus_x - MIN_PARALLEL_GAP, bus_x + MIN_PARALLEL_GAP,
                reserved, net, outward=outward,
            )
            plan.stack_buses[(col, side, net)] = bus_x
            plan.hub_buses[net] = bus_x
            reserved.append((bus_x, y_lo, y_hi, net))

    stacked_groups: dict[tuple[float, str], list[tuple[TopologyPort, TopologyPort]]] = (
        defaultdict(list)
    )
    gap_groups: dict[tuple, list[tuple[TopologyPort, TopologyPort]]] = defaultdict(list)
    for a, b in two_port_pairs:
        key = wire_routing_key(a, b)
        if key[0] == "stack":
            col = round(port_column_x(a), 1)
            side = stacked_wire_bus_side([a, b])
            stacked_groups[(col, side)].append((a, b))
        else:
            gap_groups[key].append((a, b))

    for (col, side), group in stacked_groups.items():
        group.sort(key=lambda ab: (ab[0].net, ab[0].y, ab[1].y))
        n_lanes = len(group)
        for lane, (a, b) in enumerate(group):
            net = a.net
            bus_x = column_bus_x(col, side, lane=lane, n_lanes=n_lanes)
            y_lo, y_hi = min(a.y, b.y), max(a.y, b.y)
            outward = 1.0 if side == "right" else -1.0
            bus_x = allocate_bus_x(
                bus_x, y_lo, y_hi, bus_x - MIN_PARALLEL_GAP, bus_x + MIN_PARALLEL_GAP,
                reserved, net, outward=outward,
            )
            plan.pair_buses[net] = bus_x
            reserved.append((bus_x, y_lo, y_hi, net))

    for key, group in gap_groups.items():
        group.sort(key=lambda ab: (min(ab[0].y, ab[1].y), ab[0].net))
        x_lo, x_hi = key[1], key[2]
        n_slots = len(group)
        all_stubs = [
            stub for a, b in group for stub in (port_stub_x(a), port_stub_x(b))
        ]
        channel_lo, channel_hi = gutter_bus_x_bounds(all_stubs)
        assigned_bus: list[float] = []
        gkey = (x_lo, x_hi)
        plan.gutter_spans.setdefault(gkey, [])
        for slot, (a, b) in enumerate(group):
            net = a.net
            bus_lo, bus_hi = channel_lo, channel_hi
            if n_slots > 1:
                span = bus_hi - bus_lo
                need = (n_slots - 1) * MIN_PARALLEL_GAP
                bus_x = bus_lo + slot * MIN_PARALLEL_GAP
                if span < need - WIRE_EPS:
                    bus_x = bus_lo + slot * (span / max(n_slots - 1, 1))
            else:
                bus_x = (bus_lo + bus_hi) / 2
            bus_x = min(bus_hi, max(bus_lo, bus_x))
            y_lo, y_hi = min(a.y, b.y), max(a.y, b.y)
            for prev in assigned_bus:
                if bus_x < prev + MIN_PARALLEL_GAP - WIRE_EPS:
                    bus_x = prev + MIN_PARALLEL_GAP
            bus_x = min(bus_hi, max(bus_lo, bus_x))
            bus_x = allocate_bus_x(
                bus_x, y_lo, y_hi, bus_lo, bus_hi, reserved, net,
                outward=bus_outward(bus_x, bus_lo, bus_hi),
                assigned_in_group=assigned_bus,
            )
            for prev in assigned_bus:
                if abs(bus_x - prev) < MIN_PARALLEL_GAP - WIRE_EPS:
                    bus_x = min(bus_hi, prev + MIN_PARALLEL_GAP)
            assigned_bus.append(bus_x)
            plan.pair_buses[net] = bus_x
            plan.gutter_spans[gkey].append(bus_x)
            reserved.append((bus_x, y_lo, y_hi, net))

    for gkey, net_groups in gutter_hub_nets.items():
        x_lo, x_hi = gkey
        net_groups.sort(key=lambda t: t[0])
        assigned_bus = sorted({
            bx for bx in plan.gutter_spans.get(gkey, [])
            if x_lo - WIRE_EPS <= bx <= x_hi + WIRE_EPS
        })
        plan.gutter_spans.setdefault(gkey, [])
        for net, ports in net_groups:
            stubs = [port_stub_x(p) for p in ports]
            bus_lo, bus_hi = gutter_bus_x_bounds(stubs)
            bus_x = (bus_lo + bus_hi) / 2
            for prev in assigned_bus:
                if abs(bus_x - prev) < MIN_PARALLEL_GAP - WIRE_EPS:
                    bus_x = prev + MIN_PARALLEL_GAP
            bus_x = min(bus_hi, max(bus_lo, bus_x))
            y_lo = min(p.y for p in ports)
            y_hi = max(p.y for p in ports)
            bus_x = allocate_bus_x(
                bus_x, y_lo, y_hi, bus_lo, bus_hi, reserved, net,
                outward=bus_outward(bus_x, bus_lo, bus_hi),
                assigned_in_group=assigned_bus,
            )
            for prev in assigned_bus:
                if abs(bus_x - prev) < MIN_PARALLEL_GAP - WIRE_EPS:
                    bus_x = min(bus_hi, prev + MIN_PARALLEL_GAP)
            assigned_bus.append(bus_x)
            plan.hub_buses[net] = bus_x
            plan.gutter_spans[gkey].append(bus_x)
            reserved.append((bus_x, y_lo, y_hi, net))

    return plan


_BUS_KINDS_SPAN = frozenset({"hub", "gutter", "stack_column"})


def gutter_bus_span_from_plan(
    plan: BusPlan,
    all_ports: list[TopologyPort],
) -> dict[tuple[float, float], tuple[float, float, int]]:
    """Per gutter span: ``(x_min, x_max, n_buses)`` from the bus plan."""
    by_net = group_ports_by_net(all_ports)
    gutter_nets = gutter_groups(all_ports)
    result: dict[tuple[float, float], tuple[float, float, int]] = {}

    bus_xs_by_gutter: dict[tuple[float, float], set[float]] = defaultdict(set)
    for gkey, xs in plan.gutter_spans.items():
        for x in xs:
            bus_xs_by_gutter[gkey].add(round(x, 1))
    for net, bus_x in plan.hub_buses.items():
        group = by_net.get(net, [])
        if len(group) < 3:
            continue
        gkey = net_gutter_key(group)
        if gkey is not None:
            bus_xs_by_gutter[gkey].add(round(bus_x, 1))
    for net, bus_x in plan.pair_buses.items():
        group = by_net.get(net, [])
        if len(group) == 2:
            gkey = net_gutter_key(group)
            if gkey is not None:
                bus_xs_by_gutter[gkey].add(round(bus_x, 1))

    for gkey in gutter_nets:
        xs = sorted(bus_xs_by_gutter.get(gkey, set()))
        if xs:
            result[gkey] = (xs[0], xs[-1], len(xs))
    return result
