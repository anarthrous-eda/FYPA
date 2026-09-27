"""Click-to-select copper primitives and Tab-expansion along the net."""
from __future__ import annotations

import math
import shapely.geometry as _sg
import shapely.prepared as _sp
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QTextEdit,
)

from fypa.viewer.overlays import _EDITOR_MARKER_HIT_PX
from fypa.viewer.widgets import _esc


class _CopperPickMixin:
    """Click-to-select copper primitives and Tab-expansion along the net."""

    # --- Viewer-mode copper-primitive picker ------------------------------
    #
    # Click-to-select on any visible copper primitive in viewer mode. The
    # selected primitive's outline is drawn as a dashed yellow polygon
    # (gl_mesh_viewer.set_primitive_selection_outline) and the right-hand
    # editor panel widget is repurposed to show its properties (kind,
    # net, layer, geometry). Bare-substrate clicks (or Escape) clear.

    def _primitives_index(self) -> dict[tuple[int, str], list[dict]]:
        """Lazy ``(layer_id, net) -> [primitive dicts]`` index over
        ``metadata['primitives']``. Built once per project; the per-shape
        caches survive on the dicts so repeat hits stay cheap."""
        if self._primitives_by_layer_net is not None:
            return self._primitives_by_layer_net
        idx: dict[tuple[int, str], list[dict]] = {}
        md = self.metadata or {}
        prims = md.get("primitives") or {}
        for bucket in ("tracks", "arcs", "regions",
                       "shape_based_regions", "fills", "planes"):
            for rec in prims.get(bucket, []):
                lids = rec.get("layer_ids")
                if lids:
                    for lid in lids:
                        key = (int(lid), str(rec.get("net", "")))
                        idx.setdefault(key, []).append(rec)
                else:
                    key = (int(rec.get("layer_id", -1)),
                           str(rec.get("net", "")))
                    idx.setdefault(key, []).append(rec)
        self._primitives_by_layer_net = idx
        return idx

    def _primitive_prepared_shape(self, prim: dict):
        """Build (and cache on the primitive dict) a prepared shapely
        geometry for ``prim``. Returns ``None`` if the geometry can't be
        constructed (degenerate primitive)."""
        cached = prim.get("_prepared_shape")
        if cached is not None:
            return cached
        kind = prim.get("kind")
        shp = None
        try:
            if kind == "track":
                line = _sg.LineString([(prim["ax"], prim["ay"]),
                                       (prim["bx"], prim["by"])])
                # Round caps + joins to match the actual copper polygon
                # (altium_geometry._track_polygon uses cap_style=1).
                shp = line.buffer(max(prim["width_mm"] * 0.5, 1e-6),
                                  cap_style=1, join_style=1,
                                  resolution=8)
            elif kind == "arc":
                # Match altium_geometry._arc_polyline_points exactly so
                # the buffered outline matches the copper polygon.
                from fypa.altium_geometry import ARC_CHORD_TOLERANCE_MM
                sweep_deg = ((prim["end_angle_deg"]
                              - prim["start_angle_deg"]) % 360.0)
                if sweep_deg == 0.0:
                    sweep_deg = 360.0
                start = math.radians(prim["start_angle_deg"])
                sweep = math.radians(sweep_deg)
                cx, cy, r = prim["cx"], prim["cy"], prim["radius_mm"]
                half_w = max(prim["width_mm"] * 0.5, 1e-6)
                if r <= 0.0:
                    shp = _sg.Point(cx, cy).buffer(half_w, resolution=8)
                else:
                    cos_arg = max(-1.0, 1.0 - ARC_CHORD_TOLERANCE_MM / r)
                    max_step_rad = 2.0 * math.acos(cos_arg)
                    if max_step_rad <= 0.0:
                        n = max(8, int(round(sweep_deg)))
                    else:
                        n = max(8, int(math.ceil(sweep / max_step_rad)))
                    pts = [(cx + r * math.cos(start + sweep * k / n),
                            cy + r * math.sin(start + sweep * k / n))
                           for k in range(n + 1)]
                    line = _sg.LineString(pts)
                    # Round caps + joins to match the actual copper
                    # polygon (altium_geometry._arc_polygon uses
                    # cap_style=1). Round caps at the (coincident)
                    # endpoints of a full-circle arc overlap cleanly
                    # into the tube; flat / square caps would stick a
                    # radial sliver inside the ring.
                    shp = line.buffer(half_w, cap_style=1, join_style=1,
                                      resolution=8)
            elif kind == "fill":
                import shapely.affinity as _sa
                box = _sg.box(prim["x1_mm"], prim["y1_mm"],
                              prim["x2_mm"], prim["y2_mm"])
                rot = float(prim.get("rotation_deg", 0.0) or 0.0)
                shp = (_sa.rotate(box, rot, origin="center")
                       if rot else box)
            elif kind in ("region", "shape_based_region", "plane"):
                outline = prim.get("outline") or []
                if len(outline) >= 3:
                    holes = [h for h in (prim.get("holes") or [])
                             if len(h) >= 3]
                    shp = _sg.Polygon(outline, holes)
        except Exception:
            shp = None
        if shp is None or shp.is_empty:
            return None
        prepped = _sp.prep(shp)
        prim["_prepared_shape"] = prepped
        prim["_shape"] = shp
        return prepped

    def _pad_overlay_visible(self, rec: dict) -> bool:
        """True if pad ``rec`` is currently shown by the Pads overlay on at
        least one board side. Mirrors the visibility test in the pad loop of
        :meth:`_refresh_overlay_geometry` so editor-mode selection can't pick
        a pad the user can't actually see."""
        md = self.metadata or {}
        enabled = md.get("enabled_copper_layer_ids") or []
        if not enabled:
            return False
        lids = rec.get("layer_ids") or []
        sides = self._overlay_side_states("pads")
        alphas = self._overlay_side_alpha("pads")
        rail_members = set(self._effective_rail_members(self._visible_rails()))
        for side, lid in (("top", enabled[0]), ("bottom", enabled[-1])):
            if lid is None or lid not in lids:
                continue
            vis, _solid = sides[side]
            if vis is None:
                continue
            if vis == "rails" and not (
                    rail_members and {rec.get("net")} & rail_members):
                continue
            if alphas.get(side, 1.0) <= 0.0:
                continue
            return True
        return False

    def _via_or_pad_at_point(self, world_x: float, world_y: float,
                              require_pad_visible: bool = False
                              ) -> dict | None:
        """Fast path for via / through-hole-pad / SMT-pad clicks. These
        sit on top of copper polygons, so they need to be tested before
        the per-polygon all-copper hit-test would shadow them.

        Returns a primitive-shaped dict ``{"kind", "record", "layer_id",
        "net"}`` for the topmost visible hit, or ``None``.

        When ``require_pad_visible`` is set, SMT pads hidden by the Pads
        overlay are skipped (editor mode — you can only select what you
        can see)."""
        md = self.metadata or {}
        visible = self._visible_all_copper_layer_ids()
        if not visible:
            return None
        best: dict | None = None
        best_rank = 1 << 30

        def _take(kind: str, rec: dict, layer_ids: list[int]) -> None:
            nonlocal best, best_rank
            for lid in layer_ids:
                phys = visible.get(int(lid))
                if phys is None:
                    continue
                rank = self._phys_stackup_rank.get(phys, 1 << 30)
                if rank < best_rank:
                    best_rank = rank
                    best = {"kind": kind, "record": rec,
                            "layer_id": int(lid),
                            "net": rec.get("net", "") or ""}
                break

        for bucket_kind, bucket in (("via", md.get("vias") or []),
                                     ("pth", md.get("pths") or [])):
            for rec in bucket:
                d = float(rec.get("diameter_mm", 0.0) or 0.0)
                if d <= 0.0:
                    continue
                dx = world_x - float(rec.get("x_mm", 0.0))
                dy = world_y - float(rec.get("y_mm", 0.0))
                if (dx * dx + dy * dy) > (d * 0.5) ** 2:
                    continue
                ls = int(rec.get("layer_start", 0))
                le = int(rec.get("layer_end", 0))
                if ls and le:
                    span = list(range(min(ls, le), max(ls, le) + 1))
                else:
                    span = []
                _take(bucket_kind, rec, span)

        pads = md.get("pads") or []
        if pads:
            pt = _sg.Point(world_x, world_y)
            for rec in pads:
                outline = rec.get("outline") or []
                if len(outline) < 3:
                    continue
                if require_pad_visible and not self._pad_overlay_visible(rec):
                    continue
                try:
                    poly = _sg.Polygon(outline)
                    if poly.is_empty or not poly.contains(pt):
                        continue
                except Exception:
                    continue
                _take("pad", rec, list(rec.get("layer_ids") or []))
        return best

    def _primitive_at_point(self, world_x: float, world_y: float,
                             require_pad_visible: bool = False
                             ) -> dict | None:
        """Topmost visible copper primitive containing the world-mm point.
        Returns a dict ``{"kind", "record", "layer_id", "net"}`` or
        ``None`` on bare substrate / hidden copper.

        Probe order: vias / pads first (they sit on top of pours), then
        ``_all_copper_at_point`` narrows the topmost-visible (layer, net),
        then walks the per-(layer, net) primitive list for the
        track / arc / fill / region whose shape contains the point.

        ``require_pad_visible`` is forwarded to :meth:`_via_or_pad_at_point`
        so editor-mode picks ignore pads hidden by the Pads overlay."""
        hit = self._via_or_pad_at_point(
            world_x, world_y, require_pad_visible=require_pad_visible)
        if hit is not None:
            return hit
        cover = self._all_copper_at_point(world_x, world_y)
        if cover is None:
            return None
        key = (int(cover["layer_id"]), str(cover["net"]))
        prims = self._primitives_index().get(key) or []
        if not prims:
            return None
        pt = _sg.Point(world_x, world_y)
        for prim in prims:
            prepped = self._primitive_prepared_shape(prim)
            if prepped is None:
                continue
            try:
                if not prepped.contains(pt):
                    continue
            except Exception:
                continue
            return {"kind": prim["kind"], "record": prim,
                    "layer_id": int(cover["layer_id"]),
                    "net": str(prim["net"])}
        return None

    def _primitive_outline_rings(self, hit: dict
                                  ) -> list[list[tuple[float, float]]]:
        """Closed world-mm rings outlining the selected primitive — the
        geometry the GL viewer draws as a dashed yellow polygon. Always
        returns at least one ring (caller has a hit)."""
        kind = hit.get("kind")
        rec = hit.get("record") or {}
        if kind in ("via", "pth"):
            cx = float(rec.get("x_mm", 0.0))
            cy = float(rec.get("y_mm", 0.0))
            r = float(rec.get("diameter_mm", 0.0)) * 0.5
            steps = 64
            return [[
                (cx + r * math.cos(2.0 * math.pi * k / steps),
                 cy + r * math.sin(2.0 * math.pi * k / steps))
                for k in range(steps)
            ]]
        if kind == "pad":
            outline = rec.get("outline") or []
            return [[(float(x), float(y)) for x, y in outline]]
        shp = rec.get("_shape")
        if shp is None:
            self._primitive_prepared_shape(rec)
            shp = rec.get("_shape")
        rings: list[list[tuple[float, float]]] = []
        if shp is not None:
            polys = (list(shp.geoms)
                     if shp.geom_type == "MultiPolygon" else [shp])
            for poly in polys:
                ext = getattr(poly, "exterior", None)
                if ext is None or ext.is_empty:
                    continue
                rings.append([(float(x), float(y))
                              for x, y in ext.coords])
                for hole in getattr(poly, "interiors", []):
                    if not hole.is_empty:
                        rings.append([(float(x), float(y))
                                      for x, y in hole.coords])
        if not rings:
            outline = rec.get("outline")
            if outline:
                rings = [[(float(x), float(y)) for x, y in outline]]
        return rings

    @staticmethod
    def _fmt_mm(v) -> str:
        try:
            return f"{float(v):.3f} mm"
        except (TypeError, ValueError):
            return "—"

    @staticmethod
    def _fmt_xy(x, y) -> str:
        try:
            return f"({float(x):.3f}, {float(y):.3f}) mm"
        except (TypeError, ValueError):
            return "—"

    @staticmethod
    def _fmt_deg(v) -> str:
        try:
            return f"{float(v):.2f}°"
        except (TypeError, ValueError):
            return "—"

    _PAD_SHAPE_NAMES = {
        1: "Round",
        2: "Rectangular",
        3: "Octagonal",
        9: "Rounded Rectangle",
    }

    def _layer_name_for_id(self, layer_id) -> str:
        """Physical-layer name for ``layer_id`` (the inverse of
        ``_phys_name_to_layer_id``). Returns the raw id as a string when
        the layer isn't in the stackup map."""
        if layer_id is None:
            return "—"
        try:
            lid = int(layer_id)
        except (TypeError, ValueError):
            return str(layer_id)
        for name, mapped in self._phys_name_to_layer_id.items():
            if mapped == lid:
                return name
        return f"layer {lid}"

    def _populate_copper_props_form(self, hit: dict) -> None:
        """(Re)build the right-panel form describing the selected copper
        primitive. Rows are kind-specific; the common header lists shape,
        net, and layer (or layer span for via / PTH / multi-layer pad)."""
        if not hasattr(self, "_copper_props_layout"):
            return
        lay = self._copper_props_layout
        self._clear_layout(lay)

        kind = hit.get("kind") or "?"
        rec = hit.get("record") or {}
        net = str(hit.get("net") or rec.get("net") or "")

        kind_label = {
            "track": "Track",
            "arc": "Arc",
            "fill": "Fill",
            "region": "Region",
            "shape_based_region": "Region (shape-based)",
            "plane": "Plane",
            "via": "Via",
            "pth": "Through-hole pad",
            "pad": "Pad",
        }.get(kind, kind.title())

        lay.addWidget(QLabel(f"<b>{_esc(kind_label)}</b>"))

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(4)

        def add(label: str, value: str) -> None:
            v = QLabel(value)
            v.setTextInteractionFlags(Qt.TextSelectableByMouse)
            v.setWordWrap(True)
            form.addRow(QLabel(label), v)

        add("Net", net or "(no net)")

        if kind in ("via", "pth"):
            ls = self._layer_name_for_id(rec.get("layer_start"))
            le = self._layer_name_for_id(rec.get("layer_end"))
            add("Layer span", f"{ls} → {le}" if ls != le else ls)
        elif kind == "pad":
            ids = list(rec.get("layer_ids") or [])
            if len(ids) <= 1:
                add("Layer", self._layer_name_for_id(
                    ids[0] if ids else hit.get("layer_id")))
            else:
                names = [self._layer_name_for_id(i) for i in ids]
                add("Layers", ", ".join(names))
        else:
            ids = list(rec.get("layer_ids") or [])
            if len(ids) > 1:
                names = [self._layer_name_for_id(i) for i in ids]
                add("Layers", ", ".join(names))
            else:
                add("Layer", self._layer_name_for_id(
                    ids[0] if ids else hit.get("layer_id")))

        if kind == "track":
            ax, ay = float(rec["ax"]), float(rec["ay"])
            bx, by = float(rec["bx"]), float(rec["by"])
            length = math.hypot(bx - ax, by - ay)
            add("Start", self._fmt_xy(ax, ay))
            add("End", self._fmt_xy(bx, by))
            add("Width", self._fmt_mm(rec.get("width_mm")))
            add("Length", self._fmt_mm(length))
            if rec.get("is_polygon_outline"):
                add("Polygon outline", "yes")
            if rec.get("is_keepout"):
                add("Keepout", "yes")
        elif kind == "arc":
            start_deg = float(rec.get("start_angle_deg", 0.0))
            end_deg = float(rec.get("end_angle_deg", 0.0))
            sweep = (end_deg - start_deg) % 360.0
            if sweep == 0.0 and start_deg != end_deg:
                sweep = 360.0
            r = float(rec.get("radius_mm", 0.0))
            arc_len = r * math.radians(sweep)
            add("Center", self._fmt_xy(rec.get("cx"), rec.get("cy")))
            add("Radius", self._fmt_mm(r))
            add("Start angle", self._fmt_deg(start_deg))
            add("End angle", self._fmt_deg(end_deg))
            add("Sweep angle", self._fmt_deg(sweep))
            add("Width", self._fmt_mm(rec.get("width_mm")))
            add("Arc length", self._fmt_mm(arc_len))
            if rec.get("is_keepout"):
                add("Keepout", "yes")
        elif kind == "fill":
            x1 = float(rec.get("x1_mm", 0.0))
            y1 = float(rec.get("y1_mm", 0.0))
            x2 = float(rec.get("x2_mm", 0.0))
            y2 = float(rec.get("y2_mm", 0.0))
            add("Corner 1", self._fmt_xy(x1, y1))
            add("Corner 2", self._fmt_xy(x2, y2))
            add("Width", self._fmt_mm(abs(x2 - x1)))
            add("Height", self._fmt_mm(abs(y2 - y1)))
            add("Rotation", self._fmt_deg(rec.get("rotation_deg")))
            add("Area", f"{abs((x2 - x1) * (y2 - y1)):.3f} mm²")
            if rec.get("is_keepout"):
                add("Keepout", "yes")
        elif kind in ("region", "shape_based_region"):
            outline = rec.get("outline") or []
            shp = rec.get("_shape")
            if shp is None:
                self._primitive_prepared_shape(rec)
                shp = rec.get("_shape")
            add("Vertices", f"{len(outline)}")
            if kind == "shape_based_region":
                add("Arc edges", f"{int(rec.get('arc_edge_count', 0))}")
            if shp is not None:
                minx, miny, maxx, maxy = shp.bounds
                add("Bounding box",
                    f"x: {minx:.3f} → {maxx:.3f} mm\n"
                    f"y: {miny:.3f} → {maxy:.3f} mm")
                add("Area", f"{shp.area:.4f} mm²")
            if rec.get("is_polygon_outline"):
                add("Polygon outline", "yes")
            if rec.get("is_keepout"):
                add("Keepout", "yes")
            if rec.get("is_board_cutout"):
                add("Board cutout", "yes")
        elif kind == "plane":
            shp = rec.get("_shape")
            if shp is None:
                self._primitive_prepared_shape(rec)
                shp = rec.get("_shape")
            add("Type", "Internal plane (negative)")
            if shp is not None:
                minx, miny, maxx, maxy = shp.bounds
                add("Bounding box",
                    f"x: {minx:.3f} → {maxx:.3f} mm\n"
                    f"y: {miny:.3f} → {maxy:.3f} mm")
                add("Area", f"{shp.area:.4f} mm²")
                n_clear = len(getattr(shp, "interiors", [])) if (
                    shp.geom_type == "Polygon") else sum(
                    len(g.interiors) for g in getattr(shp, "geoms", []))
                add("Clearances", f"{n_clear}")
        elif kind in ("via", "pth"):
            add("Center",
                self._fmt_xy(rec.get("x_mm"), rec.get("y_mm")))
            add("Outer diameter", self._fmt_mm(rec.get("diameter_mm")))
            add("Hole diameter",
                self._fmt_mm(rec.get("hole_diameter_mm")))
            if kind == "pth":
                des = rec.get("designator")
                if des:
                    add("Designator", str(des))
            else:
                lbl = rec.get("ipc4761_label")
                if lbl:
                    add("IPC-4761", str(lbl))
                fm = rec.get("fill_material")
                if fm:
                    add("Fill material", str(fm))
        elif kind == "pad":
            des = rec.get("designator")
            if des:
                add("Designator", str(des))
            add("Center",
                self._fmt_xy(rec.get("x_mm"), rec.get("y_mm")))
            add("Width", self._fmt_mm(rec.get("width_mm")))
            add("Height", self._fmt_mm(rec.get("height_mm")))
            shape_code = int(rec.get("shape_code", 0) or 0)
            add("Shape",
                self._PAD_SHAPE_NAMES.get(shape_code,
                                          f"code {shape_code}"))
            add("Rotation", self._fmt_deg(rec.get("rotation_deg")))
            if shape_code == 9:
                add("Corner radius",
                    f"{int(rec.get('corner_radius_pct', 0))}%")
            if rec.get("is_through_hole"):
                add("Hole diameter", self._fmt_mm(rec.get("hole_mm")))
            if rec.get("is_smt"):
                add("Mount", "SMT")
            elif rec.get("is_through_hole"):
                add("Mount", "Through-hole")

        lay.addLayout(form)

    def _select_copper_primitive(self, hit: dict) -> None:
        """Store the click-selected primitive, push its dashed-yellow
        outline to the GL viewer, populate the right-panel form, and
        show the panel."""
        self._copper_selection = hit
        rings = self._primitive_outline_rings(hit)
        self._gl_viewer.set_primitive_selection_outline(rings)
        self._populate_copper_props_form(hit)
        self._show_copper_props_layout()
        # Show before positioning so :meth:`_position_editor_panel`'s
        # ``panel.isVisible()`` check correctly pushes the legend inset.
        self._editor_panel.show()
        self._position_editor_panel()

    def _clear_copper_selection(self) -> None:
        """Drop any viewer-mode copper selection — clears the dashed-yellow
        GL overlay, empties the right-panel form, and hides the panel
        (unless editor mode is active, in which case the panel stays
        visible with the PDN editor contents)."""
        if self._copper_selection is None:
            return
        self._copper_selection = None
        gl = getattr(self, "_gl_viewer", None)
        if gl is not None:
            gl.set_primitive_selection_outline(None)
        lay = getattr(self, "_copper_props_layout", None)
        if lay is not None:
            self._clear_layout(lay)
        if not self._editor_mode:
            self._show_pdn_editor_layout()
            panel = getattr(self, "_editor_panel", None)
            if panel is not None:
                panel.hide()
                # Clear the legend's right-side inset now that the panel
                # is gone (see :meth:`_position_editor_panel`).
                self._position_editor_panel()

    # --- Tab: expand the dashed-yellow selection along its net -------------
    #
    # With a copper primitive selected (the dashed-yellow outline, in viewer
    # or editor mode), Tab steps the outline out through three levels and
    # back round: the clicked primitive -> every same-net primitive on its
    # layer -> every same-net primitive on all visible layers -> the clicked
    # primitive again. Shift+Tab steps the other way. "Connected" is by net
    # name, not copper geometry, so disjoint same-net islands are included.
    # Display only: nothing downstream (Apply, Delete) reads the expansion.

    # ``{"seed": <selection object>, "level": 0..2}``. The seed is the
    # selection dict itself (``_copper_selection`` in viewer mode, the
    # ``_editor_selection`` copper dict in editor mode), compared by
    # identity: any new click replaces that object, so the level falls back
    # to 0 without every clear / select path having to reset it.
    _tab_expand: dict | None = None

    _TAB_LEVELS = 3

    def _tab_expand_seed(self) -> tuple[object, str, int, dict | None] | None:
        """``(seed_obj, net, layer_id, hit)`` for the current copper
        selection, or ``None`` when nothing Tab can expand is selected.
        ``hit`` is the primitive-picker record whose outline level 0
        restores — editor mode can hold a copper selection with no
        primitive (the rail-mesh fallback picker), which has no outline."""
        if self._editor_mode:
            sel = self._editor_selection
            if not sel or sel.get("kind") != "copper" or self._editor_multi:
                return None
            if sel.get("layer_id") is None:
                return None
            return (sel, str(sel.get("net") or ""), int(sel["layer_id"]),
                    sel.get("hit"))
        hit = self._copper_selection
        if hit is None or hit.get("layer_id") is None:
            return None
        return (hit, str(hit.get("net") or ""), int(hit["layer_id"]), hit)

    def _same_net_copper_hits(self, net: str, layer_ids: set[int]
                              ) -> list[dict]:
        """Every copper primitive on ``net`` touching any of ``layer_ids``,
        as primitive-picker-shaped dicts (``{"kind", "record", "layer_id",
        "net"}``) so :meth:`_primitive_outline_rings` can outline them.

        Covers what the picker can select: tracks / arcs / fills / regions /
        planes, vias and through-hole pads whose span crosses a listed
        layer, and pads. Each record appears once even when it sits on
        several listed layers (a via, a multi-layer pad). Keepouts and
        board cutouts carry no current, so they are left out. In editor
        mode pads hidden by the Pads overlay are skipped, matching the
        click picker's ``require_pad_visible``."""
        md = self.metadata or {}
        out: list[dict] = []
        seen: set[int] = set()

        def take(kind: str, rec: dict, lid: int) -> None:
            if id(rec) in seen:
                return
            seen.add(id(rec))
            out.append({"kind": kind, "record": rec,
                        "layer_id": int(lid), "net": net})

        index = self._primitives_index()
        for lid in sorted(layer_ids):
            for rec in index.get((int(lid), net)) or []:
                if rec.get("is_keepout") or rec.get("is_board_cutout"):
                    continue
                take(rec.get("kind") or "region", rec, lid)
        for kind, bucket in (("via", md.get("vias") or []),
                             ("pth", md.get("pths") or [])):
            for rec in bucket:
                if (rec.get("net") or "") != net:
                    continue
                if float(rec.get("diameter_mm", 0.0) or 0.0) <= 0.0:
                    continue
                ls = int(rec.get("layer_start", 0) or 0)
                le = int(rec.get("layer_end", 0) or 0)
                if not (ls and le):
                    continue
                lo, hi = min(ls, le), max(ls, le)
                hits = [lid for lid in layer_ids if lo <= lid <= hi]
                if hits:
                    take(kind, rec, min(hits))
        for rec in md.get("pads") or []:
            if (rec.get("net") or "") != net:
                continue
            if len(rec.get("outline") or []) < 3:
                continue
            hits = [int(lid) for lid in (rec.get("layer_ids") or [])
                    if int(lid) in layer_ids]
            if not hits:
                continue
            if self._editor_mode and not self._pad_overlay_visible(rec):
                continue
            take("pad", rec, min(hits))
        return out

    def _cycle_tab_expand(self, step: int = 1) -> bool:
        """Advance the Tab expansion one level (``step=-1`` for Shift+Tab)
        and push the new outline. Returns ``False`` when there is no copper
        selection to expand, so the key press can fall through to Qt's
        normal focus handling."""
        seed = self._tab_expand_seed()
        if seed is None:
            return False
        seed_obj, net, layer_id, hit = seed
        state = self._tab_expand
        level = state["level"] if (state and state["seed"] is seed_obj) else 0
        if not net or net == "(none)":
            # Unnamed copper shares one sentinel "net", so expanding by name
            # would outline every unrelated scrap of no-net copper.
            self.statusBar().showMessage(
                "Tab expands along a net — this copper has no net name.",
                4000)
            return True
        level = (level + step) % self._TAB_LEVELS
        self._tab_expand = {"seed": seed_obj, "level": level}
        gl = self._gl_viewer
        if level == 0:
            gl.set_primitive_selection_outline(
                self._primitive_outline_rings(hit) if hit else None)
            if not self._editor_mode and hit is not None:
                self._populate_copper_props_form(hit)
            self.statusBar().clearMessage()
            return True
        if level == 1:
            layers = {layer_id}
        else:
            layers = self._tab_visible_layer_ids() | {layer_id}
        hits = self._same_net_copper_hits(net, layers)
        rings: list[list[tuple[float, float]]] = []
        for h in hits:
            rings.extend(self._primitive_outline_rings(h))
        gl.set_primitive_selection_outline(rings or None)
        where = (self._layer_name_for_id(layer_id) if level == 1
                 else f"{len(layers)} visible layer"
                      f"{'' if len(layers) == 1 else 's'}")
        self.statusBar().showMessage(
            f"{net}: {len(hits)} primitive{'' if len(hits) == 1 else 's'} "
            f"on {where} — Tab to "
            f"{'expand to all visible layers' if level == 1 else 'go back'}"
            ".")
        if not self._editor_mode:
            self._populate_copper_group_form(net, layers, hits)
        return True

    def _tab_visible_layer_ids(self) -> set[int]:
        """Copper layers that actually show something, for the last Tab
        level. The all-copper eyes always count. A physical-layer (rail
        heatmap) eye counts only while a rail is visible: with no rails
        (unsolved, or every rail eye off) that eye draws nothing, so an
        open one must not pull its layer into the selection."""
        ids = set(self._visible_all_copper_layer_ids().keys())
        if self._visible_rails():
            for name in self._visible_layers():
                lid = self._phys_name_to_layer_id.get(name)
                if lid is not None:
                    ids.add(lid)
        return ids

    def _populate_copper_group_form(self, net: str, layer_ids: set[int],
                                    hits: list[dict]) -> None:
        """Right-panel summary for a Tab-expanded viewer-mode selection:
        net, layers, and a per-kind primitive count."""
        if not hasattr(self, "_copper_props_layout"):
            return
        lay = self._copper_props_layout
        self._clear_layout(lay)
        lay.addWidget(QLabel(f"<b>{len(hits)} copper primitives</b>"))
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(4)

        def add(label: str, value: str) -> None:
            v = QLabel(value)
            v.setTextInteractionFlags(Qt.TextSelectableByMouse)
            v.setWordWrap(True)
            form.addRow(QLabel(label), v)

        add("Net", net)
        names = [self._layer_name_for_id(lid)
                 for lid in sorted(layer_ids,
                                   key=lambda i: self._phys_stackup_rank.get(
                                       self._layer_name_for_id(i), 1 << 30))]
        add("Layers" if len(names) > 1 else "Layer", ", ".join(names))
        labels = (("track", "Tracks"), ("arc", "Arcs"), ("fill", "Fills"),
                  ("region", "Regions"),
                  ("shape_based_region", "Regions (shape-based)"),
                  ("plane", "Planes"), ("via", "Vias"),
                  ("pth", "Through-hole pads"), ("pad", "Pads"))
        counts: dict[str, int] = {}
        for h in hits:
            counts[h["kind"]] = counts.get(h["kind"], 0) + 1
        for kind, label in labels:
            if counts.get(kind):
                add(label, str(counts[kind]))
        lay.addLayout(form)

    def _tab_targets_viewport(self) -> bool:
        """Whether a Tab press belongs to the viewport rather than Qt's
        focus chain: the GL view has keyboard focus, or the cursor is over
        it and no text entry has focus (Tab in a text box must still move
        to the next field)."""
        gl = getattr(self, "_gl_viewer", None)
        if gl is None:
            return False
        fw = QApplication.focusWidget()
        if fw is gl:
            return True
        if not gl.underMouse():
            return False
        if isinstance(fw, QAbstractSpinBox):
            return False
        if (isinstance(fw, (QLineEdit, QTextEdit, QPlainTextEdit))
                and not fw.isReadOnly()):
            return False
        if isinstance(fw, QComboBox) and fw.isEditable():
            return False
        return True

    def _on_editor_click(self, world_x: float, world_y: float) -> None:
        """Editor-mode left-click: drop a pending free marker if one is
        armed, else select the component / placed marker / copper under
        the cursor."""
        if self._editor_pending_marker is not None:
            self._place_free_marker(world_x, world_y)
            return
        # Shift / Ctrl click extends or trims a marquee selection (Altium's
        # modifier convention). The GL widget's ``clicked`` signal carries no
        # modifier state, so read it live - it is still current at delivery.
        mods = QApplication.keyboardModifiers()
        extend = bool(mods & Qt.ShiftModifier)
        toggle = bool(mods & Qt.ControlModifier)
        if extend or toggle:
            cand = self._marquee_candidate_at(world_x, world_y)
            if cand is None:
                # Modifier-click on empty space: leave the selection be
                # rather than silently dropping a hard-won multi-selection.
                return
            base = self._editor_multi or self._selection_as_multi_entries()
            merged = {(e["kind"], e["id"]): e for e in base}
            key = (cand.key[0], cand.key[1])
            if toggle and key in merged:
                merged.pop(key)
            else:
                merged[key] = self._multi_entry_for(cand)
            self._set_editor_multi(list(merged.values()))
            return
        # A plain click always starts a fresh selection.
        self._editor_multi = []
        # Component first: a marker sitting on a component reads as that
        # component's source / sink, so clicking it loads the component
        # properties rather than the free-marker form.
        comp = self._component_at(world_x, world_y)
        if comp is not None:
            self._select_component(comp)
            return
        # A SOURCE / SINK marker drawn for a schematic (or existing
        # component editor) directive is a click target in its own right:
        # the glyph is a bigger, more obvious mark than the bare footprint,
        # so clicking it selects the owning component — opening its
        # read-only / Unlock form (see _populate_editor_form). The marker's
        # pin coordinate lands inside the component box, so we resolve the
        # owner from there; visible_sides_only is off because the marker is
        # only ever drawn on a visible layer, so its owner is reachable even
        # if the footprint box would be gated out.
        row = self._pick_hovered_marker(world_x, world_y)
        if row is not None:
            owner = self._component_at(
                row["x_mm"], row["y_mm"], visible_sides_only=False)
            if owner is not None:
                self._select_component(owner)
                return
        # A component may sit here but on a hidden side — it can't be
        # selected. Say why, then fall through to the marker / copper
        # underneath (whatever the user can actually see).
        hidden = self._component_at(world_x, world_y, visible_sides_only=False)
        if hidden is not None:
            side = hidden.get("side") or "hidden"
            self.statusBar().showMessage(
                f"{hidden.get('designator') or 'Component'} is on the "
                f"hidden {side} side — turn on the {side}-side Components "
                "overlay to select it.", 4000)
        marker = self._free_marker_at(world_x, world_y)
        if marker is not None:
            self._select_free_marker(marker)
            return
        hit = self._primitive_at_point(world_x, world_y,
                                       require_pad_visible=True)
        if hit is not None and hit.get("net") \
                and not self._focus_blocks_pick(hit.get("net")):
            self._select_copper(hit["net"], hit,
                                anchor_xy=(world_x, world_y))
            return
        # No visible primitive — fall back to the more permissive
        # visible-only picker (rail mesh / stub / visible all-copper).
        pick = self._visible_editor_copper_pick(world_x, world_y)
        if pick is not None and pick.get("net") \
                and not self._focus_blocks_pick(
                    pick.get("net"), world_x, world_y,
                    pick.get("layer_id")):
            self._select_copper(pick["net"],
                                anchor_xy=(world_x, world_y),
                                layer_id=pick.get("layer_id"))
            return
        # Copper exists here but only on a hidden layer — mention it so
        # the user knows why nothing got selected, then fall through to
        # clear (a click the user can't see reads as "click off copper",
        # matching viewer-mode behaviour).
        hidden = self._editor_copper_pick(world_x, world_y)
        if hidden is not None and hidden.get("net"):
            phys = hidden.get("physical") or "a hidden layer"
            self.statusBar().showMessage(
                f"Copper on {phys} is hidden — turn on its eye to "
                "select it.", 4000)
        # Bare substrate (or hidden-only copper) — clear the selection.
        self._editor_selection = None
        self._clear_editor_highlight()
        self._update_editor_panel()

    def _free_marker_at(self, world_x: float,
                        world_y: float):
        """The placed free-marker :class:`EditorDirective` whose anchor is
        under the click, or ``None``. The hit radius scales with the view
        zoom so it tracks the on-screen marker size; the nearest marker
        wins when several overlap."""
        if self._project is None:
            return None
        try:
            _cx, _cy, mm_per_px = self._gl_viewer.view_center_scale()
        except Exception:
            mm_per_px = 0.1
        radius = max(float(mm_per_px) * _EDITOR_MARKER_HIT_PX, 1e-6)
        best = None
        best_d2 = radius * radius
        for d in self._project.editor_directives:
            if d.kind != "free" or d.anchor_xy is None:
                continue
            # A marker the net focus has taken off screen must not be
            # clickable or draggable either.
            if not self._directive_focus_visible(d):
                continue
            dx = world_x - float(d.anchor_xy[0])
            dy = world_y - float(d.anchor_xy[1])
            d2 = dx * dx + dy * dy
            if d2 <= best_d2:
                best, best_d2 = d, d2
        return best

    def _select_free_marker(self, directive) -> None:
        """Select a placed free source / sink marker — the right-hand
        panel opens its PDN form so the role / value / nets can be
        edited (see :meth:`_populate_editor_form`)."""
        self._editor_selection = {"kind": "free", "id": directive.id}
        self._gl_viewer.set_primitive_selection_outline(None)
        # An unnamed-copper marker carries ``p_net == "(none)"`` until the
        # user runs Apply on the copper-name form. Use the polygon-identity
        # highlight (computed from the anchor) instead of net-name dim so
        # disjoint unnamed pieces don't all light up because they share
        # the same sentinel name.
        is_real_net = bool(directive.p_net) and directive.p_net != "(none)"
        highlight = (self._connected_nets(directive.p_net)
                     if is_real_net else set())
        highlight_polys: set[tuple[int, int]] = set()
        if (directive.anchor_xy is not None
                and directive.layer_id is not None):
            seed_poly = self._copper_poly_under_point(
                float(directive.anchor_xy[0]),
                float(directive.anchor_xy[1]),
                int(directive.layer_id),
            )
            if seed_poly is not None:
                highlight_polys = self._connected_copper_polys(
                    int(directive.layer_id), seed_poly)
        self._apply_editor_highlight(highlight, polys=highlight_polys)
        self._populate_editor_form()

    def _select_component(self, comp: dict) -> None:
        """Select a PCB component for PDN editing and highlight the copper
        connected to its pins."""
        nets = list(comp.get("nets", []))
        self._editor_selection = {
            "kind": "component",
            "designator": comp.get("designator"),
            "nets": nets,
            "bbox": comp.get("bbox"),
            # True once the user clicks "Unlock" on a component whose PDN
            # values come from the Altium schematic — see _on_editor_unlock.
            "unlocked": False,
        }
        self._gl_viewer.set_primitive_selection_outline(None)
        highlight: set[str] = set()
        for n in nets:
            highlight |= self._connected_nets(n)
        self._apply_editor_highlight(highlight)
        self._populate_editor_form()

    def _select_copper(self, net: str, hit: dict | None = None,
                       anchor_xy: tuple[float, float] | None = None,
                       layer_id: int | None = None) -> None:
        """Select a copper net for PDN editing and highlight every net
        connected to it. When ``hit`` is the primitive picker's result
        for the click, its outline is pushed to the GL viewer as the
        same dashed-yellow polygon used by viewer-mode copper selection.
        ``anchor_xy`` records the click point so the right-hand panel's
        copper-name form (shown only when ``net == "(none)"``) can pin
        the rename to THIS polygon — its layer comes from ``hit`` when
        present, else from the explicit ``layer_id`` kwarg (callers that
        resolved the click through the visibility-aware picker get a
        layer but no primitive record).

        Highlight = the connected ``all_copper`` polygons reached from
        the click via the cached union-find. For named nets the existing
        whole-net dim mask is also applied; for ``"(none)"`` we leave
        the net set empty so disjoint unnamed pieces dim instead of all
        lighting up because they share the same sentinel name."""
        sel: dict = {"kind": "copper", "net": net}
        if anchor_xy is not None:
            sel["anchor_xy"] = (float(anchor_xy[0]), float(anchor_xy[1]))
        if hit is not None and hit.get("layer_id") is not None:
            layer_id = int(hit["layer_id"])
        if layer_id is not None:
            sel["layer_id"] = int(layer_id)
        if hit is not None:
            # Kept so a Tab cycle (see _cycle_tab_expand) can restore the
            # single-primitive outline after expanding along the net.
            sel["hit"] = hit
        self._editor_selection = sel
        rings = self._primitive_outline_rings(hit) if hit else None
        self._gl_viewer.set_primitive_selection_outline(rings)
        highlight_polys: set[tuple[int, int]] = set()
        if layer_id is not None and anchor_xy is not None:
            seed_poly = self._copper_poly_under_point(
                anchor_xy[0], anchor_xy[1], int(layer_id))
            if seed_poly is not None:
                highlight_polys = self._connected_copper_polys(
                    int(layer_id), seed_poly)
        nets_hi = (set() if net == "(none)"
                   else self._connected_nets(net))
        self._apply_editor_highlight(nets_hi, polys=highlight_polys)
        self._populate_editor_form()

    def _apply_editor_highlight(self, nets: set[str],
                                polys: set[tuple[int, int]] | None = None,
                                ) -> None:
        """Set the connectivity highlight and re-render — non-connected
        copper dims to 10%. ``polys`` is an optional per-polygon highlight
        set (``{(layer_id, id(poly_dict))}``) used to light up exactly the
        connected pieces when the click landed on copper whose net name
        alone can't pin them down (e.g. ``"(none)"``)."""
        self._editor_highlight_nets = set(nets)
        self._editor_highlight_polys = set(polys or ())
        self._render()
        self._update_editor_panel()

    def _clear_editor_highlight(self) -> None:
        """Drop the connectivity highlight; copper returns to full opacity.
        Also clears the dashed-yellow outline set by a copper selection."""
        changed = bool(self._editor_highlight_nets
                       or self._editor_highlight_polys)
        self._editor_highlight_nets = set()
        self._editor_highlight_polys = set()
        if changed:
            self._render()
        self._gl_viewer.set_primitive_selection_outline(None)
