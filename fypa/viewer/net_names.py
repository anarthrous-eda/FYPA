"""Heuristics for recognising supply and ground nets by name."""
from __future__ import annotations

import re


# Net names that read as a ground / return rather than a supply. Bridging two
# of these is routine (AGND to GND through a ferrite is a standard layout);
# bridging two different *supplies* almost never is, so the Bridges tab warns
# only in the latter case.
_GROUND_NET_TOKENS: frozenset[str] = frozenset({
    "GND", "GROUND", "AGND", "DGND", "PGND", "SGND", "EGND", "CHASSIS",
    "EARTH", "VSS", "VSSA", "COM", "RTN", "RETURN", "0V",
})




def _supply_net_key(name: str) -> str:
    """Normalised identity of the supply a net name refers to.

    ``+3V3``, ``3V3``, ``+3V3_SW`` and ``3v3-filt`` are all the same supply
    seen at different points, so bridging them is not worth a warning. The
    key strips polarity, separators and the common post-regulation suffixes.
    """
    key = re.sub(r"[^A-Z0-9]", "", str(name).upper())
    for suffix in ("SW", "FILT", "FILTERED", "SENSE", "SNS", "IN", "OUT",
                   "A", "D", "F"):
        if len(key) <= len(suffix) + 1 or not key.endswith(suffix):
            continue
        stripped = key[: -len(suffix)]
        # Only strip when what is left still reads as a supply. Otherwise
        # "VDD" loses its final D to the analog/digital suffix rule and
        # becomes "VD", which no longer matches "VDDA" -> "VDD" — so the
        # commonest paired analog supply on any board looks like two
        # different rails and warns on a textbook ferrite.
        if not _looks_like_supply_net(stripped):
            continue
        key = stripped
        break
    return key




def _looks_like_supply_net(name: str) -> bool:
    """True when a net name reads as a power rail rather than a return.

    Deliberately conservative: it must NOT look like a ground, and must
    carry a voltage-ish token (``3V3``, ``VCC``, ``VDD``, ``+5``). Anything
    unrecognised returns False, so an unusual naming scheme produces no
    warning rather than a false one.
    """
    raw = str(name).strip().upper()
    if not raw:
        return False
    squashed = re.sub(r"[^A-Z0-9]", "", raw)
    if not squashed:
        return False
    if squashed in _GROUND_NET_TOKENS:
        return False
    if any(squashed.startswith(g) or squashed.endswith(g)
           for g in ("GND", "VSS", "AGND", "DGND", "PGND")):
        return False
    # 3V3 / 1V8 / 12V style, or an explicit supply prefix.
    if re.search(r"\d+V\d*", squashed):
        return True
    return bool(re.match(r"^(VCC|VDD|VBAT|VBUS|VIN|VOUT|VREF|VVDD|PWR|\+)",
                         raw.replace(" ", "")))
