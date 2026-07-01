"""Terminal role classification for topology layout and metadata."""

from __future__ import annotations


def _terminal_matches(base: str, terminal: str) -> bool:
    """True for ``base`` or ``base`` + digits (e.g. P, P1, IN_P2)."""
    if terminal == base:
        return True
    suffix = terminal[len(base):]
    return terminal.startswith(base) and suffix.isdigit()


def is_power_input_port(role: str, terminal: str) -> bool:
    """Load-side inputs that need an upstream PDN driver on their net."""
    if role == "SINK":
        return _terminal_matches("P", terminal)
    if role == "REGULATOR":
        return _terminal_matches("IN_P", terminal)
    return False


def is_output_port(role: str, terminal: str, side: str) -> bool:
    """Ports that drive left-to-right column placement (power flow, not returns)."""
    del side  # reserved for future side-aware rules
    if role == "SOURCE":
        return _terminal_matches("P", terminal)
    if role == "REGULATOR":
        return _terminal_matches("OUT_P", terminal)
    if role in ("RESISTOR", "SERIES"):
        return _terminal_matches("N", terminal)
    return False
