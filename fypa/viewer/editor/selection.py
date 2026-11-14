"""Editor-mode selection and connectivity highlight."""
from __future__ import annotations

import logging

import numpy as np
import shapely
import shapely.geometry as _sg
import shapely.prepared as _sp

from fypa.viewer.overlays import _EDITOR_OVERLAY_DIM_ALPHA, _EDITOR_OVERLAY_DIM_BG


def _polygon_from_record(poly: dict):
    """One ``all_copper`` polygon record → shapely Polygon, or ``None`` when
    it has no exterior, fails to build, or is empty."""
    ext = poly.get("exterior")
    if ext is None or (hasattr(ext, "size") and ext.size == 0):
        return None
    try:
        shp = _sg.Polygon(ext, poly.get("holes") or [])
    except Exception:
        return None
    return None if shp.is_empty else shp


def _closed_xy_ring(r) -> bool:
    return (isinstance(r, np.ndarray) and r.ndim == 2 and r.shape[1] == 2
            and len(r) >= 4 and bool((r[0] == r[-1]).all()))


def _polygons_from_records(polys: list[dict]) -> list:
    """:func:`_polygon_from_record` over a list, building every record whose
    rings are already closed ``(n, 2)`` arrays in one
    ``shapely.from_ragged_array`` call (~10× faster than a Polygon per
    record on a 200k-polygon board). Anything else takes the per-record
    path, so the result matches it either way."""
    out: list = [None] * len(polys)
    fast: list[int] = []
    rings: list[np.ndarray] = []
    rings_per_poly: list[int] = []
    for i, poly in enumerate(polys):
        ext = poly.get("exterior")
        holes = poly.get("holes") or []
        if _closed_xy_ring(ext) and all(_closed_xy_ring(h) for h in holes):
            fast.append(i)
            rings.append(ext)
            rings.extend(holes)
            rings_per_poly.append(1 + len(holes))
        else:
            out[i] = _polygon_from_record(poly)
    if fast:
        try:
            coords = np.concatenate(rings).astype(np.float64, copy=False)
            ring_off = np.zeros(len(rings) + 1, dtype=np.int64)
            np.cumsum([len(r) for r in rings], out=ring_off[1:])
            poly_off = np.zeros(len(fast) + 1, dtype=np.int64)
            np.cumsum(rings_per_poly, out=poly_off[1:])
            geoms = shapely.from_ragged_array(
                shapely.GeometryType.POLYGON, coords, (ring_off, poly_off))
            empty = shapely.is_empty(geoms)
            for i, g, e in zip(fast, geoms.tolist(), empty.tolist()):
                out[i] = None if e else g
        except Exception:
            for i in fast:
                out[i] = _polygon_from_record(polys[i])
    return out


def _bridge_polygon_hits_slow(indexed: dict, bridges: list
                              ) -> list[list[tuple[int, int]]]:
    """Per-via reference for :func:`_bridge_polygon_hits_bulk`: for each
    bridge, the first ``all_copper`` polygon (in STRtree candidate order)
    its centre lies in or on, on each spanned layer, as ``(layer, id(poly))``
    keys in span order. Polygons whose predicate raises are skipped."""
    out: list[list[tuple[int, int]]] = []
    for cx, cy, span in bridges:
        pt = _sg.Point(cx, cy)
        hits: list[tuple[int, int]] = []
        for lid in span:
            data = indexed.get(lid)
            if data is None:
                continue
            try:
                cand = data["tree"].query(pt)
            except Exception:
                continue
            shapes = data["shapes"]
            polys = data["polys"]
            for j in cand:
                try:
                    idx = int(j)
                except Exception:
                    continue
                try:
                    if shapes[idx].intersects(pt):
                        hits.append((lid, id(polys[idx])))
                        break
                except Exception:
                    continue
        out.append(hits)
    return out


def _bridge_polygon_hits_bulk(indexed: dict, bridges: list
                              ) -> list[list[tuple[int, int]]]:
    """Same result as :func:`_bridge_polygon_hits_slow`, computed per layer
    with one bulk STRtree query and one vectorised ``intersects`` against
    the *prepared* candidate polygons. The per-via form tested every via
    against big unprepared pours one call at a time - ~30 s on a large
    board, vs. well under a second here. Raises if any predicate fails
    (invalid geometry); the caller then falls back to the per-via form."""
    nb = len(bridges)
    if not nb:
        return []
    pts = shapely.points(
        np.fromiter((b[0] for b in bridges), dtype=float, count=nb),
        np.fromiter((b[1] for b in bridges), dtype=float, count=nb),
    )
    on_layer: dict[int, list[int]] = {}
    for i, (_cx, _cy, span) in enumerate(bridges):
        for lid in span:
            on_layer.setdefault(lid, []).append(i)
    # first_hit[lid][i] = polygon index hit by bridge i on lid, else -1.
    first_hit: dict[int, np.ndarray] = {}
    for lid, members in on_layer.items():
        data = indexed.get(lid)
        if data is None:
            continue
        idx = np.asarray(members, dtype=np.int64)
        src, dst = data["tree"].query(pts[idx])
        if not len(src):
            continue
        shapes = np.asarray(data["shapes"], dtype=object)
        shapely.prepare(shapes[np.unique(dst)])
        hit = shapely.intersects(shapes[dst], pts[idx[src]])
        src, dst = src[hit], dst[hit]
        if not len(src):
            continue
        order = np.lexsort((dst, src))
        src, dst = src[order], dst[order]
        uniq, first, counts = np.unique(
            src, return_index=True, return_counts=True)
        arr = np.full(nb, -1, dtype=np.int64)
        arr[idx[uniq]] = dst[first]
        # The per-via form keeps the first intersecting candidate in a
        # single-point query's order (tree traversal, not index order). A
        # via inside two overlapping polygons on one layer is rare, so
        # redo just those with that query to pick the same one.
        for k in np.flatnonzero(counts > 1).tolist():
            s = int(uniq[k])
            hit_set = set(dst[first[k]:first[k] + counts[k]].tolist())
            for c in data["tree"].query(pts[idx[s]]).tolist():
                if c in hit_set:
                    arr[idx[s]] = c
                    break
        first_hit[lid] = arr
    out: list[list[tuple[int, int]]] = []
    for i, (_cx, _cy, span) in enumerate(bridges):
        hits: list[tuple[int, int]] = []
        for lid in span:
            arr = first_hit.get(lid)
            if arr is None:
                continue
            j = int(arr[i])
            if j >= 0:
                hits.append((lid, id(indexed[lid]["polys"][j])))
        out.append(hits)
    return out


class _EditorSelectionMixin:
    """Editor-mode selection and connectivity highlight."""

    # --- Editor mode: selection + connectivity highlight --------------------

    def _editor_alpha_array(self, layer_probes: list[dict],
                            total_vertices: int) -> np.ndarray | None:
        """Per-vertex alpha for editor-mode copper dimming: 1.0 for copper
        in the connectivity highlight, 0.1 for the rest - or a hard 0.0 for
        everything outside the focused net while a net focus is held.
        Returns ``None`` when neither is active (the mesh then draws fully
        opaque)."""
        focus_nets = (self._editor_focus_nets
                      if self._editor_focus_active() else None)
        if focus_nets is None and (
                not self._editor_mode or not self._editor_highlight_nets):
            return None
        hi = self._editor_highlight_nets
        parts: list[np.ndarray] = []
        # layer_probes is top-first; the combined mesh was built
        # bottom-first, so iterate reversed to match the xs/ys/tris order.
        for p in reversed(layer_probes):
            n = int(p.get("n_vertices", 0))
            if n <= 0:
                continue
            if focus_nets is not None:
                a = 1.0 if p.get("net") in focus_nets else 0.0
            else:
                a = 1.0 if p.get("net") in hi else 0.1
            parts.append(np.full(n, a, dtype=np.float32))
        if not parts:
            return None
        arr = np.concatenate(parts)
        if arr.size != total_vertices:
            return None   # ordering mismatch - skip dimming, never crash
        return arr

    def _combined_mesh_alpha_array(self, layer_probes: list[dict],
                                    total_vertices: int) -> np.ndarray | None:
        """Per-vertex alpha for the heatmap mesh, combining the per-layer
        TransparencyButton setting with editor-mode connectivity dimming.

        For each (phys, net) slice in the combined mesh, the alpha is
        ``layer_transparency_alpha * editor_dim_alpha``. Returns ``None``
        when no adjustment is needed (every layer fully opaque AND no
        editor highlight active) so the mesh draws on the GPU fast path."""
        if not layer_probes:
            return None
        transp_by_name = dict(
            getattr(self, "_layer_transparency_buttons", []))
        editor_hi = (self._editor_highlight_nets
                      if (self._editor_mode
                          and self._editor_highlight_nets) else None)
        # Net focus takes the mesh to alpha 0 outside the focused net - the
        # mesh is per-(layer, net), so the net name alone decides. A focus on
        # synthetic / unnamed copper has no real net in ``_editor_focus_nets``
        # and correctly blanks the whole mesh: none of the solved rails is the
        # copper the user asked to see.
        focus_nets = (self._editor_focus_nets
                      if self._editor_focus_active() else None)

        # Cheap pre-check - bail out to the fast path when nothing wants a
        # non-1.0 alpha. The mesh shader's constant attribute is faster
        # than uploading + blending a per-vertex array that's all 1s.
        layer_dim = any(
            (transp_by_name.get(lp.get("physical")) is not None
             and transp_by_name[lp.get("physical")].alpha() < 1.0)
            for lp in layer_probes
        )
        if not layer_dim and editor_hi is None and focus_nets is None:
            return None

        parts: list[np.ndarray] = []
        # layer_probes is top-first; the combined mesh was built
        # bottom-first, so iterate reversed to match the xs/ys/tris order.
        for lp in reversed(layer_probes):
            n = int(lp.get("n_vertices", 0))
            if n <= 0:
                continue
            tb = transp_by_name.get(lp.get("physical"))
            layer_a = tb.alpha() if tb is not None else 1.0
            if focus_nets is not None:
                editor_a = 1.0 if lp.get("net") in focus_nets else 0.0
            elif editor_hi is not None:
                editor_a = 1.0 if lp.get("net") in editor_hi else 0.1
            else:
                editor_a = 1.0
            parts.append(np.full(n, float(layer_a * editor_a),
                                  dtype=np.float32))
        if not parts:
            return None
        arr = np.concatenate(parts)
        if arr.size != total_vertices:
            return None   # ordering mismatch - skip dimming, never crash
        return arr

    def _push_mesh_alpha(self) -> None:
        """Re-push the heatmap mesh's per-vertex alpha array from the
        cached ``self._layer_probes``. Used by the layer Transparency
        toggle to update the mesh shading without rebuilding geometry."""
        gv = getattr(self, "_gl_viewer", None)
        if gv is None or not self._layer_probes:
            return
        # The uploaded batch covers hidden layers too, so the alpha array
        # is built over every block of it, not just the visible probes.
        blocks = self._mesh_alpha_blocks()
        total = sum(int(lp.get("n_vertices", 0)) for lp in blocks)
        if total <= 0:
            return
        gv.set_vertex_alpha(
            self._combined_mesh_alpha_array(blocks, total)
        )

    def _editor_dim_rgb(self, rgb: tuple[float, float, float],
                        net: str | None,
                        layer_id: int | None = None,
                        poly: dict | None = None,
                        ) -> tuple[float, float, float]:
        """Dim an all-copper overlay colour for editor-mode connectivity
        focus. With a selection highlight active, copper whose ``net`` is
        not in the highlight is blended toward the editor background - the
        same recede the heatmap mesh gets from :meth:`_editor_alpha_array`.
        Highlighted copper, and any time no highlight is active, is
        returned unchanged.

        ``layer_id`` and ``poly``: when supplied, the polygon-identity
        highlight set is consulted too. This is what keeps a single
        unnamed-copper rail lit while disjoint unnamed pieces dim - the
        net name alone (``"(none)"``) can't tell them apart."""
        if not self._editor_mode:
            return rgb
        if (not self._editor_highlight_nets
                and not self._editor_highlight_polys):
            return rgb
        if net and net in self._editor_highlight_nets:
            return rgb
        if (poly is not None and layer_id is not None
                and (int(layer_id), id(poly)) in self._editor_highlight_polys):
            return rgb
        f = _EDITOR_OVERLAY_DIM_ALPHA
        bg = _EDITOR_OVERLAY_DIM_BG
        return (rgb[0] * f + bg[0] * (1.0 - f),
                rgb[1] * f + bg[1] * (1.0 - f),
                rgb[2] * f + bg[2] * (1.0 - f))

    def _connected_nets(self, net: str | None) -> set[str]:
        """Nets electrically connected to ``net`` - the net itself plus
        anything bridged to it by a SERIES directive. Same-net copper is
        connected by definition, including across layers through vias.

        Solved SERIES bridges are folded in via the rail-group union-find
        (``_rail_to_members``); editor SERIES directives the user has
        placed but not yet resolved are folded in here as well, so the
        connectivity highlight spans them before a re-solve."""
        if not net:
            return set()
        group: set[str] = {net}
        for members in self._rail_to_members.values():
            if net in members:
                group = set(members)
                break
        # Each editor SERIES directive shorts its P and N nets together.
        # Iterate to closure so a chain of bridges resolves transitively.
        bridges = [
            (d.p_net, d.n_net)
            for d in (self._project.editor_directives
                      if self._project is not None else [])
            if d.role == "SERIES" and d.p_net and d.n_net
        ]
        changed = True
        while changed:
            changed = False
            for a, b in bridges:
                if a in group and b not in group:
                    group.add(b)
                    changed = True
                elif b in group and a not in group:
                    group.add(a)
                    changed = True
        return group

    def _component_side_visible(self, comp: dict) -> bool:
        """Whether the component's side is currently drawn - i.e. the
        Components overlay (Board Features) is showing that side.

        Editor-mode selection skips components on a hidden side: you
        can't select what you can't see. This mirrors the exact
        visibility test in :meth:`_refresh_overlay_geometry` (a side is
        drawn when its overlay ``vis`` is not ``None``), so selection and
        the on-screen component boxes always agree. Components with no /
        unknown side info, or before the overlay state is built, are
        never blocked."""
        side = comp.get("side")
        if side not in ("top", "bottom"):
            return True
        try:
            vis, _solid = self._overlay_side_states("components").get(
                side, (None, None))
        except (AttributeError, KeyError):
            return True
        return vis is not None

    def _component_at(self, world_x: float, world_y: float, *,
                      visible_sides_only: bool = True) -> dict | None:
        """Metadata component record whose bounding box covers the point,
        or ``None``. Smallest box wins when component boxes nest.

        With ``visible_sides_only`` (the default, used for editor-mode
        selection) a component whose side's copper layer is hidden is
        skipped, so the pick can never land on an invisible component -
        and still finds the smallest *visible* one when sides overlap."""
        if not self.metadata:
            return None
        best: dict | None = None
        best_area: float | None = None
        for rec in self.metadata.get("components", []):
            if visible_sides_only and not self._component_side_visible(rec):
                continue
            bbox = rec.get("bbox")
            if not bbox or len(bbox) != 4:
                continue
            x0, y0, x1, y1 = bbox
            if x0 <= world_x <= x1 and y0 <= world_y <= y1:
                area = (x1 - x0) * (y1 - y0)
                if best_area is None or area < best_area:
                    best, best_area = rec, area
        return best

    def _all_copper_bridges(self) -> list[tuple[float, float, list[int]]]:
        """Cached list of ``(x_mm, y_mm, span_layer_ids)`` for every via
        and through-hole pad in the metadata. Used by the connected-copper
        index - each entry is a candidate inter-layer bridge whose centre
        may lie inside an ``all_copper`` polygon on multiple layers and
        electrically couple them."""
        cached = getattr(self, "_all_copper_bridges_cache", None)
        if cached is not None:
            return cached
        md = self.metadata or {}
        enabled_ids = sorted(self._phys_name_to_layer_id.values())
        bridges: list[tuple[float, float, list[int]]] = []
        for bucket in (md.get("vias") or [], md.get("pths") or []):
            for rec in bucket:
                ls = rec.get("layer_start")
                le = rec.get("layer_end")
                if ls is None or le is None:
                    continue
                lo, hi = (ls, le) if ls <= le else (le, ls)
                span = [lid for lid in enabled_ids if lo <= lid <= hi]
                if len(span) <= 1:
                    continue
                cx = rec.get("x_mm")
                cy = rec.get("y_mm")
                if cx is None or cy is None:
                    continue
                bridges.append((float(cx), float(cy), span))
        self._all_copper_bridges_cache = bridges
        return bridges

    def _layer_strtrees(self) -> dict[int, dict]:
        """Cached per-layer Shapely ``STRtree`` + parallel ``shapes``,
        ``polys`` and ``nets`` lists for fast point-in-polygon lookup against
        the ``all_copper`` overlay. Building a Polygon and an STRtree once
        per layer collapses the O(polys × queries) point tests the flood,
        click and hover handlers used to do into ``O(log polys)`` candidate
        prefilters. ``nets[i]`` is the owning record's net for ``polys[i]``
        (may be falsy for ``"(none)"`` copper)."""
        cached = getattr(self, "_layer_strtrees_cache", None)
        if cached is not None:
            return cached
        import shapely.strtree as _st
        md = self.metadata or {}
        flat: list[tuple[int, dict, object]] = []
        for rec in md.get("all_copper") or []:
            lid = rec.get("layer_id")
            if lid is None:
                continue
            net = rec.get("net")
            for poly in rec.get("polygons", []):
                flat.append((int(lid), poly, net))
        shapes = _polygons_from_records([p for _l, p, _n in flat])
        buckets: dict[int, dict] = {}
        for (lid, poly, net), shp in zip(flat, shapes):
            b = buckets.setdefault(
                lid, {"shapes": [], "polys": [], "nets": []})
            if shp is None:
                continue
            b["shapes"].append(shp)
            b["polys"].append(poly)
            b["nets"].append(net)
        out: dict[int, dict] = {}
        for lid, b in buckets.items():
            if not b["shapes"]:
                continue
            out[lid] = {
                "tree": _st.STRtree(b["shapes"]),
                "shapes": b["shapes"],
                # Polygon areas in one vectorised call - the net table and
                # the region auto-namer sum these over every component.
                "areas": shapely.area(
                    np.asarray(b["shapes"], dtype=object)).tolist(),
                "polys": b["polys"],
                "nets": b["nets"],
            }
        self._layer_strtrees_cache = out
        return out

    def _named_copper_hit_on_layer(self, layer_id: int, pt) -> str | None:
        """Net of the ``all_copper`` polygon on ``layer_id`` containing
        ``pt`` (a shapely ``Point``), or ``None``. STRtree-prefiltered so the
        cost is ``O(log polys)`` per layer. Skips ``"(none)"`` copper - the
        hover / editor picks only want named nets from this helper."""
        data = self._layer_strtrees().get(int(layer_id))
        if data is None:
            return None
        data["shapes"]
        polys = data["polys"]
        nets = data["nets"]
        try:
            cand = data["tree"].query(pt)
        except Exception:
            return None
        # A no-net polygon can overlap netted copper (a free region drawn
        # over a fill), so a "(none)" hit only wins when no real net covers
        # the point too.
        fallback = None
        for j in cand:
            try:
                idx = int(j)
            except Exception:
                continue
            net = nets[idx]
            if not net:
                continue
            if net == "(none)" and fallback is not None:
                continue
            prepped = self._copper_poly_prepared(polys[idx])
            if prepped is None:
                continue
            try:
                if prepped.contains(pt):
                    if net != "(none)":
                        return net
                    fallback = net
            except Exception:
                continue
        return fallback

    def _connected_components_data(self) -> dict:
        """Cached connected-components decomposition of every
        ``all_copper`` polygon, with vias and through-hole pads coupling
        polygons across layers. Returns ``{component_of, members_of}``:
        ``component_of[(layer, id(poly))]`` is the polygon's root key,
        and ``members_of[root]`` is the set of polygons in that root's
        component. Built once per viewer instance - every per-click
        flood-fill collapses to a dict lookup against this index.

        Polygons are coupled iff some via / THP centre lies inside both
        of them on their respective layers. The boundary is treated as
        inside (``intersects``, not ``contains``), so a via that drops
        exactly on the edge of a pour still bridges the two layers.
        No-net (``"(none)"``) polygons are also coupled to any polygon they
        touch or overlap on their own layer."""
        cached = getattr(self, "_cc_cache", None)
        if cached is not None:
            return cached
        indexed = self._layer_strtrees()
        bridges = self._all_copper_bridges()

        parent: dict[tuple[int, int], tuple[int, int]] = {}
        for lid, data in indexed.items():
            for poly in data["polys"]:
                key = (lid, id(poly))
                parent[key] = key

        def find(x):
            root = x
            while parent[root] != root:
                root = parent[root]
            while parent[x] != root:
                parent[x], x = root, parent[x]
            return root

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        try:
            bridge_hits = _bridge_polygon_hits_bulk(indexed, bridges)
        except Exception:
            # Invalid geometry can make the bulk predicate raise; fall back
            # to per-via tests, which skip just the polygons that fail.
            logging.getLogger(__name__).debug(
                "bulk via/PTH coupling failed; per-via fallback",
                exc_info=True)
            bridge_hits = _bridge_polygon_hits_slow(indexed, bridges)
        for hits in bridge_hits:
            for h in hits[1:]:
                union(hits[0], h)

        # Same-layer overlap with no-net copper. Each real net's copper is
        # already one union per layer, but no-net primitives (a free
        # "Place > Region" drawn on top of a netted fill, say) stay
        # separate polygons even though they touch or overlap other copper.
        # Physically they're one piece, so they join that piece's
        # component. Different real nets are never joined to each other
        # here: overlap between them is a short, not one net.
        import numpy as _np
        import shapely as _shp
        for lid, data in indexed.items():
            none_idx = _np.array(
                [i for i, n in enumerate(data["nets"])
                 if not n or n == "(none)"], dtype=_np.int64)
            if not len(none_idx):
                continue
            shapes = _np.asarray(data["shapes"], dtype=object)
            polys = data["polys"]
            try:
                # Bounding-box candidates first, then the exact test with
                # the *other* polygon prepared: the no-net pieces are
                # usually small and the copper they sit on is a big pour
                # with thousands of vertices, which unprepared costs ~20 s
                # across a large board (vs <1 s prepared).
                src, dst = data["tree"].query(shapes[none_idx])
                src = none_idx[src]
                keep = src != dst
                src, dst = src[keep], dst[keep]
                if not len(src):
                    continue
                _shp.prepare(shapes[_np.unique(dst)])
                hit = _shp.intersects(shapes[dst], shapes[src])
                pairs = zip(src[hit].tolist(), dst[hit].tolist())
            except Exception:
                # Invalid geometry can make the bulk predicate raise; fall
                # back to per-pair tests and skip the polygons that fail.
                pairs = []
                for i in none_idx.tolist():
                    try:
                        cand = data["tree"].query(shapes[i])
                    except Exception:
                        continue
                    for j in cand:
                        j = int(j)
                        if j == i:
                            continue
                        try:
                            if shapes[i].intersects(shapes[j]):
                                pairs.append((i, j))
                        except Exception:
                            continue
            for i, j in pairs:
                union((lid, id(polys[i])), (lid, id(polys[j])))

        component_of: dict[tuple[int, int], tuple[int, int]] = {}
        members_of: dict[tuple[int, int], set[tuple[int, int]]] = {}
        for key in parent:
            root = find(key)
            component_of[key] = root
            members_of.setdefault(root, set()).add(key)

        # The real (Altium) net of each component that carries one. A
        # CopperName only names copper that has no net of its own, and a
        # no-net polygon joined to netted copper reports that copper's net.
        real_net_of: dict[tuple[int, int], str] = {}
        for lid, data in indexed.items():
            for poly, net in zip(data["polys"], data["nets"]):
                if net and net != "(none)":
                    root = component_of.get((lid, id(poly)))
                    if root is not None:
                        real_net_of.setdefault(root, net)

        cache = {"component_of": component_of, "members_of": members_of,
                 "real_net_of": real_net_of}
        self._cc_cache = cache
        return cache

    def _connected_copper_polys(self, seed_layer_id: int, seed_poly: dict
                                ) -> set[tuple[int, int]]:
        """Every ``all_copper`` polygon electrically connected to
        ``seed_poly`` on ``seed_layer_id`` - same component in the
        cached union-find. Returns ``{(layer_id, id(poly_dict))}``,
        always including the seed key itself."""
        cc = self._connected_components_data()
        seed_key = (int(seed_layer_id), id(seed_poly))
        root = cc["component_of"].get(seed_key)
        if root is None:
            return {seed_key}
        return set(cc["members_of"].get(root, {seed_key}))

    def _copper_poly_under_point(self, world_x: float, world_y: float,
                                  layer_id: int) -> dict | None:
        """The ``all_copper`` polygon dict on ``layer_id`` containing the
        point, or ``None``. STRtree-prefiltered so the cost is
        ``O(log polys)`` per call rather than scanning every polygon on
        the layer. Works for both ``"(none)"`` and named copper."""
        indexed = self._layer_strtrees()
        data = indexed.get(int(layer_id))
        if data is None:
            return None
        pt = _sg.Point(float(world_x), float(world_y))
        try:
            cand = data["tree"].query(pt)
        except Exception:
            return None
        shapes = data["shapes"]
        polys = data["polys"]
        for j in cand:
            try:
                idx = int(j)
            except Exception:
                continue
            try:
                if shapes[idx].intersects(pt):
                    return polys[idx]
            except Exception:
                continue
        return None

    def _copper_name_at(self, world_x: float, world_y: float,
                        layer_id: int | None):
        """The :class:`~fypa.project_file.CopperName` whose anchor sits
        on copper electrically connected to (``world_x``, ``world_y``,
        ``layer_id``), or ``None`` when none of the project's renames
        apply here. "Connected" means the click polygon and the anchor
        polygon are in the same via-coupled component - so a rename
        placed on top-layer copper is also found when the user clicks
        the same rail's bottom-layer piece.

        Copper connected to a real net is never "named": its net wins, even
        if a stale rename's anchor happens to sit on it."""
        if self._project is None or not self._project.copper_names:
            return None
        if layer_id is None:
            return None
        click_poly = self._copper_poly_under_point(world_x, world_y, layer_id)
        if click_poly is None:
            return None
        cc = self._connected_components_data()
        seed_key = (int(layer_id), id(click_poly))
        root = cc["component_of"].get(seed_key)
        if root is None:
            return None
        if root in cc.get("real_net_of", ()):
            return None
        members = cc["members_of"].get(root)
        if not members:
            return None
        indexed = self._layer_strtrees()
        for c in self._project.copper_names:
            clid = int(c.layer_id)
            data = indexed.get(clid)
            if data is None:
                continue
            anchor = _sg.Point(float(c.anchor_xy[0]), float(c.anchor_xy[1]))
            try:
                cand = data["tree"].query(anchor)
            except Exception:
                continue
            shapes = data["shapes"]
            polys = data["polys"]
            for j in cand:
                try:
                    idx = int(j)
                except Exception:
                    continue
                try:
                    if shapes[idx].intersects(anchor):
                        if (clid, id(polys[idx])) in members:
                            return c
                except Exception:
                    continue
        return None

    def _connected_real_net(self, world_x: float, world_y: float,
                            layer_id: int | None) -> str | None:
        """The real net of the copper connected to the no-net polygon at
        the point, or ``None``. A free region drawn over a netted fill has
        no net of its own but is physically part of that net."""
        if layer_id is None:
            return None
        poly = self._copper_poly_under_point(world_x, world_y, layer_id)
        if poly is None:
            return None
        cc = self._connected_components_data()
        root = cc["component_of"].get((int(layer_id), id(poly)))
        if root is None:
            return None
        return cc.get("real_net_of", {}).get(root)

    def _copper_name_override(self, world_x: float, world_y: float,
                               layer_id: int | None) -> str | None:
        """The user-given net name for the unnamed copper polygon at
        (``world_x``, ``world_y``, ``layer_id``), or ``None``. Thin
        wrapper over :meth:`_copper_name_at`."""
        c = self._copper_name_at(world_x, world_y, layer_id)
        return c.name if c is not None else None

    def _apply_copper_name_to_pick(self, pick: dict | None,
                                    world_x: float, world_y: float
                                    ) -> dict | None:
        """If ``pick`` lands on copper currently named ``"(none)"`` and
        a :class:`CopperName` rename pins it to a real name, return a
        copy with the renamed net. ``None`` / non-"(none)" picks pass
        through unchanged."""
        if pick is None or pick.get("net") != "(none)":
            return pick
        new_name = (self._connected_real_net(
                        world_x, world_y, pick.get("layer_id"))
                    or self._copper_name_override(
                        world_x, world_y, pick.get("layer_id")))
        if new_name is None:
            return pick
        out = dict(pick)
        out["net"] = new_name
        return out

    def _editor_copper_pick(self, world_x: float,
                            world_y: float) -> dict | None:
        """Copper under the point for editor-mode selection / marker
        placement. Tries, in order: the solved rail mesh, the excluded
        stubs, then the full per-(layer, net) copper set. Returns
        ``{"net", "physical", "layer_id"}`` or ``None`` on bare substrate.

        The final fallback to :meth:`_copper_at_point` is what lets a
        source / sink land on *any* copper - ``_probe_at_point`` and
        ``_probe_at_stub`` only see the rail currently drawn in the
        heatmap, so without it placement is limited to that rail.

        Picks that land on copper whose net is ``"(none)"`` are passed
        through :meth:`_apply_copper_name_to_pick` so a user-supplied
        :class:`CopperName` rename surfaces as the new net name."""
        hit = self._probe_at_point(world_x, world_y)
        if hit is not None and hit[1].get("net"):
            info = hit[1]
            return {"net": info["net"],
                    "physical": info.get("physical"),
                    "layer_id": info.get("layer_id")}
        stub_hit = self._probe_at_stub(world_x, world_y)
        if stub_hit is not None and stub_hit[1].get("net"):
            phys = stub_hit[1].get("physical")
            return {"net": stub_hit[1]["net"],
                    "physical": phys,
                    "layer_id": self._phys_name_to_layer_id.get(phys)}
        return self._apply_copper_name_to_pick(
            self._copper_at_point(world_x, world_y), world_x, world_y)

    def _visible_editor_copper_pick(self, world_x: float,
                                    world_y: float) -> dict | None:
        """Like :meth:`_editor_copper_pick`, but the all-copper fallback is
        restricted to layers whose all-copper eye is on. The rail and stub
        probes are inherently visible-only (they walk the rendered heatmap
        / stub geometry), so they're reused as-is; the all-copper fallback
        is what could otherwise hit copper on a hidden layer."""
        hit = self._probe_at_point(world_x, world_y)
        if hit is not None and hit[1].get("net"):
            info = hit[1]
            return {"net": info["net"],
                    "physical": info.get("physical"),
                    "layer_id": info.get("layer_id")}
        stub_hit = self._probe_at_stub(world_x, world_y)
        if stub_hit is not None and stub_hit[1].get("net"):
            phys = stub_hit[1].get("physical")
            return {"net": stub_hit[1]["net"],
                    "physical": phys,
                    "layer_id": self._phys_name_to_layer_id.get(phys)}
        return self._apply_copper_name_to_pick(
            self._all_copper_at_point(world_x, world_y), world_x, world_y)

    def _net_at(self, world_x: float, world_y: float) -> str | None:
        """Copper net under the point - any copper, not just the rail in
        the heatmap. ``None`` on bare substrate."""
        pick = self._editor_copper_pick(world_x, world_y)
        return pick.get("net") if pick else None

    def _copper_poly_prepared(self, poly: dict):
        """Return (and cache) a shapely PreparedGeometry for one
        ``all_copper`` polygon (exterior ring plus any holes). Mirrors
        :meth:`_stub_prepared_shape`; the cache lives on the polygon
        dict so repeated point tests stay cheap."""
        cached = poly.get("_prepared_shape_cache")
        if cached is not None:
            return cached
        ext = poly.get("exterior")
        if ext is None or (hasattr(ext, "size") and ext.size == 0):
            return None
        try:
            shp = _sg.Polygon(ext, poly.get("holes") or [])
        except Exception:
            return None
        if shp.is_empty:
            return None
        prepped = _sp.prep(shp)
        poly["_prepared_shape_cache"] = prepped
        return prepped

    def _copper_at_point(self, world_x: float,
                         world_y: float) -> dict | None:
        """Copper under the point from the full per-(layer, net) copper
        set - every net on every layer, not just the rail currently drawn
        in the heatmap. Returns ``{"net", "physical", "layer_id"}`` for
        the topmost layer whose copper covers the point, or ``None`` on
        bare substrate."""
        md = self.metadata
        if not md:
            return None
        indexed = self._layer_strtrees()
        if not indexed:
            return None
        id_to_phys = {v: k for k, v in self._phys_name_to_layer_id.items()}
        pt = _sg.Point(world_x, world_y)
        # Walk indexed layers top-first (lowest stackup rank) and return the
        # first STRtree hit - the topmost layer wins, so no candidate below it
        # can improve the pick. O(layers × log polys) instead of a full scan.
        for layer_id in sorted(
            indexed,
            key=lambda lid: self._phys_stackup_rank.get(
                id_to_phys.get(lid), 1 << 30),
        ):
            net = self._named_copper_hit_on_layer(layer_id, pt)
            if net is not None:
                return {"net": net,
                        "physical": id_to_phys.get(layer_id),
                        "layer_id": layer_id}
        return None

    def _visible_all_copper_layer_ids(self) -> dict[int, str]:
        """``layer_id → physical_name`` for layers whose 'all copper'
        eye2 button is currently on. The hover fallback uses this to
        restrict bare-copper lookups to copper the user can actually
        see - reporting a net from a hidden layer would be confusing."""
        visible: dict[int, str] = {}
        for name, eye2 in getattr(self, "_layer_eye2_buttons", []) or []:
            if not eye2.isVisibleState():
                continue
            lid = self._phys_name_to_layer_id.get(name)
            if lid is None:
                continue
            visible[lid] = name
        return visible

    def _visible_layer_ids(self) -> set[int]:
        """Set of copper ``layer_id`` the user can currently see - the union
        of the physical-layer eyes (heatmap) and the 'all copper' eye2
        overlays. Used to gate editor-mode markers so a placed source / sink
        only draws while the copper layer it sits on is visible."""
        ids: set[int] = set()
        for name in self._visible_layers():
            lid = self._phys_name_to_layer_id.get(name)
            if lid is not None:
                ids.add(lid)
        ids.update(self._visible_all_copper_layer_ids().keys())
        return ids

    def _all_copper_at_point(self, x: float, y: float) -> dict | None:
        """Hover-fallback: topmost visible all-copper polygon covering
        (x, y), filtered by :meth:`_visible_all_copper_layer_ids`.
        Mirrors :meth:`_copper_at_point` but skips hidden layers, so the
        status bar only names a net the user can see.  Returns
        ``{"net", "physical", "layer_id"}`` or ``None``."""
        md = self.metadata
        if not md:
            return None
        visible = self._visible_all_copper_layer_ids()
        if not visible:
            return None
        indexed = self._layer_strtrees()
        if not indexed:
            return None
        # A selected layer is painted on top (2D) and is the layer the user
        # is focused on, so it wins the pick wherever it has copper under the
        # point - matching what's drawn above the dimmed others. The stackup
        # rank still orders everything else, and a selected layer with no
        # copper here falls back to topmost-stackup exactly as before.
        sel = self._selected_layer
        pt = _sg.Point(x, y)
        # Only visible layers that actually have an index, ordered
        # (selected-first, then topmost stackup) - first STRtree hit wins.
        candidate_ids = [lid for lid in visible if lid in indexed]
        for layer_id in sorted(
            candidate_ids,
            key=lambda lid: (
                0 if (sel is not None and visible.get(lid) == sel) else 1,
                self._phys_stackup_rank.get(visible.get(lid), 1 << 30),
            ),
        ):
            net = self._named_copper_hit_on_layer(layer_id, pt)
            if net is None:
                continue
            # Copper the net focus has taken off screen isn't "visible": skip
            # it so the pick reaches the focused net's copper on a lower
            # layer instead of stopping at a hidden top-layer pour.
            if self._focus_blocks_pick(net, x, y, layer_id):
                continue
            return {"net": net,
                    "physical": visible.get(layer_id),
                    "layer_id": layer_id}
        return None

    def _all_copper_at_point_3d(self, x_px: float, y_px: float
                                ) -> dict | None:
        """3D variant of :meth:`_all_copper_at_point`. Walks visible
        all-copper layers top-first, unprojects the cursor pixel onto
        each layer's z plane, and returns the first layer whose copper
        covers the projected point."""
        md = self.metadata
        if not md:
            return None
        records = md.get("all_copper") or []
        if not records:
            return None
        visible = self._visible_all_copper_layer_ids()
        if not visible:
            return None
        by_layer: dict[int, list] = {}
        for rec in records:
            lid = rec.get("layer_id")
            if lid in visible:
                by_layer.setdefault(lid, []).append(rec)
        # Selected layer wins the pick wherever it carries copper under the
        # cursor (mirrors the 2D variant), else fall back to topmost-stackup.
        sel = self._selected_layer
        ordered = sorted(
            by_layer.items(),
            key=lambda kv: (
                0 if (sel is not None and visible[kv[0]] == sel) else 1,
                self._phys_stackup_rank.get(visible[kv[0]], 1 << 30),
            ),
        )
        for layer_id, recs in ordered:
            phys = visible[layer_id]
            z = self._layer_z_for(phys)
            wx, wy = self._gl_viewer.screen_to_world_at_z(x_px, y_px, z)
            pt = _sg.Point(wx, wy)
            for rec in recs:
                net = rec.get("net")
                if not net:
                    continue
                for poly in rec.get("polygons", []):
                    prepped = self._copper_poly_prepared(poly)
                    if prepped is None:
                        continue
                    try:
                        if not prepped.contains(pt):
                            continue
                    except Exception:
                        continue
                    return {"net": net, "physical": phys,
                            "layer_id": layer_id}
        return None
