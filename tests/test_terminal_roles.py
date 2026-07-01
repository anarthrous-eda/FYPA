"""Tests for terminal role classification."""

from __future__ import annotations

import pytest

from fypa.topology.terminal_roles import is_output_port, is_power_input_port


@pytest.mark.parametrize(
    "role, terminal, expected",
    [
        ("SINK", "P", True),
        ("SINK", "P1", True),
        ("SINK", "N", False),
        ("REGULATOR", "IN_P", True),
        ("REGULATOR", "IN_P2", True),
        ("REGULATOR", "OUT_P", False),
        ("REGULATOR", "OUT_P1", False),
        ("SOURCE", "P", False),
        ("RESISTOR", "N", False),
    ],
)
def test_is_power_input_port(role: str, terminal: str, expected: bool) -> None:
    assert is_power_input_port(role, terminal) is expected


@pytest.mark.parametrize(
    "role, terminal, side, expected",
    [
        ("SINK", "P", "left", False),
        ("SINK", "P1", "left", False),
        ("REGULATOR", "IN_P", "left", False),
        ("REGULATOR", "IN_P2", "left", False),
        ("REGULATOR", "OUT_P", "right", True),
        ("REGULATOR", "OUT_P2", "right", True),
        ("SOURCE", "P", "right", True),
        ("SOURCE", "P2", "right", True),
        ("SOURCE", "N", "left", False),
        ("RESISTOR", "N", "right", True),
        ("RESISTOR", "N3", "right", True),
        ("RESISTOR", "P", "left", False),
        ("SERIES", "N", "right", True),
    ],
)
def test_is_output_port(role: str, terminal: str, side: str, expected: bool) -> None:
    assert is_output_port(role, terminal, side) is expected
