"""The batched (all-meshes-at-once) Laplace assembly is bit-identical to the
per-mesh reference.

``process_mesh_laplace_operators`` builds every mesh's cotangent Laplacian in
one global vectorised pass instead of one ``laplace_operator`` call per mesh.
This test drives the real mesh pipeline on a multi-layer problem and asserts
the assembled sparse matrix is *exactly* equal to the one the per-mesh path
produces — same sparsity and byte-identical values — so the optimisation can
never silently change a solve.
"""
from __future__ import annotations

import numpy as np
import scipy.sparse
import shapely
from shapely.geometry import MultiPolygon, Point, box

from pdnsolver import mesh as M
from pdnsolver import problem as P
from pdnsolver import solver as S

_CU = 5.95e4 * 0.035


def _multi_mesh_pipeline():
    """Build several disjoint copper strips and run the solve's mesh stages,
    returning (meshes, conductances, vindex) — the inputs to the Laplace
    assembly. Distinct conductances per layer exercise the per-mesh scaling."""
    layers = []
    for i in range(4):
        y = i * 10.0
        # Vary width/length/conductance so no two layers are identical.
        layer = P.Layer(
            shape=MultiPolygon([box(0.0, y, 20.0 + 5.0 * i, y + 1.0 + i)]),
            name=f"strip{i}", conductance=_CU * (1.0 + 0.3 * i),
        )
        layers.append(layer)

    networks = []
    for layer in layers:
        a, b = P.NodeID(), P.NodeID()
        yc = layer.shape.bounds[1] + 0.5
        xr = layer.shape.bounds[2]
        networks.append(P.Network(
            connections=[
                P.Connection(layer=layer, point=Point(1.0, yc), node_id=a),
                P.Connection(layer=layer, point=Point(xr - 1.0, yc), node_id=b),
            ],
            elements=[P.VoltageSource(p=a, n=b, voltage=1.0)],
        ))
    prob = P.Problem(layers=layers, networks=networks, project_name="multi")

    mesher = M.Mesher(None)
    strtrees = S.construct_strtrees_from_layers(prob.layers)
    cgraph = S.ConnectivityGraph.create_from_problem(prob, strtrees)
    connected = S.find_connected_layer_geom_indices(cgraph)
    meshes, mesh_to_layer = S.generate_meshes_for_problem(
        prob, mesher, connected, strtrees)
    vindex = S.VertexIndexer.create(meshes)
    conductances = [prob.layers[mesh_to_layer[i]].conductance
                    for i in range(len(meshes))]
    return meshes, conductances, vindex


def _permesh_reference(meshes, conductances, vindex):
    """The former per-mesh assembly: one laplace_operator() call per mesh,
    offset into global indices and scaled by that mesh's conductance."""
    offsets = vindex.mesh_vertex_offsets[:-1]
    rows, cols, vals = [], [], []
    for mesh_i, (msh, cond) in enumerate(zip(meshes, conductances)):
        L = S.laplace_operator(msh)
        if L.nnz == 0:
            continue
        off = int(offsets[mesh_i])
        rows.append(L.row.astype(np.int64, copy=False) + off)
        cols.append(L.col.astype(np.int64, copy=False) + off)
        vals.append(L.data.astype(np.float64, copy=False) * cond)
    if rows:
        return (np.concatenate(rows), np.concatenate(cols),
                np.concatenate(vals))
    return (np.empty(0, np.int64), np.empty(0, np.int64), np.empty(0))


def _to_csr(triple, n):
    r, c, v = triple
    return scipy.sparse.coo_matrix((v, (r, c)), shape=(n, n)).tocsr()


def test_batched_laplace_is_bit_identical_to_per_mesh():
    meshes, conductances, vindex = _multi_mesh_pipeline()
    assert len(meshes) >= 4, "expected one mesh per strip"

    n = vindex.n_vertices
    batched = _to_csr(
        S.process_mesh_laplace_operators(meshes, conductances, vindex), n)
    reference = _to_csr(
        _permesh_reference(meshes, conductances, vindex), n)

    # Same sparsity pattern and byte-identical values.
    assert batched.shape == reference.shape
    diff = (batched - reference)
    diff.eliminate_zeros()
    assert diff.nnz == 0, (
        f"batched vs per-mesh differ in {diff.nnz} entries; "
        f"max |Δ| = {np.abs(diff.data).max() if diff.nnz else 0:.3e}"
    )
