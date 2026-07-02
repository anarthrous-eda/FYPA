"""GND net name aliases — import-light (no pipeline dependencies)."""

from __future__ import annotations

GND_ALIASES = frozenset({"0v", "gnd", "ground", "vss"})


def is_gnd_alias(net: str) -> bool:
    return net.lower() in GND_ALIASES
