"""Parametrized invariant checks over committed topology fixtures."""

from __future__ import annotations

import pytest

from fypa.topology import (
    GND_NET,
    build_topology_model,
    topology_wiring_report,
    validate_topology,
)
from fypa.topology.constants import WIRE_EPS
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


@pytest.mark.parametrize("fixture_name", list_topology_fixtures())
def test_bus_plan_matches_routed_bus_x(fixture_name: str):
    """Planned bus_x values must match routed wires (single-pass consistency)."""
    from fypa.topology.layout import build_node_layout
    from fypa.topology.placement.plan_lookup import (
        planned_gnd_trunk_xs,
        planned_signal_bus_x,
        planned_stack_bus_entries,
        routed_gnd_trunk_xs,
        stack_bus_x_matches_routing,
    )
    from fypa.topology.routing import build_wires

    layout = build_node_layout(load_topology_fixture(fixture_name))
    plan = layout.bus_plan
    wires, _ = build_wires(
        layout.ports,
        gnd_bus_y=layout.gnd_bus_y,
        obstacles=layout.directive_nodes,
        bus_plan=plan,
    )

    for wire in wires:
        if wire.dashed or wire.bus_x is None or wire.net == GND_NET:
            continue
        expected = planned_signal_bus_x(wire, plan, layout.ports)
        assert expected is not None, (
            f"{fixture_name}: {wire.net} routed bus_x={wire.bus_x} "
            f"but no plan entry ({wire.routing_kind})"
        )
        assert abs(wire.bus_x - expected) < WIRE_EPS, (
            f"{fixture_name}: {wire.net} routed {wire.bus_x} planned {expected} "
            f"({wire.routing_kind})"
        )

    for (col, side, net), bus_x in planned_stack_bus_entries(plan).items():
        assert stack_bus_x_matches_routing(col, side, net, bus_x, wires), (
            f"{fixture_name}: stack bus ({col}, {side}, {net}) at {bus_x} "
            f"not found in routed wires"
        )

    assert routed_gnd_trunk_xs(wires) == planned_gnd_trunk_xs(plan), (
        f"{fixture_name}: GND trunk x mismatch "
        f"routed={sorted(routed_gnd_trunk_xs(wires))} "
        f"planned={sorted(planned_gnd_trunk_xs(plan))}"
    )


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
