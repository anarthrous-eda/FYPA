"""Via cylinders (3D) and the 2D via-current markers."""
from __future__ import annotations

import numpy as np
from fypa.gl_mesh_viewer import LegendRow, MarkerGroup

from fypa.viewer.display import _build_cmap_lut, _VIA_CURRENT_MODE, _VOLTAGE_DROP_MODE
from fypa.viewer.mesh_geometry import (
    _apply_alpha,
    _generate_disk_cap,
    _generate_via_cylinder,
    _generate_via_cylinder_gradient,
)
from fypa.viewer.overlays import (
    _EDITOR_SCHDOC,
    _MARKER_LAYER_RING_W,
    _N_NET_MARKER_EDGE,
    _N_SIDE_TERMINALS,
    _rec_slot_tuple,
)
from fypa.viewer.prefs import _VIA_NO_CURRENT_OPACITY_DEFAULT


class _ViaRenderMixin:
    """Via cylinders (3D) and the 2D via-current markers."""

    # --- Via cylinders (3D-mode only) --------------------------------------

    # Cylinder tessellation — 10 sides keeps the silhouette smooth at
    # typical zoom without flooding the GPU when there are 100+ vias.
    _VIA_CYL_SEGMENTS: int = 10
    # Minimum world-mm radius for vias whose ``diameter_mm`` is missing
    # or oddly small (we still want them to be visible cylinders).
    _VIA_CYL_MIN_RADIUS_MM: float = 0.2
    # RGB colour of the via cylinders (matches the 2D orange via marker).
    _VIA_CYL_COLOR_RGB: tuple[float, float, float] = (
        0xff / 255.0, 0x8c / 255.0, 0x00 / 255.0,
    )
    # Alpha applied to barrel sections that carry no rail current — the metal
    # stub ends outside the via's outermost current-carrying copper
    # connection, where the solved section current goes to zero (the same
    # "no current" condition the "Grey no current copper" plane toggle
    # greys). The current-carrying span keeps alpha 1.0 so it renders
    # unchanged. This is the built-in default; the live value is the
    # per-instance ``_via_dead_section_alpha``, set from the "No-current via
    # opacity" Settings knob (persisted via
    # :func:`load_via_no_current_opacity`). Which hops count as no-current is
    # decided in :meth:`_hop_live_flags`, reusing the plane classifier's
    # ``_NO_CURRENT_PD_*`` power floor.
    _VIA_DEAD_SECTION_ALPHA: float = _VIA_NO_CURRENT_OPACITY_DEFAULT
    # Plated-through-hole pad styling — light grey so PTHs are visually
    # distinct from the orange vias in both the 2D marker overlay and the
    # 3D cylinder view. PTHs span every enabled copper layer (Altium pads
    # have no blind/buried span) so their cylinders run the full stack.
    _PTH_COLOR_HEX: str = "#b8b8b8"
    _PTH_CYL_COLOR_RGB: tuple[float, float, float] = (
        0xb8 / 255.0, 0xb8 / 255.0, 0xb8 / 255.0,
    )

    def _push_via_cylinders(self, phys_list: list[str],
                            rail_names: list[str] | str,
                            *, mode: str | None = None) -> None:
        """Build cylinder triangle geometry for every visible via and
        push it as one batch to the GLMeshViewer. Empty input clears the
        cylinders.

        A via is visible if any layer it crosses (full span, not just
        the endpoint pair) is visible — either via the primary eye on
        the selected rail (the via must then be on a rail-member net),
        or via the second eye (all-copper), which shows every net on
        the layer and so bypasses the rail filter.

        When the "Heatmap vias" toggle is on, each via is split into one
        cylinder per inter-layer segment, coloured by the active mode's
        heatmap (voltage / drop interpolate top↔bottom along the via;
        current and power are constant per segment). Vias shown only via
        all-copper have no rail-mode data and fall back to solid orange.
        Otherwise vias are drawn solid orange to match the 2D marker."""
        if self.metadata is None:
            self._gl_viewer.clear_cylinders()
            return
        # Map layer_id → physical name → rank → z. Use the same rank-
        # based z as the heatmap meshes so cylinders connect cleanly.
        id_to_phys = {v: k for k, v in self._phys_name_to_layer_id.items()}
        visible_ids = {self._phys_name_to_layer_id.get(p)
                       for p in phys_list}
        visible_ids.discard(None)
        # All-copper (second eye) gives a via an independent visibility
        # path: a via is shown if its span touches a copper-eye layer
        # even when no rail/heatmap eye on its span is open. This branch
        # bypasses the rail filter since all-copper itself shows every
        # net on the layer, not just rail members.
        copper_visible_ids = set(self._visible_all_copper_layer_ids().keys())
        if not visible_ids and not copper_visible_ids:
            self._gl_viewer.clear_cylinders()
            return
        rail_members = set(self._effective_rail_members(rail_names))

        # Via Current mode supersedes the "Heatmap vias" toggle — the
        # mode IS the heatmap, so we always colour every visible via /
        # PTH by its max-|segment-current| (looked up from
        # :attr:`_via_current_lookup`, populated in :meth:`_render`).
        via_current_on = (mode == _VIA_CURRENT_MODE)
        heatmap_on = via_current_on or (
            mode is not None
            and getattr(self, "heatmap_vias_box", None) is not None
            and self.heatmap_vias_box.isChecked()
        )
        lut: np.ndarray | None = None
        vmin = vmax = 0.0
        drop_ref = 0.0
        # Stackup order + fallback via R are needed for the no-current
        # barrel-fade in *every* mode (not just the heatmap ones), so they're
        # computed unconditionally. Each via dict carries its own per-hop R;
        # the fallback only applies if that's missing (e.g. legacy pickle).
        stackup_ids: list[int] = [row["layer_id"]
                                  for row in self.metadata.get("stackup", [])]
        fallback_r_seg = float(
            (self.metadata.get("physics_constants", {}) or {})
            .get("fallback_via_resistance_ohm", 1.0e-3)
        )
        if heatmap_on:
            lut = _build_cmap_lut(self._cmap_name)
            vmin = float(self._vmin)
            vmax = float(self._vmax)
            if vmax <= vmin:
                vmax = vmin + 1e-30
            if (mode == _VOLTAGE_DROP_MODE
                    and self._last_drop_reference is not None):
                drop_ref = float(self._last_drop_reference)

        pos_chunks: list[np.ndarray] = []
        col_chunks: list[np.ndarray] = []
        # The 2D legend's VIA row also gates the 3D cylinders, so the
        # user can hide vias from the 3D view by clicking the legend
        # entry the same way they do in 2D. PTHs aren't affected.
        vias_hidden = "VIA" in self._hidden_legend_keys
        for v in (self.metadata.get("vias", []) if not vias_hidden else []):
            ls_id = v.get("layer_start")
            le_id = v.get("layer_end")
            if ls_id is None or le_id is None:
                continue
            lo_id = min(ls_id, le_id)
            hi_id = max(ls_id, le_id)
            # Visibility: any layer the via crosses (full span, not just
            # endpoints) being visible via either eye lets the via show.
            # Primary eye (rail/heatmap) requires the via to be on a rail
            # member; second eye (all-copper) bypasses that filter.
            net = v.get("net", "")
            rail_ok = (
                bool(rail_members) and net in rail_members
                and any(lo_id <= lid <= hi_id for lid in visible_ids)
            )
            copper_ok = any(lo_id <= lid <= hi_id
                            for lid in copper_visible_ids)
            if not (rail_ok or copper_ok):
                continue
            phys_top = id_to_phys.get(lo_id)
            phys_bot = id_to_phys.get(hi_id)
            if phys_top is None or phys_bot is None:
                continue
            z_top = self._layer_z_for(phys_top)
            z_bot = self._layer_z_for(phys_bot)
            if z_top == z_bot:
                continue
            x = float(v.get("x_mm", 0.0))
            y = float(v.get("y_mm", 0.0))
            # Draw at drill diameter — that's the actual barrel that carries
            # current between layers (and what the FEM uses for R). The
            # outer pad diameter belongs to the per-layer copper plane,
            # which is already drawn as part of the layer mesh. Fall back
            # to pad diameter if drill data is missing.
            drill_mm = float(v.get("hole_diameter_mm") or 0.0)
            outer_mm = float(v.get("diameter_mm") or 0.0)
            radius = (drill_mm if drill_mm > 0.0 else outer_mm) * 0.5
            radius = max(radius, self._VIA_CYL_MIN_RADIUS_MM)

            # Heatmap path: per-segment cylinders coloured by mode. Falls
            # back to the solid-orange branch below when we can't sample
            # voltages on at least two of the via's spanned layers.
            heatmap_chunks: list[tuple[np.ndarray, np.ndarray]] = []
            if via_current_on and lut is not None:
                # Via Current mode: colour the barrel by the via's
                # max-|segment-current| (matches the Vias-tab value and the
                # scale-controller range), split into per-hop cylinders so
                # the no-current stub sections fade like every other mode.
                key = (v.get("net", ""), x, y)
                cur = self._via_current_lookup.get(key)
                if cur is not None:
                    c = self._shade(lut, cur, vmin, vmax)
                    heatmap_chunks = self._solid_via_chunks(
                        v, x, y, radius, lo_id, hi_id, z_top, z_bot,
                        stackup_ids, id_to_phys, c, fallback_r_seg,
                    )
            elif heatmap_on and lut is not None and mode is not None:
                heatmap_chunks = self._heatmap_via_chunks(
                    v, x, y, radius, lo_id, hi_id,
                    stackup_ids, id_to_phys,
                    mode, lut, vmin, vmax, fallback_r_seg, drop_ref,
                )

            if heatmap_chunks:
                for pos, col in heatmap_chunks:
                    pos_chunks.append(pos)
                    col_chunks.append(col)
            else:
                # Default (no heatmap) view: solid orange, but with the
                # no-current barrel sections faded.
                for pos, col in self._solid_via_chunks(
                    v, x, y, radius, lo_id, hi_id, z_top, z_bot,
                    stackup_ids, id_to_phys, self._VIA_CYL_COLOR_RGB,
                    fallback_r_seg,
                ):
                    pos_chunks.append(pos)
                    col_chunks.append(col)

        # Plated through-hole pads — same per-site gating as vias (visible
        # layer ∩ rail). Default to light grey; when the heatmap-vias/PTH
        # toggle is on they're coloured the same way as via cylinders via
        # the shared :meth:`_heatmap_via_chunks` helper (PTH dicts carry
        # the same ``net`` + ``segments`` keys vias do).
        for p in self.metadata.get("pths", []):
            ls_id = p.get("layer_start")
            le_id = p.get("layer_end")
            if ls_id is None or le_id is None:
                continue
            lo_id = min(ls_id, le_id)
            hi_id = max(ls_id, le_id)
            # Visibility: any layer the PTH crosses (full span, not just
            # endpoints) being visible lets it show — matching the via
            # rule above. Primary eye (rail/heatmap) requires the PTH's
            # net to be on a visible rail; the second eye (all-copper)
            # bypasses that filter.
            net = p.get("net", "")
            rail_ok = (
                bool(rail_members) and net in rail_members
                and any(lo_id <= lid <= hi_id for lid in visible_ids)
            )
            copper_ok = any(lo_id <= lid <= hi_id
                            for lid in copper_visible_ids)
            if not (rail_ok or copper_ok):
                continue
            phys_top = id_to_phys.get(lo_id)
            phys_bot = id_to_phys.get(hi_id)
            if phys_top is None or phys_bot is None:
                continue
            z_top = self._layer_z_for(phys_top)
            z_bot = self._layer_z_for(phys_bot)
            if z_top == z_bot:
                continue
            x = float(p.get("x_mm", 0.0))
            y = float(p.get("y_mm", 0.0))
            # Drill diameter, same reasoning as the via path above.
            drill_mm = float(p.get("hole_diameter_mm") or 0.0)
            outer_mm = float(p.get("diameter_mm") or 0.0)
            radius = (drill_mm if drill_mm > 0.0 else outer_mm) * 0.5
            radius = max(radius, self._VIA_CYL_MIN_RADIUS_MM)
            # Slotted PTH bore → obround barrel (only pads carry slots; vias
            # never do, so the via loop above passes no slot).
            slot = _rec_slot_tuple(p)

            heatmap_chunks: list[tuple[np.ndarray, np.ndarray]] = []
            if via_current_on:
                # Via Current mode: PTH pads aren't part of the cached
                # Vias-tab report (it iterates ``metadata['vias']``
                # only), so they have no entry in
                # ``_via_current_lookup``. Render them as the default
                # light-grey PTH colour to keep them visible without
                # implying a current reading we don't have.
                pass
            elif heatmap_on and lut is not None and mode is not None:
                heatmap_chunks = self._heatmap_via_chunks(
                    p, x, y, radius, lo_id, hi_id,
                    stackup_ids, id_to_phys,
                    mode, lut, vmin, vmax, fallback_r_seg, drop_ref,
                    slot=slot,
                )

            if heatmap_chunks:
                for pos, col in heatmap_chunks:
                    pos_chunks.append(pos)
                    col_chunks.append(col)
            else:
                # Default / Via-Current view: light grey, with no-current
                # barrel sections faded. PTHs on the active net (their
                # ``segments`` carry per-hop R the same as vias) fade; those
                # off the solution can't be sampled and stay fully opaque.
                for pos, col in self._solid_via_chunks(
                    p, x, y, radius, lo_id, hi_id, z_top, z_bot,
                    stackup_ids, id_to_phys, self._PTH_CYL_COLOR_RGB,
                    fallback_r_seg, slot=slot,
                ):
                    pos_chunks.append(pos)
                    col_chunks.append(col)

        if pos_chunks:
            # Chunks mix RGB (opaque, current-carrying / unsampled) and RGBA
            # (faded no-current sections); unify to RGBA so the batch is one
            # array. set_cylinders alpha-blends it.
            col_all = np.concatenate(
                [c if c.shape[1] == 4 else _apply_alpha(c, 1.0)
                 for c in col_chunks], axis=0)
            self._gl_viewer.set_cylinders(
                np.concatenate(pos_chunks, axis=0),
                col_all,
            )
        else:
            self._gl_viewer.clear_cylinders()

    def _sample_via_barrel(
        self, via: dict, x: float, y: float,
        lo_id: int, hi_id: int,
        stackup_ids: list[int], id_to_phys: dict[int, str],
        fallback_r_seg: float,
    ) -> tuple[list[tuple[int, float, float]], list[float]] | None:
        """Sample the barrel of one via/PTH against the current solution.

        Returns ``(sampled, powers)`` where ``sampled`` is the list of
        ``(layer_id, z, voltage)`` for every layer in the via's span that
        has solved copper on the via's net, and ``powers[i]`` is the I^2 R
        dissipation of hop ``i`` (between ``sampled[i]`` and
        ``sampled[i+1]``), derived from the voltage drop and the per-hop
        barrel resistance. Power (not raw current) so the no-current test in
        :meth:`_hop_live_flags` matches the plane classifier, which also
        thresholds on power.

        Returns ``None`` when there is no solution, the net isn't
        sampleable, or fewer than two layers on the span carry solved
        copper — the caller then draws the barrel fully opaque (no current
        data to judge which sections are live). Shares the exact sampling
        the heatmap path uses so the fade lines up with the colouring.
        """
        if getattr(self, "solution", None) is None:
            return None
        net = via.get("net", "")
        if not net or net in ("?", "NO_NET"):
            return None
        try:
            i_start = stackup_ids.index(lo_id)
            i_end = stackup_ids.index(hi_id)
        except ValueError:
            return None
        span_ids = stackup_ids[i_start:i_end + 1]
        if len(span_ids) < 2:
            return None
        sampled: list[tuple[int, float, float]] = []
        for lid in span_ids:
            phys = id_to_phys.get(lid)
            if phys is None:
                continue
            v_at = self._sample_via_voltage(phys, net, x, y)
            if v_at is None:
                continue
            sampled.append((lid, self._layer_z_for(phys), v_at))
        if len(sampled) < 2:
            return None
        r_by_pair: dict[frozenset[int], float] = {}
        for seg in via.get("segments") or []:
            r_by_pair[frozenset((seg["layer_a"], seg["layer_b"]))] = (
                float(seg["resistance_ohm"])
            )
        powers: list[float] = []
        for (lid_a, _z_a, v_a), (lid_b, _z_b, v_b) in zip(
                sampled, sampled[1:]):
            r_seg = r_by_pair.get(frozenset((lid_a, lid_b)), fallback_r_seg)
            i_seg = abs((v_a - v_b) / r_seg) if r_seg > 0 else 0.0
            powers.append(i_seg * i_seg * r_seg)
        return sampled, powers

    @classmethod
    def _hop_live_flags(cls, powers: list[float]) -> list[bool]:
        """Classify each barrel hop as current-carrying (opaque) or
        no-current (faded), from each hop's I^2 R dissipation.

        A hop carries current when its power clears the same relative floor
        the "Grey no current copper" plane classifier
        (:meth:`_no_current_mesh_set`) uses — ``_NO_CURRENT_PD_REL`` of the
        via's peak hop, with the ``_NO_CURRENT_PD_ABS`` absolute floor. So a
        barrel fades exactly where its own section current goes to zero:
        the stub ends outside the via's outermost current-carrying copper
        connection. A hop that carries the via's series current (even past a
        grey no-current island between two live connections) stays opaque.
        A via with no current on any hop reads fully faded."""
        if not powers:
            return []
        p_max = max(powers)
        if p_max <= cls._NO_CURRENT_PD_ABS:
            return [False] * len(powers)
        thr = max(cls._NO_CURRENT_PD_ABS, cls._NO_CURRENT_PD_REL * p_max)
        return [p > thr for p in powers]

    def _solid_via_chunks(
        self, via: dict, x: float, y: float, radius: float,
        lo_id: int, hi_id: int, z_top: float, z_bot: float,
        stackup_ids: list[int], id_to_phys: dict[int, str],
        color_rgb: tuple[float, float, float], fallback_r_seg: float,
        slot: tuple[float, float, float, bool] | None = None,
    ) -> list[tuple[np.ndarray, np.ndarray]]:
        """Flat-colour barrel for a via/PTH, split so sections that carry no
        rail current fade to :attr:`_via_dead_section_alpha` (the "No-current
        via opacity" Settings knob).

        The metal stubs above the topmost / below the bottommost live copper
        carry no current and are always faded; each inter-layer hop is opaque
        only where it carries current (see :meth:`_hop_live_flags`). When the
        via's net can't be sampled on >=2 layers there's no current data, so a
        single full-span opaque cylinder is returned — the pre-fade behaviour.
        """
        sampled_powers = self._sample_via_barrel(
            via, x, y, lo_id, hi_id, stackup_ids, id_to_phys, fallback_r_seg)
        if sampled_powers is None:
            pos, col = _generate_via_cylinder(
                x, y, z_top, z_bot, radius, color_rgb,
                n_segments=self._VIA_CYL_SEGMENTS, slot=slot,
            )
            return [(pos, col)]
        sampled, powers = sampled_powers
        live = self._hop_live_flags(powers)
        dead = self._via_dead_section_alpha
        chunks: list[tuple[np.ndarray, np.ndarray]] = []

        def _cyl(za: float, zb: float, alpha: float) -> None:
            if za == zb:
                return
            pos, col = _generate_via_cylinder(
                x, y, za, zb, radius, color_rgb,
                n_segments=self._VIA_CYL_SEGMENTS, slot=slot,
            )
            chunks.append((pos, _apply_alpha(col, alpha)))

        # Faded metal stub above the topmost live copper.
        _cyl(z_top, sampled[0][1], dead)
        for i, ((_la, z_a, _va), (_lb, z_b, _vb)) in enumerate(
                zip(sampled, sampled[1:])):
            _cyl(z_a, z_b, 1.0 if live[i] else dead)
        # Faded metal stub below the bottommost live copper.
        _cyl(sampled[-1][1], z_bot, dead)
        # All hops collapsed to zero-length (degenerate stackup) — fall back
        # to a single opaque cylinder so the via never vanishes entirely.
        if not chunks:
            pos, col = _generate_via_cylinder(
                x, y, z_top, z_bot, radius, color_rgb,
                n_segments=self._VIA_CYL_SEGMENTS, slot=slot,
            )
            return [(pos, col)]
        return chunks

    def _heatmap_via_chunks(
        self, via: dict, x: float, y: float, radius: float,
        lo_id: int, hi_id: int,
        stackup_ids: list[int], id_to_phys: dict[int, str],
        mode: str, lut: np.ndarray, vmin: float, vmax: float,
        fallback_r_seg: float, drop_ref: float,
        slot: tuple[float, float, float, bool] | None = None,
    ) -> list[tuple[np.ndarray, np.ndarray]]:
        """Per-segment cylinder geometry for one via, coloured by mode.

        Voltage / Voltage Drop produce a smooth top↔bottom gradient on
        each segment; Current Density / Power Density produce a single
        colour per segment (the value is constant along the resistor).
        Returns an empty list when the via has fewer than two sampleable
        layers — caller falls back to the solid-orange path.
        """
        net = via.get("net", "")
        if not net or net in ("?", "NO_NET"):
            return []
        try:
            i_start = stackup_ids.index(lo_id)
            i_end = stackup_ids.index(hi_id)
        except ValueError:
            return []
        span_ids = stackup_ids[i_start:i_end + 1]
        if len(span_ids) < 2:
            return []
        # (layer_id, z, raw_voltage) for every layer in the span where copper
        # for this net exists. Layers without a sampleable voltage are dropped
        # — the resulting segment list matches the FEM coupling network.
        sampled: list[tuple[int, float, float]] = []
        for lid in span_ids:
            phys = id_to_phys.get(lid)
            if phys is None:
                continue
            v_at = self._sample_via_voltage(phys, net, x, y)
            if v_at is None:
                continue
            sampled.append((lid, self._layer_z_for(phys), v_at))
        if len(sampled) < 2:
            # Only one layer was sampleable. For Voltage / Voltage Drop we can
            # still colour the whole cylinder barrel with that one voltage —
            # via resistance is negligible so a solid shade is a good
            # approximation. Current / Power need at least two voltage samples
            # to compute a meaningful value, so those modes fall back.
            if len(sampled) == 1 and mode in ("Voltage", _VOLTAGE_DROP_MODE):
                phys_lo = id_to_phys.get(lo_id)
                phys_hi = id_to_phys.get(hi_id)
                if phys_lo is None or phys_hi is None:
                    return []
                z_lo = self._layer_z_for(phys_lo)
                z_hi = self._layer_z_for(phys_hi)
                if z_lo == z_hi:
                    return []
                val = sampled[0][2] - drop_ref
                c = self._shade(lut, val, vmin, vmax)
                pos, col = _generate_via_cylinder(
                    x, y, z_lo, z_hi, radius, c,
                    n_segments=self._VIA_CYL_SEGMENTS,
                    slot=slot,
                )
                # Single cap at the one layer we could sample.
                _, z_cap, v_cap = sampled[0]
                c_cap = self._shade(lut, v_cap - drop_ref, vmin, vmax)
                cap_pos, cap_col = _generate_disk_cap(
                    x, y, z_cap, radius, c_cap,
                    n_segments=self._VIA_CYL_SEGMENTS,
                    slot=slot,
                )
                return [(pos, col), (cap_pos, cap_col)]
            return []

        # Per-hop R lookup, keyed unordered so (a,b) and (b,a) both match.
        r_by_pair: dict[frozenset[int], float] = {}
        for seg in via.get("segments") or []:
            r_by_pair[frozenset((seg["layer_a"], seg["layer_b"]))] = (
                float(seg["resistance_ohm"])
            )

        # Per-hop I^2 R dissipation → which hops carry rail current.
        # No-current hops fade to _VIA_DEAD_SECTION_ALPHA while keeping their
        # heatmap colour, so the fade lines up hop-for-hop with the solid and
        # Via-Current paths (all three read _hop_live_flags off the same R).
        hop_powers: list[float] = []
        for (lid_a, _z_a, v_a), (lid_b, _z_b, v_b) in zip(
                sampled, sampled[1:]):
            r_seg = r_by_pair.get(frozenset((lid_a, lid_b)), fallback_r_seg)
            i_seg = abs((v_a - v_b) / r_seg) if r_seg > 0 else 0.0
            hop_powers.append(i_seg * i_seg * r_seg)
        live = self._hop_live_flags(hop_powers)
        dead = self._via_dead_section_alpha

        chunks: list[tuple[np.ndarray, np.ndarray]] = []
        for i, ((lid_a, z_a, v_a), (lid_b, z_b, v_b)) in enumerate(
                zip(sampled, sampled[1:])):
            if z_a == z_b:
                continue
            if mode in ("Voltage", _VOLTAGE_DROP_MODE):
                top_val = v_a - drop_ref
                bot_val = v_b - drop_ref
                ct = self._shade(lut, top_val, vmin, vmax)
                cb = self._shade(lut, bot_val, vmin, vmax)
                pos, col = _generate_via_cylinder_gradient(
                    x, y, z_a, z_b, radius, ct, cb,
                    n_segments=self._VIA_CYL_SEGMENTS,
                    slot=slot,
                )
            else:
                # Current / power are constant along each inter-layer barrel
                # resistor — colour the whole segment one shade. R is the
                # actual per-hop value the FEM used (varies with drill,
                # plating, and dielectric thickness traversed).
                r_seg = r_by_pair.get(
                    frozenset((lid_a, lid_b)), fallback_r_seg,
                )
                i_seg = (v_a - v_b) / r_seg if r_seg > 0 else 0.0
                if mode == "Current Density":
                    val = abs(i_seg)
                else:  # Power Density (and any unexpected mode → power)
                    val = i_seg * i_seg * r_seg
                c = self._shade(lut, val, vmin, vmax)
                pos, col = _generate_via_cylinder(
                    x, y, z_a, z_b, radius, c,
                    n_segments=self._VIA_CYL_SEGMENTS,
                    slot=slot,
                )
            chunks.append(
                (pos, _apply_alpha(col, 1.0 if live[i] else dead)))

        # For Voltage / Voltage Drop add a filled disk cap at the top and
        # bottom of the via so the endpoint colour is clearly visible at each
        # copper-layer junction. The cap colour matches the sampled voltage at
        # that layer, which is the same value the copper-mesh shader draws at
        # (x, y) — so any z-fighting with the copper surface is invisible
        # (both surfaces are the same colour). Caps are omitted for
        # Current / Power Density because those modes assign per-segment values
        # rather than per-layer endpoint values.
        if chunks and mode in ("Voltage", _VOLTAGE_DROP_MODE):
            _, z_top_cap, v_top_cap = sampled[0]
            _, z_bot_cap, v_bot_cap = sampled[-1]
            ct_cap = self._shade(lut, v_top_cap - drop_ref, vmin, vmax)
            cb_cap = self._shade(lut, v_bot_cap - drop_ref, vmin, vmax)
            # Each end cap sits at the junction of the outermost hop, so it
            # fades with that hop's liveness — a cap on a dead stub end
            # shouldn't read as a bright solid disk.
            a_top = 1.0 if (live and live[0]) else dead
            a_bot = 1.0 if (live and live[-1]) else dead
            pos_t, col_t = _generate_disk_cap(
                x, y, z_top_cap, radius, ct_cap,
                n_segments=self._VIA_CYL_SEGMENTS,
                slot=slot,
            )
            pos_b, col_b = _generate_disk_cap(
                x, y, z_bot_cap, radius, cb_cap,
                n_segments=self._VIA_CYL_SEGMENTS,
                slot=slot,
            )
            chunks = ([(pos_t, _apply_alpha(col_t, a_top))] + chunks
                      + [(pos_b, _apply_alpha(col_b, a_bot))])

        return chunks

    def _via_voltage_kdtree(
        self, phys_name: str, net_name: str,
    ) -> tuple | None:
        """Lazily build (and cache for the viewer's lifetime) a nearest-
        vertex voltage lookup for the (physical layer, net) solution
        layer. Returns ``(cKDTree, potentials)`` — a kd-tree of mesh
        vertex (x, y) positions plus the matching per-vertex potentials —
        or ``None`` if no such layer exists / its mesh is empty.

        Padne adds via + directive-pin coupling sites to the Triangle
        mesher as Steiner points, so every point this samples (via
        centres, resistor pins) IS a mesh vertex — the nearest-vertex
        potential is then the exact mesh-side voltage there.

        This used to build a matplotlib ``LinearTriInterpolator``, whose
        implicit ``TrapezoidMapTriFinder`` build is O(seconds) on a large
        plane (e.g. GND) and froze the GUI for several seconds the first
        time a heavy rail was shown — the per-(phys, net) cache is why
        only the *first* toggle stalled. A ``cKDTree`` build is pure C and
        ~100x faster — the same swap the Vias / Nodes report tables
        already made (see :meth:`_compute_via_report`).

        Orphan vertices — those not referenced by any triangle, which the
        FEM pins to V=0 to keep the linear system non-singular — are
        excluded so an off-copper sample never snaps to a fake 0 V node.
        """
        key = (phys_name, net_name)
        if key in self._via_voltage_kdtree_cache:
            return self._via_voltage_kdtree_cache[key]
        li = self._index_by_pair.get(key)
        if li is None:
            self._via_voltage_kdtree_cache[key] = None
            return None
        ls = self.solution.layer_solutions[li]
        xs_parts: list[np.ndarray] = []
        ys_parts: list[np.ndarray] = []
        vs_parts: list[np.ndarray] = []
        for xys, tris_local, pot in zip(
            ls.vertex_xys, ls.triangles, ls.potentials,
        ):
            if xys.shape[0] == 0 or tris_local.size == 0:
                continue
            used = np.unique(tris_local.ravel())
            xs_parts.append(xys[used, 0])
            ys_parts.append(xys[used, 1])
            vs_parts.append(pot[used])
        if not xs_parts:
            self._via_voltage_kdtree_cache[key] = None
            return None
        from scipy.spatial import cKDTree
        pts = np.column_stack([
            np.concatenate(xs_parts), np.concatenate(ys_parts),
        ])
        entry = (cKDTree(pts), np.concatenate(vs_parts))
        self._via_voltage_kdtree_cache[key] = entry
        return entry

    def _sample_via_voltage(
        self, phys_name: str, net_name: str, x: float, y: float,
    ) -> float | None:
        """Sample the (phys, net) voltage at (x, y) via a nearest mesh-
        vertex lookup. ``None`` only if the (phys, net) layer doesn't
        exist or has no mesh.

        For via centres and directive pins the (x, y) IS a mesh vertex
        (a padne Steiner point), so the nearest vertex is an exact hit.
        For a point off the solved copper — a stub centroid, or a via in
        a clearance gap — the nearest mesh vertex on the same net is
        still the best estimate: the coupling node is always close by,
        and voltage is near-constant over copper carrying little current.
        """
        entry = self._via_voltage_kdtree(phys_name, net_name)
        if entry is None:
            return None
        tree, vs = entry
        _dist, idx = tree.query((x, y))
        f = float(vs[int(idx)])
        return f if np.isfinite(f) else None

    # Colour buckets used by the 2D Via Current marker overlay. 64 is
    # fine enough that adjacent buckets are visually indistinguishable
    # at typical zoom, while keeping the number of MarkerGroup draw
    # calls bounded no matter how many vias the board has.
    _VIA_CURRENT_BUCKETS: int = 128
    # Minimum pixel diameter for 2D Via Current markers. When the user
    # zooms out so far that a via's physical footprint would be
    # sub-pixel, the marker stays at this floor so vias never shrink
    # to invisible dots. At those zoom levels markers will overlap —
    # the user explicitly asked for this trade-off so the heatmap
    # remains readable.
    _VIA_MARKER_MIN_PX: float = 6.0
    # Fallback diameter (mm) for vias with no diameter / hole metadata.
    # 0.4 mm matches the conservative default used by the cylinder
    # path's ``_VIA_CYL_MIN_RADIUS_MM``.
    _VIA_MARKER_FALLBACK_DIAM_MM: float = 0.4

    def _build_via_current_marker_groups(
        self, target_layer_ids: set[int], rail_members: set[str],
    ) -> list[MarkerGroup]:
        """Build a list of MarkerGroups for the 2D Via Current overlay.

        Vias whose span includes a visible layer AND whose net is on
        the selected rails are looked up in :attr:`_via_current_lookup`
        for their max-|segment-current|, then bucketed into
        :data:`_VIA_CURRENT_BUCKETS` colour bins. One MarkerGroup per
        non-empty bucket gives a heatmap-coloured marker per via with
        a bounded number of draw calls. Each marker's drawn pixel
        diameter scales with the via's physical pad diameter (so
        zooming in reveals the real footprint), floored at
        :data:`_VIA_MARKER_MIN_PX` (so zooming out doesn't collapse
        vias into invisible dots).
        """
        if not self._via_current_lookup or self.metadata is None:
            return []
        lut = _build_cmap_lut(self._cmap_name)
        vmin = float(self._vmin)
        vmax = float(self._vmax)
        if vmax <= vmin:
            vmax = vmin + 1e-12
        n_buckets = max(2, int(self._VIA_CURRENT_BUCKETS))
        # bucket -> list of (x, y, diameter_mm)
        buckets: dict[int, list[tuple[float, float, float]]] = {}
        for v in self.metadata.get("vias", []):
            net = v.get("net", "")
            if rail_members and net not in rail_members:
                continue
            ls_id = v.get("layer_start")
            le_id = v.get("layer_end")
            if ls_id is None or le_id is None:
                continue
            lo, hi = (ls_id, le_id) if ls_id <= le_id else (le_id, ls_id)
            if not any(lo <= lid <= hi for lid in target_layer_ids):
                continue
            x = float(v.get("x_mm", 0.0))
            y = float(v.get("y_mm", 0.0))
            cur = self._via_current_lookup.get((net, x, y))
            if cur is None:
                continue
            t = (cur - vmin) / (vmax - vmin)
            t = max(0.0, min(1.0, t))
            bucket = int(round(t * (n_buckets - 1)))
            # Match the 3D cylinder convention: drill diameter is the
            # actual current-carrying barrel and the value the FEM
            # solved with, so the 2D marker tracks the same metric.
            # Pad diameter (the annular ring) varies more across via
            # classes — using it makes power vs signal vias look
            # noticeably different even when their drill bores are
            # similar. Fall back to pad diameter, then to a sane
            # default, if drill data is missing.
            drill = float(v.get("hole_diameter_mm") or 0.0)
            outer = float(v.get("diameter_mm") or 0.0)
            diameter_mm = drill if drill > 0.0 else (
                outer if outer > 0.0 else self._VIA_MARKER_FALLBACK_DIAM_MM
            )
            buckets.setdefault(bucket, []).append((x, y, diameter_mm))
        groups: list[MarkerGroup] = []
        lut_max = lut.shape[0] - 1
        bucket_div = max(1, n_buckets - 1)
        for bucket, items in buckets.items():
            lut_idx = int(round(bucket / bucket_div * lut_max))
            r, g, b = int(lut[lut_idx, 0]), int(lut[lut_idx, 1]), int(lut[lut_idx, 2])
            hex_color = f"#{r:02x}{g:02x}{b:02x}"
            n = len(items)
            xs_arr = np.fromiter((it[0] for it in items), dtype=np.float64,
                                  count=n)
            ys_arr = np.fromiter((it[1] for it in items), dtype=np.float64,
                                  count=n)
            diam_arr = np.fromiter((it[2] for it in items), dtype=np.float64,
                                    count=n)
            groups.append(MarkerGroup(
                xs=xs_arr,
                ys=ys_arr,
                color=hex_color,
                symbol="o",
                size=8,  # ignored when world_diameters_mm is set
                edge_color="#000000",
                edge_width=0.4,
                world_diameters_mm=diam_arr,
                min_pixel_diameter=self._VIA_MARKER_MIN_PX,
            ))
        return groups

    def _update_markers_and_legend(self, phys_list: list[str],
                                   rail_names: list[str] | str) -> None:
        """Build the marker overlay + legend for the current view and
        push both to the GL viewer.

        Role markers (SOURCE / SINK / SERIES / REGULATOR), the orange
        via dots, and the Vias-tab "Go" highlight (a yellow ring at
        :attr:`_highlight_via_xy`) are drawn whenever a layer and rail
        are selected.

        Each legend row is also a per-category visibility toggle: clicking
        a row sets :attr:`_hidden_legend_keys` and the matching marker
        groups are skipped on the next render (the row stays in the
        chip, slashed).
        """
        groups: list[MarkerGroup] = []
        # Structured legend rows pushed to the GL viewer's top-right
        # chip. A row marked hidden suppresses its MarkerGroup above but
        # still appears in the chip (slashed) so the toggle is
        # discoverable + reversible.
        legend_rows: list[LegendRow] = []
        legend_seen_keys: set[str] = set()

        def _add_legend(key: str, glyph_symbol: str, color: str,
                        label: str | None = None) -> None:
            if key in legend_seen_keys:
                return
            legend_seen_keys.add(key)
            legend_rows.append(LegendRow(
                key=key,
                label=label or key,
                glyph=self._LEGEND_GLYPHS.get(glyph_symbol, "●"),
                color=color,
                hidden=key in self._hidden_legend_keys,
            ))

        # Hover-index for SOURCE/SINK markers is rebuilt from the same
        # pin walk below, so drop the stale cache up-front. ``hover_rows``
        # collects the solved-directive entries; editor-directive rows are
        # appended once the editor markers are built (see below).
        self._marker_hover_index_cache = None
        hover_rows: list[dict] = []

        target_layer_ids: set[int] = set()
        for phys in phys_list:
            lid = self._phys_name_to_layer_id.get(phys)
            if lid is not None:
                target_layer_ids.add(lid)

        # In 3D mode, each marker needs its layer's z so projection
        # through the MVP places it on the correct copper plane.
        in_3d = self.view_3d_box.isChecked()
        id_to_phys = {v: k for k, v in self._phys_name_to_layer_id.items()}
        mode = self.mode_combo.currentText()
        is_via_current = (mode == _VIA_CURRENT_MODE)

        # All-copper layers are an independent visibility source for via
        # markers (see :meth:`_push_via_cylinders`); we still want to
        # enter the block when only copper eyes are on so those vias get
        # emitted. The pin walk naturally produces nothing in that case
        # because every pin's layer_id falls outside ``target_layer_ids``.
        copper_layer_ids = set(self._visible_all_copper_layer_ids().keys())
        if (self.metadata is not None
                and (target_layer_ids or copper_layer_ids)):
            rail_members = set(self._effective_rail_members(rail_names))
            # Solved SOURCE / SINK / SERIES markers follow the net focus the
            # same way the editor-directive ones do (see
            # :meth:`_directive_focus_visible`). ``None`` when no focus is
            # held, so the ordinary viewer is untouched.
            focus_marker_nets = (self._editor_focus_nets
                                 if self._editor_focus_active() else None)

            # Keyed (role, is_n_side): the P side and N side of a
            # directive draw as separate groups so the N side can carry
            # the green outline.
            per_role: dict[tuple[str, bool],
                           tuple[list[float], list[float],
                                 list[float], list[str | None]]] = {}
            # Designators whose schematic directive an editor directive
            # overrides — their hover rows are suppressed so the bottom
            # bar reports the (pending) editor value, not the stale one.
            overridden = self._overridden_designators()
            for d in self.metadata.get("directives", []):
                role = d.get("role")
                if role not in self._ROLE_MARKER_STYLE:
                    continue
                # In editor mode the placed editor directives draw their own
                # live markers (see _editor_marker_groups), so skip the solved
                # copies of them here — otherwise each free source / sink shows
                # twice (the editable marker plus a static solved one at the
                # same anchor). Outside editor mode the solved marker is the
                # only representation, so keep it.
                if self._editor_mode and d.get("schdoc") == _EDITOR_SCHDOC:
                    continue
                directive_overridden = d.get("designator") in overridden
                directive_current = self._directive_current_for_hover(d)
                directive_label = str(d.get("label") or d.get("designator")
                                      or "")
                for term_name, term in (d.get("terminals") or {}).items():
                    term_pins = term.get("pins") or []
                    is_n_side = term_name in _N_SIDE_TERMINALS
                    # Per-pin current estimate for hover. Equal split when
                    # area weighting is off; I * A_i / ΣA when the solve used
                    # area-weighted star coupling (physics_constants flag).
                    # Same equal-potential approximation as the star model —
                    # FEM pin currents can still differ when copper access
                    # to the pads is unequal.
                    n_pins = len(term_pins)
                    area_weighted = bool(
                        ((self.metadata or {}).get("physics_constants") or {})
                        .get("area_weighted_pin_coupling")
                    )
                    per_pin_currents: list[float | None]
                    if (directive_current is not None
                            and np.isfinite(directive_current)
                            and n_pins > 0):
                        if area_weighted:
                            areas = [
                                float(p.get("area_mm2") or 0.0)
                                for p in term_pins
                            ]
                            positive = [a for a in areas if a > 0.0]
                            if positive:
                                a_mean = sum(positive) / len(positive)
                                weights = [
                                    a if a > 0.0 else a_mean for a in areas
                                ]
                                wsum = sum(weights) or 1.0
                                per_pin_currents = [
                                    directive_current * w / wsum
                                    for w in weights
                                ]
                            else:
                                per_pin_currents = [
                                    directive_current / n_pins
                                ] * n_pins
                        else:
                            per_pin_currents = [
                                directive_current / n_pins
                            ] * n_pins
                    else:
                        per_pin_currents = [None] * n_pins
                    for pin_i, pin in enumerate(term_pins):
                        lid = pin.get("layer_id")
                        if lid not in target_layer_ids:
                            continue
                        # A solved directive's markers — both the P side and
                        # the return (N) side — show only while their net is
                        # on a visible rail. This holds in editor mode as well
                        # as normal viewing, so hiding a rail also hides its
                        # source / sink / return markers instead of leaking
                        # every rail's directives in at once. No visible rail
                        # ⇒ no rail markers (don't fall through to "show
                        # everything").
                        # An auto-bridge is not gated on rail visibility:
                        # after the merge both its pads report the surviving
                        # net, which is usually a return net the user is not
                        # currently viewing — so the rail test would hide
                        # exactly the inferred shorts they most need to see.
                        # Its own legend row provides the off switch instead.
                        if (role != "AUTO_BRIDGE"
                                and pin.get("net") not in rail_members):
                            continue
                        # Net focus narrows this further to the focused net:
                        # the whole point of focusing is to see one net's
                        # sources and sinks without the neighbours'. Unlike
                        # the rail test above, this one also takes
                        # AUTO_BRIDGE — an inferred short on other copper is
                        # exactly the kind of clutter focus is clearing.
                        if focus_marker_nets is not None \
                                and pin.get("net") not in focus_marker_nets:
                            continue
                        xs, ys, zs, rcs = per_role.setdefault(
                            (role, is_n_side), ([], [], [], []))
                        px = pin.get("x_mm", 0.0)
                        py = pin.get("y_mm", 0.0)
                        xs.append(px)
                        ys.append(py)
                        phys_for_pin = id_to_phys.get(lid)
                        zs.append(self._layer_z_for(phys_for_pin)
                                   if phys_for_pin else 0.0)
                        rcs.append(self._layer_color_for(phys_for_pin)
                                   if phys_for_pin else None)
                        if role in ("SOURCE", "SINK") and not directive_overridden:
                            hover_rows.append({
                                "x_mm": float(px),
                                "y_mm": float(py),
                                "role": role,
                                "label": directive_label,
                                "terminal": term_name,
                                "net": pin.get("net", ""),
                                "physical": phys_for_pin or "",
                                "current_a": per_pin_currents[pin_i],
                                "directive_current_a": directive_current,
                                "terminal_pin_count": n_pins,
                                "size_px": int(
                                    self._ROLE_MARKER_STYLE[role]["size"]
                                ),
                            })

            for (role, is_n_side), (xs, ys, zs, rcs) in per_role.items():
                if not xs:
                    continue
                style = self._ROLE_MARKER_STYLE[role]
                role_key = style["label"]
                # One legend row per role — the P / N split is a marker
                # detail, not a separate legend entry. Add the row even
                # when the user has hidden it (so the toggle stays
                # reachable) but skip emitting the MarkerGroup so the
                # markers themselves disappear from the canvas.
                _add_legend(role_key, style["symbol"], style["color"])
                if role_key in self._hidden_legend_keys:
                    continue
                groups.append(MarkerGroup(
                    xs=np.asarray(xs, dtype=np.float64),
                    ys=np.asarray(ys, dtype=np.float64),
                    zs=np.asarray(zs, dtype=np.float64),
                    color=style["color"],
                    symbol=style["symbol"],
                    size=int(style["size"]),
                    edge_color=(_N_NET_MARKER_EDGE if is_n_side
                                else "#000000"),
                    edge_width=1.6,
                    ring_colors=rcs,
                    ring_width=_MARKER_LAYER_RING_W,
                ))

            # Via *markers* (orange dots) — the MarkerGroup is 2D-only
            # (in 3D the cylinders drawn natively in GL take over the
            # same role), but the VIA legend row is registered in both
            # modes so the user can hide vias from the 3D view too.
            # :meth:`_push_via_cylinders` gates on the same hidden key.
            # Skipped entirely in Via Current mode — those vias come via
            # the per-bucket coloured marker batch below so the 2D view
            # shows the same heatmap the cylinders show in 3D.
            if not is_via_current:
                # (x, y) -> diameter_mm: dedup across visible layers while
                # keeping each via's physical diameter so the orange dot
                # is sized to the real footprint. The GL viewer floors the
                # drawn size at _VIA_MARKER_MIN_PX so vias stay visible
                # when zoomed out.
                via_pts: dict[tuple[float, float], float] = {}
                for lid in target_layer_ids:
                    vxs, vys, vds = self._collect_via_positions(
                        lid, rail_members)
                    for vx, vy, vd in zip(vxs, vys, vds):
                        via_pts[(vx, vy)] = vd
                # All-copper (second eye) layers add their own vias
                # without the rail filter — same rule as the 3D cylinder
                # path in :meth:`_push_via_cylinders`.
                for lid in copper_layer_ids:
                    vxs, vys, vds = self._collect_via_positions(
                        lid, set(), rail_scoped=False)
                    for vx, vy, vd in zip(vxs, vys, vds):
                        via_pts[(vx, vy)] = vd
                if via_pts:
                    _add_legend("VIA", "o", "#ff8c00")
                    if (not in_3d
                            and "VIA" not in self._hidden_legend_keys):
                        n_via = len(via_pts)
                        via_xs = np.fromiter((p[0] for p in via_pts),
                                             dtype=np.float64, count=n_via)
                        via_ys = np.fromiter((p[1] for p in via_pts),
                                             dtype=np.float64, count=n_via)
                        via_ds = np.fromiter(via_pts.values(),
                                             dtype=np.float64, count=n_via)
                        groups.append(MarkerGroup(
                            xs=via_xs,
                            ys=via_ys,
                            color="#ff8c00",
                            symbol="o",
                            size=6,  # ignored when world_diameters_mm set
                            edge_color="#000000",
                            edge_width=0.4,
                            world_diameters_mm=via_ds,
                            min_pixel_diameter=self._VIA_MARKER_MIN_PX,
                        ))

                # PTH (plated through-hole) markers — 2D only; the 3D
                # path uses the cylinder batch instead and has no legend
                # toggle for PTHs.
                # (x, y) -> (diameter_mm, slot) so a slotted PTH draws as a
                # capsule sized to its bore while round PTHs stay dots.
                pth_pts: dict[tuple[float, float],
                              tuple[float, tuple[float, float, float, bool]
                                    | None]] = {}
                if not in_3d:
                    for lid in target_layer_ids:
                        pxs, pys, pds, psl = self._collect_pth_positions(
                            lid, rail_members)
                        for px, py, pd, ps in zip(pxs, pys, pds, psl):
                            pth_pts[(px, py)] = (pd, ps)
                    # All-copper (second eye) layers add their own PTHs
                    # without the rail filter — same rule as vias above:
                    # a PTH shows whenever any layer within its span has
                    # its copper eye on, regardless of net / rail.
                    for lid in copper_layer_ids:
                        pxs, pys, pds, psl = self._collect_pth_positions(
                            lid, set(), rail_scoped=False)
                        for px, py, pd, ps in zip(pxs, pys, pds, psl):
                            pth_pts[(px, py)] = (pd, ps)
                if pth_pts:
                    _add_legend("PTH", "o", self._PTH_COLOR_HEX)
                    if "PTH" not in self._hidden_legend_keys:
                        n_pth = len(pth_pts)
                        pth_xs = np.fromiter((p[0] for p in pth_pts),
                                             dtype=np.float64, count=n_pth)
                        pth_ys = np.fromiter((p[1] for p in pth_pts),
                                             dtype=np.float64, count=n_pth)
                        pth_ds = np.fromiter(
                            (v[0] for v in pth_pts.values()),
                            dtype=np.float64, count=n_pth)
                        pth_slots = [v[1] for v in pth_pts.values()]
                        # Only attach the obround list when at least one PTH
                        # is slotted — keeps the common all-round board on the
                        # plain circular-marker path.
                        world_obrounds = (pth_slots
                                          if any(s is not None
                                                 for s in pth_slots)
                                          else None)
                        groups.append(MarkerGroup(
                            xs=pth_xs,
                            ys=pth_ys,
                            color=self._PTH_COLOR_HEX,
                            symbol="o",
                            size=6,  # ignored when world_diameters_mm set
                            edge_color="#000000",
                            edge_width=0.4,
                            world_diameters_mm=pth_ds,
                            min_pixel_diameter=self._VIA_MARKER_MIN_PX,
                            world_obrounds=world_obrounds,
                        ))

        # Via Current mode (2D fallback): emit one MarkerGroup per
        # colour bucket so each via shows its current value. The
        # scale controller already explains the ramp, so we don't add
        # legend rows for the bucketed groups.
        if (is_via_current and not in_3d
                and target_layer_ids and self.metadata is not None):
            rail_members = set(self._effective_rail_members(rail_names))
            groups.extend(self._build_via_current_marker_groups(
                target_layer_ids, rail_members,
            ))

        # Jump-highlight ring — always shown, drawn last so it sits on
        # top of every other marker. In 3D place it on the top of the
        # stackup (z=0) so it's clearly visible above the via cylinder.
        if self._highlight_via_xy is not None:
            hx, hy = self._highlight_via_xy
            groups.append(MarkerGroup(
                xs=np.array([hx], dtype=np.float64),
                ys=np.array([hy], dtype=np.float64),
                zs=np.array([0.0], dtype=np.float64),
                color="#ffff00",
                symbol="o",
                size=28,
                edge_color="#000000",
                edge_width=2.5,
            ))

        # Editor-mode directive markers (placed sources / sinks) — only
        # populated while editor mode is active. The non-editor groups are
        # cached so a free-marker drag can refresh just the markers via
        # _refresh_editor_markers without re-walking every pin.
        self._non_editor_marker_groups = list(groups)
        groups.extend(self._editor_marker_groups())

        self._gl_viewer.set_markers(groups)

        # Hover index = solved-directive rows + editor-directive rows, so
        # the bottom bar reports pending editor edits before a Resolve.
        self._metadata_marker_hover_rows = hover_rows
        self._set_marker_hover_rows(
            hover_rows + self._editor_marker_hover_rows())

        self._gl_viewer.set_overlay_top_right_legend(legend_rows)
