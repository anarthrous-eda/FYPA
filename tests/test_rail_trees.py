"""SERIES spanning trees for the expandable Rails list."""

from fypa.rail_groups import (
    RailTreeNode,
    build_rail_trees,
    compute_rail_groups,
    flatten_rail_tree,
)


def _source(net: str) -> dict:
    return {
        "role": "SOURCE",
        "terminals": {
            "P": {"requested_net": net, "pins": [{"net": net}]},
            "N": {"requested_net": "GND", "pins": [{"net": "GND"}]},
        },
    }


def _resistor(a: str, b: str) -> dict:
    return {
        "role": "RESISTOR",
        "terminals": {
            "P": {"pins": [{"net": a}]},
            "N": {"pins": [{"net": b}]},
        },
    }


def test_rail_tree_chain_nests_by_series_hops():
    """A—R—A.1—R—A.1.1 nests as A → A.1 → A.1.1."""
    metadata = {
        "directives": [
            _source("A"),
            _resistor("A", "A.1"),
            _resistor("A.1", "A.1.1"),
        ],
    }
    names, members = compute_rail_groups(metadata)
    assert "A" in names
    trees = build_rail_trees(metadata, members)
    tree = trees["A"]
    assert tree == RailTreeNode(
        name="A",
        children=(
            RailTreeNode(
                name="A.1",
                children=(RailTreeNode(name="A.1.1"),),
            ),
        ),
    )
    assert flatten_rail_tree(tree) == [
        ("A", 1),
        ("A.1", 2),
        ("A.1.1", 3),
    ]


def test_rail_tree_star_splits_under_primary():
    """A—R—A.1 and A—R—A.2 are siblings under A."""
    metadata = {
        "directives": [
            _source("A"),
            _resistor("A", "A.1"),
            _resistor("A", "A.2"),
        ],
    }
    _, members = compute_rail_groups(metadata)
    tree = build_rail_trees(metadata, members)["A"]
    assert tree.name == "A"
    assert [c.name for c in tree.children] == ["A.1", "A.2"]
    assert all(c.children == () for c in tree.children)
    assert flatten_rail_tree(tree) == [
        ("A", 1),
        ("A.1", 2),
        ("A.2", 2),
    ]


def test_rail_tree_branch_matches_example_shape():
    """A → A.1 → {A.1.1, A.1.2} and A → A.2."""
    metadata = {
        "directives": [
            _source("A"),
            _resistor("A", "A.1"),
            _resistor("A", "A.2"),
            _resistor("A.1", "A.1.1"),
            _resistor("A.1", "A.1.2"),
        ],
    }
    _, members = compute_rail_groups(metadata)
    tree = build_rail_trees(metadata, members)["A"]
    assert flatten_rail_tree(tree) == [
        ("A", 1),
        ("A.1", 2),
        ("A.1.1", 3),
        ("A.1.2", 3),
        ("A.2", 2),
    ]


def test_rail_tree_cycle_is_spanning_tree_without_duplicates():
    """A cycle yields a BFS tree; every member appears once."""
    metadata = {
        "directives": [
            _source("A"),
            _resistor("A", "B"),
            _resistor("B", "C"),
            _resistor("C", "A"),
        ],
    }
    _, members = compute_rail_groups(metadata)
    tree = build_rail_trees(metadata, members)["A"]
    rows = flatten_rail_tree(tree)
    names = [n for n, _ in rows]
    assert names[0] == "A"
    assert sorted(names) == sorted(members["A"])
    assert len(names) == len(set(names))


def test_rail_tree_orphan_attaches_under_primary():
    """Member joined only via alias union (no SERIES edge) hangs under primary."""
    metadata = {
        "directives": [
            {
                "role": "SOURCE",
                "terminals": {
                    "P": {
                        "requested_net": "VIN",
                        "pins": [{"net": "VIN"}, {"net": "VIN_ALIAS"}],
                    },
                    "N": {
                        "requested_net": "GND",
                        "pins": [{"net": "GND"}],
                    },
                },
            },
            _resistor("VIN", "VOUT"),
        ],
    }
    _, members = compute_rail_groups(metadata)
    tree = build_rail_trees(metadata, members)["VIN"]
    child_names = {c.name for c in tree.children}
    assert "VOUT" in child_names
    assert "VIN_ALIAS" in child_names
    assert all(c.children == () for c in tree.children)


def test_rail_tree_local_label_series_nests_via_alias():
    """SERIES on a local label still nests under the canonical primary."""
    metadata = {
        "net_canonical": {
            "VIN_LOCAL": "VIN",
            "VIN": "VIN",
        },
        "directives": [
            {
                "role": "SOURCE",
                "terminals": {
                    "P": {
                        "requested_net": "VIN_LOCAL",
                        "resolved_via_local": True,
                        "pins": [{"net": "VIN"}],
                    },
                    "N": {
                        "requested_net": "GND",
                        "pins": [{"net": "GND"}],
                    },
                },
            },
            {
                "role": "RESISTOR",
                "terminals": {
                    "P": {
                        "requested_net": "VIN_LOCAL",
                        "pins": [{"net": "VIN_LOCAL"}],
                    },
                    "N": {"pins": [{"net": "VOUT"}]},
                },
            },
            _resistor("VOUT", "VOUT_FB"),
        ],
    }
    _, members = compute_rail_groups(metadata)
    assert flatten_rail_tree(build_rail_trees(metadata, members)["VIN"]) == [
        ("VIN", 1),
        ("VIN_LOCAL", 2),
        ("VOUT", 3),
        ("VOUT_FB", 4),
    ]


def test_rail_tree_orphan_component_keeps_series_nesting():
    """SERIES chain unreachable from primary stays nested under an orphan root."""
    # No alias edge from PRIMARY to the chain — only UF membership via a
    # shared requested_net union that we simulate by listing all members
    # explicitly after grouping would have merged them. Build the tree
    # directly so the adjacency has SERIES only among the orphan chain.
    from fypa.rail_groups import _rail_tree_adjacency, _spanning_tree_from_primary

    metadata = {
        "directives": [
            _resistor("X", "Y"),
            _resistor("Y", "Z"),
        ],
    }
    adj = _rail_tree_adjacency(metadata)
    # PRIMARY is in the member set but has no SERIES/alias edge to X/Y/Z.
    tree = _spanning_tree_from_primary(
        "PRIMARY",
        ["PRIMARY", "X", "Y", "Z"],
        adj,
    )
    assert flatten_rail_tree(tree) == [
        ("PRIMARY", 1),
        ("X", 2),
        ("Y", 3),
        ("Z", 4),
    ]


def test_build_rail_trees_empty_input():
    assert build_rail_trees(None, {}) == {}
    assert build_rail_trees({"directives": []}, {}) == {}
