"""Topology voltage gauges: which blocks get one, what a reading says, and
how it is drawn. No solve: readings are built from hand-made report loads."""

from __future__ import annotations

import copy
import re

import pytest

from fypa.report.model import PASS, LoadResult, RailReport
from fypa.topology import build_topology_model, render_topology_svg
from fypa.topology.gauges import (
    LIMIT_COLOR,
    WARN_COLOR,
    apply_readings,
    draw_gauge,
    find_gauge_at,
    gauge_specs,
    reading_for,
    readings_from_loads,
)
from fypa.topology.hit_test import topology_tooltip_at
from fypa.topology.types import NodeGauge
from topology_fixtures import project_b_compact_metadata

ERR = "#ff7070"
FG = "#eee"      # a passing reading is drawn in the text colour


def _metadata(**sink):
    md = copy.deepcopy(project_b_compact_metadata())
    for d in md["directives"]:
        if d["role"] == "SINK":
            d.update(sink)
    return md


def _sink_node(model):
    return next(n for n in model.nodes if n.designator == "U1")


# --- which blocks get a gauge ---------------------------------------------------


def test_only_a_load_with_a_minimum_gets_a_gauge():
    sink = {"role": "SINK", "label": "U1", "terminals": {}}
    assert gauge_specs({"directives": [sink]}) == []
    assert gauge_specs({"directives": [dict(sink, max_voltage=3.6)]}) == []
    (g,) = gauge_specs({"directives": [dict(sink, min_voltage=3.0,
                                            max_voltage=3.6)]})
    assert (g.key, g.min_v, g.max_v, g.label) == ("SINK:U1", 3.0, 3.6, "")
    # A maximum at or under the minimum is ignored, not drawn inverted.
    (g,) = gauge_specs({"directives": [dict(sink, min_voltage=3.0,
                                            max_voltage=2.0)]})
    assert g.max_v is None


def test_regulator_input_gauge_from_its_dropout():
    reg = {"role": "REGULATOR", "label": "U2", "value": 3.3, "dropout": 0.3,
           "vin_max": 6.0, "terminals": {}}
    (g,) = gauge_specs({"directives": [reg]})
    assert g.key == "REGULATOR:U2"
    assert g.min_v == pytest.approx(3.6)
    assert g.max_v == 6.0


def test_several_gauges_in_one_block_are_tagged_by_port():
    a = {"role": "SINK", "label": "U5#1", "min_voltage": 1.0, "terminals": {}}
    b = {"role": "SINK", "label": "U5#2", "min_voltage": 3.0, "terminals": {}}
    specs = gauge_specs({"directives": [a, b],
                         "port_directives": {"P1": a, "P2": b}})
    assert [(s.key, s.label) for s in specs] == [("SINK:U5#1", "P1"),
                                                 ("SINK:U5#2", "P2")]


def test_gauge_row_makes_the_block_taller_only_when_asked():
    md = _metadata(min_voltage=1.0)
    off = _sink_node(build_topology_model(md, layout="rows"))
    on = _sink_node(build_topology_model(md, layout="rows", gauges=True))
    assert off.gauges == []
    (g,) = on.gauges
    assert on.height > off.height and on.width > off.width
    assert on.x < g.x and g.x + g.width < on.x + on.width
    assert on.y < g.y and g.y + g.height < on.y + on.height
    plain = _sink_node(build_topology_model(_metadata(), layout="rows",
                                            gauges=True))
    assert plain.gauges == [] and plain.height == off.height


# --- readings -----------------------------------------------------------------------


def _load(v_load, *, min_v=1.0, max_v=None, v_low=None, v_high=None,
          margin=None, key="SINK:U1"):
    ld = LoadResult(
        label="U1", kind="sink", net="VDD", pin_count=1, current_a=1.0,
        v_load=v_load, drop_v=None if v_load is None else 1.2 - v_load,
        return_drop_v=0.004, min_v=min_v, margin_v=None, status=PASS,
        worst_pin="U1-3", worst_layer="Top", max_v=max_v, v_low=v_low,
        v_high=v_high, key=key)
    if v_load is not None:
        ld.margin_v = v_load - min_v
        if v_low is not None:
            ld.margin_low_v = v_low - min_v
        if v_high is not None and max_v is not None:
            ld.margin_high_v = max_v - v_high
    return ld


def _rail(src_min=None, src_max=None):
    return RailReport(name="VDD", kind="supply", members=["VDD"],
                      nominal_v=1.2, source_label="", load_a=1.0,
                      source_min_v=src_min, source_max_v=src_max)


@pytest.mark.parametrize("v, status", [
    (1.15, "PASS"),
    (1.005, "WARN"),    # 5 mV: under 1 % of the 1.2 V setpoint
    (0.98, "FAIL"),
])
def test_reading_status_follows_the_worst_margin(v, status):
    r = reading_for(_load(v), _rail())
    assert r.status == status
    assert r.low == r.high == r.nominal == v
    assert r.src_low == r.src_high == 1.2      # no range: the setpoint


def test_reading_spans_the_supply_range_and_says_ground_counts():
    ld = _load(1.15, max_v=1.25, v_low=1.11, v_high=1.26)
    r = reading_for(ld, _rail(1.16, 1.26))
    assert (r.low, r.high) == (1.11, 1.26)
    assert (r.src_low, r.src_high) == (1.16, 1.26)
    assert r.status == "FAIL"                   # over the max at light load
    assert "ground rise counts" in r.tooltip
    assert "4 mV of it in the return" in r.tooltip
    assert "range 1.16–1.26 V" in r.tooltip


def test_unsolved_load_has_no_reading():
    assert reading_for(_load(None), _rail()) is None
    assert readings_from_loads([(_load(None), _rail())]) == {}
    assert set(readings_from_loads([(_load(1.1), _rail())])) == {"SINK:U1"}


# --- drawing --------------------------------------------------------------------------


def _gauge(reading=None, *, max_v=None, style="bar", label=""):
    return NodeGauge(key="SINK:U1", label=label, min_v=1.0, max_v=max_v,
                     x=0.0, y=0.0, width=168.0, height=36.0, reading=reading,
                     style=style)


def _draw(g):
    parts: list[str] = []
    draw_gauge(parts, g, bg="#000", fg=FG, fg_dim="#888", border="#555",
               err=ERR, surface="#333")
    return "\n".join(parts)


def _band_fills(svg):
    """Fills of the at-pins marks (a dot, or capsule pieces over a range)."""
    return re.findall(r'class="topo-gauge-pin"[^>]* fill="([^"]+)"', svg)


def _texts(svg):
    return re.findall(r">([^<>]+)</text>", svg)


def test_unsolved_gauge_shows_the_window_labelled():
    texts = _texts(_draw(_gauge(max_v=1.3)))
    assert "min 1 V" in texts and "max 1.3 V" in texts
    assert "not solved yet" in texts


def test_allowed_zone_is_green_and_outside_is_red():
    svg = _draw(_gauge(max_v=1.3))
    zones = re.findall(r'fill="([^"]+)" fill-opacity', svg)
    assert zones == [ERR, LIMIT_COLOR, ERR]
    # No maximum: green from the minimum to the end, red only below.
    zones = re.findall(r'fill="([^"]+)" fill-opacity', _draw(_gauge()))
    assert zones == [ERR, LIMIT_COLOR]


def test_only_the_part_past_a_limit_is_red():
    ld = _load(1.15, max_v=1.25, v_low=1.11, v_high=1.26)
    fills = _band_fills(_draw(_gauge(reading_for(ld, _rail(1.16, 1.26)),
                                     max_v=1.25)))
    assert FG in fills and ERR in fills
    ok = _draw(_gauge(reading_for(_load(1.15), _rail())))
    assert _band_fills(ok) == [FG]
    warn = _draw(_gauge(reading_for(_load(1.005), _rail())))
    assert _band_fills(warn) == [WARN_COLOR]


def test_margin_line_names_the_limit_the_margin_is_to():
    texts = _texts(_draw(_gauge(reading_for(_load(1.15), _rail()))))
    assert "1.15 V · ✓ +150 mV to min" in texts
    ld = _load(1.15, max_v=1.25, v_low=1.11, v_high=1.26)
    texts = _texts(_draw(_gauge(reading_for(ld, _rail(1.16, 1.26)),
                                max_v=1.25)))
    assert "1.11–1.26 V · ✗ -10 mV to max" in texts


def test_bar_margin_line_is_centred_above_the_bar():
    g = _gauge(reading_for(_load(1.15), _rail()))
    g.show_value = False          # the port shows the voltage
    svg = _draw(g)
    (line,) = re.findall(r'<text x="([\d.]+)" y="([\d.]+)" text-anchor="middle"'
                         r'[^>]*font-size="6"[^>]*>✓ \+150 mV to min<', svg)
    assert float(line[0]) == pytest.approx(g.x + g.width / 2, abs=0.1)
    bar_y = float(re.search(r'<rect x="[\d.]+" y="([\d.]+)"', svg).group(1))
    assert float(line[1]) < bar_y


def test_value_line_drops_words_before_the_number_when_tight():
    g = _gauge(reading_for(_load(1.15), _rail()))
    g.width = 70.0
    texts = _texts(_draw(g))
    assert "+150 mV" in " ".join(texts) and "to min" not in " ".join(texts)


def test_tick_labels_under_the_bar_skip_what_would_overlap():
    texts = _texts(_draw(_gauge(reading_for(_load(1.15), _rail()))))
    assert "min 1 V" in texts and "supply 1.2 V" in texts
    # Supply right on the maximum: its label would sit on "max", so it goes.
    texts = _texts(_draw(_gauge(reading_for(_load(1.15), _rail()),
                                max_v=1.2)))
    assert "max 1.2 V" in texts and "supply 1.2 V" not in texts


def _supply_marks(svg):
    return re.findall(r'<path class="topo-gauge-supply" d="([^"]+)"', svg)


def test_supply_without_a_range_is_a_caret_not_a_bracket():
    flat = _supply_marks(_draw(_gauge(reading_for(_load(1.15), _rail()))))
    assert len(flat) == 1 and flat[0].endswith("Z")       # closed caret
    ranged = _supply_marks(_draw(_gauge(reading_for(_load(1.15),
                                                    _rail(1.14, 1.26)))))
    assert len(ranged) == 1 and not ranged[0].endswith("Z")   # open bracket


def test_value_off_the_scale_gets_one_arrow_on_its_side():
    # 0.5 V against a 1.0 V minimum: far off the low end only.
    svg = _draw(_gauge(reading_for(_load(0.5), _rail())))
    (poly,) = re.findall(r'<polygon points="([^"]+)"', svg)
    xs = [float(p.split(",")[0]) for p in poly.split()]
    assert min(xs) < 0.0                         # points left, past x = 0


def test_text_style_is_two_lines_and_no_bar():
    svg = _draw(_gauge(reading_for(_load(1.15), _rail(1.14, 1.26)),
                       max_v=1.3, style="text", label="P1"))
    assert "<rect" not in svg
    texts = _texts(svg)
    assert "P1 1.15 V" in texts
    assert "min 1 V · max 1.3 V · supply 1.14–1.26 V" in texts


def test_text_style_is_shorter_than_the_bar():
    md = _metadata(min_voltage=1.0)
    bar = _sink_node(build_topology_model(md, layout="rows", gauges="bar"))
    text = _sink_node(build_topology_model(md, layout="rows", gauges="text"))
    assert text.height < bar.height
    assert bar.gauges[0].style == "bar" and text.gauges[0].style == "text"


def test_render_and_hover_through_the_model():
    md = _metadata(min_voltage=1.0, max_voltage=1.3)
    model = build_topology_model(md, layout="rows", gauges=True)
    apply_readings(model, {"SINK:U1": reading_for(_load(1.15), _rail())},
                   stale=True)
    (g,) = _sink_node(model).gauges
    assert g.reading is not None and g.stale
    svg = render_topology_svg(model)
    assert 'class="topo-gauge" opacity="0.45"' in svg
    assert find_gauge_at(model, g.x + 1, g.y + g.height / 2) is g
    tip = topology_tooltip_at(model, g.x + 1, g.y + g.height / 2)
    assert "U1: 1.15 V at its pins" in tip and "Resolve" in tip


def test_voltages_drop_trailing_zeros():
    from fypa.topology.gauges import fmt_volts, port_voltage_text

    assert [fmt_volts(v) for v in (1.0, 0.99, 0.951, 0.9912, -0.0001, 12.0)]         == ["1", "0.99", "0.951", "0.991", "0", "12"]
    assert port_voltage_text(1.0004) == "1 V"


# --- port voltages ------------------------------------------------------------------


def test_supply_ports_get_a_key_and_draw_their_voltage():
    md = _metadata(min_voltage=1.0)
    model = build_topology_model(md, layout="rows", gauges="bar")
    keys = {p.vkey for n in model.nodes for p in n.ports if p.vkey}
    assert {"SINK:U1:P", "SOURCE:J1:P"} <= keys
    assert not any(p.vkey.endswith((":N", "_N")) for n in model.nodes
                   for p in n.ports)
    model.port_voltages = True
    apply_readings(model, {"SINK:U1": reading_for(_load(1.15), _rail())},
                   ports={"SINK:U1:P": 0.9912, "SOURCE:J1:P": 1.0})
    sink = _sink_node(model)
    port = next(p for p in sink.ports if p.vkey == "SINK:U1:P")
    assert port.voltage == 0.9912 and "0.991 V at these pins" in port.tooltip
    assert sink.gauges[0].show_value is False   # the port has it
    svg = render_topology_svg(model)
    assert ">0.991 V</text>" in svg and ">1 V</text>" in svg
    model.port_voltages = False
    assert ">0.991 V</text>" not in render_topology_svg(model)


@pytest.mark.parametrize("cascade", [False, True])
def test_port_voltage_labels_never_overlap(cascade):
    """Widest labels on every port: the layout leaves room for both ends of
    every wire (J1 → U2 → U1 here, inline when cascaded)."""
    md = _metadata(min_voltage=1.0)
    model = build_topology_model(md, layout="rows", gauges="bar",
                                 cascade=cascade, port_voltages=True)
    ports = {p.vkey: -12.345 for n in model.nodes for p in n.ports if p.vkey}
    assert len(ports) >= 4
    apply_readings(model, None, ports=ports)
    svg = render_topology_svg(model)
    boxes = [tuple(float(v) for v in m) for m in re.findall(
        r'<g class="topo-port-v">\s*<rect x="([-\d.]+)" y="([-\d.]+)"'
        r' width="([\d.]+)" height="([\d.]+)"', svg)]
    assert len(boxes) == len(ports)
    for i, (ax, ay, aw, ah) in enumerate(boxes):
        for bx, by, bw, bh in boxes[i + 1:]:
            assert (ax + aw <= bx or bx + bw <= ax
                    or ay + ah <= by or by + bh <= ay), "labels overlap"


@pytest.mark.parametrize("cascade", [False, True])
def test_port_voltage_labels_clear_the_port_tags(cascade):
    """Regression: a regulator's input tag (``VIN ▷``) sat 14 px from its port,
    so the input voltage drawn under that stub ran over the tag."""
    md = _metadata(min_voltage=1.0)
    model = build_topology_model(md, layout="rows", gauges="bar",
                                 cascade=cascade, port_voltages=True)
    ports = {p.vkey: -12.345 for n in model.nodes for p in n.ports if p.vkey}
    apply_readings(model, None, ports=ports)
    svg = render_topology_svg(model)
    boxes = [tuple(float(v) for v in m) for m in re.findall(
        r'<g class="topo-port-v">\s*<rect x="([-\d.]+)" y="([-\d.]+)"'
        r' width="([\d.]+)" height="([\d.]+)"', svg)]
    assert model.tags and boxes
    for t in model.tags:
        tx0, ty0, tx1, ty1 = t.x, t.y - t.height / 2, t.x + t.width, t.y + t.height / 2
        for bx, by, bw, bh in boxes:
            assert (bx + bw <= tx0 or tx1 <= bx or by + bh <= ty0 or ty1 <= by), (
                f"port voltage over the {t.text} tag")


def test_port_voltages_widen_the_gap_between_blocks():
    md = _metadata(min_voltage=1.0)
    plain = build_topology_model(md, layout="rows", cascade=True)
    roomy = build_topology_model(md, layout="rows", cascade=True,
                                 port_voltages=True)
    assert roomy.width > plain.width and roomy.port_voltages


def test_text_style_leaves_the_voltage_to_the_port():
    g = _gauge(reading_for(_load(1.15), _rail()), style="text")
    assert "1.15 V" in _texts(_draw(g))
    g.show_value = False
    texts = _texts(_draw(g))
    assert "✓ +150 mV to min" in texts
    assert not any("1.15 V" in t for t in texts)
