"""Bus plan dataclass shared by placement planning modules."""

from __future__ import annotations

from dataclasses import dataclass, field

from fypa.topology.placement.types import GndTrunkKey, GutterSpanKey, StackBusKey


@dataclass
class BusPlan:
    """Precomputed vertical bus positions for signal routing."""

    pair_buses: dict[str, float] = field(default_factory=dict)
    hub_buses: dict[str, float] = field(default_factory=dict)
    stack_buses: dict[StackBusKey, float] = field(default_factory=dict)
    gnd_trunks: dict[GndTrunkKey, float] = field(default_factory=dict)
    reserved_verticals: list[tuple[float, float, float, str]] = field(
        default_factory=list,
    )
    gutter_spans: dict[GutterSpanKey, list[float]] = field(
        default_factory=dict,
    )
