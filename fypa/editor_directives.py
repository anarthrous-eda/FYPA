"""Turn FYPA editor-mode directives into solver-ready annotation specs.

Editor mode (see :mod:`fypa.project_file`) lets the user place PDN sources /
sinks without editing the Altium schematic. Those edits live in the ``.fypa``
project file as :class:`~fypa.project_file.EditorDirective` records.

Before a re-solve, :func:`apply_editor_directives` converts each editor
directive into a real :class:`~fypa.altium.annotations.SourceSpec` /
:class:`~fypa.altium.annotations.SinkSpec` / :class:`~fypa.altium.annotations.ResistorSpec`
and appends it to the loaded
project's :class:`~fypa.altium.annotations.AnnotationResult`. From there
:func:`fypa.altium.loader.build_problem` treats it exactly like a schematic
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

    ``loaded`` is a :class:`fypa.altium.loader.LoadedProject`; it is mutated
    in place (the caller owns a fresh copy loaded from the design-info
    pickle). Returns a list of human-readable warnings for directives that
    could not be resolved — those are skipped rather than aborting the solve.
    """
    from fypa.altium.annotations import (
        ResistorSpec,
        SinkSpec,
        SourceSpec,
        TerminalPin,
        TerminalSpec,
    )
    from fypa.altium.extract import Pt2D

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
    # carry, so the FEM matrix goes near-singular. Mirror fypa.altium.annotations'
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
            min_v = (float(ed.min_voltage)
                     if getattr(ed, "min_voltage", None) is not None
                     else None)
            spec = SinkSpec(
                designator=spec_designator, schdoc_name=_EDITOR_SCHDOC,
                current=float(ed.current), p=p_term, n=n_term,
                channel_index=None, return_group=return_group,
                min_voltage=min_v,
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


def apply_copper_names(loaded, copper_names) -> list[str]:
    """Promote user-named unnamed-copper pieces into real nets on
    ``loaded.extracted``, in place.

    Each :class:`~fypa.project_file.CopperName` pins a single anchor on
    a single copper layer to a user-given net name. ``loaded.extracted``
    surfaces unassigned copper with ``net_index == NO_NET``; this
    function finds the connected component of NO_NET geometry on the
    rename's layer that contains the anchor, appends a fresh
    :class:`~fypa.altium.extract.RawNet` carrying the new name, and
    re-points every NO_NET primitive overlapping that component at the
    new net. The bucketing in
    :func:`fypa.altium_geometry.build_net_layer_shapes` then routes
    those primitives into the new net's FEM slab instead of dropping
    them as NO_NET.

    Returns a list of human-readable warnings for renames whose anchor
    didn't sit on a NO_NET polygon (e.g. the user named copper and then
    the underlying design changed); the rename is skipped, not fatal.

    The mutation uses :func:`dataclasses.replace` because
    :class:`~fypa.altium.extract.ExtractedProject` is a frozen
    dataclass — the result is a brand-new tuple of nets / regions /
    tracks / etc., and ``loaded.extracted`` is rebound to it.
    """
    import dataclasses

    import shapely.geometry as _sg
    import shapely.ops as _sops

    from fypa.altium.extract import NO_NET, RawNet
    from fypa.altium_geometry import (
        _arc_polygon,
        _fill_polygon,
        _region_polygon,
        _shape_based_region_polygon,
        _track_polygon,
    )

    warnings: list[str] = []
    if not copper_names:
        return warnings

    extracted = loaded.extracted
    nets = list(extracted.nets)
    name_to_index: dict[str, int] = {n.name: i for i, n in enumerate(nets)}

    # Per-rename: ``layer_id`` and a prepared polygon representing the
    # connected component of NO_NET copper containing the anchor.
    # Primitives matching ``net_index == NO_NET`` and overlapping this
    # polygon get re-pointed at the rename's net_index.
    matches: list[tuple[int, _sg.base.BaseGeometry, int]] = []
    enabled = extracted.enabled_copper_layer_ids()

    def _no_net_pieces_on_layer(layer_id: int):
        """Every NO_NET primitive's individual polygon on ``layer_id``.
        Used to compute the per-layer union once and reuse it for any
        rename that targets this layer."""
        pieces: list[_sg.base.BaseGeometry] = []
        for t in extracted.tracks:
            if (t.layer_id == layer_id and t.net_index == NO_NET
                    and not t.is_keepout
                    and not t.is_polygon_outline
                    and t.width_mm > 0):
                pieces.append(_track_polygon(t))
        for a in extracted.arcs:
            if (a.layer_id == layer_id and a.net_index == NO_NET
                    and not a.is_keepout and a.width_mm > 0):
                pieces.append(_arc_polygon(a))
        for r in extracted.regions:
            if (r.layer_id == layer_id and r.net_index == NO_NET
                    and not r.is_keepout and not r.is_polygon_outline
                    and not r.is_board_cutout and r.kind == 0
                    and len(r.outline) >= 3):
                poly = _region_polygon(r)
                if not poly.is_empty:
                    pieces.append(poly)
        for r in extracted.shape_based_regions:
            if (r.layer_id == layer_id and r.net_index == NO_NET
                    and not r.is_keepout and not r.is_polygon_outline
                    and not r.is_board_cutout and r.kind == 0
                    and len(r.outline) >= 3):
                poly = _shape_based_region_polygon(r)
                if not poly.is_empty:
                    pieces.append(poly)
        for f in extracted.fills:
            if (f.layer_id == layer_id and f.net_index == NO_NET
                    and not f.is_keepout):
                poly = _fill_polygon(f)
                if poly is not None and not poly.is_empty:
                    pieces.append(poly)
        return pieces

    # Per-layer union cache — multiple renames on the same layer reuse it.
    union_cache: dict[int, _sg.base.BaseGeometry] = {}

    for c in copper_names:
        layer_id = int(c.layer_id)
        if layer_id not in enabled:
            warnings.append(
                f"Copper rename {c.name!r}: layer {layer_id} is not in "
                "the enabled copper stack; skipped.")
            continue
        if layer_id not in union_cache:
            pieces = _no_net_pieces_on_layer(layer_id)
            if pieces:
                union_cache[layer_id] = _sops.unary_union(pieces)
            else:
                union_cache[layer_id] = _sg.GeometryCollection()
        unioned = union_cache[layer_id]
        if unioned.is_empty:
            warnings.append(
                f"Copper rename {c.name!r}: no unnamed copper on layer "
                f"{layer_id}; skipped.")
            continue
        # Pick the connected component of NO_NET copper that contains
        # the rename's anchor. Other disjoint NO_NET components stay
        # unaffected.
        anchor = _sg.Point(float(c.anchor_xy[0]), float(c.anchor_xy[1]))
        components = (list(unioned.geoms)
                      if unioned.geom_type == "MultiPolygon"
                      else [unioned])
        match_poly = None
        for comp in components:
            if comp.is_empty:
                continue
            try:
                if comp.contains(anchor):
                    match_poly = comp
                    break
            except Exception:
                continue
        if match_poly is None:
            warnings.append(
                f"Copper rename {c.name!r}: anchor "
                f"({c.anchor_xy[0]:g}, {c.anchor_xy[1]:g}) is not on a "
                f"NO_NET copper polygon on layer {layer_id}; skipped.")
            continue
        # Assign / reuse a net_index for the user's name. Reusing an
        # existing entry is fine — names are unique by the time they
        # reach here (the UI rejects collisions with named nets).
        if c.name in name_to_index:
            net_idx = name_to_index[c.name]
        else:
            nets.append(RawNet(name=c.name))
            net_idx = len(nets) - 1
            name_to_index[c.name] = net_idx
        matches.append((layer_id, match_poly, net_idx))

    if not matches:
        # No anchors landed on NO_NET copper — every rename was a no-op
        # (warned above). ``nets`` is still a fresh copy of the original
        # tuple; no replacement needed.
        return warnings

    def _retag_no_net(primitive, poly):
        """If ``primitive.net_index == NO_NET`` and ``poly`` intersects
        any of the rename polygons on its layer, return a new primitive
        with ``net_index`` set to the rename's net index. Otherwise
        return the primitive unchanged."""
        if primitive.net_index != NO_NET or poly is None or poly.is_empty:
            return primitive
        for lid, match_poly, net_idx in matches:
            if lid != primitive.layer_id:
                continue
            try:
                if match_poly.intersects(poly):
                    return dataclasses.replace(primitive, net_index=net_idx)
            except Exception:
                continue
        return primitive

    # Walk each primitive list once; cheap-poly the geometry only for
    # NO_NET entries on a layer that has a rename.
    rename_layers = {lid for lid, _, _ in matches}

    def _maybe(prim, poly_fn):
        if prim.net_index != NO_NET or prim.layer_id not in rename_layers:
            return prim
        try:
            poly = poly_fn(prim)
        except Exception:
            return prim
        return _retag_no_net(prim, poly)

    new_tracks = tuple(
        _maybe(t, _track_polygon) if (
            not t.is_keepout and not t.is_polygon_outline and t.width_mm > 0
        ) else t
        for t in extracted.tracks
    )
    new_arcs = tuple(
        _maybe(a, _arc_polygon) if (
            not a.is_keepout and a.width_mm > 0
        ) else a
        for a in extracted.arcs
    )
    new_regions = tuple(
        _maybe(r, _region_polygon) if (
            not r.is_keepout and not r.is_polygon_outline
            and not r.is_board_cutout and r.kind == 0
            and len(r.outline) >= 3
        ) else r
        for r in extracted.regions
    )
    new_sbr = tuple(
        _maybe(r, _shape_based_region_polygon) if (
            not r.is_keepout and not r.is_polygon_outline
            and not r.is_board_cutout and r.kind == 0
            and len(r.outline) >= 3
        ) else r
        for r in extracted.shape_based_regions
    )
    new_fills = tuple(
        _maybe(f, _fill_polygon) if not f.is_keepout else f
        for f in extracted.fills
    )

    # Vias and through-hole pads also start out NO_NET on a Gerber-sourced
    # project (Gerber + Excellon carry no net info). Retag any whose
    # ``center`` lies inside a rename's match polygon on a layer the
    # via/pad spans. Without this, the via-coupling network in
    # build_problem drops these terminals and multi-layer rails stay
    # disconnected. On Altium-sourced projects vias arrive pre-tagged so
    # this loop is a no-op.
    from fypa.altium_geometry import MULTI_LAYER_PAD_LAYER_ID

    def _via_layers(v):
        lo = min(v.layer_start, v.layer_end)
        hi = max(v.layer_start, v.layer_end)
        return [lid for lid in enabled if lo <= lid <= hi]

    def _retag_via(v):
        if v.net_index != NO_NET:
            return v
        if not any(lid in rename_layers for lid in _via_layers(v)):
            return v
        anchor_pt = _sg.Point(float(v.center.x), float(v.center.y))
        for lid, match_poly, net_idx in matches:
            if lid not in _via_layers(v):
                continue
            try:
                if match_poly.contains(anchor_pt):
                    return dataclasses.replace(v, net_index=net_idx)
            except Exception:
                continue
        return v

    def _pad_layers(p):
        if p.layer_id == MULTI_LAYER_PAD_LAYER_ID:
            return list(enabled)
        return [p.layer_id]

    def _retag_pad(p):
        if p.net_index != NO_NET:
            return p
        if not any(lid in rename_layers for lid in _pad_layers(p)):
            return p
        anchor_pt = _sg.Point(float(p.center.x), float(p.center.y))
        for lid, match_poly, net_idx in matches:
            if lid not in _pad_layers(p):
                continue
            try:
                if match_poly.contains(anchor_pt):
                    return dataclasses.replace(p, net_index=net_idx)
            except Exception:
                continue
        return p

    new_vias = tuple(_retag_via(v) for v in extracted.vias)
    new_pads = tuple(_retag_pad(p) for p in extracted.pads)

    loaded.extracted = dataclasses.replace(
        extracted,
        nets=tuple(nets),
        tracks=new_tracks,
        arcs=new_arcs,
        regions=new_regions,
        shape_based_regions=new_sbr,
        fills=new_fills,
        vias=new_vias,
        pads=new_pads,
    )

    # If the loaded project cached its lazy unioned geometry, it's stale
    # now — drop the cache so the next access rebuilds against the
    # renamed primitives.
    loaded.__dict__.pop("geometry", None)

    log.info(
        "apply_copper_names: applied %d rename(s), %d warning(s).",
        len(matches), len(warnings),
    )
    return warnings
