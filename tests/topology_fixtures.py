"""Committed topology metadata fixtures for CI."""

from __future__ import annotations

import json
from pathlib import Path

_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "topology"


def list_topology_fixtures() -> list[str]:
    """Basenames of JSON fixtures (without extension).

    ``*_rails`` fixtures are regression-only (column placement) and may
    have non-zero wiring-issue counts.

    ``hub_*`` fixtures are full-board hub routing regressions.
    """
    return sorted(
        p.stem
        for p in _FIXTURES_DIR.glob("*.json")
        if not p.stem.endswith("_rails")
        and not p.stem.endswith("_overlap")
        and not p.stem.startswith("hub_")
    )


def load_topology_fixture(name: str) -> dict:
    """Load a topology metadata dict from ``tests/fixtures/topology/{name}.json``."""
    path = _FIXTURES_DIR / f"{name}.json"
    if not path.is_file():
        raise FileNotFoundError(f"topology fixture not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def project_b_compact_metadata() -> dict:
    """Small compact layout (J1, U2, U1, R1)."""
    return load_topology_fixture("project_b_compact")
