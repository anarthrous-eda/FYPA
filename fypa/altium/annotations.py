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

Indices are sparse (any positive integer; gaps allowed); a channel is
"present" iff its value param (``PDNn_V`` for SOURCE / REGULATOR,
``PDNn_I`` for SINK, ``PDNn_R`` for SERIES) is set. All four roles support
the same indexed-prefix scheme.

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
"""
from __future__ import annotations

import logging
import re
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


# --- SI value parsing ---------------------------------------------------------

_SI_PREFIXES: dict[str, float] = {
    "f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6,
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
            (?P<eng_letter>[a-zA-Zµ])     # 3V3 form: int + unit-letter + frac
            (?P<eng_frac>\d+)?
        )?
        |
        (?P<leaddot>\.\d+)               # .001 — no leading integer digit
    )
    (?P<rest>[a-zA-Zµ%Ω]*)            # SI prefix / unit suffix
    \s*$
    """,
    re.VERBOSE,
)

_SCI_VALUE_RE = re.compile(
    r"""^\s*
    (?P<mantissa>[+-]?(?:\d+\.?\d*|\.\d+)[eE][+-]?\d+)
    (?P<rest>[a-zA-Zµ%Ω]*)
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


# Value parameter suffix that makes a channel "present" for each role. A
# channel exists iff the value param for its (effective) role is set —
# ``PDN<n>_V`` for SOURCE / REGULATOR, ``PDN<n>_I`` for SINK, ``PDN<n>_R``
# for SERIES.
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


@dataclass(frozen=True)
class TerminalSpec:
    """A lumped element's terminal — the set of pads electrically tied to it."""
    pins: tuple[TerminalPin, ...]
    # The net the directive named for this terminal (the PDN_*_NET value),
    # kept purely for display. It differs from the pins' actual nets when the
    # terminal resolved via a SERIES bridge — the component has no pad on the
    # named net, so the resolver matched pads on a bridged-equivalent net
    # instead. ``None`` when the terminal was given by a *_PINS override.
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
    if not lookup_des:
        return ""
    target_des = lookup_des.upper()
    if pads_by_component is None:
        pads_by_component = _build_pads_by_component(proj)
    routed_pads = pads_by_component.get(pcb_index, {})

    sheet_votes: dict[str, int] = {}
    sheet_paths: dict[str, str] = {}

    if routed_pads:
        if netlist_index is None:
            netlist_index = _build_netlist_designator_index(proj.compiled_netlist)
        for pin_key, names, sheets in netlist_index.get(target_des, ()):
            pad = routed_pads.get(pin_key)
            if pad is None:
                continue
            if proj.nets[pad.net_index].name.upper() not in names:
                continue
            for sheet in sheets:
                key = sheet.replace("\\", "/").lower()
                sheet_votes[key] = sheet_votes.get(key, 0) + 1
                sheet_paths.setdefault(key, sheet)

    if sheet_votes:
        # Deterministic tie-break: highest vote, ties broken by the
        # lexicographically smallest sheet key (sorted() before max()).
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
        c.schdoc_name for c in proj.sch_components
        if c.designator.upper() == target_des
    ]
    if len(sch_matches) == 1:
        return sch_matches[0]
    return ""


def _iter_pdn_parameter_sources(proj: ExtractedProject) -> list[PdnParameterSource]:
    """Schematic PDN directives plus PCB-only directives (Blanket ECO path)."""
    sources: list[PdnParameterSource] = []
    sch_with_role: set[str] = set()

    for comp in proj.sch_components:
        if _ci_get(comp.parameters, ROLE_KEY) is None:
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
        if _ci_get(pcb.parameters, ROLE_KEY) is None:
            continue
        lookup_des = pcb.source_designator or pcb.designator
        if lookup_des.upper() in sch_with_role:
            continue
        if pads_by_component is None:
            # Built once, only when a PCB-sourced directive actually exists.
            pads_by_component = _build_pads_by_component(proj)
            netlist_index = _build_netlist_designator_index(
                proj.compiled_netlist
            )
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


def _sheet_name_matches(schdoc_name: str, source_sheets: list[str]) -> bool:
    # A net with no recorded sheet provenance (e.g. a global/power net) cannot
    # contradict the inferred instance sheet, so it is accepted. This is the
    # one remaining permissive path; cross-instance mis-binding is bounded by
    # the routed-pin scoping the caller applies (see _resolve_local_net_pins).
    if not source_sheets:
        return True
    if not schdoc_name:
        return False
    target_full = schdoc_name.replace("\\", "/").lower()
    target_base = Path(schdoc_name).name.lower()
    has_dir = "/" in target_full
    for sheet in source_sheets:
        s = sheet.replace("\\", "/").lower()
        if s == target_full:
            return True
        if not has_dir and Path(sheet).name.lower() == target_base:
            return True
    return False


def _resolve_local_net_pins(
    netlist,
    sch_designator: str,
    schdoc_name: str,
    local_net_name: str,
    *,
    routed_pin_keys: set[str] | None = None,
    pcb_designator: str | None = None,
) -> list[str]:
    """Return pin designators on ``sch_designator`` for a local sheet net name.

    ``pcb_designator`` is the placed instance's (possibly channel-flattened)
    designator, e.g. ``J3_SL8M3``. In a repeated ("multi-channel") sheet the
    compiled netlist keys both terminals and mangled net-label aliases by that
    flattened form, whereas ``sch_designator`` is the base schematic designator
    (``J3``); terminal and alias matching accept either.
    """
    if netlist is None:
        return []
    # Netlist terminal designators may be the base schematic designator or the
    # channel-flattened instance designator depending on the design; accept
    # both. The channel token lives on the flattened form.
    des_candidates = {sch_designator.upper()}
    if pcb_designator:
        des_candidates.add(pcb_designator.upper())
    ln = local_net_name.upper()

    def _label_matches(label: str | None) -> bool:
        # Exact label, or the channel-mangled form Altium generates inside a
        # repeated ("multi-channel") sheet: a local label ``S00A`` in sheet
        # instance ``SL8M3`` compiles to ``S00A_SL8M3`` (carried as an alias of
        # the flattened physical net, e.g. ``IOUT3``). The flattened instance
        # designator gains the same suffix (``J3`` → ``J3_SL8M3``), so accept a
        # ``<net>_<channel>`` alias only when one of this instance's designators
        # ends with ``_<channel>``. That binds the alias to this connector's own
        # channel and stops sibling instances (``S00A_SL8M0``) from matching.
        if not label:
            return False
        lu = label.upper()
        if lu == ln:
            return True
        if lu.startswith(ln + "_"):
            channel = lu[len(ln) + 1:]
            if channel and any(d.endswith("_" + channel) for d in des_candidates):
                return True
        return False

    pins: list[str] = []
    seen: set[str] = set()
    unscoped_used = False
    for net in netlist.nets:
        names = [net.name, *getattr(net, "aliases", ())]
        if not any(_label_matches(n) for n in names):
            continue
        net_sheets = list(getattr(net, "source_sheets", ()) or ())
        if not _sheet_name_matches(schdoc_name, net_sheets):
            continue
        for term in net.terminals:
            if term.designator.upper() not in des_candidates:
                continue
            pin = str(term.pin)
            key = pin.upper()
            if routed_pin_keys is not None and key not in routed_pin_keys:
                continue
            if key not in seen:
                seen.add(key)
                pins.append(pin)
                if schdoc_name and not net_sheets:
                    unscoped_used = True
    if unscoped_used:
        log.debug(
            "Local net %r on %s resolved via net(s) lacking sheet provenance; "
            "match scoped by routed pins only.",
            local_net_name, sch_designator,
        )
    return pins


def _terminal_layer_for_pad(pad: RawPad, enabled_layers: list[int]) -> int:
    """For SMT pads → their layer; for through-hole pads → topmost enabled copper layer.

    The geometry module places through-hole copper on every layer and the via barrel
    couples them, so attaching the lumped element to the top layer is sufficient.
    """
    if pad.is_through_hole or pad.layer_id == MULTI_LAYER_PAD_LAYER_ID:
        return enabled_layers[0]
    return pad.layer_id


def _resolve_terminal(
    proj: ExtractedProject,
    pcb_index: int,
    net_name: str | None,
    override_pins: list[str] | None,
    enabled_layers: list[int],
    role_diagnostic: str,
    bridge_groups: dict[str, frozenset[str]] | None = None,
    warnings: list[str] | None = None,
    net_remap: dict[int, int] | None = None,
    sch_lookup_designator: str | None = None,
    schdoc_name: str | None = None,
) -> tuple[TerminalSpec | None, list[str]]:
    """Resolve a terminal to its participating pads.

    ``pcb_index`` indexes :attr:`ExtractedProject.pcb_components` — one
    specific placed component. Callers obtain it from
    :func:`_find_pcb_instances`, which is what tells the channels of a
    multi-channel part apart (their PCB designators may be identical).

    Returns ``(spec, errors)``. If ``errors`` is non-empty, ``spec`` is ``None``.
    If ``bridge_groups`` is supplied and the literal net match fails, the
    resolver retries against every net in the same SERIES-bridge group and
    appends a one-line note to ``warnings`` naming the bridge used.
    """
    errors: list[str] = []
    resolved_via_local = False
    designator = proj.pcb_components[pcb_index].designator
    component_pads = _pads_by_component_all(proj).get(pcb_index, [])
    if not component_pads:
        errors.append(
            f"{role_diagnostic}: component {designator!r} has no pads on the PCB"
        )
        return None, errors

    if override_pins:
        wanted = {pin.upper() for pin in override_pins}
        matched = [p for p in component_pads if p.designator.upper() in wanted]
        missing = wanted - {p.designator.upper() for p in matched}
        if missing:
            errors.append(
                f"{role_diagnostic}: pin overrides on {designator} not found: "
                f"{sorted(missing)}"
            )
        if not matched:
            return None, errors
    else:
        if not net_name:
            errors.append(
                f"{role_diagnostic}: neither a net nor pin overrides supplied"
            )
            return None, errors
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
            routed_pin_keys = {
                p.designator.upper()
                for p in component_pads
                if p.net_index != NO_NET
            }
            local_pins = _resolve_local_net_pins(
                proj.compiled_netlist,
                sch_lookup_designator,
                schdoc_name or "",
                net_name,
                routed_pin_keys=routed_pin_keys or None,
                pcb_designator=designator,
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
            return None, errors

        if not resolved_via_local:
            # Bridge-aware fallback: if no pad sits on the literal net but the
            # user has declared (via SERIES directives) that this net is bridged
            # to others, accumulate pads from EVERY equivalent net in the bridge
            # group. This matters when a component (e.g. a multi-output regulator)
            # sources the rail through several parallel SERIES resistors, each
            # with its own pre-resistor net (VOUT0_PRE, VOUT1_PRE, …) that all
            # bridge to the same downstream rail (DAC_SOA_VDD). Previously this
            # picked only the FIRST equivalent net, silently dropping half the
            # source pins.
            seen_pad_designators: set[str] = {p.designator for p in matched}
            bridges_used: list[str] = []
            if bridge_groups is not None:
                group = bridge_groups.get(net_name.upper())
                if group is not None:
                    for alt_net in sorted(group):
                        if alt_net.upper() == net_name.upper():
                            continue
                        alt_indices = _net_indices_by_name(proj, alt_net)
                        if not alt_indices:
                            continue
                        if net_remap:
                            alt_indices = [net_remap.get(ix, ix) for ix in alt_indices]
                        alt_wanted = set(alt_indices)
                        alt_pads = [p for p in component_pads
                                    if p.net_index in alt_wanted
                                    and p.designator not in seen_pad_designators]
                        if alt_pads:
                            matched = matched + alt_pads
                            seen_pad_designators.update(
                                p.designator for p in alt_pads
                            )
                            bridges_used.append(alt_net)

            if bridges_used and warnings is not None:
                bridge_list = ", ".join(repr(b) for b in bridges_used)
                msg = (
                    f"{role_diagnostic}: no pad on {net_name!r}; resolved via "
                    f"SERIES bridge to pin(s) on {bridge_list}"
                )
                if len(bridges_used) >= 2:
                    msg += (
                        ". All listed nets are in the same SERIES bridge "
                        "group — check SERIES directives (e.g. a "
                        "multi-channel part bridging VDD to several outputs)"
                    )
                warnings.append(msg)

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
            return None, errors

    pins = tuple(
        TerminalPin(
            pad_designator=p.designator,
            layer_id=(_tl := _terminal_layer_for_pad(p, enabled_layers)),
            net_index=p.net_index,
            point=p.center,
            pad_polygon=_pad_polygon(p, _tl),
        )
        for p in matched
    )
    return TerminalSpec(
        pins=pins, requested_net=net_name, resolved_via_local=resolved_via_local,
    ), errors


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


def _collect_bridge_groups(
    parameter_sources: list[PdnParameterSource],
    proj: ExtractedProject,
) -> dict[str, frozenset[str]]:
    """Build a mapping from each net name to its electrical-equivalence class
    based on SERIES directives in the schematic.

    Two nets land in the same group if they are bridged by a SERIES
    directive (inductor DCR, 0-ohm jumper, ferrite bead, sense resistor, …).
    Transitive: if A↔B and B↔C are both bridged, A, B, C all belong to one
    group. Net names are upper-cased for case-insensitive comparison.

    Used by :func:`_resolve_terminal` to allow ``PDN_P_NET=+5V`` to resolve to
    a pin on ``5V_SW`` when L1 bridges them — matching the user's "U3 sources
    +5V" mental model without asking them to know the switching-node net name.
    """
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]  # path compression
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for comp in parameter_sources:
        part_role_raw = _ci_get(comp.parameters, ROLE_KEY)
        if part_role_raw is None:
            continue
        part_role = part_role_raw.strip().upper()
        # Only SERIES-role channels bridge nets. On a mixed-role part the
        # part role may be e.g. SINK, so filter by each channel's effective
        # role rather than the part role.
        indices = [
            idx for idx in _discover_channel_indices(comp.parameters, "R")
            if _effective_role(comp.parameters, idx, part_role)
            in _RESISTOR_LIKE_ROLES
        ]
        if not indices:
            continue
        for idx in indices:
            p_net = _ci_get(comp.parameters, _channel_key("P_NET", idx))
            n_net = _ci_get(comp.parameters, _channel_key("N_NET", idx))
            # Replicate auto-infer for 2-pin resistors so the bridge graph stays
            # consistent with the per-directive parser. A multi-channel SERIES
            # part bridges a different net pair in each channel, so union every
            # instance — stopping at the first would strand the other channels.
            if p_net is None and n_net is None and \
                    _ci_get(comp.parameters, _channel_key("P_PINS", idx)) is None and \
                    _ci_get(comp.parameters, _channel_key("N_PINS", idx)) is None:
                if len(indices) == 1:
                    for pcb_idx in _pcb_indices_for_source(comp, proj):
                        inferred = _autoinfer_2pin_nets(proj, pcb_idx)
                        if inferred is not None:
                            union(inferred[0].upper(), inferred[1].upper())
            if p_net and n_net:
                union(p_net.upper(), n_net.upper())

    # Materialise each equivalence class as a frozenset and map every net to it.
    classes: dict[str, set[str]] = {}
    for net in list(parent.keys()):
        root = find(net)
        classes.setdefault(root, set()).add(net)
    return {net: frozenset(classes[find(net)]) for net in parent}


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
    bridge_groups: dict[str, frozenset[str]] | None = None,
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

    p_spec, p_err = _resolve_terminal(
        proj, pcb_index, p_net, p_pins, enabled_layers,
        f"{role_diag} P-terminal",
        bridge_groups=bridge_groups, warnings=result.warnings,
        net_remap=net_remap,
        sch_lookup_designator=sch_lookup_designator,
        schdoc_name=schdoc_name,
    )
    n_spec, n_err = _resolve_terminal(
        proj, pcb_index, n_net, n_pins, enabled_layers,
        f"{role_diag} N-terminal",
        bridge_groups=bridge_groups, warnings=result.warnings,
        net_remap=net_remap,
        sch_lookup_designator=sch_lookup_designator,
        schdoc_name=schdoc_name,
    )
    result.errors.extend(p_err)
    result.errors.extend(n_err)
    if p_spec is None or n_spec is None:
        return None
    return p_spec, n_spec


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
    bridge_groups: dict[str, frozenset[str]] | None = None,
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
    spec, errs = _resolve_terminal(
        proj, pcb_index, net, pins, enabled_layers,
        f"{role_diag} terminal", bridge_groups=bridge_groups,
        warnings=result.warnings, net_remap=net_remap,
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


def _parse_source(comp, proj, enabled_layers, result, bridge_groups=None,
                  net_remap=None, supply_map=None, only_indices=None):
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
        v = _require_value(comp.parameters, _channel_key("V", idx), role_diag, result)
        if v is None:
            continue
        mode = _terminal_mode(comp.parameters, idx, role_diag, result)
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
                    proj, pcb_idx, comp.parameters,
                    _channel_key("NET", idx), _channel_key("PINS", idx),
                    enabled_layers, inst_diag, result,
                    bridge_groups=bridge_groups, net_remap=net_remap,
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
                proj, pcb_idx, comp.parameters,
                _channel_key("P_NET", idx), _channel_key("N_NET", idx),
                _channel_key("P_PINS", idx), _channel_key("N_PINS", idx),
                enabled_layers, inst_diag, result, bridge_groups=bridge_groups,
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


def _parse_sink(comp, proj, enabled_layers, result, bridge_groups=None,
                net_remap=None, supply_map=None, only_indices=None):
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
        i = _require_value(comp.parameters, _channel_key("I", idx), role_diag, result)
        if i is None:
            continue
        mode = _terminal_mode(comp.parameters, idx, role_diag, result)
        if mode is None:
            continue
        min_v = _optional_value(
            comp.parameters, _channel_key("MIN_V", idx), role_diag, result,
        )
        for pcb_idx in pcb_indices:
            pcb_des = proj.pcb_components[pcb_idx].designator
            inst_diag = (
                f"SINK on {_channel_label(pcb_des, idx)}"
                if len(pcb_indices) > 1 else role_diag
            )
            if mode == "single":
                p = _resolve_single_terminal(
                    proj, pcb_idx, comp.parameters,
                    _channel_key("NET", idx), _channel_key("PINS", idx),
                    enabled_layers, inst_diag, result,
                    bridge_groups=bridge_groups, net_remap=net_remap,
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
                proj, pcb_idx, comp.parameters,
                _channel_key("P_NET", idx), _channel_key("N_NET", idx),
                _channel_key("P_PINS", idx), _channel_key("N_PINS", idx),
                enabled_layers, inst_diag, result, bridge_groups=bridge_groups,
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


def _parse_resistance(comp, proj, enabled_layers, result, bridge_groups=None,
                      net_remap=None, supply_map=None, only_indices=None):
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
        r = _require_value(
            comp.parameters, _channel_key("R", idx), role_diag, result,
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
                _ci_get(comp.parameters, _channel_key(k, idx)) is not None
                for k in ("P_NET", "N_NET", "P_PINS", "N_PINS")
            )
            params = dict(comp.parameters)
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
                params[_channel_key("P_NET", idx)] = inferred[0]
                params[_channel_key("N_NET", idx)] = inferred[1]
                result.warnings.append(
                    f"{inst_diag}: auto-inferred "
                    f"{_channel_key('P_NET', idx)}={inferred[0]!r}, "
                    f"{_channel_key('N_NET', idx)}={inferred[1]!r} "
                    f"from 2-pin connectivity"
                )
            pair = _resolve_two_terminal(
                proj, pcb_idx, params,
                _channel_key("P_NET", idx), _channel_key("N_NET", idx),
                _channel_key("P_PINS", idx), _channel_key("N_PINS", idx),
                enabled_layers, inst_diag, result,
                net_remap=net_remap,
                bridge_groups=bridge_groups,
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
) -> dict[str, float]:
    """Map upper-cased supply net names to nominal voltages from SOURCE /
    REGULATOR schematic parameters (before pad resolution)."""
    raw: dict[str, set[float]] = {}

    def _register(net: str | None, voltage: float) -> None:
        if net is None or not str(net).strip():
            return
        raw.setdefault(net.strip().upper(), set()).add(float(voltage))

    for comp in parameter_sources:
        part_role_raw = _ci_get(comp.parameters, ROLE_KEY)
        if part_role_raw is None:
            continue
        part_role = part_role_raw.strip().upper()
        # Both SOURCE and REGULATOR carry PDN<n>_V; switch on each channel's
        # effective role so a SOURCE (or REGULATOR) channel on a mixed-role
        # part still contributes its nominal rail voltage.
        for idx in _discover_channel_indices(comp.parameters, "V"):
            eff = _effective_role(comp.parameters, idx, part_role)
            if eff not in ("SOURCE", "REGULATOR"):
                continue
            v_raw = _ci_get(comp.parameters, _channel_key("V", idx))
            if v_raw is None or not str(v_raw).strip():
                continue
            try:
                v = parse_si_value(v_raw)
            except ValueError:
                continue
            if eff == "SOURCE":
                p_net = _ci_get(comp.parameters, _channel_key("P_NET", idx))
                single_net = _ci_get(comp.parameters, _channel_key("NET", idx))
                _register(p_net or single_net, v)
            else:  # REGULATOR
                out_net = _ci_get(comp.parameters, _channel_key("OUT_P_NET", idx))
                _register(out_net, v)
    out: dict[str, float] = {}
    for net, voltages in raw.items():
        if len(voltages) == 1:
            out[net] = next(iter(voltages))
    return out


def _lookup_inferred_vin(
    in_p_net: str | None,
    supply_map: dict[str, float],
) -> float | None:
    """Nominal Vin from an upstream SOURCE / REGULATOR on ``in_p_net``.

    Uses an exact net-name match only — SERIES bridge equivalence must not
    expand Vin lookup, because sense paths through GND can join unrelated
    rails (e.g. 48 V input and 12 V output) into one class. Pad resolution
    still uses bridge groups via :func:`_resolve_terminal`.
    """
    if in_p_net is None or not str(in_p_net).strip():
        return None
    v = supply_map.get(in_p_net.strip().upper())
    if v is not None and v > 0:
        return v
    return None


def _resolve_regulator_gain(
    params: dict[str, str],
    idx: int | None,
    v_out: float,
    in_p_net: str | None,
    supply_map: dict[str, float],
    role_diag: str,
    result: AnnotationResult,
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

    vin = _lookup_inferred_vin(in_p_net, supply_map)
    if vin is None or vin <= 0:
        in_key = _channel_key("IN_P_NET", idx)
        result.errors.append(
            f"{role_diag}: cannot infer input voltage for SMPS gain — "
            f"no unique upstream SOURCE/REGULATOR voltage found for "
            f"{in_key}={in_p_net!r}"
        )
        return None

    gain = v_out / (vin * eff)
    return gain, reg_type, eff, True


def _parse_regulator(comp, proj, enabled_layers, result, bridge_groups=None,
                     net_remap=None, supply_map=None, only_indices=None):
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
        v = _require_value(
            comp.parameters, _channel_key("V", idx), role_diag, result,
        )
        in_p_net = _ci_get(comp.parameters, _channel_key("IN_P_NET", idx))
        resolved = _resolve_regulator_gain(
            comp.parameters, idx, v, in_p_net,
            supply_map, role_diag, result,
        ) if v is not None else None
        if v is None or resolved is None:
            continue
        g, reg_type, eff, adaptive = resolved
        q_key = _channel_key("QUIESCENT", idx)
        if _ci_get(comp.parameters, q_key) is None:
            quiescent = 0.0
        else:
            iq_raw = _optional_value(comp.parameters, q_key, role_diag, result)
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
                proj, pcb_idx, comp.parameters,
                _channel_key("OUT_P_NET", idx), _channel_key("OUT_N_NET", idx),
                _channel_key("OUT_P_PINS", idx), _channel_key("OUT_N_PINS", idx),
                enabled_layers, f"{inst_diag} OUT", result,
                bridge_groups=bridge_groups,
                net_remap=net_remap,
                sch_lookup_designator=comp.lookup_designator,
                schdoc_name=comp.schdoc_name,
            )
            in_ = _resolve_two_terminal(
                proj, pcb_idx, comp.parameters,
                _channel_key("IN_P_NET", idx), _channel_key("IN_N_NET", idx),
                _channel_key("IN_P_PINS", idx), _channel_key("IN_N_PINS", idx),
                enabled_layers, f"{inst_diag} IN", result,
                bridge_groups=bridge_groups,
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
                               proj: ExtractedProject,
                               bridge_groups: dict[str, frozenset[str]]) -> None:
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
    for group in bridge_groups.values():
        idxs: list[int] = []
        for name in group:
            idxs.extend(_net_indices_by_name(proj, name))
        for other in idxs[1:]:
            union(idxs[0], other)

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
) -> dict[str, list[int | None]]:
    """Group a part's present channels by effective role.

    Each channel's effective role is its ``PDN<n>_ROLE`` override (validated
    against :data:`VALID_ROLES`) or the part-wide ``part_role``. A channel is
    *present* iff the value parameter for its effective role is set
    (``PDN<n>_V`` / ``PDN<n>_I`` / ``PDN<n>_R``). Returns ``{role: [index,
    …]}`` in discovery order (unindexed first, then ascending index) so a
    mixed part — e.g. a DAC that SINKs on its supply rail and SOURCEs on its
    outputs — dispatches each channel to the right per-role parser.

    A part that carries a uniform role (the common case) needs no
    ``PDN<n>_ROLE`` at all: every channel simply inherits ``part_role``, and
    two sinks stay ``{"SINK": [None, 1]}``. Only channels that diverge from
    the default carry an override.

    A channel that declares a role (or a cross-role value parameter) but is
    missing the value parameter its role needs produces an error and is
    dropped.
    """
    candidates: set[int | None] = set()
    for suffix in set(_VALUE_SUFFIX_BY_ROLE.values()):
        candidates.update(_discover_channel_indices(params, suffix))
    # An indexed PDN<n>_ROLE override also marks its channel present, so a
    # channel that declares a role but omits its value param gets a clear
    # error. The *unindexed* PDN_ROLE is the part-wide default (not a
    # channel), so it is excluded here.
    for idx in _discover_channel_indices(params, "ROLE"):
        if idx is not None:
            candidates.add(idx)
    ordered = sorted(candidates, key=lambda x: (x is not None, x or 0))

    grouped: dict[str, list[int | None]] = {}
    for idx in ordered:
        override_raw = _ci_get(params, _channel_key("ROLE", idx))
        if override_raw is not None:
            eff = override_raw.strip().upper()
            if eff not in VALID_ROLES:
                result.errors.append(
                    f"{_channel_label(designator, idx)}: unknown "
                    f"{_channel_key('ROLE', idx)}={override_raw!r} — must be "
                    f"one of {sorted(VALID_ROLES)}"
                )
                continue
        else:
            eff = part_role
        value_key = _channel_key(_VALUE_SUFFIX_BY_ROLE[eff], idx)
        if _ci_get(params, value_key) is None:
            result.errors.append(
                f"{eff} on {_channel_label(designator, idx)}: missing "
                f"{value_key}"
            )
            continue
        grouped.setdefault(eff, []).append(idx)
    return grouped


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
        if stray and _ci_get(comp.parameters, ROLE_KEY) is None:
            result.warnings.append(
                f"{comp.designator} ({comp.schdoc_name}): has {len(stray)} "
                f"PDN_* parameter(s) but no PDN_ROLE — directive ignored"
            )
    for pcb in proj.pcb_components:
        stray = [k for k in pcb.parameters if k.upper().startswith(PARAM_PREFIX)]
        if stray and _ci_get(pcb.parameters, ROLE_KEY) is None:
            lookup = pcb.source_designator or pcb.designator
            result.warnings.append(
                f"{pcb.designator} (PCB, from {lookup}): has {len(stray)} "
                f"PDN_* parameter(s) but no PDN_ROLE — directive ignored"
            )

    parameter_sources = _iter_pdn_parameter_sources(proj)

    # Build bridge groups from RESISTANCE directives so that, e.g., a SOURCE
    # naming PDN_P_NET=+5V can resolve to a U3 pin on 5V_SW when L1 bridges
    # them. Computed once; consulted as a fallback inside _resolve_terminal.
    bridge_groups = _collect_bridge_groups(parameter_sources, proj)
    supply_map = _collect_supply_voltages_by_net(parameter_sources)

    for comp in parameter_sources:
        if comp.designator.upper() in skip_set:
            continue  # Absorbed by net-merge pre-pass — see fypa.altium.loader.
        role_raw = _ci_get(comp.parameters, ROLE_KEY)
        if role_raw is None:
            continue

        role = role_raw.strip().upper()
        if role not in VALID_ROLES:
            result.errors.append(
                f"{comp.designator} ({comp.schdoc_name}): unknown PDN_ROLE={role_raw!r} "
                f"— must be one of {sorted(VALID_ROLES)}"
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
            specs = _PARSER_BY_ROLE[role](comp, proj, enabled_layers, result,
                                          bridge_groups=bridge_groups,
                                          net_remap=net_remap,
                                          supply_map=supply_map)
            result.directives.extend(specs)
        else:
            for chan_role, idxs in channel_roles.items():
                specs = _PARSER_BY_ROLE[chan_role](
                    comp, proj, enabled_layers, result,
                    bridge_groups=bridge_groups, net_remap=net_remap,
                    supply_map=supply_map, only_indices=idxs,
                )
                # Every parser returns a list — empty if the directive failed
                # to resolve, one element per resolved channel otherwise.
                result.directives.extend(specs)

    # Cross-directive checks (mode consistency, open-loop) + return grouping.
    _validate_directive_groups(result, proj, bridge_groups)
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
