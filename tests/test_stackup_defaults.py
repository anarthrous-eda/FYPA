"""Copper-thickness fallback when the Altium stackup carries none.

``altium_monkey`` initialises ``copper_thickness`` to 0 mils when a .PcbDoc
has no COPTHICK field for a layer. Zero thickness means zero sheet
conductance — an infinitely resistive layer — which downstream shows up as a
nonsense solve or a ZeroDivisionError rather than as an obvious "your stackup
is missing" error. The geometry layer substitutes 1 oz and says so.
"""
from __future__ import annotations

import logging

import pytest

from fypa.altium_geometry import (
    COPPER_CONDUCTIVITY_S_PER_MM,
    DEFAULT_COPPER_THICKNESS_MM,
    _layer_conductance,
    _thickness_warned,
)
from fypa.altium.extract import RawStackupLayer


def _layer(thickness_mm: float, layer_id: int = 1,
           name: str = "Top") -> RawStackupLayer:
    return RawStackupLayer(
        layer_id=layer_id, name=name, copper_thickness_mm=thickness_mm,
        dielectric_thickness_mm=0.2, next_layer_id=0,
        is_plane=False, plane_net_name=None, mech_enabled=True,
    )


@pytest.fixture(autouse=True)
def _reset_warn_memo():
    """The once-per-layer warning memo is module state; clear it so each
    test sees a fresh warning."""
    _thickness_warned.clear()
    yield
    _thickness_warned.clear()


def test_real_thickness_is_used_unchanged():
    cond = _layer_conductance(_layer(0.035))
    assert cond == pytest.approx(0.035 * COPPER_CONDUCTIVITY_S_PER_MM)


def test_heavy_copper_is_used_unchanged():
    cond = _layer_conductance(_layer(0.105))     # 3 oz
    assert cond == pytest.approx(0.105 * COPPER_CONDUCTIVITY_S_PER_MM)


@pytest.mark.parametrize("bad", [0.0, -0.0, -1.0])
def test_missing_thickness_falls_back_to_one_ounce(bad):
    cond = _layer_conductance(_layer(bad))
    assert cond == pytest.approx(
        DEFAULT_COPPER_THICKNESS_MM * COPPER_CONDUCTIVITY_S_PER_MM)


def test_missing_thickness_never_yields_zero_conductance():
    """The bug this guards: zero conductance makes the layer infinitely
    resistive and divides by zero when the metadata reports ohms/square."""
    assert _layer_conductance(_layer(0.0)) > 0.0


def test_fallback_is_warned(caplog):
    with caplog.at_level(logging.WARNING, logger="fypa.altium_geometry"):
        _layer_conductance(_layer(0.0, layer_id=7, name="Inner3"))
    messages = [r.getMessage() for r in caplog.records]
    assert any("no copper thickness" in m for m in messages)
    assert any("Inner3" in m for m in messages)
    # The message must say what was substituted and how to fix it properly.
    joined = " ".join(messages)
    assert "35" in joined and "Settings" in joined


def test_warning_is_emitted_once_per_layer(caplog):
    """A stackup-less board must not log once per (layer, net) bucket."""
    with caplog.at_level(logging.WARNING, logger="fypa.altium_geometry"):
        for _ in range(5):
            _layer_conductance(_layer(0.0, layer_id=3, name="Inner1"))
    warnings = [r for r in caplog.records if "no copper thickness" in r.getMessage()]
    assert len(warnings) == 1


def test_distinct_layers_each_warn(caplog):
    with caplog.at_level(logging.WARNING, logger="fypa.altium_geometry"):
        _layer_conductance(_layer(0.0, layer_id=1, name="Top"))
        _layer_conductance(_layer(0.0, layer_id=32, name="Bottom"))
    warnings = [r for r in caplog.records if "no copper thickness" in r.getMessage()]
    assert len(warnings) == 2


def test_good_layer_does_not_warn(caplog):
    with caplog.at_level(logging.WARNING, logger="fypa.altium_geometry"):
        _layer_conductance(_layer(0.035))
    assert not [r for r in caplog.records
                if "no copper thickness" in r.getMessage()]
