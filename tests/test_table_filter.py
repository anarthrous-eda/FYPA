"""Excel-style column filters (fypa.viewer.table_filter)."""
from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


def _table(qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTableWidget, QTableWidgetItem

    from fypa.viewer.table_filter import TableColumnFilter
    from fypa.viewer.tabs.messages import _MessagesSortItem

    rows = [
        ("Go", "GND", 1.5, True),
        ("Go", "+3V3", 0.25, False),
        ("Go", "GND", None, True),
        ("Go", "+5V", 3.0, True),
    ]
    t = QTableWidget(len(rows), 4)
    t.setHorizontalHeaderLabels(["", "Net", "I (A)", "Use"])
    ctrl = TableColumnFilter(t, numeric_cols=[2], skip_cols=[0])
    for r,(go, net, cur, use) in enumerate(rows):
        t.setItem(r, 0, QTableWidgetItem(go))
        t.setItem(r, 1, QTableWidgetItem(net))
        if cur is None:
            t.setItem(r, 2, QTableWidgetItem("—"))
        else:
            # Sort key on UserRole, decorated display text — the Capacitors
            # tab's pattern.
            it = _MessagesSortItem(f"~{cur:g}")
            it.setData(Qt.UserRole, cur)
            t.setItem(r, 2, it)
        chk = QTableWidgetItem("")
        chk.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
        chk.setCheckState(Qt.Checked if use else Qt.Unchecked)
        t.setItem(r, 3, chk)
    # Enabled after populating, as the tabs do — otherwise every setItem
    # re-sorts the rows out from under the loop.
    t.setSortingEnabled(True)
    t.sortByColumn(0, Qt.AscendingOrder)
    return t, ctrl


def _shown(t, ctrl):
    return [t.item(r, 1).text() for r in range(t.rowCount()) if ctrl.accepts(r)]


def test_value_exclusion_and_blanks(qapp):
    t, ctrl = _table(qapp)
    assert ctrl.cell_key(2, 2) == "(Blanks)"
    assert ctrl.cell_key(1, 3) == "Not ticked"
    fired = []
    ctrl.changed.connect(lambda: fired.append(1))
    ctrl.set_state(1, excluded={"GND"})
    assert fired and ctrl.is_active()
    assert _shown(t, ctrl) == ["+3V3", "+5V"]
    ctrl.set_state(3, excluded={"Not ticked"})
    assert _shown(t, ctrl) == ["+5V"]
    ctrl.clear_all()
    assert not ctrl.is_active() and len(_shown(t, ctrl)) == 4


def test_numeric_range_reads_sort_key_and_drops_blanks(qapp):
    t, ctrl = _table(qapp)
    assert ctrl.cell_number(0, 2) == 1.5      # from UserRole, not "~1.5"
    ctrl.set_state(2, lo=1.0)
    assert _shown(t, ctrl) == ["GND", "+5V"]  # the blank row is filtered out
    ctrl.set_state(2, lo=0.0, hi=2.0)
    assert _shown(t, ctrl) == ["GND", "+3V3"]


def test_popup_checklist_round_trip(qapp):
    from PySide6.QtCore import Qt

    from fypa.viewer.table_filter import _FilterPopup

    t, ctrl = _table(qapp)
    pop = _FilterPopup(ctrl, 1, t)
    keys = [pop._list.item(i).data(Qt.UserRole)
            for i in range(1, pop._list.count())]
    assert sorted(keys) == ["+3V3", "+5V", "GND"]
    # Search narrows the list; OK keeps only the ticked matches.
    pop._search.setText("V")
    pop._accept()
    assert _shown(t, ctrl) == ["+3V3", "+5V"]

    # Another column's filter narrows what this column lists (Excel cascade),
    # and an exclusion on an unlisted value survives the next OK.
    ctrl.set_state(1, excluded={"+3V3"})
    ctrl.set_state(3, excluded={"Not ticked"})   # hides the +3V3 row
    pop = _FilterPopup(ctrl, 1, t)
    keys = {pop._list.item(i).data(Qt.UserRole)
            for i in range(1, pop._list.count())}
    assert keys == {"GND", "+5V"}
    pop._accept()
    assert ctrl._states[1].excluded == {"+3V3"}


def test_funnel_press_does_not_sort(qapp):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    t, ctrl = _table(qapp)
    t.resize(500, 200)
    t.show()
    hdr = t.horizontalHeader()
    opened = []
    hdr.filterRequested.disconnect()
    hdr.filterRequested.connect(lambda c, _p: opened.append(c))
    before = hdr.sortIndicatorSection(), hdr.sortIndicatorOrder()
    x = hdr.sectionViewportPosition(1) + hdr.sectionSize(1) - 6
    QTest.mouseClick(hdr.viewport(), Qt.LeftButton, pos=QPoint(x, hdr.height() // 2))
    assert opened == [1]
    assert (hdr.sortIndicatorSection(), hdr.sortIndicatorOrder()) == before
    # The label still sorts.
    x = hdr.sectionViewportPosition(1) + 6
    QTest.mouseClick(hdr.viewport(), Qt.LeftButton, pos=QPoint(x, hdr.height() // 2))
    assert hdr.sortIndicatorSection() == 1
    t.close()
