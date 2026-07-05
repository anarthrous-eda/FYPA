"""Direction and power-balance tests for the VoltageRegulator stamp.

Guards the sign of the input-current mirror (``solver.py``,
``stamp_network_into_system``): the regulator must DRAW ``gain * I_out``
from its input pin (``s_f``) and return it at ``s_t``. With the sign
flipped the regulator *injects* current into its input rail, so the input
pin solves ABOVE the source voltage — an inverted upstream IR-drop map.

The pre-existing regulator tests could not catch this: the quiescent test
checks network construction only, the adaptive-gain test mocks ``solve``,
and the unsym-path test asserts residual + finiteness — all satisfied by a
sign flip. These tests assert the physical direction and magnitudes.

Topology (three 30 x 5 mm strips, 1 oz copper):

  VIN strip:  5 V source pad at x=0.5 ......... regulator s_f pin at x=29.5
  GND strip:  source n at x=0.5, reg s_t / v_n, load return
  OUT strip:  regulator v_p at x=0.5 .......... 1 A load f at x=29.5

plus a quiescent CurrentSource (I_q) across the regulator input pins, the
way the FYPA loader models regulator quiescent draw.
"""
from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import MultiPolygon, Point, box

from pdnsolver import mesh as M
from pdnsolver import problem as P
from pdnsolver import solver as S

# 1 oz copper sheet conductance, S (conductivity [S/mm] * thickness [mm]).
_CU = 5.95e4 * 0.035

_LEN = 30.0     # strip length, mm
_W = 5.0        # strip width, mm
_GAIN = 0.66    # I_in = gain * I_out (loader derives this from efficiency)
_I_OUT = 1.0    # load current, A
_I_Q = 0.25     # regulator quiescent draw, A
_V_SRC = 5.0
_V_REG = 3.3


def _strip(y0: float, name: str) -> P.Layer:
    return P.Layer(shape=MultiPolygon([box(0.0, y0, _LEN, y0 + _W)]),
                   name=name, conductance=_CU)


def _build_and_solve():
    vin = _strip(0.0, "VIN")
    gnd = _strip(10.0, "GND")
    out = _strip(20.0, "OUT")

    # 5 V source at the left edge of VIN / GND.
    sp, sn = P.NodeID(), P.NodeID()
    src_net = P.Network(
        connections=[
            P.Connection(layer=vin, point=Point(0.5, 2.5), node_id=sp),
            P.Connection(layer=gnd, point=Point(0.5, 12.5), node_id=sn),
        ],
        elements=[P.VoltageSource(p=sp, n=sn, voltage=_V_SRC)],
    )

    # Regulator: input from the right edge of VIN, output drives OUT.
    vp, vn, sf, st = (P.NodeID() for _ in range(4))
    reg_net = P.Network(
        connections=[
            P.Connection(layer=vin, point=Point(29.5, 2.5), node_id=sf),
            P.Connection(layer=gnd, point=Point(29.5, 12.5), node_id=st),
            P.Connection(layer=out, point=Point(0.5, 22.5), node_id=vp),
            P.Connection(layer=gnd, point=Point(15.0, 12.5), node_id=vn),
        ],
        elements=[P.VoltageRegulator(v_p=vp, v_n=vn, s_f=sf, s_t=st,
                                     voltage=_V_REG, gain=_GAIN)],
    )

    # Quiescent draw across the regulator's input pins (separate network,
    # matching how the loader stamps PDN_REGULATOR quiescent current).
    qf, qt = P.NodeID(), P.NodeID()
    quiescent_net = P.Network(
        connections=[
            P.Connection(layer=vin, point=Point(29.5, 2.5), node_id=qf),
            P.Connection(layer=gnd, point=Point(29.5, 12.5), node_id=qt),
        ],
        elements=[P.CurrentSource(f=qf, t=qt, current=_I_Q)],
    )

    # 1 A load at the right edge of OUT.
    lf, lt = P.NodeID(), P.NodeID()
    load_net = P.Network(
        connections=[
            P.Connection(layer=out, point=Point(29.5, 22.5), node_id=lf),
            P.Connection(layer=gnd, point=Point(20.0, 12.5), node_id=lt),
        ],
        elements=[P.CurrentSource(f=lf, t=lt, current=_I_OUT)],
    )

    prob = P.Problem(
        layers=[vin, gnd, out],
        networks=[src_net, reg_net, quiescent_net, load_net],
        project_name="regulator-direction",
    )
    sol = S.solve(prob, mesher_config=M.Mesher.Config(maximum_size=0.5))
    assert sol.solver_info.residual_norm < 1e-6
    return sol


@pytest.fixture(scope="module")
def reg_solution():
    return _build_and_solve()


def _layer_arrays(sol, layer_index: int):
    ls = sol.layer_solutions[layer_index]
    xys = np.asarray(ls.meshes[0]._source_xys, dtype=np.float64)
    vals = np.asarray(ls.potentials[0].values, dtype=np.float64)
    return xys, vals


def _sample(xys: np.ndarray, vals: np.ndarray, x: float, y: float) -> float:
    """Potential at the mesh vertex nearest (x, y) — pins are mesh seeds, so
    the nearest vertex IS the attach vertex."""
    i = np.argmin((xys[:, 0] - x) ** 2 + (xys[:, 1] - y) ** 2)
    return float(vals[i])


# Analytic uniform-flow IR drop along VIN between the pads (29 mm of 5 mm
# strip carrying the full input current). The measured pad-to-pad drop also
# contains the point-injection constriction at both ends, so it exceeds this
# — the tolerance band below allows for it.
_I_IN = _GAIN * _I_OUT + _I_Q
_STRIP_DROP_V = _I_IN * (1.0 / _CU) * 29.0 / _W


def test_regulator_input_pin_below_source_voltage(reg_solution):
    """(a) The regulator DRAWS from VIN: V(input pin) < V(source pad) by
    roughly the strip's IR drop. A flipped mirror sign inverts this."""
    xys, vals = _layer_arrays(reg_solution, 0)  # VIN
    v_src = _sample(xys, vals, 0.5, 2.5)
    v_reg = _sample(xys, vals, 29.5, 2.5)
    drop = v_src - v_reg
    assert drop > 0, (
        f"regulator INJECTS into its input rail (V rises by {-drop * 1e3:.3f}"
        " mV along VIN) — input-current mirror sign is flipped"
    )
    # Constriction at the two point injections adds to the uniform-flow drop;
    # a generous band still rejects both a sign flip and a wrong gain.
    assert 0.8 * _STRIP_DROP_V < drop < 2.0 * _STRIP_DROP_V


def test_input_current_equals_gain_times_iout_plus_iq(reg_solution):
    """(b) Current in the VIN strip = gain * I_out + I_q. Measured from the
    interior potential slope: I = |dV/dx| * G * W (uniform lengthwise flow
    away from the injection points)."""
    xys, vals = _layer_arrays(reg_solution, 0)  # VIN
    x = xys[:, 0]
    band = (x >= 0.3 * _LEN) & (x <= 0.7 * _LEN)
    assert band.sum() >= 10
    slope, _ = np.polyfit(x[band], vals[band], 1)
    i_measured = abs(slope) * _CU * _W
    assert i_measured == pytest.approx(_I_IN, rel=0.03)


def test_source_power_matches_regulator_input_power(reg_solution):
    """(c) Source output power == regulator input power + copper dissipation
    of the VIN/GND run (a fraction of a percent here). If the mirror sign
    were flipped, the 'input' pair would GENERATE power and P_in would
    exceed P_src."""
    vin_xys, vin_v = _layer_arrays(reg_solution, 0)
    gnd_xys, gnd_v = _layer_arrays(reg_solution, 1)
    v_src = _sample(vin_xys, vin_v, 0.5, 2.5) - _sample(gnd_xys, gnd_v, 0.5, 12.5)
    v_in = (_sample(vin_xys, vin_v, 29.5, 2.5)
            - _sample(gnd_xys, gnd_v, 29.5, 12.5))
    p_src = v_src * _I_IN
    p_in = v_in * _I_IN
    # The input pair must be a consumer fed through resistive copper.
    assert 0.0 < p_in < p_src
    assert p_in == pytest.approx(p_src, rel=0.01)


def test_output_rail_regulated(reg_solution):
    """Sanity: the output side still enforces V(v_p) - V(v_n) = 3.3 V."""
    out_xys, out_v = _layer_arrays(reg_solution, 2)
    gnd_xys, gnd_v = _layer_arrays(reg_solution, 1)
    v_out = (_sample(out_xys, out_v, 0.5, 22.5)
             - _sample(gnd_xys, gnd_v, 15.0, 12.5))
    assert v_out == pytest.approx(_V_REG, abs=1e-9)
