"""Which PDN directives sit on a rail that can carry current.

A rail is a set of nets joined by series elements (sense resistors,
ferrites, links). Current only flows on a rail that has both a supply and a
load:

* a SINK needs a supply on its rail, traced back to a real SOURCE - through
  any regulators in between, each of which needs its own input supplied;
* a SOURCE needs something drawing from its rail (a sink, a regulator's
  input, or a series element to ground);
* a REGULATOR needs a supplied input *and* something drawing from its output.

Dropping one directive can strand others (a regulator with an unfed input
takes the loads it feeds with it), so the rules repeat until nothing changes.

**Return nets don't join rails.** Every directive's return terminal (N,
OUT_N, IN_N) usually sits on GND. Grouping rails by every net a directive
touches would put every rail on the board into one group through GND, so a
load on a rail nothing feeds looked "closed" as soon as any other rail had a
source. Return nets are therefore left out of the rail grouping; a series
element from a rail to a return net (a shunt) counts as a load on that rail.

Pure logic over hashable net keys (net indices in the solver, upper-cased
names in the editor), shared by :func:`fypa.altium.loader._analyze_open_loop_rails`
and :func:`fypa.editor_directives.has_closed_pdn_loop` so the Resolve button
and the solve can never disagree.
"""

from __future__ import annotations

from collections.abc import Hashable
from dataclasses import dataclass, field


@dataclass
class Item:
    """One directive, reduced to the nets that matter here.

    ``kind`` is ``source`` (``out`` = its P nets), ``sink`` (``inp`` = its P
    nets), ``regulator`` (``out`` / ``inp`` = its OUT_P / IN_P nets) or
    ``series`` (``out`` / ``inp`` = its two sides); ``ignore`` for anything
    else. ``ret`` is the return terminal's nets (empty for an ideal return
    and for series elements)."""

    kind: str
    out: set[Hashable] = field(default_factory=set)
    inp: set[Hashable] = field(default_factory=set)
    ret: set[Hashable] = field(default_factory=set)


# Why a directive was dropped.
NO_LOAD = "no_load"              # source / regulator: nothing draws from its output
NO_SUPPLY = "no_supply"          # sink: nothing supplies its rail
INPUT_UNFED = "input_unfed"      # regulator: nothing supplies its input


class Rails:
    """Union of nets joined by series elements (return nets excluded)."""

    def __init__(self, items: list[Item]) -> None:
        # Nets used only as a return. One that is also some directive's
        # supply side (a sink wired between two rails) stays a rail.
        self.returns: set[Hashable] = set()
        supply_side: set[Hashable] = set()
        for it in items:
            if it.kind in ("source", "sink", "regulator"):
                self.returns |= it.ret
                supply_side |= it.out | it.inp
        self.returns -= supply_side
        self._parent: dict[Hashable, Hashable] = {}
        self.shunt_loads: set[Hashable] = set()
        for it in items:
            if it.kind != "series":
                continue
            a = {n for n in it.out if n not in self.returns}
            b = {n for n in it.inp if n not in self.returns}
            to_return = bool((it.out | it.inp) & self.returns)
            sides = sorted(a | b, key=repr)
            for other in sides[1:]:
                self._union(sides[0], other)
            if to_return:
                # A path to ground draws current from the rail it hangs off.
                self.shunt_loads |= {self.find(n) for n in a | b}
        self.shunt_loads = {self.find(n) for n in self.shunt_loads}

    def find(self, n: Hashable) -> Hashable:
        self._parent.setdefault(n, n)
        while self._parent[n] != n:
            self._parent[n] = self._parent[self._parent[n]]
            n = self._parent[n]
        return n

    def _union(self, a: Hashable, b: Hashable) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra

    def of(self, nets: set[Hashable]) -> set[Hashable]:
        return {self.find(n) for n in nets if n not in self.returns}


def analyse(items: list[Item]) -> tuple[dict[int, str], Rails]:
    """``({index: reason}, rails)`` for every source / sink / regulator that
    can carry no current. Series elements are never dropped."""
    rails = Rails(items)
    active = {i for i, it in enumerate(items)
              if it.kind in ("source", "sink", "regulator")}
    dropped: dict[int, str] = {}
    while True:
        # Supply side: rails a real source reaches, through regulators whose
        # input is itself supplied (a loop of regulators feeding each other
        # with no source is not supplied).
        powered: set[Hashable] = set()
        for i in active:
            if items[i].kind == "source":
                powered |= rails.of(items[i].out)
        grew = True
        while grew:
            grew = False
            for i in active:
                it = items[i]
                if it.kind == "regulator" and rails.of(it.inp) & powered:
                    new = rails.of(it.out) - powered
                    if new:
                        powered |= new
                        grew = True
        # Load side: rails something draws current from.
        loaded = set(rails.shunt_loads)
        for i in active:
            it = items[i]
            if it.kind in ("sink", "regulator"):
                loaded |= rails.of(it.inp)
        drop: dict[int, str] = {}
        for i in active:
            it = items[i]
            if it.kind == "sink":
                if not rails.of(it.inp) & powered:
                    drop[i] = NO_SUPPLY
            elif it.kind == "source":
                if not rails.of(it.out) & loaded:
                    drop[i] = NO_LOAD
            elif it.kind == "regulator":
                if not rails.of(it.inp) & powered:
                    drop[i] = INPUT_UNFED
                elif not rails.of(it.out) & loaded:
                    drop[i] = NO_LOAD
        if not drop:
            return dropped, rails
        dropped.update(drop)
        active -= drop.keys()


def rail_members(rails: Rails, items: list[Item], rail: Hashable) -> set[Hashable]:
    """Every net touched by a directive that lies on ``rail`` (for messages)."""
    out: set[Hashable] = set()
    for it in items:
        for n in it.out | it.inp:
            if n not in rails.returns and rails.find(n) == rail:
                out.add(n)
    return out
