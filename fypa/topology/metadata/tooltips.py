"""Tooltip text for topology nodes and ports."""

from __future__ import annotations

from fypa.topology.constants import ROLE_PORTS
from fypa.topology.metadata.nets import is_ideal_return
from fypa.topology.util import (
    format_current_a,
    format_directive_value,
    fmt_compact,
    reformat_legacy_value_str,
)


def directive_tooltip_lines(directive: dict) -> list[str]:
    lines: list[str] = []
    role = str(directive.get("role", ""))
    fv = format_directive_value(directive)
    if fv:
        lines.append(fv)
    if role == "REGULATOR":
        if directive.get("gain") is not None:
            lines.append(f"gain {fmt_compact(float(directive['gain']))}")
        iq = directive.get("quiescent_current")
        if iq is not None and float(iq) > 0:
            lines.append(f"Iq {format_current_a(float(iq))}")
        rtype = directive.get("regulator_type")
        if rtype:
            lines.append(str(rtype))
        eff = directive.get("efficiency")
        if eff is not None:
            lines.append(f"\u03b7 {fmt_compact(float(eff))}")
    terms = directive.get("terminals") or {}
    port_defs = ROLE_PORTS.get(role, [("P", "left", 0), ("N", "right", 1)])
    n_names = {p for p, _, _ in port_defs if p in ("N", "IN_N")}
    if any(is_ideal_return(terms.get(p)) for p in n_names):
        lines.append("single-net (ideal return)")
    if not lines:
        vs = str(directive.get("value_str", "")).strip()
        if vs:
            lines.append(reformat_legacy_value_str(vs))
    return lines


def component_tooltip(
    role: str,
    designator: str,
    directives: list[dict],
) -> str:
    display_role = "SERIES" if role in ("RESISTOR", "SERIES") else role
    lines = [f"{display_role} {designator}"]
    for d in directives:
        ch_label = str(d.get("label") or "")
        if len(directives) > 1 and ch_label:
            lines.append(ch_label)
        lines.extend(directive_tooltip_lines(d))
    return "\n".join(lines)


def port_tooltip(net_label: str, directive: dict | None, terminal: str) -> str:
    parts = [net_label]
    if directive is None:
        return net_label
    fv = format_directive_value(directive)
    if fv:
        parts.append(fv)
    ch = directive.get("channel_index")
    if ch is not None:
        parts.insert(0, f"ch {ch} · {terminal}")
    return "\n".join(parts)
