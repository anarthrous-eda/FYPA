"""Header-band text fitting: the role word and designator must not collide."""

import re

from fypa.topology import build_topology_model, render_topology_svg
from fypa.topology.constants import (
    HEADER_FONT_SIZE,
    HEADER_LABEL_MIN_FONT,
    HEADER_LABEL_SHRINK_FLOOR,
    HEADER_TEXT_GAP,
    HEADER_TEXT_PAD,
    NODE_W,
)
from fypa.topology.render import _fit_header_texts
from fypa.topology.util import estimate_text_width
from tests.topology_fixtures import project_b_compact_metadata


def _long_designator_metadata(name: str) -> dict:
    """project_b_compact with every designator replaced by ``name``-prefixed ids."""
    md = project_b_compact_metadata()
    for i, directive in enumerate(md["directives"]):
        renamed = f"{name}{i}"
        directive["designator"] = renamed
        directive["label"] = renamed
    return md


_HEADER_TEXT_RE = re.compile(
    r'<text x="(?P<x>[-\d.]+)" y="(?P<y>[-\d.]+)" fill="#ffffff"'
    r'(?P<anchor> text-anchor="end")?'
    r' font-family="Segoe UI,sans-serif" font-size="(?P<size>[\d.]+)"'
    r' font-weight="600">(?P<text>[^<]*)</text>'
)


def _header_text_spans(svg: str) -> list[tuple[float, float, float]]:
    """(y, left, right) extents of every header text in ``svg``."""
    spans = []
    for m in _HEADER_TEXT_RE.finditer(svg):
        width = estimate_text_width(m["text"], float(m["size"]))
        x = float(m["x"])
        left = x - width if m["anchor"] else x
        spans.append((float(m["y"]), left, left + width))
    return spans


def test_short_designator_keeps_role_word_at_full_size():
    fitted = _fit_header_texts("SINK", "U12", NODE_W)
    assert fitted == ("SINK", "U12", HEADER_FONT_SIZE)


def test_designator_without_label_keeps_role_word():
    fitted = _fit_header_texts("REGULATOR", None, NODE_W)
    assert fitted == ("REGULATOR", None, HEADER_FONT_SIZE)


def test_slightly_long_designator_shrinks_both_texts_together():
    fitted = _fit_header_texts("SINK", "VREG_CORE_1V8_X", NODE_W)
    assert fitted.role == "SINK"
    assert fitted.label == "VREG_CORE_1V8_X"
    assert HEADER_LABEL_SHRINK_FLOOR <= fitted.font_size < HEADER_FONT_SIZE


def test_long_designator_drops_role_word_and_keeps_full_name():
    fitted = _fit_header_texts("SINK", "PART_18b854315d83", NODE_W)
    assert fitted == (None, "PART_18b854315d83", HEADER_FONT_SIZE)


def test_designator_too_long_for_the_band_is_truncated():
    fitted = _fit_header_texts("SINK", "PART_18b854315d83_AND_MORE", NODE_W)
    assert fitted.role is None
    assert fitted.label.startswith("PART_18b854315d83")
    assert fitted.label.endswith("…")
    assert fitted.font_size == HEADER_LABEL_MIN_FONT


def test_fitted_header_texts_always_fit_the_band():
    band = NODE_W - 2 * HEADER_TEXT_PAD
    labels = [
        None, "U1", "R14", "VREG_1V8", "VREG_CORE_1V8", "U12_BUCK_CORE",
        "PART_18b854315d83", "PART_18b854315d83_AND_MORE", "W" * 40, "l" * 40,
    ]
    for role in ("SOURCE", "SINK", "SERIES", "REGULATOR"):
        for label in labels:
            fitted = _fit_header_texts(role, label, NODE_W)
            used = 0.0
            if fitted.role:
                used += estimate_text_width(fitted.role, fitted.font_size)
            if fitted.label:
                used += estimate_text_width(fitted.label, fitted.font_size)
            if fitted.role and fitted.label:
                used += HEADER_TEXT_GAP
            assert used <= band, f"{role} + {label} overruns the header band"


def test_rendered_header_texts_do_not_overlap():
    """Regression: long auto-generated designators used to run over the role word."""
    md = _long_designator_metadata("PART_e9f50b2700f9a")
    svg = render_topology_svg(build_topology_model(md))
    spans = _header_text_spans(svg)
    assert spans, "no header texts rendered"
    for y, left, right in spans:
        for other_y, other_left, other_right in spans:
            if (y, left, right) == (other_y, other_left, other_right) or y != other_y:
                continue
            assert right <= other_left or left >= other_right, (
                f"header texts overlap at y={y}"
            )


def test_rendered_short_designators_keep_the_role_word():
    svg = render_topology_svg(build_topology_model(project_b_compact_metadata()))
    assert 'font-size="10" font-weight="600">SOURCE<' in svg
    assert 'font-weight="600">J1</text>' in svg
