"""Sheet-level PDN_IGNORE extraction mirrors altium_monkey SchDoc layout.

Real ``.SchDoc`` binaries are not checked in (anonymize / size). These fixtures
use the same type names and ownership links altium_monkey sets:
``AltiumSchPin._record_index`` ↔ ``AltiumSchParameter.owner_index``, plus the
optional ``pin.pin_parameters`` list after library attach.
"""
from __future__ import annotations

from types import SimpleNamespace

from fypa.altium.extract import (
    _extract_sch_component,
    _sheet_ignored_pins_by_component,
)


class AltiumSchPin:
    def __init__(self, designator, record_index, pin_parameters=None):
        self.designator = designator
        self._record_index = record_index
        self.pin_parameters = list(pin_parameters or [])
        self.owner_index = 0


class AltiumSchParameter:
    def __init__(self, name, text, owner_index):
        self.name = name
        self.text = text
        self.owner_index = owner_index


class AltiumSchDesignator:
    def __init__(self, text):
        self.text = text


def _sch_comp(designator: str, pins: list[AltiumSchPin]):
    return SimpleNamespace(
        children=[AltiumSchDesignator(designator), *pins],
        pins=pins,
        parameters={},
    )


def test_sheet_ignore_via_all_objects_owner_index_multi_component():
    """Two parts on one sheet; only the pin whose record owns PDN_IGNORE."""
    u1_en = AltiumSchPin("EN", record_index=10)
    u1_pwr = AltiumSchPin("1", record_index=11)
    u2_en = AltiumSchPin("EN", record_index=20)
    comps = [
        _sch_comp("U1", [u1_en, u1_pwr]),
        _sch_comp("U2", [u2_en]),
    ]
    all_objects = [
        comps[0],
        u1_en,
        u1_pwr,
        AltiumSchParameter("PDN_IGNORE", "1", owner_index=10),
        comps[1],
        u2_en,
        # Component-owned param must not mark U2's EN (owner is component idx 5
        # in a real file; here use an index that is not a pin record).
        AltiumSchParameter("PDN_IGNORE", "1", owner_index=99),
    ]
    ignored = _sheet_ignored_pins_by_component(comps, all_objects)
    assert ignored[0] == frozenset({"EN"})
    assert ignored[1] == frozenset()


def test_sheet_ignore_via_pin_parameters_and_owner_index_union():
    pin = AltiumSchPin(
        "EP",
        record_index=7,
        pin_parameters=[AltiumSchParameter("PDN", "IGNORE", owner_index=7)],
    )
    comps = [_sch_comp("U3", [pin])]
    all_objects = [
        AltiumSchParameter("PDN_IGNORE", "TRUE", owner_index=7),
    ]
    ignored = _sheet_ignored_pins_by_component(comps, all_objects)
    assert ignored[0] == frozenset({"EP"})


def test_extract_sch_component_carries_batch_ignored_pins():
    pin = AltiumSchPin("EN", record_index=3)
    comp = _sch_comp("U1", [pin])
    ignored = _sheet_ignored_pins_by_component(
        [comp],
        [AltiumSchParameter("PDN_IGNORE", "YES", owner_index=3)],
    )[0]
    rec = _extract_sch_component(comp, "Pwr.SchDoc", ignored)
    assert rec is not None
    assert rec.ignored_pins == frozenset({"EN"})
    assert "EN" in rec.pin_designators
