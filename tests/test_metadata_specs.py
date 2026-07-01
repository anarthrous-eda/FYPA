"""Tests for metadata component spec parsing."""

from __future__ import annotations

from fypa.topology.metadata.specs import directives_to_component_specs


def test_sink_multi_channel_port_names():
    directives = [
        {
            "role": "SINK",
            "designator": "U1",
            "channel_index": 1,
            "terminals": {
                "P": {"pins": [{"net": "VDD", "pad": "1"}]},
                "N": {"pins": [{"net": "GND", "pad": "2"}]},
            },
        },
        {
            "role": "SINK",
            "designator": "U1",
            "channel_index": 2,
            "terminals": {
                "P": {"pins": [{"net": "VDD", "pad": "3"}]},
                "N": {"pins": [{"net": "GND", "pad": "2"}]},
            },
        },
    ]
    specs = directives_to_component_specs(directives, [], {})
    assert len(specs) == 1
    pnames = [p[0] for p in specs[0]["port_defs"]]
    assert "P1" in pnames and "P2" in pnames
    assert pnames.count("N") == 1


def test_regulator_dedupes_shared_in_n():
    directives = [
        {
            "role": "REGULATOR",
            "designator": "U1",
            "channel_index": 1,
            "terminals": {
                "IN_N": {"pins": [{"net": "GND", "pad": "2"}]},
            },
        },
        {
            "role": "REGULATOR",
            "designator": "U1",
            "channel_index": 2,
            "terminals": {
                "IN_N": {"pins": [{"net": "GND", "pad": "2"}]},
            },
        },
    ]
    specs = directives_to_component_specs(directives, [], {})
    in_n = [p for p in specs[0]["port_defs"] if p[0] == "IN_N"]
    assert len(in_n) == 1


def test_passive_merge_p_when_shared_pad():
    directives = [
        {
            "role": "RESISTOR",
            "designator": "R1",
            "channel_index": 1,
            "terminals": {
                "P": {"pins": [{"net": "A", "pad": "1"}]},
                "N": {"pins": [{"net": "B", "pad": "2"}]},
            },
        },
        {
            "role": "RESISTOR",
            "designator": "R1",
            "channel_index": 2,
            "terminals": {
                "P": {"pins": [{"net": "A", "pad": "1"}]},
                "N": {"pins": [{"net": "C", "pad": "3"}]},
            },
        },
    ]
    specs = directives_to_component_specs(directives, [], {})
    pnames = [p[0] for p in specs[0]["port_defs"] if p[0].startswith("P")]
    assert pnames == ["P"]
    n_names = [p[0] for p in specs[0]["port_defs"] if p[0].startswith("N")]
    assert len(n_names) == 2
