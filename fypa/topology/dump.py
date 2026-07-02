"""Write topology debug artifacts (pickle, wiring JSON, SVG) to a folder."""

from __future__ import annotations

import json
import pickle
from pathlib import Path

from fypa.topology.builder import build_topology_model
from fypa.topology.render import render_topology_svg
from fypa.topology.report import topology_wiring_report
from fypa.topology.types import TopologyModel

TOPOLOGY_PKL = "topology.pkl"
WIRING_JSON = "wiring.json"
TOPOLOGY_SVG = "topology.svg"

_TOPOLOGY_PICKLE_KEYS = ("directives", "net_canonical", "annotation_errors")


def topology_pickle_metadata(metadata: dict) -> dict:
    """Topology-only metadata dict safe for :mod:`pickle`.

    Strips the heavy solve bundle (copper polygons, primitives, …) and any
    viewer-session ``shapely.prepared`` caches that cannot be pickled.
    """
    from fypa.cli import sanitize_metadata_for_pickle

    trimmed = {key: metadata[key] for key in _TOPOLOGY_PICKLE_KEYS if key in metadata}
    clean = sanitize_metadata_for_pickle(trimmed)
    if clean is None:
        return {}
    return clean


def dump_topology_debug(
    out_dir: Path,
    metadata: dict,
    *,
    model: TopologyModel | None = None,
    theme: dict[str, str] | None = None,
) -> tuple[Path, Path, Path]:
    """Write ``topology.pkl``, ``wiring.json``, and ``topology.svg`` under *out_dir*.

    *metadata* is the same dict the viewer uses for the Topology tab (including
    live editor preview). The pickle is readable by
    ``tools/dump_topology_wiring.py`` and ``scripts/debug_topology_columns.py``.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if model is None:
        model = build_topology_model(metadata)

    pkl_path = out_dir / TOPOLOGY_PKL
    wiring_path = out_dir / WIRING_JSON
    svg_path = out_dir / TOPOLOGY_SVG

    with pkl_path.open("wb") as f:
        pickle.dump(
            topology_pickle_metadata(metadata),
            f,
            protocol=pickle.HIGHEST_PROTOCOL,
        )

    wiring_path.write_text(
        json.dumps(topology_wiring_report(model), indent=2),
        encoding="utf-8",
    )
    svg_path.write_text(
        render_topology_svg(model, theme=theme),
        encoding="utf-8",
    )
    return pkl_path, wiring_path, svg_path
