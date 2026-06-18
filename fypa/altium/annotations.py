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
REGULATOR      PDN_V, PDN_GAIN                 PDN_OUT_P_NET, PDN_OUT_N_NET,
                                               PDN_IN_P_NET,  PDN_IN_N_NET    (overrides: *_PINS)
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
least one SOURCE *and* one SINK on the same net (a group with only one is an
"open loop" error). Every SOURCE and SINK that shares a net must use the same
mode — a group cannot mix single-net and two-terminal directives.

Multi-channel SOURCE / SINK
---------------------------
``SOURCE`` and ``SINK`` roles support multiple independent channels on a
single part — useful for an IC with several supply pins, each on its own
rail. Channels are addressed by appending an integer to ``PDN`` in the
parameter prefix: the legacy unindexed form (``PDN_V`` / ``PDN_P_NET`` / …)
and any number of indexed channels (``PDN1_V`` / ``PDN1_P_NET`` / …,
``PDN2_V`` / …, …) coexist as independent channels. Each channel produces
its own directive spec with the part-wide ``PDN_ROLE``.

Example — a SINK with three independent supply rails::

    PDN_ROLE   = SINK
    PDN_I      = 500mA     PDN_P_NET  = +3V3   PDN_N_NET  = GND
    PDN1_I     = 250mA     PDN1_P_NET = +1V8   PDN1_N_NET = GND
    PDN2_I     = 50mA      PDN2_P_NET = +5V    PDN2_N_NET = GND

Indices are sparse (any positive integer; gaps allowed); a channel is
"present" iff its value param (``PDNn_V`` for SOURCE / REGULATOR,
``PDNn_I`` for SINK, ``PDNn_R`` for SERIES) is set. All four roles support
the same indexed-prefix scheme; each channel produces its own directive
spec with the part-wide ``PDN_ROLE``.

Values support SI prefixes and units (``500mA``, ``3V3``, ``1.5k``, ``0.1``).

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


# --- SI value parsing ---------------------------------------------------------

_SI_PREFIXES: dict[str, float] = {
    "f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6,
    "m": 1e-3, "":  1.0,    "k": 1e3,  "K": 1e3,
    "M": 1e6,  "G": 1e9,    "T": 1e12,
}
# Units we tolerate trailing (the unit suffix is informational; we don't enforce
# unit/role consistency — the user is responsible for putting volts on a SOURCE).
_TRAILING_UNITS: tuple[str, ...] = ("V", "A", "Ohm", "OHM", "ohm", "Ω", "Hz", "S", "F", "H", "%")

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


def parse_si_value(s: str) -> float:
    """Parse a value string with SI prefix and optional unit.

    Accepts:
        ``"500mA"``, ``"1.5k"``, ``"0.1"``, ``"3V3"`` (engineering form → 3.3),
        ``"1MΩ"``, ``"-2.7"``.

    Raises :class:`ValueError` on unparseable input.
    """
    if s is None:
        raise ValueError("empty value")
    text = str(s).strip()
    if not text:
        raise ValueError("empty value")

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
        # rest may start with an SI prefix letter, then optionally a unit.
        if rest:
            first = rest[0]
            if first in _SI_PREFIXES and (len(rest) == 1 or rest[1:] in _TRAILING_UNITS
                                          or rest[1:].lower() in {u.lower() for u in _TRAILING_UNITS}):
                magnitude *= _SI_PREFIXES[first]
            # else: rest is a bare unit (V, A, Ohm, %) — magnitude unchanged.

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


def _schdoc_for_source_designator(proj: ExtractedProject, designator: str) -> str:
    target = designator.upper()
    for comp in proj.sch_components:
        if comp.designator.upper() == target:
            return comp.schdoc_name
    return "(pcb)"


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

    for idx, pcb in enumerate(proj.pcb_components):
        if _ci_get(pcb.parameters, ROLE_KEY) is None:
            continue
        lookup_des = pcb.source_designator or pcb.designator
        if lookup_des.upper() in sch_with_role:
            continue
        sources.append(PdnParameterSource(
            designator=pcb.designator,
            schdoc_name=_schdoc_for_source_designator(proj, lookup_des),
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
    if not source_sheets:
        return True
    if schdoc_name == "(pcb)":
        return True
    target = Path(schdoc_name).name.lower()
    return any(Path(sheet).name.lower() == target for sheet in source_sheets)


def _resolve_local_net_pins(
    netlist,
    sch_designator: str,
    schdoc_name: str,
    local_net_name: str,
) -> list[str]:
    """Return pin designators on ``sch_designator`` for a local sheet net name."""
    if netlist is None:
        return []
    target_des = sch_designator.upper()
    pins: list[str] = []
    seen: set[str] = set()
    for net in netlist.nets:
        names = [net.name, *getattr(net, "aliases", ())]
        if not any(n.upper() == local_net_name.upper() for n in names if n):
            continue
        if not _sheet_name_matches(schdoc_name, list(getattr(net, "source_sheets", ()) or ())):
            continue
        for term in net.terminals:
            if term.designator.upper() != target_des:
                continue
            pin = str(term.pin)
            key = pin.upper()
            if key not in seen:
                seen.add(key)
                pins.append(pin)
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
    component_pads = [p for p in proj.pads if p.component_index == pcb_index]
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

        if not matched and sch_lookup_designator and schdoc_name:
            local_pins = _resolve_local_net_pins(
                proj.compiled_netlist,
                sch_lookup_designator,
                schdoc_name,
                net_name,
            )
            if local_pins:
                wanted_pins = {pin.upper() for pin in local_pins}
                matched = [
                    p for p in component_pads
                    if p.designator.upper() in wanted_pins
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
                warnings.append(
                    f"{role_diagnostic}: no pad on {net_name!r}; resolved via "
                    f"SERIES bridge to pin(s) on {bridge_list}"
                )

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


def _net_indices_by_name(proj: ExtractedProject, name: str) -> list[int]:
    """Every net index whose name matches ``name`` (case-insensitive).

    A multi-channel PCB stores one net per channel, and Altium does not
    channel-qualify the names in its Nets6 stream — so a per-channel net name
    (e.g. ``NetC144_PWR_SW13_2``) is shared by all of those channels' distinct
    nets. Connectivity stays unambiguous (pads carry net *indices*); only the
    name is ambiguous, so callers must consider every index for a name and
    let the specific component's own pads pick out its channel.
    """
    target = name.upper()
    return [i for i, n in enumerate(proj.nets) if n.name.upper() == target]


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
        role_raw = _ci_get(comp.parameters, ROLE_KEY)
        if role_raw is None or role_raw.strip().upper() not in _RESISTOR_LIKE_ROLES:
            continue
        indices = _discover_channel_indices(comp.parameters, "R")
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
    pads = [p for p in proj.pads if p.component_index == pcb_index]
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
    pads = [p for p in proj.pads if p.component_index == pcb_index]
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


@dataclass(frozen=True)
class ResistorSpec(_BaseSpec):
    resistance: float
    p: TerminalSpec
    n: TerminalSpec
    channel_index: int | None = None  # None = legacy unindexed; int = PDN<n>_*


@dataclass(frozen=True)
class RegulatorSpec(_BaseSpec):
    voltage: float
    gain: float
    out_p: TerminalSpec
    out_n: TerminalSpec
    in_p: TerminalSpec
    in_n: TerminalSpec
    channel_index: int | None = None  # None = legacy unindexed; int = PDN<n>_*


DirectiveSpec = SourceSpec | SinkSpec | ResistorSpec | RegulatorSpec


@dataclass
class AnnotationResult:
    directives: list[DirectiveSpec] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

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
        result.errors.append(f"{role_diag}: {key}={raw!r} — {e}")
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
        result.errors.append(f"{role_diag}: {key}={raw!r} — {e}")
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
    has_single = (_ci_get(params, net_key) is not None
                  or _ci_get(params, pins_key) is not None)
    has_two = any(
        _ci_get(params, _channel_key(k, idx)) is not None
        for k in ("P_NET", "N_NET", "P_PINS", "N_PINS")
    )
    if has_single and has_two:
        result.errors.append(
            f"{role_diag}: {net_key} cannot be combined with "
            f"{p_net_key}/{n_net_key} — use {net_key} alone for a single-net "
            f"check, or {p_net_key} + {n_net_key} for a two-terminal check"
        )
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


def _has_single_net_params(params: dict[str, str]) -> bool:
    """True if any ``PDN[n]_NET`` / ``PDN[n]_PINS`` parameter is present.

    Used to reject ``PDN_NET`` on SERIES / REGULATOR — single-net mode is
    SOURCE/SINK only."""
    for k, v in params.items():
        m = _INDEXED_KEY_RE.match(k.strip())
        if m and m.group(2).upper() in ("NET", "PINS") \
                and v is not None and str(v).strip():
            return True
    return False


def _parse_source(comp, proj, enabled_layers, result, bridge_groups=None,
                  net_remap=None):
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
                net_remap=None):
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
                      net_remap=None):
    role_raw = (_ci_get(comp.parameters, ROLE_KEY) or "SERIES").strip().upper()
    role_diag_base = f"{role_raw} on {comp.designator}"
    if _has_single_net_params(comp.parameters):
        result.errors.append(
            f"{role_diag_base}: PDN_NET is only valid on SOURCE/SINK — a "
            f"SERIES directive bridges two nets, use PDN_P_NET and PDN_N_NET"
        )
        return []
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


def _parse_regulator(comp, proj, enabled_layers, result, bridge_groups=None,
                     net_remap=None):
    role_diag_base = f"REGULATOR on {comp.designator}"
    if _has_single_net_params(comp.parameters):
        result.errors.append(
            f"{role_diag_base}: PDN_NET is only valid on SOURCE/SINK — a "
            f"REGULATOR has four terminals, use PDN_OUT_P_NET / PDN_OUT_N_NET "
            f"/ PDN_IN_P_NET / PDN_IN_N_NET"
        )
        return []
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

    specs: list[RegulatorSpec] = []
    for idx in indices:
        role_diag = f"REGULATOR on {_channel_label(comp.designator, idx)}"
        v = _require_value(
            comp.parameters, _channel_key("V", idx), role_diag, result,
        )
        g = _require_value(
            comp.parameters, _channel_key("GAIN", idx), role_diag, result,
        )
        if v is None or g is None:
            continue
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
      path; and
    * a single-net group needs at least one SOURCE *and* one SINK, else no
      current flows (an open loop).

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
        if not any(isinstance(d, SourceSpec) for d in single):
            result.errors.append(
                f"single-net group ({labels}): has a SINK but no SOURCE — no "
                f"current can flow (open loop). Add a single-net SOURCE on "
                f"the same net."
            )
        if not any(isinstance(d, SinkSpec) for d in single):
            result.errors.append(
                f"single-net group ({labels}): has a SOURCE but no SINK — no "
                f"current can flow (open loop). Add a single-net SINK on the "
                f"same net."
            )
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

        specs = _PARSER_BY_ROLE[role](comp, proj, enabled_layers, result,
                                      bridge_groups=bridge_groups,
                                      net_remap=net_remap)
        # Every parser now returns a list — empty if the directive failed
        # to resolve, single-element for single-channel roles, multi-element
        # for multi-channel SOURCE/SINK.
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
        return head + f"  V={d.voltage:g} V, gain={d.gain:g}\n" + \
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
