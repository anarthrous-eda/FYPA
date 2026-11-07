"""The merged low-ohm link step of build_problem's connectivity check.

A SERIES link at or below NET_MERGE_RESISTANCE_THRESHOLD_OHM is absorbed: its
two nets become one canonical net and build_problem re-emits the link as a
coupling resistor. The copper on its two sides is then one component even when
no via joins it, so the connectivity check must not report a break there
(_union_absorbed_bridge_ends).
"""

import shapely.geometry

from fypa.altium.loader import _AbsorbedBridge, _union_absorbed_bridge_ends

NET = 3


def _box(x0: float, x1: float):
    return shapely.geometry.box(x0, 0.0, x1, 1.0)


# Two islands of the canonical net on layer 1, with a gap between them, and a
# third piece of the same net on layer 2 under the right-hand island.
PIECES = [
    (1, 0, _box(0.0, 1.0)),
    (1, 1, _box(2.0, 3.0)),
    (2, 0, _box(2.0, 3.0)),
]


def _bridge(p=(1, 0.5, 0.5), n=(1, 2.5, 0.5), net=NET) -> _AbsorbedBridge:
    return _AbsorbedBridge(
        designator="R7", resistance=0.0,
        p_layer_id=p[0], p_x_mm=p[1], p_y_mm=p[2],
        n_layer_id=n[0], n_x_mm=n[1], n_y_mm=n[2],
        canonical_net_index=net,
    )


def _components(bridges) -> list[set[int]]:
    parent = list(range(len(PIECES)))

    def find(i: int) -> int:
        while parent[i] != i:
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        parent[find(b)] = find(a)

    _union_absorbed_bridge_ends(bridges, NET, PIECES, union)
    groups: dict[int, set[int]] = {}
    for i in range(len(PIECES)):
        groups.setdefault(find(i), set()).add(i)
    return sorted(groups.values(), key=min)


def test_a_merged_link_joins_the_islands_at_its_two_pads():
    assert _components([_bridge()]) == [{0, 1}, {2}]


def test_without_a_merged_link_the_islands_stay_apart():
    assert _components([]) == [{0}, {1}, {2}]


def test_a_link_on_another_net_joins_nothing():
    assert _components([_bridge(net=NET + 1)]) == [{0}, {1}, {2}]


def test_each_pad_is_matched_on_its_own_layer():
    """The N pad sits on layer 2: it joins the layer-2 piece, not the layer-1
    island at the same XY."""
    assert _components([_bridge(n=(2, 2.5, 0.5))]) == [{0, 2}, {1}]


def test_a_pad_on_no_copper_joins_nothing():
    """An end in the gap between the islands lands on no piece; the link is
    skipped rather than joined to an arbitrary piece."""
    assert _components([_bridge(n=(1, 1.5, 0.5))]) == [{0}, {1}, {2}]
