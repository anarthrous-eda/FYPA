"""Placement and bus-plan key types (column, gutter, routing)."""

from __future__ import annotations

from typing import TypeAlias

ColumnSideKey: TypeAlias = tuple[float, str]
"""``(column_x, "left" | "right")`` — stacked hub lane or stack-column group."""

GutterSpanKey: TypeAlias = tuple[float, float]
"""``(x_lo, x_hi)`` — column gap span shared by gutter-routed nets."""

StackBusKey: TypeAlias = tuple[float, str, str]
"""``(column_x, side, net)`` — per-net bus beside a stack column."""

GndTrunkKey: TypeAlias = tuple[float, str]
"""``(trunk_x, "left" | "right")`` — GND column trunk from the bus plan."""

StackRoutingKey: TypeAlias = tuple[str, str, str]
"""``("stack", node_a, node_b)`` — two-port net in the same column."""

GapRoutingKey: TypeAlias = tuple[str, float, float]
"""``("gap", x_lo, x_hi)`` — two-port net across a column gutter."""

WireRoutingKey: TypeAlias = StackRoutingKey | GapRoutingKey
