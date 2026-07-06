"""Tests for find_port_at hit testing."""

from __future__ import annotations

from fypa.topology import build_topology_model, find_port_at
from tests.topology_fixtures import load_topology_fixture


def test_find_port_at_hits_port_circle():
    model = build_topology_model(load_topology_fixture("project_b_compact"))
    port = next(
        p for n in model.nodes for p in n.ports
        if p.node_id == "J1" and p.terminal == "P"
    )
    hit = find_port_at(model, port.x, port.y)
    assert hit is port


def test_find_port_at_misses_far_from_ports():
    model = build_topology_model(load_topology_fixture("project_b_compact"))
    assert find_port_at(model, -100.0, -100.0) is None
