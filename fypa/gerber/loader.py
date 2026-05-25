"""Loader for Gerber-sourced :class:`~fypa.altium.loader.LoadedProject`.

Mirror of :func:`fypa.altium.loader.load_project` minus the Altium extraction
step: takes an already-built :class:`~fypa.altium.extract.ExtractedProject`
(produced by :func:`fypa.gerber.extract.extract_gerber_project`) and runs the
shared annotation parse + LoadedProject assembly.

With ``sch_components = ()``, :func:`parse_annotations` returns an empty
result (no SOURCE / SINK / SERIES / REGULATOR directives) and the SERIES
bridge-merge pre-pass in :func:`fypa.altium.loader.load_project` becomes a
no-op — so the only thing that distinguishes a Gerber project from a fresh
Altium project at this stage is the absence of schematic directives. The
user adds them via editor mode (:mod:`fypa.editor_directives`).
"""
from __future__ import annotations

import logging

from fypa.altium.annotations import parse_annotations
from fypa.altium.extract import ExtractedProject
from fypa.altium.loader import LoadedProject

log = logging.getLogger(__name__)


def load_gerber_project(extracted: ExtractedProject) -> LoadedProject:
    """Build a :class:`LoadedProject` from a Gerber-derived ExtractedProject.

    The geometry is built lazily by :class:`LoadedProject` itself (first
    access to ``.geometry``); we only need to run the annotation parser
    here so the LoadedProject invariant ``annotations is not None`` holds.
    """
    enabled = extracted.enabled_copper_layer_ids()
    if not enabled:
        log.warning(
            "Gerber project has no enabled copper layers — was the stackup "
            "empty? The board won't solve.",
        )
    annotations = parse_annotations(extracted, enabled_layers=enabled)
    return LoadedProject(
        extracted=extracted,
        annotations=annotations,
        absorbed_bridges=[],
    )
