"""SMD capacitor package library — typical ESL / ESR per case size.

The mounted loop inductance FYPA extracts from geometry is only half of what a
capacitor contributes to a rail's impedance; the part itself adds its own
equivalent series inductance (ESL) and resistance (ESR). Those are properties
of the component, not of the board, so they cannot be derived from the layout.

Rather than demand a vendor S-parameter model per part, FYPA ships a table of
**typical values per SMD case size**, which the user can edit, and allows a
per-part override for the capacitors that matter. The defaults are ordinary
X5R/X7R MLCC figures near self-resonance — good enough to place the rail's
anti-resonances within a factor of about two, which is what a first-pass PDN
review needs. For a sign-off number, override the handful of caps that set the
minimum with values from the vendor's model.

**Only SMD chip packages are supported.** Electrolytics, tantalum D-case bricks
and any through-hole part have neither a meaningful case-size code nor an ESL
that a table could predict; those capacitors are reported as unsupported and
excluded from the impedance model unless the user gives them an explicit
per-part ESR / ESL override.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PackageModel:
    """Typical parasitics of one SMD case size."""
    name: str
    esl_h: float
    esr_ohm: float

    @property
    def esl_nh(self) -> float:
        return self.esl_h * 1e9

    @property
    def esr_mohm(self) -> float:
        return self.esr_ohm * 1e3


def _pkg(name: str, esl_nh: float, esr_mohm: float) -> PackageModel:
    return PackageModel(name, esl_nh * 1e-9, esr_mohm * 1e-3)


# Imperial case code → typical MLCC parasitics. ESL grows with body length
# (the current has further to travel between terminations); ESR falls with
# electrode area. Reverse-geometry parts (0204, 0306, …) put the terminations
# on the long edges and so undercut the equivalent standard part's ESL.
DEFAULT_PACKAGE_MODELS: dict[str, PackageModel] = {
    "01005": _pkg("01005", 0.20, 60.0),
    "0201":  _pkg("0201",  0.30, 40.0),
    "0402":  _pkg("0402",  0.45, 25.0),
    "0603":  _pkg("0603",  0.60, 20.0),
    "0805":  _pkg("0805",  0.70, 15.0),
    "1206":  _pkg("1206",  0.90, 12.0),
    "1210":  _pkg("1210",  1.00, 10.0),
    "1812":  _pkg("1812",  1.40, 10.0),
    "2220":  _pkg("2220",  1.80, 10.0),
    # Reverse-geometry (terminations on the long sides).
    "0204":  _pkg("0204",  0.20, 25.0),
    "0306":  _pkg("0306",  0.25, 20.0),
    "0508":  _pkg("0508",  0.30, 15.0),
    "0612":  _pkg("0612",  0.35, 12.0),
}

# Metric (mm×0.1) case code → imperial. Note the collision: "0603" is imperial
# 0603 *and* the metric code for an 0201. The metric reading is only taken when
# the footprint name says so — see :func:`detect_package`.
_METRIC_TO_IMPERIAL: dict[str, str] = {
    "0402": "01005",
    "0603": "0201",
    "1005": "0402",
    "1608": "0603",
    "2012": "0805",
    "3216": "1206",
    "3225": "1210",
    "4532": "1812",
    "5750": "2220",
}

# IPC-7351 chip-capacitor land names, e.g. "CAPC1608X90N" — the digits are
# always metric.
_IPC_RE = re.compile(r"CAPC(\d{4})X\d+", re.IGNORECASE)
# KiCad-style "C_0402_1005Metric", or anything spelling out the convention.
_METRIC_RE = re.compile(r"(\d{4})\s*metric", re.IGNORECASE)
# A bare case code delimited by anything that isn't a digit.
_CODE_RE = re.compile(r"(?<!\d)(\d{4,5})(?!\d)")


def detect_package(footprint: str) -> str | None:
    """Imperial case code for an SMD footprint name, or ``None``.

    Handles the three conventions seen in the wild — a bare imperial code
    (``C_0402_SL``, ``0603``), an explicit metric one (``C_0402_1005Metric``),
    and IPC-7351 land names (``CAPC1608X90N``) whose digits are always metric.
    Returns ``None`` for anything else, which is the signal that the part is
    not an SMD chip capacitor and needs a per-part override to take part in the
    impedance model.
    """
    if not footprint:
        return None

    ipc = _IPC_RE.search(footprint)
    if ipc:
        return _METRIC_TO_IMPERIAL.get(ipc.group(1))

    metric = _METRIC_RE.search(footprint)
    if metric:
        return _METRIC_TO_IMPERIAL.get(metric.group(1))

    for match in _CODE_RE.finditer(footprint):
        code = match.group(1)
        if code in DEFAULT_PACKAGE_MODELS:
            return code
    return None


class PackageLibrary:
    """The editable case-size table, as shown in the Impedance tab's setup.

    Starts from :data:`DEFAULT_PACKAGE_MODELS`; user edits are stored per
    package name and round-trip through the ``.fypa`` project file. Only the
    values are editable — the set of packages is fixed, because it is what
    :func:`detect_package` can recognise.
    """

    def __init__(self, models: dict[str, PackageModel] | None = None) -> None:
        self._models = dict(models or DEFAULT_PACKAGE_MODELS)

    def __iter__(self):
        # Ordered by body size (the dict's insertion order), not alphabetically:
        # a table that runs 01005 → 2220 reads like a datasheet.
        return iter(self._models.values())

    def __len__(self) -> int:
        return len(self._models)

    def get(self, package: str | None) -> PackageModel | None:
        if not package:
            return None
        return self._models.get(package)

    def set_values(self, package: str, esl_h: float, esr_ohm: float) -> None:
        if package not in self._models:
            raise KeyError(f"unknown package {package!r}")
        self._models[package] = PackageModel(package, esl_h, esr_ohm)

    def reset(self) -> None:
        self._models = dict(DEFAULT_PACKAGE_MODELS)

    def is_default(self, package: str) -> bool:
        return self._models.get(package) == DEFAULT_PACKAGE_MODELS.get(package)

    def to_dict(self) -> dict:
        """Only the packages the user actually changed, so a later revision of
        the built-in defaults reaches projects that never overrode them."""
        return {
            name: {"esl_h": m.esl_h, "esr_ohm": m.esr_ohm}
            for name, m in self._models.items()
            if m != DEFAULT_PACKAGE_MODELS.get(name)
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "PackageLibrary":
        lib = cls()
        for name, values in (d or {}).items():
            if name not in lib._models:
                continue        # a package this build no longer knows
            try:
                lib.set_values(name, float(values["esl_h"]),
                               float(values["esr_ohm"]))
            except (KeyError, TypeError, ValueError):
                continue
        return lib
