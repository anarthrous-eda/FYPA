"""Facade between topology metadata and node layout."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from fypa.topology.constants import GND_NET, RETURN_PORT_SORT_BASE
from fypa.topology.metadata.nets import (
    canonical_net,
    is_ideal_return,
    net_to_rail_map,
    port_display_net,
    terminal_net,
    wire_net,
)
from fypa.topology.metadata.specs import (
    directives_to_component_specs,
    driven_power_nets,
    jump_row_for_directive,
    natural_sort_key,
    spec_has_series_role,
    spec_port_role,
)
from fypa.topology.metadata.tooltips import port_tooltip
from fypa.topology.metadata_schema import NodeSpec, TerminalDict, TopologyMetadata
from fypa.topology.terminal_roles import is_output_port, is_power_input_port
from fypa.topology.util import truncate_label


@dataclass(frozen=True)
class ResolvedPort:
    wnet: str
    plabel: str
    tooltip: str


@dataclass(frozen=True)
class ParsedLayoutInput:
    node_specs: list[NodeSpec]
    net_to_rail: dict[str, str]
    driven_nets: set[str]
    needs_gnd: bool
    columns: dict[str, int]


def _column_flow_net(term: TerminalDict | None) -> str | None:
    """Physical net for column placement (GND collapsed; no rail-group merge).

    Rail merging is for the solver/viewer dropdown — using it here creates
    feedback cycles when SERIES bridges join upstream and downstream nets
    (e.g. VDD_3V3 ↔ VDD_IMU) onto one canonical name.
    """
    if not term or is_ideal_return(term):
        return None
    return wire_net(terminal_net(term))


def _compact_columns(col: dict[str, int]) -> dict[str, int]:
    """Remap sparse column indices to a dense 0..n-1 range (no empty columns)."""
    if not col:
        return col
    order = sorted(set(col.values()))
    remap = {old: new for new, old in enumerate(order)}
    return {nid: remap[c] for nid, c in col.items()}


def _has_source_rail_p_input(
    spec: NodeSpec,
    source_ids: set[str],
    outputs_by_net: dict[str, list[str]],
) -> bool:
    """True when a P port is fed directly from a SOURCE output."""
    for pname, term in (spec.get("terms") or {}).items():
        if not pname.startswith("P") or not term or is_ideal_return(term):
            continue
        flow_net = _column_flow_net(term)
        if not flow_net:
            continue
        for pid in outputs_by_net.get(flow_net, []):
            if pid in source_ids:
                return True
    return False


def _resolve_mutual_loop_parents(
    loop_parent: dict[str, str],
    node_specs: list[NodeSpec],
    source_ids: set[str],
    outputs_by_net: dict[str, list[str]],
) -> dict[str, str]:
    """Break A↔B mutual loops: keep the source-rail bridge as parent."""
    spec_by_id = {s["node_id"]: s for s in node_specs}
    resolved = dict(loop_parent)
    for child, parent in list(loop_parent.items()):
        if resolved.get(parent) != child:
            continue
        child_spec = spec_by_id[child]
        parent_spec = spec_by_id[parent]
        child_src = _has_source_rail_p_input(child_spec, source_ids, outputs_by_net)
        parent_src = _has_source_rail_p_input(parent_spec, source_ids, outputs_by_net)
        # Pick the loop root (the node that keeps no parent link) symmetrically,
        # so both (child, parent) and (parent, child) iterations agree on it: the
        # source-rail bridge wins, else the lexicographically smaller node id as
        # an arbitrary-but-deterministic tie-break. ``pop`` is idempotent, so the
        # second direction of the mutual pair is a harmless no-op rather than a
        # KeyError on an already-removed key.
        if child_src and not parent_src:
            root = child
        elif parent_src and not child_src:
            root = parent
        else:
            root = min(child, parent)
        resolved.pop(root, None)
    return resolved


def _detect_loop_series_parents(
    node_specs: list[NodeSpec],
    outputs_by_net: dict[str, list[str]],
    inputs_by_net: dict[str, list[str]],
) -> dict[str, str]:
    """Map loop-child SERIES/RESISTOR node_id -> parent node_id.

    A loop child receives on P ports from the parent's N outputs and drives
    back into the parent's P inputs (e.g. J7 relative to U1).
    """
    loop_parent: dict[str, str] = {}
    spec_by_id = {s["node_id"]: s for s in node_specs}
    for s in node_specs:
        if not spec_has_series_role(s):
            continue
        child_id = s["node_id"]
        for parent_id, parent in spec_by_id.items():
            if parent_id == child_id:
                continue
            from_parent = False
            to_parent = False
            for pname, term in (s["terms"] or {}).items():
                if not term or is_ideal_return(term):
                    continue
                flow_net = _column_flow_net(term)
                if not flow_net:
                    continue
                if pname.startswith("P"):
                    if parent_id in outputs_by_net.get(flow_net, []):
                        from_parent = True
                elif pname.startswith("N"):
                    if parent_id in inputs_by_net.get(flow_net, []):
                        to_parent = True
            if from_parent and to_parent:
                loop_parent[child_id] = parent_id
                break
    source_ids = {s["node_id"] for s in node_specs if s["role"] in ("SOURCE", "REGULATOR")}
    return _resolve_mutual_loop_parents(loop_parent, node_specs, source_ids, outputs_by_net)


def _passive_upstream_cols(
    spec: NodeSpec,
    nid: str,
    outputs_by_net: dict[str, list[str]],
    col: dict[str, int],
    loop_parent: dict[str, str],
) -> list[int]:
    """Column indices of nodes driving this passive's P-side inputs (excl. loop-back)."""
    upstream: list[int] = []
    for pname, term in (spec.get("terms") or {}).items():
        if not term or is_ideal_return(term) or not pname.startswith("P"):
            continue
        flow_net = _column_flow_net(term)
        if not flow_net:
            continue
        for pid in outputs_by_net.get(flow_net, []):
            if pid == nid:
                continue
            if loop_parent.get(pid) == nid:
                continue
            upstream.append(col.get(pid, 0))
    return upstream


def _passive_downstream_cols(
    spec: NodeSpec,
    nid: str,
    inputs_by_net: dict[str, list[str]],
    col: dict[str, int],
) -> list[int]:
    """Column indices of nodes fed from this passive's N-side outputs."""
    downstream: list[int] = []
    for pname, term in (spec.get("terms") or {}).items():
        if not term or is_ideal_return(term) or not pname.startswith("N"):
            continue
        flow_net = _column_flow_net(term)
        if not flow_net:
            continue
        for pid in inputs_by_net.get(flow_net, []):
            if pid != nid:
                downstream.append(col.get(pid, 0))
    return downstream


def _apply_passive_column_col(
    spec: NodeSpec,
    nid: str,
    col: dict[str, int],
    outputs_by_net: dict[str, list[str]],
    inputs_by_net: dict[str, list[str]],
    loop_parent: dict[str, str],
) -> None:
    """Place an inline passive from upstream/downstream peers (not loop children)."""
    if nid in loop_parent:
        return
    upstream_cols = _passive_upstream_cols(spec, nid, outputs_by_net, col, loop_parent)
    if upstream_cols:
        col[nid] = max(max(upstream_cols) + 1, col.get(nid, 0))
        return
    downstream_cols = _passive_downstream_cols(spec, nid, inputs_by_net, col)
    if downstream_cols:
        col[nid] = max(min(downstream_cols), col.get(nid, 0))


def _dedupe_port_rows_on_same_side(
    port_defs: list[tuple[str, str, int]],
) -> list[tuple[str, str, int]]:
    """Give each port on a symbol edge its own layout row (no overlapping circles)."""
    by_side: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
    for item in port_defs:
        by_side[item[1]].append(item)
    out: list[tuple[str, str, int]] = []
    for side in ("left", "right"):
        for row_i, (pname, s, _sk) in enumerate(
            sorted(by_side.get(side, []), key=lambda t: (t[2], t[0]))
        ):
            out.append((pname, s, row_i))
    return out


def _child_facing_net_rows(spec: NodeSpec, face_side: str) -> dict[str, int]:
    """Layout row per flow net for channel ports on the parent-facing edge."""
    terms = spec.get("terms") or {}
    net_rows: dict[str, int] = {}
    face_channel = [
        (pname, sort_key)
        for pname, side, sort_key in spec["port_defs"]
        if side == face_side and pname.startswith(("P", "N"))
    ]
    face_channel.sort(key=lambda t: (t[1], t[0]))
    for row_i, (pname, _) in enumerate(face_channel):
        net = _column_flow_net(terms.get(pname))
        if net and net not in net_rows:
            net_rows[net] = row_i
    return net_rows


def _assign_face_port_rows(
    port_defs: list[tuple[str, str, int]],
    terms: dict[str, TerminalDict],
    face_side: str,
    net_row_hints: dict[str, int],
) -> list[tuple[str, str, int]]:
    """Unique rows on ``face_side``; loop nets share the child's row index."""
    out: list[tuple[str, str, int]] = []
    face_ports: list[tuple[str, str, int]] = []
    for item in port_defs:
        if item[1] == face_side:
            face_ports.append(item)
        else:
            out.append(item)

    assigned: dict[str, int] = {}
    used_rows: set[int] = set()
    hinted: list[tuple[str, str, int, str]] = []
    pending: list[tuple[str, str, int]] = []
    for pname, side, sort_key in face_ports:
        net = _column_flow_net(terms.get(pname))
        if net and net in net_row_hints:
            hinted.append((pname, side, sort_key, net))
        else:
            pending.append((pname, side, sort_key))

    for pname, _side, _sk, net in sorted(hinted, key=lambda t: (net_row_hints[t[3]], t[0])):
        row = net_row_hints[net]
        while row in used_rows:
            row += 1
        assigned[pname] = row
        used_rows.add(row)

    next_row = 0
    for pname, _side, _sk in sorted(pending, key=lambda t: (t[2], t[0])):
        while next_row in used_rows:
            next_row += 1
        assigned[pname] = next_row
        used_rows.add(next_row)
        next_row += 1

    for pname, side, _sk in face_ports:
        out.append((pname, side, assigned[pname]))
    return out


def _orient_loop_series_ports(
    node_specs: list[NodeSpec],
    col: dict[str, int],
    loop_parent: dict[str, str],
    outputs_by_net: dict[str, list[str]],
    inputs_by_net: dict[str, list[str]],
) -> None:
    """Loop child: all channel ports on the parent-facing side (one row each).

    Loop parent: N/P ports on nets shared with the child face the child column.
    """
    spec_by_id = {s["node_id"]: s for s in node_specs}
    loop_children: dict[str, list[str]] = defaultdict(list)
    for child_id, parent_id in loop_parent.items():
        loop_children[parent_id].append(child_id)

    for s in node_specs:
        if not spec_has_series_role(s):
            continue
        nid = s["node_id"]
        if nid not in loop_parent:
            continue
        parent_col = col.get(loop_parent[nid], 0)
        child_col = col.get(nid, 0)
        if parent_col < child_col:
            face = "left"
        elif parent_col > child_col:
            face = "right"
        else:
            face = "left"
        channel_ports = [
            (pname, side, sort_key)
            for pname, side, sort_key in s["port_defs"]
            if pname.startswith(("P", "N"))
        ]
        other_ports = [
            (pname, side, sort_key)
            for pname, side, sort_key in s["port_defs"]
            if not pname.startswith(("P", "N"))
        ]
        channel_ports.sort(key=lambda t: (t[2], t[0]))
        s["port_defs"] = [
            (pname, face, row_i) for row_i, (pname, _side, _sk) in enumerate(channel_ports)
        ] + other_ports

    for s in node_specs:
        if not spec_has_series_role(s):
            continue
        nid = s["node_id"]
        children = loop_children.get(nid)
        if not children:
            continue
        child_set = set(children)
        parent_col = col.get(nid, 0)
        child_col = min(col.get(c, parent_col) for c in children)
        if child_col > parent_col:
            face_child = "right"
        elif child_col < parent_col:
            face_child = "left"
        else:
            continue
        terms = s.get("terms") or {}
        flip_p: set[str] = set()
        flip_n: set[str] = set()
        for pname, term in terms.items():
            if not term or is_ideal_return(term):
                continue
            flow_net = _column_flow_net(term)
            if not flow_net:
                continue
            if pname.startswith("N"):
                if any(c in inputs_by_net.get(flow_net, []) for c in child_set):
                    flip_n.add(pname)
            elif pname.startswith("P"):
                if any(c in outputs_by_net.get(flow_net, []) for c in child_set):
                    flip_p.add(pname)
        if not flip_p and not flip_n:
            continue
        flipped = [
            (
                pname,
                face_child if pname in flip_p or pname in flip_n else side,
                sort_key,
            )
            for pname, side, sort_key in s["port_defs"]
        ]
        child_net_rows: dict[str, int] = {}
        for child_id in children:
            child_spec = spec_by_id[child_id]
            child_col = col.get(child_id, parent_col)
            if child_col > parent_col:
                child_face = "left"
            elif child_col < parent_col:
                child_face = "right"
            else:
                continue
            child_net_rows.update(_child_facing_net_rows(child_spec, child_face))
        if child_net_rows:
            s["port_defs"] = _assign_face_port_rows(
                flipped,
                terms,
                face_child,
                child_net_rows,
            )
        else:
            s["port_defs"] = _dedupe_port_rows_on_same_side(flipped)


def _column_net(
    role: str,
    term: TerminalDict | None,
    net_to_rail: dict[str, str],
    *,
    terminal: str = "",
) -> str | None:
    """Net key for the column-placement graph.

    SERIES / RESISTOR and load power inputs use physical wire names so
    bridged downstream nets (VDD_MCU, LED_R, …) do not collapse onto the
    upstream rail.  Other roles keep rail-canonical names so parallel loads
    on a shared rail stay aligned.
    """
    if not term or is_ideal_return(term):
        return None
    if role in ("RESISTOR", "SERIES"):
        return _column_flow_net(term)
    if is_power_input_port(role, terminal):
        return _column_flow_net(term)
    return canonical_net(terminal_net(term), net_to_rail)


def _push_passive_load_columns(
    node_specs: list[NodeSpec],
    col: dict[str, int],
    inputs_by_net: dict[str, list[str]],
    loop_parent: dict[str, str],
    role_by_id: dict[str, str],
    outputs_by_net: dict[str, list[str]],
) -> None:
    """Loads on a single-channel passive's N net sit one column right of the bridge."""
    for s in node_specs:
        if not spec_has_series_role(s):
            continue
        nid = s["node_id"]
        if nid in loop_parent:
            continue
        if not _passive_upstream_cols(s, nid, outputs_by_net, col, loop_parent):
            continue
        n_terms = [
            pname
            for pname, term in (s.get("terms") or {}).items()
            if pname.startswith("N") and term and not is_ideal_return(term)
        ]
        if len(n_terms) != 1:
            continue
        pcol = col.get(nid, 0)
        flow_net = _column_flow_net((s.get("terms") or {}).get(n_terms[0]))
        if not flow_net:
            continue
        for load_id in inputs_by_net.get(flow_net, []):
            if load_id == nid:
                continue
            if role_by_id.get(load_id) != "SINK":
                continue
            col[load_id] = max(col.get(load_id, 0), pcol + 1)


def _propagation_edges(
    node_specs: list[NodeSpec],
    outputs_by_net: dict[str, list[str]],
    inputs_by_net: dict[str, list[str]],
    net_to_rail: dict[str, str],
    loop_parent: dict[str, str],
) -> dict[str, list[str]]:
    """Directed edges ``nid -> other`` walked by the column-relaxation passes.

    Mirrors the edge traversal in :func:`assign_columns` exactly (output ports,
    flow-net resolution, GND/self/loop-parent skips) so cycle detection sees the
    same graph those passes walk.
    """
    edges: dict[str, list[str]] = defaultdict(list)
    for s in node_specs:
        nid = s["node_id"]
        for pname, side, _ in s["port_defs"]:
            port_role = spec_port_role(s, pname)
            if not is_output_port(port_role, pname, side):
                continue
            term = (s["terms"] or {}).get(pname)
            if is_ideal_return(term):
                continue
            flow_net = _column_net(port_role, term, net_to_rail, terminal=pname)
            if not flow_net or flow_net == GND_NET:
                continue
            for other in inputs_by_net.get(flow_net, []):
                if other == nid or (nid in loop_parent and other == loop_parent[nid]):
                    continue
                edges[nid].append(other)
    return edges


def _detect_propagation_back_edges(
    edges: dict[str, list[str]],
    root_order: list[str],
) -> set[tuple[str, str]]:
    """DFS back-edges whose removal makes the propagation graph acyclic.

    Passive SERIES/RESISTOR loops are already broken via ``loop_parent``, but a
    non-passive cycle — e.g. two REGULATORs feeding each other on an ORing
    power-path — has no such handling: the relaxation loops would ping-pong,
    bumping each other's column until the iteration guard trips, so the final
    order depended on the guard count rather than topology. Breaking these edges
    turns the graph into a DAG and makes the longest-path relaxation converge to
    a stable order.

    ``root_order`` seeds the DFS: the column-0 sources come first so exploration
    runs *downstream* from them and the edge closing a cycle back toward a source
    is the one classified as the back-edge (the semantically correct one to drop).
    An unanchored mutual loop falls back to node order as a deterministic
    tie-break.
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {}
    back: set[tuple[str, str]] = set()
    for root in root_order:
        if color.get(root, WHITE) != WHITE:
            continue
        color[root] = GRAY
        stack: list[tuple[str, int]] = [(root, 0)]
        while stack:
            node, i = stack[-1]
            neighbors = edges.get(node, ())
            if i < len(neighbors):
                stack[-1] = (node, i + 1)
                nxt = neighbors[i]
                c = color.get(nxt, WHITE)
                if c == WHITE:
                    color[nxt] = GRAY
                    stack.append((nxt, 0))
                elif c == GRAY:
                    back.add((node, nxt))
            else:
                color[node] = BLACK
                stack.pop()
    return back


def assign_columns(
    node_specs: list[NodeSpec],
    net_to_rail: dict[str, str],
) -> dict[str, int]:
    """Place nodes in columns by propagating from SOURCE outputs along nets."""
    col: dict[str, int] = {}
    role_by_id = {s["node_id"]: s["role"] for s in node_specs}

    sources = [s for s in node_specs if s["role"] in ("SOURCE",)]
    for s in node_specs:
        if s["role"] == "REGULATOR" and not sources:
            sources.append(s)
    if not sources:
        sources = node_specs[:1] if node_specs else []

    for s in sources:
        col[s["node_id"]] = 0

    outputs_by_net: dict[str, list[str]] = defaultdict(list)
    inputs_by_net: dict[str, list[str]] = defaultdict(list)
    inputs_by_canonical: dict[str, list[str]] = defaultdict(list)
    for s in node_specs:
        nid = s["node_id"]
        for pname, side, _ in s["port_defs"]:
            term = (s["terms"] or {}).get(pname)
            if is_ideal_return(term):
                continue
            port_role = spec_port_role(s, pname)
            flow_net = _column_net(port_role, term, net_to_rail, terminal=pname)
            if not flow_net or flow_net == GND_NET:
                continue
            if is_output_port(port_role, pname, side):
                outputs_by_net[flow_net].append(nid)
            else:
                inputs_by_net[flow_net].append(nid)
            if not is_output_port(port_role, pname, side):
                cn = canonical_net(terminal_net(term), net_to_rail)
                if cn and cn != GND_NET:
                    inputs_by_canonical[cn].append(nid)

    loop_parent = _detect_loop_series_parents(node_specs, outputs_by_net, inputs_by_net)
    back_edges = _detect_propagation_back_edges(
        _propagation_edges(node_specs, outputs_by_net, inputs_by_net, net_to_rail, loop_parent),
        [s["node_id"] for s in sources] + [s["node_id"] for s in node_specs],
    )

    changed = True
    guard = 0
    while changed and guard < len(node_specs) + 5:
        guard += 1
        changed = False
        for s in node_specs:
            nid = s["node_id"]
            base = col.get(nid, 0)
            for pname, side, _ in s["port_defs"]:
                port_role = spec_port_role(s, pname)
                if not is_output_port(port_role, pname, side):
                    continue
                term = (s["terms"] or {}).get(pname)
                if is_ideal_return(term):
                    continue
                flow_net = _column_net(port_role, term, net_to_rail, terminal=pname)
                if not flow_net or flow_net == GND_NET:
                    continue
                for other in inputs_by_net.get(flow_net, []):
                    if other == nid:
                        continue
                    if nid in loop_parent and other == loop_parent[nid]:
                        continue
                    if (nid, other) in back_edges:
                        continue
                    new_c = base + 1
                    if new_c > col.get(other, -1):
                        col[other] = new_c
                        changed = True

    for s in node_specs:
        nid = s["node_id"]
        if nid not in col:
            col[nid] = max(col.values(), default=0) + 1

    for child_id, parent_id in loop_parent.items():
        col[child_id] = col.get(parent_id, 0) + 1

    for s in node_specs:
        if not spec_has_series_role(s):
            continue
        nid = s["node_id"]
        if nid in loop_parent:
            continue
        upstream_cols = _passive_upstream_cols(s, nid, outputs_by_net, col, loop_parent)
        if upstream_cols:
            col[nid] = min(col.get(nid, 0), max(upstream_cols) + 1)

    for s in node_specs:
        if not spec_has_series_role(s):
            continue
        _apply_passive_column_col(
            s,
            s["node_id"],
            col,
            outputs_by_net,
            inputs_by_net,
            loop_parent,
        )

    # Parallel taps on the P-side rail sit to the right of the bridge (not
    # downstream loads on the N-side nets).
    for s in node_specs:
        if not spec_has_series_role(s):
            continue
        nid = s["node_id"]
        rcol = col.get(nid, 0)
        downstream: set[str] = set()
        for pname, term in (s["terms"] or {}).items():
            if not term or is_ideal_return(term) or not pname.startswith("N"):
                continue
            n_net = _column_flow_net(term)
            if not n_net:
                continue
            for other in inputs_by_net.get(n_net, []):
                if other != nid:
                    downstream.add(other)
        for pname, term in (s["terms"] or {}).items():
            if not term or is_ideal_return(term):
                continue
            if not pname.startswith("P"):
                continue
            rail = canonical_net(terminal_net(term), net_to_rail)
            if not rail or rail == GND_NET:
                continue
            for other in inputs_by_canonical.get(rail, []):
                if other == nid or other in downstream:
                    continue
                if role_by_id.get(other) in ("RESISTOR", "SERIES"):
                    continue
                if col.get(other, 0) <= rcol:
                    col[other] = max(col[other], rcol + 1)

    changed = True
    guard = 0
    while changed and guard < len(node_specs) + 5:
        guard += 1
        changed = False
        for s in node_specs:
            if s["role"] in ("RESISTOR", "SERIES"):
                continue
            nid = s["node_id"]
            base = col.get(nid, 0)
            for pname, side, _ in s["port_defs"]:
                port_role = spec_port_role(s, pname)
                if not is_output_port(port_role, pname, side):
                    continue
                term = (s["terms"] or {}).get(pname)
                if is_ideal_return(term):
                    continue
                flow_net = _column_net(port_role, term, net_to_rail, terminal=pname)
                if not flow_net or flow_net == GND_NET:
                    continue
                for other in inputs_by_net.get(flow_net, []):
                    if other == nid:
                        continue
                    if nid in loop_parent and other == loop_parent[nid]:
                        continue
                    if (nid, other) in back_edges:
                        continue
                    new_c = base + 1
                    if new_c > col.get(other, -1):
                        col[other] = new_c
                        changed = True

    for child_id, parent_id in loop_parent.items():
        col[child_id] = col.get(parent_id, 0) + 1

    for s in node_specs:
        if not spec_has_series_role(s):
            continue
        _apply_passive_column_col(
            s,
            s["node_id"],
            col,
            outputs_by_net,
            inputs_by_net,
            loop_parent,
        )

    for child_id, parent_id in loop_parent.items():
        col[child_id] = col.get(parent_id, 0) + 1

    _push_passive_load_columns(
        node_specs, col, inputs_by_net, loop_parent, role_by_id, outputs_by_net
    )

    if col:
        sink_col = max(col.values())
        for s in node_specs:
            if s["role"] == "SINK":
                col[s["node_id"]] = sink_col

    # Orient each SERIES/RESISTOR so the terminal carrying the downstream loads
    # faces right. Peers are keyed by *resolved physical net* (not the canonical
    # rail — 0-Ω bridges merge a resistor's two nets onto one rail, which would
    # make both terminals look identical). Flip P→right / N→left only when P has
    # downstream nodes and NO upstream driver (a mid-rail tap keeps its driver on
    # the P side, so the default P-left is correct and must stay).
    wnet_cols: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for s in node_specs:
        for rp in (s.get("resolved_ports") or {}).values():
            if rp.wnet and rp.wnet != GND_NET:
                wnet_cols[rp.wnet].append((s["node_id"], col.get(s["node_id"], 0)))

    _orient_loop_series_ports(node_specs, col, loop_parent, outputs_by_net, inputs_by_net)

    for s in node_specs:
        if not spec_has_series_role(s):
            continue
        nid = s["node_id"]
        if nid in loop_parent:
            continue
        rcol = col.get(nid, 0)
        rports = s.get("resolved_ports") or {}

        def _cols(prefix):
            return [
                c
                for pname, rp in rports.items()
                if pname.startswith(prefix)
                for oid, c in wnet_cols.get(rp.wnet, [])
                if oid != nid
            ]

        p_cols, n_cols = _cols("P"), _cols("N")
        p_up, p_down = any(c < rcol for c in p_cols), any(c > rcol for c in p_cols)
        n_down = any(c > rcol for c in n_cols)
        if p_down and not p_up and not n_down:
            s["port_defs"] = [
                (
                    pname,
                    "right" if pname.startswith("P") else "left" if pname.startswith("N") else side,
                    sort_key,
                )
                for pname, side, sort_key in s["port_defs"]
            ]

    return _compact_columns(col)


def specs_by_column(
    node_specs: list[NodeSpec],
    columns: dict[str, int],
) -> tuple[dict[int, list[NodeSpec]], int]:
    """Group component specs by column index (insertion order within each column)."""
    by_col: dict[int, list[NodeSpec]] = defaultdict(list)
    for spec in node_specs:
        by_col[columns.get(spec["node_id"], 0)].append(spec)
    max_col = max(by_col.keys(), default=0)
    return by_col, max_col


def _enrich_resolved_ports(spec: NodeSpec, net_to_rail: dict[str, str]) -> None:
    resolved: dict[str, ResolvedPort] = {}
    port_directives = spec.get("port_directives") or {}
    terms = spec.get("terms") or {}
    for pname, _, _ in spec["port_defs"]:
        term = terms.get(pname)
        raw = terminal_net(term)
        cnet = canonical_net(raw, net_to_rail) or "?"
        wnet = wire_net(raw)
        if not wnet:
            continue
        plabel = truncate_label(port_display_net(term, cnet))
        resolved[pname] = ResolvedPort(
            wnet=wnet,
            plabel=plabel,
            tooltip=port_tooltip(plabel, port_directives.get(pname), pname),
        )
    spec["resolved_ports"] = resolved


def parse_topology_directives(metadata: TopologyMetadata) -> ParsedLayoutInput:
    """Parse metadata into layout-ready component specs and rail maps."""
    # Deferred: rail_groups imports topology.constants; eager import here
    # would cycle with metadata/__init__ → layout_bridge during package init.
    from fypa.rail_groups import compute_rail_groups

    _, rail_to_members = compute_rail_groups(metadata)
    net_to_rail = net_to_rail_map(rail_to_members)
    errors = list(metadata.get("annotation_errors") or [])
    directives = sorted(
        metadata.get("directives") or [],
        key=lambda d: natural_sort_key(str(d.get("designator") or d.get("label", ""))),
    )
    node_specs = directives_to_component_specs(directives, errors, net_to_rail)
    needs_gnd = False
    for spec in node_specs:
        _enrich_resolved_ports(spec, net_to_rail)
        for pname, _, _ in spec["port_defs"]:
            term = (spec["terms"] or {}).get(pname)
            if canonical_net(terminal_net(term), net_to_rail) == GND_NET:
                needs_gnd = True
    columns = assign_columns(node_specs, net_to_rail)
    return ParsedLayoutInput(
        node_specs=node_specs,
        net_to_rail=net_to_rail,
        driven_nets=driven_power_nets(node_specs, net_to_rail),
        needs_gnd=needs_gnd,
        columns=columns,
    )


def is_return_port_row(sort_key: int) -> bool:
    return sort_key >= RETURN_PORT_SORT_BASE


__all__ = [
    "ParsedLayoutInput",
    "ResolvedPort",
    "assign_columns",
    "is_return_port_row",
    "jump_row_for_directive",
    "parse_topology_directives",
    "specs_by_column",
]
