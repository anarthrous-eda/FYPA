"""Rail grouping for PDN net names — shared by the viewer and topology schematic."""

from __future__ import annotations

from fypa.topology.net_aliases import is_gnd_alias
from fypa.topology.metadata_schema import TopologyMetadata


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
