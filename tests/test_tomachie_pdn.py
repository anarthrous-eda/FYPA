# Copyright (c) 2026 Tomachie LLC.
"""PDN_* parameters from a Tomachie power-tree file (fypa.tomachie_pdn)."""

import copy
from dataclasses import dataclass

import pytest

from fypa.tomachie_pdn import PdnImportError, TomachiePdn, apply_to_project, dc_resistance

V = {"min": "not_specified", "max": "not_specified", "basis": "net_name"}

PDN = {
    "schema": "tomachie-pdn", "schema_version": 1, "spec_revision": 3,
    "ground_nets": ["GND"],
    "rails": [
        {"name": "VBUS", "voltage": {"nominal": 5.0, **V},
         "source": {"role": "board_input", "refdes": "J1", "pins": ["J1_A4", "J1_B9"],
                    "output_net": "VBUS", "output_path": []},
         "loads": [{"role": "regulator", "refdes": "U2", "pins": ["U2_2", "U2_3"]},
                   {"role": "regulator", "refdes": "U3", "pins": ["U3_1"]}],
         "offboard_pins": ["J2_1"]},
        {"name": "+3V3", "voltage": {"nominal": 3.3, **V},
         "source": {"role": "regulator", "refdes": "U2", "pins": ["U2_8"], "output_net": "SW",
                    "output_path": [
                        {"refdes": "L1", "class": "inductor", "net_in": "SW", "net_out": "+3V3",
                         "resistance_ohm": "unknown", "datasheet_figures": [
                             {"spec": "dcr", "typ": "64", "max": "68", "unit": "mΩ"},
                             {"spec": "impedance", "typ": "120", "unit": "ohm"}]}]},
         "loads": [{"role": "ic", "refdes": "U5", "pins": ["U5_7", "U5_8"],
                    "current": {"amps": 0.2}, "current_max": {"amps": 0.35},
                    "supply_min": {"volts": 3.0, "basis": "min", "spec": "vdd", "page": 4}},
                   {"role": "ic", "refdes": "U6", "pins": ["U6_1"], "current": "unknown",
                    "current_max": "unknown", "supply_min": "unknown"},
                   {"role": "resistive_path", "refdes": "R17", "pins": ["R17_2"],
                    "path_parts": ["R17", "D3"], "current": {"amps": 0.008,
                                                             "basis": "v_over_r"}},
                   {"role": "ic", "refdes": "U8", "pins": ["U8_VDD_IO"],
                    "current": {"amps": 0.001}}]},
        {"name": "+3V3A", "voltage": {"nominal": 3.3, **V},
         "source": {"role": "parent_rail"}, "parent_rail": "+3V3",
         "series_from_parent": [
             {"refdes": "FB1", "class": "ferrite", "net_in": "+3V3", "net_out": "+3V3A",
              "resistance_ohm": "unknown",
              "datasheet_figures": [{"spec": "dcr", "max": "0.08", "unit": "ohm"}]},
             {"refdes": "R9", "class": "resistor", "net_in": "+3V3", "net_out": "+3V3A",
              "resistance_ohm": 0.0, "datasheet_figures": []},
             # Not in any regulation loop: its DCR is a real drop, typ or max by corner.
             {"refdes": "FB2", "class": "ferrite", "net_in": "+3V3", "net_out": "+3V3A",
              "resistance_ohm": "unknown",
              "datasheet_figures": [{"spec": "dcr", "typ": "45", "max": "60", "unit": "mΩ"},
                                    {"spec": "impedance", "typ": "600", "unit": "ohm"}]}],
         "loads": [{"role": "ic", "refdes": "U7", "pins": ["U7_3"],
                    "current": {"amps": 0.01}, "current_max": {"amps": 0.02}}]},
        {"name": "+1V8", "voltage": {"nominal": 1.8, **V},
         "source": {"role": "regulator", "refdes": "U3", "pins": ["U3_5"], "output_net": "+1V8",
                    "output_path": []},
         "loads": []},
    ],
    "regulators": [
        {"refdes": "U2", "type": "switching", "efficiency": 0.88,
         "input_rails": ["VBUS"], "output_rails": ["+3V3"]},
        {"refdes": "U3", "type": "linear", "input_rails": ["VBUS"], "output_rails": ["+1V8"]},
    ],
}


def params(corner="typ", pdn=PDN):
    return TomachiePdn(copy.deepcopy(pdn), corner).parameters()


def test_source_on_the_connector_pads():
    p, _ = params()
    assert p["J1"] == {"PDN1_ROLE": "SOURCE", "PDN1_V": "5", "PDN1_P_NET": "VBUS",
                       "PDN1_N_NET": "GND", "PDN1_P_PINS": "A4,B9"}


def test_switcher_regulates_on_its_switch_node_with_the_inductor_in_series():
    p, notes = params()
    u2 = p["U2"]
    assert (u2["PDN1_ROLE"], u2["PDN1_REGULATOR_TYPE"], u2["PDN1_REGULATOR_EFFICIENCY"]) == \
        ("REGULATOR", "SMPS", "0.88")
    assert (u2["PDN1_OUT_P_NET"], u2["PDN1_IN_P_NET"], u2["PDN1_IN_P_PINS"]) == ("SW", "VBUS", "2,3")
    l1 = p["L1"]
    assert (l1["PDN1_ROLE"], l1["PDN1_P_NET"], l1["PDN1_N_NET"]) == ("SERIES", "SW", "+3V3")
    # The output inductor is inside U2's regulation loop (feedback after it), so
    # it is bridged at 1 mOhm - above the 0.9 mOhm net-merge threshold - and its
    # DCR (64 mOhm) is not a rail drop; its loss is in the efficiency.
    assert float(l1["PDN1_R"]) == pytest.approx(0.001)
    assert any("L1: inside U2's regulation loop; bridged" in n for n in notes)


def test_ldo():
    p, _ = params()
    assert p["U3"]["PDN1_REGULATOR_TYPE"] == "LDO" and "PDN1_REGULATOR_EFFICIENCY" not in p["U3"]


def test_sink_with_datasheet_minimum():
    p, _ = params()
    assert p["U5"] == {"PDN1_ROLE": "SINK", "PDN1_I": "0.2", "PDN1_P_NET": "+3V3",
                       "PDN1_N_NET": "GND", "PDN1_P_PINS": "7,8", "PDN1_MIN_V": "3"}


def test_worst_case_corner_takes_the_maximums():
    p, _ = params("max")
    assert p["U5"]["PDN1_I"] == "0.35"
    # A switcher's output inductor stays bridged in the worst case too: its
    # maximum DCR (68 mOhm) is inside the regulation loop as well.
    assert float(p["L1"]["PDN1_R"]) == pytest.approx(0.001)


def test_series_dcr_typical_nominally_maximum_in_the_worst_case():
    nominal, _ = params()
    worst, _ = params("max")
    # DCR, never the impedance; typical in the nominal corner, maximum in the worst.
    assert float(nominal["FB2"]["PDN1_R"]) == pytest.approx(0.045)
    assert float(worst["FB2"]["PDN1_R"]) == pytest.approx(0.060)
    assert (nominal["FB2"]["PDN1_P_NET"], nominal["FB2"]["PDN1_N_NET"]) == ("+3V3", "+3V3A")


def test_ferrite_branch_and_zero_ohm_link():
    p, _ = params()
    assert p["FB1"]["PDN1_R"] == "0.08"           # only a maximum printed
    assert "R9" not in p                           # 0 ohm: FYPA's own auto-bridge


def test_what_cannot_be_set_is_said():
    p, notes = params()
    assert "U6" not in p
    assert any("U6 on +3V3: current not known" in n for n in notes)
    assert any("J2_1" in n and "lower bound" in n for n in notes)


def test_switcher_without_efficiency_is_said():
    pdn = copy.deepcopy(PDN)
    pdn["regulators"][0]["efficiency"] = "unknown"
    p, notes = params(pdn=pdn)
    assert "PDN1_REGULATOR_EFFICIENCY" not in p["U2"]
    assert any("U2 (+3V3): efficiency not read" in n for n in notes)


def test_wrong_schema_and_corner_are_refused():
    with pytest.raises(PdnImportError):
        TomachiePdn({"schema": "x"})
    with pytest.raises(PdnImportError):
        TomachiePdn(copy.deepcopy(PDN), "worst")


def test_dc_resistance_never_takes_an_impedance():
    assert dc_resistance({"resistance_ohm": "unknown", "datasheet_figures": [
        {"spec": "impedance_at_frequency", "typ": "330", "unit": "ohm"}]}) == (None, None)


@dataclass(frozen=True)
class _FakeProject:
    """apply_to_project reads and replaces only sch_components."""
    sch_components: tuple


def test_designer_annotation_wins_and_missing_parts_are_noted():
    from fypa.altium.extract import RawSchComponent
    comps = (RawSchComponent("U5", "a.SchDoc", {"PDN_ROLE": "SINK", "PDN_I": "1"}, ("7",)),
             RawSchComponent("J1", "a.SchDoc", {"Value": "USB-C"}, ("A4",)))
    p, _ = params()
    out, notes = apply_to_project(_FakeProject(comps), p)
    by = {c.designator: c.parameters for c in out.sch_components}
    assert by["U5"] == {"PDN_ROLE": "SINK", "PDN_I": "1"}
    assert by["J1"]["PDN1_ROLE"] == "SOURCE" and by["J1"]["Value"] == "USB-C"
    assert any("U5: keeps its own" in n for n in notes)
    assert any("U2: in the file but not in the schematic" in n for n in notes)


def test_resistive_path_returns_through_its_last_part():
    p, _ = params()
    assert p["R17"] == {"PDN1_ROLE": "SINK", "PDN1_I": "0.008", "PDN1_P_NET": "+3V3",
                        "PDN1_N_NET": "GND", "PDN1_P_PINS": "2", "PDN1_N_DES": "D3"}


def test_pad_name_with_an_underscore_survives():
    p, _ = params()
    assert p["U8"]["PDN1_P_PINS"] == "VDD_IO"


def test_regulator_quiescent_current():
    pdn = copy.deepcopy(PDN)
    pdn["regulators"][1]["quiescent_current"] = {"amps": 5.5e-5, "basis": "typ"}
    p, _ = params(pdn=pdn)
    assert p["U3"]["PDN1_QUIESCENT"] == "5.5e-05"


def test_project_named_in_the_file_is_found_up_to_two_folders_above(tmp_path):
    from fypa.viewer.tomachie_menu import _project_for
    prj = tmp_path / "board.PrjPcb"
    prj.write_text("")
    pdn = tmp_path / "output" / "enrich" / "board.pdn.json"
    pdn.parent.mkdir(parents=True)
    assert _project_for(None, pdn, {"project_file": "board.PrjPcb"}) == prj


def test_nothing_is_set_on_a_rail_nothing_drives():
    pdn = copy.deepcopy(PDN)
    pdn["rails"][0]["source"]["pins"] = []          # VBUS has no source now
    p, notes = params(pdn=pdn)
    # VBUS undriven -> U2's +3V3 and U3's +1V8 undriven -> +3V3A (behind FB1) too.
    for ref in ("U2", "U3", "U5", "U7", "R17", "L1", "FB1"):
        assert ref not in p, ref
    assert any("Rail +3V3: nothing in the file drives it" in n for n in notes)
    assert any("U2 (+3V3): nothing in the file drives its input (VBUS)" in n for n in notes)


def test_a_file_written_for_another_project_is_noticed():
    from pathlib import Path
    from fypa.viewer.tomachie_menu import written_for_another_project
    pdn = {"project_file": "board.PrjPcb"}
    assert written_for_another_project(pdn, Path("x/BOARD.PrjPcb")) is None
    assert written_for_another_project(pdn, Path("x/other.PrjPcb")) == "board.PrjPcb"
    assert written_for_another_project({}, Path("x/other.PrjPcb")) is None


SWITCH_NODE = {"net": "SW", "pins": ["U2_8"], "rectifier": "diode",
               "freewheel_diode": {"refdes": "D9", "part_number": "SS34",
                                   "anode_pins": ["D9_2"], "cathode_pins": ["D9_1"],
                                   "anode_net": "GND"}}


def _with_switch_node(switch_node):
    pdn = copy.deepcopy(PDN)
    pdn["spec_revision"] = 6
    pdn["regulators"][0]["switch_node"] = switch_node
    return pdn


def test_non_synchronous_buck_puts_its_freewheel_diode_on_the_switch_node():
    p, notes = params(pdn=_with_switch_node(SWITCH_NODE))
    # D = output rail nominal / input rail nominal = 3.3 V / 5 V.
    assert p["D9"] == {"PDN1_ROLE": "FREEWHEEL", "PDN1_DUTY": "0.66", "PDN1_SMPS_HOST": "U2",
                       "PDN1_P_NET": "GND", "PDN1_N_NET": "SW", "PDN1_P_PINS": "2",
                       "PDN1_N_PINS": "1"}
    assert p["U2"]["PDN1_OUT_P_PINS"] == "8"
    assert any("D9: freewheel diode of U2" in n for n in notes)


def test_synchronous_or_absent_switch_node_changes_nothing():
    before, _ = params()
    sync, _ = params(pdn=_with_switch_node(dict(SWITCH_NODE, rectifier="synchronous")))
    assert sync == before and "D9" not in sync


def test_diode_switch_node_without_its_pads_is_said_and_not_guessed():
    partial = copy.deepcopy(SWITCH_NODE)
    del partial["freewheel_diode"]["anode_pins"]
    before, _ = params()
    p, notes = params(pdn=_with_switch_node(partial))
    assert p == before
    assert any("U2 (+3V3): non-synchronous" in n and "anode_pins" in n for n in notes)


def test_rectifier_to_the_output_or_unknown_changes_nothing():
    before, _ = params()
    for rect in ("diode_to_output", "something_new"):
        sn = {"net": "SW", "pins": ["U2_8"], "rectifier": rect}
        p, _ = params(pdn=_with_switch_node(sn))
        assert p == before, rect


def test_source_return_from_the_file():
    pdn = copy.deepcopy(PDN)
    pdn["ground_nets"] = ["GND", "V-"]
    pdn["rails"][0]["source"]["return"] = {
        "pins": ["J1_A1"], "net": "V-", "ground_net": "GND",
        "path": [{"refdes": "L9", "class": "inductor", "net_in": "GND", "net_out": "V-",
                  "resistance_ohm": "unknown",
                  "datasheet_figures": [{"spec": "dcr", "max": "0.002", "unit": "ohm"}]}]}
    p, _ = params(pdn=pdn)
    j1 = p["J1"]
    assert (j1["PDN1_N_NET"], j1["PDN1_N_PINS"]) == ("V-", "A1")
    assert p["L9"] == {"PDN1_ROLE": "SERIES", "PDN1_R": "0.002", "PDN1_P_NET": "GND",
                       "PDN1_N_NET": "V-"}


def _with_load_switch(**reg):
    """VBUS -> U4 (a USB power switch, 5 V in, 5 V out) -> U4_OUT -> FB4 -> VBUS_OUT -> U9."""
    pdn = copy.deepcopy(PDN)
    pdn["spec_revision"] = 6
    pdn["rails"][0]["loads"].append({"role": "regulator", "refdes": "U4", "pins": ["U4_2", "U4_3"]})
    pdn["rails"].append(
        {"name": "VBUS_OUT", "voltage": {"nominal": 5.0, **V},
         "source": {"role": "regulator", "refdes": "U4", "pins": ["U4_6", "U4_7"],
                    "output_net": "U4_OUT", "output_path": [
                        {"refdes": "FB4", "class": "ferrite", "net_in": "U4_OUT",
                         "net_out": "VBUS_OUT", "resistance_ohm": "unknown",
                         "datasheet_figures": [{"spec": "dcr", "typ": "50", "max": "80",
                                                "unit": "mΩ"}]}]},
         "loads": [{"role": "ic", "refdes": "U9", "pins": ["U9_1"], "current": {"amps": 0.5},
                    "current_max": {"amps": 0.9}}]})
    pdn["regulators"].append({"refdes": "U4", "type": "load_switch", "type_basis": "schematic",
                              "input_rails": ["VBUS"], "output_rails": ["VBUS_OUT"], **reg})
    return pdn


def test_load_switch_is_a_series_part_with_its_on_resistance():
    figs = [{"spec": "rds_on", "typ": "89", "max": "120", "unit": "mΩ", "page": 4}]
    pdn = _with_load_switch(datasheet_resistances=figs,
                            quiescent_current={"amps": 9e-5, "basis": "typ"})
    nominal, notes = params(pdn=pdn)
    worst, _ = params("max", pdn=pdn)
    u4 = nominal["U4"]
    # A SERIES from its input pins to its output pins, never a REGULATOR (an LDO
    # making 5 V from 5 V is out of regulation).
    assert {k: u4[k] for k in u4 if k.startswith("PDN1_")} == {
        "PDN1_ROLE": "SERIES", "PDN1_R": "0.089", "PDN1_P_NET": "VBUS",
        "PDN1_N_NET": "U4_OUT", "PDN1_P_PINS": "2,3", "PDN1_N_PINS": "6,7"}
    assert float(worst["U4"]["PDN1_R"]) == pytest.approx(0.120)
    # Its quiescent current is drawn at its input, as an LDO's is.
    assert (u4["PDN2_ROLE"], u4["PDN2_I"], u4["PDN2_P_NET"], u4["PDN2_P_PINS"]) == \
        ("SINK", "9e-05", "VBUS", "2,3")
    assert not any(v == "REGULATOR" for v in u4.values())
    # The ferrite after it is a real drop (no regulation loop): its DCR, by corner.
    assert (nominal["FB4"]["PDN1_P_NET"], nominal["FB4"]["PDN1_N_NET"]) == ("U4_OUT", "VBUS_OUT")
    assert (float(nominal["FB4"]["PDN1_R"]), float(worst["FB4"]["PDN1_R"])) ==         (pytest.approx(0.050), pytest.approx(0.080))
    assert nominal["U9"]["PDN1_P_NET"] == "VBUS_OUT"     # the rail behind it is driven
    assert any("U4: load switch, a SERIES" in n for n in notes)


def test_load_switch_without_an_on_resistance_is_bridged_and_said():
    p, notes = params(pdn=_with_load_switch())
    assert p["U4"]["PDN1_ROLE"] == "SERIES" and float(p["U4"]["PDN1_R"]) == pytest.approx(0.001)
    assert "PDN2_ROLE" not in p["U4"]                    # no quiescent current in the file
    assert any("U4: load switch, on-resistance not in the file; bridged at 1 mOhm" in n
               for n in notes)


def _board(parts):
    """A stand-in for the extracted project: ``{designator: [net of each pad]}``."""
    from types import SimpleNamespace as NS
    nets = sorted({n for pads in parts.values() for n in pads})
    comps = [NS(designator=d, source_designator=d) for d in parts]
    pads = [NS(component_index=i, net_index=nets.index(n))
            for i, d in enumerate(parts) for n in parts[d]]
    return NS(pads=pads, pcb_components=comps, net_name=lambda i: nets[i])


def test_input_filter_parts_take_their_dc_resistance_from_the_file():
    """VBUS -> C9 (feedthrough, no figure) -> VBUS_F1 -> FB5 || FB6 -> VBUS_F2 -> U2."""
    ferrite = {"class": "ferrite", "resistance_ohm": "unknown",
               "datasheet_figures": [{"spec": "dcr", "typ": "40", "max": "50", "unit": "mΩ"},
                                     {"spec": "impedance", "typ": "600", "unit": "ohm"}]}
    pdn = copy.deepcopy(PDN)
    pdn["spec_revision"] = 6
    pdn["rails"][0]["loads"][0].update(input_net="VBUS_F2", input_filter=[
        {"refdes": "C9", "class": "capacitor", "net_in": "VBUS", "net_out": "VBUS_F1",
         "resistance_ohm": "unknown", "datasheet_figures": []},
        dict(ferrite, refdes="FB5", net_in="VBUS_F1", net_out="VBUS_F2"),
        dict(ferrite, refdes="FB6", net_in="VBUS_F1", net_out="VBUS_F2")])
    board = _board({"C9": ["VBUS", "VBUS_F1", "GND"], "FB5": ["VBUS_F1", "VBUS_F2"],
                    "FB6": ["VBUS_F1", "VBUS_F2"], "U2": ["VBUS_F2", "SW", "GND"],
                    "J1": ["VBUS", "GND"]})
    for corner, r in (("typ", 0.040), ("max", 0.050)):
        reader = TomachiePdn(copy.deepcopy(pdn), corner)
        p, _ = reader.parameters()
        notes = reader.place_on_board(p, board)
        # Each ferrite its DCR, never its impedance; the parallel one too.
        for fb in ("FB5", "FB6"):
            assert (p[fb]["PDN1_P_NET"], p[fb]["PDN1_N_NET"]) == ("VBUS_F1", "VBUS_F2")
            assert float(p[fb]["PDN1_R"]) == pytest.approx(r), (corner, fb)
        # The feedthrough capacitor has no resistance figure: bridged and named.
        assert float(p["C9"]["PDN1_R"]) == pytest.approx(0.001)
        assert any(n.startswith("C9: DC resistance not in the file; bridged") for n in notes)
        assert not any(n.startswith("FB5: DC resistance not in the file") for n in notes)


def test_return_named_on_another_ground_with_no_path_is_closed_from_the_board():
    """An input connector J3: the file names its return V- with an empty path; every regulator
    returns to GND, so V- is joined to GND through the choke's second winding."""
    pdn = copy.deepcopy(PDN)
    pdn["ground_nets"] = ["GND", "V-"]
    pdn["rails"][0]["source"]["return"] = {"pins": ["J1_A1"], "net": "V-", "path": [],
                                           "ground_net": "V-"}
    pdn["rails"][2]["series_from_parent"].append(
        {"refdes": "L9", "class": "inductor", "net_in": "+3V3", "net_out": "+3V3A",
         "resistance_ohm": "unknown",
         "datasheet_figures": [{"spec": "dcr", "max": "0.002", "unit": "ohm"}]})
    reader = TomachiePdn(pdn, "typ")
    p, _ = reader.parameters()
    notes = reader.place_on_board(p, _board({"J1": ["VBUS", "V-"], "L9": ["V-", "GND"]}))
    assert p["J1"]["PDN1_N_NET"] == "V-"
    winding = {k[5:]: v for k, v in p["L9"].items() if k.startswith("PDN2_")}
    assert winding == {"ROLE": "SERIES", "R": "0.002", "P_NET": "V-", "N_NET": "GND"}
    assert any("L9: return winding V- -> GND" in n for n in notes)


def test_rails_sourced_from_the_same_output_pins_are_one_regulator():
    """U9_6 sources DVDD12 and, behind L7, AVDD12: one REGULATOR, not two in
    parallel (the report counted the shared load once per channel)."""
    pdn = copy.deepcopy(PDN)
    pdn["rails"].append(
        {"name": "+1V8A", "voltage": {"nominal": 1.8, **V},
         "source": {"role": "regulator", "refdes": "U3", "pins": ["U3_5"], "output_net": "+1V8",
                    "output_path": [{"refdes": "L7", "class": "ferrite", "net_in": "+1V8",
                                     "net_out": "+1V8A", "resistance_ohm": "unknown",
                                     "datasheet_figures": [{"spec": "dcr", "max": "0.1",
                                                            "unit": "ohm"}]}]},
         "loads": []})
    pdn["regulators"][1]["output_rails"] = ["+1V8", "+1V8A"]
    p, notes = params(pdn=pdn)
    assert [k for k in p["U3"] if k.endswith("_ROLE")] == ["PDN1_ROLE"]
    assert (p["L7"]["PDN1_P_NET"], p["L7"]["PDN1_N_NET"], p["L7"]["PDN1_R"]) == \
        ("+1V8", "+1V8A", "0.1")
    assert any("U3 (+1V8A): the same output pins as its REGULATOR for +1V8" in n for n in notes)


def test_worst_case_drives_a_source_at_its_minimum_voltage():
    """Spec: a worst-case solve drives each source at voltage.min (e.g. V+: 48 V
    nominal, 21 V min); a regulator output stays at its set voltage, and a buck's
    duty cycle follows the lower input."""
    pdn = _with_switch_node(SWITCH_NODE)
    pdn["rails"][0]["voltage"] = {"nominal": 5.0, "min": 4.0, "max": 5.5, "basis": "user"}
    nominal, _ = params(pdn=pdn)
    worst, notes = params("max", pdn=pdn)
    assert (nominal["J1"]["PDN1_V"], worst["J1"]["PDN1_V"]) == ("5", "4")
    assert worst["U2"]["PDN1_V"] == "3.3" and worst["U3"]["PDN1_V"] == "1.8"
    assert (float(nominal["D9"]["PDN1_DUTY"]), float(worst["D9"]["PDN1_DUTY"])) == \
        (pytest.approx(3.3 / 5.0), pytest.approx(3.3 / 4.0))
    assert any("Rail VBUS: worst case, source J1 at its minimum 4 V" in n for n in notes)
    # A minimum that is not a number leaves the source at its nominal.
    plain, _ = params("max")
    assert plain["J1"]["PDN1_V"] == "5"


def _with_isolated(**reg):
    """VBUS -> U4 (isolated DC/DC, UCC12040-like) -> ISO5 / GND_ISO -> U9."""
    pdn = copy.deepcopy(PDN)
    pdn["spec_revision"] = 6
    pdn["ground_nets"] = ["GND", "GND_ISO"]
    pdn["rails"][0]["loads"].append({"role": "regulator", "refdes": "U4", "pins": ["U4_1"]})
    pdn["rails"].append(
        {"name": "ISO5", "voltage": {"nominal": 5.0, **V},
         "source": {"role": "regulator", "refdes": "U4", "pins": ["U4_8"],
                    "output_net": "ISO5", "output_path": []},
         "loads": [{"role": "ic", "refdes": "U9", "pins": ["U9_1"], "current": {"amps": 0.05}},
                   {"role": "resistive_path", "refdes": "R20", "pins": ["R20_1"],
                    "path_parts": ["R20", "R21"], "current": {"amps": 0.001}}]})
    pdn["regulators"].append({"refdes": "U4", "type": "isolated", "type_basis": "construction",
                              "input_rails": ["VBUS"], "output_rails": ["ISO5"], **reg})
    return pdn


ISO_BOARD = {"J1": ["VBUS", "GND"], "U4": ["VBUS", "GND", "ISO5", "GND_ISO"],
             "U9": ["ISO5", "GND_ISO"], "U2": ["VBUS", "SW", "GND"], "L1": ["SW", "+3V3"],
             "R20": ["ISO5", "MID"], "R21": ["MID", "GND_ISO"]}


def _iso(pdn):
    reader = TomachiePdn(pdn, "typ")
    p, notes = reader.parameters()
    return p, notes + reader.place_on_board(p, _board(ISO_BOARD))


def test_isolated_converter_returns_on_its_output_side_ground():
    p, notes = _iso(_with_isolated(efficiency=0.8, quiescent_current={"amps": 0.05}))
    u4 = p["U4"]
    assert (u4["PDN1_ROLE"], u4["PDN1_REGULATOR_TYPE"], u4["PDN1_V"]) == ("REGULATOR", "SMPS", "5")
    assert (u4["PDN1_IN_P_NET"], u4["PDN1_IN_N_NET"]) == ("VBUS", "GND")
    assert (u4["PDN1_OUT_P_NET"], u4["PDN1_OUT_N_NET"]) == ("ISO5", "GND_ISO")
    # Pout / (Vin x eff): the efficiency, no no-load current on top.
    assert u4["PDN1_REGULATOR_EFFICIENCY"] == "0.8" and "PDN1_QUIESCENT" not in u4
    assert (p["U9"]["PDN1_P_NET"], p["U9"]["PDN1_N_NET"]) == ("ISO5", "GND_ISO")
    # A resistive path returns through its last part (R21), whose pad is on GND_ISO.
    assert (p["R20"]["PDN1_N_DES"], p["R20"]["PDN1_N_NET"]) == ("R21", "GND_ISO")
    assert p["U5"]["PDN1_N_NET"] == "GND"                 # the input side is untouched
    assert any("U4: isolated converter, output side returns on GND_ISO" in n for n in notes)


def test_isolated_converter_without_efficiency_uses_its_no_load_input():
    p, notes = _iso(_with_isolated(efficiency="unknown", quiescent_current={"amps": 0.05}))
    assert p["U4"]["PDN1_QUIESCENT"] == "0.05" and "PDN1_REGULATOR_EFFICIENCY" not in p["U4"]
    assert any("losses above no-load are not included" in n for n in notes)


def test_isolated_converter_with_neither_is_driven_and_its_input_named_unknown():
    p, notes = _iso(_with_isolated(efficiency="unknown", quiescent_current="unknown"))
    assert p["U4"]["PDN1_GAIN"] == "0"                     # draws nothing it could state
    assert p["U9"]["PDN1_N_NET"] == "GND_ISO"              # ISO5 is still driven
    assert any("U4 (ISO5): isolated converter, neither efficiency nor no-load input" in n
               and "VBUS's drop is a lower bound" in n for n in notes)


def test_isolated_rail_without_a_stated_voltage_is_not_driven():
    pdn = _with_isolated(efficiency=0.8)
    pdn["rails"][-1]["voltage"] = {"nominal": "not_specified", **V}
    p, notes = params(pdn=pdn)
    assert "U4" not in p and "U9" not in p
    assert any("U4 (ISO5): no stated voltage; no REGULATOR" in n for n in notes)


def _two_inputs(entry_currents=None):
    """U3 (an LDO) takes its input from VBUS (U3_1) and from +3V3 behind FB9 (U3_4)."""
    pdn = copy.deepcopy(PDN)
    pdn["rails"][1]["loads"].append(
        {"role": "regulator", "refdes": "U3", "pins": ["U3_4"], "input_net": "+3V3_F",
         "input_filter": [{"refdes": "FB9", "class": "ferrite", "net_in": "+3V3",
                           "net_out": "+3V3_F", "resistance_ohm": "unknown",
                           "datasheet_figures": [{"spec": "dcr", "max": "0.08", "unit": "ohm"}]}]})
    reg = pdn["regulators"][1]
    reg.update(input_rails=["VBUS", "+3V3"], input_current={"amps": 0.4},
               quiescent_current={"amps": 2e-4})
    for rail, amps in (entry_currents or {}).items():
        for r in pdn["rails"]:
            for l in r["loads"]:
                if r["name"] == rail and l["refdes"] == "U3":
                    l["current"] = {"amps": amps}
    return pdn


def test_regulator_with_two_input_rails_shares_its_input_equally_when_the_file_is_silent():
    reader = TomachiePdn(_two_inputs(), "typ")
    p, notes = reader.parameters()
    u3 = p["U3"]
    assert (u3["PDN1_ROLE"], u3["PDN1_IN_P_NET"], u3["PDN1_IN_P_PINS"]) == ("REGULATOR", "VBUS", "1")
    assert (u3["PDN1_GAIN"], u3["PDN1_QUIESCENT"]) == ("0.5", "0.0001")
    assert {k[5:]: v for k, v in u3.items() if k.startswith("PDN2_")} == {
        "ROLE": "SINK", "I": "0.2", "P_NET": "+3V3_F", "N_NET": "GND", "P_PINS": "4"}
    assert ("U3", "+3V3_F", "+3V3") in reader.reg_inputs       # FB9 completed from the board
    assert any("U3 (+1V8): 2 input rails (VBUS, +3V3); its input current is shared equally"
               in n for n in notes)


def test_regulator_with_two_input_rails_follows_a_split_the_file_states():
    p, notes = params(pdn=_two_inputs({"VBUS": 0.001, "+3V3": 0.399}))
    assert p["U3"]["PDN1_GAIN"] == "0.0025"
    assert p["U3"]["PDN2_I"] == "0.399"
    assert any("shared as the file states" in n for n in notes)


def test_regulator_bias_entry_is_a_sink_and_the_input_stays_whole():
    """An AP2132 (U9): VIN U9_3 on 3V3 carries the input current; VCTRL / EN on
    5V is a regulator_bias load with its own (control) current - no equal split."""
    pdn = _two_inputs()
    vbus = pdn["rails"][0]["loads"][1]
    assert vbus["refdes"] == "U3"
    vbus.update(role="regulator_bias", current={"amps": 2.5e-4},
                current_max={"amps": 5e-4})
    pdn["regulators"][1]["input_rails"] = ["+3V3", "VBUS"]
    nominal, notes = params(pdn=pdn)
    worst, _ = params("max", pdn=pdn)
    u3 = nominal["U3"]
    assert (u3["PDN1_ROLE"], u3["PDN1_IN_P_NET"], u3["PDN1_IN_P_PINS"]) == \
        ("REGULATOR", "+3V3_F", "4")
    assert "PDN1_GAIN" not in u3                           # the whole input, no split
    assert not any("input rails" in n for n in notes)
    bias = {k[5:]: v for k, v in u3.items() if k.startswith("PDN2_")}
    assert bias == {"ROLE": "SINK", "I": "0.00025", "P_NET": "VBUS", "N_NET": "GND",
                    "P_PINS": "1"}
    assert worst["U3"]["PDN2_I"] == "0.0005"
    # An unknown bias current is named, like any other load's.
    vbus["current"] = vbus["current_max"] = "unknown"
    p, notes = params(pdn=pdn)
    assert not any(k.endswith("_ROLE") and v == "SINK" for k, v in p["U3"].items())
    assert any("U3 on VBUS: current not known; no SINK" in n for n in notes)


@pytest.mark.parametrize("basis", ["signal_pin_only", "not_a_supply"])
def test_a_part_on_a_rail_by_no_supply_pin_gets_no_sink(basis):
    """A part on the rail only by a detect, enable or reference pin (basis signal_pin_only,
    not_a_supply in older files; 0 A) is no SINK, and is named once in the notes."""
    pdn = copy.deepcopy(PDN)
    pdn["rails"][1]["loads"].append({"role": "ic", "refdes": "U9", "pins": ["U9_4"],
                                     "current": {"amps": 0.0, "basis": basis}})
    p, notes = params(pdn=pdn)
    assert "U9" not in p
    assert any(n.startswith("On a rail by a detect") and "U9 (+3V3)" in n for n in notes)


def test_only_a_maximum_current_is_used_in_the_typical_corner():
    """A load whose file gives a maximum current but no typical is solved at the maximum,
    said once; left out, the rail's drop would read too low."""
    pdn = copy.deepcopy(PDN)
    pdn["rails"][1]["loads"][1]["current_max"] = {"amps": 0.025, "basis": "max"}   # U6
    p, notes = params(pdn=pdn)
    assert p["U6"]["PDN1_I"] == "0.025"
    assert any(n.startswith("U6: only a maximum current is known; 25 mA is used")
               for n in notes)


def test_a_regulator_of_unknown_type_is_an_ideal_source_at_its_output():
    """A regulator whose type the file does not give still drives its rail, as a SOURCE
    at its output pins drawing nothing from its input, said once - left out, every rail
    behind it went undriven."""
    pdn = copy.deepcopy(PDN)
    pdn["regulators"][1]["type"] = "unknown"                          # U3, +1V8
    p, notes = params(pdn=pdn)
    assert p["U3"] == {"PDN1_ROLE": "SOURCE", "PDN1_V": "1.8", "PDN1_P_NET": "+1V8",
                       "PDN1_N_NET": "GND", "PDN1_P_PINS": "5"}
    assert any(n.startswith("U3 (+1V8): not known whether linear or switching; modeled "
                            "as an ideal source") for n in notes)


def test_a_series_diode_carries_its_forward_drop():
    """A series diode is a SERIES with PDN_VF from its datasheet, not a 1 mOhm bridge; the
    worst-case corner takes the maximum. With no forward voltage it stays bridged, said."""
    pdn = copy.deepcopy(PDN)
    diode = {"refdes": "D1", "class": "diode", "net_in": "+3V3", "net_out": "+3V3A",
             "resistance_ohm": "unknown",
             "datasheet_figures": [{"spec": "vf", "typ": "0.40", "unit": "V", "if": "1A"},
                                   {"spec": "vf", "typ": "0.50", "unit": "V", "if": "3A"},
                                   {"spec": "vfm", "max": "0.60", "unit": "V", "if": "3A"}]}
    pdn["rails"][2]["series_from_parent"].append(diode)
    p, notes = params(pdn=pdn)
    assert (p["D1"]["PDN1_VF"], p["D1"]["PDN1_R"]) == ("0.35", "0.05")
    assert any(n.startswith("D1: series diode, forward drop 0.35 V + 50 mOhm") for n in notes)
    p, _ = params("max", pdn=pdn)
    assert p["D1"]["PDN1_VF"] == "0.45"
    diode["datasheet_figures"] = []
    p, notes = params(pdn=pdn)
    assert "PDN1_VF" not in p["D1"] and p["D1"]["PDN1_R"] == "0.001"
    assert any("no forward voltage in the file" in n for n in notes)


def test_isolated_converter_takes_the_ground_its_rail_is_stated_to_return_on():
    """With pads on two isolated grounds, the board alone cannot say which one the output
    side returns on; the rail's ground in the file (spec revision 7) does."""
    board = dict(ISO_BOARD, U4=ISO_BOARD["U4"] + ["GND_X"], U9=ISO_BOARD["U9"] + ["GND_X"],
                 R21=ISO_BOARD["R21"] + ["GND_X"])
    pdn = _with_isolated(efficiency=0.8)
    pdn["ground_nets"] = list(pdn.get("ground_nets", ["GND"])) + ["GND_ISO", "GND_X"]
    reader = TomachiePdn(copy.deepcopy(pdn), "typ")
    p, notes = reader.parameters()
    notes += reader.place_on_board(p, _board(board))
    assert any("output-side ground was not found" in n for n in notes)
    next(r for r in pdn["rails"] if r["name"] == "ISO5")["ground"] = "GND_ISO"
    reader = TomachiePdn(copy.deepcopy(pdn), "typ")
    p, notes = reader.parameters()
    notes += reader.place_on_board(p, _board(board))
    assert p["U4"]["PDN1_OUT_N_NET"] == "GND_ISO"
    assert any("U4: isolated converter, output side returns on GND_ISO" in n for n in notes)


def test_a_pull_resistor_returns_through_the_part_it_pulls():
    """R20 pulls U9_3 up: its current returns through U9, so U9's ground is its return
    and an isolated rail's ground is still found from the loads."""
    pdn = _with_isolated(efficiency=0.8)
    iso5 = next(r for r in pdn["rails"] if r["name"] == "ISO5")
    iso5["loads"][1] = {"role": "pull_resistor", "refdes": "R20", "pins": ["R20_1"],
                        "pull_to": "U9_3", "current": {"amps": 0.0005}}
    board = dict(ISO_BOARD, U4=["VBUS", "GND", "ISO5"], R20=["ISO5", "PULLED"])
    reader = TomachiePdn(copy.deepcopy(pdn), "typ")
    p, notes = reader.parameters()
    notes += reader.place_on_board(p, _board(board))
    assert (p["R20"]["PDN1_N_DES"], p["R20"]["PDN1_N_NET"]) == ("U9", "GND_ISO")
    assert any("U4: isolated converter, output side returns on GND_ISO" in n for n in notes)


def test_a_pull_into_a_part_with_no_ground_pad_has_an_unknown_return():
    """R20 drives a solid-state relay's LED input (K1_1): the current comes back through
    what drives K1, which the file does not say - no SINK, and a note, not an error."""
    pdn = _with_isolated(efficiency=0.8)
    iso5 = next(r for r in pdn["rails"] if r["name"] == "ISO5")
    iso5["loads"][1] = {"role": "pull_resistor", "refdes": "R20", "pins": ["R20_1"],
                        "pull_to": "K1_1", "current": {"amps": 0.005}}
    board = dict(ISO_BOARD, R20=["ISO5", "LED_A"], K1=["LED_A", "LED_K", "OUT_A", "OUT_B"])
    reader = TomachiePdn(copy.deepcopy(pdn), "typ")
    p, notes = reader.parameters()
    notes += reader.place_on_board(p, _board(board))
    assert "R20" not in p
    assert any("R20: pulls K1, which has no pad on" in n and "lower bound" in n for n in notes)


def test_a_part_not_fitted_is_an_open():
    """not_fitted (spec revision 7): a two-net resistor is a 500 Mohm SERIES; a capacitor
    and a part on more nets are left out."""
    pdn = copy.deepcopy(PDN)
    pdn["not_fitted"] = [
        {"refdes": "R9", "class": "resistor", "value": "", "nets": ["+3V3", "SIG"],
         "basis": "adhoc"},
        {"refdes": "C9", "class": "capacitor", "value": "1uF", "nets": ["+3V3", "GND"],
         "basis": "variant"},
        {"refdes": "U9", "class": "device", "value": "", "nets": ["+3V3", "GND", "SIG"],
         "basis": "variant"}]
    p, notes = params(pdn=pdn)
    assert (p["R9"]["PDN1_ROLE"], p["R9"]["PDN1_R"]) == ("SERIES", "5e+08")
    assert "C9" not in p and "U9" not in p
    assert any("Not fitted, left out: C9, U9" in n for n in notes)


def test_a_load_behind_a_ferrite_solves_the_ferrite():
    """U9's supply pin sits behind FB3 (input_net / input_filter): FB3 is a SERIES at its
    DC resistance, also when U9's current is not known."""
    pdn = _with_isolated(efficiency=0.8)
    iso5 = next(r for r in pdn["rails"] if r["name"] == "ISO5")
    fb3 = {"refdes": "FB3", "class": "ferrite", "net_in": "ISO5", "net_out": "NetC9_2",
           "resistance_ohm": "unknown",
           "datasheet_figures": [{"spec": "dcr", "max": "0.080", "unit": "ohm"}]}
    for current in ({"amps": 0.05}, "unknown"):
        iso5["loads"][0] = {"role": "ic", "refdes": "U9", "pins": ["U9_1"], "current": current,
                            "input_net": "NetC9_2", "input_filter": [fb3]}
        p, _ = params(pdn=copy.deepcopy(pdn))
        assert (p["FB3"]["PDN1_ROLE"], p["FB3"]["PDN1_P_NET"], p["FB3"]["PDN1_N_NET"]) ==             ("SERIES", "ISO5", "NetC9_2")
        assert p["FB3"]["PDN1_R"] == "0.08"


def test_a_series_diode_with_a_vf_curve_is_fitted_on_it():
    """The 25 C curve, a line through its points above a tenth of its highest current."""
    pdn = copy.deepcopy(PDN)
    pdn["rails"][2]["series_from_parent"].append({
        "refdes": "D1", "class": "diode", "net_in": "+3V3", "net_out": "+3V3A",
        "resistance_ohm": "unknown", "datasheet_figures": [],
        "vf_curves": [{"label": "TJ=125C", "points": [[0.1, 0.20], [1, 0.30], [3, 0.40]]},
                      {"label": "TJ=25C", "points": [[0.01, 0.15], [0.5, 0.375], [1, 0.40],
                                                     [3, 0.50]]}]})
    p, notes = params(pdn=pdn)
    assert (p["D1"]["PDN1_VF"], p["D1"]["PDN1_R"]) == ("0.35", "0.05")
    assert any("Vf-vs-If curve (TJ=25C)" in n for n in notes)


def test_worst_case_runs_a_regulator_at_the_low_end_of_its_band():
    """U2's +3V3 at 3.21 V in the worst case (its band at the temperature grade), at 3.3 V
    nominally; a rail with no band stays at its set voltage."""
    pdn = copy.deepcopy(PDN)
    pdn["rails"][1]["regulator_band"] = {
        "basis": "divider", "at_25c": {"min_v": 3.25, "max_v": 3.35},
        "at_grade": {"min_v": 3.21, "max_v": 3.39}, "grade_c": 85.0}
    nominal, _ = params(pdn=pdn)
    worst, notes = params("max", pdn=pdn)
    assert (nominal["U2"]["PDN1_V"], worst["U2"]["PDN1_V"]) == ("3.3", "3.21")
    assert worst["U3"]["PDN1_V"] == "1.8"
    assert any("Rail +3V3: worst case, regulator U2 at the low end of its output band "
               "3.21 V (set 3.3 V)" in n for n in notes)


def test_a_0_ohm_jumper_takes_its_jumper_resistance_in_the_worst_case():
    pdn = copy.deepcopy(PDN)
    r9 = pdn["rails"][2]["series_from_parent"][1]
    r9["datasheet_figures"] = [{"spec": "jumper_resistance", "max": "20", "unit": "mohm"},
                               {"spec": "resistance", "max": "100M", "unit": "ohm"}]
    nominal, _ = params(pdn=pdn)
    worst, _ = params("max", pdn=pdn)
    assert "R9" not in nominal                      # a 0 ohm link: FYPA bridges it
    assert worst["R9"]["PDN1_R"] == "0.02"


def test_a_dc_resistance_named_after_its_part_is_taken_and_its_siblings_are_not():
    figs = [{"spec": "BLM18PG121SH1_dc_resistance", "max": "0.05", "unit": "ohm"},
            {"spec": "BLM18PG221SN1_dc_resistance", "max": "0.10", "unit": "ohm"},
            {"spec": "BLM18PG121SH1_impedance_100MHz", "typ": "120", "unit": "ohm"},
            {"spec": "insulation_resistance", "typ": "10", "unit": "Mohm"}]
    assert dc_resistance({"resistance_ohm": "unknown", "part_number": "BLM18PG121SH1D",
                          "datasheet_figures": figs}) == (None, pytest.approx(0.05))


def test_a_part_that_draws_what_it_drives_sinks_its_own_current():
    pdn = copy.deepcopy(PDN)
    pdn["rails"][1]["loads"][3].update(current={"amps": 6e-6, "basis": "max"},
                                       load_dependent=True,
                                       load_dependent_reason="static figure")
    p, notes = params(pdn=pdn)
    assert p["U8"]["PDN1_I"] == "6e-06"
    assert any("U8: draws what it drives (static figure); its own current 6 uA is used" in n
               for n in notes)


def test_a_load_out_of_range_before_any_copper_is_named():
    pdn = copy.deepcopy(PDN)
    pdn["rails"][1]["loads"][0]["copper_margin"] = {"v": -0.05, "high_room_v": -0.02}
    _, notes = params(pdn=pdn)
    assert any("U5 on +3V3: 50 mV below its minimum supply before any copper" in n
               for n in notes)
    assert any("U5 on +3V3: 20 mV above its maximum supply" in n for n in notes)


def test_a_figure_named_for_another_pin_is_not_the_parts_resistance():
    """A load switch's FLAG FET (flg_on_resistance, 30 ohm) is not its switch (rds_on);
    a relay's contact resistance is its DC resistance, its coil's is not."""
    from fypa.tomachie_pdn import dc_resistance
    switch = {"resistance_ohm": "unknown", "datasheet_figures": [
        {"spec": "flg_on_resistance", "typ": "30", "max": "60", "unit": "Ω"},
        {"spec": "rds_on", "typ": "75", "max": "95", "unit": "mΩ"}]}
    assert dc_resistance(switch) == (pytest.approx(0.075), pytest.approx(0.095))
    relay = {"resistance_ohm": "unknown", "datasheet_figures": [
        {"spec": "coil_resistance", "typ": "208", "unit": "ohm"},
        {"spec": "contact_resistance", "max": "100", "unit": "mΩ"}]}
    assert dc_resistance(relay) == (None, pytest.approx(0.1))


def _switched(**over):
    pdn = copy.deepcopy(PDN)
    pdn["ground_nets"] = list(pdn.get("ground_nets", ["GND"])) + ["PGND"]
    sp = {"net": "PSU_P", "in": {"net": "PSU_P", "terminals": ["H8"]},
          "out": {"net": "LOAD_P", "terminals": ["H9"]},
          "return": {"net": "PGND", "terminals": ["H10", "H11"]},
          "switched_by": "K1", "contact_only": False,
          "series": [{"refdes": "R25", "class": "resistor", "net_in": "PSU_P",
                      "net_out": "SNT_N", "resistance_ohm": 0.01, "datasheet_figures": []},
                     {"refdes": "K1", "class": "relay", "net_in": "SNT_N", "net_out": "LOAD_P",
                      "resistance_ohm": "unknown", "datasheet_figures": [
                          {"spec": "coil_resistance", "typ": "208", "unit": "ohm"},
                          {"spec": "contact_resistance", "max": "100", "unit": "mΩ"}]}],
          "voltage": {"nominal": 10.0, "min": 0.0, "max": 25.0, "basis": "user"},
          "load_current_max": {"amps": 2.5, "basis": "user"}}
    sp.update(over)
    pdn["switched_paths"] = [sp]
    return pdn


def test_a_switched_path_is_a_source_series_parts_and_a_sink_on_its_return():
    reader = TomachiePdn(_switched(), "typ")
    p, notes = reader.parameters()
    board = dict(ISO_BOARD, H8=["PSU_P"], H9=["LOAD_P"], H10=["PGND"], H11=["PGND"],
                 R25=["PSU_P", "SNT_N"], K1=["SNT_N", "LOAD_P"])
    notes += reader.place_on_board(p, _board(board))
    h8, h9 = p["H8"], p["H9"]
    assert (h8["PDN1_ROLE"], h8["PDN1_V"], h8["PDN1_P_NET"], h8["PDN1_N_NET"],
            h8["PDN1_N_DES"]) == ("SOURCE", "10", "PSU_P", "PGND", "H10")
    assert (h9["PDN1_ROLE"], h9["PDN1_I"], h9["PDN1_N_NET"], h9["PDN1_N_DES"]) ==         ("SINK", "2.5", "PGND", "H11")
    assert (p["R25"]["PDN1_R"], p["K1"]["PDN1_R"]) == ("0.01", "0.1")
    assert not any("H8: no pad on any ground" in n for n in notes)


@pytest.mark.parametrize("over,why", [
    ({"contact_only": True}, "contacts only"),
    ({"return": None}, "names no return"),
    ({"voltage": "not_stated"}, "voltage is not stated"),
])
def test_a_switched_path_it_cannot_solve_is_said(over, why):
    p, notes = params(pdn=_switched(**over))
    assert "H8" not in p and any(why in n for n in notes)


def test_a_part_not_fitted_is_modeled_as_the_file_says():
    pdn = copy.deepcopy(PDN)
    pdn["not_fitted"] = [
        {"refdes": "F9", "class": "fuse", "nets": ["+3V3", "SIG"], "basis": "variant",
         "model_as": {"resistance_ohm": 5e8}},
        {"refdes": "C9", "class": "capacitor", "nets": ["+3V3", "GND"], "basis": "variant",
         "model_as": {"capacitance_f": 0.0}}]
    p, _ = params(pdn=pdn)
    assert (p["F9"]["PDN1_ROLE"], p["F9"]["PDN1_R"]) == ("SERIES", "5e+08")
    assert "C9" not in p


def _rail(name, volts, src, loads):
    return {"name": name, "voltage": {"nominal": volts, **V}, "source": src, "loads": loads}


def _board_input_pdn():
    pdn = copy.deepcopy(PDN)
    fuse = {"refdes": "F1", "class": "fuse", "net_in": "VIN", "net_out": "VBAT",
            "resistance_ohm": 0.02, "datasheet_figures": []}
    pdn["rails"] = [
        _rail("VBAT", 12.0, {"role": "board_input", "refdes": "J5", "pins": ["J5_1"],
                             "output_net": "VIN", "output_path": [fuse]}, []),
        _rail("VIN", 12.0, {"role": "board_input", "refdes": "J5", "pins": ["J5_1"],
                            "output_net": "VIN", "output_path": []}, [])]
    pdn["regulators"] = []
    return pdn


def test_a_board_inputs_series_parts_carry_its_current():
    p, _ = params(pdn=_board_input_pdn())
    assert (p["F1"]["PDN1_ROLE"], p["F1"]["PDN1_R"]) == ("SERIES", "0.02")
    assert (p["F1"]["PDN1_P_NET"], p["F1"]["PDN1_N_NET"]) == ("VIN", "VBAT")


def test_two_rails_from_one_connector_pin_are_one_source():
    p, notes = params(pdn=_board_input_pdn())
    assert [k for k, v in p["J5"].items() if k.endswith("_ROLE") and v == "SOURCE"] == ["PDN1_ROLE"]
    assert any("J5's output, placed with VBAT" in n for n in notes)


def test_a_cable_allowance_is_named_in_the_worst_case():
    pdn = _board_input_pdn()
    pdn["rails"][0]["source"]["cable"] = {"resistance_ohm": 0.25}
    _, notes = params(pdn=pdn, corner="max")
    assert any("cable allowance 250 mOhm" in n for n in notes)
    _, notes = params(pdn=pdn)
    assert not any("cable allowance" in n for n in notes)


def _motor_pdn(driver):
    pdn = copy.deepcopy(PDN)
    pdn["rails"][0]["loads"].append(
        {"role": "motor_output", "refdes": "J8", "pins": ["J8_1", "J8_2"],
         "current": {"amps": 1.5, "basis": "user"}, "driver": driver})
    return pdn


def test_a_motor_leaves_the_rail_at_its_drivers_pins():
    p, _ = params(pdn=_motor_pdn({"refdes": "U9", "supply_pins": ["U9_4", "U9_5"],
                                  "return_pins": ["U9_7"], "output_nets": ["M1A", "M1B"]}))
    assert "J8" not in p
    assert p["U9"] == {"PDN1_ROLE": "SINK", "PDN1_I": "1.5", "PDN1_P_NET": "VBUS",
                       "PDN1_N_NET": "GND", "PDN1_P_PINS": "4,5", "PDN1_N_PINS": "7"}


def test_a_motor_with_no_driver_pins_is_named_and_left_out():
    p, notes = params(pdn=_motor_pdn({}))
    assert "J8" not in p
    assert any("J8 on VBUS: motor with no driver supply pins" in n for n in notes)


def test_an_led_a_driver_sinks_returns_at_the_drivers_ground_pins():
    pdn = copy.deepcopy(PDN)
    pdn["rails"][1]["loads"].append(
        {"role": "resistive_path", "refdes": "J6", "pins": ["J6_11"], "path_parts": ["J6", "R30"],
         "current": {"amps": 0.004, "basis": "v_over_r"},
         "driver": {"refdes": "U4", "supply_pins": [], "return_pins": ["U4_9"]}})
    p, _ = params(pdn=pdn)
    assert (p["J6"]["PDN1_N_DES"], p["J6"]["PDN1_N_PINS"]) == ("U4", "9")
