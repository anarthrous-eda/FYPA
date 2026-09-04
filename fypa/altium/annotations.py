"""PDN_* annotation parser for FYPA.

Reads :class:`fypa.altium.extract.ExtractedProject` and produces typed lumped-element
directive specs (``SourceSpec``, ``SinkSpec``, ``ResistorSpec``,
``RegulatorSpec``) plus per-terminal pad resolution against the PCB. The
:mod:`fypa.altium.loader` module will turn these into padne ``Network`` objects.

Annotation schema (component parameters in the schematic)
---------------------------------------------------------
Every directive lives on a single Altium component as a set of parameters whose
names begin with ``PDN_``. ``PDN_ROLE`` selects the role; the other parameters
supply the value and the rail/return nets. Pin sets are auto-resolved by
finding the named component's pads that sit on the named net; explicit pin
overrides are honoured if supplied.

============   =============================   ==================================================
Role           Value params                    Net / pin params
============   =============================   ==================================================
SOURCE         PDN_V                           PDN_P_NET, PDN_N_NET           (overrides: *_PINS)
                                               *or* PDN_NET                  (overrides: PDN_PINS)
SINK           PDN_I                           PDN_P_NET, PDN_N_NET           (overrides: *_PINS)
                                               *or* PDN_NET                  (overrides: PDN_PINS)
SERIES         PDN_R                           PDN_P_NET, PDN_N_NET (optional) (overrides: *_PINS)
REGULATOR      PDN_V                           PDN_OUT_P_NET, PDN_OUT_N_NET,
               PDN_REGULATOR_TYPE              PDN_IN_P_NET,  PDN_IN_N_NET    (overrides: *_PINS)
               PDN_REGULATOR_EFFICIENCY        *or* PDN_GAIN (fixed override)
               PDN_QUIESCENT (optional)
============   =============================   ==================================================

Single-net (point-to-point) SOURCE / SINK
------------------------------------------
A SOURCE or SINK normally names a rail net (``PDN_P_NET``) and a return net
(``PDN_N_NET``). For a point-to-point check on a net that has no return
reference — e.g. tracing copper from a connector to a high-side switch — give
``PDN_NET`` instead of the P/N pair. The directive then has one terminal on
PCB copper; its other terminal is an ideal 0 Ω return, so the result reflects
only that net's copper voltage drop.

``PDN_NET`` and ``PDN_P_NET``/``PDN_N_NET`` are mutually exclusive on one
directive — supplying both, or neither, is an error. Single-net mode is
SOURCE/SINK only (SERIES bridges two nets; REGULATOR has four terminals).

Current still has to flow in a closed loop: a single-net analysis needs at
least one SOURCE *and* one SINK on the same net. A rail with only one type
(an "open loop") can't carry current, so it is skipped — not solved — with a
per-rail warning, while the rest of the board still solves; the directives are
kept so the viewer still draws their markers (see
:func:`fypa.altium.loader._flag_open_loop_rails`). Every SOURCE and SINK that
shares a net must use the same mode — a group cannot mix single-net and
two-terminal directives (that *is* still an error).

Multi-channel SOURCE / SINK
---------------------------
Every role supports multiple independent channels on a single part —
useful for an IC with several supply pins, each on its own rail. Channels
are addressed by appending an integer to ``PDN`` in the parameter prefix:
the legacy unindexed form (``PDN_V`` / ``PDN_P_NET`` / …) and any number of
indexed channels (``PDN1_V`` / ``PDN1_P_NET`` / …, ``PDN2_V`` / …, …)
coexist as independent channels. Each channel produces its own directive
spec.

Example — a SINK with three independent supply rails::

    PDN_ROLE   = SINK
    PDN_I      = 500mA     PDN_P_NET  = +3V3   PDN_N_NET  = GND
    PDN1_I     = 250mA     PDN1_P_NET = +1V8   PDN1_N_NET = GND
    PDN2_I     = 50mA      PDN2_P_NET = +5V    PDN2_N_NET = GND

Indices are sparse (any positive integer; gaps allowed). A channel is
"present" when it has a value param for its role (``PDNn_V`` / ``PDNn_I`` /
``PDNn_R``), a channel-defining *terminal* param, or an indexed
``PDNn_ROLE`` — and the value may be **inherited** from the unindexed
template (``PDN_V`` when ``PDN1_V`` is omitted). Modifiers (``MIN_V``,
``GAIN``, ``QUIESCENT``, ``REGULATOR_TYPE``, ``REGULATOR_EFFICIENCY``) tune
a channel that already exists and never create one, so a stray
``PDN1_MIN_V`` cannot clone the unindexed directive off the template and
double-count it. A REGULATOR is defined by its ``OUT_*`` pair alone —
``PDN_IN_*`` is the side meant to be shared. All four roles support the
same indexed-prefix scheme.

Unindexed template inheritance
------------------------------
Unindexed ``PDN_*`` parameters act as defaults for indexed channels: for
channel *n*, ``PDNn_X`` wins when set, otherwise ``PDN_X`` is used. Two
limits keep a channel from inheriting something it never asked for:

* **Role.** The template belongs to the part-wide ``PDN_ROLE``, so only
  channels of that role read it. A ``PDN1_ROLE=SERIES`` channel on a
  ``PDN_ROLE=SINK`` part inherits nothing — otherwise it would pick up the
  sink's ``PDN_P_NET`` / ``PDN_N_NET`` and drop a resistor across the rail.
  A part with *no* part-wide role is the exception (the indexed-only
  ``PDNn_ROLE`` form): its unindexed params are pure templates, so every
  channel may read them.
* **Terminal mode.** Single-net (``NET`` / ``PINS``) and two-terminal
  (``P_NET`` / ``N_NET`` / ``*_PINS``) are mutually exclusive, so a channel
  that already declares one form does not inherit the other. A legacy
  ``PDN_NET`` channel and a two-terminal ``PDN1_*`` channel coexist fine.

When at least one indexed channel exists, the legacy ``index=None`` channel
is emitted only if it is a real channel in its own right: its **own** value
param (not an inherited one) plus a *complete* terminal set for its role. A
complete set means both sides of a two-terminal pair (or ``PDN_NET`` /
``PDN_PINS`` for single-net SOURCE/SINK); for REGULATOR, both ``OUT_*``
sides — so shared ``PDN_IN_*`` / ``PDN_N_NET`` / one SERIES side stay
templates. Terminals without a value (``PDN_P_NET`` + ``PDN_N_NET`` beside
only ``PDN1_I`` / ``PDN2_I``) are a terminal template, not a channel.
Parts with only ``PDNn_ROLE`` (no part-wide ``PDN_ROLE``) always treat the
unindexed form as template-only when indexed channels exist.

Example — dual SERIES paths sharing one resistance::

    PDN_ROLE   = SERIES
    PDN_R      = 0.05
    PDN1_P_NET = VIN_A     PDN1_N_NET = VOUT_A
    PDN2_P_NET = VIN_B     PDN2_N_NET = VOUT_B

Example — dual SERIES with a shared P-side net::

    PDN_ROLE   = SERIES
    PDN_R      = 0.05
    PDN_P_NET  = VIN
    PDN1_N_NET = VOUT_A
    PDN2_N_NET = VOUT_B

Example — dual REGULATOR outputs sharing Vin / type / voltage::

    PDN_ROLE           = REGULATOR
    PDN_V              = 3.3
    PDN_REGULATOR_TYPE = LDO
    PDN_IN_P_NET       = VIN       PDN_IN_N_NET = GND
    PDN1_OUT_P_NET     = VOUT_P    PDN1_OUT_N_NET = GND
    PDN2_OUT_P_NET     = GND       PDN2_OUT_N_NET = VOUT_N

The classic pattern with a real unindexed channel *plus* indexed ones
(``PDN_I`` + ``PDN_P_NET`` + ``PDN_N_NET`` alongside ``PDN1_I`` + …) is
unchanged: unindexed keeps a complete terminal pair, so it remains a
directive.

Values support SI prefixes and units (``500mA``, ``3V3``, ``1.5k``, ``0.1``).

Per-channel role (mixed-role parts)
-----------------------------------
``PDN_ROLE`` is the part-wide **default** role for every channel. Any
channel may override it with ``PDN<n>_ROLE`` — so a single physical part
can carry channels of different roles. This is what lets one component be
both a source and a sink: e.g. a DAC that draws supply current on its power
rail (SINK) *and* drives current out of its outputs (SOURCE).

A uniform-role part needs no ``PDN<n>_ROLE`` at all — two sinks are still
just ``PDN_ROLE = SINK`` plus ``PDN_I`` / ``PDN1_I``. Only the channels that
diverge from the default carry an override, so pick the majority role as
``PDN_ROLE`` and override the exceptions.

When every active channel carries its own ``PDN<n>_ROLE``, the part-wide
``PDN_ROLE`` may be omitted entirely — e.g. a part with a SERIES channel
(``PDN1_ROLE`` / ``PDN1_R``) and a SINK channel (``PDN2_ROLE`` / ``PDN2_I``)
on different nets. Each indexed channel must then declare its role explicitly;
channels without ``PDN<n>_ROLE`` and without a part-wide default are rejected.

Example — a DAC: supply SINK (channels 0/1) plus two output SOURCEs::

    PDN_ROLE   = SINK                                    # part-wide default
    PDN_I      = 80mA    PDN_P_NET  = AVDD      PDN_N_NET  = GND
    PDN1_I     = 20mA    PDN1_P_NET = DVDD      PDN1_N_NET = GND
    PDN2_ROLE  = SOURCE                                  # this channel overrides
    PDN2_V     = 2.5     PDN2_P_NET = DAC_OUT0  PDN2_N_NET = GND
    PDN3_ROLE  = SOURCE
    PDN3_V     = 1.8     PDN3_P_NET = DAC_OUT1  PDN3_N_NET = GND

A channel's effective role selects which value param marks it "present"
(``_V`` for a SOURCE/REGULATOR channel, ``_I`` for SINK, ``_R`` for SERIES)
and which terminal / net parameters are recognised. ``PDN<n>_ROLE`` must be
one of SOURCE / SINK / SERIES / REGULATOR. (A SOURCE and a SINK on the same
*net* of one part just feed current straight back — mixed-role parts are for
channels on **different** nets; a genuine input→output converter is better
modelled with a single REGULATOR channel.)

Auto-inference for 2-pin SERIES
--------------------------------
For a SERIES directive on a 2-pin component (inductor DCR, 0Ω jumper,
ferrite bead, sense resistor, ...), if neither nets nor pin overrides are
supplied for the **only** channel on that part, the parser fills in P_NET
and N_NET automatically from the two nets the component sits on. When a
part carries more than one SERIES channel (``PDN1_R`` + ``PDN2_R``, …),
each channel must name its nets or pin overrides explicitly — auto-inference
is not attempted.

Local net resolution (multi-channel / reused sheets)
----------------------------------------------------
``PDN_P_NET``, ``PDN_N_NET``, and ``PDN_NET`` may name a **local sheet label**
(e.g. ``VCC_EFUSE`` on ``efuse.SchDoc``) even when the PCB net is channel-
qualified (``VCC_EFUSE.4``, ``S00A_SL8M7``, ``CAN.RX1``, …). FYPA does **not**
parse ``ChannelDesignatorFormatString`` — resolution is **pin-driven**:

1. Direct PCB net-name match when the parameter already names a flattened net.
2. Compiled schematic netlist → schematic pin(s) for the local label on the
   inferred sheet → PCB pad(s) on that component instance (primary path).
3. Pad net names cross-checked against netlist ``aliases`` for that pin.
4. Degraded fallback (no compiled netlist): weak suffix heuristics only.

:class:`InstanceLocalNetResolver` (cached per :class:`ExtractedProject`) performs
sheet inference for PCB-only directives, instance-scoped local-net expansion
for SERIES bridge validation, and the steps above. See the user guide section
*Local net names* in ``docs/user-guide/01-sources-and-sinks.md``.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path

import shapely.geometry

from fypa.altium.extract import (
    ExtractedProject,
    NO_NET,
    Pt2D,
    RawPad,
)
from fypa.altium_geometry import _pad_polygon


log = logging.getLogger(__name__)


ROLE_KEY: str = "PDN_ROLE"
PARAM_PREFIX: str = "PDN_"
MULTI_LAYER_PAD_LAYER_ID: int = 74

# Indexed-channel suffix on parameter names. Matches "PDN_X" (no index) and
# "PDN<n>_X" (positive integer index) so roles can carry multiple
# independent channels on one part. Index `None` is the legacy unindexed
# form; integer indices are additional channels.
_INDEXED_KEY_RE = re.compile(r"^PDN(\d+)?_(.+)$", re.IGNORECASE)

# Roles that produce a Resistor lumped element (a series resistance between
# two nets).
_RESISTOR_LIKE_ROLES: frozenset[str] = frozenset({"SERIES"})

VALID_ROLES: frozenset[str] = frozenset({"SOURCE", "SINK", "REGULATOR"}) | _RESISTOR_LIKE_ROLES

# Altium ``ComponentKind`` values that are Net Ties (altium_monkey.ComponentKind).
COMPONENT_KIND_NET_TIE_BOM: int = 3
COMPONENT_KIND_NET_TIE_NO_BOM: int = 4
NET_TIE_COMPONENT_KINDS: frozenset[int] = frozenset({
    COMPONENT_KIND_NET_TIE_BOM,
    COMPONENT_KIND_NET_TIE_NO_BOM,
})
# Synthetic SERIES resistance for auto-detected Net Ties. Must stay below
# ``fypa.altium.loader.NET_MERGE_RESISTANCE_THRESHOLD_OHM`` (0.9 mΩ) so the
# loader merges the two nets instead of inserting a fragile lumped short.
NET_TIE_BRIDGE_RESISTANCE_OHM: float = 0.5e-3

_COMMON_TERMINAL_SUFFIXES: frozenset[str] = frozenset({
    "NET", "PINS", "P_NET", "N_NET", "P_PINS", "N_PINS",
})
_KNOWN_SUFFIXES_BY_ROLE: dict[str, frozenset[str]] = {
    "SOURCE": _COMMON_TERMINAL_SUFFIXES | frozenset({"V"}),
    "SINK": _COMMON_TERMINAL_SUFFIXES | frozenset({"I", "MIN_V"}),
    "REGULATOR": frozenset({
        "V", "GAIN", "REGULATOR_TYPE", "REGULATOR_EFFICIENCY", "QUIESCENT",
        "OUT_P_NET", "OUT_N_NET", "OUT_P_PINS", "OUT_N_PINS",
        "IN_P_NET", "IN_N_NET", "IN_P_PINS", "IN_N_PINS",
    }),
    "SERIES": frozenset({"R", "P_NET", "N_NET", "P_PINS", "N_PINS"}),
}

_ALL_INHERITABLE_SUFFIXES: frozenset[str] = frozenset().union(
    *(_KNOWN_SUFFIXES_BY_ROLE.values())
)

# The two mutually exclusive terminal forms for SOURCE / SINK (see
# _terminal_mode). Template inheritance treats each as an atomic group: a
# channel that already picks one form must not inherit the other from the
# unindexed template, or it inherits its way into a "conflicts with" error.
_SINGLE_NET_SUFFIXES: frozenset[str] = frozenset({"NET", "PINS"})
_TWO_TERMINAL_SUFFIXES: frozenset[str] = frozenset({
    "P_NET", "N_NET", "P_PINS", "N_PINS",
})

# Suffixes that *define* a channel, so setting one marks that channel present
# in _resolve_channel_roles. Deliberately narrower than
# _KNOWN_SUFFIXES_BY_ROLE: the modifiers (MIN_V, GAIN, QUIESCENT,
# REGULATOR_TYPE, REGULATOR_EFFICIENCY) tune a channel that already exists,
# so treating a stray PDN1_MIN_V as a channel would clone the whole unindexed
# directive off the template and double-count the load. A REGULATOR is
# defined by its OUT pair alone — PDN_IN_* is the side meant to be shared,
# so an indexed IN never conscripts a channel into existence.
_TERMINAL_SUFFIXES_BY_ROLE: dict[str, frozenset[str]] = {
    "SOURCE": _COMMON_TERMINAL_SUFFIXES,
    "SINK": _COMMON_TERMINAL_SUFFIXES,
    "REGULATOR": frozenset({
        "OUT_P_NET", "OUT_N_NET", "OUT_P_PINS", "OUT_N_PINS",
    }),
    "SERIES": _TWO_TERMINAL_SUFFIXES,
}


# --- SI value parsing ---------------------------------------------------------

_SI_PREFIXES: dict[str, float] = {
    # Both micro signs: U+00B5 MICRO SIGN is what Altium emits, U+03BC GREEK
    # SMALL LETTER MU is what datasheets, Word and several CAD exports produce.
    # They are visually identical, so accepting only one silently drops the part.
    "f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6,
    "m": 1e-3, "":  1.0,    "k": 1e3,  "K": 1e3,
    "M": 1e6,  "G": 1e9,    "T": 1e12,
}
# Units we tolerate trailing (the unit suffix is informational; we don't enforce
# unit/role consistency — the user is responsible for putting volts on a SOURCE).
# "R" is the EE ohm shorthand (10R = 10 Ω, 10mR = 10 mΩ); without it a value
# like "10mR" left the milli prefix unapplied and parsed as 10 Ω (1000× high).
# The engineering form "0R01" (R as decimal point → 0.01 Ω) is handled
# separately by _VALUE_RE's eng_letter branch and is unaffected.
_TRAILING_UNITS: tuple[str, ...] = (
    "V", "A", "Ohm", "OHM", "ohm", "Ω", "R", "Hz", "S", "F", "H", "%",
)

_VALUE_RE = re.compile(
    r"""^\s*
    (?P<sign>[+-]?)
    (?:
        (?P<int>\d+)
        (?:
            (?P<dotfrac>\.\d*)?          # 3.3
            |
            (?P<eng_letter>[a-zA-Zµμ])     # 3V3 form: int + unit-letter + frac
            (?P<eng_frac>\d+)?
        )?
        |
        (?P<leaddot>\.\d+)               # .001 — no leading integer digit
    )
    (?P<rest>[a-zA-Zµμ%Ω]*)            # SI prefix / unit suffix
    \s*$
    """,
    re.VERBOSE,
)

_SCI_VALUE_RE = re.compile(
    r"""^\s*
    (?P<mantissa>[+-]?(?:\d+\.?\d*|\.\d+)[eE][+-]?\d+)
    (?P<rest>[a-zA-Zµμ%Ω]*)
    \s*$
    """,
    re.VERBOSE,
)

_SPACE_BEFORE_SUFFIX_RE = re.compile(
    r"^([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s+(.+)$",
)

_PARSE_VALUE_HINT = "; use forms like 100mA, 3V3, 1.5E-9"


def _normalize_si_text(text: str) -> str:
    """Collapse whitespace between numeric part and unit suffix (``100 mA`` → ``100mA``).

    Does not join a spaced exponent (``1.5 E-9`` stays as-is).
    """
    m = _SPACE_BEFORE_SUFFIX_RE.match(text)
    if m is None:
        return text
    suffix = m.group(2)
    if suffix and suffix[0] in "eE":
        return text
    return m.group(1) + suffix


_TRAILING_UNITS_LOWER = frozenset(u.lower() for u in _TRAILING_UNITS)


def _apply_si_suffix(magnitude: float, rest: str) -> float:
    if not rest:
        return magnitude
    # A trailing token that is itself a known unit (matched case-insensitively)
    # is a unit, never an SI prefix — so "F"/"f" are Farad, not femto, and the
    # two cases agree. Femto is still reachable when followed by a unit ("fF").
    if rest.lower() in _TRAILING_UNITS_LOWER:
        return magnitude
    first = rest[0]
    if first in _SI_PREFIXES and (
        len(rest) == 1
        or rest[1:].lower() in _TRAILING_UNITS_LOWER
    ):
        return magnitude * _SI_PREFIXES[first]
    return magnitude


def parse_si_value(s: str) -> float:
    """Parse a value string with SI prefix and optional unit.

    Accepts:
        ``"500mA"``, ``"100 mA"``, ``"1.5k"``, ``"0.1"``, ``"3V3"`` (engineering form → 3.3),
        ``"1MΩ"``, ``"-2.7"``, ``"1.5E-9"``, ``"2.2e+6"``.

    Raises :class:`ValueError` on unparseable input.
    """
    if s is None:
        raise ValueError("empty value")
    text = _normalize_si_text(str(s).strip())
    if not text:
        raise ValueError("empty value")

    if re.search(r"\d[eE]", text):
        sci = _SCI_VALUE_RE.match(text)
        if sci:
            magnitude = float(sci.group("mantissa"))
            rest = sci.group("rest") or ""
            return _apply_si_suffix(magnitude, rest)

    m = _VALUE_RE.match(text)
    if not m:
        raise ValueError(f"cannot parse value {text!r}")

    sign = -1.0 if m.group("sign") == "-" else 1.0
    int_part = m.group("int")
    rest = m.group("rest") or ""

    if m.group("eng_letter"):
        eng_letter = m.group("eng_letter")
        eng_frac = m.group("eng_frac") or ""
        # Engineering form: digit + SI/unit letter + (digits) — e.g. 3V3, 4k7, 2u2
        # The letter is BOTH a unit/prefix indicator and the decimal point.
        magnitude = float(f"{int_part}.{eng_frac}" if eng_frac else int_part)
        # Decide whether the letter is an SI prefix or just a unit.
        if eng_letter in _SI_PREFIXES:
            magnitude *= _SI_PREFIXES[eng_letter]
        # Allow trailing unit chars after eng form: 3V3, no _rest needed.
    else:
        if m.group("leaddot"):
            magnitude = float(m.group("leaddot"))   # ".001" → 0.001
        else:
            dotfrac = m.group("dotfrac") or ""
            magnitude = float(f"{int_part}{dotfrac}")
        magnitude = _apply_si_suffix(magnitude, rest)

    return sign * magnitude


# --- per-component parameter lookup -------------------------------------------

def _ci_get(params: dict[str, str], key: str) -> str | None:
    """Case-insensitive parameter lookup with whitespace trimming.

    Altium's parameter sheet (and copy/paste from other tools) often leaves
    a stray leading/trailing space on values — e.g. ``" SINK"`` instead of
    ``"SINK"`` — and on parameter *names* too (``"PDN_P_PINS "``). Either
    would otherwise bounce the directive: a spaced value reaches the role
    validator as an unknown role, a spaced name makes the parameter invisible
    to lookup. Strip both ends of name and value here so every downstream
    consumer gets the canonical form. An all-whitespace value is treated as
    not-present.
    """
    key_l = key.lower()
    for k, v in params.items():
        if k.strip().lower() == key_l:
            if v is None:
                return None
            stripped = str(v).strip()
            return stripped if stripped else None
    return None


def _split_pin_list(s: str | None) -> list[str] | None:
    if s is None:
        return None
    items = [t.strip() for t in re.split(r"[,\s]+", s) if t.strip()]
    return items or None


def _channel_key(suffix: str, index: int | None) -> str:
    """Compose the parameter name for a given suffix on channel ``index``.

    ``index=None`` returns the legacy unindexed key (``PDN_<suffix>``);
    any positive integer returns the indexed form (``PDN<n>_<suffix>``).
    """
    return f"PDN_{suffix}" if index is None else f"PDN{index}_{suffix}"


def _inheritable_suffixes(
    params: dict[str, str],
    index: int | None,
    role: str,
) -> frozenset[str]:
    """Suffixes channel ``index`` (effective ``role``) may inherit.

    Two narrowings, both about not inheriting something the channel never
    asked for:

    *Role* — the unindexed template belongs to the part-wide ``PDN_ROLE``, so
    only a channel of that same role may read it. A ``PDN1_ROLE=SERIES``
    channel on a ``PDN_ROLE=SINK`` part must not pick up the sink's
    ``PDN_P_NET`` / ``PDN_N_NET``: that silently bridges the two rails the
    sink sits across with a resistor nobody declared. A part with *no*
    part-wide role is the exception — its unindexed params are pure templates
    with no channel of their own, which is the indexed-only ``PDNn_ROLE``
    form — so every channel may read them.

    *Terminal mode* — a channel that already declares one of the two mutually
    exclusive SOURCE/SINK terminal forms does not inherit the other, which
    would otherwise turn a working ``PDN_NET`` + ``PDN1_P_NET``/``PDN1_N_NET``
    part into a ``_terminal_mode`` conflict.
    """
    part_role = _part_role_default(params)
    if part_role in VALID_ROLES and part_role != role:
        return frozenset()
    allowed = set(_KNOWN_SUFFIXES_BY_ROLE.get(role, _ALL_INHERITABLE_SUFFIXES))
    declares = {
        suffix for suffix in _SINGLE_NET_SUFFIXES | _TWO_TERMINAL_SUFFIXES
        if _ci_get(params, _channel_key(suffix, index)) is not None
    }
    if declares & _SINGLE_NET_SUFFIXES:
        allowed -= _TWO_TERMINAL_SUFFIXES
    if declares & _TWO_TERMINAL_SUFFIXES:
        allowed -= _SINGLE_NET_SUFFIXES
    return frozenset(allowed)


def _channel_get(
    params: dict[str, str],
    suffix: str,
    index: int | None,
    role: str,
) -> str | None:
    """Read ``PDN<n>_<suffix>``, falling back to unindexed ``PDN_<suffix>``.

    Indexed channels inherit unset parameters from the unindexed template,
    but only the suffixes :func:`_inheritable_suffixes` allows for ``role`` —
    so this agrees with what :func:`_materialize_channel_params` hands the
    per-role parsers, and role resolution cannot admit a channel the parser
    will then reject as missing its value.

    The legacy channel (``index is None``) never falls back — there is no
    further parent. ``PDN_ROLE`` is *not* read through this helper; use
    :func:`_effective_role` for role resolution.
    """
    direct = _ci_get(params, _channel_key(suffix, index))
    if direct is not None or index is None:
        return direct
    if suffix not in _inheritable_suffixes(params, index, role):
        return None
    return _ci_get(params, _channel_key(suffix, None))


def _materialize_channel_params(
    params: dict[str, str],
    index: int | None,
    role: str,
) -> dict[str, str]:
    """Copy ``params`` with unindexed templates written into indexed keys.

    For ``index is None`` returns a shallow copy unchanged. For an indexed
    channel, every suffix :func:`_inheritable_suffixes` allows that is missing
    as ``PDNn_X`` but present as ``PDN_X`` is copied onto ``PDNn_X``, so
    downstream helpers that look up exact channel keys (``_ci_get`` /
    ``_require_value``) see the effective value. ``role`` is the channel's
    *effective* role, not the part-wide default. Does not invent a
    ``PDNn_ROLE`` from ``PDN_ROLE``.
    """
    out = dict(params)
    if index is None:
        return out
    for suffix in _inheritable_suffixes(params, index, role):
        indexed_key = _channel_key(suffix, index)
        if _ci_get(out, indexed_key) is not None:
            continue
        template = _ci_get(params, _channel_key(suffix, None))
        if template is not None:
            out[indexed_key] = template
    return out


def _unindexed_has_defining_terminals(
    params: dict[str, str],
    role: str,
) -> bool:
    """True when the legacy channel has a *complete* terminal set for ``role``.

    A lone shared side (``PDN_N_NET``, ``PDN_P_NET``, ``PDN_IN_*``) is not
    enough — those stay templates when indexed channels exist. SOURCE/SINK
    may also be complete via single-net ``PDN_NET`` / ``PDN_PINS``.
    """
    if role in ("SOURCE", "SINK"):
        if (
            _ci_get(params, _channel_key("NET", None)) is not None
            or _ci_get(params, _channel_key("PINS", None)) is not None
        ):
            return True
        has_p = (
            _ci_get(params, _channel_key("P_NET", None)) is not None
            or _ci_get(params, _channel_key("P_PINS", None)) is not None
        )
        has_n = (
            _ci_get(params, _channel_key("N_NET", None)) is not None
            or _ci_get(params, _channel_key("N_PINS", None)) is not None
        )
        return has_p and has_n
    if role == "SERIES":
        has_p = (
            _ci_get(params, _channel_key("P_NET", None)) is not None
            or _ci_get(params, _channel_key("P_PINS", None)) is not None
        )
        has_n = (
            _ci_get(params, _channel_key("N_NET", None)) is not None
            or _ci_get(params, _channel_key("N_PINS", None)) is not None
        )
        return has_p and has_n
    if role == "REGULATOR":
        has_out_p = (
            _ci_get(params, _channel_key("OUT_P_NET", None)) is not None
            or _ci_get(params, _channel_key("OUT_P_PINS", None)) is not None
        )
        has_out_n = (
            _ci_get(params, _channel_key("OUT_N_NET", None)) is not None
            or _ci_get(params, _channel_key("OUT_N_PINS", None)) is not None
        )
        return has_out_p and has_out_n
    return False


def _active_roles_for_discovery(
    params: dict[str, str],
    part_role: str,
) -> set[str]:
    """Roles whose value/terminal suffixes may mark a channel present.

    Uses the part-wide default plus every valid ``PDNn_ROLE`` override so
    mixed-role parts still discover SOURCE and SINK channels, while a
    leftover ``PDN1_R`` on a SINK-only part does not invent a phantom channel.
    """
    roles: set[str] = set()
    if part_role in VALID_ROLES:
        roles.add(part_role)
    for idx in _discover_channel_indices(params, "ROLE"):
        if idx is None:
            continue
        raw = _ci_get(params, _channel_key("ROLE", idx))
        if raw is None:
            continue
        role = raw.strip().upper()
        if role in VALID_ROLES:
            roles.add(role)
    return roles


def _discover_channel_indices(params: dict[str, str],
                              value_suffix: str) -> list[int | None]:
    """Return channel indices for which a value parameter is present.

    A channel is "present" iff its value param (``PDN_<value_suffix>`` for
    the unindexed channel, ``PDN<n>_<value_suffix>`` for indexed channels)
    has a non-empty value. The unindexed channel (``None``) is listed first;
    integer indices follow in ascending order. Gaps in the integer indices
    are allowed.
    """
    indices: set[int | None] = set()
    suffix_l = value_suffix.lower()
    for k, v in params.items():
        m = _INDEXED_KEY_RE.match(k.strip())
        if m is None:
            continue
        if m.group(2).lower() != suffix_l:
            continue
        if v is None or not str(v).strip():
            continue
        idx_str = m.group(1)
        indices.add(int(idx_str) if idx_str else None)
    return sorted(indices, key=lambda x: (x is not None, x or 0))


def _channel_label(designator: str, index: int | None) -> str:
    """Display label for a directive — ``"U5"`` for the unindexed channel,
    ``"U5#1"`` / ``"U5#2"`` / … for indexed channels."""
    return designator if index is None else f"{designator}#{index}"


# Value parameter suffix for each role. A channel needs this value (on the
# channel itself or inherited from the unindexed template) to be present.
_VALUE_SUFFIX_BY_ROLE: dict[str, str] = {
    "SOURCE": "V", "SINK": "I", "SERIES": "R", "REGULATOR": "V",
}


def _effective_role(params: dict[str, str], index: int | None,
                    part_role: str) -> str:
    """Effective role of one channel.

    A channel carries the part-wide ``PDN_ROLE`` (``part_role``) unless it
    sets its own ``PDN<n>_ROLE`` override — which is what lets a single part
    mix roles (e.g. a DAC that SINKs on its supply rail and SOURCEs on its
    outputs). Returns the upper-cased role; an unknown override string is
    returned as-is (callers validate against :data:`VALID_ROLES`)."""
    override = _ci_get(params, _channel_key("ROLE", index))
    if override is not None:
        return override.strip().upper()
    return part_role


def _has_indexed_role_params(params: dict[str, str]) -> bool:
    """True when at least one non-empty ``PDN<n>_ROLE`` parameter exists (n ≥ 1)."""
    for k, v in params.items():
        m = _INDEXED_KEY_RE.match(k.strip())
        if m is None or not m.group(1):
            continue
        if m.group(2).upper() != "ROLE":
            continue
        if v is not None and str(v).strip():
            return True
    return False


def _is_pdn_annotated(params: dict[str, str]) -> bool:
    """True when the part carries ``PDN_ROLE`` or explicit ``PDN<n>_ROLE`` keys."""
    if _ci_get(params, ROLE_KEY) is not None:
        return True
    return _has_indexed_role_params(params)


def _has_any_pdn_params(params: dict[str, str]) -> bool:
    """True when any ``PDN_*`` / ``PDN<n>_*`` parameter key is present."""
    return any(_INDEXED_KEY_RE.match(k.strip()) for k in params)


def _part_role_default(params: dict[str, str]) -> str:
    """Upper-cased part-wide ``PDN_ROLE``, or ``""`` when only indexed roles are set."""
    raw = _ci_get(params, ROLE_KEY)
    if raw is None:
        return ""
    return raw.strip().upper()


# --- terminal / pin resolution ------------------------------------------------

@dataclass(frozen=True)
class TerminalPin:
    """One physical pad participating in a lumped-element terminal."""
    pad_designator: str
    layer_id: int
    net_index: int          # pad's net (used by loader to pick the right (layer, net) padne Layer)
    point: Pt2D
    # Outer copper outline of the pad, in PCB mm coords — same basis as
    # ``point``. The loader passes this to padne as the Connection's
    # equipotential ``region`` so the terminal couples over the whole pad
    # footprint instead of a single point. ``None`` for degenerate pads.
    pad_polygon: shapely.geometry.Polygon | None = None
    # Owning PCB component when known. Used so P/N overlap arbitration does
    # not treat pad ``"1"`` on J2 and pad ``"1"`` on J3 as the same pin
    # (multi-connector / banana-jack sources).
    component_designator: str | None = None


@dataclass(frozen=True)
class TerminalSpec:
    """A lumped element's terminal — the set of pads electrically tied to it."""
    pins: tuple[TerminalPin, ...]
    # The net the directive named for this terminal (the PDN_*_NET value),
    # kept for display and metadata export. ``None`` when the terminal was
    # given by a *_PINS override.
    requested_net: str | None = None
    # True when ``requested_net`` is a local sheet label resolved via the
    # compiled schematic netlist rather than a direct PCB net-name match.
    resolved_via_local: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.pins


@dataclass(frozen=True, slots=True)
class PdnParameterSource:
    """One component carrying PDN_* parameters — from schematic or PCB."""

    designator: str
    schdoc_name: str
    parameters: dict[str, str]
    # When set, the directive is bound to this single PCB placement (PCB
    # parameter source after Blanket/Parameter-Set ECO).
    pcb_index: int | None = None
    # Schematic designator used for local-net lookup (source_designator when
    # the directive is PCB-sourced).
    sch_lookup_designator: str = ""

    @property
    def lookup_designator(self) -> str:
        return self.sch_lookup_designator or self.designator


def _build_pads_by_component(
    proj: ExtractedProject,
) -> dict[int, dict[str, RawPad]]:
    """Routed pads grouped by ``component_index`` then upper-cased designator."""
    out: dict[int, dict[str, RawPad]] = {}
    for p in proj.pads:
        if p.net_index == NO_NET:
            continue
        out.setdefault(p.component_index, {})[p.designator.upper()] = p
    return out


def _build_netlist_designator_index(
    netlist,
) -> dict[str, list[tuple[str, tuple[str, ...], tuple[str, ...]]]]:
    """Compiled-netlist terminals keyed by component designator.

    Each entry is ``(pin_key, net_names_upper, source_sheets)``. Built once so
    callers avoid an O(nets x terminals) rescan per directive.
    """
    out: dict[str, list[tuple[str, tuple[str, ...], tuple[str, ...]]]] = {}
    if netlist is None:
        return out
    for net in netlist.nets:
        names = tuple(
            n.upper() for n in (net.name, *getattr(net, "aliases", ())) if n
        )
        sheets = tuple(s for s in (getattr(net, "source_sheets", ()) or ()) if s)
        for term in net.terminals:
            out.setdefault(term.designator.upper(), []).append(
                (str(term.pin).upper(), names, sheets)
            )
    return out


def _schdoc_for_pcb_instance(
    proj: ExtractedProject,
    pcb_index: int,
    lookup_des: str,
    *,
    pads_by_component: dict[int, dict[str, RawPad]] | None = None,
    netlist_index: dict[str, list[tuple[str, tuple[str, ...], tuple[str, ...]]]]
    | None = None,
) -> str:
    """Infer the schematic sheet for one PCB placement.

    Uses compiled-netlist terminal ↔ pad connectivity so Blanket/ECO
    directives on a specific ``pcb_index`` stay scoped to that instance's
    sheet rather than the first ``sch_components`` row for ``lookup_des``.

    Pass ``pads_by_component`` / ``netlist_index`` (from
    :func:`_build_pads_by_component` / :func:`_build_netlist_designator_index`)
    to reuse the indexes across many directives; otherwise they are built
    on demand for this single call.
    """
    return _instance_resolver(proj).infer_schdoc(
        pcb_index,
        lookup_des,
        pads_by_component=pads_by_component,
        netlist_index=netlist_index,
    )


def _designator_candidates(
    sch_designator: str,
    pcb_designator: str | None = None,
) -> set[str]:
    candidates = {sch_designator.upper()}
    if pcb_designator:
        candidates.add(pcb_designator.upper())
    return candidates


# Local-net match quality (lower = better). Used to discard ambiguous
# alias-only hits when a stronger name-level or PCB-confirmed match exists
# (shared bare aliases across hierarchy levels are common in multi-channel
# netlists).
_LOCAL_NET_TIER_OVERRIDE = -2  # explicit PDN_*_PINS — the user named the pads
_LOCAL_NET_TIER_DIRECT = -1   # direct PCB net name match
_LOCAL_NET_TIER_PCB = 0       # netlist row lists the pad's PCB net name
_LOCAL_NET_TIER_NAME = 1      # match on compiled net.name (exact/channel)
_LOCAL_NET_TIER_ALIAS = 2     # match on a netlist alias only


def _channel_token_after_prefix(label: str, prefix: str) -> str | None:
    """If ``label`` is ``prefix`` + ``.``/``_`` + token, return the token."""
    if not label.startswith(prefix) or len(label) <= len(prefix):
        return None
    sep = label[len(prefix)]
    if sep not in "._":
        return None
    token = label[len(prefix) + 1:]
    return token or None


def _designator_has_channel_token(designator: str, token: str) -> bool:
    """True when ``designator`` ends with ``.token`` or ``_token``."""
    return designator.endswith(("." + token, "_" + token))


def _is_flattened_channel_token(des_candidates: set[str], token: str) -> bool:
    """True when ``token`` is the channel suffix Altium added when flattening.

    ``des_candidates`` holds the schematic designator and the placed one, so a
    genuine channel instance shows up as a PAIR — ``R1`` plus ``R1.1`` — where
    one is the other plus a separator and the token. Testing the suffix alone
    also accepts a part merely NAMED that way: ``FB_2`` is not channel 2 of
    ``FB``, but it ends in ``_2``, so an unrelated repeated sheet's ``VIN.2``
    would match it.
    """
    return any(
        other != d and d == other + sep + token
        for d in des_candidates
        for other in des_candidates
        for sep in "._"
    )


def _local_net_label_matches(
    label: str | None,
    local_net_name: str,
    des_candidates: set[str],
) -> bool:
    """True when ``label`` names the same local net class as ``local_net_name``.

    Channel-mangled aliases (``S00A_SL8M7``, ``S00A.4``, ``VIN_1``) are
    accepted when an instance designator in ``des_candidates`` carries the
    same channel *token*, regardless of whether the netlist used ``.`` or
    ``_`` as the separator (Altium's channel designator format and net
    annotation can disagree: designator ``R1.1`` vs net ``VIN_1``).

    Accepting either separator is deliberate, but it must not accept a part
    that merely happens to be named that way, so the token has to be the
    suffix that flattening actually added — see
    :func:`_is_flattened_channel_token`.
    """
    if not label:
        return False
    ln = local_net_name.upper()
    lu = label.upper()
    if lu == ln:
        return True
    channel = _channel_token_after_prefix(lu, ln)
    if (channel
            and any(_designator_has_channel_token(d, channel)
                    for d in des_candidates)
            and _is_flattened_channel_token(des_candidates, channel)):
        return True
    return False


def _channel_suffix_from_pcb_designator(pcb_designator: str) -> str | None:
    """Numeric channel tail from a flattened PCB designator (degraded mode only).

    Altium's ``$Component.$ChannelIndex`` room style yields ``R63.4`` → ``4``.
    Non-numeric tails (``J3_SL8M7``) are handled via netlist aliases, not here.
    """
    if "." not in pcb_designator:
        return None
    suffix = pcb_designator.rsplit(".", 1)[-1]
    return suffix if suffix.isdigit() else None


def _degraded_pcb_net_candidates(
    local_net_name: str,
    pcb_designator: str,
) -> list[str]:
    """Last-resort suffixed PCB net guesses when the compiled netlist is
    unavailable.

    Only channel-suffix guesses are returned — the caller has already tried
    ``local_net_name`` as a direct PCB net name before falling back here.
    """
    if "." in local_net_name:
        return []
    suffix = _channel_suffix_from_pcb_designator(pcb_designator)
    if not suffix:
        return []
    return [f"{local_net_name}.{suffix}"]


@dataclass
class InstanceLocalNetResolver:
    """Pin-driven local-net resolution for one :class:`ExtractedProject`."""

    proj: ExtractedProject
    _expanded_cache: dict[str, tuple[str, ...]] = field(
        default_factory=dict, repr=False,
    )
    _pads_by_component: dict[int, dict[str, RawPad]] | None = field(
        default=None, repr=False,
    )
    _netlist_index: (
        dict[str, list[tuple[str, tuple[str, ...], tuple[str, ...]]]] | None
    ) = field(default=None, repr=False)
    _sheet_map: dict[str, str] | None = field(default=None, repr=False)

    def sheet_map(self) -> dict[str, str]:
        """Memoised physical-page-id → logical-sheet-name map.

        Consulted once per candidate net inside the netlist scans and again
        per placement in :meth:`infer_schdoc`, so building it per call is
        O(pages) work repeated thousands of times on a hierarchical board.
        """
        if self._sheet_map is None:
            self._sheet_map = _physical_sheet_file_map(self.proj)
        return self._sheet_map

    def pads_index(self) -> dict[int, dict[str, RawPad]]:
        """Memoized :func:`_build_pads_by_component` for this project."""
        if self._pads_by_component is None:
            self._pads_by_component = _build_pads_by_component(self.proj)
        return self._pads_by_component

    def designator_index(
        self,
    ) -> dict[str, list[tuple[str, tuple[str, ...], tuple[str, ...]]]]:
        """Memoized :func:`_build_netlist_designator_index` for this project."""
        if self._netlist_index is None:
            self._netlist_index = _build_netlist_designator_index(
                self.proj.compiled_netlist,
            )
        return self._netlist_index

    def infer_schdoc(
        self,
        pcb_index: int,
        lookup_des: str,
        *,
        pads_by_component: dict[int, dict[str, RawPad]] | None = None,
        netlist_index: dict[str, list[tuple[str, tuple[str, ...], tuple[str, ...]]]]
        | None = None,
    ) -> str:
        if not lookup_des:
            return ""
        pcb_designator = self.proj.pcb_components[pcb_index].designator
        des_candidates = _designator_candidates(lookup_des, pcb_designator)
        if pads_by_component is None:
            pads_by_component = self.pads_index()
        routed_pads = pads_by_component.get(pcb_index, {})

        sheet_votes: dict[str, int] = {}
        sheet_paths: dict[str, str] = {}

        if routed_pads:
            if netlist_index is None:
                netlist_index = self.designator_index()
            for pin_key, pad in routed_pads.items():
                pcb_net_upper = self.proj.nets[pad.net_index].name.upper()
                for des_key in des_candidates:
                    for nl_pin, names, sheets in netlist_index.get(des_key, ()):
                        if nl_pin != pin_key:
                            continue
                        vote_weight = 1
                        if pcb_net_upper in names:
                            vote_weight = 2
                        for sheet in sheets:
                            key = sheet.replace("\\", "/").lower()
                            sheet_votes[key] = sheet_votes.get(key, 0) + vote_weight
                            sheet_paths.setdefault(key, sheet)

        if sheet_votes:
            best = max(sorted(sheet_votes), key=lambda k: sheet_votes[k])
            top = sheet_votes[best]
            if sum(1 for v in sheet_votes.values() if v == top) > 1:
                log.debug(
                    "Ambiguous sheet vote for %s (pcb_index=%d): %s; choosing %s",
                    lookup_des, pcb_index,
                    sorted(k for k, v in sheet_votes.items() if v == top),
                    sheet_paths[best],
                )
            return sheet_paths[best]

        sch_matches = [
            c.schdoc_name for c in self.proj.sch_components
            if c.designator.upper() == lookup_des.upper()
        ]
        if len(sch_matches) == 1:
            return sch_matches[0]
        return ""

    def expand_net_names(
        self,
        local_name: str,
        pcb_index: int,
    ) -> tuple[str, ...]:
        """All PCB / netlist labels equivalent to a local schematic net name,
        scoped to one PCB placement so channel slots of a repeated sheet
        (``VCC_EFUSE.1`` vs ``VCC_EFUSE.4``) never merge across instances.
        """
        cache_key = f"{local_name.upper()}\0{pcb_index}"
        cached = self._expanded_cache.get(cache_key)
        if cached is not None:
            return cached

        names = {local_name.upper()}

        if self.proj.compiled_netlist is not None:
            pads_by = self.pads_index()
            pcb = self.proj.pcb_components[pcb_index]
            lookup_des = pcb.source_designator or pcb.designator
            schdoc = self.infer_schdoc(
                pcb_index, lookup_des, pads_by_component=pads_by,
            )
            routed = pads_by.get(pcb_index, {})
            if routed:
                # _build_pads_by_component already drops NO_NET pads.
                pcb_net_by_pin = {
                    pin_key: self.proj.nets[pad.net_index].name.upper()
                    for pin_key, pad in routed.items()
                }
                local_pins, _tier = _resolve_local_net_pins(
                    self.proj.compiled_netlist,
                    lookup_des,
                    schdoc,
                    local_name,
                    routed_pin_keys=set(pcb_net_by_pin) or None,
                    pcb_designator=pcb.designator,
                    pcb_net_by_pin=pcb_net_by_pin or None,
                    sheet_map=self.sheet_map(),
                    # Equivalence expansion, not terminal selection: this feeds
                    # the SERIES bridge union and the net-merge canonical, both
                    # of which need EVERY equivalent PCB net name. Keeping only
                    # the best tier silently shrinks the class and can make the
                    # analysis-group check reject a topology it accepted.
                    best_tier_only=False,
                )
                wanted = {p.upper() for p in local_pins}
                for pin_key, pad in routed.items():
                    if pin_key in wanted and pad.net_index != NO_NET:
                        names.add(self.proj.nets[pad.net_index].name.upper())

        result = tuple(sorted(names))
        self._expanded_cache[cache_key] = result
        return result


_resolver_cache: dict[int, tuple[ExtractedProject, InstanceLocalNetResolver]] = {}


def _instance_resolver(proj: ExtractedProject) -> InstanceLocalNetResolver:
    entry = _resolver_cache.get(id(proj))
    if entry is None or entry[0] is not proj:
        _resolver_cache.clear()
        resolver = InstanceLocalNetResolver(proj)
        _resolver_cache[id(proj)] = (proj, resolver)
        return resolver
    return entry[1]


def _iter_pdn_parameter_sources(proj: ExtractedProject) -> list[PdnParameterSource]:
    """Schematic PDN directives plus PCB-only directives (Blanket ECO path)."""
    sources: list[PdnParameterSource] = []
    sch_with_role: set[str] = set()

    for comp in proj.sch_components:
        if not _is_pdn_annotated(comp.parameters):
            continue
        sch_with_role.add(comp.designator.upper())
        sources.append(PdnParameterSource(
            designator=comp.designator,
            schdoc_name=comp.schdoc_name,
            parameters=comp.parameters,
            sch_lookup_designator=comp.designator,
        ))

    pads_by_component: dict[int, dict[str, RawPad]] | None = None
    netlist_index: (
        dict[str, list[tuple[str, tuple[str, ...], tuple[str, ...]]]] | None
    ) = None
    for idx, pcb in enumerate(proj.pcb_components):
        if not _is_pdn_annotated(pcb.parameters):
            continue
        lookup_des = pcb.source_designator or pcb.designator
        if lookup_des.upper() in sch_with_role:
            continue
        if pads_by_component is None:
            # Fetched once, only when a PCB-sourced directive actually exists.
            resolver = _instance_resolver(proj)
            pads_by_component = resolver.pads_index()
            netlist_index = resolver.designator_index()
        sources.append(PdnParameterSource(
            designator=pcb.designator,
            schdoc_name=_schdoc_for_pcb_instance(
                proj, idx, lookup_des,
                pads_by_component=pads_by_component,
                netlist_index=netlist_index,
            ),
            parameters=pcb.parameters,
            pcb_index=idx,
            sch_lookup_designator=lookup_des,
        ))
    return sources


def _pcb_indices_for_source(comp: PdnParameterSource,
                            proj: ExtractedProject) -> list[int]:
    if comp.pcb_index is not None:
        return [comp.pcb_index]
    return _find_pcb_instances(proj, comp.designator)


def _physical_sheet_file_map(proj: ExtractedProject | None) -> dict[str, str]:
    """physical page id (lower) → logical ``*.SchDoc`` name.

    Prefer :meth:`InstanceLocalNetResolver.sheet_map`, which memoises this —
    it is consulted once per candidate net inside the netlist scans, so
    rebuilding it per call is O(pages) work repeated thousands of times a load.
    """
    if proj is None:
        return {}
    return {
        pid.replace("\\", "/").lower(): fn
        for pid, fn in proj.physical_sheet_names or ()
        if pid and fn
    }


def _logical_schdoc_name(name: str, sheet_map: dict[str, str]) -> str | None:
    """Map a physical page id to its logical ``*.SchDoc`` name.

    Returns ``None`` when ``name`` is a physical page id the compiled map does
    not cover — the caller must then treat that entry as *unknown provenance*
    rather than matching on it.

    There is no reliable way to recover the sheet from the id alone: a child
    page id embeds its PARENT's logical id, so scanning it for a ``*.SchDoc``
    token yields the root sheet, not the page's own. That both hides a genuine
    match (a local net on Power.SchDoc looks like it lives on the root) and
    invents false ones (a root-sheet net appears to live on every child page).
    Guessing is worse than admitting the id is unresolved.

    A plain sheet name passes through unchanged, with directories preserved so
    ``SubA/Power.SchDoc`` and ``SubB/Power.SchDoc`` stay distinct.
    """
    if not name:
        return ""
    key = name.replace("\\", "/").lower()
    if key in sheet_map:
        return sheet_map[key]
    if key.startswith("physical:"):
        return None
    return name.replace("\\", "/")


def _sheet_name_matches(
    schdoc_name: str,
    source_sheets: list[str],
    *,
    sheet_map: dict[str, str] | None = None,
) -> bool:
    # A net with no recorded sheet provenance (e.g. a global/power net) cannot
    # contradict the inferred instance sheet, so it is accepted. This is the
    # one remaining permissive path; cross-instance mis-binding is bounded by
    # the routed-pin scoping the caller applies (see _resolve_local_net_pins).
    if not source_sheets:
        return True
    if not schdoc_name:
        return False
    sheet_map = sheet_map or {}
    target = _logical_schdoc_name(schdoc_name, sheet_map)
    if target is None:
        return False
    target_full = target.replace("\\", "/").lower()
    target_base = Path(target).name.lower()
    has_dir = "/" in target_full
    resolved_any = False
    for sheet in source_sheets:
        resolved = _logical_schdoc_name(sheet, sheet_map)
        if resolved is None:
            continue          # unmapped page id — provenance unknown, not "no"
        resolved_any = True
        s = resolved.replace("\\", "/").lower()
        if s == target_full:
            return True
        if not has_dir and Path(resolved).name.lower() == target_base:
            return True
    # Every entry was an unresolvable page id, so this net carries no usable
    # provenance. Fall through to the same permissive rule as a net with no
    # source_sheets at all rather than rejecting on a guess.
    return not resolved_any


def _resolve_local_net_pins(
    netlist,
    sch_designator: str,
    schdoc_name: str,
    local_net_name: str,
    *,
    routed_pin_keys: set[str] | None = None,
    pcb_designator: str | None = None,
    pcb_net_by_pin: dict[str, str] | None = None,
    sheet_map: dict[str, str] | None = None,
    best_tier_only: bool = True,
) -> tuple[list[str], int]:
    """Return ``(pin designators, match_tier)`` for a local sheet net name.

    ``pcb_designator`` is the placed instance's (possibly channel-flattened)
    designator, e.g. ``J3_SL8M3``. In a repeated ("multi-channel") sheet the
    compiled netlist keys both terminals and mangled net-label aliases by that
    flattened form, whereas ``sch_designator`` is the base schematic designator
    (``J3``); terminal and alias matching accept either.

    When several netlist nets share a bare local alias, only the strongest
    match tier is kept:

    * :data:`_LOCAL_NET_TIER_PCB` — the pad's PCB net name itself is a
      channel form of the local label (``VIN_1`` for local ``VIN``);
    * :data:`_LOCAL_NET_TIER_NAME` — ``net.name`` matches the local label
      (exact or channel-token);
    * :data:`_LOCAL_NET_TIER_ALIAS` — only an alias matched.

    ``pcb_net_by_pin`` maps upper-cased pin designators to upper-cased PCB
    net names for the PCB-confirmed tier.
    """
    if netlist is None:
        return [], _LOCAL_NET_TIER_ALIAS
    des_candidates = _designator_candidates(sch_designator, pcb_designator)

    # (tier, pin) candidates; lower tier wins.
    scored: list[tuple[int, str]] = []
    unscoped_used = False
    for net in netlist.nets:
        aliases = list(getattr(net, "aliases", ()) or ())
        name_match = _local_net_label_matches(
            net.name, local_net_name, des_candidates,
        )
        alias_match = any(
            _local_net_label_matches(a, local_net_name, des_candidates)
            for a in aliases
        )
        if not name_match and not alias_match:
            continue
        net_sheets = list(getattr(net, "source_sheets", ()) or ())
        if not _sheet_name_matches(schdoc_name, net_sheets,
                                   sheet_map=sheet_map):
            continue
        base_tier = (
            _LOCAL_NET_TIER_NAME if name_match else _LOCAL_NET_TIER_ALIAS
        )
        for term in net.terminals:
            if term.designator.upper() not in des_candidates:
                continue
            pin = str(term.pin)
            key = pin.upper()
            if routed_pin_keys is not None and key not in routed_pin_keys:
                continue
            tier = base_tier
            if pcb_net_by_pin is not None:
                pcb_n = pcb_net_by_pin.get(key)
                # PCB-confirmed only when the physical net name itself is a
                # channel form of the requested local label (VIN_1 / VIN.1 for
                # local VIN). Being listed on the row is not enough — every
                # terminal's primary name is on its own row, so that would
                # promote alias-only hits to the top tier and erase ranking.
                if pcb_n and _local_net_label_matches(
                    pcb_n, local_net_name, des_candidates,
                ):
                    tier = _LOCAL_NET_TIER_PCB
            scored.append((tier, pin, bool(schdoc_name and not net_sheets)))
    if not scored:
        return [], _LOCAL_NET_TIER_ALIAS
    best = min(t for t, _, _ in scored)
    pins: list[str] = []
    seen: set[str] = set()
    for tier, pin, unscoped in scored:
        if best_tier_only and tier != best:
            continue
        key = pin.upper()
        if key not in seen:
            seen.add(key)
            pins.append(pin)
            unscoped_used = unscoped_used or unscoped
    # Reported only for nets that actually contributed a RETURNED pin.
    # Flagging every scored candidate fired this for rows the tier ranking
    # discarded — a false trail when debugging a mis-mapped instance.
    if unscoped_used:
        log.debug(
            "Local net %r on %s resolved via net(s) lacking sheet provenance; "
            "match scoped by routed pins only.",
            local_net_name, sch_designator,
        )
    return pins, best


def _terminal_layer_for_pad(pad: RawPad, enabled_layers: list[int]) -> int:
    """For SMT pads → their layer; for through-hole pads → topmost enabled copper layer.

    The geometry module places through-hole copper on every layer and the via barrel
    couples them, so attaching the lumped element to the top layer is sufficient.
    """
    if pad.is_through_hole or pad.layer_id == MULTI_LAYER_PAD_LAYER_ID:
        return enabled_layers[0]
    return pad.layer_id


def _resolve_alias_fallback_pads(
    proj: ExtractedProject,
    component_pads: list[RawPad],
    net_name: str,
    sch_lookup_designator: str,
    schdoc_name: str,
    pcb_designator: str,
) -> list[RawPad]:
    """Match pads via compiled-netlist aliases when pin-local resolution failed.

    Only accepts a pad when its pin appears on a netlist row for the schematic
    designator, the row's label class matches ``net_name``, the pad's PCB net is
    listed on that row, and the row's sheet matches ``schdoc_name``. The broad
    ``family`` match (all pads on any equivalent label) is intentionally omitted
    to avoid cross-channel leaks when several channel nets share one local alias.
    """
    if proj.compiled_netlist is None:
        return []
    netlist_index = _instance_resolver(proj).designator_index()
    des_candidates = _designator_candidates(sch_lookup_designator, pcb_designator)
    sheet_map = _instance_resolver(proj).sheet_map()
    matched: list[RawPad] = []
    seen_pins: set[str] = set()
    for pad in component_pads:
        if pad.net_index == NO_NET:
            continue
        pin_key = pad.designator.upper()
        if pin_key in seen_pins:
            continue
        pcb_n = proj.nets[pad.net_index].name.upper()
        for des_key in des_candidates:
            for nl_pin, names, sheets in netlist_index.get(des_key, ()):
                if nl_pin != pin_key or pcb_n not in names:
                    continue
                if not any(
                    _local_net_label_matches(n, net_name, des_candidates)
                    for n in names
                ):
                    continue
                if not _sheet_name_matches(schdoc_name, list(sheets),
                                           sheet_map=sheet_map):
                    continue
                matched.append(pad)
                seen_pins.add(pin_key)
                break
            if pin_key in seen_pins:
                break
    return matched


def _resolve_terminal(
    proj: ExtractedProject,
    pcb_index: int,
    net_name: str | None,
    override_pins: list[str] | None,
    enabled_layers: list[int],
    role_diagnostic: str,
    warnings: list[str] | None = None,
    net_remap: dict[int, int] | None = None,
    sch_lookup_designator: str | None = None,
    schdoc_name: str | None = None,
) -> tuple[TerminalSpec | None, list[str], int]:
    """Resolve a terminal to its participating pads.

    ``pcb_index`` indexes :attr:`ExtractedProject.pcb_components` — one
    specific placed component. Callers obtain it from
    :func:`_find_pcb_instances`, which is what tells the channels of a
    multi-channel part apart (their PCB designators may be identical).

    Collects every pad on that component whose PCB connectivity (or schematic
    local-net fallback) matches the named net directly. SERIES bridge
    equivalence classes are *not* consulted here — those belong to the
    solver's net graph, not terminal pin selection.

    Returns ``(spec, errors, match_tier)``. If ``errors`` is non-empty,
    ``spec`` is ``None`` — EXCEPT on the partial pin-override path, where an
    unmatched entry in ``PDN_*_PINS`` records an error while the pads that did
    match still yield a spec. Callers that arbitrate between terminals must
    therefore check ``errors`` as well as ``spec``.
    ``match_tier`` is a :data:`_LOCAL_NET_TIER_*`
    constant used by :func:`_resolve_two_terminal` to arbitrate overlapping
    P/N pin sets.
    """
    errors: list[str] = []
    resolved_via_local = False
    match_tier = _LOCAL_NET_TIER_DIRECT
    designator = proj.pcb_components[pcb_index].designator
    component_pads = _pads_by_component_all(proj).get(pcb_index, [])
    if not component_pads:
        errors.append(
            f"{role_diagnostic}: component {designator!r} has no pads on the PCB"
        )
        return None, errors, match_tier

    if override_pins:
        # Outranks every inferred match. Both paths used to return DIRECT, so
        # a user told to "set PDN_P_PINS / PDN_N_PINS to disambiguate" got the
        # same equal-tier error back and had no way out of it.
        match_tier = _LOCAL_NET_TIER_OVERRIDE
        wanted = {pin.upper() for pin in override_pins}
        matched = [p for p in component_pads if p.designator.upper() in wanted]
        missing = wanted - {p.designator.upper() for p in matched}
        if missing:
            errors.append(
                f"{role_diagnostic}: pin overrides on {designator} not found: "
                f"{sorted(missing)}"
            )
        if not matched:
            return None, errors, match_tier
    else:
        if not net_name:
            errors.append(
                f"{role_diagnostic}: neither a net nor pin overrides supplied"
            )
            return None, errors, match_tier
        net_indices = _net_indices_by_name(proj, net_name)
        matched: list[RawPad] = []
        if net_indices:
            # Apply the loader's net-merge remap so user annotations naming
            # EITHER side of an absorbed SERIES bridge (e.g. both "0V" and
            # "Pgnd" when R2 merged them) resolve to the same canonical
            # net_index — which is what the primitives' net_index was rewritten
            # to in _apply_net_remap.
            if net_remap:
                net_indices = [net_remap.get(ix, ix) for ix in net_indices]
            # A multi-channel net name covers several distinct nets; this
            # component sits in exactly one channel, so matching its own pads
            # against the whole name-class still selects only its channel's net.
            wanted_nets = set(net_indices)
            matched = [p for p in component_pads if p.net_index in wanted_nets]

        if not matched and sch_lookup_designator:
            pcb_net_by_pin = {
                p.designator.upper(): proj.nets[p.net_index].name.upper()
                for p in component_pads
                if p.net_index != NO_NET
            }
            routed_pin_keys = set(pcb_net_by_pin)
            local_pins, local_tier = _resolve_local_net_pins(
                proj.compiled_netlist,
                sch_lookup_designator,
                schdoc_name or "",
                net_name,
                routed_pin_keys=routed_pin_keys or None,
                pcb_designator=designator,
                pcb_net_by_pin=pcb_net_by_pin or None,
                sheet_map=_instance_resolver(proj).sheet_map(),
            )
            if local_pins:
                wanted_pins = {pin.upper() for pin in local_pins}
                matched = [
                    p for p in component_pads
                    if p.designator.upper() in wanted_pins
                    and p.net_index != NO_NET
                ]
                if matched:
                    resolved_via_local = True
                    match_tier = local_tier
                    if warnings is not None:
                        pcb_net_names = sorted({
                            proj.nets[p.net_index].name
                            for p in matched
                            if p.net_index != NO_NET
                        })
                        nets_text = ", ".join(pcb_net_names) if pcb_net_names else "?"
                        warnings.append(
                            f"{role_diagnostic}: resolved local net "
                            f"{net_name!r} via schematic pins "
                            f"{sorted(local_pins)} → PCB net(s) {nets_text}"
                        )

        if (
            not matched
            and sch_lookup_designator
            and proj.compiled_netlist is not None
        ):
            alias_matched = _resolve_alias_fallback_pads(
                proj,
                component_pads,
                net_name,
                sch_lookup_designator,
                schdoc_name or "",
                designator,
            )
            if alias_matched:
                matched = alias_matched
                # ALIAS, not PCB. "The pad's PCB net is listed on the row" is
                # the criterion _resolve_local_net_pins explicitly rejects for
                # the PCB tier: every terminal's primary name appears on its
                # own row, so promoting on it erases the ranking. Stamping
                # tier 0 here let an alias-only hit outrank a genuine net.name
                # match on the opposite terminal, and the arbitrator then
                # stripped the stronger side to empty and failed the solve.
                match_tier = _LOCAL_NET_TIER_ALIAS
                # NOT resolved_via_local: that flag is not a tier concept.
                # loader.build_solve_metadata exports it and rail_groups
                # branches on it to choose a rail's display name, so setting
                # it here silently renamed rails in the GUI for any board
                # whose SOURCE resolves by alias. The tier is carried by
                # match_tier, which is what the arbitration actually reads.
                if warnings is not None:
                    pcb_net_names = sorted({
                        proj.nets[p.net_index].name
                        for p in matched
                        if p.net_index != NO_NET
                    })
                    nets_text = ", ".join(pcb_net_names) if pcb_net_names else "?"
                    warnings.append(
                        f"{role_diagnostic}: resolved local net "
                        f"{net_name!r} via netlist alias on pin(s) "
                        f"{sorted(p.designator for p in matched)} "
                        f"→ PCB net(s) {nets_text}"
                    )

        if not matched and proj.compiled_netlist is None:
            for candidate in _degraded_pcb_net_candidates(net_name, designator):
                candidate_indices = _net_indices_by_name(proj, candidate)
                if not candidate_indices:
                    continue
                if net_remap:
                    candidate_indices = [
                        net_remap.get(ix, ix) for ix in candidate_indices
                    ]
                wanted_nets = set(candidate_indices)
                matched = [p for p in component_pads if p.net_index in wanted_nets]
                if matched:
                    if warnings is not None:
                        warnings.append(
                            f"{role_diagnostic}: no compiled netlist — guessed "
                            f"PCB net {candidate!r} for {net_name!r} from the "
                            f"channel suffix of {designator!r}; verify this is "
                            f"the intended net"
                        )
                    break

        if not matched and not net_indices:
            # Common authoring slip is a near-miss spelling (e.g. "+3.3V" vs
            # "+3V3"). Suggest the closest extant net name(s) so the user
            # doesn't have to scan a long net list to find the right one.
            import difflib
            suggestions = difflib.get_close_matches(
                net_name, [n.name for n in proj.nets], n=3, cutoff=0.5,
            )
            hint = (
                f"  Did you mean: {', '.join(repr(s) for s in suggestions)}?"
                if suggestions else ""
            )
            local_hint = ""
            if sch_lookup_designator and proj.compiled_netlist is None:
                local_hint = "  (schematic netlist unavailable for local-net fallback.)"
            errors.append(
                f"{role_diagnostic}: net {net_name!r} does not exist on the "
                f"PCB and could not be resolved as a local schematic net."
                f"{hint}{local_hint}"
            )
            return None, errors, match_tier

        if not matched:
            # List the nets that this component's pads actually sit on, so the
            # user can either correct PDN_*_NET or realise the directive is on
            # the wrong component. Buck regulator outputs commonly trip this
            # (pin sits on switching node, rail appears after the inductor).
            pad_nets = sorted({
                proj.nets[p.net_index].name
                for p in component_pads
                if p.net_index != NO_NET
            })
            pads_listing = ", ".join(pad_nets) if pad_nets else "(no connected pads)"
            errors.append(
                f"{role_diagnostic}: component {designator} has no pad on net "
                f"{net_name!r}. {designator}'s pads connect to: {pads_listing}"
                f" (could be due a series part not setup with PDN_ROLE: SERIES)"
            )
            return None, errors, match_tier

    pins = tuple(
        TerminalPin(
            pad_designator=p.designator,
            layer_id=(_tl := _terminal_layer_for_pad(p, enabled_layers)),
            net_index=p.net_index,
            point=p.center,
            pad_polygon=_pad_polygon(p, _tl),
            component_designator=designator,
        )
        for p in matched
    )
    return TerminalSpec(
        pins=pins, requested_net=net_name, resolved_via_local=resolved_via_local,
    ), errors, match_tier


def _find_pcb_instances(proj: ExtractedProject, sch_designator: str) -> list[int]:
    """Return the indices of every PCB component placed from a schematic part.

    Matching is by the PCB component's ``source_designator`` — the schematic
    (logical) designator Altium stamps on every placed component. In a
    multi-channel design Altium re-bases the *physical* designator (schematic
    ``C118`` may be placed as ``C144_PWR_SW13``) and repeats the part once per
    channel, so ``source_designator`` is the only reliable link back to the
    schematic component a directive is authored on, and one schematic
    designator yields several PCB instances.

    Indices into :attr:`ExtractedProject.pcb_components` are returned — not
    designator strings — because a multi-channel design can legitimately place
    several distinct components under one physical designator; only the index
    identifies each uniquely.

    Falls back to a physical-designator exact match for components carrying no
    ``source_designator`` (hand-placed, or a PCB with no schematic origin). An
    empty list means no PCB placement was found at all.
    """
    target = sch_designator.upper()
    hits = [
        i for i, c in enumerate(proj.pcb_components)
        if c.source_designator and c.source_designator.upper() == target
    ]
    if hits:
        return hits
    return [
        i for i, c in enumerate(proj.pcb_components)
        if c.designator.upper() == target
    ]


# Per-project caches (id-keyed, bounded to the current project by an identity
# re-check + clear; ExtractedProject is frozen+slots so the maps can't live on
# it). These replace O(nets)/O(pads) linear scans that ran once per directive
# terminal — O(directives × nets/pads) overall.
_net_indices_cache: dict[int, tuple[ExtractedProject, dict[str, list[int]]]] = {}
_pads_by_comp_cache: dict[int, tuple[ExtractedProject, dict[int, list[RawPad]]]] = {}


def _net_indices_by_name(proj: ExtractedProject, name: str) -> list[int]:
    """Every net index whose name matches ``name`` (case-insensitive).

    A multi-channel PCB stores one net per channel, and Altium does not
    channel-qualify the names in its Nets6 stream — so a per-channel net name
    (e.g. ``NetC144_PWR_SW13_2``) is shared by all of those channels' distinct
    nets. Connectivity stays unambiguous (pads carry net *indices*); only the
    name is ambiguous, so callers must consider every index for a name and
    let the specific component's own pads pick out its channel.

    Name → indices is built once per project and cached, so this is O(1) per
    lookup instead of an O(nets) scan.
    """
    entry = _net_indices_cache.get(id(proj))
    if entry is None or entry[0] is not proj:
        by_name: dict[str, list[int]] = {}
        for i, n in enumerate(proj.nets):
            by_name.setdefault(n.name.upper(), []).append(i)
        _net_indices_cache.clear()
        _net_indices_cache[id(proj)] = (proj, by_name)
        entry = _net_indices_cache[id(proj)]
    return list(entry[1].get(name.upper(), ()))  # fresh list — callers may keep it


def _pads_by_component_all(proj: ExtractedProject) -> dict[int, list[RawPad]]:
    """All pads grouped by ``component_index`` (unlike
    :func:`_build_pads_by_component`, this keeps NO_NET pads and returns lists,
    matching the ``[p for p in proj.pads if p.component_index == …]`` scans it
    replaces). Built once per project and cached."""
    entry = _pads_by_comp_cache.get(id(proj))
    if entry is None or entry[0] is not proj:
        by_comp: dict[int, list[RawPad]] = {}
        for p in proj.pads:
            by_comp.setdefault(p.component_index, []).append(p)
        _pads_by_comp_cache.clear()
        _pads_by_comp_cache[id(proj)] = (proj, by_comp)
        entry = _pads_by_comp_cache[id(proj)]
    return entry[1]


def _series_channel_indices(
    comp: PdnParameterSource,
) -> list[int | None]:
    """SERIES (resistor-like) channel indices on one parameter source.

    Uses the same discovery / template-only rules as
    :func:`_resolve_channel_roles` so bridge validation sees the same
    channels the SERIES parser will emit.
    """
    part_role = _part_role_default(comp.parameters)
    scratch = AnnotationResult()
    grouped = _resolve_channel_roles(
        comp.parameters, part_role, comp.designator, scratch,
        report_errors=False,
    )
    return list(grouped.get("SERIES", []))


def _series_channel_params(
    comp: PdnParameterSource,
    ch_idx: int | None,
) -> dict[str, str]:
    """``comp.parameters`` with the SERIES channel's templates materialized.

    The bridge graph must see the same effective nets the SERIES parser emits
    — including ones inherited from a shared unindexed side — so both go
    through :func:`_materialize_channel_params` rather than reading raw keys.
    """
    return _materialize_channel_params(comp.parameters, ch_idx, "SERIES")


def _series_channel_has_net_params(
    params: dict[str, str],
    ch_idx: int | None,
) -> bool:
    return (
        _ci_get(params, _channel_key("P_PINS", ch_idx)) is not None
        or _ci_get(params, _channel_key("N_PINS", ch_idx)) is not None
    )


def _net_name_for_pins(
    proj: ExtractedProject,
    pcb_idx: int,
    pins: list[str],
) -> str | None:
    """The one net every listed pad of a placement sits on, else ``None``.

    A multi-pad terminal (a series FET's three drain pads, say) names a net only
    when all of its pads agree; a pad that is unrouted, absent from the
    footprint, or on a different net leaves the side unnamed.
    """
    pads = {
        p.designator.upper(): p
        for p in _pads_by_component_all(proj).get(pcb_idx, [])
    }
    net_indices: set[int] = set()
    for pin in pins:
        pad = pads.get(pin.strip().upper())
        if pad is None or pad.net_index == NO_NET:
            return None
        net_indices.add(pad.net_index)
    if len(net_indices) != 1:
        return None
    return proj.nets[next(iter(net_indices))].name


def _series_channel_side_net(
    params: dict[str, str],
    proj: ExtractedProject,
    ch_idx: int | None,
    pcb_idx: int,
    net_suffix: str,
    pins_suffix: str,
) -> str | None:
    """One side (P or N) of a SERIES channel resolved to a net name.

    ``params`` is the channel's materialized parameter set (see
    :func:`_series_channel_params`), so a side inherited from a shared
    unindexed ``PDN_P_NET`` / ``PDN_P_PINS`` template resolves here too rather
    than dropping the channel out of the SERIES graph.

    ``PDN<n>_P_NET`` wins when set; otherwise the ``PDN<n>_P_PINS`` pad list is
    resolved back through the placement's pads, so the pin form contributes the
    same net pair as the name form.
    """
    name = _ci_get(params, _channel_key(net_suffix, ch_idx))
    if name and name.strip():
        return name
    pins = _split_pin_list(
        _ci_get(params, _channel_key(pins_suffix, ch_idx)),
    )
    if not pins:
        return None
    return _net_name_for_pins(proj, pcb_idx, pins)


def _resolve_series_channel_nets(
    comp: PdnParameterSource,
    proj: ExtractedProject,
    ch_idx: int | None,
    pcb_idx: int,
    ch_indices: list[int | None],
) -> tuple[str, str, bool] | None:
    """P/N net names for one SERIES channel on one PCB placement.

    Returns ``(p_net, n_net, directed)``. ``directed`` is ``False`` on the
    2-pin auto-inference path: :func:`_autoinfer_2pin_nets` takes P and N from
    raw pad order, which carries no information about which side is upstream.
    Callers that only need the unordered pair (bridge unioning) ignore the flag;
    :func:`_collect_series_upstream_map` must not read power flow out of it.
    """
    params = _series_channel_params(comp, ch_idx)
    p_net = _ci_get(params, _channel_key("P_NET", ch_idx))
    n_net = _ci_get(params, _channel_key("N_NET", ch_idx))
    if p_net is None and n_net is None and not _series_channel_has_net_params(
        params, ch_idx,
    ):
        if len(ch_indices) != 1:
            return None
        inferred = _autoinfer_2pin_nets(proj, pcb_idx)
        if inferred is None:
            return None
        return inferred[0], inferred[1], False
    p_resolved = _series_channel_side_net(
        params, proj, ch_idx, pcb_idx, "P_NET", "P_PINS",
    )
    n_resolved = _series_channel_side_net(
        params, proj, ch_idx, pcb_idx, "N_NET", "N_PINS",
    )
    if p_resolved and n_resolved:
        return p_resolved, n_resolved, True
    return None


def _iter_series_bridge_pairs(
    parameter_sources: list[PdnParameterSource],
    proj: ExtractedProject,
) -> Iterator[tuple[int, str, str, bool]]:
    """Yield ``(pcb_index, p_net, n_net, directed)`` per SERIES bridge placement.

    Every channel of every placement yields its own pair — a multi-channel
    SERIES part bridges a different net pair per channel, and a repeated
    sheet places the same channel once per PCB instance. Explicit
    ``PDN<n>_P_NET`` / ``PDN<n>_N_NET`` pairs, the ``PDN<n>_P_PINS`` /
    ``PDN<n>_N_PINS`` pin form, and single-channel 2-pin auto-inference are all
    resolved by :func:`_resolve_series_channel_nets`, which also reports whether
    the P/N order is meaningful (``directed``).
    """
    for comp in parameter_sources:
        ch_indices = _series_channel_indices(comp)
        if not ch_indices:
            continue
        pcb_indices = _pcb_indices_for_source(comp, proj)
        for ch_idx in ch_indices:
            for pcb_idx in pcb_indices:
                resolved = _resolve_series_channel_nets(
                    comp, proj, ch_idx, pcb_idx, ch_indices,
                )
                if resolved is not None:
                    yield pcb_idx, resolved[0], resolved[1], resolved[2]


def _union_series_bridge_net_indices(
    proj: ExtractedProject,
    parameter_sources: list[PdnParameterSource],
    union: Callable[[int, int], None],
) -> None:
    """Union PCB net indices for each SERIES placement, scoped to that instance.

    Local net names are expanded with :meth:`InstanceLocalNetResolver.expand_net_names`
    ``pcb_index`` so channel slots from one repeated sheet do not bridge unrelated
    channels in analysis-group validation.
    """
    resolver = _instance_resolver(proj)
    for pcb_idx, p_net, n_net, _directed in _iter_series_bridge_pairs(
        parameter_sources, proj,
    ):
        idxs: list[int] = []
        for name in (p_net, n_net):
            for expanded in resolver.expand_net_names(
                name, pcb_index=pcb_idx,
            ):
                idxs.extend(_net_indices_by_name(proj, expanded))
        unique_idxs = list(dict.fromkeys(idxs))
        for other in unique_idxs[1:]:
            union(unique_idxs[0], other)


def _autoinfer_2pin_nets(proj: ExtractedProject, pcb_index: int) -> tuple[str, str] | None:
    """For a 2-pin component on two distinct nets, return ``(p_net, n_net)``.

    ``pcb_index`` indexes :attr:`ExtractedProject.pcb_components`. Returns
    ``None`` if the component is not 2-pin, has any unconnected pad, or has
    both pads on the same net (e.g. a closed solder jumper) — i.e. any case
    where the assignment is ambiguous or doesn't make physical sense.
    """
    pads = _pads_by_component_all(proj).get(pcb_index, [])
    if len(pads) != 2:
        return None
    if pads[0].net_index == NO_NET or pads[1].net_index == NO_NET:
        return None
    if pads[0].net_index == pads[1].net_index:
        return None
    return proj.nets[pads[0].net_index].name, proj.nets[pads[1].net_index].name


def _autoinfer_failure_reason(proj: ExtractedProject, pcb_index: int) -> str:
    """Human-readable reason why :func:`_autoinfer_2pin_nets` declined.

    Mirrors that function's guards so the per-directive parser can tell the
    user exactly why the P/N nets couldn't be inferred (and must be set
    explicitly).
    """
    pads = _pads_by_component_all(proj).get(pcb_index, [])
    if len(pads) != 2:
        return f"the footprint has {len(pads)} pads, not 2"
    if pads[0].net_index == NO_NET or pads[1].net_index == NO_NET:
        return "a pad is not connected to any net"
    if pads[0].net_index == pads[1].net_index:
        return "both pads are on the same net"
    return "the net pair is ambiguous"


# --- directive specs ---------------------------------------------------------

@dataclass(frozen=True)
class _BaseSpec:
    designator: str
    schdoc_name: str


@dataclass(frozen=True)
class SourceSpec(_BaseSpec):
    voltage: float
    p: TerminalSpec
    # ``None`` => single-net (PDN_NET) directive: the N terminal is an ideal
    # 0 Ω return rather than PCB copper. See the module docstring.
    n: TerminalSpec | None
    channel_index: int | None = None  # None = legacy unindexed; int = PDN<n>_*
    # Single-net directives sharing one analysis group share a return node so
    # their current loop closes; ``None`` for two-terminal directives.
    return_group: int | None = None
    # True => this directive sits on a single-type rail (only sources or only
    # sinks) that can't carry current, so ``build_problem`` excludes it from
    # the FEM. The directive is still kept in ``AnnotationResult.directives``
    # so the viewer keeps drawing its marker — see
    # :func:`fypa.altium.loader._flag_open_loop_rails`.
    solve_excluded: bool = False


@dataclass(frozen=True)
class SinkSpec(_BaseSpec):
    current: float
    p: TerminalSpec
    n: TerminalSpec | None  # ``None`` => single-net directive (see SourceSpec)
    channel_index: int | None = None  # None = legacy unindexed; int = PDN<n>_*
    return_group: int | None = None
    # Optional minimum acceptable rail voltage (PDN_MIN_V). When set, the
    # viewer's Nodes table compares the sink's actual P-terminal voltage
    # against this limit and flags pass / fail per pin.
    min_voltage: float | None = None
    solve_excluded: bool = False  # see SourceSpec.solve_excluded


@dataclass(frozen=True)
class ResistorSpec(_BaseSpec):
    resistance: float
    p: TerminalSpec
    n: TerminalSpec
    channel_index: int | None = None  # None = legacy unindexed; int = PDN<n>_*
    solve_excluded: bool = False  # see SourceSpec.solve_excluded


_REGULATOR_TYPES: frozenset[str] = frozenset({"LDO", "SMPS"})


@dataclass(frozen=True)
class RegulatorSpec(_BaseSpec):
    voltage: float
    gain: float
    out_p: TerminalSpec
    out_n: TerminalSpec
    in_p: TerminalSpec
    in_n: TerminalSpec
    channel_index: int | None = None  # None = legacy unindexed; int = PDN<n>_*
    solve_excluded: bool = False  # see SourceSpec.solve_excluded
    regulator_type: str | None = None  # "LDO" | "SMPS"
    efficiency: float = 1.0
    adaptive_gain_eligible: bool = False  # SMPS without explicit PDN_GAIN
    quiescent_current: float = 0.0  # constant input current (A), optional PDN_QUIESCENT


DirectiveSpec = SourceSpec | SinkSpec | ResistorSpec | RegulatorSpec


@dataclass
class AnnotationResult:
    directives: list[DirectiveSpec] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Per-rail "won't be solved" notices for single-type rails (only sources
    # or only sinks). A subset of ``warnings`` (also shown in the Setup tab),
    # kept separately so the GUI can pop them as an active dialog on load /
    # after Resolve. Populated by
    # :func:`fypa.altium.loader._flag_open_loop_rails`.
    open_loop_rails: list[str] = field(default_factory=list)
    # Per-net "source and sink are on disconnected copper" notices. Unlike
    # open_loop_rails these rails ARE solved, but the result is unreliable (no
    # copper path closes the current loop, so the sink reads ~0 V and the FEM
    # injects a large ground-balancing current). Surfaced as an active dialog
    # like open_loop_rails. Populated by :func:`fypa.altium.loader.build_problem`.
    connectivity_breaks: list[str] = field(default_factory=list)
    # Warnings about directives the low-Ω net-merge pass absorbs. A subset of
    # ``warnings``. :func:`fypa.altium.loader.load_project` parses twice — once
    # to discover the merges, once on the merged nets — and the second pass is
    # told to skip every absorbed designator, so it cannot re-raise these. The
    # loader copies them from the first result into the final one; without
    # that, a Net Tie that wrongly bridged +3V3 to GND would merge the rails
    # with nothing in the annotation log to say so.
    absorbed_notes: list[str] = field(default_factory=list)

    def note_absorbed(self, message: str) -> None:
        """Warn, and mark the message to survive the post-merge re-parse."""
        self.warnings.append(message)
        self.absorbed_notes.append(message)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        by_kind: dict[str, list[str]] = {}
        for d in self.directives:
            by_kind.setdefault(type(d).__name__, []).append(
                _channel_label(d.designator, getattr(d, "channel_index", None))
            )
        lines = [f"Annotation result: {len(self.directives)} directive(s)"]
        for kind, designators in sorted(by_kind.items()):
            lines.append(f"  {kind:<14} {len(designators):>3}  on: {', '.join(designators)}")
        if self.warnings:
            lines.append(f"  warnings: {len(self.warnings)}")
            for w in self.warnings:
                lines.append(f"    - {w}")
        if self.errors:
            lines.append(f"  errors: {len(self.errors)}")
            for e in self.errors:
                lines.append(f"    - {e}")
        return "\n".join(lines)


# --- per-role parsers ---------------------------------------------------------

def _require_value(params: dict[str, str], key: str, role_diag: str, result: AnnotationResult) -> float | None:
    raw = _ci_get(params, key)
    if raw is None:
        result.errors.append(f"{role_diag}: missing required parameter {key}")
        return None
    try:
        return parse_si_value(raw)
    except ValueError as e:
        result.errors.append(f"{role_diag}: {key}={raw!r} — {e}{_PARSE_VALUE_HINT}")
        return None


def _optional_value(params: dict[str, str], key: str, role_diag: str,
                    result: AnnotationResult) -> float | None:
    """Like :func:`_require_value` but the parameter is allowed to be absent.
    Returns ``None`` when the key isn't set; appends an error and returns
    ``None`` when the key IS set but doesn't parse as a number."""
    raw = _ci_get(params, key)
    if raw is None:
        return None
    try:
        return parse_si_value(raw)
    except ValueError as e:
        result.errors.append(f"{role_diag}: {key}={raw!r} — {e}{_PARSE_VALUE_HINT}")
        return None


def _resolve_two_terminal(
    proj: ExtractedProject,
    pcb_index: int,
    params: dict[str, str],
    p_net_key: str,
    n_net_key: str,
    p_pins_key: str,
    n_pins_key: str,
    enabled_layers: list[int],
    role_diag: str,
    result: AnnotationResult,
    net_remap: dict[int, int] | None = None,
    sch_lookup_designator: str | None = None,
    schdoc_name: str | None = None,
) -> tuple[TerminalSpec, TerminalSpec] | None:
    p_net = _ci_get(params, p_net_key)
    n_net = _ci_get(params, n_net_key)
    p_pins = _split_pin_list(_ci_get(params, p_pins_key))
    n_pins = _split_pin_list(_ci_get(params, n_pins_key))

    if p_net is None and p_pins is None:
        result.errors.append(f"{role_diag}: missing {p_net_key} (or {p_pins_key})")
    if n_net is None and n_pins is None:
        result.errors.append(f"{role_diag}: missing {n_net_key} (or {n_pins_key})")
    if p_net is None and p_pins is None or n_net is None and n_pins is None:
        return None

    p_spec, p_err, p_tier = _resolve_terminal(
        proj, pcb_index, p_net, p_pins, enabled_layers,
        f"{role_diag} P-terminal",
        warnings=result.warnings,
        net_remap=net_remap,
        sch_lookup_designator=sch_lookup_designator,
        schdoc_name=schdoc_name,
    )
    n_spec, n_err, n_tier = _resolve_terminal(
        proj, pcb_index, n_net, n_pins, enabled_layers,
        f"{role_diag} N-terminal",
        warnings=result.warnings,
        net_remap=net_remap,
        sch_lookup_designator=sch_lookup_designator,
        schdoc_name=schdoc_name,
    )
    result.errors.extend(p_err)
    result.errors.extend(n_err)
    if p_spec is None or n_spec is None:
        return None
    if p_err or n_err:
        # A partial pin override yields a spec AND an error. The directive is
        # already failing, so arbitrating a knowingly truncated terminal would
        # only append a second, contradictory complaint about it.
        return None

    p_spec, n_spec = _arbitrate_overlapping_terminals(
        p_spec, n_spec, p_tier, n_tier, role_diag, result,
    )
    if p_spec is None or n_spec is None:
        return None
    return p_spec, n_spec


def _terminal_pin_overlap_key(pin: TerminalPin) -> str:
    """Internal identity for P/N overlap arbitration — never shown to a user.

    Pads on different components with the same pad designator (typical for
    single-pin lab jacks) are distinct. Without ``component_designator``,
    fall back to pad name only — same as the historical same-footprint case.

    Note both terminals of an annotation-authored directive always resolve
    from one ``pcb_index``, so the component half is currently the same on
    both sides; it matters only for a caller that pairs terminals across
    components. Use :func:`_overlap_pad_names` for anything user-visible.
    """
    pad = pin.pad_designator.upper()
    if pin.component_designator:
        return f"{pin.component_designator.upper()}:{pad}"
    return pad


def _overlap_pad_names(spec: TerminalSpec, overlap: set[str]) -> list[str]:
    """Pad designators behind ``overlap``, as the user must type them.

    The arbitration keys are composite ("R5:1"), but PDN_P_PINS / PDN_N_PINS
    match on the bare pad designator ("1"). Printing the key sent users to
    set PDN_P_PINS='R5:1' and get "pin overrides not found: ['R5:1']" back.
    """
    return sorted({
        pin.pad_designator
        for pin in spec.pins
        if _terminal_pin_overlap_key(pin) in overlap
    })


def _arbitrate_overlapping_terminals(
    p_spec: TerminalSpec,
    n_spec: TerminalSpec,
    p_tier: int,
    n_tier: int,
    role_diag: str,
    result: AnnotationResult,
) -> tuple[TerminalSpec | None, TerminalSpec | None]:
    """Drop shared pins from the weaker terminal; error when tiers are equal.

    Ambiguous local-net aliases can make a SERIES P-terminal claim both pads
    of a two-pin part (shorting the series element). Prefer the higher-quality
    match tier; when tiers tie, require explicit ``PDN_*_PINS`` overrides.

    Overlap is keyed by component+pad when ``component_designator`` is set.
    Note that no current caller produces P and N on different components:
    :func:`_resolve_two_terminal` resolves both from one ``pcb_index``, so the
    component half is always equal and the key reduces to the pad name. It is
    kept for a caller that pairs terminals across components; until one
    exists, the multi-connector case it describes cannot arise.
    """
    p_keys = {_terminal_pin_overlap_key(pin) for pin in p_spec.pins}
    n_keys = {_terminal_pin_overlap_key(pin) for pin in n_spec.pins}
    overlap = p_keys & n_keys
    if not overlap:
        return p_spec, n_spec

    pad_names = _overlap_pad_names(p_spec, overlap)
    overlap_text = ", ".join(pad_names)

    def _label(spec: TerminalSpec) -> str:
        return spec.requested_net or "?"

    if p_tier < n_tier:
        kept = tuple(
            pin for pin in n_spec.pins
            if _terminal_pin_overlap_key(pin) not in overlap
        )
        if not kept:
            # Error only. Warning first that the pins were "dropped from the
            # N-terminal" and then that the N-terminal is empty and the
            # directive discarded reads as two contradictory lines in the log.
            result.errors.append(
                f"{role_diag}: N-terminal ({_label(n_spec)!r}) would be empty "
                f"after removing pin(s) {overlap_text}, which the P-terminal "
                f"({_label(p_spec)!r}) matched more strongly; set "
                f"PDN_N_PINS to name the pad(s) that belong to N"
            )
            return p_spec, None
        result.warnings.append(
            f"{role_diag}: P and N both matched pin(s) {overlap_text}; "
            f"kept on P ({_label(p_spec)!r}, the stronger match), "
            f"dropped from N ({_label(n_spec)!r})"
        )
        return p_spec, TerminalSpec(
            pins=kept,
            requested_net=n_spec.requested_net,
            resolved_via_local=n_spec.resolved_via_local,
        )

    if n_tier < p_tier:
        kept = tuple(
            pin for pin in p_spec.pins
            if _terminal_pin_overlap_key(pin) not in overlap
        )
        if not kept:
            result.errors.append(
                f"{role_diag}: P-terminal ({_label(p_spec)!r}) would be empty "
                f"after removing pin(s) {overlap_text}, which the N-terminal "
                f"({_label(n_spec)!r}) matched more strongly; set "
                f"PDN_P_PINS to name the pad(s) that belong to P"
            )
            return None, n_spec
        result.warnings.append(
            f"{role_diag}: P and N both matched pin(s) {overlap_text}; "
            f"kept on N ({_label(n_spec)!r}, the stronger match), "
            f"dropped from P ({_label(p_spec)!r})"
        )
        return TerminalSpec(
            pins=kept,
            requested_net=p_spec.requested_net,
            resolved_via_local=p_spec.resolved_via_local,
        ), n_spec

    result.errors.append(
        f"{role_diag}: P and N terminals both resolve to pin(s) "
        f"{overlap_text} with equally strong evidence "
        f"(P={_label(p_spec)!r}, N={_label(n_spec)!r}) — the element would be "
        f"shorted. Set PDN_P_PINS / PDN_N_PINS to the pad designator(s) each "
        f"side owns; an explicit pin list outranks a net-name match."
    )
    return None, None


def _terminal_mode(params: dict[str, str], idx: int | None,
                   role_diag: str, result: AnnotationResult) -> str | None:
    """Decide whether a SOURCE/SINK channel is single-net or two-terminal.

    A single-net channel carries ``PDN_NET`` (or ``PDN_PINS``); a two-terminal
    channel carries ``PDN_P_NET``/``PDN_N_NET`` (or their ``*_PINS``). The two
    are mutually exclusive — see the module docstring. Returns ``"single"``,
    ``"two"``, or ``None`` (a validation error has been appended to ``result``).
    """
    net_key = _channel_key("NET", idx)
    pins_key = _channel_key("PINS", idx)
    p_net_key = _channel_key("P_NET", idx)
    n_net_key = _channel_key("N_NET", idx)
    p_pins_key = _channel_key("P_PINS", idx)
    single_set: list[str] = []
    if _ci_get(params, net_key) is not None:
        single_set.append(net_key)
    if _ci_get(params, pins_key) is not None:
        single_set.append(f"{pins_key} (single-net pin override)")
    two_set: list[str] = []
    for suffix in ("P_NET", "N_NET", "P_PINS", "N_PINS"):
        key = _channel_key(suffix, idx)
        if _ci_get(params, key) is not None:
            two_set.append(key)
    has_single = bool(single_set)
    has_two = bool(two_set)
    if has_single and has_two:
        single_text = " + ".join(single_set)
        two_text = " + ".join(two_set)
        msg = (
            f"{role_diag}: {single_text} conflicts with {two_text} "
            f"(two-terminal). Use either single-net ({net_key} or "
            f"{pins_key} on a single-net check only) or two-terminal "
            f"({p_net_key} + {n_net_key}, pins {p_pins_key} / "
            f"{_channel_key('N_PINS', idx)}), not both."
        )
        if _ci_get(params, pins_key) is not None:
            msg += f" For a two-terminal check use {p_pins_key}, not {pins_key}."
        result.errors.append(msg)
        return None
    if has_single:
        return "single"
    if has_two:
        return "two"
    result.errors.append(
        f"{role_diag}: no terminal net specified — set {p_net_key} and "
        f"{n_net_key}, or {net_key} for a single-net (point-to-point) check"
    )
    return None


def _resolve_single_terminal(
    proj: ExtractedProject,
    pcb_index: int,
    params: dict[str, str],
    net_key: str,
    pins_key: str,
    enabled_layers: list[int],
    role_diag: str,
    result: AnnotationResult,
    net_remap: dict[int, int] | None = None,
    sch_lookup_designator: str | None = None,
    schdoc_name: str | None = None,
) -> TerminalSpec | None:
    """Resolve the single PCB terminal of a single-net SOURCE/SINK directive.

    The directive's other terminal is an ideal 0 Ω return (see the module
    docstring), so only this one lands on copper. Returns the
    :class:`TerminalSpec`, or ``None`` if resolution failed (errors appended
    to ``result``).
    """
    net = _ci_get(params, net_key)
    pins = _split_pin_list(_ci_get(params, pins_key))
    spec, errs, _tier = _resolve_terminal(
        proj, pcb_index, net, pins, enabled_layers,
        f"{role_diag} terminal", warnings=result.warnings, net_remap=net_remap,
        sch_lookup_designator=sch_lookup_designator,
        schdoc_name=schdoc_name,
    )
    result.errors.extend(errs)
    return spec


def _has_single_net_params(params: dict[str, str],
                           indices: list[int | None] | None = None) -> bool:
    """True if any ``PDN[n]_NET`` / ``PDN[n]_PINS`` parameter is present.

    Used to reject ``PDN_NET`` on SERIES / REGULATOR — single-net mode is
    SOURCE/SINK only. When ``indices`` is given, only those channels are
    considered, so a single-net SOURCE/SINK channel on a mixed-role part does
    not trip the check for that part's SERIES / REGULATOR channels."""
    wanted = None if indices is None else set(indices)
    for k, v in params.items():
        m = _INDEXED_KEY_RE.match(k.strip())
        if not (m and m.group(2).upper() in ("NET", "PINS")
                and v is not None and str(v).strip()):
            continue
        if wanted is not None:
            idx = int(m.group(1)) if m.group(1) else None
            if idx not in wanted:
                continue
        return True
    return False


def _parse_source(comp, proj, enabled_layers, result,
                  net_remap=None, supply_map=None, only_indices=None,
                  series_graph=None):
    # ``only_indices`` (from the per-channel role dispatcher) restricts this
    # parser to the channels whose effective role is SOURCE; when ``None`` the
    # whole part is SOURCE and channels are discovered here.
    if only_indices is not None:
        indices = list(only_indices)
    else:
        indices = _discover_channel_indices(comp.parameters, "V")
        if not indices:
            result.errors.append(
                f"SOURCE on {comp.designator}: missing PDN_V "
                f"(or PDN<n>_V for an indexed channel)"
            )
            return []
    pcb_indices = _pcb_indices_for_source(comp, proj)
    if not pcb_indices:
        result.errors.append(
            f"SOURCE on {comp.designator}: component {comp.designator!r} "
            f"is not placed on the PCB"
        )
        return []
    if len(pcb_indices) > 1:
        names = ", ".join(proj.pcb_components[i].designator for i in pcb_indices)
        result.warnings.append(
            f"SOURCE on {comp.designator}: expanding to "
            f"{len(pcb_indices)} multi-channel PCB instances ({names})"
        )
    specs: list[SourceSpec] = []
    for idx in indices:
        role_diag = f"SOURCE on {_channel_label(comp.designator, idx)}"
        params = _materialize_channel_params(comp.parameters, idx, "SOURCE")
        v = _require_value(params, _channel_key("V", idx), role_diag, result)
        if v is None:
            continue
        mode = _terminal_mode(params, idx, role_diag, result)
        if mode is None:
            continue
        for pcb_idx in pcb_indices:
            pcb_des = proj.pcb_components[pcb_idx].designator
            inst_diag = (
                f"SOURCE on {_channel_label(pcb_des, idx)}"
                if len(pcb_indices) > 1 else role_diag
            )
            if mode == "single":
                p = _resolve_single_terminal(
                    proj, pcb_idx, params,
                    _channel_key("NET", idx), _channel_key("PINS", idx),
                    enabled_layers, inst_diag, result,
                    net_remap=net_remap,
                    sch_lookup_designator=comp.lookup_designator,
                    schdoc_name=comp.schdoc_name,
                )
                if p is None:
                    continue
                specs.append(SourceSpec(
                    designator=pcb_des, schdoc_name=comp.schdoc_name,
                    voltage=v, p=p, n=None, channel_index=idx,
                ))
                continue
            pair = _resolve_two_terminal(
                proj, pcb_idx, params,
                _channel_key("P_NET", idx), _channel_key("N_NET", idx),
                _channel_key("P_PINS", idx), _channel_key("N_PINS", idx),
                enabled_layers, inst_diag, result,
                net_remap=net_remap,
                sch_lookup_designator=comp.lookup_designator,
                schdoc_name=comp.schdoc_name,
            )
            if pair is None:
                continue
            specs.append(SourceSpec(
                designator=pcb_des, schdoc_name=comp.schdoc_name,
                voltage=v, p=pair[0], n=pair[1], channel_index=idx,
            ))
    return specs


def _parse_sink(comp, proj, enabled_layers, result,
                net_remap=None, supply_map=None, only_indices=None,
                series_graph=None):
    # ``only_indices`` restricts this parser to the part's SINK-role channels;
    # see _parse_source.
    if only_indices is not None:
        indices = list(only_indices)
    else:
        indices = _discover_channel_indices(comp.parameters, "I")
        if not indices:
            result.errors.append(
                f"SINK on {comp.designator}: missing PDN_I "
                f"(or PDN<n>_I for an indexed channel)"
            )
            return []
    pcb_indices = _pcb_indices_for_source(comp, proj)
    if not pcb_indices:
        result.errors.append(
            f"SINK on {comp.designator}: component {comp.designator!r} "
            f"is not placed on the PCB"
        )
        return []
    if len(pcb_indices) > 1:
        names = ", ".join(proj.pcb_components[i].designator for i in pcb_indices)
        result.warnings.append(
            f"SINK on {comp.designator}: expanding to "
            f"{len(pcb_indices)} multi-channel PCB instances ({names})"
        )
    specs: list[SinkSpec] = []
    for idx in indices:
        role_diag = f"SINK on {_channel_label(comp.designator, idx)}"
        params = _materialize_channel_params(comp.parameters, idx, "SINK")
        i = _require_value(params, _channel_key("I", idx), role_diag, result)
        if i is None:
            continue
        mode = _terminal_mode(params, idx, role_diag, result)
        if mode is None:
            continue
        min_v = _optional_value(
            params, _channel_key("MIN_V", idx), role_diag, result,
        )
        for pcb_idx in pcb_indices:
            pcb_des = proj.pcb_components[pcb_idx].designator
            inst_diag = (
                f"SINK on {_channel_label(pcb_des, idx)}"
                if len(pcb_indices) > 1 else role_diag
            )
            if mode == "single":
                p = _resolve_single_terminal(
                    proj, pcb_idx, params,
                    _channel_key("NET", idx), _channel_key("PINS", idx),
                    enabled_layers, inst_diag, result,
                    net_remap=net_remap,
                    sch_lookup_designator=comp.lookup_designator,
                    schdoc_name=comp.schdoc_name,
                )
                if p is None:
                    continue
                specs.append(SinkSpec(
                    designator=pcb_des, schdoc_name=comp.schdoc_name,
                    current=i, p=p, n=None, channel_index=idx,
                    min_voltage=min_v,
                ))
                continue
            pair = _resolve_two_terminal(
                proj, pcb_idx, params,
                _channel_key("P_NET", idx), _channel_key("N_NET", idx),
                _channel_key("P_PINS", idx), _channel_key("N_PINS", idx),
                enabled_layers, inst_diag, result,
                net_remap=net_remap,
                sch_lookup_designator=comp.lookup_designator,
                schdoc_name=comp.schdoc_name,
            )
            if pair is None:
                continue
            specs.append(SinkSpec(
                designator=pcb_des, schdoc_name=comp.schdoc_name,
                current=i, p=pair[0], n=pair[1], channel_index=idx,
                min_voltage=min_v,
            ))
    return specs


def _parse_resistance(comp, proj, enabled_layers, result,
                      net_remap=None, supply_map=None, only_indices=None,
                      series_graph=None):
    # This parser only ever handles SERIES-role channels (part-wide or a
    # PDN<n>_ROLE=SERIES override), so the role for diagnostics is always
    # SERIES regardless of the part-wide PDN_ROLE.
    role_raw = "SERIES"
    role_diag_base = f"{role_raw} on {comp.designator}"
    if _has_single_net_params(comp.parameters, only_indices):
        result.errors.append(
            f"{role_diag_base}: PDN_NET is only valid on SOURCE/SINK — a "
            f"SERIES directive bridges two nets, use PDN_P_NET and PDN_N_NET"
        )
        return []
    if only_indices is not None:
        indices = list(only_indices)
    else:
        indices = _discover_channel_indices(comp.parameters, "R")
        if not indices:
            result.errors.append(
                f"{role_diag_base}: missing PDN_R "
                f"(or PDN<n>_R for an indexed channel)"
            )
            return []

    pcb_indices = _pcb_indices_for_source(comp, proj)
    if not pcb_indices:
        result.errors.append(
            f"{role_diag_base}: component {comp.designator!r} is not placed "
            f"on the PCB"
        )
        return []
    if len(pcb_indices) > 1:
        names = ", ".join(proj.pcb_components[i].designator for i in pcb_indices)
        result.warnings.append(
            f"{role_raw} on {comp.designator}: expanding to "
            f"{len(pcb_indices)} multi-channel PCB instances ({names})"
        )

    specs: list[ResistorSpec] = []
    for idx in indices:
        role_diag = f"{role_raw} on {_channel_label(comp.designator, idx)}"
        params = _materialize_channel_params(comp.parameters, idx, role_raw)
        r = _require_value(
            params, _channel_key("R", idx), role_diag, result,
        )
        if r is None:
            continue
        if r <= 0:
            result.errors.append(
                f"{role_diag}: {_channel_key('R', idx)} must be positive, got {r}"
            )
            continue
        for pcb_idx in pcb_indices:
            pcb_des = proj.pcb_components[pcb_idx].designator
            inst_diag = (
                f"{role_raw} on {_channel_label(pcb_des, idx)}"
                if len(pcb_indices) > 1 else role_diag
            )
            given = any(
                _ci_get(params, _channel_key(k, idx)) is not None
                for k in ("P_NET", "N_NET", "P_PINS", "N_PINS")
            )
            resolve_params = dict(params)
            if not given:
                if len(indices) > 1:
                    result.errors.append(
                        f"{inst_diag}: multi-channel SERIES requires explicit "
                        f"{_channel_key('P_NET', idx)} / {_channel_key('N_NET', idx)} "
                        f"or {_channel_key('P_PINS', idx)} / "
                        f"{_channel_key('N_PINS', idx)} per channel"
                    )
                    continue
                inferred = _autoinfer_2pin_nets(proj, pcb_idx)
                if inferred is None:
                    reason = _autoinfer_failure_reason(proj, pcb_idx)
                    result.errors.append(
                        f"{inst_diag}: {_channel_key('P_NET', idx)} and "
                        f"{_channel_key('N_NET', idx)} are required "
                        f"({reason}, so the two nets cannot be auto-inferred) — "
                        f"set them explicitly (or use "
                        f"{_channel_key('P_PINS', idx)} / "
                        f"{_channel_key('N_PINS', idx)})"
                    )
                    continue
                resolve_params[_channel_key("P_NET", idx)] = inferred[0]
                resolve_params[_channel_key("N_NET", idx)] = inferred[1]
                result.warnings.append(
                    f"{inst_diag}: auto-inferred "
                    f"{_channel_key('P_NET', idx)}={inferred[0]!r}, "
                    f"{_channel_key('N_NET', idx)}={inferred[1]!r} "
                    f"from 2-pin connectivity"
                )
            pair = _resolve_two_terminal(
                proj, pcb_idx, resolve_params,
                _channel_key("P_NET", idx), _channel_key("N_NET", idx),
                _channel_key("P_PINS", idx), _channel_key("N_PINS", idx),
                enabled_layers, inst_diag, result,
                net_remap=net_remap,
                sch_lookup_designator=comp.lookup_designator,
                schdoc_name=comp.schdoc_name,
            )
            if pair is None:
                continue
            specs.append(ResistorSpec(
                designator=pcb_des, schdoc_name=comp.schdoc_name,
                resistance=r, p=pair[0], n=pair[1], channel_index=idx,
            ))
    return specs


def _collect_supply_voltages_by_net(
    parameter_sources: list[PdnParameterSource],
    proj: ExtractedProject,
    net_remap: dict[int, int] | None = None,
) -> dict[str, float]:
    """Map canonical supply net names to nominal voltages from SOURCE /
    REGULATOR schematic parameters (before pad resolution).

    Names are canonicalised with :func:`_canonical_supply_net_name` so this map,
    :func:`_collect_series_upstream_map`, and the ``IN_P_NET`` a regulator is
    looked up by are all keyed the same way. Without that, any design the
    loader's net-merge pre-pass renamed a rail in would find no Vin at all.
    """
    raw: dict[str, set[float]] = {}

    def _register(
        net: str | None, voltage: float, pcb_indices: list[int],
    ) -> None:
        if net is None or not str(net).strip():
            return
        # One placement per repeated-sheet instance; each may canonicalise
        # differently, and the declared voltage holds for all of them.
        keys = {
            _canonical_supply_net_name(proj, net, net_remap, pcb_index=i)
            for i in pcb_indices
        } or {_canonical_supply_net_name(proj, net, net_remap)}
        for key in keys:
            raw.setdefault(key, set()).add(float(voltage))

    for comp in parameter_sources:
        part_role = _part_role_default(comp.parameters)
        pcb_indices = _pcb_indices_for_source(comp, proj)
        # Both SOURCE and REGULATOR carry PDN<n>_V; switch on each channel's
        # effective role so a SOURCE (or REGULATOR) channel on a mixed-role
        # part still contributes its nominal rail voltage. Template inheritance
        # goes through :func:`_resolve_channel_roles` + materialize.
        scratch = AnnotationResult()
        grouped = _resolve_channel_roles(
            comp.parameters, part_role, comp.designator, scratch,
            report_errors=False,
        )
        for role in ("SOURCE", "REGULATOR"):
            for idx in grouped.get(role, []):
                params = _materialize_channel_params(comp.parameters, idx, role)
                v_raw = _ci_get(params, _channel_key("V", idx))
                if v_raw is None or not str(v_raw).strip():
                    continue
                try:
                    v = parse_si_value(v_raw)
                except ValueError:
                    continue
                if role == "SOURCE":
                    p_net = _ci_get(params, _channel_key("P_NET", idx))
                    single_net = _ci_get(params, _channel_key("NET", idx))
                    _register(p_net or single_net, v, pcb_indices)
                else:  # REGULATOR
                    out_net = _ci_get(params, _channel_key("OUT_P_NET", idx))
                    _register(out_net, v, pcb_indices)
    out: dict[str, float] = {}
    for net, voltages in raw.items():
        if len(voltages) == 1:
            out[net] = next(iter(voltages))
    return out


def _canonical_supply_net_name(
    proj: ExtractedProject,
    net_name: str,
    net_remap: dict[int, int] | None = None,
    pcb_index: int | None = None,
) -> str:
    """Upper-case supply net label, remapped to its merge canonical.

    A repeated (multi-channel) sheet shares one net *name* across several
    distinct net indices — see :func:`_net_indices_by_name` — so the label alone
    cannot pick a channel and taking the first index would key the Vin graph off
    whichever instance happens to come first in ``proj.nets``. When
    ``pcb_index`` is given the candidates are scoped to that placement with
    :meth:`InstanceLocalNetResolver.expand_net_names`, matching
    :func:`_union_series_bridge_net_indices`.

    If the surviving candidates still disagree on a canonical name the label is
    returned unchanged: a name-keyed graph cannot express a per-instance split,
    and leaving the label alone at least keeps both sides of the lookup
    consistent.
    """
    key = net_name.strip().upper()
    if not net_remap:
        # No merge happened, so the canonical name is the label itself. Skips
        # the index lookup on the overwhelmingly common path.
        return key
    if pcb_index is None:
        names: tuple[str, ...] = (key,)
    else:
        names = _instance_resolver(proj).expand_net_names(
            key, pcb_index=pcb_index,
        )
    indices: list[int] = []
    for name in names:
        indices.extend(_net_indices_by_name(proj, name))
    if not indices:
        return key
    canonical = {
        proj.nets[net_remap.get(i, i)].name.upper() for i in indices
    }
    if len(canonical) != 1:
        return key
    return next(iter(canonical))


@dataclass(frozen=True)
class _SeriesVinGraph:
    """SERIES power-flow graph used to infer an SMPS input voltage.

    ``upstream`` holds directed ``downstream N_NET → upstream P_NET`` edges from
    channels that state a P/N order. ``undirected`` holds auto-inferred 2-pin
    links, whose P/N order comes from raw pad order and so cannot say which side
    is upstream; the walk crosses one only when the step is unambiguous.
    ``ambiguous`` lists downstream nets two SERIES channels claim different
    upstream nets for.
    """

    upstream: dict[str, str] = field(default_factory=dict)
    undirected: dict[str, frozenset[str]] = field(default_factory=dict)
    ambiguous: frozenset[str] = frozenset()


def _collect_series_upstream_map(
    parameter_sources: list[PdnParameterSource],
    proj: ExtractedProject,
    net_remap: dict[int, int] | None = None,
    skip_designators: set[str] | None = None,
) -> _SeriesVinGraph:
    """Build the SERIES power-flow graph for nominal Vin lookup.

    Channels come from the same :func:`_iter_series_bridge_pairs` composition
    the bridge-union path uses, so per-channel ``PDN<n>_ROLE`` overrides, the
    ``PDN<n>_P_PINS`` / ``PDN<n>_N_PINS`` pin form, and repeated-sheet
    placements are all handled in one place.

    Unlike the undirected bridge union, direction is preserved: sense paths that
    undirectedly join unrelated rails via GND must not collapse Vin inference.

    ``skip_designators`` mirrors :func:`parse_annotations` — a SERIES element the
    loader's net-merge pre-pass absorbed has both ends on one net now, and
    registering it would contradict the real edge on that rail.
    """
    upstream: dict[str, str] = {}
    undirected: dict[str, set[str]] = {}
    ambiguous: set[str] = set()
    skip = {d.upper() for d in (skip_designators or ())}

    def _register_directed(downstream: str, upstream_net: str) -> None:
        if downstream in ambiguous:
            return
        prev = upstream.get(downstream)
        if prev is not None:
            if prev != upstream_net:
                ambiguous.add(downstream)
                upstream.pop(downstream, None)
            return
        upstream[downstream] = upstream_net

    sources = [
        comp for comp in parameter_sources
        if comp.designator.upper() not in skip
    ]
    for pcb_idx, p_net, n_net, directed in _iter_series_bridge_pairs(
        sources, proj,
    ):
        up = _canonical_supply_net_name(proj, p_net, net_remap, pcb_index=pcb_idx)
        dn = _canonical_supply_net_name(proj, n_net, net_remap, pcb_index=pcb_idx)
        if up == dn:
            # Both ends collapsed onto one net (a merged short, or an element
            # annotated across a single net). A self-edge carries no power-flow
            # information and would falsely conflict with the rail's real edge.
            continue
        if directed:
            _register_directed(dn, up)
        else:
            undirected.setdefault(dn, set()).add(up)
            undirected.setdefault(up, set()).add(dn)

    return _SeriesVinGraph(
        upstream=upstream,
        undirected={k: frozenset(v) for k, v in undirected.items()},
        ambiguous=frozenset(ambiguous),
    )


# Depth cap on the SERIES walk. A real Vin chain is a handful of elements
# (fuse, ORing FET, filter, sense resistor); anything longer is far more likely
# to be a sense/bleed path stitched into a chain than a power path.
_SERIES_VIN_MAX_HOPS: int = 8


def _lookup_inferred_vin(
    in_p_net: str | None,
    supply_map: dict[str, float],
    graph: _SeriesVinGraph | None = None,
) -> tuple[float | None, str | None, int]:
    """Nominal Vin from an upstream SOURCE / REGULATOR reachable from ``in_p_net``.

    ``supply_map`` lists voltages on SOURCE ``P_NET`` and REGULATOR
    ``OUT_P_NET`` only. Both it and ``in_p_net`` must already be canonicalised
    by :func:`_canonical_supply_net_name`, so a merged rail is keyed one way.
    When ``graph`` is given, walk SERIES edges upstream until a mapped voltage
    is found.

    Returns ``(vin, failure, hops)``. ``hops`` is 0 for a direct hit and lets
    the caller warn when Vin came from a walked chain rather than the rail the
    user named. ``failure`` is ``'ambiguous'`` (two SERIES channels disagree on
    the upstream net), ``'undirected'`` (several auto-inferred neighbours, so
    the direction is unknown), ``'cycle'``, ``'too_deep'``, or ``None`` on
    success / no match.

    Undirected bridge equivalence must not be used here: sense paths through
    GND can join unrelated rails into one class.
    """
    if in_p_net is None or not str(in_p_net).strip():
        return None, None, 0
    net = in_p_net.strip().upper()
    visited: set[str] = set()
    hops = 0
    while True:
        v = supply_map.get(net)
        if v is not None and v > 0:
            return v, None, hops
        # Checked *after* the direct lookup: a rail whose voltage is explicitly
        # declared resolves even when it is also the downstream end of two
        # conflicting SERIES links (parallel ferrites, ORing FETs, fuse +
        # bypass), which is exactly where a real design puts them.
        if graph is not None and net in graph.ambiguous:
            return None, "ambiguous", hops
        visited.add(net)
        if graph is None:
            return None, None, hops
        parent = graph.upstream.get(net)
        if parent is None:
            # No stated direction from here — fall back to an auto-inferred
            # 2-pin link, but only when a single unvisited neighbour makes the
            # step unambiguous.
            candidates = [
                other for other in graph.undirected.get(net, ())
                if other not in visited
            ]
            if len(candidates) > 1:
                return None, "undirected", hops
            if not candidates:
                return None, None, hops
            parent = candidates[0]
        if parent in visited:
            return None, "cycle", hops
        hops += 1
        if hops > _SERIES_VIN_MAX_HOPS:
            return None, "too_deep", hops
        net = parent


def _resolve_regulator_gain(
    params: dict[str, str],
    idx: int | None,
    v_out: float,
    in_p_net: str | None,
    supply_map: dict[str, float],
    role_diag: str,
    result: AnnotationResult,
    series_graph: _SeriesVinGraph | None = None,
    proj: ExtractedProject | None = None,
    net_remap: dict[int, int] | None = None,
) -> tuple[float, str | None, float, bool] | None:
    """Return ``(gain, regulator_type, efficiency, adaptive_gain_eligible)``."""
    gain_key = _channel_key("GAIN", idx)
    type_key = _channel_key("REGULATOR_TYPE", idx)
    eff_key = _channel_key("REGULATOR_EFFICIENCY", idx)

    gain_present = _ci_get(params, gain_key) is not None
    explicit_gain = _optional_value(params, gain_key, role_diag, result)
    if gain_present and explicit_gain is None:
        # PDN_GAIN was set but did not parse — ``_optional_value`` already
        # recorded the error. Abort rather than silently falling through to
        # auto-gain, which would mask the user's typo'd manual value.
        return None
    type_raw = _ci_get(params, type_key)
    reg_type = type_raw.strip().upper() if type_raw else None

    if explicit_gain is not None:
        if reg_type is not None:
            result.warnings.append(
                f"{role_diag}: {gain_key} overrides "
                f"{type_key} / {eff_key}"
            )
        elif _ci_get(params, eff_key) is not None:
            result.warnings.append(
                f"{role_diag}: {eff_key} is ignored when {gain_key} is set"
            )
        return explicit_gain, reg_type, 1.0, False

    if reg_type is None:
        result.errors.append(
            f"{role_diag}: missing {gain_key} or {type_key}"
        )
        return None

    if reg_type not in _REGULATOR_TYPES:
        result.errors.append(
            f"{role_diag}: {type_key}={type_raw!r} — "
            f"must be one of {sorted(_REGULATOR_TYPES)}"
        )
        return None

    if reg_type == "LDO":
        if _ci_get(params, eff_key) is not None:
            result.warnings.append(
                f"{role_diag}: {eff_key} is ignored for LDO"
            )
        return 1.0, reg_type, 1.0, False

    # SMPS
    if _ci_get(params, eff_key) is None:
        eff = 1.0
    else:
        eff = _optional_value(params, eff_key, role_diag, result)
        if eff is None:
            return None
    if not (0.0 < eff <= 1.0):
        result.errors.append(
            f"{role_diag}: {eff_key} must be in (0, 1], got {eff}"
        )
        return None

    if v_out <= 0:
        result.errors.append(
            f"{role_diag}: PDN_V must be positive, got {v_out}"
        )
        return None

    lookup_net = (
        _canonical_supply_net_name(proj, in_p_net, net_remap)
        if proj is not None and in_p_net else in_p_net
    )
    vin, vin_failure, vin_hops = _lookup_inferred_vin(
        lookup_net, supply_map, graph=series_graph,
    )
    in_key = _channel_key("IN_P_NET", idx)
    if vin is None or vin <= 0:
        if vin_failure == "ambiguous":
            result.errors.append(
                f"{role_diag}: cannot infer input voltage for SMPS gain — "
                f"ambiguous SERIES upstream for {in_key}={in_p_net!r}"
            )
        elif vin_failure == "undirected":
            result.errors.append(
                f"{role_diag}: cannot infer input voltage for SMPS gain — "
                f"several auto-inferred SERIES elements meet at "
                f"{in_key}={in_p_net!r} and none states a P/N direction; set "
                f"PDN_P_NET / PDN_N_NET on the one carrying the input power"
            )
        elif vin_failure == "cycle":
            result.errors.append(
                f"{role_diag}: cannot infer input voltage for SMPS gain — "
                f"cyclic SERIES upstream path for {in_key}={in_p_net!r}"
            )
        elif vin_failure == "too_deep":
            result.errors.append(
                f"{role_diag}: cannot infer input voltage for SMPS gain — "
                f"the SERIES chain from {in_key}={in_p_net!r} is longer than "
                f"{_SERIES_VIN_MAX_HOPS} elements without reaching a "
                f"SOURCE/REGULATOR; set PDN_GAIN explicitly"
            )
        else:
            result.errors.append(
                f"{role_diag}: cannot infer input voltage for SMPS gain — "
                f"no unique upstream SOURCE/REGULATOR voltage found for "
                f"{in_key}={in_p_net!r}"
            )
        return None

    if vin_hops:
        # Nothing here proves the walked chain is the power path rather than a
        # sense / bleed / snubber leg that happens to be annotated SERIES, and a
        # wrong Vin scales the gain silently. Say so rather than degrade quietly.
        result.warnings.append(
            f"{role_diag}: Vin={vin:g} V was inferred {vin_hops} SERIES hop(s) "
            f"upstream of {in_key}={in_p_net!r}, not from a rail declared on "
            f"that net — confirm that chain is the power path, or set "
            f"{_channel_key('GAIN', idx)} explicitly"
        )

    gain = v_out / (vin * eff)
    return gain, reg_type, eff, True


def _parse_regulator(comp, proj, enabled_layers, result,
                     net_remap=None, supply_map=None, only_indices=None,
                     series_graph=None):
    role_diag_base = f"REGULATOR on {comp.designator}"
    if _has_single_net_params(comp.parameters, only_indices):
        result.errors.append(
            f"{role_diag_base}: PDN_NET is only valid on SOURCE/SINK — a "
            f"REGULATOR has four terminals, use PDN_OUT_P_NET / PDN_OUT_N_NET "
            f"/ PDN_IN_P_NET / PDN_IN_N_NET"
        )
        return []
    if only_indices is not None:
        indices = list(only_indices)
    else:
        indices = _discover_channel_indices(comp.parameters, "V")
        if not indices:
            result.errors.append(
                f"REGULATOR on {comp.designator}: missing PDN_V "
                f"(or PDN<n>_V for an indexed channel)"
            )
            return []

    pcb_indices = _pcb_indices_for_source(comp, proj)
    if not pcb_indices:
        result.errors.append(
            f"REGULATOR on {comp.designator}: component {comp.designator!r} "
            f"is not placed on the PCB"
        )
        return []
    if len(pcb_indices) > 1:
        names = ", ".join(proj.pcb_components[i].designator for i in pcb_indices)
        result.warnings.append(
            f"REGULATOR on {comp.designator}: expanding to "
            f"{len(pcb_indices)} multi-channel PCB instances ({names})"
        )

    if supply_map is None:
        supply_map = {}

    specs: list[RegulatorSpec] = []
    for idx in indices:
        role_diag = f"REGULATOR on {_channel_label(comp.designator, idx)}"
        params = _materialize_channel_params(comp.parameters, idx, "REGULATOR")
        v = _require_value(
            params, _channel_key("V", idx), role_diag, result,
        )
        in_p_net = _ci_get(params, _channel_key("IN_P_NET", idx))
        resolved = _resolve_regulator_gain(
            params, idx, v, in_p_net,
            supply_map, role_diag, result,
            series_graph=series_graph, proj=proj, net_remap=net_remap,
        ) if v is not None else None
        if v is None or resolved is None:
            continue
        g, reg_type, eff, adaptive = resolved
        q_key = _channel_key("QUIESCENT", idx)
        if _ci_get(params, q_key) is None:
            quiescent = 0.0
        else:
            iq_raw = _optional_value(params, q_key, role_diag, result)
            if iq_raw is None:
                # Present but unparseable — error already recorded; skip the
                # spec rather than building it with a silent quiescent=0.
                continue
            if iq_raw < 0:
                result.errors.append(f"{role_diag}: {q_key} must be >= 0")
                continue
            quiescent = iq_raw
        for pcb_idx in pcb_indices:
            pcb_des = proj.pcb_components[pcb_idx].designator
            inst_diag = (
                f"REGULATOR on {_channel_label(pcb_des, idx)}"
                if len(pcb_indices) > 1 else role_diag
            )
            out = _resolve_two_terminal(
                proj, pcb_idx, params,
                _channel_key("OUT_P_NET", idx), _channel_key("OUT_N_NET", idx),
                _channel_key("OUT_P_PINS", idx), _channel_key("OUT_N_PINS", idx),
                enabled_layers, f"{inst_diag} OUT", result,
                net_remap=net_remap,
                sch_lookup_designator=comp.lookup_designator,
                schdoc_name=comp.schdoc_name,
            )
            in_ = _resolve_two_terminal(
                proj, pcb_idx, params,
                _channel_key("IN_P_NET", idx), _channel_key("IN_N_NET", idx),
                _channel_key("IN_P_PINS", idx), _channel_key("IN_N_PINS", idx),
                enabled_layers, f"{inst_diag} IN", result,
                net_remap=net_remap,
                sch_lookup_designator=comp.lookup_designator,
                schdoc_name=comp.schdoc_name,
            )
            if out is None or in_ is None:
                continue
            specs.append(RegulatorSpec(
                designator=pcb_des, schdoc_name=comp.schdoc_name,
                voltage=v, gain=g,
                out_p=out[0], out_n=out[1],
                in_p=in_[0], in_n=in_[1],
                channel_index=idx,
                regulator_type=reg_type,
                efficiency=eff,
                adaptive_gain_eligible=adaptive,
                quiescent_current=quiescent,
            ))
    return specs


_PARSER_BY_ROLE = {
    "SOURCE": _parse_source,
    "SINK": _parse_sink,
    "SERIES": _parse_resistance,
    "REGULATOR": _parse_regulator,
}


# --- cross-directive validation ----------------------------------------------

def _spec_terminals(d: DirectiveSpec) -> list[TerminalSpec]:
    """Every resolved terminal of a directive. A single-net SOURCE/SINK has
    no N terminal (its return is ideal), so only its P terminal is listed."""
    if isinstance(d, RegulatorSpec):
        return [d.out_p, d.out_n, d.in_p, d.in_n]
    terms = [d.p]
    if getattr(d, "n", None) is not None:
        terms.append(d.n)
    return terms


def _validate_directive_groups(result: AnnotationResult,
                               proj: ExtractedProject | None,
                               parameter_sources: list[PdnParameterSource]
                               | None = None) -> None:
    """Cross-directive checks on every analysis group + return-node grouping.

    An *analysis group* is a set of directives that share copper (their
    terminals touch a common net, transitively, including SERIES bridges).
    Within a group:

    * single-net (``PDN_NET``) and two-terminal (``PDN_P_NET``/``PDN_N_NET``)
      SOURCE/SINK directives may not be mixed — they disagree on the return
      path (this is an error).

    The open-loop check (a group with only sources or only sinks) is NOT done
    here — it moved to :func:`fypa.altium.loader._flag_open_loop_rails`, which
    runs over the final merged directive list at solve time so the rail is
    skipped and warned about rather than blocking the whole board.

    Single-net directives in one group are stamped with a shared
    ``return_group`` id; the loader gives each group one ideal-return node so
    its point-to-point loop closes.
    """
    directives = result.directives
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    def dir_nets(d: DirectiveSpec) -> set[int]:
        nets: set[int] = set()
        for term in _spec_terminals(d):
            for pin in term.pins:
                if pin.net_index != NO_NET:
                    nets.add(pin.net_index)
        return nets

    # Every net one directive touches belongs to the same group.
    for d in directives:
        nets = sorted(dir_nets(d))
        for other in nets[1:]:
            union(nets[0], other)
    # SERIES bridges (ferrite / 0 Ω link) join the nets they span, so a
    # point-to-point check across one stays a single group.
    if proj is not None and parameter_sources:
        _union_series_bridge_net_indices(proj, parameter_sources, union)

    groups: dict[int, list[DirectiveSpec]] = {}
    for d in directives:
        nets = dir_nets(d)
        if not nets:
            continue  # unresolved directive — already errored elsewhere
        groups.setdefault(find(min(nets)), []).append(d)

    return_group_by_root: dict[int, int] = {}
    next_group_id = 0
    for root, members in groups.items():
        single = [d for d in members
                  if isinstance(d, (SourceSpec, SinkSpec)) and d.n is None]
        two = [d for d in members
               if isinstance(d, (SourceSpec, SinkSpec)) and d.n is not None]
        two += [d for d in members if isinstance(d, RegulatorSpec)]
        labels = ", ".join(sorted(
            _channel_label(d.designator, getattr(d, "channel_index", None))
            for d in members
        ))
        if single and two:
            result.errors.append(
                f"analysis group ({labels}): mixes single-net (PDN_NET) and "
                f"two-terminal (PDN_P_NET/PDN_N_NET) directives — every SOURCE "
                f"and SINK sharing a net must use the same mode"
            )
            continue
        if not single:
            continue
        # NOTE: the open-loop check (a single-net group with only sources or
        # only sinks) used to be a hard error here, which blocked the whole
        # board from solving. It now lives in
        # :func:`fypa.altium.loader._flag_open_loop_rails`, which runs over the
        # final merged directive list (schematic + editor) at solve time: it
        # marks just that rail ``solve_excluded`` and warns, so the rest of
        # the board still solves and the markers are kept. The ``return_group``
        # stamping below stays so a closed single-net loop still works.
        return_group_by_root[root] = next_group_id
        next_group_id += 1

    if not return_group_by_root:
        return
    # Stamp the shared return-group id onto every single-net directive. A
    # single-net directive in a mixed (errored) group has no return group —
    # leave it unstamped; the errors block the solve anyway.
    stamped: list[DirectiveSpec] = []
    for d in directives:
        if isinstance(d, (SourceSpec, SinkSpec)) and d.n is None:
            nets = dir_nets(d)
            gid = return_group_by_root.get(find(min(nets))) if nets else None
            if gid is not None:
                d = replace(d, return_group=gid)
        stamped.append(d)
    result.directives = stamped


def _warn_unknown_pdn_params(
    comp: PdnParameterSource,
    part_role: str,
    result: AnnotationResult,
) -> None:
    """Warn on PDN_* parameter names that are not recognized for a channel's
    effective role.

    Each parameter is checked against the allowed suffix set of *its own
    channel's* role — the part-wide ``PDN_ROLE`` unless the channel overrides
    it with ``PDN<n>_ROLE`` — so e.g. a ``PDN2_V`` on a SOURCE channel of an
    otherwise-SINK part is not flagged."""
    diag = f"{comp.designator} ({comp.schdoc_name})"
    for key, raw in comp.parameters.items():
        if raw is None or not str(raw).strip():
            continue
        m = _INDEXED_KEY_RE.match(key.strip())
        if m is None:
            continue
        idx_str, suffix = m.group(1), m.group(2)
        suffix_u = suffix.upper()
        if suffix_u == "ROLE":
            continue
        idx = int(idx_str) if idx_str else None
        eff_role = _effective_role(comp.parameters, idx, part_role)
        if eff_role not in VALID_ROLES:
            # An invalid PDN<n>_ROLE override — _resolve_channel_roles already
            # errors on it; don't pile on per-parameter "unknown" noise.
            continue
        allowed = _KNOWN_SUFFIXES_BY_ROLE.get(eff_role, frozenset())
        if suffix_u in allowed:
            continue
        ch = f"#{idx}" if idx is not None else ""
        if suffix_u == "PIN":
            suggest = _channel_key("P_PINS", idx)
            result.warnings.append(
                f"{diag}{ch}: unknown parameter {key!r} — did you mean "
                f"{suggest}?"
            )
            continue
        if suffix_u.startswith("OUT_") and eff_role in ("SOURCE", "SINK"):
            result.warnings.append(
                f"{diag}{ch}: {key!r} is a REGULATOR parameter — did you mean "
                f"to set the channel's role to REGULATOR "
                f"({_channel_key('ROLE', idx)}=REGULATOR)?"
            )
            continue
        result.warnings.append(
            f"{diag}{ch}: unknown PDN parameter {key!r} (ignored)"
        )


def _resolve_channel_roles(
    params: dict[str, str],
    part_role: str,
    designator: str,
    result: AnnotationResult,
    *,
    report_errors: bool = True,
) -> dict[str, list[int | None]]:
    """Group a part's present channels by effective role.

    Each channel's effective role is its ``PDN<n>_ROLE`` override (validated
    against :data:`VALID_ROLES`) or the part-wide ``part_role``. A channel is
    *present* when it carries a value param, a terminal param for an *active*
    role on the part, or an indexed ``PDNn_ROLE`` — the value may be
    inherited from the unindexed template via :func:`_channel_get`. Returns
    ``{role: [index, …]}`` in discovery order (unindexed first, then
    ascending index).

    Discovery is scoped to :func:`_active_roles_for_discovery` so a leftover
    cross-role value (e.g. ``PDN1_R`` on a SINK-only part) does not invent a
    phantom channel.

    When indexed channels exist and the unindexed form lacks a *complete*
    terminal set for its role, ``None`` is omitted (template-only). A real
    unindexed channel alongside indexed ones (complete terminals) is kept.
    Parts with only ``PDNn_ROLE`` (no part-wide ``PDN_ROLE``) always treat
    the unindexed form as template-only when indexed channels exist.

    A channel that declares a role (or a cross-role value / terminal) but is
    missing the value parameter its role needs — even after template
    inheritance — produces an error and is dropped when ``report_errors`` is
    true; dry-run callers (bridge / supply maps) pass ``report_errors=False``
    to omit invalid channels quietly.
    """
    candidates: set[int | None] = set()
    marking_suffixes: set[str] = set()
    for role in _active_roles_for_discovery(params, part_role):
        marking_suffixes.add(_VALUE_SUFFIX_BY_ROLE[role])
        marking_suffixes |= set(_TERMINAL_SUFFIXES_BY_ROLE[role])
    for suffix in marking_suffixes:
        candidates.update(_discover_channel_indices(params, suffix))
    # An indexed PDN<n>_ROLE override also marks its channel present, so a
    # channel that declares a role but omits its value param gets a clear
    # error. The *unindexed* PDN_ROLE is the part-wide default (not a
    # channel), so it is excluded here.
    for idx in _discover_channel_indices(params, "ROLE"):
        if idx is not None:
            candidates.add(idx)

    has_indexed = any(idx is not None for idx in candidates)
    if has_indexed and None in candidates:
        # Drop legacy when it is only a value/meta template. Indexed-only
        # parts (no part-wide PDN_ROLE) always treat unindexed params as
        # templates — otherwise PDN_R / PDN_V would error as "missing
        # PDN_ROLE" while still emitting the indexed directives.
        value_key = _channel_key(_VALUE_SUFFIX_BY_ROLE.get(part_role, ""), None)
        if part_role not in VALID_ROLES:
            candidates.discard(None)
        elif _ci_get(params, value_key) is None:
            # Shared terminals but no value of its own — a terminal template
            # (PDN_P_NET + PDN_N_NET with only PDN1_I / PDN2_I). Keeping it
            # would report a "missing PDN_I" for a channel the user never
            # declared. The value must be set *directly*: inheriting it from
            # itself is what makes this a template in the first place.
            candidates.discard(None)
        elif not _unindexed_has_defining_terminals(params, part_role):
            candidates.discard(None)

    ordered = sorted(candidates, key=lambda x: (x is not None, x or 0))

    grouped: dict[str, list[int | None]] = {}
    for idx in ordered:
        override_raw = _ci_get(params, _channel_key("ROLE", idx))
        if override_raw is not None:
            eff = override_raw.strip().upper()
            if eff not in VALID_ROLES:
                if report_errors:
                    result.errors.append(
                        f"{_channel_label(designator, idx)}: unknown "
                        f"{_channel_key('ROLE', idx)}={override_raw!r} — must be "
                        f"one of {sorted(VALID_ROLES)}"
                    )
                continue
        else:
            if not part_role:
                if report_errors:
                    result.errors.append(
                        f"{_channel_label(designator, idx)}: missing "
                        f"{_channel_key('ROLE', idx)} (no part-wide PDN_ROLE)"
                    )
                continue
            eff = part_role
        value_suffix = _VALUE_SUFFIX_BY_ROLE[eff]
        if _channel_get(params, value_suffix, idx, eff) is None:
            if report_errors:
                value_key = _channel_key(value_suffix, idx)
                inheritable = value_suffix in _inheritable_suffixes(
                    params, idx, eff,
                )
                if idx is not None and inheritable:
                    result.errors.append(
                        f"{eff} on {_channel_label(designator, idx)}: missing "
                        f"{value_key} (or template "
                        f"{_channel_key(value_suffix, None)})"
                    )
                else:
                    # Either the legacy channel (no parent to inherit from) or
                    # a channel whose PDN<n>_ROLE differs from the part-wide
                    # role, so the unindexed value belongs to another role and
                    # naming it as a template would send the user the wrong way.
                    result.errors.append(
                        f"{eff} on {_channel_label(designator, idx)}: missing "
                        f"{value_key}"
                    )
            continue
        grouped.setdefault(eff, []).append(idx)
    return grouped


def _is_nettie_component_kind(kind: int) -> bool:
    return int(kind) in NET_TIE_COMPONENT_KINDS


def _schdocs_with_pcb_placements(proj: ExtractedProject) -> set[str]:
    """Schematic sheets that placed at least one component on this board.

    A .PrjPcb can hold several .PcbDoc files while ``extract_project`` loads
    only the selected one, so most sheets of a multi-board project contribute
    nothing here. Used to tell "this tie is missing from the board" (worth a
    warning) from "this tie belongs to a different board" (not).
    """
    placed: set[str] = set()
    for pcb in proj.pcb_components:
        if pcb.source_designator:
            placed.add(pcb.source_designator.upper())
        if pcb.designator:
            placed.add(pcb.designator.upper())
    return {
        sch.schdoc_name for sch in proj.sch_components
        if sch.designator.upper() in placed
    }


def _nettie_net_names(proj: ExtractedProject, pcb_index: int) -> list[str]:
    """Distinct connected net *names* on a PCB placement, pad order preserved.

    Deduplicated by name (case-insensitively), not by net index. A
    multi-channel board stores one net per channel and Altium does not
    channel-qualify the names in Nets6, so two distinct indices can share one
    name — see :func:`_net_indices_by_name`. The chain built from this list is
    resolved by :func:`_resolve_terminal`, which matches on the *name*, so
    keeping both indices would pair a net with itself: P and N would resolve to
    the same pads and :func:`_arbitrate_overlapping_terminals` would reject the
    tie as a short instead of bridging it.
    """
    pads = _pads_by_component_all(proj).get(pcb_index, [])
    names: list[str] = []
    seen: set[str] = set()
    for pad in pads:
        ni = pad.net_index
        if ni == NO_NET or not (0 <= ni < len(proj.nets)):
            continue
        name = proj.nets[ni].name
        key = name.strip().upper()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def _nettie_skip_reason(proj: ExtractedProject, pcb_index: int,
                        net_names: list[str]) -> str:
    """Why a Net Tie placement has nothing to bridge.

    Keyed on the distinct-net count the bridge actually needs.
    :func:`_autoinfer_failure_reason` describes a different guard (the 2-pin
    auto-infer rules) and would tell the user a 3-pad tie failed because of its
    pad count, which the N-pad chain does not care about.
    """
    pads = _pads_by_component_all(proj).get(pcb_index, [])
    if not pads:
        return "the footprint has no pads"
    if not net_names:
        return f"none of its {len(pads)} pad(s) are connected to a net"
    return (
        f"all {len(pads)} pad(s) are on one net ({net_names[0]!r}) — "
        f"there is nothing to bridge"
    )


def _synth_nettie_bridge_for_instance(
    proj: ExtractedProject,
    pcb_index: int,
    schdoc_name: str,
    enabled_layers: list[int],
    result: AnnotationResult,
    net_remap: dict[int, int] | None,
) -> list[ResistorSpec]:
    """Build low-Ω SERIES specs that short every distinct NetTie net together."""
    pcb_des = proj.pcb_components[pcb_index].designator
    role_diag = f"NetTie on {pcb_des}"
    net_names = _nettie_net_names(proj, pcb_index)
    if len(net_names) < 2:
        reason = _nettie_skip_reason(proj, pcb_index, net_names)
        result.note_absorbed(
            f"{role_diag} ({schdoc_name}): skipped auto-bridge — {reason}"
        )
        return []

    # Resolution failures here are not the user's annotation mistakes: nobody
    # asked for this directive, it was inferred from ComponentKind. Collect
    # into a scratch result and downgrade its errors to warnings, so a Net Tie
    # the bridge cannot resolve does not fail `fypa annotations` in CI or raise
    # a load-time error telling the user to set PDN_N_PINS on a part they never
    # annotated. The advice in those messages still holds for anyone who wants
    # to take over with explicit PDN_* parameters, so it is kept verbatim.
    scratch = AnnotationResult()
    specs: list[ResistorSpec] = []
    # Chain consecutive nets so N nets become N-1 shorts (union-find merge
    # collapses the whole set). Two-pin NetTies take the single pair path.
    for p_net, n_net in zip(net_names, net_names[1:]):
        params = {"PDN_P_NET": p_net, "PDN_N_NET": n_net}
        pair = _resolve_two_terminal(
            proj, pcb_index, params,
            "PDN_P_NET", "PDN_N_NET", "PDN_P_PINS", "PDN_N_PINS",
            enabled_layers, role_diag, scratch,
            net_remap=net_remap,
            schdoc_name=schdoc_name,
        )
        if pair is None:
            continue
        specs.append(ResistorSpec(
            designator=pcb_des,
            schdoc_name=schdoc_name,
            resistance=NET_TIE_BRIDGE_RESISTANCE_OHM,
            p=pair[0],
            n=pair[1],
            channel_index=None,
        ))
    for warning in scratch.warnings:
        result.note_absorbed(warning)
    for err in scratch.errors:
        result.note_absorbed(f"{err} (auto-bridge skipped)")
    if specs:
        result.note_absorbed(
            f"{role_diag} ({schdoc_name}): auto-bridged "
            f"{' ↔ '.join(net_names)} as low-Ω NetTie short "
            f"({NET_TIE_BRIDGE_RESISTANCE_OHM * 1e3:.2g} mΩ)"
        )
    return specs


def _synth_nettie_directives(
    proj: ExtractedProject,
    enabled_layers: list[int],
    result: AnnotationResult,
    skip_set: set[str],
    net_remap: dict[int, int] | None = None,
) -> list[ResistorSpec]:
    """Emit synthetic SERIES bridges for Altium Net Tie schematic components.

    Components with ``ComponentKind`` Net Tie / Net Tie (No BOM) short their
    pads by design. Without PDN annotations FYPA would leave those nets
    disconnected; this pass synthesises a low-Ω SERIES directive per PCB
    placement so the loader's net-merge path collapses them.

    A Net Tie carrying ``ComponentKind`` only on the PCB record — added by ECO,
    or a board opened without its schematics — is picked up too, so the bridge
    does not depend on a matching symbol existing.

    An explicit ``PDN_ROLE`` on the same part wins — auto-bridge is skipped.
    """
    specs: list[ResistorSpec] = []
    # Every placement this pass has decided about, bridged or deliberately not.
    # The PCB sweep at the end consults it so a schematic opt-out is not undone
    # by the same ComponentKind sitting on the footprint record.
    handled_pcb: set[int] = set()
    placed_schdocs = _schdocs_with_pcb_placements(proj)

    def can_bridge(pcb_idx: int) -> bool:
        if pcb_idx in handled_pcb:
            return False
        handled_pcb.add(pcb_idx)
        pcb = proj.pcb_components[pcb_idx]
        # PCB Blanket/ECO PDN_* on the placement overrides auto-bridge,
        # same as schematic PDN_* on the symbol.
        if _has_any_pdn_params(pcb.parameters):
            return False
        return pcb.designator.upper() not in skip_set

    for sch in proj.sch_components:
        if not _is_nettie_component_kind(sch.component_kind):
            continue
        pcb_indices = _find_pcb_instances(proj, sch.designator)
        # Any schematic PDN_* (even incomplete) opts out of auto-bridge so a
        # half-finished annotation is not silently replaced by a merge short.
        # The placements are still marked handled: Altium stamps ComponentKind
        # on the footprint too, so the PCB sweep below would otherwise bridge
        # straight over the user's own directive.
        if _has_any_pdn_params(sch.parameters) or sch.designator.upper() in skip_set:
            handled_pcb.update(pcb_indices)
            continue
        if not pcb_indices:
            # extract_project reads every SchDoc in the .PrjPcb but only one
            # .PcbDoc, so on a multi-board project every Net Tie belonging to
            # another board lands here. Warn only for sheets that placed
            # something on *this* board — otherwise the log fills with notices
            # about parts the user never asked this board to contain.
            if sch.schdoc_name in placed_schdocs:
                result.note_absorbed(
                    f"NetTie {sch.designator} ({sch.schdoc_name}): no PCB "
                    f"placement found — cannot auto-bridge"
                )
            continue
        for pcb_idx in pcb_indices:
            if can_bridge(pcb_idx):
                specs.extend(_synth_nettie_bridge_for_instance(
                    proj, pcb_idx, sch.schdoc_name, enabled_layers,
                    result, net_remap,
                ))

    # PCB-side Net Ties with no schematic counterpart reached above.
    for pcb_idx, pcb in enumerate(proj.pcb_components):
        if pcb_idx in handled_pcb:
            continue
        if not _is_nettie_component_kind(pcb.component_kind):
            continue
        if not can_bridge(pcb_idx):
            continue
        lookup_des = pcb.source_designator or pcb.designator
        specs.extend(_synth_nettie_bridge_for_instance(
            proj, pcb_idx,
            _schdoc_for_pcb_instance(proj, pcb_idx, lookup_des),
            enabled_layers, result, net_remap,
        ))
    return specs


# --- public entry -------------------------------------------------------------

def parse_annotations(proj: ExtractedProject,
                      enabled_layers: list[int] | None = None,
                      skip_designators: set[str] | None = None,
                      net_remap: dict[int, int] | None = None,
                      ) -> AnnotationResult:
    """Scan schematic and PCB components for PDN_* parameters and build directives.

    `enabled_layers` is the Top→Bottom-ordered list of copper layer ids
    (from :meth:`ExtractedProject.enabled_copper_layer_ids`). If omitted we
    compute it ourselves.

    `skip_designators` is an optional case-insensitive set of designators
    to skip entirely. Used by the loader's net-merge pre-pass: SERIES
    directives that were identified as net-merging shorts on the first
    parse are skipped on the second parse (after the merge has been
    applied), because both their pins would now resolve to the same net.

    `net_remap` is an optional ``{non_canonical_net_index:
    canonical_net_index}`` map applied after every ``_net_index_by_name``
    lookup. Used by the loader's net-merge pre-pass so user annotations
    that reference EITHER the canonical or the non-canonical merged name
    still resolve to the correct (canonical) net index — pads on the
    merged net have all been remapped to the canonical index.
    """
    if enabled_layers is None:
        enabled_layers = proj.enabled_copper_layer_ids()
    if not enabled_layers:
        return AnnotationResult(errors=[
            "no enabled copper layers — cannot place terminals"
        ])

    result = AnnotationResult()
    seen_designators: set[str] = set()
    skip_set: set[str] = {d.upper() for d in (skip_designators or set())}

    for comp in proj.sch_components:
        stray = [k for k in comp.parameters if k.upper().startswith(PARAM_PREFIX)]
        if stray and not _is_pdn_annotated(comp.parameters):
            result.warnings.append(
                f"{comp.designator} ({comp.schdoc_name}): has {len(stray)} "
                f"PDN_* parameter(s) but no PDN_ROLE or PDN<n>_ROLE — "
                f"directive ignored"
            )
    for pcb in proj.pcb_components:
        stray = [k for k in pcb.parameters if k.upper().startswith(PARAM_PREFIX)]
        if stray and not _is_pdn_annotated(pcb.parameters):
            lookup = pcb.source_designator or pcb.designator
            result.warnings.append(
                f"{pcb.designator} (PCB, from {lookup}): has {len(stray)} "
                f"PDN_* parameter(s) but no PDN_ROLE or PDN<n>_ROLE — "
                f"directive ignored"
            )

    parameter_sources = _iter_pdn_parameter_sources(proj)

    supply_map = _collect_supply_voltages_by_net(
        parameter_sources, proj, net_remap=net_remap,
    )
    series_graph = _collect_series_upstream_map(
        parameter_sources, proj, net_remap=net_remap,
        skip_designators=skip_set,
    )

    for comp in parameter_sources:
        if comp.designator.upper() in skip_set:
            continue  # Absorbed by net-merge pre-pass — see fypa.altium.loader.
        role_raw = _ci_get(comp.parameters, ROLE_KEY)
        if role_raw is None:
            if not _has_indexed_role_params(comp.parameters):
                continue
            role = ""
        else:
            role = role_raw.strip().upper()
            if role not in VALID_ROLES:
                result.errors.append(
                    f"{comp.designator} ({comp.schdoc_name}): unknown "
                    f"PDN_ROLE={role_raw!r} — must be one of "
                    f"{sorted(VALID_ROLES)}"
                )
                continue

        # A designator with a SOURCE in one schdoc and SINK in another would be
        # ambiguous — flag duplicates (schematic logical designators only).
        key = comp.lookup_designator.upper()
        if comp.pcb_index is None and key in seen_designators:
            result.warnings.append(
                f"{comp.designator}: appears in multiple schdocs with PDN_ROLE — "
                f"only the first occurrence is used"
            )
            continue
        if comp.pcb_index is None:
            seen_designators.add(key)

        _warn_unknown_pdn_params(comp, role, result)

        # Split the part's channels by effective role (part-wide PDN_ROLE, or
        # a per-channel PDN<n>_ROLE override) and dispatch each group to its
        # role parser. A uniform-role part yields a single group, so single-
        # and multi-channel same-role parts behave exactly as before; a mixed
        # part (source + sink on one component) yields one group per role.
        channel_roles = _resolve_channel_roles(
            comp.parameters, role, comp.designator, result,
        )
        if not channel_roles:
            # No resolvable channels — call the part-role parser so it emits
            # the role-appropriate "missing PDN_V / PDN_I / …" diagnostic.
            if not role:
                continue
            specs = _PARSER_BY_ROLE[role](comp, proj, enabled_layers, result,
                                          net_remap=net_remap,
                                          supply_map=supply_map,
                                          series_graph=series_graph)
            result.directives.extend(specs)
        else:
            for chan_role, idxs in channel_roles.items():
                specs = _PARSER_BY_ROLE[chan_role](
                    comp, proj, enabled_layers, result,
                    net_remap=net_remap,
                    supply_map=supply_map, only_indices=idxs,
                    series_graph=series_graph,
                )
                # Every parser returns a list — empty if the directive failed
                # to resolve, one element per resolved channel otherwise.
                result.directives.extend(specs)

    # Altium Net Ties short their pads by ComponentKind — synthesise low-Ω
    # SERIES bridges so the loader merge path connects those nets without
    # requiring a PDN_ROLE=SERIES on every NetTie symbol.
    result.directives.extend(_synth_nettie_directives(
        proj, enabled_layers, result, skip_set, net_remap=net_remap,
    ))

    # Cross-directive checks (mode consistency, open-loop) + return grouping.
    _validate_directive_groups(result, proj, parameter_sources)
    return result


# --- self-check ---------------------------------------------------------------

def _describe_terminal(label: str, term: TerminalSpec) -> str:
    parts = [
        f"{p.pad_designator}@layer{p.layer_id}({p.point.x:.2f},{p.point.y:.2f})"
        for p in term.pins
    ]
    return f"    {label:<8} pins: {', '.join(parts) if parts else '(none)'}"


def _describe_terminal_n(term: TerminalSpec | None) -> str:
    """N-terminal line — single-net directives have an ideal return instead."""
    if term is None:
        return f"    {'N':<8} (ideal 0 Ω return — single-net check)"
    return _describe_terminal("N", term)


def _describe_directive(d: DirectiveSpec) -> str:
    label = _channel_label(d.designator, getattr(d, "channel_index", None))
    head = f"  {type(d).__name__:<14} {label}  ({d.schdoc_name})"
    if isinstance(d, SourceSpec):
        return head + f"  V={d.voltage:g} V\n" + \
            _describe_terminal("P", d.p) + "\n" + _describe_terminal_n(d.n)
    if isinstance(d, SinkSpec):
        return head + f"  I={d.current:g} A\n" + \
            _describe_terminal("P", d.p) + "\n" + _describe_terminal_n(d.n)
    if isinstance(d, ResistorSpec):
        return head + f"  R={d.resistance:g} Ω\n" + \
            _describe_terminal("P", d.p) + "\n" + _describe_terminal("N", d.n)
    if isinstance(d, RegulatorSpec):
        extra = ""
        if d.regulator_type:
            extra = f", type={d.regulator_type}"
            if d.regulator_type == "SMPS":
                extra += f", eff={d.efficiency:g}"
            if d.adaptive_gain_eligible:
                extra += ", adaptive"
        return head + f"  V={d.voltage:g} V, gain={d.gain:g}{extra}\n" + \
            _describe_terminal("OUT_P", d.out_p) + "\n" + _describe_terminal("OUT_N", d.out_n) + "\n" + \
            _describe_terminal("IN_P", d.in_p) + "\n" + _describe_terminal("IN_N", d.in_n)
    return head


if __name__ == "__main__":
    import sys
    from fypa.altium.extract import extract_project

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    if len(sys.argv) != 2:
        print("usage: python -m fypa.altium.annotations PATH_TO.PrjPcb", file=sys.stderr)
        sys.exit(2)

    proj = extract_project(sys.argv[1])
    result = parse_annotations(proj)
    print(result.summary())
    print()
    for d in result.directives:
        print(_describe_directive(d))
    sys.exit(0 if result.ok else 1)
