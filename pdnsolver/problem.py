
import shapely

from dataclasses import dataclass, field


# This file should specify the data structure that gets passed to the mesher
# and later to the FEM solver.


@dataclass(frozen=True)
class Layer:
    """
    Represents a single copper layer of the input circuit board.

    The individual polygons are cached in the .geoms field to avoid expensive
    Shapely copying when accessing them repeatedly using shape.geoms[...].
    """
    shape: shapely.geometry.MultiPolygon
    name: str

    # This is in Siemens
    # Note that this is computed by
    # conductivity [S/mm] * thickness [mm]
    conductance: float

    # Cached tuple of individual polygons, extracted from shape
    geoms: tuple[shapely.geometry.Polygon, ...] = field(init=False, repr=False)

    def __post_init__(self):
        # Extract individual polygons from MultiPolygon and cache them
        # This avoids expensive Shapely copying on repeated .geoms access
        object.__setattr__(self, 'geoms', tuple(self.shape.geoms))


@dataclass(frozen=True, eq=False)
class NodeID:
    """
    Opaque identifier for a node in the network.
    """


@dataclass(frozen=True)
class Connection:
    """
    Represents a connection between an internal node of the Network and
    the copper of a layer.

    ``point`` is the nominal attach location (a component pad centre).

    When ``region`` is given, the connection is an *equipotential patch*
    rather than a single point: every mesh vertex that falls under
    ``region`` (the pad outline) is tied to ``node_id`` at one common
    potential, so the element's terminal current crosses the pad boundary
    distributed by the surrounding copper instead of through a single
    vertex. ``point`` is still used to seed the mesh and as the fallback
    attach vertex when ``region`` catches no mesh vertex.

    ``barrel`` marks a via / plated-hole hop whose ``region`` is the drill
    circle: the plated wall ties everything inside it to one potential, so
    current spreads into the plane from the drill radius rather than from a
    point (a point contact over-states via-to-plane spreading resistance by
    ~80% and diverges under mesh refinement). Barrel patches are claimed
    *after* every pad patch, so a via inside a terminal pad joins that pad's
    group instead of splitting it.
    """
    layer: Layer
    point: shapely.geometry.Point
    node_id: NodeID = field(default_factory=NodeID)
    region: shapely.geometry.Polygon | None = None
    barrel: bool = False


# eq=False on every lumped element: they are always handled as instances and
# keyed by identity (e.g. NodeIndexer.extra_source_to_global_index), never
# compared or hashed by value. The default eq=True would make two identical
# parallel elements (e.g. two VRM phases with the same voltage across the same
# nodes) collide as one dict key — under-sizing the MNA system by a variable
# and doubling that element's constraint row. eq=False restores identity
# semantics (and identity hash) so duplicates stay distinct.
@dataclass(frozen=True, eq=False)
class BaseLumped:
    """
    Represents a lumped element in the network.
    """

    def __post_init__(self):
        assert self.terminals, "Lumped elements must have terminals"

    @property
    def terminals(self) -> list[NodeID]:
        raise NotImplementedError

    @property
    def is_source(self) -> bool:
        return False

    @property
    def extra_variable_count(self) -> int:
        return 0


@dataclass(frozen=True)
class Network:
    connections: list[Connection]
    elements: list[BaseLumped]
    nodes: dict[NodeID, int] = field(init=False)
    has_source: bool = field(init=False)

    def __post_init__(self):
        # Initialize the nodes
        node_set = set()
        for element in self.elements:
            for terminal in element.terminals:
                if not isinstance(terminal, NodeID):
                    raise TypeError("Terminal must be a NodeID")
                node_set.add(terminal)

        # Do not allow floating elements
        for connection in self.connections:
            if connection.node_id not in node_set:
                raise ValueError("Connection must be connected to at least one element")

        keys = list(node_set)
        nodes = {key: i for i, key in enumerate(keys)}
        # This bypasses the frozen dataclass restriction
        object.__setattr__(self, "nodes", nodes)
        # Check if the network has a source
        has_source = any(element.is_source for element in self.elements)
        object.__setattr__(self, "has_source", has_source)


@dataclass(frozen=True, eq=False)
class Resistor(BaseLumped):
    a: NodeID
    b: NodeID
    resistance: float
    # True for a resistor that is a real dissipating part — a via barrel
    # hop, a series part (shunt, ferrite, fuse) — whose I²R the board
    # thermal model heats the board with at its pads. False (the default)
    # for modelling artefacts such as a multi-pin terminal's star-coupling
    # resistors, whose loss is fictitious.
    heat_source: bool = False

    def __post_init__(self):
        super().__post_init__()
        if self.resistance <= 0:
            raise ValueError(f"Resistance must be positive, got {self.resistance}")

    @property
    def terminals(self) -> list[NodeID]:
        return [self.a, self.b]


@dataclass(frozen=True, eq=False)
class VoltageSource(BaseLumped):
    """Ideal source: ``V(p) - V(n) = voltage``, its current delivered at p/n.

    With remote sense (``sense_p`` and/or ``sense_n`` set) the source holds
    ``V(sense_p) - V(sense_n) = voltage`` instead, still delivering its
    current at p/n — so the output pins rise by whatever the copper drops
    between them and the sense point, as a regulator's feedback loop does.
    A sense node carries no current; an unset one falls back to p / n
    (local sense). The constraint row then differs from the current
    column, so the system is no longer symmetric."""
    p: NodeID
    n: NodeID
    voltage: float
    sense_p: NodeID | None = None
    sense_n: NodeID | None = None

    @property
    def has_remote_sense(self) -> bool:
        return self.sense_p is not None or self.sense_n is not None

    @property
    def terminals(self) -> list[NodeID]:
        nodes = [self.p, self.n]
        if self.sense_p is not None:
            nodes.append(self.sense_p)
        if self.sense_n is not None:
            nodes.append(self.sense_n)
        return nodes

    @property
    def is_source(self) -> bool:
        return True

    @property
    def extra_variable_count(self) -> int:
        return 1


@dataclass(frozen=True, eq=False)
class CurrentSource(BaseLumped):
    f: NodeID
    t: NodeID
    current: float

    @property
    def terminals(self) -> list[NodeID]:
        return [self.f, self.t]

    @property
    def is_source(self) -> bool:
        return True


@dataclass(frozen=True, eq=False)
class VoltageRegulator(BaseLumped):
    v_p: NodeID
    v_n: NodeID
    s_f: NodeID
    s_t: NodeID

    voltage: float
    gain: float
    # Current drawn at ``s_f`` (and returned at ``s_t``) as a multiple of
    # ``i_v``. ``None`` means "use :attr:`gain`", which is the internal-FET
    # case: the element spans IN↔OUT and converts, so the input draws
    # ``gain * i_v``. An external-FET stage instead cuts at the part that
    # carries the full output current, so the element is a plain conductor
    # there and ``input_gain`` is 1.0 — the switching ratio is carried by
    # :attr:`switch_legs` instead.
    input_gain: float | None = None
    # Averaged switch legs for external-FET stages (LS FETs): each entry is
    # ``(f, t, coeff)``, a current of ``coeff * i_v`` flowing from *f* to *t*.
    # *f* loses that current and *t* receives it, so each leg is balanced on
    # its own and injects nothing into the rest of the network.
    switch_legs: tuple[tuple[NodeID, NodeID, float], ...] = ()
    # Remote sense of the output (see VoltageSource): the regulator holds
    # V(sense_p) - V(sense_n) = voltage while delivering i_v at v_p / v_n.
    sense_p: NodeID | None = None
    sense_n: NodeID | None = None

    @property
    def has_remote_sense(self) -> bool:
        return self.sense_p is not None or self.sense_n is not None

    @property
    def terminals(self) -> list[NodeID]:
        nodes = [self.v_p, self.v_n, self.s_f, self.s_t]
        for f, t, _coeff in self.switch_legs:
            nodes.extend((f, t))
        if self.sense_p is not None:
            nodes.append(self.sense_p)
        if self.sense_n is not None:
            nodes.append(self.sense_n)
        return nodes

    @property
    def is_source(self) -> bool:
        return True

    @property
    def extra_variable_count(self) -> int:
        return 1


@dataclass(frozen=True)
class Problem:
    layers: list[Layer]
    networks: list[Network]
    project_name: str | None = None
    # Loads whose supply voltage the solver differentiates with respect to the
    # copper (see pdnsolver.sensitivity): ``(key, CurrentSource)`` pairs, the
    # key being the caller's label for the load ("U5", "U7#1"). For each one
    # the solve also returns the adjoint field that ranks where extra copper
    # would raise V(f) - V(t) the most. Empty by default: no extra work.
    sensitivity_targets: tuple[tuple[str, BaseLumped], ...] = ()
