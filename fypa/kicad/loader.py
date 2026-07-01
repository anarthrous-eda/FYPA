"""Loader for KiCAD-sourced :class:`~fypa.altium.loader.LoadedProject`.

Mirror of :func:`fypa.altium.loader.load_project` with the KiCAD extraction step
swapped in for the Altium one: build an
:class:`~fypa.altium.extract.ExtractedProject` from ``.kicad_pcb`` (+ optional
``.kicad_sch``) via :func:`fypa.kicad.extract.extract_kicad_project`, then run
the shared annotation parse and low-resistance SERIES bridge-merge exactly as the
Altium path does.

Unlike the Gerber path, a KiCAD project carries components with ``PDN_*``
footprint-property parameters (and, when the schematic is present, symbol
fields), so :func:`parse_annotations` yields real SOURCE / SINK / SERIES /
REGULATOR directives with no editor-mode step required.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fypa.altium.loader import LoadedProject, _load_from_extracted
from fypa.kicad.extract import extract_kicad_project

log = logging.getLogger(__name__)


def load_kicad_project(
    kicad_pcb_path: str | Path,
    sch_path: str | Path | None = None,
) -> LoadedProject:
    """Extract a KiCAD board and assemble a :class:`LoadedProject`.

    Runs the same annotation parse + SERIES bridge-merge pre-pass as the Altium
    loader (via the shared :func:`fypa.altium.loader._load_from_extracted`), so
    low-resistance bridges are absorbed identically.
    """
    extracted = extract_kicad_project(kicad_pcb_path, sch_path)
    if not extracted.enabled_copper_layer_ids():
        log.warning(
            "KiCAD project has no enabled copper layers — the board won't solve.")
    return _load_from_extracted(extracted)
