"""FYPA loader — orchestrates extract + geometry + annotations.

Public entry: :func:`load_project`. Returns a :class:`LoadedProject` bundle
containing everything the downstream FEM stage needs:

* the raw extraction snapshot (for debugging / introspection),
* the per-layer Shapely geometry with conductance,
* the parsed annotation directives, with pin sets resolved to PCB locations.

This module deliberately stops short of building padne ``Network`` /
``Connection`` objects directly — the padne port (Stage 5+) will translate
``DirectiveSpec`` → padne lumped-element networks once the C++-replacement
mesher is in place.
"""
from __future__ import annotations

import functools
import logging
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path

import shapely.geometry
import shapely.strtree

from fypa.altium.annotations import (
    AnnotationResult,
    DirectiveSpec,
    RegulatorSpec,
    ResistorSpec,
    SinkSpec,
    SourceSpec,
    TerminalPin,
    TerminalSpec,
    parse_annotations,
)
from fypa.altium.extract import (
    ExtractedProject,
    NO_NET,
    extract_project,
)
import dataclasses
from fypa.altium_geometry import (
    GeometryLayer,
    _pad_outer_shape,
    build_layer_geometries,
    build_per_net_geometry_layers_split,
)
from pdnsolver import problem as _pp


# Via barrel resistance is computed per-via, per-hop from physical geometry:
#
#     R_hop = COPPER_RESISTIVITY_OHM_MM * L_hop / A_annulus
#
# where A_annulus = pi * ((d_drill/2)^2 - ((d_drill - 2*t_plating)/2)^2) is
# the cross-section of the plated barrel (drill diameter minus the unplated
# centre void), and L_hop is the z-distance between the centres of the two
# copper layers being bridged — so each hop's resistance scales with the
# dielectric (and any intervening copper) it actually traverses.
#
# 25 µm (~1 mil) is the standard plated-through-hole copper wall thickness
# called out by IPC-A-600 Class 2; designs requiring Class 3 typically use
# the same nominal value with tighter minima. Override if your fab quotes
# differently.
PLATING_THICKNESS_MM: float = 0.025

# Same conductivity used by the plane-sheet model (5.95e4 S/mm == 5.95e7 S/m
# for annealed copper). Resistivity in ohm·mm so that L_hop [mm] / A [mm^2]
# yields R in ohms directly.
COPPER_RESISTIVITY_OHM_MM: float = 1.0 / 5.95e4

# Fallback per-hop resistance used only when the via geometry is missing
# (drill diameter == 0, no stackup z-data, plating fills the hole, etc.).
# Picked to match the previous fixed model (1 mΩ) so behaviour for ill-formed
# inputs is unchanged.
FALLBACK_VIA_RESISTANCE_OHM: float = 1.0e-3

# Bulk resistivity of conductive via-fill paste (Ω·mm). Defaults to a typical
# silver-loaded thermosetting epoxy — vendor data sheets cluster around
# 5×10⁻⁵ Ω·cm = 5×10⁻⁶ Ω·m = 5×10⁻³ Ω·mm, i.e. ~300× annealed copper.
# Pure copper-filled vias (electroplated copper closure) approach copper's
# own resistivity; tune via the Settings tab if the fab specifies a value.
#
# Used only when a via's IPC-4761 fill row carries a material string that
# classifies as conductive (see :func:`_is_conductive_fill`). The fill is
# modelled as a copper-coloured rod inside the plated barrel and combined
# with the wall via the standard parallel-resistor formula.
CONDUCTIVE_FILL_RESISTIVITY_OHM_MM: float = 5.0e-3

# Multi-pin terminal coupling. Padne requires each terminal pin to have its
# own NodeID; pins belonging to the same terminal are tied together via small
# "coupling" resistors in a star topology to the terminal's main NodeID.
#
# Physically this represents the IC package's internal pin-to-pin resistance
# (bond wires + die metal + on-die supply trace), which is typically in the
# 10-100 mΩ range for power pins. Using a value that is *too small* (e.g.
# 1 mΩ) causes a critical pathology: when the same chip has multiple power
# pads landing on different mesh nodes that naturally settle at slightly
# different voltages (a normal IR drop of just a few mV), the tiny coupling
# resistance drives a huge equalisation current between the pads (I = ΔV/R
# → 5 mV / 1 mΩ = 5 A circulating per coupling). With several multi-pin
# sinks this can dominate the solve: tens of amps of phantom circulating
# current flow through any series element supplying the rail (e.g. a
# ferrite bead), making it appear to drop several volts when the real
# sink current is only a few amps.
#
# 100 mΩ is a physically reasonable default — bounds the equalisation
# current to ~50 mA per 5 mV pad-voltage spread, which is negligible
# relative to typical sink currents.
COUPLING_RESISTANCE_OHM: float = 100.0e-3

# Coupling resistance for VOLTAGE-forcing terminals (SOURCE P/N, REGULATOR
# OUT_P/OUT_N). These represent an ideal voltage source whose internal
# package resistance is effectively zero — the source's job is to short
# its pins to the same potential. Using the same 100 mΩ as the SINK
# coupling silently introduces a brutal voltage drop per pin at high
# currents: an 80 A SOURCE split across 8 pins flows 10 A per pin × 0.1 Ω
# = 1 V drop, dragging the visible source-pad voltage to 0 V (the user's
# observed bug). 1 µΩ keeps the drop in the µV range regardless of pin
# current and doesn't introduce the "phantom equalising current" pathology
# that 100 mΩ guards against on SINKs (the source's voltage constraint
# already enforces equal pin potentials directly, so there's nothing for
# the coupling to equalise — at any value).
SOURCE_COUPLING_RESISTANCE_OHM: float = 1.0e-6


log = logging.getLogger(__name__)


# Threshold below which a SERIES directive is treated as an electrical short
# rather than a real series resistance. SERIES elements below this threshold
# are recognised as the user's way of saying "this is a wire link / 0Ω jumper
# between two named nets" — typically because the schematic has split a
# logically-single rail into multiple named nets joined by a placeholder
# component (e.g. 0Ω resistor between two ground domains). The two nets are
# auto-merged into one FEM net, and the SERIES element is removed from the
# circuit (its resistance is absorbed). This eliminates the fragile two-net
# topology that creates large ground-balancing currents in the solver when
# the bridge resistance is tiny relative to the FEM matrix's natural scale.
NET_MERGE_RESISTANCE_THRESHOLD_OHM: float = 0.9e-3  # 0.9 mΩ


# --- Tunable solve settings --------------------------------------------------
#
# The viewer's Settings tab uses this dataclass to bundle every physics /
# mesh knob a user might want to change between runs. ``apply_to_modules``
# overwrites the corresponding module-level constants so the next call to
# :func:`load_project` + :func:`build_problem` picks up the new values
# without having to thread them through every signature.

@dataclass
class SolveSettings:
    """Tunable physics + meshing parameters for a solve.

    Defaults match annealed copper at 20 °C and the standard IPC-A-600
    plated-through-hole barrel. ``copper_conductivity_s_per_mm`` is derived
    from ``copper_resistivity_20c_microohm_cm`` and ``temperature_c`` via
    the linear temperature coefficient so a user changing the board's
    operating temperature gets a physically-correct sheet conductance for
    every layer.
    """

    # Copper electrical model
    temperature_c: float = 20.0
    copper_resistivity_20c_microohm_cm: float = 1.68
    copper_temp_coefficient_per_c: float = 0.00393
    # Via model
    plating_thickness_mm: float = PLATING_THICKNESS_MM
    coupling_resistance_ohm: float = COUPLING_RESISTANCE_OHM
    fallback_via_resistance_ohm: float = FALLBACK_VIA_RESISTANCE_OHM
    conductive_fill_resistivity_ohm_mm: float = CONDUCTIVE_FILL_RESISTIVITY_OHM_MM
    # Meshing
    mesh_min_angle_deg: float = 20.0
    mesh_max_size_mm: float = 0.6
    # Adaptive (variable-density) meshing: fine near pins/vias/edges,
    # coarse in plane interiors. Off by default — it helps boards with
    # large quiet copper but gives little benefit on via-stitched planes.
    adaptive_mesh: bool = False

    @property
    def copper_conductivity_s_per_mm(self) -> float:
        """Temperature-corrected copper conductivity in S/mm.

        ρ(T) = ρ₂₀ · (1 + α·(T − 20))  then  σ = 0.1 / ρ[Ω·cm]  (→ S/mm).
        """
        rho_ohm_cm_20 = self.copper_resistivity_20c_microohm_cm * 1.0e-6
        rho_t = rho_ohm_cm_20 * (
            1.0 + self.copper_temp_coefficient_per_c * (self.temperature_c - 20.0)
        )
        rho_t = max(rho_t, 1.0e-12)
        return 0.1 / rho_t

    @property
    def copper_resistivity_ohm_mm(self) -> float:
        sigma = self.copper_conductivity_s_per_mm
        return float("inf") if sigma <= 0 else 1.0 / sigma

    def apply_to_modules(self) -> None:
        """Patch module-level physics constants so the next solve uses
        these values. Idempotent — call again to change."""
        import fypa.altium_geometry as _g
        _g.COPPER_CONDUCTIVITY_S_PER_MM = self.copper_conductivity_s_per_mm
        # Keep loader-side constants in sync. Functions that reference them
        # via the bare module-global name (e.g. _barrel_segment_resistance_ohm)
        # will pick the new values up automatically on the next call.
        globals()["COPPER_RESISTIVITY_OHM_MM"] = self.copper_resistivity_ohm_mm
        globals()["PLATING_THICKNESS_MM"] = self.plating_thickness_mm
        globals()["COUPLING_RESISTANCE_OHM"] = self.coupling_resistance_ohm
        globals()["FALLBACK_VIA_RESISTANCE_OHM"] = self.fallback_via_resistance_ohm
        globals()["CONDUCTIVE_FILL_RESISTIVITY_OHM_MM"] = (
            self.conductive_fill_resistivity_ohm_mm
        )

    @classmethod
    def from_metadata(cls, metadata: dict | None) -> SolveSettings:
        """Recover a SolveSettings whose physics + mesh values match what
        the solve actually used, by reading the metadata bundle written by
        :func:`build_solve_metadata`. Anything missing from the metadata
        keeps its dataclass default."""
        s = cls()
        if not metadata:
            return s
        phys = metadata.get("physics_constants") or {}
        if "copper_resistivity_20c_microohm_cm" in phys:
            s.copper_resistivity_20c_microohm_cm = float(
                phys["copper_resistivity_20c_microohm_cm"]
            )
        elif "copper_resistivity_microohm_cm" in phys:
            # Older pickles only stored the at-temperature value — re-back-
            # out to a 20 °C reference using the (also recorded) T and α
            # so the SolveSettings reproduces it exactly.
            rho_at_t = float(phys["copper_resistivity_microohm_cm"])
            t = float(phys.get("temperature_c", 20.0))
            a = float(phys.get("copper_temp_coefficient_per_c", 0.00393))
            denom = max(1.0 + a * (t - 20.0), 1.0e-9)
            s.copper_resistivity_20c_microohm_cm = rho_at_t / denom
        if "plating_thickness_mm" in phys:
            s.plating_thickness_mm = float(phys["plating_thickness_mm"])
        if "coupling_resistance_ohm" in phys:
            s.coupling_resistance_ohm = float(phys["coupling_resistance_ohm"])
        if "fallback_via_resistance_ohm" in phys:
            s.fallback_via_resistance_ohm = float(phys["fallback_via_resistance_ohm"])
        if "conductive_fill_resistivity_ohm_mm" in phys:
            s.conductive_fill_resistivity_ohm_mm = float(
                phys["conductive_fill_resistivity_ohm_mm"]
            )
        if "temperature_c" in phys:
            s.temperature_c = float(phys["temperature_c"])
        if "copper_temp_coefficient_per_c" in phys:
            s.copper_temp_coefficient_per_c = float(
                phys["copper_temp_coefficient_per_c"]
            )
        mesher = metadata.get("mesher_config") or {}
        if "minimum_angle_deg" in mesher:
            s.mesh_min_angle_deg = float(mesher["minimum_angle_deg"])
        if "maximum_size_mm" in mesher:
            s.mesh_max_size_mm = float(mesher["maximum_size_mm"])
        if "adaptive_mesh" in mesher:
            s.adaptive_mesh = bool(mesher["adaptive_mesh"])
        return s


class LoadedProject:
    """Everything FYPA needs to hand to the FEM stage.

    ``geometry`` is computed lazily on first access — the FEM solve path
    uses :func:`build_per_net_geometry_layers` directly off ``extracted``
    and never touches ``geometry``, so deferring the legacy all-nets-
    unioned per-layer build saves ~0.8 s on every solve. Quicklook PNGs,
    :meth:`diagnostic_summary`, and the viewer's stackup-override path
    still trigger the build through the property — they need the unioned
    shapes for display.
    """

    extracted: ExtractedProject
    annotations: AnnotationResult

    def __init__(
        self,
        extracted: ExtractedProject,
        annotations: AnnotationResult,
        geometry: list[GeometryLayer] | None = None,
        absorbed_bridges: list[_AbsorbedBridge] | None = None,
    ) -> None:
        self.extracted = extracted
        self.annotations = annotations
        # Bridges from absorbed low-Ω SERIES directives — build_problem
        # re-emits them as same-net coupling Resistors at the original pad
        # locations so the FEM keeps the physical connection between the
        # two formerly-separate-net copper islands.
        self.absorbed_bridges: list[_AbsorbedBridge] = absorbed_bridges or []
        if geometry is not None:
            # Seed the cached_property's slot so the lazy compute is skipped.
            # Used by altium_viewer._apply_stackup_overrides, which already
            # holds a recomputed geometry.
            self.__dict__["geometry"] = geometry

    @functools.cached_property
    def geometry(self) -> list[GeometryLayer]:
        # First access only — the heavy per-layer single-union build.
        # Callers that only need is_solveable / the solve pipeline never
        # come through here. Timed at INFO: on a big board this union
        # runs for over a minute, and it is otherwise invisible — it is
        # triggered lazily (diagnostic_summary / quicklook / stackup
        # overrides), not from an explicitly-labelled pipeline stage.
        log.debug(
            "LoadedProject.geometry: lazily building per-layer single-union "
            "geometry (first access)",
        )
        _t0 = time.monotonic()
        result = build_layer_geometries(self.extracted)
        log.info(
            "LoadedProject.geometry: per-layer single-union build took "
            "%.2fs (%d layer(s))",
            time.monotonic() - _t0, len(result),
        )
        return result

    @property
    def project_name(self) -> str:
        return self.extracted.prjpcb_path.stem

    @property
    def is_solveable(self) -> bool:
        """True if we have at least one enabled copper layer, no annotation
        errors, and one SOURCE-equivalent directive.

        Reads from ``extracted`` + ``annotations`` only — no longer triggers
        the lazy geometry build (which is the entire reason geometry can be
        lazy). The previous "all layers empty" check is dropped: if the
        solve actually has no copper to work with the mesher will fail
        loudly, and producing the union just to confirm that case is
        wasteful on every solve.
        """
        if not self.extracted.enabled_copper_layer_ids():
            return False
        if self.annotations.errors:
            return False
        return any(
            type(d).__name__ in {"SourceSpec", "RegulatorSpec"}
            for d in self.annotations.directives
        )

    def diagnostic_summary(self) -> str:
        lines = [f"Loaded project: {self.project_name}"]
        # Touching self.geometry here triggers the lazy build — by design:
        # the summary's whole purpose is to show per-layer polygon counts.
        layer_lines = []
        for L in self.geometry:
            n = len(L.shape.geoms) if not L.shape.is_empty else 0
            area = L.shape.area if not L.shape.is_empty else 0.0
            layer_lines.append(
                f"    id={L.layer_id:>2}  {L.name:<14}  "
                f"{n:>4} polys  {area:>9.2f} mm^2  G={L.conductance:.3g} S"
            )
        lines.append(f"  Geometry: {len(self.geometry)} copper layer(s)")
        lines.extend(layer_lines)
        # Annotations
        lines.append(f"  Annotations: {len(self.annotations.directives)} directive(s)")
        for d in self.annotations.directives:
            lines.append(f"    {type(d).__name__:<14} {d.designator}  ({d.schdoc_name})")
        if self.annotations.warnings:
            lines.append(f"  Warnings ({len(self.annotations.warnings)}):")
            for w in self.annotations.warnings:
                lines.append(f"    ! {w}")
        if self.annotations.errors:
            lines.append(f"  Errors ({len(self.annotations.errors)}):")
            for e in self.annotations.errors:
                lines.append(f"    [X] {e}")
        if self.is_solveable:
            lines.append("  -> Ready to solve.")
        else:
            reasons = []
            if not self.extracted.enabled_copper_layer_ids():
                reasons.append("no enabled copper layers")
            if self.annotations.errors:
                reasons.append(f"{len(self.annotations.errors)} annotation error(s)")
            has_source = any(
                type(d).__name__ in {"SourceSpec", "RegulatorSpec"}
                for d in self.annotations.directives
            )
            if not has_source:
                reasons.append("no SOURCE or REGULATOR directive")
            lines.append(f"  -> Not ready to solve: {'; '.join(reasons)}.")
        return "\n".join(lines)


def _directive_terminals(d: DirectiveSpec) -> list[TerminalSpec]:
    """Return all PCB-resolved terminals of a directive, regardless of kind.

    A single-net (PDN_NET) SOURCE/SINK has no N terminal — its return is an
    ideal node, not copper — so only its P terminal is returned."""
    if isinstance(d, RegulatorSpec):
        return [d.out_p, d.out_n, d.in_p, d.in_n]
    if d.n is None:
        return [d.p]
    return [d.p, d.n]


def _collect_active_nets(directives, extracted: ExtractedProject) -> set[int]:
    """Set of net indices that any directive's terminal touches.

    Used to restrict the FEM to rails the user actually cares about — signal
    nets without a directive don't need their own padne ``Layer`` and would
    only clutter the GUI's layer selector with hundreds of nets.
    """
    active: set[int] = set()
    for d in directives:
        for term in _directive_terminals(d):
            for pin in term.pins:
                if pin.net_index != NO_NET:
                    active.add(pin.net_index)
    return active


def _terminal_connections(
    term: TerminalSpec,
    main_node: _pp.NodeID,
    layer_by_layer_and_net: dict[tuple[int, int], _pp.Layer],
    coupling_resistance_ohm: float | None = None,
) -> tuple[list[_pp.Connection], list[_pp.BaseLumped]]:
    """Map a :class:`TerminalSpec` to padne ``Connection`` records + any
    auxiliary coupling elements needed when the terminal has multiple pins.

    Each pin is attached to the padne ``Layer`` representing its own
    ``(physical_layer, net)`` pair — so the Connection always lands on
    same-net copper regardless of how any other net's geometry happens to
    overlap in the union-collapsed view.

    Padne requires each Connection's NodeID to map to exactly ONE mesh vertex.
    For a single-pin terminal we attach directly to ``main_node``; for
    multi-pin terminals we mint one NodeID per pin and tie each back to
    ``main_node`` through a small coupling resistor in a star topology.

    ``coupling_resistance_ohm`` defaults to :data:`COUPLING_RESISTANCE_OHM`
    (the SINK-friendly 100 mΩ default). Callers should pass
    :data:`SOURCE_COUPLING_RESISTANCE_OHM` for VOLTAGE-forcing terminals so
    high-current sources don't see a ~1 V drop per pin to their virtual
    hub.

    Pins whose ``(layer, net)`` pair has no padne Layer (e.g. internal planes
    pending implementation, or a net with no extracted copper on that layer)
    are silently skipped; the caller is responsible for warning if that
    leaves the terminal empty.
    """
    if coupling_resistance_ohm is None:
        coupling_resistance_ohm = COUPLING_RESISTANCE_OHM
    valid: list[tuple[_pp.Layer, TerminalPin]] = []
    for pin in term.pins:
        layer = layer_by_layer_and_net.get((pin.layer_id, pin.net_index))
        if layer is None or layer.shape.is_empty:
            continue
        valid.append((layer, pin))

    if not valid:
        return [], []

    if len(valid) == 1:
        layer, pin = valid[0]
        return [_pp.Connection(
            layer=layer,
            point=shapely.geometry.Point(pin.point.x, pin.point.y),
            node_id=main_node,
            region=pin.pad_polygon,
        )], []

    conns: list[_pp.Connection] = []
    aux: list[_pp.BaseLumped] = []
    for layer, pin in valid:
        pin_node = _pp.NodeID()
        conns.append(_pp.Connection(
            layer=layer,
            point=shapely.geometry.Point(pin.point.x, pin.point.y),
            node_id=pin_node,
            region=pin.pad_polygon,
        ))
        aux.append(_pp.Resistor(
            a=pin_node, b=main_node, resistance=coupling_resistance_ohm,
        ))
    return conns, aux


def _directive_to_network(
    d: DirectiveSpec,
    layer_by_layer_and_net: dict[tuple[int, int], _pp.Layer],
    return_ref_nodes: dict[int, _pp.NodeID] | None = None,
) -> _pp.Network | None:
    """Build one padne ``Network`` per directive. Returns ``None`` if the
    directive has no connections after layer resolution.

    Multi-pin terminals contribute auxiliary coupling resistors (see
    :func:`_terminal_connections`) that are bundled into the same Network as
    the main element.

    A single-net (PDN_NET) SOURCE/SINK has only a P terminal on copper; its
    N terminal is bound to the shared ideal-return ``NodeID`` for its
    ``return_group`` (looked up in ``return_ref_nodes``). Because every
    single-net SOURCE and SINK in one group reuses the same NodeID, their
    point-to-point current loop closes — the solver merges a NodeID shared
    across networks into one system variable.
    """
    return_ref_nodes = return_ref_nodes if return_ref_nodes is not None else {}

    def _return_ref(refs: dict[int, _pp.NodeID],
                    group: int | None) -> _pp.NodeID:
        """The shared ideal-return node for a single-net group, minting a
        fresh isolated one if the group id is unknown (defensive — see the
        SourceSpec branch)."""
        node = refs.get(group)
        if node is None:
            node = _pp.NodeID()
            refs[group] = node
        return node
    def _gather(*term_node_coupling_triples):
        all_conns: list[_pp.Connection] = []
        all_aux: list[_pp.BaseLumped] = []
        for term, node, coupling in term_node_coupling_triples:
            c, a = _terminal_connections(
                term, node, layer_by_layer_and_net,
                coupling_resistance_ohm=coupling,
            )
            all_conns.extend(c)
            all_aux.extend(a)
        return all_conns, all_aux

    # Per-terminal coupling-resistance policy:
    # * SOURCE P/N and REGULATOR OUT_P/OUT_N are VOLTAGE-forcing — the
    #   element constrains pin potentials directly, so the star-coupling
    #   resistor exists only to satisfy padne's "one mesh vertex per
    #   NodeID" rule. It must be tiny so high-current sources don't see a
    #   spurious V = I·R drop at each pin (1 V at 10 A × 100 mΩ is what
    #   dragged the user's +1 V source pads to 0 V).
    # * SINK P/N stay on the 100 mΩ default — that value models real
    #   package internal pin-to-pin resistance AND suppresses the
    #   "equalising-current" pathology that small couplings cause when a
    #   sink's pads sit on copper with natural IR drops.
    # * SERIES is almost always single-pin per terminal (an SMT resistor's
    #   two pads), so coupling resistance doesn't kick in.
    # * REGULATOR sense pair (in_p/in_n) carries no current — leave on the
    #   default; the value doesn't matter numerically.
    if isinstance(d, SourceSpec):
        node_p = _pp.NodeID()
        if d.n is None:
            # Single-net: N terminal is the group's shared ideal-return node.
            # ``return_group`` is always set for a single-net directive that
            # reaches a solve (the parser stamps it; an unstamped one only
            # exists in an errored group that blocks the solve) — fall back
            # to a fresh node defensively rather than risk a KeyError.
            node_n = _return_ref(return_ref_nodes, d.return_group)
            element: _pp.BaseLumped = _pp.VoltageSource(
                p=node_p, n=node_n, voltage=d.voltage)
            conns, aux = _gather(
                (d.p, node_p, SOURCE_COUPLING_RESISTANCE_OHM),
            )
        else:
            node_n = _pp.NodeID()
            element = _pp.VoltageSource(p=node_p, n=node_n, voltage=d.voltage)
            conns, aux = _gather(
                (d.p, node_p, SOURCE_COUPLING_RESISTANCE_OHM),
                (d.n, node_n, SOURCE_COUPLING_RESISTANCE_OHM),
            )
    elif isinstance(d, SinkSpec):
        node_f = _pp.NodeID()
        if d.n is None:
            node_t = _return_ref(return_ref_nodes, d.return_group)
            element = _pp.CurrentSource(f=node_f, t=node_t, current=d.current)
            conns, aux = _gather(
                (d.p, node_f, COUPLING_RESISTANCE_OHM),
            )
        else:
            node_t = _pp.NodeID()
            element = _pp.CurrentSource(f=node_f, t=node_t, current=d.current)
            conns, aux = _gather(
                (d.p, node_f, COUPLING_RESISTANCE_OHM),
                (d.n, node_t, COUPLING_RESISTANCE_OHM),
            )
    elif isinstance(d, ResistorSpec):
        node_a, node_b = _pp.NodeID(), _pp.NodeID()
        element = _pp.Resistor(a=node_a, b=node_b, resistance=d.resistance)
        conns, aux = _gather(
            (d.p, node_a, COUPLING_RESISTANCE_OHM),
            (d.n, node_b, COUPLING_RESISTANCE_OHM),
        )
    elif isinstance(d, RegulatorSpec):
        node_vp, node_vn = _pp.NodeID(), _pp.NodeID()
        node_sf, node_st = _pp.NodeID(), _pp.NodeID()
        element = _pp.VoltageRegulator(
            v_p=node_vp, v_n=node_vn, s_f=node_sf, s_t=node_st,
            voltage=d.voltage, gain=d.gain,
        )
        conns, aux = _gather(
            (d.out_p, node_vp, SOURCE_COUPLING_RESISTANCE_OHM),
            (d.out_n, node_vn, SOURCE_COUPLING_RESISTANCE_OHM),
            (d.in_p, node_sf, COUPLING_RESISTANCE_OHM),
            (d.in_n, node_st, COUPLING_RESISTANCE_OHM),
        )
    else:
        raise TypeError(f"Unknown directive type: {type(d).__name__}")

    if not conns:
        return None
    return _pp.Network(connections=conns, elements=[element, *aux])


def _via_pad_layer_span(start: int, end: int, enabled_layers: list[int]) -> list[int]:
    """Return enabled copper layers that a via/through-hole pad bridges,
    in Top->Bottom order."""
    if start > end:
        start, end = end, start
    return [lid for lid in enabled_layers if start <= lid <= end]


def _layer_z_centers_mm(
    extracted: ExtractedProject,
    enabled_layers: list[int],
) -> dict[int, float]:
    """Return ``{layer_id: z_center_mm}`` for every enabled copper layer.

    Z origin is the top surface of the topmost enabled copper layer; z grows
    downward through the stack. The z-coordinate returned is the *centre* of
    each copper layer (i.e. top surface + 0.5 * copper_thickness), so the
    distance between two layer centres is the natural barrel-segment length
    for a via bridging them.

    Returns an empty dict when the stackup has no enabled rows — callers
    treat that as "no z data available" and fall back to a constant R.
    """
    by_id = {s.layer_id: s for s in extracted.stackup}
    z = 0.0
    out: dict[int, float] = {}
    for i, lid in enumerate(enabled_layers):
        s = by_id.get(lid)
        if s is None:
            continue
        t_cu = float(s.copper_thickness_mm)
        out[lid] = z + 0.5 * t_cu
        # Advance to the top of the next copper layer: full copper + dielectric
        # below this one. Dielectric is only present between copper layers, so
        # skip it on the last entry.
        if i + 1 < len(enabled_layers):
            z += t_cu + float(s.dielectric_thickness_mm)
    return out


def _barrel_segment_resistance_ohm(
    drill_diameter_mm: float,
    hop_length_mm: float,
    plating_thickness_mm: float,
    *,
    conductive_fill_resistivity_ohm_mm: float | None = None,
) -> float:
    """DC resistance of one plated-barrel segment.

    Models the barrel as a hollow copper cylinder: outer radius == drill / 2,
    inner radius == outer - plating thickness. If plating fills the hole
    (e.g. the drill is smaller than 2 * plating, as with via-in-pad fills)
    the barrel collapses to a solid copper rod.

    When ``conductive_fill_resistivity_ohm_mm`` is supplied, the unplated
    inner void is modelled as a *second* conducting cylinder (the fill rod)
    in parallel with the plated wall — see IPC-4761 types V / VIa / VIb /
    VII with a copper- or silver-paste material. The two resistances are
    combined as ``1/R = 1/R_wall + 1/R_fill``, and the segment record's
    effective R reflects the shunt.

    Falls back to :data:`FALLBACK_VIA_RESISTANCE_OHM` when geometry is missing
    or degenerate, so a missing drill size never produces a divide-by-zero or
    an unrealistic 0 Ω short.
    """
    if drill_diameter_mm <= 0.0 or hop_length_mm <= 0.0:
        return FALLBACK_VIA_RESISTANCE_OHM
    r_outer = 0.5 * drill_diameter_mm
    r_inner = r_outer - max(plating_thickness_mm, 0.0)
    if r_inner <= 0.0:
        # Plating closes the barrel — solid copper rod, no separate fill
        # element (there's no void left to fill).
        area_mm2 = math.pi * r_outer * r_outer
        if area_mm2 <= 0.0:
            return FALLBACK_VIA_RESISTANCE_OHM
        return COPPER_RESISTIVITY_OHM_MM * hop_length_mm / area_mm2
    wall_area_mm2 = math.pi * (r_outer * r_outer - r_inner * r_inner)
    if wall_area_mm2 <= 0.0:
        return FALLBACK_VIA_RESISTANCE_OHM
    r_wall = COPPER_RESISTIVITY_OHM_MM * hop_length_mm / wall_area_mm2
    if (conductive_fill_resistivity_ohm_mm is None
            or conductive_fill_resistivity_ohm_mm <= 0.0):
        return r_wall
    # Parallel shunt: solid rod of fill material inside the wall.
    fill_area_mm2 = math.pi * r_inner * r_inner
    if fill_area_mm2 <= 0.0:
        return r_wall
    r_fill = (
        conductive_fill_resistivity_ohm_mm * hop_length_mm / fill_area_mm2
    )
    if r_fill <= 0.0:
        return r_wall
    return (r_wall * r_fill) / (r_wall + r_fill)


# IPC-4761 via-protection enum values that include a barrel FILL operation
# (the unplated centre void is back-filled with paste or copper). Tenting,
# covering, plugging, and capping leave the conductive cross-section of the
# barrel unchanged and so do NOT trigger the fill-aware resistance branch.
#
# Values mirror ``altium_monkey.PcbIpc4761ViaType``:
#   5 = TYPE_5_FILLING, 10/11 = TYPE_6A/B_FILLING_AND_COVERING,
#   12 = TYPE_7_FILLING_AND_CAPPING.
# (Type 9 in older corpora was also TYPE_5_FILLING; both are accepted for
# resilience against minor enum reorderings in altium_monkey.)
_IPC4761_FILL_TYPES: frozenset[int] = frozenset({5, 9, 10, 11, 12})


# Substrings (case-insensitive) that classify an IPC-4761 fill material as
# electrically conductive. Altium stores the material as free text on the
# via_structure FILLING row; common values include "Copper", "Cu",
# "Silver Epoxy", "Ag-loaded", "Conductive Paste". Non-conductive epoxies
# and polymers (e.g. "Polymer", "Resin", "Epoxy", "Non-Conductive") do not
# match and so do not change the resistance.
_CONDUCTIVE_MATERIAL_KEYWORDS: tuple[str, ...] = (
    "conductive",
    "copper",
    "silver",
    " cu",   # leading space avoids matching "Cuprate", "Cure", etc.
    "cu ",
    " ag",
    "ag ",
    "ag-",
    "ag/",
    "cu/",
    "cu-",
)


def _is_conductive_fill(ipc4761_via_type: int, fill_material: str) -> bool:
    """Return True iff this via is IPC-4761 filled AND the fill material's
    name implies electrical conductivity.

    A non-conductive (epoxy / polymer) fill leaves the plated wall as the
    sole DC current path, so the resistance model is unchanged. A conductive
    (copper / silver paste / copper-filled) fill adds a parallel rod down
    the centre of the via, lowering the effective hop resistance.
    """
    if int(ipc4761_via_type) not in _IPC4761_FILL_TYPES:
        return False
    if not fill_material:
        # Filled but unspecified material — conservative default: treat as
        # non-conductive (epoxy is the much more common case). The user can
        # override per-board by editing the IPC-4761 row in Altium.
        return False
    mat = f" {fill_material.lower().strip()} "
    if "non-conductive" in mat or "nonconductive" in mat:
        return False
    return any(kw in mat for kw in _CONDUCTIVE_MATERIAL_KEYWORDS)


# Friendly IPC-4761 type labels for display. Falls back to "Type {n}" /
# "—" for unknown / NONE values. Kept here so the viewer's Vias tab and
# any other surface can share the exact same strings.
_IPC4761_TYPE_LABELS: dict[int, str] = {
    0:  "—",                              # NONE / unprotected
    1:  "Ia (tent, top)",
    2:  "Ib (tent, both)",
    3:  "IIa (tent + cover, top)",
    4:  "IIb (tent + cover, both)",
    5:  "IIIa (plug, top)",
    6:  "IIIb (plug, both)",
    7:  "IVa (plug + cover, top)",
    8:  "IVb (plug + cover, both)",
    9:  "V (fill)",
    10: "VIa (fill + cover, top)",
    11: "VIb (fill + cover, both)",
    12: "VII (fill + cap)",
}


def ipc4761_label(ipc4761_via_type: int, fill_material: str = "") -> str:
    """Human-readable IPC-4761 protection label for one via, optionally
    enriched with the fill material when present (e.g. "V (fill) · Copper").
    """
    base = _IPC4761_TYPE_LABELS.get(int(ipc4761_via_type),
                                     f"Type {int(ipc4761_via_type)}")
    if int(ipc4761_via_type) in _IPC4761_FILL_TYPES and fill_material:
        return f"{base} · {fill_material}"
    return base


@dataclass(frozen=True, slots=True)
class _ViaSite:
    """One via or through-hole pad treated as an inter-layer coupling site."""
    x_mm: float
    y_mm: float
    span: tuple[int, ...]        # enabled layer ids the barrel physically reaches
    net_index: int
    drill_diameter_mm: float     # 0.0 if unknown (triggers fallback R)
    # IPC-4761 metadata; only ``ipc4761_via_type`` and ``fill_material`` drive
    # the resistance model, the rest is passthrough for the viewer.
    ipc4761_via_type: int = 0
    fill_material: str = ""


def _coupling_networks(
    sites: list[_ViaSite],
    layer_by_layer_and_net: dict[tuple[int, int], _pp.Layer],
    layer_z_mm: dict[int, float],
    plating_thickness_mm: float | None = None,
    conductive_fill_resistivity_ohm_mm: float | None = None,
) -> tuple[list[_pp.Network], list[dict]]:
    """Build small-Resistor networks coupling adjacent enabled copper layers
    at each through-hole / via location.

    For each site we keep only the layers in its physical span where the via's
    nominal net actually has copper, then chain a Resistor between each
    consecutive pair. The per-hop resistance is computed from the via's drill
    diameter, the standard plating thickness, and the z-distance between the
    two copper-layer centres — so a hop that crosses a thicker dielectric
    (or skips an intervening unconnected layer) gets a proportionally larger
    R, and a narrower drill gets a larger R for the same hop.

    Returns ``(networks, segment_records)``. Each entry in ``segment_records``
    describes one inserted resistor (site coordinates, layer pair, length, R)
    so :func:`build_solve_metadata` can ship it to the viewer for accurate
    per-segment current / power analysis.

    Sites whose net is :data:`NO_NET` or whose nominal net has copper on fewer
    than two layers in the span are skipped (no element inserted).
    """
    # Read the default *now* so callers that omit it pick up any monkey-
    # patched module value (Settings tab → Re-run Solver path).
    if plating_thickness_mm is None:
        plating_thickness_mm = PLATING_THICKNESS_MM
    if conductive_fill_resistivity_ohm_mm is None:
        conductive_fill_resistivity_ohm_mm = CONDUCTIVE_FILL_RESISTIVITY_OHM_MM
    networks: list[_pp.Network] = []
    segment_records: list[dict] = []
    skipped_unknown_net = 0
    skipped_missing_layer = 0
    skipped_xy_outside_copper = 0
    for site in sites:
        if site.net_index == NO_NET:
            skipped_unknown_net += 1
            continue
        pt = shapely.geometry.Point(site.x_mm, site.y_mm)
        # Only chain the via through layers where the net's copper
        # actually covers the via's (x, y). Checking "net has copper
        # SOMEWHERE on this layer" isn't enough: a through-hole via
        # whose rail net also has copper on (say) 6 inner layers far
        # from the via gets a phantom segment per inner layer. padne
        # creates a Steiner point at the via xy on each of those
        # layers — outside their actual copper — and the FEM
        # interpolates a neighbour voltage there, opening a fake
        # current path through the via. A test-point via with one
        # real endpoint then reports a non-zero (and entirely
        # fictional) current. ``covers`` includes the boundary so
        # vias whose centre sits on the edge of a pad still count.
        layers_for_net = [
            lid for lid in site.span
            if (L := layer_by_layer_and_net.get((lid, site.net_index))) is not None
            and not L.shape.is_empty
            and L.shape.covers(pt)
        ]
        if len(layers_for_net) < 2:
            # Either the net has copper on <2 layers in the span at
            # all, or — more usefully — the via xy isn't inside the
            # net's copper on enough of those layers to bridge them.
            has_copper_anywhere = False
            for lid in site.span:
                L_any = layer_by_layer_and_net.get((lid, site.net_index))
                if L_any is not None and not L_any.shape.is_empty:
                    has_copper_anywhere = True
                    break
            if has_copper_anywhere:
                skipped_xy_outside_copper += 1
            else:
                skipped_missing_layer += 1
            continue
        is_conductive_fill = _is_conductive_fill(
            site.ipc4761_via_type, site.fill_material,
        )
        fill_rho = (
            conductive_fill_resistivity_ohm_mm if is_conductive_fill else None
        )
        for lid_a, lid_b in zip(layers_for_net, layers_for_net[1:]):
            la = layer_by_layer_and_net[(lid_a, site.net_index)]
            lb = layer_by_layer_and_net[(lid_b, site.net_index)]
            z_a = layer_z_mm.get(lid_a)
            z_b = layer_z_mm.get(lid_b)
            if z_a is None or z_b is None:
                hop_length_mm = 0.0   # forces fallback
            else:
                hop_length_mm = abs(z_b - z_a)
            r_hop = _barrel_segment_resistance_ohm(
                site.drill_diameter_mm, hop_length_mm, plating_thickness_mm,
                conductive_fill_resistivity_ohm_mm=fill_rho,
            )
            node_a, node_b = _pp.NodeID(), _pp.NodeID()
            element = _pp.Resistor(a=node_a, b=node_b, resistance=r_hop)
            conns = [
                _pp.Connection(layer=la, point=pt, node_id=node_a),
                _pp.Connection(layer=lb, point=pt, node_id=node_b),
            ]
            networks.append(_pp.Network(connections=conns, elements=[element]))
            segment_records.append({
                "x_mm": site.x_mm,
                "y_mm": site.y_mm,
                "net_index": site.net_index,
                "layer_a": lid_a,
                "layer_b": lid_b,
                "hop_length_mm": hop_length_mm,
                "drill_diameter_mm": site.drill_diameter_mm,
                "resistance_ohm": r_hop,
                "is_conductive_fill": is_conductive_fill,
            })

    if skipped_unknown_net:
        log.debug("Skipped %d via/TH-pad coupling site(s) with no net assignment.",
                  skipped_unknown_net)
    if skipped_missing_layer:
        log.debug("Skipped %d via/TH-pad coupling site(s) where the via's net "
                  "has no extracted copper on at least 2 layers.",
                  skipped_missing_layer)
    if skipped_xy_outside_copper:
        log.debug("Skipped %d via/TH-pad coupling site(s) where fewer than 2 "
                  "layers' net copper actually covered the via xy "
                  "(others were dropped to avoid phantom Steiner couplings).",
                  skipped_xy_outside_copper)
    return networks, segment_records


def _via_through_holes(
    extracted: ExtractedProject,
    enabled_layers: list[int],
) -> list[_ViaSite]:
    """Collect every via + every through-hole pad as a coupling site.

    Vias use their declared layer_start / layer_end span and their stored
    drill diameter. Through-hole pads are assumed to span every enabled
    copper layer (Altium pads don't carry an explicit blind/buried span).
    Pads with a 0.0 hole size still report span info — they get the fallback
    resistance per hop because the annulus cross-section is undefined.
    """
    sites: list[_ViaSite] = []
    for v in extracted.vias:
        span = _via_pad_layer_span(v.layer_start, v.layer_end, enabled_layers)
        if len(span) >= 2:
            sites.append(_ViaSite(
                x_mm=v.center.x, y_mm=v.center.y,
                span=tuple(span), net_index=v.net_index,
                drill_diameter_mm=float(v.hole_diameter_mm),
                ipc4761_via_type=int(getattr(v, "ipc4761_via_type", 0) or 0),
                fill_material=str(getattr(v, "fill_material", "") or ""),
            ))
    for p in extracted.pads:
        if not p.is_through_hole:
            continue
        # Through-hole pads carry no IPC-4761 fill metadata in Altium —
        # the IPC-4761 protection table is a via-only concept.
        sites.append(_ViaSite(
            x_mm=p.center.x, y_mm=p.center.y,
            span=tuple(enabled_layers), net_index=p.net_index,
            drill_diameter_mm=float(p.hole_mm),
        ))
    return sites


def _build_stub_record(piece, layer_id: int, net_name: str) -> dict | None:
    """Turn one stub Polygon into the dict the viewer consumes.

    Stores exterior + holes as ``(N, 2) float32`` numpy arrays (much
    smaller pickle than nested Python lists, faster to upload to the GL
    stub-fill batch) and pre-triangulates the polygon via the ``triangle``
    library so the viewer never has to call out to ``triangle`` on its
    own. Falls back to leaving the triangulation absent if the constrained
    Delaunay fails — the viewer's lazy path will then retry.

    Returns ``None`` for degenerate pieces (no exterior or <3 vertices).
    """
    import numpy as np
    exterior = getattr(piece, "exterior", None)
    if exterior is None or exterior.is_empty:
        return None
    ext_coords = list(exterior.coords)
    if len(ext_coords) >= 2 and ext_coords[0] == ext_coords[-1]:
        ext_coords = ext_coords[:-1]
    if len(ext_coords) < 3:
        return None
    ext_arr = np.asarray(ext_coords, dtype=np.float32)

    holes_arr: list = []
    hole_rings_for_tri: list[list[tuple[float, float]]] = []
    for hole in getattr(piece, "interiors", []):
        if hole.is_empty:
            continue
        h_coords = list(hole.coords)
        if len(h_coords) >= 2 and h_coords[0] == h_coords[-1]:
            h_coords = h_coords[:-1]
        if len(h_coords) < 3:
            continue
        holes_arr.append(np.asarray(h_coords, dtype=np.float32))
        hole_rings_for_tri.append([(float(x), float(y)) for x, y in h_coords])

    record: dict = {
        "layer_id": int(layer_id),
        "net": net_name,
        "exterior": ext_arr,
        "holes": holes_arr,
    }

    # Pre-triangulate via the triangle library. Same algorithm and switches
    # the viewer used to call lazily on first open — running it at solve
    # time means the cached pickle includes the fill geometry and the
    # viewer just uploads a numpy buffer to the GPU.
    try:
        import triangle as _tri
        verts: list[tuple[float, float]] = [(float(x), float(y))
                                            for x, y in ext_coords]
        segs: list[tuple[int, int]] = [(i, (i + 1) % len(ext_coords))
                                       for i in range(len(ext_coords))]
        hole_markers: list[tuple[float, float]] = []
        for h_ring in hole_rings_for_tri:
            base = len(verts)
            verts.extend(h_ring)
            n = len(h_ring)
            for i in range(n):
                segs.append((base + i, base + (i + 1) % n))
            try:
                hp = shapely.geometry.Polygon(h_ring).representative_point()
                hole_markers.append((float(hp.x), float(hp.y)))
            except Exception:
                # Couldn't synthesise a hole marker — leave it; Triangle
                # will mesh the hole as solid, the viewer renders it.
                pass
        tri_input: dict = {"vertices": verts, "segments": segs}
        if hole_markers:
            tri_input["holes"] = hole_markers
        out = _tri.triangulate(tri_input, "pQ")
        v_arr = np.asarray(out.get("vertices", []), dtype=np.float32)
        t_arr = np.asarray(out.get("triangles", []), dtype=np.int32)
        if v_arr.size and t_arr.size:
            # Flat (N*3, 2) GL_TRIANGLES vertex soup — matches the buffer
            # layout the GL stub batch expects directly.
            record["triangles_xy"] = v_arr[t_arr.ravel()].astype(
                np.float32, copy=False,
            )
    except Exception:
        # Triangle library unhappy with this polygon — viewer will retry
        # lazily and degrade to its own fallback.
        pass
    return record


def _build_all_copper_records(
    per_net_layers: list[GeometryLayer] | None,
    net_name_fn,
) -> list[dict]:
    """Pack per-(layer, net) copper polygon rings for the Overlays
    control's per-layer "all copper" view.
    Each record: ``{layer_id, net, polygons: [{exterior, holes}, ...]}``
    with rings stored as ``(N, 2) float32`` numpy arrays (same compact form
    as :func:`_build_stub_record`)."""
    if not per_net_layers:
        return []
    import numpy as np
    out: list[dict] = []
    for gl in per_net_layers:
        if gl.shape is None or gl.shape.is_empty:
            continue
        polys = (list(gl.shape.geoms)
                 if gl.shape.geom_type == "MultiPolygon"
                 else [gl.shape])
        ring_polys: list[dict] = []
        for poly in polys:
            ext = getattr(poly, "exterior", None)
            if ext is None or ext.is_empty:
                continue
            ext_arr = np.asarray(list(ext.coords), dtype=np.float32)
            if ext_arr.shape[0] < 2:
                continue
            holes_arr: list = []
            for hole in getattr(poly, "interiors", []):
                if hole.is_empty:
                    continue
                h_arr = np.asarray(list(hole.coords), dtype=np.float32)
                if h_arr.shape[0] >= 2:
                    holes_arr.append(h_arr)
            ring_polys.append({"exterior": ext_arr, "holes": holes_arr})
        if not ring_polys:
            continue
        out.append({
            "layer_id": int(gl.layer_id),
            "net": net_name_fn(gl.net_index),
            "polygons": ring_polys,
        })
    return out


# Altium classic layer ids for the silkscreen (overlay) layers.
_TOP_OVERLAY_LAYER_ID = 33
_BOT_OVERLAY_LAYER_ID = 34
_OVERLAY_LAYER_IDS = (_TOP_OVERLAY_LAYER_ID, _BOT_OVERLAY_LAYER_ID)


def _arc_polyline(arc) -> list[list[float]]:
    """Tessellate a :class:`RawArc` into an origin-corrected ``[x, y]``
    polyline. Altium stores arc angles in degrees and sweeps CCW; a
    non-positive sweep is wrapped by a full turn."""
    start = math.radians(arc.start_angle_deg)
    end = math.radians(arc.end_angle_deg)
    sweep = end - start
    if sweep <= 1e-9:
        sweep += 2.0 * math.pi
    n = max(2, int(abs(sweep) / math.radians(12.0)) + 1)
    cx, cy, r = arc.center.x, arc.center.y, arc.radius_mm
    return [[cx + r * math.cos(start + sweep * k / n),
             cy + r * math.sin(start + sweep * k / n)]
            for k in range(n + 1)]


def _stroke_font_tables(kind: int):
    """Return ``(glyphs, advances)`` for one of Altium's three built-in PCB
    stroke fonts — 0 = Default, 1 = Sans Serif, 2 = Serif."""
    import altium_monkey.altium_stroke_font_data as sfd
    if kind == 1:
        return sfd.STROKE_FONT_SANS_SERIF, sfd.STROKE_ADVANCES_SANS_SERIF
    if kind == 2:
        return sfd.STROKE_FONT_SERIF, sfd.STROKE_ADVANCES_SERIF
    return sfd.STROKE_FONT_DEFAULT, sfd.STROKE_ADVANCES_DEFAULT


def _stroke_text_polylines(text: str, stroke_kind: int, height_mm: float,
                           anchor_x: float, anchor_y: float,
                           rotation_deg: float, mirrored: bool) -> list[list]:
    """Lay a string out in one of Altium's built-in single-stroke fonts.

    Returns the glyph stroke polylines as ``[[x, y], ...]`` lists in the
    origin-corrected millimetre frame — the exact vector geometry Altium
    itself strokes designators with. The glyph tables store coordinates
    normalised to a unit character height with a y=0 baseline; advances
    (already including Altium's per-font multiplier) are in the same unit.
    """
    if height_mm <= 0.0 or not text:
        return []
    try:
        glyphs, advances = _stroke_font_tables(stroke_kind)
    except Exception:
        return []

    # Lay out left-to-right along the baseline, in height-normalised units.
    local: list[list[tuple[float, float]]] = []
    pen = 0.0
    for ch in text:
        code = ord(ch)
        for stroke in glyphs.get(code, ()):
            if len(stroke) >= 2:
                local.append([(pen + px, py) for px, py in stroke])
        pen += float(advances.get(code, 0.7))
    if not local:
        return []

    # Scale to mm; mirror bottom-side text; rotate; translate to the anchor.
    ang = math.radians(rotation_deg)
    cos_a, sin_a = math.cos(ang), math.sin(ang)
    sx = -1.0 if mirrored else 1.0
    out: list[list] = []
    for stroke in local:
        pts: list = []
        for px, py in stroke:
            lx = sx * px * height_mm
            ly = py * height_mm
            pts.append([anchor_x + lx * cos_a - ly * sin_a,
                        anchor_y + lx * sin_a + ly * cos_a])
        out.append(pts)
    return out


def _build_overlay_records(proj: ExtractedProject, net_name_fn) -> dict:
    """Build the silkscreen / component-body / designator overlay records
    consumed by the viewer's Overlays control.

    All coordinates are the same origin-corrected millimetre frame as the
    mesh. ``silkscreen`` is a list of ``{side, polyline}``; ``components``
    a list of ``{designator, side, bbox, nets}`` (axis-aligned bounding
    box of the component's pads); ``designators`` a list of
    ``{text, x_mm, y_mm, height_mm, rotation_deg, side, nets}``."""
    def _side(layer_id: int) -> str:
        return "bottom" if layer_id == _BOT_OVERLAY_LAYER_ID else "top"

    # Overlay graphics — the tracks and (tessellated) arcs on the Top /
    # Bottom Overlay layers: component silkscreen outlines, polarity marks,
    # board markings. Text is deliberately excluded — reference designators
    # are their own overlay, and component comment / value strings are a
    # component property Altium hides on the overlay by default.
    silkscreen: list[dict] = []
    for t in proj.tracks:
        if t.layer_id in _OVERLAY_LAYER_IDS:
            silkscreen.append({
                "kind": "line",
                "side": _side(t.layer_id),
                "width_mm": float(t.width_mm),
                "polyline": [[t.a.x, t.a.y], [t.b.x, t.b.y]],
            })
    for a in proj.arcs:
        if a.layer_id in _OVERLAY_LAYER_IDS:
            silkscreen.append({
                "kind": "line",
                "side": _side(a.layer_id),
                "width_mm": float(a.width_mm),
                "polyline": _arc_polyline(a),
            })

    # Per-component net set + pad-extent points (for the body bounding box).
    # The half-diagonal extent guarantees the box contains each pad even
    # after the component's placement rotation.
    comp_nets: dict[int, set[str]] = {}
    comp_pts: dict[int, list[tuple[float, float]]] = {}
    for p in proj.pads:
        ci = p.component_index
        if ci < 0:
            continue
        comp_nets.setdefault(ci, set()).add(net_name_fn(p.net_index))
        half = 0.5 * math.hypot(p.width_mm, p.height_mm)
        comp_pts.setdefault(ci, []).extend((
            (p.center.x - half, p.center.y - half),
            (p.center.x + half, p.center.y + half),
        ))

    components: list[dict] = []
    for ci, comp in enumerate(proj.pcb_components):
        pts = comp_pts.get(ci)
        if not pts:
            continue
        xs = [x for x, _ in pts]
        ys = [y for _, y in pts]
        side = ("bottom" if str(comp.layer_name).upper().startswith("B")
                else "top")
        components.append({
            "designator": comp.designator,
            "side": side,
            "bbox": [min(xs), min(ys), max(xs), max(ys)],
            "nets": sorted(comp_nets.get(ci, set())),
        })

    designators: list[dict] = []
    for t in proj.texts:
        if not t.is_designator or not t.text:
            continue
        ci = t.component_index
        if t.layer_id in _OVERLAY_LAYER_IDS:
            side = _side(t.layer_id)
        else:
            side = "bottom" if t.is_mirrored else "top"
        # Stroke-font designators are laid out into the exact single-stroke
        # vector geometry Altium draws — so the viewer renders them in the
        # real Altium stroke font, not a substitute. TrueType designators
        # ship with empty polylines and the viewer falls back to a label.
        polylines: list = []
        if t.is_stroke:
            polylines = _stroke_text_polylines(
                t.text, t.stroke_kind, t.height_mm,
                t.center.x, t.center.y, t.rotation_deg, t.is_mirrored,
            )
        designators.append({
            "text": t.text,
            "x_mm": t.center.x,
            "y_mm": t.center.y,
            "height_mm": t.height_mm,
            "rotation_deg": t.rotation_deg,
            "side": side,
            "nets": sorted(comp_nets.get(ci, set())) if ci >= 0 else [],
            "stroke_width_mm": t.stroke_width_mm,
            "polylines": polylines,
        })

    return {
        "silkscreen": silkscreen,
        "components": components,
        "designators": designators,
    }


def _gil_yield(i: int, every: int = 256) -> None:
    """Release the GIL briefly every ``every`` iterations.

    Why: ``build_solve_metadata`` walks every via / PTH / pad on the board,
    calling into shapely C code that holds the GIL for the full duration of
    each call. On large boards (thousands of pads) the worker thread can
    monopolise the GIL for tens of seconds, freezing the GUI progress dialog
    and triggering Windows' "Not Responding" watchdog. ``time.sleep`` drops
    the GIL via ``Py_BEGIN_ALLOW_THREADS``; using a 1 ms (rather than 0 ms)
    sleep forces the Windows scheduler to actually deliver the timeslice to
    the higher-priority GUI thread (see ``_SolveWorker.run`` in
    altium_viewer.py) instead of immediately rescheduling the worker."""
    if i and (i % every) == 0:
        time.sleep(0.001)


def build_solve_metadata(
    loaded: LoadedProject,
    problem: _pp.Problem | None = None,
    mesher_config=None,
    solver_info=None,
    via_segment_records: list[dict] | None = None,
    settings: SolveSettings | None = None,
    stub_pieces_by_pair: dict[tuple[int, int], list] | None = None,
    per_net_layers: list[GeometryLayer] | None = None,
) -> dict:
    """Collect every input the solve depended on into a serialisable dict.

    Bundled into the solution pickle so the viewer's "Setup" tab can show
    stackup, physics constants, every directive's resolved terminals, the
    solver convergence stats — everything the user might want to verify the
    tool is doing the right thing.

    All values are JSON-safe primitives (no padne / shapely objects), so the
    metadata can be serialised independently if needed.
    """
    proj = loaded.extracted
    nets = proj.nets
    enabled = proj.enabled_copper_layer_ids()
    stackup_by_id = {s.layer_id: s for s in proj.stackup}

    def _net_name(idx: int) -> str:
        if 0 <= idx < len(nets):
            return nets[idx].name
        return "(none)"

    # Stackup — only the enabled copper layers (the ones the FEM actually uses).
    stackup_rows = []
    for lid in enabled:
        s = stackup_by_id.get(lid)
        if s is None:
            continue
        sheet_conductance = s.copper_thickness_mm * 5.95e4  # S = thickness_mm * conductivity_S_per_mm
        stackup_rows.append({
            "layer_id": lid,
            "name": s.name,
            "copper_thickness_mm": s.copper_thickness_mm,
            "copper_thickness_mil": s.copper_thickness_mm / 0.0254,
            "copper_thickness_oz": s.copper_thickness_mm / 0.0348,  # 1oz copper ≈ 34.8 µm
            "dielectric_thickness_mm": s.dielectric_thickness_mm,
            "sheet_conductance_S": sheet_conductance,
            "sheet_resistance_milliohm_per_sq": 1000.0 / sheet_conductance,
            "is_plane": s.is_plane,
            "plane_net_name": s.plane_net_name,
            "next_layer_id": s.next_layer_id,
        })

    # Directive summary — what each PDN_* annotation resolved to.
    directives = []
    for d in loaded.annotations.directives:
        ch_idx = getattr(d, "channel_index", None)
        label = d.designator if ch_idx is None else f"{d.designator}#{ch_idx}"
        common = {
            "role": type(d).__name__.replace("Spec", "").upper(),
            "designator": d.designator,
            # Indexed multi-channel SOURCE/SINK channels expose ``channel_index``
            # (None for the legacy unindexed channel and for non-indexable roles
            # like SERIES/REGULATOR). ``label`` is the human-friendly form
            # ("U5" or "U5#1") that the viewer should prefer in tables/headers.
            "channel_index": ch_idx,
            "label": label,
            "schdoc": d.schdoc_name,
        }
        if isinstance(d, SourceSpec):
            common["value"] = d.voltage
            common["unit"] = "V"
            common["value_str"] = f"{d.voltage:.4g} V"
            common["terminals"] = {
                "P": _terminal_summary(d.p, nets),
                "N": _terminal_summary(d.n, nets),
            }
        elif isinstance(d, SinkSpec):
            common["value"] = d.current
            common["unit"] = "A"
            common["value_str"] = f"{d.current * 1000:.4g} mA"
            common["terminals"] = {
                "P": _terminal_summary(d.p, nets),
                "N": _terminal_summary(d.n, nets),
            }
            # Optional PDN_MIN_V — minimum acceptable rail voltage at the
            # sink's P terminal. The viewer's Nodes table renders a per-pin
            # margin / pass-fail column off this. ``None`` when unset.
            if getattr(d, "min_voltage", None) is not None:
                common["min_voltage"] = float(d.min_voltage)
        elif isinstance(d, ResistorSpec):
            common["value"] = d.resistance
            common["unit"] = "Ohm"
            common["value_str"] = f"{d.resistance * 1000:.4g} mOhm"
            common["terminals"] = {
                "P": _terminal_summary(d.p, nets),
                "N": _terminal_summary(d.n, nets),
            }
        elif isinstance(d, RegulatorSpec):
            common["value"] = d.voltage
            common["unit"] = "V"
            common["value_str"] = f"V={d.voltage:.4g} V, gain={d.gain:.3g}"
            common["gain"] = d.gain
            common["terminals"] = {
                "OUT_P": _terminal_summary(d.out_p, nets),
                "OUT_N": _terminal_summary(d.out_n, nets),
                "IN_P":  _terminal_summary(d.in_p, nets),
                "IN_N":  _terminal_summary(d.in_n, nets),
            }
        directives.append(common)

    active_nets = sorted({_net_name(i) for i in _collect_active_nets(
        loaded.annotations.directives, proj)})

    # Per-via segment records (one entry per inserted coupling Resistor),
    # returned alongside the Problem from build_problem so we can ship the
    # exact per-hop R to the viewer. Empty when the caller didn't pass them.
    segment_records: list[dict] = list(via_segment_records or [])

    # Bucket segments by (x, y, net_index) so each via in `vias` can carry
    # its own list. Rounding keeps coincident vias from drifting due to
    # float noise; 1 µm tolerance is well below any meaningful via spacing.
    def _site_key(x, y, net_i):
        return (round(float(x) * 1000.0), round(float(y) * 1000.0), int(net_i))

    segments_by_site: dict[tuple[int, int, int], list[dict]] = {}
    for i, seg in enumerate(segment_records):
        _gil_yield(i)
        k = _site_key(seg["x_mm"], seg["y_mm"], seg["net_index"])
        segments_by_site.setdefault(k, []).append({
            "layer_a": seg["layer_a"],
            "layer_b": seg["layer_b"],
            "hop_length_mm": seg["hop_length_mm"],
            "resistance_ohm": seg["resistance_ohm"],
            "is_conductive_fill": bool(seg.get("is_conductive_fill", False)),
        })

    # Compact via list for the viewer's marker overlay and per-via current /
    # power calc: location, net, span, drill, AND the per-segment resistance
    # list the FEM actually inserted (so the viewer doesn't have to re-derive
    # it). ~100-300 bytes per via — negligible pickle bloat for typical boards.
    vias = []
    for i, v in enumerate(proj.vias):
        _gil_yield(i)
        site_segments = segments_by_site.get(
            _site_key(v.center.x, v.center.y, v.net_index), []
        )
        ipc_type = int(getattr(v, "ipc4761_via_type", 0) or 0)
        fill_mat = str(getattr(v, "fill_material", "") or "")
        vias.append({
            "x_mm": v.center.x,
            "y_mm": v.center.y,
            "net": _net_name(v.net_index),
            "diameter_mm": v.diameter_mm,
            "hole_diameter_mm": v.hole_diameter_mm,
            "layer_start": v.layer_start,
            "layer_end": v.layer_end,
            "segments": site_segments,
            "ipc4761_via_type": ipc_type,
            "ipc4761_label": ipc4761_label(ipc_type, fill_mat),
            "fill_material": fill_mat,
            "is_conductive_fill": _is_conductive_fill(ipc_type, fill_mat),
        })

    # Plated through-hole pads — same coupling-site treatment as vias in the
    # FEM (see ``_via_through_holes``), so we ship them to the viewer with the
    # same shape as ``vias`` for the marker / 3D cylinder overlay. Span is the
    # full enabled copper stack (Altium pads have no blind/buried span).
    # Non-plated through holes (NPTH) — mechanical / mounting holes with no
    # copper barrel and no net. They carry no electrical role, so they are
    # NOT coupled into the FEM like plated through holes; they are shipped
    # to the viewer purely for the "Non Plated TH" Board Features overlay.
    npth: list[dict] = []
    pths: list[dict] = []
    if enabled:
        pth_layer_start = enabled[0]
        pth_layer_end = enabled[-1]
        comp_des_by_idx = {i: c.designator
                            for i, c in enumerate(proj.pcb_components)}
        for i, p in enumerate(proj.pads):
            _gil_yield(i)
            if not p.is_through_hole:
                continue
            if not getattr(p, "is_plated", True):
                # NPTH: draw it as a hole, not a plated barrel. Use the
                # drilled hole diameter, falling back to the pad extent for
                # the rare hole-less mechanical pad.
                npth_d = float(p.hole_mm) or max(
                    float(p.width_mm), float(p.height_mm))
                if npth_d > 0:
                    npth.append({
                        "x_mm": p.center.x,
                        "y_mm": p.center.y,
                        "diameter_mm": npth_d,
                    })
                continue
            site_segments = segments_by_site.get(
                _site_key(p.center.x, p.center.y, p.net_index), []
            )
            diameter_mm = max(float(p.width_mm), float(p.height_mm))
            comp_des = comp_des_by_idx.get(p.component_index, "")
            designator = (f"{comp_des}-{p.designator}" if comp_des
                          else str(p.designator))
            pths.append({
                "x_mm": p.center.x,
                "y_mm": p.center.y,
                "net": _net_name(p.net_index),
                "diameter_mm": diameter_mm,
                "hole_diameter_mm": float(p.hole_mm),
                "layer_start": pth_layer_start,
                "layer_end": pth_layer_end,
                "designator": designator,
                "segments": site_segments,
                # Through-hole pads have no IPC-4761 row in Altium — show
                # an em-dash so the Vias-tab column is uniform.
                "ipc4761_via_type": 0,
                "ipc4761_label": "—",
                "fill_material": "",
                "is_conductive_fill": False,
            })

    # Pad outlines for the viewer's Overlays control (the Pads row) —
    # every SMT or
    # through-hole pad reduced to its exterior copper polygon (no drill
    # subtraction) plus the list of enabled copper layer_ids it sits on.
    # SMT pads live on one layer; through-hole / multi-layer pads span the
    # full enabled stack. Pickle cost is small (~tens of KB on typical
    # boards) and the viewer needs no shapely / extract_project access.
    pads_outline: list[dict] = []
    if enabled:
        comp_des_by_idx_p = {i: c.designator
                             for i, c in enumerate(proj.pcb_components)}
        for i, p in enumerate(proj.pads):
            _gil_yield(i)
            # Non-plated through holes are surfaced as the dedicated
            # "Non Plated TH" overlay, not as a copper Pad.
            if p.is_through_hole and not getattr(p, "is_plated", True):
                continue
            shape = _pad_outer_shape(p)
            if shape is None or shape.is_empty:
                continue
            exterior = getattr(shape, "exterior", None)
            if exterior is None or exterior.is_empty:
                continue
            # exterior.coords includes the closing vertex (last == first).
            ring = [[float(x), float(y)] for x, y in exterior.coords]
            if len(ring) < 3:
                continue
            if p.is_through_hole or p.layer_id == 74:  # 74 = Multi-Layer
                layer_ids = list(enabled)
            elif p.layer_id in enabled:
                layer_ids = [p.layer_id]
            else:
                continue
            comp_des = comp_des_by_idx_p.get(p.component_index, "")
            designator = (f"{comp_des}-{p.designator}" if comp_des
                          else str(p.designator))
            pads_outline.append({
                "outline": ring,
                "layer_ids": layer_ids,
                "is_through_hole": bool(p.is_through_hole),
                "is_smt": bool(p.is_smt),
                "designator": designator,
                "net": _net_name(p.net_index),
                "width_mm": float(p.width_mm),
                "height_mm": float(p.height_mm),
                "hole_mm": float(p.hole_mm),
                "shape_code": int(p.shape),
                "rotation_deg": float(p.rotation_deg),
                "corner_radius_pct": int(p.corner_radius_pct),
                "x_mm": float(p.center.x),
                "y_mm": float(p.center.y),
            })

    # Per-primitive copper records for the viewer's click-to-select
    # feature. Mirrors the per-(layer, net) ``all_copper`` polygons but
    # preserves individual track / arc / region / shape-based-region /
    # fill records so the viewer can identify the exact primitive under
    # the cursor and show its geometric properties. Vias and pads are
    # already individually recorded above. Netless primitives are
    # included (with ``net="(none)"``, mirroring ``_net_name``) so
    # unrouted copper stays selectable in the viewer.
    primitives: dict[str, list[dict]] = {
        "tracks": [],
        "arcs": [],
        "regions": [],
        "shape_based_regions": [],
        "fills": [],
    }
    for i, t in enumerate(proj.tracks):
        _gil_yield(i)
        primitives["tracks"].append({
            "id": len(primitives["tracks"]),
            "kind": "track",
            "layer_id": int(t.layer_id),
            "net": _net_name(t.net_index),
            "ax": float(t.a.x), "ay": float(t.a.y),
            "bx": float(t.b.x), "by": float(t.b.y),
            "width_mm": float(t.width_mm),
            "is_polygon_outline": bool(t.is_polygon_outline),
            "is_keepout": bool(t.is_keepout),
        })
    for i, a in enumerate(proj.arcs):
        _gil_yield(i)
        primitives["arcs"].append({
            "id": len(primitives["arcs"]),
            "kind": "arc",
            "layer_id": int(a.layer_id),
            "net": _net_name(a.net_index),
            "cx": float(a.center.x), "cy": float(a.center.y),
            "radius_mm": float(a.radius_mm),
            "start_angle_deg": float(a.start_angle_deg),
            "end_angle_deg": float(a.end_angle_deg),
            "width_mm": float(a.width_mm),
            "is_keepout": bool(a.is_keepout),
        })
    for i, rg in enumerate(proj.regions):
        _gil_yield(i)
        primitives["regions"].append({
            "id": len(primitives["regions"]),
            "kind": "region",
            "layer_id": int(rg.layer_id),
            "net": _net_name(rg.net_index),
            "outline": [[float(p.x), float(p.y)] for p in rg.outline],
            "holes": [[[float(p.x), float(p.y)] for p in h]
                      for h in rg.holes],
            "kind_code": int(rg.kind),
            "is_polygon_outline": bool(rg.is_polygon_outline),
            "is_keepout": bool(rg.is_keepout),
            "is_board_cutout": bool(rg.is_board_cutout),
            "polygon_index": int(rg.polygon_index),
        })
    # Shape-based regions: outline can contain arc segments. The saved
    # ``outline`` is a tessellated polyline (so the viewer can hit-test
    # with a plain shapely polygon); ``arc_edge_count`` is reported in
    # the properties panel.
    for i, rg in enumerate(proj.shape_based_regions):
        _gil_yield(i)
        pts: list[list[float]] = []
        arc_count = 0
        n = len(rg.outline)
        for vi in range(n):
            v = rg.outline[vi]
            pts.append([float(v.pos.x), float(v.pos.y)])
            if v.is_arc:
                arc_count += 1
                start = math.radians(v.start_angle_deg)
                end = math.radians(v.end_angle_deg)
                sweep = end - start
                if sweep <= 1e-9:
                    sweep += 2.0 * math.pi
                steps = max(2, int(abs(sweep) / math.radians(12.0)) + 1)
                for k in range(1, steps):
                    ang = start + sweep * k / steps
                    pts.append([
                        float(v.center.x) + v.radius_mm * math.cos(ang),
                        float(v.center.y) + v.radius_mm * math.sin(ang),
                    ])
        primitives["shape_based_regions"].append({
            "id": len(primitives["shape_based_regions"]),
            "kind": "shape_based_region",
            "layer_id": int(rg.layer_id),
            "net": _net_name(rg.net_index),
            "outline": pts,
            "holes": [[[float(p.x), float(p.y)] for p in h]
                      for h in rg.holes],
            "arc_edge_count": arc_count,
            "kind_code": int(rg.kind),
            "is_polygon_outline": bool(rg.is_polygon_outline),
            "is_keepout": bool(rg.is_keepout),
            "is_board_cutout": bool(rg.is_board_cutout),
            "polygon_index": int(rg.polygon_index),
        })
    for i, f in enumerate(proj.fills):
        _gil_yield(i)
        primitives["fills"].append({
            "id": len(primitives["fills"]),
            "kind": "fill",
            "layer_id": int(f.layer_id),
            "net": _net_name(f.net_index),
            "x1_mm": float(f.x1_mm), "y1_mm": float(f.y1_mm),
            "x2_mm": float(f.x2_mm), "y2_mm": float(f.y2_mm),
            "rotation_deg": float(f.rotation_deg),
            "is_keepout": bool(f.is_keepout),
        })

    # ``problem`` is None on the editor-mode stub path (an Altium project
    # loaded without any SOURCE/REGULATOR directive, opened for manual setup
    # like a Gerber import) — no FEM was assembled, so the network-derived
    # counts are simply zero.
    via_coupling_count = sum(
        1 for n in problem.networks
        # Heuristic: coupling networks have exactly 2 connections + 1 Resistor.
        if len(n.connections) == 2 and len(n.elements) == 1
    ) if problem is not None else 0

    # Stub copper — pieces the FEM stub filter dropped. Each entry carries
    # the layer_id, net name, and the polygon's exterior ring (and any
    # holes) so the viewer can redraw them as a neutral grey overlay,
    # preserving the visual "yes, copper exists here" cue even though no
    # FEM result is computed for them.
    stubs: list[dict] = []
    if stub_pieces_by_pair:
        for (lid, ni), pieces in stub_pieces_by_pair.items():
            net_name = _net_name(ni)
            for piece in pieces:
                stub = _build_stub_record(piece, int(lid), net_name)
                if stub is not None:
                    stubs.append(stub)

    # Per-(layer, net) copper outline rings for every net on the board —
    # active rails, other rails, and signal nets alike. Lets the viewer's
    # Overlays control draw copper that doesn't belong to the currently
    # selected rail. Rings stored as float32 (x, y) arrays — same
    # compact representation used by ``stubs``.
    all_copper: list[dict] = _build_all_copper_records(
        per_net_layers, _net_name,
    )

    # Silkscreen lines, component bounding boxes and reference-designator
    # text for the viewer's Overlays control (Heatmap tab).
    overlay_records = _build_overlay_records(proj, _net_name)

    return {
        "project_name": proj.prjpcb_path.stem,
        "prjpcb_path": str(proj.prjpcb_path),
        "pcbdoc_path": str(proj.pcbdoc_path),
        # Altium user-origin (Board6/ORIGINX,ORIGINY) in mm. Every x_mm/y_mm
        # in this metadata — and the mesh vertices in the bundled solution —
        # has already had this subtracted, so coordinates are in Altium's
        # displayed frame. Absolute (file) coords: relative + board_origin.
        "board_origin_mm": {
            "x": float(proj.board_origin_mm.x),
            "y": float(proj.board_origin_mm.y),
        },
        "extraction_summary": {
            "tracks": len(proj.tracks),
            "arcs": len(proj.arcs),
            "vias": len(proj.vias),
            "pads": len(proj.pads),
            "regions": len(proj.regions),
            "shape_based_regions": len(proj.shape_based_regions),
            "fills": len(proj.fills),
            "pcb_components": len(proj.pcb_components),
            "nets": len(proj.nets),
            "sch_components": len(proj.sch_components),
        },
        "enabled_copper_layer_ids": enabled,
        "stackup": stackup_rows,
        "physics_constants": {
            # Read the *current* module-level values so a Settings-tab
            # override is faithfully recorded in the saved pickle.
            "copper_conductivity_S_per_mm": (
                __import__(
                    "fypa.altium_geometry",
                    fromlist=["COPPER_CONDUCTIVITY_S_PER_MM"],
                ).COPPER_CONDUCTIVITY_S_PER_MM
            ),
            "copper_resistivity_ohm_mm": COPPER_RESISTIVITY_OHM_MM,
            # 1 Ω·mm = 1e-3 Ω·m; 1 Ω·mm = 1e5 µΩ·cm.
            "copper_resistivity_ohm_m": COPPER_RESISTIVITY_OHM_MM * 1.0e-3,
            "copper_resistivity_microohm_cm": COPPER_RESISTIVITY_OHM_MM * 1.0e5,
            # Settings-tab-tunable knobs (recorded so the next viewer open
            # restores them faithfully). Defaults reflect the dataclass.
            "temperature_c": (
                settings.temperature_c if settings is not None else 20.0
            ),
            "copper_temp_coefficient_per_c": (
                settings.copper_temp_coefficient_per_c
                if settings is not None else 0.00393
            ),
            "copper_resistivity_20c_microohm_cm": (
                settings.copper_resistivity_20c_microohm_cm
                if settings is not None
                else COPPER_RESISTIVITY_OHM_MM * 1.0e5
            ),
            "plating_thickness_mm": PLATING_THICKNESS_MM,
            "fallback_via_resistance_ohm": FALLBACK_VIA_RESISTANCE_OHM,
            "coupling_resistance_ohm": COUPLING_RESISTANCE_OHM,
            "conductive_fill_resistivity_ohm_mm":
                CONDUCTIVE_FILL_RESISTIVITY_OHM_MM,
            "note_via_resistance": (
                "Per-via inter-layer resistance is computed from the via's "
                "drill diameter, the plating thickness above, and the "
                "z-distance between the centres of the two copper layers it "
                "bridges: R = rho_Cu * L_hop / (pi * (r_outer^2 - r_inner^2)). "
                "Vias with an IPC-4761 conductive fill (copper / silver paste "
                "and the like) add a parallel fill-rod resistor of resistivity "
                "conductive_fill_resistivity_ohm_mm, so R_hop = R_wall || "
                "R_fill. Hops where the geometry is unknown fall back to "
                "fallback_via_resistance_ohm."
            ),
            "note_coupling_resistance": (
                "When a directive terminal has multiple pins, each pin attaches "
                "to a per-pin NodeID coupled back to the main terminal NodeID "
                "via this resistance (padne star-coupling convention)."
            ),
        },
        "directives": directives,
        "active_nets": active_nets,
        "vias": vias,
        "pths": pths,
        "npth": npth,
        "pads": pads_outline,
        "stubs": stubs,
        "all_copper": all_copper,
        "primitives": primitives,
        # Overlays-control geometry (Heatmap tab): silkscreen lines,
        # component bounding boxes, reference-designator text.
        "silkscreen": overlay_records["silkscreen"],
        "components": overlay_records["components"],
        "designators": overlay_records["designators"],
        # Closed polyline (list of [x_mm, y_mm]) of the PCB's mechanical
        # board outline. Empty list when the project has no outline.
        # Drawn as the "Show board outline" overlay in the viewer.
        "board_outline": [[float(p.x), float(p.y)]
                          for p in proj.board_outline],
        "annotation_warnings": list(loaded.annotations.warnings),
        "annotation_errors": list(loaded.annotations.errors),
        "fem_stats": {
            "padne_layer_count": len(problem.layers) if problem is not None else 0,
            "padne_network_count": (
                len(problem.networks) if problem is not None else 0
            ),
            "via_coupling_network_count": via_coupling_count,
        },
        "mesher_config": (
            {
                "minimum_angle_deg": mesher_config.minimum_angle,
                "maximum_size_mm": mesher_config.maximum_size,
                "adaptive_mesh": mesher_config.is_variable_density,
            } if mesher_config is not None else None
        ),
        "solver_stats": (
            {
                "residual_norm": float(solver_info.residual_norm),
                "ground_node_current_A": float(solver_info.ground_node_current),
            } if solver_info is not None else None
        ),
    }


def _terminal_summary(term, nets) -> dict:
    """Compact terminal summary for the metadata dict.

    ``term`` is ``None`` for the N side of a single-net (PDN_NET) directive —
    an ideal 0 Ω return with no copper; report it as such."""
    if term is None:
        return {"pin_count": 0, "pins": [], "ideal_return": True}
    pins = []
    for pin in term.pins:
        net_name = (nets[pin.net_index].name
                    if 0 <= pin.net_index < len(nets) else "(none)")
        pins.append({
            "pad": pin.pad_designator,
            "layer_id": pin.layer_id,
            "net": net_name,
            "x_mm": pin.point.x,
            "y_mm": pin.point.y,
        })
    return {
        "pin_count": len(pins),
        "pins": pins,
        # The net the directive named (PDN_*_NET) — shown by the Setup tab so
        # the user sees what they asked for even when a SERIES bridge resolved
        # the terminal onto a different (bridged-equivalent) net's pads.
        "requested_net": getattr(term, "requested_net", None),
    }


def _collect_pin_xys_per_layer_net(
    directives,
) -> dict[tuple[int, int], list[tuple[float, float]]]:
    """Index every directive pin by (layer_id, net_index) → list of (x, y).

    Used by :func:`_filter_stub_pieces` to decide which disjoint copper
    sub-pieces are anchored by a SOURCE/SINK/REGULATOR/SERIES pin.
    """
    out: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for d in directives:
        for term in _directive_terminals(d):
            for pin in term.pins:
                out.setdefault((pin.layer_id, pin.net_index), []).append(
                    (pin.point.x, pin.point.y),
                )
    return out


def _collect_via_xys_per_layer_net(
    sites: list[_ViaSite],
) -> dict[tuple[int, int], list[tuple[float, float]]]:
    """Index every via/PTH coupling site's per-layer touchdown by
    (layer_id, net_index) → list of (x, y).

    A single site contributes one entry per layer in its span (because
    each spanned layer gets one coupling connection).
    """
    out: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for s in sites:
        for lid in s.span:
            out.setdefault((lid, s.net_index), []).append((s.x_mm, s.y_mm))
    return out


def _filter_stub_pieces(
    shape: shapely.geometry.base.BaseGeometry,
    pin_xys: list[tuple[float, float]],
    via_xys: list[tuple[float, float]],
) -> tuple[shapely.geometry.base.BaseGeometry, list]:
    """Drop sub-pieces of a (layer, net) shape that can't carry current.

    A sub-piece is **kept** if either:

    * it contains at least one directive pin (SOURCE / SINK / REGULATOR /
      SERIES — anything that injects or draws current), OR
    * it contains at least two via couplings (so current has a closed-loop
      path through it — in via, out via).

    Otherwise the piece is a **dead-end stub** (zero or one via, no pins):
    no current can physically flow through it. Including such pieces in
    the FEM creates a rank-deficient sub-matrix whose solution is
    arbitrary — the solver yields garbage potentials (often "0 V mixed
    with valid V") and leaks current into the ground-balancing node,
    making the rest of the heatmap less trustworthy too.

    This applies even when the shape is a single connected polygon. The
    active-nets filter only checks whether the NET is active (has a
    driver on any physical layer); a single polygon on a DIFFERENT
    physical layer that has no pins and no/one via would otherwise pass
    into the FEM as an isolated sub-system, producing garbage voltages.

    Returns ``(filtered_shape, dropped_pieces)``. The filtered shape is
    an empty MultiPolygon if every piece was dropped. ``dropped_pieces``
    is the list of stub Polygon objects that were excluded — the caller
    ships these to the viewer so the user can still SEE the copper
    rendered as a neutral grey overlay (otherwise the copper just
    disappears from the heatmap, which is misleading).
    """
    if shape.is_empty:
        return shape, []
    geoms = (list(shape.geoms)
             if shape.geom_type == "MultiPolygon" else [shape])
    pin_pts = [shapely.geometry.Point(x, y) for x, y in pin_xys]
    # Dedupe vias by XY (rounded to 1 µm). Stacked microvias at the same
    # (x, y) contribute multiple sites — one per pair of adjacent layers
    # they span — but they all touch the same single mesh vertex on each
    # intermediate layer, so they only provide ONE connection point per
    # piece, not N. Counting them separately would incorrectly keep Layer-2
    # via-cap stubs that are really single-point couplings.
    unique_via_xys = list({(round(x * 1000), round(y * 1000)): (x, y)
                           for x, y in via_xys}.values())
    via_pts = [shapely.geometry.Point(x, y) for x, y in unique_via_xys]

    # STRtree-accelerate the "which points fall inside which piece" test.
    # Without it, the naive nested loop is O(pieces × (pins + vias)); for
    # boards with hundreds of disjoint copper sub-pieces × dozens of pin
    # touchdowns this becomes the dominant cost of the loader. Building one
    # tree of all pin+via points and querying per piece collapses that to
    # O((pieces + points) · log(points)) inside C.
    pin_count = len(pin_pts)
    all_pts = pin_pts + via_pts
    tree = shapely.strtree.STRtree(all_pts) if all_pts else None

    kept: list = []
    dropped: list = []
    for piece in geoms:
        if tree is None:
            # No driver geometry at all on this (layer, net) — every piece
            # is a stub.
            dropped.append(piece)
            continue
        cand_idx = tree.query(piece)  # bbox-prefilter against this piece
        has_pin = False
        n_via = 0
        for j in cand_idx:
            j = int(j)
            pt = all_pts[j]
            if not piece.intersects(pt):
                continue
            if j < pin_count:
                has_pin = True
                break  # one pin is enough to keep
            n_via += 1
            if n_via >= 2:
                break  # two distinct vias is enough to keep
        if has_pin or n_via >= 2:
            kept.append(piece)
        else:
            dropped.append(piece)
    # Always return a MultiPolygon — padne's Layer accesses ``shape.geoms``.
    if not kept:
        return shapely.geometry.MultiPolygon(), dropped
    return shapely.geometry.MultiPolygon(kept), dropped


def _drop_unreachable_layers(
    pp_layers: list[_pp.Layer],
    layer_by_layer_and_net: dict[tuple[int, int], _pp.Layer],
    directive_networks: list[_pp.Network],
    coupling_networks: list[_pp.Network],
    stub_pieces_by_pair: dict[tuple[int, int], list],
    nets_tuple,
) -> tuple[list[_pp.Layer], list[_pp.Network]]:
    """BFS-filter: drop FEM layers unreachable from any directive network.

    A (layer, net) slab is *reachable* if it either:

    * has a direct connection from a SOURCE / SINK / REGULATOR / SERIES
      directive, OR
    * can be reached by traversing via-coupling resistor edges from a
      reachable layer.

    Slabs that satisfy neither condition are isolated sub-systems in the
    FEM — the linear solver has no forcing function for them and produces
    arbitrary (often 0 V or wildly wrong) potentials.  This is the
    underlying cause of "huge voltage drop in copper with no current
    path": the slab has 2+ via footprints touching it (so it passes the
    per-piece stub filter), but those vias only connect it to other
    isolated slabs, never to a directive.

    Dropped slabs are moved into ``stub_pieces_by_pair`` so the viewer
    still renders them as a neutral grey overlay (the copper physically
    exists — it just carries no FEM-computed current).

    Coupling networks that reference a dropped layer are also removed so
    the final Problem is internally consistent.
    """
    if not pp_layers:
        return pp_layers, coupling_networks

    key_by_layer: dict[int, tuple[int, int]] = {
        id(layer): key for key, layer in layer_by_layer_and_net.items()
    }

    # Build undirected adjacency: layer-object-id → set of neighbour ids.
    # Edges come from coupling networks (2 connections = 1 edge).
    neighbours: dict[int, set[int]] = {id(L): set() for L in pp_layers}
    for net in coupling_networks:
        ids = [id(c.layer) for c in net.connections if id(c.layer) in neighbours]
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                neighbours[a].add(b)
                neighbours[b].add(a)

    # Seed the BFS with every layer touched by a directive network.
    reachable: set[int] = set()
    queue: list[int] = []
    for net in directive_networks:
        for c in net.connections:
            lid = id(c.layer)
            if lid in neighbours and lid not in reachable:
                reachable.add(lid)
                queue.append(lid)

    # BFS
    while queue:
        node = queue.pop()
        for nb in neighbours[node]:
            if nb not in reachable:
                reachable.add(nb)
                queue.append(nb)

    unreachable = [L for L in pp_layers if id(L) not in reachable]
    if not unreachable:
        return pp_layers, coupling_networks

    def _net_name(idx: int) -> str:
        if 0 <= idx < len(nets_tuple):
            return nets_tuple[idx].name
        return "?"

    unreachable_ids = {id(L) for L in unreachable}
    unreachable_keys = [key_by_layer[id(L)] for L in unreachable if id(L) in key_by_layer]
    log.info(
        "Connectivity filter: dropping %d FEM layer(s) with no reachable "
        "directive — isolated sub-system(s) that would yield garbage "
        "potentials. Pair(s): %s",
        len(unreachable),
        ", ".join(
            f"layer{lid}|{_net_name(ni)}" for lid, ni in unreachable_keys[:12]
        ) + ("…" if len(unreachable_keys) > 12 else ""),
    )

    # Move dropped layers' geometry to stub overlay.
    for key in unreachable_keys:
        dropped_layer = layer_by_layer_and_net.pop(key, None)
        if dropped_layer is None or dropped_layer.shape.is_empty:
            continue
        geoms = (list(dropped_layer.shape.geoms)
                 if dropped_layer.shape.geom_type == "MultiPolygon"
                 else [dropped_layer.shape])
        stub_pieces_by_pair.setdefault(key, []).extend(
            p for p in geoms if not p.is_empty
        )

    kept_layers = [L for L in pp_layers if id(L) not in unreachable_ids]
    kept_coupling = [
        net for net in coupling_networks
        if all(id(c.layer) not in unreachable_ids for c in net.connections)
    ]
    return kept_layers, kept_coupling


def build_problem(
    loaded: LoadedProject,
) -> tuple[_pp.Problem, list[dict], dict[tuple[int, int], list],
           list[GeometryLayer]]:
    """Translate a :class:`LoadedProject` into a :class:`pdnsolver.problem.Problem`
    that can be handed straight to :func:`pdnsolver.solver.solve`.

    Each :class:`GeometryLayer` becomes a padne ``Layer`` (preserving id-order
    so connections from through-hole pads land on the top layer they were
    assigned). Each :class:`DirectiveSpec` becomes a ``Network`` carrying one
    lumped element plus one ``Connection`` per resolved pin. Skipped directives
    (pins on un-modelled plane layers, etc.) are logged as warnings.

    Returns ``(problem, via_segment_records, stub_pieces_by_pair,
    per_net_layers)``:

    * ``via_segment_records`` — one dict per inserted via-coupling Resistor
      (location, layer pair, hop length, R) — pass to
      :func:`build_solve_metadata` so the viewer can do per-segment
      current/power analysis with the exact R the FEM used. (We can't
      stash it on the Problem itself because padne's Problem is frozen.)
    * ``stub_pieces_by_pair`` — ``{(layer_id, net_index): [Polygon, ...]}``
      of copper sub-pieces the stub filter excluded from the FEM. The
      viewer renders these as a neutral grey overlay so the user can
      still see the copper exists (vs. it just disappearing from the
      heatmap, which is misleading).
    * ``per_net_layers`` — the full per-(physical_layer, net) Shapely
      geometry for *every* net on the board, active or not. Passed to
      :func:`build_solve_metadata` so the viewer can outline copper
      belonging to other rails / signal nets via the Overlays control.
      The FEM itself only uses the active subset.
    """
    # Build padne Layers per (physical_layer, net) — so each net is its own
    # conductor in the FEM and cross-net unioning artefacts cannot bleed
    # voltage between rails. We restrict the set to "active" rails (nets that
    # any directive touches, plus any net bridged to one via a SERIES):
    # nets the user doesn't analyse don't need their own Layer, and including
    # them just clutters the GUI's layer selector with hundreds of signal
    # nets.
    active_nets = _collect_active_nets(loaded.annotations.directives,
                                       loaded.extracted)

    # Compute the via/PTH coupling sites up front so the stub-filter below
    # knows which sub-pieces have via connections (and how many).
    enabled = loaded.extracted.enabled_copper_layer_ids()
    sites = _via_through_holes(loaded.extracted, enabled)
    active_sites = [s for s in sites if s.net_index in active_nets]

    # Build the (layer, net) → pin/via XY indexes used by the stub filter.
    pin_xys_by_pair = _collect_pin_xys_per_layer_net(
        loaded.annotations.directives,
    )
    via_xys_by_pair = _collect_via_xys_per_layer_net(active_sites)

    # Only the active rails' geometry is needed to assemble the FEM; the
    # other ~thousands of nets (the viewer's "all copper" overlay) are
    # unioned on a background thread that overlaps the rest of this
    # function. _rest_geom_future is joined just before the return.
    _t_geom = time.monotonic()
    active_layers, _rest_geom_future = build_per_net_geometry_layers_split(
        loaded.extracted, active_nets,
    )
    log.info("build_problem: active-net geometry built in %.2fs "
             "(non-active nets unioning in background)",
             time.monotonic() - _t_geom)
    pp_layers: list[_pp.Layer] = []
    layer_by_layer_and_net: dict[tuple[int, int], _pp.Layer] = {}
    stub_pieces_by_pair: dict[tuple[int, int], list] = {}
    skipped_empty_pairs = 0
    for gl in active_layers:
        if active_nets and gl.net_index not in active_nets:
            continue  # belt-and-braces — active_layers is already filtered
        key = (gl.layer_id, gl.net_index)
        # Drop disjoint stub pieces — copper sub-islands with no directive
        # pin and ≤1 via touchdown. Such pieces carry no current; including
        # them in the FEM yields a rank-deficient sub-matrix and the solver
        # produces garbage (e.g. 0 V nodes mixed with valid voltages) plus
        # a large spurious ground-node-balancing current.
        filtered_shape, dropped_pieces = _filter_stub_pieces(
            gl.shape,
            pin_xys_by_pair.get(key, []),
            via_xys_by_pair.get(key, []),
        )
        if dropped_pieces:
            stub_pieces_by_pair[key] = dropped_pieces
        if filtered_shape.is_empty:
            # Every piece on this (layer, net) was a stub — skip the
            # padne Layer entirely. Directives/vias that wanted to land
            # here will be reported missing by the consumer code.
            skipped_empty_pairs += 1
            continue
        pl = _pp.Layer(
            shape=filtered_shape, name=gl.name, conductance=gl.conductance,
        )
        pp_layers.append(pl)
        layer_by_layer_and_net[key] = pl
    total_dropped = sum(len(v) for v in stub_pieces_by_pair.values())
    log.info("Built %d per-(layer, net) padne Layer(s) for %d active rail(s) "
             "across %d physical copper layer(s).",
             len(pp_layers), len(active_nets),
             # Read enabled-copper-layer count from extracted directly so we
             # don't trigger the lazy geometry build just to log a number.
             len(loaded.extracted.enabled_copper_layer_ids()))
    # Per-(layer, net) piece membership diagnostic. For each kept piece,
    # list the directive pins inside it and the via count. Surfaces cases
    # like: "R2's Pgnd pad lands on a piece with 0 vias" — meaning that
    # piece has no copper path to the other physical layer (and therefore
    # no path back to the reference vertex through ground plane stitching).
    # Such pieces produce the large ground-node-current symptom.
    nets_for_diag = loaded.extracted.nets
    def _diag_net_name(idx: int) -> str:
        return nets_for_diag[idx].name if 0 <= idx < len(nets_for_diag) else "?"
    # Build per-(layer, net) pin → designator/role index so we can name
    # which directive's pin lands where.
    pin_owner_by_pair: dict[tuple[int, int],
                            list[tuple[float, float, str, str]]] = {}
    for d in loaded.annotations.directives:
        role = type(d).__name__.replace("Spec", "")
        terms = _directive_terminals(d)
        for term in terms:
            for pin in term.pins:
                pin_owner_by_pair.setdefault(
                    (pin.layer_id, pin.net_index), []
                ).append((pin.point.x, pin.point.y, d.designator, role))
    # Per-(layer, net) piece-membership diagnostic — DEBUG-only. Testing each
    # kept piece against every directive pin and via is O(pieces × (pins +
    # vias)) shapely point-in-polygon calls (~2 s on a large board) and emits
    # hundreds of log lines, so the whole block is skipped unless DEBUG is on.
    if log.isEnabledFor(logging.DEBUG):
        for key, padne_layer in layer_by_layer_and_net.items():
            lid, ni = key
            net_name = _diag_net_name(ni)
            geoms = (list(padne_layer.shape.geoms)
                     if padne_layer.shape.geom_type == "MultiPolygon"
                     else [padne_layer.shape])
            # Dedupe vias by 1µm to match the stub filter.
            via_xys_raw = via_xys_by_pair.get(key, [])
            via_xys_unique = list(
                {(round(x * 1000), round(y * 1000)): (x, y) for x, y in via_xys_raw}
                .values()
            )
            owners = pin_owner_by_pair.get(key, [])
            if len(geoms) <= 1 and not owners and len(via_xys_unique) <= 1:
                continue  # Trivial single-piece layer with nothing notable.
            log.debug("  [diag] layer%d|%s: %d piece(s) kept, %d total via(s), "
                      "%d directive pin(s)",
                      lid, net_name, len(geoms),
                      len(via_xys_unique), len(owners))
            # Only enumerate per-piece details when there are multiple pieces
            # OR a single piece with notable contents — keeps the log readable
            # on big nets while still showing the diagnostic info.
            if len(geoms) > 1 or owners:
                for i, piece in enumerate(geoms):
                    pins_in_piece = [
                        f"{des}({role})" for (x, y, des, role) in owners
                        if piece.intersects(shapely.geometry.Point(x, y))
                    ]
                    vias_in_piece = sum(
                        1 for (x, y) in via_xys_unique
                        if piece.intersects(shapely.geometry.Point(x, y))
                    )
                    area_mm2 = piece.area
                    flag = ""
                    # Pieces with pins but 0 vias on a multi-layer net are the
                    # canonical "isolated pad" case: the directive lands here
                    # but there's no copper path to another layer.
                    if pins_in_piece and vias_in_piece == 0:
                        flag = "  ⚠ pin(s) but 0 vias — isolated from other layers"
                    log.debug("    piece#%d  area=%.3f mm²  vias=%d  pins=%s%s",
                              i, area_mm2, vias_in_piece,
                              ", ".join(pins_in_piece) if pins_in_piece else "(none)",
                              flag)

    if total_dropped or skipped_empty_pairs:
        log.info("Stub filter: dropped %d dead-end copper sub-piece(s); "
                 "skipped %d (layer, net) pair(s) that were entirely stubs.",
                 total_dropped, skipped_empty_pairs)

    # Cross-layer connectivity check, per net. Group every (layer, piece)
    # for a given net into connected components, where two pieces are in the
    # same component if they share a via XY (i.e., a via that lands inside
    # both pieces creates a coupling resistor between them). Pieces in
    # different components have NO copper path to each other — current sunk
    # into one component cannot return through another.
    #
    # This is the canonical diagnostic for "ground plane is fragmented" or
    # "ferrite bead's pads aren't on the same electrical island as the loads."
    nets_with_pins: dict[int, list[tuple[int, int, str, str]]] = {}
    # ↑ {net_idx: [(layer_id, piece_idx, designator, role), ...]}
    pieces_by_net: dict[int, list[tuple[int, int, shapely.geometry.base.BaseGeometry]]] = {}
    # ↑ {net_idx: [(layer_id, piece_idx, piece_polygon), ...]}
    via_xys_by_net: dict[int, list[tuple[float, float]]] = {}
    for key, padne_layer in layer_by_layer_and_net.items():
        lid, ni = key
        geoms = (list(padne_layer.shape.geoms)
                 if padne_layer.shape.geom_type == "MultiPolygon"
                 else [padne_layer.shape])
        for pi, piece in enumerate(geoms):
            pieces_by_net.setdefault(ni, []).append((lid, pi, piece))
        for x, y, des, role in pin_owner_by_pair.get(key, []):
            for pi, piece in enumerate(geoms):
                if piece.intersects(shapely.geometry.Point(x, y)):
                    nets_with_pins.setdefault(ni, []).append((lid, pi, des, role))
                    break
        # Collect via XYs for this net (deduped)
        if ni not in via_xys_by_net:
            via_xys_by_net[ni] = []
        seen = {(round(x*1000), round(y*1000)) for x, y in via_xys_by_net[ni]}
        for x, y in via_xys_by_pair.get(key, []):
            k_xy = (round(x*1000), round(y*1000))
            if k_xy not in seen:
                seen.add(k_xy)
                via_xys_by_net[ni].append((x, y))

    for ni, pieces in pieces_by_net.items():
        # Union-Find over piece indices for this net.
        parent = list(range(len(pieces)))
        def _find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        def _union(a: int, b: int) -> None:
            ra, rb = _find(a), _find(b)
            if ra != rb:
                parent[rb] = ra
        # For each via XY on this net, union all pieces containing it.
        for vx, vy in via_xys_by_net.get(ni, []):
            pt = shapely.geometry.Point(vx, vy)
            indices_touched = [
                idx for idx, (_, _, piece) in enumerate(pieces)
                if piece.intersects(pt)
            ]
            for a, b in zip(indices_touched, indices_touched[1:]):
                _union(a, b)

        # Group into components.
        components: dict[int, list[int]] = {}
        for idx in range(len(pieces)):
            components.setdefault(_find(idx), []).append(idx)
        if len(components) <= 1:
            continue  # All pieces connected — healthy net.

        net_name = _diag_net_name(ni)
        # Only report nets that have multiple components AND directive pins
        # in more than one component — those are the ones that actually
        # break the FEM (a multi-component net with all pins in one
        # component still solves correctly, the other components are stubs).
        pins_per_component: dict[int, list[tuple[int, int, str, str]]] = {}
        for (lid, pi, des, role) in nets_with_pins.get(ni, []):
            # Find which piece index this pin maps to
            for idx, (lid2, pi2, _) in enumerate(pieces):
                if lid == lid2 and pi == pi2:
                    pins_per_component.setdefault(_find(idx), []).append(
                        (lid, pi, des, role)
                    )
                    break
        # Always log nets with directive pins split across components.
        if len(pins_per_component) >= 2:
            log.warning(
                "  [diag] CONNECTIVITY BREAK on net %r: %d disconnected "
                "components, directive pins split across %d of them. "
                "Current sunk into one component cannot return through another "
                "— this drives the ground-balancing current in the solver.",
                net_name, len(components), len(pins_per_component),
            )
            for ci, (root, comp_indices) in enumerate(components.items()):
                total_area = sum(pieces[i][2].area for i in comp_indices)
                pins_here = pins_per_component.get(root, [])
                pin_summary = (
                    ", ".join(sorted({f"{des}({role})"
                                       for (_, _, des, role) in pins_here}))
                    if pins_here else "(no directive pins)"
                )
                layer_counts: dict[int, int] = {}
                for i in comp_indices:
                    layer_counts[pieces[i][0]] = layer_counts.get(pieces[i][0], 0) + 1
                layer_summary = ", ".join(
                    f"L{lid}×{n}" for lid, n in sorted(layer_counts.items())
                )
                log.warning(
                    "    component %d: %d piece(s) [%s], total area=%.1f mm², "
                    "pins=%s",
                    ci, len(comp_indices), layer_summary, total_area, pin_summary,
                )

    # Log every resolved directive at INFO level so the solve log always
    # shows what's in the FEM — makes it easy to spot wrong resistance
    # values, unexpected net connections, or missing elements.
    _nets_log = loaded.extracted.nets
    def _pin_net_names(term) -> str:
        if term is None:
            return "(ideal return)"
        names = {_nets_log[p.net_index].name
                 for p in term.pins if 0 <= p.net_index < len(_nets_log)}
        return ", ".join(sorted(names)) or "(none)"

    for d in loaded.annotations.directives:
        if isinstance(d, SourceSpec):
            log.info("  SOURCE  %s: %.4g V  P=%s  N=%s",
                     d.designator, d.voltage,
                     _pin_net_names(d.p), _pin_net_names(d.n))
        elif isinstance(d, SinkSpec):
            log.info("  SINK    %s: %.4g A  P=%s  N=%s",
                     d.designator, d.current,
                     _pin_net_names(d.p), _pin_net_names(d.n))
        elif isinstance(d, ResistorSpec):
            log.info("  SERIES  %s: %.4g Ω (%.4g mΩ)  P=%s  N=%s",
                     d.designator, d.resistance, d.resistance * 1000,
                     _pin_net_names(d.p), _pin_net_names(d.n))
        elif isinstance(d, RegulatorSpec):
            log.info("  REGULATOR %s: %.4g V  OUT_P=%s  OUT_N=%s  IN_P=%s  IN_N=%s",
                     d.designator, d.voltage,
                     _pin_net_names(d.out_p), _pin_net_names(d.out_n),
                     _pin_net_names(d.in_p), _pin_net_names(d.in_n))

    log.info("Physics constants in effect: COUPLING_RESISTANCE_OHM=%.4g Ω, "
             "FALLBACK_VIA_RESISTANCE_OHM=%.4g Ω, PLATING_THICKNESS_MM=%.4g",
             COUPLING_RESISTANCE_OHM, FALLBACK_VIA_RESISTANCE_OHM,
             PLATING_THICKNESS_MM)

    # Single-net (PDN_NET) directives sharing one analysis group share an
    # ideal 0 Ω return node so their point-to-point current loop closes.
    # Mint one NodeID per return group up front; _directive_to_network wires
    # every single-net SOURCE/SINK in that group to it.
    return_ref_nodes: dict[int, _pp.NodeID] = {}
    for d in loaded.annotations.directives:
        rg = getattr(d, "return_group", None)
        if rg is not None and rg not in return_ref_nodes:
            return_ref_nodes[rg] = _pp.NodeID()
    if return_ref_nodes:
        log.info("Single-net (PDN_NET) analysis: %d return group(s) with an "
                 "ideal 0 Ω return.", len(return_ref_nodes))

    networks: list[_pp.Network] = []
    for d in loaded.annotations.directives:
        net = _directive_to_network(d, layer_by_layer_and_net, return_ref_nodes)
        if net is None:
            log.warning("Directive %s on %s produced no connections — skipping.",
                        type(d).__name__, d.designator)
            continue
        networks.append(net)

    # Warn when a REGULATOR draws input current from a net that is also
    # downstream of a SERIES element (ferrite bead, inductor DCR, etc.).
    # The regulator's input current flows back through the SERIES element
    # and is NOT counted in "Total active sink current", so the SERIES
    # element will show a much larger voltage drop than the annotated sinks
    # alone would predict — which can make the whole downstream rail look
    # wrong. This is correct FEM behaviour but surprises most users.
    _nets = loaded.extracted.nets
    def _net_name_s(idx: int) -> str:
        return _nets[idx].name if 0 <= idx < len(_nets) else "?"
    # Collect nets that are the "output" side of any SERIES directive.
    series_output_nets: set[str] = set()
    for d in loaded.annotations.directives:
        if isinstance(d, ResistorSpec):
            for pin in d.p.pins:
                series_output_nets.add(_net_name_s(pin.net_index))
            for pin in d.n.pins:
                series_output_nets.add(_net_name_s(pin.net_index))
    # Flag any REGULATOR whose IN_P net is also a SERIES output net.
    for d in loaded.annotations.directives:
        if not isinstance(d, RegulatorSpec):
            continue
        for pin in d.in_p.pins:
            in_net = _net_name_s(pin.net_index)
            if in_net in series_output_nets:
                log.warning(
                    "REGULATOR %s: IN_P is on net %r, which is also fed "
                    "through a SERIES element. The regulator's input current "
                    "flows back through that SERIES element and is NOT counted "
                    "in 'Total active sink current'. If the SERIES element "
                    "shows an unexpectedly large voltage drop, the regulator "
                    "input current is the cause.",
                    d.designator, in_net,
                )

    # Re-emit physical bridges from auto-absorbed low-Ω SERIES directives.
    # The user's R2-style "0Ω jumper between two named-but-physically-
    # separate copper islands" was merged into a single net during load,
    # but the GEOMETRY didn't merge — the two pieces of copper are still
    # physically separated on the PCB, and only the SERIES component
    # bridged them. Materialise that bridge as a same-net Resistor at the
    # original pad locations so the FEM has a real current path between
    # the two formerly-separate-net pieces (now sharing one canonical net).
    for bridge in loaded.absorbed_bridges:
        p_layer = layer_by_layer_and_net.get(
            (bridge.p_layer_id, bridge.canonical_net_index)
        )
        n_layer = layer_by_layer_and_net.get(
            (bridge.n_layer_id, bridge.canonical_net_index)
        )
        if p_layer is None or n_layer is None:
            log.warning(
                "Absorbed bridge %s: cannot re-emit (canonical net layer "
                "missing). FEM may have a disconnected ground island.",
                bridge.designator,
            )
            continue
        # Diagnostic: report which pieces the bridge endpoints land on
        # (sanity check that we're actually bridging two different geom
        # pieces of the same net — i.e. doing useful work).
        p_pt = shapely.geometry.Point(bridge.p_x_mm, bridge.p_y_mm)
        n_pt = shapely.geometry.Point(bridge.n_x_mm, bridge.n_y_mm)
        p_geoms = (list(p_layer.shape.geoms)
                   if p_layer.shape.geom_type == "MultiPolygon"
                   else [p_layer.shape])
        n_geoms = (list(n_layer.shape.geoms)
                   if n_layer.shape.geom_type == "MultiPolygon"
                   else [n_layer.shape])
        p_piece_idx = next(
            (i for i, g in enumerate(p_geoms) if g.intersects(p_pt)), None
        )
        n_piece_idx = next(
            (i for i, g in enumerate(n_geoms) if g.intersects(n_pt)), None
        )
        log.info(
            "Bridge %s (R=%.4g Ω): P→piece#%s @(%.3f, %.3f), "
            "N→piece#%s @(%.3f, %.3f) on layer%d|%s.  "
            "Different pieces? %s",
            bridge.designator, bridge.resistance,
            p_piece_idx, bridge.p_x_mm, bridge.p_y_mm,
            n_piece_idx, bridge.n_x_mm, bridge.n_y_mm,
            bridge.p_layer_id,
            loaded.extracted.nets[bridge.canonical_net_index].name
            if 0 <= bridge.canonical_net_index < len(loaded.extracted.nets)
            else "?",
            p_piece_idx != n_piece_idx,
        )
        node_a, node_b = _pp.NodeID(), _pp.NodeID()
        bridge_resistor = _pp.Resistor(
            a=node_a, b=node_b, resistance=bridge.resistance,
        )
        conns = [
            _pp.Connection(
                layer=p_layer,
                point=p_pt,
                node_id=node_a,
            ),
            _pp.Connection(
                layer=n_layer,
                point=n_pt,
                node_id=node_b,
            ),
        ]
        networks.append(_pp.Network(connections=conns, elements=[bridge_resistor]))
    if loaded.absorbed_bridges:
        log.info(
            "Re-emitted %d physical bridge(s) from absorbed SERIES "
            "directive(s) as same-net coupling resistor(s).",
            len(loaded.absorbed_bridges),
        )

    # Inter-layer coupling: every via + every through-hole pad gets a small
    # Resistor between the (physical layer, via_net) padne Layer pair for
    # each adjacent layer it spans. Because each net is its own Layer, this
    # is automatically net-correct — no oracle filter required.
    #
    # However, coupling is only added for vias on "active" nets — nets
    # actually referenced by a directive's terminal. Adding coupling for
    # signal-net vias (which have no source/sink) creates many tiny floating
    # subsystems that make the FEM matrix singular. Filtering by active nets
    # keeps the FEM well-posed: only the rails the user cares about are
    # solved.
    layer_z_mm = _layer_z_centers_mm(loaded.extracted, enabled)
    coupling, segment_records = _coupling_networks(
        active_sites, layer_by_layer_and_net, layer_z_mm,
    )
    if coupling:
        log.info("Added %d inter-layer coupling network(s) from %d active-net "
                 "via/through-hole site(s) (skipped %d signal-net sites).",
                 len(coupling), len(active_sites), len(sites) - len(active_sites))

    # Drop any FEM layer that has no reachable path to a directive (source /
    # sink / regulator / series) through coupling edges. Such layers are
    # isolated sub-systems: the solver has no forcing function for them and
    # produces arbitrary potentials (e.g. 0 V or other garbage) that corrupt
    # the heatmap. The per-piece stub filter above catches most cases but
    # misses configurations where a piece has 2+ via footprints touching it
    # yet those vias only bridge to other isolated slabs — never to a directive.
    pp_layers, coupling = _drop_unreachable_layers(
        pp_layers, layer_by_layer_and_net,
        networks, coupling,
        stub_pieces_by_pair, loaded.extracted.nets,
    )

    networks.extend(coupling)

    problem = _pp.Problem(
        layers=pp_layers,
        networks=networks,
        project_name=loaded.project_name,
    )
    # Join the backgrounded non-active-net union — it has been running while
    # the FEM assembly above ran. per_net_layers must carry every net: the
    # viewer's "all copper" overlay needs the non-active ones too.
    per_net_layers = active_layers + _rest_geom_future.result()
    return problem, segment_records, stub_pieces_by_pair, per_net_layers


def _count_user_net_references(proj: ExtractedProject) -> dict[str, int]:
    """Count how often each net name appears in user-written ``PDN_*_NET``
    schematic parameters. Used to pick a sensible canonical when merging
    nets: the net the user wrote in their annotations stays as-is, so
    no annotation updates are required after the merge."""
    pdn_net_key_re = re.compile(r"^PDN(\d+)?_.*_NET$", re.IGNORECASE)
    counts: dict[str, int] = {}
    for comp in proj.sch_components:
        for key, value in comp.parameters.items():
            if not value:
                continue
            if pdn_net_key_re.match(key):
                name = str(value).strip().upper()
                if name:
                    counts[name] = counts.get(name, 0) + 1
    return counts


@dataclass(frozen=True, slots=True)
class _AbsorbedBridge:
    """Records the physical bridge a low-Ω SERIES element provided.

    After auto-merge, the SERIES directive's net assignment becomes
    redundant (both pads are now on the canonical net), but the physical
    copper-pad-to-copper-pad connection it provided is the ONLY thing
    holding the two formerly-separate-net copper islands together. If we
    just absorb the SERIES element and don't replace its bridge, the FEM
    sees the canonical net as two disconnected components and the solver
    falls apart (large ground-balancing currents). build_problem
    materialises this back as a same-net coupling Resistor at the
    original pad locations.
    """
    designator: str
    resistance: float
    p_layer_id: int
    p_x_mm: float
    p_y_mm: float
    n_layer_id: int
    n_x_mm: float
    n_y_mm: float
    canonical_net_index: int


def _build_net_merge_map(
    annotations: AnnotationResult, proj: ExtractedProject,
) -> tuple[dict[int, int], set[str], list[_AbsorbedBridge]]:
    """Identify SERIES directives that are effectively net-merging shorts.

    A SERIES directive with resistance below
    :data:`NET_MERGE_RESISTANCE_THRESHOLD_OHM` is treated as a wire link
    between two nets — its R value is the user's approximation of "0 Ω".
    Modelling such a link as a tiny lumped Resistor between two large
    copper bodies in the FEM creates a fragile two-net topology that the
    direct linear solver handles poorly (large ground-balancing currents,
    huge residuals). Merging the two nets into one eliminates the lumped
    element entirely — the rail becomes a single FEM net with one mesh
    per physical layer, the solver is well-conditioned, and the user's
    "this is a wire" intent is faithfully represented.

    Canonical-selection heuristic per equivalence class (in priority order):

      1. **Most user references** — pick the net name that appears most
         often in raw ``PDN_*_NET`` parameters. The user keeps writing
         that name in their annotations, so it shouldn't get renamed away.
      2. **Shorter name** — generic ground names (``0V``, ``GND``) tend
         to be short.
      3. **Lower index** — final deterministic tiebreak.

    Returns ``(net_remap, skipped_designators)``:
      * ``net_remap`` — ``{non_canonical_net_index: canonical_net_index}``.
      * ``skipped_designators`` — designators of the merged SERIES
        directives. These are excluded from the second annotation parse
        because, after merging, both pins land on the same net and
        ``_autoinfer_2pin_nets`` would correctly fail (same-net pads).
    """
    parent: dict[int, int] = {}

    def _find(x: int) -> int:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(a: int, b: int) -> None:
        ra, rb = _find(a), _find(b)
        if ra == rb:
            return
        # Provisional canonical = smaller index. We'll re-pick the
        # canonical per equivalence class below using a smarter heuristic.
        if ra < rb:
            parent[rb] = ra
        else:
            parent[ra] = rb

    skipped: set[str] = set()
    # Stash per-directive pad info so we can re-emit the physical bridge
    # after the merge — we need both the P and N pin's (layer, x, y).
    candidate_bridges: list[tuple[ResistorSpec, TerminalPin, TerminalPin]] = []
    for d in annotations.directives:
        if not isinstance(d, ResistorSpec):
            continue
        if d.resistance > NET_MERGE_RESISTANCE_THRESHOLD_OHM:
            continue
        p_nets = {pin.net_index for pin in d.p.pins if pin.net_index >= 0}
        n_nets = {pin.net_index for pin in d.n.pins if pin.net_index >= 0}
        merged_any = False
        for pa in p_nets:
            for pb in n_nets:
                if pa != pb:
                    _union(pa, pb)
                    merged_any = True
        if merged_any:
            skipped.add(d.designator.upper())
            # Record one P pin and one N pin to define the bridge location.
            # Picking the first pin of each terminal is sufficient — even if
            # the directive has multi-pin terminals, all pins on one side
            # land on the same merged-net copper.
            if d.p.pins and d.n.pins:
                candidate_bridges.append((d, d.p.pins[0], d.n.pins[0]))

    # Group net_indices into equivalence classes.
    classes: dict[int, set[int]] = {}
    for net_idx in list(parent.keys()):
        classes.setdefault(_find(net_idx), set()).add(net_idx)

    # Pick canonical per class using the user-reference heuristic.
    user_refs = _count_user_net_references(proj)

    def _canonical_key(net_idx: int) -> tuple[int, int, int]:
        if 0 <= net_idx < len(proj.nets):
            name = proj.nets[net_idx].name.upper()
            # Negate the reference count so higher counts sort first
            # (min() is used to pick the canonical).
            return (-user_refs.get(name, 0), len(name), net_idx)
        return (0, 1_000, net_idx)

    remap: dict[int, int] = {}
    for class_members in classes.values():
        if len(class_members) <= 1:
            continue
        canonical = min(class_members, key=_canonical_key)
        for member in class_members:
            if member != canonical:
                remap[member] = canonical

    # Materialise bridge records with the canonical net index resolved.
    bridges: list[_AbsorbedBridge] = []
    for d, p_pin, n_pin in candidate_bridges:
        # Both p_pin.net_index and n_pin.net_index belong to the same
        # equivalence class — apply the remap to get the canonical.
        canonical = remap.get(p_pin.net_index, p_pin.net_index)
        bridges.append(_AbsorbedBridge(
            designator=d.designator,
            resistance=d.resistance,
            p_layer_id=p_pin.layer_id,
            p_x_mm=p_pin.point.x,
            p_y_mm=p_pin.point.y,
            n_layer_id=n_pin.layer_id,
            n_x_mm=n_pin.point.x,
            n_y_mm=n_pin.point.y,
            canonical_net_index=canonical,
        ))
    return remap, skipped, bridges


def _apply_net_remap(
    proj: ExtractedProject, remap: dict[int, int],
) -> ExtractedProject:
    """Return a new ExtractedProject with all net references collapsed
    onto their canonical class representative.

    Every primitive (track, arc, via, pad, region) has its ``net_index``
    rewritten. ``proj.nets`` is left UNCHANGED so name-based lookups in
    the second annotation parse still resolve both the canonical and
    non-canonical names — `parse_annotations` is told about the remap
    separately and applies it after the name lookup to land on the
    canonical index. This way the user's annotations work regardless of
    which of the merged names they wrote.
    """
    if not remap:
        return proj
    dr = dataclasses.replace

    def _r(net_idx: int) -> int:
        return remap.get(net_idx, net_idx)

    return dr(
        proj,
        tracks=tuple(dr(t, net_index=_r(t.net_index)) for t in proj.tracks),
        arcs=tuple(dr(a, net_index=_r(a.net_index)) for a in proj.arcs),
        vias=tuple(dr(v, net_index=_r(v.net_index)) for v in proj.vias),
        pads=tuple(dr(p, net_index=_r(p.net_index)) for p in proj.pads),
        regions=tuple(dr(rg, net_index=_r(rg.net_index)) for rg in proj.regions),
        shape_based_regions=tuple(
            dr(rg, net_index=_r(rg.net_index)) for rg in proj.shape_based_regions
        ),
        fills=tuple(dr(f, net_index=_r(f.net_index)) for f in proj.fills),
        # nets unchanged — both canonical and non-canonical names still
        # resolve, and parse_annotations applies the remap after lookup.
    )


def load_project(prjpcb_path: str | Path,
                 pcbdoc_selector: str | Path | None = None,
                 ) -> LoadedProject:
    """Load and prepare an Altium project for PDN analysis.

    Performs raw extraction + PDN_* annotation parsing. The legacy
    per-physical-layer single-union geometry is built lazily on first
    access of :attr:`LoadedProject.geometry`; the FEM solve path uses
    :func:`build_per_net_geometry_layers` and never touches it. Callers
    should consult :attr:`LoadedProject.is_solveable` and
    :attr:`LoadedProject.annotations.errors` before invoking the solver.

    ``pcbdoc_selector`` picks one of several ``.PcbDoc`` files when the
    project contains more than one (see :func:`fypa.altium.extract.extract_project`).

    Auto-merge pass: SERIES directives below
    :data:`NET_MERGE_RESISTANCE_THRESHOLD_OHM` are detected as electrical
    shorts and their two nets are merged into one before geometry
    building. This eliminates the fragile two-net topology that creates
    large ground-balancing currents in the FEM solver.
    """
    log.info("Stage 1/2: extracting %s", Path(prjpcb_path).name)
    _t = time.monotonic()
    extracted = extract_project(prjpcb_path, pcbdoc_selector=pcbdoc_selector)
    log.info("Stage 1/2: extract done in %.2fs", time.monotonic() - _t)
    enabled = extracted.enabled_copper_layer_ids()
    log.info("  enabled copper layers: %s", enabled)

    log.info("Stage 2/2: parsing PDN_* annotations (pass 1: discover merges)")
    _t = time.monotonic()
    initial_annotations = parse_annotations(extracted, enabled_layers=enabled)
    log.info("Stage 2/2: annotations (pass 1) done in %.2fs",
             time.monotonic() - _t)

    # Identify low-Ω SERIES directives that are functionally net shorts and
    # build the equivalence map. If any merges are needed, rewrite the
    # ExtractedProject so all primitives use the canonical net index, then
    # re-parse annotations on the merged project (skipping the now-redundant
    # SERIES directives, whose two pads would otherwise both resolve to the
    # same merged net and trigger an auto-infer failure).
    net_remap, skipped_designators, absorbed_bridges = _build_net_merge_map(
        initial_annotations, extracted,
    )
    if net_remap:
        merge_descriptions = [
            f"{extracted.nets[old].name!r}→{extracted.nets[new].name!r}"
            for old, new in sorted(net_remap.items())
        ]
        log.warning(
            "Auto-merging %d net(s) bridged by low-Ω SERIES element(s) "
            "(threshold %.4g Ω): %s. Absorbed designator(s): %s",
            len(net_remap), NET_MERGE_RESISTANCE_THRESHOLD_OHM,
            ", ".join(merge_descriptions),
            ", ".join(sorted(skipped_designators)),
        )
        extracted = _apply_net_remap(extracted, net_remap)
        log.info("Stage 2/2: parsing PDN_* annotations (pass 2: on merged nets)")
        _t = time.monotonic()
        annotations = parse_annotations(
            extracted, enabled_layers=enabled,
            skip_designators=skipped_designators,
            net_remap=net_remap,
        )
        log.info("Stage 2/2: annotations (pass 2) done in %.2fs",
                 time.monotonic() - _t)
    else:
        annotations = initial_annotations

    # Annotation warnings are silently stored in metadata but never logged
    # to the log file in the normal (solveable) code path. Log them here at
    # WARNING level so they always appear — auto-inferred SERIES nets are
    # particularly important: a component accidentally bridging two wrong
    # nets (e.g. +3V3L → GND) shows up here and causes large phantom currents.
    for w in annotations.warnings:
        log.warning("Annotation: %s", w)
    for e in annotations.errors:
        log.error("Annotation error: %s", e)

    return LoadedProject(
        extracted=extracted,
        annotations=annotations,
        absorbed_bridges=absorbed_bridges if net_remap else [],
    )


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    if len(sys.argv) != 2:
        print("usage: python -m fypa.altium.loader PATH_TO.PrjPcb", file=sys.stderr)
        sys.exit(2)
    proj = load_project(sys.argv[1])
    print(proj.diagnostic_summary())
    sys.exit(0 if proj.is_solveable else 1)
