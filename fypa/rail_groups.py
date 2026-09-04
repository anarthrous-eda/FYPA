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
        for tname, t in terms.items():
            nets = {p.get("net") for p in t.get("pins", []) if p.get("net")}
            req = t.get("requested_net")
            for n in nets:
                find(n)  # ensure presence in union-find
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
        for a, b in resistor_bridge_pairs(d):
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


def bridge_pair_key(a: str, b: str) -> frozenset[str]:
    """Case-insensitive undirected key for a SERIES/RESISTOR pin pair."""
    return frozenset((a.upper(), b.upper()))


def resistor_bridge_pairs(directive: dict) -> list[tuple[str, str]]:
    """Every net pair bridged by a RESISTOR's two terminals.

    The one place that reads a SERIES part's terminals: the rail grouper
    (union), the rail-tree adjacency (edge) and the editor-SERIES dedupe
    (key) all call this, so a change to how terminals are read — a terminal
    with no resolved pins, a three-terminal part — cannot leave the three
    disagreeing about which nets are bridged.
    """
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
    return [
        (a, b)
        for a in sorted(nets_per_term[0])
        for b in sorted(nets_per_term[1])
        if a and b and a != b
    ]


def merge_rail_tree_metadata(
    metadata: TopologyMetadata | None,
    editor_series: list[tuple[str, str]] | None = None,
) -> dict:
    """Build metadata for :func:`build_rail_trees` (pending / editor rails).

    Carries every directive through, then appends editor SERIES
    ``(p_net, n_net)`` pairs whose bridges are not already covered
    (case-insensitive; all bipartite RESISTOR pin pairs counted).

    Filtering to the SERIES/alias-carrying roles here looked like a cheap
    narrowing, but :func:`_rail_tree_adjacency` reads ``requested_net``
    aliases off directives of *every* role. Dropping the others made the
    pending tree nest differently from the solved one built straight from
    ``metadata``, so a subnet reachable only through such an alias jumped
    levels the moment the user pressed Resolve.
    """
    meta = dict(metadata) if isinstance(metadata, dict) else {}
    directives = list(meta.get("directives") or [])
    seen_bridges: set[frozenset[str]] = set()
    for d in directives:
        for a, b in resistor_bridge_pairs(d):
            seen_bridges.add(bridge_pair_key(a, b))
    for p_net, n_net in editor_series or ():
        if not p_net or not n_net:
            continue
        key = bridge_pair_key(p_net, n_net)
        if len(key) != 2 or key in seen_bridges:
            continue
        seen_bridges.add(key)
        directives.append({
            "role": "RESISTOR",
            "terminals": {
                "P": {"pins": [{"net": p_net}]},
                "N": {"pins": [{"net": n_net}]},
            },
        })
    out = dict(meta)
    out["directives"] = directives
    return out


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
        terms = d.get("terminals") or {}
        for t in terms.values():
            nets = {p.get("net") for p in t.get("pins", []) if p.get("net")}
            req = t.get("requested_net")
            _note_aliases(*(nets or set()), req or "")
            if req:
                for n in nets:
                    _add_undirected_edge(adj, req, n)
        for a, b in resistor_bridge_pairs(d):
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


def visible_rail_tree_rows(
    rail: str,
    members: list[str],
    tree: RailTreeNode | None,
    node_expanded: dict[tuple[str, str], bool] | None = None,
) -> list[tuple[str, int, bool]]:
    """Visible subnet rows as ``(net, depth, has_children)``.

    Walks ``tree`` but only descends into nodes marked expanded in
    ``node_expanded``. Missing keys default to expanded only for the
    primary (``net == rail``) so the first level of children appears
    without exploding deep SERIES chains. Flat fallback when ``tree``
    is ``None``.
    """
    expanded_map = node_expanded if node_expanded is not None else {}
    if tree is None:
        return [(net, 1, False) for net in members]

    rows: list[tuple[str, int, bool]] = []

    def _is_expanded(net: str) -> bool:
        key = (rail, net)
        if key in expanded_map:
            return bool(expanded_map[key])
        return net == rail

    def _walk(node: RailTreeNode, depth: int) -> None:
        has_children = bool(node.children)
        rows.append((node.name, depth, has_children))
        if has_children and _is_expanded(node.name):
            for child in node.children:
                _walk(child, depth + 1)

    _walk(tree, 1)
    return rows


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


def find_rail_tree_node(
    root: RailTreeNode | None,
    net: str,
) -> RailTreeNode | None:
    """Return the node named ``net`` in ``root``, or ``None``."""
    if root is None:
        return None
    if root.name == net:
        return root
    for child in root.children:
        found = find_rail_tree_node(child, net)
        if found is not None:
            return found
    return None


def subtree_net_names(
    root: RailTreeNode | None,
    net: str,
) -> list[str]:
    """DFS preorder names for ``net`` and all SERIES descendants.

    Empty list when ``net`` is not in ``root``. A leaf returns ``[net]``.
    """
    node = find_rail_tree_node(root, net)
    if node is None:
        return []
    return [name for name, _depth in flatten_rail_tree(node, depth=1)]


def subtree_toggle_target(own_visible: bool) -> bool:
    """Ctrl+Click fan-out target for a SERIES subtree.

    The whole subtree follows what a plain click on the clicked node would
    have done to that node alone: visible → hide the branch, hidden → show
    it. Deriving the target from the descendants instead ("anything mixed →
    show all") makes hiding a partly-hidden branch impossible in one gesture,
    since the first Ctrl+Click has to render every net in it first.
    """
    return not bool(own_visible)


def rail_tree_node_partial_flags(
    root: RailTreeNode | None,
    visible: dict[str, bool],
) -> dict[str, bool]:
    """Map each tree net to whether its eye should show the partial badge.

    A node is partial when any SERIES descendant's visibility differs from
    its own — "my state does not describe this whole branch". That covers
    both mixed descendants and the node being hidden while descendants are
    drawn, and it stays purely informational: the badge never changes what a
    click does (plain click toggles this net, Ctrl+Click the branch), so
    there is no state it can trap the user in.

    Leaves, and nodes with no eye of their own, are always False. Single-pass
    post-order — O(n).
    """
    flags: dict[str, bool] = {}
    if root is None:
        return flags

    def _walk(node: RailTreeNode) -> tuple[bool, bool]:
        """Return (any descendant-or-self on, any descendant-or-self off)."""
        any_on = any_off = False
        desc_on = desc_off = False
        for child in node.children:
            c_on, c_off = _walk(child)
            desc_on = desc_on or c_on
            desc_off = desc_off or c_off
        own = visible.get(node.name)
        if own is None:
            flags[node.name] = False
        else:
            flags[node.name] = (desc_on and not own) or (desc_off and own)
        any_on = desc_on or bool(own)
        any_off = desc_off or (own is False)
        return any_on, any_off

    _walk(root)
    return flags


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
