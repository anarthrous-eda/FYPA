"""Net normalization helpers for topology metadata."""

from __future__ import annotations

from fypa.topology.constants import GND_NET, IDEAL_RETURN_RAIL
from fypa.topology.net_aliases import is_gnd_alias


def net_to_rail_map(rail_to_members: dict[str, list[str]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for primary, members in rail_to_members.items():
        for net in members:
            out[net] = primary
    return out


def terminal_net(term: dict | None) -> str | None:
    if not term:
        return None
    if term.get("ideal_return"):
        return IDEAL_RETURN_RAIL
    req = term.get("requested_net")
    pins = term.get("pins") or []
    if pins:
        net = pins[0].get("net")
        if net:
            return net
    return req


def is_ideal_return(term: dict | None) -> bool:
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


def port_display_net(term: dict | None, canonical: str) -> str:
    if not term:
        return canonical
    req = term.get("requested_net")
    if req and canonical == GND_NET:
        return req if is_gnd_alias(req) else req
    return req or canonical
