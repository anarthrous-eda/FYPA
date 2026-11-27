"""Global Focus: pick one rail and every tab shows only that rail.

The control sits on the menu bar, above the Nodes tab (see
:meth:`_GlobalFocusMixin._position_global_focus_chip`)::

    File  View  Reports  Help        (eye) GLOBAL FOCUS  +3V3  x

Idle it reads "GLOBAL FOCUS  Off" in a dim gold and opens a rail picker;
active it shows the rail in gold with an x that exits. Focus is a view
filter, never a re-solve, and it is not remembered: it starts off for every
window and project, and goes when the project does.

What is "in focus" is worked out in one place, :func:`focus_scope_nets`:
for now the rail's own nets (its members plus the sections the low-ohm merge
folded into them), with no return nets and nothing upstream of a regulator.
Both are expected to become options later; only that function needs to
change for them.

Each tab asks the mixin rather than keeping its own copy:

* :meth:`_global_focus_nets` - the net names in focus, or ``None`` when off;
* :meth:`_global_focus_rail` - the focused rail's name;
* :meth:`_global_focus_allows_rail` / :meth:`_global_focus_allows_net`.

Copper hiding reuses the editor's net focus (``_editor_focus_nets``), which
already removes copper, vias, pads and markers outside a net set: see
:class:`fypa.viewer.editor.nets._EditorNetsMixin`.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable

from PySide6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QPointF,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QToolButton,
    QWidget,
)

from fypa.viewer.theme import _T

log = logging.getLogger(__name__)

CAPTION = "GLOBAL FOCUS"

# Terminals that sit on a directive's own rail (the rest are returns). A
# regulator has one on each rail it joins; a SERIES part's two sides are both
# rail copper.
_POWER_TERMINALS = {
    "SOURCE": ("P",),
    "SINK": ("P",),
    "REGULATOR": ("OUT_P", "IN_P"),
}


# --- Scope (pure; no Qt) -------------------------------------------------------

def focus_scope_nets(rail: str, rail_to_members: dict[str, list[str]],
                     merged_children: dict[str, Iterable[str]] | None = None,
                     ) -> frozenset[str]:
    """Net names that belong to ``rail`` while it is focused.

    The rail's member nets (nets joined by SERIES / bridge parts) and, for
    each, the nets the low-ohm merge folded into it - their copper is drawn
    as sections of the parent net. Return nets and rails upstream of a
    regulator are left out in this version."""
    merged = merged_children or {}
    out: set[str] = set()
    for net in rail_to_members.get(rail, [rail]):
        out.add(net)
        out.update(merged.get(net, ()))
    return frozenset(out)


def _terminal_nets(term: dict | None) -> set[str]:
    if not term or term.get("ideal_return"):
        return set()
    nets = {p.get("net") for p in term.get("pins", []) or [] if p.get("net")}
    rn = term.get("requested_net")
    if rn:
        nets.add(rn)
    return nets


def directive_power_nets(d: dict) -> set[str]:
    """Nets a metadata directive's rail-side terminals sit on."""
    terms = d.get("terminals") or {}
    names = _POWER_TERMINALS.get(str(d.get("role") or "").upper())
    if names is None:  # SERIES / RESISTOR / AUTO_BRIDGE: every side is rail
        names = tuple(terms)
    out: set[str] = set()
    for name in names:
        out |= _terminal_nets(terms.get(name))
    return out


def directive_in_focus(d: dict, nets: frozenset[str] | set[str]) -> bool:
    return bool(directive_power_nets(d) & set(nets))


def focus_metadata(md: dict | None, nets: frozenset[str] | set[str] | None
                   ) -> dict | None:
    """A shallow copy of ``md`` with only the directives, active nets and
    merged bridges that touch ``nets`` - for the tabs that draw straight from
    the metadata (Topology, Properties). ``nets`` ``None`` = no focus."""
    if md is None or nets is None:
        return md
    nets = set(nets)
    out = dict(md)
    out["directives"] = [d for d in md.get("directives") or []
                         if directive_in_focus(d, nets)]
    if "active_nets" in md:
        out["active_nets"] = [n for n in md.get("active_nets") or []
                              if n in nets]
    if "merged_bridges" in md:
        out["merged_bridges"] = [
            b for b in md.get("merged_bridges") or []
            if b.get("p_net") in nets or b.get("n_net") in nets]
    return out


def bridge_row_in_focus(row: dict, nets: frozenset[str] | set[str]) -> bool:
    """A Bridges-tab row is in focus when either side (or any other net the
    part touches) is on the focused rail."""
    touched = {row.get("net_a"), row.get("net_b"), *(row.get("nets") or ())}
    return bool(touched & set(nets))


# --- Icon ----------------------------------------------------------------------

def make_focus_eye_pixmap(size: int = 18, *, active: bool,
                          scale: float = 2.0) -> QPixmap:
    """A gold eye. Active: a soft glow behind it and a filled iris, so it
    can't be mistaken for the plain visibility eyes in the sidebar. Idle: a
    dim gold outline only. Drawn at ``scale`` for high-DPI screens."""
    t = _T()
    gold = QColor(t["focus_gold"] if active else t["focus_gold_dim"])
    px = QPixmap(round(size * scale), round(size * scale))
    px.setDevicePixelRatio(scale)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing, True)
    c = size / 2.0
    if active:
        glow = QRadialGradient(QPointF(c, c), size / 2.0)
        inner = QColor(gold)
        inner.setAlphaF(0.55)
        outer = QColor(gold)
        outer.setAlphaF(0.0)
        glow.setColorAt(0.0, inner)
        glow.setColorAt(1.0, outer)
        p.setPen(Qt.NoPen)
        p.setBrush(glow)
        p.drawEllipse(QRectF(0, 0, size, size))
    pen = QPen(gold)
    pen.setWidthF(max(1.1, size * 0.085))
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    pad = size * 0.12
    bulge = size * 0.40
    path = QPainterPath()
    path.moveTo(pad, c)
    path.quadTo(c, c - bulge, size - pad, c)
    path.quadTo(c, c + bulge, pad, c)
    p.drawPath(path)
    iris = size * 0.19
    if active:
        p.setPen(Qt.NoPen)
        p.setBrush(gold)
        p.drawEllipse(QPointF(c, c), iris, iris)
        p.setBrush(QColor(t["bg"]))
        p.drawEllipse(QPointF(c, c), iris * 0.45, iris * 0.45)
    else:
        p.drawEllipse(QPointF(c, c), iris, iris)
    p.end()
    return px


def make_close_pixmap(width: int, height: int, center_y: float, arm: float,
                      color: str, scale: float = 2.0) -> QPixmap:
    """A painted x of half-size ``arm`` centred at (width / 2, ``center_y``)
    in a ``width`` x ``height`` pixmap. The chip puts ``center_y`` on the
    middle of the rail name's capitals, so the x lines up with the letters
    rather than with the label's box (a capital sits off the box's centre,
    and a text "×" sits on the baseline, lower still)."""
    px = QPixmap(round(width * scale), round(height * scale))
    px.setDevicePixelRatio(scale)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(QColor(color))
    pen.setWidthF(max(1.3, arm * 0.38))
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    cx = width / 2.0
    p.drawLine(QPointF(cx - arm, center_y - arm), QPointF(cx + arm, center_y + arm))
    p.drawLine(QPointF(cx + arm, center_y - arm), QPointF(cx - arm, center_y + arm))
    p.end()
    return px


# --- The control ---------------------------------------------------------------

class GlobalFocusChip(QWidget):
    """Eye + "GLOBAL FOCUS" + rail name + x. Emits :attr:`pick_requested`
    for a click anywhere but the x, and :attr:`exit_requested` for the x."""

    pick_requested = Signal()
    exit_requested = Signal()

    _ICON_PX = 16
    _CLOSE_PX = 12

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("fypaGlobalFocus")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setCursor(Qt.PointingHandCursor)
        self._rail: str | None = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 2, 4, 2)
        lay.setSpacing(5)
        self._icon = QLabel(self)
        self._caption = QLabel(CAPTION, self)
        cap_font = QFont(self.font())
        cap_font.setPointSizeF(7.0)
        cap_font.setBold(True)
        cap_font.setLetterSpacing(QFont.AbsoluteSpacing, 1.0)
        self._caption.setFont(cap_font)
        self._rail_label = QLabel(self)
        rail_font = QFont(self.font())
        rail_font.setBold(True)
        self._rail_label.setFont(rail_font)
        self._close = QToolButton(self)
        self._close.setAutoRaise(True)
        # Sized and padded in restyle(): exactly the rail name's height, so
        # the x painted on the name's capital line lands level with it (the
        # app theme's button padding made it taller than the chip before).
        self._close.setFocusPolicy(Qt.NoFocus)
        self._close.setCursor(Qt.PointingHandCursor)
        self._close.setToolTip("Exit Global Focus - show every rail again")
        self._close.clicked.connect(self.exit_requested)
        for w in (self._icon, self._caption, self._rail_label):
            w.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            lay.addWidget(w, 0, Qt.AlignVCenter)
        lay.addWidget(self._close, 0, Qt.AlignVCenter)
        self.set_state(None, enabled=True)

    def rail(self) -> str | None:
        return self._rail

    def rail_text(self) -> str:
        return self._rail_label.text()

    def close_button(self) -> QToolButton:
        return self._close

    def set_state(self, rail: str | None, *, enabled: bool,
                  disabled_tip: str = "") -> None:
        self._rail = rail
        active = rail is not None
        self._rail_label.setText(rail if active else "Off")
        self._close.setVisible(active)
        self.setEnabled(enabled)
        if not enabled:
            self.setToolTip(disabled_tip)
        elif active:
            self.setToolTip(
                f"Global Focus: every tab shows only {rail}.\n"
                "Click to switch rail; × shows every rail again.")
        else:
            self.setToolTip(
                "Global Focus: pick one rail and every tab shows only that "
                "rail.")
        self.restyle()

    def restyle(self) -> None:
        t = _T()
        active = self._rail is not None
        gold = t["focus_gold"] if active else t["focus_gold_dim"]
        self._icon.setPixmap(
            make_focus_eye_pixmap(self._ICON_PX, active=active))
        self._caption.setStyleSheet(f"QLabel {{ color: {gold}; }}")
        self._rail_label.setStyleSheet(
            f"QLabel {{ color: {gold if active else t['fg_dim']}; }}")
        fm = QFontMetricsF(self._rail_label.font())
        line_h = max(1, self._rail_label.sizeHint().height())
        text_top = (line_h - fm.height()) / 2.0
        cap_mid = text_top + fm.ascent() - fm.capHeight() / 2.0
        # Measured on screen: the x reads 1 px low at the computed centre.
        cap_mid -= 1.0
        arm = max(2.5, fm.capHeight() * 0.36)
        w = self._CLOSE_PX
        icon = QIcon()
        for mode, colour in ((QIcon.Normal, gold),
                             (QIcon.Active, t["fg_strong"])):
            icon.addPixmap(make_close_pixmap(w, line_h, cap_mid, arm, colour),
                           mode)
        self._close.setIcon(icon)
        self._close.setIconSize(QSize(w, line_h))
        self._close.setFixedSize(w + 4, line_h)
        border = gold if active else "transparent"
        self.setStyleSheet(
            f"QWidget#fypaGlobalFocus {{ border: 1px solid {border};"
            f" border-radius: 3px; background: transparent; }}"
            f"QWidget#fypaGlobalFocus:hover {{ background: {t['bg_hover']}; }}"
            f"QToolButton {{ border: none; padding: 0; margin: 0;"
            f" min-width: 0; min-height: 0; background: transparent; }}")
        self.adjustSize()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt name
        if event.button() == Qt.LeftButton and self.isEnabled():
            self.pick_requested.emit()
            event.accept()
            return
        super().mousePressEvent(event)


class _ChipPositioner(QObject):
    """Re-places the chip when anything it is lined up with moves."""

    def __init__(self, callback, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._callback = callback
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self._callback)

    def schedule(self, *_args) -> None:
        self._timer.start()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt name
        if event.type() in (QEvent.Resize, QEvent.Move, QEvent.Show,
                            QEvent.LayoutRequest):
            self._timer.start()
        return False


# --- Window mixin --------------------------------------------------------------

# (combo attribute, label attribute, filter method) for the per-tab rail
# pickers Global Focus replaces. The Impedance picker has no "All rails".
_RAIL_COMBOS = (
    ("nodes_rail_combo", "nodes_rail_label", "_apply_nodes_filter"),
    ("vias_rail_combo", "vias_rail_label", "_apply_vias_filter"),
    ("caps_rail_combo", "caps_rail_label", "_apply_caps_filter"),
)
_ALL_RAILS = "All rails"


class _GlobalFocusMixin:
    """Global Focus state and the menu-bar control. See the module doc."""

    _global_focus_rail_name: str | None = None
    _global_focus_net_set: frozenset | None = None
    _global_focus_saved_eyes: dict | None = None
    _global_focus_chip: GlobalFocusChip | None = None
    _global_focus_rule: QWidget | None = None

    # --- queries ------------------------------------------------------------

    def _global_focus_active(self) -> bool:
        return self._global_focus_rail_name is not None

    def _global_focus_rail(self) -> str | None:
        return self._global_focus_rail_name

    def _global_focus_nets(self) -> frozenset | None:
        if self._global_focus_rail_name is None:
            return None
        return self._global_focus_net_set or frozenset()

    def _global_focus_allows_rail(self, rail: str | None) -> bool:
        focus = self._global_focus_rail_name
        if focus is None:
            return True
        if rail == focus:
            return True
        # Some reports name a rail by a member net rather than the group.
        return rail is not None and rail in (self._global_focus_net_set or ())

    def _global_focus_allows_net(self, net: str | None) -> bool:
        nets = self._global_focus_nets()
        return nets is None or (net is not None and net in nets)

    def _global_focus_metadata(self, md: dict | None) -> dict | None:
        return focus_metadata(md, self._global_focus_nets())

    def _global_focus_rail_choice(self, combo: QComboBox | None,
                                  default: str = _ALL_RAILS) -> str:
        """The rail a tab's own picker stands for: the focused rail while
        focus is on, else the picker's text."""
        if self._global_focus_rail_name is not None:
            return self._global_focus_rail_name
        return combo.currentText() if combo is not None else default

    def _global_focus_warn_count(self, kind: str, count: int) -> int:
        """The tab-title warning count restricted to the focused rail.
        ``count`` (the whole-board count) is returned when focus is off or
        the rows it would be counted from aren't built yet."""
        nets = self._global_focus_nets()
        if nets is None:
            return count
        if kind == "nodes":
            rows = getattr(self, "_nodes_rows_cache", None)
            if rows is None:
                return count
            return sum(1 for r in rows if r.get("status") == "FAIL"
                       and r.get("net") in nets)
        if kind == "vias":
            rows = getattr(self, "_vias_rows_cache", None)
            if rows is None:
                return count
            warn = self._via_current_warn_a
            return sum(1 for r in rows if r.get("net") in nets
                       and r.get("current") is not None
                       and abs(r["current"]) >= warn)
        if kind == "caps":
            rows = getattr(self, "_caps_rows_cache", None)
            if rows is None:
                return count
            return sum(1 for r in rows if self._cap_row_in_focus(r)
                       and self._cap_row_is_warn(r))
        return count

    def _cap_row_in_focus(self, row: dict) -> bool:
        if self._global_focus_rail_name is None:
            return True
        return (row.get("rail") == self._global_focus_rail_name
                or row.get("rail_net") in (self._global_focus_net_set or ()))

    # --- the control --------------------------------------------------------

    def _build_global_focus_chip(self) -> None:
        """Put the control on the menu bar. Called once from ``_build_ui``,
        after the menu bar and the tab strip exist."""
        mb = self.menuBar()
        chip = GlobalFocusChip(mb)
        chip.pick_requested.connect(self._show_global_focus_menu)
        chip.exit_requested.connect(lambda: self._exit_global_focus())
        self._global_focus_chip = chip
        self._global_focus_positioner = _ChipPositioner(
            self._position_global_focus_chip, self)
        mb.installEventFilter(self._global_focus_positioner)
        strip = getattr(self, "_tab_strip", None)
        if strip is not None:
            strip.installEventFilter(self._global_focus_positioner)
            bar = strip._scroll.horizontalScrollBar()
            bar.valueChanged.connect(self._global_focus_positioner.schedule)
            bar.rangeChanged.connect(self._global_focus_positioner.schedule)
        if hasattr(self, "rail_list"):
            self.rail_list.setContextMenuPolicy(Qt.CustomContextMenu)
            self.rail_list.customContextMenuRequested.connect(
                self._on_rail_list_context_menu)
        self._sync_global_focus_chip()

    def _watch_global_focus_anchor(self) -> None:
        """Follow the Nodes tab button, which the strip re-creates on every
        rebuild."""
        positioner = getattr(self, "_global_focus_positioner", None)
        strip = getattr(self, "_tab_strip", None)
        if positioner is None or strip is None:
            return
        btn = strip.tab_button(getattr(self, "_nodes_tab_index", -1))
        if btn is not None:
            btn.installEventFilter(positioner)
        positioner.schedule()

    def _position_global_focus_chip(self) -> None:
        """Line the chip's left edge up with the Nodes tab, kept clear of the
        menus on the left and inside the menu bar on the right."""
        chip = self._global_focus_chip
        if chip is None:
            return
        try:
            mb = self.menuBar()
            chip.adjustSize()
            h = min(chip.sizeHint().height(), max(1, mb.height() - 2))
            w = chip.sizeHint().width()
            chip.resize(w, h)
            left = 0
            for act in mb.actions():
                if act.isVisible():
                    left = max(left, mb.actionGeometry(act).right() + 1)
            left += 16
            x = left
            strip = getattr(self, "_tab_strip", None)
            btn = (strip.tab_button(getattr(self, "_nodes_tab_index", -1))
                   if strip is not None else None)
            if btn is not None and btn.isVisible():
                x = (btn.mapTo(self, QPoint(0, 0)).x()
                     - mb.mapTo(self, QPoint(0, 0)).x())
            x = max(left, min(x, mb.width() - w - 4))
            chip.move(x, max(0, (mb.height() - h) // 2))
            chip.raise_()
            chip.show()
        except RuntimeError:  # a widget went away mid-rebuild
            pass

    def _sync_global_focus_chip(self) -> None:
        chip = self._global_focus_chip
        if chip is None:
            return
        rails = list(getattr(self, "_rails", None) or [])
        stub = bool(getattr(getattr(self, "solution", None), "solver_info",
                            {}).get("stub"))
        enabled = bool(rails) and not stub
        chip.set_state(
            self._global_focus_rail_name, enabled=enabled,
            disabled_tip=("Global Focus needs solved rails - solve the "
                          "design first."))
        rule = self._global_focus_rule
        if rule is not None:
            rule.setStyleSheet(
                f"background-color: {_T()['focus_gold']}; border: none;")
            rule.setVisible(self._global_focus_active())
        positioner = getattr(self, "_global_focus_positioner", None)
        if positioner is not None:
            positioner.schedule()

    def _show_global_focus_menu(self) -> None:
        chip = self._global_focus_chip
        rails = list(getattr(self, "_rails", None) or [])
        if chip is None or not rails:
            return
        menu = QMenu(self)
        head = menu.addAction("Show only this rail on every tab:")
        head.setEnabled(False)
        for rail in rails:
            act = menu.addAction(rail)
            act.setCheckable(True)
            act.setChecked(rail == self._global_focus_rail_name)
            act.triggered.connect(
                lambda _c=False, r=rail: self._enter_global_focus(r))
        if self._global_focus_active():
            menu.addSeparator()
            menu.addAction("Exit Global Focus").triggered.connect(
                lambda: self._exit_global_focus())
        menu.exec(chip.mapToGlobal(QPoint(0, chip.height())))

    def _on_rail_list_context_menu(self, pos) -> None:
        item = self.rail_list.itemAt(pos)
        rail = None
        for name, it in (getattr(self, "_rail_list_items", None) or {}).items():
            if it is item:
                rail = name
                break
        if rail is None:
            for name, items in (getattr(self, "_rail_subnet_items", None)
                                or {}).items():
                if item in items:
                    rail = name
                    break
        if rail is None:
            return
        menu = QMenu(self)
        if rail == self._global_focus_rail_name:
            menu.addAction("Exit Global Focus").triggered.connect(
                lambda: self._exit_global_focus())
        else:
            menu.addAction(f"Global Focus on {rail}").triggered.connect(
                lambda: self._enter_global_focus(rail))
        menu.exec(self.rail_list.viewport().mapToGlobal(pos))

    # --- entering and leaving -----------------------------------------------

    def _enter_global_focus(self, rail: str) -> None:
        if rail not in (getattr(self, "_rails", None) or []):
            return
        if rail == self._global_focus_rail_name:
            return
        if self._global_focus_rail_name is None:
            self._global_focus_saved_eyes = self._snapshot_rail_eyes()
        self._global_focus_rail_name = rail
        self._global_focus_net_set = self._compute_global_focus_nets(rail)
        self._show_only_rail_eyes(rail)
        self._apply_global_focus()
        self.statusBar().showMessage(
            f"Global Focus on {rail}: every tab shows only this rail. "
            "Click × next to its name to show every rail again.", 6000)

    def _exit_global_focus(self, *, reason: str | None = None,
                           apply: bool = True) -> None:
        if self._global_focus_rail_name is None:
            return
        saved = self._global_focus_saved_eyes or {}
        self._global_focus_rail_name = None
        self._global_focus_net_set = None
        self._global_focus_saved_eyes = None
        self._restore_rail_eyes(saved)
        if apply:
            self._apply_global_focus()
        self.statusBar().showMessage(
            reason or "Global Focus off - every rail is back.", 6000)

    def _compute_global_focus_nets(self, rail: str) -> frozenset:
        return focus_scope_nets(
            rail, getattr(self, "_rail_to_members", None) or {},
            getattr(self, "_merged_children", None) or {})

    def _snapshot_rail_eyes(self) -> dict:
        out: dict = {}
        for rail, eye in getattr(self, "_rail_eye_buttons", None) or []:
            subnets = (getattr(self, "_subnet_eye_buttons", None)
                       or {}).get(rail)
            if subnets:
                for net, sub in subnets.items():
                    out[("net", rail, net)] = sub.isVisibleState()
            else:
                out[("rail", rail)] = eye.isVisibleState()
        return out

    def _set_rail_eyes(self, want) -> None:
        """``want(rail, net_or_None) -> bool | None`` (``None`` leaves it)."""
        subnet_eyes = getattr(self, "_subnet_eye_buttons", None) or {}
        for rail, eye in getattr(self, "_rail_eye_buttons", None) or []:
            subnets = subnet_eyes.get(rail)
            if subnets:
                for net, sub in subnets.items():
                    vis = want(rail, net)
                    if vis is not None:
                        sub.setVisibleState(vis, emit=False)
                self._sync_rail_eye_from_subnets(rail)
                self._sync_rail_tree_node_partials(rail)
            else:
                vis = want(rail, None)
                if vis is not None:
                    eye.setVisibleState(vis, emit=False)
        self._sync_all_rails_eye()

    def _show_only_rail_eyes(self, rail: str) -> None:
        self._set_rail_eyes(lambda r, _n: r == rail)

    def _restore_rail_eyes(self, saved: dict) -> None:
        def _want(rail, net):
            key = ("net", rail, net) if net is not None else ("rail", rail)
            if key in saved:
                return saved[key]
            if net is not None and ("rail", rail) in saved:
                return saved[("rail", rail)]
            return None
        self._set_rail_eyes(_want)

    def _sync_global_focus(self) -> None:
        """Re-apply focus after the rails or the tabs were rebuilt (a solve,
        a What-If view, a theme switch). Exits with a toast when the focused
        rail is gone, and refills the per-tab rail pickers so they list the
        current rails."""
        self._refill_rail_combos()
        rail = self._global_focus_rail_name
        if rail is not None:
            if rail not in (getattr(self, "_rails", None) or []):
                self._exit_global_focus(
                    reason=(f"Global Focus off - {rail} is no longer a "
                            "solved rail."), apply=False)
            else:
                self._global_focus_net_set = self._compute_global_focus_nets(
                    rail)
                self._show_only_rail_eyes(rail)
        # Only the chrome: whoever rebuilt the rails or tabs repopulates the
        # tabs next, and each one reads the focus as it fills.
        self._apply_global_focus_chrome()
        self._watch_global_focus_anchor()

    def _refill_rail_combos(self) -> None:
        rails = list(getattr(self, "_rails", None) or [])
        for combo_attr, _label, _fn in _RAIL_COMBOS:
            combo = getattr(self, combo_attr, None)
            if combo is None:
                continue
            try:
                have = [combo.itemText(i) for i in range(combo.count())]
                if have == [_ALL_RAILS, *rails]:
                    continue
                current = combo.currentText()
                combo.blockSignals(True)
                combo.clear()
                combo.addItem(_ALL_RAILS)
                combo.addItems(rails)
                combo.setCurrentText(current if current in rails
                                     else _ALL_RAILS)
                combo.blockSignals(False)
            except RuntimeError:  # combo's tab torn down
                continue

    # --- applying it to every tab -------------------------------------------

    def _apply_global_focus_chrome(self) -> None:
        """The control, the Rails list and the per-tab rail pickers (hidden
        while focus is on - the focused rail is the only choice)."""
        active = self._global_focus_active()
        self._sync_global_focus_chip()
        self._apply_focus_to_rail_list()
        for combo_attr, label_attr, _fn in _RAIL_COMBOS + (
                ("imp_rail_combo", "imp_rail_label", ""),):
            for attr in (combo_attr, label_attr):
                w = getattr(self, attr, None)
                if w is not None:
                    try:
                        w.setVisible(not active)
                    except RuntimeError:
                        pass

    def _apply_global_focus(self, *, render: bool = True) -> None:
        """Focus changed (entered, switched or left): the chrome, then every
        tab that is already showing something refilters or repopulates."""
        self._apply_global_focus_chrome()
        for _combo, _label, fn in _RAIL_COMBOS:
            self._global_focus_call(fn)
        self._global_focus_call("_apply_bridges_filter")
        if getattr(self, "_fixes_populated", False):
            self._global_focus_call("_populate_fixes_tab")
        if getattr(self, "_impedance_populated", False):
            self._global_focus_call("_populate_impedance_tab")
        if getattr(self, "_topology_view", None) is not None:
            self._topology_populated = False
            if self.tabs.currentIndex() == getattr(
                    self, "_topology_tab_index", -1):
                self._topology_populated = True
                self._global_focus_call("_populate_topology",
                                        reuse_metadata=True)
        if getattr(self, "properties_browser", None) is not None:
            self._global_focus_call("_refresh_properties_html")
        self._global_focus_call("_refresh_whatif_compare")
        self._update_nodes_tab_title(getattr(self, "_nodes_warn_count", 0))
        self._update_vias_tab_title(getattr(self, "_vias_warn_count", 0))
        self._update_caps_tab_title()
        if getattr(self, "_editor_mode", False):
            self._global_focus_call("_refresh_net_table")
            self._global_focus_call("_populate_editor_form")
        if render:
            self._render_with_busy_popup()

    def _global_focus_call(self, name: str, *args, **kwargs) -> None:
        """Call a tab's refresh hook if this window has it. One tab failing
        to refresh must not leave the rest unfocused, so it is logged and
        the others still run."""
        fn = getattr(self, name, None)
        if fn is None:
            return
        try:
            fn(*args, **kwargs)
        except RuntimeError:
            # A widget of a tab being rebuilt (theme switch) is already gone.
            log.debug("Global Focus: %s skipped (widget gone)", name)
        except Exception:
            log.exception("Global Focus: refreshing %s failed", name)

    def _apply_focus_to_rail_list(self) -> None:
        """Hide every Rails row but the focused rail's (and its subnets) and
        the unsolved rails, or show them all again. The "All Rails" header
        stays: it carries the layer-outline button, and its eye can't bring
        another rail back (:meth:`_visible_rails` filters to the focus)."""
        lst = getattr(self, "rail_list", None)
        if lst is None:
            return
        focus = self._global_focus_rail_name
        keep: set[int] = set()
        if lst.count():
            keep.add(id(lst.item(0)))
        if focus is not None:
            item = (getattr(self, "_rail_list_items", None) or {}).get(focus)
            if item is not None:
                keep.add(id(item))
            for it in (getattr(self, "_rail_subnet_items", None)
                       or {}).get(focus, []):
                keep.add(id(it))
        for row in range(lst.count()):
            it = lst.item(row)
            it.setHidden(focus is not None and id(it) not in keep)
        self._update_rail_list_height()
