"""External-FET SMPS stage binding — connect PATH elements to REGULATOR hosts.

Called by :func:`fypa.altium.annotations.parse_annotations` after the main
parse loop to replace :class:`PathSpec` directives with :class:`ResistorSpec`
(high-side bridges) and :class:`SwitchPathLeg` entries on the host
:class:`RegulatorSpec`.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import replace

from fypa.altium.annotations import (
    AnnotationResult,
    PathSpec,
    RegulatorSpec,
    ResistorSpec,
    SwitchPathLeg,
    TerminalSpec,
    _append_error_once,
    _net_indices_by_name,
)
from fypa.altium.extract import ExtractedProject, NO_NET

log = logging.getLogger(__name__)

# Topologies that support external-FET PATH binding.
_BINDABLE_TOPOLOGIES: frozenset[str] = frozenset(
    {
        "BUCK",
        "BOOST",
        "BUCKBOOST",
    }
)


# ---------------------------------------------------------------------------
# Net helpers
# ---------------------------------------------------------------------------


def _net_index_set(
    proj: ExtractedProject,
    net_name: str | None,
    net_remap: dict[int, int] | None,
) -> frozenset[int]:
    """Resolve a net name to the set of remapped net indices."""
    if not net_name:
        return frozenset()
    indices = _net_indices_by_name(proj, net_name.strip())
    if net_remap:
        indices = [net_remap.get(i, i) for i in indices]
    return frozenset(i for i in indices if i != NO_NET)


def _terminal_net_indices(
    term: TerminalSpec,
    net_remap: dict[int, int] | None,
) -> frozenset[int]:
    """Net indices reachable from *term*'s pins (after remap)."""
    out: set[int] = set()
    for pin in term.pins:
        idx = pin.net_index
        if net_remap:
            idx = net_remap.get(idx, idx)
        if idx != NO_NET:
            out.add(idx)
    return frozenset(out)


def _spec_pad_net_indices(
    spec: PathSpec | ResistorSpec,
    net_remap: dict[int, int] | None,
) -> frozenset[int]:
    """All net indices a two-terminal spec's pads sit on (after remap)."""
    return _terminal_net_indices(spec.p, net_remap) | _terminal_net_indices(
        spec.n,
        net_remap,
    )


# ---------------------------------------------------------------------------
# Host identification
# ---------------------------------------------------------------------------


def _regulator_stage_nets(
    reg: RegulatorSpec,
    proj: ExtractedProject,
    net_remap: dict[int, int] | None,
) -> frozenset[int]:
    """Non-GND net indices declared by a regulator's terminals and SW nets.

    Used for host matching: a PATH part's pad nets must intersect these.
    GND (index 0 by convention, but we filter any net whose *name* is a
    common ground label) is excluded so ground planes don't cause false
    matches.
    """
    gnd_names = frozenset({"GND", "AGND", "DGND", "PGND", "VSS", "AVSS"})
    nets: set[int] = set()
    for term in (reg.out_p, reg.out_n, reg.in_p, reg.in_n):
        nets |= _terminal_net_indices(term, net_remap)
    for sw_name in (reg.sw1_net, reg.sw2_net):
        nets |= _net_index_set(proj, sw_name, net_remap)
    # Filter ground nets by name.
    filtered: set[int] = set()
    for ni in nets:
        if 0 <= ni < len(proj.nets):
            name = proj.nets[ni].name.strip().upper()
            if name not in gnd_names:
                filtered.add(ni)
        else:
            filtered.add(ni)
    return frozenset(filtered)


def _regulator_sw_nets(
    reg: RegulatorSpec,
    proj: ExtractedProject,
    net_remap: dict[int, int] | None,
) -> frozenset[int]:
    """Net indices of a regulator's declared switch nodes."""
    nets: set[int] = set()
    for sw_name in (reg.sw1_net, reg.sw2_net):
        nets |= _net_index_set(proj, sw_name, net_remap)
    return frozenset(nets)


def _find_host_regulator(
    path: PathSpec,
    regulators: list[RegulatorSpec],
    proj: ExtractedProject,
    net_remap: dict[int, int] | None,
    result: AnnotationResult,
) -> RegulatorSpec | None:
    """Find the unique REGULATOR host for a PATH element."""
    pad_nets = _spec_pad_net_indices(path, net_remap)
    diag = f"PATH on {path.designator}"

    if path.smps_host:
        host_u = path.smps_host.strip().upper()
        candidates = [r for r in regulators if r.designator.upper() == host_u]
        if not candidates:
            _append_error_once(
                result,
                f"{diag}: PDN_SMPS_HOST={path.smps_host!r} does not match any REGULATOR designator",
            )
            return None
        if len(candidates) > 1:
            _append_error_once(
                result,
                f"{diag}: PDN_SMPS_HOST={path.smps_host!r} matches multiple "
                f"REGULATOR channels — use a unique designator",
            )
            return None
        return candidates[0]

    # Only a regulator that declares a switch stage can host a PATH part.
    # Without this, any second regulator hanging off the same input rail —
    # an LDO, an internal-FET SMPS — matches on VIN alone and makes every
    # high-side part "ambiguous", which is most boards.
    hosts = [
        r for r in regulators
        if r.sw1_net or r.sw2_net or r.smps_topology
    ]
    if not hosts:
        _append_error_once(
            result,
            f"{diag}: could not bind to a REGULATOR host — no REGULATOR "
            f"declares a switch stage (set PDN_SW1_NET and "
            f"PDN_SMPS_TOPOLOGY on the controller)",
        )
        return None

    # A switch node names exactly one stage; the IN/OUT rails are shared, so
    # they are only a fallback for parts with no pad on a switch node.
    matches = [
        r for r in hosts
        if pad_nets & _regulator_sw_nets(r, proj, net_remap)
    ]
    if not matches:
        matches = [
            r for r in hosts
            if pad_nets & _regulator_stage_nets(r, proj, net_remap)
        ]
    if not matches:
        _append_error_once(
            result,
            f"{diag}: could not bind to a REGULATOR host — none of the "
            f"declared regulators share a non-GND net with this part's pads",
        )
        return None
    if len(matches) > 1:
        host_names = ", ".join(m.designator for m in matches)
        _append_error_once(
            result,
            f"{diag}: ambiguous host — pads touch nets from multiple "
            f"regulators ({host_names}); set PDN_SMPS_HOST to disambiguate",
        )
        return None
    return matches[0]


# ---------------------------------------------------------------------------
# Phase / leg classification
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# SW1↔SW2 chain walk (shunt + inductor)
# ---------------------------------------------------------------------------


def _classify_path(
    path: PathSpec,
    reg: RegulatorSpec,
    proj: ExtractedProject,
    net_remap: dict[int, int] | None,
) -> str:
    """Classify a bound PATH element's role in the stage.

    The regulator element replaces the one PATH part that carries the full
    output current *and* blocks the DC path from IN to OUT — the **cut**. That
    is always the part with a terminal on OUT_P: the inductor in a buck, the
    high-side FET in a boost, the output-side high-side FET in a buck-boost.
    Picking it this way is what lets the element be a plain conductor there
    (it draws ``i_v``, not ``gain·i_v``), with the conversion ratio carried by
    the low-side legs instead.

    Everything else is either a **low-side leg** — it reaches a ground net
    from a switch node, and carries a topology-dependent fraction of ``i_v``
    — or a plain resistive **bridge** whose current KCL settles on its own.
    A boost's inductor and a sense shunt in the inductor chain are both just
    bridges; nothing needs to tell them apart.

    Returns one of ``"cut"`` / ``"ls_in"`` / ``"ls_out"`` / ``"bridge"`` /
    ``"unknown"``.
    """
    all_nets = (
        _terminal_net_indices(path.p, net_remap)
        | _terminal_net_indices(path.n, net_remap)
    )

    in_p_nets = _terminal_net_indices(reg.in_p, net_remap)
    in_n_nets = _terminal_net_indices(reg.in_n, net_remap)
    out_p_nets = _terminal_net_indices(reg.out_p, net_remap)
    out_n_nets = _terminal_net_indices(reg.out_n, net_remap)
    sw1_nets = _net_index_set(proj, reg.sw1_net, net_remap)
    sw2_nets = _net_index_set(proj, reg.sw2_net, net_remap)
    gnd_nets = in_n_nets | out_n_nets
    sw_nets = sw1_nets | sw2_nets

    # The cut. Checked first: when a buck aliases SW2_NET to OUT_P its
    # inductor matches the switch-node tests below as well.
    if all_nets & out_p_nets and all_nets & sw_nets:
        return "cut"

    # A switch node down to a ground net. Which side of the inductor that
    # switch node sits on decides the coefficient, and with only one switch
    # node the net names cannot say: a buck switches on the input side
    # (VIN–HS–SW–L–OUT) and a boost on the output side (VIN–L–SW–HS–OUT).
    # Only the topology distinguishes them. A SW2_NET aliased onto OUT_P or
    # IN_P is an annotation convenience, not a second switch node.
    real_sw2 = sw2_nets - out_p_nets - in_p_nets
    if all_nets & gnd_nets and all_nets & sw_nets:
        if real_sw2:
            return "ls_out" if all_nets & real_sw2 else "ls_in"
        topo = reg.smps_topology or ""
        if topo == "BOOST" or (topo == "BUCKBOOST" and reg.gain >= 1.0):
            return "ls_out"
        return "ls_in"

    # Any other annotated two-terminal part touching the stage is copper the
    # solve should see. Being permissive here is deliberate: the alternative
    # is dropping a conductor the user explicitly annotated.
    stage_nets = in_p_nets | out_p_nets | gnd_nets | sw_nets
    if all_nets & stage_nets and len(all_nets) >= 2:
        return "bridge"

    return "unknown"


def _orient_leg(
    path: PathSpec,
    gnd_nets: frozenset[int],
    net_remap: dict[int, int] | None,
    *,
    from_ground: bool,
) -> tuple[TerminalSpec, TerminalSpec]:
    """Return ``(p, n)`` for a leg carrying current from *p* to *n*.

    A buck's low-side FET freewheels: current flows out of the ground plane,
    through the FET, into the switch node (``from_ground=True``). A boost's
    low-side FET is the opposite — it pulls the inductor current down into
    the ground plane.
    """
    p_on_gnd = bool(_terminal_net_indices(path.p, net_remap) & gnd_nets)
    gnd_term, sw_term = (path.p, path.n) if p_on_gnd else (path.n, path.p)
    return (gnd_term, sw_term) if from_ground else (sw_term, gnd_term)


# ---------------------------------------------------------------------------
# Coefficient computation
# ---------------------------------------------------------------------------


def _compute_ls_coeffs(
    topology: str,
    gain: float,
) -> tuple[float, float]:
    """Return ``(ls_in_coeff, ls_out_coeff)`` for the given topology + gain.

    Coefficients are multiplied by i_v (output current) in the MNA stamp.
    """
    if topology == "BUCK":
        return (max(0.0, 1.0 - gain), 0.0)
    if topology == "BOOST":
        return (0.0, max(0.0, gain - 1.0))
    # BUCKBOOST: use boost coefficients when G >= 1, else buck.
    if gain >= 1.0:
        return (0.0, max(0.0, gain - 1.0))
    return (max(0.0, 1.0 - gain), 0.0)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def finalize_smps_stages(
    result: AnnotationResult,
    proj: ExtractedProject,
    net_remap: dict[int, int] | None = None,
) -> None:
    """Bind PATH elements to REGULATOR hosts, mutating *result.directives*.

    * The **cut** — the PATH part joining a switch node to OUT_P — is
      replaced by the regulator element itself: its resistance becomes
      ``cut_resistance`` and its two pads become the host's in_p / out_p.
      Because that part carries the full output current, the element is a
      plain conductor across it and draws ``i_v``, not ``gain·i_v``.
    * PATH elements reaching a ground net from a switch node become
      :class:`SwitchPathLeg` entries on the host :class:`RegulatorSpec`
      (``switch_path_legs``), carrying the rest of the switch-node current.
    * Every other bound PATH element becomes a :class:`ResistorSpec` so the
      downstream net-merge and rail_groups logic handles it as an ordinary
      series bridge; KCL settles its current on its own.
    * Vin-sense terminals are propagated from hs_in's P-side (or the
      regulator's original in_p if no hs_in exists).
    """
    paths: list[PathSpec] = [d for d in result.directives if isinstance(d, PathSpec)]
    if not paths:
        return

    regulators: list[RegulatorSpec] = [d for d in result.directives if isinstance(d, RegulatorSpec)]
    if not regulators:
        for p in paths:
            _append_error_once(
                result,
                f"PATH on {p.designator}: could not bind to a REGULATOR host "
                f"— no REGULATOR directives found",
            )
        return

    # Group paths by host regulator.
    host_map: dict[str, RegulatorSpec] = {}
    path_groups: dict[str, list[tuple[PathSpec, str]]] = defaultdict(list)
    bound_path_ids: set[int] = set()

    for p in paths:
        host = _find_host_regulator(p, regulators, proj, net_remap, result)
        if host is None:
            continue
        topo = host.smps_topology
        if topo is None and not (host.sw1_net or host.sw2_net):
            _append_error_once(
                result,
                f"PATH on {p.designator}: host {host.designator} has no "
                f"PDN_SMPS_TOPOLOGY and no SW nets — cannot bind",
            )
            continue
        if topo and topo == "INVERTER":
            # Not a warning: every bound PATH is dropped from the directive
            # list, so "skipping" would solve a board with the phase FETs
            # missing and no indication of why the phase net floats.
            _append_error_once(
                result,
                f"PATH on {p.designator}: host {host.designator} uses INVERTER "
                f"topology — external-FET binding is not supported for "
                f"INVERTER, so this part cannot be modelled; remove the PATH "
                f"annotation or model the stage as BUCK / BOOST / BUCKBOOST",
            )
            continue
        if topo and topo not in _BINDABLE_TOPOLOGIES:
            _append_error_once(
                result,
                f"PATH on {p.designator}: host {host.designator} has "
                f"unsupported SMPS_TOPOLOGY={topo!r}",
            )
            continue
        role = _classify_path(p, host, proj, net_remap)
        host_key = f"{host.designator}#{host.channel_index}"
        host_map[host_key] = host
        path_groups[host_key].append((p, role))
        bound_path_ids.add(id(p))

    # A bound part that matches none of the host's stage nets: report it
    # rather than quietly dropping copper the user annotated.
    for host_key, grouped in list(path_groups.items()):
        host = host_map[host_key]
        kept: list[tuple[PathSpec, str]] = []
        for path, role in grouped:
            if role == "unknown":
                _append_error_once(
                    result,
                    f"PATH on {path.designator}: could not classify pad nets "
                    f"against host {host.designator}'s stage nets "
                    f"(IN/OUT/SW) — check net names or set PDN_P_NET / "
                    f"PDN_N_NET explicitly",
                )
                bound_path_ids.discard(id(path))
            else:
                kept.append((path, role))
        path_groups[host_key] = kept

    # Drop host groups that lost every path.
    path_groups = {k: v for k, v in path_groups.items() if v}

    # Build replacement directives.
    new_directives: list = []
    replaced_reg_ids: set[int] = set()

    for host_key, grouped in path_groups.items():
        host = host_map[host_key]
        topo = host.smps_topology
        if topo is None:
            _append_error_once(
                result,
                f"REGULATOR on {host.designator}: PATH parts are bound to it "
                f"but PDN_SMPS_TOPOLOGY is not set — set it to one of "
                f"{', '.join(sorted(_BINDABLE_TOPOLOGIES))} so the switch-leg "
                f"currents can be derived",
            )
            continue
        ls_in_coeff, ls_out_coeff = _compute_ls_coeffs(topo, host.gain)
        gnd_nets = (
            _terminal_net_indices(host.in_n, net_remap)
            | _terminal_net_indices(host.out_n, net_remap)
        )
        in_p_nets = _terminal_net_indices(host.in_p, net_remap)
        out_p_nets = _terminal_net_indices(host.out_p, net_remap)

        bridges: list[ResistorSpec] = []
        ls_legs: list[SwitchPathLeg] = []
        cut_path: PathSpec | None = None
        hs_in_spec: PathSpec | None = None

        for path, role in grouped:
            if role == "cut":
                if cut_path is not None:
                    _append_error_once(
                        result,
                        f"PATH on {path.designator}: host {host.designator} "
                        f"already has {cut_path.designator} between a switch "
                        f"node and OUT_P — only one output-side part per "
                        f"stage is supported",
                    )
                    continue
                cut_path = path
            elif role in ("ls_in", "ls_out"):
                coeff = ls_in_coeff if role == "ls_in" else ls_out_coeff
                if coeff <= 0.0:
                    result.warnings.append(
                        f"PATH on {path.designator}: {role} leg coefficient is "
                        f"zero for {topo} at gain {host.gain:.3f}, so the part "
                        f"carries no averaged current and is left out of the "
                        f"model entirely — its copper is not solved and any "
                        f"PDN_R on it is ignored"
                    )
                    continue
                p_term, n_term = _orient_leg(
                    path,
                    gnd_nets,
                    net_remap,
                    from_ground=(role == "ls_in"),
                )
                ls_legs.append(
                    SwitchPathLeg(
                        designator=path.designator,
                        kind=role,
                        resistance=path.resistance,
                        p=p_term,
                        n=n_term,
                        coeff=coeff,
                    )
                )
            else:
                bridges.append(
                    ResistorSpec(
                        designator=path.designator,
                        schdoc_name=path.schdoc_name,
                        resistance=path.resistance,
                        p=path.p,
                        n=path.n,
                        channel_index=path.channel_index,
                    )
                )
                if hs_in_spec is None and (
                    _terminal_net_indices(path.p, net_remap) & in_p_nets
                    or _terminal_net_indices(path.n, net_remap) & in_p_nets
                ):
                    hs_in_spec = path

        if cut_path is None:
            _append_error_once(
                result,
                f"REGULATOR on {host.designator}: no PATH part joins a switch "
                f"node to OUT_P, so nothing cuts the DC path from IN to OUT "
                f"— annotate the inductor (buck) or the output-side high-side "
                f"FET (boost) with PDN_ROLE=PATH",
            )
            continue

        # The cut element replaces the regulator's own IN_P / OUT_P: OUT_P
        # moves to its output-side pad, IN_P to its switch-node-side pad. The
        # element is then a conductor across that part, carrying i_v.
        if _terminal_net_indices(cut_path.p, net_remap) & out_p_nets:
            updated_out_p, updated_in_p = cut_path.p, cut_path.n
        else:
            updated_out_p, updated_in_p = cut_path.n, cut_path.p

        # Vin sense: the IN_P-side pad of the high-side input bridge, so the
        # sensed voltage includes that part's drop.
        if hs_in_spec is not None:
            if _terminal_net_indices(hs_in_spec.p, net_remap) & in_p_nets:
                vin_sense_p = hs_in_spec.p
            else:
                vin_sense_p = hs_in_spec.n
        else:
            vin_sense_p = host.in_p
        vin_sense_n = host.in_n

        updated_reg = replace(
            host,
            in_p=updated_in_p,
            out_p=updated_out_p,
            switch_path_legs=tuple(ls_legs),
            vin_sense_p=vin_sense_p,
            vin_sense_n=vin_sense_n,
            cut_resistance=cut_path.resistance,
        )
        replaced_reg_ids.add(id(host))

        new_directives.extend(bridges)
        new_directives.append(updated_reg)

    # Rebuild the directive list: keep non-PATH/non-replaced-regulator as-is,
    # replace bound regulators, drop bound PATHs.
    rebuilt: list = []
    for d in result.directives:
        if isinstance(d, PathSpec):
            if id(d) in bound_path_ids:
                continue  # consumed by binding
            # Unbound PATH — already errored; drop from directives.
            continue
        if isinstance(d, RegulatorSpec) and id(d) in replaced_reg_ids:
            continue  # replaced by updated version in new_directives
        rebuilt.append(d)
    rebuilt.extend(new_directives)
    result.directives = rebuilt
