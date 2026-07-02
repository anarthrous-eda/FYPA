"""Tests for validate/report issue merging."""

from __future__ import annotations

from fypa.topology import (
    TopologyModel,
    TopologyNode,
    TopologyWire,
    merge_validation_issues,
    validate_topology,
)


def test_merge_validation_issues_preserves_order():
    model = TopologyModel(
        wires=[
            TopologyWire(
                net="VDD",
                path_d="M 0,0 H 10",
                label="VDD",
                label_x=0.0,
                label_y=0.0,
            ),
        ],
    )
    wire_issues = [{"code": "wire_issue", "message": "from report", "severity": "error"}]
    merged = merge_validation_issues(model, wire_issues)
    assert merged[0]["code"] == "wire_issue"
    assert any(i["code"] == "label_not_at_origin" for i in merged)


def test_vertical_under_node_is_warning_not_summary_issue():
    node = TopologyNode(
        node_id="U1",
        label="U1",
        designator="U1",
        role="SINK",
        x=10.0,
        y=10.0,
        width=50.0,
        height=20.0,
        config_label="",
        has_error=False,
        bounds=(10.0, 10.0, 50.0, 20.0),
    )
    wire = TopologyWire(
        net="VDD",
        path_d="M 30,5 V 50",
        src_node="U1",
    )
    model = TopologyModel(nodes=[node], wires=[wire], width=100.0, height=100.0)
    issues = validate_topology(model)
    warn = next(i for i in issues if i["code"] == "vertical_under_node")
    assert warn["severity"] == "warning"
    assert "segment_through_foreign_node" not in {i["code"] for i in issues}
