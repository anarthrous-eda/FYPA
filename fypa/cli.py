"""FYPA — DC power-delivery-network analysis for Altium PCB designs.

CLI entry point. Subcommands:

  extract       Parse the project, print a summary of extracted records.
  geometry      Build per-layer Shapely geometry; optionally save a quicklook PNG.
  annotations   Parse PDN_* annotations and show resolved terminals.
  load          Full pipeline (extract → geometry → annotations) with a
                solve-readiness verdict.
  solve         Run the FEM solver and pickle the solution.
  show          Open the interactive solution viewer for a pickled solution.
  gui           Open the viewer and import a .PrjPcb (same as File > Import).
  paraview      Export a pickled solution to ParaView VTK.
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import sys
import time
from pathlib import Path

import pickle


class _GilYieldingWriter:
    """File wrapper that drops the GIL on every pickle ``write()`` call,
    rate-limited to once every ``min_interval_s`` seconds.

    Why: ``pickle.dump`` of the per-solve metadata + lean solution is one
    long C call that holds the GIL through its serialisation loop. On
    large boards the metadata dict can be tens of MB and the dump can run
    for several seconds — long enough to freeze the GUI's progress dialog
    and trip the Windows "Not Responding" watchdog. Each time the C
    pickler flushes its internal buffer it calls ``write()`` on us; we
    hand the bytes through to the real file and drop the GIL
    (``time.sleep`` enters ``Py_BEGIN_ALLOW_THREADS``) so the GUI thread
    can repaint. The time-based throttle keeps overhead bounded if pickle
    happens to call ``write()`` in many small chunks (cap at one yield
    per ~30 ms — comfortably finer-grained than the progress-bar repaint
    timer and Windows' "Not Responding" watchdog window)."""

    def __init__(self, f, min_interval_s: float = 0.030) -> None:
        self._f = f
        self._min_interval_s = min_interval_s
        self._next_yield = time.monotonic()

    def write(self, data) -> int:
        n = self._f.write(data)
        now = time.monotonic()
        if now >= self._next_yield:
            time.sleep(0.001)
            self._next_yield = time.monotonic() + self._min_interval_s
        return n

from fypa.altium.annotations import _describe_directive, parse_annotations
from fypa.altium.extract import extract_project
from fypa.altium_geometry import _save_quicklook, build_layer_geometries
from fypa.altium.loader import (
    build_solve_metadata,
    load_project,
    solve_problem_adaptive,
)
from fypa.lean_solution import LeanSolution, to_lean_solution
from pdnsolver import mesh as _pdn_mesh
from pdnsolver import solver as _pdn_solver

# This module is ``fypa/cli.py``; the repo-root ``FYPA.py`` is a thin shim
# that calls :func:`main` here. ``_PKG_DIR`` is the fypa/ package directory;
# ``_REPO_ROOT`` is its parent, which holds the FYPA.py shim, pdnsolver/, and
# the log/ and .cache/ folders.
_PKG_DIR: Path = Path(__file__).resolve().parent
_REPO_ROOT: Path = _PKG_DIR.parent

# Log file location. When frozen by PyInstaller, anchor it next to FYPA.exe
# (same rationale as _CACHE_DIR below) so it's visible to users and the
# Help > Open Log menu item resolves to the same path the logger writes to.
# In a dev checkout it lives in the source tree's log/ folder.
if getattr(sys, "frozen", False):
    _LOG_FILE: Path = Path(sys.executable).parent / "log" / "fypa.log"
else:
    _LOG_FILE: Path = _REPO_ROOT / "log" / "fypa.log"

# --- v1 known-issue mitigations ---------------------------------------------
# A previous mitigation here suppressed all disconnected-mesh generation for
# performance. That turned out to be INCORRECT for nets whose copper is
# "disconnected" in padne's via-coupling graph but IS connected through lumped
# elements (e.g. +3V3L_REG_O: tiny regulator-output copper with no vias, but
# connected to the rest of the circuit through a SOURCE and a SERIES ferrite
# bead). With an empty mesh, padne has no FEM nodes for the lumped-element
# terminals to attach to. PSU2 and FB6 silently lose their connections, the
# downstream rail (+3V3L) ends up with no source, and the solver injects a
# large ground-balancing current (~37 A for a 70 mΩ ferrite bead) to prevent
# a singular matrix — producing garbage voltages (0.4 V instead of 3.3 V).
#
# The performance concern (hundreds of tiny stubs → slow triangulation) is now
# handled by fypa.altium.loader._filter_stub_pieces and _drop_unreachable_layers,
# which remove isolated copper from padne's Problem before it reaches the
# mesher. The remaining "disconnected" components are few and small (legitimate
# copper areas connected only via lumped elements). Letting padne mesh them
# normally is correct and fast enough.


__version__ = "0.1.0-dev"


# --- argparse setup -----------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="FYPA",
        description=__doc__.splitlines()[0],
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("-d", "--debug", action="store_true",
                   help="Enable DEBUG-level logging")
    p.add_argument("--version", action="version",
                   version=f"FYPA {__version__}")

    sub = p.add_subparsers(dest="command", required=True)

    def _add_pcbdoc_arg(sp_: argparse.ArgumentParser) -> None:
        sp_.add_argument(
            "--pcbdoc", default=None,
            help="Which .PcbDoc to use when the project has more than one. "
                 "Accepts an absolute path, project-relative path, filename, "
                 "or filename stem. Default: first PcbDoc in project order.",
        )

    sp = sub.add_parser("extract", help="Extract raw records from a project and summarise")
    sp.add_argument("prjpcb", type=Path, help="Path to the .PrjPcb file")
    _add_pcbdoc_arg(sp)

    sp = sub.add_parser("geometry", help="Build per-layer geometry and summarise")
    sp.add_argument("prjpcb", type=Path)
    sp.add_argument("--png", type=Path, default=None,
                    help="If given, save a per-layer quicklook PNG here.")
    _add_pcbdoc_arg(sp)

    sp = sub.add_parser("annotations", help="Parse PDN_* annotations and show terminals")
    sp.add_argument("prjpcb", type=Path)
    _add_pcbdoc_arg(sp)

    sp = sub.add_parser("load", help="Full pipeline; report solve-readiness")
    sp.add_argument("prjpcb", type=Path)
    sp.add_argument("--png", type=Path, default=None,
                    help="Optional path for a per-layer geometry quicklook PNG.")
    _add_pcbdoc_arg(sp)

    def _add_mesh_args(
        sp_: argparse.ArgumentParser, *, optional: bool = False,
    ) -> None:
        """Add ``--mesh-angle`` / ``--mesh-size`` / ``--adaptive-regulator-gain``.

        When ``optional`` is True (CLI ``gui``), mesh size/angle default to
        ``None`` so the launcher Settings tab is left alone unless the user
        passes the flags explicitly.
        """
        default = _pdn_mesh.Mesher.Config()
        sp_.add_argument(
            "--mesh-angle", type=float,
            default=(None if optional else default.minimum_angle),
            help="Minimum-angle constraint (degrees) for mesh triangles"
                 + (" (default: Settings tab)" if optional else ""),
        )
        sp_.add_argument(
            "--mesh-size", type=float,
            default=(None if optional else default.maximum_size),
            help="Maximum edge size for mesh triangles (mm)"
                 + (" (default: Settings tab)" if optional else ""),
        )
        sp_.add_argument(
            "--adaptive-regulator-gain", action="store_true",
            help="Iterate SMPS regulator gain from solved input voltage",
        )

    sp = sub.add_parser("solve", help="Solve the FEM problem and pickle the solution")
    sp.add_argument("prjpcb", type=Path)
    sp.add_argument("output", type=Path, help="Path to write the pickled solution to")
    _add_mesh_args(sp)
    _add_pcbdoc_arg(sp)

    sp = sub.add_parser("show", help="Open the interactive solution viewer")
    sp.add_argument("solution", type=Path)

    sp = sub.add_parser(
        "gui",
        help="Open the viewer and import a .PrjPcb (same as File > Import)",
    )
    sp.add_argument("prjpcb", type=Path)
    sp.add_argument("--no-cache", action="store_true",
                    help="Force a clean import (ignore design-info / solve "
                         "caches) — same as File > Import Altium Design "
                         "(Clean).")
    _add_mesh_args(sp, optional=True)
    _add_pcbdoc_arg(sp)

    sp = sub.add_parser("paraview", help="Export a pickled solution to ParaView VTK")
    sp.add_argument("solution", type=Path)
    sp.add_argument("output_dir", type=Path)

    sp = sub.add_parser(
        "gerber-gui",
        help="Open the viewer and import a board from Gerber + Excellon files",
    )
    sp.add_argument(
        "input", type=Path, nargs="?", default=None,
        help="Folder of Gerber files OR a saved .fypa with source_kind=gerber. "
             "Omit to open the empty launcher (use File > Import Gerber Files…).",
    )

    return p


# --- subcommand implementations ----------------------------------------------

def _setup_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    _LOG_FILE.parent.mkdir(exist_ok=True)
    fh = logging.FileHandler(_LOG_FILE, mode="w", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(fh)
    # Route Python warnings.warn() through the logging system so the
    # padne solver's SolverWarning (e.g. "Ground node current is not zero…")
    # appears in the log file. Without this, warnings.warn writes only to
    # stderr and is invisible when running through the GUI.
    logging.captureWarnings(True)
    # In-memory mirror of every log record, used by the viewer's Messages
    # tab. Installed here so solve-time warnings emitted before the
    # viewer is imported are still captured.
    from fypa.log_buffer import install as _install_msg_buffer
    _install_msg_buffer()
    logging.getLogger(__name__).info("Log file: %s", _LOG_FILE)


def _force_utf8_console() -> None:
    """Reconfigure stdout/stderr to UTF-8 so non-ASCII characters (paths, net
    names, region vertices' coordinates etc.) don't crash on Windows cp1252."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass


def do_extract(args: argparse.Namespace) -> int:
    proj = extract_project(args.prjpcb, pcbdoc_selector=args.pcbdoc)
    enabled = proj.enabled_copper_layer_ids()
    enabled_desc = ", ".join(
        f"{i}({proj.stackup[i-1].name})" if 1 <= i <= len(proj.stackup) else str(i)
        for i in enabled
    )
    print(f"Project: {proj.prjpcb_path.name}")
    print(f"  tracks         : {len(proj.tracks):>6}")
    print(f"  arcs           : {len(proj.arcs):>6}")
    print(f"  vias           : {len(proj.vias):>6}")
    print(f"  pads           : {len(proj.pads):>6}")
    print(f"  regions        : {len(proj.regions):>6}")
    print(f"  shape_based_regions: {len(proj.shape_based_regions):>6}")
    print(f"  fills          : {len(proj.fills):>6}")
    print(f"  pcb_components : {len(proj.pcb_components):>6}")
    print(f"  nets           : {len(proj.nets):>6}")
    print(f"  stackup rows   : {len(proj.stackup):>6}")
    print(f"  sch_components : {len(proj.sch_components):>6}")
    print(f"  enabled copper layers (Top->Bottom): {enabled_desc}")
    return 0


def do_geometry(args: argparse.Namespace) -> int:
    proj = extract_project(args.prjpcb, pcbdoc_selector=args.pcbdoc)
    layers = build_layer_geometries(proj)
    print(f"Built {len(layers)} copper layer(s):")
    for L in layers:
        n = len(L.shape.geoms) if not L.shape.is_empty else 0
        area = L.shape.area if not L.shape.is_empty else 0.0
        plane = "  [PLANE]" if L.is_plane else ""
        print(f"  id={L.layer_id:>2}  {L.name:<14}  "
              f"{n:>4} polys  {area:>9.2f} mm^2  G={L.conductance:.3g} S{plane}")
    if args.png:
        args.png.parent.mkdir(parents=True, exist_ok=True)
        _save_quicklook(layers, str(args.png))
        print(f"Wrote {args.png}")
    return 0


def do_annotations(args: argparse.Namespace) -> int:
    proj = extract_project(args.prjpcb, pcbdoc_selector=args.pcbdoc)
    result = parse_annotations(proj)
    print(result.summary())
    print()
    for d in result.directives:
        print(_describe_directive(d))
    return 0 if result.ok else 1


def do_load(args: argparse.Namespace) -> int:
    loaded = load_project(args.prjpcb, pcbdoc_selector=args.pcbdoc)
    print(loaded.diagnostic_summary())
    if args.png:
        args.png.parent.mkdir(parents=True, exist_ok=True)
        _save_quicklook(loaded.geometry, str(args.png))
        print(f"\nWrote {args.png}")
    return 0 if loaded.is_solveable else 1


def _require_pyside6(command: str) -> bool:
    """Soft-import PySide6 + matplotlib; print a helpful install hint if missing."""
    try:
        import PySide6  # noqa: F401
        import matplotlib  # noqa: F401  (availability check only)
        return True
    except ImportError as e:
        print(
            f"`{command}` needs PySide6 + matplotlib for the viewer.\n"
            "Install with:  .venv\\Scripts\\python.exe -m pip install PySide6 matplotlib\n"
            f"(import failed: {e})",
            file=sys.stderr,
        )
        return False


def _solve_loaded(loaded, args) -> tuple[LeanSolution, dict]:
    """Run the FEM solver against a LoadedProject. Returns a lean numeric
    solution + metadata dict. The padne :class:`Solution` is converted
    to :class:`LeanSolution` immediately so the heavy half-edge mesh
    structures can be garbage-collected before anything downstream
    touches them — slashes cache pickle size by ~80× on typical boards.

    On a mesh failure, returns a stub LeanSolution + metadata with
    ``mesh_failures`` populated (same packaging as the GUI solve worker)
    so headless ``solve`` can report the bad copper instead of aborting.
    """
    if not loaded.is_solveable:
        print(loaded.diagnostic_summary(), file=sys.stderr)
        raise SystemExit(1)
    from fypa.altium.loader import SolveSettings as _SolveSettings
    settings = _SolveSettings()
    settings.mesh_min_angle_deg = float(args.mesh_angle)
    settings.mesh_max_size_mm = float(args.mesh_size)
    mesher_config = _pdn_mesh.Mesher.Config(
        minimum_angle=settings.mesh_min_angle_deg,
        maximum_size=settings.mesh_max_size_mm,
    )
    adaptive = bool(getattr(args, "adaptive_regulator_gain", False))
    try:
        (padne_solution, problem, via_segment_records,
         stub_pieces_by_pair, per_net_layers, adaptive_info) = (
            solve_problem_adaptive(
                loaded,
                mesher_config,
                adaptive_regulator_gain=adaptive,
            )
        )
    except _pdn_mesh.MeshingException as mesh_exc:
        from fypa.altium.loader import package_mesh_failure
        return package_mesh_failure(
            loaded, mesh_exc, mesher_config, settings=settings,
        )
    # Always log the solver diagnostic stats. ground_node_current should be
    # ~0 for a well-posed problem; a large value indicates either an isolated
    # GND copper region (no via path to the chosen reference vertex) or a
    # phantom current path that the FEM is balancing with an artificial
    # ground injection. When this is non-zero the absolute voltages are
    # unreliable — they're typically offset by a constant.
    si = padne_solution.solver_info
    log = logging.getLogger(__name__)
    log.info("Solver stats: ground_node_current=%.4g A, residual_norm=%.4g",
             si.ground_node_current, si.residual_norm)
    if abs(si.ground_node_current) > 1e-3:
        log.warning(
            "Ground node current is %.4g A — far from zero. The FEM is "
            "injecting / extracting this current at the chosen reference "
            "vertex to balance the system. Likely causes: (1) a GND/return "
            "net has copper regions reachable only via lumped elements, not "
            "via a direct via path to the reference; (2) a directive "
            "terminal lands on a small isolated copper island. Absolute "
            "voltages will be offset by roughly this current × ground-path "
            "resistance.", si.ground_node_current,
        )
    metadata = build_solve_metadata(
        loaded, problem,
        mesher_config=mesher_config,
        solver_info=padne_solution.solver_info,
        via_segment_records=via_segment_records,
        stub_pieces_by_pair=stub_pieces_by_pair,
        per_net_layers=per_net_layers,
        regulator_adaptive_gain=adaptive_info,
    )
    return to_lean_solution(padne_solution), metadata


def _load_solution_pickle(path, *, lean_ify: bool = True
                          ) -> tuple[object, dict | None]:
    """Load a pickled solve output and return ``(solution, metadata)``.

    Accepted formats:

    * **Lean wrapped** (current default):
      ``{"solution": LeanSolution, "metadata": dict, ...}``
    * **Legacy padne wrapped**:
      ``{"solution": padne.Solution, "metadata": dict}`` — by default
      run through :func:`to_lean_solution` so the viewer always sees a
      LeanSolution. Pass ``lean_ify=False`` to get the raw padne object
      (the ParaView export path needs the full half-edge structure).
    * **Bare** padne ``Solution`` — produced by very old runs;
      metadata is ``None``.
    """
    with open(path, "rb") as f:
        obj = pickle.load(f)
        # Split-format caches (large boards) start with a marker string and
        # stream the rest; dispatch to the chunked reader on that marker so
        # this function can open both auto-cache and Save-Solution pickles.
        if isinstance(obj, str) and obj == _SPLIT_CACHE_MARKER:
            obj = _load_split_solve_cache(f)
    if isinstance(obj, dict) and "solution" in obj:
        sol = obj["solution"]
        meta = obj.get("metadata")
    else:
        sol = obj
        meta = None
    if lean_ify and not isinstance(sol, LeanSolution):
        sol = to_lean_solution(sol)
    return sol, meta


# --- Solve cache --------------------------------------------------------------
#
# The GUI can skip work when the project hasn't changed since the last run.
# Two cache layers live under ``FYPA/.cache/<project_stem>_<hash>/``:
#
#   * ``design-info.pkl`` — pickled :class:`LoadedProject`. Reused when the
#     project files + extract/geometry/annotation/loader sources are
#     unchanged. Skips the ~1-3 s extract+geometry+parse pass.
#   * ``solve.pkl``       — the full FEM solution + metadata. Reused when
#     EVERYTHING (project + all tool sources) is unchanged. Skips the
#     ~10-60 s mesh+solve pass.
#
# "Import Altium Design" tries the solve cache first, then the design-info
# cache, then a full extract+solve. "Import Altium Design (Clean)" and
# "Reload Design Info" both bypass the cache reads and force a fresh
# extract+solve. Both writes always happen so the next run can still use
# the cache.

# Bump when the cache pickle format changes in an incompatible way so old
# caches are invalidated automatically (a load with a different version
# treats it as a miss).
_CACHE_SCHEMA_VERSION: int = 7

# Bump when a solver/loader/geometry change alters numerical output — even
# whitespace edits to the tool sources used to invalidate the cache because
# their (mtime, size) fingerprint changed. Switching to a content hash
# (below) means cosmetic-only refactors no longer force a re-solve, but
# real semantic changes still must invalidate. Bump this integer when you
# make a change you want to force a recompute for.
_SOLVE_SCHEMA_VERSION: int = 3

# Cache files live here, keyed by SHA-1 of the project's absolute path
# (so projects with the same .PrjPcb basename in different directories
# don't collide). Wipe the directory at any time to force a fresh solve.
# When frozen by PyInstaller, anchor the cache next to FYPA.exe instead
# of inside _internal\ so it's visible to users and survives re-extracting
# a new build over the old folder.
if getattr(sys, "frozen", False):
    _CACHE_DIR: Path = Path(sys.executable).parent / ".cache"
else:
    _CACHE_DIR: Path = _REPO_ROOT / ".cache"

# Tool-side source files whose CONTENT HASH feeds into the DESIGN-INFO
# fingerprint. If you edit one in a way that changes the bytes, the next
# load invalidates the cached LoadedProject. Anything that affects the
# raw extract / geometry build / annotation parse belongs here.
_DESIGN_TOOL_SOURCES: tuple[Path, ...] = (
    _PKG_DIR / "altium" / "extract.py",
    _PKG_DIR / "altium" / "annotations.py",
    _PKG_DIR / "altium_geometry.py",
    _PKG_DIR / "altium" / "loader.py",
    _PKG_DIR / "gerber" / "extract.py",
    _PKG_DIR / "gerber" / "loader.py",
)

# Additional tool-side source files whose CONTENT HASH feeds into the
# SOLVE fingerprint (on top of the design-info sources). Anything that
# affects FEM assembly, meshing, or the solver itself belongs here.
# cli.py and lean_solution.py live in the fypa/ package; the pdnsolver
# modules sit at the repo root next to it.
_SOLVE_TOOL_SOURCES: tuple[Path, ...] = (
    _PKG_DIR / "cli.py",
    _PKG_DIR / "lean_solution.py",
    _REPO_ROOT / "pdnsolver" / "problem.py",
    _REPO_ROOT / "pdnsolver" / "mesh.py",
    _REPO_ROOT / "pdnsolver" / "solver.py",
)

# Combined list — used by old fingerprint helpers + tests.
_CACHE_TOOL_SOURCES: tuple[Path, ...] = _DESIGN_TOOL_SOURCES + _SOLVE_TOOL_SOURCES


def _stat_fingerprint(path: Path) -> tuple[float, int] | None:
    """``(mtime, size)`` tuple for a file, or None if it doesn't exist."""
    try:
        s = path.stat()
    except OSError:
        return None
    return (s.st_mtime, s.st_size)


def _content_hash(path: Path) -> str | None:
    """SHA-1 of the file's bytes, or None if it doesn't exist.

    Used to fingerprint tool source files. SHA-1 is fine here — we're not
    using it as a security primitive, just a content-equality check, and
    these files are small (low-MB tops) so the hash cost is negligible.
    """
    try:
        with open(path, "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()
    except OSError:
        return None


def _resolve_pcbdoc(prjpcb_path: Path,
                    selector: str | None) -> Path:
    """Return the absolute path of the PcbDoc that this run will solve
    against. ``selector`` is filtered through altium_monkey's matcher;
    ``None`` picks the first PcbDoc in project order. Raises ``ValueError``
    when the selector doesn't match any board in the project.

    Gerber-sourced projects use a synthetic ``.fypa-gerber`` pseudo-path
    in place of a real ``.PrjPcb``; for those there is no PcbDoc to
    resolve, so we return the path unchanged.
    """
    # Non-Altium paths (Gerber import path; a saved .fypa with source_kind
    # == "gerber"; etc.) have no PcbDoc collection to walk.
    if prjpcb_path.suffix.lower() != ".prjpcb":
        return prjpcb_path
    from fypa.altium.extract import list_pcbdoc_paths
    paths = list_pcbdoc_paths(prjpcb_path)
    if not paths:
        raise RuntimeError(
            f"Project {prjpcb_path.name} does not reference any PcbDoc."
        )
    if selector is None:
        return paths[0]
    sel_lower = selector.replace("\\", "/").strip().lower()
    sel_path = Path(selector)
    for p in paths:
        if (p.name.lower() == sel_lower
                or p.stem.lower() == sel_lower
                or str(p).replace("\\", "/").lower() == sel_lower):
            return p
        if sel_path.is_absolute() and p.resolve() == sel_path.resolve():
            return p
    raise ValueError(
        f"--pcbdoc '{selector}' didn't match any PcbDoc in "
        f"{prjpcb_path.name}. Available: "
        f"{', '.join(p.name for p in paths)}"
    )


_FINGERPRINTABLE_DOC_EXTENSIONS: frozenset[str] = frozenset({
    ".schdoc",   # user-edited schematics — PDN_* parameters live here
    ".pcbdoc",   # the board itself
    ".harness",  # harness defs affect netlist compilation
})
# Deliberately EXCLUDED from the fingerprint:
#   .annotation  — Altium auto-generates these on Tools>Annotate; their
#                  existence flaps between runs (the .PrjPcb references
#                  them via DocumentPath= even when the file isn't on
#                  disk), which invalidates the cache for no reason.
#   .outjob      — output-job recipe, doesn't affect the solve.
#   anything else under DocumentPath= that we don't recognise.


# Gerber / Excellon input-file extensions. A Gerber-sourced project's
# synthetic ``<folder>.fypa-gerber`` pseudo-path never exists on disk, so the
# usual .PrjPcb + DocumentPath walk fingerprints nothing — regenerating the
# Gerbers in place (the normal respin: same filenames, new content) would then
# leave the fingerprint unchanged and reuse the stale extract/solution. We
# instead fold in every CAD-output file sitting in the pseudo-path's folder.
# The set matches the fypa.gerber.extract classifier's copper / drill / outline
# / silk extensions (numbered variants like ``.g1`` / ``.gbr3`` / ``.gd2``
# handled by the ``\d*`` groups). Over-including a stray file is safe — it only
# costs a spurious re-extract, never a stale reuse.
_GERBER_INPUT_RE: re.Pattern[str] = re.compile(
    r"\.(?:gbr\d*|g\d+|gtl|gbl|gko|gm1|gto|gbo|gd\d+|gg\d+|sol|cmp"
    r"|drl|xln|tap|nc|txt)$",
    re.IGNORECASE,
)

# Suffix of the synthetic pseudo-path a Gerber import uses in place of a real
# .PrjPcb (see fypa.altium_viewer._pick_gerber_inputs).
_GERBER_PSEUDO_SUFFIX: str = ".fypa-gerber"


def _gerber_input_fingerprints(folder: Path) -> dict[str, tuple[float, int] | None]:
    """``{absolute_path: (mtime, size)}`` for every Gerber/Excellon-looking
    input file in ``folder`` (non-recursive). Backs the Gerber branch of
    :func:`_project_file_fingerprints`."""
    files: dict[str, tuple[float, int] | None] = {}
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return files
    for p in entries:
        try:
            if not p.is_file():
                continue
        except OSError:
            continue
        if _GERBER_INPUT_RE.search(p.name):
            files[str(p.resolve())] = _stat_fingerprint(p)
    return files


def _project_file_fingerprints(prjpcb_path: Path) -> dict[str, tuple[float, int] | None]:
    """``{absolute_path: (mtime, size)}`` for the .PrjPcb and every
    solve-relevant document it references. Used by both the design-info
    and solve fingerprints to detect user-edited project changes.

    Project files use stat (mtime + size) — they're large, the user is
    the only one editing them, and "modified means stale" is the right
    semantics.

    Only documents whose extension is in
    :data:`_FINGERPRINTABLE_DOC_EXTENSIONS` are included. Altium-generated
    auxiliaries (``.Annotation`` in particular) get listed in the
    .PrjPcb's ``DocumentPath=`` lines but are created/deleted out from
    under us, which would otherwise flap the fingerprint between runs
    and invalidate the cache.

    Files that don't exist on disk are skipped entirely (rather than
    stored as ``None``) so a referenced-but-missing document can't flap
    the fingerprint either.

    A Gerber-sourced project (``.fypa-gerber`` pseudo-path) has no .PrjPcb
    or DocumentPath references, so we fingerprint the real Gerber/drill/
    outline inputs in its folder instead — otherwise a regenerated-in-place
    Gerber set would silently reuse the stale cached extract.
    """
    prjpcb_abs = prjpcb_path.resolve()
    if prjpcb_abs.suffix.lower() == _GERBER_PSEUDO_SUFFIX:
        return _gerber_input_fingerprints(prjpcb_abs.parent)
    files: dict[str, tuple[float, int] | None] = {}
    files[str(prjpcb_abs)] = _stat_fingerprint(prjpcb_abs)
    try:
        text = prjpcb_abs.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        text = ""
    for match in re.finditer(r"^\s*DocumentPath\s*=\s*(.+?)\s*$",
                              text, re.MULTILINE):
        rel = match.group(1)
        doc_abs = (prjpcb_abs.parent / rel).resolve()
        if doc_abs.suffix.lower() not in _FINGERPRINTABLE_DOC_EXTENSIONS:
            continue
        fp = _stat_fingerprint(doc_abs)
        if fp is None:
            continue
        files[str(doc_abs)] = fp
    return files


def _tool_source_hashes(sources: tuple[Path, ...]) -> dict[str, str | None]:
    """``{absolute_path: sha1_hex_or_None}`` for the given tool source
    files. Cosmetic edits change the bytes and so invalidate the cache,
    but that's the price of a simple content-equality check."""
    return {str(src.resolve()): _content_hash(src) for src in sources}


def _design_info_fingerprint(prjpcb_path: Path,
                             pcbdoc_path: Path | None = None) -> dict:
    """Fingerprint for the cached LoadedProject (design-info.pkl).

    Includes project files + the design-side tool source hashes. Edits
    to the FEM/mesher/solver sources alone DO NOT invalidate this layer
    — the LoadedProject only depends on extract/geometry/annotation.
    """
    return {
        "schema_version": _CACHE_SCHEMA_VERSION,
        "files": _project_file_fingerprints(prjpcb_path),
        "tool_source_hashes": _tool_source_hashes(_DESIGN_TOOL_SOURCES),
        "pcbdoc_path": str(pcbdoc_path.resolve()) if pcbdoc_path else None,
    }


def _settings_fingerprint(settings=None) -> dict:
    """The solve-affecting tunables (physics + meshing), folded into the solve
    fingerprint so a solve run with non-default settings is keyed separately
    from a default one. Without this, a Re-run with a custom mesh size or
    conductivity writes the cache under the same key a later plain "Import
    Altium Design" (which uses defaults) reads — serving the wrong solve.

    ``settings`` is a :class:`fypa.altium.loader.SolveSettings`; ``None`` means
    the toolchain defaults, matching what a plain import solves with. Sorted,
    primitives-only, so it hashes stably."""
    from fypa.altium.loader import SolveSettings
    if settings is None:
        settings = SolveSettings()
    from dataclasses import asdict, is_dataclass
    raw = asdict(settings) if is_dataclass(settings) else dict(settings)
    return {k: raw[k] for k in sorted(raw)}


def _project_fingerprint(prjpcb_path: Path,
                         pcbdoc_path: Path | None = None,
                         settings=None) -> dict:
    """Fingerprint for the cached solve (solve.pkl).

    Includes everything: project files + all tool source hashes + the
    solve-affecting settings. Any semantic edit to the toolchain — or a change
    in mesh/physics settings — invalidates this layer.
    """
    return {
        "schema_version": _CACHE_SCHEMA_VERSION,
        "solve_schema_version": _SOLVE_SCHEMA_VERSION,
        "files": _project_file_fingerprints(prjpcb_path),
        "tool_source_hashes": _tool_source_hashes(_CACHE_TOOL_SOURCES),
        "pcbdoc_path": str(pcbdoc_path.resolve()) if pcbdoc_path else None,
        "solve_settings": _settings_fingerprint(settings),
    }


def _project_cache_dir(prjpcb_path: Path,
                       pcbdoc_path: Path | None = None) -> Path:
    """``.cache/<project_stem>_<hash>/`` — one folder per project +
    selected PcbDoc. Includes the resolved PcbDoc path in the hash so
    multi-PCB projects don't clobber each other's cache slot."""
    abs_prj = str(prjpcb_path.resolve())
    abs_pcb = str(pcbdoc_path.resolve()) if pcbdoc_path else ""
    digest = hashlib.sha1(
        (abs_prj + "\x00" + abs_pcb).encode("utf-8")
    ).hexdigest()[:16]
    stem = prjpcb_path.stem
    if pcbdoc_path is not None and pcbdoc_path.stem != prjpcb_path.stem:
        stem = f"{stem}_{pcbdoc_path.stem}"
    return _CACHE_DIR / f"{stem}_{digest}"


def _design_info_cache_path(prjpcb_path: Path,
                            pcbdoc_path: Path | None = None) -> Path:
    return _project_cache_dir(prjpcb_path, pcbdoc_path) / "design-info.pkl"


def _solve_cache_path(prjpcb_path: Path,
                      pcbdoc_path: Path | None = None) -> Path:
    return _project_cache_dir(prjpcb_path, pcbdoc_path) / "solve.pkl"


def _cache_path_for(prjpcb_path: Path,
                    pcbdoc_path: Path | None = None) -> Path:
    """Backward-compat alias; returns the solve-cache path."""
    return _solve_cache_path(prjpcb_path, pcbdoc_path)


def _try_load_cached_solution(
    prjpcb_path: Path, current_fp: dict,
    pcbdoc_path: Path | None = None,
) -> tuple[_pdn_solver.Solution, dict] | None:
    """Return ``(solution, metadata)`` from the on-disk solve cache if
    its embedded fingerprint matches ``current_fp``; ``None`` otherwise.
    Silently treats unreadable / outdated / corrupt cache files as misses,
    and deletes corrupt cache files so we don't keep paying to re-read a
    multi-megabyte truncated pickle on every subsequent run.
    """
    cache_path = _solve_cache_path(prjpcb_path, pcbdoc_path)
    if not cache_path.exists():
        return None
    try:
        with open(cache_path, "rb") as f:
            first = pickle.load(f)
            if isinstance(first, str) and first == _SPLIT_CACHE_MARKER:
                blob = _load_split_solve_cache(f)
            else:
                blob = first
    except Exception as e:
        logging.getLogger(__name__).warning(
            "Cache at %s couldn't be read (%s); deleting and re-solving.",
            cache_path, e,
        )
        try:
            cache_path.unlink()
        except OSError:
            pass
        return None
    if not isinstance(blob, dict):
        return None
    cached_fp = blob.get("fingerprint")
    if cached_fp != current_fp:
        _log_fingerprint_diff(
            "Solve cache", cache_path, cached_fp, current_fp,
        )
        return None
    return blob.get("solution"), blob.get("metadata")


def _log_fingerprint_diff(
    label: str, cache_path: Path,
    cached_fp: dict | None, current_fp: dict,
) -> None:
    """Log the keys that differ between the cached and current fingerprint.
    Used by the cache-miss path so the user can see *why* the cache was
    invalidated instead of silently re-solving.
    """
    log = logging.getLogger(__name__)
    if not isinstance(cached_fp, dict):
        log.info("%s miss at %s: cached fingerprint not a dict (%r).",
                 label, cache_path, type(cached_fp).__name__)
        return
    diffs: list[str] = []
    for key in sorted(set(cached_fp.keys()) | set(current_fp.keys())):
        c = cached_fp.get(key)
        n = current_fp.get(key)
        if c == n:
            continue
        if isinstance(c, dict) and isinstance(n, dict):
            sub_diffs: list[str] = []
            for sk in sorted(set(c.keys()) | set(n.keys())):
                cv = c.get(sk)
                nv = n.get(sk)
                if cv != nv:
                    sub_diffs.append(f"    {sk}: cached={cv!r} current={nv!r}")
                    if len(sub_diffs) >= 8:
                        sub_diffs.append("    … (more entries differ)")
                        break
            diffs.append(f"  {key}:\n" + "\n".join(sub_diffs))
        else:
            diffs.append(f"  {key}: cached={c!r} current={n!r}")
    log.info("%s miss at %s — fingerprint differs:\n%s",
             label, cache_path, "\n".join(diffs) if diffs else "  (no diff?)")


_SPLIT_CACHE_MARKER = "split-v3"
_CHUNKED_LIST_MARKER = "__chunked_list__"
# Default per-chunk size for long-list fields. Picked so each per-chunk
# ``pickle.dump`` returns in well under 100 ms — fast enough for the
# GUI to repaint between chunks (we sleep 1 ms after each).
_CACHE_CHUNK_SIZE = 200


def _dump_chunked_value(writer, value, chunk_size: int = _CACHE_CHUNK_SIZE) -> None:
    """Pickle one value, transparently chunking long sequences.

    A single ``pickle.dump`` of a large list (e.g. ``pads_outline`` on a
    board with 10 000+ pads, or ``solution.layer_solutions`` on a
    multi-layer board with chunky numpy arrays) is a multi-second C
    call that holds the GIL continuously — the GUI's progress dialog
    freezes mid-animation and Windows raises "Not Responding".
    Splitting the list into ``chunk_size`` slices and dumping each
    slice as its own pickle gives us a yield point every few tens of ms
    (see the ``time.sleep(0.001)`` between dumps). Non-list values
    pickle in one shot — they're typically small enough that no
    chunking is needed.

    ``chunk_size`` can be tuned per call: ``layer_solutions`` entries
    are individually large (each carries multiple numpy arrays), so the
    caller passes ``chunk_size=1`` to pickle them one at a time."""
    if isinstance(value, (list, tuple)) and len(value) > chunk_size:
        n = len(value)
        pickle.dump((_CHUNKED_LIST_MARKER, n),
                    writer, protocol=pickle.HIGHEST_PROTOCOL)
        for i in range(0, n, chunk_size):
            time.sleep(0.001)
            pickle.dump(value[i:i + chunk_size],
                        writer, protocol=pickle.HIGHEST_PROTOCOL)
    else:
        pickle.dump(value, writer, protocol=pickle.HIGHEST_PROTOCOL)


def _load_chunked_value(f):
    """Counterpart of :func:`_dump_chunked_value`. Reads one logical
    value, transparently rejoining chunks if it was split on write."""
    v = pickle.load(f)
    if isinstance(v, tuple) and len(v) == 2 and v[0] == _CHUNKED_LIST_MARKER:
        total = v[1]
        out: list = []
        while len(out) < total:
            out.extend(pickle.load(f))
        return out
    return v


def _dump_split_solve_cache(
    f, fingerprint: dict, solution, metadata: dict,
) -> None:
    """Serialise the solve cache as a sequence of independent pickles
    rather than one big nested object.

    Why: a single ``pickle.dump`` of the full ``{fingerprint, solution,
    metadata}`` dict is one long C call that holds the GIL through its
    serialisation loop. On large boards the data is tens of MB and the
    dump runs for several seconds — long enough to freeze the GUI
    progress dialog and trip Windows' "Not Responding" watchdog.

    The fix has three layers:

    1. Each top-level field (fingerprint, the three pieces of solution,
       every metadata key) is its own ``pickle.dump`` so we can
       ``time.sleep(0.001)`` between them — that's a guaranteed yield
       point per field.

    2. ``solution.layer_solutions`` is chunked one entry per pickle so
       each per-layer dump (a few numpy arrays' worth) finishes in tens
       of ms.

    3. Long-list metadata values (``pads_outline``, ``vias``, etc.) are
       chunked 200 entries per pickle for the same reason.

    Wire format (each line is one independent pickle in the same file)::

        "split-v3"
        fingerprint
        solution.problem
        solution.solver_info
        chunked(solution.layer_solutions, chunk_size=1)
        sorted metadata keys
        for each key:
            chunked(metadata[key], chunk_size=200)

    Where ``chunked(value, ...)`` is either the value itself (short
    sequence) or a ``(_CHUNKED_LIST_MARKER, total_len)`` header followed
    by ``ceil(total_len / chunk_size)`` slice pickles.

    Concatenated pickles in one file are read back via repeated
    ``pickle.load(f)``. :func:`_load_split_solve_cache` is the
    counterpart; :func:`_try_load_cached_solution` auto-detects whether
    a file is split-format or the legacy single-dict format."""
    log = logging.getLogger(__name__)
    log.info("Cache write [split-v3]: START")
    t0 = time.monotonic()
    writer = _GilYieldingWriter(f)
    pickle.dump(_SPLIT_CACHE_MARKER, writer, protocol=pickle.HIGHEST_PROTOCOL)
    time.sleep(0.001)
    pickle.dump(fingerprint, writer, protocol=pickle.HIGHEST_PROTOCOL)
    time.sleep(0.001)
    log.info("Cache write: marker+fingerprint done (%.2fs)",
             time.monotonic() - t0)
    # Solution: split into (problem, solver_info, layer_solutions) and
    # chunk layer_solutions one-per-pickle. Without this split, a board
    # with many layers × meshes × numpy arrays pickles as one huge C
    # call that holds the GIL for several seconds.
    t = time.monotonic()
    pickle.dump(solution.problem, writer, protocol=pickle.HIGHEST_PROTOCOL)
    time.sleep(0.001)
    log.info("Cache write: solution.problem done (%.2fs, layers=%d)",
             time.monotonic() - t,
             len(getattr(solution.problem, "layers", []) or []))
    t = time.monotonic()
    pickle.dump(solution.solver_info, writer, protocol=pickle.HIGHEST_PROTOCOL)
    time.sleep(0.001)
    log.info("Cache write: solution.solver_info done (%.2fs)",
             time.monotonic() - t)
    t = time.monotonic()
    n_ls = len(solution.layer_solutions)
    _dump_chunked_value(writer, solution.layer_solutions, chunk_size=1)
    time.sleep(0.001)
    log.info("Cache write: solution.layer_solutions done (%.2fs, chunks=%d)",
             time.monotonic() - t, n_ls)
    keys = sorted(metadata.keys())
    pickle.dump(keys, writer, protocol=pickle.HIGHEST_PROTOCOL)
    for k in keys:
        time.sleep(0.001)
        t = time.monotonic()
        v = metadata[k]
        size_hint = (len(v) if hasattr(v, "__len__") else "n/a")
        _dump_chunked_value(writer, v)
        elapsed = time.monotonic() - t
        if elapsed >= 0.1:
            log.info("Cache write: metadata[%s] done (%.2fs, len=%s)",
                     k, elapsed, size_hint)
    log.info("Cache write [split-v3]: DONE in %.2fs", time.monotonic() - t0)


def _load_split_solve_cache(f) -> dict:
    """Read a split-format solve cache. ``f`` must be positioned just
    after the ``split-v3`` marker pickle (i.e. the caller already peeked
    that marker to dispatch here)."""
    from fypa.lean_solution import LeanSolution
    fingerprint = pickle.load(f)
    problem = pickle.load(f)
    solver_info = pickle.load(f)
    layer_solutions = _load_chunked_value(f)
    solution = LeanSolution(
        problem=problem,
        layer_solutions=layer_solutions,
        solver_info=solver_info,
    )
    keys = pickle.load(f)
    metadata = {k: _load_chunked_value(f) for k in keys}
    return {
        "fingerprint": fingerprint,
        "solution": solution,
        "metadata": metadata,
    }


def _save_cached_solution(
    prjpcb_path: Path, fingerprint: dict,
    solution: _pdn_solver.Solution, metadata: dict,
    pcbdoc_path: Path | None = None,
) -> bool:
    """Write the solve output + its fingerprint to the cache. Returns
    True on success, False if the write failed.

    Writes go to a sibling ``*.tmp`` file first and are atomically
    renamed into place on success — an interrupted or failed write
    therefore can never leave a truncated cache pickle behind to confuse
    the next load.
    """
    cache_path = _solve_cache_path(prjpcb_path, pcbdoc_path)
    tmp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp_path, "wb") as f:
            _dump_split_solve_cache(f, fingerprint, solution, metadata)
        os.replace(tmp_path, cache_path)
        return True
    except Exception as e:
        logging.getLogger(__name__).warning(
            "Couldn't write cache at %s (%s: %s); ignoring.",
            cache_path, type(e).__name__, e,
        )
        try:
            tmp_path.unlink()
        except OSError:
            pass
        return False


def _try_load_cached_design_info(
    prjpcb_path: Path, current_fp: dict | None,
    pcbdoc_path: Path | None = None,
):
    """Return the cached :class:`LoadedProject` if its embedded
    design-info fingerprint matches ``current_fp``; ``None`` otherwise.
    Silently treats unreadable / outdated / corrupt cache files as misses.

    Pass ``current_fp=None`` to skip the fingerprint comparison entirely
    and return the cached design info as-is (an absent / corrupt cache is
    still a ``None`` miss). The editor 'Resolve' path uses this: a resolve
    re-solves against the design that's already loaded, so re-stat'ing the
    Altium project files to revalidate the cache would be pointless work.
    """
    cache_path = _design_info_cache_path(prjpcb_path, pcbdoc_path)
    if not cache_path.exists():
        return None
    log = logging.getLogger(__name__)
    # Reusing the design extract = unpickling this cached LoadedProject.
    # On a big board (e.g. corvette) that single pickle.load runs well
    # over a minute, so time it explicitly — file size + duration give a
    # reportable number independent of the GUI's stage breakdown.
    try:
        size_mb = cache_path.stat().st_size / 1e6
        _t0 = time.monotonic()
        with open(cache_path, "rb") as f:
            blob = pickle.load(f)
        log.info(
            "Design-info cache: unpickled %s (%.1f MB) in %.2fs",
            cache_path.name, size_mb, time.monotonic() - _t0,
        )
    except Exception as e:
        log.warning(
            "Design-info cache at %s couldn't be read (%s); re-extracting.",
            cache_path, e,
        )
        return None
    if not isinstance(blob, dict):
        return None
    if current_fp is not None and blob.get("fingerprint") != current_fp:
        log.info("Design-info cache: fingerprint mismatch — re-extracting.")
        return None
    log.info("Design-info cache hit — reusing the design extract.")
    return blob.get("loaded")


def _save_cached_design_info(
    prjpcb_path: Path, fingerprint: dict, loaded,
    pcbdoc_path: Path | None = None,
) -> None:
    """Write the LoadedProject + its design-info fingerprint to the
    cache. Failures are non-fatal."""
    cache_path = _design_info_cache_path(prjpcb_path, pcbdoc_path)
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "wb") as f:
            pickle.dump({
                "fingerprint": fingerprint,
                "loaded": loaded,
            }, _GilYieldingWriter(f), protocol=pickle.HIGHEST_PROTOCOL)
    except Exception as e:
        logging.getLogger(__name__).warning(
            "Couldn't write design-info cache at %s (%s); ignoring.",
            cache_path, e,
        )


def sanitize_metadata_for_pickle(metadata: dict | None) -> dict | None:
    """Return a metadata tree safe for :mod:`pickle`.

    The viewer attaches non-picklable ``shapely.prepared`` caches to
    polygon / primitive dicts during a session; strip those before save.
    """
    if metadata is None:
        return None

    def _scrub(obj):
        if isinstance(obj, dict):
            return {
                k: _scrub(v)
                for k, v in obj.items()
                if k not in _PREPARED_GEOM_KEYS
            }
        if isinstance(obj, list):
            return [_scrub(v) for v in obj]
        if isinstance(obj, tuple):
            return tuple(_scrub(v) for v in obj)
        return obj

    return _scrub(metadata)


_PREPARED_GEOM_KEYS = frozenset({
    "_prepared_shape_cache",
    "_prepared_shape",
    "prepared_shape",
})


def save_solution_file(path: Path, solution, metadata: dict | None) -> None:
    """Write a user-saved solution snapshot to ``path``.

    Format is the same dict layout the auto-cache uses (and that
    :func:`_load_solution_pickle` accepts), without the cache
    fingerprint. ``metadata`` already carries ``prjpcb_path`` and
    ``pcbdoc_path`` (set by :func:`build_solve_metadata`), so reloading
    the file later re-attaches the solution to the right project +
    board for Re-run / Reload Design Info."""
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_metadata = sanitize_metadata_for_pickle(metadata)
    with open(path, "wb") as f:
        pickle.dump({"solution": solution, "metadata": safe_metadata}, f,
                    protocol=pickle.HIGHEST_PROTOCOL)


def do_solve(args: argparse.Namespace) -> int:
    loaded = load_project(args.prjpcb, pcbdoc_selector=args.pcbdoc)
    solution, metadata = _solve_loaded(loaded, args)
    if metadata.get("mesh_failures"):
        for rec in metadata["mesh_failures"]:
            summary = rec.get("summary") or "Meshing failed."
            print(summary, file=sys.stderr)
        print(
            "Meshing failed — solution not written. "
            "Open the project with `gui` to inspect the bad copper.",
            file=sys.stderr,
        )
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # HIGHEST_PROTOCOL: pickle protocol 5 (Python 3.8+) gets out-of-band
    # numpy buffer support automatically for ndarray fields inside the
    # LeanSolution, which shrinks the pickle and speeds up load. Keeps
    # parity with the cache writer below.
    with open(args.output, "wb") as f:
        pickle.dump({"solution": solution, "metadata": metadata}, f,
                    protocol=pickle.HIGHEST_PROTOCOL)
    print(f"Solution saved to {args.output}")
    return 0


def do_show(args: argparse.Namespace) -> int:
    if not _require_pyside6("show"):
        return 2
    from fypa import altium_viewer  # PySide6 import lives inside the viewer
    solution, metadata = _load_solution_pickle(args.solution)
    return altium_viewer.main(solution, metadata=metadata) or 0


def do_gui(args: argparse.Namespace) -> int:
    """Open the viewer launcher and import ``args.prjpcb`` the same way as
    File > Import Altium Design (progress dialog, ``_SolveWorker``, mesh-
    failure stub, caches).

    Respects the persisted "Solve automatically on Altium import" preference
    — when that is off, the design opens as a stub without meshing (same as
    File > Import). ``--no-cache`` maps to Import (Clean).

    PcbDoc selection stays non-interactive (first board, or ``--pcbdoc``) so
    Altium ``Run_FYPA.pas`` / automation never block on a picker. Mesh
    ``--mesh-angle`` / ``--mesh-size`` are applied only when passed; otherwise
    the launcher Settings-tab values are used.
    """
    if not _require_pyside6("gui"):
        return 2
    from fypa import altium_viewer

    # Always resolve silently — never open the multi-PcbDoc GUI picker.
    pcbdoc_path = _resolve_pcbdoc(
        args.prjpcb, getattr(args, "pcbdoc", None),
    )

    adaptive = getattr(args, "adaptive_regulator_gain", False)
    target: dict = {
        "prjpcb_path": args.prjpcb.resolve(),
        "pcbdoc_path": pcbdoc_path,
        "clean": bool(getattr(args, "no_cache", False)),
        # True only when the flag is passed; otherwise the launcher uses
        # the persisted Settings-tab preference (same as File > Import).
        "adaptive_regulator_gain": True if adaptive else None,
    }
    # Only override Settings-tab mesh values when the user passed the flags.
    if getattr(args, "mesh_angle", None) is not None:
        target["mesh_min_angle_deg"] = float(args.mesh_angle)
    if getattr(args, "mesh_size", None) is not None:
        target["mesh_max_size_mm"] = float(args.mesh_size)
    return altium_viewer.main(None, altium_import_target=target) or 0


def do_gerber_gui(args: argparse.Namespace) -> int:
    """Open the viewer's empty launcher, then immediately trigger the
    Gerber-import flow against ``args.input`` (either a folder of
    Gerbers or a previously-saved ``.fypa`` with ``source_kind=gerber``).

    The actual file-picker + dialog flow lives in the viewer module so
    this subcommand is a thin pre-arrangement around
    :func:`fypa.altium_viewer.main`.
    """
    if not _require_pyside6("gerber-gui"):
        return 2
    from fypa import altium_viewer
    return altium_viewer.main(
        None,
        gerber_import_target=args.input,
    ) or 0


def do_paraview(args: argparse.Namespace) -> int:
    try:
        import lxml.etree  # noqa: F401 — ensure the export dep is present
    except ImportError as e:
        print(f"`paraview` needs the `lxml` package: {e}", file=sys.stderr)
        return 2
    # Export from the lean numeric solution: it carries the flat per-vertex
    # coordinates, per-triangle indices and potentials the VTU writer needs
    # (the half-edge Mesh the old pdnsolver.paraview exporter walked is no
    # longer built by the solver). ``lean_ify=True`` normalises both current
    # lean pickles and legacy padne pickles to that form. This is the same
    # writer File > Export > ParaView uses, so CLI and GUI output match.
    from fypa.paraview_export import export_lean_solution
    solution, _ = _load_solution_pickle(args.solution, lean_ify=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    n_files = export_lean_solution(solution, args.output_dir)
    print(f"ParaView export complete: {n_files} file(s) in {args.output_dir}")
    return 0


_DISPATCH = {
    "extract": do_extract,
    "geometry": do_geometry,
    "annotations": do_annotations,
    "load": do_load,
    "solve": do_solve,
    "show": do_show,
    "gui": do_gui,
    "gerber-gui": do_gerber_gui,
    "paraview": do_paraview,
}


def main(argv: list[str] | None = None) -> int:
    _force_utf8_console()
    parser = _build_parser()
    if argv is None and len(sys.argv) == 1:
        # No arguments — open the empty viewer launcher so the user can
        # pick a .PrjPcb (or .pkl) from the File menu.
        _setup_logging(False)
        if not _require_pyside6("gui"):
            return 2
        from fypa import altium_viewer
        return altium_viewer.main(None) or 0
    args = parser.parse_args(argv)
    _setup_logging(args.debug)
    return _DISPATCH[args.command](args)


if __name__ == "__main__":
    # Required on Windows when this script is frozen with PyInstaller.
    # pdnsolver runs the mesher in a ProcessPoolExecutor; on Windows the
    # workers re-launch the same .exe under the 'spawn' start method, and
    # without freeze_support() each child would re-enter main() instead
    # of the worker bootstrap — infinite-loop GUI spawn. No-op in dev
    # (uses Python interpreter directly), no-op on POSIX.
    import multiprocessing
    multiprocessing.freeze_support()
    sys.exit(main())
