"""Auto setup engine: net rules, datasheet facts, classification."""
from __future__ import annotations

from pathlib import Path

import pytest

from fypa.autosetup import candidates_for_net, suggest
from fypa.autosetup.classify import DEFAULT_SINK_CURRENT
from fypa.autosetup.datasheets import (
    DatasheetLibrary,
    extract_text,
    normalise_pn,
    parse_facts,
    pdf_support_available,
)
from fypa.autosetup.design import DesignView, Part, Pin
from fypa.autosetup.nets import (
    is_ground,
    is_supply_pin,
    looks_like_rail,
    parse_resistance,
    pin_base,
    rail_voltage,
)

# --- net rules -----------------------------------------------------------------


@pytest.mark.parametrize("name, volts", [
    ("+1V8", 1.8), ("+3V3D", 3.3), ("12P0V", 12.0), ("+12V", 12.0), ("5V", 5.0),
    ("P1V8_AUX", 1.8), ("+1V8A_O", 1.8), ("3.3V", 3.3), ("-5V", -5.0),
    ("VDD_1V0", 1.0), ("+VDD", None), ("GND", None),
    ("SW_P3V3", None), ("FB_P1V8", None), ("1V8D_REG_EN", None), ("NetR65_2", None),
    ("+3V3_SW", 3.3),   # a switched rail, not a switch node
])
def test_rail_voltage(name, volts) -> None:
    assert rail_voltage(name) == volts


@pytest.mark.parametrize("name, styles, gnd", [
    ("GND", (), True), ("AGND", (), True), ("PGND", (), True), ("GND_ISO", (), True),
    ("0V", (), True), ("SGND", (), True), ("VSS", (), True),
    ("CHASSIS", (4,), True), ("+3V3", (), False), ("GNDREF_SENSE", (), True),
    ("+1V8", (2,), False),
])
def test_is_ground(name, styles, gnd) -> None:
    assert is_ground(name, frozenset(styles)) is gnd


def test_rail_like_names() -> None:
    assert looks_like_rail("+VDD") and looks_like_rail("VBUS") and looks_like_rail("+3V3")
    assert not looks_like_rail("SW_P3V3") and not looks_like_rail("SDA")
    assert not looks_like_rail("GND")


@pytest.mark.parametrize("raw, base", [
    ("VIN_3", "VIN"), ("VCC(A)", "VCC"), ("OUT2", "OUT"), ("SW_1", "SW"),
    ("GND_10", "GND"), ("IN+", "IN+"), ("PVIN 2", "PVIN"),
])
def test_pin_base(raw, base) -> None:
    assert pin_base(raw) == base


def test_supply_pins() -> None:
    assert all(is_supply_pin(n) for n in ("VCC", "VCC(B)", "VS", "VDDIO", "AVDD_1", "V+"))
    assert not any(is_supply_pin(n) for n in ("VBUS", "IN+", "SDA", "EN", "GND"))


@pytest.mark.parametrize("text, ohms", [
    ("5m", 0.005), ("5mΩ", 0.005), ("5 mOhm", 0.005), ("0R005", 0.005),
    ("0.1", 0.1), ("10k", 1e4), ("4k7", 4700), ("0R", 0.0), ("1M", None), ("abc", None),
])
def test_parse_resistance(text, ohms) -> None:
    got = parse_resistance(text)
    assert got == (pytest.approx(ohms) if ohms is not None else None)


# --- datasheet facts -------------------------------------------------------------

BUCK = """TPSX 3-V to 17-V, 3-A Step-Down Converter
1 Features
• Input voltage range: 3 V to 17 V
• Up to 95% efficiency
• Quiescent current of 17 µA (typical)
2 Applications
• Standard 12-V rail supplies"""

LDO = """TLVX 500mA, Low-IQ, Low-Dropout Regulator
1 Features
• Input voltage range: 1.45V to 5.5V
• Low IQ: 25μA (typical)
• Fixed output voltages of 1.8 V
2 Applications
• DC/DC converter post-regulation"""

MONITOR = """INAX 85V Power Monitor With I2C Interface
1 Features
• Operational current: 640µA (typical)
2 Applications
• DC/DC converters and power inverters"""

SWITCH = """TPSX 5.5-V, 2-A, 52-mΩ On-Resistance Load Switch
1 Features
• Integrated Single Channel Load Switch
• On-Resistance (RON) – RON = 52 mΩ (typical)"""

BEAD = """BLMX Chip Ferrite Bead
Impedance 120 Ω at 100 MHz, DC resistance (max.) 25 mΩ, rated current 3 A"""


def test_buck_facts() -> None:
    f = parse_facts(BUCK, Path("x.pdf"))
    assert f.kind.value == "SMPS" and f.topology.value == "BUCK"
    assert f.efficiency.value == pytest.approx(0.95)
    assert f.vin_range.value == (3.0, 17.0)
    assert f.quiescent.value == pytest.approx(17e-6)
    assert "95%" in f.efficiency.evidence


def test_ldo_facts_ignore_the_applications_list() -> None:
    f = parse_facts(LDO, Path("x.pdf"))
    assert f.kind.value == "LDO" and f.topology is None
    assert f.quiescent.value == pytest.approx(25e-6)
    assert f.vout_fixed.value == 1.8


def test_monitor_is_not_a_regulator() -> None:
    f = parse_facts(MONITOR, Path("x.pdf"))
    assert f.kind is None and f.is_regulator is None
    assert f.supply_current.value == pytest.approx(640e-6)


def test_switch_and_bead_resistance() -> None:
    s = parse_facts(SWITCH, Path("x.pdf"))
    assert s.is_switch.value and s.resistance.value == pytest.approx(0.052)
    b = parse_facts(BEAD, Path("x.pdf"))
    assert b.resistance.value == pytest.approx(0.025)


def test_library_matches_part_numbers_to_files(tmp_path) -> None:
    for name in ("ina238.pdf", "INA2380.pdf", "TPS62130_datasheet.pdf", "notes.txt"):
        (tmp_path / name).write_bytes(b"%PDF-1.4\n")
    lib = DatasheetLibrary(tmp_path, text_reader=lambda p: "")
    assert len(lib) == 3
    assert lib.find(["INA238AQDGSRQ1"]).name == "ina238.pdf"
    assert lib.find(["TPS62130RGTR"]).name == "TPS62130_datasheet.pdf"
    assert lib.find(["LM1117"]) is None
    assert normalise_pn("MAX38913AATD+") == "MAX38913AATD"


def test_library_falls_back_to_first_page_text(tmp_path) -> None:
    (tmp_path / "datasheet_42.pdf").write_bytes(b"%PDF-1.4\n")
    lib = DatasheetLibrary(tmp_path, text_reader=lambda p: "MPM3606A 21V, 0.6A Power Module")
    assert lib.find(["MPM3606AGQV-P"]).name == "datasheet_42.pdf"


def _minimal_pdf(lines: list[str]) -> bytes:
    """A one-page PDF with real text (Helvetica), xref offsets correct."""
    content = "BT /F1 10 Tf 40 760 Td 14 TL " + " ".join(
        f"({ln}) Tj T*" for ln in lines) + " ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        "/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    return out


@pytest.mark.skipif(not pdf_support_available(), reason="pypdf not installed")
def test_real_pdf_text_extraction(tmp_path) -> None:
    pdf = tmp_path / "TPSX.pdf"
    pdf.write_bytes(_minimal_pdf([
        "TPSX 3-V to 17-V Step-Down Converter", "Up to 93% efficiency"]))
    text = extract_text(pdf)
    assert "Step-Down Converter" in text
    f = parse_facts(text, pdf)
    assert f.kind.value == "SMPS" and f.efficiency.value == pytest.approx(0.93)


def test_unreadable_pdf_gives_no_text(tmp_path) -> None:
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    assert extract_text(bad) == ""


def _encrypt(pdf: bytes, user_password: str) -> bytes:
    import io

    from pypdf import PdfReader, PdfWriter

    w = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf)))
    w.encrypt(user_password=user_password, owner_password="owner", algorithm="AES-256")
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


@pytest.mark.skipif(not pdf_support_available(), reason="pypdf not installed")
def test_aes_encrypted_datasheet_with_no_user_password_reads(tmp_path) -> None:
    # Vendor datasheets are often AES-encrypted against editing only.
    pdf = tmp_path / "SEAM.pdf"
    pdf.write_bytes(_encrypt(_minimal_pdf(["SEAM Open Pin Field Array"]), ""))
    assert "Open Pin Field" in extract_text(pdf)


@pytest.mark.skipif(not pdf_support_available(), reason="pypdf not installed")
def test_unreadable_datasheets_are_reported_not_raised(tmp_path) -> None:
    (tmp_path / "TPS62130.pdf").write_bytes(
        _encrypt(_minimal_pdf(["TPS62130 Step-Down Converter"]), "secret"))
    (tmp_path / "STM32.pdf").write_bytes(b"not a pdf")
    res = suggest(_board(), datasheets=DatasheetLibrary(tmp_path))
    note = next(n for n in res.notes if "Couldn't read" in n)
    assert "TPS62130.pdf (password protected)" in note
    assert "STM32.pdf (damaged or not a PDF" in note
    # The parts still get suggestions, from the design alone.
    (buck,) = _by(res, "REGULATOR", "U1")
    assert buck.regulator_type == "SMPS" and "efficiency" in buck.estimated


# --- wizard value entry ---------------------------------------------------------


@pytest.mark.parametrize("text, value", [
    ("640u", 640e-6), ("640 µA", 640e-6), ("0.64m", 0.64e-3), ("1.8", 1.8),
    ("1.8 V", 1.8), ("10 mΩ", 0.01), ("2,5", 2.5), ("12V", 12.0), ("abc", None), ("", None),
])
def test_wizard_parses_typed_values(text, value) -> None:
    from fypa.viewer.autosetup_dialog import parse_value

    got = parse_value(text)
    assert got == (pytest.approx(value) if value is not None else None)


def test_wizard_formats_values_round_trip() -> None:
    from fypa.viewer.autosetup_dialog import format_value, parse_value

    assert format_value(640e-6, "current") == "640 µA"
    assert format_value(0.01, "resistance") == "10 mΩ"
    assert format_value(3.3, "voltage") == "3.3 V"
    assert format_value(None, "voltage") == "—"
    for v, f in ((640e-6, "current"), (0.01, "resistance"), (1.8, "voltage")):
        assert parse_value(format_value(v, f)) == pytest.approx(v)


# --- classification ---------------------------------------------------------------


def _part(des: str, pins: list[tuple[str, str, str]], **params: str) -> Part:
    return Part(designator=des, logical=des, params=dict(params),
                pins=[Pin(pad=p, name=n, net=net) for p, n, net in pins])


def _board(**extra) -> DesignView:
    """J1 brings 12 V in; U1 bucks it to 3.3 V through L1; U2 is an LDO to
    1.8 V; U3 is an MCU on both rails; R5 senses the MCU's 3.3 V; U4 is a
    3.3 V load switch; R9 is a 0 Ω link; R7 is a pull-up."""
    parts = [
        _part("J1", [("1", "1", "+12V"), ("2", "2", "+12V"), ("3", "3", "GND")]),
        _part("U1", [("1", "VIN", "+12V"), ("2", "SW", "SW1"), ("3", "BST", "BST1"),
                     ("4", "FB", "FB1"), ("5", "GND", "GND")], Comment="TPS62130RGTR"),
        _part("L1", [("1", "1", "SW1"), ("2", "2", "+3V3")]),
        _part("U2", [("1", "IN", "+3V3"), ("2", "OUT", "+1V8"), ("3", "BYP", "BYP2"),
                     ("4", "GND", "GND")], Comment="TLV75518"),
        _part("U3", [("1", "VDD", "+3V3_MCU"), ("2", "VDDIO", "+1V8"),
                     ("3", "VSS", "GND"), ("4", "PA0", "LED")], Comment="STM32"),
        _part("R5", [("1", "1", "+3V3"), ("2", "2", "+3V3_MCU")], Value="10m"),
        _part("U4", [("1", "VIN", "+3V3"), ("2", "VOUT", "+3V3_SW"), ("3", "GND", "GND"),
                     ("4", "ON", "EN4")], Comment="TPS22918"),
        _part("U5", [("1", "VCC", "+3V3_SW"), ("2", "GND", "GND")], Comment="SENSOR"),
        _part("R9", [("1", "1", "+3V3"), ("2", "2", "+3V3_B")], Value="0R"),
        _part("R7", [("1", "1", "+3V3"), ("2", "2", "SDA")], Value="4k7"),
    ]
    nets = sorted({p.net for part in parts for p in part.pins})
    return DesignView(parts=parts, net_names=nets, **extra)


def _by(result, role: str, des: str):
    hits = [s for s in result.suggestions if s.role == role and s.designator == des]
    assert hits, f"no {role} on {des}: {[(s.role, s.designator) for s in result.suggestions]}"
    return hits


def test_board_without_datasheets() -> None:
    res = suggest(_board())
    (src,) = _by(res, "SOURCE", "J1")
    assert (src.p_net, src.n_net, src.voltage) == ("+12V", "GND", 12.0)

    (buck,) = _by(res, "REGULATOR", "U1")
    assert (buck.in_p_net, buck.p_net, buck.n_net) == ("+12V", "+3V3", "GND")
    assert buck.regulator_type == "SMPS" and buck.voltage == 3.3
    assert buck.smps_topology == "BUCK" and "efficiency" in buck.estimated
    assert any("L1" in r for r in buck.reasons)

    (ldo,) = _by(res, "REGULATOR", "U2")
    assert ldo.regulator_type == "LDO" and ldo.voltage == 1.8

    (sw,) = _by(res, "SERIES", "U4")      # same voltage in and out: load switch
    assert (sw.p_net, sw.n_net) == ("+3V3", "+3V3_SW")

    (sense,) = _by(res, "SERIES", "R5")
    assert sense.resistance == pytest.approx(0.01)

    mcu = {s.p_net: s for s in _by(res, "SINK", "U3")}
    assert set(mcu) == {"+3V3_MCU", "+1V8"}
    assert all(s.current == DEFAULT_SINK_CURRENT and "current" in s.estimated
               for s in mcu.values())
    _by(res, "SINK", "U5")

    designators = {s.designator for s in res.suggestions}
    assert not designators & {"R9", "R7", "L1"}   # 0 Ω link, pull-up, buck inductor
    assert res.notes and not res.datasheets_used
    assert all(s.accepted for s in res.suggestions)


def test_existing_setup_is_skipped_and_counts_as_a_driver() -> None:
    view = _board(configured={"U1": "REGULATOR (schematic)"}, driven={"+3V3"})
    res = suggest(view)
    assert not [s for s in res.suggestions if s.designator == "U1"]
    assert ("U1", "REGULATOR (schematic)") in res.already_configured
    # +3V3 is driven by the existing regulator, so U3's rail is fed.
    assert not any("nothing drives" in r for s in _by(res, "SINK", "U3") for r in s.reasons)


def test_rail_driven_through_an_existing_bridge_gets_no_new_source() -> None:
    parts = [
        _part("J1", [("1", "1", "VIN_RAW"), ("2", "2", "GND")]),
        _part("J2", [("1", "1", "+5V"), ("2", "2", "GND")]),
    ]
    view = DesignView(parts=parts, net_names=["VIN_RAW", "+5V", "GND"],
                      driven={"+5V"}, bridges=[("+5V", "VIN_RAW")])
    res = suggest(view)
    assert not [s for s in res.suggestions if s.role == "SOURCE"]


def test_datasheet_facts_drive_type_efficiency_and_current(tmp_path) -> None:
    for name in ("TPS62130.pdf", "STM32.pdf", "TPS22918.pdf"):
        (tmp_path / name).write_bytes(b"%PDF-1.4\n")
    texts = {
        "TPS62130.pdf": BUCK,
        "STM32.pdf": "STM32 MCU\n1 Features\n• Supply current: 12 mA (typical)",
        "TPS22918.pdf": SWITCH,
    }
    lib = DatasheetLibrary(tmp_path, text_reader=lambda p: texts[p.name])
    res = suggest(_board(), datasheets=lib)
    (buck,) = _by(res, "REGULATOR", "U1")
    assert buck.efficiency == pytest.approx(0.92) and "efficiency" not in buck.estimated
    assert buck.datasheet.name == "TPS62130.pdf"
    mcu = _by(res, "SINK", "U3")
    assert all(s.current == pytest.approx(0.006) for s in mcu)   # 12 mA over 2 rails
    (sw,) = _by(res, "SERIES", "U4")
    assert sw.resistance == pytest.approx(0.052) and "resistance" not in sw.estimated
    assert res.datasheet_matches["U1"].name == "TPS62130.pdf"
    assert "U2" in res.without_datasheet


def test_regulator_without_output_voltage_is_not_preaccepted() -> None:
    view = _board()
    u2 = view.part("U2")
    for p in u2.pins:
        if p.net == "+1V8":
            p.net = "VCORE"
    res = suggest(view)
    (ldo,) = _by(res, "REGULATOR", "U2")
    assert ldo.voltage is None and "voltage" in ldo.missing()
    assert not ldo.accepted


def test_suggestion_becomes_an_editor_directive() -> None:
    res = suggest(_board())
    (buck,) = _by(res, "REGULATOR", "U1")
    ed = buck.to_editor_directive()
    assert (ed.role, ed.designator, ed.p_net, ed.in_p_net, ed.n_net, ed.in_n_net) == (
        "REGULATOR", "U1", "+3V3", "+12V", "GND", "GND")
    assert ed.voltage == 3.3 and ed.regulator_type == "SMPS"
    assert ed.efficiency == pytest.approx(0.9) and ed.origin == "autosetup"
    (sink,) = [s for s in _by(res, "SINK", "U3") if s.p_net == "+1V8"]
    sed = sink.to_editor_directive()
    assert sed.role == "SINK" and sed.current == DEFAULT_SINK_CURRENT
    assert not sed.single_net and sed.n_net == "GND" and sed.voltage is None


def test_candidates_for_net_put_classifier_picks_first() -> None:
    view = _board()
    res = suggest(view)
    # No classifier sink sits on +3V3 itself: ICs on the net come first.
    cands = candidates_for_net(view, res, "+3V3", "SINK")
    assert [c.designator for c in cands[:2]] == ["U2", "U4"]
    assert {c.designator for c in cands} >= {"L1", "R5", "R7", "R9"}
    src = candidates_for_net(view, None, "+12V", "SOURCE")
    assert src[0].designator == "J1" and src[0].voltage == 12.0
    reg = candidates_for_net(view, res, "+3V3", "REGULATOR")
    assert reg[0].designator == "U1" and reg[0].confidence > 0.5
