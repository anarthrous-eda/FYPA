"""SVG rendering for the topology schematic."""

from __future__ import annotations

from fypa.topology.constants import (
    GND_NET,
    GND_SYMBOL_OFFSET,
    GND_WIRE_COLOR,
    HEADER_H,
    JUNCTION_R,
    NON_GND_WIRE_COLOR,
    PORT_R,
    ROLE_COLORS,
    SINGLE_NET_ROLE_COLORS,
    WIRE_EPS,
)
from fypa.topology.geometry import (
    compute_schematic_geometry,
    vertical_bridge_path,
)


def _wire_stroke(net: str) -> str:
    """All wires are power nets: ground draws green, non-ground rails draw red."""
    return GND_WIRE_COLOR if net == GND_NET else NON_GND_WIRE_COLOR


def _segment_net_at(segments, x: float, y: float) -> str:
    """Net of a wire segment incident to ``(x, y)`` (for colouring junction dots)."""
    for s in segments:
        if s.orient == "H" and abs(s.y1 - y) < WIRE_EPS:
            if min(s.x1, s.x2) - WIRE_EPS <= x <= max(s.x1, s.x2) + WIRE_EPS:
                return s.net
        elif s.orient == "V" and abs(s.x1 - x) < WIRE_EPS:
            if min(s.y1, s.y2) - WIRE_EPS <= y <= max(s.y1, s.y2) + WIRE_EPS:
                return s.net
    return ""


from fypa.topology.types import TopologyModel, TopologyNode, TopologyWire
from fypa.topology.util import esc, truncate_label


def _draw_wire(
    parts: list[str],
    wire: TopologyWire,
) -> None:
    dash = ' stroke-dasharray="5 4"' if wire.dashed else ""
    parts.append(
        f'<path d="{wire.path_d}" fill="none" stroke="{esc(_wire_stroke(wire.net))}"'
        f' stroke-width="2" stroke-linecap="round" stroke-linejoin="round"'
        f"{dash}/>"
    )


def _draw_wires_schematic(
    parts: list[str],
    wires: list[TopologyWire],
    *,
    bg: str,
    gnd_symbol_x: float | None = None,
    gnd_bus_y: float | None = None,
) -> None:
    """Draw wires with schematic junction dots and vertical-over-horizontal bridges."""
    dashed = [w for w in wires if w.dashed]
    geo = compute_schematic_geometry(
        wires,
        gnd_symbol_x=gnd_symbol_x,
        gnd_bus_y=gnd_bus_y,
    )

    sw = 2.0
    for h in geo.horizontals:
        stroke = esc(_wire_stroke(h.net))
        parts.append(
            f'<line x1="{h.x1:.1f}" y1="{h.y1:.1f}" x2="{h.x2:.1f}" y2="{h.y2:.1f}"'
            f' stroke="{stroke}" stroke-width="{sw}" stroke-linecap="round"/>'
        )

    for vi, v in enumerate(geo.verticals):
        crosses = sorted(set(geo.vert_crossings.get(vi, [])))
        d = vertical_bridge_path(v.x1, v.y1, v.y2, crosses)
        stroke = esc(_wire_stroke(v.net))
        parts.append(
            f'<path d="{d}" fill="none" stroke="{stroke}"'
            f' stroke-width="{sw}" stroke-linecap="round" stroke-linejoin="round"/>'
        )

    for jx, jy in geo.junctions:
        stroke = esc(_wire_stroke(_segment_net_at(geo.segments, jx, jy)))
        parts.append(f'<circle cx="{jx:.1f}" cy="{jy:.1f}" r="{JUNCTION_R:.1f}" fill="{stroke}"/>')

    for w in dashed:
        _draw_wire(parts, w)


def _net_highlight_fragment(
    model: TopologyModel,
    net: str,
    *,
    stroke: str,
    stroke_width: float = 5.0,
) -> str:
    """SVG shapes highlighting every segment of ``net`` (schematic geometry)."""
    parts: list[str] = []
    stroke_esc = esc(stroke)
    sw = stroke_width
    op = ' opacity="0.88"'
    geo = compute_schematic_geometry(
        model.wires,
        gnd_symbol_x=model.gnd_symbol_x,
        gnd_bus_y=model.gnd_bus_y,
    )

    for h in geo.horizontals:
        if h.net != net:
            continue
        parts.append(
            f'<line x1="{h.x1:.1f}" y1="{h.y1:.1f}" x2="{h.x2:.1f}" y2="{h.y2:.1f}"'
            f' stroke="{stroke_esc}" stroke-width="{sw}" stroke-linecap="round"{op}/>'
        )

    for vi, v in enumerate(geo.verticals):
        if v.net != net:
            continue
        crosses = sorted(set(geo.vert_crossings.get(vi, [])))
        d = vertical_bridge_path(v.x1, v.y1, v.y2, crosses)
        parts.append(
            f'<path d="{d}" fill="none" stroke="{stroke_esc}" stroke-width="{sw}"'
            f' stroke-linecap="round" stroke-linejoin="round"{op}/>'
        )

    for wire in model.wires:
        if wire.net != net or not wire.dashed:
            continue
        dash = ' stroke-dasharray="5 4"'
        parts.append(
            f'<path d="{wire.path_d}" fill="none" stroke="{stroke_esc}"'
            f' stroke-width="{sw}" stroke-linecap="round" stroke-linejoin="round"'
            f"{dash}{op}/>"
        )

    return "".join(parts)


def render_net_highlight_svg(
    model: TopologyModel,
    net: str,
    *,
    theme: dict[str, str] | None = None,
    width: float | None = None,
    color: str | None = None,
) -> str:
    """Transparent SVG overlay highlighting one net (for hover feedback)."""
    theme = theme or {}
    stroke = color or theme.get("accent", "#b8d4ff")
    w = width if width is not None else model.width
    h = model.height
    body = _net_highlight_fragment(model, net, stroke=stroke)
    if not body:
        return ""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.0f}" height="{h:.0f}"'
        f' viewBox="0 0 {w:.0f} {h:.0f}">{body}</svg>'
    )


def _draw_wire_label(
    parts: list[str],
    wire: TopologyWire,
    *,
    fg: str,
    bg: str,
) -> None:
    if not wire.label:
        return
    x, y = wire.label_x, wire.label_y
    if x == 0.0 and y == 0.0:
        return
    text = wire.label
    tw = max(len(text) * 5.4, 18.0)
    th = 12.0
    if wire.label_vertical:
        parts.append(f'<g transform="translate({x:.1f},{y:.1f})">')
        parts.append(
            f'<rect x="{-th / 2 - 2:.1f}" y="{-tw / 2:.1f}"'
            f' width="{th + 4:.1f}" height="{tw:.1f}" fill="{esc(bg)}"'
            f' fill-opacity="0.5" stroke="none" rx="2"/>'
        )
        parts.append(
            f'<text transform="rotate(-90)" text-anchor="middle"'
            f' dominant-baseline="middle" fill="{esc(fg)}"'
            f' font-family="Segoe UI,sans-serif" font-size="8">'
            f"{esc(text)}</text>"
        )
        parts.append("</g>")
        return
    anchor = wire.label_text_anchor or "middle"
    if anchor == "start":
        rx = x - 2.0
    elif anchor == "end":
        rx = x - tw - 2.0
    else:
        rx = x - tw / 2 - 2.0
    ry = y - th / 2
    parts.append(
        f'<rect x="{rx:.1f}" y="{ry:.1f}"'
        f' width="{tw + 4:.1f}" height="{th:.1f}" fill="{esc(bg)}"'
        f' fill-opacity="0.5" stroke="none" rx="2"/>'
    )
    parts.append(
        f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{esc(anchor)}"'
        f' dominant-baseline="middle" fill="{esc(fg)}"'
        f' font-family="Segoe UI,sans-serif"'
        f' font-size="8">{esc(text)}</text>'
    )


def _draw_gnd_symbol(
    parts: list[str],
    cx: float,
    bus_y: float,
    *,
    stroke: str,
    fg: str,
) -> None:
    """IEC-style ground symbol (three decreasing bars) below the return rail."""
    sw = 2.0
    top_y = bus_y + GND_SYMBOL_OFFSET
    parts.append(
        f'<line x1="{cx:.1f}" y1="{bus_y:.1f}" x2="{cx:.1f}"'
        f' y2="{top_y:.1f}" stroke="{esc(stroke)}"'
        f' stroke-width="{sw}" stroke-linecap="round"/>'
    )
    base_y = top_y + 4.0
    for i, half_w in enumerate((14.0, 9.0, 5.5)):
        y = base_y + i * 5.0
        parts.append(
            f'<line x1="{cx - half_w:.1f}" y1="{y:.1f}" x2="{cx + half_w:.1f}"'
            f' y2="{y:.1f}" stroke="{esc(stroke)}" stroke-width="{sw}"'
            f' stroke-linecap="round"/>'
        )
    parts.append(
        f'<text x="{cx:.1f}" y="{base_y + 24:.1f}" text-anchor="middle"'
        f' fill="{esc(fg)}" font-family="Segoe UI,sans-serif"'
        f' font-size="9" font-weight="600">GND</text>'
    )


def _role_display_title(role: str) -> str:
    return "SERIES" if role in ("RESISTOR", "SERIES") else role


def _role_color(role: str, *, single_net: bool, fg: str) -> str:
    if single_net:
        return SINGLE_NET_ROLE_COLORS.get(role, ROLE_COLORS.get(role, fg))
    return ROLE_COLORS.get(role, fg)


def _draw_section_header(
    parts: list[str],
    *,
    x: float,
    y: float,
    w: float,
    role: str,
    label: str | None,
    color: str,
    round_top: bool,
) -> None:
    """Draw one role header band — square bottom, optional rounded top.

    Used for every band of a composite symbol and (with ``round_top=True``)
    for a single-role node's header.
    """
    if round_top:
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{HEADER_H:.1f}"'
            f' rx="6" fill="{esc(color)}"/>'
        )
        parts.append(
            f'<rect x="{x:.1f}" y="{y + HEADER_H - 6:.1f}" width="{w:.1f}"'
            f' height="6" fill="{esc(color)}"/>'
        )
    else:
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{HEADER_H:.1f}"'
            f' fill="{esc(color)}"/>'
        )
    parts.append(
        f'<text x="{x + 8:.1f}" y="{y + 15:.1f}" fill="#ffffff"'
        f' font-family="Segoe UI,sans-serif" font-size="10" font-weight="600">'
        f"{esc(_role_display_title(role))}</text>"
    )
    if label:
        parts.append(
            f'<text x="{x + w - 8:.1f}" y="{y + 15:.1f}" fill="#ffffff"'
            f' text-anchor="end" font-family="Segoe UI,sans-serif"'
            f' font-size="10" font-weight="600">{esc(label)}</text>'
        )


def _draw_node(
    parts: list[str],
    node: TopologyNode,
    *,
    bg: str,
    bg_alt: str,
    fg: str,
    fg_dim: str,
    border: str,
    err: str,
) -> None:
    x, y, w, h = node.x, node.y, node.width, node.height
    stroke = err if node.has_error else border
    sw = 1.0

    parts.append(
        f'<rect class="topo-hit" data-label="{esc(node.label)}"'
        f' x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}"'
        f' rx="6" fill="{esc(bg_alt)}" stroke="{esc(stroke)}"'
        f' stroke-width="{sw}"/>'
    )

    if node.sections:
        port_colors = {
            sec.role: _role_color(sec.role, single_net=False, fg=fg)
            for sec in node.sections
        }
        for i, sec in enumerate(node.sections):
            _draw_section_header(
                parts,
                x=x,
                y=y + sec.y,
                w=w,
                role=sec.role,
                label=node.label if i == 0 else None,
                color=port_colors[sec.role],
                round_top=(i == 0),
            )
    else:
        color = _role_color(node.role, single_net=node.single_net, fg=fg)
        port_colors = {node.role: color}
        _draw_section_header(
            parts,
            x=x,
            y=y,
            w=w,
            role=node.role,
            label=node.label,
            color=color,
            round_top=True,
        )

    if node.has_error:
        parts.append(
            f'<text x="{x + w - 6:.1f}" y="{y + h - 6:.1f}" text-anchor="end"'
            f' fill="{esc(err)}" font-size="12">⚠</text>'
        )

    for port in node.ports:
        px, py = port.x, port.y
        color = port_colors.get(port.role or node.role, fg)
        parts.append(
            f'<circle cx="{px:.1f}" cy="{py:.1f}" r="{PORT_R:.1f}"'
            f' fill="{esc(color)}" stroke="{esc(border)}" stroke-width="1"/>'
        )
        text_x = px + 8.0 if port.side == "left" else px - 8.0
        anchor = "start" if port.side == "left" else "end"
        line = truncate_label(port.label)
        parts.append(
            f'<text x="{text_x:.1f}" y="{py + 3:.1f}" text-anchor="{anchor}"'
            f' fill="{esc(fg_dim)}" font-family="Consolas,monospace"'
            f' font-size="8">{esc(line)}</text>'
        )


def _draw_legend(parts: list[str], y: float, *, fg_dim: str) -> None:
    items = (
        ("SOURCE", ROLE_COLORS["SOURCE"]),
        ("SINK", ROLE_COLORS["SINK"]),
        ("SERIES", ROLE_COLORS["RESISTOR"]),
        ("REGULATOR", ROLE_COLORS["REGULATOR"]),
    )
    x = 12.0
    for name, color in items:
        parts.append(
            f'<rect x="{x:.1f}" y="{y - 10:.1f}" width="10" height="10"'
            f' rx="2" fill="{esc(color)}"/>'
        )
        parts.append(
            f'<text x="{x + 14:.1f}" y="{y:.1f}" fill="{esc(fg_dim)}"'
            f' font-family="Segoe UI,sans-serif" font-size="10">{name}</text>'
        )
        x += 78.0
    parts.append(
        f'<line x1="{x:.1f}" y1="{y - 4:.1f}" x2="{x + 18:.1f}" y2="{y - 4:.1f}"'
        f' stroke="{esc(fg_dim)}" stroke-width="2" stroke-dasharray="5 4"/>'
    )
    parts.append(
        f'<text x="{x + 24:.1f}" y="{y:.1f}" fill="{esc(fg_dim)}"'
        f' font-family="Segoe UI,sans-serif" font-size="10">extern</text>'
    )


def render_topology_svg(
    model: TopologyModel,
    *,
    theme: dict[str, str] | None = None,
    width: float | None = None,
) -> str:
    """Render the topology model as an SVG document string."""
    theme = theme or {}
    bg = theme.get("bg", "#2b2b2b")
    bg_alt = theme.get("bg_alt", "#333333")
    fg = theme.get("fg", "#e6e6e6")
    fg_dim = theme.get("fg_dim", "#909090")
    err = theme.get("err", "#ff7070")
    border = theme.get("border", "#555555")

    w = width if width is not None else model.width
    h = model.height

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.0f}"'
        f' height="{h:.0f}" viewBox="0 0 {w:.0f} {h:.0f}">',
        f'<rect width="100%" height="100%" fill="{esc(bg)}"/>',
    ]

    _draw_wires_schematic(
        parts,
        model.wires,
        bg=bg,
        gnd_symbol_x=model.gnd_symbol_x,
        gnd_bus_y=model.gnd_bus_y,
    )

    if model.gnd_symbol_x is not None and model.gnd_bus_y is not None:
        _draw_gnd_symbol(
            parts,
            model.gnd_symbol_x,
            model.gnd_bus_y,
            stroke=fg_dim,
            fg=fg,
        )

    for node in model.nodes:
        _draw_node(
            parts,
            node,
            bg=bg,
            bg_alt=bg_alt,
            fg=fg,
            fg_dim=fg_dim,
            border=border,
            err=err,
        )

    for wire in model.wires:
        _draw_wire_label(parts, wire, fg=fg, bg=bg)

    _draw_legend(parts, h - 16, fg_dim=fg_dim)
    parts.append("</svg>")
    return "\n".join(parts)
