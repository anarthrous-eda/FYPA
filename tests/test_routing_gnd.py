"""Regressions for GND trunk placement (routing/gnd.py).

The column-trunk refactor dropped the foreign-body obstacle avoidance that the
old per-port ``gnd_drop_x`` provided, so a shared GND trunk could be drawn
straight through an unrelated symbol body. The trunk column must shift clear.
"""

from __future__ import annotations

from fypa.topology.constants import NODE_W, OBSTACLE_CLEAR
from fypa.topology.geometry import parse_wire_path, vertical_crosses_node
from fypa.topology.routing.context import RoutingContext
from fypa.topology.routing.gnd import _apply_gnd_column_trunk_attach, gnd_wire_paths
from fypa.topology.routing.obstacles import shift_x_clear_of_vertical_obstacles
from fypa.topology.types import TopologyNode, TopologyPort


def _gnd_port(node_id: str, x: float, y: float, side: str = "right") -> TopologyPort:
    return TopologyPort(
        terminal="P", net="GND", label="GND", side=side, x=x, y=y, node_id=node_id
    )


def _blocker(node_id: str, x: float, y: float, h: float = 100.0) -> TopologyNode:
    return TopologyNode(
        node_id=node_id,
        label=node_id,
        designator=node_id,
        role="SINK",
        x=x,
        y=y,
        width=NODE_W,
        height=h,
        config_label="",
        has_error=False,
        bounds=(x, y, NODE_W, h),
        ports=[],
    )


def test_shift_x_clear_steps_past_chained_bodies():
    """Shifting past one body onto another must keep going (convergence)."""
    a = _blocker("A", 110.0, 0.0, h=100.0)  # covers x 110..238
    b = _blocker("B", 240.0, 0.0, h=100.0)  # 238+CLEAR=248 lands inside B (240..368)
    x = shift_x_clear_of_vertical_obstacles(120.0, 10.0, 90.0, [a, b], set(), 1.0)
    assert x == 240.0 + NODE_W + OBSTACLE_CLEAR
    assert not vertical_crosses_node(a, x, 10.0, 90.0)
    assert not vertical_crosses_node(b, x, 10.0, 90.0)


def test_gnd_trunk_column_shifts_clear_of_foreign_body():
    """The shared trunk column moves outward off a crossed symbol body."""
    port = _gnd_port("U1", x=100.0, y=300.0, side="right")  # stub column x=120
    blocker = _blocker("BLK", x=110.0, y=350.0, h=100.0)  # body over the trunk run
    trunk_x = _apply_gnd_column_trunk_attach([port], 500.0, [blocker])
    assert trunk_x == 110.0 + NODE_W + OBSTACLE_CLEAR
    assert not vertical_crosses_node(blocker, trunk_x, 300.0, 500.0)
    # Ports are re-pinned to the shifted column.
    assert port.wire_x == trunk_x


def test_gnd_trunk_own_node_does_not_push_column():
    """A body belonging to the group's own node must not move the trunk."""
    port = _gnd_port("U1", x=100.0, y=300.0, side="right")
    own = _blocker("U1", x=110.0, y=350.0, h=100.0)  # same node_id as the port
    trunk_x = _apply_gnd_column_trunk_attach([port], 500.0, [own])
    assert trunk_x == 120.0  # unchanged stub column


def test_gnd_wire_paths_trunk_clears_body():
    """End-to-end: the emitted gnd_trunk vertical does not cut the symbol body."""
    port = _gnd_port("U1", x=100.0, y=300.0, side="right")
    blocker = _blocker("BLK", x=110.0, y=350.0, h=100.0)
    wires, _bus_min = gnd_wire_paths(
        [port], bus_y=500.0, obstacles=[blocker], ctx=RoutingContext()
    )
    trunks = [w for w in wires if w.routing_kind == "gnd_trunk"]
    assert trunks, "expected a gnd_trunk wire for a port offset from the bus"
    for w in trunks:
        trunk_x = parse_wire_path(w.path_d)[0][0]
        assert not vertical_crosses_node(blocker, trunk_x, 300.0, 500.0), (
            f"trunk at x={trunk_x} still crosses the body: {w.path_d!r}"
        )
