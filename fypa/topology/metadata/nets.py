"""Net normalization helpers for topology metadata."""

from __future__ import annotations

import logging

from fypa.topology.constants import GND_NET, IDEAL_RETURN_RAIL
from fypa.topology.metadata_schema import TerminalDict
from fypa.topology.net_aliases import is_gnd_alias

log = logging.getLogger(__name__)

# terminal_net() is called many times per terminal during layout; warn about
# each inconsistent terminal once per process rather than per call.
_warned_terminal_nets: set[tuple] = set()


def net_to_rail_map(rail_to_members: dict[str, list[str]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for primary, members in rail_to_members.items():
        for net in members:
            out[net] = primary
    return out


def terminal_pin_nets(term: TerminalDict | None) -> list[str]:
    """Distinct PCB net names on a terminal's pads, sorted for display."""
    if not term:
        return []
    pins = term.get("pins") or []
    return sorted({p.get("net") for p in pins if p.get("net")})


def terminal_net(term: TerminalDict | None) -> str | None:
    if not term:
        return None
    if term.get("ideal_return"):
        return IDEAL_RETURN_RAIL
    req = term.get("requested_net")
    pins = term.get("pins") or []
    pin_nets = terminal_pin_nets(term)
    if len(pin_nets) > 1 or (req and pin_nets and req not in pin_nets):
        key = (req, tuple(pin_nets))
        if key not in _warned_terminal_nets:
            _warned_terminal_nets.add(key)
            if len(pin_nets) > 1:
                log.warning(
                    "Terminal pins disagree on net (%s); routing uses the first "
                    "pin's net %r (requested_net=%r).",
                    ", ".join(pin_nets), pins[0].get("net"), req,
                )
            else:
                log.warning(
                    "Terminal pins resolved to net %r, contradicting "
                    "requested_net=%r; using the pin net.",
                    pins[0].get("net"), req,
                )
    if pins:
        net = pins[0].get("net")
        if net:
            return net
    return req


def is_ideal_return(term: TerminalDict | None) -> bool:
    return bool(term and term.get("ideal_return"))


def wire_net(raw: str | None) -> str | None:
    """Net identity for schematic wires (GND collapsed; ideal returns omitted)."""
    if not raw:
        return None
    if raw == IDEAL_RETURN_RAIL:
        return None
    if raw == GND_NET:
        return GND_NET
    if is_gnd_alias(raw):
        return GND_NET
    return raw


def canonical_net(
    net: str | None,
    net_to_rail: dict[str, str],
) -> str | None:
    if not net:
        return None
    if net == IDEAL_RETURN_RAIL:
        return None
    if net == GND_NET:
        return GND_NET
    if is_gnd_alias(net):
        return GND_NET
    return net_to_rail.get(net, net)


def _port_display_single_net(
    term: TerminalDict | None,
    physical_net: str | None,
) -> str:
    if not term:
        return physical_net or "?"
    req = term.get("requested_net")
    if req and physical_net and is_gnd_alias(physical_net):
        return req
    return physical_net or req or "?"


def port_display_net(
    term: TerminalDict | None,
    physical_net: str | None = None,
    *,
    channel_row: bool = False,
) -> str:
    """Label text for a topology port — physical PCB net name(s).

    ``physical_net`` is normally :func:`terminal_net` (first pin when pads
    disagree). Rail grouping (:func:`canonical_net`, :func:`_column_net`) is
    used only for column placement and wire grouping — not for labels — so
    rail members keep distinct names (e.g. ``VDD_48V_PORT.1`` vs
    ``VDD_48V_RP``).

    A terminal whose pads span several nets normally lists them all,
    comma-separated: the port is the only place that tie is visible. When the
    port is a channel row (``channel_row``, i.e. sibling rows exist for the
    same terminal, so the pad set spans the whole part) the label shows just
    that row's net, matching the one wire the row drives.

    A GND-alias net shows the schematic ``requested_net`` instead, so the
    ground symbol keeps the designer's name (``AGND``, ``DGND``, …). A
    multi-net terminal that merely *includes* a ground pad still lists every
    net, since no single requested name describes it.

    Routing (:func:`terminal_net` → ``ResolvedPort.wnet``) always uses the
    first pin net so each connector row still drives one wire/bus.
    """
    pin_nets = terminal_pin_nets(term)
    if len(pin_nets) > 1 and not channel_row:
        return ", ".join(pin_nets)
    # terminal_net() only when pads exist — an ideal return has none and would
    # yield the IDEAL_RETURN_RAIL sentinel rather than a name.
    return _port_display_single_net(
        term,
        physical_net or (terminal_net(term) if pin_nets else None),
    )
