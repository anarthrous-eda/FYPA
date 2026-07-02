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
