"""SVG rendering for the topology schematic.

Blocks are "tonal": a role-tinted header band with the role word in the role
colour, a role-tinted outline and ring ports. Wiring is "soft": thin muted
rails with rounded corners, ringed junction dots, row panels, pill-shaped net
and voltage labels and tinted port tags. Colours come from
:mod:`fypa.topology.style`, resolved per theme.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NamedTuple

from fypa.topology.constants import (
    BRIDGE_R,
    GND_NET,
    GND_SYMBOL_OFFSET,
    HEADER_FONT_SIZE,
    HEADER_FONT_STEP,
    HEADER_H,
    HEADER_LABEL_MIN_FONT,
    HEADER_LABEL_SHRINK_FLOOR,
    HEADER_TEXT_GAP,
    HEADER_TEXT_PAD,
    TAG_POINT,
    TAG_TEXT_PAD,
    WIRE_EPS,
)
from fypa.topology.geometry import (
    SchematicGeometry,
    compute_schematic_geometry,
    vertical_bridge_path,
)
from fypa.topology.hit_test import JUMP_ICON_R, jump_icon_center
from fypa.topology.style import Palette, mix, palette
from fypa.topology.types import (
    TopologyBand,
    TopologyModel,
    TopologyNode,
    TopologyTag,
    TopologyWire,
)
from fypa.topology.util import (
    CHIP_LABEL_FONT,
    CHIP_LABEL_X,
    CHIP_TEXT_GAP,
    CHIP_VALUE_FONT,
    chip_right_pad,
    esc,
    estimate_text_width,
    truncate_label,
    truncate_text_to_width,
)

SANS = "Segoe UI,sans-serif"
MONO = "Consolas,monospace"

WIRE_W = 1.6            # every wire, power and ground
CORNER_R = 7.0          # bend radius where a wire turns a corner
JUNCTION_DOT_R = 3.2    # junction dot, ringed in the canvas colour
NODE_RX = 7.0           # block corner radius
CHIP_RX = 6.0
PORT_RING_R = 4.4       # block port ring
CHIP_PORT_R = 3.8
GRID_STEP = 16.0        # dot grid pitch, in diagram units (the viewer's too)


def _text(x: float, y: float, text: str, *, fill: str, size: float,
          anchor: str | None = None, weight: int | None = None,
          font: str = SANS, italic: bool = False, cls: str = "") -> str:
    attrs = [f'class="{cls}"'] if cls else []
    attrs.append(f'x="{x:.1f}" y="{y:.1f}"')
    if anchor:
        attrs.append(f'text-anchor="{anchor}"')
    attrs.append(f'fill="{esc(fill)}" font-family="{font}" font-size="{size:g}"')
    if weight:
        attrs.append(f'font-weight="{weight}"')
    if italic:
        attrs.append('font-style="italic"')
    return f"<text {' '.join(attrs)}>{esc(text)}</text>"


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


# --- wires --------------------------------------------------------------------------


def _wire_paths(geo: SchematicGeometry) -> list[tuple[str, str]]:
    """``(net, path_d)`` for every wire piece, with each plain corner (one
    horizontal meeting one vertical of the same net, no junction) cut back
    and rounded off with a quarter curve. Bridge hops stay on the verticals."""

    def key(x: float, y: float) -> tuple[float, float]:
        return round(x, 1), round(y, 1)

    junctions = {key(x, y) for x, y in geo.junctions}
    ends: dict[tuple[str, float, float], list[tuple[str, int, int]]] = {}
    for orient, segs in (("H", geo.horizontals), ("V", geo.verticals)):
        for i, s in enumerate(segs):
            for e, (x, y) in enumerate(((s.x1, s.y1), (s.x2, s.y2))):
                ends.setdefault((s.net, *key(x, y)), []).append((orient, i, e))
    hops = [(geo.verticals[b.vertical_index].x1, b.y) for b in geo.bridges]

    trim_h = [[0.0, 0.0] for _ in geo.horizontals]
    trim_v = [[0.0, 0.0] for _ in geo.verticals]
    curves: list[tuple[str, str]] = []
    for (net, x, y), hits in ends.items():
        if (x, y) in junctions or len(hits) != 2:
            continue
        hs = [(i, e) for o, i, e in hits if o == "H"]
        vs = [(i, e) for o, i, e in hits if o == "V"]
        if len(hs) != 1 or len(vs) != 1:
            continue
        (hi, he), (vi, ve) = hs[0], vs[0]
        h, v = geo.horizontals[hi], geo.verticals[vi]
        hx = h.x2 if he == 0 else h.x1      # the horizontal's far end
        vy = v.y2 if ve == 0 else v.y1      # the vertical's far end
        r = min(CORNER_R, abs(hx - x) / 2, abs(vy - y) / 2)
        # Keep the curve clear of a bridge hop on either leg.
        for bx, by in hops:
            if abs(bx - x) < WIRE_EPS:
                r = min(r, abs(by - y) - BRIDGE_R - 1.0)
            if abs(by - y) < WIRE_EPS:
                r = min(r, abs(bx - x) - BRIDGE_R - 1.0)
        if r < 1.0:
            continue
        trim_h[hi][he] = r
        trim_v[vi][ve] = r
        dh = 1.0 if hx > x else -1.0
        dv = 1.0 if vy > y else -1.0
        curves.append((net, f"M {x + dh * r:.1f},{y:.1f} Q {x:.1f},{y:.1f}"
                            f" {x:.1f},{y + dv * r:.1f}"))

    out: list[tuple[str, str]] = []
    for h, (t1, t2) in zip(geo.horizontals, trim_h):
        d = 1.0 if h.x2 >= h.x1 else -1.0
        out.append((h.net, f"M {h.x1 + d * t1:.1f},{h.y1:.1f} H {h.x2 - d * t2:.1f}"))
    for vi, (v, (t1, t2)) in enumerate(zip(geo.verticals, trim_v)):
        if v.y1 <= v.y2:
            lo, hi = v.y1 + t1, v.y2 - t2
        else:
            lo, hi = v.y2 + t2, v.y1 - t1
        crosses = sorted(set(geo.vert_crossings.get(vi, [])))
        out.append((v.net, vertical_bridge_path(v.x1, lo, hi, crosses)))
    return out + curves


def _draw_wires_schematic(
    parts: list[str],
    wires: list[TopologyWire],
    pal: Palette,
    *,
    gnd_symbol_x: float | None = None,
    gnd_bus_y: float | None = None,
) -> None:
    """Draw wires with rounded corners, junction dots and bridge hops."""
    geo = compute_schematic_geometry(
        wires, gnd_symbol_x=gnd_symbol_x, gnd_bus_y=gnd_bus_y,
    )
    for net, d in _wire_paths(geo):
        parts.append(
            f'<path d="{d}" fill="none" stroke="{esc(pal.wire(net))}"'
            f' stroke-width="{WIRE_W:g}" stroke-linecap="round"'
            f' stroke-linejoin="round"/>'
        )
    for w in wires:
        if w.dashed:
            parts.append(
                f'<path d="{w.path_d}" fill="none" stroke="{esc(pal.wire(w.net))}"'
                f' stroke-width="{WIRE_W:g}" stroke-linecap="round"'
                f' stroke-linejoin="round" stroke-dasharray="5 4"/>'
            )
    for jx, jy in geo.junctions:
        color = pal.wire(_segment_net_at(geo.segments, jx, jy))
        parts.append(
            f'<circle cx="{jx:.1f}" cy="{jy:.1f}" r="{JUNCTION_DOT_R:g}"'
            f' fill="{esc(color)}" stroke="{esc(pal.bg)}" stroke-width="1.5"/>'
        )


def _net_highlight_fragment(
    model: TopologyModel,
    net: str,
    *,
    stroke: str,
    stroke_width: float = 5.0,
) -> str:
    """SVG shapes highlighting every piece of ``net``, on the same rounded
    paths the diagram draws."""
    parts: list[str] = []
    stroke_esc = esc(stroke)
    sw = stroke_width
    op = ' opacity="0.88"'
    geo = compute_schematic_geometry(
        model.wires,
        gnd_symbol_x=model.gnd_symbol_x,
        gnd_bus_y=model.gnd_bus_y,
    )
    for wnet, d in _wire_paths(geo):
        if wnet != net:
            continue
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
        parts.append(
            f'<path d="{wire.path_d}" fill="none" stroke="{stroke_esc}"'
            f' stroke-width="{sw}" stroke-linecap="round" stroke-linejoin="round"'
            f' stroke-dasharray="5 4"{op}/>'
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


def _draw_wire_label(parts: list[str], wire: TopologyWire, pal: Palette) -> None:
    """Net name on a pill outlined in a faded wire colour."""
    if not wire.label:
        return
    x, y = wire.label_x, wire.label_y
    if x == 0.0 and y == 0.0:
        return
    text = wire.label
    w = estimate_text_width(text, 8) + 10.0
    h = 12.0
    edge = esc(mix(pal.wire(wire.net), pal.bg, 0.5))
    if wire.label_vertical:
        parts.append(f'<g transform="translate({x:.1f},{y:.1f}) rotate(-90)">')
        parts.append(
            f'<rect x="{-w / 2:.1f}" y="{-h / 2:.1f}" width="{w:.1f}" height="{h:g}"'
            f' rx="6" fill="{esc(pal.bg)}" stroke="{edge}" stroke-width="0.8"/>'
        )
        parts.append(_text(0.0, 3.0, text, fill=pal.fg, size=8, anchor="middle"))
        parts.append("</g>")
        return
    anchor = wire.label_text_anchor or "middle"
    if anchor == "start":
        rx = x - 5.0
    elif anchor == "end":
        rx = x - w + 5.0
    else:
        rx = x - w / 2
    parts.append(
        f'<rect x="{rx:.1f}" y="{y - h / 2:.1f}" width="{w:.1f}" height="{h:g}"'
        f' rx="6" fill="{esc(pal.bg)}" stroke="{edge}" stroke-width="0.8"/>'
    )
    parts.append(_text(rx + w / 2, y + 3.0, text, fill=pal.fg, size=8,
                       anchor="middle"))


def _draw_gnd_symbol(
    parts: list[str],
    cx: float,
    bus_y: float,
    *,
    stroke: str,
    fg: str,
) -> None:
    """IEC-style ground symbol (three decreasing bars) below the return rail."""
    sw = WIRE_W
    top_y = bus_y + GND_SYMBOL_OFFSET
    parts.append(
        f'<line x1="{cx:.1f}" y1="{bus_y:.1f}" x2="{cx:.1f}"'
        f' y2="{top_y:.1f}" stroke="{esc(stroke)}"'
        f' stroke-width="{sw:g}" stroke-linecap="round"/>'
    )
    base_y = top_y + 4.0
    for i, half_w in enumerate((14.0, 9.0, 5.5)):
        y = base_y + i * 5.0
        parts.append(
            f'<line x1="{cx - half_w:.1f}" y1="{y:.1f}" x2="{cx + half_w:.1f}"'
            f' y2="{y:.1f}" stroke="{esc(stroke)}" stroke-width="{sw:g}"'
            f' stroke-linecap="round"/>'
        )
    parts.append(_text(cx, base_y + 24, "GND", fill=fg, size=9, anchor="middle",
                       weight=600))


# --- blocks -------------------------------------------------------------------------


def _role_display_title(role: str) -> str:
    if role in ("RESISTOR", "SERIES"):
        return "SERIES"
    if role == "AUTO_BRIDGE":
        return "BRIDGE"
    return role


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
    through the role word. Both texts shrink together until that would make
    them too small to read; past that point the designator wins, taking the
    whole band on its own - the band's tint still names the role via the
    legend. Only a designator too long for the full band is truncated, and the
    hover tooltip always carries the untruncated name.

    The role word is drawn :func:`_role_word_size` (smaller than the fitted
    size), so it always takes no more room than fitted here.
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


def _role_word_size(fitted_size: float) -> float:
    """The role word sits two sizes under the designator (whole px: Qt
    rounds a fractional font size)."""
    return max(HEADER_LABEL_MIN_FONT, fitted_size - 2.0)


def _top_rounded(x: float, y: float, w: float, h: float, r: float) -> str:
    return (
        f"M {x:.1f},{y + h:.1f} V {y + r:.1f} Q {x:.1f},{y:.1f} {x + r:.1f},{y:.1f}"
        f" H {x + w - r:.1f} Q {x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f}"
        f" V {y + h:.1f} Z"
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
    pal: Palette,
) -> None:
    """One role header band: a tint of the role colour, the role word in that
    colour on the left, the designator on the right. Rounded on top for a
    block's first band."""
    fill = esc(pal.header_fill(color))
    if round_top:
        parts.append(f'<path d="{_top_rounded(x, y, w, HEADER_H, NODE_RX)}"'
                     f' fill="{fill}"/>')
    else:
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{HEADER_H:.1f}"'
            f' fill="{fill}"/>'
        )
    parts.append(
        f'<line x1="{x:.1f}" y1="{y + HEADER_H:.1f}" x2="{x + w:.1f}"'
        f' y2="{y + HEADER_H:.1f}" stroke="{esc(pal.header_rule(color))}"/>'
    )
    fitted = _fit_header_texts(_role_display_title(role), label, w)
    if fitted.role:
        parts.append(_text(
            x + HEADER_TEXT_PAD, y + 14.5, fitted.role,
            fill=pal.role_text(color), size=_role_word_size(fitted.font_size),
            weight=700, cls="topo-head",
        ))
    if fitted.label:
        parts.append(_text(
            x + w - HEADER_TEXT_PAD, y + 15, fitted.label, fill=pal.fg,
            size=fitted.font_size, anchor="end", weight=600, cls="topo-head",
        ))


def _draw_jump_icon(parts: list[str], center: tuple[float, float], *, color: str) -> None:
    """Small "locate" crosshair: a ring with four ticks (show on Board)."""
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


def _draw_warning(parts: list[str], x: float, y: float, color: str) -> None:
    """Outlined warning triangle, 11 px wide, bottom-left corner at ``(x, y)``
    (drawn, so it never depends on a font having the glyph)."""
    c = esc(color)
    parts.append(
        f'<g class="topo-warn"><path d="M {x:.1f},{y:.1f} l 5.5,-9.5 l 5.5,9.5 Z"'
        f' fill="none" stroke="{c}" stroke-width="1.3" stroke-linejoin="round"/>'
        f'<path d="M {x + 5.5:.1f},{y - 6:.1f} v 2.8 M {x + 5.5:.1f},{y - 1.7:.1f}'
        f' v 0.2" stroke="{c}" stroke-width="1.3" stroke-linecap="round"/></g>'
    )


def _port_ring(px: float, py: float, r: float, color: str, pal: Palette) -> str:
    return (f'<circle cx="{px:.1f}" cy="{py:.1f}" r="{r:g}" fill="{esc(pal.bg)}"'
            f' stroke="{esc(color)}" stroke-width="1.8"/>')


def _draw_node(parts: list[str], node: TopologyNode, pal: Palette) -> None:
    x, y, w, h = node.x, node.y, node.width, node.height

    if node.compact:
        _draw_chip(parts, node, pal)
        return

    if node.sections:
        port_colors = {sec.role: pal.role(sec.role) for sec in node.sections}
        frame_color = port_colors[node.sections[0].role]
    else:
        frame_color = pal.role(node.role, single_net=node.single_net)
        port_colors = {node.role: frame_color}

    parts.append(
        f'<rect class="topo-hit" data-label="{esc(node.label)}"'
        f' x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}"'
        f' rx="{NODE_RX:g}" fill="{esc(pal.surface)}"/>'
    )
    if node.sections:
        for i, sec in enumerate(node.sections):
            _draw_section_header(
                parts, x=x, y=y + sec.y, w=w, role=sec.role,
                label=node.label if i == 0 else None,
                color=port_colors[sec.role], round_top=(i == 0), pal=pal,
            )
    else:
        _draw_section_header(
            parts, x=x, y=y, w=w, role=node.role, label=node.label,
            color=frame_color, round_top=True, pal=pal,
        )
    stroke = pal.err if node.has_error else pal.outline(frame_color)
    parts.append(
        f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}"'
        f' rx="{NODE_RX:g}" fill="none" stroke="{esc(stroke)}"'
        f' stroke-width="{1.3 if node.has_error else 1:g}"/>'
    )

    icon = jump_icon_center(node)
    if node.has_error:
        # Bottom-right normally; bottom-left when the jump icon is there.
        _draw_warning(parts, x + 7 if icon is not None else x + w - 18,
                      y + h - 5, pal.err)
    if icon is not None:
        _draw_jump_icon(parts, icon, color=pal.fg_dim)

    if node.value_str:
        parts.append(_text(
            x + w / 2, y + h - 8, truncate_label(node.value_str, max_len=26),
            fill=pal.fg, size=9, anchor="middle", weight=500,
        ))

    if node.gauges:
        from fypa.topology.gauges import draw_gauge

        for g in node.gauges:
            draw_gauge(parts, g, bg=pal.bg, fg=pal.fg, fg_dim=pal.fg_dim,
                       border=pal.border, err=pal.err, surface=pal.surface)

    for port in node.ports:
        px, py = port.x, port.y
        color = port_colors.get(port.role or node.role, frame_color)
        if port.side == "bottom":
            color = pal.gnd
        parts.append(_port_ring(px, py, PORT_RING_R, color, pal))
        if port.side == "bottom":
            continue
        left = port.side == "left"
        parts.append(_text(
            px + 9.0 if left else px - 9.0, py + 3, truncate_label(port.label),
            fill=pal.fg_dim, size=8, anchor=None if left else "end", font=MONO,
        ))


def _draw_chip(parts: list[str], node: TopologyNode, pal: Palette) -> None:
    """One-line symbol for a two-terminal SERIES part or a 0 Ω auto-bridge:
    a pill tinted with its role (green = SERIES, grey = BRIDGE), designator,
    value."""
    x, y, w, h = node.x, node.y, node.width, node.height
    color = pal.role(node.role)
    stroke = pal.err if node.has_error else pal.chip_outline(color)
    parts.append(
        f'<rect class="topo-hit" data-label="{esc(node.label)}"'
        f' x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}"'
        f' rx="{CHIP_RX:g}" fill="{esc(pal.chip_fill(color))}"'
        f' stroke="{esc(stroke)}" stroke-width="1"/>'
    )
    icon = jump_icon_center(node)
    # The layout sizes the chip to fit both texts (chip_width); only past
    # CHIP_MAX_W does the designator get cut, never the value.
    value = node.value_str or ""
    value_end = x + w - chip_right_pad(icon is not None)
    label_room = value_end - (x + CHIP_LABEL_X)
    if value:
        label_room -= CHIP_TEXT_GAP + estimate_text_width(value, CHIP_VALUE_FONT)
    label = truncate_text_to_width(node.label, label_room, CHIP_LABEL_FONT)
    parts.append(_text(x + CHIP_LABEL_X, y + h / 2 + 3.5, label, fill=pal.fg,
                       size=CHIP_LABEL_FONT, weight=600))
    if icon is not None:
        _draw_jump_icon(parts, icon, color=pal.fg_dim)
    if value:
        parts.append(_text(value_end, y + h / 2 + 3.5, value, fill=pal.fg_dim,
                           size=CHIP_VALUE_FONT, anchor="end"))
    for port in node.ports:
        parts.append(_port_ring(port.x, port.y, CHIP_PORT_R, color, pal))


# --- legend (columns layout) ----------------------------------------------------------


def _draw_legend(parts: list[str], y: float, pal: Palette) -> None:
    x = 12.0
    for name in ("SOURCE", "SINK", "SERIES", "REGULATOR"):
        parts.append(
            f'<rect x="{x:.1f}" y="{y - 10:.1f}" width="10" height="10"'
            f' rx="3" fill="{esc(pal.role(name))}"/>'
        )
        parts.append(_text(x + 14, y, name, fill=pal.fg_dim, size=10))
        x += 78.0
    parts.append(
        f'<line x1="{x:.1f}" y1="{y - 4:.1f}" x2="{x + 18:.1f}" y2="{y - 4:.1f}"'
        f' stroke="{esc(pal.fg_dim)}" stroke-width="{WIRE_W:g}"'
        f' stroke-dasharray="5 4"/>'
    )
    parts.append(_text(x + 24, y, "extern", fill=pal.fg_dim, size=10))


# --- rows layout ------------------------------------------------------------------------

_DASHED_TAGS = ("undriven", "add")


def _tag_path(tag: TopologyTag, inset: float = 0.0) -> str:
    """Pentagon pointing right: flat left edge, arrow point on the right
    (``inset`` shrinks it inside its box)."""
    i = inset
    x0, x1 = tag.x + i, tag.x + tag.width - i * 1.3
    top, bot = tag.y - tag.height / 2 + i, tag.y + tag.height / 2 - i
    point = TAG_POINT - i * 0.4
    return (
        f"M {x0:.1f},{top:.1f} H {x1 - point:.1f} L {x1:.1f},{tag.y:.1f}"
        f" L {x1 - point:.1f},{bot:.1f} H {x0:.1f} Z"
    )


def _draw_tag(parts: list[str], tag: TopologyTag, pal: Palette) -> None:
    """Tinted pentagon with a thin edge in the tag's colour (dashed for an
    undriven rail or the "+ SINK" handle)."""
    color = pal.tag(tag.kind)
    fill = esc(mix(color, pal.bg, 0.22))
    dashed = tag.kind in _DASHED_TAGS
    # A dashed edge stays at full strength: the dashes are the message.
    edge = ' stroke-dasharray="3 2"' if dashed else ' opacity="0.55"'
    parts.append(
        f'<path class="topo-tag" d="{_tag_path(tag, 1.5)}" fill="{fill}"'
        f' stroke="{fill}" stroke-width="3" stroke-linejoin="round"/>'
    )
    parts.append(
        f'<path d="{_tag_path(tag, 0.4)}" fill="none" stroke="{esc(color)}"'
        f' stroke-width="0.9" stroke-linejoin="round"{edge}/>'
    )
    text_x = tag.x + TAG_TEXT_PAD + (tag.width - TAG_POINT - 2 * TAG_TEXT_PAD) / 2
    parts.append(_text(text_x, tag.y + 3, tag.text, fill=pal.fg, size=8,
                       anchor="middle", weight=600, font=MONO))


def _draw_local_gnd(parts: list[str], x: float, y: float, color: str) -> None:
    """Small three-bar ground symbol whose top centre is at ``(x, y)``."""
    for i, half_w in enumerate((7.0, 4.5, 2.0)):
        yy = y + i * 3.5
        parts.append(
            f'<line x1="{x - half_w:.1f}" y1="{yy:.1f}" x2="{x + half_w:.1f}"'
            f' y2="{yy:.1f}" stroke="{esc(color)}" stroke-width="1.5"'
            f' stroke-linecap="round"/>'
        )


def _draw_band(parts: list[str], band: TopologyBand, pal: Palette) -> None:
    """Row header: a dot in the row's colour, the collapse toggle, the rail
    name and what drives it."""
    if band.kind == "bridge_group":
        parts.append(_text(band.x, band.y + 11, band.title, fill=pal.fg_dim,
                           size=9, font=MONO))
        return
    y = band.y
    parts.append(
        f'<circle cx="{band.x + 4:.1f}" cy="{y + 11.5:.1f}" r="3.5"'
        f' fill="{esc(pal.band_accent(band.kind))}"/>'
    )
    title_x = band.x + 14
    if band.key:
        # Collapse toggle: ▾ open, ▸ collapsed (the viewer's header click).
        cx, cy = title_x + 4.0, y + 11.5
        tri = (
            f"{cx - 2.5:.1f},{cy - 4:.1f} {cx + 2.5:.1f},{cy:.1f} {cx - 2.5:.1f},{cy + 4:.1f}"
            if band.collapsed else
            f"{cx - 4:.1f},{cy - 2.5:.1f} {cx + 4:.1f},{cy - 2.5:.1f} {cx:.1f},{cy + 2.5:.1f}"
        )
        parts.append(f'<polygon points="{tri}" fill="{esc(pal.fg_dim)}"/>')
        title_x += 13
    parts.append(_text(title_x, y + 15.5, band.title, fill=pal.fg, size=11,
                       weight=600))
    if band.subtitle or band.collapsed:
        sub = band.subtitle + ("   (collapsed)" if band.collapsed else "")
        sub_x = title_x + estimate_text_width(band.title, 11) + 12
        # An undriven rail's note is a warning, not a part description.
        parts.append(_text(sub_x, y + 15.5, sub.strip(), fill=pal.fg_dim, size=10,
                           italic=band.kind == "undriven"))


def _row_panels(model: TopologyModel) -> list[tuple[float, float, float, float]]:
    """``(x, y, w, h)`` of the panel behind each row: from just above its
    header to just below the lowest thing drawn in it."""
    rows = [b for b in model.bands if b.kind != "bridge_group"]
    if not rows:
        return []
    content_end = model.height - 52.0 - 20.0 * len(model.notes)
    bottoms: list[float] = []
    for n in model.nodes:
        bottoms.append(n.y + n.height)
        for p in n.ports:
            if p.voltage is not None:
                bottoms.append(p.y + 17.0)
    bottoms += [gy + 9.0 for _, gy in model.gnd_glyphs]
    bottoms += [t.y + t.height / 2 for t in model.tags]
    tops = [b.y for b in rows] + [content_end + 14.0]
    out = []
    for i, band in enumerate(rows):
        y0, limit = band.y - 6.0, tops[i + 1] - 8.0
        inside = [v for v in bottoms if band.y < v <= tops[i + 1]]
        y1 = min(limit, max(inside, default=band.y + 18.0) + 12.0)
        x0 = band.x - 10.0
        out.append((x0, y0, band.x + band.width + 10.0 - x0, y1 - y0))
    return out


_ROWS_LEGEND_ROLES = ("REGULATOR", "SOURCE", "SINK", "SERIES", "BRIDGE")
_ROWS_LEGEND_TAGS = (
    ("net", "power port"),
    ("ref", "regulator port"),
    ("undriven", "undriven"),
    ("end", "no loads"),
)


def rows_legend_width() -> float:
    """Widest line of the rows-layout legend (the model is made at least this wide)."""
    roles = sum(10.0 + 4 + estimate_text_width(n, 10) + 16 for n in _ROWS_LEGEND_ROLES)
    tags = sum(26 + estimate_text_width(t, 10) + 16 for _, t in _ROWS_LEGEND_TAGS)
    return 12.0 + max(roles, tags + 20 + estimate_text_width("ground", 10))


def _draw_rows_legend(parts: list[str], y: float, pal: Palette) -> None:
    x = 12.0
    for name in _ROWS_LEGEND_ROLES:
        parts.append(
            f'<rect x="{x:.1f}" y="{y - 10:.1f}" width="10" height="10"'
            f' rx="3" fill="{esc(pal.role(name))}"/>'
        )
        parts.append(_text(x + 14, y, name, fill=pal.fg_dim, size=10))
        x += 10 + 4 + estimate_text_width(name, 10) + 16
    x = 12.0
    y += 18.0
    for kind, text in _ROWS_LEGEND_TAGS:
        _draw_tag(parts, TopologyTag(net="", text="", kind=kind, x=x, y=y - 5,
                                     width=21.0, height=11.0), pal)
        parts.append(_text(x + 26, y, text, fill=pal.fg_dim, size=10))
        x += 26 + estimate_text_width(text, 10) + 16
    _draw_local_gnd(parts, x + 7, y - 9, pal.gnd)
    parts.append(_text(x + 20, y, "ground", fill=pal.fg_dim, size=10))


def _render_rows(parts: list[str], model: TopologyModel, pal: Palette) -> None:
    for x, y, w, h in _row_panels(model):
        parts.append(
            f'<rect class="topo-row" x="{x:.1f}" y="{y:.1f}" width="{w:.1f}"'
            f' height="{h:.1f}" rx="10" fill="{esc(pal.lane_fill)}"'
            f' stroke="{esc(pal.lane_stroke)}"/>'
        )
    for band in model.bands:
        _draw_band(parts, band, pal)
    _draw_wires_schematic(parts, model.wires, pal)
    for gx, gy in model.gnd_glyphs:
        _draw_local_gnd(parts, gx, gy, pal.gnd)
    for node in model.nodes:
        _draw_node(parts, node, pal)
    for tag in model.tags:
        _draw_tag(parts, tag, pal)
    for wire in model.wires:
        _draw_wire_label(parts, wire, pal)
    if model.port_voltages:
        from fypa.topology.gauges import draw_port_voltage

        for node in model.nodes:
            for port in node.ports:
                draw_port_voltage(parts, port, bg=pal.bg, fg=pal.fg,
                                  surface=pal.surface, border=pal.border)
    y = model.height - 52.0 - 20.0 * len(model.notes)
    for note in model.notes:
        y += 20.0
        parts.append(_text(12, y - 6, note, fill=pal.fg_dim, size=10, italic=True))
    _draw_rows_legend(parts, model.height - 30, pal)


def _dot_grid(parts: list[str], w: float, h: float, pal: Palette) -> None:
    parts.append(
        f'<defs><pattern id="topo-grid" width="{GRID_STEP:g}" height="{GRID_STEP:g}"'
        f' patternUnits="userSpaceOnUse"><circle cx="{GRID_STEP / 2:g}"'
        f' cy="{GRID_STEP / 2:g}" r="0.9" fill="{esc(pal.grid_dot)}"/></pattern></defs>'
        f'<rect width="{w:.0f}" height="{h:.0f}" fill="url(#topo-grid)"/>'
    )


def render_topology_svg(
    model: TopologyModel,
    *,
    theme: dict[str, str] | None = None,
    width: float | None = None,
    background: bool = True,
    grid: bool = False,
) -> str:
    """Render the topology model as an SVG document string.

    ``background=False`` leaves the canvas transparent: the viewer paints its
    own background (and dot grid) behind the diagram. ``grid`` draws the dot
    grid into the SVG itself, for a standalone file.
    """
    pal = palette(theme)
    w = width if width is not None else model.width
    h = model.height

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.0f}"'
        f' height="{h:.0f}" viewBox="0 0 {w:.0f} {h:.0f}">',
    ]
    if background:
        parts.append(f'<rect width="100%" height="100%" fill="{esc(pal.bg)}"/>')
    if grid:
        _dot_grid(parts, w, h, pal)

    if model.layout == "rows":
        _render_rows(parts, model, pal)
        parts.append("</svg>")
        return "\n".join(parts)

    _draw_wires_schematic(
        parts, model.wires, pal,
        gnd_symbol_x=model.gnd_symbol_x, gnd_bus_y=model.gnd_bus_y,
    )
    if model.gnd_symbol_x is not None and model.gnd_bus_y is not None:
        _draw_gnd_symbol(parts, model.gnd_symbol_x, model.gnd_bus_y,
                         stroke=pal.gnd, fg=pal.fg)
    for node in model.nodes:
        _draw_node(parts, node, pal)
    for wire in model.wires:
        _draw_wire_label(parts, wire, pal)
    _draw_legend(parts, h - 16, pal)
    parts.append("</svg>")
    return "\n".join(parts)
