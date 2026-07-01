"""Directive → component spec parsing for topology layout."""

from __future__ import annotations

from collections import defaultdict
import re

from fypa.topology.constants import (
    GND_NET,
    RETURN_PORT_GND_SORT_BASE,
    RETURN_PORT_SORT_BASE,
    ROLE_PORTS,
)
from fypa.topology.metadata.nets import (
    canonical_net,
    is_ideal_return,
    terminal_net,
)
from fypa.topology.metadata.tooltips import component_tooltip
from fypa.topology.metadata_schema import DirectiveDict
from fypa.topology.terminal_roles import is_output_port


def natural_sort_key(label: str) -> tuple:
    parts = re.split(r"(\d+)", label)
    key: list = []
    for p in parts:
        if p.isdigit():
            key.append((0, int(p)))
        else:
            key.append((1, p))
    return tuple(key)


def jump_row_for_directive(directive: dict) -> dict | None:
    label = str(directive.get("label") or directive.get("designator", ""))
    terms = directive.get("terminals") or {}
    for term_name, term in terms.items():
        for pin in term.get("pins") or []:
            if pin.get("x_mm") is not None and pin.get("y_mm") is not None:
                return {
                    "designator": str(directive.get("designator") or label),
                    "role": directive.get("role", ""),
                    "terminal": term_name,
                    "pad": pin.get("pad", ""),
                    "net": pin.get("net", ""),
                    "layer_id": pin.get("layer_id"),
                    "x_mm": pin.get("x_mm"),
                    "y_mm": pin.get("y_mm"),
                }
    return None


def _directive_has_error(directive: dict, errors: list[str]) -> bool:
    label = str(directive.get("label") or directive.get("designator", ""))
    desig = str(directive.get("designator", ""))
    for err in errors:
        if desig and desig in err:
            return True
        if label and label in err:
            return True
    return False


def _channel_sort_key(directive: dict) -> tuple:
    idx = directive.get("channel_index")
    if idx is not None:
        return (0, int(idx))
    label = str(directive.get("label") or "")
    m = re.search(r"#(\d+)$", label)
    if m:
        return (0, int(m.group(1)))
    return (1, natural_sort_key(label))


def _suffix_for_channel(index: int, *, multi: bool, base: str) -> str:
    if not multi:
        return base
    return f"{base}{index}"


def _terminal_physical_key(term: dict | None, net_to_rail: dict[str, str]) -> str:
    if not term:
        return ""
    cnet = canonical_net(terminal_net(term), net_to_rail) or ""
    pads = tuple(sorted(p.get("pad", "") for p in term.get("pins") or []))
    if pads:
        return f"{cnet}|{','.join(pads)}"
    return cnet


def _dedupe_return_terms(
    channels: list[dict],
    terminal: str,
    net_to_rail: dict[str, str],
) -> list[tuple[str, dict, int]]:
    seen: dict[str, dict] = {}
    order: list[tuple[str, dict, int]] = []
    for d in channels:
        term = (d.get("terminals") or {}).get(terminal)
        if not term or is_ideal_return(term):
            continue
        key = _terminal_physical_key(term, net_to_rail)
        if key in seen:
            continue
        seen[key] = term
        cnet = canonical_net(terminal_net(term), net_to_rail) or ""
        sort = (
            RETURN_PORT_GND_SORT_BASE + len(order)
            if cnet == GND_NET
            else RETURN_PORT_SORT_BASE + len(order)
        )
        suffix = "" if len(seen) == 1 and len(channels) == 1 else str(len(seen))
        pname = terminal if not suffix else f"{terminal}{suffix}"
        order.append((pname, term, sort))
    if len(order) == 1:
        order[0] = (terminal, order[0][1], order[0][2])
    return order


def _visible_port_defs(
    port_defs: list[tuple[str, str, int]],
    terms: dict,
) -> list[tuple[str, str, int]]:
    return [
        pd for pd in port_defs
        if not is_ideal_return(terms.get(pd[0]))
    ]


def _passive_channel_port_defs(
    channels: list[dict],
    net_to_rail: dict[str, str],
    *,
    multi: bool,
) -> tuple[list[tuple[str, str, int]], dict[str, dict], dict[str, dict]]:
    port_defs: list[tuple[str, str, int]] = []
    terms: dict[str, dict] = {}
    port_directives: dict[str, dict] = {}

    rows: list[tuple[int, dict, dict | None, dict | None]] = []
    for i, d in enumerate(channels):
        ch_idx = int(d.get("channel_index") or (i + 1))
        p_term = (d.get("terminals") or {}).get("P")
        n_term = (d.get("terminals") or {}).get("N")
        rows.append((ch_idx, d, p_term, n_term))

    p_keys = [
        _terminal_physical_key(p, net_to_rail)
        for _, _, p, _ in rows
        if p and not is_ideal_return(p)
    ]
    merge_p = len(p_keys) > 1 and len({k for k in p_keys if k}) == 1

    for row_i, (ch_idx, d, p_term, n_term) in enumerate(rows):
        if p_term and not is_ideal_return(p_term):
            if not merge_p or row_i == 0:
                pname = "P" if merge_p else _suffix_for_channel(ch_idx, multi=multi, base="P")
                port_defs.append((pname, "left", row_i))
                terms[pname] = p_term
                port_directives[pname] = d
        if n_term and not is_ideal_return(n_term):
            pname = _suffix_for_channel(ch_idx, multi=multi, base="N")
            port_defs.append((pname, "right", row_i))
            terms[pname] = n_term
            port_directives[pname] = d

    return port_defs, terms, port_directives


def component_spec_from_directives(
    designator: str,
    role: str,
    channels: list[dict],
    errors: list[str],
    net_to_rail: dict[str, str],
) -> dict:
    channels = sorted(channels, key=_channel_sort_key)
    multi = len(channels) > 1
    port_defs: list[tuple[str, str, int]] = []
    terms: dict[str, dict] = {}
    port_directives: dict[str, dict] = {}
    has_error = any(_directive_has_error(d, errors) for d in channels)

    if role == "SINK":
        for i, d in enumerate(channels):
            ch_idx = d.get("channel_index") or (i + 1)
            pname = _suffix_for_channel(int(ch_idx), multi=multi, base="P")
            p_term = (d.get("terminals") or {}).get("P")
            if p_term and not is_ideal_return(p_term):
                port_defs.append((pname, "left", i))
                terms[pname] = p_term
                port_directives[pname] = d
        for pname, n_term, sort_key in _dedupe_return_terms(channels, "N", net_to_rail):
            port_defs.append((pname, "left", sort_key))
            terms[pname] = n_term
    elif role == "REGULATOR":
        for i, d in enumerate(channels):
            ch_idx = d.get("channel_index") or (i + 1)
            for base, side in (("IN_P", "left"), ("OUT_P", "right")):
                pname = _suffix_for_channel(int(ch_idx), multi=multi, base=base)
                term = (d.get("terminals") or {}).get(base)
                if term and not is_ideal_return(term):
                    port_defs.append((pname, side, i))
                    terms[pname] = term
                    port_directives[pname] = d
        for pname, term, sort_key in _dedupe_return_terms(channels, "IN_N", net_to_rail):
            port_defs.append((pname, "left", sort_key))
            terms[pname] = term
        for pname, term, sort_key in _dedupe_return_terms(channels, "OUT_N", net_to_rail):
            port_defs.append((pname, "right", sort_key))
            terms[pname] = term
    elif role == "SOURCE":
        for i, d in enumerate(channels):
            ch_idx = d.get("channel_index") or (i + 1)
            for base, side in (("P", "right"), ("N", "left")):
                pname = _suffix_for_channel(int(ch_idx), multi=multi, base=base)
                term = (d.get("terminals") or {}).get(base)
                if term and not is_ideal_return(term):
                    port_defs.append((pname, side, i))
                    terms[pname] = term
                    if base == "P":
                        port_directives[pname] = d
    elif role in ("RESISTOR", "SERIES"):
        p_defs, p_terms, p_dirs = _passive_channel_port_defs(
            channels, net_to_rail, multi=multi,
        )
        port_defs.extend(p_defs)
        terms.update(p_terms)
        port_directives.update(p_dirs)
    else:
        d = channels[0]
        port_defs = list(ROLE_PORTS.get(role, [("P", "left", 0), ("N", "right", 1)]))
        terms = dict(d.get("terminals") or {})
        port_defs = _visible_port_defs(port_defs, terms)
        for pname, _, _ in port_defs:
            port_directives[pname] = d

    if not port_defs:
        d = channels[0]
        port_defs = list(ROLE_PORTS.get(role, [("P", "left", 0), ("N", "right", 1)]))
        terms = dict(d.get("terminals") or {})
        port_defs = _visible_port_defs(port_defs, terms)
        for pname, _, _ in port_defs:
            port_directives[pname] = d

    return {
        "node_id": designator,
        "label": designator,
        "designator": designator,
        "role": role,
        "config_label": "",
        "has_error": has_error,
        "terms": terms,
        "port_defs": port_defs,
        "port_directives": port_directives,
        "tooltip": component_tooltip(role, designator, channels),
        "directive": channels[0],
        "directives": channels,
    }


def directives_to_component_specs(
    directives: list[DirectiveDict],
    errors: list[str],
    net_to_rail: dict[str, str],
) -> list[dict]:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for d in directives:
        desig = str(d.get("designator") or d.get("label") or "?")
        role = str(d.get("role", ""))
        groups[(desig, role)].append(d)

    specs: list[dict] = []
    for (desig, role) in sorted(groups.keys(), key=lambda k: natural_sort_key(k[0])):
        specs.append(component_spec_from_directives(
            desig, role, groups[(desig, role)], errors, net_to_rail,
        ))
    return specs


def driven_power_nets(
    node_specs: list[dict],
    net_to_rail: dict[str, str],
) -> set[str]:
    driven: set[str] = set()
    for s in node_specs:
        for pname, side, _ in s["port_defs"]:
            term = (s["terms"] or {}).get(pname)
            if is_ideal_return(term):
                continue
            if not is_output_port(s["role"], pname, side):
                continue
            cnet = canonical_net(terminal_net(term), net_to_rail)
            if cnet and cnet != GND_NET:
                driven.add(cnet)
    return driven
