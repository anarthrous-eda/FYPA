"""Rows layout for the topology diagram: one row per power origin.

The classic layout puts every directive in one left-to-right chain of columns
and routes every net between them, which on a real board turns into a single
very wide row with long wires running over most of it. This layout instead
breaks the PDN into rows a reader can take in one at a time:

* Each SOURCE starts a row. Each REGULATOR starts its own row, placed under the
  row that feeds it and indented one step per conversion stage, so the row
  order reads as the power tree.
* Within a row, nets fan out left to right as a tree: a net's driver on the
  left, everything it feeds stacked in the next column. Tree wiring never
  crosses itself, so there are no bridges and no long horizontal runs.
* A connection that leaves the row is not drawn as a wire. Both ends get a
  power-port tag instead (``→ U6`` where the regulator is fed, the input net
  on the regulator's own row), and clicking a tag pans to the other end.
* Returns get a local ground symbol on each part instead of a shared rail.
* Rails nothing drives get their own rows, opened by a warning tag.
* 0 Ω auto-bridges on nets no source, sink or regulator touches (I²C pull-up
  links and the like) are listed at the end, hidden unless asked for.

Every port is either wired, tagged or grounded, so no connection is lost when
the tree has to give up on drawing a wire (loops, parts with several rails,
parallel sources).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field

from fypa.topology.constants import (
    BODY_PAD,
    GND_NET,
    HEADER_H,
    MARGIN,
    NODE_W,
    PORT_ROW_H,
    TAG_POINT,
    TAG_TEXT_PAD,
)
from fypa.topology.metadata.layout_bridge import parse_topology_directives
from fypa.topology.metadata.specs import (
    jump_row_for_directive,
    natural_sort_key,
    spec_port_role,
)
from fypa.topology.metadata_schema import NodeSpec, TopologyMetadata
from fypa.topology.terminal_roles import is_output_port, is_single_net_node
from fypa.topology.types import (
    TopologyBand,
    TopologyModel,
    TopologyNode,
    TopologyPort,
    TopologyTag,
    TopologyWire,
)
from fypa.topology.util import (
    estimate_text_width,
    format_directive_value,
    truncate_label,
)

# --- geometry ---------------------------------------------------------------

BAND_H = 26.0            # row header text band above a row's content
ROW_GAP = 22.0           # empty space between one row's content and the next band
SIBLING_GAP = 12.0       # between stacked subtrees fed by the same net
DEPTH_INDENT = 32.0      # per regulator stage
BUS_INSET = 22.0         # bus x distance in front of the child column
BUS_SPACING = 14.0       # between buses of one node's sibling output nets
MIN_TRUNK = 56.0         # shortest wire from a driver port to its bus
LABEL_FONT = 8.0
LABEL_PAD = 10.0         # each side of a wire label along its trunk
LABEL_RISE = 7.0         # wire label centre above the wire
TAG_H = 16.0
TAG_STUB = 14.0          # wire between a port and its tag
TAG_FONT = 8.0
VALUE_ROW_H = 14.0       # extra body line showing the directive value
GND_STUB = 8.0           # wire from a bottom GND port down to the symbol
GND_GLYPH_H = 14.0       # height of the three-bar ground symbol
GND_PORT_SPACING = 28.0
CHIP_W = 124.0
CHIP_H = 20.0
CHIP_GAP = 10.0
BRIDGE_WRAP_W = 700.0    # min wrap width of the off-tree bridges list

PASSIVE_ROLES = ("RESISTOR", "SERIES", "AUTO_BRIDGE")
ORIGIN_ROLES = ("SOURCE", "REGULATOR")


def tag_width(text: str) -> float:
    return estimate_text_width(text, TAG_FONT) + 2 * TAG_TEXT_PAD + TAG_POINT


def _label_width(text: str) -> float:
    return estimate_text_width(text, LABEL_FONT) + 2 * LABEL_PAD


# --- parsed parts -----------------------------------------------------------


@dataclass
class _Port:
    name: str
    net: str          # wire net (GND_NET for any ground alias)
    label: str        # display net name
    tooltip: str
    side: str         # side from the spec (flow-oriented)
    sort_key: int
    role: str         # effective role of this port


@dataclass
class _Part:
    spec: NodeSpec
    node_id: str
    kind: str         # "origin" | "passive" | "load"
    ports: list[_Port]
    auto_bridge: bool = False

    @property
    def power_ports(self) -> list[_Port]:
        return [p for p in self.ports if p.net and p.net != GND_NET]

    @property
    def gnd_ports(self) -> list[_Port]:
        return [p for p in self.ports if p.net == GND_NET]

    @property
    def self_bridge(self) -> bool:
        """A passive whose every rail port sits on one net (a 0 Ω link
        between two copper names merged into one rail)."""
        rail = self.power_ports
        if self.kind != "passive" or self.gnd_ports or not rail:
            return False
        # Ports on one net are often deduplicated to a single P row.
        return len({p.net for p in rail}) == 1

    def output_ports(self) -> list[_Port]:
        return [
            p for p in self.power_ports
            if p.role in ORIGIN_ROLES and is_output_port(p.role, p.name, p.side)
        ]

    def propagating(self, p: _Port) -> bool:
        """Does power continue through this part from port ``p``?"""
        return self.kind == "passive" and p.role in PASSIVE_ROLES


def _parts_from_specs(specs: list[NodeSpec]) -> list[_Part]:
    parts: list[_Part] = []
    for s in specs:
        resolved = s.get("resolved_ports") or {}
        ports: list[_Port] = []
        for pname, side, sk in s["port_defs"]:
            rp = resolved.get(pname)
            if rp is None:
                continue
            ports.append(_Port(
                name=pname,
                net=rp.wnet or "",
                label=rp.plabel or rp.wnet or "",
                tooltip=rp.tooltip,
                side=side,
                sort_key=sk,
                role=spec_port_role(s, pname),
            ))
        roles = {p.role for p in ports} | {s["role"]}
        part = _Part(spec=s, node_id=s["node_id"], kind="load", ports=ports)
        if part.output_ports():
            part.kind = "origin"
        elif roles & set(PASSIVE_ROLES):
            part.kind = "passive"
        part.auto_bridge = roles <= {"AUTO_BRIDGE"}
        parts.append(part)
    return parts


# --- logical tree -------------------------------------------------------------


@dataclass
class _Branch:
    """One net fanning out from a driver to the parts it feeds."""

    net: str
    label: str
    children: list[_Child] = field(default_factory=list)


@dataclass
class _Child:
    kind: str                     # "node" | "chip" | "ref"
    part: _Part
    in_ports: list[str] = field(default_factory=list)  # wired to the parent bus
    branches: dict[str, _Branch] = field(default_factory=dict)  # port -> branch

    def size(self) -> int:
        return 1 + sum(c.size() for b in self.branches.values() for c in b.children)


@dataclass
class _Row:
    kind: str                     # "source" | "regulator" | "undriven" | "other"
    depth: int
    root: _Child | None = None    # origin part (source / regulator rows)
    root_branch: _Branch | None = None  # undriven rows: tree from a bare net
    extra: list[_Child] = field(default_factory=list)  # "other" rows


class _TreeBuilder:
    """Claims parts and nets row by row (breadth-first within a row)."""

    def __init__(self, parts: list[_Part]) -> None:
        self.parts = {p.node_id: p for p in parts}
        self.by_net: dict[str, list[tuple[_Part, _Port]]] = defaultdict(list)
        for part in parts:
            for port in part.power_ports:
                self.by_net[port.net].append((part, port))
        self.placed: set[str] = set()
        self.net_done: set[str] = set()
        # regulator node_id -> list of nets on which a "→ U6" ref was placed
        self.refs: dict[str, list[str]] = defaultdict(list)
        self.ref_order: list[str] = []

    def grow(self, branches: list[_Branch]) -> None:
        """Fill ``branches`` (nets already marked done) breadth-first."""
        queue = list(branches)
        while queue:
            branch = queue.pop(0)
            seen_ref: set[str] = set()
            for part, port in sorted(
                self.by_net.get(branch.net, []),
                key=lambda pp: natural_sort_key(pp[0].node_id),
            ):
                if part.kind == "origin":
                    if port in part.output_ports():
                        continue  # a parallel driver: tagged on its own row
                    if part.node_id in seen_ref:
                        continue
                    seen_ref.add(part.node_id)
                    branch.children.append(_Child(kind="ref", part=part))
                    self.refs[part.node_id].append(branch.net)
                    if part.node_id not in self.ref_order:
                        self.ref_order.append(part.node_id)
                    continue
                if part.node_id in self.placed:
                    continue
                self.placed.add(part.node_id)
                in_ports = [p.name for p in part.power_ports if p.net == branch.net]
                if part.self_bridge:
                    branch.children.append(
                        _Child(kind="chip", part=part, in_ports=in_ports[:1]),
                    )
                    continue
                child = _Child(kind="node", part=part, in_ports=in_ports)
                for p in part.power_ports:
                    if p.net == branch.net or not part.propagating(p):
                        continue
                    if p.net in self.net_done:
                        continue  # loop / already drawn: tagged later
                    self.net_done.add(p.net)
                    nb = _Branch(net=p.net, label=p.net)
                    child.branches[p.name] = nb
                    queue.append(nb)
                branch.children.append(child)
        for b in branches:
            _sort_branch_tree(b)

    def origin_child(self, part: _Part) -> _Child:
        self.placed.add(part.node_id)
        child = _Child(kind="node", part=part)
        new: list[_Branch] = []
        for p in part.output_ports():
            if p.net in self.net_done:
                continue
            self.net_done.add(p.net)
            nb = _Branch(net=p.net, label=p.net)
            child.branches[p.name] = nb
            new.append(nb)
        self.grow(new)
        return child


def _child_order(c: _Child) -> tuple:
    rank = {"node": 0, "chip": 2, "ref": 1}[c.kind]
    if c.kind == "node" and c.part.kind == "passive":
        return (0, -c.size(), natural_sort_key(c.part.node_id))
    if c.kind == "node":
        return (1, 0, natural_sort_key(c.part.node_id))
    return (1 + rank, 0, natural_sort_key(c.part.node_id))


def _sort_branch_tree(branch: _Branch) -> None:
    branch.children.sort(key=_child_order)
    for c in branch.children:
        for b in c.branches.values():
            _sort_branch_tree(b)


def _pick_undriven_root(parts: list[_Part], builder: _TreeBuilder) -> str | None:
    """Rail an undriven row starts from: the net most parts take power *from*.

    Spec port sides are already flow-oriented (inputs left), so prefer a net
    that appears on left-side ports and not on right-side ones.
    """
    score: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for part in parts:
        if part.node_id in builder.placed or part.auto_bridge:
            continue
        for p in part.power_ports:
            if p.net in builder.net_done:
                continue
            if p.side == "right":
                score[p.net][0] += 1
            else:
                score[p.net][1] += 1
    if not score:
        return None
    return min(
        score,
        key=lambda n: (score[n][0], -score[n][1], natural_sort_key(n)),
    )


def _plan_rows(parts: list[_Part]) -> tuple[list[_Row], list[_Part], _TreeBuilder]:
    builder = _TreeBuilder(parts)
    rows: list[_Row] = []
    origins = sorted(
        (p for p in parts if p.kind == "origin"),
        key=lambda p: (
            0 if any(pt.role == "SOURCE" for pt in p.ports) else 1,
            natural_sort_key(p.node_id),
        ),
    )

    def emit(part: _Part, depth: int) -> None:
        kind = "source" if any(pt.role == "SOURCE" for pt in part.output_ports()) else "regulator"
        before = len(builder.ref_order)
        row = _Row(kind=kind, depth=depth, root=builder.origin_child(part))
        rows.append(row)
        for reg_id in list(builder.ref_order[before:]):
            if reg_id not in builder.placed:
                emit(builder.parts[reg_id], depth + 1)

    for part in origins:
        if part.node_id not in builder.placed:
            emit(part, 0)

    # Rails nothing drives: one row each, starting from the input-most net.
    while True:
        root = _pick_undriven_root(parts, builder)
        if root is None:
            break
        builder.net_done.add(root)
        branch = _Branch(net=root, label=root)
        builder.grow([branch])
        rows.append(_Row(kind="undriven", depth=0, root_branch=branch))
        for reg_id in list(builder.ref_order):
            if reg_id not in builder.placed:
                emit(builder.parts[reg_id], 1)

    leftovers = [
        p for p in parts
        if p.node_id not in builder.placed and not p.auto_bridge
    ]
    if leftovers:
        row = _Row(kind="other", depth=0)
        for p in sorted(leftovers, key=lambda p: natural_sort_key(p.node_id)):
            builder.placed.add(p.node_id)
            row.extra.append(_Child(kind="node", part=p))
        rows.append(row)

    hidden = [p for p in parts if p.node_id not in builder.placed]
    return rows, hidden, builder


# --- geometry -----------------------------------------------------------------


@dataclass
class _Shape:
    """A child's local geometry, before it is placed."""

    width: float
    height: float          # body height
    extent: float          # body + anything hanging below (ground symbol)
    port_xy: dict[str, tuple[float, float]]   # local port positions
    port_side: dict[str, str]
    left_tags: float = 0.0     # how far tags reach left of the body
    right_tags: float = 0.0    # how far tags reach right of the body
    tagged: list[str] = field(default_factory=list)


def _node_shape(child: _Child, builder: _TreeBuilder) -> _Shape:
    part = child.part
    if child.kind == "ref":
        w = tag_width(part.spec["label"])
        return _Shape(w, TAG_H, TAG_H, {"in": (0.0, TAG_H / 2)}, {"in": "left"})
    if child.kind == "chip":
        port_xy = {}
        sides = {}
        if child.in_ports:
            port_xy[child.in_ports[0]] = (0.0, CHIP_H / 2)
            sides[child.in_ports[0]] = "left"
        return _Shape(CHIP_W, CHIP_H, CHIP_H, port_xy, sides)

    branch_ports = set(child.branches)
    sides: dict[str, str] = {}
    for p in part.ports:
        if p.net == GND_NET and not part.spec.get("sections"):
            sides[p.name] = "bottom"
        elif p.name in child.in_ports:
            sides[p.name] = "left"
        elif p.name in branch_ports:
            sides[p.name] = "right"
        elif part.kind == "passive" and part.propagating(p) and child.in_ports:
            sides[p.name] = "right"  # flow continues out of the far side
        else:
            sides[p.name] = p.side
    left = sorted((p for p in part.ports if sides[p.name] == "left"), key=_port_order(child))
    right = sorted((p for p in part.ports if sides[p.name] == "right"), key=_port_order(child))
    bottom = [p for p in part.ports if sides[p.name] == "bottom"]
    n_rows = max(len(left), len(right), 1)
    value = _value_text(part)
    h = HEADER_H + BODY_PAD + n_rows * PORT_ROW_H + BODY_PAD
    if value:
        h += VALUE_ROW_H
    port_xy: dict[str, tuple[float, float]] = {}
    for i, p in enumerate(left):
        port_xy[p.name] = (0.0, HEADER_H + BODY_PAD + i * PORT_ROW_H + PORT_ROW_H / 2)
    for i, p in enumerate(right):
        port_xy[p.name] = (NODE_W, HEADER_H + BODY_PAD + i * PORT_ROW_H + PORT_ROW_H / 2)
    n_b = len(bottom)
    for i, p in enumerate(bottom):
        port_xy[p.name] = (NODE_W / 2 + (i - (n_b - 1) / 2) * GND_PORT_SPACING, h)
    extent = h + (GND_STUB + GND_GLYPH_H if bottom else 0.0)

    shape = _Shape(NODE_W, h, extent, port_xy, sides)
    for p in part.ports:
        if p.net == GND_NET:
            continue
        if p.name in child.in_ports or p.name in branch_ports:
            continue
        shape.tagged.append(p.name)
        reach = TAG_STUB + tag_width(truncate_label(p.net))
        if sides[p.name] == "left":
            shape.left_tags = max(shape.left_tags, reach)
        else:
            shape.right_tags = max(shape.right_tags, reach)
    # Branches with nothing on them are tagged once placement is done; keep
    # room for those tags too so wire labels don't land on them.
    for name, branch in child.branches.items():
        if not branch.children:
            reach = TAG_STUB + tag_width(truncate_label(branch.net))
            shape.right_tags = max(shape.right_tags, reach)
    return shape


def _port_order(child: _Child):
    """Wired inputs first, then branch drivers, then by spec order."""
    branch_ports = list(child.branches)

    def key(p: _Port) -> tuple:
        if p.name in child.in_ports:
            return (0, child.in_ports.index(p.name), 0)
        if p.name in branch_ports:
            return (1, branch_ports.index(p.name), 0)
        return (2, p.sort_key, 0)

    return key


def _value_text(part: _Part) -> str:
    return directive_value_text(part.spec.get("directive") or {})


def directive_value_text(d: dict) -> str:
    """Short value for a metadata directive: ``12 V``, ``5 mΩ``, ``10 A``,
    ``BUCK · 1 V``. Shared by the Topology blocks and the part sidebar so
    both say the same thing."""
    value = format_directive_value(d)
    if d.get("role") == "REGULATOR":
        kind = str(d.get("smps_topology") or d.get("regulator_type") or "").strip()
        if kind and d.get("value") is not None:
            return f"{kind} · {value}"
        if d.get("value") is None:
            return str(d.get("value_str") or "").strip()
    return value


class _Placer:
    def __init__(
        self,
        builder: _TreeBuilder,
        add_candidates: Mapping[str, Collection[str]] | None = None,
    ) -> None:
        self.builder = builder
        # net (upper) -> designators (upper) that could be added as a SINK;
        # None = no "+ SINK" handles at all.
        self.add_candidates = (
            None if add_candidates is None else {
                k.upper(): {d.upper() for d in v} for k, v in add_candidates.items()
            }
        )
        self.nodes: list[TopologyNode] = []
        self.wires: list[TopologyWire] = []
        self.tags: list[TopologyTag] = []
        self.gnd_glyphs: list[tuple[float, float]] = []
        self.max_x = 0.0
        # net -> point where it is drawn from (driver port), for tag targets
        self.net_home: dict[str, tuple[float, float]] = {}
        # regulator node_id -> list of (net, ref tag point)
        self.ref_points: dict[str, list[tuple[str, tuple[float, float]]]] = defaultdict(list)
        self.node_pos: dict[str, tuple[float, float]] = {}
        # deferred tags: (node, port, side) resolved once every net home is known
        self.pending_tags: list[tuple[TopologyNode, TopologyPort, _Part]] = []
        self.feed_tags: list[tuple[TopologyTag, str, str]] = []
        # driver ports whose net has nothing hanging off it in this row
        self.pending_ends: list[TopologyPort] = []

    # -- bookkeeping --------------------------------------------------------

    def _grow_x(self, x: float) -> None:
        self.max_x = max(self.max_x, x)

    def _shape(self, child: _Child) -> _Shape:
        return _node_shape(child, self.builder)

    # -- subtree measurement ------------------------------------------------

    def first_in_offset(self, child: _Child, shape: _Shape) -> float:
        if child.kind == "ref":
            return TAG_H / 2
        if child.in_ports:
            return shape.port_xy[child.in_ports[0]][1]
        return HEADER_H + BODY_PAD + PORT_ROW_H / 2

    # -- placement ------------------------------------------------------------

    def place_child(
        self, child: _Child, x: float, top: float,
    ) -> tuple[float, list[tuple[float, float]]]:
        """Place ``child`` with its left edge at ``x``; return (bottom, in points)."""
        shape = self._shape(child)
        part = child.part
        if child.kind == "ref":
            y = top + TAG_H / 2
            text = part.spec["label"]
            tag = TopologyTag(
                net="",
                text=text,
                kind="ref",
                x=x,
                y=y,
                width=shape.width,
                height=TAG_H,
                target_node=part.node_id,
            )
            self.tags.append(tag)
            self._grow_x(x + shape.width)
            return top + TAG_H, [(x, y)]

        node = self._make_node(child, shape, x, top)
        self.nodes.append(node)
        self.node_pos[part.node_id] = (x, top)
        self._grow_x(x + shape.width + shape.right_tags)
        ports = {p.terminal: p for p in node.ports}
        in_points = [(ports[n].x, ports[n].y) for n in child.in_ports if n in ports]
        bottom = top + shape.extent

        for p in node.ports:
            if p.side == "bottom":
                self.wires.append(TopologyWire(
                    net=GND_NET,
                    path_d=f"M {p.x:.1f},{p.y:.1f} V {p.y + GND_STUB:.1f}",
                    src_node=node.node_id,
                    src_terminal=p.terminal,
                    routing_kind="gnd_local",
                ))
                self.gnd_glyphs.append((p.x, p.y + GND_STUB))
            elif p.terminal in shape.tagged:
                self.pending_tags.append((node, p, part))

        if child.branches:
            bottom = max(bottom, self._place_branches(child, node, shape, x, top))
        return bottom, in_points

    def _place_branches(
        self, child: _Child, node: TopologyNode, shape: _Shape, x: float, top: float,
    ) -> float:
        ports = {p.terminal: p for p in node.ports}
        branches = sorted(
            ((ports[name], b) for name, b in child.branches.items() if name in ports),
            key=lambda pb: pb[0].y,
        )
        if not branches:
            return top
        nb = len(branches)
        child_shapes = {
            id(c): self._shape(c) for _, b in branches for c in b.children
        }
        left_clear = BUS_INSET + max(
            (s.left_tags for s in child_shapes.values()), default=0.0,
        )
        label_need = max(
            (_label_width(truncate_label(b.label)) for _, b in branches), default=0.0,
        )
        right_clear = max(
            MIN_TRUNK,
            shape.right_tags + label_need,
            shape.right_tags + BUS_INSET,
        )
        node_right = x + shape.width
        cx = node_right + right_clear + (nb - 1) * BUS_SPACING + left_clear
        cursor = top
        bottom = top
        for bi, (drv, branch) in enumerate(branches):
            bus_x = cx - left_clear - bi * BUS_SPACING
            cursor, b_bottom = self._place_branch(
                branch, drv, cx, bus_x, cursor, child_shapes,
                label_from=node_right + shape.right_tags,
            )
            bottom = max(bottom, b_bottom)
        return bottom

    def _place_branch(
        self,
        branch: _Branch,
        drv: TopologyPort,
        cx: float,
        bus_x: float,
        top: float,
        child_shapes: dict[int, _Shape],
        *,
        label_from: float | None = None,
    ) -> tuple[float, float]:
        """Lay out one net's children; return (next free y, bottom).

        ``label_from`` is the leftmost x the net label may start at (clear of
        tags hanging off the driver's other ports).
        """
        net = branch.net
        self.net_home.setdefault(net, (drv.x, drv.y))
        taps: list[tuple[float, float]] = []
        cursor = top
        bottom = drv.y
        for i, c in enumerate(branch.children):
            shape = child_shapes[id(c)]
            off = self.first_in_offset(c, shape)
            ctop = max(cursor, drv.y - off) if i == 0 else cursor
            c_bottom, pts = self.place_child(c, cx, ctop)
            if c.kind == "ref":
                self.ref_points[c.part.node_id].append((net, pts[0]))
                self.tags[-1].net = net
            taps.extend(pts)
            cursor = c_bottom + SIBLING_GAP
            bottom = max(bottom, c_bottom)
        # "+ SINK" handle one tap below the last load. A rail with no loads
        # gets the "no loads" end tag instead, which already offers it.
        n_add = self._add_sink_count(net) if branch.children else 0
        add_y = cursor + TAG_H / 2
        if n_add:
            cursor += TAG_H + SIBLING_GAP
            bottom = max(bottom, add_y + TAG_H / 2)
        self._route_net(net, branch.label, drv, bus_x, taps, label_from=label_from)
        if n_add:
            self._add_sink_handle(net, branch.label, drv, bus_x, cx, add_y, taps, n_add)
        return cursor, bottom

    def _add_sink_count(self, net: str) -> int:
        """Candidate loads on ``net`` not already drawn on it (any role)."""
        if self.add_candidates is None or not net or net == GND_NET:
            return 0
        cands = self.add_candidates.get(net.upper())
        if not cands:
            return 0
        drawn = {
            str(part.spec.get("designator") or "").split("#")[0].upper()
            for part, _ in self.builder.by_net.get(net, [])
        }
        return len(cands - drawn)

    def _add_sink_handle(
        self,
        net: str,
        label: str,
        drv: TopologyPort,
        bus_x: float,
        cx: float,
        y: float,
        taps: list[tuple[float, float]],
        n: int,
    ) -> None:
        """Dashed tap off the bus to a dashed ``+ SINK (n)`` tag at ``y``."""
        from_y = max([drv.y] + [ty for _, ty in taps])
        self.wires.append(TopologyWire(
            net=net,
            path_d=f"M {bus_x:.1f},{from_y:.1f} V {y:.1f} H {cx:.1f}",
            routing_kind="add_tap",
            dashed=True,
        ))
        text = f"+ SINK ({n})"
        w = tag_width(text)
        self.tags.append(TopologyTag(
            net=net, text=text, kind="add", x=cx, y=y, width=w, height=TAG_H,
            tooltip=(
                f"{label}: {n} more part(s) on this rail (ICs, connectors) "
                "could be a load.\nClick to add a SINK on it in Manual setup."
            ),
        ))
        self._grow_x(cx + w)

    def _route_net(
        self,
        net: str,
        label: str,
        drv: TopologyPort,
        bus_x: float,
        taps: list[tuple[float, float]],
        *,
        label_from: float | None = None,
    ) -> None:
        text = truncate_label(label)
        straight = len(taps) == 1 and abs(taps[0][1] - drv.y) < 0.5
        if not taps:
            # Nothing hangs off this net here: tag it once every part is
            # placed - "continues at …" if the net reaches a part drawn
            # elsewhere (a loop), else "no loads".
            self.pending_ends.append(drv)
            return
        end_x = taps[0][0] if straight else bus_x
        label_x = (drv.x + min(end_x, bus_x)) / 2
        if label_from is not None:
            label_x = max(label_x, label_from + _label_width(text) / 2)
        trunk = TopologyWire(
            net=net,
            path_d=f"M {drv.x:.1f},{drv.y:.1f} H {end_x:.1f}",
            label=text,
            label_x=label_x,
            label_y=drv.y - LABEL_RISE,
            src_node=drv.node_id,
            src_terminal=drv.terminal,
            routing_kind="row_trunk",
        )
        self.wires.append(trunk)
        if straight:
            return
        ys = [drv.y] + [ty for _, ty in taps]
        self.wires.append(TopologyWire(
            net=net,
            path_d=f"M {bus_x:.1f},{min(ys):.1f} V {max(ys):.1f}",
            routing_kind="row_bus",
            bus_x=bus_x,
        ))
        for tx, ty in taps:
            self.wires.append(TopologyWire(
                net=net,
                path_d=f"M {bus_x:.1f},{ty:.1f} H {tx:.1f}",
                routing_kind="row_tap",
            ))

    def _add_tag_at_port(
        self, port: TopologyPort, net: str, label: str, *, kind: str,
        tooltip: str = "", target: tuple[float, float] | None = None,
    ) -> TopologyTag:
        text = truncate_label(label)
        w = tag_width(text)
        if port.side == "left":
            tip = port.x - TAG_STUB
            x0 = tip - w
            path = f"M {tip:.1f},{port.y:.1f} H {port.x:.1f}"
        else:
            x0 = port.x + TAG_STUB
            path = f"M {port.x:.1f},{port.y:.1f} H {x0:.1f}"
        self.wires.append(TopologyWire(
            net=net, path_d=path, routing_kind="tag_stub",
            src_node=port.node_id, src_terminal=port.terminal,
        ))
        tag = TopologyTag(
            net=net, text=text, kind=kind, x=x0, y=port.y, width=w,
            height=TAG_H, tooltip=tooltip, target=target,
        )
        self.tags.append(tag)
        self._grow_x(x0 + w)
        return tag

    def _make_node(self, child: _Child, shape: _Shape, x: float, y: float) -> TopologyNode:
        part = child.part
        s = part.spec
        resolved = s.get("resolved_ports") or {}
        compact = child.kind == "chip"
        node = TopologyNode(
            node_id=part.node_id,
            label=s["label"],
            designator=s["designator"],
            role=s["role"],
            x=x,
            y=y,
            width=shape.width,
            height=shape.height,
            config_label="",
            has_error=s["has_error"],
            tooltip=_node_tooltip(part),
            single_net=(
                is_single_net_node(s["role"], s["port_defs"])
                if not s.get("sections") else False
            ),
            bounds=(x, y, shape.width, shape.height),
            jump_row=jump_row_for_directive(s["directive"]),
            compact=compact,
            value_str=_value_text(part),
        )
        for p in part.ports:
            if p.name not in shape.port_xy:
                continue
            lx, ly = shape.port_xy[p.name]
            rp = resolved.get(p.name)
            two_sided = any(
                abs(oy - ly) < 0.5 and abs(ox - lx) > 0.5
                for name, (ox, oy) in shape.port_xy.items() if name != p.name
            )
            node.ports.append(TopologyPort(
                terminal=p.name,
                net=p.net,
                label=truncate_label(p.label, max_len=10 if two_sided else 22),
                side=shape.port_side.get(p.name, p.side),
                x=x + lx,
                y=y + ly,
                node_id=part.node_id,
                role=p.role,
                tooltip=rp.tooltip if rp is not None else p.tooltip,
            ))
        return node

    # -- rows -----------------------------------------------------------------

    def place_origin_row(self, row: _Row, x: float, top: float) -> float:
        child = row.root
        assert child is not None
        shape = self._shape(child)
        bottom, _ = self.place_child(child, x + shape.left_tags, top)
        return bottom

    def place_undriven_row(self, row: _Row, x: float, top: float) -> float:
        branch = row.root_branch
        assert branch is not None
        text = truncate_label(branch.label)
        w = tag_width(text)
        shapes = {id(c): self._shape(c) for c in branch.children}
        first = branch.children[0] if branch.children else None
        off = self.first_in_offset(first, shapes[id(first)]) if first else TAG_H / 2
        y = top + max(off, TAG_H / 2)
        tag = TopologyTag(
            net=branch.net,
            text=text,
            kind="undriven",
            x=x,
            y=y,
            width=w,
            height=TAG_H,
            tooltip=(
                f"{branch.label}: nothing drives this rail.\n"
                "No SOURCE or REGULATOR output is on this net or upstream of it."
            ),
        )
        self.tags.append(tag)
        drv = TopologyPort(
            terminal="", net=branch.net, label=branch.label, side="right",
            x=x + w, y=y, node_id="",
        )
        left_clear = BUS_INSET + max((s.left_tags for s in shapes.values()), default=0.0)
        cx = x + w + max(MIN_TRUNK, _label_width(text)) + left_clear
        _, bottom = self._place_branch(branch, drv, cx, cx - left_clear, top, shapes)
        return max(bottom, y + TAG_H / 2)

    def place_other_row(self, row: _Row, x: float, top: float) -> float:
        bottom = top
        for child in row.extra:
            shape = self._shape(child)
            cx = x + shape.left_tags
            b, _ = self.place_child(child, cx, top)
            bottom = max(bottom, b)
            x = cx + shape.width + shape.right_tags + 40.0
        return bottom

    def _others_on_net(
        self, net: str, node_id: str,
    ) -> list[tuple[str, str, tuple[float, float]]]:
        """(node_id, terminal, position) of placed ports on ``net`` not on ``node_id``."""
        out = []
        for node in self.nodes:
            if node.node_id == node_id:
                continue
            for p in node.ports:
                if p.net == net:
                    out.append((node.node_id, p.terminal, (p.x, p.y)))
        return out

    def resolve_tags(self) -> None:
        """Tag every port that is neither wired nor grounded."""
        for drv in self.pending_ends:
            others = self._others_on_net(drv.net, drv.node_id)
            if others:
                where = ", ".join(f"{n}.{t}" for n, t, _ in others[:4])
                self._add_tag_at_port(
                    drv, drv.net, drv.net, kind="net",
                    tooltip=f"{drv.net} - continues at {where}. Click to jump there.",
                    target=others[0][2],
                )
            else:
                self._add_tag_at_port(
                    drv, drv.net, drv.net, kind="end",
                    tooltip=f"{drv.net} - no sink or regulator is on this rail.",
                )
        for node, port, part in self.pending_tags:
            if part.kind == "origin" and port.role == "REGULATOR" and not is_output_port(
                port.role, port.terminal, port.side,
            ):
                refs = [pt for net, pt in self.ref_points.get(part.node_id, []) if net == port.net]
                if refs:
                    target = refs[0]
                    tip = f"{port.net} - fed from the row above. Click to jump there."
                elif port.net in self.net_home:
                    target = self.net_home[port.net]
                    tip = f"{port.net} - drawn elsewhere. Click to jump there."
                else:
                    target = None
                    tip = f"{port.net} - nothing drives this rail."
                kind = "feed" if target is not None else "undriven"
                tag = self._add_tag_at_port(
                    port, port.net, port.net, kind=kind, tooltip=tip, target=target,
                )
                self.feed_tags.append((tag, part.node_id, port.net))
                continue
            home = self.net_home.get(port.net)
            if home is not None and (abs(home[0] - port.x) > 0.5 or abs(home[1] - port.y) > 0.5):
                tip = f"{port.net} - drawn elsewhere. Click to jump there."
                kind = "net"
            elif home is not None:
                tip = port.net
                kind = "net"
                home = None
            else:
                others = self._others_on_net(port.net, port.node_id)
                if others:
                    home = others[0][2]
                    where = ", ".join(f"{n}.{t}" for n, t, _ in others[:4])
                    tip = (
                        f"{port.net} - nothing drives this rail; also on {where}."
                        " Click to jump there."
                    )
                else:
                    tip = f"{port.net} - nothing drives this rail."
                kind = "undriven" if part.kind != "origin" else "net"
            self._add_tag_at_port(
                port, port.net, port.net, kind=kind, tooltip=tip, target=home,
            )
        # "→ U6" refs jump to the regulator's node.
        for tag in self.tags:
            if tag.kind != "ref":
                continue
            pos = self.node_pos.get(tag.target_node)
            if pos is not None:
                tag.target = (pos[0] + NODE_W / 2, pos[1] + HEADER_H / 2)
            tag.tooltip = (
                f"{tag.net and self._net_label(tag.net) or ''} feeds regulator "
                f"{tag.text}. Click to jump to its row."
            ).strip()

    def _net_label(self, net: str) -> str:
        for part, port in self.builder.by_net.get(net, []):
            return port.label
        return net


def _node_tooltip(part: _Part) -> str:
    tip = part.spec.get("tooltip") or ""
    d = part.spec.get("directive") or {}
    shorted = d.get("shorted_nets")
    if part.auto_bridge and shorted:
        extra = f"0 Ω link shorting {shorted}"
        tip = f"{tip}\n{extra}" if tip else extra
    return tip


def _band_text(row: _Row) -> tuple[str, str]:
    if row.kind in ("source", "regulator") and row.root is not None:
        part = row.root.part
        value = _value_text(part)
        outs = ", ".join(truncate_label(p.net) for p in part.output_ports())
        role = "Source" if row.kind == "source" else "Regulator"
        title = f"{role} {part.spec['label']}"
        bits = [value, outs]
        if row.kind == "regulator":
            ins = sorted({
                truncate_label(p.net) for p in part.power_ports
                if p not in part.output_ports() and p.role == "REGULATOR"
            })
            bits.insert(0, ", ".join(ins))
        return title, "  →  ".join(b for b in bits if b)
    if row.kind == "undriven" and row.root_branch is not None:
        return (
            f"Undriven rail {truncate_label(row.root_branch.label)}",
            "no source or regulator feeds this rail",
        )
    return "Unconnected", "parts with no rail to hang off"


def _place_hidden_bridges(
    placer: _Placer, hidden: list[_Part], x: float, top: float, wrap_w: float,
) -> tuple[float, list[TopologyBand]]:
    """Auto-bridges on nets with no PDN role, grouped by net.

    Each group is a net name over a run of chips; groups flow left to right
    and wrap at ``wrap_w`` like words, so a dozen one-bridge nets take a
    couple of lines rather than a dozen.
    """
    by_net: dict[str, list[_Part]] = defaultdict(list)
    for part in hidden:
        nets = {p.net for p in part.power_ports} or {"GND"}
        by_net[", ".join(sorted(nets))].append(part)
    bands: list[TopologyBand] = []
    per_line = max(1, int((wrap_w + CHIP_GAP) // (CHIP_W + CHIP_GAP)))
    group_gap = 2 * CHIP_GAP
    line_top = top
    cx = x
    line_h = 0.0
    for net in sorted(by_net, key=natural_sort_key):
        parts = sorted(by_net[net], key=lambda p: natural_sort_key(p.node_id))
        n_cols = min(len(parts), per_line)
        n_lines = -(-len(parts) // per_line)
        title = truncate_label(net)
        g_w = max(
            n_cols * (CHIP_W + CHIP_GAP) - CHIP_GAP,
            estimate_text_width(title, 9) + 4,
        )
        g_h = 18.0 + n_lines * (CHIP_H + CHIP_GAP) - CHIP_GAP
        if cx > x and cx + g_w > x + wrap_w:
            line_top += line_h + 14.0
            cx = x
            line_h = 0.0
        bands.append(TopologyBand(
            title=title, subtitle="", x=cx, y=line_top, width=0.0, kind="bridge_group",
        ))
        for i, part in enumerate(parts):
            px = cx + (i % per_line) * (CHIP_W + CHIP_GAP)
            py = line_top + 18.0 + (i // per_line) * (CHIP_H + CHIP_GAP)
            child = _Child(kind="chip", part=part)
            shape = _Shape(CHIP_W, CHIP_H, CHIP_H, {}, {})
            placer.nodes.append(placer._make_node(child, shape, px, py))
            placer._grow_x(px + CHIP_W)
        cx += g_w + group_gap
        line_h = max(line_h, g_h)
    return line_top + line_h + 14.0, bands


def build_rows_model(
    metadata: TopologyMetadata | None,
    *,
    show_bridges: bool = False,
    add_candidates: Mapping[str, Collection[str]] | None = None,
) -> TopologyModel:
    """Build the rows-layout topology model (see module docstring).

    ``add_candidates`` (net -> designators of parts that could be a load on
    it) turns on the "+ SINK (n)" handle under each rail's last load: a
    dashed tap and tag, drawn only where some candidate isn't already on
    that rail. ``None`` (the default, e.g. the design report) draws none.
    """
    if metadata is None:
        return TopologyModel(layout="rows")
    parsed = parse_topology_directives(metadata)
    parts = _parts_from_specs(parsed.node_specs)
    rows, hidden, builder = _plan_rows(parts)
    placer = _Placer(builder, add_candidates)

    bands: list[TopologyBand] = []
    y = MARGIN
    for row in rows:
        x = MARGIN + row.depth * DEPTH_INDENT
        title, subtitle = _band_text(row)
        bands.append(TopologyBand(
            title=title, subtitle=subtitle, x=x, y=y, width=0.0,
            kind=row.kind, depth=row.depth,
        ))
        top = y + BAND_H
        if row.kind in ("source", "regulator"):
            bottom = placer.place_origin_row(row, x, top)
        elif row.kind == "undriven":
            bottom = placer.place_undriven_row(row, x, top)
        else:
            bottom = placer.place_other_row(row, x, top)
        y = bottom + ROW_GAP
    placer.resolve_tags()

    notes: list[str] = []
    if hidden:
        if show_bridges:
            bands.append(TopologyBand(
                title="Auto-bridges off the power tree",
                subtitle=(
                    f"{len(hidden)} zero-ohm link(s) on nets no source, sink or "
                    "regulator uses"
                ),
                x=MARGIN, y=y, width=0.0, kind="bridges",
            ))
            wrap = max(BRIDGE_WRAP_W, placer.max_x - MARGIN)
            y, groups = _place_hidden_bridges(placer, hidden, MARGIN, y + BAND_H, wrap)
            bands.extend(groups)
            y += ROW_GAP - 14.0
        else:
            notes.append(
                f"{len(hidden)} auto-bridge(s) on nets with no source, sink or "
                "regulator are hidden - use “Bridges” to show them."
            )

    from fypa.topology.render import rows_legend_width

    width = max(placer.max_x + MARGIN, rows_legend_width() + 24.0)
    for band in bands:
        band.width = width - band.x - MARGIN
    height = y + (20.0 * len(notes)) + 52.0
    return TopologyModel(
        nodes=placer.nodes,
        wires=placer.wires,
        width=width,
        height=height,
        layout="rows",
        tags=placer.tags,
        bands=bands,
        gnd_glyphs=placer.gnd_glyphs,
        notes=notes,
    )
