"""Gerber + Excellon import path.

Submodules:

* :mod:`fypa.gerber.extract`   — converts a set of Gerber + Excellon files into
  an :class:`~fypa.altium.extract.ExtractedProject` (the same dataclass the
  Altium path produces), so everything downstream is format-agnostic.
* :mod:`fypa.gerber.loader`    — thin sibling of :func:`fypa.altium.loader.load_project`
  that skips the Altium-specific extract step.
* :mod:`fypa.gerber.import_ui` — PySide6 dialogs: file-classification confirmation +
  per-layer stackup editor.

Gerber files carry no schematic information, so PDN source / sink directives
can only be specified via the existing editor mode
(:mod:`fypa.editor_directives`).
"""
