"""Tests for topology metadata schema validation."""

from __future__ import annotations

import pytest

from fypa.topology.metadata_schema import assert_topology_metadata
from tests.topology_fixtures import list_topology_fixtures, load_topology_fixture


@pytest.mark.parametrize("name", list_topology_fixtures())
def test_topology_fixture_matches_schema(name: str) -> None:
    assert_topology_metadata(load_topology_fixture(name))


def test_assert_topology_metadata_rejects_bad_directives() -> None:
    with pytest.raises(TypeError, match="directives must be a list"):
        assert_topology_metadata({"directives": "not-a-list"})


def test_assert_topology_metadata_rejects_bad_directive_entry() -> None:
    with pytest.raises(TypeError, match="directives\\[0\\] must be a dict"):
        assert_topology_metadata({"directives": ["bad"]})
