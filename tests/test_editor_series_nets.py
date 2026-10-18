"""Editor-mode SERIES N-net picker (issue #58).

A SERIES element bridges two of its own component's pads, so its N picker
must offer only that component's pin nets — not every net on the board,
which buried the right one among unrelated and auto-minted ``$NET_xxx``
names. SOURCE / SINK keep the full list: their N terminal can sit on
another part (PDN_N_DES).

The pickers are real QComboBoxes on a stubbed
:class:`~fypa.altium_viewer.PdnViewer` (its ``__init__`` opens a whole
board).
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

BOARD_NETS = ["$NET_001", "+12V", "GND", "VAC_OUT", "VIN"]


@pytest.fixture(scope="module")
def qapp():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def viewer(qapp):
    from PySide6.QtWidgets import QComboBox, QMainWindow
    import fypa.altium_viewer as V

    v = V.PdnViewer.__new__(V.PdnViewer)
    QMainWindow.__init__(v)
    v._all_net_names = lambda: list(BOARD_NETS)
    v._editor_selection = {"kind": "component", "designator": "R5",
                           "nets": ["VIN", "VAC_OUT"]}
    v._ef_pnet = QComboBox()
    v._ef_pnet.addItems(["VIN", "VAC_OUT"])
    v._ef_nnet = QComboBox()
    v._ef_nnet.addItems(BOARD_NETS)
    v._ef_nnet_keep = None
    return v


def _items(combo) -> list[str]:
    return [combo.itemText(i) for i in range(combo.count())]


def test_series_offers_only_component_nets(viewer):
    viewer._refresh_nnet_items("SERIES")
    assert _items(viewer._ef_nnet) == ["VIN", "VAC_OUT"]


def test_series_defaults_n_to_the_other_pin_net(viewer):
    viewer._ef_pnet.setCurrentText("VIN")
    viewer._refresh_nnet_items("SERIES")
    assert viewer._ef_nnet.currentText() == "VAC_OUT"


def test_sink_keeps_every_board_net(viewer):
    viewer._refresh_nnet_items("SERIES")
    viewer._refresh_nnet_items("SINK")
    assert _items(viewer._ef_nnet) == BOARD_NETS


def test_series_keeps_a_valid_current_pick(viewer):
    viewer._ef_nnet.setCurrentText("VAC_OUT")
    viewer._refresh_nnet_items("SERIES")
    assert viewer._ef_nnet.currentText() == "VAC_OUT"


def test_loaded_directive_n_net_survives_the_filter(viewer):
    # A saved SERIES whose N net is not one of the pin nets (e.g. written
    # before this filter) must not be silently swapped on the next Apply.
    viewer._ef_nnet.setCurrentText("GND")
    viewer._ef_nnet_keep = "GND"
    viewer._refresh_nnet_items("SERIES")
    assert viewer._ef_nnet.currentText() == "GND"


def test_no_net_sentinel_is_not_offered(viewer):
    viewer._editor_selection["nets"] = ["VIN", "(none)", "GND"]
    viewer._refresh_nnet_items("SERIES")
    assert _items(viewer._ef_nnet) == ["VIN", "GND"]
