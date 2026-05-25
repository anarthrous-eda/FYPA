"""Altium-specific extract / annotation / loader code.

Submodules:

* :mod:`fypa.altium.extract`    — parses .PrjPcb + .PcbDoc into :class:`ExtractedProject`.
* :mod:`fypa.altium.annotations`— parses ``PDN_*`` schematic parameters.
* :mod:`fypa.altium.loader`     — orchestrator; produces a :class:`LoadedProject`
  ready for the format-agnostic ``build_problem`` / ``build_solve_metadata``.

The Gerber import path lives in :mod:`fypa.gerber` and produces the same
:class:`~fypa.altium.extract.ExtractedProject` shape, so everything downstream
(geometry, viewer, cache, solver) works regardless of source.
"""
