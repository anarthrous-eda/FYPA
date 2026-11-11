"""Shorted (merged) nets as switchable sections of their surviving net.

The low-Ω merge folds a net into another before the solve (HS_SNS into +12V
through R80, R1, R2), so the FEM has one +12V mesh per layer and no HS_SNS
mesh at all. ``metadata["premerged_copper"]`` keeps the outlines of the copper
each folded-away net used to own (see
:func:`fypa.altium.loader.build_premerged_copper_records`). This module uses
those outlines to:

* list each surviving net's shorted children, so the Rails control can show
  them as rows under it, and
* label every triangle of a merged layer mesh with the original net it lies
  in, so the heatmap can draw only the sections whose eyes are open.

Label 0 is the surviving net's own copper; label ``i + 1`` is
``names[i]``. A triangle is classified by its centroid, so a triangle that
straddles the old boundary goes wholly to one side — the mesh was built on
the merged outline and has no edge there.
"""
from __future__ import annotations

import numpy as np


def _canonical_lookup(metadata: dict | None) -> dict[str, str]:
    """``{ORIGINAL (upper): canonical name}`` for every folded-away net."""
    recs = (metadata or {}).get("premerged_copper") or []
    canon_map = (metadata or {}).get("net_canonical") or {}
    out: dict[str, str] = {}
    # Copper records carry the true canonical name. A whole-layer plane
    # record takes its name from the stackup, which still names the plane by
    # its ORIGINAL net, so it is resolved second, from the other records or
    # the canonical-name map.
    for rec in recs:
        orig = rec.get("net")
        canon = rec.get("canonical")
        if orig and canon and not rec.get("whole_layer") and canon != orig:
            out.setdefault(orig.upper(), canon)
    for rec in recs:
        orig = rec.get("net")
        if not orig or orig.upper() in out:
            continue
        canon = canon_map.get(orig.upper()) or rec.get("canonical")
        if canon and canon.upper() != orig.upper():
            out[orig.upper()] = canon
    return out


def merged_children(
    metadata: dict | None, nets: list[str] | set[str],
) -> dict[str, list[str]]:
    """``{net: [shorted child nets]}`` for each of ``nets`` that absorbed any.

    ``nets`` are the names the viewer uses (the solved layers' net names);
    matching against the records is case-insensitive, and the result is keyed
    by the name as given. Children are original net names, sorted, and never
    one of ``nets`` themselves (a name that survived as its own net is not a
    section of another).
    """
    lookup = _canonical_lookup(metadata)
    if not lookup:
        return {}
    by_upper = {n.upper(): n for n in nets}
    out: dict[str, set[str]] = {}
    orig_names: dict[str, str] = {}
    for rec in (metadata or {}).get("premerged_copper") or []:
        orig = rec.get("net")
        if orig:
            orig_names.setdefault(orig.upper(), orig)
    for orig_u, canon in lookup.items():
        owner = by_upper.get(canon.upper())
        if owner is None or orig_u in by_upper:
            continue
        out.setdefault(owner, set()).add(orig_names.get(orig_u, orig_u))
    return {k: sorted(v) for k, v in out.items()}


def section_records(
    metadata: dict | None, layer_id: int | None, net: str,
) -> tuple[list[str], list[tuple[str, list[dict]]], str | None]:
    """Records of ``net``'s shorted children on one layer.

    Returns ``(names, [(child, polygons)], whole_layer_child)``: the child
    names on this layer (order defines the labels), each child's polygon
    dicts, and the child that floods the whole layer as a plane, if any.
    """
    if layer_id is None:
        return [], [], None
    lookup = _canonical_lookup(metadata)
    want = net.upper()
    polys: dict[str, list[dict]] = {}
    whole: str | None = None
    for rec in (metadata or {}).get("premerged_copper") or []:
        if rec.get("layer_id") != layer_id:
            continue
        orig = rec.get("net")
        if not orig or (lookup.get(orig.upper()) or "").upper() != want:
            continue
        if rec.get("whole_layer"):
            whole = whole or orig
            polys.setdefault(orig, [])
            continue
        polys.setdefault(orig, []).extend(rec.get("polygons") or [])
    names = sorted(polys)
    return names, [(n, polys[n]) for n in names], whole


def label_points(
    xs: np.ndarray, ys: np.ndarray,
    names: list[str],
    child_polys: list[tuple[str, list[dict]]],
    whole_child: str | None,
) -> np.ndarray:
    """Section label (0 = surviving net, ``i + 1`` = ``names[i]``) per point."""
    import shapely
    from shapely import geometry as _sg

    n = int(np.asarray(xs).size)
    default = (names.index(whole_child) + 1) if whole_child in names else 0
    labels = np.full(n, default, dtype=np.int16)
    if n == 0:
        return labels
    shapes: list = []
    owner: list[int] = []
    for name, polys in child_polys:
        lab = names.index(name) + 1
        for poly in polys:
            try:
                shp = _sg.Polygon(poly["exterior"], list(poly.get("holes") or []))
            except Exception:
                continue
            if shp.is_empty:
                continue
            if not shp.is_valid:
                shp = shp.buffer(0)
                if shp.is_empty:
                    continue
            shapes.append(shp)
            owner.append(lab)
    if not shapes:
        return labels
    tree = shapely.STRtree(shapes)
    pts = shapely.points(np.asarray(xs, dtype=np.float64),
                         np.asarray(ys, dtype=np.float64))
    pt_idx, shp_idx = tree.query(pts, predicate="intersects")
    if pt_idx.size:
        # Overlapping outlines: the first (lowest-index) shape wins, which
        # is deterministic because the record order is.
        order = np.lexsort((shp_idx, pt_idx))
        pt_idx = pt_idx[order]
        shp_idx = shp_idx[order]
        first = np.ones(pt_idx.size, dtype=bool)
        first[1:] = pt_idx[1:] != pt_idx[:-1]
        owner_arr = np.asarray(owner, dtype=np.int16)
        labels[pt_idx[first]] = owner_arr[shp_idx[first]]
    return labels


def triangle_labels(
    xs: np.ndarray, ys: np.ndarray, tris: np.ndarray,
    names: list[str],
    child_polys: list[tuple[str, list[dict]]],
    whole_child: str | None,
) -> np.ndarray:
    """Section label per triangle, classified at the centroid."""
    tris = np.asarray(tris)
    if tris.size == 0:
        return np.empty(0, dtype=np.int16)
    cx = np.asarray(xs)[tris].mean(axis=1)
    cy = np.asarray(ys)[tris].mean(axis=1)
    return label_points(cx, cy, names, child_polys, whole_child)


def keep_mask(
    labels: np.ndarray, names: list[str], hidden: frozenset[str],
    own_name: str,
) -> np.ndarray:
    """Boolean per-label-entry: True where the section is not hidden."""
    hidden_u = {h.upper() for h in hidden}
    table = np.array(
        [own_name.upper() not in hidden_u]
        + [n.upper() not in hidden_u for n in names],
        dtype=bool,
    )
    return table[labels]
