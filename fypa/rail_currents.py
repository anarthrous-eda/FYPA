"""DC rail currents estimated from directives alone (KCL, no solve needed).

A rail's load is its SINK currents plus the input current of every regulator
it feeds; a regulator's input current is ``gain × output-rail load`` plus its
quiescent current. Shared by the design report and the Topology tooltips.
"""
from __future__ import annotations

import math


def pin_nets(d: dict, term: str) -> set[str]:
    return {p.get("net") for p in ((d.get("terminals") or {}).get(term) or {})
            .get("pins", []) if p.get("net")}


def to_float(x) -> float | None:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


class RailCurrents:
    """Rail loads and regulator input currents for one directive list."""

    def __init__(self, directives: list[dict],
                 rail_to_members: dict[str, list[str]]) -> None:
        self.directives = directives
        self.rail_to_members = rail_to_members
        self.net_to_rail = {n: rail for rail, members in rail_to_members.items()
                            for n in members}
        self._load_cache: dict[str, float | None] = {}

    def rail_of(self, d: dict, term: str) -> str | None:
        for net in pin_nets(d, term):
            if net in self.net_to_rail:
                return self.net_to_rail[net]
        return None

    def rail_load(self, rail: str, _stack: frozenset = frozenset()
                  ) -> float | None:
        """Total DC current drawn from a rail: its SINK currents plus the
        estimated input current of every regulator it feeds (KCL)."""
        if rail in self._load_cache:
            return self._load_cache[rail]
        if rail in _stack:
            return None
        members = set(self.rail_to_members.get(rail, [rail]))
        total, found = 0.0, False
        for d in self.directives:
            role = d.get("role")
            if role == "SINK" and pin_nets(d, "P") & members:
                total += to_float(d.get("value")) or 0.0
                found = True
            elif role == "REGULATOR" and pin_nets(d, "IN_P") & members:
                i_in = self.regulator_input_current(d, _stack | {rail})
                if i_in is not None:
                    total += i_in
                    found = True
        result = total if found else None
        self._load_cache[rail] = result
        return result

    def regulator_input_current(self, d: dict, _stack=frozenset()
                                ) -> float | None:
        out_rail = self.rail_of(d, "OUT_P")
        if out_rail is None:
            return None
        i_out = self.rail_load(out_rail, _stack)
        if i_out is None:
            return None
        gain = to_float(d.get("gain"))
        gain = 1.0 if gain is None else gain
        return gain * i_out + (to_float(d.get("quiescent_current")) or 0.0)

    def supplier_count(self, rail: str) -> int:
        """SOURCE / REGULATOR outputs driving ``rail`` — more than one means
        the rail load is shared and no single supplier's share is known."""
        members = set(self.rail_to_members.get(rail, [rail]))
        n = 0
        for d in self.directives:
            term = {"SOURCE": "P", "REGULATOR": "OUT_P"}.get(d.get("role"))
            if term and pin_nets(d, term) & members:
                n += 1
        return n
