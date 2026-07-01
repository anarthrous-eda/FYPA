"""Committed topology metadata fixtures for CI."""

from __future__ import annotations

import json
from pathlib import Path

_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "topology"


def list_topology_fixtures() -> list[str]:
    """Basenames of JSON fixtures (without extension)."""
    return sorted(p.stem for p in _FIXTURES_DIR.glob("*.json"))


def load_topology_fixture(name: str) -> dict:
    """Load a topology metadata dict from ``tests/fixtures/topology/{name}.json``."""
    path = _FIXTURES_DIR / f"{name}.json"
    if not path.is_file():
        raise FileNotFoundError(f"topology fixture not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def front_like_metadata() -> dict:
    """Small Front-like layout (J1, U2, U1, R1)."""
    return load_topology_fixture("front_like")
