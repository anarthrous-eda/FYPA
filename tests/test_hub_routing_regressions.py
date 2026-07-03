"""Hub routing regressions from committed layout fixtures.

Fixtures are exported board metadata with layout-neutral names.  Each test
class maps to a concrete routing bug that previously appeared on full-board
probes.
"""

from __future__ import annotations

import pytest

from fypa.topology import parse_wire_path
from fypa.topology.constants import GND_NET, PORT_WIRE_STUB_MIN, WIRE_EPS
from fypa.topology.placement import port_stub_x
from fypa.topology.validate import check_dangling_wire_endpoints, validate_topology
from fypa.topology.validate.segments import check_wires_through_foreign_nodes
from tests.hub_regression_helpers import (
    FIXTURE_ESCAPE_BRANCH,
    FIXTURE_ROW_DETOUR,
    HUB_FIXTURES,
    all_net_ports_connected,
    build_hub_fixture,
    detoured_row_feed,
    eastward_singleton_tap,
    escape_vertical_x,
    horizontal_segments_crossing_node,
    hub_bus_column,
    hub_row_wires,
    regulator_on_hub_row,
    upstream_escape_tap,
)
from tests.test_topology_geometry import foreign_segment_overlap_issues


@pytest.mark.parametrize("fixture_name", HUB_FIXTURES)
def test_hub_fixture_passes_topology_validation(fixture_name: str) -> None:
    model = build_hub_fixture(fixture_name)
    issues = [
        i
        for i in validate_topology(model)
        if i["code"] != "foreign_wire_crossing"
    ]
    assert not issues, issues


@pytest.mark.parametrize("fixture_name", HUB_FIXTURES)
def test_hub_fixture_has_no_foreign_node_crossings(fixture_name: str) -> None:
    model = build_hub_fixture(fixture_name)
    issues = check_wires_through_foreign_nodes(model)
    assert not issues, issues


@pytest.mark.parametrize("fixture_name", HUB_FIXTURES)
def test_hub_fixture_has_no_dangling_endpoints(fixture_name: str) -> None:
    from fypa.topology.geometry import compute_schematic_geometry

    model = build_hub_fixture(fixture_name)
    geo = compute_schematic_geometry(
        model.wires,
        gnd_symbol_x=model.gnd_symbol_x,
        gnd_bus_y=model.gnd_bus_y,
    )
    issues = check_dangling_wire_endpoints(model, geo)
    assert not issues, issues


def test_hub_net_disconnected_when_row_feed_fails(monkeypatch) -> None:
    """``hub_net_disconnected`` when row-to-bus routing returns no feed."""
    from fypa.topology.routing import hub as hub_mod

    monkeypatch.setattr(hub_mod, "_connect_row_to_bus", lambda *_a, **_k: (None, None))
    model = build_hub_fixture(FIXTURE_ROW_DETOUR)
    issues = validate_topology(model)
    assert any(i["code"] == "hub_net_disconnected" for i in issues), issues


class TestHubRowDetourReachesTrunk:
    """Row bus must join the trunk when ``row_y`` is foreign-blocked.

    Regression: downstream loads on the trunk looked connected while the
    source row (connector + on-row regulator) was electrically orphaned.
    """

    FIXTURE = FIXTURE_ROW_DETOUR
    POWER_NET = "VDD_48V"

    @pytest.fixture
    def model(self):
        return build_hub_fixture(self.FIXTURE)

    def test_every_power_port_is_on_one_connected_net(self, model) -> None:
        assert all_net_ports_connected(model, self.POWER_NET)

    def test_row_feed_reaches_planned_bus_column(self, model) -> None:
        bus_x = hub_bus_column(model, self.POWER_NET)
        feed = detoured_row_feed(model, self.POWER_NET)
        end_x, _end_y = parse_wire_path(feed.path_d)[-1]
        assert abs(end_x - bus_x) < WIRE_EPS, feed.path_d

    def test_detour_runs_above_on_row_regulator_body(self, model) -> None:
        row_wire = hub_row_wires(model, self.POWER_NET)[0]
        regulator = regulator_on_hub_row(model, row_wire)
        _nx, ny, _nw, nh = regulator.bounds
        feed = detoured_row_feed(model, self.POWER_NET)
        pts = parse_wire_path(feed.path_d)
        detour_y = pts[1][1]
        assert detour_y < ny - WIRE_EPS, (
            f"detour must clear regulator body top y={ny}, got {detour_y}: {feed.path_d}"
        )

    def test_no_power_segment_runs_through_on_row_regulator(self, model) -> None:
        row_wire = hub_row_wires(model, self.POWER_NET)[0]
        regulator = regulator_on_hub_row(model, row_wire)
        hits = horizontal_segments_crossing_node(model, self.POWER_NET, regulator)
        assert not hits, [f"{w.path_d} at y={y}" for w, y in hits]


class TestHubEscapeVerticalEastTap:
    """Eastward singletons branch horizontally from an upstream escape vertical.

    Regression: an extra trunk vertical and bus knick were added even though
    the upstream escape column already spanned the downstream port height.
    """

    FIXTURE = FIXTURE_ESCAPE_BRANCH
    POWER_NET = "VDD_5V0"

    @pytest.fixture
    def model(self):
        return build_hub_fixture(self.FIXTURE)

    def test_every_power_port_is_on_one_connected_net(self, model) -> None:
        assert all_net_ports_connected(model, self.POWER_NET)

    def test_downstream_tap_is_a_single_horizontal_from_escape_column(self, model) -> None:
        escape = upstream_escape_tap(model, self.POWER_NET)
        east = eastward_singleton_tap(model, self.POWER_NET)
        col_x = escape_vertical_x(escape)
        start_x, start_y = parse_wire_path(east.path_d)[0]
        assert abs(start_x - col_x) < WIRE_EPS, east.path_d
        assert " V " not in east.path_d
        downstream_x = parse_wire_path(east.path_d)[-1][0]
        downstream_port = next(
            p
            for n in model.nodes
            for p in n.ports
            if p.net == self.POWER_NET and abs(p.x - downstream_x) < WIRE_EPS
        )
        assert abs(start_y - downstream_port.y) < WIRE_EPS

    def test_power_net_has_no_hub_trunk_wire(self, model) -> None:
        assert not any(
            w.routing_kind == "hub"
            for w in model.wires
            if w.net == self.POWER_NET
        )

    def test_row_meets_escape_column_without_separate_bus_feed(self, model) -> None:
        escape = upstream_escape_tap(model, self.POWER_NET)
        col_x = escape_vertical_x(escape)
        row_wire = hub_row_wires(model, self.POWER_NET)[0]
        row_y = parse_wire_path(row_wire.path_d)[0][1]
        span_lo = min(x for x, _y in parse_wire_path(row_wire.path_d))
        span_hi = max(x for x, _y in parse_wire_path(row_wire.path_d))
        assert span_lo - WIRE_EPS <= col_x <= span_hi + WIRE_EPS
        assert abs(parse_wire_path(escape.path_d)[-1][1] - row_y) < WIRE_EPS

    def test_power_and_gnd_vertical_bands_do_not_overlap(self, model) -> None:
        assert not foreign_segment_overlap_issues(model)


class TestHubRegulatorColumnSeparation:
    """Regulator power and GND drops must stay on distinct stub columns."""

    FIXTURE = FIXTURE_ESCAPE_BRANCH

    @pytest.fixture
    def model(self):
        return build_hub_fixture(self.FIXTURE)

    def test_top_regulator_power_and_gnd_stubs_differ(self, model) -> None:
        top = max(
            (n for n in model.nodes if n.role == "REGULATOR"),
            key=lambda n: n.y,
        )
        left = [p for p in top.ports if p.side == "left"]
        pwr = next(p for p in left if p.net != GND_NET)
        gnd = next(p for p in left if p.net == GND_NET)
        assert port_stub_x(pwr) != port_stub_x(gnd)

    def test_bottom_regulator_gnd_avoids_power_stub_column(self, model) -> None:
        from fypa.topology import path_to_segments

        bottom = min(
            (n for n in model.nodes if n.role == "REGULATOR"),
            key=lambda n: n.y,
        )
        left = [p for p in bottom.ports if p.side == "left"]
        pwr = next(p for p in left if p.net != GND_NET)
        gnd = next(p for p in left if p.net == GND_NET)
        assert port_stub_x(pwr) != port_stub_x(gnd)
        pwr_x = round(port_stub_x(pwr), 1)
        for wire in model.wires:
            if wire.net != GND_NET:
                continue
            for seg in path_to_segments(wire.net, parse_wire_path(wire.path_d)):
                if seg.orient == "V" and abs(seg.x1 - pwr_x) < 1.0:
                    pytest.fail(
                        f"GND vertical on power column x={pwr_x}: {wire.path_d}",
                    )


class TestHubStackedInputStubLength:
    """Stacked connector inputs must honor minimum stub length before turning."""

    FIXTURE = FIXTURE_ESCAPE_BRANCH

    @pytest.fixture
    def model(self):
        return build_hub_fixture(self.FIXTURE)

    def test_stacked_negative_inputs_keep_minimum_stub(self, model) -> None:
        from fypa.topology import path_to_segments

        stacked = [n for n in model.nodes if "." in n.designator]
        assert len(stacked) >= 2
        for node in stacked:
            port = next(p for p in node.ports if p.net == "V-")
            tap = next(
                w
                for w in model.wires
                if w.net == "V-" and w.routing_kind == "hub_tap" and w.src_node == port.node_id
            )
            segs = path_to_segments("V-", parse_wire_path(tap.path_d))
            if port.side == "left":
                port_seg = next(
                    (s for s in segs if s.orient == "H" and abs(s.x2 - port.x) < 1.0),
                    None,
                )
            else:
                port_seg = next(
                    (s for s in segs if s.orient == "H" and abs(s.x1 - port.x) < 1.0),
                    None,
                )
            assert port_seg is not None, tap.path_d
            assert port_seg.length >= PORT_WIRE_STUB_MIN - 0.6, tap.path_d
            stub = port_stub_x(port)
            assert abs(port_seg.x1 - stub) < 1.0 or abs(port_seg.x2 - stub) < 1.0
