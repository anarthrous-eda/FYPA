"""Tests for the altium_monkey runtime hotspot patch."""

from __future__ import annotations

import fypa.altium.altium_monkey_patches as patches
from altium_monkey import SheetEntrySide, make_sch_sheet_entry
from altium_monkey.altium_netlist_single_sheet import AltiumNetlistSingleSheetCompiler


def test_fractional_sheet_entry_native_offset_differs_from_times_ten():
    entry = make_sch_sheet_entry(
        name="VRAIL_D",
        side=SheetEntrySide.LEFT,
        distance_from_top_mils=470.0,
    )
    assert entry.distance_from_top == 4
    assert entry.distance_from_top_frac1 == 700000
    assert round(entry._distance_from_top_native_units()) == 47
    assert entry.distance_from_top * 10 == 40


def test_apply_altium_monkey_patches_is_idempotent(monkeypatch):
    monkeypatch.setattr(patches, "_APPLIED", False)
    patches.apply_altium_monkey_patches()
    assert patches._APPLIED is True
    first = AltiumNetlistSingleSheetCompiler._extract_sheet_entries
    patches.apply_altium_monkey_patches()
    second = AltiumNetlistSingleSheetCompiler._extract_sheet_entries
    assert first is second
