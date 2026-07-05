"""IPC-4761 conductive-fill classification (round-2 finding).

Value 5 is TYPE_3A_PLUGGING, not a FILLING type (9/10/11/12) — a plugged via
must not receive the parallel conductive-fill rod even when its plug material
name matches a conductive keyword, or its barrel resistance is understated.
"""
from __future__ import annotations

from fypa.altium.loader import _IPC4761_FILL_TYPES, _is_conductive_fill


def test_plugging_type_5_is_not_a_fill():
    # Type 5 = IIIa plugging. Even with a conductive-looking material, it is
    # not a barrel fill and must classify non-conductive.
    assert 5 not in _IPC4761_FILL_TYPES
    assert _is_conductive_fill(5, "Copper") is False
    assert _is_conductive_fill(5, "Silver Epoxy") is False


def test_filling_types_with_conductive_material():
    # 9 = TYPE_5_FILLING, 10/11 = 6A/6B, 12 = TYPE_7 — all real fills.
    for t in (9, 10, 11, 12):
        assert t in _IPC4761_FILL_TYPES
        assert _is_conductive_fill(t, "Copper") is True
        assert _is_conductive_fill(t, "Silver Epoxy") is True


def test_filling_with_nonconductive_or_empty_material():
    assert _is_conductive_fill(9, "Non-Conductive Epoxy") is False
    assert _is_conductive_fill(9, "Polymer") is False
    assert _is_conductive_fill(9, "") is False


def test_unfilled_types_never_conductive():
    for t in (0, 1, 2, 6, 7, 8):  # none, tenting, plugging variants, covering
        assert _is_conductive_fill(t, "Copper") is False
