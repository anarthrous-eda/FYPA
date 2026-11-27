"""Which names a KiCad board net goes by in the schematic.

Altium designs carry a compiled netlist; KiCad keeps none on disk. This module
traces the schematic's own connectivity - wires, junctions, pins, local /
global / hierarchical labels, sheet pins and power symbols - per sheet
*instance* (a sheet used twice is two instances), then ties each schematic net
to a board net through its part pins (designator + pin number = footprint
pad).

What it is for: a ``PDN_*_NET`` field on a symbol in a sub-sheet may name the
net as that sheet sees it - a local label ``VBUS`` that the board calls
``/Power/VBUS``, or a hierarchical label that the parent sheet joins to
``+5V``. :func:`resolve_net_name` turns such a name into the board's net name.

Pure stdlib; reads the files with :mod:`fypa.kicad.sexpr`. Buses and
bus entries are not followed (power nets rarely run through buses).
"""
from __future__ import annotations

import logging
import math
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from fypa.kicad import sexpr
from fypa.kicad.sexpr import SNode

log = logging.getLogger(__name__)

_TOL_MM = 1e-3          # coincidence tolerance (schematic grid is 0.05 in+)
_KEY = 1e-4             # coordinate key resolution


@dataclass
class SchematicNets:
    """Schematic names → board net indices.

    ``local[(sheet, name)]`` holds local and hierarchical label names as seen
    on one sheet instance (``sheet`` is the instance path in the board's form:
    ``"/"`` for the root, ``"/<sheet uuid>/…/"`` below it); ``global_[name]``
    holds global labels and power-symbol names. ``sheet_of`` gives each
    designator's sheet instance.
    """
    local: dict[tuple[str, str], int] = field(default_factory=dict)
    global_: dict[str, int] = field(default_factory=dict)
    sheet_of: dict[str, str] = field(default_factory=dict)
    nets: int = 0                       # schematic nets traced
    tied: int = 0                       # ... tied to a board net
    disagreeing: int = 0                # ... whose pins sit on two board nets


# ---------------------------------------------------------------------------
# Geometry: library pin → sheet coordinates
# ---------------------------------------------------------------------------
def pin_position(px: float, py: float, at: tuple[float, float, float],
                 mirror: str | None) -> tuple[float, float]:
    """A library pin's connection point on the sheet.

    Library symbols are drawn Y-up, sheets Y-down, so the pin is first flipped
    in Y; then the placed symbol's rotation (counter-clockwise on screen) and
    then its mirror (``x``: about the X axis, ``y``: about the Y axis) are
    applied, and it is moved to the symbol's position. (Order and sign were
    checked against a real KiCad 7 schematic: of the four combinations this
    is the one whose pins land on the board nets they should.)
    """
    x, y = px, -py
    a = math.radians(at[2])
    ca, sa = round(math.cos(a), 12), round(math.sin(a), 12)
    # Counter-clockwise on a Y-down screen.
    x, y = x * ca + y * sa, -x * sa + y * ca
    if mirror == "x":
        y = -y
    elif mirror == "y":
        x = -x
    return at[0] + x, at[1] + y


def _key(x: float, y: float) -> tuple[int, int]:
    return round(x / _KEY), round(y / _KEY)


def _on_segment(p, a, b) -> bool:
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    ll = dx * dx + dy * dy
    if ll < 1e-12:
        return math.hypot(px - ax, py - ay) <= _TOL_MM
    t = ((px - ax) * dx + (py - ay) * dy) / ll
    if t < -1e-9 or t > 1 + 1e-9:
        return False
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy)) <= _TOL_MM


# ---------------------------------------------------------------------------
# Library symbols
# ---------------------------------------------------------------------------
@dataclass
class _LibPin:
    number: str
    name: str
    x: float
    y: float
    hidden: bool
    etype: str


def _lib_index(root: SNode) -> dict[str, SNode]:
    lib = root.node("lib_symbols")
    return ({s.atom(0, "") or "": s for s in lib.nodes("symbol")}
            if lib is not None else {})


def _lib_pins(defn: SNode, lib: dict[str, SNode], unit: int, style: int,
              depth: int = 0) -> list[_LibPin]:
    """Pins of one unit / body style of a library symbol (unit 0 and style 0
    are common to all)."""
    out: list[_LibPin] = []
    parent = defn.s("extends")
    if parent and parent in lib and depth < 4:
        out += _lib_pins(lib[parent], lib, unit, style, depth + 1)
    base = defn.atom(0, "") or ""
    for sub in defn.nodes("symbol"):
        name = sub.atom(0, "") or ""
        tail = name[len(base.split(":")[-1]):] if name else ""
        parts = tail.rsplit("_", 2)
        try:
            u, c = int(parts[-2]), int(parts[-1])
        except (ValueError, IndexError):
            u, c = 0, 0
        if u not in (0, unit) or c not in (0, style):
            continue
        for pin in sub.nodes("pin"):
            at = pin.node("at")
            if at is None:
                continue
            nm = pin.node("name")
            nb = pin.node("number")
            out.append(_LibPin(
                number=(nb.atom(0, "") if nb is not None else "") or "",
                name=(nm.atom(0, "") if nm is not None else "") or "",
                x=at.f_at(0), y=at.f_at(1),
                hidden="hide" in pin.atoms or pin.node("hide") is not None,
                etype=pin.atom(0, "") or ""))
    return out


# ---------------------------------------------------------------------------
# Union-find
# ---------------------------------------------------------------------------
class _UF:
    def __init__(self) -> None:
        self.parent: list[int] = []

    def add(self) -> int:
        self.parent.append(len(self.parent))
        return len(self.parent) - 1

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


# ---------------------------------------------------------------------------
# Tracing
# ---------------------------------------------------------------------------
def _references(sym: SNode, inst: str, root_uuid: str,
                root_refs: dict[str, str]) -> str:
    """The symbol's designator on sheet instance *inst*."""
    insts = sym.node("instances")
    if insts is not None:
        for project in insts.nodes("project"):
            for path in project.nodes("path"):
                parts = [p for p in (path.atom(0, "") or "").split("/") if p]
                if parts and parts[0] == root_uuid:
                    parts = parts[1:]
                if "/" + "".join(p + "/" for p in parts) == inst:
                    ref = path.s("reference")
                    if ref:
                        return ref
    uid = sym.s("uuid") or ""
    hit = root_refs.get(inst + uid)
    if hit:
        return hit
    for p in sym.nodes("property"):
        if p.atom(0) == "Reference":
            return p.atom(1, "") or ""
    return ""


def build_schematic_nets(sch_path: Path,
                         pad_nets: dict[tuple[str, str], int]) -> SchematicNets:
    """Trace the schematic at *sch_path* (its root sheet) and tie its nets to
    the board through *pad_nets* (``(designator, pad number) → net index``,
    designators upper-case)."""
    result = SchematicNets()
    files: dict[Path, SNode] = {}

    def load(p: Path) -> SNode | None:
        key = p.resolve()
        if key not in files:
            try:
                files[key] = sexpr.parse_file(p)
            except (OSError, ValueError) as exc:
                log.warning("Could not read KiCad sheet %s: %s", p, exc)
                return None
        return files[key]

    root = load(sch_path)
    if root is None:
        return result
    root_uuid = root.s("uuid") or ""
    # KiCad 6: every instance's reference in one root table, by full path.
    root_refs: dict[str, str] = {}
    table = root.node("symbol_instances")
    if table is not None:
        for path in table.nodes("path"):
            parts = [p for p in (path.atom(0, "") or "").split("/") if p]
            if parts and parts[0] == root_uuid:
                parts = parts[1:]
            if parts and path.s("reference"):
                inst = "/" + "".join(p + "/" for p in parts[:-1])
                root_refs[inst + parts[-1]] = path.s("reference") or ""

    uf = _UF()
    terminals: dict[int, list[tuple[str, str]]] = {}   # node → (ref, pin)
    local_names: list[tuple[int, str, str]] = []        # (node, sheet, name)
    global_names: list[tuple[int, str]] = []
    global_first: dict[str, int] = {}
    # (child sheet instance, label name) → node, both ends of a sheet pin
    hier_child: dict[tuple[str, str], int] = {}
    hier_parent: dict[tuple[str, str], int] = {}

    def trace(node: SNode, path: Path, inst: str, depth: int) -> None:
        lib = _lib_index(node)
        wires: list[tuple[tuple[float, float], tuple[float, float], int]] = []
        points: list[tuple[tuple[float, float], int]] = []
        by_key: dict[tuple[int, int], int] = {}

        def point(xy: tuple[float, float]) -> int:
            k = _key(*xy)
            if k in by_key:
                return by_key[k]
            n = uf.add()
            by_key[k] = n
            points.append((xy, n))
            return n

        for w in node.nodes("wire"):
            pts = w.node("pts")
            xys = [(q.f_at(0), q.f_at(1)) for q in pts.nodes("xy")] if pts else []
            for a, b in zip(xys, xys[1:]):
                n = uf.add()
                wires.append((a, b, n))
                uf.union(n, point(a))
                uf.union(n, point(b))
        for j in node.nodes("junction"):
            at = j.node("at")
            if at is not None:
                point((at.f_at(0), at.f_at(1)))
        for kind in ("label", "global_label", "hierarchical_label"):
            for lb in node.nodes(kind):
                at = lb.node("at")
                name = lb.atom(0, "") or ""
                if at is None or not name:
                    continue
                n = point((at.f_at(0), at.f_at(1)))
                if kind == "label":
                    local_names.append((n, inst, name))
                elif kind == "global_label":
                    global_names.append((n, name))
                else:
                    local_names.append((n, inst, name))
                    hier_child[(inst, name)] = n
        for sym in node.nodes("symbol"):
            at = sym.node("at")
            if at is None:
                continue
            defn = lib.get(sym.s("lib_name") or sym.s("lib_id") or "")
            if defn is None:
                continue
            place = (at.f_at(0), at.f_at(1), at.f_at(2))
            mirror = sym.s("mirror")
            unit = int(sym.f("unit", default=1) or 1)
            style = int(sym.f("convert", default=1) or 1)
            pins = _lib_pins(defn, lib, unit, style)
            props = {p.atom(0): p.atom(1, "") or "" for p in sym.nodes("property")}
            if defn.node("power") is not None or "power" in defn.atoms:
                # A power port (pin type power_in) names a global net after
                # its Value. PWR_FLAG has a power_out pin and names nothing -
                # joining every flag by its "PWR_FLAG" value would merge
                # every flagged net into one.
                name = (props.get("Value", "")
                        if any(p.etype == "power_in" for p in pins) else "")
                for pin in pins:
                    n = point(pin_position(pin.x, pin.y, place, mirror))
                    if name:
                        global_names.append((n, name))
                continue
            ref = _references(sym, inst, root_uuid, root_refs)
            if not ref or ref.startswith("#"):
                continue
            result.sheet_of.setdefault(ref.upper(), inst)
            for pin in pins:
                n = point(pin_position(pin.x, pin.y, place, mirror))
                terminals.setdefault(n, []).append((ref.upper(), pin.number))
                if pin.hidden and pin.etype == "power_in" and pin.name:
                    # KiCad 6 / 7: a hidden power input joins the global net
                    # of its name.
                    global_names.append((n, pin.name))
        children: list[tuple[str, Path]] = []
        for sh in node.nodes("sheet"):
            uid = sh.s("uuid") or ""
            child_file = ""
            for p in sh.nodes("property"):
                if p.atom(0) in ("Sheetfile", "Sheet file"):
                    child_file = p.atom(1, "") or ""
            child_inst = inst + uid + "/"
            for pin in sh.nodes("pin"):
                at = pin.node("at")
                name = pin.atom(0, "") or ""
                if at is not None and name:
                    hier_parent[(child_inst, name)] = point(
                        (at.f_at(0), at.f_at(1)))
            if child_file:
                children.append((child_inst, path.parent / child_file))

        # Wires meet anything lying on them (a T-junction or a label on a
        # wire's middle), not just at their ends.
        for xy, n in points:
            for a, b, wn in wires:
                if _on_segment(xy, a, b):
                    uf.union(n, wn)
        # Same local label name on one sheet instance: one net.
        first: dict[str, int] = {}
        for n, sheet, name in local_names:
            if sheet == inst:
                if name in first:
                    uf.union(first[name], n)
                else:
                    first[name] = n
        if depth < 32:
            for child_inst, child_path in children:
                sub = load(child_path)
                if sub is not None:
                    trace(sub, child_path, child_inst, depth + 1)

    trace(root, sch_path, "/", 0)

    for n, name in global_names:
        if name in global_first:
            uf.union(global_first[name], n)
        else:
            global_first[name] = n
    for key, n in hier_parent.items():
        child = hier_child.get(key)
        if child is not None:
            uf.union(n, child)

    # Tie each schematic net to the board net its pins sit on.
    votes: dict[int, Counter] = {}
    pins_on: dict[int, list[tuple[str, str, int]]] = {}
    for n, terms in terminals.items():
        root_n = uf.find(n)
        for ref, pin in terms:
            net = pad_nets.get((ref, pin))
            if net is not None and net >= 0:
                votes.setdefault(root_n, Counter())[net] += 1
                pins_on.setdefault(root_n, []).append((ref, pin, net))
    board_of: dict[int, int] = {}
    for root_n, c in votes.items():
        (net, _count), *rest = c.most_common()
        board_of[root_n] = net
        if rest:
            # Schematic and board disagree (board not updated from the
            # schematic, or a part the tracer can't place): the majority wins.
            result.disagreeing += 1
            log.debug("KiCad schematic net with pins on board nets %s: %s",
                      dict(c), pins_on.get(root_n))
    roots = {uf.find(i) for i in range(len(uf.parent))}
    result.nets = len(roots)
    result.tied = len(board_of)

    def put(table: dict, key, net: int, clash: set) -> None:
        if key in table and table[key] != net:
            clash.add(key)
        table[key] = net

    clash_l: set = set()
    clash_g: set = set()
    for n, sheet, name in local_names:
        net = board_of.get(uf.find(n))
        if net is not None:
            put(result.local, (sheet, name), net, clash_l)
    for n, name in global_names:
        net = board_of.get(uf.find(n))
        if net is not None:
            put(result.global_, name, net, clash_g)
    for k in clash_l:                     # ambiguous: don't guess
        result.local.pop(k, None)
    for k in clash_g:
        result.global_.pop(k, None)
    return result


def resolve_net_name(sn: SchematicNets, designator: str, name: str,
                     board_names: list[str]) -> str | None:
    """The board's name for net *name* as written on part *designator*, or
    ``None`` when the schematic doesn't say. The part's own sheet first, then
    global names; exact match first, then ignoring case."""
    sheet = sn.sheet_of.get(designator.upper())
    for exact in (True, False):
        for (sh, nm), net in sn.local.items():
            if sh == sheet and (nm == name if exact else nm.lower() == name.lower()):
                return board_names[net] if 0 <= net < len(board_names) else None
        for nm, net in sn.global_.items():
            if nm == name if exact else nm.lower() == name.lower():
                return board_names[net] if 0 <= net < len(board_names) else None
    return None
