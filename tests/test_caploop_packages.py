"""SMD package library and footprint classification (fypa.caploop.packages)."""
from __future__ import annotations

import pytest

from fypa.caploop.packages import (
    DEFAULT_PACKAGE_MODELS,
    PackageLibrary,
    detect_package,
    format_package_label,
    normalize_footprint_convention,
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
    # Bare metric codes with Altium-style suffix letters.
    ("1005B", "0402"),
    ("1608C", "0603"),
    ("1005R", "0402"),
    ("1608C-HD", "0603"),
])
def test_detect_package(footprint, expected):
    assert detect_package(footprint) == expected


@pytest.mark.parametrize("footprint,expected", [
    ("0603", "0201"),
    ("0402", "01005"),
])
def test_detect_package_metric_convention(footprint, expected):
    assert detect_package(footprint, convention="metric") == expected


@pytest.mark.parametrize("footprint,expected", [
    ("C_0603_SL", "0201"),
    ("C_0402_SL", "01005"),
    ("C0603", "0201"),
    ("CAP_0603", "0201"),
])
def test_explicit_convention_beats_the_imperial_name_heuristic(
        footprint, expected):
    """The marker heuristic exists to GUESS which convention a library uses.
    An explicit selection answers that, so it wins — testing the marker first
    left Metric with no effect on the commonest library name shapes."""
    assert detect_package(footprint, convention="metric") == expected


@pytest.mark.parametrize("footprint,expected", [
    ("C_0603_SL", "0603"),
    ("C_0402_SL", "0402"),
    ("C0603", "0603"),
])
def test_auto_still_reads_marked_names_as_imperial(footprint, expected):
    """Auto is the default and keeps the heuristic, so nobody who leaves the
    setting alone sees a change."""
    assert detect_package(footprint, convention="auto") == expected


@pytest.mark.parametrize("footprint", ["1608", "1005B", "3216"])
def test_imperial_convention_rejects_metric_only_codes(footprint):
    """There is no imperial 1608 or 3216 chip package, so under a declared
    imperial library the token is not a case code at all. Without this,
    'imperial' was indistinguishable from 'auto' on every input."""
    assert detect_package(footprint, convention="imperial") is None
    assert detect_package(footprint, convention="auto") is not None


@pytest.mark.parametrize("footprint", [
    "TANT_3216_A", "POLY_3216", "ELEC_3216", "ALUM_3216",
])
def test_non_chip_parts_stay_unsupported(footprint):
    """EIA tantalum A-case 3216 collides with metric chip 3216. Accepting it
    gave the part 1206 MLCC parasitics — two orders of magnitude off its real
    ESR — where the module contract says it must report as unsupported."""
    for conv in ("auto", "metric", "imperial"):
        assert detect_package(footprint, convention=conv) is None


def test_a_named_imperial_package_beats_an_incidental_metric_token():
    """C_0805_3216 names its land size; 3216 is a cross-reference in the name.
    Ranking metric-only tokens higher resolved it to 1206."""
    for conv in ("auto", "metric", "imperial"):
        assert detect_package("C_0805_3216", convention=conv) == "0805"


@pytest.mark.parametrize("footprint", ["C0402_SL", "C0402-SL", "C0603_X",
                                       "C0603-X"])
def test_separator_does_not_change_classification(footprint):
    """A word-boundary cannot match before an underscore, so the two
    spellings of one land pattern classified as different body sizes."""
    assert (detect_package(footprint, convention="auto")
            == detect_package(footprint.replace("_", "-"),
                              convention="auto"))


@pytest.mark.parametrize("footprint", ["CAPC1608X90N", "C_0402_1005Metric"])
def test_unambiguous_names_are_convention_independent(footprint):
    """A name that already states its units must not be overridden by the
    convention setting."""
    results = {detect_package(footprint, convention=c)
               for c in ("auto", "metric", "imperial")}
    assert len(results) == 1 and None not in results


def test_unambiguous_metric_code_wins_over_bare_ambiguous_imperial():
    """Bare 0603 without a land-pattern marker loses to 1005."""
    assert detect_package("0603_1005") == "0402"


@pytest.mark.parametrize("footprint,expected", [
    ("C_0603_1005", "0603"),
    ("C_0402_1005", "0402"),
])
def test_explicit_imperial_marker_wins_over_metric_code(footprint, expected):
    assert detect_package(footprint) == expected


def test_format_package_label():
    assert format_package_label("0402", "imperial") == "0402"
    assert format_package_label("0402", "metric") == "1005"
    assert format_package_label("0402", "auto") == "0402"
    assert format_package_label(None, "metric") == "—"


def test_normalize_footprint_convention():
    assert normalize_footprint_convention("metric") == "metric"
    assert normalize_footprint_convention("bogus") == "auto"


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
