"""Rail grouping for PDN net names — shared by the viewer and topology schematic."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from fypa.topology.net_aliases import is_gnd_alias
from fypa.topology.metadata_schema import TopologyMetadata


@dataclass(frozen=True)
class RailTreeNode:
    """One net in a rail's SERIES spanning tree (root = primary)."""

    name: str
    children: tuple[RailTreeNode, ...] = ()


def compute_rail_groups(
    metadata: TopologyMetadata | None,
) -> tuple[list[str], dict[str, list[str]]]:
    """Group nets into rails based on RESISTOR bridges.

    Walks the metadata's directive list:

    * **RESISTOR** directives bridge their two terminal nets → union them.
    * **SOURCE / SINK / REGULATOR** directives mark their terminal's
      *named* net (the ``PDN_*_NET`` value) as a "primary candidate" —
      any group containing a primary is a rail worth showing in the
      dropdown; groups that don't (signal nets, unused bridges) are
      dropped.

    The group's **display name** is a primary in it — i.e. a net a
    directive explicitly named, never a net that was only pulled into
    the group by a SERIES bridge. So a sink whose ``PDN_N_NET = GND``
    resolved (via the bridge) onto ``+DM_SW1`` still gives a rail named
    ``GND``, not ``+DM_SW1``. Returns
    ``(rail_names_sorted, {primary_name: [all member nets]})``.
    """
    if metadata is None:
        return [], {}

    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]  # path compression
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    primary_candidates: set[str] = set()
    bridge_named: set[str] = set()
    source_rails: set[str] = set()
    regulator_in_rails: set[str] = set()
    regulator_out_rails: set[str] = set()
    canon_map: dict[str, str] = metadata.get("net_canonical") or {}

    def _canonical(net: str) -> str:
        if not net:
            return net
        return canon_map.get(net.upper(), net)

    def _note_rail(net: str) -> str:
        return _canonical(net)

    def _add_primary(net: str) -> None:
        if net:
            primary_candidates.add(_note_rail(net))

    for d in metadata.get("directives", []):
        role = d.get("role", "")
        terms = d.get("terminals") or {}
        nets_per_term: list[set[str]] = []
        for tname, t in terms.items():
            nets = {p.get("net") for p in t.get("pins", []) if p.get("net")}
            req = t.get("requested_net")
            for n in nets:
                find(n)  # ensure presence in union-find
            if nets:
                nets_per_term.append(nets)
            if req:
                find(req)
                for n in nets:
                    union(req, n)
            if role in ("SOURCE", "SINK", "REGULATOR"):
                if t.get("resolved_via_local") and nets:
                    for n in nets:
                        _add_primary(n)
                elif req:
                    canon_req = _note_rail(req)
                    _add_primary(req)
                    if nets and req not in nets:
                        bridge_named.add(canon_req)
                elif nets:
                    for n in nets:
                        _add_primary(n)
            if role == "SOURCE" and tname == "P":
                if t.get("resolved_via_local") and nets:
                    source_rails.update(_note_rail(n) for n in nets)
                elif req:
                    source_rails.add(_note_rail(req))
                elif nets:
                    source_rails.update(_note_rail(n) for n in nets)
            if role == "REGULATOR" and tname == "IN_P":
                if t.get("resolved_via_local") and nets:
                    regulator_in_rails.update(_note_rail(n) for n in nets)
                elif req:
                    regulator_in_rails.add(_note_rail(req))
                elif nets:
                    regulator_in_rails.update(_note_rail(n) for n in nets)
            if role == "REGULATOR" and tname == "OUT_P":
                if t.get("resolved_via_local") and nets:
                    regulator_out_rails.update(_note_rail(n) for n in nets)
                elif req:
                    regulator_out_rails.add(_note_rail(req))
                elif nets:
                    regulator_out_rails.update(_note_rail(n) for n in nets)
        if role == "RESISTOR" and len(nets_per_term) == 2:
            for a in nets_per_term[0]:
                for b in nets_per_term[1]:
                    union(a, b)

    groups: dict[str, set[str]] = {}
    for net in list(parent.keys()):
        groups.setdefault(find(net), set()).add(net)

    rail_to_members: dict[str, list[str]] = {}
    for _root, members in groups.items():
        primaries = members & primary_candidates
        if not primaries:
            continue
        canon_primaries = {_canonical(p) for p in primaries}

        def _primary_sort_key(n: str) -> tuple[int, str]:
            if n in source_rails:
                return (0, n)
            if n in bridge_named:
                return (1, n)
            if n in regulator_in_rails:
                return (2, n)
            if n in regulator_out_rails:
                return (3, n)
            if n.startswith("+"):
                return (4, n)
            u = n.upper()
            if u.startswith(("VDD", "VCC", "VPWR")):
                return (5, n)
            if is_gnd_alias(n):
                return (7, n)
            return (6, n)

        primary = sorted(canon_primaries, key=_primary_sort_key)[0]
        rail_to_members[primary] = sorted(members)

    def _rail_sort_key(rail: str) -> tuple[int, str]:
        if is_gnd_alias(rail):
            return (2, rail)
        if rail.startswith("+"):
            return (0, rail)
        return (1, rail)

    rail_names = sorted(rail_to_members.keys(), key=_rail_sort_key)
    return rail_names, rail_to_members


TREE_DIRECTIVE_ROLES = frozenset({
    "RESISTOR", "SOURCE", "SINK", "REGULATOR",
})


def filter_directives_for_rail_trees(
    directives: list[dict] | None,
) -> list[dict]:
    """Keep directives that contribute SERIES or alias edges for rail trees."""
    return [
        d for d in (directives or [])
        if d.get("role") in TREE_DIRECTIVE_ROLES
    ]


def resistor_bridge_pairs(directive: dict) -> list[frozenset[str]]:
    """All undirected pin-net pairs from a RESISTOR's two terminals."""
    if directive.get("role") != "RESISTOR":
        return []
    terms = directive.get("terminals") or {}
    nets_per_term: list[set[str]] = []
    for t in terms.values():
        nets = {p.get("net") for p in t.get("pins", []) if p.get("net")}
        if nets:
            nets_per_term.append(nets)
    if len(nets_per_term) != 2:
        return []
    pairs: list[frozenset[str]] = []
    for a in nets_per_term[0]:
        for b in nets_per_term[1]:
            if a and b and a != b:
                pairs.append(frozenset((a, b)))
    return pairs


def _add_undirected_edge(adj: dict[str, set[str]], a: str, b: str) -> None:
    if not a or not b or a == b:
        return
    adj.setdefault(a, set()).add(b)
    adj.setdefault(b, set()).add(a)


def _rail_tree_adjacency(
    metadata: TopologyMetadata | None,
) -> dict[str, set[str]]:
    """Undirected graph for rail subnet trees.

    Includes:

    * **RESISTOR/SERIES** pin-to-pin bridges
    * **Alias** edges: ``requested_net`` ↔ pin nets (same unions the
      rail grouper uses), plus each name ↔ its ``net_canonical`` form

    Alias edges let BFS start at the primary and still walk bridges that
    were annotated only on a local label.
    """
    adj: dict[str, set[str]] = {}
    if metadata is None:
        return adj
    canon_map: dict[str, str] = metadata.get("net_canonical") or {}

    def _canonical(net: str) -> str:
        if not net:
            return net
        return canon_map.get(net.upper(), net)

    def _note_aliases(*nets: str) -> None:
        for net in nets:
            if not net:
                continue
            _add_undirected_edge(adj, net, _canonical(net))

    for d in metadata.get("directives", []):
        role = d.get("role", "")
        terms = d.get("terminals") or {}
        nets_per_term: list[set[str]] = []
        for t in terms.values():
            nets = {p.get("net") for p in t.get("pins", []) if p.get("net")}
            req = t.get("requested_net")
            if nets:
                nets_per_term.append(nets)
            _note_aliases(*(nets or set()), req or "")
            if req:
                for n in nets:
                    _add_undirected_edge(adj, req, n)
        if role == "RESISTOR" and len(nets_per_term) == 2:
            for a in nets_per_term[0]:
                for b in nets_per_term[1]:
                    _add_undirected_edge(adj, a, b)
    return adj


def _bfs_attach_children(
    root: str,
    *,
    allowed: set[str],
    adj: dict[str, set[str]],
    visited: set[str],
    children_of: dict[str, list[str]],
) -> None:
    """BFS from ``root`` through ``allowed`` nets; record tree edges."""
    queue: deque[str] = deque([root])
    while queue:
        u = queue.popleft()
        for v in sorted(adj.get(u, set()) & allowed):
            if v in visited:
                continue
            visited.add(v)
            children_of.setdefault(u, []).append(v)
            children_of.setdefault(v, [])
            queue.append(v)


def _spanning_tree_from_primary(
    primary: str,
    members: list[str],
    adj: dict[str, set[str]],
) -> RailTreeNode:
    """BFS spanning tree of ``members`` rooted at ``primary``.

    Walks SERIES and alias edges. Any remaining connected components (nets
    with no path to the primary) are attached under the primary as their
    own BFS subtrees — not flattened — so SERIES chains among orphans keep
    their nesting.
    """
    member_set = set(members)
    if not member_set:
        return RailTreeNode(name=primary)

    root = primary if primary in member_set else sorted(member_set)[0]
    children_of: dict[str, list[str]] = {n: [] for n in member_set}
    if root not in children_of:
        children_of[root] = []

    visited: set[str] = {root}
    _bfs_attach_children(
        root,
        allowed=member_set,
        adj=adj,
        visited=visited,
        children_of=children_of,
    )

    while True:
        remaining = member_set - visited
        if not remaining:
            break
        seed = sorted(remaining)[0]
        visited.add(seed)
        children_of.setdefault(root, []).append(seed)
        children_of.setdefault(seed, [])
        _bfs_attach_children(
            seed,
            allowed=remaining,
            adj=adj,
            visited=visited,
            children_of=children_of,
        )

    for parent, kids in list(children_of.items()):
        children_of[parent] = sorted(kids)

    def _node(name: str) -> RailTreeNode:
        return RailTreeNode(
            name=name,
            children=tuple(_node(c) for c in children_of.get(name, ())),
        )

    return _node(root)


def build_rail_trees(
    metadata: TopologyMetadata | None,
    rail_to_members: dict[str, list[str]],
) -> dict[str, RailTreeNode]:
    """Build a SERIES spanning tree per rail (BFS from each primary).

    ``rail_to_members`` stays the flat membership used for copper / eyes;
    this returns the nested display shape only. Rails with a single member
    yield a leaf root. Missing or empty input yields ``{}``.

    Members listed under a primary but with no SERIES/alias path to that
    primary are attached as BFS subtrees under the primary (orphan
    components), so callers may pass an explicit membership map to exercise
    that path without going through :func:`compute_rail_groups`.
    """
    if not rail_to_members:
        return {}
    adj = _rail_tree_adjacency(metadata)
    return {
        primary: _spanning_tree_from_primary(primary, members, adj)
        for primary, members in rail_to_members.items()
    }


def flatten_rail_tree(
    root: RailTreeNode,
    *,
    depth: int = 1,
) -> list[tuple[str, int]]:
    """DFS preorder ``(net_name, depth)`` rows for the Rails list indent."""
    rows: list[tuple[str, int]] = [(root.name, depth)]
    for child in root.children:
        rows.extend(flatten_rail_tree(child, depth=depth + 1))
    return rows


def resolve_rail_member_nets(
    rail_names: list[str],
    rail_to_members: dict[str, list[str]],
    subnet_visible: dict[str, dict[str, bool]] | None = None,
    *,
    rail_only: bool = False,
) -> list[str]:
    """Resolve visible rail names to the net names whose copper should draw.

    When ``subnet_visible`` is provided for a multi-net rail, only member nets
    marked visible are included. Members omitted from the per-rail map default
    to visible (so a partial map does not silently hide nets). Single-net
    rails ignore ``subnet_visible`` entirely. ``rail_only`` further restricts
    to each rail's primary name.
    """
    if not rail_names:
        return []
    members: list[str] = []
    seen: set[str] = set()
    for rail_name in rail_names:
        full = rail_to_members.get(rail_name, [rail_name])
        subnets = (subnet_visible or {}).get(rail_name)
        if subnets is not None and len(full) > 1:
            picks = [n for n in full if subnets.get(n, True)]
        else:
            picks = list(full)
        if rail_only:
            picks = [rail_name] if rail_name in picks else []
        for net in picks:
            if net not in seen:
                seen.add(net)
                members.append(net)
    return members
