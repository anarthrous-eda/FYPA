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
    import time

    import shapely.geometry as _sg
    import shapely.ops as _sops
    import shapely.strtree as _st

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

    t_total0 = time.monotonic()
    timings: dict[str, float] = {}
    extracted = loaded.extracted
    nets = list(extracted.nets)
    name_to_index: dict[str, int] = {n.name: i for i, n in enumerate(nets)}

    # Per-rename: ``layer_id`` and a prepared polygon representing one
    # connected component of NO_NET copper. After flood-filling across
    # vias / through-hole pads, a single rename can produce many match
    # records (one per electrically-connected NO_NET component reached
    # across layers). Primitives matching ``net_index == NO_NET`` and
    # overlapping any of these polygons get re-pointed at the rename's
    # net_index.
    matches: list[tuple[int, _sg.base.BaseGeometry, int]] = []
    enabled = extracted.enabled_copper_layer_ids()

    from fypa.altium_geometry import MULTI_LAYER_PAD_LAYER_ID

    def _bridge_layers_for_via(v) -> list[int]:
        lo = min(v.layer_start, v.layer_end)
        hi = max(v.layer_start, v.layer_end)
        return [lid for lid in enabled if lo <= lid <= hi]

    def _bridge_layers_for_pad(p) -> list[int]:
        if p.is_through_hole or p.layer_id == MULTI_LAYER_PAD_LAYER_ID:
            return list(enabled)
        return [p.layer_id]

    # Cross-layer bridges: vias + through-hole pads currently labelled
    # NO_NET (Gerber-sourced projects haven't tagged them yet). Each
    # bridge has a centre and the set of enabled layers it spans, so the
    # flood-fill below can step from one layer's component to another's
    # along the bridge.
    t0 = time.monotonic()
    no_net_bridges: list[tuple[_sg.Point, list[int]]] = []
    for v in extracted.vias:
        if v.net_index != NO_NET:
            continue
        layers = _bridge_layers_for_via(v)
        if len(layers) <= 1:
            continue
        no_net_bridges.append(
            (_sg.Point(float(v.center.x), float(v.center.y)), layers))
    for p in extracted.pads:
        if p.net_index != NO_NET:
            continue
        if not (p.is_through_hole or p.layer_id == MULTI_LAYER_PAD_LAYER_ID):
            continue
        layers = _bridge_layers_for_pad(p)
        if len(layers) <= 1:
            continue
        no_net_bridges.append(
            (_sg.Point(float(p.center.x), float(p.center.y)), layers))
    timings["bridge collection"] = time.monotonic() - t0

    # Single-pass collection of NO_NET primitives across all enabled
    # layers. Building each primitive's shapely polygon ONCE and caching
    # it by primitive id lets the retag step at the end skip the
    # rebuild — on a Gerber-sourced board with tens of thousands of SBRs
    # the rebuild used to be the dominant cost of the resolve.
    t0 = time.monotonic()
    pieces_by_layer: dict[int, list[_sg.base.BaseGeometry]] = {}
    prim_poly_cache: dict[int, _sg.base.BaseGeometry] = {}
    enabled_set = set(enabled)

    def _take(prim, poly):
        if poly is None or poly.is_empty:
            return
        prim_poly_cache[id(prim)] = poly
        pieces_by_layer.setdefault(prim.layer_id, []).append(poly)

    for t in extracted.tracks:
        if (t.layer_id in enabled_set and t.net_index == NO_NET
                and not t.is_keepout and not t.is_polygon_outline
                and t.width_mm > 0):
            try:
                _take(t, _track_polygon(t))
            except Exception:
                continue
    for a in extracted.arcs:
        if (a.layer_id in enabled_set and a.net_index == NO_NET
                and not a.is_keepout and a.width_mm > 0):
            try:
                _take(a, _arc_polygon(a))
            except Exception:
                continue
    for r in extracted.regions:
        if (r.layer_id in enabled_set and r.net_index == NO_NET
                and not r.is_keepout and not r.is_polygon_outline
                and not r.is_board_cutout and r.kind == 0
                and len(r.outline) >= 3):
            try:
                _take(r, _region_polygon(r))
            except Exception:
                continue
    # SBRs are the bulk of a Gerber-sourced board's geometry (tens of
    # thousands per project). The straight-edge / no-hole-with-arcs case
    # (every Gerber-rendered SBR, plus most Altium ShapeBasedRegions6
    # entries) goes through a vectorised batch path: one
    # ``shapely.linearrings`` + one ``shapely.polygons`` C call builds all
    # exteriors + holes at once, dropping ~10× the per-instance Python
    # overhead. SBRs with arc edges fall through to the slow path —
    # discretising arcs has to happen per-region anyway.
    import numpy as _np
    import shapely as _shp

    sbr_fast: list = []          # (prim, outline_xy, [hole_xy, ...])
    sbr_slow: list = []          # primitives that need the slow path
    for r in extracted.shape_based_regions:
        if not (r.layer_id in enabled_set and r.net_index == NO_NET
                and not r.is_keepout and not r.is_polygon_outline
                and not r.is_board_cutout and r.kind == 0
                and len(r.outline) >= 3):
            continue
        if any(v.is_arc and v.radius_mm > 0.0 for v in r.outline):
            sbr_slow.append(r)
            continue
        sbr_fast.append(r)

    if sbr_fast:
        # One numpy array per ring; concat once. Per-vertex tuple gather
        # is still in Python, but the shapely construction itself is one
        # vectorised C dispatch. The whole batch path is wrapped in
        # try/except — any shapely / numpy upset (API drift, malformed
        # ring, etc.) falls back to the per-region builder so the resolve
        # still completes instead of bubbling up as an opaque crash.
        batch_polys = None
        try:
            ext_arrays = [
                _np.asarray([(v.pos.x, v.pos.y) for v in r.outline],
                            dtype=_np.float64)
                for r in sbr_fast
            ]
            ext_sizes = _np.fromiter((len(a) for a in ext_arrays),
                                     dtype=_np.int64, count=len(ext_arrays))
            ext_coords = _np.concatenate(ext_arrays, axis=0)
            ext_indices = _np.repeat(_np.arange(len(sbr_fast)), ext_sizes)
            ext_rings = _shp.linearrings(ext_coords, indices=ext_indices)

            hole_arrays: list = []
            hole_owner: list[int] = []
            for i, r in enumerate(sbr_fast):
                for ring in r.holes:
                    if not ring:
                        continue
                    hole_arrays.append(_np.asarray(
                        [(p.x, p.y) for p in ring], dtype=_np.float64))
                    hole_owner.append(i)

            if hole_arrays:
                # shapely 2.x: when ``indices=`` is given, ALL rings
                # (exterior + holes) live in the same flat array, the
                # first occurrence of each index becomes the shell, and
                # indices must be in increasing order. So: stack
                # exteriors then holes, then stable-sort by index — the
                # exterior of polygon i keeps its position before any
                # holes that target i. Passing ``holes=`` alongside
                # ``indices=`` is explicitly disallowed.
                hole_sizes = _np.fromiter(
                    (len(a) for a in hole_arrays),
                    dtype=_np.int64, count=len(hole_arrays))
                hole_coords = _np.concatenate(hole_arrays, axis=0)
                hole_ring_indices = _np.repeat(
                    _np.arange(len(hole_arrays)), hole_sizes)
                hole_rings = _shp.linearrings(
                    hole_coords, indices=hole_ring_indices)
                all_rings = _np.concatenate([ext_rings, hole_rings])
                all_indices = _np.concatenate([
                    _np.arange(len(sbr_fast), dtype=_np.int64),
                    _np.asarray(hole_owner, dtype=_np.int64),
                ])
                order = _np.argsort(all_indices, kind="stable")
                batch_polys = _shp.polygons(
                    all_rings[order], indices=all_indices[order])
            else:
                batch_polys = _shp.polygons(ext_rings)
        except Exception:
            log.warning(
                "apply_copper_names: SBR batch polygon build failed; "
                "falling back to per-region build", exc_info=True)
            batch_polys = None

        if batch_polys is not None:
            # Validity check is a single vectorised call. Most polys come
            # from GEOS boolean output (Gerber) and are valid by
            # construction; the rare invalid ones fall back to the
            # per-instance sanitiser.
            valid_mask = _shp.is_valid(batch_polys)
            for i, prim in enumerate(sbr_fast):
                poly = batch_polys[i]
                if poly is None or poly.is_empty:
                    continue
                if not bool(valid_mask[i]):
                    try:
                        poly = _shape_based_region_polygon(prim)
                    except Exception:
                        continue
                _take(prim, poly)
        else:
            for prim in sbr_fast:
                try:
                    _take(prim, _shape_based_region_polygon(prim))
                except Exception:
                    continue

    for r in sbr_slow:
        try:
            _take(r, _shape_based_region_polygon(r))
        except Exception:
            continue
    for f in extracted.fills:
        if (f.layer_id in enabled_set and f.net_index == NO_NET
                and not f.is_keepout):
            try:
                _take(f, _fill_polygon(f))
            except Exception:
                continue
    timings["primitive polygon build"] = time.monotonic() - t0

    # Per-layer NO_NET geometry, decomposed into one polygon per
    # electrically-disjoint copper piece, plus an STRtree for fast
    # point-in-polygon queries. Each layer's ``unary_union`` is the
    # dominant cost on a Gerber board (thousands of disjoint pieces per
    # layer, 16 layers) — shapely 2 releases the GIL inside union_all,
    # so we fan out across a thread pool the same way
    # :func:`fypa.altium_geometry._parallel_union_buckets` does.
    t0 = time.monotonic()
    import shapely

    def _union_layer(lid_pieces):
        lid, pieces = lid_pieces
        try:
            unioned = shapely.union_all(pieces)
        except Exception:
            unioned = _sops.unary_union(pieces)
        if unioned.is_empty:
            return lid, None
        comps = (list(unioned.geoms)
                 if unioned.geom_type == "MultiPolygon"
                 else [unioned])
        comps = [c for c in comps if not c.is_empty]
        if not comps:
            return lid, None
        return lid, {"shapes": comps, "tree": _st.STRtree(comps)}

    layer_index: dict[int, dict] = {}
    big_layers = sum(1 for v in pieces_by_layer.values() if len(v) > 200)
    if big_layers >= 2 and len(pieces_by_layer) >= 2:
        import concurrent.futures
        import os
        # Cap workers — union_all releases the GIL but still pegs a core
        # per task; min(8, cpu_count()) is plenty.
        max_workers = min(8, (os.cpu_count() or 4),
                          len(pieces_by_layer))
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=max_workers) as ex:
            for lid, data in ex.map(_union_layer, pieces_by_layer.items()):
                if data is not None:
                    layer_index[lid] = data
    else:
        for item in pieces_by_layer.items():
            lid, data = _union_layer(item)
            if data is not None:
                layer_index[lid] = data
    timings["per-layer union + STRtree"] = time.monotonic() - t0

    if not layer_index:
        # Nothing to rename — every rename will hit the "no unnamed copper"
        # warning path. Skip the heavy setup.
        for c in copper_names:
            layer_id = int(c.layer_id)
            if layer_id not in enabled:
                warnings.append(
                    f"Copper rename {c.name!r}: layer {layer_id} is not "
                    "in the enabled copper stack; skipped.")
            else:
                warnings.append(
                    f"Copper rename {c.name!r}: no unnamed copper on "
                    f"layer {layer_id}; skipped.")
        return warnings

    # Union-find over (layer_id, component_idx) keys. Each via / THP-pad
    # unions the components its centre falls inside, so a flood query for
    # a rename's anchor component collapses to a single union-find lookup.
    parent: dict[tuple[int, int], tuple[int, int]] = {}
    for lid, data in layer_index.items():
        for i in range(len(data["shapes"])):
            parent[(lid, i)] = (lid, i)

    def _uf_find(x):
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def _uf_union(a, b):
        ra, rb = _uf_find(a), _uf_find(b)
        if ra != rb:
            parent[rb] = ra

    def _component_for(layer_id: int, point) -> int | None:
        """Index of the layer's NO_NET component containing ``point``,
        or ``None``. STRtree-prefiltered."""
        data = layer_index.get(layer_id)
        if data is None:
            return None
        try:
            cand = data["tree"].query(point)
        except Exception:
            return None
        shapes = data["shapes"]
        for j in cand:
            try:
                idx = int(j)
            except Exception:
                continue
            try:
                if shapes[idx].intersects(point):
                    return idx
            except Exception:
                continue
        return None

    t0 = time.monotonic()
    for centre, span_layers in no_net_bridges:
        hits: list[tuple[int, int]] = []
        for lid in span_layers:
            idx = _component_for(lid, centre)
            if idx is not None:
                hits.append((lid, idx))
        for h in hits[1:]:
            _uf_union(hits[0], h)

    members_of: dict[tuple[int, int], set[tuple[int, int]]] = {}
    for key in parent:
        root = _uf_find(key)
        members_of.setdefault(root, set()).add(key)
    timings["bridge union-find"] = time.monotonic() - t0

    # Assign a rename's net_idx to every component in its anchor's
    # union-find root. First rename wins on overlap (a later rename
    # whose anchor lands on an already-claimed component is skipped).
    t0 = time.monotonic()
    component_net: dict[tuple[int, int], int] = {}

    for c in copper_names:
        layer_id = int(c.layer_id)
        if layer_id not in enabled:
            warnings.append(
                f"Copper rename {c.name!r}: layer {layer_id} is not in "
                "the enabled copper stack; skipped.")
            continue
        data = layer_index.get(layer_id)
        if data is None:
            warnings.append(
                f"Copper rename {c.name!r}: no unnamed copper on layer "
                f"{layer_id}; skipped.")
            continue
        anchor = _sg.Point(float(c.anchor_xy[0]), float(c.anchor_xy[1]))
        anchor_idx = _component_for(layer_id, anchor)
        if anchor_idx is None:
            warnings.append(
                f"Copper rename {c.name!r}: anchor "
                f"({c.anchor_xy[0]:g}, {c.anchor_xy[1]:g}) is not on a "
                f"NO_NET copper polygon on layer {layer_id}; skipped.")
            continue
        if c.name in name_to_index:
            net_idx = name_to_index[c.name]
        else:
            nets.append(RawNet(name=c.name))
            net_idx = len(nets) - 1
            name_to_index[c.name] = net_idx
        root = _uf_find((layer_id, anchor_idx))
        for key in members_of.get(root, ()):
            if key in component_net:
                continue
            component_net[key] = net_idx

    # Flatten to the (layer_id, component_polygon, net_idx) tuples the
    # retag helpers below already know how to consume.
    for (lid, idx), net_idx in component_net.items():
        matches.append((lid, layer_index[lid]["shapes"][idx], net_idx))
    timings["rename closure"] = time.monotonic() - t0

    if not matches:
        # No anchors landed on NO_NET copper — every rename was a no-op
        # (warned above). ``nets`` is still a fresh copy of the original
        # tuple; no replacement needed.
        log.info("apply_copper_names: applied 0, %d warning(s), %.2fs",
                 len(warnings), time.monotonic() - t_total0)
        return warnings

    # Component-key → net_idx for direct primitive lookup. A primitive
    # whose polygon belongs to a renamed component gets that component's
    # net_idx, so the retag step doesn't have to re-test geometry.
    # Build a per-layer STRtree of *all* layer components (not just the
    # renamed ones) so a primitive's polygon can be matched back to a
    # component in O(log components).
    t0 = time.monotonic()
    rename_layers: set[int] = {lid for (lid, _), _ in component_net.items()}

    def _retag_from_polygon(primitive, poly):
        """Look up which renamed component ``poly`` falls in (one query
        against the layer's STRtree) and re-point the primitive's
        net_index. Returns ``primitive`` unchanged when nothing claims
        ``poly``."""
        if primitive.net_index != NO_NET or poly is None or poly.is_empty:
            return primitive
        data = layer_index.get(primitive.layer_id)
        if data is None:
            return primitive
        try:
            cand = data["tree"].query(poly)
        except Exception:
            return primitive
        shapes = data["shapes"]
        for j in cand:
            try:
                idx = int(j)
            except Exception:
                continue
            net_idx = component_net.get((primitive.layer_id, idx))
            if net_idx is None:
                continue
            try:
                if shapes[idx].intersects(poly):
                    return dataclasses.replace(
                        primitive, net_index=net_idx)
            except Exception:
                continue
        return primitive

    def _maybe(prim):
        """Retag a NO_NET primitive on a rename layer using the cached
        polygon — never rebuilds the polygon, never iterates matches."""
        if prim.net_index != NO_NET or prim.layer_id not in rename_layers:
            return prim
        poly = prim_poly_cache.get(id(prim))
        if poly is None:
            return prim
        return _retag_from_polygon(prim, poly)

    new_tracks = tuple(_maybe(t) for t in extracted.tracks)
    new_arcs = tuple(_maybe(a) for a in extracted.arcs)
    new_regions = tuple(_maybe(r) for r in extracted.regions)
    new_sbr = tuple(_maybe(r) for r in extracted.shape_based_regions)
    new_fills = tuple(_maybe(f) for f in extracted.fills)
    timings["primitive retag"] = time.monotonic() - t0

    # Vias and through-hole pads also start out NO_NET on a Gerber-sourced
    # project (Gerber + Excellon carry no net info). Retag any whose
    # ``center`` lies inside a renamed component on a layer the via/pad
    # spans. Without this, the via-coupling network in build_problem
    # drops these terminals and multi-layer rails stay disconnected. On
    # Altium-sourced projects vias arrive pre-tagged so this loop is a
    # no-op.
    t0 = time.monotonic()

    def _retag_point_centred(span_layers: list[int], centre):
        """Find a renamed component on one of ``span_layers`` whose
        polygon contains ``centre``. Returns the rename's ``net_idx`` or
        ``None``. STRtree-prefiltered via the per-layer component tree."""
        for lid in span_layers:
            if lid not in rename_layers:
                continue
            idx = _component_for(lid, centre)
            if idx is None:
                continue
            net_idx = component_net.get((lid, idx))
            if net_idx is not None:
                return net_idx
        return None

    def _retag_via(v):
        if v.net_index != NO_NET:
            return v
        v_layers = _bridge_layers_for_via(v)
        if not any(lid in rename_layers for lid in v_layers):
            return v
        net_idx = _retag_point_centred(
            v_layers, _sg.Point(float(v.center.x), float(v.center.y)))
        if net_idx is None:
            return v
        return dataclasses.replace(v, net_index=net_idx)

    def _retag_pad(p):
        if p.net_index != NO_NET:
            return p
        p_layers = _bridge_layers_for_pad(p)
        if not any(lid in rename_layers for lid in p_layers):
            return p
        net_idx = _retag_point_centred(
            p_layers, _sg.Point(float(p.center.x), float(p.center.y)))
        if net_idx is None:
            return p
        return dataclasses.replace(p, net_index=net_idx)

    new_vias = tuple(_retag_via(v) for v in extracted.vias)
    new_pads = tuple(_retag_pad(p) for p in extracted.pads)
    timings["via/pad retag"] = time.monotonic() - t0

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

    total = time.monotonic() - t_total0
    log.info(
        "apply_copper_names: applied %d rename(s), %d warning(s), %.2fs "
        "(%s)",
        len(matches), len(warnings), total,
        ", ".join(f"{k}={v*1000:.0f}ms" for k, v in timings.items()),
    )
    return warnings
