"""Deterministic SVG normalization for topology snapshot tests."""

from __future__ import annotations

import re

DEFAULT_TEST_THEME = {
    "bg": "#2b2b2b",
    "bg_alt": "#333333",
    "fg": "#e6e6e6",
    "fg_dim": "#909090",
    "err": "#ff7070",
    "border": "#555555",
}


def _round_float(match: re.Match[str]) -> str:
    value = float(match.group(0))
    rounded = round(value, 1)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.1f}"


def normalize_topology_svg(svg: str) -> str:
    """Return a deterministic SVG string for snapshot comparison."""
    normalized = re.sub(r"\d+\.\d+", _round_float, svg)
    normalized = re.sub(r">\s+<", "><", normalized)
    normalized = normalized.strip()
    if not normalized.endswith("\n"):
        normalized += "\n"
    return normalized
