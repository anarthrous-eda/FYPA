"""Auto setup: propose PDN sources, regulators, sinks and series elements.

Reads the design (pin names, nets, part parameters, power-port symbols via
altium_monkey) and, when given one, a folder of datasheets; returns
:class:`~fypa.autosetup.classify.Suggestion` objects for the user to review
in the Topology tab's Auto setup wizard. Nothing is applied here - an
accepted suggestion becomes an ordinary FYPA editor directive.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from fypa.autosetup.classify import AutoSetupResult, Suggestion, suggest
from fypa.autosetup.datasheets import DatasheetLibrary, pdf_support_available
from fypa.autosetup.design import DesignView, build_design_view

__all__ = [
    "AutoSetupResult",
    "DatasheetLibrary",
    "DesignView",
    "Suggestion",
    "build_design_view",
    "candidates_for_net",
    "pdf_support_available",
    "run_autosetup",
    "suggest",
]


def run_autosetup(
    loaded,
    *,
    editor_directives=(),
    datasheet_dir: Path | str | None = None,
    progress: Callable[[float, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    scan_schdocs: bool = True,
) -> tuple[AutoSetupResult, DesignView]:
    """Analyse ``loaded`` and return ``(result, design view)``.

    Raises ``InterruptedError`` when ``cancelled()`` turns true."""
    def note(msg: str) -> None:
        if progress is not None:
            progress(0.05, msg)
        if cancelled is not None and cancelled():
            raise InterruptedError

    note("Reading the design")
    view = build_design_view(loaded, editor_directives=editor_directives,
                             scan_schdocs=scan_schdocs, progress=note)
    library = None
    if datasheet_dir:
        note("Indexing datasheets")
        library = DatasheetLibrary(datasheet_dir)
    result = suggest(view, datasheets=library, progress=progress, cancelled=cancelled)
    if datasheet_dir and library is not None and not len(library):
        result.notes.insert(0, f"No PDF files found under {datasheet_dir}.")
    if datasheet_dir and not pdf_support_available():
        result.notes.insert(0, "PDF reading is unavailable (pypdf is not "
                               "installed); datasheets were not read.")
    return result, view


def candidates_for_net(view: DesignView, result: AutoSetupResult | None,
                       net: str, role: str) -> list[Suggestion]:
    """Ways to add a ``role`` directive on ``net`` (the Topology tab's
    click-to-add): the classifier's own suggestions for that net first, then
    every other part with a pin on the net, prefilled as far as the design
    allows. ``role`` is SOURCE, SINK or REGULATOR (output on ``net``)."""
    from fypa.autosetup.nets import CONNECTOR_PREFIXES, IC_PREFIXES, rail_voltage

    out: list[Suggestion] = []
    seen: set[str] = set()
    if result is not None:
        for s in result.suggestions:
            if s.role == role and s.p_net == net:
                out.append(s)
                seen.add(s.designator.upper())

    def rank(part) -> tuple:
        if role == "SOURCE":
            first = part.prefix in CONNECTOR_PREFIXES
        elif role == "REGULATOR":
            first = part.prefix in IC_PREFIXES
        else:
            first = part.prefix in IC_PREFIXES or part.prefix in CONNECTOR_PREFIXES
        return (not first, -len(part.pins_on(net)), part.designator)

    for part in sorted(view.parts_on(net), key=rank):
        if part.designator.upper() in seen:
            continue
        gnd = view.gnd_net_of(part)
        s = Suggestion(role=role, designator=part.designator, part_number=part.mpn,
                       p_net=net, n_net=gnd, single_net=gnd is None,
                       confidence=0.3)
        s.reasons.append(f"{part.designator} has {len(part.pins_on(net))} pin(s) on {net}")
        if role in ("SOURCE", "REGULATOR"):
            s.voltage = rail_voltage(net)
        if role == "REGULATOR":
            others = sorted(n for n in part.nets() if n != net and not view.is_gnd(n))
            s.in_p_net = others[0] if others else None
            s.regulator_type = "LDO"
        out.append(s)
    return out
