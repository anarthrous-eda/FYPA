"""Turn FYPA editor-mode directives into solver-ready annotation specs.

Editor mode (see :mod:`fypa.project_file`) lets the user place PDN sources /
sinks without editing the Altium schematic. Those edits live in the ``.fypa``
project file as :class:`~fypa.project_file.EditorDirective` records.

Before a re-solve, :func:`apply_editor_directives` converts each editor
directive into a real :class:`~fypa.altium_annotations.SourceSpec` /
:class:`~fypa.altium_annotations.SinkSpec` and appends it to the loaded
project's :class:`~fypa.altium_annotations.AnnotationResult`. From there
:func:`fypa.altium_loader.build_problem` treats it exactly like a schematic
directive — it meshes the referenced nets and stamps the lumped element.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Shared return node for every single-net editor directive. A single-net
# SOURCE and a single-net SINK both reference this group so their current
# loop closes through one common ideal-0 V return (see SourceSpec docs).
_EDITOR_RETURN_GROUP = 9001

_EDITOR_SCHDOC = "(editor)"


def apply_editor_directives(loaded, editor_directives) -> list[str]:
    """Append synthetic SourceSpec / SinkSpec specs to
    ``loaded.annotations.directives`` — one per editor directive.

    ``loaded`` is a :class:`fypa.altium_loader.LoadedProject`; it is mutated
    in place (the caller owns a fresh copy loaded from the design-info
    pickle). Returns a list of human-readable warnings for directives that
    could not be resolved — those are skipped rather than aborting the solve.
    """
    from fypa.altium_annotations import (
        SinkSpec,
        SourceSpec,
        TerminalPin,
        TerminalSpec,
    )
    from fypa.altium_extract import Pt2D

    extracted = loaded.extracted
    enabled = extracted.enabled_copper_layer_ids()
    if not enabled:
        return ["Editor directives skipped: board has no enabled copper layers."]
    top_layer = enabled[0]

    # net name (upper-cased) -> net index
    net_index: dict[str, int] = {}
    for i, net in enumerate(extracted.nets):
        nm = getattr(net, "name", None)
        if nm:
            net_index.setdefault(nm.upper(), i)

    # physical PCB designator -> pcb_components index
    comp_index: dict[str, int] = {}
    for i, comp in enumerate(extracted.pcb_components):
        if comp.designator:
            comp_index.setdefault(comp.designator, i)

    def _component_center(designator: str | None) -> tuple[float, float] | None:
        ci = comp_index.get(designator) if designator else None
        if ci is None:
            return None
        pts = [p.center for p in extracted.pads if p.component_index == ci]
        if not pts:
            return None
        return (sum(p.x for p in pts) / len(pts),
                sum(p.y for p in pts) / len(pts))

    def _resolve_terminal(net_name, *, designator, fallback_xy,
                          fallback_layer_id):
        """Build a TerminalSpec on ``net_name``. A component-bound directive
        with real pads on that net gets one pin per pad; otherwise a single
        synthetic pin at ``fallback_xy`` (free-marker anchor or component
        centre). Returns ``None`` when the net name is unknown."""
        if not net_name:
            return None
        nidx = net_index.get(net_name.upper())
        if nidx is None:
            return None
        pins: list = []
        ci = comp_index.get(designator) if designator else None
        if ci is not None:
            for p in extracted.pads:
                if p.component_index != ci or p.net_index != nidx:
                    continue
                through = getattr(p, "is_through_hole", False)
                lid = top_layer if through else p.layer_id
                pins.append(TerminalPin(
                    pad_designator=p.designator or "(editor)",
                    layer_id=lid,
                    net_index=nidx,
                    point=p.center,
                    pad_polygon=None,
                ))
        if not pins:
            # Free marker, or a component with no pad on this net — couple
            # at the supplied fallback point on the net's copper.
            fx, fy = fallback_xy
            pins.append(TerminalPin(
                pad_designator="(editor)",
                layer_id=fallback_layer_id or top_layer,
                net_index=nidx,
                point=Pt2D(float(fx), float(fy)),
                pad_polygon=None,
            ))
        return TerminalSpec(pins=tuple(pins), requested_net=net_name)

    warnings: list[str] = []

    # Drop schematic directives that an unlocked editor directive overrides,
    # so the two don't both stamp a lumped element on the same component.
    override_desigs = {
        ed.overrides_designator for ed in editor_directives
        if getattr(ed, "overrides_designator", None)
    }
    if override_desigs:
        kept = [d for d in loaded.annotations.directives
                if d.designator not in override_desigs]
        dropped = len(loaded.annotations.directives) - len(kept)
        loaded.annotations.directives = kept
        if dropped:
            log.info("apply_editor_directives: dropped %d schematic "
                     "directive(s) overridden by the editor.", dropped)

    applied = 0
    for ed in editor_directives:
        label = ed.designator or f"editor:{ed.id}"
        if ed.role not in ("SOURCE", "SINK"):
            warnings.append(
                f"{label}: role {ed.role!r} is not supported by the editor "
                "re-solve; skipped."
            )
            continue
        if ed.kind == "free":
            fallback_xy = ed.anchor_xy or (0.0, 0.0)
            fallback_lid = ed.layer_id
        else:
            fallback_xy = _component_center(ed.designator) or (0.0, 0.0)
            fallback_lid = top_layer

        p_term = _resolve_terminal(
            ed.p_net, designator=ed.designator,
            fallback_xy=fallback_xy, fallback_layer_id=fallback_lid,
        )
        if p_term is None:
            warnings.append(
                f"{label}: P net {ed.p_net!r} not found on the board; skipped."
            )
            continue
        n_term = None
        if not ed.single_net:
            n_term = _resolve_terminal(
                ed.n_net, designator=ed.designator,
                fallback_xy=fallback_xy, fallback_layer_id=fallback_lid,
            )
            if n_term is None:
                warnings.append(
                    f"{label}: N net {ed.n_net!r} not found on the board; "
                    "skipped."
                )
                continue
        return_group = _EDITOR_RETURN_GROUP if ed.single_net else None
        spec_designator = ed.designator or f"EDIT_{ed.id}"

        if ed.role == "SOURCE":
            if ed.voltage is None:
                warnings.append(f"{label}: SOURCE has no voltage; skipped.")
                continue
            spec = SourceSpec(
                designator=spec_designator, schdoc_name=_EDITOR_SCHDOC,
                voltage=float(ed.voltage), p=p_term, n=n_term,
                channel_index=None, return_group=return_group,
            )
        else:  # SINK
            if ed.current is None:
                warnings.append(f"{label}: SINK has no current; skipped.")
                continue
            spec = SinkSpec(
                designator=spec_designator, schdoc_name=_EDITOR_SCHDOC,
                current=float(ed.current), p=p_term, n=n_term,
                channel_index=None, return_group=return_group,
            )
        loaded.annotations.directives.append(spec)
        applied += 1

    log.info("apply_editor_directives: applied %d, skipped %d.",
             applied, len(warnings))
    return warnings
