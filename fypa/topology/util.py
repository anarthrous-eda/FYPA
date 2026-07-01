"""Formatting helpers for topology tooltips and labels."""

from __future__ import annotations

import html
import re

from fypa.topology.constants import LABEL_MAX_LEN, OMEGA


def _esc(text: str) -> str:
    return html.escape(str(text), quote=True)


def truncate_label(label: str, *, max_len: int = LABEL_MAX_LEN) -> str:
    """Truncate display text with an ellipsis when longer than ``max_len``."""
    if len(label) <= max_len:
        return label
    return label[: max_len - 1] + "…"


def _fmt_compact(value: float) -> str:
    """Decimal string without scientific notation; trim trailing zeros."""
    if value == 0:
        return "0"
    s = f"{value:.4g}"
    if "e" in s.lower():
        s = f"{value:.6g}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def _format_resistance_ohm(ohms: float) -> str:
    ar = abs(ohms)
    if ar == 0:
        return f"0 {OMEGA}"
    if ar >= 1e6:
        return f"{_fmt_compact(ar / 1e6)} M{OMEGA}"
    if ar >= 1e3:
        return f"{_fmt_compact(ar / 1e3)} k{OMEGA}"
    if ar >= 1:
        return f"{_fmt_compact(ar)} {OMEGA}"
    if ar >= 1e-3:
        return f"{_fmt_compact(ar * 1e3)} m{OMEGA}"
    return f"{_fmt_compact(ar * 1e6)} \u03bc{OMEGA}"


def _format_current_a(amps: float) -> str:
    aa = abs(amps)
    if aa == 0:
        return "0 A"
    if aa >= 1:
        return f"{_fmt_compact(aa)} A"
    if aa >= 1e-3:
        return f"{_fmt_compact(aa * 1e3)} mA"
    return f"{_fmt_compact(aa * 1e6)} \u03bcA"


def _format_voltage_v(volts: float) -> str:
    av = abs(volts)
    if av == 0:
        return "0 V"
    if av >= 1:
        return f"{_fmt_compact(av)} V"
    if av >= 1e-3:
        return f"{_fmt_compact(av * 1e3)} mV"
    return f"{_fmt_compact(av * 1e6)} \u03bcV"


def _format_directive_value(directive: dict) -> str:
    """Compact value for tooltips (Ω, mA, V — no scientific notation)."""
    role = str(directive.get("role", ""))
    value = directive.get("value")
    if value is not None:
        v = float(value)
        unit = str(directive.get("unit", ""))
        if role in ("RESISTOR", "SERIES") or unit == "Ohm":
            return _format_resistance_ohm(v)
        if role == "SINK" or unit == "A":
            return _format_current_a(v)
        if unit == "V":
            return _format_voltage_v(v)
    raw = str(directive.get("value_str", "")).strip()
    if not raw:
        return ""
    return _reformat_legacy_value_str(raw)


def _reformat_legacy_value_str(text: str) -> str:
    """Best-effort cleanup when only ``value_str`` is available."""
    text = text.strip()
    m = re.match(
        r"^([\d.eE+\-]+)\s*mOhm$", text, re.IGNORECASE,
    )
    if m:
        return _format_resistance_ohm(float(m.group(1)) * 1e-3)
    m = re.match(r"^([\d.eE+\-]+)\s*mA$", text, re.IGNORECASE)
    if m:
        return _format_current_a(float(m.group(1)) * 1e-3)
    m = re.match(r"^([\d.eE+\-]+)\s*V$", text, re.IGNORECASE)
    if m:
        return _format_voltage_v(float(m.group(1)))
    return text.replace("Ohm", OMEGA)


format_directive_value = _format_directive_value
reformat_legacy_value_str = _reformat_legacy_value_str
esc = _esc
fmt_compact = _fmt_compact
format_current_a = _format_current_a
