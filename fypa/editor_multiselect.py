"""Multi-select support for editor-mode PDN markers.

Pure logic, no Qt — the marquee hit-test and the "shared value vs ``*``"
field merge are the two pieces worth testing on their own, so they live
here rather than inside :class:`~fypa.altium_viewer.PdnViewer`.

The viewer builds a :class:`MarqueeCandidate` per drawn marker (one per
editor directive or schematic directive, carrying *every* glyph point it
renders), hands them to :func:`marquee_select` with the dragged rectangle,
and feeds the survivors' field values to :func:`common_fields` to decide
which rows of the side panel show a real value and which show ``*``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "MIXED",
    "MarqueeCandidate",
    "MULTI_SINK_FIELDS",
    "common_fields",
    "marquee_select",
    "merge_values",
    "normalise_rect",
    "points_enclosed",
]


class _Mixed:
    """Sentinel for "the selected objects disagree on this field".

    Displayed as ``*`` in the side panel. A singleton so callers can test
    with ``is MIXED``; falsy so ``if value:`` guards read naturally.
    """

    __slots__ = ()

    def __repr__(self) -> str:      # pragma: no cover - debug aid
        return "MIXED"

    def __str__(self) -> str:
        return "*"

    def __bool__(self) -> bool:
        return False


MIXED = _Mixed()

# The :class:`~fypa.project_file.EditorDirective` fields a multi-sink edit
# is allowed to write. Deliberately excludes the per-part pad lists
# (``p_pins`` / ``n_pins`` / ``p_des`` / ``n_des``) — pushing one pad list
# across several different footprints is never what the user means — and
# ``anchor_xy`` / ``layer``, which are per-marker geometry.
MULTI_SINK_FIELDS: tuple[str, ...] = (
    "current",
    "min_voltage",
    "single_net",
    "p_net",
    "n_net",
)


@dataclass(frozen=True)
class MarqueeCandidate:
    """One marquee-selectable marker, as the viewport actually draws it.

    ``points`` holds every glyph position for the marker in world mm — a
    free marker has one, a component-bound directive has one per pad on
    its net(s). ``key`` identifies the underlying object: ``("directive",
    id)`` for an editor directive, ``("schematic", designator)`` for a
    still-locked Altium schematic directive.
    """

    key: tuple[str, str]
    role: str
    points: tuple[tuple[float, float], ...]
    label: str = ""


def normalise_rect(x0: float, y0: float, x1: float, y1: float
                   ) -> tuple[float, float, float, float]:
    """Order a dragged rectangle's corners as ``(xmin, ymin, xmax, ymax)``.

    The drag can start at any corner, so the raw press / release pair is
    not necessarily min-then-max on either axis.
    """
    lo_x, hi_x = (x0, x1) if x0 <= x1 else (x1, x0)
    lo_y, hi_y = (y0, y1) if y0 <= y1 else (y1, y0)
    return (float(lo_x), float(lo_y), float(hi_x), float(hi_y))


def points_enclosed(points: Sequence[tuple[float, float]],
                    rect: tuple[float, float, float, float]) -> bool:
    """Whether *every* point lies inside ``rect`` (a normalised bbox).

    Full enclosure, not touching: a component sink glyphs at each of its
    pads, and clipping one corner pad of a large connector should not drag
    the whole sink into the selection. An empty point list is never
    enclosed — a marker with nothing drawn isn't on screen to select.
    """
    if not points:
        return False
    x0, y0, x1, y1 = rect
    return all(x0 <= px <= x1 and y0 <= py <= y1 for px, py in points)


def marquee_select(candidates: Iterable[MarqueeCandidate],
                   rect: tuple[float, float, float, float],
                   ) -> list[MarqueeCandidate]:
    """The candidates fully enclosed by ``rect``, in input order."""
    return [c for c in candidates if points_enclosed(c.points, rect)]


def merge_values(values: Iterable[Any]) -> Any:
    """One shared value, or :data:`MIXED` when they disagree.

    An empty iterable merges to ``None`` (nothing selected ⇒ nothing to
    show). ``None`` is a value like any other, so a group where every
    sink leaves Min V unset merges to ``None`` (a blank field) rather
    than to ``*``.
    """
    it = iter(values)
    try:
        first = next(it)
    except StopIteration:
        return None
    for v in it:
        # ``isinstance`` guard because ``1.0 == True`` in Python: a bool
        # field and a numeric field that happen to compare equal are still
        # a disagreement. An int / float pair is *not* — a project file
        # round-trip can hand back ``1`` where another sink holds ``1.0``,
        # and flagging that as ``*`` would be a lie.
        if isinstance(v, bool) != isinstance(first, bool):
            return MIXED
        if v != first:
            return MIXED
    return first


def common_fields(objects: Sequence[Any],
                  fields: Iterable[str] = MULTI_SINK_FIELDS,
                  getter: Callable[[Any, str], Any] | None = None,
                  ) -> dict[str, Any]:
    """Map each field name to its shared value across ``objects``, or
    :data:`MIXED` where they differ.

    ``getter`` defaults to :func:`getattr` with a ``None`` fallback, which
    is what :class:`~fypa.project_file.EditorDirective` needs.
    """
    read = getter if getter is not None else _attr
    return {
        name: merge_values([read(o, name) for o in objects])
        for name in fields
    }


def _attr(obj: Any, name: str) -> Any:
    return getattr(obj, name, None)
