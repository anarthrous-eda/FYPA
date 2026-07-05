"""Triangular mesher for pdnsolver, ported from padne's CGAL-based mesher.

Upstream (padne) wraps CGAL's Constrained Delaunay + quality refiner in a C++
extension (``padne._cgal``). This module replaces that wrapper with the
:mod:`triangle` PyPI package (Shewchuk's Triangle, Windows wheels available).

Behavioural differences from upstream
-------------------------------------
* **Variable-density meshing is not implemented.** The
  ``variable_density_*`` and ``variable_size_maximum_factor`` config fields are
  accepted for API compatibility but ignored — meshes are uniform-density,
  sized by ``maximum_size``. Re-introducing variable density on top of Triangle
  is possible (pre-seed extra Steiner points near boundaries, or use the
  ``triunsuitable`` C callback) but deferred until profiling shows it matters.
* ``MeshingException`` is still raised on geometry that Triangle cannot mesh
  (self-intersections, duplicate vertices, etc.).
"""

import functools
import logging
import math

import numpy as np
import scipy.spatial
import shapely
import shapely.geometry
import shapely.strtree
import triangle as _triangle

from dataclasses import dataclass, field
from typing import Optional, Protocol, TypeVar, Generic
from collections.abc import Iterator


log = logging.getLogger(__name__)


# The purpose of this module is to generate triangular meshes from Shapely
# (multi)polygons

index_type = np.uint64


class HasIndex(Protocol):
    i: index_type


@dataclass(frozen=True)
class Vector:
    dx: float
    dy: float

    def dot(self, other: "Vector") -> float:
        return self.dx * other.dx + self.dy * other.dy

    def __add__(self, other: "Vector") -> "Vector":
        if not isinstance(other, Vector):
            raise TypeError("Addition is only defined for Vectors")
        return Vector(self.dx + other.dx, self.dy + other.dy)

    def __mul__(self, scalar: float) -> "Vector":
        return Vector(self.dx * scalar, self.dy * scalar)

    def __rmul__(self, scalar: float) -> "Vector":
        return self.__mul__(scalar)

    def __neg__(self) -> "Vector":
        return Vector(-self.dx, -self.dy)

    def __xor__(self, other: "Vector") -> float:
        # Should there be a special 2-vector type?
        return self.dx * other.dy - self.dy * other.dx

    def __abs__(self) -> float:
        return np.sqrt(self.dx ** 2 + self.dy ** 2)


@dataclass(frozen=True)
class Point:
    x: float
    y: float

    def distance(self, other: "Point") -> float:
        return np.sqrt((self.x - other.x) ** 2 + (self.y - other.y) ** 2)

    def __sub__(self, other: "Point") -> Vector:
        if not isinstance(other, Point):
            raise TypeError("Subtraction is only defined for Points")
        return Vector(self.x - other.x, self.y - other.y)

    def to_shapely(self) -> shapely.geometry.Point:
        """
        Convert this Point to a shapely.geometry.Point.

        Returns:
            A shapely Point with the same coordinates
        """
        return shapely.geometry.Point(self.x, self.y)


@dataclass(eq=False, repr=False)
class Vertex:
    p: Point
    out: Optional["HalfEdge"] = None
    i: index_type = field(default=index_type(0))

    def orbit(self) -> Iterator["HalfEdge"]:
        edge = self.out
        while True:
            yield edge
            edge = edge.twin.next
            if edge == self.out:
                break


@dataclass(eq=False, repr=False)
class HalfEdge:
    origin: Vertex
    twin: Optional["HalfEdge"] = None
    next: Optional["HalfEdge"] = None
    prev: Optional["HalfEdge"] = None
    face: Optional["Face"] = None
    i: index_type = field(default=index_type(0))

    def __getstate__(self):
        # We _do not_ pickle the twin/next/prev halfedges explicitly
        # to avoid reaching recursion depth limits
        # The Mesh class performs additional bookkeeping and rehydration
        # to ensure that the topology is properly unpickled.
        state = self.__dict__.copy()
        censor_keys = ["next", "prev", "twin"]
        for key in censor_keys:
            state[key] = id(state[key])
        return state

    @property
    def is_boundary(self) -> bool:
        return self.face.is_boundary

    @staticmethod
    def connect(e1: "HalfEdge", e2: "HalfEdge") -> None:
        e1.next = e2
        e2.prev = e1

    def walk(self) -> Iterator["HalfEdge"]:
        edge = self
        while True:
            yield edge
            edge = edge.next
            if edge == self:
                break

    def cotan(self) -> float:
        """
        Compute the cotangent weight for this half-edge.
        """
        vertex_i = self.origin
        # Grab the vertex on the other side of the edge
        vertex_k = self.twin.origin
        ratio = 0.
        for other in [self.next.next, self.twin.next.next]:
            if other.next.face.is_boundary:
                # Do not include boundary edges
                continue
            vi = vertex_i.p - other.origin.p
            vk = vertex_k.p - other.origin.p
            ratio += abs(vi.dot(vk) / (vi ^ vk)) / 2
        return ratio


@dataclass(eq=False)
class Face:
    edge: HalfEdge = None
    is_boundary: bool = False
    i: index_type = field(default=index_type(0))

    @property
    def edges(self):
        edge = self.edge
        while True:
            yield edge
            edge = edge.next
            if edge == self.edge:
                break

    @property
    def vertices(self):
        for edge in self.edges:
            yield edge.origin

    @property
    def centroid(self) -> Point:
        """
        Compute the centroid of the face using the average of vertex coordinates.
        """
        x_sum = 0.0
        y_sum = 0.0
        count = 0
        for vertex in self.vertices:
            x_sum += vertex.p.x
            y_sum += vertex.p.y
            count += 1
        return Point(x_sum / count, y_sum / count)

    @property
    def area(self) -> float:
        """
        Compute the area using the shoelace formula.
        Returns the absolute value to ensure positive area regardless of vertex order.
        """
        area = 0.0
        for edge in self.edges:
            p1 = edge.origin.p
            p2 = edge.next.origin.p
            area += (p1.x * p2.y - p2.x * p1.y)
        return 0.5 * abs(area)


_IS_T = TypeVar("_IS_T", bound=HasIndex)


class IndexStore(Generic[_IS_T]):
    """
    A simple class that stores objects with indices.

    Supports a *lazy* mode (see :meth:`init_lazy`): the store is pre-sized to
    a known element count but the per-element objects are constructed only on
    first access. Mesh vertex/face stubs are expensive to materialise in bulk
    — millions of tiny Python objects on a large board — yet the solver
    pipeline only ever needs the element *count* (it reads geometry from the
    flat triangle-soup arrays). Lazy mode skips that cost entirely for the
    solve path; consumers that genuinely walk the stubs (ParaView export,
    etc.) materialise them on demand.
    """

    def __init__(self):
        self._idx_to_obj: list[_IS_T] = []
        # When set, slots holding ``None`` are built on access via
        # ``_builder(i)``. Must stay picklable (a module-level function or a
        # functools.partial of one) so a Mesh can be pickled into the cache.
        self._builder = None

    def init_lazy(self, count: int, builder) -> None:
        """Pre-size the store to ``count`` slots, each built on first access
        by ``builder(i)``. See the class docstring for the rationale."""
        self._idx_to_obj = [None] * count
        self._builder = builder

    @property
    def next_index(self) -> index_type:
        """Get the next available index without adding an object."""
        return index_type(len(self._idx_to_obj))

    def add(self, obj: _IS_T) -> None:
        obj.i = self.next_index
        self._idx_to_obj.append(obj)

    def to_index(self, obj: _IS_T) -> index_type:
        """Get the index of an object. This is for backwards compatibility, otherwise use obj.i directly."""
        return obj.i

    def to_object(self, idx: int | index_type) -> _IS_T:
        """Get the object at a given index, building it if the store is lazy
        and this slot hasn't been materialised yet."""
        i = int(idx)
        obj = self._idx_to_obj[i]
        if obj is None and self._builder is not None:
            obj = self._builder(i)
            self._idx_to_obj[i] = obj
        return obj

    def __len__(self) -> int:
        return len(self._idx_to_obj)

    def __iter__(self) -> Iterator[_IS_T]:
        if self._builder is None:
            return iter(self._idx_to_obj)
        return (self.to_object(i) for i in range(len(self._idx_to_obj)))

    def __contains__(self, obj: _IS_T) -> bool:
        """Check if an object is in the index store."""
        return 0 <= obj.i < len(self._idx_to_obj) and self._idx_to_obj[obj.i] is obj

    def items(self) -> Iterator[tuple[index_type, _IS_T]]:
        for idx in range(len(self._idx_to_obj)):
            yield index_type(idx), self.to_object(idx)


def _build_vertex_stub(xys: np.ndarray, i: int) -> "Vertex":
    """Construct vertex stub ``i`` for a lazily-populated mesh store (see
    :meth:`IndexStore.init_lazy`). Module-level so a ``functools.partial``
    binding it stays picklable when a Mesh is written to the solve cache."""
    v = Vertex(Point(float(xys[i, 0]), float(xys[i, 1])))
    v.i = index_type(i)
    return v


def _build_face_stub(i: int) -> "Face":
    """Construct face stub ``i`` for a lazily-populated mesh store."""
    f = Face()
    f.i = index_type(i)
    return f


class Mesh:
    def __init__(self):
        self.vertices = IndexStore[Vertex]()
        self.halfedges = IndexStore[HalfEdge]()
        self.faces = IndexStore[Face]()
        self.boundaries = IndexStore[Face]()
        self._edge_map: dict[tuple[int, int], HalfEdge] = {}
        # Source triangle-soup arrays, populated by from_triangle_soup. Default
        # to empty so a hand-built Mesh still has these attributes. The
        # vectorised laplace_operator in solver.py reads these directly; an
        # older mesh that lacks them falls back to a half-edge walk.
        self._source_xys: np.ndarray = np.empty((0, 2), dtype=np.float64)
        self._source_tris: np.ndarray = np.empty((0, 3), dtype=np.int64)
        # Bool-per-vertex mask: True iff the vertex appears in any triangle.
        # Replaces the ``vertex.out is None`` orphan test the solver used to
        # do — that test only worked because the half-edge build set
        # ``vertex.out`` for every vertex incident to a triangle, but
        # from_triangle_soup no longer builds half-edges by default.
        self._in_triangle_mask: np.ndarray = np.empty(0, dtype=bool)

    def __getstate__(self):
        state = self.__dict__.copy()
        # Will be important for rehydrating the mesh
        ids_to_hedges = {
            id(hedge): hedge for hedge in state["halfedges"]
        }
        state["_ids_to_hedges"] = ids_to_hedges
        return state

    def __setstate__(self, state):
        _ids_to_hedges = state.pop("_ids_to_hedges")
        # Rehydrate the halfedges
        for hedge in state["halfedges"]:
            # This should be set to id(...) in the __getstate__ method
            # of HalfEdge
            assert isinstance(hedge.next, int) and isinstance(hedge.prev, int) \
                and isinstance(hedge.twin, int), "HalfEdge state is not properly serialized"
            hedge.next = _ids_to_hedges[hedge.next]
            hedge.prev = _ids_to_hedges[hedge.prev]
            hedge.twin = _ids_to_hedges[hedge.twin]

        self.__dict__.update(state)

    def make_vertex(self, p: Point) -> Vertex:
        v = Vertex(p)
        self.vertices.add(v)
        return v

    def connect_vertices(self, v1: Vertex, v2: Vertex) -> HalfEdge:
        """
        Return a half-edge between the two vertices v1 and v2. If the half
        edge does not exist, create it. The twin half-edge is also created.
        """
        key12 = (self.vertices.to_index(v1), self.vertices.to_index(v2))
        key21 = (key12[1], key12[0])
        if key12 in self._edge_map:
            assert key21 in self._edge_map, "Inconsistent half edge state"
            return self._edge_map[key12]

        # Assert that the twin also does not exist. It should not be possible
        # to have one without the other
        assert key21 not in self._edge_map, "Inconsistent half edge state"

        e12 = HalfEdge(v1)
        self.halfedges.add(e12)
        e21 = HalfEdge(v2)
        self.halfedges.add(e21)
        e12.twin = e21
        e21.twin = e12

        self._edge_map[key12] = e12
        self._edge_map[key21] = e21

        # Update the vertex out pointers
        if v1.out is None:
            v1.out = e12
        if v2.out is None:
            v2.out = e21

        return e12

    def euler_characteristic(self) -> int:
        return len(self.vertices) - len(self.halfedges) // 2 + len(self.faces)

    @classmethod
    def from_triangle_arrays(cls,
                             xys: np.ndarray,
                             tris: np.ndarray,
                             build_halfedges: bool = False) -> "Mesh":
        """Build a Mesh straight from numpy triangle-soup arrays.

        Same semantics as :meth:`from_triangle_soup` but takes the raw
        ``(N, 2) float64`` vertex array and ``(M, 3) int64`` triangle
        array directly, skipping the Python-side ``list[Point]`` /
        ``list[tuple[int, int, int]]`` round trip Triangle's output
        would otherwise pay. Called by ``Mesher.poly_to_mesh`` where the
        arrays come straight out of the triangle library.
        """
        mesh = cls()
        xys = np.ascontiguousarray(xys, dtype=np.float64).reshape(-1, 2)
        tris = np.ascontiguousarray(tris, dtype=np.int64).reshape(-1, 3)
        mesh._source_xys = xys
        mesh._source_tris = tris

        n_v = xys.shape[0]
        mask = np.zeros(n_v, dtype=bool)
        if tris.shape[0] > 0:
            mask[tris.ravel()] = True
        mesh._in_triangle_mask = mask

        # Vertex/Face stubs are populated lazily — the solver pipeline only
        # needs the element counts (it reads geometry from the flat
        # _source_xys / _source_tris arrays), so materialising millions of
        # tiny Python objects up front is pure waste. Consumers that do walk
        # the stubs (ParaView export, …) build them on demand.
        mesh.vertices.init_lazy(n_v, functools.partial(_build_vertex_stub, xys))
        mesh.faces.init_lazy(tris.shape[0], _build_face_stub)

        if build_halfedges:
            mesh._build_halfedges()

        return mesh

    @classmethod
    def from_triangle_soup(cls,
                           points: list[Point],
                           triangles: list[tuple[int, int, int]],
                           build_halfedges: bool = False) -> "Mesh":
        """Build a Mesh from raw Triangle output.

        Stores the flat triangle-soup arrays (``_source_xys`` +
        ``_source_tris``) and lightweight ``Vertex`` / ``Face`` stubs
        (one per input point / triangle, with the right ``.i`` indices
        so ``ZeroForm`` / ``TwoForm`` allocate correctly). The full
        half-edge graph (``halfedges`` + ``boundaries``, plus
        ``vertex.out`` / ``face.edge`` cross-references) is NOT built by
        default — every solver hot path now reads the flat arrays
        directly, which is dramatically faster and uses much less memory.

        Pass ``build_halfedges=True`` to opt into the legacy half-edge
        construction (e.g. for the ParaView export, which walks faces
        and edges in Python). The downstream solver pipeline doesn't
        need it.
        """
        mesh = cls()

        # Stash the flat numpy form of the input first — downstream code
        # (vectorised laplace_operator, compute_power_density,
        # lean_solution conversion) reads these directly. Vertex order
        # matches ``mesh.vertices`` exactly; triangle order matches
        # ``mesh.faces`` (face.i is the triangle row).
        if points:
            mesh._source_xys = np.asarray(
                [(p.x, p.y) for p in points], dtype=np.float64,
            )
        else:
            mesh._source_xys = np.empty((0, 2), dtype=np.float64)
        if triangles:
            mesh._source_tris = np.asarray(triangles, dtype=np.int64)
        else:
            mesh._source_tris = np.empty((0, 3), dtype=np.int64)

        # Membership mask — True iff the vertex appears in any triangle.
        # Replaces the old ``vertex.out is None`` orphan check.
        n_v = len(points)
        mask = np.zeros(n_v, dtype=bool)
        if mesh._source_tris.shape[0] > 0:
            mask[mesh._source_tris.ravel()] = True
        mesh._in_triangle_mask = mask

        # Lightweight stubs — one Vertex per input point, one Face per
        # triangle. We populate the IndexStores directly (bypassing
        # ``make_vertex`` / ``faces.add``) so the per-element overhead
        # is one Python object allocation + one .i set, not a function
        # call. Crucially, vertex.out stays None for every vertex (no
        # half-edges to point at) — the solver consults
        # ``_in_triangle_mask`` instead.
        v_list = mesh.vertices._idx_to_obj
        for i, p in enumerate(points):
            v = Vertex(p)
            v.i = index_type(i)
            v_list.append(v)

        f_list = mesh.faces._idx_to_obj
        for i in range(mesh._source_tris.shape[0]):
            f = Face()
            f.i = index_type(i)
            f_list.append(f)

        if build_halfedges:
            mesh._build_halfedges()

        return mesh

    def _build_halfedges(self) -> None:
        """Construct the full half-edge graph from the flat triangle soup.

        Idempotent — does nothing if half-edges already exist. The solver
        no longer needs this (every hot path reads ``_source_xys`` +
        ``_source_tris`` directly), so it's only useful for tools that
        genuinely require the half-edge connectivity such as the
        ParaView exporter.

        Builds the interior half-edges by walking ``_source_tris``, then
        threads the boundary half-edges into per-loop boundary faces.
        After this call, ``self.halfedges``, ``self.boundaries``, every
        ``Face.edge``, every ``HalfEdge.next/prev/twin/face`` and every
        ``Vertex.out`` are populated.
        """
        if len(self.halfedges) > 0:
            return  # already built
        if self._source_tris.shape[0] == 0:
            return

        vertices = list(self.vertices)
        faces = list(self.faces)
        triangles = [tuple(int(i) for i in t) for t in self._source_tris]
        # Reset face stubs so connect_vertices can attach them as it goes —
        # they're already in self.faces with the right .i, we just need
        # the edge pointers set.
        for face_idx, tri in enumerate(triangles):
            assert len(tri) == 3
            v1, v2, v3 = vertices[tri[0]], vertices[tri[1]], vertices[tri[2]]
            face = faces[face_idx]

            vertex_edge_pairs = [(v1, v2), (v2, v3), (v3, v1)]
            current_hedges = []
            for u, v in vertex_edge_pairs:
                hedge = self.connect_vertices(u, v)
                u.out = hedge
                face.edge = hedge
                hedge.face = face
                current_hedges.append(hedge)
            for h1, h2 in zip(current_hedges,
                              current_hedges[1:] + [current_hedges[0]]):
                HalfEdge.connect(h1, h2)

        # Boundary face construction — identical to the upstream
        # construction logic, just lifted out so it doesn't run in the
        # default path.
        boundary_hedges = set()
        vertex_to_boundary_hedge = {}
        for hedge in self.halfedges:
            if hedge.face is not None:
                continue
            boundary_hedges.add(hedge)
            if hedge.origin in vertex_to_boundary_hedge:
                raise ValueError("Non-manifold mesh")
            vertex_to_boundary_hedge[hedge.origin] = hedge

        boundary_hedges = {h for h in self.halfedges if h.face is None}
        while boundary_hedges:
            hedge = boundary_hedges.pop()
            face = Face(is_boundary=True)
            self.boundaries.add(face)
            face.edge = hedge
            hedge.face = face

            hedge_prev = hedge
            while True:
                vertex_next = hedge_prev.twin.origin
                hedge_next = vertex_to_boundary_hedge.get(vertex_next)
                if hedge_next not in boundary_hedges:
                    break
                boundary_hedges.remove(hedge_next)
                assert hedge_next.next is None
                HalfEdge.connect(hedge_prev, hedge_next)
                hedge_next.face = face
                hedge_prev = hedge_next
            HalfEdge.connect(hedge_prev, hedge)


@dataclass
class ZeroForm:
    mesh: Mesh
    values: np.ndarray = field(init=False, repr=False)

    def __post_init__(self):
        self.values = np.zeros(len(self.mesh.vertices), dtype=np.float64)

    def __getitem__(self, vertex: Vertex) -> float:
        if vertex not in self.mesh.vertices:
            raise KeyError("Vertex not in mesh")
        return float(self.values[vertex.i])

    def __setitem__(self, vertex: Vertex, value: float) -> None:
        if vertex not in self.mesh.vertices:
            raise KeyError("Vertex not in mesh")
        self.values[vertex.i] = value

    def __add__(self, other: "ZeroForm") -> "ZeroForm":
        """Add two ZeroForms element-wise.

        Args:
            other: The ZeroForm to add to this one

        Returns:
            A new ZeroForm with the sum of values

        Raises:
            ValueError: If the two ZeroForms are on different meshes
        """
        if self.mesh is not other.mesh:
            raise ValueError("Cannot add ZeroForms on different meshes")
        result = ZeroForm(self.mesh)
        result.values = self.values + other.values
        return result

    def __sub__(self, other: "ZeroForm") -> "ZeroForm":
        """Subtract another ZeroForm element-wise.

        Args:
            other: The ZeroForm to subtract from this one

        Returns:
            A new ZeroForm with the difference of values

        Raises:
            ValueError: If the two ZeroForms are on different meshes
        """
        if self.mesh is not other.mesh:
            raise ValueError("Cannot subtract ZeroForms on different meshes")
        result = ZeroForm(self.mesh)
        result.values = self.values - other.values
        return result

    def __mul__(self, scalar: float) -> "ZeroForm":
        """Multiply this ZeroForm by a scalar.

        Args:
            scalar: The scalar value to multiply by

        Returns:
            A new ZeroForm with scaled values
        """
        result = ZeroForm(self.mesh)
        result.values = self.values * scalar
        return result

    def __rmul__(self, scalar: float) -> "ZeroForm":
        """Right multiplication by a scalar.

        Args:
            scalar: The scalar value to multiply by

        Returns:
            A new ZeroForm with scaled values
        """
        return self.__mul__(scalar)

    def __truediv__(self, scalar: float) -> "ZeroForm":
        """Divide this ZeroForm by a scalar.

        Args:
            scalar: The scalar value to divide by

        Returns:
            A new ZeroForm with divided values

        Raises:
            ZeroDivisionError: If scalar is zero
        """
        if scalar == 0:
            raise ZeroDivisionError("Cannot divide ZeroForm by zero")
        result = ZeroForm(self.mesh)
        result.values = self.values / scalar
        return result

    def __neg__(self) -> "ZeroForm":
        """Negate all values in this ZeroForm.

        Returns:
            A new ZeroForm with negated values
        """
        result = ZeroForm(self.mesh)
        result.values = -self.values
        return result

    def d(self) -> "OneForm":
        """
        Compute the exterior derivative (gradient) of this 0-form.

        For a function f on vertices, the exterior derivative df is a 1-form where:
        (df)[edge] = f(target_vertex) - f(source_vertex)

        Returns:
            A OneForm representing the gradient of this function
        """
        one_form = OneForm(self.mesh)

        for hedge in self.mesh.halfedges:
            # For edge from A to B: df[edge] = f(B) - f(A)
            assert hedge.twin is not None
            target_value = self.values[hedge.twin.origin.i]
            source_value = self.values[hedge.origin.i]
            one_form.values[hedge.i] = target_value - source_value

        return one_form


@dataclass
class OneForm:
    """
    A discrete 1-form defined on the (h)edges of a mesh.
    """
    mesh: Mesh
    values: np.ndarray = field(init=False, repr=False)

    def __post_init__(self):
        self.values = np.zeros(len(self.mesh.halfedges), dtype=np.float64)

    def __getitem__(self, hedge: HalfEdge) -> float:
        """Get the value of the 1-form on a half-edge."""
        if hedge not in self.mesh.halfedges:
            raise KeyError("HalfEdge not in mesh")
        return float(self.values[hedge.i])

    def __setitem__(self, hedge: HalfEdge, value: float) -> None:
        """Set the value of the 1-form on a half-edge, ensuring antisymmetry."""
        if hedge not in self.mesh.halfedges:
            raise KeyError("HalfEdge not in mesh")
        assert hedge.twin is not None
        self.values[hedge.i] = value
        self.values[hedge.twin.i] = -value

    def __add__(self, other: "OneForm") -> "OneForm":
        """Add two OneForm objects element-wise."""
        if self.mesh is not other.mesh:
            raise ValueError("Cannot add OneForms on different meshes")
        result = OneForm(self.mesh)
        result.values = self.values + other.values
        return result

    def __sub__(self, other: "OneForm") -> "OneForm":
        """Subtract another OneForm element-wise."""
        if self.mesh is not other.mesh:
            raise ValueError("Cannot subtract OneForms on different meshes")
        result = OneForm(self.mesh)
        result.values = self.values - other.values
        return result

    def __mul__(self, scalar: float) -> "OneForm":
        """Multiply this OneForm by a scalar."""
        result = OneForm(self.mesh)
        result.values = self.values * scalar
        return result

    def __rmul__(self, scalar: float) -> "OneForm":
        """Right multiplication by a scalar."""
        return self.__mul__(scalar)

    def __truediv__(self, scalar: float) -> "OneForm":
        """Divide this OneForm by a scalar."""
        if scalar == 0:
            raise ZeroDivisionError("Cannot divide OneForm by zero")
        result = OneForm(self.mesh)
        result.values = self.values / scalar
        return result

    def __neg__(self) -> "OneForm":
        """Negate all values in this OneForm."""
        result = OneForm(self.mesh)
        result.values = -self.values
        return result


@dataclass
class TwoForm:
    """
    A discrete 2-form defined on the faces of a mesh.
    """
    mesh: Mesh
    values: np.ndarray = field(init=False, repr=False)

    def __post_init__(self):
        self.values = np.zeros(len(self.mesh.faces), dtype=np.float64)

    def __getitem__(self, face: Face) -> float:
        """Get the value of the 2-form on a face."""
        if face not in self.mesh.faces and face not in self.mesh.boundaries:
            raise KeyError("Face not in mesh")
        # Boundary faces always return 0.0
        if face in self.mesh.boundaries:
            return 0.0
        return float(self.values[face.i])

    def __setitem__(self, face: Face, value: float) -> None:
        """Set the value of the 2-form on a face."""
        if face not in self.mesh.faces:
            raise KeyError("Face not in mesh.faces (boundary faces not supported)")
        self.values[face.i] = value

    def __add__(self, other: "TwoForm") -> "TwoForm":
        """Add two TwoForm objects element-wise."""
        if self.mesh is not other.mesh:
            raise ValueError("Cannot add TwoForms on different meshes")
        result = TwoForm(self.mesh)
        result.values = self.values + other.values
        return result

    def __sub__(self, other: "TwoForm") -> "TwoForm":
        """Subtract another TwoForm element-wise."""
        if self.mesh is not other.mesh:
            raise ValueError("Cannot subtract TwoForms on different meshes")
        result = TwoForm(self.mesh)
        result.values = self.values - other.values
        return result

    def __mul__(self, scalar: float) -> "TwoForm":
        """Multiply this TwoForm by a scalar."""
        result = TwoForm(self.mesh)
        result.values = self.values * scalar
        return result

    def __rmul__(self, scalar: float) -> "TwoForm":
        """Right multiplication by a scalar."""
        return self.__mul__(scalar)

    def __truediv__(self, scalar: float) -> "TwoForm":
        """Divide this TwoForm by a scalar."""
        if scalar == 0:
            raise ZeroDivisionError("Cannot divide TwoForm by zero")
        result = TwoForm(self.mesh)
        result.values = self.values / scalar
        return result

    def __neg__(self) -> "TwoForm":
        """Negate all values in this TwoForm."""
        result = TwoForm(self.mesh)
        result.values = -self.values
        return result


class MeshingException(RuntimeError):
    """
    Exception raised when CGAL mesh generation fails due to invalid geometry.

    This includes cases such as:
    - Self-intersecting polygons with unauthorized constraint intersections
    - Degenerate edges that are too short (near-duplicate vertices)
    - Other geometric degeneracies that prevent mesh generation

    With CGAL_DEBUG enabled, these issues are detected early through CGAL's
    internal precondition checking, preventing crashes and providing clear
    error messages.
    """


class Mesher:
    """
    This class is responsible for generating a mesh from a Shapely polygon.
    Works through the triangle library.
    """

    @dataclass(frozen=True)
    class Config:
        """Configuration parameters for mesh generation."""
        minimum_angle: float = 20.0
        maximum_size: float = 0.6
        # Variable density parameters
        variable_density_min_distance: float = 0.5
        variable_density_max_distance: float = 3.0
        # 1.0 == uniform meshing (the default). Set > 1 to enable adaptive
        # variable-density meshing — triangles in plane interiors may grow
        # up to this factor larger than the fine size near features.
        variable_size_maximum_factor: float = 1.0
        distance_map_quantization: float = 1.0

        # Static relaxed configuration for disconnected copper triangulation
        RELAXED = None  # Will be initialized after class definition

        @property
        def is_variable_density(self) -> bool:
            """Return True if variable density meshing is enabled."""
            return self.variable_size_maximum_factor != 1.0

        def __post_init__(self):
            """Validate configuration parameters."""
            if not (0 <= self.minimum_angle <= 60):
                raise ValueError(f"minimum_angle must be between 0 and 60 degrees, got {self.minimum_angle}")

            if self.maximum_size < 0:
                raise ValueError(f"maximum_size must be non-negative, got {self.maximum_size}")

            if self.variable_density_min_distance < 0:
                raise ValueError(f"variable_density_min_distance must be non-negative, got {self.variable_density_min_distance}")

            if self.variable_density_max_distance <= self.variable_density_min_distance:
                raise ValueError(f"variable_density_max_distance ({self.variable_density_max_distance}) must be greater than variable_density_min_distance ({self.variable_density_min_distance})")

            if self.variable_size_maximum_factor < 1.0:
                raise ValueError(f"variable_size_maximum_factor must be >= 1.0, got {self.variable_size_maximum_factor}")

            if self.distance_map_quantization <= 0:
                raise ValueError(f"distance_map_quantization must be positive, got {self.distance_map_quantization}")

    def __init__(self, config: Optional['Mesher.Config'] = None):
        self.config = config if config is not None else Mesher.Config()

    def _prepare_polygon_for_triangle(self,
                                      poly: shapely.geometry.Polygon,
                                      seed_points: list[Point] = (),
                                      ) -> tuple[list, list, list]:
        """Convert a Shapely polygon to (vertices, segments, hole_markers).

        Triangle's convention: the polygon's exterior is automatically in
        domain; each hole is identified by one interior point ("hole marker").
        That's the OPPOSITE of CGAL's "seeds mark in-domain" convention used by
        upstream padne, so we walk ``poly.interiors`` and synthesise one
        representative point per hole.

        ``seed_points`` are user-provided locations (typically lumped-element
        attachment points). Triangle preserves all input vertices in its output,
        so we simply append them — no constraint segments needed.
        """
        vertices: list[tuple[float, float]] = []
        segments: list[tuple[int, int]] = []
        holes: list[tuple[float, float]] = []

        def insert_linear_ring(ring: shapely.geometry.LinearRing) -> None:
            assert ring.is_closed
            if not ring.is_ccw:
                ring = shapely.geometry.LinearRing(reversed(ring.coords))
            i_first = len(vertices)
            for p in ring.coords[:-1]:
                vertices.append((float(p[0]), float(p[1])))
            n = len(ring.coords) - 1
            for i in range(n):
                segments.append((i_first + i, i_first + (i + 1) % n))

        insert_linear_ring(poly.exterior)

        for hole_ring in poly.interiors:
            insert_linear_ring(hole_ring)
            # Synthesise a hole marker: representative_point on a Polygon built
            # from this hole's ring guarantees a point strictly inside the hole.
            hole_poly = shapely.geometry.Polygon(hole_ring)
            hp = hole_poly.representative_point()
            holes.append((float(hp.x), float(hp.y)))

        # Append user seed points last; Triangle keeps all input vertices.
        for sp in seed_points:
            vertices.append((float(sp.x), float(sp.y)))

        return vertices, segments, holes

    def _build_triangle_switches(self,
                                  max_size_override: float | None = None) -> str:
        """Build the Triangle switches string from the mesher config.

        ``p`` enables planar straight-line graph input (use ``segments``).
        ``q<angle>`` enforces a minimum angle (quality refinement).
        ``a<area>`` caps triangle area — derived from ``maximum_size`` assuming
        equilateral triangles (area = √3/4 · size²).
        ``Q`` quiets Triangle's stdout noise.

        ``max_size_override`` lets callers pass a per-polygon adaptive value
        (typically from :meth:`polygon_adaptive_max_size`) without mutating
        the shared ``Mesher.Config``. Pass ``None`` (default) to use the
        config value.
        """
        parts = ["p"]
        if self.config.minimum_angle > 0:
            parts.append(f"q{self.config.minimum_angle:g}")
        max_size = (max_size_override if max_size_override is not None
                    else self.config.maximum_size)
        if max_size > 0:
            max_area = (math.sqrt(3.0) / 4.0) * (max_size ** 2)
            parts.append(f"a{max_area:.6g}")
        parts.append("Q")  # quiet
        return "".join(parts)

    @staticmethod
    def polygon_adaptive_max_size(
        polygon: shapely.geometry.Polygon,
        config_max_size: float,
        width_refinement_factor: float = 5.0,
    ) -> float:
        """Per-polygon mesh max-size derived from the polygon's local width.

        Why this exists
        ---------------
        The cotangent-Laplacian FEM systematically *under*-estimates the
        end-to-end resistance of a thin conductor when the mesh has only
        one or two triangles across the conductor's width. Refining the
        global ``maximum_size`` fixes it but over-meshes wide pours that
        don't need it. This helper produces a per-polygon cap so narrow
        nets refine themselves and large pours stay at the global value.

        The width estimator
        -------------------
        ``2 × polygon.area / polygon.length`` ≈ the local width for thin
        shapes (perimeter ≈ 2L for L >> W, area = L·W, so 2·L·W/2·L = W).
        For a 1 mm × 20 mm trace it returns ~0.95 mm; for a 50×50 mm pour
        it returns ~25 mm.

        The width estimate is divided by ``width_refinement_factor`` (5 by
        default — i.e. aim for ~5 triangles across the width) and clamped
        to ``config_max_size`` so the adaptive value never coarsens beyond
        what the user asked for.

        Limitation: composite polygons (e.g. a big pour with a thin spur)
        get the pour's averaged estimator and the spur is still under-
        refined. Fixing that requires local-width sampling (medial axis)
        or per-region max-area attributes — deferred until profile-level
        accuracy on mixed geometry actually matters.
        """
        if polygon.length <= 0.0 or polygon.area <= 0.0:
            return config_max_size  # degenerate; defer to global
        characteristic_width = 2.0 * polygon.area / polygon.length
        width_based = characteristic_width / width_refinement_factor
        if config_max_size <= 0:
            return width_based  # no global cap — just use the width-based size
        return min(config_max_size, width_based)

    def poly_to_mesh(self,
                     poly: shapely.geometry.Polygon,
                     seed_points: list[Point] = ()) -> Mesh:
        """Convert a Shapely polygon (with optional holes) to a triangular mesh.

        Returns a half-edge :class:`Mesh`. Raises :class:`MeshingException` if
        Triangle reports an unmeshable input (self-intersection, degenerate
        edges, etc.).

        In-process variant: delegates to the module-level
        :func:`_triangulate_arrays` (also used by the multiprocess
        worker), so serial and parallel meshing share one Triangle code
        path with identical numerical output.
        """
        seed_xy = None
        if seed_points:
            seed_xy = np.asarray(
                [(float(p.x), float(p.y)) for p in seed_points],
                dtype=np.float64,
            )
        if self.config.is_variable_density:
            adaptive = (self.config.minimum_angle, self.config.maximum_size,
                        self.config.variable_size_maximum_factor,
                        self.config.variable_density_min_distance,
                        self.config.variable_density_max_distance)
            out_vertices, out_triangles = _triangulate_adaptive(
                poly, seed_xy, adaptive,
            )
            return Mesh.from_triangle_arrays(out_vertices, out_triangles)
        vertices, segments, holes = _prepare_polygon_for_triangle_arrays(
            poly, seed_xy,
        )
        switches = self._build_triangle_switches()
        out_vertices, out_triangles = _triangulate_arrays(
            vertices, segments, holes, switches,
        )
        return Mesh.from_triangle_arrays(out_vertices, out_triangles)


Mesher.Config.RELAXED = Mesher.Config(
    minimum_angle=5.0,
    maximum_size=0,
    variable_size_maximum_factor=1.0
)


# --- Parallel mesh helpers ----------------------------------------------------
#
# Module-level (NOT instance-method) so they can be pickled across the
# ProcessPoolExecutor boundary. The solver runs Triangle for each (layer,
# net) copper piece in parallel — each call is independent, no shared
# state, so we feed each polygon into a worker and collect the raw
# (vertices, triangles) numpy arrays back. The Mesh stub assembly
# (Vertex/Face objects) stays in the parent process so we don't pay to
# pickle tens of thousands of tiny Python objects back across.


def _prepare_polygon_for_triangle_arrays(
    poly: shapely.geometry.Polygon,
    seed_xy: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Numpy-array form of :meth:`Mesher._prepare_polygon_for_triangle`.

    Same semantics, but builds the per-ring vertex / segment arrays with
    numpy instead of Python lists + tuple appends. The Python-loop
    version costs ~5–15 % on polygons with thousands of boundary vertices
    (large GND copper); the numpy build is a single C-loop per ring.

    Returns ``(vertices, segments, holes)`` where
      * ``vertices`` is ``(N, 2) float64``
      * ``segments`` is ``(M, 2) int32`` (Triangle prefers int32)
      * ``holes`` is ``(H, 2) float64`` (may be empty)

    ``seed_xy`` is an optional ``(K, 2) float64`` array of user-specified
    interior points (lumped-element attachment points); Triangle preserves
    all input vertices, so we just append them after the ring vertices.
    """
    vertex_chunks: list[np.ndarray] = []
    segment_chunks: list[np.ndarray] = []
    holes_list: list[tuple[float, float]] = []
    offset = 0

    def insert_ring(ring: shapely.geometry.LinearRing) -> None:
        nonlocal offset
        # ring.coords[:-1] drops the closing duplicate vertex that Shapely
        # appends to keep the ring closed.
        coords = np.asarray(ring.coords[:-1], dtype=np.float64)
        if coords.ndim == 1:
            coords = coords.reshape(-1, 2)
        if not ring.is_ccw:
            coords = coords[::-1]
        n = coords.shape[0]
        if n == 0:
            return
        idx = np.arange(n, dtype=np.int32)
        seg = np.empty((n, 2), dtype=np.int32)
        seg[:, 0] = idx + offset
        seg[:, 1] = ((idx + 1) % n) + offset
        vertex_chunks.append(coords)
        segment_chunks.append(seg)
        offset += n

    insert_ring(poly.exterior)
    for hole_ring in poly.interiors:
        insert_ring(hole_ring)
        # representative_point() guarantees a point strictly inside the
        # hole — that's what Triangle's "holes" array means.
        hp = shapely.geometry.Polygon(hole_ring).representative_point()
        holes_list.append((float(hp.x), float(hp.y)))

    if seed_xy is not None and seed_xy.size > 0:
        seeds = np.ascontiguousarray(seed_xy, dtype=np.float64).reshape(-1, 2)
        vertex_chunks.append(seeds)

    if vertex_chunks:
        vertices = np.vstack(vertex_chunks)
    else:
        vertices = np.empty((0, 2), dtype=np.float64)
    if segment_chunks:
        segments = np.vstack(segment_chunks)
    else:
        segments = np.empty((0, 2), dtype=np.int32)
    if holes_list:
        holes = np.asarray(holes_list, dtype=np.float64)
    else:
        holes = np.empty((0, 2), dtype=np.float64)
    return vertices, segments, holes


def _triangulate_arrays(
    vertices: np.ndarray,
    segments: np.ndarray,
    holes: np.ndarray,
    switches: str,
) -> tuple[np.ndarray, np.ndarray]:
    """Single Triangle call — given prepped arrays, return raw output
    ``(vertices, triangles)``. Raises :class:`MeshingException` on
    failure. The output vertex array may be larger than the input (Steiner
    points added by quality refinement).
    """
    tri_input: dict = {"vertices": vertices, "segments": segments}
    if holes.shape[0] > 0:
        tri_input["holes"] = holes
    try:
        tri_output = _triangle.triangulate(tri_input, switches)
    except Exception as e:
        raise MeshingException(f"triangle.triangulate failed: {e}") from e
    out_vertices = tri_output.get("vertices")
    out_triangles = tri_output.get("triangles")
    if out_vertices is None or out_triangles is None:
        raise MeshingException(
            "triangle.triangulate returned no vertices/triangles "
            f"(switches={switches!r}, input has {len(vertices)} vertices, "
            f"{len(segments)} segments, {len(holes)} holes)"
        )
    return out_vertices, out_triangles


# Above this many boundary vertices, an unindexed ``shapely.distance`` (roughly
# O(vertices) per centroid) costs more than building a one-off STRtree over the
# boundary segments and querying nearest; below it the tree-build overhead isn't
# worth it. The two paths give bit-identical distances.
_ADAPTIVE_DIST_INDEX_MIN_BOUNDARY_PTS = 2000


def _boundary_distance(boundary, centroids: np.ndarray) -> np.ndarray:
    """Distance from each centroid to the polygon boundary.

    For large boundaries, index the boundary *segments* in an STRtree and take
    the nearest-segment distance — O(M log S) rather than the unindexed
    ``shapely.distance``'s O(M·S), which on a big GND pour (tens of thousands of
    boundary vertices × thousands of pass-1 triangles) is the dominant cost of
    adaptive meshing (measured ~5× at 50 k boundary vertices). Bit-identical to
    ``shapely.distance``: the nearest segment's distance *is* the boundary
    distance. Small boundaries take the direct path (tree build not worth it)."""
    cpts = shapely.points(centroids[:, 0], centroids[:, 1])
    if shapely.get_num_coordinates(boundary) < _ADAPTIVE_DIST_INDEX_MIN_BOUNDARY_PTS:
        return np.asarray(shapely.distance(boundary, cpts), dtype=np.float64)

    lines = (list(boundary.geoms)
             if boundary.geom_type.startswith("Multi") else [boundary])
    seg_arrays = []
    for line in lines:
        c = shapely.get_coordinates(line)
        if len(c) < 2:
            continue
        seg_arrays.append(shapely.linestrings(np.stack([c[:-1], c[1:]], axis=1)))
    if not seg_arrays:
        return np.asarray(shapely.distance(boundary, cpts), dtype=np.float64)

    tree = shapely.strtree.STRtree(np.concatenate(seg_arrays))
    res, dists = tree.query_nearest(
        cpts, return_distance=True, all_matches=False)
    out = np.empty(centroids.shape[0], dtype=np.float64)
    out[res[0]] = dists
    return out


def _triangulate_adaptive(
    poly: shapely.geometry.Polygon,
    seed_xy: np.ndarray | None,
    adaptive: tuple,
) -> tuple[np.ndarray, np.ndarray]:
    """Variable-density triangulation — fine near features, coarse in the
    interior of large copper.

    Two Triangle passes. Pass 1 builds a uniform *coarse* mesh (refinement
    can only subdivide, so the first pass must be at the coarsest size).
    Pass 2 refines it with a per-triangle area cap that grades from the
    fine size — within ``min_dist`` of a feature — to the coarse size,
    beyond ``max_dist``; a "feature" is a seed point (directive pin / via
    touchdown) or the copper boundary. PDN potential fields concentrate at
    terminals and geometric features and are near-flat in plane interiors,
    so this keeps full resolution where it matters and far fewer unknowns
    where it doesn't — as accurate as the uniform mesh, far smaller.

    ``adaptive`` = ``(minimum_angle, maximum_size, factor, min_dist,
    max_dist)``. The fine size is the width-aware per-polygon value; the
    coarse size is ``fine * factor``.
    """
    minimum_angle, maximum_size, factor, min_dist, max_dist = adaptive
    fine_size = Mesher.polygon_adaptive_max_size(poly, maximum_size)
    if not (fine_size > 0):
        fine_size = maximum_size if maximum_size > 0 else 0.6
    coarse_size = fine_size * max(factor, 1.0)
    _AREA = math.sqrt(3.0) / 4.0          # equilateral-triangle area / size²
    q = f"q{minimum_angle:g}" if minimum_angle > 0 else ""

    verts, segs, holes = _prepare_polygon_for_triangle_arrays(poly, seed_xy)
    p1_in: dict = {"vertices": verts, "segments": segs}
    if holes.shape[0] > 0:
        p1_in["holes"] = holes
    try:
        p1 = _triangle.triangulate(
            p1_in, f"p{q}a{_AREA * coarse_size * coarse_size:.6g}Q")
    except Exception as e:
        raise MeshingException(f"adaptive mesh pass 1 failed: {e}") from e
    p1_v, p1_t = p1.get("vertices"), p1.get("triangles")
    if p1_v is None or p1_t is None or len(p1_t) == 0:
        return (p1_v if p1_v is not None else np.empty((0, 2), np.float64),
                p1_t if p1_t is not None else np.empty((0, 3), np.int64))

    # Per-triangle target area from a *conservative* distance to the nearest
    # feature: min over the triangle's vertices, minus the circumradius,
    # clamped at 0. Grading by the centroid distance alone let a coarse
    # pass-1 triangle CONTAINING a feature (a seed / boundary sliver well
    # inside it) stay coarse whenever its centroid was beyond ``max_dist``;
    # the circumradius bounds how far any interior point can be from the
    # nearest vertex, so ``min_vertex_dist − circumradius`` lower-bounds the
    # true feature distance anywhere in the triangle.
    vdist = _boundary_distance(poly.boundary, p1_v)
    if seed_xy is not None and len(seed_xy) > 0:
        kd = scipy.spatial.cKDTree(
            np.ascontiguousarray(seed_xy, dtype=np.float64).reshape(-1, 2))
        vdist = np.minimum(vdist, kd.query(p1_v)[0])
    tri_pts = p1_v[p1_t]
    ea = np.linalg.norm(tri_pts[:, 1] - tri_pts[:, 2], axis=1)
    eb = np.linalg.norm(tri_pts[:, 2] - tri_pts[:, 0], axis=1)
    ec = np.linalg.norm(tri_pts[:, 0] - tri_pts[:, 1], axis=1)
    d1 = tri_pts[:, 1] - tri_pts[:, 0]
    d2 = tri_pts[:, 2] - tri_pts[:, 0]
    # 2 × triangle area (z of the 2-D cross product; np.cross on 2-D vectors
    # is deprecated in numpy 2.x).
    tri_area2 = np.abs(d1[:, 0] * d2[:, 1] - d1[:, 1] * d2[:, 0])
    # Circumradius R = abc / (4A); degenerate triangles (A ≈ 0) get dist 0
    # → refined at the fine size, the safe direction.
    circumradius = np.divide(
        ea * eb * ec, 2.0 * tri_area2,
        out=np.full(len(p1_t), np.inf), where=tri_area2 > 0.0,
    )
    dist = np.maximum(vdist[p1_t].min(axis=1) - circumradius, 0.0)
    frac = np.clip((dist - min_dist) / max(max_dist - min_dist, 1e-9), 0.0, 1.0)
    size = fine_size + frac * (coarse_size - fine_size)
    target_area = _AREA * size * size

    # Pass 2 — refine the coarse mesh down to the graded per-triangle caps.
    p2_in: dict = {
        "vertices": p1_v,
        "triangles": p1_t,
        "triangle_max_area": np.ascontiguousarray(target_area, dtype=np.float64),
    }
    p1_segs = p1.get("segments")
    if p1_segs is not None and len(p1_segs):
        p2_in["segments"] = p1_segs
    try:
        p2 = _triangle.triangulate(p2_in, f"rp{q}aQ")
    except Exception as e:
        raise MeshingException(
            f"adaptive mesh pass 2 (refine) failed: {e}") from e
    out_v, out_t = p2.get("vertices"), p2.get("triangles")
    if out_v is None or out_t is None:
        raise MeshingException("adaptive mesh refine returned no triangles")
    return out_v, out_t


def triangulate_worker(
    poly_wkb: bytes,
    seed_xy: np.ndarray | None,
    switches: str,
    adaptive: tuple | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """ProcessPoolExecutor worker entry point.

    Must be top-level (not a method, not a closure) so the spawn-mode
    pool can pickle the function reference. Receives polygon + seeds in
    cheap-to-pickle forms (WKB bytes + numpy array), runs Triangle, and
    returns raw numpy arrays. The parent process rebuilds the
    :class:`Mesh` stubs from these arrays — pickling the full Mesh would
    drag tens of thousands of Vertex / Face Python objects back across
    the boundary and erase the parallelism win.

    When ``adaptive`` is given, runs the variable-density two-pass mesher
    (see :func:`_triangulate_adaptive`); otherwise the uniform single pass.
    """
    poly = shapely.wkb.loads(poly_wkb)
    if adaptive is not None:
        return _triangulate_adaptive(poly, seed_xy, adaptive)
    vertices, segments, holes = _prepare_polygon_for_triangle_arrays(
        poly, seed_xy,
    )
    return _triangulate_arrays(vertices, segments, holes, switches)


# shapely.wkb isn't pulled in by ``import shapely.geometry`` — touch it
# at module load so the lazy import doesn't fire inside every worker
# (which would defeat much of the spawn-once amortisation).
import shapely.wkb  # noqa: E402
