"""Shared routing state: band reservation and slot registry."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class RoutingContext:
    """Shared routing state for gutter slot allocation and collision avoidance."""

    _gutter_nets: dict[tuple, list[str]] = field(
        default_factory=lambda: defaultdict(list),
    )
    _horizontal_bands: list[tuple[float, float, float, str]] = field(
        default_factory=list,
    )
    _vertical_bands: list[tuple[float, float, float, str]] = field(
        default_factory=list,
    )
    _stack_nets: dict[tuple, list[str]] = field(
        default_factory=lambda: defaultdict(list),
    )

    def allocate_gutter_slot(self, x_lo: float, x_hi: float, net: str) -> int:
        key = (round(x_lo, 1), round(x_hi, 1))
        nets = self._gutter_nets[key]
        if net not in nets:
            nets.append(net)
        return nets.index(net)

    def gutter_slot_count(self, x_lo: float, x_hi: float) -> int:
        key = (round(x_lo, 1), round(x_hi, 1))
        return len(self._gutter_nets.get(key, []))

    def allocate_stack_lane(self, col: float, side: str, net: str) -> int:
        key = (round(col, 1), side)
        nets = self._stack_nets[key]
        if net not in nets:
            nets.append(net)
        return nets.index(net)

    def stack_lane_count(self, col: float, side: str) -> int:
        key = (round(col, 1), side)
        return len(self._stack_nets.get(key, []))

    def reserve_horizontal(
        self,
        y: float,
        x_lo: float,
        x_hi: float,
        net: str,
    ) -> None:
        lo, hi = min(x_lo, x_hi), max(x_lo, x_hi)
        self._horizontal_bands.append((y, lo, hi, net))

    def reserve_vertical(
        self,
        x: float,
        y_lo: float,
        y_hi: float,
        net: str,
    ) -> None:
        lo, hi = min(y_lo, y_hi), max(y_lo, y_hi)
        self._vertical_bands.append((x, lo, hi, net))

    @property
    def horizontal_bands(self) -> list[tuple[float, float, float, str]]:
        return self._horizontal_bands

    @property
    def vertical_bands(self) -> list[tuple[float, float, float, str]]:
        return self._vertical_bands
