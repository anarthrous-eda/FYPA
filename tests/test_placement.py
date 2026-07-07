"""Table-driven unit tests for placement.py keys and stubs."""

from __future__ import annotations

from fypa.topology.constants import GND_NET, PORT_WIRE_STUB
from fypa.topology.placement import (
    gnd_column_trunk_x,
    net_gutter_key,
    port_stub_length,
    port_stub_x,
)
from fypa.topology.types import TopologyPort


def _port(**kwargs) -> TopologyPort:
    defaults = {
        "terminal": "P",
        "net": "SIG",
        "label": "SIG",
        "side": "right",
        "x": 100.0,
        "y": 50.0,
        "node_id": "U1",
    }
    defaults.update(kwargs)
    return TopologyPort(**defaults)


def test_port_stub_x_default_and_staggered():
    p = _port(side="right", x=100.0)
    assert port_stub_length(p) == PORT_WIRE_STUB
    assert port_stub_x(p) == 100.0 + PORT_WIRE_STUB
    p.stub_length = 12.0
    assert port_stub_x(p) == 112.0
    left = _port(side="left", x=200.0, stub_length=16.0)
    assert port_stub_x(left) == 184.0


def test_net_gutter_key_two_port_gap():
    a = _port(node_id="A", x=100.0, side="right")
    b = _port(node_id="B", x=200.0, side="left")
    assert net_gutter_key([a, b]) == (100.0, 200.0)


def test_net_gutter_key_stack_returns_none():
    a = _port(node_id="A", x=100.0, y=50.0)
    b = _port(node_id="B", x=100.0, y=80.0)
    assert net_gutter_key([a, b]) is None


def test_gutter_groups_includes_multi_port_hub_nets():
    from fypa.topology.placement import gutter_groups

    a = _port(node_id="A", net="HUB", x=100.0, side="right")
    b = _port(node_id="B", net="HUB", x=200.0, side="left")
    c = _port(node_id="C", net="HUB", x=300.0, side="left")
    pair_a = _port(node_id="P1", net="PAIR", x=110.0, side="right")
    pair_b = _port(node_id="P2", net="PAIR", x=290.0, side="left")
    groups = gutter_groups([a, b, c, pair_a, pair_b])
    assert groups[(100.0, 300.0)] == {"HUB"}
    assert groups[(110.0, 290.0)] == {"PAIR"}


def test_allocate_bus_x_leaves_valid_west_candidate_untouched():
    """A candidate comfortably west of an assigned bus is valid and must not be
    shoved east. Regression for the one-sided proximity test (finding 5.4)."""
    from fypa.topology.placement.bus_grid import allocate_bus_x
    from fypa.topology.constants import MIN_PARALLEL_GAP

    prev = 300.0
    nominal = prev - 5 * MIN_PARALLEL_GAP  # far west, well-separated
    x = allocate_bus_x(
        nominal,
        0.0, 100.0,
        bus_lo=nominal - 50.0, bus_hi=prev + 50.0,
        reserved_verticals=[],
        net="VDD",
        outward=1.0,
        assigned_in_group=[prev],
    )
    assert abs(x - nominal) < 1e-6  # not pushed toward prev


def test_allocate_bus_x_separates_when_actually_too_close():
    """A candidate within MIN_PARALLEL_GAP of an assigned bus is shifted to a
    valid, at-least-a-gap-away slot inside the corridor."""
    from fypa.topology.placement.bus_grid import allocate_bus_x
    from fypa.topology.constants import MIN_PARALLEL_GAP, WIRE_EPS

    prev = 300.0
    nominal = prev + MIN_PARALLEL_GAP / 3  # too close, east of prev
    x = allocate_bus_x(
        nominal,
        0.0, 100.0,
        bus_lo=200.0, bus_hi=400.0,
        reserved_verticals=[],
        net="VDD",
        outward=1.0,
        assigned_in_group=[prev],
    )
    assert abs(x - prev) >= MIN_PARALLEL_GAP - WIRE_EPS


def test_gnd_column_trunk_x_prefers_gnd_stub():
    gnd = _port(node_id="U1", net=GND_NET, side="left", x=120.0, stub_length=12.0)
    sig = _port(node_id="U1", net="VDD", side="left", x=120.0, y=70.0)
    trunk = gnd_column_trunk_x([gnd, sig])
    assert trunk == port_stub_x(gnd)


def test_gutter_approach_side_mixed_falls_back_to_majority():
    """Regression: a gap group with approach ports on opposite sides must not
    raise (which aborted the whole layout) — it now picks the majority side."""
    from fypa.topology.placement.pair_slots import gutter_approach_side

    left = _port(side="left")
    right = _port(side="right")
    # Two left approach ports, one right -> majority "left".
    group = [(left, right), (left, right), (right, left)]
    assert gutter_approach_side(group) == "left"


def test_gutter_approach_side_tie_is_deterministic_and_order_independent():
    from fypa.topology.placement.pair_slots import gutter_approach_side

    left = _port(side="left")
    right = _port(side="right")
    # A 1:1 tie resolves deterministically regardless of group order.
    assert gutter_approach_side([(left, right), (right, left)]) == "right"
    assert gutter_approach_side([(right, left), (left, right)]) == "right"


def test_gutter_approach_side_single_side_unchanged():
    from fypa.topology.placement.pair_slots import gutter_approach_side

    left = _port(side="left")
    assert gutter_approach_side([(left, left), (left, left)]) == "left"


def test_gutter_approach_side_empty_group_raises():
    import pytest

    from fypa.topology.placement.pair_slots import gutter_approach_side

    with pytest.raises(ValueError):
        gutter_approach_side([])
