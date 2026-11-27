"""The Import Gerber Files window: step navigation (Back always, Next only
when the step is OK, the step list only to steps already reached), the
stackup keeping typed values when the layers change, and the import running
on the Review step (success closes, failure and cancel stay open)."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def qapp():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("")
    return p


@pytest.fixture
def fake_run(monkeypatch):
    """Replace GerberImportRun with a stub the test drives by hand."""
    from PySide6.QtCore import QObject, Signal

    from fypa.viewer import gerber_import_wizard as W

    made = []

    class _FakeRun(QObject):
        stage_changed = Signal(str)
        substage_changed = Signal(str)
        total_stages = Signal(int)
        succeeded = Signal(object)
        failed = Signal(str)

        def __init__(self, result, pseudo, parent=None):
            super().__init__(parent)
            self.result, self.pseudo = result, pseudo
            self.started = self.cancelled = False
            made.append(self)

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

    monkeypatch.setattr(W, "GerberImportRun", _FakeRun)
    monkeypatch.setattr("fypa.viewer.session._any_gerber_import_running",
                        lambda: False)
    return made


def _wizard(tmp_path, names=("b.GTL", "b.GBL", "b.DRL")):
    from fypa.viewer.gerber_import_wizard import GerberImportWizard
    files = [_touch(tmp_path / "g" / n) for n in names]
    return GerberImportWizard(files=files), files


def _step_enabled(wiz, i) -> bool:
    from PySide6.QtCore import Qt
    return bool(wiz.step_list.item(i).flags() & Qt.ItemIsEnabled)


def test_next_blocked_until_files_valid(qapp, tmp_path):
    from fypa.viewer.gerber_import_wizard import GerberImportWizard
    wiz = GerberImportWizard()
    assert wiz._current == 0
    assert not wiz.next_btn.isEnabled()
    assert not wiz.back_btn.isEnabled()
    assert not _step_enabled(wiz, 1) and not _step_enabled(wiz, 2)

    top = _touch(tmp_path / "x.GTL")
    wiz.files_page.add_files([top])
    assert not wiz.next_btn.isEnabled()
    assert "Bottom" in wiz.message_label.text()

    wiz.files_page.add_files([_touch(tmp_path / "x.GBL")])
    assert wiz.next_btn.isEnabled()
    # Step 2 isn't reached yet, so it can't be clicked even though Next works.
    assert not _step_enabled(wiz, 1)


def test_back_and_forward_keep_files_and_stackup_values(qapp, tmp_path):
    from fypa.gerber.extract import LAYER_ID_BOTTOM, LAYER_ID_TOP
    wiz, files = _wizard(tmp_path, ("b.GTL", "b.G1", "b.GBL", "b.DRL"))
    wiz._on_next()
    assert wiz._current == 1
    assert wiz.stackup_page.layer_ids() == [LAYER_ID_TOP, 2, LAYER_ID_BOTTOM]

    # Type a value on Top, then go back and drop the inner layer.
    wiz.stackup_page._copper_spin(0).setValue(0.07)
    wiz.back_btn.click()
    assert wiz._current == 0
    assert wiz.files_page.files() == files
    # The step list now lets the user jump straight back to Stackup.
    assert _step_enabled(wiz, 1)
    from fypa.gerber.extract import LAYER_ID_IGNORE
    wiz.files_page.set_role(1, LAYER_ID_IGNORE)
    wiz._on_step_clicked(1)
    assert wiz._current == 1
    assert wiz.stackup_page.layer_ids() == [LAYER_ID_TOP, LAYER_ID_BOTTOM]
    assert wiz.stackup_page.values()[0].copper_thickness_mm == pytest.approx(
        0.07)


def test_invalid_files_lock_later_steps(qapp, tmp_path):
    from fypa.gerber.extract import LAYER_ID_IGNORE
    wiz, _files = _wizard(tmp_path)
    wiz._on_next()
    wiz._on_next()
    assert wiz._current == 2
    wiz._on_step_clicked(0)
    wiz.files_page.set_role(0, LAYER_ID_IGNORE)   # no Top any more
    assert not _step_enabled(wiz, 1) and not _step_enabled(wiz, 2)
    wiz._on_step_clicked(2)
    assert wiz._current == 0
    assert not wiz.next_btn.isEnabled()


def test_import_success_closes_with_output(qapp, tmp_path, fake_run):
    from PySide6.QtWidgets import QDialog
    wiz, files = _wizard(tmp_path)
    wiz._on_next()
    wiz._on_next()
    assert wiz.next_btn.text() == "Import"
    wiz._on_next()
    run = fake_run[-1]
    assert run.started
    assert run.pseudo.name == "g.fypa-gerber"
    assert not wiz.back_btn.isEnabled() and not wiz.next_btn.isEnabled()
    assert not _step_enabled(wiz, 0)

    run.total_stages.emit(9)
    run.stage_changed.emit("Rendering layers…")
    assert "Rendering layers" in wiz.stage_label.text()
    assert wiz.progress_bar.value() == 1

    run.succeeded.emit(("stub", {}, None, None))
    assert wiz.result() == QDialog.Accepted
    res, result, pseudo = wiz.import_output
    assert res[0] == "stub" and pseudo == run.pseudo
    assert result.drill_files == [files[2]]


def test_import_failure_stays_open_with_back(qapp, tmp_path, fake_run):
    wiz, _files = _wizard(tmp_path)
    wiz._on_next()
    wiz._on_next()
    wiz._on_next()
    fake_run[-1].failed.emit("ValueError: bad aperture")
    assert wiz.import_output is None
    assert wiz._current == 2
    assert wiz.next_btn.text() == "Try again"
    assert wiz.back_btn.isEnabled()
    assert "bad aperture" in wiz.message_label.text()
    wiz._on_next()
    assert len(fake_run) == 2 and fake_run[-1].started


def test_cancel_during_import_stays_on_review(qapp, tmp_path, fake_run):
    wiz, _files = _wizard(tmp_path)
    wiz._on_next()
    wiz._on_next()
    wiz._on_next()
    assert wiz.cancel_btn.text() == "Cancel import"
    wiz.cancel_btn.click()
    assert fake_run[-1].cancelled
    assert wiz._run is None and wiz._current == 2
    assert wiz.next_btn.text() == "Import" and wiz.next_btn.isEnabled()
    assert "cancelled" in wiz.message_label.text()


def test_close_asks_before_discarding(qapp, tmp_path, monkeypatch, fake_run):
    from PySide6.QtWidgets import QDialog, QMessageBox
    wiz, _files = _wizard(tmp_path)
    wiz.show()
    asked = []
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **k: asked.append(1) or QMessageBox.No))
    wiz.reject()
    assert asked and wiz.isVisible()

    # Closing while an import runs cancels it.
    wiz._on_next()
    wiz._on_next()
    wiz._on_next()
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.Yes))
    wiz.reject()
    assert fake_run[-1].cancelled
    assert wiz.result() == QDialog.Rejected and not wiz.isVisible()


def test_empty_window_closes_without_asking(qapp, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from fypa.viewer.gerber_import_wizard import GerberImportWizard
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: pytest.fail("asked")))
    wiz = GerberImportWizard()
    wiz.show()
    wiz.reject()
    assert not wiz.isVisible()


def test_stackup_sum_warning_is_not_blocking(qapp, tmp_path):
    wiz, _files = _wizard(tmp_path)
    wiz._on_next()
    wiz.stackup_page._dielectric_spin(0).setValue(5.0)
    assert "add up to" in wiz.message_label.text()
    assert wiz.next_btn.isEnabled()


def test_import_run_cancel_drops_result_and_stops_pool(qapp, monkeypatch):
    """GerberImportRun (shared by the window and the Recent replay): cancel
    asks the worker to stop, tears down the render pool, and nothing is
    reported afterwards; a normal finish is passed on once."""
    from PySide6.QtCore import QObject, Signal

    from fypa.viewer import gerber_import as GI

    class _FakeWorker(QObject):
        stage_changed = Signal(str)
        substage_changed = Signal(str)
        total_stages = Signal(int)
        finished_ok = Signal(object)
        failed = Signal(str)

        def __init__(self, *a, parent=None):
            super().__init__(parent)
            self.interrupted = False

        def start(self):
            pass

        def isRunning(self):  # noqa: N802 (Qt name)
            return False

        def requestInterruption(self):  # noqa: N802 (Qt name)
            self.interrupted = True

    pools = []
    monkeypatch.setattr(GI, "_GerberImportWorker", _FakeWorker)
    monkeypatch.setattr("fypa.gerber.extract.cancel_active_gerber_pool",
                        lambda: pools.append(1))

    got = []
    run = GI.GerberImportRun("result", Path("x.fypa-gerber"))
    run.succeeded.connect(got.append)
    run.failed.connect(got.append)
    run.start()
    worker = run._worker
    run.cancel()
    assert worker.interrupted and pools == [1]
    assert not run.is_running()
    worker.finished_ok.emit("late result")
    assert got == []

    run2 = GI.GerberImportRun("result", Path("x.fypa-gerber"))
    run2.succeeded.connect(got.append)
    run2.start()
    w2 = run2._worker
    w2.finished_ok.emit("res")
    w2.finished_ok.emit("again")
    assert got == ["res"]
    assert not run2.is_running()
