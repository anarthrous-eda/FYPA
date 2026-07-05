"""GND net name aliases — import-light (no pipeline dependencies)."""

from __future__ import annotations

# Net names that are unconditionally ground. Deliberately does NOT include
# "vss": plenty of real boards carry a negative rail named VSS (e.g. −5 V),
# and drawing it merged with GND would invent a connection that doesn't
# exist. A VSS net merges with GND only through the electrical rail
# grouping (a RESISTOR/SERIES bridge onto a GND-grouped net); otherwise it
# draws as its own rail and ``validate_topology`` emits a warning so the
# user knows it was not assumed to be ground.
GND_ALIASES = frozenset({"0v", "gnd", "ground"})

# Ground-*looking* names that must not be merged by name alone (see above).
CONDITIONAL_GND_NAMES = frozenset({"vss"})


def is_gnd_alias(net: str) -> bool:
    return net.lower() in GND_ALIASES


def is_conditional_gnd_name(net: str) -> bool:
    """True for nets that read like ground but are only ground when
    electrically tied to a GND-grouped net (e.g. ``VSS``)."""
    return net.lower() in CONDITIONAL_GND_NAMES
