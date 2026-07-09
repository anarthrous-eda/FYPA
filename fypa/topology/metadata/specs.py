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
    wire_net,
)
from fypa.topology.metadata.tooltips import component_tooltip
from fypa.topology.metadata_schema import (
    DirectiveDict,
    JumpRowDict,
    NodeSpec,
    PortDef,
    RoleSection,
    TerminalDict,
)
from fypa.topology.terminal_roles import is_output_port


# Composite port_defs carry ``section_index * stride + sort_key``. Return-port
# sentinels (RETURN_PORT_SORT_BASE = 900 / RETURN_PORT_GND_SORT_BASE = 950)
# stay ≥ 900 for every section, so ``is_return_port_row`` keeps working on
# composite keys as long as a section's regular rows never reach the stride
# and the section count stays below RETURN_PORT_SORT_BASE / stride (= 9;
# there are only 4 distinct roles). Row layout itself always goes through the
# per-section port_defs, which keep their original sort keys.
_SECTION_SORT_STRIDE = 100

_ROLE_SECTION_ORDER: dict[str, int] = {
    "SOURCE": 0,
    "REGULATOR": 1,
    "SERIES": 2,
    "RESISTOR": 2,
    "SINK": 3,
}


def spec_port_role(spec: NodeSpec, pname: str) -> str:
    """Effective role for one port — per-port on multi-role symbols."""
    port_roles = spec.get("port_roles")
    if port_roles and pname in port_roles:
        return port_roles[pname]
    return spec["role"]


def spec_has_role(spec: NodeSpec, roles: tuple[str, ...]) -> bool:
    """True when the top-level role or any stacked section role is in *roles*."""
    if spec["role"] in roles:
        return True
    sections = spec.get("sections")
    return bool(sections and any(sec["role"] in roles for sec in sections))


def spec_has_series_role(spec: NodeSpec) -> bool:
    """True when the component carries a SERIES / RESISTOR role block."""
    return spec_has_role(spec, ("RESISTOR", "SERIES"))


def spec_series_terms(spec: NodeSpec) -> list[tuple[str, TerminalDict | None]]:
    """(port, terminal) pairs whose effective role is SERIES / RESISTOR.

    On a multi-role composite this excludes the other sections' ports (e.g.
    a SINK channel's supply pins), so the series loop/column heuristics only
    see the nets the bridge itself connects.
    """
    return [
        (pname, term)
        for pname, term in (spec.get("terms") or {}).items()
        if spec_port_role(spec, pname) in ("RESISTOR", "SERIES")
    ]


def _role_section_sort_key(role: str) -> tuple[int, str]:
    return (_ROLE_SECTION_ORDER.get(role, 9), role)


def _section_sort_key(spec: NodeSpec) -> tuple:
    """Order stacked sections by PDN channel index (PDN1, PDN2, …)."""
    indices = [
        int(d["channel_index"])
        for d in spec["directives"]
        if d.get("channel_index") is not None
    ]
    if indices:
        return (0, min(indices))
    return (1, _role_section_sort_key(spec["role"]))


def _merge_specs_for_designator(designator: str, specs: list[NodeSpec]) -> NodeSpec:
    """Stack multiple role blocks into one composite symbol."""
    ordered = sorted(specs, key=_section_sort_key)
    sections: list[RoleSection] = []
    port_defs: list[PortDef] = []
    terms: dict[str, TerminalDict] = {}
    port_directives: dict[str, DirectiveDict] = {}
    port_roles: dict[str, str] = {}
    directives: list[DirectiveDict] = []
    has_error = False
    tooltip_parts: list[str] = []

    used_names: set[str] = set()
    for sec_i, s in enumerate(ordered):
        has_error = has_error or s["has_error"]
        display_role = "SERIES" if s["role"] in ("RESISTOR", "SERIES") else s["role"]
        tooltip_parts.append(f"{display_role} {designator}")
        for line in component_tooltip(s["role"], designator, s["directives"]).splitlines()[1:]:
            if line:
                tooltip_parts.append(line)
        offset = sec_i * _SECTION_SORT_STRIDE
        rename_map: dict[str, str] = {}
        for pname, side, sk in s["port_defs"]:
            out_pname = pname
            d = s["port_directives"].get(pname)
            ch_idx = d.get("channel_index") if d else None
            if ch_idx is not None:
                base = _base_terminal_name(pname)
                out_pname = _suffix_for_channel(int(ch_idx), multi=True, base=base)
            if out_pname in used_names:
                base = _base_terminal_name(out_pname)
                suffix = int(ch_idx) if ch_idx is not None else sec_i + 1
                out_pname = _suffix_for_channel(suffix, multi=True, base=base)
                while out_pname in used_names:
                    suffix += 1
                    out_pname = _suffix_for_channel(suffix, multi=True, base=base)
            rename_map[pname] = out_pname
            used_names.add(out_pname)
        # Section dicts are keyed by the renamed port names throughout, so a
        # section is self-consistent with the composite-level maps.
        sec: RoleSection = {
            "role": s["role"],
            "port_defs": [],
            "terms": {},
            "port_directives": {},
            "directives": list(s["directives"]),
        }
        for pname, side, sk in s["port_defs"]:
            out_pname = rename_map[pname]
            sec["port_defs"].append((out_pname, side, sk))
            sec["terms"][out_pname] = s["terms"][pname]
            port_roles[out_pname] = s["role"]
            port_defs.append((out_pname, side, offset + sk))
            terms[out_pname] = s["terms"][pname]
            if pname in s["port_directives"]:
                sec["port_directives"][out_pname] = s["port_directives"][pname]
                port_directives[out_pname] = s["port_directives"][pname]
        sections.append(sec)
        directives.extend(s["directives"])

    primary = ordered[0]
    return {
        "node_id": designator,
        "label": designator,
        "designator": designator,
        "role": primary["role"],
        "config_label": "",
        "has_error": has_error,
        "terms": terms,
        "port_defs": port_defs,
        "port_directives": port_directives,
        "port_roles": port_roles,
        "sections": sections,
        "tooltip": "\n".join(tooltip_parts),
        "directive": primary["directive"],
        "directives": directives,
    }


def natural_sort_key(label: str) -> tuple:
    parts = re.split(r"(\d+)", label)
    key: list = []
    for p in parts:
        if p.isdigit():
            key.append((0, int(p)))
        else:
            key.append((1, p))
    return tuple(key)


def jump_row_for_directive(directive: DirectiveDict) -> JumpRowDict | None:
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


def _directive_has_error(directive: DirectiveDict, errors: list[str]) -> bool:
    label = str(directive.get("label") or directive.get("designator", ""))
    desig = str(directive.get("designator", ""))
    # Word-boundary match so U1 doesn't inherit an error mentioning U12.
    for err in errors:
        if desig and re.search(rf"\b{re.escape(desig)}\b", err):
            return True
        if label and re.search(rf"\b{re.escape(label)}\b", err):
            return True
    return False


def _channel_sort_key(directive: DirectiveDict) -> tuple:
    idx = directive.get("channel_index")
    if idx is not None:
        return (0, int(idx))
    label = str(directive.get("label") or "")
    m = re.search(r"#(\d+)$", label)
    if m:
        return (0, int(m.group(1)))
    return (1, natural_sort_key(label))


def _channel_number(directive: DirectiveDict, position: int) -> int:
    """Channel index for port naming; falls back to 1-based position.

    Uses ``is not None`` (not truthiness) so a legitimate ``channel_index``
    of ``0`` is honoured, matching :func:`_channel_sort_key`.
    """
    idx = directive.get("channel_index")
    return int(idx) if idx is not None else position + 1


def _suffix_for_channel(index: int, *, multi: bool, base: str) -> str:
    if not multi:
        return base
    return f"{base}{index}"


def _terminal_physical_key(term: TerminalDict | None) -> str:
    """Pad set + wire net — rail merging must not fold distinct schematic ports."""
    if not term:
        return ""
    wnet = wire_net(terminal_net(term)) or ""
    pads = tuple(sorted(p.get("pad", "") for p in term.get("pins") or []))
    if pads:
        return f"{wnet}|{','.join(pads)}"
    return wnet


def _base_terminal_name(pname: str) -> str:
    m = re.match(r"^(IN_P|OUT_P|IN_N|OUT_N|P|N)(\d+)?$", pname)
    return m.group(1) if m else pname


def _terminal_merge_rank(pname: str) -> tuple[int, int]:
    """Lower rank wins when merging ports that share the same physical connection."""
    base = _base_terminal_name(pname)
    priority = {
        "IN_P": 0,
        "P": 1,
        "IN_N": 2,
        "N": 3,
        "OUT_P": 4,
        "OUT_N": 5,
    }
    suffix = 0
    m = re.match(r"^(?:IN_P|OUT_P|IN_N|OUT_N|P|N)(\d+)$", pname)
    if m:
        suffix = int(m.group(1))
    return priority.get(base, 50), suffix


def _collapse_ports_by_physical_key(
    port_defs: list[PortDef],
    terms: dict[str, TerminalDict],
    port_directives: dict[str, DirectiveDict],
    net_to_rail: dict[str, str],
    *,
    role: str = "",
) -> tuple[list[PortDef], dict[str, TerminalDict], dict[str, DirectiveDict]]:
    """One schematic port per distinct pad set (same net + pads → single connector)."""
    passthrough: list[PortDef] = []
    groups: dict[str, list[PortDef]] = defaultdict(list)
    for pd in port_defs:
        pname = pd[0]
        term = terms.get(pname)
        if not term or is_ideal_return(term):
            passthrough.append(pd)
            continue
        key = _terminal_physical_key(term)
        if not key:
            passthrough.append(pd)
            continue
        groups[key].append(pd)

    new_defs: list[PortDef] = list(passthrough)
    new_terms: dict[str, TerminalDict] = {p[0]: terms[p[0]] for p in passthrough}
    new_directives: dict[str, DirectiveDict] = {
        p[0]: port_directives[p[0]] for p in passthrough if p[0] in port_directives
    }

    base_counts: dict[str, int] = defaultdict(int)
    grouped: list[tuple[PortDef, list[PortDef]]] = []
    for _key, pds in groups.items():
        if role in ("RESISTOR", "SERIES"):
            by_base: dict[str, list[PortDef]] = defaultdict(list)
            for pd in pds:
                by_base[_base_terminal_name(pd[0])].append(pd)
            subgroups = list(by_base.values())
        else:
            subgroups = [pds]
        for sg in subgroups:
            winner = min(sg, key=lambda p: (_terminal_merge_rank(p[0]), p[2]))
            base = _base_terminal_name(winner[0])
            base_counts[base] += 1
            grouped.append((winner, sg))

    for winner, pds in grouped:
        base = _base_terminal_name(winner[0])
        pname = base if base_counts[base] == 1 else winner[0]
        side = winner[1]
        sort_key = min(p[2] for p in pds)
        new_defs.append((pname, side, sort_key))
        new_terms[pname] = terms[winner[0]]
        if winner[0] in port_directives:
            new_directives[pname] = port_directives[winner[0]]

    new_defs.sort(key=lambda p: p[2])
    return new_defs, new_terms, new_directives


def _dedupe_return_terms(
    channels: list[DirectiveDict],
    terminal: str,
    net_to_rail: dict[str, str],
) -> list[tuple[str, TerminalDict, int]]:
    seen: dict[str, TerminalDict] = {}
    order: list[tuple[str, TerminalDict, int]] = []
    for d in channels:
        term = (d.get("terminals") or {}).get(terminal)
        if not term or is_ideal_return(term):
            continue
        key = _terminal_physical_key(term)
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
    port_defs: list[PortDef],
    terms: dict[str, TerminalDict],
) -> list[PortDef]:
    return [pd for pd in port_defs if not is_ideal_return(terms.get(pd[0]))]


def _passive_channel_port_defs(
    channels: list[DirectiveDict],
    net_to_rail: dict[str, str],
    *,
    multi: bool,
) -> tuple[list[PortDef], dict[str, TerminalDict], dict[str, DirectiveDict]]:
    port_defs: list[PortDef] = []
    terms: dict[str, TerminalDict] = {}
    port_directives: dict[str, DirectiveDict] = {}

    rows: list[tuple[int, DirectiveDict, TerminalDict | None, TerminalDict | None]] = []
    for i, d in enumerate(channels):
        ch_idx = _channel_number(d, i)
        p_term = (d.get("terminals") or {}).get("P")
        n_term = (d.get("terminals") or {}).get("N")
        rows.append((ch_idx, d, p_term, n_term))

    p_keys = [_terminal_physical_key(p) for _, _, p, _ in rows if p and not is_ideal_return(p)]
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
    channels: list[DirectiveDict],
    errors: list[str],
    net_to_rail: dict[str, str],
) -> NodeSpec:
    channels = sorted(channels, key=_channel_sort_key)
    multi = len(channels) > 1
    port_defs: list[PortDef] = []
    terms: dict[str, TerminalDict] = {}
    port_directives: dict[str, DirectiveDict] = {}
    has_error = any(_directive_has_error(d, errors) for d in channels)

    if role == "SINK":
        for i, d in enumerate(channels):
            ch_idx = _channel_number(d, i)
            pname = _suffix_for_channel(ch_idx, multi=multi, base="P")
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
            ch_idx = _channel_number(d, i)
            for base, side in (("IN_P", "left"), ("OUT_P", "right")):
                pname = _suffix_for_channel(ch_idx, multi=multi, base=base)
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
            ch_idx = _channel_number(d, i)
            for base, side in (("P", "right"), ("N", "left")):
                pname = _suffix_for_channel(ch_idx, multi=multi, base=base)
                term = (d.get("terminals") or {}).get(base)
                if term and not is_ideal_return(term):
                    port_defs.append((pname, side, i))
                    terms[pname] = term
                    if base == "P":
                        port_directives[pname] = d
    elif role in ("RESISTOR", "SERIES"):
        p_defs, p_terms, p_dirs = _passive_channel_port_defs(
            channels,
            net_to_rail,
            multi=multi,
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

    port_defs, terms, port_directives = _collapse_ports_by_physical_key(
        port_defs,
        terms,
        port_directives,
        net_to_rail,
        role=role,
    )

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
) -> list[NodeSpec]:
    groups: dict[tuple[str, str], list[DirectiveDict]] = defaultdict(list)
    for d in directives:
        desig = str(d.get("designator") or d.get("label") or "?")
        role = str(d.get("role", ""))
        groups[(desig, role)].append(d)

    by_designator: dict[str, list[NodeSpec]] = defaultdict(list)
    for desig, role in sorted(groups.keys(), key=lambda k: natural_sort_key(k[0])):
        by_designator[desig].append(
            component_spec_from_directives(
                desig,
                role,
                groups[(desig, role)],
                errors,
                net_to_rail,
            )
        )

    specs: list[NodeSpec] = []
    for desig in sorted(by_designator.keys(), key=natural_sort_key):
        role_specs = by_designator[desig]
        if len(role_specs) == 1:
            specs.append(role_specs[0])
        else:
            specs.append(_merge_specs_for_designator(desig, role_specs))
    return specs


def driven_power_nets(
    node_specs: list[NodeSpec],
    net_to_rail: dict[str, str],
) -> set[str]:
    driven: set[str] = set()
    for s in node_specs:
        for pname, side, _ in s["port_defs"]:
            term = (s["terms"] or {}).get(pname)
            if is_ideal_return(term):
                continue
            if not is_output_port(spec_port_role(s, pname), pname, side):
                continue
            cnet = canonical_net(terminal_net(term), net_to_rail)
            if cnet and cnet != GND_NET:
                driven.add(cnet)
    return driven
