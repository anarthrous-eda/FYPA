"""Per-subnet rail visibility resolution."""

from fypa.rail_groups import resolve_rail_member_nets


_RAIL_TO_MEMBERS = {
    "+3V3": ["+3V3", "3V3_SW", "3V3_LDO"],
    "GND": ["GND"],
}


def test_resolve_all_members_when_no_subnet_filter():
    out = resolve_rail_member_nets(
        ["+3V3"],
        _RAIL_TO_MEMBERS,
        None,
        rail_only=False,
    )
    assert out == ["+3V3", "3V3_SW", "3V3_LDO"]


def test_resolve_subnet_filter_limits_members():
    subnet_visible = {
        "+3V3": {
            "+3V3": True,
            "3V3_SW": False,
            "3V3_LDO": True,
        },
    }
    out = resolve_rail_member_nets(
        ["+3V3"],
        _RAIL_TO_MEMBERS,
        subnet_visible,
        rail_only=False,
    )
    assert out == ["+3V3", "3V3_LDO"]


def test_resolve_rail_only_restricts_to_primary():
    subnet_visible = {
        "+3V3": {
            "+3V3": True,
            "3V3_SW": True,
            "3V3_LDO": True,
        },
    }
    out = resolve_rail_member_nets(
        ["+3V3"],
        _RAIL_TO_MEMBERS,
        subnet_visible,
        rail_only=True,
    )
    assert out == ["+3V3"]


def test_resolve_rail_only_with_subnet_off_primary():
    subnet_visible = {
        "+3V3": {
            "+3V3": False,
            "3V3_SW": True,
            "3V3_LDO": False,
        },
    }
    out = resolve_rail_member_nets(
        ["+3V3"],
        _RAIL_TO_MEMBERS,
        subnet_visible,
        rail_only=True,
    )
    assert out == []


def test_resolve_single_net_rail_ignores_subnet_map():
    subnet_visible = {"GND": {"GND": False}}
    out = resolve_rail_member_nets(
        ["GND"],
        _RAIL_TO_MEMBERS,
        subnet_visible,
        rail_only=False,
    )
    assert out == ["GND"]


def test_resolve_multiple_rails_deduplicates():
    members = {
        "A": ["A", "SHARED"],
        "B": ["B", "SHARED"],
    }
    out = resolve_rail_member_nets(["A", "B"], members, None, rail_only=False)
    assert out == ["A", "SHARED", "B"]


def test_resolve_partial_subnet_map_defaults_missing_to_visible():
    subnet_visible = {
        "+3V3": {
            "3V3_SW": False,
        },
    }
    out = resolve_rail_member_nets(
        ["+3V3"],
        _RAIL_TO_MEMBERS,
        subnet_visible,
        rail_only=False,
    )
    assert out == ["+3V3", "3V3_LDO"]
