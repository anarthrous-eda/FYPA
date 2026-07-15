"""Runtime shims for altium_monkey netlist connectivity.

Two independent upstream defects are patched here:

1. Sheet-entry hotspot geometry. ``_extract_sheet_entries()`` computes entry
   connection hotspots as ``entry.distance_from_top * 10``, which drops the
   fractional ``distance_from_top_frac1`` component. Under a non-zero
   connection tolerance the misplaced hotspot can snap onto an adjacent net's
   wire and merge two electrically distinct rails.

2. Connection tolerance. ``AltiumNetlistSingleSheetCompiler`` defaults its
   connection tolerance to ``0`` (exact match), and the multi-sheet compiler
   constructs its per-sheet compilers without passing one. Port and wire
   connection points are stored as truncated integers, so a port whose real
   right edge sits at e.g. ``x=495.57`` reports ``495`` while the wire it
   touches starts at ``496.06`` -> ``496``. With an exact-match tolerance the
   one-unit integer gap no longer bridges and every port-wired pin detaches
   into an auto-named net (e.g. ``NetJ1_7``) instead of its rail (e.g.
   ``VBUS``). Earlier altium_monkey releases used a non-zero default, so this
   regressed silently on the version bump. Restoring a minimal tolerance
   re-bridges the truncation gap without merging genuinely separate rails
   (adjacent wires are many units apart).

Remove the relevant shim once the pinned altium_monkey release includes each
fix.
"""
from __future__ import annotations

import inspect
import logging

log = logging.getLogger(__name__)

_APPLIED = False

# Minimum per-axis connection tolerance (in parsed 10-mil coordinate units).
# Integer truncation of port/wire connection points loses < 1 unit per axis, so
# a genuine connection can present at most a 1-unit integer gap. ``1`` bridges
# that gap; adjacent-but-distinct wires on real sheets are several units apart,
# so it does not introduce cross-connections. ``_points_connected`` uses a
# per-axis (Chebyshev) comparison, so this also covers diagonal truncation.
MIN_CONNECTION_TOLERANCE = 1


def _needs_sheet_entry_hotspot_patch() -> bool:
    from altium_monkey.altium_netlist_single_sheet import (
        AltiumNetlistSingleSheetCompiler,
    )

    try:
        source = inspect.getsource(AltiumNetlistSingleSheetCompiler._extract_sheet_entries)
    except (OSError, TypeError):
        return True
    return (
        "distance_from_top * 10" in source
        and "_distance_from_top_native_units" not in source
    )


def _needs_harness_entry_hotspot_patch() -> bool:
    from altium_monkey.altium_netlist_multi_sheet import (
        AltiumNetlistMultiSheetCompiler,
    )

    try:
        source = inspect.getsource(AltiumNetlistMultiSheetCompiler._expand_harness_entries)
    except (OSError, TypeError):
        return True
    return "entry.distance_from_top * 10" in source


def _needs_connection_tolerance_patch() -> bool:
    """True when the single-sheet compiler defaults to an exact-match tolerance.

    Older altium_monkey releases used a non-zero default that bridged the
    integer-truncation gap between port and wire connection points; the current
    pin defaults to ``0``, which drops those connections.
    """
    from altium_monkey.altium_netlist_single_sheet import (
        AltiumNetlistSingleSheetCompiler,
    )

    try:
        default = inspect.signature(
            AltiumNetlistSingleSheetCompiler.__init__
        ).parameters["tolerance"].default
    except (ValueError, KeyError, TypeError):
        return False
    if not isinstance(default, int):
        return False
    return default < MIN_CONNECTION_TOLERANCE


def apply_altium_monkey_patches() -> None:
    """Apply upstream connectivity shims once per process (no-op when fixed)."""
    global _APPLIED
    if _APPLIED:
        return

    patch_sheet = _needs_sheet_entry_hotspot_patch()
    patch_harness = _needs_harness_entry_hotspot_patch()
    patch_tolerance = _needs_connection_tolerance_patch()
    if not patch_sheet and not patch_harness and not patch_tolerance:
        _APPLIED = True
        log.debug("altium_monkey connectivity patches not needed (upstream fixed).")
        return

    if patch_sheet:
        from altium_monkey.altium_netlist_single_sheet import (
            AltiumNetlistSingleSheetCompiler as _Single,
        )

        def _extract_sheet_entries(self) -> None:
            for sheet_sym_info in self.schdoc.get_sheet_symbols():
                ss = sheet_sym_info.record
                sym_x = ss.location.x
                sym_y = ss.location.y

                for entry in sheet_sym_info.entries:
                    entry_name = entry.display_name or ""
                    if not entry_name:
                        continue

                    dist = round(entry._distance_from_top_native_units())
                    side = entry.side

                    if side == 0:
                        hotspot = (sym_x, sym_y - dist)
                    elif side == 1:
                        hotspot = (sym_x + ss.x_size, sym_y - dist)
                    elif side == 2:
                        hotspot = (sym_x + dist, sym_y)
                    elif side == 3:
                        hotspot = (sym_x + dist, sym_y - ss.y_size)
                    else:
                        log.warning(
                            "Unknown sheet entry side %s for '%s'",
                            side,
                            entry_name,
                        )
                        continue

                    self._sheet_entries[hotspot] = (entry_name, sheet_sym_info)
                    self._sheet_entry_objects[hotspot] = entry
                    log.debug(
                        "Sheet entry '%s' at hotspot %s (side=%s, dist=%s)",
                        entry_name,
                        hotspot,
                        side,
                        dist,
                    )

        _Single._extract_sheet_entries = _extract_sheet_entries  # type: ignore[method-assign]

    if patch_harness:
        from altium_monkey.altium_netlist_multi_sheet import (
            AltiumNetlistMultiSheetCompiler as _Multi,
        )
        from altium_monkey.altium_netlist_multi_sheet_support import (
            _build_port_location_map,
            _build_wire_endpoint_map,
            _find_or_create_net_for_wire,
        )

        def _expand_harness_entries(self, port_net_map, other_nets):
            harness_keys = set()
            for sheet_idx, schdoc in enumerate(self._schdocs):
                if not schdoc.harness_connectors:
                    continue

                source_sheet = schdoc.filepath.name if schdoc.filepath else ""
                source_sheet_index = self._source_sheet_index(sheet_idx)
                wire_endpoint_map = _build_wire_endpoint_map(schdoc)
                port_location_map = _build_port_location_map(schdoc)

                for connector in schdoc.harness_connectors:
                    harness_port_name = self._find_harness_port_name(
                        connector,
                        schdoc.signal_harnesses,
                        port_location_map,
                    )

                    for entry in connector.entries:
                        entry_y = connector.location.y - round(
                            entry._distance_from_top_native_units()
                        )
                        entry_x_left = connector.location.x
                        entry_x_right = connector.location.x + connector.xsize

                        wire_uid = wire_endpoint_map.get((entry_x_left, entry_y))
                        if not wire_uid:
                            wire_uid = wire_endpoint_map.get((entry_x_right, entry_y))
                        if not wire_uid:
                            continue

                        merge_key = (
                            f"{harness_port_name}.{entry.name}"
                            if harness_port_name
                            else entry.name
                        )

                        _find_or_create_net_for_wire(
                            wire_uid,
                            sheet_idx,
                            merge_key,
                            port_net_map,
                            other_nets,
                            harness_keys,
                            str(getattr(entry, "unique_id", "") or ""),
                            str(getattr(entry, "name", "") or ""),
                            source_sheet,
                            source_sheet_index,
                        )

            return harness_keys

        _Multi._expand_harness_entries = _expand_harness_entries  # type: ignore[method-assign]

    if patch_tolerance:
        from altium_monkey.altium_netlist_single_sheet import (
            AltiumNetlistSingleSheetCompiler as _SingleTol,
        )

        _orig_init = _SingleTol.__init__
        _init_sig = inspect.signature(_orig_init)

        def _init_with_min_tolerance(self, *args, **kwargs):
            bound = _init_sig.bind(self, *args, **kwargs)
            bound.apply_defaults()
            tol = bound.arguments.get("tolerance")
            if tol is None or tol < MIN_CONNECTION_TOLERANCE:
                bound.arguments["tolerance"] = MIN_CONNECTION_TOLERANCE
            _orig_init(*bound.args, **bound.kwargs)

        _SingleTol.__init__ = _init_with_min_tolerance  # type: ignore[method-assign]

    _APPLIED = True
    log.info(
        "Applied altium_monkey connectivity patches "
        "(sheet=%s, harness=%s, tolerance=%s).",
        patch_sheet,
        patch_harness,
        patch_tolerance,
    )
