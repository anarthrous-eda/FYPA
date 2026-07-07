"""Unit tests for Altium import worker option selection."""

from fypa.altium_viewer import _altium_import_worker_options


def test_clean_import_respects_disabled_auto_solve():
    opts = _altium_import_worker_options(
        "board.PrjPcb", clean=True, auto_solve=False,
    )
    assert opts["use_design_cache"] is False
    assert opts["try_solve_cache_first"] is False
    assert opts["load_only"] is True


def test_clean_import_solves_when_auto_solve_enabled():
    opts = _altium_import_worker_options(
        "board.PrjPcb", clean=True, auto_solve=True,
    )
    assert opts["use_design_cache"] is False
    assert opts["try_solve_cache_first"] is False
    assert opts["load_only"] is False


def test_normal_import_load_only_when_auto_solve_disabled():
    opts = _altium_import_worker_options(
        "board.PrjPcb", clean=False, auto_solve=False,
    )
    assert opts["use_design_cache"] is True
    assert opts["try_solve_cache_first"] is False
    assert opts["load_only"] is True


def test_normal_import_solves_when_auto_solve_enabled():
    opts = _altium_import_worker_options(
        "board.PrjPcb", clean=False, auto_solve=True,
    )
    assert opts["use_design_cache"] is True
    assert opts["try_solve_cache_first"] is True
    assert opts["load_only"] is False
