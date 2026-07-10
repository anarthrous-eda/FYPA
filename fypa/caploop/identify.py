"""Decoupling-capacitor identification and per-cap geometry bundling.

Finds every decoupling capacitor (a two-terminal C-designator component
connected across two power rails, where GND counts as a rail), associates
each side's escape vias, selects the reference plane-pair cavity, and parses
the informational part values — everything the tier engines and the
Capacitors tab consume.

Detection is heuristic (designator prefix ``C`` + digit, exactly two nets,
both rail-grouped) with per-cap user overrides layered on top: an exclude
drops a detected cap from the analysis (kept in the list, ``included=False``,
so the GUI can show it greyed out), an include forces a structurally-valid
cap through even when its nets aren't in any annotated rail group.
"""

from __future__ import annotations

import dataclasses
import math
import re
from dataclasses import dataclass

import shapely.geometry
import shapely.geometry.base

from fypa.altium.annotations import parse_si_value
from fypa.altium.extract import (
    NO_NET,
    ExtractedProject,
    RawPad,
    RawPcbComponent,
)
from fypa.altium.loader import _layer_z_centers_mm, _via_pad_layer_span
from fypa.altium_geometry import _pad_polygon
from fypa.caploop.constants import CapLoopSettings
from fypa.caploop.packages import detect_package
from fypa.topology.net_aliases import is_gnd_alias

# Altium layer ids (see fypa.altium_geometry / fypa.altium.annotations).
MULTI_LAYER_PAD_LAYER_ID: int = 74
TOP_LAYER_ID: int = 1
BOTTOM_LAYER_ID: int = 32

_CAP_DESIGNATOR_RE = re.compile(r"^C\d", re.IGNORECASE)

# Parameter keys tried, in order, for the informational part values. Altium
# libraries are conventions, not schemas — miss on all of them just leaves
# the table cell blank.
_CAPACITANCE_KEYS = ("Capacitance", "Value", "Val", "Comment")
_VOLTAGE_KEYS = ("Voltage", "Voltage Rating", "Rated Voltage",
                 "VoltageRating", "VRating")

# Plausibility bands: a token that parses outside these is some other
# quantity (a resistance, a percentage, a temperature grade), not the value
# we're after.
_CAPACITANCE_BAND_F = (1e-13, 1.0)
_VOLTAGE_BAND_V = (1.0, 1000.0)

_TOKEN_SPLIT_RE = re.compile(r"[\s/,;|]+")


@dataclass(frozen=True, slots=True)
class EscapeVia:
    """One via (or through-hole pad bore) carrying a cap pad's current into
    the stack. ``via_index`` indexes ``extracted.vias``; ``-1`` marks a
    through-hole pad acting as its own escape.

    Two distances, because they answer different questions. ``dist_mm`` (to
    the nearest pad *centre*) drives the search and cluster window — it is
    the stable "is this via near this pad" measure. ``escape_mm`` (to the
    nearest pad *boundary*, zero when the via lands inside the pad) is the
    physical length of the escape run, and is what the inductance model and
    the long-escape flag use. On a 1206 pad the two differ by ~0.8 mm, so
    using the centre distance would charge a via-in-pad for an escape it
    never makes.
    """
    via_index: int
    x_mm: float
    y_mm: float
    dist_mm: float            # to the nearest pad centre of its side
    escape_mm: float          # to the nearest pad edge (0 = via in pad)
    drill_mm: float
    span: tuple[int, ...]     # enabled copper layer ids the barrel bridges
    is_pad_hole: bool = False


@dataclass(frozen=True, slots=True)
class CavityRef:
    """The reference plane pair (rail copper on one layer, return copper on
    another) the cap's loop closes through. Z values are copper-layer centres
    from :func:`fypa.altium.loader._layer_z_centers_mm` (origin at the top
    copper surface, growing downward)."""
    layer_rail: int
    layer_return: int
    name_rail: str
    name_return: str
    z_rail_mm: float
    z_return_mm: float
    h_cav_mm: float           # dielectric gap between the two copper faces
    depth_mm: float           # mounting surface → nearer cavity layer centre
    both_planes: bool         # both layers are Altium internal planes


@dataclass(frozen=True, slots=True)
class CapInstance:
    """One decoupling capacitor with everything the tier engines need."""
    component_index: int
    designator: str           # physical (PCB) designator — the override key
    source_designator: str
    footprint: str
    # Imperial SMD case code parsed from the footprint ("0402"), or None for a
    # part the package library can't classify — a tantalum brick, an
    # electrolytic. The impedance model needs an explicit per-part ESL/ESR
    # override for those.
    package: str | None
    center_xy: tuple[float, float]
    mount_layer_id: int       # TOP_LAYER_ID or BOTTOM_LAYER_ID
    rail_net: str             # power side (display rail)
    return_net: str           # GND-ish side
    rail_group: str           # rail-group primary name (rail filter key)
    pads_rail: tuple[int, ...]      # indices into extracted.pads
    pads_return: tuple[int, ...]
    # Current-carrying width of each side's pad (its narrow dimension) — the
    # escape term's ``w``. Captured here so the tier engines never need the
    # ExtractedProject.
    pad_width_rail_mm: float
    pad_width_return_mm: float
    vias_rail: tuple[EscapeVia, ...]
    vias_return: tuple[EscapeVia, ...]
    cavity: CavityRef | None
    capacitance_f: float | None
    voltage_rating_v: float | None
    design_voltage_v: float | None
    target_label: str | None        # directive label ("U5" / "U5#1")
    target_pins: tuple[dict, ...]   # metadata pin dicts of the target's P terminal
    target_pins_n: tuple[dict, ...]  # …and its return terminal (Tier-3 IC loop)
    target_is_override: bool
    flags: tuple[str, ...]
    included: bool = True
    # True when detection alone (two rail-grouped nets) would have found
    # this cap — False when only a force-include override admitted it. The
    # GUI needs the distinction: clearing the override on a forced cap
    # removes it from the analysis entirely, so re-checking its box must
    # write ``include=True``, not ``None``.
    auto_detected: bool = True


# --- part-value parsing ---------------------------------------------------

def _ci_param(params: dict[str, str], key: str) -> str | None:
    """Case-insensitive, whitespace-trimmed parameter lookup (Altium sheets
    routinely carry stray spaces on names and values)."""
    key_l = key.strip().lower()
    for k, v in params.items():
        if str(k).strip().lower() == key_l:
            text = str(v).strip()
            return text or None
    return None


def _parse_banded_tokens(
    text: str,
    band: tuple[float, float],
    require_char: str | None = None,
    exclude_char: str | None = None,
) -> float | None:
    """First token of ``text`` that parses inside ``band``.

    Tokens are split on whitespace and common separators so composite value
    strings ("0.1uF/16V", "CAP CER 100NF 25V X7R") yield their parts. A token
    must contain a letter (an SI prefix or unit) — a bare number like "0.1"
    is unit-ambiguous (µF by convention, F by SI) and is skipped rather than
    guessed. ``require_char`` restricts to tokens containing that character
    (only "16V"-style tokens count as voltages); ``exclude_char`` drops
    tokens containing it (a "0.5V" token must not be read as 0.5 F).

    SI prefixes are case-sensitive ("n" is nano, "N" is nothing), but Altium
    comments are frequently upper-cased ("100NF") — when the literal token
    doesn't land in band, retry lower-cased. The band makes this safe: the
    only case-folding hazard is M(ega)→m(illi), and no plausible band
    contains both readings of the same token.
    """
    lo, hi = band
    for token in _TOKEN_SPLIT_RE.split(text):
        if not token or not any(c.isalpha() for c in token):
            continue
        token_l = token.lower()
        if require_char is not None and require_char not in token_l:
            continue
        if exclude_char is not None and exclude_char in token_l:
            continue
        for candidate in (token, token_l):
            try:
                value = parse_si_value(candidate)
            except ValueError:
                continue
            if lo <= value <= hi:
                return value
    return None


def parse_cap_params(
    pcb_comp: RawPcbComponent,
    sch_params: dict[str, str] | None,
) -> tuple[float | None, float | None]:
    """Heuristic (capacitance_F, voltage_rating_V) from component parameters.

    Informational columns only — never inputs to the inductance math — so
    unparseable values return ``None`` (blank cell), not an error.
    """
    param_dicts = [d for d in (sch_params, pcb_comp.parameters) if d]

    capacitance = None
    for params in param_dicts:
        for key in _CAPACITANCE_KEYS:
            text = _ci_param(params, key)
            if text is None:
                continue
            capacitance = _parse_banded_tokens(
                text, _CAPACITANCE_BAND_F, exclude_char="v")
            if capacitance is not None:
                break
        if capacitance is not None:
            break

    voltage = None
    for params in param_dicts:
        for key in _VOLTAGE_KEYS + _CAPACITANCE_KEYS:
            text = _ci_param(params, key)
            if text is None:
                continue
            voltage = _parse_banded_tokens(
                text, _VOLTAGE_BAND_V, require_char="v")
            if voltage is not None:
                break
        if voltage is not None:
            break

    return capacitance, voltage


# --- escape-via association -------------------------------------------------

def associate_escape_vias(
    pads: list[RawPad],
    extracted: ExtractedProject,
    side_net_indices: set[int],
    enabled_layers: list[int],
    settings: CapLoopSettings,
    mount_layer_id: int | None = None,
) -> tuple[EscapeVia, ...]:
    """Escape vias serving one side (one net group) of a capacitor.

    Distance-threshold clustering: candidate vias share the side's net group
    and lie within ``escape_via_search_mm`` of a pad centre. The cluster
    keeps candidates within ``escape_via_max_dist_mm`` AND within
    ``escape_cluster_slack ×`` the nearest candidate's distance (floored at
    a pad-adjacent 0.3 mm so a via-in-pad at distance 0 doesn't collapse the
    window) — the slack window is what rejects a stitching field that
    happens to fall inside the search radius. With nothing inside the tight
    radius, the single nearest candidate within the search radius is used
    (the caller flags it as a long escape). Through-hole pads are their own
    escape at distance 0.

    ``mount_layer_id`` is the layer the pads sit on. A via whose barrel does
    not reach that layer cannot take current off the pad, however close it
    looks from above — buried and far-side vias routinely sit within a
    millimetre of a pad on the opposite face. Without this filter a
    bottom-mounted cap happily "escapes" through a layer 2–15 buried via and
    is charged the full board thickness of escape inductance. Only *direct*
    escapes are returned; a stacked continuation via (pad → L15 → L3) is not
    a parallel barrel and is found instead by :func:`expand_reachable_layers`.

    Trace-following was considered and rejected: at sub-3 mm length scales it
    duplicates the geometry-union work for marginal accuracy.
    """
    escapes: list[EscapeVia] = []
    for pad in pads:
        if pad.hole_mm > 0.0 and pad.is_plated and (
                pad.is_through_hole
                or pad.layer_id == MULTI_LAYER_PAD_LAYER_ID):
            escapes.append(EscapeVia(
                via_index=-1,
                x_mm=pad.center.x, y_mm=pad.center.y,
                dist_mm=0.0, escape_mm=0.0,
                drill_mm=pad.hole_mm,
                span=tuple(enabled_layers),
                is_pad_hole=True,
            ))

    # Pad outlines, for the edge distance. Falls back to the pad's inscribed
    # radius when the polygon can't be built, so a missing outline degrades
    # to a slightly conservative escape length rather than crashing.
    pad_polys = []
    for pad in pads:
        try:
            poly = _pad_polygon(pad, pad.layer_id)
        except Exception:  # pragma: no cover — defensive
            poly = None
        pad_polys.append(poly)

    def _edge_distance(x: float, y: float) -> float:
        point = shapely.geometry.Point(x, y)
        best = math.inf
        for pad, poly in zip(pads, pad_polys):
            if poly is not None and not poly.is_empty:
                best = min(best, poly.distance(point))
            else:
                inscribed = 0.5 * min(pad.width_mm, pad.height_mm)
                centre = math.hypot(x - pad.center.x, y - pad.center.y)
                best = min(best, max(0.0, centre - inscribed))
        return 0.0 if best is math.inf else best

    candidates: list[EscapeVia] = []
    for vi, v in enumerate(extracted.vias):
        if v.net_index not in side_net_indices:
            continue
        dist = min(
            (math.hypot(v.center.x - p.center.x, v.center.y - p.center.y)
             for p in pads),
            default=math.inf,
        )
        if dist > settings.escape_via_search_mm:
            continue
        span = tuple(_via_pad_layer_span(
            v.layer_start, v.layer_end, enabled_layers))
        if mount_layer_id is not None and mount_layer_id not in span:
            continue
        candidates.append(EscapeVia(
            via_index=vi,
            x_mm=v.center.x, y_mm=v.center.y,
            dist_mm=dist,
            escape_mm=_edge_distance(v.center.x, v.center.y),
            drill_mm=v.hole_diameter_mm,
            span=span,
        ))

    if candidates:
        candidates.sort(key=lambda e: e.dist_mm)
        nearest = min(candidates[0].dist_mm,
                      escapes[0].dist_mm if escapes else math.inf)
        if nearest <= settings.escape_via_max_dist_mm:
            window = max(nearest, 0.3) * settings.escape_cluster_slack
            escapes.extend(
                e for e in candidates
                if e.dist_mm <= settings.escape_via_max_dist_mm
                and e.dist_mm <= window
            )
        elif not escapes:
            # Nothing local — keep the single nearest so the model has a
            # current path at all; the caller raises "long-escape".
            escapes.append(candidates[0])

    escapes.sort(key=lambda e: e.dist_mm)
    return tuple(escapes)


# --- reference-cavity selection ----------------------------------------------

def expand_reachable_layers(
    direct: tuple[EscapeVia, ...],
    extracted: ExtractedProject,
    side_net_indices: set[int],
    enabled_layers: list[int],
    settings: CapLoopSettings,
    max_hops: int = 3,
) -> frozenset[int]:
    """Copper layers this side's current can reach from its escape vias.

    A direct escape via may only get part-way down the stack; boards route
    the rest with a stacked or staggered continuation (pad → L15 via one via,
    L15 → L3 via another). Those continuations are *series* barrels, not
    parallel escape paths, so they must not join the escape cluster (they
    would inflate the parallel-pair count and understate the loop). They do,
    however, decide which reference planes the capacitor can actually see.

    Walks same-net vias near the cluster whose span overlaps the layers
    already reached, unioning their spans, until nothing new is found.
    """
    if not direct:
        return frozenset()
    reach: set[int] = set()
    for e in direct:
        reach.update(e.span)
    cx = sum(e.x_mm for e in direct) / len(direct)
    cy = sum(e.y_mm for e in direct) / len(direct)

    nearby: list[tuple[int, ...]] = []
    for v in extracted.vias:
        if v.net_index not in side_net_indices:
            continue
        if math.hypot(v.center.x - cx, v.center.y - cy) \
                > settings.escape_via_search_mm:
            continue
        nearby.append(tuple(_via_pad_layer_span(
            v.layer_start, v.layer_end, enabled_layers)))

    for _ in range(max_hops):
        grew = False
        for span in nearby:
            if reach.isdisjoint(span) or reach.issuperset(span):
                continue
            reach.update(span)
            grew = True
        if not grew:
            break
    return frozenset(reach)


def _probe_region(vias: tuple[EscapeVia, ...],
                  settings: CapLoopSettings
                  ) -> shapely.geometry.base.BaseGeometry | None:
    """A small disc around each of a side's escape vias.

    Coverage is tested against this region, not against a bare point, for
    two reasons. A plane sheet is built with ``include_vias=False``, so it
    carries an anti-pad (foreign net) or a thermal-relief air gap (same net)
    *hole* exactly where the via is — a point probe at the via would report
    "no copper here". And the midpoint between two vias of a cluster often
    falls in one of those holes too. Asking "does this net have copper within
    a via-clearance of its own via?" is the question we actually mean.
    """
    if not vias:
        return None
    margin = settings.plane_antipad_clearance_mm + 0.25
    discs = [
        shapely.geometry.Point(v.x_mm, v.y_mm).buffer(
            0.5 * max(v.drill_mm, 0.2) + margin)
        for v in vias
    ]
    return shapely.union_all(discs)


def _is_sheet_like(shape, probe_xy: tuple[float, float],
                   settings: CapLoopSettings) -> bool:
    """Does this net's copper form a *plane* here, rather than a pad or a
    trace stub?

    A reference plane must span the cap, not merely touch it. Measured as the
    fraction of a disc around the via cluster that the copper fills: an
    anti-pad-perforated plane still fills most of it, an 0402 pad fills a few
    percent. Deliberately shape-based rather than ``is_plane``-based, because
    plenty of boards build their power/ground references as pours on signal
    layers (Altium never marks those as internal planes).
    """
    r = settings.plane_probe_radius_mm
    if r <= 0.0:
        return True
    disc = shapely.geometry.Point(probe_xy).buffer(r)
    return shape.intersection(disc).area >= \
        settings.plane_probe_coverage * disc.area


def select_reference_cavity(
    vias_rail: tuple[EscapeVia, ...],
    vias_return: tuple[EscapeVia, ...],
    reach_rail: frozenset[int],
    reach_return: frozenset[int],
    probe_rail_xy: tuple[float, float],
    probe_return_xy: tuple[float, float],
    mount_layer_id: int,
    extracted: ExtractedProject,
    enabled_layers: list[int],
    z_centers: dict[int, float],
    rail_net_indices: set[int],
    return_net_indices: set[int],
    net_layer_shapes: dict[tuple[int, int],
                           shapely.geometry.base.BaseGeometry] | None,
    settings: CapLoopSettings,
) -> CavityRef | None:
    """Pick the plane pair the cap's loop closes through.

    Candidates are layer pairs (L_rail, L_return) that the side's current can
    reach (``reach_*``, which follows stacked via chains) and where the side's
    copper forms a sheet at *that side's own* via cluster (``net_layer_shapes``
    lookup — plane sheets in those shapes are already perforated with anti-pads
    and pullback by the geometry build). Probing each side at its own cluster
    matters: the midpoint between a rail via and a return via 2 mm away
    frequently lands in the other net's anti-pad, which would spuriously
    report "no cavity".

    Ranking is **depth first**, then dielectric gap, then plane-vs-pour. The
    capacitor's loop closes through the nearest plane pair it can reach, not
    through whichever pair on the board happens to be most tightly coupled —
    ranking by gap first once picked a pair on the far side of a 16-layer
    board (1.7 mm away) over the pair directly beneath the part, because its
    dielectric was 15 µm thinner. Depth is quantised to 50 µm so a hair's
    difference doesn't outrank a genuinely tighter cavity.
    """
    if net_layer_shapes is None or not z_centers:
        return None
    stackup_by_id = {s.layer_id: s for s in extracted.stackup}

    def _covered_layers(vias: tuple[EscapeVia, ...],
                        reachable: frozenset[int],
                        net_indices: set[int],
                        probe_xy: tuple[float, float]) -> list[int]:
        region = _probe_region(vias, settings)
        if region is None:
            region = shapely.geometry.Point(probe_xy).buffer(0.3)
        out = []
        for lid in reachable:
            # The cap's own mounting layer is never its reference plane: the
            # loop leaves the pads and goes *down* into the stack, and the
            # escape term already accounts for the run across the pad. Without
            # this, a cap's own pad qualifies as a "plane" and the cavity
            # collapses onto the pads themselves.
            if lid == mount_layer_id or lid not in z_centers:
                continue
            for ni in net_indices:
                shape = net_layer_shapes.get((lid, ni))
                if shape is None or shape.is_empty:
                    continue
                if shape.intersects(region) \
                        and _is_sheet_like(shape, probe_xy, settings):
                    out.append(lid)
                    break
        return out

    rail_layers = _covered_layers(vias_rail, reach_rail, rail_net_indices,
                                  probe_rail_xy)
    return_layers = _covered_layers(vias_return, reach_return,
                                    return_net_indices, probe_return_xy)
    if not rail_layers or not return_layers:
        return None

    z_mount = 0.0 if mount_layer_id == TOP_LAYER_ID \
        else max(z_centers.values())

    best: tuple | None = None
    for la in rail_layers:
        sa = stackup_by_id.get(la)
        for lb in return_layers:
            if la == lb:
                continue
            sb = stackup_by_id.get(lb)
            t_cu_a = sa.copper_thickness_mm if sa else 0.0
            t_cu_b = sb.copper_thickness_mm if sb else 0.0
            h_cav = abs(z_centers[la] - z_centers[lb]) - 0.5 * (t_cu_a + t_cu_b)
            if h_cav <= 0.0:
                continue
            plane_penalty = 2 - int(bool(sa and sa.is_plane)) \
                - int(bool(sb and sb.is_plane))
            depth = min(abs(z_centers[la] - z_mount),
                        abs(z_centers[lb] - z_mount))
            # Depth dominates (quantised, so near-ties fall through to the
            # tighter cavity); see the docstring.
            key = (round(depth / 0.05), round(h_cav, 4), plane_penalty)
            if best is None or key < best[0]:
                best = (key, la, lb, h_cav, depth,
                        plane_penalty == 0)
    if best is None:
        return None
    _, la, lb, h_cav, depth, both_planes = best
    sa = stackup_by_id.get(la)
    sb = stackup_by_id.get(lb)
    return CavityRef(
        layer_rail=la,
        layer_return=lb,
        name_rail=sa.name if sa else f"L{la}",
        name_return=sb.name if sb else f"L{lb}",
        z_rail_mm=z_centers[la],
        z_return_mm=z_centers[lb],
        h_cav_mm=h_cav,
        depth_mm=depth,
        both_planes=both_planes,
    )


# --- rail metadata lookups ----------------------------------------------------

def _terminal_nets(term: dict) -> set[str]:
    nets = {p.get("net") for p in term.get("pins", []) if p.get("net")}
    req = term.get("requested_net")
    if req:
        nets.add(req)
    return nets


def design_voltage_for_rail(
    rail_members: set[str],
    directives: list[dict] | None,
) -> float | None:
    """Rail voltage from the annotated SOURCE (or REGULATOR output) driving
    any member net of the cap's rail group."""
    if not directives:
        return None
    for want_role, term_name in (("SOURCE", "P"), ("REGULATOR", "OUT_P")):
        for d in directives:
            if d.get("role") != want_role:
                continue
            term = (d.get("terminals") or {}).get(term_name) or {}
            if _terminal_nets(term) & rail_members:
                value = d.get("value")
                if value is not None:
                    return float(value)
    return None


def _directive_pins(d: dict) -> tuple[tuple[dict, ...], tuple[dict, ...]]:
    """(power-side pins, return-side pins) of a SINK / REGULATOR directive.
    An ideal-return terminal (``PDN_NET`` with no copper N side) yields an
    empty return tuple — the IC-side via loop is then unmodellable."""
    terms = d.get("terminals") or {}
    p_term = terms.get("P") or terms.get("OUT_P") or {}
    n_term = terms.get("N") or terms.get("OUT_N") or {}
    return (tuple(p_term.get("pins", [])), tuple(n_term.get("pins", [])))


def default_target_for_rail(
    rail_members: set[str],
    directives: list[dict] | None,
) -> tuple[str | None, tuple[dict, ...], tuple[dict, ...]]:
    """The default loop endpoint: the largest-current SINK whose P terminal
    lands on the cap's rail group. Returns
    ``(label, P-terminal pins, N-terminal pins)`` — the N pins locate the
    IC's return vias for the Tier-3 loop.
    """
    if not directives:
        return None, (), ()
    best: dict | None = None
    for d in directives:
        if d.get("role") != "SINK":
            continue
        term = (d.get("terminals") or {}).get("P") or {}
        if not (_terminal_nets(term) & rail_members):
            continue
        if best is None or float(d.get("value") or 0.0) > \
                float(best.get("value") or 0.0):
            best = d
    if best is None:
        return None, (), ()
    p_pins, n_pins = _directive_pins(best)
    return best.get("label"), p_pins, n_pins


def eligible_target_labels(
    rail_members: set[str],
    directives: list[dict] | None,
) -> list[str]:
    """Directive labels a cap on this rail may target: every SINK or
    REGULATOR whose consuming terminal lands on a member net. Feeds the
    Capacitors tab's target picker."""
    out: list[str] = []
    for d in directives or []:
        if d.get("role") not in ("SINK", "REGULATOR"):
            continue
        terms = d.get("terminals") or {}
        term = terms.get("P") or terms.get("OUT_P") or {}
        label = d.get("label")
        if label and (_terminal_nets(term) & rail_members) \
                and label not in out:
            out.append(label)
    return out


def resolve_target_by_label(
    label: str,
    directives: list[dict] | None,
) -> tuple[tuple[dict, ...], tuple[dict, ...]]:
    """(power pins, return pins) of the directive with ``label``. Both empty
    when the label no longer resolves (a stale override)."""
    for d in directives or []:
        if d.get("label") == label:
            return _directive_pins(d)
    return (), ()


# --- orientation ---------------------------------------------------------------

def _rail_priority(net_name: str) -> int:
    """Lower = more rail-like (power side); higher = more return-like."""
    if is_gnd_alias(net_name):
        return 3
    if net_name.startswith("+"):
        return 0
    if net_name.upper().startswith(("VDD", "VCC", "VPWR")):
        return 1
    return 2


# --- main entry ------------------------------------------------------------------

def apply_cap_overrides(
    caps: list[CapInstance],
    rail_to_members: dict[str, list[str]],
    metadata_directives: list[dict] | None,
    include_overrides: dict[str, bool] | None = None,
    target_overrides: dict[str, str] | None = None,
) -> list[CapInstance]:
    """Re-apply the include / target overrides to an already-identified list.

    Overrides change nothing about the board: the escape vias, the reference
    cavity and the pad geometry are all identical. Re-running
    :func:`identify_capacitors` for a checkbox click would redo the copper
    coverage tests for every capacitor — seconds on a real board, on the GUI
    thread. This applies just the parts that actually moved.

    Note that a *force-include* is not applied here: admitting a capacitor
    whose nets aren't rail-grouped changes which capacitors exist at all, so
    it belongs to the identification pass and is keyed into its cache.
    """
    include_overrides = include_overrides or {}
    target_overrides = target_overrides or {}

    out: list[CapInstance] = []
    for cap in caps:
        members = set(rail_to_members.get(cap.rail_group, [cap.rail_group]))
        label = target_overrides.get(cap.designator)
        if label:
            pins, pins_n = resolve_target_by_label(label, metadata_directives)
            is_override = True
        else:
            label, pins, pins_n = default_target_for_rail(
                members, metadata_directives)
            is_override = False

        flags = tuple(f for f in cap.flags if f != "no-target")
        if label is None:
            flags += ("no-target",)

        out.append(dataclasses.replace(
            cap,
            target_label=label,
            target_pins=pins,
            target_pins_n=pins_n,
            target_is_override=is_override,
            flags=flags,
            included=include_overrides.get(cap.designator, True),
        ))
    return out


def identify_capacitors(
    extracted: ExtractedProject,
    rail_to_members: dict[str, list[str]],
    metadata_directives: list[dict] | None = None,
    settings: CapLoopSettings | None = None,
    net_layer_shapes: dict[tuple[int, int],
                           shapely.geometry.base.BaseGeometry] | None = None,
    include_overrides: dict[str, bool] | None = None,
    target_overrides: dict[str, str] | None = None,
) -> list[CapInstance]:
    """Find every decoupling capacitor and bundle its analysis geometry.

    ``rail_to_members`` is :func:`fypa.rail_groups.compute_rail_groups`
    output; ``metadata_directives`` is the solve metadata's ``directives``
    list (design voltage + default targets come from it — both stay ``None``
    without it). ``net_layer_shapes`` is
    :func:`fypa.altium_geometry.build_net_layer_shapes` output and is only
    needed for cavity selection (``None`` ⇒ every cap gets the ``no-cavity``
    flag). Overrides are keyed by physical designator: ``include_overrides``
    forces a structurally-valid cap in (``True``) or drops a detected one
    (``False``); ``target_overrides`` repoints the loop endpoint at another
    directive label.
    """
    settings = settings or CapLoopSettings()
    include_overrides = include_overrides or {}
    target_overrides = target_overrides or {}

    net_names = [n.name for n in extracted.nets]
    enabled_layers = extracted.enabled_copper_layer_ids()
    z_centers = _layer_z_centers_mm(extracted, enabled_layers)

    # net name → rail-group primary. Un-grouped GND aliases count as their
    # own rail so boards whose directives never name GND still classify
    # GND-referenced caps.
    member_to_rail: dict[str, str] = {}
    for primary, members in rail_to_members.items():
        for m in members:
            member_to_rail[m] = primary
    rail_members_by_primary = {p: set(ms) for p, ms in rail_to_members.items()}
    for name in net_names:
        if name not in member_to_rail and is_gnd_alias(name):
            member_to_rail[name] = name
            rail_members_by_primary.setdefault(name, {name})

    # Net-group member indices per net name (all nets sharing its rail group,
    # so escape vias on e.g. PGND count for a GND-side pad after a merge).
    name_to_index = {n: i for i, n in enumerate(net_names)}

    def _group_indices(net_name: str) -> set[int]:
        primary = member_to_rail.get(net_name)
        members = rail_members_by_primary.get(primary, {net_name}) \
            if primary is not None else {net_name}
        return {name_to_index[m] for m in members if m in name_to_index}

    # Pads by component (real nets only).
    pads_by_comp: dict[int, list[int]] = {}
    for pi, pad in enumerate(extracted.pads):
        if pad.component_index >= 0 and pad.net_index != NO_NET:
            pads_by_comp.setdefault(pad.component_index, []).append(pi)

    sch_params_by_designator = {
        sc.designator: sc.parameters for sc in extracted.sch_components
    }

    caps: list[CapInstance] = []
    for ci, comp in enumerate(extracted.pcb_components):
        name_for_match = comp.source_designator or comp.designator
        if not name_for_match or not _CAP_DESIGNATOR_RE.match(name_for_match):
            continue
        pad_indices = pads_by_comp.get(ci, [])
        nets_here = sorted({extracted.pads[pi].net_index for pi in pad_indices})
        if len(nets_here) != 2:
            continue

        name_a, name_b = (net_names[nets_here[0]], net_names[nets_here[1]])
        forced = include_overrides.get(comp.designator) is True
        on_rails = name_a in member_to_rail and name_b in member_to_rail
        if not on_rails and not forced:
            continue

        # Orientation: the more GND-ish side is the return.
        if _rail_priority(name_a) <= _rail_priority(name_b):
            rail_net, return_net = name_a, name_b
        else:
            rail_net, return_net = name_b, name_a
        rail_idx_set = _group_indices(rail_net)
        return_idx_set = _group_indices(return_net)
        rail_group = member_to_rail.get(rail_net, rail_net)

        pads_rail = tuple(
            pi for pi in pad_indices
            if extracted.pads[pi].net_index in rail_idx_set)
        pads_return = tuple(
            pi for pi in pad_indices
            if extracted.pads[pi].net_index in return_idx_set)

        def _pad_width(indices: tuple[int, ...]) -> float:
            """The pad's narrow dimension — the width the escape current
            crosses. Zero-size pads fall back to a plausible 0402 land."""
            widths = [min(extracted.pads[pi].width_mm,
                          extracted.pads[pi].height_mm)
                      for pi in indices]
            widths = [w for w in widths if w > 0.0]
            return sum(widths) / len(widths) if widths else 0.5

        mount_layer_id = (
            BOTTOM_LAYER_ID
            if str(comp.layer_name).upper().startswith("B")
            else TOP_LAYER_ID)

        vias_rail = associate_escape_vias(
            [extracted.pads[pi] for pi in pads_rail], extracted,
            rail_idx_set, enabled_layers, settings, mount_layer_id)
        vias_return = associate_escape_vias(
            [extracted.pads[pi] for pi in pads_return], extracted,
            return_idx_set, enabled_layers, settings, mount_layer_id)
        reach_rail = expand_reachable_layers(
            vias_rail, extracted, rail_idx_set, enabled_layers, settings)
        reach_return = expand_reachable_layers(
            vias_return, extracted, return_idx_set, enabled_layers, settings)

        def _cluster_xy(vias: tuple[EscapeVia, ...]) -> tuple[float, float]:
            if not vias:
                return (comp.center.x, comp.center.y)
            return (sum(e.x_mm for e in vias) / len(vias),
                    sum(e.y_mm for e in vias) / len(vias))

        cavity = select_reference_cavity(
            vias_rail, vias_return, reach_rail, reach_return,
            _cluster_xy(vias_rail), _cluster_xy(vias_return),
            mount_layer_id, extracted, enabled_layers, z_centers,
            rail_idx_set, return_idx_set, net_layer_shapes, settings)

        capacitance_f, voltage_rating_v = parse_cap_params(
            comp,
            sch_params_by_designator.get(comp.source_designator)
            or sch_params_by_designator.get(comp.designator))

        rail_member_names = rail_members_by_primary.get(
            rail_group, {rail_net})
        design_voltage_v = design_voltage_for_rail(
            set(rail_member_names), metadata_directives)

        override_label = target_overrides.get(comp.designator)
        if override_label:
            target_label = override_label
            target_pins, target_pins_n = resolve_target_by_label(
                override_label, metadata_directives)
            target_is_override = True
        else:
            target_label, target_pins, target_pins_n = \
                default_target_for_rail(
                    set(rail_member_names), metadata_directives)
            target_is_override = False

        flags: list[str] = []
        if not vias_rail or not vias_return:
            flags.append("no-escape-via")
        elif min(len(vias_rail), len(vias_return)) == 1:
            flags.append("single-via")
        # The worse of the two sides' shortest escape runs, measured from the
        # pad edge — a via-in-pad escapes in 0 mm and must never flag.
        nearest_escape = max(
            (min(e.escape_mm for e in side)
             for side in (vias_rail, vias_return) if side),
            default=0.0)
        if nearest_escape > settings.long_escape_warn_mm:
            flags.append("long-escape")
        if cavity is None:
            flags.append("no-cavity")
        elif cavity.depth_mm > settings.far_plane_warn_mm:
            flags.append("far-plane")
        if target_label is None:
            flags.append("no-target")

        caps.append(CapInstance(
            component_index=ci,
            designator=comp.designator,
            source_designator=comp.source_designator,
            footprint=comp.footprint,
            package=detect_package(comp.footprint),
            center_xy=(comp.center.x, comp.center.y),
            mount_layer_id=mount_layer_id,
            rail_net=rail_net,
            return_net=return_net,
            rail_group=rail_group,
            pads_rail=pads_rail,
            pads_return=pads_return,
            pad_width_rail_mm=_pad_width(pads_rail),
            pad_width_return_mm=_pad_width(pads_return),
            vias_rail=vias_rail,
            vias_return=vias_return,
            cavity=cavity,
            capacitance_f=capacitance_f,
            voltage_rating_v=voltage_rating_v,
            design_voltage_v=design_voltage_v,
            target_label=target_label,
            target_pins=target_pins,
            target_pins_n=target_pins_n,
            target_is_override=target_is_override,
            flags=tuple(flags),
            included=include_overrides.get(comp.designator, True),
            auto_detected=on_rails,
        ))

    caps.sort(key=lambda c: (c.rail_group, c.designator))
    return caps
