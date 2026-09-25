"""Marquee multi-select logic tests.

``fypa.editor_multiselect`` holds the two pieces of the editor-mode
rubber-band feature that are worth testing without a viewer: deciding
which drawn markers a dragged rectangle captures, and merging the
captured markers' field values into either a shared value or the ``*``
sentinel the side panel shows for a disagreement.
"""
from __future__ import annotations

from fypa.editor_multiselect import (
    MIXED,
    MULTI_SINK_FIELDS,
    MarqueeCandidate,
    common_fields,
    marquee_select,
    merge_values,
    normalise_rect,
    points_enclosed,
)
from fypa.project_file import EditorDirective


def _cand(key: str, *points, role: str = "SINK") -> MarqueeCandidate:
    return MarqueeCandidate(key=("directive", key), role=role,
                            points=tuple(points), label=key)


# --- rectangle normalisation -------------------------------------------

def test_normalise_rect_orders_corners_from_any_drag_direction():
    want = (1.0, 2.0, 5.0, 8.0)
    assert normalise_rect(1, 2, 5, 8) == want      # down-right
    assert normalise_rect(5, 8, 1, 2) == want      # up-left
    assert normalise_rect(5, 2, 1, 8) == want      # down-left
    assert normalise_rect(1, 8, 5, 2) == want      # up-right


# --- enclosure ----------------------------------------------------------

def test_points_enclosed_requires_every_point_inside():
    rect = (0.0, 0.0, 10.0, 10.0)
    assert points_enclosed([(1, 1), (9, 9)], rect)
    # One pad outside ⇒ the whole marker stays unselected. A big connector
    # sink glyphs at each pad, and clipping one corner must not drag it in.
    assert not points_enclosed([(1, 1), (11, 9)], rect)


def test_points_enclosed_counts_the_boundary_as_inside():
    assert points_enclosed([(0, 0), (10, 10)], (0.0, 0.0, 10.0, 10.0))


def test_points_enclosed_is_false_for_a_marker_with_nothing_drawn():
    # No glyph points ⇒ the marker is not on screen (hidden layer / rail),
    # so it must not be selectable.
    assert not points_enclosed([], (0.0, 0.0, 10.0, 10.0))


def test_marquee_select_keeps_only_fully_enclosed_candidates_in_order():
    inside = _cand("a", (2, 2))
    straddling = _cand("b", (2, 2), (99, 99))
    outside = _cand("c", (50, 50))
    also_inside = _cand("d", (3, 3), (4, 4))
    hits = marquee_select([inside, straddling, outside, also_inside],
                          normalise_rect(0, 0, 10, 10))
    assert [c.key[1] for c in hits] == ["a", "d"]


# --- value merging ------------------------------------------------------

def test_merge_values_returns_the_shared_value():
    assert merge_values([0.5, 0.5, 0.5]) == 0.5


def test_merge_values_flags_a_disagreement_as_mixed():
    assert merge_values([0.5, 1.5]) is MIXED


def test_merge_values_treats_a_shared_none_as_a_value_not_a_disagreement():
    # Every sink leaving Min V unset is agreement — the panel shows a blank
    # field, not ``*``.
    assert merge_values([None, None]) is None
    assert merge_values([None, 1.8]) is MIXED


def test_merge_values_does_not_confuse_bools_with_equal_numbers():
    # ``1.0 == True`` in Python; a bool field and a numeric one that happen
    # to compare equal are still a disagreement.
    assert merge_values([True, 1]) is MIXED
    assert merge_values([True, True]) is True
    assert merge_values([True, False]) is MIXED


def test_merge_values_tolerates_an_int_float_round_trip():
    # A project-file round-trip can hand back ``1`` where another sink
    # holds ``1.0``; calling that ``*`` would be a lie.
    assert merge_values([1, 1.0]) == 1


def test_merge_values_of_nothing_is_none():
    assert merge_values([]) is None


# --- field merge over real directives -----------------------------------

def _sink(**kw) -> EditorDirective:
    base = {"kind": "component", "role": "SINK", "single_net": True,
            "p_net": "+3V3", "current": 0.5}
    base.update(kw)
    return EditorDirective(**base)


def test_common_fields_splits_shared_values_from_mixed_ones():
    sinks = [
        _sink(designator="U1", current=0.5, min_voltage=3.0),
        _sink(designator="U2", current=1.25, min_voltage=3.0),
    ]
    got = common_fields(sinks)
    assert got["current"] is MIXED          # 0.5 vs 1.25
    assert got["min_voltage"] == 3.0        # both 3.0
    assert got["p_net"] == "+3V3"           # both on the same rail
    assert got["single_net"] is True
    assert got["n_net"] is None             # both unset


def test_common_fields_covers_every_multi_editable_field():
    got = common_fields([_sink(designator="U1")])
    assert set(got) == set(MULTI_SINK_FIELDS)


def test_common_fields_of_one_directive_is_never_mixed():
    got = common_fields([_sink(designator="U1", current=2.0)])
    assert all(v is not MIXED for v in got.values())
    assert got["current"] == 2.0


def test_multi_editable_fields_exclude_per_part_pad_lists():
    # Pushing one pad list or DES list across several different footprints
    # is never what the user means, so those fields must stay out.
    for name in ("p_pins", "n_pins", "p_des", "n_des", "anchor_xy", "layer"):
        assert name not in MULTI_SINK_FIELDS


def test_mixed_sentinel_renders_as_a_star_and_is_falsy():
    assert str(MIXED) == "*"
    assert not MIXED
