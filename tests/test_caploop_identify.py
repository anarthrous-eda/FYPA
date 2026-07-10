"""Decoupling-capacitor identification (fypa.caploop.identify).

Covers detection heuristics + overrides, escape-via clustering (stitching
fields rejected, long escapes flagged, TH pads as their own escape),
reference-cavity selection against per-(layer, net) shapes, informational
part-value parsing, and the metadata lookups (design voltage, default SINK
target). Fixtures are hand-built ExtractedProjects in the style of
test_multilayer_copper.py.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import shapely.geometry

from fypa.altium.extract import (
    ExtractedProject,
    Pt2D,
    RawNet,
    RawPad,
    RawPcbComponent,
    RawStackupLayer,
    RawVia,
    _v9_dielectric_gaps,
)
from fypa.caploop.constants import CapLoopSettings
from fypa.caploop.identify import (
    associate_escape_vias,
    default_target_for_rail,
    design_voltage_for_rail,
    has_flag,
    identify_capacitors,
    parse_cap_params,
)

# --- fixtures -------------------------------------------------------------

GND, PWR, SIG = 0, 1, 2
NET_NAMES = ("GND", "+3V3", "SPI_CLK")

# 4-layer stack: Top(1) → GND plane(39) → PWR plane(40) → Bottom(32).
# z centres: L1=0.0175, L39=0.2525, L40=0.5875, L32=0.8225.


def _stackup() -> tuple[RawStackupLayer, ...]:
    def lay(lid, name, nxt, diel, plane_net=None):
        return RawStackupLayer(
            layer_id=lid, name=name, copper_thickness_mm=0.035,
            dielectric_thickness_mm=diel, next_layer_id=nxt,
            is_plane=plane_net is not None, plane_net_name=plane_net,
            mech_enabled=True,
        )
    return (
        lay(1, "Top", 39, 0.2),
        lay(39, "GND Plane", 40, 0.3, plane_net="GND"),
        lay(40, "PWR Plane", 32, 0.2, plane_net="+3V3"),
        lay(32, "Bottom", 0, 0.0),
    )


def _pad(x, y, net, comp=0, hole=0.0, designator="1"):
    return RawPad(
        center=Pt2D(x, y), width_mm=0.6, height_mm=0.5, hole_mm=hole,
        shape=2, rotation_deg=0.0, layer_id=74 if hole > 0.0 else 1,
        net_index=net, designator=designator, component_index=comp,
        is_through_hole=hole > 0.0, is_smt=hole == 0.0,
    )


def _via(x, y, net, drill=0.3, layers=(1, 32)):
    return RawVia(
        center=Pt2D(x, y), diameter_mm=drill * 2, hole_diameter_mm=drill,
        layer_start=layers[0], layer_end=layers[1], net_index=net,
    )


def _comp(designator, x=0.0, y=0.0, source=None, layer="TOP", params=None):
    return RawPcbComponent(
        designator=designator, center=Pt2D(x, y), rotation_deg=0.0,
        layer_name=layer, footprint="0402",
        source_designator=source if source is not None else designator,
        parameters=params or {},
    )


def _proj(**overrides) -> ExtractedProject:
    base = {
        "prjpcb_path": Path("t.PrjPcb"),
        "pcbdoc_path": Path("t.PcbDoc"),
        "tracks": (), "arcs": (), "vias": (), "pads": (), "regions": (),
        "shape_based_regions": (), "fills": (), "pcb_components": (),
        "nets": tuple(RawNet(n) for n in NET_NAMES),
        "stackup": _stackup(), "sch_components": (),
        "compiled_netlist": None,
        # An outline is what lets build_net_layer_shapes flood the two
        # internal planes, so callers that build real geometry (rather than
        # passing _plane_shapes()) still get a reference cavity.
        "board_outline": (Pt2D(-10.0, -10.0), Pt2D(10.0, -10.0),
                          Pt2D(10.0, 10.0), Pt2D(-10.0, 10.0)),
    }
    base.update(overrides)
    return ExtractedProject(**base)


def _plane_shapes():
    """Rail copper on the PWR plane, return copper on the GND plane."""
    sheet = shapely.geometry.box(-10.0, -10.0, 10.0, 10.0)
    return {(39, GND): sheet, (40, PWR): sheet}


RAILS = {"+3V3": ["+3V3"], "GND": ["GND"]}


def _standard_cap_project(**overrides):
    """C1 across +3V3/GND with a 2-via escape cluster per side."""
    base = {
        "pcb_components": (_comp("C1"),),
        "pads": (_pad(-0.5, 0.0, PWR), _pad(0.5, 0.0, GND)),
        "vias": (
            _via(-0.9, 0.0, PWR), _via(-1.05, 0.0, PWR),
            _via(0.9, 0.0, GND), _via(1.05, 0.0, GND),
        ),
    }
    base.update(overrides)
    return _proj(**base)


def _identify(proj, **kwargs):
    kwargs.setdefault("net_layer_shapes", _plane_shapes())
    return identify_capacitors(proj, RAILS, **kwargs)


# --- detection -------------------------------------------------------------


def test_package_is_classified_from_the_footprint():
    import dataclasses
    caps = _identify(_standard_cap_project())
    assert caps[0].package == "0402"          # _comp()'s footprint

    proj = _standard_cap_project(
        pcb_components=(dataclasses.replace(_comp("C1"),
                                            footprint="FP-TCJD-MFG"),))
    # A tantalum brick: no case code, so the impedance model needs an override.
    assert _identify(proj)[0].package is None


def test_detects_decoupling_cap_and_orientation():
    caps = _identify(_standard_cap_project())
    assert len(caps) == 1
    cap = caps[0]
    assert cap.designator == "C1"
    assert cap.rail_net == "+3V3" and cap.return_net == "GND"
    assert cap.rail_group == "+3V3"
    assert cap.included
    assert len(cap.vias_rail) == 2 and len(cap.vias_return) == 2
    assert not has_flag(cap.flags, "single-via") and not has_flag(cap.flags, "no-escape-via")


def test_skips_non_cap_designators():
    proj = _standard_cap_project(
        pcb_components=(_comp("R1"), _comp("CN1"), _comp("CONN2")))
    assert _identify(proj) == []


def test_skips_component_with_three_nets():
    proj = _standard_cap_project(
        pads=(_pad(-0.5, 0.0, PWR), _pad(0.5, 0.0, GND),
              _pad(0.0, 0.5, SIG)))
    assert _identify(proj) == []


def test_skips_cap_on_signal_net():
    # AC-coupling cap SPI_CLK ↔ GND: one side isn't a rail.
    proj = _standard_cap_project(
        pads=(_pad(-0.5, 0.0, SIG), _pad(0.5, 0.0, GND)))
    assert _identify(proj) == []


def test_force_include_override_admits_signal_cap():
    proj = _standard_cap_project(
        pads=(_pad(-0.5, 0.0, SIG), _pad(0.5, 0.0, GND)))
    caps = _identify(proj, include_overrides={"C1": True})
    assert len(caps) == 1
    assert caps[0].rail_net == "SPI_CLK" and caps[0].return_net == "GND"
    assert caps[0].included
    # Only the override admitted it — the GUI must not clear that override
    # when the user re-checks the box.
    assert not caps[0].auto_detected


def test_exclude_override_keeps_cap_listed_but_not_included():
    caps = _identify(_standard_cap_project(),
                     include_overrides={"C1": False})
    assert len(caps) == 1 and not caps[0].included
    # Detection found it on its own; the exclude is the deviation.
    assert caps[0].auto_detected


def test_gnd_counts_as_rail_without_annotation():
    # No GND rail group at all — the GND-alias fallback still classifies it.
    caps = identify_capacitors(
        _standard_cap_project(), {"+3V3": ["+3V3"]},
        net_layer_shapes=_plane_shapes())
    assert len(caps) == 1 and caps[0].return_net == "GND"


# --- escape-via association -------------------------------------------------


def test_escape_cluster_rejects_stitching_field():
    settings = CapLoopSettings()
    proj = _standard_cap_project()
    pads = [proj.pads[0]]  # rail pad at (-0.5, 0)
    proj = _proj(vias=(
        _via(-0.9, 0.0, PWR),          # dist 0.4 — cluster seed
        _via(-1.05, 0.0, PWR),         # dist 0.55 ≤ 0.4*1.5 — kept
        _via(-3.0, 0.0, PWR),          # dist 2.5 — stitching, rejected
        _via(-0.8, 0.0, GND),          # wrong net — never a candidate
    ))
    escapes = associate_escape_vias(pads, proj, {PWR}, [1, 39, 40, 32],
                                    settings)
    assert [e.via_index for e in escapes] == [0, 1]
    assert escapes[0].dist_mm == pytest.approx(0.4)
    assert escapes[0].span == (1, 39, 40, 32)


def test_long_escape_within_max_dist_is_kept_and_flagged():
    proj = _standard_cap_project(vias=(
        _via(-2.0, 0.0, PWR),   # rail: dist 1.5 > warn 1.0, ≤ max 2.0
        _via(0.9, 0.0, GND), _via(1.05, 0.0, GND),
    ))
    caps = _identify(proj)
    assert len(caps[0].vias_rail) == 1
    assert has_flag(caps[0].flags, "long-escape")


def test_distant_via_kept_as_only_path_when_nothing_local():
    proj = _standard_cap_project(vias=(
        _via(-3.0, 0.0, PWR),   # rail: dist 2.5 > max 2.0, ≤ search 3.0
        _via(0.9, 0.0, GND), _via(1.05, 0.0, GND),
    ))
    caps = _identify(proj)
    assert len(caps[0].vias_rail) == 1
    assert caps[0].vias_rail[0].dist_mm == pytest.approx(2.5)
    assert has_flag(caps[0].flags, "long-escape")


def test_no_escape_via_flag_names_the_offending_pad():
    """A capacitor can escape cleanly on one pad and not at all on the other.
    The flag must say which, or the user has no idea which pad to go and look
    at."""
    proj = _standard_cap_project(vias=(
        _via(0.9, 0.0, GND), _via(1.05, 0.0, GND),
    ))
    caps = _identify(proj)
    assert caps[0].vias_rail == ()
    assert has_flag(caps[0].flags, "no-escape-via")
    # The rail pad is the stranded one; the GND pad has two vias.
    assert "no-escape-via (+3V3)" in caps[0].flags


def test_no_escape_via_names_both_pads_when_both_are_stranded():
    caps = _identify(_standard_cap_project(vias=()))
    assert "no-escape-via (+3V3, GND)" in caps[0].flags


def test_single_via_flag_names_the_offending_pad():
    proj = _standard_cap_project(vias=(
        _via(-0.9, 0.0, PWR),                             # rail: one via
        _via(0.9, 0.0, GND), _via(1.05, 0.0, GND),        # return: two
    ))
    assert "single-via (+3V3)" in _identify(proj)[0].flags


def test_long_escape_flag_names_the_offending_pad():
    proj = _standard_cap_project(vias=(
        _via(-2.0, 0.0, PWR),                             # rail: 1.5 mm out
        _via(0.9, 0.0, GND), _via(1.05, 0.0, GND),        # return: adjacent
    ))
    flags = _identify(proj)[0].flags
    assert "long-escape (+3V3)" in flags
    long_flag = next(f for f in flags if f.startswith("long-escape"))
    assert "GND" not in long_flag


def test_has_flag_matches_the_base_token_only():
    flags = ("no-escape-via (+3V3, GND)", "no-cavity")
    assert has_flag(flags, "no-escape-via")
    assert has_flag(flags, "no-cavity")
    assert not has_flag(flags, "no-target")
    # A shared prefix is not a match.
    assert not has_flag(("single-via (GND)",), "single")


def test_through_hole_pad_is_its_own_escape():
    proj = _standard_cap_project(
        pads=(_pad(-0.5, 0.0, PWR, hole=0.5), _pad(0.5, 0.0, GND)),
        vias=(_via(0.9, 0.0, GND), _via(1.05, 0.0, GND)),
    )
    caps = _identify(proj)
    rail = caps[0].vias_rail
    assert len(rail) == 1 and rail[0].is_pad_hole
    assert rail[0].dist_mm == 0.0
    assert rail[0].drill_mm == pytest.approx(0.5)
    assert not has_flag(caps[0].flags, "no-escape-via")


def test_single_via_flag():
    proj = _standard_cap_project(vias=(
        _via(-0.9, 0.0, PWR),
        _via(0.9, 0.0, GND), _via(1.05, 0.0, GND),
    ))
    caps = _identify(proj)
    assert has_flag(caps[0].flags, "single-via")


def test_escape_is_measured_from_the_pad_edge_not_its_centre():
    """A via inside the pad escapes in 0 mm. Measuring from the pad centre
    would charge a via-in-pad ~half the pad length of escape run — and on a
    1206 land that is most of a nanohenry of fictional inductance."""
    proj = _standard_cap_project(vias=(
        _via(-0.5, 0.0, PWR),          # dead centre of the rail pad
        _via(0.9, 0.0, GND), _via(1.05, 0.0, GND),
    ))
    caps = _identify(proj)
    rail = caps[0].vias_rail[0]
    assert rail.dist_mm == pytest.approx(0.0)
    assert rail.escape_mm == pytest.approx(0.0)


def test_via_just_outside_a_pad_has_a_small_escape():
    # Rail pad spans x ∈ [-0.8, -0.2]; a via at -1.0 is 0.2 mm past the edge
    # but 0.5 mm from the centre.
    proj = _standard_cap_project(vias=(
        _via(-1.0, 0.0, PWR),
        _via(0.9, 0.0, GND), _via(1.05, 0.0, GND),
    ))
    rail = _identify(proj)[0].vias_rail[0]
    assert rail.dist_mm == pytest.approx(0.5)
    assert rail.escape_mm == pytest.approx(0.2, abs=0.02)


def test_via_in_pad_never_raises_long_escape():
    proj = _standard_cap_project(
        pads=(_pad(-0.5, 0.0, PWR), _pad(0.5, 0.0, GND)),
        vias=(_via(-0.5, 0.0, PWR), _via(0.5, 0.0, GND)),
    )
    caps = _identify(proj)
    assert not has_flag(caps[0].flags, "long-escape")


def test_buried_via_that_misses_the_mounting_layer_is_not_an_escape():
    """A via whose barrel never reaches the pad's layer cannot carry that
    pad's current, however close it looks from above. Buried and far-side
    vias routinely sit under a pad on the opposite face; charging the cap a
    board-thickness of escape inductance through one is pure fiction."""
    proj = _standard_cap_project(vias=(
        # Same net and right next to the rail pad, but spans L39–L40 only.
        _via(-0.6, 0.0, PWR, layers=(39, 40)),
        _via(0.9, 0.0, GND), _via(1.05, 0.0, GND),
    ))
    caps = _identify(proj)
    assert caps[0].vias_rail == ()
    assert has_flag(caps[0].flags, "no-escape-via")


def test_bottom_mounted_cap_ignores_top_only_vias():
    proj = _standard_cap_project(
        pcb_components=(_comp("C1", layer="BOTTOM"),),
        vias=(_via(-0.9, 0.0, PWR, layers=(1, 39)),      # top-side blind via
              _via(-0.95, 0.0, PWR, layers=(40, 32)),    # reaches Bottom
              _via(0.9, 0.0, GND, layers=(40, 32))),
    )
    caps = _identify(proj)
    assert caps[0].mount_layer_id == 32
    assert [v.via_index for v in caps[0].vias_rail] == [1]


def test_stacked_via_chain_extends_cavity_reach_without_adding_a_pair():
    """A continuation via (pad → L39, then L39 → L40) is a *series* barrel.
    It must not join the escape cluster — that would inflate the parallel
    pair count and understate the loop — but the plane it reaches must still
    be visible to cavity selection."""
    from fypa.caploop.identify import expand_reachable_layers

    proj = _standard_cap_project(vias=(
        _via(-0.9, 0.0, PWR, layers=(1, 39)),     # direct escape, stops at 39
        _via(-1.1, 0.0, PWR, layers=(39, 40)),    # continuation 39 → 40
        _via(0.9, 0.0, GND, layers=(1, 39)),
    ))
    caps = _identify(proj)
    cap = caps[0]
    # Only the direct via is an escape.
    assert [v.via_index for v in cap.vias_rail] == [0]
    assert cap.pad_width_rail_mm > 0.0

    reach = expand_reachable_layers(
        cap.vias_rail, proj, {PWR}, proj.enabled_copper_layer_ids(),
        CapLoopSettings())
    assert 40 in reach          # the continuation was followed
    # …and the cavity uses the plane only the chain could reach.
    assert cap.cavity is not None
    assert cap.cavity.layer_rail == 40


def test_cavity_prefers_the_pair_nearest_the_mounting_surface():
    """Ranking by dielectric gap alone once chose a plane pair on the far
    side of a 16-layer board because its dielectric was 15 µm thinner. The
    loop closes through the *nearest* reachable pair."""
    near = shapely.geometry.box(-10.0, -10.0, 10.0, 10.0)
    # Add a tighter-but-deeper pair: rail on 32, return on 40 is not it —
    # instead make the shallow pair (39/40) slightly looser than a deep one.
    caps = _identify(_standard_cap_project(),
                     net_layer_shapes={(39, GND): near, (40, PWR): near,
                                       (32, GND): near})
    cav = caps[0].cavity
    assert cav is not None
    # Top-mounted: the 39/40 pair sits directly beneath; 40/32 is deeper.
    assert (cav.layer_rail, cav.layer_return) == (40, 39)
    assert cav.depth_mm < 0.3


# --- cavity selection ---------------------------------------------------------


def test_cavity_selects_facing_plane_pair():
    caps = _identify(_standard_cap_project())
    cav = caps[0].cavity
    assert cav is not None
    assert (cav.layer_rail, cav.layer_return) == (40, 39)
    # Gap between the two plane copper faces = the 0.3 mm dielectric.
    assert cav.h_cav_mm == pytest.approx(0.3)
    assert cav.both_planes
    # Nearer plane (GND at z=0.2525) is within the 0.4 mm warn depth.
    assert "far-plane" not in caps[0].flags


def test_no_cavity_without_shapes():
    caps = _identify(_standard_cap_project(), net_layer_shapes=None)
    assert caps[0].cavity is None
    assert "no-cavity" in caps[0].flags


def test_no_cavity_when_copper_absent_at_cap():
    # Plane copper exists but 20 mm away from the cap's via cluster.
    far = shapely.geometry.box(20.0, 20.0, 40.0, 40.0)
    caps = _identify(_standard_cap_project(),
                     net_layer_shapes={(39, GND): far, (40, PWR): far})
    assert caps[0].cavity is None
    assert "no-cavity" in caps[0].flags


def test_mounting_layer_is_never_the_reference_plane():
    """The cap's own pads sit on the mounting layer. Letting that layer
    qualify would collapse the cavity onto the pads themselves — whose
    'sheet' is two disjoint islands with no return path between them."""
    shapes = _plane_shapes()
    # Big top-layer copper on both nets, right under the cap.
    shapes[(1, PWR)] = shapely.geometry.box(-10.0, -10.0, 10.0, 10.0)
    shapes[(1, GND)] = shapely.geometry.box(-10.0, -10.0, 10.0, 10.0)
    cav = _identify(_standard_cap_project(), net_layer_shapes=shapes)[0].cavity
    assert cav is not None
    assert 1 not in (cav.layer_rail, cav.layer_return)
    assert (cav.layer_rail, cav.layer_return) == (40, 39)


def test_a_pad_sized_copper_island_is_not_a_reference_plane():
    """Sheet-likeness, not mere touching: a tiny island of the net's copper
    on an inner layer must not be mistaken for a plane."""
    island_pwr = shapely.geometry.box(-1.2, -0.3, -0.7, 0.3)   # ~0.3 mm²
    caps = _identify(_standard_cap_project(),
                     net_layer_shapes={(40, PWR): island_pwr,
                                       (39, GND): shapely.geometry.box(
                                           -10.0, -10.0, 10.0, 10.0)})
    assert caps[0].cavity is None
    assert "no-cavity" in caps[0].flags


# --- part-value parsing ---------------------------------------------------------


@pytest.mark.parametrize("params,expect_c,expect_v", [
    ({"Capacitance": "100nF"}, 1e-7, None),
    ({"Value": "0.1uF/16V"}, 1e-7, 16.0),
    ({"Comment": "CAP CER 100NF 25V X7R 0402"}, 1e-7, 25.0),
    ({"Comment": "4u7", "Voltage": "6V3"}, 4.7e-6, 6.3),
    ({"Value": "10k"}, None, None),          # a resistor value — out of band
    ({"Comment": "0.1"}, None, None),        # bare number — unit-ambiguous
    ({"Comment": "DNP"}, None, None),
    ({}, None, None),
])
def test_parse_cap_params(params, expect_c, expect_v):
    comp = _comp("C1", params=params)
    c, v = parse_cap_params(comp, None)
    if expect_c is None:
        assert c is None
    else:
        assert c == pytest.approx(expect_c)
    if expect_v is None:
        assert v is None
    else:
        assert v == pytest.approx(expect_v)


def test_parse_cap_params_prefers_schematic_params():
    comp = _comp("C1", params={"Value": "1uF"})
    c, _ = parse_cap_params(comp, {"Capacitance": "100nF"})
    assert c == pytest.approx(1e-7)


# --- metadata lookups -------------------------------------------------------------


def _directives():
    def term(net, *pins):
        return {
            "pins": [
                {"pad": p, "layer_id": 1, "net": net,
                 "x_mm": 5.0, "y_mm": 5.0}
                for p in pins
            ],
            "requested_net": net,
        }
    return [
        {"role": "SOURCE", "label": "VR1", "value": 3.3,
         "terminals": {"P": term("+3V3", "1"), "N": term("GND", "2")}},
        {"role": "SINK", "label": "U5", "value": 1.5,
         "terminals": {"P": term("+3V3", "A1", "A2"), "N": term("GND", "B1")}},
        {"role": "SINK", "label": "U9", "value": 0.2,
         "terminals": {"P": term("+3V3", "C1"), "N": term("GND", "C2")}},
    ]


def test_design_voltage_from_source():
    assert design_voltage_for_rail({"+3V3"}, _directives()) == \
        pytest.approx(3.3)
    assert design_voltage_for_rail({"+5V"}, _directives()) is None


def test_default_target_is_largest_current_sink():
    label, p_pins, n_pins = default_target_for_rail({"+3V3"}, _directives())
    assert label == "U5"
    assert [p["pad"] for p in p_pins] == ["A1", "A2"]
    # The return pins locate the IC's ground vias for the Tier-3 loop.
    assert [p["pad"] for p in n_pins] == ["B1"]


def test_identify_wires_metadata_and_target_override():
    caps = _identify(_standard_cap_project(),
                     metadata_directives=_directives())
    cap = caps[0]
    assert cap.design_voltage_v == pytest.approx(3.3)
    assert cap.target_label == "U5" and not cap.target_is_override
    assert "no-target" not in cap.flags

    caps = _identify(_standard_cap_project(),
                     metadata_directives=_directives(),
                     target_overrides={"C1": "U9"})
    cap = caps[0]
    assert cap.target_label == "U9" and cap.target_is_override
    assert [p["pad"] for p in cap.target_pins] == ["C1"]


def test_no_target_flag_without_directives():
    caps = _identify(_standard_cap_project())
    assert caps[0].target_label is None
    assert "no-target" in caps[0].flags


# --- stackup Dk extraction ----------------------------------------------------------


def _v9(name, idx, cu=0.0, h=0.0, dk=0.0, df=0.0):
    return SimpleNamespace(
        name=name, stack_index=idx, copper_thickness=cu,
        diel_constant=dk, diel_loss_tangent=df, diel_height=h,
        is_copper=cu > 0.0, is_dielectric=h > 0.0,
    )


def test_v9_dielectric_gaps_weighted_average():
    pcb = SimpleNamespace(board=SimpleNamespace(v9_stack=[
        _v9("Top Layer", 0, cu=1.4),
        _v9("Dielectric 1", 1, h=10.0, dk=4.2, df=0.02),
        _v9("Dielectric 2", 2, h=30.0, dk=4.6, df=0.02),
        _v9("Bottom Layer", 3, cu=1.4),
    ]))
    gaps = _v9_dielectric_gaps(pcb)
    dk, df = gaps["top layer"]
    assert dk == pytest.approx((4.2 * 10 + 4.6 * 30) / 40)
    assert df == pytest.approx(0.02)
    # Bottom copper has no gap below it — no data, not a crash.
    assert gaps["bottom layer"] == (None, None)


def test_v9_dielectric_gaps_missing_dk_yields_none():
    pcb = SimpleNamespace(board=SimpleNamespace(v9_stack=[
        _v9("Top Layer", 0, cu=1.4),
        _v9("Dielectric 1", 1, h=10.0),   # Dk not stored (0.0)
        _v9("Bottom Layer", 2, cu=1.4),
    ]))
    assert _v9_dielectric_gaps(pcb)["top layer"] == (None, None)


def test_v9_dielectric_gaps_no_stack():
    pcb = SimpleNamespace(board=SimpleNamespace(v9_stack=[]))
    assert _v9_dielectric_gaps(pcb) == {}
