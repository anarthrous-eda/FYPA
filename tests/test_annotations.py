"""PDN_* annotation parser tests — single-net (PDN_NET) validation.

These exercise the parser's pure logic directly (no Altium extraction):
``_terminal_mode`` decides single-net vs two-terminal per channel, and
``_validate_directive_groups`` enforces the cross-directive rules — mode
consistency within an analysis group, the open-loop check, and return-group
assignment. See ``fypa.altium.annotations`` for the schema.
"""
from __future__ import annotations

from fypa.altium.annotations import (
    AnnotationResult,
    SinkSpec,
    SourceSpec,
    TerminalPin,
    TerminalSpec,
    _terminal_mode,
    _validate_directive_groups,
)
from fypa.altium.extract import Pt2D


# --- _terminal_mode -----------------------------------------------------------

def test_terminal_mode_single_net():
    result = AnnotationResult()
    assert _terminal_mode({"PDN_NET": "VBATT"}, None, "SOURCE on J1",
                          result) == "single"
    assert not result.errors


def test_terminal_mode_two_terminal():
    result = AnnotationResult()
    assert _terminal_mode({"PDN_P_NET": "+5V", "PDN_N_NET": "GND"}, None,
                          "SOURCE on U1", result) == "two"
    assert not result.errors


def test_terminal_mode_rejects_mixing_pdn_net_with_p_net():
    result = AnnotationResult()
    mode = _terminal_mode({"PDN_NET": "VBATT", "PDN_P_NET": "+5V"}, None,
                          "SOURCE on J1", result)
    assert mode is None
    assert any("cannot be combined" in e for e in result.errors)


def test_terminal_mode_rejects_no_terminal_net():
    result = AnnotationResult()
    mode = _terminal_mode({}, None, "SINK on U1", result)
    assert mode is None
    assert any("no terminal net" in e for e in result.errors)


def test_terminal_mode_indexed_channel():
    result = AnnotationResult()
    assert _terminal_mode({"PDN2_NET": "VBATT"}, 2, "SINK on U1#2",
                          result) == "single"
    assert not result.errors


# --- _validate_directive_groups ----------------------------------------------

def _term(net_index: int) -> TerminalSpec:
    return TerminalSpec(pins=(TerminalPin(
        pad_designator="1", layer_id=1, net_index=net_index,
        point=Pt2D(0.0, 0.0)),))


def _single_source(net: int, des: str = "J1") -> SourceSpec:
    return SourceSpec(designator=des, schdoc_name="s.SchDoc", voltage=5.0,
                      p=_term(net), n=None)


def _single_sink(net: int, des: str = "U1") -> SinkSpec:
    return SinkSpec(designator=des, schdoc_name="s.SchDoc", current=1.0,
                    p=_term(net), n=None)


def _two_terminal_sink(p_net: int, n_net: int, des: str = "U2") -> SinkSpec:
    return SinkSpec(designator=des, schdoc_name="s.SchDoc", current=1.0,
                    p=_term(p_net), n=_term(n_net))


def test_single_net_group_ok_and_shares_return_group():
    result = AnnotationResult(directives=[
        _single_source(0), _single_sink(0)])
    _validate_directive_groups(result, None, {})
    assert not result.errors
    assert {d.return_group for d in result.directives} == {0}


def test_single_net_open_loop_source_without_sink():
    result = AnnotationResult(directives=[_single_source(0)])
    _validate_directive_groups(result, None, {})
    assert any("no SINK" in e for e in result.errors)


def test_single_net_open_loop_sink_without_source():
    result = AnnotationResult(directives=[_single_sink(0)])
    _validate_directive_groups(result, None, {})
    assert any("no SOURCE" in e for e in result.errors)


def test_group_may_not_mix_single_net_and_two_terminal():
    # Single-net SOURCE and a two-terminal SINK both touch net 0.
    result = AnnotationResult(directives=[
        _single_source(0), _two_terminal_sink(0, 1)])
    _validate_directive_groups(result, None, {})
    assert any("mixes single-net" in e for e in result.errors)


def test_independent_single_net_groups_get_distinct_return_groups():
    result = AnnotationResult(directives=[
        _single_source(0, "J1"), _single_sink(0, "U1"),
        _single_source(5, "J2"), _single_sink(5, "U2")])
    _validate_directive_groups(result, None, {})
    assert not result.errors
    by_des = {d.designator: d for d in result.directives}
    assert by_des["J1"].return_group == by_des["U1"].return_group
    assert by_des["J2"].return_group == by_des["U2"].return_group
    assert by_des["J1"].return_group != by_des["J2"].return_group


def test_two_terminal_only_board_is_unaffected():
    # A normal analysis: no PDN_NET anywhere, no errors, no return groups.
    result = AnnotationResult(directives=[
        SourceSpec(designator="U1", schdoc_name="s.SchDoc", voltage=5.0,
                   p=_term(0), n=_term(1)),
        _two_terminal_sink(0, 1, des="U2")])
    _validate_directive_groups(result, None, {})
    assert not result.errors
    assert all(d.return_group is None for d in result.directives)
