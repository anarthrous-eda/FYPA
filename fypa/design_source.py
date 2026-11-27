"""Which CAD tool a loaded design came from, for wording and coordinates.

A solve's metadata names its design by ``prjpcb_path`` (an Altium ``.PrjPcb``,
a KiCad ``.kicad_pcb`` or a Gerber import's pseudo-path) and, for the non-Altium
kinds, a ``source_kind``. Viewer code asks here rather than testing suffixes
itself, so the three kinds are told apart in one place.

No Qt imports: also used by the report and the CLI.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fypa.kicad import is_kicad_path

ALTIUM = "altium"
KICAD = "kicad"
GERBER = "gerber"

# Shown after "KiCad" wherever FYPA names the format while KiCad import is in
# beta (menus, the start window, the report).
KICAD_BETA_TAG = "(beta)"


def kind_of_path(path: str | Path | None) -> str:
    """``"kicad"`` for a KiCad board / project path, else ``"altium"``."""
    return KICAD if is_kicad_path(path) else ALTIUM


def source_kind(metadata: Mapping[str, Any] | None) -> str:
    """``"altium"``, ``"kicad"`` or ``"gerber"`` for a solve's metadata.

    Metadata written before KiCad support carries no ``source_kind`` for an
    Altium or KiCad design, so the path decides when the field is absent.
    """
    md = metadata or {}
    kind = md.get("source_kind")
    if kind in (GERBER, KICAD):
        return kind
    return kind_of_path(md.get("prjpcb_path"))


def tool_name(metadata: Mapping[str, Any] | None) -> str:
    """The design tool to name in a message: ``"KiCad"`` or ``"Altium"``.

    A Gerber import has no tool to go back to; it is named ``"Altium"`` only
    where callers already branch on Gerber first.
    """
    return "KiCad" if source_kind(metadata) == KICAD else "Altium"


def is_kicad(metadata: Mapping[str, Any] | None) -> bool:
    return source_kind(metadata) == KICAD


def display_y_sign(metadata: Mapping[str, Any] | None) -> float:
    """Multiply a FYPA Y by this to get the Y the design tool shows.

    FYPA works Y-up, as Altium does. KiCad shows Y growing down the page, and
    its boards are read mirrored to Y-up (``fypa.kicad.extract``), so a KiCad
    design's Y is shown and typed negated, and matches the KiCad status bar.
    """
    return -1.0 if is_kicad(metadata) else 1.0


def is_live_design_path(path: str | Path | None) -> bool:
    """True for a design FYPA can read again from disk: an Altium ``.PrjPcb``
    or a KiCad board / project. False for a Gerber import's pseudo-path, a
    bare solution and ``None``. Reload Design and the design-changed watch
    act only on these."""
    if not path:
        return False
    return Path(path).suffix.lower() == ".prjpcb" or is_kicad_path(path)
