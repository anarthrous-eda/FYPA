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


def test_gnd_column_trunk_x_prefers_gnd_stub():
    gnd = _port(node_id="U1", net=GND_NET, side="left", x=120.0, stub_length=12.0)
    sig = _port(node_id="U1", net="VDD", side="left", x=120.0, y=70.0)
    trunk = gnd_column_trunk_x([gnd, sig])
    assert trunk == port_stub_x(gnd)
