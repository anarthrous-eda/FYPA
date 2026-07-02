"""Routing regressions: multi-row hub connectivity and degenerate pairs.

These exercise wire-level routing paths that the SVG snapshot fixtures do not
cover (no committed fixture produces a multi-row hub whose trunk sits beside
its rows, nor two coincident ports on one net).
"""

from __future__ import annotations

from fypa.topology.geometry import parse_wire_path
from fypa.topology.routing.context import RoutingContext
from fypa.topology.routing.hub import route_hub
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


def test_every_hub_row_reaches_trunk_when_bus_sits_beside_rows():
    """Each row bus must extend to the trunk column, not just the last one.

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
        assert _max_x(w.path_d) >= bus_x - 1e-6, (
            f"row bus {w.path_d!r} stops before the trunk at x={bus_x}"
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
