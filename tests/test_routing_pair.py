"""Regressions for gutter-pair bus-x placement (routing/pair.py)."""

from __future__ import annotations

import fypa.topology.routing.pair as pair_mod
from fypa.topology.constants import NODE_W
from fypa.topology.routing.context import RoutingContext
from fypa.topology.types import TopologyNode, TopologyPort


def _port(node_id: str, x: float, side: str) -> TopologyPort:
    return TopologyPort(
        terminal="P", net="SIG", label="SIG", side=side, x=x, y=50.0, node_id=node_id
    )


def _obstacle(node_id: str, x: float) -> TopologyNode:
    return TopologyNode(
        node_id=node_id,
        label=node_id,
        designator=node_id,
        role="SINK",
        x=x,
        y=0.0,
        width=NODE_W,
        height=80.0,
        config_label="",
        has_error=False,
        bounds=(x, 0.0, NODE_W, 80.0),
        ports=[],
    )


def test_bus_x_for_pair_forwards_reserved_verticals(monkeypatch):
    """Regression: the column-gap branch must forward already-reserved verticals
    to the bus-x picker, not an empty list — otherwise a gutter bus can be placed
    on top of a GND trunk or another net's vertical in the same corridor."""
    captured: dict = {}

    def fake_pick(*args, **kwargs):
        captured["reserved"] = kwargs.get("reserved")
        return 222.0

    monkeypatch.setattr(pair_mod, "pick_gutter_bus_x", fake_pick)

    ctx = RoutingContext()
    ctx.reserve_vertical(200.0, 40.0, 60.0, "OTHER")

    a = _port("A", x=100.0, side="right")
    b = _port("B", x=400.0, side="left")
    # Two obstacle columns yield a non-empty column-gap list, and a non-degenerate
    # channel selects the column-gap branch that used to hardcode reserved=[].
    obstacles = [_obstacle("O1", 0.0), _obstacle("O2", 400.0)]

    result = pair_mod._bus_x_for_pair(
        a,
        b,
        bus_plan=None,
        ctx=ctx,
        col=0.0,
        side="right",
        lane=0,
        n_lanes=1,
        slot=0,
        n_slots=1,
        channel_lo=100.0,
        channel_hi=300.0,
        assigned_bus=[],
        obstacles=obstacles,
    )

    assert result == 222.0
    assert captured["reserved"] == list(ctx.vertical_bands)
    assert captured["reserved"], "reserved verticals were not forwarded to the picker"
