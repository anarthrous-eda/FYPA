"""Shared routing state: band reservation for collision avoidance."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RoutingContext:
    """Shared routing state for wire collision avoidance."""

    _horizontal_bands: list[tuple[float, float, float, str]] = field(
        default_factory=list,
    )
    _vertical_bands: list[tuple[float, float, float, str]] = field(
        default_factory=list,
    )

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
