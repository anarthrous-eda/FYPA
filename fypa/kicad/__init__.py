"""KiCAD project import for FYPA.

Reads KiCAD's S-expression PCB / schematic files (``.kicad_pcb`` /
``.kicad_sch``) and adapts them into the same
:class:`~fypa.altium.extract.ExtractedProject` the Altium and Gerber paths
produce, so all downstream stages (geometry, annotations, FEM solve, viewer)
work unchanged.

Targets the latest KiCAD file format (KiCAD 9). Parsing is done by a small
self-contained S-expression reader (:mod:`fypa.kicad.sexpr`) — pure stdlib, no
KiCAD / ``pcbnew`` install and no third-party parser required, so it works in
CI and inside the PyInstaller bundle.

Entry points:

* :func:`fypa.kicad.extract.extract_kicad_project` — files → ExtractedProject.
* :func:`fypa.kicad.loader.load_kicad_project` — ExtractedProject → LoadedProject.
"""
