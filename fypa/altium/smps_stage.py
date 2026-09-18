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

    # Auto-match: intersect PATH pad nets with each regulator's stage nets.
    matches: list[RegulatorSpec] = []
    for reg in regulators:
        stage_nets = _regulator_stage_nets(reg, proj, net_remap)
        if pad_nets & stage_nets:
            matches.append(reg)
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


def _path_edge_nets(
    path: PathSpec,
    net_remap: dict[int, int] | None,
) -> tuple[frozenset[int], frozenset[int]]:
    return (
        _terminal_net_indices(path.p, net_remap),
        _terminal_net_indices(path.n, net_remap),
    )


def _assign_sw_chain_roles(
    unclassified: list[PathSpec],
    sw1_nets: frozenset[int],
    sw2_nets: frozenset[int],
    net_remap: dict[int, int] | None,
    host_des: str,
    result: AnnotationResult,
) -> dict[int, str]:
    """Walk PATH edges from SW1 to SW2; last edge = inductor, earlier = shunt.

    Returns ``{id(path): role}`` for paths that sit on the chain.
    """
    if not unclassified or not sw1_nets or not sw2_nets:
        return {}

    # Graph: net_index -> list of (other_net_index, path)
    adj: dict[int, list[tuple[int, PathSpec]]] = defaultdict(list)
    for path in unclassified:
        p_nets, n_nets = _path_edge_nets(path, net_remap)
        # 2-pin idealisation: each terminal contributes one net (take any).
        if not p_nets or not n_nets:
            continue
        # Connect every p-net to every n-net (normally one each).
        for a in p_nets:
            for b in n_nets:
                if a == b:
                    continue
                adj[a].append((b, path))
                adj[b].append((a, path))

    # BFS from SW1 looking for SW2; record parent edge path.
    start = next(iter(sw1_nets))
    goals = set(sw2_nets)
    queue = [start]
    visited: set[int] = {start}
    # net -> (prev_net, path_used)
    came_from: dict[int, tuple[int, PathSpec]] = {}
    found: int | None = None
    while queue:
        cur = queue.pop(0)
        if cur in goals and cur != start:
            found = cur
            break
        for nxt, path in adj.get(cur, []):
            if nxt in visited:
                continue
            visited.add(nxt)
            came_from[nxt] = (cur, path)
            queue.append(nxt)

    if found is None:
        # Direct SW1↔SW2 single-part inductor already classified elsewhere;
        # leftover unknown parts are reported by the caller.
        return {}

    # Reconstruct edge sequence start → found.
    edges: list[PathSpec] = []
    route_nets: set[int] = {found}
    cur = found
    while cur != start:
        prev, path = came_from[cur]
        edges.append(path)
        cur = prev
        route_nets.add(cur)
    edges.reverse()
    if not edges:
        return {}

    roles: dict[int, str] = {}
    for path in edges[:-1]:
        roles[id(path)] = "shunt"
    roles[id(edges[-1])] = "inductor"

    # Ambiguous extra PATH parts touching the chain but not on the unique path.
    on_chain = {id(p) for p in edges}
    for path in unclassified:
        if id(path) in on_chain:
            continue
        pad_nets = _spec_pad_net_indices(path, net_remap)
        if pad_nets & route_nets:
            _append_error_once(
                result,
                f"PATH on {path.designator}: ambiguous SW1↔SW2 chain on host "
                f"{host_des} — part touches the inductor path but is not on "
                f"the unique shortest route; check PDN_P_NET / PDN_N_NET",
            )
    return roles


def _classify_path(
    path: PathSpec,
    reg: RegulatorSpec,
    proj: ExtractedProject,
    net_remap: dict[int, int] | None,
) -> str:
    """Classify a bound PATH element's role in the stage.

    Returns one of:
        ``"hs_in"`` / ``"ls_in"`` / ``"hs_out"`` / ``"ls_out"`` /
        ``"inductor"`` / ``"shunt"`` / ``"unknown"``
    """
    p_nets = _terminal_net_indices(path.p, net_remap)
    n_nets = _terminal_net_indices(path.n, net_remap)
    all_nets = p_nets | n_nets

    in_p_nets = _terminal_net_indices(reg.in_p, net_remap)
    in_n_nets = _terminal_net_indices(reg.in_n, net_remap)
    out_p_nets = _terminal_net_indices(reg.out_p, net_remap)
    out_n_nets = _terminal_net_indices(reg.out_n, net_remap)
    sw1_nets = _net_index_set(proj, reg.sw1_net, net_remap)
    sw2_nets = _net_index_set(proj, reg.sw2_net, net_remap)

    # Direct SW1↔SW2 bridge (no intermediate net) → inductor.
    # Checked before HS_OUT: when SW2_NET aliases OUT_P (common BUCK
    # annotation), a SW1↔VOUT inductor would otherwise look like hs_out.
    if sw1_nets and sw2_nets:
        if all_nets & sw1_nets and all_nets & sw2_nets:
            return "inductor"

    # IN_P ∩ SW1 → hs_in (bridge)
    if all_nets & in_p_nets and all_nets & sw1_nets:
        return "hs_in"
    # SW1 ∩ IN_N → ls_in (switch leg)
    if all_nets & sw1_nets and all_nets & in_n_nets:
        return "ls_in"
    # SW2 ∩ OUT_P → hs_out (bridge)
    if all_nets & sw2_nets and all_nets & out_p_nets:
        return "hs_out"
    # SW2 ∩ OUT_N → ls_out
    if all_nets & out_n_nets and all_nets & sw2_nets:
        return "ls_out"
    # Single SW-node topologies (BUCK/BOOST) that declare only SW1:
    if sw1_nets and not sw2_nets:
        if all_nets & out_p_nets and all_nets & sw1_nets:
            return "hs_out"
        if all_nets & sw1_nets and all_nets & out_n_nets:
            return "ls_out"

    return "unknown"


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

    * PATH elements classified as **hs_in** / **hs_out** / **shunt** become
      :class:`ResistorSpec` so the downstream net-merge and rail_groups logic
      handles them as ordinary series bridges.
    * PATH elements classified as **ls_in** / **ls_out** become
      :class:`SwitchPathLeg` entries on the host :class:`RegulatorSpec`
      (``switch_path_legs``).
    * The inductor PATH becomes ``inductor_dcr`` on the host and its
      terminals replace the regulator's in_p/out_p (depending on SW1/SW2
      orientation).
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
            result.warnings.append(
                f"PATH on {p.designator}: host {host.designator} uses INVERTER "
                f"topology — external-FET binding is not yet supported for "
                f"INVERTER; skipping",
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

    # Second pass: resolve SW1↔SW2 chain for parts still classified unknown.
    for host_key, grouped in list(path_groups.items()):
        host = host_map[host_key]
        sw1_nets = _net_index_set(proj, host.sw1_net, net_remap)
        sw2_nets = _net_index_set(proj, host.sw2_net, net_remap)
        unknowns = [p for p, role in grouped if role == "unknown"]
        if not unknowns:
            continue
        chain_roles = _assign_sw_chain_roles(
            unknowns,
            sw1_nets,
            sw2_nets,
            net_remap,
            host.designator,
            result,
        )
        new_grouped: list[tuple[PathSpec, str]] = []
        for path, role in grouped:
            if role == "unknown" and id(path) in chain_roles:
                new_grouped.append((path, chain_roles[id(path)]))
            elif role == "unknown":
                _append_error_once(
                    result,
                    f"PATH on {path.designator}: could not classify pad nets "
                    f"against host {host.designator}'s stage nets "
                    f"(IN/OUT/SW) — check net names or set PDN_P_NET / "
                    f"PDN_N_NET explicitly",
                )
                bound_path_ids.discard(id(path))
            else:
                new_grouped.append((path, role))
        path_groups[host_key] = new_grouped

    # Drop host groups that lost every path after chain resolution.
    path_groups = {k: v for k, v in path_groups.items() if v}

    # Check for unbound PATH elements.
    for p in paths:
        if id(p) not in bound_path_ids:
            # Error already appended by _find_host_regulator or topology check.
            pass

    # Build replacement directives.
    new_directives: list = []
    replaced_reg_ids: set[int] = set()

    for host_key, grouped in path_groups.items():
        host = host_map[host_key]
        topo = host.smps_topology or "BUCK"
        ls_in_coeff, ls_out_coeff = _compute_ls_coeffs(topo, host.gain)

        hs_bridges: list[ResistorSpec] = []
        ls_legs: list[SwitchPathLeg] = []
        inductor_path: PathSpec | None = None
        inductor_dcr: float | None = None
        hs_in_spec: PathSpec | None = None

        for path, role in grouped:
            if role in ("hs_in", "hs_out", "shunt"):
                hs_bridges.append(
                    ResistorSpec(
                        designator=path.designator,
                        schdoc_name=path.schdoc_name,
                        resistance=path.resistance,
                        p=path.p,
                        n=path.n,
                        channel_index=path.channel_index,
                    )
                )
                if role == "hs_in":
                    hs_in_spec = path
            elif role == "ls_in":
                if ls_in_coeff > 0:
                    sw1_nets = _net_index_set(proj, host.sw1_net, net_remap)
                    p_term, n_term = path.p, path.n
                    if not (_terminal_net_indices(p_term, net_remap) & sw1_nets):
                        p_term, n_term = path.n, path.p
                    ls_legs.append(
                        SwitchPathLeg(
                            designator=path.designator,
                            kind="ls_in",
                            resistance=path.resistance,
                            p=p_term,
                            n=n_term,
                            coeff=ls_in_coeff,
                        )
                    )
                else:
                    result.warnings.append(
                        f"PATH on {path.designator}: {role} leg coefficient is "
                        f"zero for {topo} at gain {host.gain:.3f}, so the part "
                        f"carries no averaged current and is left out of the "
                        f"model entirely — its copper is not solved and any "
                        f"PDN_R on it is ignored"
                    )
            elif role == "ls_out":
                if ls_out_coeff > 0:
                    sw2_nets = _net_index_set(proj, host.sw2_net, net_remap)
                    sw_nets = sw2_nets or _net_index_set(
                        proj,
                        host.sw1_net,
                        net_remap,
                    )
                    p_term, n_term = path.p, path.n
                    if sw_nets and not (_terminal_net_indices(p_term, net_remap) & sw_nets):
                        p_term, n_term = path.n, path.p
                    ls_legs.append(
                        SwitchPathLeg(
                            designator=path.designator,
                            kind="ls_out",
                            resistance=path.resistance,
                            p=p_term,
                            n=n_term,
                            coeff=ls_out_coeff,
                        )
                    )
                else:
                    result.warnings.append(
                        f"PATH on {path.designator}: {role} leg coefficient is "
                        f"zero for {topo} at gain {host.gain:.3f}, so the part "
                        f"carries no averaged current and is left out of the "
                        f"model entirely — its copper is not solved and any "
                        f"PDN_R on it is ignored"
                    )
            elif role == "inductor":
                if inductor_path is not None:
                    _append_error_once(
                        result,
                        f"PATH on {path.designator}: multiple inductors between "
                        f"SW1 and SW2 on host {host.designator} — only one "
                        f"inductor PATH per stage is supported",
                    )
                    continue
                inductor_path = path
                inductor_dcr = path.resistance

        if (
            host.sw1_net
            and host.sw2_net
            and any(
                r in ("hs_in", "hs_out", "ls_in", "ls_out", "shunt", "inductor") for _, r in grouped
            )
            and inductor_path is None
        ):
            _append_error_once(
                result,
                f"REGULATOR on {host.designator}: PATH stage with SW1/SW2 "
                f"needs exactly one inductor between those nets — none found",
            )

        # Determine inductor terminals orientation (which side is SW1, which SW2).
        updated_in_p = host.in_p
        updated_out_p = host.out_p
        if inductor_path is not None:
            sw1_nets = _net_index_set(proj, host.sw1_net, net_remap)
            ind_p_nets = _terminal_net_indices(inductor_path.p, net_remap)
            if ind_p_nets & sw1_nets:
                updated_in_p = inductor_path.p
                updated_out_p = inductor_path.n
            else:
                # N on SW1, or intermediate-net inductor (P toward SW1 via chain).
                # Prefer the pad whose net is closer to SW1: if neither pad is
                # on SW1 (shunt in between), keep schematic P→SW1 convention
                # from chain walk (inductor is last edge; P/N already set).
                sw2_nets = _net_index_set(proj, host.sw2_net, net_remap)
                ind_n_nets = _terminal_net_indices(inductor_path.n, net_remap)
                if ind_n_nets & sw1_nets:
                    updated_in_p = inductor_path.n
                    updated_out_p = inductor_path.p
                elif ind_p_nets & sw2_nets:
                    updated_in_p = inductor_path.n
                    updated_out_p = inductor_path.p
                else:
                    updated_in_p = inductor_path.p
                    updated_out_p = inductor_path.n

        # Vin sense: VIN-side pad of hs_in, else original regulator IN.
        if hs_in_spec is not None:
            in_p_nets = _terminal_net_indices(host.in_p, net_remap)
            if _terminal_net_indices(hs_in_spec.p, net_remap) & in_p_nets:
                vin_sense_p = hs_in_spec.p
            else:
                vin_sense_p = hs_in_spec.n
        else:
            vin_sense_p = host.in_p
        vin_sense_n = host.in_n

        # Build the updated RegulatorSpec.
        updated_reg = replace(
            host,
            in_p=updated_in_p if inductor_path else host.in_p,
            out_p=updated_out_p if inductor_path else host.out_p,
            switch_path_legs=tuple(ls_legs),
            vin_sense_p=vin_sense_p,
            vin_sense_n=vin_sense_n,
            inductor_dcr=inductor_dcr,
        )
        replaced_reg_ids.add(id(host))

        # Emit HS bridges as ResistorSpec.
        new_directives.extend(hs_bridges)
        # The updated regulator replaces the original.
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
