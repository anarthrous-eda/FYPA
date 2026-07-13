"""Per-rail PDN impedance Z(f) — the third classic PDN parameter.

Where :mod:`fypa.caploop.tier1`–:mod:`~fypa.caploop.tier3` extract the loop
inductance of each decoupling capacitor from geometry, this module turns those
numbers into the quantity a designer actually signs off on: the impedance the
IC sees looking into its power rail, across frequency, against a target mask.

Per TI SWPA222A §4 the target is

    Z_target = V_rail · ripple% / I_transient

held from DC to F_MAX, the frequency beyond which adding capacitors no longer
brings |Z| down because plane spreading and package inductance dominate.

The model is the classic lumped one. Every element sits in parallel between the
rail and its return, seen from the IC:

* each capacitor — ``Z = ESR + jω(ESL + L_mount) + 1/(jωC)``, where ``L_mount``
  is what FYPA extracted (Tier 3 if solved, else Tier 2, else Tier 1) and
  ``ESL``/``ESR`` come from the package library or a per-part override;
* the VRM — ``Z = R + jωL``, which sets the low-frequency floor;
* the plane pair itself — ``Z = 1/(jωC_plane)``, ``C_plane = ε0·εr·A/h``.

``Z(f) = 1 / Σ 1/Z_k``. Two capacitors whose branches are inductive and
capacitive respectively at the same frequency form a parallel resonance: the
denominator nearly cancels and |Z| spikes. Those **anti-resonances** — not the
individual minima — are what a decoupling strategy lives or dies by, so they
are found and reported explicitly.

Limitations worth knowing: this is a lumped model, so it cannot see plane
*resonances* (the board becoming a cavity a half-wavelength across) and it is
therefore trustworthy below the first of them — a few hundred MHz on a typical
board. It also ignores the cap↔cap mutual inductance available in
:class:`~fypa.caploop.tier2_fem.CavityMatrix`; that matters most when many caps
share one cavity and would raise the anti-resonance peaks slightly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

# ε0 in farads per millimetre (ε0 = 8.8541878e-12 F/m), to match the repo's
# millimetre geometry convention.
EPS0_F_PER_MM: float = 8.8541878128e-15

# Fallback relative permittivity when the stackup carries no Dk (FR-4).
DEFAULT_DK: float = 4.5


@dataclass(frozen=True, slots=True)
class CapBranch:
    """One capacitor as a series R-L-C branch."""
    designator: str
    capacitance_f: float
    esr_ohm: float
    esl_h: float             # the part's own ESL
    l_mount_h: float         # the board's mounted loop inductance
    package: str | None = None
    esl_is_override: bool = False
    esr_is_override: bool = False

    @property
    def total_l_h(self) -> float:
        return self.esl_h + self.l_mount_h

    @property
    def srf_hz(self) -> float:
        """Series self-resonant frequency — where this branch is at its
        lowest impedance (``|Z| = ESR``)."""
        lc = self.total_l_h * self.capacitance_f
        if lc <= 0.0:
            return math.inf
        return 1.0 / (2.0 * math.pi * math.sqrt(lc))


@dataclass(frozen=True, slots=True)
class VrmModel:
    """The regulator's output impedance, as a series R + L.

    A real VRM's control loop makes it look resistive up to its bandwidth and
    inductive above; ``r_ohm`` is that closed-loop output resistance and
    ``l_h`` the output inductance including the sense/plane path. Defaults are
    a plausible point-of-load converter, not any particular part.
    """
    r_ohm: float = 0.002
    l_h: float = 5e-9

    def impedance(self, omega: np.ndarray) -> np.ndarray:
        return self.r_ohm + 1j * omega * self.l_h


@dataclass(frozen=True, slots=True)
class RailTarget:
    """The target-impedance mask for one rail (TI SWPA222A §4)."""
    rail: str
    voltage_v: float
    ripple_pct: float = 5.0
    transient_current_a: float = 1.0
    f_max_hz: float = 40e6

    @property
    def z_target_ohm(self) -> float:
        """``V · ripple% / I_transient``; infinite when no current is declared
        (nothing to hold the rail down against)."""
        if self.transient_current_a <= 0.0:
            return math.inf
        return (self.voltage_v * self.ripple_pct / 100.0) \
            / self.transient_current_a


@dataclass(frozen=True, slots=True)
class Antiresonance:
    """A parallel-resonance peak in |Z(f)|."""
    freq_hz: float
    z_ohm: float
    exceeds_target: bool


@dataclass
class RailImpedance:
    """The solved Z(f) for one rail, plus everything the plot needs."""
    rail: str
    freqs_hz: np.ndarray
    z_ohm: np.ndarray                 # complex
    target: RailTarget
    branches: list[CapBranch]
    vrm: VrmModel
    c_plane_f: float
    antiresonances: list[Antiresonance]
    skipped: list[tuple[str, str]]    # (designator, reason)

    @property
    def z_mag(self) -> np.ndarray:
        return np.abs(self.z_ohm)

    @property
    def worst_peak(self) -> Antiresonance | None:
        below = [a for a in self.antiresonances
                 if a.freq_hz <= self.target.f_max_hz]
        return max(below, key=lambda a: a.z_ohm) if below else None

    def meets_target(self) -> bool:
        """Does |Z| stay under the mask from DC to F_MAX?"""
        if not math.isfinite(self.target.z_target_ohm):
            return True
        band = self.freqs_hz <= self.target.f_max_hz
        if not band.any():
            return True
        return bool(np.max(self.z_mag[band]) <= self.target.z_target_ohm)

    def reached_frequency_hz(self) -> float:
        """The highest frequency, starting from DC, below which |Z| never
        breaches the mask. This is the honest reading of "how far does the
        decoupling actually hold" when the target isn't met."""
        z_t = self.target.z_target_ohm
        if not math.isfinite(z_t):
            return float(self.freqs_hz[-1])
        over = np.flatnonzero(self.z_mag > z_t)
        if over.size == 0:
            return float(self.freqs_hz[-1])
        return float(self.freqs_hz[max(over[0] - 1, 0)])


# --- branch construction -------------------------------------------------------

def cap_branch(
    designator: str,
    capacitance_f: float | None,
    l_mount_h: float | None,
    package: str | None,
    library,
    esr_override: float | None = None,
    esl_override: float | None = None,
) -> tuple[CapBranch | None, str]:
    """Build one capacitor's branch, or explain why it can't take part.

    A per-part override wins over the package library, and supplies both values
    for a part the library can't classify — that is how a tantalum brick or a
    through-hole electrolytic gets into the model at all.
    """
    if not capacitance_f or capacitance_f <= 0.0:
        return None, "no capacitance value parsed from the part"
    if l_mount_h is None:
        return None, "no mounted loop inductance"

    model = library.get(package)
    esl = esl_override if esl_override is not None else (
        model.esl_h if model else None)
    esr = esr_override if esr_override is not None else (
        model.esr_ohm if model else None)
    if esl is None or esr is None:
        return None, (
            "unsupported package (not an SMD chip capacitor) — "
            "set ESL and ESR on this part to include it")
    return CapBranch(
        designator=designator,
        capacitance_f=capacitance_f,
        esr_ohm=esr,
        esl_h=esl,
        l_mount_h=l_mount_h,
        package=package,
        esl_is_override=esl_override is not None,
        esr_is_override=esr_override is not None,
    ), ""


def plane_capacitance_f(area_mm2: float, h_mm: float,
                        dk: float | None = None) -> float:
    """Parallel-plate capacitance of a plane pair: ``ε0·εr·A/h``.

    Small — tens of nanofarads on a big board — but it is the only thing
    holding the rail down above the last capacitor's self-resonance, so it sets
    the high-frequency tail.
    """
    if area_mm2 <= 0.0 or h_mm <= 0.0:
        return 0.0
    return EPS0_F_PER_MM * (dk or DEFAULT_DK) * area_mm2 / h_mm


# --- the solve -------------------------------------------------------------------

def log_freqs(f_min_hz: float = 1e3, f_max_hz: float = 1e9,
              points: int = 600) -> np.ndarray:
    return np.logspace(math.log10(f_min_hz), math.log10(f_max_hz), points)


def branch_impedance(branch: CapBranch, omega: np.ndarray) -> np.ndarray:
    return (branch.esr_ohm
            + 1j * (omega * branch.total_l_h
                    - 1.0 / (omega * branch.capacitance_f)))


def rail_impedance(
    rail: str,
    freqs_hz: np.ndarray,
    branches: list[CapBranch],
    target: RailTarget,
    vrm: VrmModel | None = None,
    c_plane_f: float = 0.0,
    skipped: list[tuple[str, str]] | None = None,
) -> RailImpedance:
    """Parallel-combine every branch and locate the anti-resonances."""
    omega = 2.0 * math.pi * np.asarray(freqs_hz, dtype=np.float64)
    admittance = np.zeros_like(omega, dtype=np.complex128)

    for branch in branches:
        admittance += 1.0 / branch_impedance(branch, omega)
    if vrm is not None:
        admittance += 1.0 / vrm.impedance(omega)
    if c_plane_f > 0.0:
        admittance += 1j * omega * c_plane_f

    with np.errstate(divide="ignore", invalid="ignore"):
        z = np.where(admittance == 0, np.inf + 0j, 1.0 / admittance)

    return RailImpedance(
        rail=rail, freqs_hz=np.asarray(freqs_hz, dtype=np.float64), z_ohm=z,
        target=target, branches=list(branches),
        vrm=vrm or VrmModel(), c_plane_f=c_plane_f,
        antiresonances=find_antiresonances(freqs_hz, np.abs(z), target),
        skipped=list(skipped or []),
    )


def find_antiresonances(freqs_hz, z_mag, target: RailTarget | None = None,
                        ) -> list[Antiresonance]:
    """Local maxima of |Z(f)| — the parallel resonances between branches.

    A strict interior local maximum, so a monotonic rise into the top of the
    sweep isn't reported as a peak (it isn't one; the sweep just ended).
    """
    z = np.asarray(z_mag, dtype=np.float64)
    f = np.asarray(freqs_hz, dtype=np.float64)
    if z.size < 3:
        return []
    interior = np.flatnonzero((z[1:-1] > z[:-2]) & (z[1:-1] > z[2:])) + 1
    z_t = target.z_target_ohm if target else math.inf
    return [
        Antiresonance(freq_hz=float(f[i]), z_ohm=float(z[i]),
                      exceeds_target=bool(z[i] > z_t))
        for i in interior
    ]
