"""Vertical node alignment within columns."""

from __future__ import annotations

from collections import defaultdict

from fypa.topology.constants import (
    BODY_PAD,
    GND_NET,
    HEADER_H,
    MARGIN,
    PORT_ROW_H,
    ROW_GAP,
    WIRE_EPS,
)
from fypa.topology.metadata.layout_bridge import is_return_port_row
from fypa.topology.metadata_schema import NodeSpec
from fypa.topology.terminal_roles import is_output_port


def node_height(n_rows: int) -> float:
    return HEADER_H + BODY_PAD + max(n_rows, 1) * PORT_ROW_H + BODY_PAD


def port_layout_rows(port_defs: list[tuple[str, str, int]]) -> tuple[int, dict[int, int]]:
    """Map sort_key to 0-based layout row; returns (n_rows, sort_key -> row)."""
    channel_rows = (
        max(
            (sk for _, _, sk in port_defs if not is_return_port_row(sk)),
            default=-1,
        )
        + 1
    )
    return_ports = sorted(
        ((pname, side, sk) for pname, side, sk in port_defs if is_return_port_row(sk)),
        key=lambda t: t[2],
    )
    row_map: dict[int, int] = {}
    for ret_i, (_, _, sk) in enumerate(return_ports):
        row_map[sk] = channel_rows + ret_i
    n_rows = max(channel_rows + len(return_ports), 1)
    return n_rows, row_map


def _spec_layout_height(spec: NodeSpec) -> float:
    n_layout_rows, _ = port_layout_rows(spec["port_defs"])
    return node_height(n_layout_rows)


def _intervals_overlap(
    y: float,
    height: float,
    occupied: list[tuple[float, float]],
) -> bool:
    y_end = y + height
    for y0, y1 in occupied:
        if y_end + ROW_GAP > y0 and y < y1 + ROW_GAP:
            return True
    return False


def _alloc_free_y(
    occupied: list[tuple[float, float]],
    height: float,
    *,
    preferred: float | None = None,
) -> float:
    if preferred is not None and not _intervals_overlap(preferred, height, occupied):
        return preferred
    y = float(MARGIN)
    for y0, y1 in sorted(occupied):
        if y + height + ROW_GAP <= y0 + WIRE_EPS:
            return y
        y = max(y, y1 + ROW_GAP)
    return y


def _direct_alignment_pairs(
    node_specs: list[NodeSpec],
    columns: dict[str, int],
) -> list[frozenset[str]]:
    """Node pairs on the same net with no other node on that net between them."""
    net_members: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for spec in node_specs:
        nid = spec["node_id"]
        col = columns[nid]
        for resolved in (spec.get("resolved_ports") or {}).values():
            wnet = resolved.wnet
            if not wnet or wnet == GND_NET:
                continue
            net_members[wnet].append((nid, col))

    pairs: set[frozenset[str]] = set()
    for members in net_members.values():
        unique: dict[str, int] = {}
        for nid, col in members:
            unique.setdefault(nid, col)
        by_col = sorted(unique.items(), key=lambda t: t[1])
        for i, (a, ca) in enumerate(by_col):
            for b, cb in by_col[i + 1 :]:
                if ca == cb:
                    continue
                if any(ca < c < cb for _, c in by_col if _ not in (a, b)):
                    continue
                pairs.add(frozenset((a, b)))
    return list(pairs)


def _pick_downstream_align_partner(
    spec: dict,
    candidates: list[str],
    columns: dict[str, int],
    specs_by_id: dict[str, dict],
) -> str | None:
    """Nearest downstream column; tie-break by shared output-net then node id."""
    if not candidates:
        return None

    next_col = min(columns[o] for o in candidates)
    in_next = [o for o in candidates if columns[o] == next_col]
    if len(in_next) == 1:
        return in_next[0]
    role = spec["role"]
    output_nets: set[str] = set()
    for pname, side, _ in spec["port_defs"]:
        if not is_output_port(role, pname, side):
            continue
        resolved = (spec.get("resolved_ports") or {}).get(pname)
        if resolved and resolved.wnet:
            output_nets.add(resolved.wnet)
    if output_nets:
        shared = [
            o
            for o in in_next
            if any(
                (specs_by_id[o].get("resolved_ports") or {}).get(pn)
                and (specs_by_id[o].get("resolved_ports") or {})[pn].wnet in output_nets
                for pn in (specs_by_id[o].get("resolved_ports") or {})
            )
        ]
        if shared:
            return min(shared, key=str)
    return min(in_next, key=str)


def assign_vertical_positions(
    node_specs: list[NodeSpec],
    columns: dict[str, int],
    max_col: int,
) -> dict[str, float]:
    """Place node tops: align to the directly connected downstream neighbour only."""
    heights = {s["node_id"]: _spec_layout_height(s) for s in node_specs}
    specs_by_id = {s["node_id"]: s for s in node_specs}
    pairs = _direct_alignment_pairs(node_specs, columns)
    higher_partners: dict[str, list[str]] = defaultdict(list)
    for pair in pairs:
        a, b = tuple(pair)
        ca, cb = columns[a], columns[b]
        if ca < cb:
            higher_partners[a].append(b)
        elif cb < ca:
            higher_partners[b].append(a)

    occupied: dict[int, list[tuple[float, float]]] = defaultdict(list)
    y_assign: dict[str, float] = {}

    for c in range(max_col, -1, -1):
        pending = [s for s in node_specs if columns[s["node_id"]] == c]
        while pending:
            progressed = False
            for spec in list(pending):
                nid = spec["node_id"]
                nh = heights[nid]
                downstream = [
                    o for o in higher_partners.get(nid, []) if columns[o] > c and o in y_assign
                ]
                if downstream:
                    partner = _pick_downstream_align_partner(
                        spec,
                        downstream,
                        columns,
                        specs_by_id,
                    )

                    if partner is None:
                        continue
                    y = y_assign[partner]
                elif c == max_col:
                    y = _alloc_free_y(occupied[c], nh)
                else:
                    continue
                if _intervals_overlap(y, nh, occupied[c]):
                    y = _alloc_free_y(occupied[c], nh)
                y_assign[nid] = y
                occupied[c].append((y, y + nh))
                pending.remove(spec)
                progressed = True
            if not progressed:
                for spec in pending:
                    nid = spec["node_id"]
                    nh = heights[nid]
                    y = _alloc_free_y(occupied[c], nh)
                    y_assign[nid] = y
                    occupied[c].append((y, y + nh))
                break
    return y_assign
