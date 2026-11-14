"""Datasheet lookup and fact extraction for auto setup.

A datasheet folder is matched to parts by part number (file name first,
then the first page's text), its first pages are read with ``pypdf``, and a
handful of facts that decide how a part is set up are pulled out with
regular expressions: is it a regulator, LDO or switcher, which topology,
efficiency, input range, fixed output voltage, quiescent /
supply current, DC resistance / on-resistance.

Each fact keeps the sentence it came from (``evidence``) so the review
screen can show *why* a value was suggested. Datasheets are written for
people, so this is best effort: a fact that isn't found is simply absent,
and every suggestion built on one is reviewed before it is applied.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

MAX_PAGES = 12          # features + electrical characteristics live up front
MAX_PDFS = 5000
_MIN_KEY = 5            # shortest part-number prefix accepted as a match


def pdf_support_available() -> bool:
    try:
        import pypdf  # noqa: F401
    except ImportError:
        return False
    return True


def normalise_pn(text: str) -> str:
    """Upper-case alphanumerics only: ``MAX38913AATD+`` → ``MAX38913AATD``."""
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


@dataclass
class Fact:
    value: Any
    evidence: str


@dataclass
class DatasheetFacts:
    path: Path
    is_regulator: Fact | None = None
    kind: Fact | None = None              # "LDO" | "SMPS"
    topology: Fact | None = None          # BUCK | BOOST | BUCKBOOST | INVERTER
    is_switch: Fact | None = None         # load switch / eFuse
    efficiency: Fact | None = None        # 0..1

    vin_range: Fact | None = None         # (lo, hi) V
    vout_fixed: Fact | None = None        # V
    quiescent: Fact | None = None         # A
    supply_current: Fact | None = None    # A
    resistance: Fact | None = None        # Ω (DCR / RON)
    facts_found: list[str] = field(default_factory=list)


def _amps(value: str, unit: str) -> float:
    """``unit`` is one of nA / µA / uA / mA / A (any case from re.I)."""
    c = unit.strip()[:1].lower()
    mult = {"n": 1e-9, "µ": 1e-6, "μ": 1e-6, "u": 1e-6, "m": 1e-3}.get(c, 1.0)
    return float(value) * mult


def _ohms(value: str, unit: str) -> float:
    """``unit`` is mΩ / Ω / mohm / ohm(s)."""
    return float(value) * (1e-3 if unit.strip()[:1].lower() == "m" else 1.0)


def _snippet(text: str, m: re.Match, width: int = 70) -> str:
    a = max(0, m.start() - width)
    b = min(len(text), m.end() + width)
    return ("…" if a else "") + text[a:b].strip() + ("…" if b < len(text) else "")


_NUM = r"(\d+(?:\.\d+)?)"
_CUR = r"(nA|[µμu]A|mA|A)\b"
_RX = {
    "ldo": re.compile(r"\b(LDO|low[- ]?dropout|linear regulator)\b", re.I),
    "smps": re.compile(
        r"\b(step[- ]down|step[- ]up|buck|boost|switching regulator|switch(?:ing|-mode) "
        r"(?:converter|power)|DC[-/ ]?DC|power module|synchronous rectif\w*|"
        r"inverting (?:converter|regulator))\b", re.I),
    "regulator": re.compile(
        r"\b(regulator|LDO|converter|power module|step[- ]down|step[- ]up|buck|boost)\b", re.I),
    "switch": re.compile(r"\b(load switch|power switch|e-?fuse|power[- ]path|"
                         r"current[- ]limited switch|high[- ]side switch)\b", re.I),
    "buckboost": re.compile(r"\bbuck[- ]?boost\b", re.I),
    "inverter": re.compile(r"\binverting\b", re.I),
    "boost": re.compile(r"\b(boost|step[- ]up)\b", re.I),
    "buck": re.compile(r"\b(buck|step[- ]down)\b", re.I),
    "eff": re.compile(
        rf"(?:{_NUM}\s?%\s*(?:peak\s+|maximum\s+|max\.?\s+|typical\s+)?efficiency)|"
        rf"(?:efficiency\s*(?:of|up to|as high as|reaches|:|>|≥)?\s*{_NUM}\s?%)", re.I),
    "vin": re.compile(
        rf"(?:input voltage(?: range)?\s*(?:of|from|:)?\s*{_NUM}\s?V?\s?(?:to|-|-|-)\s?{_NUM}\s?V)|"
        rf"(?:{_NUM}\s?V?\s?(?:to|-|-|-)\s?{_NUM}\s?V\s+(?:input|VIN|operating input))", re.I),
    "vfixed": re.compile(
        rf"fixed output(?: voltages?)?\s*(?:of|:)?\s*{_NUM}\s?V", re.I),
    "iq": re.compile(
        rf"(?:quiescent current\s*(?:\(I\s?Q\))?\s*(?:of|is|:)?\s*(?:only\s*)?{_NUM}\s?{_CUR})|"
        rf"(?:{_NUM}\s?{_CUR}\s+(?:typical\s+|low\s+)?quiescent current)|"
        rf"(?:\bI\s?Q\s*[:=]\s*{_NUM}\s?{_CUR})", re.I),
    "isupply": re.compile(
        rf"(?:(?:supply|operating|operational|active)\s+current\s*(?:\(I\s?(?:CC|DD|S|Q)\))?\s*(?:of|is|:)?\s*"
        rf"(?:only\s*)?(?:typ(?:ical)?\.?\s*)?{_NUM}\s?{_CUR})|"
        rf"(?:{_NUM}\s?{_CUR}\s+(?:typical\s+|max(?:imum)?\s+)?(?:supply|operating) current)", re.I),
    "dcr": re.compile(
        rf"(?:DC resistance|DCR|R\s?DC)\s*(?:\((?:max|typ)\.?\))?\s*(?:of|:|=)?\s*{_NUM}\s?(mΩ|Ω|m?ohms?)",
        re.I),
    "ron": re.compile(
        rf"(?:{_NUM}\s?(mΩ|Ω)\s+(?:typical\s+|typ\.?\s+)?(?:R\s?ON|on[- ]resistance))|"
        rf"(?:(?:R\s?ON|on[- ]resistance)\s*(?:of|:|=)?\s*{_NUM}\s?(mΩ|Ω))", re.I),
}


def _first(rx: re.Pattern, text: str) -> re.Match | None:
    return rx.search(text)


def _groups(m: re.Match) -> list[str]:
    return [g for g in m.groups() if g is not None]


def parse_facts(text: str, path: Path) -> DatasheetFacts:
    """Pull the setup-relevant facts out of datasheet text."""
    t = re.sub(r"[ \t\r\f\v]+", " ", text)
    t = re.sub(r"\s*\n\s*", " \n ", t)
    flat = t.replace("\n", " ")
    f = DatasheetFacts(path=path)

    # Title + features: what the part *is*. Stop at the Applications list,
    # which names what the part is used *with* ("DC/DC converters" on a
    # power monitor's datasheet).
    head = flat[:6000]
    if m := re.search(r"\b\d?\s?Applications\b", flat[80:6000]):
        head = flat[: 80 + m.start()]
    if m := _first(_RX["switch"], head):
        f.is_switch = Fact(True, _snippet(flat, m))
    ldo = list(_RX["ldo"].finditer(head))
    smps = list(_RX["smps"].finditer(head))
    if ldo or smps:
        first_ldo = ldo[0].start() if ldo else 10**9
        first_smps = smps[0].start() if smps else 10**9
        if len(smps) > len(ldo) * 2 or (smps and first_smps < first_ldo and len(smps) >= len(ldo)):
            f.kind = Fact("SMPS", _snippet(flat, smps[0]))
        else:
            f.kind = Fact("LDO", _snippet(flat, ldo[0]))
    if m := _first(_RX["regulator"], head):
        if f.is_switch is None or f.kind is not None:
            f.is_regulator = Fact(True, _snippet(flat, m))
    if f.kind is not None and f.kind.value == "SMPS":
        for topo in ("buckboost", "inverter", "buck", "boost"):
            if m := _first(_RX[topo], head):
                f.topology = Fact(topo.upper(), _snippet(flat, m))
                break

    if m := _first(_RX["eff"], flat):
        v = float(_groups(m)[0])
        if 30 <= v <= 99.9:
            f.efficiency = Fact(v / 100.0, _snippet(flat, m))
    if m := _first(_RX["vin"], flat):
        g = _groups(m)
        lo, hi = float(g[0]), float(g[1])
        if 0 < lo < hi <= 1000:
            f.vin_range = Fact((lo, hi), _snippet(flat, m))
    if m := _first(_RX["vfixed"], flat):
        f.vout_fixed = Fact(float(_groups(m)[0]), _snippet(flat, m))
    if m := _first(_RX["iq"], flat):
        g = _groups(m)
        f.quiescent = Fact(_amps(g[0], g[1]), _snippet(flat, m))
    if m := _first(_RX["isupply"], flat):
        g = _groups(m)
        f.supply_current = Fact(_amps(g[0], g[1]), _snippet(flat, m))
    if m := _first(_RX["dcr"], flat):
        g = _groups(m)
        f.resistance = Fact(_ohms(g[0], g[1]), _snippet(flat, m))
    elif m := _first(_RX["ron"], flat):
        g = _groups(m)
        f.resistance = Fact(_ohms(g[0], g[1]), _snippet(flat, m))

    f.facts_found = [k for k in ("kind", "topology", "is_switch", "efficiency",
                                 "vin_range", "vout_fixed",
                                 "quiescent", "supply_current", "resistance")
                     if getattr(f, k) is not None]
    return f


def read_pdf_text(path: Path, max_pages: int = MAX_PAGES) -> tuple[str, str | None]:
    """``(text of the first max_pages pages, None)``, or ``("", reason)``.

    Many vendor datasheets are encrypted against editing or printing only -
    an empty user password opens them - which pypdf handles once a crypto
    backend (``cryptography``) is installed. A real password, or a damaged
    file, gives a short reason instead of text."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return "", "PDF reading unavailable (pypdf not installed)"
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted and not reader.decrypt(""):
            return "", "password protected"
        chunks = []
        for page in reader.pages[:max_pages]:
            try:
                chunks.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001 - one bad page shouldn't lose the rest
                continue
        text = "\n".join(chunks)
        if not text.strip():
            return "", "no text layer (scanned image?)"
        return text, None
    except Exception as e:  # noqa: BLE001 - encrypted / corrupt / not a PDF
        reason = _short_reason(e)
        log.warning("Auto setup: could not read %s (%s)", path, reason)
        log.debug("Auto setup: read failure detail", exc_info=True)
        return "", reason


def _short_reason(e: Exception) -> str:
    name = type(e).__name__
    msg = str(e).splitlines()[0] if str(e) else ""
    if name == "DependencyError":
        return f"needs a crypto library ({msg})"
    if name in ("PdfReadError", "PdfStreamError", "EmptyFileError"):
        return f"damaged or not a PDF ({msg})" if msg else "damaged or not a PDF"
    if name in ("FileNotFoundError", "PermissionError"):
        return msg or name
    return f"{name}: {msg}" if msg else name


def extract_text(path: Path, max_pages: int = MAX_PAGES) -> str:
    """Text of the first ``max_pages`` pages ("" on any failure)."""
    return read_pdf_text(path, max_pages)[0]


class DatasheetLibrary:
    """PDFs under a folder, matched to parts by part number."""

    def __init__(self, folder: Path | str, *, text_reader: Callable[[Path], str] | None = None):
        self.folder = Path(folder)
        self._read = text_reader or self._read_recording
        # PDFs that couldn't be read, with why - shown in the wizard.
        self.unreadable: dict[Path, str] = {}
        self.pdfs: list[Path] = []
        if self.folder.is_dir():
            for p in self.folder.rglob("*"):
                if p.suffix.lower() == ".pdf" and p.is_file():
                    self.pdfs.append(p)
                    if len(self.pdfs) >= MAX_PDFS:
                        break
        self._stems = [(p, normalise_pn(p.stem)) for p in self.pdfs]
        self._text: dict[Path, str] = {}
        self._first_page: dict[Path, str] = {}
        self._facts: dict[Path, DatasheetFacts] = {}

    def __len__(self) -> int:
        return len(self.pdfs)

    def text(self, path: Path) -> str:
        if path not in self._text:
            self._text[path] = self._read(path)
        return self._text[path]

    def _first_page_text(self, path: Path) -> str:
        if path not in self._first_page:
            full = self._text.get(path)
            if full is None:
                try:
                    full = self._read_first_page(path)
                except Exception:  # noqa: BLE001
                    full = ""
            self._first_page[path] = normalise_pn(full[:4000])
        return self._first_page[path]

    def _read_recording(self, path: Path, max_pages: int = MAX_PAGES) -> str:
        text, reason = read_pdf_text(path, max_pages)
        if reason is not None:
            self.unreadable[path] = reason
        return text

    def _read_first_page(self, path: Path) -> str:
        if self._read != self._read_recording:
            return self._read(path)
        return self._read_recording(path, max_pages=1)

    def find(self, part_numbers: list[str]) -> Path | None:
        """Best PDF for any of ``part_numbers``: longest matching prefix in a
        file name, else in a first page's text."""
        keys = [k for k in (normalise_pn(pn) for pn in part_numbers) if len(k) >= _MIN_KEY]
        if not keys or not self.pdfs:
            return None
        best: tuple[int, int, Path] | None = None
        for path, stem in self._stems:
            for key in keys:
                n = _match_len(key, stem)
                if n and (best is None or (n, -len(stem)) > (best[0], best[1])):
                    best = (n, -len(stem), path)
        if best is not None:
            return best[2]
        for path, _stem in self._stems:
            page = self._first_page_text(path)
            for key in keys:
                if len(key) >= 6 and key[: max(6, len(key) - 4)] in page:
                    return path
        return None

    def facts(self, path: Path) -> DatasheetFacts:
        if path not in self._facts:
            self._facts[path] = parse_facts(self.text(path), path)
        return self._facts[path]


def _match_len(key: str, stem: str) -> int:
    """Length of the longest prefix of ``key`` (≥ _MIN_KEY) found in ``stem``,
    also accepting a stem that is itself a prefix of the key (``INA238`` for
    ``INA238AQDGSRQ1``)."""
    if not stem:
        return 0
    for n in range(len(key), _MIN_KEY - 1, -1):
        if key[:n] in stem:
            return n
    return 0
