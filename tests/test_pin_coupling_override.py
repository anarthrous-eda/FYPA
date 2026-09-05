"""``PDN_PIN_R`` — per-part override of the multi-pin star coupling resistance.

The global default (``loader.COUPLING_RESISTANCE_OHM``, 100 mohm) stands in for
an IC package's internal pin-to-pin resistance: bond wires, die metal, the
on-die supply grid. That is a property of the *part*, not the board — a BGA's
supply grid and a TO-220's leadframe are nothing alike — so it is overridable
per part. Unset, behaviour is exactly as before.
"""
from __future__ import annotations

import logging

import pytest

from fypa.altium.loader import COUPLING_RESISTANCE_OHM, _sink_pin_coupling


class _Sink:
    """Minimal stand-in carrying only what the resolver reads."""

    def __init__(self, value):
        self.designator = "U1"
        self.pin_coupling_ohm = value


def test_unset_uses_the_global_default():
    assert _sink_pin_coupling(_Sink(None)) == COUPLING_RESISTANCE_OHM


def test_missing_attribute_is_tolerated():
    """A spec pickled before the field existed must still resolve."""
    class _Old:
        designator = "U2"
    assert _sink_pin_coupling(_Old()) == COUPLING_RESISTANCE_OHM


@pytest.mark.parametrize("value", [0.02, 0.25, 1e-3, 5.0])
def test_positive_override_is_used(value):
    assert _sink_pin_coupling(_Sink(value)) == pytest.approx(value)


def test_integer_and_string_numbers_are_accepted():
    assert _sink_pin_coupling(_Sink(1)) == pytest.approx(1.0)
    assert _sink_pin_coupling(_Sink("0.05")) == pytest.approx(0.05)


@pytest.mark.parametrize("bad", [0.0, -0.0, -1.0, -0.001])
def test_non_positive_falls_back_rather_than_shorting_the_pins(bad):
    """Zero coupling would short every pin of the terminal through an ideal
    wire and divide by zero in the stamp — the default is the safe answer."""
    assert _sink_pin_coupling(_Sink(bad)) == COUPLING_RESISTANCE_OHM


@pytest.mark.parametrize("junk", ["abc", "", None.__class__, object()])
def test_unparseable_falls_back(junk):
    assert _sink_pin_coupling(_Sink(junk)) == COUPLING_RESISTANCE_OHM


def test_non_positive_is_warned_not_silent(caplog):
    with caplog.at_level(logging.WARNING, logger="fypa.altium.loader"):
        _sink_pin_coupling(_Sink(0.0))
    assert any("PDN_PIN_R" in r.getMessage() for r in caplog.records)


def test_sink_spec_carries_the_field_and_defaults_to_none():
    from fypa.altium.annotations import SinkSpec
    fields = SinkSpec.__dataclass_fields__
    assert "pin_coupling_ohm" in fields
    assert fields["pin_coupling_ohm"].default is None


def test_pin_r_is_an_accepted_sink_parameter():
    """Otherwise the parser would flag PDN_PIN_R as an unknown suffix."""
    from fypa.altium.annotations import _KNOWN_SUFFIXES_BY_ROLE
    assert "PIN_R" in _KNOWN_SUFFIXES_BY_ROLE["SINK"]
