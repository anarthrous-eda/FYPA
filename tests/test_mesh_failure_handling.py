"""Tests for FEM mesh failure reporting and geometry guards."""

import numpy as np
import shapely.geometry

from fypa.altium_viewer import (
    _activate_mesh_failure_layer,
    _mesh_failure_outline_rings,
    _phys_name_for_layer_id,
    _primary_mesh_failure_layer_id,
)
from fypa.altium.loader import _filter_tiny_pieces
from pdnsolver.mesh import (
    MeshingException,
    _dedupe_ring_coords,
    _humanize_triangle_error,
    _parse_triangle_location,
    repair_polygon_for_triangulation,
)


def test_parse_triangle_precision_location():
    msg = (
        "Error:  Ran out of precision at (12.491, 36.196).\n"
        "I attempted to split a segment to a smaller size than"
    )
    assert _parse_triangle_location(msg) == (12.491, 36.196)


def test_humanize_invalid_geometry_message():
    cause = _humanize_triangle_error(
        "Triangulation failed -- probably because of invalid geometry on input.",
    )
    assert "invalid" in cause.lower()
    assert "self-intersect" in cause.lower() or "degenerate" in cause.lower()


def test_meshing_exception_user_message_includes_layer_and_location():
    exc = MeshingException(
        "triangle.triangulate failed: bad",
        layer_name="Top|+3V3",
        geom_index=2,
        area_mm2=0.05,
        bounds=(12.0, 35.0, 13.0, 37.0),
        location_xy=(12.491, 36.196),
        triangle_cause=_humanize_triangle_error("invalid geometry on input"),
    )
    text = exc.format_user_message()
    assert "Top|+3V3" in text
    assert "island #3" in text
    assert "12.491" in text
    assert "36.196" in text


def test_repair_polygon_welds_near_duplicate_vertices():
    # Two nearly-coincident vertices on a skinny rectangle.
    poly = shapely.geometry.Polygon([
        (0.0, 0.0),
        (10.0, 0.0),
        (10.0, 1e-5),
        (10.0 + 1e-7, 1.0),
        (0.0, 1.0),
    ])
    repaired = repair_polygon_for_triangulation(poly)
    assert not repaired.is_empty
    assert repaired.is_valid


def test_filter_tiny_pieces_drops_unanchored_slivers():
    big = shapely.geometry.box(0, 0, 10, 10)
    tiny = shapely.geometry.box(20, 20, 20.0005, 20.0005)
    shape = shapely.geometry.MultiPolygon([big, tiny])
    kept, dropped = _filter_tiny_pieces(shape, 1e-4, [], [])
    assert dropped == [tiny]
    assert kept.area == big.area


def test_mesh_failure_outline_uses_local_marker_not_whole_pour():
    huge = shapely.geometry.box(0, 0, 200, 150)
    rec = {
        "location_xy": [12.491, 36.196],
        "exterior": np.asarray(huge.exterior.coords[:-1], dtype=np.float32),
    }
    rings = _mesh_failure_outline_rings(rec)
    assert len(rings) >= 2  # inner + outer circle
    # Must not include the 200 mm pour outline.
    max_span = max(
        max(x for x, _y in ring) - min(x for x, _y in ring)
        for ring in rings
    )
    assert max_span < 20.0


def test_dedupe_ring_coords_collapses_coincident_vertices():
    coords = np.asarray([
        [0.0, 0.0],
        [1e-7, 0.0],
        [10.0, 0.0],
        [10.0, 10.0],
        [0.0, 10.0],
    ], dtype=np.float64)
    out = _dedupe_ring_coords(coords, 1e-4)
    assert out.shape[0] == 4


def test_filter_tiny_pieces_returns_multipolygon_for_single_piece():
    poly = shapely.geometry.box(0, 0, 10, 10)
    kept, dropped = _filter_tiny_pieces(poly, 1e-4, [], [])
    assert dropped == []
    assert kept.geom_type == "MultiPolygon"
    assert len(kept.geoms) == 1


def test_filter_tiny_pieces_keeps_anchored_sliver_with_pin():
    tiny = shapely.geometry.box(0, 0, 0.0005, 0.0005)
    kept, dropped = _filter_tiny_pieces(
        tiny, 1e-4, [(0.0001, 0.0001)], [],
    )
    assert dropped == []
    assert not kept.is_empty


class _FakeEye:
    def __init__(self, visible: bool = False):
        self._visible = visible

    def isVisibleState(self) -> bool:
        return self._visible

    def setVisibleState(self, on: bool, *, emit: bool = True) -> None:
        self._visible = on


class _FakeViewer:
    def __init__(self):
        self.metadata = {
            "mesh_failures": [{"layer_id": 2, "summary": "bad island"}],
        }
        self._phys_name_to_layer_id = {"Top": 1, "Bottom": 2}
        self._layer_eye_buttons = [
            ("Top", _FakeEye(True)),
            ("Bottom", _FakeEye(False)),
        ]
        self._layer_eye2_buttons = [
            ("Top", _FakeEye(True)),
            ("Bottom", _FakeEye(False)),
        ]
        self._selected_layer = None
        self.synced_eye = False
        self.synced_eye2 = False

    def _apply_layer_selection_highlight(self) -> None:
        pass

    def _sync_all_layers_eye(self) -> None:
        self.synced_eye = True

    def _sync_all_layers_eye2(self) -> None:
        self.synced_eye2 = True


def test_primary_mesh_failure_layer_id_skips_unknown():
    assert _primary_mesh_failure_layer_id([
        {"layer_id": -1},
        {"layer_id": 3},
    ]) == 3


def test_phys_name_for_layer_id_reverse_maps_stackup():
    viewer = _FakeViewer()
    assert _phys_name_for_layer_id(viewer, 2) == "Bottom"
    assert _phys_name_for_layer_id(viewer, 99) is None


def test_activate_mesh_failure_layer_enables_and_selects():
    viewer = _FakeViewer()
    assert _activate_mesh_failure_layer(viewer) is True
    assert viewer._layer_eye_buttons[1][1].isVisibleState()
    assert viewer._layer_eye2_buttons[1][1].isVisibleState()
    assert viewer._selected_layer == "Bottom"
    assert viewer.synced_eye and viewer.synced_eye2
    assert _activate_mesh_failure_layer(viewer) is False


def test_package_mesh_failure_returns_stub_for_cli_gui_path(monkeypatch):
    """Mesh-failure packaging must keep Settings in metadata for Setup tab."""
    from types import SimpleNamespace

    from fypa.altium.loader import SolveSettings, package_mesh_failure
    from fypa.lean_solution import LeanSolution

    poly = shapely.geometry.box(0.0, 0.0, 2.0, 1.0)
    geom_layer = SimpleNamespace(
        name="Bottom Layer",
        conductance=1.0,
        shape=poly,
        layer_id=32,
        is_plane=False,
    )
    loaded = SimpleNamespace(
        project_name="example",
        geometry=[geom_layer],
        extracted=SimpleNamespace(
            nets=[],
            stackup=[],
            board_outline=[],
            enabled_copper_layer_ids=lambda: [32],
            tracks=[], arcs=[], vias=[], pads=[], regions=[],
            pcb_components=[], sch_components=[],
            prjpcb_path="example.PrjPcb",
            pcbdoc_path="Main.PcbDoc",
        ),
        annotations=SimpleNamespace(
            directives=[], warnings=[], errors=[],
            open_loop_rails=[], connectivity_breaks=[],
        ),
    )

    # Fake a Problem already attached to the exception (solve_problem_adaptive
    # path) so package_mesh_failure does not call build_problem.
    fake_layer = SimpleNamespace(
        name="Bottom Layer|VIN",
        geoms=[poly],
    )
    fake_problem = SimpleNamespace(layers=[fake_layer], networks=[])
    per_net = [SimpleNamespace(name="Bottom Layer|VIN", layer_id=32)]

    exc = MeshingException(
        "triangle aborted",
        layer_name="Bottom Layer|VIN",
        layer_index=0,
        geom_index=0,
        piece_index=0,
        bounds=(0.0, 0.0, 2.0, 1.0),
    )
    exc.built_problem = fake_problem
    exc.built_via_segment_records = []
    exc.built_stub_pieces_by_pair = {}
    exc.built_per_net_layers = per_net

    def _boom(*_a, **_k):
        raise AssertionError("build_problem should not run when artifacts exist")

    monkeypatch.setattr(
        "fypa.altium.loader.build_problem", _boom,
    )
    captured_settings = {}

    def _fake_meta(*_a, mesh_failures=None, settings=None, **_k):
        captured_settings["settings"] = settings
        return {
            "mesh_failures": list(mesh_failures or []),
            "project_name": "example",
        }

    monkeypatch.setattr(
        "fypa.altium.loader.build_solve_metadata", _fake_meta,
    )

    settings = SolveSettings()
    settings.temperature_c = 85.0
    stub, metadata = package_mesh_failure(
        loaded, exc, mesher_config=None, settings=settings,
    )
    assert isinstance(stub, LeanSolution)
    assert stub.solver_info.get("stub") is True
    assert metadata["mesh_failures"]
    assert "Bottom Layer|VIN" in (metadata["mesh_failures"][0].get("summary") or "")
    assert captured_settings["settings"] is settings
    assert captured_settings["settings"].temperature_c == 85.0


def test_solve_loaded_reports_failure_without_building_a_stub(monkeypatch):
    """Headless ``solve`` writes nothing on a mesh failure, so it must not pay
    for package_mesh_failure — which re-runs build_problem over the whole board
    and builds a stub solution for every copper layer."""
    from types import SimpleNamespace

    from fypa import cli

    exc = MeshingException("triangle aborted", layer_name="Bottom Layer|VIN")

    def _raise(*_a, **_k):
        raise exc

    packaged: list[bool] = []
    monkeypatch.setattr(cli, "solve_problem_adaptive", _raise)
    monkeypatch.setattr(
        "fypa.altium.loader.package_mesh_failure",
        lambda *_a, **_k: packaged.append(True),
    )
    monkeypatch.setattr(
        "fypa.altium.loader.build_mesh_failure_records",
        lambda *_a, **_k: [{"summary": "bad copper"}],
    )

    loaded = SimpleNamespace(is_solveable=True)
    args = SimpleNamespace(
        mesh_angle=20.0, mesh_size=1.0, adaptive_regulator_gain=False,
    )
    solution, metadata = cli._solve_loaded(loaded, args)
    assert solution is None
    assert metadata["mesh_failed"] is True
    assert metadata["mesh_failures"]
    assert packaged == [], "headless must not run the GUI stub packaging"


def test_do_gui_routes_through_viewer_import(monkeypatch, tmp_path):
    """CLI ``gui`` must use LauncherWindow + _SolveWorker, not CLI solve."""
    from pathlib import Path
    from types import SimpleNamespace

    from fypa import cli
    import fypa.altium_viewer as av

    prj = tmp_path / "example.PrjPcb"
    prj.write_text("PcbProject", encoding="utf-8")
    pcb = tmp_path / "Main.PcbDoc"
    pcb.write_text("PCB", encoding="utf-8")
    captured: dict = {}

    def _fake_main(solution, **kwargs):
        captured["solution"] = solution
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(av, "main", _fake_main)
    monkeypatch.setattr(cli, "_require_pyside6", lambda *_a, **_k: True)
    monkeypatch.setattr(
        cli, "_resolve_pcbdoc", lambda *_a, **_k: pcb,
    )

    args = SimpleNamespace(
        prjpcb=prj,
        pcbdoc=None,
        no_cache=True,
        mesh_angle=None,
        mesh_size=None,
        adaptive_regulator_gain=None,      # neither flag passed
    )
    assert cli.do_gui(args) == 0
    assert captured["solution"] is None
    tgt = captured["altium_import_target"]
    assert Path(tgt["prjpcb_path"]) == prj.resolve()
    assert Path(tgt["pcbdoc_path"]) == pcb
    assert tgt["clean"] is True
    # No --mesh-* → do not clobber launcher Settings.
    assert "mesh_min_angle_deg" not in tgt
    assert "mesh_max_size_mm" not in tgt
    assert tgt["adaptive_regulator_gain"] is None
    # Nothing explicit was asked for, so the auto-solve preference still rules.
    assert tgt["force_solve"] is False


def test_do_gui_applies_explicit_mesh_flags(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from fypa import cli
    import fypa.altium_viewer as av

    prj = tmp_path / "example.PrjPcb"
    prj.write_text("PcbProject", encoding="utf-8")
    pcb = tmp_path / "Main.PcbDoc"
    pcb.write_text("PCB", encoding="utf-8")
    captured: dict = {}

    monkeypatch.setattr(
        av, "main",
        lambda solution, **kwargs: captured.update(
            {"solution": solution, **kwargs},
        ) or 0,
    )
    monkeypatch.setattr(cli, "_require_pyside6", lambda *_a, **_k: True)
    monkeypatch.setattr(cli, "_resolve_pcbdoc", lambda *_a, **_k: pcb)

    args = SimpleNamespace(
        prjpcb=prj,
        pcbdoc=None,
        no_cache=False,
        mesh_angle=25.0,
        mesh_size=0.5,
        adaptive_regulator_gain=True,
    )
    assert cli.do_gui(args) == 0
    tgt = captured["altium_import_target"]
    assert tgt["mesh_min_angle_deg"] == 25.0
    assert tgt["mesh_max_size_mm"] == 0.5
    assert tgt["adaptive_regulator_gain"] is True
    # Mesh flags mean nothing without a solve, so they override the
    # "Solve automatically on Altium import" preference.
    assert tgt["force_solve"] is True


def test_schedule_cli_altium_import_skips_unset_mesh(monkeypatch, tmp_path):
    """Unset mesh keys must not rewrite launcher Settings."""
    from types import SimpleNamespace

    from fypa import altium_viewer as av
    from fypa.altium.loader import SolveSettings

    settings = SolveSettings()
    settings.mesh_min_angle_deg = 33.0
    settings.mesh_max_size_mm = 1.25
    window = SimpleNamespace(_solve_settings=settings)
    opened = {}

    monkeypatch.setattr(
        av, "_open_altium_project_at",
        lambda win, prj, pcb, *, clean=False, force_solve=False: opened.update(
            {"prj": prj, "pcb": pcb, "clean": clean,
             "force_solve": force_solve},
        ),
    )
    pcb = tmp_path / "Main.PcbDoc"
    pcb.write_text("x", encoding="utf-8")
    av._schedule_cli_altium_import(window, {
        "prjpcb_path": tmp_path / "example.PrjPcb",
        "pcbdoc_path": pcb,
        "clean": False,
    })
    assert settings.mesh_min_angle_deg == 33.0
    assert settings.mesh_max_size_mm == 1.25
    assert opened["pcb"] == pcb
    assert opened["clean"] is False


# --- mesh_failed vs an empty mesh_failures list --------------------------------


def test_do_solve_fails_when_meshing_failed_with_no_records(
        monkeypatch, tmp_path):
    """build_mesh_failure_records returns [] when _build_stub_record rejects
    the piece — the same degenerate sliver that makes Triangle abort. Reading
    that as success pickled an empty stub and exited 0."""
    from types import SimpleNamespace

    from fypa import cli

    monkeypatch.setattr(cli, "load_project", lambda *_a, **_k: object())
    monkeypatch.setattr(
        cli, "_solve_loaded",
        lambda *_a, **_k: (None, {"mesh_failures": [], "mesh_failed": True}),
    )
    out = tmp_path / "out.pkl"
    args = SimpleNamespace(prjpcb=tmp_path / "x.PrjPcb", pcbdoc=None,
                           output=out)
    assert cli.do_solve(args) == 1
    assert not out.exists(), "a failed mesh must not write a solution file"


def test_do_solve_writes_when_meshing_succeeded(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from fypa import cli

    monkeypatch.setattr(cli, "load_project", lambda *_a, **_k: object())
    monkeypatch.setattr(
        cli, "_solve_loaded",
        lambda *_a, **_k: ("solution", {"mesh_failures": [],
                                        "mesh_failed": False}),
    )
    out = tmp_path / "out.pkl"
    args = SimpleNamespace(prjpcb=tmp_path / "x.PrjPcb", pcbdoc=None,
                           output=out)
    assert cli.do_solve(args) == 0
    assert out.exists()


# --- adaptive gain must not disable the solve cache ---------------------------


def test_cache_serves_adaptive_request_when_no_regulator_is_eligible():
    """"Adaptive SMPS gain" is persisted globally, so it arrives set on boards
    it cannot affect. Refusing the cache there cost a full 10-60 s re-solve on
    every import for a bit-identical result."""
    import fypa.altium_viewer as av

    plain = {"directives": [{"kind": "sink"}],
             "regulator_adaptive_gain": {"enabled": False}}
    assert av._cache_serves_adaptive_request(plain, True) is True
    assert av._cache_serves_adaptive_request(plain, False) is True


def test_cache_refused_when_an_eligible_regulator_exists():
    import fypa.altium_viewer as av

    eligible = {
        "directives": [{"kind": "regulator", "adaptive_gain_eligible": True}],
        "regulator_adaptive_gain": {"enabled": False},
    }
    assert av._cache_serves_adaptive_request(eligible, True) is False
    # …but a non-adaptive request is happy with a non-adaptive solve.
    assert av._cache_serves_adaptive_request(eligible, False) is True


def test_cache_accepted_when_it_is_itself_an_adaptive_solve():
    import fypa.altium_viewer as av

    adaptive = {
        "directives": [{"kind": "regulator", "adaptive_gain_eligible": True}],
        "regulator_adaptive_gain": {"enabled": True, "converged": True},
    }
    assert av._cache_serves_adaptive_request(adaptive, True) is True


# --- CLI flag lifetime ---------------------------------------------------------


def test_cli_adaptive_flag_is_consumed_by_one_import(monkeypatch):
    """A failed or cancelled CLI import leaves the launcher open; latching the
    flag would override the Settings tab for every later File > Import."""
    from types import SimpleNamespace

    import fypa.altium_viewer as av

    monkeypatch.setattr(av, "load_adaptive_regulator_gain", lambda: False)
    window = SimpleNamespace(_cli_adaptive_regulator_gain=True)

    assert av._consume_cli_adaptive_flag(window) is True
    assert window._cli_adaptive_regulator_gain is None
    # Next import falls back to the persisted preference.
    assert av._consume_cli_adaptive_flag(window) is False


def test_cli_can_force_adaptive_gain_off(monkeypatch):
    """--no-adaptive-regulator-gain must beat a persisted preference of ON."""
    from types import SimpleNamespace

    import fypa.altium_viewer as av

    monkeypatch.setattr(av, "load_adaptive_regulator_gain", lambda: True)
    window = SimpleNamespace(_cli_adaptive_regulator_gain=False)
    assert av._consume_cli_adaptive_flag(window) is False


def test_no_cli_flag_uses_the_persisted_preference(monkeypatch):
    from types import SimpleNamespace

    import fypa.altium_viewer as av

    monkeypatch.setattr(av, "load_adaptive_regulator_gain", lambda: True)
    assert av._consume_cli_adaptive_flag(SimpleNamespace()) is True


def test_cli_mesh_overrides_resync_the_settings_widgets(monkeypatch, tmp_path):
    """The Settings tab was populated during __init__, so an override applied
    afterwards left the widget showing the old value — a dirty red outline on
    a launcher the user never touched, and Apply would undo the override."""
    from types import SimpleNamespace

    import fypa.altium_viewer as av
    from fypa.altium.loader import SolveSettings

    class _Edit:
        def __init__(self):
            self.text_value = "20"

        def setText(self, v):
            self.text_value = v

    edit = _Edit()
    refreshed: list[bool] = []
    window = SimpleNamespace(
        _solve_settings=SolveSettings(),
        settings_edit_mesh_min_angle_deg=edit,
        _update_settings_field_styles=lambda: refreshed.append(True),
    )
    opened: dict = {}
    monkeypatch.setattr(
        av, "_open_altium_project_at",
        lambda win, prj, pcb, *, clean=False, force_solve=False:
            opened.update({"force_solve": force_solve}),
    )
    pcb = tmp_path / "Main.PcbDoc"
    pcb.write_text("x", encoding="utf-8")

    av._schedule_cli_altium_import(window, {
        "prjpcb_path": tmp_path / "example.PrjPcb",
        "pcbdoc_path": pcb,
        "mesh_min_angle_deg": 25.0,
        "force_solve": True,
    })
    assert window._solve_settings.mesh_min_angle_deg == 25.0
    assert edit.text_value == "25"
    assert refreshed == [True]
    assert opened["force_solve"] is True


def test_cli_mesh_overrides_warn_when_there_is_nothing_to_apply_them_to(
        monkeypatch, tmp_path, caplog):
    """The guard used to drop --mesh-* silently while --clean still applied."""
    from types import SimpleNamespace

    import fypa.altium_viewer as av

    monkeypatch.setattr(
        av, "_open_altium_project_at",
        lambda *_a, **_k: None,
    )
    pcb = tmp_path / "Main.PcbDoc"
    pcb.write_text("x", encoding="utf-8")
    with caplog.at_level("WARNING"):
        av._schedule_cli_altium_import(SimpleNamespace(), {
            "prjpcb_path": tmp_path / "example.PrjPcb",
            "pcbdoc_path": pcb,
            "mesh_max_size_mm": 0.5,
        })
    assert any("Ignoring CLI mesh override" in r.message for r in caplog.records)
