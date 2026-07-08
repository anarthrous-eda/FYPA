"""Vertical bus-x allocation on the MIN_PARALLEL_GAP grid."""

from __future__ import annotations

from fypa.topology.constants import GND_NET, MIN_PARALLEL_GAP, WIRE_EPS
from fypa.topology.placement.ports import port_stub_x
from fypa.topology.types import TopologyPort


def gnd_column_trunk_x(group: list[TopologyPort]) -> float:
    gnd_ports = [p for p in group if p.net == GND_NET]
    if gnd_ports:
        stubs = [port_stub_x(p) for p in gnd_ports]
        if all(p.side == "left" for p in gnd_ports):
            return min(stubs)
        if all(p.side == "right" for p in gnd_ports):
            return max(stubs)
        return port_stub_x(gnd_ports[0])
    stubs = [port_stub_x(p) for p in group]
    if all(p.side == "left" for p in group):
        return min(stubs)
    if all(p.side == "right" for p in group):
        return max(stubs)
    return sum(stubs) / len(stubs)


def _vertical_blocks_x(
    x: float,
    y_lo: float,
    y_hi: float,
    reserved: list[tuple[float, float, float, str]],
    net: str,
) -> bool:
    lo, hi = min(y_lo, y_hi), max(y_lo, y_hi)
    for vx, vy_lo, vy_hi, vnet in reserved:
        if vnet == net and abs(vx - x) < WIRE_EPS:
            continue
        if abs(vx - x) >= MIN_PARALLEL_GAP - WIRE_EPS:
            continue
        if hi > vy_lo + WIRE_EPS and lo < vy_hi + WIRE_EPS:
            return True
    return False


def _shift_for_blockers(
    x: float,
    y_lo: float,
    y_hi: float,
    reserved: list[tuple[float, float, float, str]],
    net: str,
    *,
    outward: float,
) -> float:
    lo, hi = min(y_lo, y_hi), max(y_lo, y_hi)
    for vx, vy_lo, vy_hi, vnet in reserved:
        if vnet == net and abs(vx - x) < WIRE_EPS:
            continue
        if vnet == GND_NET and abs(vx - x) < MIN_PARALLEL_GAP - WIRE_EPS:
            return vx + outward * MIN_PARALLEL_GAP if outward >= 0 else vx - MIN_PARALLEL_GAP
        if (
            abs(vx - x) < MIN_PARALLEL_GAP - WIRE_EPS
            and hi > vy_lo + WIRE_EPS
            and lo < vy_hi + WIRE_EPS
        ):
            return vx + outward * MIN_PARALLEL_GAP if outward >= 0 else vx - MIN_PARALLEL_GAP
    return x


def nudge_bus_from_gnd_columns(
    bus_x: float,
    y_lo: float,
    y_hi: float,
    reserved: list[tuple[float, float, float, str]],
    *,
    anchor_stub: float | None = None,
) -> float:
    """Keep hub buses MIN_PARALLEL_GAP from foreign GND columns (x axis)."""
    lo, hi = min(y_lo, y_hi), max(y_lo, y_hi)
    x = bus_x

    def _foreign_blocks(cx: float) -> bool:
        for rx, ry_lo, ry_hi, rnet in reserved:
            if rnet == GND_NET:
                continue
            if abs(rx - cx) > WIRE_EPS:
                continue
            if hi > ry_lo + WIRE_EPS and lo < ry_hi - WIRE_EPS:
                return True
        return False

    for vx, _vy_lo, _vy_hi, vnet in reserved:
        if vnet != GND_NET:
            continue
        if anchor_stub is not None and abs(vx - anchor_stub) < WIRE_EPS:
            continue
        if abs(x - vx) < MIN_PARALLEL_GAP - WIRE_EPS:
            if anchor_stub is not None:
                preferred = [
                    anchor_stub,
                    anchor_stub + MIN_PARALLEL_GAP,
                    anchor_stub - MIN_PARALLEL_GAP,
                ]
                for candidate in preferred:
                    if candidate == anchor_stub:
                        if not _foreign_blocks(candidate):
                            return candidate
                        continue
                    if abs(candidate - vx) >= MIN_PARALLEL_GAP - WIRE_EPS:
                        if not _foreign_blocks(candidate):
                            return candidate
            west, east = vx - MIN_PARALLEL_GAP, vx + MIN_PARALLEL_GAP
            options = [c for c in (west, east) if not _foreign_blocks(c)]
            if not options:
                options = [west, east]
            if anchor_stub is not None:
                x = min(options, key=lambda c: abs(c - anchor_stub))
            elif x <= vx:
                x = west
            else:
                x = east
    return x


def _separate_from_assigned(
    x: float,
    assigned: list[float],
    bus_lo: float,
    bus_hi: float,
    *,
    outward: float,
) -> float:
    """Keep ``x`` at least ``MIN_PARALLEL_GAP`` from each assigned bus.

    Two-sided: a candidate comfortably to *either* side of ``prev`` is left
    alone (the old ``x < prev + MIN_PARALLEL_GAP`` test also fired for an ``x``
    far *west* of ``prev`` and shoved every such bus east, exhausting the
    corridor). Only a genuinely-too-close ``x`` is shifted, to whichever side
    keeps it in ``[bus_lo, bus_hi]`` and moves it least.
    """
    for prev in assigned:
        if abs(x - prev) >= MIN_PARALLEL_GAP - WIRE_EPS:
            continue
        east, west = prev + MIN_PARALLEL_GAP, prev - MIN_PARALLEL_GAP
        in_range = [
            c for c in (east, west) if bus_lo - WIRE_EPS <= c <= bus_hi + WIRE_EPS
        ]
        if in_range:
            x = min(in_range, key=lambda c: abs(c - x))
        else:
            x = east if outward >= 0 else west
    return x


def allocate_bus_x(
    nominal: float,
    y_lo: float,
    y_hi: float,
    bus_lo: float,
    bus_hi: float,
    reserved_verticals: list[tuple[float, float, float, str]],
    net: str,
    *,
    outward: float,
    assigned_in_group: list[float] | None = None,
) -> float:
    """Pick the first valid bus x on the MIN_PARALLEL_GAP grid inside [bus_lo, bus_hi]."""
    assigned = assigned_in_group or []
    n_slots = max(
        int((bus_hi - bus_lo) / MIN_PARALLEL_GAP) + 1,
        len(assigned) + 1,
        8,
    )
    candidates: list[float] = [nominal]
    for k in range(n_slots + 1):
        candidates.append(bus_lo + k * MIN_PARALLEL_GAP)
    candidates.append((bus_lo + bus_hi) / 2)
    seen: set[float] = set()
    ordered: list[float] = []
    for c in candidates:
        r = round(c, 1)
        if r not in seen:
            seen.add(r)
            ordered.append(c)
    ordered.sort(key=lambda c: abs(c - nominal))

    for candidate in ordered:
        x = max(bus_lo, min(bus_hi, candidate))
        x = _separate_from_assigned(x, assigned, bus_lo, bus_hi, outward=outward)
        x = max(bus_lo, min(bus_hi, x))
        if _vertical_blocks_x(x, y_lo, y_hi, reserved_verticals, net):
            x = _shift_for_blockers(
                x,
                y_lo,
                y_hi,
                reserved_verticals,
                net,
                outward=outward,
            )
            x = max(bus_lo, min(bus_hi, x))
            x = _separate_from_assigned(x, assigned, bus_lo, bus_hi, outward=outward)
            x = max(bus_lo, min(bus_hi, x))
        if not _vertical_blocks_x(x, y_lo, y_hi, reserved_verticals, net):
            return x
    return max(bus_lo, min(bus_hi, nominal))
