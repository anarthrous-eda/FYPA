"""Net normalization helpers for topology metadata."""

from __future__ import annotations

import logging
import re

from fypa.topology.constants import GND_NET, IDEAL_RETURN_RAIL
from fypa.topology.metadata_schema import TerminalDict
from fypa.topology.net_aliases import is_gnd_alias

log = logging.getLogger(__name__)

# terminal_net() is called many times per terminal during layout; warn about
# each inconsistent terminal once per process rather than per call.
_warned_terminal_nets: set[tuple] = set()

_PASSIVE_CHANNEL_PORT = re.compile(r"^(?:P|N)\d+$")
# Passive channel rows from specs._suffix_for_channel (P1, N2, …) only.
# Regulator rows (IN_P1, OUT_P2, …) are not matched — multi-pin there
# lists every pad net.


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
    if req and physical_net and (
        physical_net == GND_NET or is_gnd_alias(physical_net)
    ):
        return req
    if physical_net and physical_net != "?":
        return physical_net
    return req or physical_net or "?"


def port_display_net(
    term: TerminalDict | None,
    physical_net: str | None = None,
    *,
    role: str = "",
    port_name: str = "",
) -> str:
    """Label text for a topology port — physical PCB net name(s).

    ``physical_net`` is normally :func:`terminal_net` (first pin when pads
    disagree). Rail grouping (:func:`canonical_net`, :func:`_column_net`) is
    used only for column placement and wire grouping — not for labels — so
    rail members keep distinct names (e.g. ``VDD_48V_PORT.1`` vs
    ``VDD_48V_RP``).

    Multi-pin terminals list every distinct pad net (comma-separated) unless
    the port is a channel-split passive row (``N1``, ``P2``, … — ``P``/``N``
    plus a channel index from :func:`specs._suffix_for_channel`) where each
    row drives one gutter net. Regulator channel ports (``IN_P1``, ``OUT_P2``,
    …) are not in that exception and list all pad nets when they disagree.
    GND aliases still show the schematic ``requested_net`` when present.

    Routing (:func:`terminal_net` → ``ResolvedPort.wnet``) always uses the
    first pin net so each connector row still drives one wire/bus.
    """
    pin_nets = terminal_pin_nets(term)
    if len(pin_nets) > 1:
        if role in ("RESISTOR", "SERIES") and _PASSIVE_CHANNEL_PORT.match(port_name):
            return _port_display_single_net(term, physical_net or terminal_net(term))
        return ", ".join(pin_nets)
    return _port_display_single_net(
        term,
        physical_net or (pin_nets[0] if pin_nets else None),
    )
