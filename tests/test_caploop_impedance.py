"""Per-rail PDN impedance Z(f) (fypa.caploop.impedance).

Checks the branch algebra against closed-form landmarks (self-resonance, the
ESR floor, the VRM floor, the capacitive and inductive asymptotes), the
target-mask arithmetic from TI SWPA222A §4, and the anti-resonance finder —
including the case the whole feature exists to expose: two capacitors of
different value forming a parallel resonance between their self-resonances.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from fypa.caploop.impedance import (
    DEFAULT_DK,
    EPS0_F_PER_MM,
    CapBranch,
    RailTarget,
    VrmModel,
    branch_impedance,
    cap_branch,
    find_antiresonances,
    log_freqs,
    plane_capacitance_f,
    rail_impedance,
)
from fypa.caploop.packages import PackageLibrary


def _branch(designator="C1", c=100e-9, esr=25e-3, esl=0.45e-9, lm=0.5e-9):
    return CapBranch(designator=designator, capacitance_f=c, esr_ohm=esr,
                     esl_h=esl, l_mount_h=lm)


def _target(**kw):
    kw.setdefault("rail", "+3V3")
    kw.setdefault("voltage_v", 3.3)
    return RailTarget(**kw)


# --- the target mask (TI SWPA222A §4) --------------------------------------


def test_z_target_is_ripple_voltage_over_transient_current():
    t = _target(voltage_v=1.0, ripple_pct=5.0, transient_current_a=2.0)
    assert t.z_target_ohm == pytest.approx(0.025)     # 50 mV / 2 A


def test_z_target_is_infinite_without_a_transient_current():
    assert math.isinf(_target(transient_current_a=0.0).z_target_ohm)


# --- one branch ---------------------------------------------------------------


def test_branch_impedance_bottoms_out_at_esr_on_resonance():
    b = _branch()
    omega = np.array([2 * math.pi * b.srf_hz])
    z = branch_impedance(b, omega)[0]
    assert z.real == pytest.approx(b.esr_ohm)
    assert abs(z.imag) < 1e-9          # reactance cancels exactly


def test_self_resonance_uses_esl_plus_mounted_inductance():
    b = _branch(c=100e-9, esl=0.45e-9, lm=0.55e-9)
    expected = 1.0 / (2 * math.pi * math.sqrt(1e-9 * 100e-9))
    assert b.total_l_h == pytest.approx(1.0e-9)
    assert b.srf_hz == pytest.approx(expected)
    # A worse mount pushes the useful band down — the whole point of Tiers 1-3.
    assert _branch(lm=2e-9).srf_hz < b.srf_hz


def test_branch_is_capacitive_below_and_inductive_above_resonance():
    b = _branch()
    lo = branch_impedance(b, np.array([2 * math.pi * b.srf_hz / 100]))[0]
    hi = branch_impedance(b, np.array([2 * math.pi * b.srf_hz * 100]))[0]
    assert lo.imag < 0 and hi.imag > 0
    assert lo.real == pytest.approx(b.esr_ohm)


# --- the rail --------------------------------------------------------------------


def test_rail_impedance_floor_is_the_vrm_resistance_at_dc():
    freqs = log_freqs(1e0, 1e3, 50)
    r = rail_impedance("+3V3", freqs, [_branch()], _target(),
                       vrm=VrmModel(r_ohm=2e-3, l_h=5e-9))
    # At 1 Hz the caps are open and the VRM inductance is negligible.
    assert abs(r.z_ohm[0]) == pytest.approx(2e-3, rel=1e-3)


def test_two_identical_caps_halve_the_impedance():
    freqs = log_freqs(1e5, 1e7, 100)
    one = rail_impedance("+3V3", freqs, [_branch("C1")], _target(), vrm=None)
    two = rail_impedance("+3V3", freqs, [_branch("C1"), _branch("C2")],
                         _target(), vrm=None)
    assert np.allclose(two.z_mag, one.z_mag / 2.0)


def test_plane_capacitance_holds_the_high_frequency_tail():
    freqs = log_freqs(1e8, 1e9, 50)
    without = rail_impedance("+3V3", freqs, [_branch()], _target(), vrm=None)
    with_plane = rail_impedance("+3V3", freqs, [_branch()], _target(),
                                vrm=None, c_plane_f=20e-9)
    assert np.all(with_plane.z_mag < without.z_mag)


def test_plane_capacitance_formula():
    # 100 mm × 100 mm plane pair, 0.2 mm apart, Dk 4.5.
    c = plane_capacitance_f(100.0 * 100.0, 0.2, 4.5)
    assert c == pytest.approx(EPS0_F_PER_MM * 4.5 * 10000.0 / 0.2)
    assert 1e-9 < c < 5e-9                       # a couple of nanofarads
    assert plane_capacitance_f(1e4, 0.2, None) == \
        pytest.approx(plane_capacitance_f(1e4, 0.2, DEFAULT_DK))
    assert plane_capacitance_f(0.0, 0.2) == 0.0
    assert plane_capacitance_f(1e4, 0.0) == 0.0


# --- anti-resonance ------------------------------------------------------------------


def test_two_unequal_caps_create_an_antiresonance_between_their_srfs():
    """The failure mode the whole plot exists to reveal: between a bulk cap's
    self-resonance (where it turns inductive) and a small cap's (where it is
    still capacitive) the two form a parallel tank and |Z| spikes."""
    bulk = _branch("C1", c=10e-6, esr=5e-3, esl=0.9e-9, lm=1.0e-9)
    small = _branch("C2", c=100e-9, esr=25e-3, esl=0.45e-9, lm=0.5e-9)
    freqs = log_freqs(1e4, 1e9, 4000)
    r = rail_impedance("+3V3", freqs, [bulk, small], _target(), vrm=None)

    assert r.antiresonances, "no parallel resonance found"
    peak = max(r.antiresonances, key=lambda a: a.z_ohm)
    assert bulk.srf_hz < peak.freq_hz < small.srf_hz
    # The peak rises above either cap's own ESR floor.
    assert peak.z_ohm > max(bulk.esr_ohm, small.esr_ohm)


def test_a_single_cap_has_no_antiresonance():
    freqs = log_freqs(1e4, 1e9, 2000)
    r = rail_impedance("+3V3", freqs, [_branch()], _target(), vrm=None)
    assert r.antiresonances == []


def test_a_monotonic_tail_is_not_reported_as_a_peak():
    freqs = np.array([1e3, 1e4, 1e5])
    assert find_antiresonances(freqs, np.array([1.0, 2.0, 3.0])) == []
    assert find_antiresonances(freqs, np.array([3.0, 2.0, 1.0])) == []
    peaks = find_antiresonances(freqs, np.array([1.0, 3.0, 2.0]))
    assert len(peaks) == 1 and peaks[0].freq_hz == pytest.approx(1e4)


def test_antiresonance_flags_a_breach_of_the_target():
    freqs = np.array([1e3, 1e4, 1e5])
    target = _target(voltage_v=1.0, ripple_pct=5.0, transient_current_a=1.0)
    assert target.z_target_ohm == pytest.approx(0.05)
    (peak,) = find_antiresonances(freqs, np.array([0.01, 0.2, 0.01]), target)
    assert peak.exceeds_target
    (ok,) = find_antiresonances(freqs, np.array([0.01, 0.02, 0.01]), target)
    assert not ok.exceeds_target


# --- verdicts ---------------------------------------------------------------------------


def test_meets_target_and_reached_frequency():
    freqs = log_freqs(1e3, 1e9, 2000)
    # A generous target that a single 100 nF easily meets to 40 MHz.
    easy = _target(transient_current_a=0.01)     # Z_target = 16.5 Ω
    r = rail_impedance("+3V3", freqs, [_branch()], easy,
                       vrm=VrmModel(r_ohm=1e-3))
    assert r.meets_target()

    hard = _target(transient_current_a=100.0)    # Z_target = 1.65 mΩ
    r2 = rail_impedance("+3V3", freqs, [_branch()], hard,
                        vrm=VrmModel(r_ohm=1e-3))
    assert not r2.meets_target()
    # It holds up to some frequency, then breaches — and that frequency is
    # inside the swept band.
    reached = r2.reached_frequency_hz()
    assert freqs[0] <= reached < hard.f_max_hz


def test_worst_peak_only_considers_frequencies_below_f_max():
    bulk = _branch("C1", c=10e-6, esr=5e-3, esl=0.9e-9, lm=1.0e-9)
    small = _branch("C2", c=100e-9, esr=25e-3, esl=0.45e-9, lm=0.5e-9)
    freqs = log_freqs(1e4, 1e9, 4000)
    peak_f = max(
        rail_impedance("+3V3", freqs, [bulk, small], _target(), vrm=None)
        .antiresonances, key=lambda a: a.z_ohm).freq_hz

    below = rail_impedance("+3V3", freqs, [bulk, small],
                           _target(f_max_hz=peak_f * 2), vrm=None)
    above = rail_impedance("+3V3", freqs, [bulk, small],
                           _target(f_max_hz=peak_f / 2), vrm=None)
    assert below.worst_peak is not None
    assert above.worst_peak is None or above.worst_peak.freq_hz < peak_f


# --- branch construction from parts --------------------------------------------------------


def test_cap_branch_uses_the_package_library():
    lib = PackageLibrary()
    branch, reason = cap_branch("C1", 100e-9, 0.5e-9, "0402", lib)
    assert reason == "" and branch is not None
    assert branch.esl_h == pytest.approx(lib.get("0402").esl_h)
    assert branch.esr_ohm == pytest.approx(lib.get("0402").esr_ohm)
    assert not branch.esl_is_override and not branch.esr_is_override


def test_per_part_override_beats_the_library():
    lib = PackageLibrary()
    branch, _ = cap_branch("C1", 100e-9, 0.5e-9, "0402", lib,
                           esr_override=3e-3, esl_override=0.2e-9)
    assert branch.esr_ohm == pytest.approx(3e-3)
    assert branch.esl_h == pytest.approx(0.2e-9)
    assert branch.esl_is_override and branch.esr_is_override


def test_unsupported_package_is_excluded_with_a_reason():
    branch, reason = cap_branch("C9", 100e-6, 0.5e-9, None, PackageLibrary())
    assert branch is None
    assert "unsupported package" in reason and "SMD" in reason


def test_an_override_admits_an_unsupported_package():
    """A tantalum brick has no case-size code, but the user can still model
    it by giving both parasitics explicitly."""
    branch, reason = cap_branch("C9", 100e-6, 0.5e-9, None, PackageLibrary(),
                                esr_override=50e-3, esl_override=2.5e-9)
    assert reason == "" and branch is not None
    assert branch.package is None


def test_missing_inputs_are_explained_not_guessed():
    lib = PackageLibrary()
    assert cap_branch("C1", None, 0.5e-9, "0402", lib)[1] == \
        "no capacitance value parsed from the part"
    assert cap_branch("C1", 100e-9, None, "0402", lib)[1] == \
        "no mounted loop inductance"


def test_rail_impedance_carries_the_skipped_list_through():
    r = rail_impedance("+3V3", log_freqs(1e5, 1e6, 10), [_branch()],
                       _target(), skipped=[("C9", "unsupported package")])
    assert r.skipped == [("C9", "unsupported package")]
