"""KiCad project import for FYPA (beta).

Reads KiCad's S-expression board / schematic files (``.kicad_pcb`` /
``.kicad_sch``) and adapts them into the same
:class:`~fypa.altium.extract.ExtractedProject` the Altium and Gerber paths
produce, so all downstream stages (geometry, annotations, FEM solve, viewer)
work unchanged.

Reads KiCad 7 to 9 board files. Parsing is done by a small self-contained
S-expression reader (:mod:`fypa.kicad.sexpr`) - pure stdlib, no KiCad /
``pcbnew`` install and no third-party parser required, so it works in CI and
inside the PyInstaller bundle.

A KiCad design is identified by its ``.kicad_pcb`` path everywhere FYPA stores
a project path (the ``prjpcb_path`` of an Altium design). Picking the
``.kicad_pro`` is accepted and resolved to the board beside it.

Entry point: :func:`fypa.kicad.extract.extract_kicad_project`, reached through
:func:`fypa.altium.extract.extract_project` (and so ``load_project``) whenever
the path is a KiCad file - see :func:`is_kicad_path`.
"""
from __future__ import annotations

from pathlib import Path

KICAD_SUFFIXES: frozenset[str] = frozenset({".kicad_pcb", ".kicad_pro"})


def is_kicad_path(path: str | Path | None) -> bool:
    """True when *path* names a KiCad board or project file."""
    if path is None:
        return False
    return Path(path).suffix.lower() in KICAD_SUFFIXES
