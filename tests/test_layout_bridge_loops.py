"""Regressions for mutual-loop parent resolution in layout_bridge.

A↔B mutual loops appear twice in ``loop_parent`` (both directions). The tie-break
must resolve them symmetrically and idempotently; a prior version deleted the
same key twice and raised ``KeyError`` when the node ids sorted differently as
strings than in natural order (e.g. R2/R10).
"""

from __future__ import annotations

import fypa.topology.metadata.layout_bridge as lb
from fypa.topology.metadata.layout_bridge import _resolve_mutual_loop_parents


def _spec(node_id: str) -> dict:
    return {"node_id": node_id, "terms": {}}


def test_mutual_loop_no_keyerror_regardless_of_dict_order():
    """R2/R10 sort differently as strings than naturally; neither ordering crashes."""
    specs = [_spec("R2"), _spec("R10")]
    for loop_parent in ({"R2": "R10", "R10": "R2"}, {"R10": "R2", "R2": "R10"}):
        resolved = _resolve_mutual_loop_parents(dict(loop_parent), specs, set(), {})
        # Exactly one direction survives -> a single acyclic parent link.
        assert len(resolved) == 1
        (child, parent), = resolved.items()
        # The root keeps no parent link of its own.
        assert parent not in resolved


def test_mutual_loop_root_choice_is_deterministic():
    """With no source rail, the lexicographically smaller id is the root."""
    specs = [_spec("R2"), _spec("R10")]
    # root = min("R2", "R10") = "R10" (popped) -> "R2" -> "R10" survives.
    assert _resolve_mutual_loop_parents(
        {"R2": "R10", "R10": "R2"}, specs, set(), {}
    ) == {"R2": "R10"}


def test_mutual_loop_source_rail_bridge_wins(monkeypatch):
    """The node fed from a SOURCE rail becomes the root regardless of id order."""
    specs = [_spec("A"), _spec("B")]
    monkeypatch.setattr(
        lb, "_has_source_rail_p_input", lambda spec, *a, **k: spec["node_id"] == "B"
    )
    # B is sourced -> B is root (its parent link removed) -> A -> B survives.
    resolved = _resolve_mutual_loop_parents({"A": "B", "B": "A"}, specs, set(), {})
    assert resolved == {"A": "B"}
