"""SERIES/RESISTOR port-side orientation by downstream flow.

A bridge whose loads sit on one terminal and whose driver sits on the *other*
terminal must face its load terminal toward the loads (right), instead of the
static P-left/N-right default. But a mid-rail tap — driver and loads on the
same terminal — must keep the default, or the driver wire wraps the box.

Regression for the Methuselah design, where the source drives ``VOUT*_PRE``
(the resistor N net) and the sinks hang off ``DAC_SOA_VDD`` (the resistor P
net); the P ports were drawn on the left, forcing the rail to detour around
the whole diagram to reach the sinks.
"""

from __future__ import annotations

from fypa.topology import build_topology_model


def _term(net: str, pad: str, *, pin_net: str | None = None) -> dict:
    """A terminal on ``net``; ``pin_net`` overrides the physical pin net.

    Methuselah's SOURCE has ``requested_net`` = the rail but pins on the
    ``*_PRE`` bridge net, so the two differ — ``pin_net`` reproduces that.
    """
    return {"requested_net": net, "pins": [{"net": pin_net or net, "pad": pad}]}


def _resistor_sides(model) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for n in model.nodes:
        if n.role in ("RESISTOR", "SERIES"):
            out[n.designator] = {p.terminal: p.side for p in n.ports}
    return out


def test_bridge_flips_load_terminal_to_the_right():
    """Driver on N, loads on P → P faces right, N faces left."""
    meta = {
        "directives": [
            {"role": "SOURCE", "designator": "J1", "label": "J1", "value_str": "5 V",
             "terminals": {"P": _term("RAIL", "1", pin_net="PRE"), "N": _term("GND", "2")}},
            {"role": "RESISTOR", "designator": "R1", "label": "R1", "value_str": "0 mOhm",
             "terminals": {"P": _term("RAIL", "1"), "N": _term("PRE", "2")}},
            {"role": "SINK", "designator": "U1", "label": "U1", "value_str": "10 mA",
             "terminals": {"P": _term("RAIL", "1"), "N": _term("GND", "2")}},
            {"role": "SINK", "designator": "U2", "label": "U2", "value_str": "10 mA",
             "terminals": {"P": _term("RAIL", "1"), "N": _term("GND", "2")}},
        ]
    }
    sides = _resistor_sides(build_topology_model(meta))["R1"]
    assert sides["P"] == "right", "load terminal (P) should face the sinks"
    assert sides["N"] == "left", "driver terminal (N) should face the source"


def test_mid_rail_tap_keeps_default_orientation():
    """Driver AND loads on P (N dead-ends) → keep default P-left/N-right."""
    meta = {
        "directives": [
            {"role": "SOURCE", "designator": "J1", "label": "J1", "value_str": "5 V",
             "terminals": {"P": _term("RAIL", "1"), "N": _term("GND", "2")}},
            {"role": "RESISTOR", "designator": "R1", "label": "R1", "value_str": "0 mOhm",
             "terminals": {"P": _term("RAIL", "1"), "N": _term("PRE", "2")}},
            {"role": "SINK", "designator": "U1", "label": "U1", "value_str": "10 mA",
             "terminals": {"P": _term("RAIL", "1"), "N": _term("GND", "2")}},
        ]
    }
    sides = _resistor_sides(build_topology_model(meta))["R1"]
    assert sides["P"] == "left", "mid-rail tap must keep the P-left default"
    assert sides["N"] == "right"


def test_loop_series_ports_face_parent():
    """Loop child puts all channel ports on the parent-facing side; parent faces child."""
    from tests.topology_fixtures import load_topology_fixture

    model = build_topology_model(load_topology_fixture("rudder_stepper_loop_rails"))
    sides = _resistor_sides(model)
    j7 = sides["J7"]
    u1 = sides["U1"]
    assert j7["P1"] == "left"
    assert j7["N1"] == "left"
    assert j7["P2"] == "left"
    assert j7["N2"] == "left"
    assert u1["P1"] == "left", "source-rail P stays upstream"
    assert u1["N1"] == "right"
    assert u1["P2"] == "right"
    assert u1["N3"] == "right"
    assert u1["P4"] == "right"

    def _net_y(designator: str) -> dict[str, float]:
        node = next(n for n in model.nodes if n.designator == designator)
        return {p.net: p.y for p in node.ports}

    j7_y = _net_y("J7")
    u1_y = _net_y("U1")
    for net in ("AX", "AY", "BX", "BY"):
        assert u1_y[net] == j7_y[net], f"{net} must align between U1 and J7"

    u1_ports = next(n for n in model.nodes if n.designator == "U1").ports
    positions = {(p.x, p.y) for p in u1_ports}
    assert len(positions) == len(u1_ports), "U1 ports must not overlap"

    from fypa.topology.constants import MIN_PARALLEL_GAP
    from fypa.topology.geometry import parse_wire_path, path_to_segments
    from fypa.topology.validate.util import foreign_segments_cross

    sns_vertical_x: list[float] = []
    sns_segments: dict[str, list] = {}
    for w in model.wires:
        if w.net not in ("SNS_A", "SNS_B"):
            continue
        segs = path_to_segments(w.net, parse_wire_path(w.path_d))
        sns_segments[w.net] = segs
        for seg in segs:
            if seg.orient == "V":
                sns_vertical_x.append(seg.x1)
    assert len(sns_vertical_x) >= 2
    gap = abs(sns_vertical_x[0] - sns_vertical_x[1])
    assert gap >= MIN_PARALLEL_GAP - 0.6, f"SNS vertical corridors too close: {gap:.1f}px"

    assert not foreign_segments_cross(
        sns_segments["SNS_A"], sns_segments["SNS_B"]
    ), "SNS_A and SNS_B must not cross in the gutter"
