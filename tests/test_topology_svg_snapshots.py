"""SVG snapshot regression tests for topology rendering."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fypa.topology import build_topology_model, render_topology_svg
from fypa.topology.svg_testutil import DEFAULT_TEST_THEME, normalize_topology_svg
from tests.topology_fixtures import load_topology_fixture

_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "topology" / "svg"
_SNAPSHOT_NAMES = (
    "project_b_hub_vdd",
    "project_b_compact",
    "column_gnd_feedback",
    "gnd_junction_tap",
    "gutter_parallel_four_nets",
)


@pytest.mark.parametrize("name", _SNAPSHOT_NAMES)
def test_topology_svg_snapshot(name: str) -> None:
    model = build_topology_model(load_topology_fixture(name))
    svg = normalize_topology_svg(
        render_topology_svg(model, theme=DEFAULT_TEST_THEME),
    )
    golden_path = _FIXTURES_DIR / f"{name}.svg"

    if os.environ.get("UPDATE_TOPOLOGY_SVG") == "1":
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(svg, encoding="utf-8")
        pytest.skip(f"updated golden {golden_path.name}")

    assert golden_path.is_file(), (
        f"missing golden SVG {golden_path}; run with UPDATE_TOPOLOGY_SVG=1"
    )
    golden = golden_path.read_text(encoding="utf-8")
    assert svg == golden
