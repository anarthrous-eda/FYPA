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


def terminal_net(term: TerminalDict | None) -> str | None:
    if not term:
        return None
    if term.get("ideal_return"):
        return IDEAL_RETURN_RAIL
    req = term.get("requested_net")
    pins = term.get("pins") or []
    pin_nets = sorted({p.get("net") for p in pins if p.get("net")})
    if len(pin_nets) > 1 or (req and pin_nets and req not in pin_nets):
        key = (req, tuple(pin_nets))
        if key not in _warned_terminal_nets:
            _warned_terminal_nets.add(key)
            if len(pin_nets) > 1:
                log.warning(
                    "Terminal pins disagree on net (%s); using the first pin's "
                    "net %r (requested_net=%r).",
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


def port_display_net(term: TerminalDict | None, canonical: str) -> str:
    if not term:
        return canonical
    req = term.get("requested_net")
    if req and canonical == GND_NET:
        return req
    return req or canonical
