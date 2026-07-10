"""SMD package library and footprint classification (fypa.caploop.packages)."""
from __future__ import annotations

import pytest

from fypa.caploop.packages import (
    DEFAULT_PACKAGE_MODELS,
    PackageLibrary,
    detect_package,
)


@pytest.mark.parametrize("footprint,expected", [
    # Bare imperial codes — the common Altium convention, incl. the real
    # footprints on ExampleDesigns/Imperial.
    ("C_0402_SL", "0402"),
    ("C_0603_SL", "0603"),
    ("C_1210_SL", "1210"),
    ("0805", "0805"),
    ("CAP0201", "0201"),
    ("C01005", "01005"),
    # Metric spelled out (KiCad).
    ("C_0402_1005Metric", "0402"),
    ("C_0201_0603Metric", "0201"),
    # IPC-7351 land names: the digits are always metric.
    ("CAPC1608X90N", "0603"),
    ("CAPC1005X55N", "0402"),
    # Reverse geometry.
    ("C_0306_SL", "0306"),
])
def test_detect_package(footprint, expected):
    assert detect_package(footprint) == expected


def test_metric_and_imperial_0603_are_disambiguated():
    """0603 is imperial 0603 *and* the metric code for an 0201. The metric
    reading is taken only when the footprint says so."""
    assert detect_package("C_0603_SL") == "0603"
    assert detect_package("C_0201_0603Metric") == "0201"
    assert detect_package("CAPC0603X33N") == "0201"


@pytest.mark.parametrize("footprint", [
    "FP-TCJD-MFG",        # tantalum D case, from ExampleDesigns/Imperial
    "CAP_RADIAL_5MM",
    "ELEC_THT_8x10",
    "",
])
def test_non_smd_footprints_are_unclassified(footprint):
    assert detect_package(footprint) is None


def test_defaults_are_monotonic_in_body_size():
    """ESL grows and ESR falls as the body gets longer — if a default is ever
    edited into an implausible value this catches it."""
    order = ["01005", "0201", "0402", "0603", "0805", "1206", "1210"]
    esls = [DEFAULT_PACKAGE_MODELS[p].esl_h for p in order]
    esrs = [DEFAULT_PACKAGE_MODELS[p].esr_ohm for p in order]
    assert esls == sorted(esls)
    assert esrs == sorted(esrs, reverse=True)


def test_default_values_are_in_a_plausible_band():
    for model in DEFAULT_PACKAGE_MODELS.values():
        assert 0.1e-9 <= model.esl_h <= 2.0e-9
        assert 1e-3 <= model.esr_ohm <= 100e-3


def test_reverse_geometry_beats_its_standard_equivalent():
    # 0306 is an 0603 rotated: terminations on the long edges, so lower ESL.
    assert DEFAULT_PACKAGE_MODELS["0306"].esl_h < \
        DEFAULT_PACKAGE_MODELS["0603"].esl_h


# --- the editable library ------------------------------------------------------


def test_library_starts_from_the_defaults():
    lib = PackageLibrary()
    assert lib.get("0402") == DEFAULT_PACKAGE_MODELS["0402"]
    assert lib.get(None) is None
    assert lib.get("D-CASE") is None
    assert lib.is_default("0402")


def test_library_edits_and_resets():
    lib = PackageLibrary()
    lib.set_values("0402", 0.9e-9, 50e-3)
    assert lib.get("0402").esl_h == pytest.approx(0.9e-9)
    assert not lib.is_default("0402")
    lib.reset()
    assert lib.is_default("0402")


def test_library_rejects_unknown_packages():
    with pytest.raises(KeyError):
        PackageLibrary().set_values("D-CASE", 1e-9, 1e-3)


def test_library_persists_only_user_edits():
    """A project that never touched a package must inherit later revisions of
    the built-in default rather than freezing today's value."""
    lib = PackageLibrary()
    assert lib.to_dict() == {}
    lib.set_values("0603", 0.75e-9, 18e-3)
    assert set(lib.to_dict()) == {"0603"}

    restored = PackageLibrary.from_dict(lib.to_dict())
    assert restored.get("0603").esl_h == pytest.approx(0.75e-9)
    assert restored.get("0603").esr_ohm == pytest.approx(18e-3)
    assert restored.is_default("0402")


def test_library_from_dict_ignores_junk():
    lib = PackageLibrary.from_dict({
        "0402": {"esl_h": "not a number", "esr_ohm": 1e-3},
        "NOSUCH": {"esl_h": 1e-9, "esr_ohm": 1e-3},
        "0603": {"esl_h": 0.5e-9},          # missing esr
    })
    assert lib.is_default("0402") and lib.is_default("0603")


def test_library_iterates_smallest_first():
    names = [m.name for m in PackageLibrary()]
    assert names[0] == "01005"
    assert names.index("0402") < names.index("1206")
