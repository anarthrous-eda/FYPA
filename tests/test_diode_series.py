# Copyright (c) 2026 Tomachie LLC.
"""A diode in series (SERIES with PDN_VF): a resistor (the diode's slope) with Vf / R
carried back across it, so I = (Va - Vb - Vf) / R while it conducts."""

import numpy as np
import pytest

from pdnsolver import problem as pp
from pdnsolver.solver import stamp_network_into_system


def test_the_load_sits_one_forward_drop_and_one_slope_drop_below_the_source():
    vf, r, i_load = 0.45, 0.05, 1.0
    gnd, a, b = pp.NodeID(), pp.NodeID(), pp.NodeID()
    src = pp.VoltageSource(p=a, n=gnd, voltage=12.0)
    elements = [src, pp.Resistor(a=a, b=b, resistance=r),
                pp.CurrentSource(f=b, t=a, current=vf / r),          # the diode's drop
                pp.CurrentSource(f=b, t=gnd, current=i_load)]        # the load
    nodes = [gnd, a, b]
    idx = {n: i for i, n in enumerate(nodes)}

    class _Indexer:
        node_to_global_index = idx
        extra_source_to_global_index = {src: len(nodes)}

    n = len(nodes) + 1
    rows, cols, vals, rhs = [], [], [], np.zeros(n)
    stamp_network_into_system(pp.Network(connections=[], elements=elements), _Indexer(),
                              rows, cols, vals, rhs)
    m = np.zeros((n, n))
    for i, j, v in zip(rows, cols, vals):
        m[i, j] += v
    m[idx[gnd], :] = 0.0                                   # ground at 0 V
    m[idx[gnd], idx[gnd]] = 1.0
    rhs[idx[gnd]] = 0.0
    v = np.linalg.solve(m, rhs)
    assert v[idx[a]] == pytest.approx(12.0)
    assert v[idx[b]] == pytest.approx(12.0 - vf - i_load * r)
