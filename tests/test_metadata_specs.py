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


def test_regulator_merges_shared_power_and_return_ports():
    """Multi-channel regulators show one port per shared pad set (e.g. VDD_5V0, GND)."""
    directives = [
        {
            "role": "REGULATOR",
            "designator": "U4",
            "channel_index": 1,
            "terminals": {
                "IN_P": {"pins": [{"net": "VDD_5V0", "pad": "8"}, {"net": "VDD_5V0", "pad": "3"}]},
                "OUT_P": {"pins": [{"net": "V+", "pad": "11"}]},
                "IN_N": {"pins": [{"net": "GND", "pad": "4"}]},
                "OUT_N": {"pins": [{"net": "GND", "pad": "4"}]},
            },
        },
        {
            "role": "REGULATOR",
            "designator": "U4",
            "channel_index": 2,
            "terminals": {
                "IN_P": {"pins": [{"net": "VDD_5V0", "pad": "8"}, {"net": "VDD_5V0", "pad": "3"}]},
                "OUT_P": {"pins": [{"net": "GND", "pad": "4"}]},
                "IN_N": {"pins": [{"net": "GND", "pad": "4"}]},
                "OUT_N": {"pins": [{"net": "V-", "pad": "6"}]},
            },
        },
    ]
    specs = directives_to_component_specs(directives, [], {})
    pnames = [p[0] for p in specs[0]["port_defs"]]
    assert pnames.count("IN_P") == 1
    assert "IN_P1" not in pnames and "IN_P2" not in pnames
    assert pnames.count("IN_N") == 1
    assert "OUT_P1" in pnames or "OUT_P" in pnames
    assert sum(1 for p in pnames if p.startswith("OUT_P")) == 1
    assert pnames.count("OUT_N") == 1


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
