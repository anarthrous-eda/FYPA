"""Lazy package init and import-light submodules."""

from __future__ import annotations


def test_topology_package_init_is_lightweight():
    import sys

    for name in (
        "fypa.topology.builder",
        "fypa.topology.layout",
        "fypa.topology.metadata.layout_bridge",
    ):
        sys.modules.pop(name, None)
    import fypa.topology  # noqa: F401

    assert "fypa.topology.builder" not in sys.modules


def test_rail_groups_import_without_cycle():
    from fypa.rail_groups import compute_rail_groups
    from fypa.topology.net_aliases import is_gnd_alias

    assert callable(compute_rail_groups)
    assert is_gnd_alias("GND")


def test_lazy_public_api():
    from fypa.topology import build_topology_model, TopologyModel

    assert callable(build_topology_model)
    assert TopologyModel is not None
