"""Routing regressions: multi-row hub connectivity and degenerate pairs.

These exercise wire-level routing paths that the SVG snapshot fixtures do not
cover (no committed fixture produces a multi-row hub whose trunk sits beside
its rows, nor two coincident ports on one net).
"""

from __future__ import annotations

from fypa.topology.geometry import parse_wire_path
from fypa.topology.routing.context import RoutingContext
from fypa.topology.routing.hub import _route_hub_tap, hub_row_edge_x, route_hub
from fypa.topology.routing.paths import hub_row_stub_columns
from fypa.topology.routing.paths import two_port_path
from fypa.topology.types import TopologyPort


def _port(node_id: str, y: float, wire_x: float) -> TopologyPort:
    """A right-side hub port whose stub column is pinned at ``wire_x``."""
    return TopologyPort(
        terminal="P",
        net="VDD",
        label="VDD",
        side="right",
        x=0.0,
        y=y,
        node_id=node_id,
        wire_x=wire_x,
    )


def _max_x(path_d: str) -> float:
    return max(x for x, _y in parse_wire_path(path_d))


def _xs_monotonic(path_d: str) -> bool:
    xs = [x for x, _y in parse_wire_path(path_d)]
    non_decr = all(b >= a - 1e-6 for a, b in zip(xs, xs[1:]))
    non_incr = all(b <= a + 1e-6 for a, b in zip(xs, xs[1:]))
    return non_decr or non_incr


def _row_ports_for_wire(
    ports: list[TopologyPort],
    row_wire,
) -> list[TopologyPort]:
    """Ports that define stub columns for a ``hub_row`` wire."""
    ids = {row_wire.src_node, row_wire.dst_node}
    matched = [p for p in ports if p.node_id in ids]
    if len(matched) >= 2:
        return matched
    row_y = parse_wire_path(row_wire.path_d)[0][1]
    return [p for p in ports if abs(p.y - row_y) < 1e-6]


def _row_feed_reaches_trunk(
    wires: list,
    row_wire,
    bus_x: float,
    *,
    row_ports: list[TopologyPort],
) -> bool:
    """Row path or a row-edge hub tap feed reaches the trunk column."""
    if _max_x(row_wire.path_d) >= bus_x - 1e-6:
        return True
    row_y = parse_wire_path(row_wire.path_d)[0][1]
    row_lo, row_hi = hub_row_stub_columns(row_ports)
    edge_x = hub_row_edge_x(row_lo, row_hi, bus_x)
    for wire in wires:
        if wire.routing_kind != "hub_tap" or wire.net != row_wire.net:
            continue
        wpts = parse_wire_path(wire.path_d)
        if len(wpts) < 2:
            continue
        if abs(wpts[0][1] - row_y) > 1e-6:
            continue
        if abs(wpts[0][0] - edge_x) > 1e-6:
            continue
        if _max_x(wire.path_d) >= bus_x - 1e-6:
            return True
    return False


def test_every_hub_row_reaches_trunk_when_bus_sits_beside_rows():
    """Each row bus must reach the trunk column, not just the last one.

    Regression for the edge-tap extending ``row_wires[-1]`` (always the last
    appended row) instead of the row currently being processed, which left
    earlier rows electrically orphaned from the trunk.
    """
    bus_x = 200.0
    ports = [
        _port("A", y=100.0, wire_x=50.0),
        _port("B", y=100.0, wire_x=100.0),
        _port("C", y=200.0, wire_x=60.0),
        _port("D", y=200.0, wire_x=120.0),
    ]
    wires = route_hub("VDD", ports, bus_x, obstacles=[], ctx=RoutingContext())

    row_wires = [w for w in wires if w.routing_kind == "hub_row"]
    assert len(row_wires) == 2, "expected one bus per row"
    for w in row_wires:
        assert _row_feed_reaches_trunk(
            wires,
            w,
            bus_x,
            row_ports=_row_ports_for_wire(ports, w),
        ), (
            f"row bus {w.path_d!r} stops before the trunk at x={bus_x}"
        )


def test_two_port_path_uses_stub_when_port_to_bus_horizontal_blocked():
    """Blocked port→bus horizontals must not cut through foreign symbol bodies."""
    from fypa.topology.constants import NODE_W
    from fypa.topology.routing.obstacles import horizontal_segment_clear
    from fypa.topology.types import TopologyNode

    blocker = TopologyNode(
        node_id="U2",
        label="U2",
        designator="U2",
        role="SINK",
        x=150.0,
        y=80.0,
        width=NODE_W,
        height=80.0,
        config_label="",
        has_error=False,
        bounds=(150.0, 80.0, NODE_W, 80.0),
        ports=[],
    )
    start = TopologyPort(
        terminal="P",
        net="SIG",
        label="SIG",
        side="right",
        x=100.0 + NODE_W,
        y=100.0,
        node_id="U1",
        wire_x=180.0,
    )
    end = TopologyPort(
        terminal="P",
        net="SIG",
        label="SIG",
        side="left",
        x=400.0,
        y=200.0,
        node_id="U3",
        wire_x=380.0,
    )
    bus_x = 220.0
    path = two_port_path(
        start,
        end,
        bus_x=bus_x,
        net="SIG",
        obstacles=[blocker],
        ctx=RoutingContext(),
    )
    pts = parse_wire_path(path)
    assert pts[0] == (start.x, start.y)
    assert not horizontal_segment_clear(
        start.y,
        min(start.x, bus_x),
        max(start.x, bus_x),
        [blocker],
        {start.node_id, end.node_id},
    )
    assert pts[1][0] == 180.0, f"expected stub escape before bus column, got {path!r}"


def test_two_port_path_reserves_actual_vertical_column():
    """Vertical reservations must match the column the path actually uses."""
    from fypa.topology.constants import NODE_W
    from fypa.topology.routing.obstacles import horizontal_segment_clear
    from fypa.topology.types import TopologyNode

    blocker = TopologyNode(
        node_id="U2",
        label="U2",
        designator="U2",
        role="SINK",
        x=150.0,
        y=80.0,
        width=NODE_W,
        height=80.0,
        config_label="",
        has_error=False,
        bounds=(150.0, 80.0, NODE_W, 80.0),
        ports=[],
    )
    start = TopologyPort(
        terminal="P",
        net="SIG",
        label="SIG",
        side="right",
        x=100.0 + NODE_W,
        y=100.0,
        node_id="U1",
        wire_x=180.0,
    )
    end = TopologyPort(
        terminal="P",
        net="SIG",
        label="SIG",
        side="left",
        x=400.0,
        y=200.0,
        node_id="U3",
        wire_x=380.0,
    )
    bus_x = 220.0
    ctx = RoutingContext()
    path = two_port_path(
        start,
        end,
        bus_x=bus_x,
        net="SIG",
        obstacles=[blocker],
        ctx=ctx,
    )
    from fypa.topology.geometry import path_to_segments

    vertical_xs = {
        round(seg.x1, 1)
        for seg in path_to_segments("SIG", parse_wire_path(path))
        if seg.orient == "V"
    }
    reserved_xs = {round(vx, 1) for vx, _lo, _hi, _net in ctx.vertical_bands}
    assert vertical_xs <= reserved_xs, (
        f"path verticals {vertical_xs} not covered by reservations {reserved_xs}: {path!r}"
    )
    assert not horizontal_segment_clear(
        start.y,
        min(start.x, bus_x),
        max(start.x, bus_x),
        [blocker],
        {start.node_id, end.node_id},
    )
    assert 180.0 in vertical_xs or 180.0 in reserved_xs


def test_two_port_same_row_detour_returns_to_destination_port():
    """Regression: when both ports share a row but an obstacle forces a detour,
    the wire must drop back to the port row and end on the destination port
    (previously it terminated on the detour row, leaving the port open)."""
    from fypa.topology.constants import NODE_W
    from fypa.topology.types import TopologyNode

    # Obstacle body straddles the shared row (y=100) between the two stub columns.
    blocker = TopologyNode(
        node_id="OBS",
        label="OBS",
        designator="OBS",
        role="SINK",
        x=200.0,
        y=80.0,
        width=NODE_W,
        height=40.0,
        config_label="",
        has_error=False,
        bounds=(200.0, 80.0, NODE_W, 40.0),
        ports=[],
    )
    start = TopologyPort(
        terminal="P", net="SIG", label="SIG", side="right",
        x=100.0, y=100.0, node_id="U1",
    )
    end = TopologyPort(
        terminal="P", net="SIG", label="SIG", side="left",
        x=400.0, y=100.0, node_id="U3",
    )
    path = two_port_path(
        start, end, bus_x=250.0, net="SIG", obstacles=[blocker], ctx=RoutingContext()
    )
    pts = parse_wire_path(path)
    assert pts[0] == (start.x, start.y)
    # The branch under test only guards the bug if a detour actually happened.
    assert any(abs(y - start.y) > 1.0 for _x, y in pts), (
        f"expected an off-row detour, got {path!r}"
    )
    # The wire must terminate exactly on the destination port.
    assert pts[-1] == (end.x, end.y), f"path does not reach the port: {path!r}"


def test_hub_row_feed_forces_connection_when_all_candidates_blocked():
    """Regression: a hub row whose every clear feed is blocked by foreign wiring
    must still be connected to the trunk (forced detour), not left detached."""
    from fypa.topology.routing.hub import _HubRowPlan, _connect_row_to_bus

    port = _port("U1", y=100.0, wire_x=100.0)
    plan = _HubRowPlan(
        group=[port],
        y_row=100.0,
        span_lo=100.0,
        span_hi=200.0,
        row_lo=100.0,
        row_hi=200.0,
        detoured=False,
    )
    bus_x = 400.0
    ctx = RoutingContext()
    # Block the on-row horizontal feed and every off-row vertical drop at the
    # row edge column with foreign-net reservations.
    ctx.reserve_horizontal(100.0, 150.0, 450.0, "OTHER")
    ctx.reserve_vertical(200.0, -1000.0, 1000.0, "OTHER")

    trunk_y, bus_leg = _connect_row_to_bus(plan, bus_x, ctx, "VDD", [])

    assert trunk_y is not None, "row was left with no trunk attachment"
    assert bus_leg is not None, "no feed wire emitted -> row detached from trunk"
    xs = [x for x, _y in parse_wire_path(bus_leg)]
    assert max(xs) >= bus_x - 1e-6, f"feed does not reach the trunk: {bus_leg!r}"


def test_hub_eastward_tap_uses_upstream_vertical_before_bus():
    """Downstream singletons branch from an existing tap vertical when possible."""
    from fypa.topology.constants import NODE_W
    from fypa.topology.types import TopologyNode

    blocker = TopologyNode(
        node_id="BLK",
        label="BLK",
        designator="BLK",
        role="SINK",
        x=300.0,
        y=350.0,
        width=NODE_W,
        height=80.0,
        config_label="",
        has_error=False,
        bounds=(300.0, 350.0, NODE_W, 80.0),
        ports=[],
    )
    bus_x = 680.0
    ports = [
        TopologyPort(
            terminal="P",
            net="VDD",
            label="VDD",
            side="right",
            x=164.0,
            y=381.0,
            node_id="J3",
            wire_x=184.0,
        ),
        TopologyPort(
            terminal="P",
            net="VDD",
            label="VDD",
            side="left",
            x=264.0,
            y=381.0,
            node_id="U3",
            wire_x=244.0,
        ),
        TopologyPort(
            terminal="IN_P",
            net="VDD",
            label="VDD",
            side="left",
            x=264.0,
            y=75.0,
            node_id="U4",
            wire_x=244.0,
        ),
        TopologyPort(
            terminal="P",
            net="VDD",
            label="VDD",
            side="left",
            x=732.0,
            y=279.0,
            node_id="U1",
            wire_x=712.0,
        ),
    ]

    def _node(node_id: str, y: float) -> TopologyNode:
        return TopologyNode(
            node_id=node_id,
            label=node_id,
            designator=node_id,
            role="SINK",
            x=264.0,
            y=y,
            width=NODE_W,
            height=80.0,
            config_label="",
            has_error=False,
            bounds=(264.0, y, NODE_W, 80.0),
            ports=[],
        )

    wires = route_hub(
        "VDD",
        ports,
        bus_x,
        obstacles=[blocker, _node("U4", 75.0), _node("U1", 279.0)],
        ctx=RoutingContext(),
    )
    u1_tap = next(w for w in wires if w.src_node == "U1")
    u4_tap = next(w for w in wires if w.src_node == "U4")
    feed_x = parse_wire_path(u4_tap.path_d)[-1][0]
    assert parse_wire_path(u1_tap.path_d)[0][0] == feed_x, u1_tap.path_d
    assert feed_x < bus_x - 1e-6
    assert not any(w.routing_kind == "hub" for w in wires)


def test_hub_row_bus_feed_detour_avoids_row_member_bodies():
    """Detoured row feeds must clear every symbol body, including row members."""
    from fypa.topology.constants import NODE_W
    from fypa.topology.geometry import horizontal_crosses_node
    from fypa.topology.types import TopologyNode

    regulator = TopologyNode(
        node_id="U4",
        label="U4",
        designator="U4",
        role="SINK",
        x=264.0,
        y=222.0,
        width=NODE_W,
        height=74.0,
        config_label="",
        has_error=False,
        bounds=(264.0, 222.0, NODE_W, 74.0),
        ports=[],
    )
    blocker = TopologyNode(
        node_id="BLK",
        label="BLK",
        designator="BLK",
        role="SINK",
        x=300.0,
        y=250.0,
        width=NODE_W,
        height=40.0,
        config_label="",
        has_error=False,
        bounds=(300.0, 250.0, NODE_W, 40.0),
        ports=[],
    )
    bus_x = 456.0
    ports = [
        TopologyPort(
            terminal="P",
            net="VDD",
            label="VDD",
            side="right",
            x=164.0,
            y=261.0,
            node_id="J3",
            wire_x=184.0,
        ),
        TopologyPort(
            terminal="P",
            net="VDD",
            label="VDD",
            side="left",
            x=264.0,
            y=261.0,
            node_id="U4",
            wire_x=244.0,
        ),
    ]
    wires = route_hub(
        "VDD",
        ports,
        bus_x,
        obstacles=[regulator, blocker],
        ctx=RoutingContext(),
    )
    for wire in wires:
        if wire.routing_kind != "hub_tap":
            continue
        from fypa.topology.geometry import path_to_segments

        for seg in path_to_segments("VDD", parse_wire_path(wire.path_d)):
            if seg.orient != "H":
                continue
            y = seg.y1
            x_lo, x_hi = min(seg.x1, seg.x2), max(seg.x1, seg.x2)
            assert not horizontal_crosses_node(regulator, y, x_lo, x_hi), wire.path_d


def test_hub_row_feed_detours_when_row_horizontal_blocked():
    """Row-to-trunk feed uses obstacle detour Y when the row span is blocked."""
    from fypa.topology.constants import NODE_W
    from fypa.topology.types import TopologyNode

    blocker = TopologyNode(
        node_id="BLK",
        label="BLK",
        designator="BLK",
        role="SINK",
        x=300.0,
        y=350.0,
        width=NODE_W,
        height=80.0,
        config_label="",
        has_error=False,
        bounds=(300.0, 350.0, NODE_W, 80.0),
        ports=[],
    )
    bus_x = 680.0
    ports = [
        TopologyPort(
            terminal="P",
            net="VDD",
            label="VDD",
            side="right",
            x=164.0,
            y=381.0,
            node_id="J3",
            wire_x=184.0,
        ),
        TopologyPort(
            terminal="P",
            net="VDD",
            label="VDD",
            side="left",
            x=264.0,
            y=381.0,
            node_id="U3",
            wire_x=244.0,
        ),
        TopologyPort(
            terminal="P",
            net="VDD",
            label="VDD",
            side="left",
            x=732.0,
            y=279.0,
            node_id="U1",
            wire_x=712.0,
        ),
    ]
    wires = route_hub(
        "VDD",
        ports,
        bus_x,
        obstacles=[blocker],
        ctx=RoutingContext(),
    )
    row_wire = next(w for w in wires if w.routing_kind == "hub_row")
    assert _row_feed_reaches_trunk(
        wires,
        row_wire,
        bus_x,
        row_ports=[p for p in ports if p.node_id in {row_wire.src_node, row_wire.dst_node}],
    ), [w.path_d for w in wires]


def test_hub_row_stub_columns_ignores_symbol_edge_extension():
    """Stub span follows port stubs, not the optional right-symbol ``H`` in the path."""
    from fypa.topology.routing.paths import hub_row_path, hub_row_stub_columns

    group = [
        TopologyPort(
            terminal="P",
            net="VDD",
            label="VDD",
            side="right",
            x=164.0,
            y=261.0,
            node_id="J3",
            wire_x=184.0,
        ),
        TopologyPort(
            terminal="P",
            net="VDD",
            label="VDD",
            side="left",
            x=264.0,
            y=261.0,
            node_id="U4",
            wire_x=244.0,
        ),
    ]
    row_lo, row_hi = hub_row_stub_columns(group)
    assert (row_lo, row_hi) == (184.0, 244.0)
    path_d = hub_row_path(group, 261.0)
    assert path_d == "M 164.0,261.0 H 184.0 H 244.0 H 264.0"


def test_hub_row_feed_detour_reserves_vertical_column():
    """Detoured row-to-trunk feeds must reserve the vertical column they use."""
    from fypa.topology.constants import NODE_W
    from fypa.topology.geometry import path_to_segments
    from fypa.topology.types import TopologyNode

    regulator = TopologyNode(
        node_id="U4",
        label="U4",
        designator="U4",
        role="SINK",
        x=264.0,
        y=222.0,
        width=NODE_W,
        height=74.0,
        config_label="",
        has_error=False,
        bounds=(264.0, 222.0, NODE_W, 74.0),
        ports=[],
    )
    blocker = TopologyNode(
        node_id="BLK",
        label="BLK",
        designator="BLK",
        role="SINK",
        x=300.0,
        y=250.0,
        width=NODE_W,
        height=40.0,
        config_label="",
        has_error=False,
        bounds=(300.0, 250.0, NODE_W, 40.0),
        ports=[],
    )
    bus_x = 456.0
    ports = [
        TopologyPort(
            terminal="P",
            net="VDD",
            label="VDD",
            side="right",
            x=164.0,
            y=261.0,
            node_id="J3",
            wire_x=184.0,
        ),
        TopologyPort(
            terminal="P",
            net="VDD",
            label="VDD",
            side="left",
            x=264.0,
            y=261.0,
            node_id="U4",
            wire_x=244.0,
        ),
    ]
    ctx = RoutingContext()
    wires = route_hub(
        "VDD",
        ports,
        bus_x,
        obstacles=[regulator, blocker],
        ctx=ctx,
    )
    feed = next(
        w
        for w in wires
        if w.routing_kind == "hub_tap" and " V " in w.path_d and _max_x(w.path_d) >= bus_x - 1e-6
    )
    vertical_xs = {
        round(seg.x1, 1)
        for seg in path_to_segments("VDD", parse_wire_path(feed.path_d))
        if seg.orient == "V"
    }
    reserved_xs = {round(vx, 1) for vx, _lo, _hi, _net in ctx.vertical_bands}
    assert vertical_xs <= reserved_xs, (
        f"feed verticals {vertical_xs} not covered by reservations {reserved_xs}: {feed.path_d!r}"
    )


def test_connect_row_to_bus_retries_alternate_detour_y(monkeypatch):
    """When the first detour Y fails, try the next candidate."""
    from fypa.topology.constants import NODE_W, WIRE_EPS
    from fypa.topology.routing.hub import _HubRowPlan, _connect_row_to_bus
    from fypa.topology.routing.obstacles import horizontal_segment_clear
    from fypa.topology.types import TopologyNode

    port_a = TopologyPort(
        terminal="P",
        net="VDD",
        label="VDD",
        side="right",
        x=164.0,
        y=261.0,
        node_id="J3",
        wire_x=184.0,
    )
    port_b = TopologyPort(
        terminal="P",
        net="VDD",
        label="VDD",
        side="left",
        x=264.0,
        y=261.0,
        node_id="U4",
        wire_x=244.0,
    )
    plan = _HubRowPlan(
        group=[port_a, port_b],
        y_row=261.0,
        span_lo=164.0,
        span_hi=264.0,
        row_lo=184.0,
        row_hi=244.0,
        detoured=False,
    )
    regulator = TopologyNode(
        node_id="U4",
        label="U4",
        designator="U4",
        role="SINK",
        x=264.0,
        y=222.0,
        width=NODE_W,
        height=74.0,
        config_label="",
        has_error=False,
        bounds=(264.0, 222.0, NODE_W, 74.0),
        ports=[],
    )
    ctx = RoutingContext()
    blocked_ys = {212.0}

    def selective_clear(y, x_lo, x_hi, obstacles, skip):
        if abs(y - 261.0) < WIRE_EPS:
            return False
        if any(abs(y - blocked) < WIRE_EPS for blocked in blocked_ys):
            return False
        return horizontal_segment_clear(y, x_lo, x_hi, obstacles, skip)

    monkeypatch.setattr(
        "fypa.topology.routing.hub.horizontal_segment_clear",
        selective_clear,
    )
    monkeypatch.setattr(
        "fypa.topology.routing.hub.obstacle_detour_y_candidates",
        lambda *_a, **_k: [261.0, 212.0, 180.0],
    )

    trunk_y, path_d = _connect_row_to_bus(plan, 456.0, ctx, "VDD", [regulator])
    assert path_d is not None
    assert trunk_y == 180.0
    assert " V 180.0 " in f" {path_d} "


def test_connect_row_to_bus_skips_foreign_vertical_column():
    """Detour vertical must not share a column with a foreign reserved vertical."""
    from fypa.topology.constants import NODE_W
    from fypa.topology.routing.hub import _HubRowPlan, _connect_row_to_bus
    from fypa.topology.types import TopologyNode

    port_a = TopologyPort(
        terminal="P",
        net="VDD",
        label="VDD",
        side="right",
        x=164.0,
        y=261.0,
        node_id="J3",
        wire_x=184.0,
    )
    port_b = TopologyPort(
        terminal="P",
        net="VDD",
        label="VDD",
        side="left",
        x=264.0,
        y=261.0,
        node_id="U4",
        wire_x=244.0,
    )
    plan = _HubRowPlan(
        group=[port_a, port_b],
        y_row=261.0,
        span_lo=164.0,
        span_hi=264.0,
        row_lo=184.0,
        row_hi=244.0,
        detoured=False,
    )
    regulator = TopologyNode(
        node_id="U4",
        label="U4",
        designator="U4",
        role="SINK",
        x=264.0,
        y=222.0,
        width=NODE_W,
        height=74.0,
        config_label="",
        has_error=False,
        bounds=(264.0, 222.0, NODE_W, 74.0),
        ports=[],
    )
    ctx = RoutingContext()
    ctx.reserve_vertical(244.0, 200.0, 280.0, "SIG")
    trunk_y, path_d = _connect_row_to_bus(plan, 456.0, ctx, "VDD", [regulator])
    assert path_d is not None
    assert trunk_y is not None
    assert abs(trunk_y - 212.0) > 1e-6, path_d


def test_detoured_hub_row_emits_row_bus_and_vertical_drops():
    """Detoured row plans still emit ``hub_row`` plus drops from port Y onto the bus."""
    from fypa.topology.types import TopologyNode

    blocker = TopologyNode(
        node_id="BLK",
        label="BLK",
        designator="BLK",
        role="SINK",
        x=250.0,
        y=248.0,
        width=20.0,
        height=30.0,
        config_label="",
        has_error=False,
        bounds=(250.0, 248.0, 20.0, 30.0),
        ports=[],
    )
    ports = [
        TopologyPort(
            terminal="P",
            net="VDD",
            label="VDD",
            side="right",
            x=164.0,
            y=261.0,
            node_id="J3",
            wire_x=184.0,
        ),
        TopologyPort(
            terminal="P",
            net="VDD",
            label="VDD",
            side="left",
            x=264.0,
            y=261.0,
            node_id="U4",
            wire_x=244.0,
        ),
    ]
    wires = route_hub(
        "VDD",
        ports,
        456.0,
        obstacles=[blocker],
        ctx=RoutingContext(),
    )
    row_wire = next(w for w in wires if w.routing_kind == "hub_row")
    row_y = parse_wire_path(row_wire.path_d)[0][1]
    assert abs(row_y - 261.0) > 1e-6, row_wire.path_d
    drops = [
        w
        for w in wires
        if w.routing_kind == "hub_tap"
        and " V " in w.path_d
        and abs(parse_wire_path(w.path_d)[-1][1] - row_y) < 1e-6
    ]
    assert len(drops) == 2, [w.path_d for w in wires]


def test_detoured_row_drop_skips_foreign_vertical_column():
    """Detoured port drops must not use a stub column blocked by foreign verticals."""
    from fypa.topology.types import TopologyNode

    blocker = TopologyNode(
        node_id="BLK",
        label="BLK",
        designator="BLK",
        role="SINK",
        x=250.0,
        y=248.0,
        width=20.0,
        height=30.0,
        config_label="",
        has_error=False,
        bounds=(250.0, 248.0, 20.0, 30.0),
        ports=[],
    )
    ports = [
        TopologyPort(
            terminal="P",
            net="VDD",
            label="VDD",
            side="right",
            x=164.0,
            y=261.0,
            node_id="J3",
            wire_x=184.0,
        ),
        TopologyPort(
            terminal="P",
            net="VDD",
            label="VDD",
            side="left",
            x=264.0,
            y=261.0,
            node_id="U4",
            wire_x=244.0,
        ),
    ]
    ctx = RoutingContext()
    ctx.reserve_vertical(244.0, 200.0, 280.0, "SIG")
    wires = route_hub(
        "VDD",
        ports,
        456.0,
        obstacles=[blocker],
        ctx=ctx,
    )
    u4_tap = next(w for w in wires if w.src_node == "U4")
    row_y = parse_wire_path(next(w for w in wires if w.routing_kind == "hub_row").path_d)[0][1]
    assert abs(row_y - 261.0) > 1e-6
    assert " H 228.0 " in f" {u4_tap.path_d} ", u4_tap.path_d
    assert u4_tap.path_d.endswith(f"V {row_y:.1f}")


def test_detoured_row_connector_merges_at_port_column():
    """Detoured rows expose member columns so connector sub-symbols can drop vertically."""
    from fypa.topology.constants import NODE_W
    from fypa.topology.types import TopologyNode

    def _connector_node(
        designator: str,
        node_id: str,
        x: float,
        y: float,
        wire_x: float,
    ) -> TopologyNode:
        return TopologyNode(
            node_id=node_id,
            label=designator,
            designator=designator,
            role="CONNECTOR",
            x=x,
            y=y,
            width=NODE_W,
            height=40.0,
            config_label="",
            has_error=False,
            bounds=(x, y, NODE_W, 40.0),
            ports=[
                TopologyPort(
                    terminal="P",
                    net="VDD",
                    label="VDD",
                    side="right",
                    x=x,
                    y=y + 20.0,
                    node_id=node_id,
                    wire_x=wire_x,
                ),
            ],
        )

    blocker = TopologyNode(
        node_id="BLK",
        label="BLK",
        designator="BLK",
        role="SINK",
        x=250.0,
        y=248.0,
        width=20.0,
        height=30.0,
        config_label="",
        has_error=False,
        bounds=(250.0, 248.0, 20.0, 30.0),
        ports=[],
    )
    j21 = _connector_node("J2.1", "J2.1", 164.0, 241.0, 184.0)
    j22 = _connector_node("J2.2", "J2.2", 264.0, 241.0, 244.0)
    j23 = _connector_node("J2.3", "J2.3", 164.0, 80.0, 184.0)
    ports = [j21.ports[0], j22.ports[0], j23.ports[0]]
    wires = route_hub(
        "VDD",
        ports,
        456.0,
        obstacles=[j21, j22, j23, blocker],
        ctx=RoutingContext(),
    )
    row_wire = next(w for w in wires if w.routing_kind == "hub_row")
    row_y = parse_wire_path(row_wire.path_d)[0][1]
    assert abs(row_y - 261.0) > 1e-6
    merge_tap = next(w for w in wires if w.src_node == "J2.3")
    assert merge_tap.path_d == f"M 164.0,100.0 V {row_y:.1f}"


def test_route_hub_tap_scans_later_row_spans_at_same_y():
    """Do not stop row-span search when the first same-Y span misses the stub column."""
    port = TopologyPort(
        terminal="P",
        net="VDD",
        label="VDD",
        side="right",
        x=320.0,
        y=200.0,
        node_id="U5",
        wire_x=320.0,
    )
    row_spans = [
        (200.0, 140.0, 160.0, {140.0, 160.0}),
        (200.0, 300.0, 340.0, {300.0, 320.0, 340.0}),
    ]
    path_d, tap_y = _route_hub_tap(
        port,
        456.0,
        [],
        RoutingContext(),
        "VDD",
        row_spans,
        {},
    )
    assert path_d == ""
    assert tap_y == 200.0


def test_route_hub_tap_same_y_off_row_stub_needs_tap():
    """Same-Y ports off the stub span still need a tap even when inside the body span."""
    port = TopologyPort(
        terminal="P",
        net="VDD",
        label="VDD",
        side="right",
        x=220.0,
        y=200.0,
        node_id="U5",
        wire_x=200.0,
    )
    row_spans = [
        (200.0, 130.0, 160.0, {140.0, 160.0}),
    ]
    path_d, tap_y = _route_hub_tap(
        port,
        456.0,
        [],
        RoutingContext(),
        "VDD",
        row_spans,
        {},
    )
    assert path_d != ""
    assert tap_y == 200.0
    assert path_d.startswith("M 220.0,200.0")


def test_route_hub_tap_same_y_stub_overlap_without_membership_needs_tap():
    """Same-Y singletons must not skip taps when only the stub column overlaps a row."""
    port = TopologyPort(
        terminal="P",
        net="VDD",
        label="VDD",
        side="right",
        x=220.0,
        y=200.0,
        node_id="U5",
        wire_x=150.0,
    )
    row_spans = [
        (200.0, 130.0, 140.0, {120.0, 140.0}),
        (200.0, 150.0, 160.0, {164.0, 264.0}),
    ]
    path_d, tap_y = _route_hub_tap(
        port,
        456.0,
        [],
        RoutingContext(),
        "VDD",
        row_spans,
        {},
    )
    assert path_d != ""
    assert tap_y == 200.0


def test_route_hub_tap_tries_later_row_when_first_span_fails():
    """Keep scanning row spans when the first matching stub cannot drop onto its row."""
    from fypa.topology.types import TopologyNode

    blocker = TopologyNode(
        node_id="BLK",
        label="BLK",
        designator="BLK",
        role="SINK",
        x=100.0,
        y=215.0,
        width=300.0,
        height=20.0,
        config_label="",
        has_error=False,
        bounds=(100.0, 215.0, 300.0, 20.0),
        ports=[],
    )
    port = TopologyPort(
        terminal="P",
        net="VDD",
        label="VDD",
        side="right",
        x=140.0,
        y=220.0,
        node_id="U1",
        wire_x=150.0,
    )
    ctx = RoutingContext()
    ctx.reserve_vertical(150.0, 200.0, 219.0, "SIG")
    row_spans = [
        (200.0, 130.0, 160.0, set()),
        (240.0, 130.0, 170.0, set()),
    ]
    path_d, tap_y = _route_hub_tap(
        port,
        456.0,
        [blocker],
        ctx,
        "VDD",
        row_spans,
        {},
    )
    assert tap_y == 240.0
    assert path_d.endswith("V 240.0")


def test_hub_tap_vertical_merge_at_port_reserves_column():
    from fypa.topology.routing.paths import hub_tap_vertical_to_row

    port = TopologyPort(
        terminal="P",
        net="VDD",
        label="VDD",
        side="right",
        x=164.0,
        y=100.0,
        node_id="J2.3",
        wire_x=184.0,
    )
    ctx = RoutingContext()
    hub_tap_vertical_to_row(
        port,
        261.0,
        merge_at_port=True,
        ctx=ctx,
        net="VDD",
    )
    assert ctx.vertical_bands == [(164.0, 100.0, 261.0, "VDD")]


def test_obstacle_detour_y_candidates_respects_wire_eps(monkeypatch):
    from fypa.topology.constants import WIRE_EPS
    from fypa.topology.routing import obstacles as obstacles_mod
    from fypa.topology.routing.obstacles import obstacle_detour_y_candidates

    monkeypatch.setattr(obstacles_mod, "obstacle_detour_y", lambda *_a, **_k: 262.5)
    monkeypatch.setattr(
        obstacles_mod,
        "_obstacle_detour_y_direction",
        lambda *_a, downward, **_k: 258.0 if downward else 261.8,
    )
    order = obstacle_detour_y_candidates(
        RoutingContext(),
        260.0,
        184.0,
        456.0,
        [],
        set(),
        "VDD",
    )
    assert order == [260.0, 262.5, 258.0, 261.8]
    assert all(
        sum(1 for y in order if abs(y - candidate) < WIRE_EPS) == 1 for candidate in order
    )


def test_coincident_ports_do_not_produce_a_double_back_wire():
    """Two ports at the same point route as a single stub, not a stub-back-stub.

    Regression for ``two_port_path`` emitting ``H 120 H 100 H 120 H 100`` for
    coincident endpoints — a self-overlapping wire that ``simplify_wire_path``
    cannot collapse.
    """
    p = TopologyPort(
        terminal="P", net="N", label="N", side="right",
        x=100.0, y=100.0, node_id="A", wire_x=120.0,
    )
    q = TopologyPort(
        terminal="N", net="N", label="N", side="right",
        x=100.0, y=100.0, node_id="B", wire_x=120.0,
    )
    path = two_port_path(p, q, bus_x=120.0, net="N")
    assert _xs_monotonic(path), f"coincident-port wire doubles back: {path!r}"
