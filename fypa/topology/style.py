"""Colours for the Topology diagram ("tonal" blocks on "soft" wiring).

One :class:`Palette` per render, resolved from the viewer's theme tokens, so
the dark viewer, the light viewer and the report's white page each get role
and wire colours tuned for their background. Role colours are softer than the
old saturated ones: on a whole board they tint a block's header and outline
instead of filling it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fypa.topology.constants import GND_NET

# Role hues, tuned per background. SERIES and RESISTOR are the same part.
_ROLES = {
    "dark": {
        "SOURCE": "#e8644f", "SINK": "#4d9fec", "RESISTOR": "#42b883",
        "SERIES": "#42b883", "REGULATOR": "#c77ddf", "AUTO_BRIDGE": "#8791a1",
        "BRIDGE": "#8791a1",
    },
    "light": {
        "SOURCE": "#cf4631", "SINK": "#1f73d6", "RESISTOR": "#1f8f5c",
        "SERIES": "#1f8f5c", "REGULATOR": "#a545c4", "AUTO_BRIDGE": "#6b7483",
        "BRIDGE": "#6b7483",
    },
}
# Single-net (ideal-return) sources and sinks: a darker shade of the role, so
# a part that only touches its rail reads differently from one with a return.
_SINGLE_NET = {
    "dark": {"SOURCE": "#b8503f", "SINK": "#3a7cbd"},
    "light": {"SOURCE": "#9c3424", "SINK": "#16569f"},
}
_WIRES = {
    "dark": {"power": "#ef6f5e", "gnd": "#58a96f", "undriven": "#e0a030",
             "end": "#8a8f98"},
    "light": {"power": "#d04a37", "gnd": "#2f8a4c", "undriven": "#a86a00",
              "end": "#7b818c"},
}


def _rgb(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def mix(a: str, b: str, t: float) -> str:
    """``t`` parts of colour ``a`` over ``1 - t`` of ``b``, as ``#rrggbb``."""
    ra, rb = _rgb(a), _rgb(b)
    return "#" + "".join(
        f"{round(x * t + y * (1 - t)):02x}" for x, y in zip(ra, rb))


def luminance(color: str) -> float:
    r, g, b = _rgb(color)
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0


@dataclass
class Palette:
    """Every colour one diagram render uses."""

    dark: bool
    bg: str
    surface: str        # block bodies
    fg: str
    fg_dim: str
    border: str
    err: str
    accent: str
    power: str          # non-ground rail wires
    gnd: str            # ground wires, ports and symbols
    roles: dict[str, str] = field(default_factory=dict)
    single: dict[str, str] = field(default_factory=dict)
    undriven: str = ""
    end: str = ""
    warn: str = "#ffb84d"   # a block with a design warning

    def role(self, role: str, *, single_net: bool = False) -> str:
        if single_net and role in self.single:
            return self.single[role]
        return self.roles.get(role, self.fg_dim)

    def role_text(self, color: str) -> str:
        """The role word on a tinted header: the role hue, nudged toward
        the text colour so it reads at 8 px."""
        return mix("#ffffff", color, 0.3) if self.dark else mix("#000000", color, 0.22)

    def wire(self, net: str) -> str:
        return self.gnd if net == GND_NET else self.power

    def tag(self, kind: str) -> str:
        return {
            "net": self.power,
            "feed": self.power,
            "ref": self.roles["REGULATOR"],
            "undriven": self.undriven,
            "end": self.end,
            "add": self.roles["SINK"],   # "+ SINK (n)" handle (viewer only)
        }.get(kind, self.fg)

    def band_accent(self, kind: str) -> str:
        return {
            "source": self.roles["SOURCE"],
            "regulator": self.roles["REGULATOR"],
            "undriven": self.undriven,
        }.get(kind, self.fg_dim)

    # Derived tones -------------------------------------------------------------

    def header_fill(self, color: str) -> str:
        return mix(color, self.surface, 0.22)

    def header_rule(self, color: str) -> str:
        return mix(color, self.surface, 0.6)

    def outline(self, color: str) -> str:
        return mix(color, self.border, 0.35)

    def chip_fill(self, color: str) -> str:
        return mix(color, self.surface, 0.18)

    def chip_outline(self, color: str) -> str:
        return mix(color, self.surface, 0.55)

    @property
    def lane_fill(self) -> str:
        return mix(self.fg, self.bg, 0.035)

    @property
    def lane_stroke(self) -> str:
        return mix(self.fg, self.bg, 0.1)

    @property
    def grid_dot(self) -> str:
        return mix(self.fg, self.bg, 0.16)


def palette(theme: dict[str, str] | None) -> Palette:
    """The palette for ``theme`` (viewer theme tokens; missing ones default
    to the dark viewer's)."""
    theme = theme or {}
    bg = theme.get("bg", "#2b2b2b")
    bg_alt = theme.get("bg_alt", "#333333")
    dark = luminance(bg) < 0.5
    mode = "dark" if dark else "light"
    if dark:
        surface = bg_alt
    else:
        # Blocks sit lighter than the canvas; on a white page, a touch darker.
        surface = "#ffffff" if luminance(bg) < 0.985 else bg_alt
    w = _WIRES[mode]
    return Palette(
        dark=dark,
        bg=bg,
        surface=surface,
        fg=theme.get("fg", "#e6e6e6"),
        fg_dim=theme.get("fg_dim", "#909090"),
        border=theme.get("border", "#555555"),
        err=theme.get("err", "#ff7070"),
        accent=theme.get("accent", "#b8d4ff"),
        power=w["power"],
        gnd=w["gnd"],
        roles=dict(_ROLES[mode]),
        single=dict(_SINGLE_NET[mode]),
        undriven=w["undriven"],
        end=w["end"],
        warn=theme.get("warn", "#ffb84d"),
    )
