"""Auto-bridging 0 ohm links, and the unannotated-bridge diagnostic.

Two related behaviours, both about copper the solve would otherwise miss:

* A part that is electrically a piece of metal (an Altium Net Tie, a 0 ohm
  resistor, a wire link) is bridged automatically with a low-ohm
  ``ResistorSpec`` — the same object a hand-written ``PDN_ROLE=SERIES``
  produces, so it renders and behaves as a SERIES element everywhere
  downstream.
* A part that conducts but whose resistance only the user knows (a ferrite,
  a fuse, a shunt, a connector) is NOT bridged. Instead, when it joins a
  solved rail to a net no directive touches, the loader reports it so the
  user can decide whether to annotate it.
"""
from __future__ import annotations

import pytest

from fypa.altium.annotations import (
    _looks_like_jumper_footprint,
    _looks_like_zero_ohm_value,
    _zero_ohm_bridge_reason,
)
from fypa.altium.loader import _bridge_part_kind


# ---------------------------------------------------------------------------
# Value / footprint recognition
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "0", "0R", "0R0", "0 R", "0.0", "0,0", "0E0",
    "0 Ohm", "0OHM", "0ohms", "R0", "R00",
    "JUMPER", "jmp", "Link", "SHORT", "shunt", "ZERO",
    "0Ω", "0 Ω",          # ohm sign, both spacings
    "0Ω",                      # legacy ohm-sign code point
])
def test_zero_ohm_values_are_recognised(text):
    assert _looks_like_zero_ohm_value(text) is True


@pytest.mark.parametrize("text", [
    "10R", "0.01", "0R01", "0R1", "1R0", "4R7", "1k", "100", "0.1",
    "", "NC", "FERRITE", "DNP", "1000", "22uF",
])
def test_real_values_are_not_mistaken_for_links(text):
    """A false positive here silently shorts two nets, which is the one
    failure a PDN tool must not have — so anything with a non-zero
    magnitude must be rejected."""
    assert _looks_like_zero_ohm_value(text) is False


@pytest.mark.parametrize("footprint,expected", [
    ("JUMPER-0603", True),
    ("SolderBridge_2", True),
    ("WIRE_LINK", True),
    ("wirelink-5mm", True),
    ("R0603", False),
    ("SOIC-8", False),
    ("", False),
    # Net-tie footprints are deliberately excluded: ComponentKind is the
    # authoritative signal, so matching the land pattern too would bridge a
    # standard part that merely reuses it.
    ("NetTie2", False),
    ("NET_TIE_2", False),
])
def test_jumper_footprint_recognition(footprint, expected):
    assert _looks_like_jumper_footprint(footprint) is expected


def test_value_beats_footprint():
    """A part marked 10R in a footprint called JUMPER is a real resistor."""
    assert _zero_ohm_bridge_reason({"Value": "10R"}, "JUMPER-0603") is None


def test_zero_value_bridges_regardless_of_footprint():
    reason = _zero_ohm_bridge_reason({"Value": "0R"}, "R0603")
    assert reason is not None and "0R" in reason


def test_empty_value_falls_back_to_footprint():
    reason = _zero_ohm_bridge_reason({}, "JUMPER-0603")
    assert reason is not None and "JUMPER-0603" in reason


def test_no_value_and_ordinary_footprint_is_left_alone():
    assert _zero_ohm_bridge_reason({}, "R0603") is None


def test_value_keys_are_searched_in_priority_order():
    """Resistance wins over Value wins over Comment."""
    assert _zero_ohm_bridge_reason(
        {"Resistance": "0R", "Comment": "10k"}, "R0603") is not None
    assert _zero_ohm_bridge_reason(
        {"Resistance": "10k", "Comment": "0R"}, "R0603") is None


def test_value_key_match_is_case_insensitive():
    assert _zero_ohm_bridge_reason({"VALUE": "0R"}, "R0603") is not None
    assert _zero_ohm_bridge_reason({"value": "0R"}, "R0603") is not None


# ---------------------------------------------------------------------------
# Designator classification for the diagnostic sweep
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("designator,kind", [
    ("FB1", "ferrite bead"),
    ("JP1", "jumper"),
    ("R12", "resistor"),
    ("F3", "fuse"),
    ("L5", "inductor / ferrite"),
    ("J2", "connector"),
    ("P1", "connector"),
    ("CN4", "connector"),
    ("TP3", "test point"),
    ("SW1", "switch"),
])
def test_conducting_part_prefixes_are_classified(designator, kind):
    assert _bridge_part_kind(designator) == kind


@pytest.mark.parametrize("designator", [
    "U7",     # IC — spans nets but does not conduct between them at DC
    "C10",    # capacitor — open at DC
    "D4",     # diode
    "Y1",     # crystal
    "",
    "R",      # prefix with no instance number
    "FB",
    "X9",     # unknown prefix
])
def test_non_conducting_parts_are_ignored(designator):
    assert _bridge_part_kind(designator) is None


def test_longer_prefixes_win_over_shorter_ones():
    """FB/JP/CN must not be read as F/J/C."""
    assert _bridge_part_kind("FB1") == "ferrite bead"
    assert _bridge_part_kind("F1") == "fuse"
    assert _bridge_part_kind("JP1") == "jumper"
    assert _bridge_part_kind("J1") == "connector"
    assert _bridge_part_kind("CN1") == "connector"


def test_classification_is_case_insensitive():
    assert _bridge_part_kind("fb1") == "ferrite bead"
    assert _bridge_part_kind("r12") == "resistor"


# ---------------------------------------------------------------------------
# End-to-end: a 0 ohm link becomes a real SERIES directive
# ---------------------------------------------------------------------------

from pathlib import Path  # noqa: E402

from fypa.altium.annotations import (  # noqa: E402
    NET_TIE_BRIDGE_RESISTANCE_OHM,
    ResistorSpec,
    parse_annotations,
)
from fypa.altium.extract import (  # noqa: E402
    ExtractedProject,
    Pt2D,
    RawNet,
    RawPad,
    RawPcbComponent,
    RawSchComponent,
    RawStackupLayer,
)


def _stackup():
    return (RawStackupLayer(
        layer_id=1, name="Top", copper_thickness_mm=0.035,
        dielectric_thickness_mm=0.0, next_layer_id=0,
        is_plane=False, plane_net_name=None, mech_enabled=True,
    ),)


def _pad(comp_idx: int, pin: str, net_index: int, x: float = 0.0) -> RawPad:
    return RawPad(
        center=Pt2D(x, 0), width_mm=1, height_mm=1, hole_mm=0,
        shape=2, rotation_deg=0, layer_id=1, net_index=net_index,
        designator=pin, component_index=comp_idx,
        is_through_hole=False, is_smt=True,
    )


def _link_proj(value: str, footprint: str = "R0603",
               designator: str = "R9") -> ExtractedProject:
    """A single two-pin part straddling GND and AGND."""
    return ExtractedProject(
        prjpcb_path=Path("t.PrjPcb"), pcbdoc_path=Path("t.PcbDoc"),
        tracks=(), arcs=(), vias=(), regions=(), shape_based_regions=(),
        fills=(), stackup=_stackup(), compiled_netlist=None,
        nets=(RawNet("GND"), RawNet("AGND")),
        sch_components=(RawSchComponent(
            designator=designator, schdoc_name="Pwr.SchDoc",
            parameters={"Value": value} if value else {},
            pin_designators=("1", "2"),
        ),),
        pcb_components=(RawPcbComponent(
            designator=designator, center=Pt2D(0, 0), rotation_deg=0.0,
            layer_name="TOP", footprint=footprint,
            source_designator=designator,
        ),),
        pads=(_pad(0, "1", 0, 0.0), _pad(0, "2", 1, 1.0)),
    )


def _resistors(result):
    return [d for d in result.directives if isinstance(d, ResistorSpec)]


def test_zero_ohm_resistor_is_auto_bridged_as_series():
    result = parse_annotations(_link_proj("0R"), enabled_layers=[1])
    specs = _resistors(result)
    assert len(specs) == 1
    assert specs[0].designator == "R9"
    assert specs[0].resistance == NET_TIE_BRIDGE_RESISTANCE_OHM


def test_auto_bridged_link_is_the_same_spec_type_as_a_pdn_series():
    """It must be a ResistorSpec, because that is what makes the viewer
    label it SERIES and the loader merge the nets."""
    specs = _resistors(parse_annotations(_link_proj("0R"), enabled_layers=[1]))
    assert type(specs[0]) is ResistorSpec


def test_jumper_footprint_with_no_value_is_auto_bridged():
    result = parse_annotations(
        _link_proj("", footprint="JUMPER-0603"), enabled_layers=[1])
    assert len(_resistors(result)) == 1


def test_real_resistor_is_not_auto_bridged():
    assert _resistors(parse_annotations(_link_proj("10k"), enabled_layers=[1])) == []


def test_small_but_nonzero_resistor_is_not_auto_bridged():
    """0R01 is a 10 mohm shunt, not a link — shorting it would erase a real
    measurable drop."""
    assert _resistors(parse_annotations(_link_proj("0R01"), enabled_layers=[1])) == []


def test_auto_bridge_is_reported_not_silent():
    result = parse_annotations(_link_proj("0R"), enabled_layers=[1])
    notes = " ".join(result.warnings + result.infos + result.errors)
    assert "auto-bridged" in notes
    assert "R9" in notes


def test_explicit_pdn_role_overrides_auto_bridge():
    """A user directive always wins; the part must not be bridged twice."""
    proj = _link_proj("0R")
    proj.sch_components[0].parameters.update(
        {"PDN_ROLE": "SERIES", "PDN_R": "0.05"})
    specs = _resistors(parse_annotations(proj, enabled_layers=[1]))
    assert len(specs) == 1
    assert specs[0].resistance == pytest.approx(0.05)
