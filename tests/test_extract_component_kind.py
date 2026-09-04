"""ComponentKind is captured from PCB records, not just schematic ones.

A Net Tie can exist only on the PcbDoc — added by ECO, or a board opened
without its schematics — and the auto-bridge in
:mod:`fypa.altium.annotations` keys off ``component_kind`` on both sides.
"""

from __future__ import annotations

from enum import IntEnum

from fypa.altium.annotations import (
    COMPONENT_KIND_NET_TIE_BOM,
    COMPONENT_KIND_NET_TIE_NO_BOM,
)
from fypa.altium.extract import _component_kind_value, _extract_pcb_components


class _Kind(IntEnum):
    """Stand-in for altium_monkey's ComponentKind (an enum with .value)."""

    STANDARD = 0
    NET_TIE_BOM = COMPONENT_KIND_NET_TIE_BOM
    NET_TIE_NO_BOM = COMPONENT_KIND_NET_TIE_NO_BOM


class _StubPcbComponent:
    def __init__(self, designator: str, **extra):
        self.designator = designator
        self.x = "0mil"
        self.y = "0mil"
        self.rotation = "0"
        self.layer = "TOP"
        self.footprint = "NetTie2"
        self.raw_record = {"SOURCEDESIGNATOR": designator}
        self.parameters = {}
        self.unique_id = ""
        for key, value in extra.items():
            setattr(self, key, value)


class _StubPcb:
    def __init__(self, components):
        self.components = components


def test_pcb_component_kind_enum_is_captured():
    pcb = _StubPcb([
        _StubPcbComponent("NT1", component_kind=_Kind.NET_TIE_NO_BOM),
        _StubPcbComponent("R1", component_kind=_Kind.STANDARD),
    ])
    out = _extract_pcb_components(pcb, 0.0, 0.0)
    assert [c.component_kind for c in out] == [
        COMPONENT_KIND_NET_TIE_NO_BOM, 0,
    ]


def test_pcb_component_kind_defaults_to_standard_when_absent():
    """An altium_monkey build that does not expose the field must not crash."""
    pcb = _StubPcb([_StubPcbComponent("R1")])
    out = _extract_pcb_components(pcb, 0.0, 0.0)
    assert len(out) == 1
    assert out[0].component_kind == 0


def test_component_kind_value_accepts_enum_int_and_junk():
    assert _component_kind_value(_StubPcbComponent(
        "X", component_kind=_Kind.NET_TIE_BOM)) == COMPONENT_KIND_NET_TIE_BOM
    assert _component_kind_value(_StubPcbComponent(
        "X", component_kind=COMPONENT_KIND_NET_TIE_BOM)
    ) == COMPONENT_KIND_NET_TIE_BOM
    assert _component_kind_value(_StubPcbComponent("X")) == 0
    assert _component_kind_value(_StubPcbComponent(
        "X", component_kind=None)) == 0
    # A malformed record must degrade to STANDARD rather than abort the load.
    assert _component_kind_value(_StubPcbComponent(
        "X", component_kind="not-a-kind")) == 0
