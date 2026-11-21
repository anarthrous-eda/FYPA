"""Via kinds - through / blind / buried / micro - and how a via is sorted
into one.

Kept free of solver and viewer imports so both sides share one definition:
the loader picks a plating thickness per kind, the viewer lists the kinds as
sub-rows of the Board Features "Vias" row.
"""
from __future__ import annotations

# A blind / buried via counts as a microvia when it spans one dielectric
# (two adjacent copper layers) and its drill is at most this (IPC-2226:
# a microvia is <= 0.15 mm). Altium carries no microvia flag on a via.
MICROVIA_MAX_DRILL_MM: float = 0.15

# The kinds a board via (not a plated through-hole pad) can be, in the order
# the viewer lists them.
BOARD_VIA_KINDS: tuple[str, ...] = ("through", "blind", "buried", "micro")

VIA_KIND_LABELS: dict[str, str] = {
    "through": "Through",
    "blind": "Blind",
    "buried": "Buried",
    "micro": "Micro",
}


def via_layer_span(start: int, end: int, enabled_layers: list[int]) -> list[int]:
    """Return enabled copper layers that a via/through-hole pad bridges,
    in Top->Bottom order.

    ``enabled_layers`` is in Top->Bottom *positional* order but internal plane
    layers carry raw ids 39-54 (INTERNAL_PLANE_1..16) that are numerically
    ABOVE the Bottom-layer id (32) even though they sit physically between Top
    and Bottom. A raw-id range test (``start <= lid <= end``) therefore silently
    drops every internal plane from a normal Top(1)->Bottom(32) through-via
    span, so vias never couple current into via-stitched planes. Slice by
    position instead - exactly what the geometry side's ``_via_on_layer`` does.
    """
    try:
        i_a = enabled_layers.index(start)
        i_b = enabled_layers.index(end)
    except ValueError:
        # An endpoint isn't in the enabled chain (e.g. a via referencing a
        # disabled layer) - fall back to the raw-id range rather than crash.
        if start > end:
            start, end = end, start
        return [lid for lid in enabled_layers if start <= lid <= end]
    lo, hi = (i_a, i_b) if i_a <= i_b else (i_b, i_a)
    return enabled_layers[lo:hi + 1]


def classify_via(start: int, end: int, span: list[int] | tuple[int, ...],
                 enabled_layers: list[int], drill_mm: float) -> str:
    """Via kind from its declared end layers: *through* when both ends are
    the stack's outer layers, *blind* with one, *buried* with neither; a
    blind or buried via over one dielectric with a small drill is *micro*.
    Outer layers are the first and last enabled copper layers."""
    if not enabled_layers:
        return "through"
    outer = {enabled_layers[0], enabled_layers[-1]}
    n_outer = (start in outer) + (end in outer)
    if n_outer == 2 and start != end:
        return "through"
    if (len(span) == 2 and 0.0 < drill_mm <= MICROVIA_MAX_DRILL_MM):
        return "micro"
    return "blind" if n_outer == 1 else "buried"


def via_record_kind(rec: dict, enabled_layers: list[int]) -> str:
    """Kind of one ``metadata["vias"]`` record: the ``via_kind`` the loader
    stamped on it, or - for a record that carries none (an older solution, a
    Gerber import) - the same classification worked out from its span and
    drill. ``enabled_layers`` are the copper layer ids, top to bottom."""
    kind = rec.get("via_kind")
    if kind in BOARD_VIA_KINDS:
        return kind
    start = rec.get("layer_start")
    end = rec.get("layer_end")
    if start is None or end is None:
        return "through"
    layers = list(enabled_layers)
    return classify_via(start, end, via_layer_span(start, end, layers),
                        layers, float(rec.get("hole_diameter_mm") or 0.0))
