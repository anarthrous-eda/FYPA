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


def test_autofill_never_overwrites_what_the_user_set() -> None:
    d = M.new_supply()
    d.p_net, d.designator, d.voltage = "+12V", "J1", 11.5
    M.set_ground(d, M.IDEAL_RETURN)
    assert M.autofill(d, _board()) == []
    assert d.voltage == 11.5 and d.single_net and d.n_net is None


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
