"""Parametrized invariant checks over committed topology fixtures."""

from __future__ import annotations

import pytest

from fypa.topology import (
    build_topology_model,
    topology_wiring_report,
    validate_topology,
)
from tests.topology_fixtures import list_topology_fixtures, load_topology_fixture


@pytest.mark.parametrize("fixture_name", list_topology_fixtures())
def test_topology_fixture_has_zero_issues(fixture_name: str):
    model = build_topology_model(load_topology_fixture(fixture_name))
    report = topology_wiring_report(model)
    assert report["summary"]["issues"] == 0, report["issues"]
    errors = [
        i for i in validate_topology(model)
        if i.get("severity", "error") != "warning"
    ]
    assert not errors


def test_render_report_junction_parity():
    """Junction coordinates in the report match compute_schematic_geometry."""
    from fypa.topology.geometry import compute_schematic_geometry

    model = build_topology_model(load_topology_fixture("gnd_junction_tap"))
    geo = compute_schematic_geometry(
        model.wires,
        gnd_symbol_x=model.gnd_symbol_x,
        gnd_bus_y=model.gnd_bus_y,
    )
    expected = {
        (j["x"], j["y"])
        for j in topology_wiring_report(model)["schematic"]["junctions"]
    }
    actual = {(round(x, 1), round(y, 1)) for x, y in geo.junctions}
    assert expected == actual
