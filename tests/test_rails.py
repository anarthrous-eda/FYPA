"""Rail list naming — canonical top-level net names in hierarchical designs."""

from fypa.altium.loader import build_net_canonical_map
from fypa.rail_groups import compute_rail_groups


def test_build_net_canonical_map_skips_alias_when_netlist_has_distinct_entry():
    """Spurious compiler alias must not fold a net that owns its own entry."""
    class _Net:
        def __init__(self, name, aliases=()):
            self.name = name
            self.aliases = list(aliases)

    class _Netlist:
        nets = [
            _Net("VDD_1V25D"),
            _Net("VDD_1V25A", aliases=["VDD_1V25D"]),
            _Net("VDD_1V25A"),
        ]

    m = build_net_canonical_map(_Netlist())
    assert m["VDD_1V25A"] == "VDD_1V25A"
    assert m["VDD_1V25D"] == "VDD_1V25D"


def test_build_net_canonical_map_skips_alias_when_pcb_has_distinct_net():
    """Schematic alias must not fold a PCB net that still exists by that name."""
    class _Net:
        def __init__(self, name, aliases=()):
            self.name = name
            self.aliases = list(aliases)

    class _Netlist:
        nets = [
            _Net("VDD_1V25D"),
            _Net("VDD_1V25A", aliases=["VDD_1V25D"]),
        ]

    m = build_net_canonical_map(
        _Netlist(),
        pcb_net_names={"VDD_1V25A", "VDD_1V25D"},
    )
    assert m["VDD_1V25A"] == "VDD_1V25A"
    assert m["VDD_1V25D"] == "VDD_1V25D"


def test_rail_groups_keep_split_pcb_nets_when_schematic_aliases_overlap():
    """Two regulator outputs on distinct PCB nets stay separate rails."""
    metadata = {
        "net_canonical": {
            "VDD_1V25A": "VDD_1V25A",
            "VDD_1V25D": "VDD_1V25D",
        },
        "directives": [
            {
                "role": "REGULATOR",
                "terminals": {
                    "OUT_P": {
                        "requested_net": "VDD_1V25D",
                        "pins": [{"net": "VDD_1V25D"}],
                    },
                    "OUT_N": {"requested_net": "GND", "pins": [{"net": "GND"}]},
                    "IN_P": {"requested_net": "VDD_12V", "pins": [{"net": "VDD_12V"}]},
                    "IN_N": {"requested_net": "GND", "pins": [{"net": "GND"}]},
                },
            },
            {
                "role": "REGULATOR",
                "terminals": {
                    "OUT_P": {
                        "requested_net": "VDD_1V25A",
                        "pins": [{"net": "VDD_1V25A"}],
                    },
                    "OUT_N": {"requested_net": "GND", "pins": [{"net": "GND"}]},
                    "IN_P": {"requested_net": "VDD_12V", "pins": [{"net": "VDD_12V"}]},
                    "IN_N": {"requested_net": "GND", "pins": [{"net": "GND"}]},
                },
            },
        ],
    }
    names, members = compute_rail_groups(metadata)
    assert "VDD_1V25D" in names
    assert "VDD_1V25A" in names
    assert members["VDD_1V25D"] == ["VDD_1V25D"]
    assert members["VDD_1V25A"] == ["VDD_1V25A"]


def test_build_net_canonical_map_maps_aliases_to_name():
    class _Net:
        def __init__(self, name, aliases=()):
            self.name = name
            self.aliases = list(aliases)

    class _Netlist:
        nets = [
            _Net("+5V", aliases=["VDD_5V", "5V_LOCAL"]),
            _Net("GND"),
        ]

    m = build_net_canonical_map(_Netlist())
    assert m["+5V"] == "+5V"
    assert m["VDD_5V"] == "+5V"
    assert m["5V_LOCAL"] == "+5V"
    assert m["GND"] == "GND"


def test_rail_groups_use_canonical_name_for_local_net_label():
    metadata = {
        "net_canonical": {
            "5V_LOCAL": "+5V",
            "+5V": "+5V",
        },
        "directives": [
            {
                "role": "SOURCE",
                "terminals": {
                    "P": {
                        "requested_net": "5V_LOCAL",
                        "resolved_via_local": True,
                        "pins": [{"net": "+5V"}],
                    },
                    "N": {
                        "requested_net": "GND",
                        "pins": [{"net": "GND"}],
                    },
                },
            },
        ],
    }
    names, members = compute_rail_groups(metadata)
    assert "+5V" in names
    assert "5V_LOCAL" not in names
    assert "+5V" in members["+5V"]


def test_rail_groups_keep_named_net_for_series_bridge():
    """A SINK naming GND that bridges to +DM_SW1 stays labelled GND."""
    metadata = {
        "directives": [
            {
                "role": "SINK",
                "terminals": {
                    "P": {
                        "requested_net": "+DM_SW1",
                        "pins": [{"net": "+DM_SW1"}],
                    },
                    "N": {
                        "requested_net": "GND",
                        "pins": [{"net": "+DM_SW1"}],
                    },
                },
            },
            {
                "role": "RESISTOR",
                "terminals": {
                    "P": {"pins": [{"net": "GND"}]},
                    "N": {"pins": [{"net": "+DM_SW1"}]},
                },
            },
        ],
    }
    names, members = compute_rail_groups(metadata)
    assert "GND" in names
    assert "+DM_SW1" in members["GND"]


def test_rail_groups_prefer_source_rail_over_bridged_led_nets():
    """project_b design: VDD_3V3_PWR (SOURCE) bridged to LED nets via SERIES."""
    metadata = {
        "net_canonical": {
            "VDD_3V3": "VDD_3V3_PWR",
            "VDD_3V3_PWR": "VDD_3V3_PWR",
        },
        "directives": [
            {
                "role": "SOURCE",
                "terminals": {
                    "P": {"requested_net": "VDD_3V3_PWR", "pins": [{"net": "VDD_3V3_PWR"}]},
                    "N": {"requested_net": "GND", "pins": [{"net": "GND"}]},
                },
            },
            {
                "role": "SINK",
                "terminals": {
                    "P": {"requested_net": "LED_R", "pins": [{"net": "LED_B"}, {"net": "LED_G"}, {"net": "LED_R"}]},
                    "N": {"requested_net": "GND", "pins": [{"net": "GND"}]},
                },
            },
            {
                "role": "RESISTOR",
                "terminals": {
                    "P": {"requested_net": "VDD_3V3", "resolved_via_local": True, "pins": [{"net": "VDD_3V3_PWR"}]},
                    "N": {"requested_net": "LED_R", "pins": [{"net": "LED_B"}, {"net": "LED_G"}, {"net": "LED_R"}]},
                },
            },
            {
                "role": "REGULATOR",
                "terminals": {
                    "OUT_P": {"requested_net": "VDD_1V8", "pins": [{"net": "VDD_1V8"}]},
                    "OUT_N": {"requested_net": "GND", "pins": [{"net": "GND"}]},
                    "IN_P": {"requested_net": "VDD_3V3_PWR", "pins": [{"net": "VDD_3V3_PWR"}]},
                    "IN_N": {"requested_net": "GND", "pins": [{"net": "GND"}]},
                },
            },
        ],
    }
    names, members = compute_rail_groups(metadata)
    assert "VDD_3V3_PWR" in names
    assert "VDD_1V8" in names
    assert "LED_B" not in names
    assert "LED_R" in members["VDD_3V3_PWR"]
    assert "VDD_3V3_PWR" in members["VDD_3V3_PWR"]
    assert members["VDD_1V8"] == ["VDD_1V8"]
