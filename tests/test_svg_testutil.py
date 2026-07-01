"""Tests for SVG snapshot normalization."""

from __future__ import annotations

from fypa.topology.svg_testutil import normalize_topology_svg


def test_normalize_topology_svg_rounds_floats():
    raw = '<line x1="10.456" y1="20.789" x2="30.1" y2="40.0"/>'
    out = normalize_topology_svg(raw)
    assert 'x1="10.5"' in out
    assert 'y1="20.8"' in out
    assert 'x2="30.1"' in out
    assert 'y2="40"' in out or 'y2="40.0"' in out


def test_normalize_topology_svg_puts_each_element_on_its_own_line():
    raw = '<svg>  <rect/>  </svg>'
    assert normalize_topology_svg(raw) == '<svg>\n<rect/>\n</svg>\n'
