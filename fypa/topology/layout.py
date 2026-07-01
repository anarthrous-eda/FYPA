"""Node column layout and port placement for the topology schematic."""

from __future__ import annotations

from collections import defaultdict

from fypa.topology.constants import (
    BODY_PAD,
    COL_GAP,
    GND_BUS_BELOW,
    GND_NET,
    HEADER_H,
    GND_PORT_WIRE_STUB,
    MARGIN,
    MIN_PARALLEL_GAP,
    NODE_W,
    PORT_ROW_H,
    PORT_WIRE_STUB,
    PORT_WIRE_STUB_MIN,
    ROW_GAP,
    WIRE_EPS,
    WIRE_GUTTER_PAD,
)
from fypa.topology.layout_result import LayoutResult
from fypa.topology.metadata.layout_bridge import (
    is_return_port_row,
    jump_row_for_directive,
    parse_topology_directives,
    specs_by_column,
)
from fypa.topology.metadata_schema import TopologyMetadata
from fypa.topology.placement import (
    BusPlan,
    gutter_bus_span_from_plan,
    gutter_groups,
    group_ports_by_net,
    plan_signal_buses,
)
from fypa.topology.terminal_roles import is_power_input_port
from fypa.topology.types import TopologyNode, TopologyPort


def _node_height(n_rows: int) -> float:
    return HEADER_H + BODY_PAD + max(n_rows, 1) * PORT_ROW_H + BODY_PAD


def _port_layout_rows(port_defs: list[tuple[str, str, int]]) -> tuple[int, dict[int, int]]:
    """Map sort_key to 0-based layout row; returns (n_rows, sort_key -> row)."""
    channel_rows = max(
        (sk for _, _, sk in port_defs if not is_return_port_row(sk)),
        default=-1,
    ) + 1
    return_ports = sorted(
        ((pname, side, sk) for pname, side, sk in port_defs if is_return_port_row(sk)),
        key=lambda t: t[2],
    )
    row_map: dict[int, int] = {}
    for ret_i, (_, _, sk) in enumerate(return_ports):
        row_map[sk] = channel_rows + ret_i
    n_rows = max(channel_rows + len(return_ports), 1)
    return n_rows, row_map


def _col_gap_for_gutter_slots(n_slots: int, *, gnd_reserve: bool = True) -> float:
    if n_slots <= 1:
        return COL_GAP
    gnd_pad = MIN_PARALLEL_GAP if gnd_reserve else 0.0
    needed_usable = (n_slots - 1) * MIN_PARALLEL_GAP + gnd_pad
    return max(
        COL_GAP,
        needed_usable
        + 2 * PORT_WIRE_STUB
        + MIN_PARALLEL_GAP
        + WIRE_GUTTER_PAD,
    )


def _col_gap_for_bus_span(x_min: float, x_max: float) -> float:
    needed = (x_max - x_min) + 2 * PORT_WIRE_STUB + MIN_PARALLEL_GAP + WIRE_GUTTER_PAD
    return max(COL_GAP, needed)


def _gutter_channel_count(nets_in_gutter: set[str], all_ports: list[TopologyPort]) -> int:
    count = 0
    for net in nets_in_gutter:
        group = [p for p in all_ports if p.net == net]
        if len(group) < 2:
            continue
        count += 1
    return max(count, 1)


def _column_left_edges(
    nodes: list[TopologyNode],
    max_col: int,
    *,
    x_offset: float = MARGIN,
    base_gap: float = COL_GAP,
) -> list[float]:
    unique = sorted({n.x for n in nodes})
    if not unique:
        return [x_offset + c * (NODE_W + base_gap) for c in range(max_col + 1)]
    while len(unique) < max_col + 1:
        unique.append(unique[-1] + NODE_W + base_gap)
    return unique[: max_col + 1]


def _column_x_positions(
    x_offset: float, gaps: list[float], max_col: int,
) -> list[float]:
    xs = [x_offset]
    for c in range(max_col):
        xs.append(xs[-1] + NODE_W + gaps[c])
    return xs


def _required_gaps(
    all_ports: list[TopologyPort],
    col_x: list[float],
    max_col: int,
    base_gap: float,
    bus_plan: BusPlan,
    *,
    gnd_bus_y: float | None = None,
) -> list[float]:
    """Per-gap widths: each gap only widens for gutters whose span crosses it."""
    del gnd_bus_y
    gaps = [base_gap] * max_col
    spans = gutter_bus_span_from_plan(bus_plan, all_ports)
    for (x_lo, x_hi), nets_in_gutter in gutter_groups(all_ports).items():
        req = _col_gap_for_gutter_slots(_gutter_channel_count(nets_in_gutter, all_ports))
        measured = spans.get((x_lo, x_hi))
        if measured is not None:
            x_min, x_max, n_buses = measured
            req = max(
                req,
                _col_gap_for_bus_span(x_min, x_max),
                _col_gap_for_gutter_slots(n_buses),
            )
        if req <= base_gap + WIRE_EPS:
            continue
        for g in range(max_col):
            gap_lo = col_x[g] + NODE_W
            gap_hi = col_x[g + 1]
            if x_hi > gap_lo + WIRE_EPS and x_lo < gap_hi - WIRE_EPS:
                gaps[g] = max(gaps[g], req)
    return gaps


def assign_stacked_stub_lengths(ports: list[TopologyPort]) -> None:
    by_side: dict[str, list[TopologyPort]] = defaultdict(list)
    for port in ports:
        by_side[port.side].append(port)
    span = PORT_WIRE_STUB - PORT_WIRE_STUB_MIN
    for side, group in by_side.items():
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
            t = i / max(n - 1, 1)
            if side == "right":
                port.stub_length = PORT_WIRE_STUB - t * span
            else:
                port.stub_length = PORT_WIRE_STUB_MIN + t * span


def _place_columns(
    node_specs: list[dict],
    by_col: dict[int, list[dict]],
    max_col: int,
    col_x: list[float],
) -> tuple[list[TopologyNode], list[TopologyPort]]:
    nodes: list[TopologyNode] = []
    all_ports: list[TopologyPort] = []
    for c in range(max_col + 1):
        col_nodes = by_col.get(c, [])
        y_cursor = float(MARGIN)
        for s in col_nodes:
            visible_ports = s["port_defs"]
            n_layout_rows, return_row_map = _port_layout_rows(visible_ports)
            nh = _node_height(n_layout_rows)
            nx = col_x[c]
            ny = y_cursor
            y_cursor += nh + ROW_GAP
            role = s["role"]
            node = TopologyNode(
                node_id=s["node_id"],
                label=s["label"],
                designator=s["designator"],
                role=role,
                x=nx,
                y=ny,
                width=NODE_W,
                height=nh,
                config_label="",
                has_error=s["has_error"],
                tooltip=s.get("tooltip", ""),
                bounds=(nx, ny, NODE_W, nh),
                jump_row=jump_row_for_directive(s["directive"]),
            )
            resolved_ports = s.get("resolved_ports") or {}
            sorted_ports = sorted(visible_ports, key=lambda t: t[2])
            for pname, side, sort_key in sorted_ports:
                resolved = resolved_ports.get(pname)
                if resolved is None:
                    continue
                row_i = (
                    sort_key if not is_return_port_row(sort_key)
                    else return_row_map[sort_key]
                )
                py = ny + HEADER_H + BODY_PAD + row_i * PORT_ROW_H + PORT_ROW_H / 2
                px = nx if side == "left" else nx + NODE_W
                port = TopologyPort(
                    terminal=pname,
                    net=resolved.wnet,
                    label=resolved.plabel,
                    side=side,
                    x=px,
                    y=py,
                    node_id=s["node_id"],
                    is_power_input=is_power_input_port(role, pname),
                    tooltip=resolved.tooltip,
                )
                node.ports.append(port)
                all_ports.append(port)
            assign_stacked_stub_lengths(node.ports)
            nodes.append(node)
    return nodes, all_ports


def place_nodes(
    node_specs: list[dict],
    by_col: dict[int, list[dict]],
    max_col: int,
    *,
    x_offset: float = MARGIN,
    col_gap: float = COL_GAP,
    gnd_bus_y: float | None = None,
) -> tuple[list[TopologyNode], list[TopologyPort], float, BusPlan, list[float]]:
    gaps = [col_gap] * max_col
    nodes: list[TopologyNode] = []
    all_ports: list[TopologyPort] = []
    slot_plan = BusPlan()

    for _ in range(max_col + 2):
        col_x = _column_x_positions(x_offset, gaps, max_col)
        nodes, all_ports = _place_columns(
            node_specs, by_col, max_col, col_x,
        )
        new_gaps = _required_gaps(
            all_ports, col_x, max_col, base_gap=col_gap, bus_plan=slot_plan,
            gnd_bus_y=gnd_bus_y,
        )
        if all(new_gaps[g] <= gaps[g] + WIRE_EPS for g in range(max_col)):
            break
        gaps = [max(gaps[g], new_gaps[g]) for g in range(max_col)]

    col_x = _column_x_positions(x_offset, gaps, max_col)
    by_net = group_ports_by_net(all_ports)
    gnd_ports = by_net.get(GND_NET, [])
    bus_plan = plan_signal_buses(
        by_net,
        gnd_ports=gnd_ports if gnd_bus_y is not None else None,
        gnd_bus_y=gnd_bus_y,
    )
    refined_gaps = _required_gaps(
        all_ports, col_x, max_col, base_gap=col_gap, bus_plan=bus_plan,
        gnd_bus_y=gnd_bus_y,
    )
    if any(refined_gaps[g] > gaps[g] + WIRE_EPS for g in range(max_col)):
        gaps = [max(gaps[g], refined_gaps[g]) for g in range(max_col)]
        col_x = _column_x_positions(x_offset, gaps, max_col)
        nodes, all_ports = _place_columns(
            node_specs, by_col, max_col, col_x,
        )
        by_net = group_ports_by_net(all_ports)
        gnd_ports = by_net.get(GND_NET, [])
        bus_plan = plan_signal_buses(
            by_net,
            gnd_ports=gnd_ports if gnd_bus_y is not None else None,
            gnd_bus_y=gnd_bus_y,
        )

    content_right = max((n.x + n.width for n in nodes), default=x_offset)
    return nodes, all_ports, content_right, bus_plan, gaps


def refine_place_nodes_for_gnd(
    node_specs: list[dict],
    by_col: dict[int, list[dict]],
    max_col: int,
    gaps: list[float],
    *,
    gnd_bus_y: float,
    x_offset: float = MARGIN,
    col_gap: float = COL_GAP,
) -> tuple[list[TopologyNode], list[TopologyPort], float, BusPlan, list[float]]:
    """Re-plan signal buses with GND trunks; re-place only if gaps must widen."""
    col_x = _column_x_positions(x_offset, gaps, max_col)
    nodes, all_ports = _place_columns(node_specs, by_col, max_col, col_x)
    by_net = group_ports_by_net(all_ports)
    gnd_ports = by_net.get(GND_NET, [])
    bus_plan = plan_signal_buses(
        by_net,
        gnd_ports=gnd_ports,
        gnd_bus_y=gnd_bus_y,
    )
    refined_gaps = _required_gaps(
        all_ports, col_x, max_col, base_gap=col_gap, bus_plan=bus_plan,
        gnd_bus_y=gnd_bus_y,
    )
    if any(refined_gaps[g] > gaps[g] + WIRE_EPS for g in range(max_col)):
        gaps = [max(gaps[g], refined_gaps[g]) for g in range(max_col)]
        col_x = _column_x_positions(x_offset, gaps, max_col)
        nodes, all_ports = _place_columns(node_specs, by_col, max_col, col_x)
        by_net = group_ports_by_net(all_ports)
        gnd_ports = by_net.get(GND_NET, [])
        bus_plan = plan_signal_buses(
            by_net,
            gnd_ports=gnd_ports,
            gnd_bus_y=gnd_bus_y,
        )
    content_right = max((n.x + n.width for n in nodes), default=x_offset)
    return nodes, all_ports, content_right, bus_plan, gaps


def build_node_layout(
    metadata: TopologyMetadata | None,
) -> LayoutResult:
    """Parse metadata and place nodes; returns layout state for wire routing."""
    empty = LayoutResult(
        nodes=[],
        ports=[],
        content_right=MARGIN,
        max_col=0,
        needs_gnd=False,
        gnd_bus_y=None,
        directive_nodes=[],
        node_specs=[],
        net_to_rail={},
        driven_nets=set(),
        bus_plan=BusPlan(),
    )
    if metadata is None:
        return empty

    parsed = parse_topology_directives(metadata)
    by_col, max_col = specs_by_column(parsed.node_specs, parsed.columns)
    nodes, all_ports, content_right, bus_plan, gaps = place_nodes(
        parsed.node_specs,
        by_col=by_col,
        max_col=max_col,
    )

    directive_nodes = [n for n in nodes if n.role != "GND"]
    directive_bottom = (
        max((n.y + n.height for n in directive_nodes), default=MARGIN)
    )
    gnd_bus_y = directive_bottom + GND_BUS_BELOW if parsed.needs_gnd else None

    if parsed.needs_gnd and gnd_bus_y is not None:
        nodes, all_ports, content_right, bus_plan, gaps = refine_place_nodes_for_gnd(
            parsed.node_specs,
            by_col=by_col,
            max_col=max_col,
            gaps=gaps,
            gnd_bus_y=gnd_bus_y,
        )
        directive_nodes = [n for n in nodes if n.role != "GND"]
        directive_bottom = (
            max((n.y + n.height for n in directive_nodes), default=MARGIN)
        )
        gnd_bus_y = directive_bottom + GND_BUS_BELOW

    return LayoutResult(
        nodes=nodes,
        ports=all_ports,
        content_right=content_right,
        max_col=max_col,
        needs_gnd=parsed.needs_gnd,
        gnd_bus_y=gnd_bus_y,
        directive_nodes=directive_nodes,
        node_specs=parsed.node_specs,
        net_to_rail=parsed.net_to_rail,
        driven_nets=parsed.driven_nets,
        bus_plan=bus_plan,
    )
