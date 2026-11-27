"""Voltage gauges on the Topology tab's SINK and regulator-input blocks.

Only loads that declare a minimum get one: a SINK with ``PDN_MIN_V``, a
regulator input with a dropout or ``PDN_VIN_MIN``. It sits under the block's
port rows, in one of two styles:

``"bar"`` (three lines)
    * a slim track shaded by zone: green where the voltage is allowed
      (minimum to maximum, or to the end with no maximum), faded red
      outside it, with a tick at each limit;
    * a caret above it at the supply's setpoint, or a bracket over its range
      (``PDN_V_TOL`` / ``PDN_V_MIN`` / ``PDN_V_MAX``), before any drop;
    * a dot (or a capsule over that range) where the voltage at the load's
      pins sits - so the gap to the caret is the drop in the copper;
    * ``min`` / ``max`` / ``supply`` values printed under their marks;
    * the voltage at the pins and the margin, e.g. ``0.975 V  ✓ +74.5 mV to
      min``.

``"text"`` (two lines)
    The same voltage and margin line, then ``min 0.9 V · supply 1 V``.

Every bar is scaled to its own window - the minimum at :data:`LO_FRAC`, the
maximum (or, with none, the top of the supply's range) at :data:`HI_FRAC` -
so healthy gauges all look alike and an outlier stands out. A value off the
bar is pinned to its end with an arrow.

The voltage is the design report's "voltage at load": lowest supply pin minus
highest return pin, so ground rise counts. Layout (which blocks get a gauge)
is from the setup metadata alone; the readings come from the last solve and
are attached with :func:`apply_readings`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from fypa.rail_currents import to_float
from fypa.topology.types import GaugeReading, NodeGauge, TopologyModel
from fypa.topology.util import esc, estimate_text_width, truncate_text_to_width
from fypa.worst_case import regulator_input_minimum

STYLES = ("bar", "text")

# A block with a gauge is this wide (others NODE_W): room for the text
# style's "min 0.955 V · max 1.04 V · supply 0.97–1.05 V".
GAUGE_NODE_W = 184.0
GAUGE_INSET = 8.0       # gauge inset from the block's left / right edge
BAR_ROW_H = 12.0        # the bar itself
MARGIN_ROW_H = 10.0     # bar style: the margin line above the bar
TICK_ROW_H = 11.0       # min / max / supply values under the bar
LINE_H = 13.0           # one text line
TRACK_H = 4.0          # the slim track
VALUE_FONT = 8.0
MARGIN_FONT = 6.0       # bar style's margin line (whole px: Qt rounds)
SMALL_FONT = 7.0
LO_FRAC = 0.18          # minimum's position along the bar
HI_FRAC = 0.82          # maximum's (or the supply top's) position

LIMIT_COLOR = "#2ca92c"
WARN_COLOR = "#e0a030"
_FONT = "Segoe UI,sans-serif"
_VERDICT_MARK = {"PASS": "✓", "WARN": "⚠", "FAIL": "✗"}


def gauge_row_h(style: str) -> float:
    """Body height one gauge takes in ``style``."""
    return 2 * LINE_H if style == "text" else MARGIN_ROW_H + BAR_ROW_H + TICK_ROW_H


@dataclass(frozen=True)
class GaugeSpec:
    key: str
    label: str
    min_v: float
    max_v: float | None


def base_terminal(pname: str) -> str:
    m = re.match(r"^(IN_P|OUT_P|IN_N|OUT_N|P|N)(\d+)?$", pname)
    return m.group(1) if m else pname


def gauge_specs(spec: Mapping) -> list[GaugeSpec]:
    """One :class:`GaugeSpec` per load in a node spec that declares a
    minimum: a SINK with ``PDN_MIN_V``, a REGULATOR with a dropout or
    ``PDN_VIN_MIN``. A maximum alone gets no gauge."""
    port_dirs = spec.get("port_directives") or {}
    out: list[GaugeSpec] = []
    for d in spec.get("directives") or []:
        role = d.get("role")
        if role == "SINK":
            lo, hi = to_float(d.get("min_voltage")), to_float(d.get("max_voltage"))
            term = "P"
        elif role == "REGULATOR":
            need = regulator_input_minimum(d)
            lo, hi = (need[0] if need else None), to_float(d.get("vin_max"))
            term = "IN_P"
        else:
            continue
        if lo is None:
            continue
        if hi is not None and hi <= lo:
            hi = None
        label = str(d.get("label") or d.get("designator") or "?")
        port = next((p for p, pd in port_dirs.items()
                     if pd is d and base_terminal(p) == term), "")
        out.append(GaugeSpec(f"{role}:{label}", port or term, lo, hi))
    if len(out) == 1:
        out[0] = GaugeSpec(out[0].key, "", out[0].min_v, out[0].max_v)
    return out


# --- readings -------------------------------------------------------------------


def _fmt_mv(v: float, signed: bool = False) -> str:
    sign = "+" if signed and v >= 0 else ""
    if abs(v) < 1.0:
        return f"{sign}{v * 1e3:.3g} mV"
    return f"{sign}{fmt_volts(v)} V"


def fmt_volts(v: float) -> str:
    """To the millivolt, without trailing zeros: ``0.951``, ``0.99``, ``1``."""
    text = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _fmt_range(lo: float, hi: float) -> str:
    if abs(hi - lo) < 5e-4:
        return f"{fmt_volts(lo)} V"
    return f"{fmt_volts(lo)}–{fmt_volts(hi)} V"


def _fv(v: float) -> str:
    """A limit or setpoint as written: ``0.9``, ``0.955``, ``3.3``."""
    return f"{v:.4g}"


def reading_for(load, rail, *, warn_pct: float = 1.0) -> GaugeReading | None:
    """A :class:`GaugeReading` from a report ``LoadResult`` and its
    ``RailReport``; ``None`` when the load's pins were not solved."""
    if load.v_load is None:
        return None
    low = load.v_low if load.v_low is not None else load.v_load
    high = load.v_high if load.v_high is not None else load.v_load
    low, high = min(low, high, load.v_load), max(low, high, load.v_load)
    margin = load.limiting_margin_v
    limit = ("max" if margin is not None and load.margin_high_v is not None
             and margin == load.margin_high_v else "min")
    setpoint = rail.nominal_v
    if margin is None:
        status = "PASS"
    elif margin < 0:
        status = "FAIL"
    elif setpoint and margin < setpoint * warn_pct / 100:
        status = "WARN"
    else:
        status = "PASS"
    src_low = rail.source_min_v if rail.source_min_v is not None else setpoint
    src_high = rail.source_max_v if rail.source_max_v is not None else setpoint

    lines = [f"{load.label}: {_fmt_range(low, high)} at its pins"
             + (" over the supply's range" if high - low >= 5e-4 else "")]
    lines.append("(supply pins minus return pins, so ground rise counts)")
    if high - low >= 5e-4:
        lines.append(f"{fmt_volts(load.v_load)} V with the supply at nominal")
    allowed = f"Allowed: min {load.min_v:.4g} V" if load.min_v is not None \
        else "Allowed:"
    if load.min_basis:
        allowed += f" ({load.min_basis})"
    if load.max_v is not None:
        allowed += f", max {load.max_v:.4g} V"
    lines.append(allowed)
    if margin is not None:
        word = {"PASS": "pass", "WARN": "low margin", "FAIL": "FAIL"}[status]
        lines.append(f"Worst margin: {_fmt_mv(margin, signed=True)} to the "
                     f"{limit}imum ({word})")
    if setpoint is not None:
        supply = f"Supply {setpoint:.4g} V"
        if (src_low is not None and src_high is not None
                and src_high - src_low >= 5e-4):
            supply += f" (range {src_low:.4g}–{src_high:.4g} V)"
        if load.drop_v is not None:
            supply += f"; drop {_fmt_mv(load.drop_v)}"
            if load.return_drop_v:
                supply += f", {_fmt_mv(load.return_drop_v)} of it in the return"
        lines.append(supply)
    if load.worst_pin:
        lines.append(f"Lowest pin {load.worst_pin}"
                     + (f" on {load.worst_layer}" if load.worst_layer else ""))
    return GaugeReading(
        low=low, high=high, nominal=load.v_load, margin=margin, status=status,
        setpoint=setpoint, src_low=src_low, src_high=src_high, limit=limit,
        tooltip="\n".join(lines),
    )


def readings_from_loads(pairs: Iterable, *, warn_pct: float = 1.0,
                        ) -> dict[str, GaugeReading]:
    """``{LoadResult.key: reading}`` from ``fypa.report.collect.load_results``."""
    out: dict[str, GaugeReading] = {}
    for load, rail in pairs:
        if not load.key:
            continue
        r = reading_for(load, rail, warn_pct=warn_pct)
        if r is not None:
            out[load.key] = r
    return out


def apply_readings(model: TopologyModel,
                   readings: Mapping[str, GaugeReading] | None,
                   *, ports: Mapping[str, float] | None = None,
                   stale: bool = False) -> None:
    """Attach the solved readings to the model's gauges, and the per-port
    voltages (``"ROLE:label:TERM"`` -> V) to its ports (in place). With
    ``model.port_voltages`` on, a bar gauge leaves the voltage to the port."""
    for node in model.nodes:
        for g in node.gauges:
            g.reading = None if readings is None else readings.get(g.key)
            g.stale = stale
            g.show_value = not model.port_voltages
        for p in node.ports:
            v = None if ports is None or not p.vkey else ports.get(p.vkey)
            p.voltage = v
            p.voltage_stale = stale
            if v is not None:
                line = (f"{port_voltage_text(v)} at these pins"
                        " (minus their return)")
                p.tooltip = f"{p.tooltip}\n{line}" if p.tooltip else line


# --- drawing ----------------------------------------------------------------------


def _scale(g: NodeGauge) -> tuple[float, float]:
    """(volts at LO_FRAC, volts at HI_FRAC) for this gauge."""
    lo = g.min_v
    if g.max_v is not None:
        return lo, g.max_v
    r = g.reading
    hi = None
    if r is not None:
        hi = max(v for v in (r.src_high, r.setpoint, r.high) if v is not None)
    if hi is None or hi <= lo + 1e-6:
        hi = lo + max(abs(lo) * 0.1, 0.05)
    return lo, hi


def _text(parts: list[str], x: float, y: float, text: str, *, color: str,
          size: float, anchor: str = "start", bold: bool = False) -> None:
    weight = ' font-weight="600"' if bold else ""
    parts.append(
        f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}"'
        f' fill="{esc(color)}" font-family="{_FONT}" font-size="{size:g}"'
        f'{weight}>{esc(text)}</text>')


def _verdict_color(status: str, err: str) -> str:
    return {"FAIL": err, "WARN": WARN_COLOR}.get(status, LIMIT_COLOR)


def _value_line(parts: list[str], g: NodeGauge, top: float, *, fg: str,
                fg_dim: str, err: str) -> None:
    """``[P1] 0.975 V            ✓ +74.5 mV to min``."""
    base = top + 9.5
    prefix = f"{g.label} " if g.label else ""
    r = g.reading
    if r is None:
        _text(parts, g.x, base, prefix + "not solved yet", color=fg_dim,
              size=VALUE_FONT)
        return
    if not g.show_value:
        # The port beside the block shows the voltage: just the margin.
        if r.margin is None:
            return
        text = (f"{prefix}{_VERDICT_MARK[r.status]} "
                f"{_fmt_mv(r.margin, signed=True)} to {r.limit}")
        _text(parts, g.x, base, text, color=_verdict_color(r.status, err),
              size=VALUE_FONT, bold=True)
        return
    left = prefix + _fmt_range(r.low, r.high)
    _text(parts, g.x, base, left, color=fg, size=VALUE_FONT, bold=True)
    if r.margin is None:
        return
    room = g.width - estimate_text_width(left, VALUE_FONT) - 8.0
    mark = _VERDICT_MARK[r.status]
    margin = _fmt_mv(r.margin, signed=True)
    # Drop the words, then the mark, before the number.
    for right in (f"{mark} {margin} to {r.limit}", f"{mark} {margin}", margin):
        if estimate_text_width(right, VALUE_FONT) <= room:
            break
    _text(parts, g.x + g.width, base, right,
          color=_verdict_color(r.status, err), size=VALUE_FONT,
          anchor="end", bold=True)


def _margin_line(parts: list[str], g: NodeGauge, top: float, *,
                 fg_dim: str, err: str) -> None:
    """Bar style, centred above the bar: ``✓ +74.5 mV to min``. Led by the
    voltage at the pins (``0.975 V · …``) unless the port shows it."""
    base = top + 7.5
    cx = g.x + g.width / 2
    prefix = f"{g.label} " if g.label else ""
    r = g.reading
    if r is None:
        _text(parts, cx, base, prefix + "not solved yet", color=fg_dim,
              size=MARGIN_FONT, anchor="middle")
        return
    value = "" if not g.show_value else _fmt_range(r.low, r.high)
    if r.margin is None:
        text = prefix + value
    else:
        mark = _VERDICT_MARK[r.status]
        margin = _fmt_mv(r.margin, signed=True)
        lead = prefix + (f"{value} · " if value else "")
        # Drop the words, then the mark, before the number.
        for text in (f"{lead}{mark} {margin} to {r.limit}",
                     f"{lead}{mark} {margin}", f"{lead}{margin}"):
            if estimate_text_width(text, MARGIN_FONT) <= g.width:
                break
    if not text:
        return
    color = fg_dim if r.margin is None else _verdict_color(r.status, err)
    _text(parts, cx, base, text, color=color, size=MARGIN_FONT,
          anchor="middle", bold=True)


def _limits_line(parts: list[str], g: NodeGauge, top: float, *,
                 fg_dim: str) -> None:
    """``min 0.9 V · max 1.04 V · supply 1 V`` (text style)."""
    bits = [f"min {_fv(g.min_v)} V"]
    if g.max_v is not None:
        bits.append(f"max {_fv(g.max_v)} V")
    r = g.reading
    if r is not None and r.src_low is not None and r.src_high is not None:
        if r.src_high - r.src_low >= 5e-4:
            bits.append(f"supply {_fv(r.src_low)}–{_fv(r.src_high)} V")
        else:
            bits.append(f"supply {_fv(r.src_low)} V")
    text = " · ".join(bits)
    if estimate_text_width(text, SMALL_FONT) > g.width and len(bits) > 1:
        text = " · ".join(bits[:-1])
    text = truncate_text_to_width(text, g.width, SMALL_FONT)
    _text(parts, g.x, top + 9.0, text, color=fg_dim, size=SMALL_FONT)


def _bar(parts: list[str], g: NodeGauge, top: float, *, fg: str,
         fg_dim: str, surface: str, err: str) -> list[tuple[float, str, str]]:
    """The slim track; returns ``(x, text, color)`` labels for under it.

    A 4 px track, faded red outside the allowed window and green inside it,
    with a tick at each limit. Above it, the supply before any drop: a small
    caret, or a bracket over its range. On it, where the pins sit: a dot, or a
    capsule over a range, in the text colour when it passes, amber for a low
    margin, red for what pokes past a limit.
    """
    from fypa.topology.style import mix

    x0, w = g.x, g.width
    yc = top + BAR_ROW_H / 2
    lo_v, hi_v = _scale(g)
    span = hi_v - lo_v

    def px(v: float) -> tuple[float, int]:
        """x of ``v`` on the bar, and -1 / +1 when it is off the low / high
        end (pinned there), else 0."""
        f = LO_FRAC + (HI_FRAC - LO_FRAC) * (v - lo_v) / span
        off = -1 if f < 0.0 else 1 if f > 1.0 else 0
        return x0 + w * min(max(f, 0.0), 1.0), off

    lo_x = px(g.min_v)[0]
    hi_x = px(g.max_v)[0] if g.max_v is not None else x0 + w
    t_top = yc - TRACK_H / 2
    parts.append(
        f'<rect x="{x0:.1f}" y="{t_top:.1f}" width="{w:.1f}"'
        f' height="{TRACK_H:g}" rx="2" fill="{esc(mix(fg, surface, 0.14))}"/>')
    # Zones: red below the minimum (and above a maximum), green between.
    for a, b, color, op in ((x0, lo_x, err, 0.5), (lo_x, hi_x, LIMIT_COLOR, 0.62),
                            (hi_x, x0 + w, err, 0.5)):
        if b - a > 0.05:
            parts.append(
                f'<rect x="{a:.1f}" y="{t_top:.1f}" width="{b - a:.1f}"'
                f' height="{TRACK_H:g}" rx="2" fill="{esc(color)}"'
                f' fill-opacity="{op:g}"/>')
    # Limits: green ticks through the track.
    for v in (g.min_v, g.max_v):
        if v is not None:
            lx, _ = px(v)
            parts.append(
                f'<line x1="{lx:.1f}" y1="{yc - 4.5:.1f}" x2="{lx:.1f}"'
                f' y2="{yc + 4.5:.1f}" stroke="{LIMIT_COLOR}" stroke-width="1.4"'
                f' stroke-linecap="round"/>')

    labels = [(lo_x, f"min {_fv(g.min_v)} V", LIMIT_COLOR)]
    if g.max_v is not None:
        labels.append((hi_x, f"max {_fv(g.max_v)} V", LIMIT_COLOR))

    r = g.reading
    if r is not None:
        # The supply before any drop: a caret, or a bracket over its range.
        if r.src_low is not None and r.src_high is not None:
            sx0, _ = px(r.src_low)
            sx1, _ = px(r.src_high)
            if sx1 - sx0 < 1.0:
                parts.append(
                    f'<path class="topo-gauge-supply" d="M {sx0 - 2.6:.1f},{yc - 7:.1f}'
                    f' h 5.2 l -2.6,3.8 Z" fill="{esc(fg_dim)}"/>')
            else:
                parts.append(
                    f'<path class="topo-gauge-supply" d="M {sx0:.1f},{yc - 4.5:.1f}'
                    f' V {yc - 6.5:.1f} H {sx1:.1f} V {yc - 4.5:.1f}" fill="none"'
                    f' stroke="{esc(fg_dim)}" stroke-width="1.1"/>')
            sv = r.setpoint if r.setpoint is not None else r.src_high
            labels.append((px(sv)[0], f"supply {_fv(sv)} V", fg_dim))
        # Where the pins sit: the verdict colour inside the window, red
        # only for what pokes past a limit.
        ax0, off_lo = px(r.low)
        ax1, off_hi = px(r.high)
        inside = WARN_COLOR if r.status == "WARN" else fg
        if ax1 - ax0 < 3.0:   # no range: a dot
            mid = min(max((ax0 + ax1) / 2, x0 + 1.5), x0 + w - 1.5)
            out = mid < lo_x or (g.max_v is not None and mid > hi_x)
            fill = err if out and r.status == "FAIL" else inside
            parts.append(
                f'<circle class="topo-gauge-pin" cx="{mid:.1f}" cy="{yc:.1f}"'
                f' r="3.6" fill="{esc(fill)}" stroke="{esc(surface)}"'
                f' stroke-width="1.4"/>')
        else:
            cuts = sorted({ax0, ax1, *(c for c in (lo_x, hi_x) if ax0 < c < ax1)})
            for a, b in zip(cuts, cuts[1:]):
                m = (a + b) / 2
                out = m < lo_x or (g.max_v is not None and m > hi_x)
                fill = err if out and r.status == "FAIL" else inside
                parts.append(
                    f'<rect class="topo-gauge-pin" x="{a:.1f}" y="{yc - 3.5:.1f}"'
                    f' width="{b - a:.1f}" height="7" rx="3.5" fill="{esc(fill)}"'
                    f' stroke="{esc(surface)}" stroke-width="1"/>')
        color = _verdict_color(r.status, err) if r.status != "PASS" else inside
        for off, edge, sign in ((off_lo == -1, x0, -1),
                                (off_hi == 1, x0 + w, 1)):
            if off:   # off the scale: arrow at that end
                tip = edge + sign * 4.0
                parts.append(
                    f'<polygon points="{edge:.1f},{yc - 3.5:.1f} {tip:.1f},{yc:.1f}'
                    f' {edge:.1f},{yc + 3.5:.1f}" fill="{esc(color)}"/>')
    return labels


def _tick_labels(parts: list[str], g: NodeGauge,
                 labels: list[tuple[float, str, str]], top: float) -> None:
    """Values under their marks, in priority order (min, max, supply); one
    that would overlap a placed label, or run off the bar, is left out."""
    placed: list[tuple[float, float]] = []
    for x, text, color in labels:
        tw = estimate_text_width(text, SMALL_FONT)
        a = min(max(x - tw / 2, g.x), g.x + g.width - tw)
        b = a + tw
        if any(a < pb + 4 and b > pa - 4 for pa, pb in placed):
            continue
        placed.append((a, b))
        _text(parts, a, top + 8.0, text, color=color, size=SMALL_FONT)


def draw_gauge(parts: list[str], g: NodeGauge, *, bg: str, fg: str,
               fg_dim: str, border: str, err: str,
               surface: str | None = None) -> None:
    """Draw ``g``; ``surface`` is the block body behind it (``bg`` when
    not given)."""
    surface = surface or bg
    opacity = ' opacity="0.45"' if g.stale else ""
    parts.append(f'<g class="topo-gauge"{opacity}>')
    if g.style == "text":
        _value_line(parts, g, g.y, fg=fg, fg_dim=fg_dim, err=err)
        _limits_line(parts, g, g.y + LINE_H, fg_dim=fg_dim)
    else:
        _margin_line(parts, g, g.y, fg_dim=fg_dim, err=err)
        labels = _bar(parts, g, g.y + MARGIN_ROW_H, fg=fg, fg_dim=fg_dim,
                      surface=surface, err=err)
        _tick_labels(parts, g, labels, g.y + MARGIN_ROW_H + BAR_ROW_H)
    parts.append("</g>")


# --- port voltages ------------------------------------------------------------------

PORT_V_FONT = 8.0
_PORT_V_PAD = 4.5
_PORT_V_GAP = 7.0      # port centre to the label's near edge
# Run of wire one port-voltage label needs beside its port, worst case
# ("-12.345 V", bold, so 10 % over the estimate) plus 4 px clear. The rows
# layout keeps this much on each side of a bus when port voltages are on,
# so the labels at the two ends of a wire never meet.
PORT_V_ROOM = (_PORT_V_GAP + 2 * _PORT_V_PAD + 4.0
               + 1.1 * estimate_text_width("-12.345 V", PORT_V_FONT))


def port_voltage_text(v: float) -> str:
    return f"{fmt_volts(v)} V"


def draw_port_voltage(parts: list[str], port, *, bg: str, fg: str,
                      surface: str | None = None, border: str | None = None,
                      ) -> None:
    """The solved voltage under the wire beside ``port``, on a pill: right
    of an output port, left of an input."""
    if port.voltage is None:
        return
    text = port_voltage_text(port.voltage)
    w = estimate_text_width(text, PORT_V_FONT) + 2 * _PORT_V_PAD
    if port.side == "right":
        x0 = port.x + _PORT_V_GAP
    else:
        x0 = port.x - _PORT_V_GAP - w
    top = port.y + 4.5
    h = PORT_V_FONT + 4.0
    opacity = ' opacity="0.45"' if port.voltage_stale else ""
    edge = f' stroke="{esc(border)}" stroke-width="0.7"' if border else ""
    parts.append(f'<g class="topo-port-v"{opacity}>')
    parts.append(
        f'<rect x="{x0:.1f}" y="{top:.1f}" width="{w:.1f}"'
        f' height="{h:.1f}" rx="{h / 2:g}" fill="{esc(surface or bg)}"{edge}/>')
    _text(parts, x0 + w / 2, top + h - 3.2, text, color=fg,
          size=PORT_V_FONT, anchor="middle", bold=True)
    parts.append("</g>")


def find_gauge_at(model: TopologyModel, x: float, y: float) -> NodeGauge | None:
    """The gauge (all its lines) under ``(x, y)``."""
    for node in reversed(model.nodes):
        for g in node.gauges:
            if g.x - 4 <= x <= g.x + g.width + 4 and g.y <= y <= g.y + g.height:
                return g
    return None
