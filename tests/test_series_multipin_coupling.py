"""SERIES elements with multi-pin terminals must not add star-coupling resistance.

Regression: a FET modelled as SERIES (e.g. Q4 on Mangala, 0.75 mΩ, source pins
1-3 on +12V and the drain tab 5 on HS_SNS) was star-wired through the SINK
default of 100 mΩ per pin. The three source pins in parallel added ~33 mΩ in
series with the 0.75 mΩ element, so 33 A dropped ~1.1 V across the part instead
of ~25 mV. The element's own resistance models the part; the per-pin couplings
must be negligible.
"""
from __future__ import annotations

import numpy as np
import pytest
import shapely.geometry

from pdnsolver import problem as _pp

from fypa.altium.annotations import ResistorSpec, TerminalPin, TerminalSpec
from fypa.altium.extract import Pt2D
from fypa.altium.loader import (
    SOURCE_COUPLING_RESISTANCE_OHM,
    SolveSettings,
    _directive_to_network,
)

R_FET = 0.75e-3
P_NET, N_NET, LAYER = 0, 1, 1


def _layer(name: str) -> _pp.Layer:
    return _pp.Layer(
        shape=shapely.geometry.MultiPolygon([shapely.geometry.box(0, 0, 10, 10)]),
        name=name,
        conductance=1.0,
    )


def _pin(pad: str, net: int, x: float, y: float) -> TerminalPin:
    return TerminalPin(pad_designator=pad, layer_id=LAYER, net_index=net,
                       point=Pt2D(x, y))


def _fet_series(p_pins: int, n_pins: int) -> ResistorSpec:
    """An LFPAK-style FET as SERIES: ``p_pins`` source pins, ``n_pins`` tabs."""
    return ResistorSpec(
        designator="Q4",
        schdoc_name="(editor)",
        resistance=R_FET,
        p=TerminalSpec(pins=tuple(
            _pin(str(i + 1), P_NET, 6.0, 2.0 + i) for i in range(p_pins))),
        n=TerminalSpec(pins=tuple(
            _pin(str(5 + i), N_NET, 2.0, 3.0 + i) for i in range(n_pins))),
    )


def _layers() -> dict:
    return {(LAYER, P_NET): _layer("L1|+12V"), (LAYER, N_NET): _layer("L1|HS_SNS")}


def _terminal_to_terminal_ohms(net: _pp.Network, d: ResistorSpec) -> float:
    """Equivalent resistance between the P pins and the N pins.

    Each terminal's pins are tied together as ideal copper would (one external
    node per side), then the lumped resistor graph is reduced by nodal
    analysis: inject 1 A at P, ground N, read V(P).
    """
    p_pts = {(p.point.x, p.point.y) for p in d.p.pins}
    ext: dict[_pp.NodeID, str] = {}
    for c in net.connections:
        ext[c.node_id] = "P" if (c.point.x, c.point.y) in p_pts else "N"

    def key(node):
        return ext.get(node, node)

    resistors = [e for e in net.elements if isinstance(e, _pp.Resistor)]
    assert len(resistors) == len(net.elements), "SERIES stamps resistors only"
    idx: dict = {}
    for r in resistors:
        for n in (key(r.a), key(r.b)):
            idx.setdefault(n, len(idx))
    g = np.zeros((len(idx), len(idx)))
    for r in resistors:
        i, j = idx[key(r.a)], idx[key(r.b)]
        if i == j:
            continue
        y = 1.0 / r.resistance
        g[i, i] += y
        g[j, j] += y
        g[i, j] -= y
        g[j, i] -= y
    keep = [i for n, i in idx.items() if n != "N"]
    rhs = np.zeros(len(idx))
    rhs[idx["P"]] = 1.0
    v = np.linalg.solve(g[np.ix_(keep, keep)], rhs[keep])
    return float(v[keep.index(idx["P"])])


@pytest.fixture(autouse=True)
def _default_settings():
    SolveSettings().apply_to_modules()
    yield
    SolveSettings().apply_to_modules()


def test_three_to_one_pin_series_resistance_is_the_element_value():
    d = _fet_series(p_pins=3, n_pins=1)
    net = _directive_to_network(d, _layers())
    assert net is not None
    # 3 P-pin connections + 1 N-pin connection.
    assert len(net.connections) == 4

    r_eq = _terminal_to_terminal_ohms(net, d)
    # Before the fix: 0.75 mΩ + 100 mΩ / 3 ≈ 34.1 mΩ.
    assert r_eq == pytest.approx(R_FET, rel=1e-2)

    # At the Mangala load the part drops ~25 mV, not ~1.1 V.
    assert 33.0 * r_eq < 0.026


def test_series_star_couplings_are_negligible_and_not_heated():
    d = _fet_series(p_pins=3, n_pins=1)
    net = _directive_to_network(d, _layers())
    resistors = [e for e in net.elements if isinstance(e, _pp.Resistor)]
    main = [r for r in resistors if r.heat_source]
    stars = [r for r in resistors if not r.heat_source]
    # Only the element itself dissipates; the star resistors are artefacts.
    assert [r.resistance for r in main] == [R_FET]
    assert len(stars) == 3
    assert all(r.resistance == SOURCE_COUPLING_RESISTANCE_OHM for r in stars)


@pytest.mark.parametrize("p_pins,n_pins", [(1, 1), (4, 1), (3, 2)])
def test_series_resistance_independent_of_pin_count(p_pins, n_pins):
    d = _fet_series(p_pins=p_pins, n_pins=n_pins)
    net = _directive_to_network(d, _layers())
    assert _terminal_to_terminal_ohms(net, d) == pytest.approx(R_FET, rel=1e-2)
