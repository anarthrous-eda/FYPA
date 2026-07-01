"""Typed metadata shapes for the topology pipeline."""

from __future__ import annotations

from typing import TypedDict


class TerminalPinDict(TypedDict, total=False):
    pad: str
    net: str
    layer_id: int
    x_mm: float
    y_mm: float


class TerminalDict(TypedDict, total=False):
    requested_net: str
    ideal_return: bool
    pins: list[TerminalPinDict]


class DirectiveDict(TypedDict, total=False):
    role: str
    designator: str
    label: str
    value_str: str
    channel_index: int
    gain: float
    quiescent_current: float
    regulator_type: str
    efficiency: float
    terminals: dict[str, TerminalDict]


class TopologyMetadata(TypedDict, total=False):
    """Minimal input for :func:`build_topology_model` and :func:`compute_rail_groups`."""

    directives: list[DirectiveDict]
    net_canonical: dict[str, str]
    annotation_errors: list[str]


def assert_topology_metadata(data: object) -> TopologyMetadata:
    """Validate a dict has the minimal topology metadata shape (dev/CI helper)."""
    if not isinstance(data, dict):
        raise TypeError(f"expected dict, got {type(data).__name__}")
    directives = data.get("directives")
    if directives is not None and not isinstance(directives, list):
        raise TypeError("directives must be a list")
    if directives is not None:
        for i, d in enumerate(directives):
            if not isinstance(d, dict):
                raise TypeError(f"directives[{i}] must be a dict")
    net_canonical = data.get("net_canonical")
    if net_canonical is not None and not isinstance(net_canonical, dict):
        raise TypeError("net_canonical must be a dict")
    annotation_errors = data.get("annotation_errors")
    if annotation_errors is not None and not isinstance(annotation_errors, list):
        raise TypeError("annotation_errors must be a list")
    return data  # type: ignore[return-value]
