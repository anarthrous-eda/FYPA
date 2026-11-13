"""Net-name rules for auto setup: ground, rail voltage, pin-name classes."""

from __future__ import annotations

import re

# Power-port symbol styles that mean ground (altium_monkey PowerObjectStyle):
# GND_POWER, GND_SIGNAL, GND_EARTH, GOST_GND_POWER, GOST_GND_EARTH.
GND_PORT_STYLES = frozenset({4, 5, 6, 8, 9})

_GND_RE = re.compile(
    r"^(?:[ADPSCE]|SYS|CHASSIS|EARTH)?_?GND(?:[_\-]?\w*)?$|^0V$|^GROUND\w*$|^VSS$",
    re.IGNORECASE,
)

# Name tokens that mark a control / switching net even when the name carries
# a voltage ("SW_P3V3", "FB_P1V8", "1V8D_REG_EN" are not rails).
_NON_RAIL_TOKENS = frozenset({
    "SW", "LX", "PH", "FB", "VFB", "EN", "ENA", "PG", "PGOOD", "PGD", "BST",
    "BOOT", "SNS", "SENSE", "SS", "TR", "COMP", "RT", "ILIM", "TRK", "MODE",
    "SYNC", "REG", "CTRL", "CTL", "ON", "OFF", "SHDN", "nEN", "RST", "RESET",
    "FLT", "FAULT", "ALERT", "SCL", "SDA", "CLK", "DATA", "TX", "RX", "ADJ",
})


def is_ground(name: str, port_styles: set[int] | frozenset[int] = frozenset()) -> bool:
    """Ground by power-port symbol style, or by name (GND, AGND, PGND_x, 0V…)."""
    if port_styles & GND_PORT_STYLES:
        return True
    return bool(name) and bool(_GND_RE.match(name.strip()))


def _tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[_\-\s./]+", name.upper()) if t]


def has_control_token(name: str) -> bool:
    """True for a control / switching net. A trailing ``SW`` is the
    exception: ``+3V3_SW`` is a *switched* rail, while ``SW_P3V3`` is a
    converter's switch node."""
    toks = _tokens(name)
    if len(toks) > 1 and toks[-1] == "SW":
        toks = toks[:-1]
    return any(t in _NON_RAIL_TOKENS for t in toks)


_V_SPLIT = re.compile(r"(?<![A-Z0-9])P?(\d{1,3})V(\d{1,3})(?!\d)")       # 1V8, 3V3, P1V8, 12V0
_P_SPLIT = re.compile(r"(?<![A-Z0-9])(\d{1,3})P(\d{1,3})V?(?![0-9])")       # 12P0V, 1P8
_PLAIN_V = re.compile(r"(?<![A-Z0-9.])(\d{1,3}(?:\.\d{1,3})?)V(?![A-Z0-9])")  # +12V, 5V, 3.3V


def rail_voltage(name: str) -> float | None:
    """Voltage spelled in a rail name: ``+1V8``→1.8, ``12P0V``→12, ``+5V``→5.

    ``None`` when the name carries no voltage, or when it is a control /
    switching net that merely mentions one (``SW_P3V3``, ``1V8D_REG_EN``).
    A leading ``-`` or ``N`` before the number gives a negative rail.
    """
    if not name or has_control_token(name):
        return None
    up = name.upper().strip()
    sign = -1.0 if up.startswith("-") or re.match(r"^[N]\d", up) else 1.0
    body = up.lstrip("+-")
    if re.match(r"^N\d", body):
        body = body[1:]
    for rx in (_V_SPLIT, _P_SPLIT):
        m = rx.search(body)
        if m:
            return sign * float(f"{int(m.group(1))}.{m.group(2)}")
    m = _PLAIN_V.search(body)
    if m:
        return sign * float(m.group(1))
    return None


_RAILISH = re.compile(
    r"^[+\-]?(?:V(?:CC|DD|IN|BUS|BAT|SYS|MAIN|IO|CORE|REF|PP|AUX|DDQ|CCIO|DDIO)"
    r"|P?VIN|VBATT?|PWR|POWER|SUPPLY)\w*$",
    re.IGNORECASE,
)


def looks_like_rail(name: str) -> bool:
    """A name a designer would give a supply rail (has a voltage, or reads
    like VCC / VDD / VIN / VBUS …) and not a control or switching net."""
    if not name or has_control_token(name) or is_ground(name):
        return False
    if rail_voltage(name) is not None:
        return True
    return bool(_RAILISH.match(name.strip()))


# --- pin names -------------------------------------------------------------


def pin_base(name: str) -> str:
    """Strip channel / instance suffixes: ``VIN_3``→VIN, ``VCC(A)``→VCC,
    ``OUT2``→OUT, ``SW_1``→SW."""
    n = re.sub(r"\s+", "", name or "").upper()
    prev = None
    while prev != n:
        prev = n
        n = re.sub(r"\([A-Z0-9]*\)$", "", n)
        n = re.sub(r"[_\-]?\d+$", "", n)
        n = re.sub(r"[_\-]$", "", n)
    return n


REG_IN_PINS = frozenset({
    "VIN", "IN", "PVIN", "AVIN", "VI", "VCCIN", "VINA", "VIND", "INPUT",
    "PVDD", "VINP", "PIN", "SVIN", "DVIN",
})
REG_OUT_PINS = frozenset({"VOUT", "OUT", "VO", "OUTPUT", "OUTS", "VOS", "SENSE"})
SW_PINS = frozenset({"SW", "LX", "PH", "SWA", "SWB", "SW1", "SW2", "LX1", "LX2", "SWITCH"})
BOOT_PINS = frozenset({"BST", "BOOT", "CB", "BOOST"})
LDO_HINT_PINS = frozenset({"BYP", "NR", "NR/SS", "NRSS", "BYPASS"})
FB_PINS = frozenset({"FB", "VFB", "ADJ"})

_SUPPLY_PREFIXES = (
    "VCC", "VDD", "AVDD", "DVDD", "AVCC", "DVCC", "PVDD", "VDDIO", "VCCIO",
    "VDDA", "VDDD", "VCCA", "VCCB", "VDDQ", "VCORE", "VBAT", "VIO", "VPP",
)


def is_supply_pin(name: str) -> bool:
    """Pin names that draw supply current on a load IC (VCC, VDD, VS, V+…)."""
    b = pin_base(name)
    if b in ("VS", "V+", "VP", "VSUP", "VSUPPLY", "VDD", "VCC", "PWR", "POWER"):
        return True
    return b.startswith(_SUPPLY_PREFIXES)


_DES_PREFIX = re.compile(r"^([A-Za-z]+)")


def designator_prefix(designator: str) -> str:
    m = _DES_PREFIX.match(designator or "")
    return m.group(1).upper() if m else ""


CONNECTOR_PREFIXES = frozenset({"J", "P", "CN", "CON", "X", "XS", "XP", "TB", "BT", "BAT", "PWR", "DC", "JP"})
IC_PREFIXES = frozenset({"U", "IC", "PS", "VR", "M", "PM", "REG", "MOD", "A", "Y", "OSC", "XO"})
PASSIVE_PREFIXES = frozenset({"R", "RN", "C", "L", "FB", "FL", "D", "LED", "Q", "TP", "F",
                              "MH", "H", "FID", "SW", "S", "NT", "T", "K", "E"})


# Unit suffixes after the milli / mega check (case-insensitive).
_R_SUFFIX = {"": 1.0, "R": 1.0, "OHM": 1.0, "OHMS": 1.0,
             "K": 1e3, "KR": 1e3, "KOHM": 1e3, "KOHMS": 1e3, "U": 1e-6}


def parse_resistance(text: str) -> float | None:
    """``5m``/``5mΩ``/``5 mOhm``→0.005, ``0R005``→0.005, ``0.1``→0.1, ``10k``→1e4.

    Lower-case ``m`` is milli; ``M``/``MEG`` (mega) is never a sense
    resistor value and is rejected. Returns ``None`` when unparseable.
    """
    if not text:
        return None
    t = text.strip().replace(" ", "").replace("Ω", "ohm").replace("Ω", "ohm")
    m = re.fullmatch(r"(\d+)[Rr](\d+)", t)            # 0R005
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")
    m = re.fullmatch(r"(\d+)([kKmM])(\d+)", t)        # 4k7, 2m5
    if m:
        mult = 1e3 if m.group(2) in "kK" else 1e-3
        if m.group(2) == "M":
            return None
        return float(f"{m.group(1)}.{m.group(3)}") * mult
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([a-zA-Z]*)", t)
    if not m:
        return None
    value = float(m.group(1))
    unit = m.group(2)
    if unit.startswith("m"):          # milli: 5m, 5mohm, 5mR
        return value * 1e-3
    if unit.startswith("M"):          # mega (or an ambiguous capital M)
        return None
    key = unit.upper()
    if key in _R_SUFFIX:
        return value * _R_SUFFIX[key]
    return None
