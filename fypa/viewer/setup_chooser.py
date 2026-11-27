"""The "Set up sources and sinks" chooser.

Opens when a design loads with no PDN setup (no SOURCE / REGULATOR), and
from Help > Set up sources and sinks…. It lays out the four ways to define
where power enters the board and where it is drawn, says when each one fits,
and each card's button takes the user straight into that route:

* **Define in Altium / KiCad** - ``PDN_*`` parameters (KiCad: symbol fields)
  in the schematic. Opens a short how-to page inside the dialog, worded for the
  design's tool (the user has to leave FYPA for this one).
* **Place on the board** - the Board tab's editor mode.
* **Auto setup** - the Topology tab's Auto setup wizard.
* **Manual setup** - the Topology tab's Manual setup tables.

A Gerber import has no schematic, so only the board route is offered there.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from fypa.viewer.theme import _T

ROUTE_ALTIUM = "altium"
ROUTE_BOARD = "board"
ROUTE_AUTO = "auto"
ROUTE_MANUAL = "manual"
# Not a card: the link under them (File > Import Setup from Another Design…).
ROUTE_IMPORT = "import"

# Full walkthrough of the schematic parameters, linked from the how-to page.
_ALTIUM_GUIDE_URL = (
    "https://github.com/anarthrous-eda/FYPA/blob/main/docs/user-guide/"
    "01-sources-and-sinks.md")
_KICAD_GUIDE_URL = (
    "https://github.com/anarthrous-eda/FYPA/blob/main/docs/user-guide/"
    "10-kicad-import.md")

_FYPA_STORE_NOTE = ("Saved beside the design (.fypa-setup.json) and restored "
                    "when it is imported again; changed parts are flagged.")
# Gerber imports have no sidecar (fypa.setup_sidecar is Altium-only).
_FYPA_STORE_NOTE_GERBER = "Saved in the FYPA project (.fypa)."

# (route, title, badge, body, store note, button label)
_CARDS = (
    (ROUTE_ALTIUM, "Define in Altium",
     "Recommended while the design is still changing",
     "Add <code>PDN_*</code> parameters to the supply, regulator and load "
     "parts in the schematic, then reload the design here.",
     "Lives in the design itself: shared with everyone who opens it and "
     "carried through every re-export.",
     "Show me how…"),
    (ROUTE_BOARD, "Place on the board",
     "Recommended for quick setups and Gerber imports",
     "Turn on <b>Edit</b> and click a part to give it a role, or drop free "
     "source / sink markers straight onto copper.",
     _FYPA_STORE_NOTE,
     "Start placing"),
    (ROUTE_AUTO, "Auto setup",
     "",
     "FYPA reads pin names, nets, part parameters and, optionally, a folder "
     "of datasheets, and proposes sources, regulators and sinks. Nothing is "
     "applied until you review it.",
     _FYPA_STORE_NOTE,
     "Run auto setup…"),
    (ROUTE_MANUAL, "Manual setup",
     "",
     "Enter every source, regulator and load in tables: pick the net, the "
     "part on it and its pins. Suits working from a power budget.",
     _FYPA_STORE_NOTE,
     "Open tables…"),
)

# Why a route is unavailable on a design with no schematic (Gerber import).
_NO_SCHEMATIC_REASON = {
    ROUTE_ALTIUM: "Needs an Altium project - this design was imported from "
                  "Gerber files.",
    ROUTE_AUTO: "Needs a schematic netlist, which Gerber files don't carry.",
    ROUTE_MANUAL: "Needs a schematic netlist, which Gerber files don't carry.",
}


class SetupChooserDialog(QDialog):
    """Four-card chooser. After ``exec()``, :attr:`route` is the picked
    route (one of the ``ROUTE_*`` constants) or ``None`` (Not now / closed),
    and :meth:`dont_show_again` says whether the box was ticked."""

    def __init__(self, parent=None, *, has_schematic: bool = True,
                 on_load: bool = True, tool: str = "Altium") -> None:
        super().__init__(parent)
        # "Altium" or "KiCad": names the schematic route and words its how-to.
        self._tool = tool
        self.setWindowTitle("Set up sources and sinks")
        self.route: str | None = None
        self._has_schematic = bool(has_schematic)
        self._on_load = bool(on_load)
        self._dont_show: QCheckBox | None = None
        self.card_buttons: dict[str, QPushButton] = {}

        t = _T()
        self._stack = QStackedWidget(self)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.addWidget(self._stack, 1)
        self._stack.addWidget(self._build_choice_page(t))
        self._stack.addWidget(self._build_altium_page(t))
        self.resize(820, self.sizeHint().height())

    # --- pages ---------------------------------------------------------------

    def _build_choice_page(self, t) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        if self._on_load:
            lead = ("This design has no sources or sinks defined yet, so "
                    "there is nothing to solve.")
        else:
            lead = ("FYPA needs to know where power enters the board and "
                    "where it is drawn.")
        intro = QLabel(
            f"<h3 style='margin:0 0 4px 0;'>Set up sources and sinks</h3>"
            f"{lead} Choose how to define them - you can mix methods and "
            f"change them later.")
        intro.setTextFormat(Qt.RichText)
        intro.setWordWrap(True)
        lay.addWidget(intro)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        for i, card in enumerate(_CARDS):
            grid.addWidget(self._build_card(t, *card), i // 2, i % 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)

        if self._has_schematic:
            # A setup already made on another version of this board (or a
            # similar one) is often the quickest start of all.
            accent = t["accent"]
            imp = QLabel(
                "Set up another version of this board already? "
                f"<a href='{ROUTE_IMPORT}' style='color:{accent};'>Import its "
                "setup…</a> - each entry is matched to a part here.")
            imp.setTextFormat(Qt.RichText)
            imp.setWordWrap(True)
            imp.setStyleSheet(f"color: {t['fg_muted']};")
            imp.linkActivated.connect(self._pick)
            self.import_link = imp
            lay.addWidget(imp)

        foot = QHBoxLayout()
        if self._on_load:
            self._dont_show = QCheckBox(
                "Don't show this again (Settings > General options)")
            self._dont_show.setToolTip(
                "Help > Set up sources and sinks… opens this either way.")
            foot.addWidget(self._dont_show)
        foot.addStretch(1)
        not_now = QPushButton("Not now" if self._on_load else "Close")
        not_now.clicked.connect(self.reject)
        foot.addWidget(not_now)
        lay.addLayout(foot)
        return page

    def _build_card(self, t, route, title, badge, body, store,
                    button) -> QFrame:
        reason = None if self._has_schematic else _NO_SCHEMATIC_REASON.get(route)
        if route == ROUTE_ALTIUM and self._has_schematic:
            title = f"Define in {self._tool}"
        # Without a schematic the board is the only route, so it is *the*
        # recommendation rather than one of two.
        if not self._has_schematic:
            badge = ("Recommended for this design" if route == ROUTE_BOARD
                     else "")
            if store == _FYPA_STORE_NOTE:
                store = _FYPA_STORE_NOTE_GERBER

        frame = QFrame()
        frame.setObjectName("setupCard")
        frame.setStyleSheet(
            f"QFrame#setupCard {{ background-color: {t['bg_alt']};"
            f" border: 1px solid {t['border']}; border-radius: 8px; }}"
            f"QFrame#setupCard QLabel {{ background: transparent; }}")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(6)

        dim = t["fg_dim"]
        head = QLabel(f"<b style='font-size:11pt;'>{title}</b>")
        head.setTextFormat(Qt.RichText)
        if reason:
            head.setStyleSheet(f"color: {dim};")
        lay.addWidget(head)
        if badge:
            tag = QLabel(badge.upper())
            tag.setStyleSheet(
                f"color: {t['ok']}; font-size: 8pt; font-weight: 600;"
                " letter-spacing: 1px;")
            lay.addWidget(tag)

        text = QLabel(body)
        text.setTextFormat(Qt.RichText)
        text.setWordWrap(True)
        text.setStyleSheet(f"color: {dim if reason else t['fg_label']};")
        lay.addWidget(text)

        note = QLabel(reason or store)
        note.setWordWrap(True)
        note.setStyleSheet(
            f"color: {t['warn'] if reason else t['fg_muted']};"
            " font-size: 8.5pt;")
        lay.addWidget(note)
        lay.addStretch(1)

        btn = QPushButton(button)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setEnabled(reason is None)
        btn.clicked.connect(lambda _c=False, r=route: self._pick(r))
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(btn)
        lay.addLayout(row)
        self.card_buttons[route] = btn
        return frame

    def _build_altium_page(self, t) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        code = t["code"]

        def c(s: str) -> str:
            return f"<code style='color:{code};'>{s}</code>"

        def table(rows) -> str:
            cells = "".join(
                f"<tr><td style='padding:1px 14px 1px 0;'>{c(k)}</td>"
                f"<td style='padding:1px 0;'>{c(v)}</td></tr>"
                for k, v in rows)
            return f"<table style='margin:2px 0 8px 18px;'>{cells}</table>"

        kicad = self._tool == "KiCad"
        if kicad:
            lead = ("Add these as fields to each symbol in the schematic "
                    "(Symbol Properties, <b>+</b> to add a field - untick "
                    "<i>Show</i> to keep them off the sheet; or on the library "
                    "symbol for parts you reuse). Net names must match the "
                    "board's net names exactly.")
            back_in = ("<p style='margin:2px 0;'><b>4. Back in FYPA</b> - in "
                       "KiCad choose <b>Tools &gt; Update PCB from Schematic"
                       "</b> (F8) so the fields reach the board, save the "
                       "board, then choose <b>File &gt; Reload Design</b> "
                       "(Ctrl+R) here.</p>")
            tip = (f"<p style='margin:10px 0 2px 0; color:{t['fg_muted']};'>"
                   "KiCad import is in beta: check the copper on the Board "
                   "tab against KiCad, and fill all zones before saving.</p>")
            guide = _KICAD_GUIDE_URL
        else:
            lead = ("Add these parameters to each part in the schematic (on "
                    "the placed part, or on the library symbol for parts you "
                    "reuse). Net names must match the schematic exactly.")
            back_in = ("<p style='margin:2px 0;'><b>4. Back in FYPA</b> - save "
                       "and recompile in Altium, then choose <b>File &gt; "
                       "Reload Design</b> (Ctrl+R).</p>")
            tip = (f"<p style='margin:10px 0 2px 0; color:{t['fg_muted']};'>"
                   f"Tip: the {c('FYPA_PdnParams')} script in FYPA's "
                   f"{c('altium_scripts')} folder adds these from a dialog "
                   "inside Altium and checks the values before writing them."
                   "</p>")
            guide = _ALTIUM_GUIDE_URL
        how = QLabel(
            "<h3 style='margin:0 0 6px 0;'>Define sources and sinks in "
            f"{self._tool}</h3>"
            + lead
            + "<p style='margin:10px 0 2px 0;'><b>1. Where power enters</b> - "
            "a connector, battery or bench supply input:</p>"
            + table([("PDN_ROLE", "SOURCE"), ("PDN_V", "5V"),
                     ("PDN_P_NET", "+5V"), ("PDN_N_NET", "GND")])
            + "<p style='margin:2px 0;'><b>2. Each load</b> - an IC, module "
            "or LED string:</p>"
            + table([("PDN_ROLE", "SINK"), ("PDN_I", "500mA"),
                     ("PDN_P_NET", "+3V3"), ("PDN_N_NET", "GND")])
            + "<p style='margin:2px 0;'><b>3. Each regulator</b> - an LDO or "
            "switcher between two rails:</p>"
            + table([("PDN_ROLE", "REGULATOR"), ("PDN_V", "3.3"),
                     ("PDN_REGULATOR_TYPE", "LDO or SMPS"),
                     ("PDN_OUT_P_NET / PDN_OUT_N_NET", "+3V3 / GND"),
                     ("PDN_IN_P_NET / PDN_IN_N_NET", "+5V / GND")])
            + back_in + tip
            + f"<p style='margin:6px 0 0 0;'><a href='{guide}' "
            f"style='color:{t['accent']};'>Full guide: pin overrides, "
            "multi-channel parts, series elements…</a></p>")
        how.setTextFormat(Qt.RichText)
        how.setWordWrap(True)
        how.setOpenExternalLinks(True)
        how.setTextInteractionFlags(
            Qt.TextBrowserInteraction | Qt.TextSelectableByMouse)
        lay.addWidget(how)
        lay.addStretch(1)

        row = QHBoxLayout()
        back = QPushButton("◀  Back")
        back.clicked.connect(lambda: self._stack.setCurrentIndex(0))
        row.addWidget(back)
        row.addStretch(1)
        close = QPushButton("Close")
        close.setDefault(True)
        # Picking the Altium route only explains it: nothing to run in FYPA.
        close.clicked.connect(self.reject)
        row.addWidget(close)
        lay.addLayout(row)
        return page

    # --- behaviour -----------------------------------------------------------

    def _pick(self, route: str) -> None:
        if route == ROUTE_ALTIUM:
            self._stack.setCurrentIndex(1)
            return
        self.route = route
        self.accept()

    def showing_altium_help(self) -> bool:
        return self._stack.currentIndex() == 1

    def dont_show_again(self) -> bool:
        return self._dont_show is not None and self._dont_show.isChecked()


def _has_schematic(window) -> bool:
    meta = getattr(window, "metadata", None) or {}
    return meta.get("source_kind") != "gerber"


def run_setup_route(window, route: str | None) -> None:
    """Take ``window`` (a viewer) into the picked route."""
    tabs = getattr(window, "tabs", None)

    def _show_tab(attr: str) -> None:
        idx = getattr(window, attr, None)
        if tabs is not None and idx is not None and idx >= 0:
            tabs.setCurrentIndex(idx)

    if route == ROUTE_BOARD:
        _show_tab("_heatmap_tab_index")
        btn = getattr(window, "_editor_toggle_btn", None)
        if btn is not None and not btn.isChecked():
            btn.setChecked(True)
        window.statusBar().showMessage(
            "Editor mode: click a part to give it a PDN role, or press S / L "
            "to drop a free SOURCE / SINK on copper. Press Solve when done.",
            20000)
    elif route == ROUTE_AUTO:
        _show_tab("_topology_tab_index")
        window._topology_run_autosetup()
    elif route == ROUTE_MANUAL:
        _show_tab("_topology_tab_index")
        window._topology_run_manual_setup()
    elif route == ROUTE_IMPORT:
        from fypa.viewer.setup_import import import_setup_from_design
        import_setup_from_design(window)


def open_setup_chooser(window, *, on_load: bool = False) -> None:
    """Show the chooser over ``window`` and run the picked route. With
    ``on_load`` it is the automatic prompt for an unconfigured design: it
    offers "Don't show this again" and honours the saved preference."""
    from fypa.viewer.prefs import save_show_setup_chooser

    from fypa.design_source import tool_name
    dlg = SetupChooserDialog(window, has_schematic=_has_schematic(window),
                             on_load=on_load,
                             tool=tool_name(getattr(window, "metadata", None)))
    dlg.exec()
    if dlg.dont_show_again():
        save_show_setup_chooser(False)
        sync = getattr(window, "_settings_setup_chooser_check", None)
        if sync is not None:
            sync.setChecked(False)
    run_setup_route(window, dlg.route)


__all__ = [
    "ROUTE_ALTIUM", "ROUTE_AUTO", "ROUTE_BOARD", "ROUTE_IMPORT", "ROUTE_MANUAL",
    "SetupChooserDialog", "open_setup_chooser", "run_setup_route",
]
