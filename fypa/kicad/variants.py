"""KiCad 10 design (assembly) variants, as FYPA's variant model.

KiCad 10 keeps variants in the design files themselves (board format
20260101+): the board lists them in its header, and each footprint carries
what one variant changes about it -

    (variants (variant (name "Lite") (description "No radio")))
    (footprint "R:R_0603" ...
      (variant (name "Lite") (dnp yes)
        (field (name "PDN_I") (value "50mA"))))

Only the differences from the base design are written. *Update PCB from
Schematic* copies the schematic's per-instance variant data onto the
footprints, so the board alone is read.

The result is the same :class:`~fypa.altium.variants.ProjectVariants` an
Altium project gives, so the variant picker, Not Fitted handling and
parameter overrides work unchanged. A part is Not Fitted in a variant when
the base design marks it Do not populate and the variant doesn't say
``(dnp no)``, or the variant says ``(dnp yes)``. KiCad records no "current"
variant in its files, so the base design is the default.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fypa.altium.variants import ProjectVariants, Variant

log = logging.getLogger(__name__)

# (resolved board path, mtime, size) -> result: the variant list is read on
# every reload check, and re-parsing a large board each time is wasteful.
_CACHE: dict[tuple[str, float, int], ProjectVariants] = {}


def read_kicad_variants(path: str | Path) -> ProjectVariants:
    """The board's variants; empty for a board without any (KiCad 9 and
    older) or one that can't be read."""
    from fypa.kicad import sexpr
    from fypa.kicad.extract import (
        _flag,
        footprint_is_dnp,
        footprint_reference,
        resolve_kicad_board,
    )
    try:
        board = resolve_kicad_board(path)
        st = board.stat()
    except (OSError, FileNotFoundError):
        return ProjectVariants(current=None)
    key = (str(board.resolve()), st.st_mtime, st.st_size)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    try:
        pcb = sexpr.parse_file(board)
    except (OSError, ValueError):
        log.warning("Could not read KiCad variants from %s", board, exc_info=True)
        return ProjectVariants(current=None)

    header = pcb.node("variants")
    names = [n for n in (v.s("name") for v in header.nodes("variant"))
             if n] if header is not None else []
    base_dnp: set[str] = set()
    dnp: dict[str, dict[str, bool]] = {n: {} for n in names}
    fields: dict[str, dict[str, dict[str, str]]] = {n: {} for n in names}
    for fp in pcb.nodes("footprint"):
        ref = footprint_reference(fp).strip().upper()
        if not ref:
            continue
        if footprint_is_dnp(fp):
            base_dnp.add(ref)
        for v in fp.nodes("variant"):
            name = v.s("name")
            if not name:
                continue
            if name not in dnp:               # listed only on a footprint
                names.append(name)
                dnp[name], fields[name] = {}, {}
            flag = _flag(v, "dnp")
            if flag is not None:
                dnp[name][ref] = flag
            over = {f.s("name"): f.s("value") or "" for f in v.nodes("field")
                    if f.s("name")}
            if over:
                fields[name][ref] = over

    out: dict[str, Variant] = {}
    for name in names:
        flags = dnp[name]
        not_fitted = frozenset(
            {d for d in base_dnp if flags.get(d, True)}
            | {d for d, yes in flags.items() if yes})
        out[name] = Variant(name=name, not_fitted=not_fitted,
                            param_overrides=fields[name])
    result = ProjectVariants(current=None, variants=out)
    _CACHE[key] = result
    return result
