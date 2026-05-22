"""Turn FYPA editor-mode directives into solver-ready annotation specs.

Editor mode (see :mod:`fypa.project_file`) lets the user place PDN sources /
sinks without editing the Altium schematic. Those edits live in the ``.fypa``
project file as :class:`~fypa.project_file.EditorDirective` records.

Before a re-solve, :func:`apply_editor_directives` converts each editor
directive into a real :class:`~fypa.altium_annotations.SourceSpec` /
:class:`~fypa.altium_annotations.SinkSpec` / :class:`~fypa.altium_annotations.ResistorSpec`
and appends it to the loaded
project's :class:`~fypa.altium_annotations.AnnotationResult`. From there
:func:`fypa.altium_loader.build_problem` treats it exactly like a schematic
directive — it meshes the referenced nets and stamps the lumped element.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Base id for editor-directive return groups. Each electrically-connected
# rail of single-net editor directives gets its own id (BASE, BASE+1, …) so
# its point-to-point loop closes through an ideal-0 V return that is NOT
# shared with any other rail. Sharing one return node across unconnected
# rails wires their copper together through a phantom conductive path and
# drives the FEM matrix near-singular. Numbered high to stay clear of the
# schematic parser's return-group ids, which start at 0.
_EDITOR_RETURN_GROUP_BASE = 9001

_EDITOR_SCHDOC = "(editor)"


def apply_editor_directives(loaded, editor_directives) -> list[str]:
    """Append synthetic SourceSpec / SinkSpec / ResistorSpec specs to
    ``loaded.annotations.directives`` — one per editor directive.

    ``loaded`` is a :class:`fypa.altium_loader.LoadedProject`; it is mutated
    in place (the caller owns a fresh copy loaded from the design-info
    pickle). Returns a list of human-readable warnings for directives that
    could not be resolved — those are skipped rather than aborting the solve.
    """
    from fypa.altium_annotations import (
        ResistorSpec,
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

    # --- Per-rail return groups for single-net editor directives ----------
    # Each electrically-connected rail needs its OWN ideal-0 V return node.
    # One return group shared across the whole board lets a SINK on rail A
    # close its loop through a SOURCE on rail B — a path the copper can't
    # carry, so the FEM matrix goes near-singular. Mirror altium_annotations'
    # _assign_return_groups: union connected nets, one return id per group.
    _uf: dict[str, str] = {}

    def _uf_find(name: str) -> str:
        _uf.setdefault(name, name)
        root = name
        while _uf[root] != root:
            root = _uf[root]
        while _uf[name] != root:        # path-compress
            _uf[name], name = root, _uf[name]
        return root

    def _uf_union(a: str, b: str) -> None:
        ra, rb = _uf_find(a), _uf_find(b)
        if ra != rb:
            _uf[rb] = ra

    # SERIES directives bridge two nets — keep a point-to-point check that
    # spans a ferrite / 0 Ω link inside a single rail group.
    for _d in loaded.annotations.directives:
        if not isinstance(_d, ResistorSpec):
            continue
        bridged: list[str] = []
        for term in (_d.p, _d.n):
            for pin in getattr(term, "pins", ()):
                ni = pin.net_index
                if 0 <= ni < len(extracted.nets):
                    nm = getattr(extracted.nets[ni], "name", None)
                    if nm:
                        bridged.append(nm.upper())
        for other in bridged[1:]:
            _uf_union(bridged[0], other)

    # Editor SERIES directives bridge two nets as well — they aren't in
    # ``loaded.annotations.directives`` yet (they get appended below), so
    # union their P / N nets here, same reasoning as the schematic
    # ResistorSpec loop above: a single-net SOURCE / SINK on either side of
    # the bridge then shares one rail return group.
    for _ed in editor_directives:
        if (getattr(_ed, "role", "") or "").upper() == "SERIES" \
                and _ed.p_net and _ed.n_net:
            _uf_union(_ed.p_net.upper(), _ed.n_net.upper())

    _rail_return_group: dict[str, int] = {}

    def _return_group_for(net_name: str) -> int:
        """Return-group id for the rail ``net_name`` sits on, minting a fresh
        id (kept clear of the schematic ids) the first time a rail is seen."""
        root = _uf_find(net_name.upper())
        gid = _rail_return_group.get(root)
        if gid is None:
            gid = _EDITOR_RETURN_GROUP_BASE + len(_rail_return_group)
            _rail_return_group[root] = gid
        return gid

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
    # Roles applied per return group + a representative rail net name, so an
    # open-loop rail (sinks but no source, or vice versa) can be flagged.
    group_roles: dict[int, set[str]] = {}
    group_net: dict[int, str] = {}
    for ed in editor_directives:
        label = ed.designator or f"editor:{ed.id}"
        if ed.role not in ("SOURCE", "SINK", "SERIES"):
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
        # SERIES always bridges two real nets; SOURCE / SINK honour the
        # directive's single-net flag.
        two_net = (not ed.single_net) or ed.role == "SERIES"
        n_term = None
        if two_net:
            if ed.role == "SERIES" and not ed.n_net:
                warnings.append(
                    f"{label}: SERIES needs both a P net and an N net; "
                    "skipped."
                )
                continue
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
        # Single-net directives get their rail's own return group; two-net
        # directives carry a real N terminal and need none.
        return_group = (_return_group_for(ed.p_net)
                        if not two_net and ed.p_net else None)
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
        elif ed.role == "SINK":
            if ed.current is None:
                warnings.append(f"{label}: SINK has no current; skipped.")
                continue
            spec = SinkSpec(
                designator=spec_designator, schdoc_name=_EDITOR_SCHDOC,
                current=float(ed.current), p=p_term, n=n_term,
                channel_index=None, return_group=return_group,
            )
        else:  # SERIES — a lumped resistance bridging the two nets
            if ed.resistance is None:
                warnings.append(f"{label}: SERIES has no resistance; skipped.")
                continue
            if ed.resistance <= 0:
                warnings.append(
                    f"{label}: SERIES resistance must be positive, got "
                    f"{ed.resistance}; skipped."
                )
                continue
            # n_term is guaranteed non-None here: SERIES forces two_net and
            # the missing-N-net case was caught above.
            spec = ResistorSpec(
                designator=spec_designator, schdoc_name=_EDITOR_SCHDOC,
                resistance=float(ed.resistance), p=p_term, n=n_term,
            )
        loaded.annotations.directives.append(spec)
        applied += 1
        if return_group is not None:
            group_roles.setdefault(return_group, set()).add(ed.role)
            group_net.setdefault(return_group, ed.p_net or "?")

    # A single-net rail only carries current with at least one SOURCE AND
    # one SINK sharing it. Warn (rather than abort) so the rest of the solve
    # still runs — but an open-loop rail solves to an unreliable result.
    for gid, roles in group_roles.items():
        rail = group_net.get(gid, "?")
        if "SOURCE" not in roles:
            warnings.append(
                f"Editor rail {rail!r}: single-net SINK(s) with no SOURCE — "
                "no current can flow (open loop). Add a single-net SOURCE on "
                "this rail, or switch the sink to two-net mode."
            )
        if "SINK" not in roles:
            warnings.append(
                f"Editor rail {rail!r}: single-net SOURCE(s) with no SINK — "
                "no current can flow (open loop). Add a single-net SINK on "
                "this rail, or switch the source to two-net mode."
            )

    log.info("apply_editor_directives: applied %d, skipped %d.",
             applied, len(warnings))
    return warnings
