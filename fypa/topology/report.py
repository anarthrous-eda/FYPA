"""Wiring analysis and debug reports for topology models."""

from __future__ import annotations

import json
from collections import defaultdict

from fypa.topology.constants import DANGLING_END_TOLERANCE, WIRE_EPS
from fypa.topology.geometry import (
    WireSeg,
    compute_schematic_geometry,
    parse_wire_path,
    path_to_segments,
    point_record,
    segment_record,
    solid_wire_index_maps,
)
from fypa.topology.issues import make_issue
from fypa.topology.placement import port_stub_x
from fypa.topology.types import TopologyModel, TopologyPort, TopologyWire
from fypa.topology.validate import merge_validation_issues


def _port_ref(node_id: str, terminal: str) -> str:
    return f"{node_id}.{terminal}" if terminal else node_id


def _port_record(port: TopologyPort) -> dict:
    return {
        "ref": _port_ref(port.node_id, port.terminal),
        "node_id": port.node_id,
        "terminal": port.terminal,
        "net": port.net,
        "side": port.side,
        "x": round(port.x, 1),
        "y": round(port.y, 1),
        "stub_x": round(port_stub_x(port), 1),
        "is_power_input": port.is_power_input,
    }


def _nearest_port(
    ports: list[TopologyPort],
    x: float,
    y: float,
    *,
    net: str | None = None,
) -> tuple[TopologyPort | None, float]:
    best: TopologyPort | None = None
    best_d = float("inf")
    for p in ports:
        if net is not None and p.net != net:
            continue
        d = abs(p.x - x) + abs(p.y - y)
        if d < best_d:
            best_d = d
            best = p
    return best, best_d


def _segments_by_solid_index(geo_segments: list[WireSeg]) -> dict[int, list[WireSeg]]:
    by_index: dict[int, list[WireSeg]] = defaultdict(list)
    for seg in geo_segments:
        if seg.wire_index >= 0:
            by_index[seg.wire_index].append(seg)
    return by_index


def _analyze_wire_issues(
    wire: TopologyWire,
    wire_id: int,
    points: list[tuple[float, float]],
    segments: list[WireSeg],
    all_ports: list[TopologyPort],
) -> list[dict]:
    """Heuristic wiring checks for one logical wire path."""
    issues: list[dict] = []

    def _issue(code: str, message: str, **extra) -> None:
        issues.append(make_issue(
            code, message,
            wire_id=wire_id,
            net=wire.net,
            routing_kind=wire.routing_kind,
            **extra,
        ))

    if len(points) < 2:
        _issue("empty_path", "Wire path has fewer than two vertices")
        return issues

    sx, sy = points[0]
    ex, ey = points[-1]
    start_port, start_d = _nearest_port(all_ports, sx, sy, net=wire.net)
    end_port, end_d = _nearest_port(all_ports, ex, ey, net=wire.net)

    # Some wire kinds intentionally terminate on a trunk/rail, not a port.
    no_port_start = {"gnd_rail", "hub", "gnd_trunk", "hub_row", "hub_tap"}
    no_port_end = {"gnd_rail", "hub", "gnd_drop", "gnd_trunk", "gnd_tap", "hub_tap", "hub_row"}
    if wire.routing_kind not in no_port_start and start_d > DANGLING_END_TOLERANCE:
        _issue(
            "dangling_start",
            f"Path start ({sx:.1f},{sy:.1f}) is not on a {wire.net} port",
            at=point_record(sx, sy),
        )
    if wire.routing_kind not in no_port_end and end_d > DANGLING_END_TOLERANCE:
        _issue(
            "dangling_end",
            f"Path end ({ex:.1f},{ey:.1f}) is not on a {wire.net} port",
            at=point_record(ex, ey),
        )

    if wire.src_node and wire.dst_node:
        expected_start = _port_ref(wire.src_node, wire.src_terminal)
        expected_end = _port_ref(wire.dst_node, wire.dst_terminal)
        if start_port and _port_ref(start_port.node_id, start_port.terminal) != expected_start:
            _issue(
                "start_port_mismatch",
                f"Expected start {expected_start}, "
                f"nearest is {_port_ref(start_port.node_id, start_port.terminal)}",
                expected=expected_start,
                actual=_port_ref(start_port.node_id, start_port.terminal),
            )
        if end_port and _port_ref(end_port.node_id, end_port.terminal) != expected_end:
            _issue(
                "end_port_mismatch",
                f"Expected end {expected_end}, "
                f"nearest is {_port_ref(end_port.node_id, end_port.terminal)}",
                expected=expected_end,
                actual=_port_ref(end_port.node_id, end_port.terminal),
            )

    for seg in segments:
        if seg.length < WIRE_EPS and wire.routing_kind != "gnd_rail":
            _issue(
                "zero_length_segment",
                f"Degenerate {seg.orient} segment at "
                f"({seg.x1:.1f},{seg.y1:.1f})",
                segment=segment_record(seg),
            )

    for i in range(len(points) - 2):
        x0, y0 = points[i]
        x1, y1 = points[i + 1]
        x2, y2 = points[i + 2]
        if wire.routing_kind in ("hub", "hub_tap", "hub_row", "stack_column", "gnd_tap", "gnd_trunk"):
            continue
        if abs(y0 - y1) < WIRE_EPS and abs(y1 - y2) < WIRE_EPS:
            d1 = x1 - x0
            d2 = x2 - x1
            if d1 * d2 < 0 and abs(d1) > WIRE_EPS and abs(d2) > WIRE_EPS:
                _issue(
                    "horizontal_backtrack",
                    f"Horizontal backtrack at y={y1:.1f} "
                    f"({x0:.1f}→{x1:.1f}→{x2:.1f})",
                    at=point_record(x1, y1),
                    vertices=[point_record(x0, y0), point_record(x1, y1),
                              point_record(x2, y2)],
                )

    return issues


def topology_wiring_report(model: TopologyModel) -> dict:
    """Structured wiring analysis for debugging routing (JSON-serializable).

    Includes port positions, per-wire path/segment breakdown, schematic
    junctions and bridge crossings, and detected layout issues.
    """
    all_ports = [p for n in model.nodes for p in n.ports]
    geo = compute_schematic_geometry(
        model.wires,
        gnd_symbol_x=model.gnd_symbol_x,
        gnd_bus_y=model.gnd_bus_y,
    )
    _, solid_by_index = solid_wire_index_maps(model.wires)
    segs_by_solid = _segments_by_solid_index(geo.segments)

    junctions = [{"x": x, "y": y} for x, y in geo.junctions]
    crossings = [
        {
            "x": b.x,
            "y": b.y,
            "vertical_net": b.vertical_net,
            "horizontal_net": b.horizontal_net,
            "vertical_index": b.vertical_index,
            "bridged": True,
        }
        for b in geo.bridges
    ]

    wire_reports: list[dict] = []
    wire_issues: list[dict] = []

    for i, w in enumerate(model.wires):
        points = parse_wire_path(w.path_d)
        if w.dashed:
            segs = path_to_segments(w.net, points)
        else:
            segs = segs_by_solid.get(solid_by_index[i], [])
        issues = _analyze_wire_issues(w, i, points, segs, all_ports)
        wire_issues.extend(issues)

        start_port, _ = _nearest_port(all_ports, points[0][0], points[0][1], net=w.net)
        end_port, _ = _nearest_port(all_ports, points[-1][0], points[-1][1], net=w.net)

        wire_reports.append({
            "id": i,
            "net": w.net,
            "dashed": w.dashed,
            "routing_kind": w.routing_kind,
            "bus_x": round(w.bus_x, 1) if w.bus_x is not None else None,
            "expected": {
                "start": _port_ref(w.src_node, w.src_terminal) if w.src_node else None,
                "end": _port_ref(w.dst_node, w.dst_terminal) if w.dst_node else None,
            },
            "matched": {
                "start": (
                    _port_ref(start_port.node_id, start_port.terminal)
                    if start_port else None
                ),
                "end": (
                    _port_ref(end_port.node_id, end_port.terminal)
                    if end_port else None
                ),
            },
            "path_d": w.path_d,
            "vertices": [point_record(x, y) for x, y in points],
            "segments": [segment_record(s) for s in segs],
            "label": w.label or None,
            "label_x": round(w.label_x, 1) if w.label else None,
            "label_y": round(w.label_y, 1) if w.label else None,
            "label_vertical": w.label_vertical if w.label else None,
            "issues": issues,
        })

    all_issues = merge_validation_issues(model, wire_issues)

    return {
        "version": 1,
        "canvas": {
            "width": round(model.width, 1),
            "height": round(model.height, 1),
            "gnd_bus_y": (
                round(model.gnd_bus_y, 1) if model.gnd_bus_y is not None else None
            ),
            "gnd_symbol_x": (
                round(model.gnd_symbol_x, 1)
                if model.gnd_symbol_x is not None else None
            ),
        },
        "summary": {
            "nodes": sum(1 for n in model.nodes if n.role != "GND"),
            "ports": len(all_ports),
            "wires": len(model.wires),
            "segments": len(geo.segments),
            "junctions": len(junctions),
            "bridge_crossings": len(crossings),
            "issues": sum(
                1 for i in all_issues if i.get("severity", "error") != "warning"
            ),
        },
        "ports": [_port_record(p) for p in all_ports],
        "wires": wire_reports,
        "schematic": {
            "junctions": junctions,
            "bridge_crossings": crossings,
            "segments": [segment_record(s) for s in geo.segments],
        },
        "issues": all_issues,
    }


def topology_wiring_report_json(
    model: TopologyModel,
    *,
    indent: int = 2,
) -> str:
    """Pretty-printed JSON wiring report — suitable for diffing and LLM analysis."""
    return json.dumps(topology_wiring_report(model), indent=indent)
