"""Point sampling of a solved PDN, shared by the viewer tables and the report.

The Nodes and Vias tabs and the design report all need the same thing: the
solved voltage (and power density) at a directive pin or a via site. This
module holds that sampling plus the two row builders, free of Qt so the
report can run headless (``FYPA report``) and still agree to the last digit
with what the viewer's tables show.

Every sample is a nearest-vertex lookup in a ``scipy.spatial.cKDTree`` of one
(physical layer, net) mesh. Padne adds directive-pin and via coupling sites as
Steiner points to the Triangle mesher, so a pin's or via's ``(x, y)`` IS a mesh
vertex and the nearest-vertex value is the exact solved value there.
"""
from __future__ import annotations

import math
from collections.abc import Callable

import numpy as np

# Tolerance for "this xy matches a mesh vertex". 0.01 mm is well below any
# Altium grid and comfortably above float noise. Anything farther is treated
# as off-mesh (the sample comes back ``None``).
MATCH_TOL_MM = 0.01


def split_composite_name(name: str) -> tuple[str, str]:
    """``"Top Layer|+3V3"`` → ``("Top Layer", "+3V3")``."""
    if "|" in name:
        phys, net = name.split("|", 1)
        return phys, net
    return name, ""


def face_to_vertex_average(tris: np.ndarray, face_values: np.ndarray,
                           n_verts: int) -> np.ndarray:
    """Average face-defined values onto vertices (each vertex gets the mean
    of its incident faces). Same accumulation as the viewer's
    ``_face_to_vertex_average`` so sampled values match the heatmap."""
    if tris.size == 0:
        return np.zeros(n_verts, dtype=np.float64)
    flat = tris.ravel()
    totals = np.bincount(
        flat, weights=np.repeat(face_values, 3), minlength=n_verts,
    )[:n_verts]
    counts = np.bincount(flat, minlength=n_verts)[:n_verts].astype(np.float64)
    counts[counts == 0] = 1.0
    return totals / counts


def pin_display_pad(pin: dict | None) -> str:
    """Display label for a metadata pin — prefer compound ``pad_label``."""
    if not pin:
        return ""
    label = pin.get("pad_label")
    if label:
        return str(label)
    pad = pin.get("pad", "")
    comp = pin.get("component")
    if comp and pad:
        return f"{comp}-{pad}"
    return "" if pad is None else str(pad)


class SolutionSampler:
    """Nearest-vertex sampler over a solved :class:`LeanSolution`.

    Trees are built lazily per (physical layer, net) — only for pairs that
    are actually sampled — and cached for the sampler's lifetime.

    Orphan vertices (not referenced by any triangle) are left out of every
    tree. Padne pins those to V=0 to keep the linear system non-singular;
    including them would let a pin or via sample V=0 instead of the real
    voltage when its (x, y) sits within the match tolerance of an orphan —
    which, combined with a ~1 mΩ via hop, reads as a multi-thousand-amp
    ghost current.
    """

    def __init__(self, solution, metadata: dict | None):
        self.solution = solution
        self.metadata = metadata or {}
        self.index_by_pair: dict[tuple[str, str], int] = {}
        for i, layer in enumerate(solution.problem.layers):
            self.index_by_pair[split_composite_name(layer.name)] = i
        self.phys_name_to_layer_id: dict[str, int] = {
            row["name"]: row["layer_id"]
            for row in self.metadata.get("stackup", [])
        }
        self.id_to_phys: dict[int, str] = {
            v: k for k, v in self.phys_name_to_layer_id.items()}
        # (phys, net) -> (tree, vs, pds_per_vertex, conductance) or
        # (None, None, None, conductance) when the pair has no mesh.
        self._trees: dict[tuple[str, str], tuple] = {}

    def tree(self, phys: str, net: str) -> tuple:
        """``(cKDTree, potentials, power_density_per_vertex, conductance)``
        for one (physical layer, net), or ``(None, None, None, cond)``."""
        key = (phys, net)
        cached = self._trees.get(key)
        if cached is not None:
            return cached
        li = self.index_by_pair.get(key)
        if li is None:
            self._trees[key] = (None, None, None, 0.0)
            return self._trees[key]
        from scipy.spatial import cKDTree

        ls = self.solution.layer_solutions[li]
        layer = self.solution.problem.layers[li]
        xs_parts, ys_parts, vs_parts, pd_parts = [], [], [], []
        for xys, tris_local, pot, pd in zip(
            ls.vertex_xys, ls.triangles, ls.potentials, ls.power_densities,
        ):
            xys = np.asarray(xys)
            tris_local = np.asarray(tris_local)
            pot = np.asarray(pot)
            n = xys.shape[0]
            if n == 0 or tris_local.size == 0:
                continue
            # Power density is stored per face; average onto vertices for
            # the nearest-vertex lookup.
            if pd is not None:
                pd_per_v = face_to_vertex_average(
                    tris_local, np.asarray(pd), n)
            else:
                pd_per_v = np.zeros(n, dtype=np.float64)
            used = np.unique(tris_local.ravel())
            xs_parts.append(xys[used, 0])
            ys_parts.append(xys[used, 1])
            vs_parts.append(pot[used])
            pd_parts.append(pd_per_v[used])
        if not xs_parts:
            self._trees[key] = (None, None, None, layer.conductance)
            return self._trees[key]
        pts = np.column_stack([
            np.concatenate(xs_parts), np.concatenate(ys_parts),
        ])
        self._trees[key] = (
            cKDTree(pts), np.concatenate(vs_parts),
            np.concatenate(pd_parts), layer.conductance,
        )
        return self._trees[key]

    def sample_batch(
        self, requests: dict[tuple[str, str], list[tuple[object, float, float]]],
    ) -> dict[object, tuple[float | None, float | None, float]]:
        """Sample many points at once, bucketed by (phys, net).

        ``requests[(phys, net)] = [(key, x, y), …]``; returns
        ``{key: (voltage, power_density, sheet_conductance)}`` with ``None``
        for off-mesh or non-finite samples. One vectorised tree query per
        bucket.
        """
        out: dict[object, tuple[float | None, float | None, float]] = {}
        for pair, reqs in requests.items():
            tree, vs_arr, pds_arr, cond = self.tree(*pair)
            if tree is None:
                for (k, _x, _y) in reqs:
                    out[k] = (None, None, cond)
                continue
            pts = np.array([(r[1], r[2]) for r in reqs], dtype=np.float64)
            distances, indices = tree.query(pts)
            for i, (k, _x, _y) in enumerate(reqs):
                if distances[i] > MATCH_TOL_MM:
                    out[k] = (None, None, cond)
                    continue
                v = float(vs_arr[indices[i]])
                pd_v = float(pds_arr[indices[i]])
                out[k] = (v if np.isfinite(v) else None,
                          pd_v if np.isfinite(pd_v) else None,
                          cond)
        return out

    def sample_pin(self, pin: dict) -> float | None:
        """Solved voltage at one metadata pin, or ``None`` off-mesh."""
        phys = self.id_to_phys.get(pin.get("layer_id"))
        x, y, net = pin.get("x_mm"), pin.get("y_mm"), pin.get("net", "")
        if phys is None or x is None or y is None or not net:
            return None
        res = self.sample_batch({(phys, net): [(0, float(x), float(y))]})
        return res[0][0]

    def layer_name(self, layer_id: int) -> str:
        """Stackup layer name for an id; ``"L<id>"`` when unknown."""
        for row in self.metadata.get("stackup", []):
            if row.get("layer_id") == layer_id:
                return row.get("name") or f"L{layer_id}"
        return f"L{layer_id}"


def compute_node_rows(sampler: SolutionSampler,
                      rail_to_members: dict[str, list[str]],
                      ) -> list[dict]:
    """One row per directive-terminal pin with V / drop / |J| / P.

    ``drop`` is voltage minus the rail group's highest directive-pin voltage.
    Current density |J| = sqrt(power_density × sheet conductance), in A/mm.
    ``min_voltage`` / ``margin`` / ``status`` come from ``PDN_MIN_V`` and are
    carried on SINK P-terminal pins only, so the margin check never fires on
    a return (N) terminal or on a non-sink.
    """
    metadata = sampler.metadata
    preps: list[dict] = []
    requests: dict[tuple[str, str], list[tuple[object, float, float]]] = {}
    for d in metadata.get("directives", []):
        role = d.get("role", "")
        # An auto-bridge is a synthetic record for the marker overlay, not a
        # terminal pair anyone can inspect: both of its pins report the
        # post-merge net and zero pad area.
        if role == "AUTO_BRIDGE":
            continue
        desig = d.get("designator", "?")
        # ``label`` disambiguates multi-channel SOURCE/SINK pins ("U5" vs
        # "U5#1").
        display_desig = str(d.get("label") or desig)
        directive_min_v = d.get("min_voltage") if role == "SINK" else None
        for term_name, term in (d.get("terminals") or {}).items():
            pin_min_v = directive_min_v if term_name == "P" else None
            for pin in term.get("pins", []):
                layer_id = pin.get("layer_id")
                net = pin.get("net", "")
                x = pin.get("x_mm")
                y = pin.get("y_mm")
                phys = sampler.id_to_phys.get(layer_id)
                prep_idx = len(preps)
                preps.append({
                    "role": role,
                    "designator": display_desig,
                    "schdoc": d.get("schdoc", ""),
                    "terminal": term_name,
                    "pad": pin_display_pad(pin),
                    "net": net,
                    "layer_id": layer_id,
                    "x_mm": x,
                    "y_mm": y,
                    "min_voltage": pin_min_v,
                })
                if phys is not None and x is not None and y is not None:
                    requests.setdefault((phys, net), []).append(
                        (prep_idx, float(x), float(y)))

    samples = sampler.sample_batch(requests)

    rows: list[dict] = []
    for prep_idx, prep in enumerate(preps):
        voltage, pd_val, conductance = samples.get(prep_idx, (None, None, 0.0))
        cd_val = (math.sqrt(max(pd_val * conductance, 0.0))
                  if pd_val is not None else None)
        min_v = prep["min_voltage"]
        # Margin = actual sample - declared minimum; ``None`` whenever either
        # side is missing (no PDN_MIN_V, or the mesh sample failed).
        if min_v is None or voltage is None:
            margin = None
            status = None
        else:
            margin = voltage - min_v
            status = "PASS" if margin >= 0 else "FAIL"
        row = dict(prep)
        row.update({
            "voltage": voltage,
            "power_density": pd_val,
            "current_density": cd_val,
            "margin": margin,
            "status": status,
        })
        rows.append(row)

    # Drop per row: V - max(V on the same rail group).
    net_to_rail = {n: rail for rail, members in rail_to_members.items()
                   for n in members}
    rail_max_v: dict[str, float] = {}
    for r in rows:
        rail = net_to_rail.get(r["net"])
        if rail is None or r["voltage"] is None:
            continue
        if rail not in rail_max_v or r["voltage"] > rail_max_v[rail]:
            rail_max_v[rail] = r["voltage"]
    for r in rows:
        rail = net_to_rail.get(r["net"])
        if rail is None or r["voltage"] is None or rail not in rail_max_v:
            r["drop"] = None
        else:
            r["drop"] = r["voltage"] - rail_max_v[rail]
    return rows


def compute_via_rows(sampler: SolutionSampler,
                     layer_name: Callable[[int], str] | None = None,
                     ) -> list[dict]:
    """One row per via with its worst per-hop current and total power.

    The voltage at the via's (x, y) is sampled on every layer that is an
    endpoint of one of the via's FEM segments — the same Layer instances
    :func:`fypa.altium.loader._coupling_networks` built the via resistors
    between. Per-hop resistance comes from the via's own ``segments`` list,
    so a hop across a thicker dielectric uses the R the FEM solved with.
    """
    metadata = sampler.metadata
    layer_name = layer_name or sampler.layer_name
    # Stackup layer ids in physical order (top → bottom).
    stackup_ids = [row["layer_id"] for row in metadata.get("stackup", [])]
    stackup_idx: dict[int, int] = {lid: i for i, lid in enumerate(stackup_ids)}

    preps: list[dict] = []
    requests: dict[tuple[str, str], list[tuple[object, float, float]]] = {}
    for via in metadata.get("vias", []):
        net = via.get("net", "")
        if not net or net in ("?", "NO_NET", ""):
            continue
        x = via.get("x_mm", 0.0)
        y = via.get("y_mm", 0.0)
        ls_id = via.get("layer_start")
        le_id = via.get("layer_end")
        if ls_id is None or le_id is None:
            continue
        lo, hi = (ls_id, le_id) if ls_id <= le_id else (le_id, ls_id)
        i_start = stackup_idx.get(lo)
        i_end = stackup_idx.get(hi)
        if i_start is None or i_end is None:
            continue
        if len(stackup_ids[i_start:i_end + 1]) < 2:
            continue
        # An EMPTY segments list means _coupling_networks skipped this via
        # (no copper on >=2 layers in its span, so no resistor was inserted
        # in the FEM). A "current" for it would be meaningless — every hop R
        # would be the fallback constant while the voltages came from
        # neighbouring sites, giving ghost currents of thousands of amps.
        site_segments = via.get("segments") or []
        if not site_segments:
            continue
        # Only sample layers that are an endpoint of a real FEM segment.
        # Sampling the via's whole stackup span would invent non-physical
        # hops (e.g. a microvia whose net also has stub copper further down).
        seg_layer_ids: set[int] = set()
        for seg in site_segments:
            seg_layer_ids.add(int(seg["layer_a"]))
            seg_layer_ids.add(int(seg["layer_b"]))
        prep_idx = len(preps)
        preps.append({
            "via": via, "net": net, "x": x, "y": y,
            "segments": site_segments,
        })
        for lid in seg_layer_ids:
            phys = sampler.id_to_phys.get(lid)
            if phys is None:
                continue
            requests.setdefault((phys, net), []).append(
                ((prep_idx, lid), x, y))

    samples = sampler.sample_batch(requests)

    rows: list[dict] = []
    for prep_idx, prep in enumerate(preps):
        seg_currents: list[float] = []
        total_power_W = 0.0
        sampled_v: dict[int, float] = {}
        for seg in prep["segments"]:
            lid_a = int(seg["layer_a"])
            lid_b = int(seg["layer_b"])
            r_hop = float(seg["resistance_ohm"])
            if r_hop <= 0.0:
                continue
            v_a = samples.get((prep_idx, lid_a), (None,))[0]
            v_b = samples.get((prep_idx, lid_b), (None,))[0]
            if v_a is None or v_b is None:
                # Without both real voltages the hop current is meaningless;
                # skip it rather than fall back to a fictitious resistance.
                continue
            i_seg = (v_a - v_b) / r_hop
            seg_currents.append(i_seg)
            total_power_W += i_seg * i_seg * r_hop
            sampled_v[lid_a] = v_a
            sampled_v[lid_b] = v_b
        if not seg_currents:
            continue
        max_abs_I = max(abs(I) for I in seg_currents)
        # Order sampled layers top→bottom so v_top / v_bottom and the span
        # label read in the natural order.
        sampled_sorted = sorted(
            sampled_v.items(), key=lambda kv: stackup_idx.get(kv[0], 1 << 30))
        top_lid, v_top = sampled_sorted[0]
        bot_lid, v_bottom = sampled_sorted[-1]
        via_dict = prep["via"]
        ipc_label = via_dict.get("ipc4761_label", "—") or "—"
        # A · marker on conductively-filled rows flags the parallel-shunt
        # resistance model.
        if via_dict.get("is_conductive_fill"):
            ipc_label = f"{ipc_label} ·"
        rows.append({
            "net": prep["net"],
            "layer_span": f"{layer_name(top_lid)} → {layer_name(bot_lid)}",
            # Sampled-and-coupled layer ids in stackup order (top first).
            "layer_ids": [lid for lid, _v in sampled_sorted],
            "x_mm": prep["x"],
            "y_mm": prep["y"],
            "diameter_mm": via_dict.get("diameter_mm"),
            "hole_diameter_mm": via_dict.get("hole_diameter_mm"),
            "ipc4761_label": ipc_label,
            "fill_material": via_dict.get("fill_material", ""),
            "is_conductive_fill": bool(via_dict.get("is_conductive_fill")),
            "v_top": v_top,
            "v_bottom": v_bottom,
            "delta_v": v_top - v_bottom,
            "current": max_abs_I,
            "power": total_power_W,
        })
    return rows
