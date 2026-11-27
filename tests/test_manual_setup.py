"""Manual setup wizard logic (fypa.autosetup.manual)."""
from __future__ import annotations

import pytest

from fypa.autosetup import manual as M
from tests.test_autosetup import _board


def _entry(d) -> M.Entry:
    return M.Entry(directive=d)


def test_net_choices_put_rails_first_and_grounds_last() -> None:
    nets = M.net_choices(_board())
    assert nets[-1] == "GND"
    assert nets.index("+3V3") < nets.index("SDA")
    assert nets.index("+12V") < nets.index("BST1")


def test_parts_on_net_busiest_first_with_value_and_pad_count() -> None:
    rows = M.parts_on_net(_board(), "+12V", prefer="supply")
    assert rows[0][0] == "J1" and rows[0][1].endswith("(2)")
    assert any(des == "U1" and "TPS62130RGTR" in label for des, label in rows)


def test_parts_already_a_load_on_the_net_go_last_and_are_marked() -> None:
    rows = M.parts_on_net(_board(), "+3V3", prefer="load", taken={"u2"})
    assert rows[0][0] == "U4"
    assert rows[-1][0] == "U2" and rows[-1][1].endswith("already a sink here")
    assert not any("already" in label for des, label in rows if des != "U2")


def test_autofill_never_picks_a_part_already_loading_the_net() -> None:
    view = _board()
    d = M.new_load()
    d.p_net = "+3V3"
    M.autofill(d, view)
    assert d.designator == "U2"                       # busiest IC first
    d = M.new_load()
    d.p_net = "+3V3"
    M.autofill(d, view, taken={"U2"})
    assert d.designator == "U4"


def test_load_candidates_are_free_ics_and_connectors_on_the_net() -> None:
    view = _board()
    # +3V3: L1, U2, R5, U4, R9, R7 - only the ICs count.
    assert M.load_candidates(view, "+3V3") == {"U2", "U4"}
    assert M.load_candidates(view, "+3V3", used={"u2"}) == {"U4"}
    assert M.load_candidates(view, "+3V3", used={"U2", "U4"}) == set()


def test_likely_loads_by_net_round_trips_through_the_sidecar(tmp_path) -> None:
    by_net = M.likely_loads_by_net(_board())
    assert by_net["+3V3"] == {"U2", "U4"}
    assert by_net["+12V"] == {"J1", "U1"}
    assert "GND" not in by_net
    path = tmp_path / "cache" / M.LOAD_CANDIDATES_FILE
    M.save_load_candidates(path, by_net)
    assert M.read_load_candidates(path) == by_net
    assert M.read_load_candidates(tmp_path / "missing.json") is None
    (tmp_path / "bad.json").write_text('{"format": "other"}', encoding="utf-8")
    assert M.read_load_candidates(tmp_path / "bad.json") is None
    # An older sidecar has no pre-merge map.
    assert M.read_premerge_parts(path) is None


def test_premerge_parts_split_a_rail_merged_through_a_0_ohm_link(tmp_path) -> None:
    from fypa.autosetup.design import DesignView, Part, Pin

    # U10's VOUT_2V5 feeds +ETH_2V5 (J9) through 0 Ω R106; the loader merged
    # +ETH_2V5 into VOUT_2V5, so only sch_net still tells the sides apart.
    def part(des, *pins):
        return Part(designator=des, logical=des, params={},
                    pins=[Pin(pad=str(i), name=str(i), net=n, sch_net=s)
                          for i, (n, s) in enumerate(pins, 1)])

    view = DesignView(parts=[
        part("U10", ("VOUT_2V5", "VOUT_2V5"), ("GND", "GND")),
        part("R106", ("VOUT_2V5", "VOUT_2V5"), ("VOUT_2V5", "+ETH_2V5")),
        part("J9", ("VOUT_2V5", "+ETH_2V5"), ("GND", "GND")),
        part("U3", ("+3V3", "+3V3")),
    ], net_names=["VOUT_2V5", "GND", "+3V3"])
    pre = M.premerge_parts_by_net(view)
    assert pre == {"VOUT_2V5": {"U10", "R106"}, "+ETH_2V5": {"R106", "J9"}}
    path = tmp_path / M.LOAD_CANDIDATES_FILE
    M.save_load_candidates(path, M.likely_loads_by_net(view), pre)
    assert M.read_premerge_parts(path) == pre
    assert M.read_load_candidates(path) == M.likely_loads_by_net(view)


def test_autofill_load_only_guesses_a_likely_load() -> None:
    view = _board()
    d = M.new_load()
    d.p_net = "+3V3"
    M.autofill(d, view, taken={"U2", "U4"})        # only passives left
    assert d.designator is None


def test_autofill_supply_from_net_and_part() -> None:
    view = _board()
    d = M.new_supply()
    d.p_net = "+12V"
    assert M.autofill(d, view) == ["voltage"]          # two parts: no guess
    assert d.voltage == 12.0 and d.designator is None
    d.designator = "J1"
    assert M.autofill(d, view) == ["ground"]
    assert d.n_net == "GND"


def test_autofill_regulator_input_when_it_has_one_other_rail() -> None:
    view = _board()
    d = M.new_supply()
    M.set_supply_type(d, "LDO")
    d.p_net, d.designator = "+1V8", "U2"
    filled = M.autofill(d, view)
    assert set(filled) == {"voltage", "ground", "input"}
    assert (d.voltage, d.in_p_net, d.n_net, d.in_n_net) == (1.8, "+3V3", "GND", "GND")


def test_autofill_input_from_input_named_pins_among_several_rails() -> None:
    from fypa.autosetup.design import DesignView, Part, Pin

    mod = Part(designator="U6", logical="U6", params={}, pins=[
        Pin("1", "VIN_1", "+12V"), Pin("2", "VIN_2", "+12V"),
        Pin("3", "VOUT_1", "+VDD"), Pin("4", "VDD1", "VDD1"),
        Pin("5", "V3P3", "V3P3"), Pin("6", "GND", "GND")])
    view = DesignView(parts=[mod], net_names=["+12V", "+VDD", "VDD1", "V3P3", "GND"])
    d = M.new_supply()
    M.set_supply_type(d, "Switcher")
    d.p_net, d.designator = "+VDD", "U6"
    M.autofill(d, view)
    assert d.in_p_net == "+12V"


def test_autofill_never_overwrites_what_the_user_set() -> None:
    d = M.new_supply()
    d.p_net, d.designator, d.voltage = "+12V", "J1", 11.5
    M.set_ground(d, M.IDEAL_RETURN)
    assert M.autofill(d, _board()) == []
    assert d.voltage == 11.5 and d.single_net and d.n_net is None


def test_input_net_choices_are_the_parts_own_nets_rails_first() -> None:
    view = _board()
    u2 = view.part("U2")                    # IN +3V3, OUT +1V8, BYP2, GND
    assert M.input_net_choices(view, u2, "+1V8") == ["+3V3", "BYP2"]
    assert M.input_net_choices(view, None, "+1V8") == []


def test_input_net_choices_put_the_vin_pin_net_before_a_rail_named_switch_node() -> None:
    from fypa.autosetup.design import DesignView, Part, Pin
    # MPM3650 module (Deimos U9): the SW node is auto-named after the part
    # (NetU9_VCC_HDIO_7 reads like a VCC rail) and has five pads to Vin's one.
    u9 = Part(designator="U9", logical="U9", params={}, pins=[
        Pin("22", "Vin", "+12V"), Pin("6", "VCC", "VCC_U9"),
        *[Pin(str(n), "SW", "NetU9_VCC_HDIO_7") for n in (7, 8, 13, 14, 23)],
        *[Pin(str(n), "OUT", "+HDIO_1V8") for n in (9, 10, 11, 12)],
        Pin("1", "PGND", "GND")])
    view = DesignView(parts=[u9], net_names=["+12V", "VCC_U9", "NetU9_VCC_HDIO_7",
                                             "+HDIO_1V8", "GND"])
    assert M.input_net_choices(view, u9, "+HDIO_1V8")[0] == "+12V"


def test_pins_of_lists_pad_and_name_in_natural_order() -> None:
    from fypa.autosetup.design import Part, Pin

    part = Part(designator="U9", logical="U9", params={}, pins=[
        Pin("10", "VIN_3", "+12V"), Pin("2", "VIN_1", "+12V"), Pin("A1", "VIN_2", "+12V"),
        Pin("3", "GND", "GND")])
    assert M.pins_of(part, "+12V") == [("2", "VIN_1"), ("10", "VIN_3"), ("A1", "VIN_2")]
    assert M.pins_of(part, None) == [] and M.pins_of(None, "+12V") == []


def test_custom_pins_with_none_ticked_is_an_error() -> None:
    d = M.new_load()
    d.p_net, d.designator, d.current, d.n_net = "+1V8", "U3", 0.1, "GND"
    d.p_pins = []
    issues = M.validate([_entry(d)], _board())
    assert any("tick at least one" in i.text for i in issues if i.level == "error")
    d.p_pins = None                         # All
    assert not [i for i in M.validate([_entry(d)], _board()) if i.level == "error"]


def test_supply_type_switch_keeps_output_and_clears_regulator_fields() -> None:
    d = M.new_supply()
    d.p_net, d.voltage = "+3V3", 3.3
    M.set_supply_type(d, "Switcher")
    assert d.role == "REGULATOR" and d.regulator_type == "SMPS"
    d.in_p_net, d.efficiency = "+12V", 0.9
    M.set_supply_type(d, "LDO")
    assert d.regulator_type == "LDO" and d.efficiency is None
    M.set_supply_type(d, "Source")
    assert d.role == "SOURCE" and d.in_p_net is None and d.p_net == "+3V3"
    assert M.supply_type(d) == "Source"


def test_validation_errors_and_warnings() -> None:
    view = _board()
    blank = M.new_load()
    wrong_pins = M.new_load()
    wrong_pins.p_net, wrong_pins.designator, wrong_pins.current = "+1V8", "U3", 0.1
    wrong_pins.n_net, wrong_pins.p_pins = "GND", ["9"]
    reg = M.new_supply()
    M.set_supply_type(reg, "LDO")
    reg.p_net, reg.designator, reg.voltage, reg.in_p_net = "+1V8", "U2", 1.8, "+1V8"
    M.set_ground(reg, "GND")
    unfed = M.new_load()
    unfed.p_net, unfed.designator, unfed.current = "+3V3_SW", "U5", 0.05
    M.set_ground(unfed, "GND")
    issues = M.validate([_entry(x) for x in (blank, wrong_pins, reg, unfed)], view)
    errs = {(i.entry_id, i.text) for i in issues if i.level == "error"}
    warns = [i.text for i in issues if i.level == "warning"]
    assert any(i == blank.id and "pick a net" in t for i, t in errs)
    assert any(i == wrong_pins.id and "pins 9 aren't on +1V8" in t for i, t in errs)
    assert any(i == reg.id and "input and output are the same net" in t for i, t in errs)
    assert any("nothing supplies +3V3_SW" in w for w in warns)
    assert not any(i == unfed.id for i, _t in errs)


def test_a_supply_through_a_bridge_feeds_the_load() -> None:
    view = _board()
    src = M.new_supply()
    src.p_net, src.designator, src.voltage = "+3V3", "U1", 3.3
    M.set_ground(src, "GND")
    load = M.new_load()
    load.p_net, load.designator, load.current = "+3V3_SW", "U5", 0.05
    M.set_ground(load, "GND")
    issues = M.validate([_entry(src), _entry(load)], view,
                        bridges=[("+3V3", "+3V3_SW")])
    assert not [i for i in issues if "nothing supplies" in i.text]


def test_buck_output_past_its_inductor_is_not_an_error() -> None:
    """The classifier puts U1's output on +3V3, the far side of L1 from its
    SW pin: U1 has no pin there, and that's how a buck is built."""
    view = _board()
    reg = M.new_supply()
    M.set_supply_type(reg, "Switcher")
    reg.p_net, reg.designator, reg.voltage, reg.in_p_net = "+3V3", "U1", 3.3, "+12V"
    M.set_ground(reg, "GND")
    issues = M.validate([_entry(reg)], view)
    assert not [i for i in issues if i.entry_id == reg.id], issues
    assert M.reach_through_series(view, view.part("U1"), "+3V3") == "L1"
    # No inductor or bridge between U1 and +1V8: still an error.
    reg.p_net = "+1V8"
    assert any("has no pin on +1V8" in i.text
               for i in M.validate([_entry(reg)], view) if i.level == "error")


def test_a_load_reaching_its_net_through_a_series_link_warns_not_errors() -> None:
    view = _board()
    load = M.new_load()
    load.p_net, load.designator, load.current = "+3V3", "U5", 0.05
    M.set_ground(load, "GND")
    issues = M.validate([_entry(load)], view, bridges=[("+3V3", "+3V3_SW")])
    assert not [i for i in issues if i.level == "error"]
    assert any("reaches +3V3 through a series link" in i.text for i in issues)


def test_pre_merge_net_names_validate_on_the_schematic_view() -> None:
    """Deimos: R1 (0 Ω) merged a module's VOUT_PS1V8 into +PS_1V8. Auto setup
    names the module's output as drawn; on the merged view U9 has no pin on
    VOUT_PS1V8 and nothing seems to supply J2's +PS_1V8."""
    from fypa.autosetup.design import DesignView, Part, Pin

    u9 = Part("U9", "U9", {}, pins=[
        Pin("1", "VIN", "+12V"), Pin("2", "VOUT", "+PS_1V8", sch_net="VOUT_PS1V8"),
        Pin("3", "GND", "GND")])
    j2 = Part("J2", "J2", {}, pins=[Pin("1", "1", "+PS_1V8"), Pin("2", "2", "GND")])
    view = DesignView(parts=[u9, j2], net_names=["+12V", "+PS_1V8", "VOUT_PS1V8", "GND"],
                      merge_links=[("VOUT_PS1V8", "+PS_1V8")],
                      merged_into={"VOUT_PS1V8": "+PS_1V8"})
    reg = M.new_supply()
    M.set_supply_type(reg, "Switcher")
    reg.p_net, reg.designator, reg.voltage, reg.in_p_net = "VOUT_PS1V8", "U9", 1.8, "+12V"
    M.set_ground(reg, "GND")
    load = M.new_load()
    load.p_net, load.designator, load.current = "+PS_1V8", "J2", 0.5
    M.set_ground(load, "GND")
    rows = [_entry(reg), _entry(load)]
    assert any("has no pin on VOUT_PS1V8" in i.text for i in M.validate(rows, view))
    assert M.validate(rows, view.schematic_view()) == []


def test_duplicate_rows_are_an_error() -> None:
    a = M.new_load()
    a.p_net, a.designator, a.current, a.n_net = "+1V8", "U3", 0.1, "GND"
    b = M.duplicate(a)
    assert b.id != a.id
    issues = M.validate([_entry(a), _entry(b)], _board())
    assert any("duplicates another sink" in i.text for i in issues)


def test_split_load_shares_current_by_supply_pins() -> None:
    loads = M.split_load(_board(), "U3", 0.3)
    by_net = {d.p_net: d.current for d in loads}
    assert by_net == pytest.approx({"+3V3_MCU": 0.15, "+1V8": 0.15})
    assert all(d.n_net == "GND" and d.role == "SINK" for d in loads)


def test_schematic_rows_are_locked_and_override_round_trips() -> None:
    md = {"directives": [
        {"role": "REGULATOR", "designator": "U6", "schdoc": "a.SchDoc", "value": 1.0,
         "regulator_type": "SMPS", "efficiency": 0.85, "smps_topology": "BUCK",
         "terminals": {
             "OUT_P": {"requested_net": "+VDD", "pins": [{"pad": "A1", "net": "+VDD"}]},
             "OUT_N": {"requested_net": "GND", "pins": []},
             "IN_P": {"requested_net": "+12V", "pins": [{"pad": "A6", "net": "+12V"}]},
             "IN_N": {"requested_net": "GND", "pins": []}}},
        {"role": "SINK", "designator": "J1", "schdoc": "a.SchDoc", "value": 10.0,
         "terminals": {"P": {"requested_net": "+VDD"}, "N": {"ideal_return": True}}},
        {"role": "AUTO_BRIDGE", "designator": "R9", "schdoc": "(auto-bridge)"},
        {"role": "SINK", "designator": "EDIT_1", "schdoc": "(editor)"},
    ]}
    rows = M.schematic_entries(md, [])
    assert [(e.directive.role, e.directive.designator, e.locked) for e in rows] == [
        ("REGULATOR", "U6", True), ("SINK", "J1", True)]
    u6 = rows[0].directive
    assert (u6.p_net, u6.in_p_net, u6.voltage, u6.regulator_type, u6.efficiency) == (
        "+VDD", "+12V", 1.0, "SMPS", 0.85)
    assert u6.p_pins == ["A1"] and u6.overrides_designator == "U6"
    j1 = rows[1].directive
    assert j1.single_net and j1.current == 10.0
    # An existing override hides the schematic row.
    assert len(M.schematic_entries(md, [u6])) == 1


# --- remote sense -------------------------------------------------------------


def _ldo_u2(**kw) -> M.EditorDirective:
    """U2 an LDO from +3V3 to +1V8; U3 (the MCU) has pin 2 on +1V8."""
    d = M.new_supply()
    M.set_supply_type(d, "LDO")
    d.p_net, d.designator, d.voltage, d.in_p_net = "+1V8", "U2", 1.8, "+3V3"
    M.set_ground(d, "GND")
    for k, v in kw.items():
        setattr(d, k, v)
    return d


def _sense_errors(d) -> list[str]:
    return [i.text for i in M.validate([_entry(d)], _board())
            if i.level == "error" and "sense" in i.text]


def test_remote_sense_on_a_regulator_validates() -> None:
    assert _sense_errors(_ldo_u2(sense_des=["U3"], sense_pins=["2"],
                                 sense_n_des=["U3"])) == []


def test_remote_sense_mistakes_are_errors() -> None:
    assert any("sense part R7 has no pin on +1V8" in t
               for t in _sense_errors(_ldo_u2(sense_des=["R7"])))
    assert any("sense pins 9 aren't on +1V8" in t
               for t in _sense_errors(_ldo_u2(sense_des=["U3"], sense_pins=["9"])))
    assert any("return sense pins are set but not the part" in t
               for t in _sense_errors(_ldo_u2(sense_n_pins=["3"])))
    assert any("tick at least one sense pin" in t
               for t in _sense_errors(_ldo_u2(sense_des=["U3"], sense_pins=[])))


def test_moving_the_output_or_ground_drops_sense_that_no_longer_fits() -> None:
    view = _board()
    d = _ldo_u2(sense_des=["U3"], sense_pins=["2"], sense_n_des=["U3"])
    M.drop_stale_sense(d, view)
    assert d.sense_des == ["U3"] and d.sense_n_des == ["U3"]   # still fits
    d.p_net = "+3V3"                                           # U3 isn't on it
    M.drop_stale_sense(d, view)
    assert d.sense_des is None and d.sense_pins is None and d.sense_n_des == ["U3"]
    src = M.new_supply()
    src.p_net, src.designator, src.voltage = "+12V", "J1", 12.0
    src.single_net, src.n_net, src.sense_n_des = False, "GND", ["U1"]
    M.set_ground(src, M.IDEAL_RETURN)                          # no copper return
    assert src.sense_n_des is None


def test_sense_from_terminal_reads_parts_and_narrowed_pins() -> None:
    term = {"pins": [{"pad": "1", "component": "J1", "net": "+12V"}]}
    every = {"J1": ["1", "2"], "U1": ["1"]}

    def all_pads(des):
        return [p for x in des for p in every[x]]
    assert M.sense_from_terminal(term, all_pads) == (["J1"], ["1"])
    term["pins"].append({"pad": "2", "component": "J1", "net": "+12V"})
    assert M.sense_from_terminal(term, all_pads) == (["J1"], None)   # all of them
    assert M.sense_from_terminal(None) == (None, None)


def test_override_carries_remote_sense_across() -> None:
    md = {"role": "REGULATOR", "designator": "U2", "schdoc": "a.SchDoc", "value": 1.8,
          "regulator_type": "LDO",
          "terminals": {
              "OUT_P": {"requested_net": "+1V8", "pins": [{"pad": "2", "net": "+1V8"}]},
              "OUT_N": {"requested_net": "GND", "pins": [{"pad": "4", "net": "GND"}]},
              "IN_P": {"requested_net": "+3V3", "pins": [{"pad": "1", "net": "+3V3"}]},
              "IN_N": {"requested_net": "GND", "pins": []}},
          "remote_sense": {
              "P": {"pins": [{"pad": "2", "component": "U3", "net": "+1V8"}]},
              "N": {"pins": [{"pad": "3", "component": "U3", "net": "GND"}]}}}
    d = M.from_schematic(md, _board())
    assert d is not None
    assert (d.sense_des, d.sense_pins) == (["U3"], None)       # U3's only +1V8 pin
    assert (d.sense_n_des, d.sense_n_pins) == (["U3"], None)
    # Without a design view the pads come across as picked.
    d = M.from_schematic(md)
    assert d is not None and d.sense_pins == ["2"]


def test_preset_from_classifier_pick_fills_the_regulator() -> None:
    from fypa.autosetup import candidates_for_net, suggest

    view = _board()
    pick = candidates_for_net(view, suggest(view), "+3V3", "REGULATOR")[0]
    d = M.preset(view, "+3V3", "REGULATOR", pick)
    assert (d.role, d.designator, d.p_net, d.in_p_net, d.n_net) == (
        "REGULATOR", "U1", "+3V3", "+12V", "GND")
    assert d.voltage == 3.3 and M.supply_type(d) == "Switcher"


def test_preset_without_a_pick_sets_role_and_net_only() -> None:
    view = _board()
    src = M.preset(view, "+12V", "SOURCE")
    assert (src.role, src.p_net, src.voltage, src.designator) == (
        "SOURCE", "+12V", 12.0, None)               # two parts: user picks
    sink = M.preset(view, "+3V3", "SINK")
    assert sink.role == "SINK" and sink.p_net == "+3V3"
    assert sink.designator and sink.current is None  # value left to the user
    reg = M.preset(view, "+3V3", "REGULATOR")
    assert reg.role == "REGULATOR" and M.supply_type(reg) == "LDO"


# --- copper (free) markers ----------------------------------------------------------


def _free(role: str, net: str, **kw) -> M.EditorDirective:
    return M.EditorDirective(kind="free", role=role, p_net=net, single_net=True,
                             anchor_xy=(34.21, 18.66), layer="Top Layer",
                             layer_id=1, **kw)


def test_copper_markers_listed_are_sources_and_sinks_only() -> None:
    assert M.is_listed_free_marker(_free("SOURCE", "+12V"))
    assert M.is_listed_free_marker(_free("SINK", "+3V3"))
    assert not M.is_listed_free_marker(_free("SERIES", "+3V3"))
    assert not M.is_listed_free_marker(M.new_load())


def test_copper_marker_title_shows_where_it_sits() -> None:
    e = _entry(_free("SOURCE", "+12V", voltage=12.0))
    assert e.is_free
    assert e.title() == "◎ (34.2, 18.7)  Source  +12V · 12 V"
    assert M.free_marker_where(e.directive, layer=True) == "Top Layer (34.2, 18.7) mm"


def test_autofill_never_gives_a_copper_marker_a_part() -> None:
    d = _free("SINK", "+3V3", current=0.1)
    M.autofill(d, _board())
    assert d.designator is None
    s = _free("SOURCE", "+12V")
    assert M.autofill(s, _board()) == ["voltage"] and s.voltage == 12.0
    assert s.designator is None


def test_copper_marker_validation_checks_values_not_parts() -> None:
    view = _board()
    ok_src = _free("SOURCE", "+12V", voltage=12.0)
    ok_load = _free("SINK", "+12V", current=0.5)
    no_i = _free("SINK", "+12V")
    bad_gnd = _free("SINK", "+12V", current=0.1)
    bad_gnd.single_net, bad_gnd.n_net = False, "+12V"
    issues = M.validate([_entry(x) for x in (ok_src, ok_load, no_i, bad_gnd)], view)
    by_id = {i.entry_id: i.text for i in issues if i.level == "error"}
    assert ok_src.id not in by_id and ok_load.id not in by_id
    assert "enter the current" in by_id[no_i.id]
    assert by_id[no_i.id].startswith("Free marker (34.2, 18.7)")
    assert "same net" in by_id[bad_gnd.id]
    # No "pick the part", no duplicate error for two markers on one rail.
    assert not any("part" in t or "duplicates" in t for t in by_id.values())


def test_a_copper_source_feeds_the_loads_on_its_rail() -> None:
    view = _board()
    src = _free("SOURCE", "+3V3_SW", voltage=3.3)
    load = M.new_load()
    load.p_net, load.designator, load.current = "+3V3_SW", "U5", 0.05
    M.set_ground(load, "GND")
    issues = M.validate([_entry(src), _entry(load)], view)
    assert not [i for i in issues if "nothing supplies" in i.text]


@pytest.fixture(scope="module")
def qapp():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def test_dialog_lists_copper_markers_with_location_and_values_only(qapp) -> None:
    from fypa.viewer.manual_setup_dialog import ManualSetupDialog

    src = _free("SOURCE", "+12V", voltage=12.0)
    load = _free("SINK", "+3V3", current=0.25)
    series = _free("SERIES", "+3V3", resistance=0.01)
    dlg = ManualSetupDialog(None, view=_board(), editor_directives=[src, load, series],
                            metadata=None)
    sup, ld = dlg.supplies, dlg.loads
    assert [sup.list.item(i).text() for i in range(sup.list.count())] == [
        "◎ (34.2, 18.7)  Source  +12V · 12 V"]
    assert ld.list.count() == 1
    ed = sup.editor
    assert ed.entry.directive.id == src.id
    assert not ed.net.isEnabled() and not ed.type.isEnabled()
    assert ed.value.isEnabled() and ed.gnd.isEnabled()
    assert ed.location.text() == "Top Layer (34.2, 18.7) mm"
    assert ed.part_row.isHidden() and ed.pins.isHidden() and not ed.location.isHidden()
    assert not ed.locate_btn.isHidden()
    assert not sup._duplicate_btn.isEnabled() and sup._remove_btn.isEnabled()
    # A value edit goes to the working copy, not the project's directive.
    ed.value.setText("11 V")
    ed._on_value()
    out = {d.id: d for d in dlg.result_directives()}
    assert out[src.id].voltage == 11.0 and src.voltage == 12.0
    assert set(out) == {src.id, load.id}          # SERIES stays on the Bridges tab
    # "Apply and show on Board" applies and names the marker.
    ed.locate_btn.click()
    from PySide6.QtWidgets import QDialog
    assert dlg.result() == QDialog.Accepted and dlg.locate_id == src.id


def test_dialog_sets_remote_sense_on_a_regulator(qapp) -> None:
    from fypa.viewer.manual_setup_dialog import ManualSetupDialog

    reg = _ldo_u2()
    dlg = ManualSetupDialog(None, view=_board(), editor_directives=[reg], metadata=None)
    ed = dlg.supplies.editor
    assert ed.entry.directive.id == reg.id
    (lab, combo, plab, picker), (nlab, ncombo, _nplab, _npicker) = ed._sense_rows
    # Rows show for a component-bound supply; pin lists only once a part is picked.
    assert not combo.isHidden() and not ncombo.isHidden() and picker.isHidden()
    assert combo.currentData() == "" and "local" in combo.currentText()
    choices = [combo.itemData(i) for i in range(combo.count())]
    assert "U3" in choices and "U2" not in choices      # not the supply itself
    combo.setCurrentIndex(combo.findData("U3"))
    ed._on_sense("p")
    d = ed.entry.directive
    assert d.sense_des == ["U3"] and d.sense_pins is None
    picker = ed._sense_rows[0][3]
    assert not picker.isHidden() and picker.all.isChecked()
    picker.all.setChecked(False)                           # pick by hand
    assert d.sense_pins == ["2"]
    out = {x.id: x for x in dlg.result_directives()}
    assert out[reg.id].sense_des == ["U3"] and reg.sense_des is None
    # Back to local sense clears the pins too.
    combo.setCurrentIndex(0)
    ed._on_sense("p")
    assert d.sense_des is None and d.sense_pins is None
