"""SVG rendering for the topology schematic."""

from __future__ import annotations

from collections.abc import Callable
from typing import NamedTuple

from fypa.topology.constants import (
    GND_NET,
    GND_SYMBOL_OFFSET,
    GND_WIRE_COLOR,
    HEADER_FONT_SIZE,
    HEADER_FONT_STEP,
    HEADER_H,
    HEADER_LABEL_MIN_FONT,
    HEADER_LABEL_SHRINK_FLOOR,
    HEADER_TEXT_GAP,
    HEADER_TEXT_PAD,
    JUNCTION_R,
    NON_GND_WIRE_COLOR,
    PORT_R,
    ROLE_COLORS,
    SINGLE_NET_ROLE_COLORS,
    WIRE_EPS,
)
from fypa.topology.hit_test import JUMP_ICON_R, jump_icon_center
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


from fypa.topology.constants import TAG_POINT, TAG_TEXT_PAD
from fypa.topology.types import (
    TopologyBand,
    TopologyModel,
    TopologyNode,
    TopologyTag,
    TopologyWire,
)
from fypa.topology.util import (
    esc,
    estimate_text_width,
    truncate_label,
    truncate_text_to_width,
)


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


def _draw_jump_icon(parts: list[str], center: tuple[float, float], *, color: str) -> None:
    """Small "locate" crosshair: a ring with four ticks (show on Heatmap)."""
    cx, cy = center
    r = JUMP_ICON_R
    c = esc(color)
    parts.append(
        f'<circle class="topo-jump" cx="{cx:.1f}" cy="{cy:.1f}" r="{r - 1.5:.1f}"'
        f' fill="none" stroke="{c}" stroke-width="1.2"/>'
    )
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        parts.append(
            f'<line x1="{cx + dx * (r - 3):.1f}" y1="{cy + dy * (r - 3):.1f}"'
            f' x2="{cx + dx * (r + 1):.1f}" y2="{cy + dy * (r + 1):.1f}"'
            f' stroke="{c}" stroke-width="1.2" stroke-linecap="round"/>'
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

    for tag in model.tags:
        if tag.net != net:
            continue
        parts.append(
            f'<path d="{_tag_path(tag)}" fill="none" stroke="{stroke_esc}"'
            f' stroke-width="3" stroke-linejoin="round"{op}/>'
        )
    for gx, gy in model.gnd_glyphs if net == GND_NET else ():
        parts.append(
            f'<line x1="{gx - 8:.1f}" y1="{gy:.1f}" x2="{gx + 8:.1f}" y2="{gy:.1f}"'
            f' stroke="{stroke_esc}" stroke-width="3" stroke-linecap="round"{op}/>'
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
    if role in ("RESISTOR", "SERIES"):
        return "SERIES"
    if role == "AUTO_BRIDGE":
        return "BRIDGE"
    return role


def _role_color(role: str, *, single_net: bool, fg: str) -> str:
    if single_net:
        return SINGLE_NET_ROLE_COLORS.get(role, ROLE_COLORS.get(role, fg))
    return ROLE_COLORS.get(role, fg)


def _largest_font_size(
    measure: Callable[[float], float],
    max_width: float,
    *,
    floor: float,
) -> float | None:
    """Largest size down to ``floor`` where ``measure(size) <= max_width``."""
    size = HEADER_FONT_SIZE
    while size >= floor - 1e-9:
        if measure(size) <= max_width:
            return size
        size -= HEADER_FONT_STEP
    return None


class _HeaderText(NamedTuple):
    """What fits in one header band: role word, designator, and their size."""

    role: str | None
    label: str | None
    font_size: float


def _fit_header_texts(role_title: str, label: str | None, w: float) -> _HeaderText:
    """Fit the role word and designator into a header band of width ``w``.

    Nodes are a fixed width, so a long designator would otherwise run straight
    through the role word. Both texts shrink together (keeping the band's text
    one size) until that would make them too small to read; past that point the
    designator wins, taking the whole band on its own — the band colour still
    names the role via the legend. Only a designator too long for the full band
    is truncated, and the hover tooltip always carries the untruncated name.
    """
    full = w - 2 * HEADER_TEXT_PAD
    if not label:
        return _HeaderText(role_title, None, HEADER_FONT_SIZE)

    size = _largest_font_size(
        lambda s: (
            estimate_text_width(role_title, s)
            + HEADER_TEXT_GAP
            + estimate_text_width(label, s)
        ),
        full,
        floor=HEADER_LABEL_SHRINK_FLOOR,
    )
    if size is not None:
        return _HeaderText(role_title, label, size)

    size = _largest_font_size(
        lambda s: estimate_text_width(label, s),
        full,
        floor=HEADER_LABEL_MIN_FONT,
    )
    if size is not None:
        return _HeaderText(None, label, size)
    return _HeaderText(
        None,
        truncate_text_to_width(label, full, HEADER_LABEL_MIN_FONT),
        HEADER_LABEL_MIN_FONT,
    )


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
    fitted = _fit_header_texts(_role_display_title(role), label, w)
    size = fitted.font_size
    if fitted.role:
        parts.append(
            f'<text x="{x + HEADER_TEXT_PAD:.1f}" y="{y + 15:.1f}" fill="#ffffff"'
            f' font-family="Segoe UI,sans-serif" font-size="{size:g}"'
            f' font-weight="600">{esc(fitted.role)}</text>'
        )
    if fitted.label:
        parts.append(
            f'<text x="{x + w - HEADER_TEXT_PAD:.1f}" y="{y + 15:.1f}" fill="#ffffff"'
            f' text-anchor="end" font-family="Segoe UI,sans-serif"'
            f' font-size="{size:g}" font-weight="600">{esc(fitted.label)}</text>'
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

    if node.compact:
        _draw_chip(parts, node, bg_alt=bg_alt, fg=fg, fg_dim=fg_dim, border=border)
        return

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

    icon = jump_icon_center(node)
    if node.has_error:
        # Bottom-right normally; bottom-left when the jump icon is there.
        ex, anchor = (x + 6, "start") if icon is not None else (x + w - 6, "end")
        parts.append(
            f'<text x="{ex:.1f}" y="{y + h - 6:.1f}" text-anchor="{anchor}"'
            f' fill="{esc(err)}" font-size="12">⚠</text>'
        )
    if icon is not None:
        _draw_jump_icon(parts, icon, color=fg_dim)

    if node.value_str:
        parts.append(
            f'<text x="{x + w / 2:.1f}" y="{y + h - 8:.1f}" text-anchor="middle"'
            f' fill="{esc(fg)}" font-family="Segoe UI,sans-serif"'
            f' font-size="9">{esc(truncate_label(node.value_str, max_len=26))}</text>'
        )

    for port in node.ports:
        px, py = port.x, port.y
        color = port_colors.get(port.role or node.role, fg)
        if port.side == "bottom":
            color = GND_WIRE_COLOR
        parts.append(
            f'<circle cx="{px:.1f}" cy="{py:.1f}" r="{PORT_R:.1f}"'
            f' fill="{esc(color)}" stroke="{esc(border)}" stroke-width="1"/>'
        )
        if port.side == "bottom":
            continue
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


# --- rows layout ------------------------------------------------------------

TAG_COLORS: dict[str, str] = {
    "net": NON_GND_WIRE_COLOR,
    "feed": NON_GND_WIRE_COLOR,
    "ref": ROLE_COLORS["REGULATOR"],
    "undriven": "#e0a030",
    "end": "#8a8f98",
}


def _draw_chip(
    parts: list[str],
    node: TopologyNode,
    *,
    bg_alt: str,
    fg: str,
    fg_dim: str,
    border: str,
) -> None:
    """One-line symbol for a 0 Ω auto-bridge: coloured lead-in, designator, value."""
    x, y, w, h = node.x, node.y, node.width, node.height
    color = ROLE_COLORS.get(node.role, fg_dim)
    parts.append(
        f'<rect class="topo-hit" data-label="{esc(node.label)}"'
        f' x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}"'
        f' rx="4" fill="{esc(bg_alt)}" stroke="{esc(border)}" stroke-width="1"/>'
    )
    parts.append(
        f'<rect x="{x:.1f}" y="{y:.1f}" width="6" height="{h:.1f}" rx="2"'
        f' fill="{esc(color)}"/>'
    )
    parts.append(
        f'<text x="{x + 12:.1f}" y="{y + h / 2 + 3.5:.1f}" fill="{esc(fg)}"'
        f' font-family="Segoe UI,sans-serif" font-size="9" font-weight="600">'
        f"{esc(truncate_label(node.label, max_len=12))}</text>"
    )
    icon = jump_icon_center(node)
    if icon is not None:
        _draw_jump_icon(parts, icon, color=fg_dim)
    if node.value_str:
        vx = x + w - (20 if icon is not None else 8)
        parts.append(
            f'<text x="{vx:.1f}" y="{y + h / 2 + 3.5:.1f}" text-anchor="end"'
            f' fill="{esc(fg_dim)}" font-family="Segoe UI,sans-serif"'
            f' font-size="8">{esc(truncate_label(node.value_str, max_len=12))}</text>'
        )
    for port in node.ports:
        parts.append(
            f'<circle cx="{port.x:.1f}" cy="{port.y:.1f}" r="{PORT_R - 1:.1f}"'
            f' fill="{esc(color)}" stroke="{esc(border)}" stroke-width="1"/>'
        )


def _tag_path(tag: TopologyTag) -> str:
    """Pentagon pointing right: flat left edge, arrow point on the right."""
    x0, x1 = tag.x, tag.x + tag.width
    top, bot = tag.y - tag.height / 2, tag.y + tag.height / 2
    return (
        f"M {x0:.1f},{top:.1f} H {x1 - TAG_POINT:.1f} L {x1:.1f},{tag.y:.1f}"
        f" L {x1 - TAG_POINT:.1f},{bot:.1f} H {x0:.1f} Z"
    )


def _draw_tag(parts: list[str], tag: TopologyTag, *, bg_alt: str, fg: str) -> None:
    color = TAG_COLORS.get(tag.kind, fg)
    dash = ' stroke-dasharray="3 2"' if tag.kind == "undriven" else ""
    parts.append(
        f'<path class="topo-tag" d="{_tag_path(tag)}" fill="{esc(bg_alt)}"'
        f' stroke="{esc(color)}" stroke-width="1.5" stroke-linejoin="round"{dash}/>'
    )
    text_x = tag.x + TAG_TEXT_PAD + (tag.width - TAG_POINT - 2 * TAG_TEXT_PAD) / 2
    parts.append(
        f'<text x="{text_x:.1f}" y="{tag.y + 3:.1f}" text-anchor="middle"'
        f' fill="{esc(fg)}" font-family="Consolas,monospace" font-size="8"'
        f' font-weight="600">{esc(tag.text)}</text>'
    )


def _draw_local_gnd(parts: list[str], x: float, y: float) -> None:
    """Small three-bar ground symbol whose top centre is at ``(x, y)``."""
    for i, half_w in enumerate((7.0, 4.5, 2.0)):
        yy = y + i * 3.5
        parts.append(
            f'<line x1="{x - half_w:.1f}" y1="{yy:.1f}" x2="{x + half_w:.1f}"'
            f' y2="{yy:.1f}" stroke="{GND_WIRE_COLOR}" stroke-width="1.6"'
            f' stroke-linecap="round"/>'
        )


def _draw_band(
    parts: list[str], band: TopologyBand, *, fg: str, fg_dim: str, border: str,
) -> None:
    if band.kind == "bridge_group":
        parts.append(
            f'<text x="{band.x:.1f}" y="{band.y + 11:.1f}" fill="{esc(fg_dim)}"'
            f' font-family="Consolas,monospace" font-size="9">{esc(band.title)}</text>'
        )
        return
    accent = {
        "source": ROLE_COLORS["SOURCE"],
        "regulator": ROLE_COLORS["REGULATOR"],
        "undriven": TAG_COLORS["undriven"],
    }.get(band.kind, fg_dim)
    y = band.y
    parts.append(
        f'<line x1="{band.x:.1f}" y1="{y:.1f}" x2="{band.x + band.width:.1f}"'
        f' y2="{y:.1f}" stroke="{esc(border)}" stroke-width="1"/>'
    )
    parts.append(
        f'<rect x="{band.x:.1f}" y="{y + 5:.1f}" width="3" height="13" rx="1"'
        f' fill="{esc(accent)}"/>'
    )
    title_x = band.x + 9
    parts.append(
        f'<text x="{title_x:.1f}" y="{y + 15.5:.1f}" fill="{esc(fg)}"'
        f' font-family="Segoe UI,sans-serif" font-size="11" font-weight="600">'
        f"{esc(band.title)}</text>"
    )
    if band.subtitle:
        sub_x = title_x + estimate_text_width(band.title, 11) + 12
        parts.append(
            f'<text x="{sub_x:.1f}" y="{y + 15.5:.1f}" fill="{esc(fg_dim)}"'
            f' font-family="Segoe UI,sans-serif" font-size="10">'
            f"{esc(band.subtitle)}</text>"
        )


_ROWS_LEGEND_ROLES = ("SOURCE", "REGULATOR", "SERIES", "SINK", "BRIDGE")
_ROWS_LEGEND_TAGS = (
    ("net", "rail continues elsewhere (click)"),
    ("ref", "feeds a regulator row (click)"),
    ("undriven", "nothing drives it"),
    ("end", "no loads"),
)


def rows_legend_width() -> float:
    """Widest line of the rows-layout legend (the model is made at least this wide)."""
    roles = sum(14 + estimate_text_width(n, 10) + 16 for n in _ROWS_LEGEND_ROLES)
    tags = sum(24 + estimate_text_width(t, 10) + 16 for _, t in _ROWS_LEGEND_TAGS)
    return 12.0 + max(roles, tags + 20 + estimate_text_width("ground", 10))


def _draw_rows_legend(parts: list[str], y: float, *, fg_dim: str, bg_alt: str) -> None:
    x = 12.0
    for name, color in (
        ("SOURCE", ROLE_COLORS["SOURCE"]),
        ("REGULATOR", ROLE_COLORS["REGULATOR"]),
        ("SERIES", ROLE_COLORS["RESISTOR"]),
        ("SINK", ROLE_COLORS["SINK"]),
        ("BRIDGE", ROLE_COLORS["AUTO_BRIDGE"]),
    ):
        parts.append(
            f'<rect x="{x:.1f}" y="{y - 10:.1f}" width="10" height="10"'
            f' rx="2" fill="{esc(color)}"/>'
        )
        parts.append(
            f'<text x="{x + 14:.1f}" y="{y:.1f}" fill="{esc(fg_dim)}"'
            f' font-family="Segoe UI,sans-serif" font-size="10">{name}</text>'
        )
        x += 14 + estimate_text_width(name, 10) + 16
    x = 12.0
    y += 18.0
    for kind, text in _ROWS_LEGEND_TAGS:
        color = TAG_COLORS[kind]
        dash = ' stroke-dasharray="3 2"' if kind == "undriven" else ""
        parts.append(
            f'<path d="M {x:.1f},{y - 10:.1f} H {x + 14:.1f} L {x + 19:.1f},{y - 5:.1f}'
            f' L {x + 14:.1f},{y:.1f} H {x:.1f} Z" fill="{esc(bg_alt)}"'
            f' stroke="{esc(color)}" stroke-width="1.5"{dash}/>'
        )
        parts.append(
            f'<text x="{x + 24:.1f}" y="{y:.1f}" fill="{esc(fg_dim)}"'
            f' font-family="Segoe UI,sans-serif" font-size="10">{esc(text)}</text>'
        )
        x += 24 + estimate_text_width(text, 10) + 16
    _draw_local_gnd(parts, x + 7, y - 9)
    parts.append(
        f'<text x="{x + 20:.1f}" y="{y:.1f}" fill="{esc(fg_dim)}"'
        f' font-family="Segoe UI,sans-serif" font-size="10">ground</text>'
    )


def _render_rows(
    parts: list[str],
    model: TopologyModel,
    *,
    bg: str,
    bg_alt: str,
    fg: str,
    fg_dim: str,
    err: str,
    border: str,
) -> None:
    for band in model.bands:
        _draw_band(parts, band, fg=fg, fg_dim=fg_dim, border=border)
    _draw_wires_schematic(parts, model.wires, bg=bg)
    for gx, gy in model.gnd_glyphs:
        _draw_local_gnd(parts, gx, gy)
    for node in model.nodes:
        _draw_node(
            parts, node, bg=bg, bg_alt=bg_alt, fg=fg, fg_dim=fg_dim,
            border=border, err=err,
        )
    for tag in model.tags:
        _draw_tag(parts, tag, bg_alt=bg_alt, fg=fg)
    for wire in model.wires:
        _draw_wire_label(parts, wire, fg=fg, bg=bg)
    y = model.height - 52.0 - 20.0 * len(model.notes)
    for note in model.notes:
        y += 20.0
        parts.append(
            f'<text x="12" y="{y - 6:.1f}" fill="{esc(fg_dim)}"'
            f' font-family="Segoe UI,sans-serif" font-size="10"'
            f' font-style="italic">{esc(note)}</text>'
        )
    _draw_rows_legend(parts, model.height - 30, fg_dim=fg_dim, bg_alt=bg_alt)


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

    if model.layout == "rows":
        _render_rows(
            parts, model, bg=bg, bg_alt=bg_alt, fg=fg, fg_dim=fg_dim,
            err=err, border=border,
        )
        parts.append("</svg>")
        return "\n".join(parts)

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
