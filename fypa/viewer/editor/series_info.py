"""Series-element block in the editor's component form.

Selecting a part the solve treats as a series element (an Altium
``PDN_ROLE=SERIES``, an editor SERIES, an auto-SERIES low-value resistor
or an auto-bridged 0 Ω link / Net Tie) shows where that model came from,
what the solve found through it, and the actions that change it.
"""
from __future__ import annotations

import logging
import math
import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QPushButton,
)

from fypa.viewer.overlays import _EDITOR_SCHDOC
from fypa.viewer.theme import _T
from fypa.viewer.widgets import _esc

_log = logging.getLogger(__name__)

# Typical power rating of a thick-film chip resistor by imperial size code.
# Only a guide - the panel says it is the typical figure for the size, not
# the part's own rating.
_CHIP_RATING_W: dict[str, float] = {
    "0201": 0.05, "0402": 0.063, "0603": 0.1, "0805": 0.125,
    "1206": 0.25, "1210": 0.5, "2010": 0.75, "2512": 1.0,
}
_CHIP_CODE_RE = re.compile(r"(?<!\d)(0201|0402|0603|0805|1206|1210|2010|2512)(?!\d)")


def _si(value: float, unit: str) -> str:
    """``2.13 A``, ``47.2 mV``, ``850 µW`` - three significant figures."""
    mag = abs(value)
    for scale, prefix in ((1.0, ""), (1e-3, "m"), (1e-6, "µ")):
        if mag >= scale:
            return f"{value / scale:.3g} {prefix}{unit}"
    if mag == 0.0:
        return f"0 {unit}"
    return f"{value / 1e-9:.3g} n{unit}"


def _ohm(value: float) -> str:
    if value >= 1e3:
        return f"{value / 1e3:.3g} kΩ"
    if value >= 1.0:
        return f"{value:.3g} Ω"
    return f"{value * 1e3:.3g} mΩ"


def _float(x) -> float | None:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _primary_net(term: dict | None) -> str | None:
    for pin in (term or {}).get("pins") or []:
        if pin.get("net"):
            return pin["net"]
    return None


def _chip_rating(designator: str, footprint: str) -> tuple[str, float] | None:
    """(size code, typical rating W) for a chip resistor, else ``None``."""
    if not re.match(r"R(?![A-Za-z])", (designator or "").upper()):
        return None
    m = _CHIP_CODE_RE.search(footprint or "")
    if m is None:
        return None
    return m.group(1), _CHIP_RATING_W[m.group(1)]


class _SeriesInfoMixin:
    """Series-element block for a component selection in editor mode."""

    # --- Data ----------------------------------------------------------------

    def _auto_series_designators(self) -> set[str]:
        """Upper-cased designators the last solve auto-modelled (state
        ``auto`` in the bridge candidates) - not written by anyone, so not
        "defined in the Altium schematic"."""
        meta = self.metadata if isinstance(self.metadata, dict) else {}
        return {str(r.get("designator") or "").strip().upper()
                for r in meta.get("bridge_candidates") or []
                if r.get("state") == "auto"}

    def _series_info(self, designator: str | None) -> dict | None:
        """Everything the series block shows for ``designator``, or ``None``
        when the part is not (and was not) a series element."""
        if not designator:
            return None
        key = designator.strip().upper()
        meta = self.metadata if isinstance(self.metadata, dict) else {}

        solved = [d for d in meta.get("directives") or []
                  if str(d.get("designator") or "").strip().upper() == key
                  and d.get("role") in ("RESISTOR", "AUTO_BRIDGE")]
        # An editor override of a schematic SERIES: the editor one is the
        # one the solve used.
        solved.sort(key=lambda d: d.get("schdoc") != _EDITOR_SCHDOC)
        directive = solved[0] if solved else None

        editor = None
        project = getattr(self, "_project", None)
        for ed in getattr(project, "editor_directives", None) or []:
            if (ed.role == "SERIES"
                    and (ed.designator or "").strip().upper() == key):
                editor = ed
                break

        try:
            rows = self._bridge_rows()
        except Exception:
            _log.debug("bridge rows unavailable", exc_info=True)
            rows = []
        row = next((r for r in rows
                    if str(r.get("designator") or "").strip().upper() == key),
                   None)
        raw_auto = key in self._auto_series_designators()
        state = (row or {}).get("state")

        merged = directive is not None and directive.get("role") == "AUTO_BRIDGE"
        if editor is not None:
            source = "editor"
        elif state == "off":
            source = "off"
        elif merged:
            source = "merged"
        elif raw_auto and state == "auto":
            source = "auto"
        elif directive is not None:
            source = "schematic"
        else:
            return None

        # Nets: the original pair for a merged link (both pads report the
        # surviving net after the merge), else the model's own P / N.
        if merged and row is not None:
            p_net, n_net = row.get("net_a"), row.get("net_b")
        elif editor is not None:
            p_net, n_net = editor.p_net, editor.n_net
        elif directive is not None:
            terms = directive.get("terminals") or {}
            p_net = _primary_net(terms.get("P"))
            n_net = _primary_net(terms.get("N"))
        else:
            p_net, n_net = (row or {}).get("net_a"), (row or {}).get("net_b")

        if editor is not None and editor.resistance is not None:
            resistance = float(editor.resistance)
        elif directive is not None:
            resistance = _float(directive.get("value"))
        else:
            resistance = _float((row or {}).get("resistance_ohm"))

        info = {
            "designator": designator,
            "source": source,
            "why": (row or {}).get("why") or "",
            "p_net": p_net, "n_net": n_net,
            "resistance": resistance,
            "footprint": (row or {}).get("footprint") or "",
            "row": row,
            "editor": editor,
            "directive": directive,
            "merged": merged,
            "shorted": (directive or {}).get("shorted_nets") or "",
            "pending": False,
        }
        # An editor SERIES the solve has not seen yet, or seen at another
        # value, has no results of its own.
        if editor is not None:
            solved_r = (_float(directive.get("value"))
                        if directive is not None
                        and directive.get("schdoc") == _EDITOR_SCHDOC
                        else None)
            info["pending"] = (solved_r is None or resistance is None
                               or not math.isclose(solved_r, resistance,
                                                   rel_tol=1e-9))
        if directive is not None and not merged and not info["pending"]:
            info.update(self._series_solved_values(directive))
        return info

    def _series_sampler(self):
        """A :class:`SolutionSampler` over the current solution, cached per
        solution object, or ``None`` with no solution."""
        solution = getattr(self, "solution", None)
        if solution is None or getattr(solution, "problem", None) is None:
            return None
        cached = getattr(self, "_series_sampler_cache", None)
        if cached is not None and cached[0] is solution:
            return cached[1]
        from fypa.solution_sampling import SolutionSampler
        try:
            sampler = SolutionSampler(solution, self.metadata)
        except Exception:
            _log.debug("series sampler failed", exc_info=True)
            return None
        self._series_sampler_cache = (solution, sampler)
        return sampler

    def _series_solved_values(self, d: dict) -> dict:
        """Current / drop / loss through a solved series directive, sampled
        at its pins the same way the report does (mean P minus mean N)."""
        out: dict = {"solved": False}
        r = _float(d.get("value"))
        sampler = self._series_sampler()
        if sampler is None or not r:
            return out
        terms = d.get("terminals") or {}
        means: dict[str, float | None] = {}
        for name in ("P", "N"):
            vals = []
            for pin in (terms.get(name) or {}).get("pins") or []:
                try:
                    v = sampler.sample_pin(pin)
                except Exception:
                    v = None
                if v is not None:
                    vals.append(v)
            means[name] = sum(vals) / len(vals) if vals else None
        vp, vn = means["P"], means["N"]
        if vp is None or vn is None:
            out["off_mesh"] = True
            return out
        dv = vp - vn
        i = dv / r
        out.update(solved=True, delta_v=dv, current=i, loss=i * i * r)
        setpoint = self._series_rail_setpoint(_primary_net(terms.get("P")))
        if setpoint is not None:
            out["rail"], out["setpoint"] = setpoint
        return out

    def _series_rail_setpoint(self, net: str | None
                              ) -> tuple[str, float] | None:
        """(rail name, source / regulator set voltage) for the rail ``net``
        belongs to, or ``None``."""
        if not net or not self.metadata:
            return None
        from fypa.rail_groups import compute_rail_groups
        try:
            _names, rail_to_members = compute_rail_groups(self.metadata)
        except Exception:
            return None
        for rail, members in rail_to_members.items():
            if net not in members:
                continue
            members = set(members)
            for d in self.metadata.get("directives") or []:
                term = {"SOURCE": "P", "REGULATOR": "OUT_P"}.get(
                    d.get("role"))
                if term is None:
                    continue
                pins = ((d.get("terminals") or {}).get(term) or {}).get(
                    "pins") or []
                if any(p.get("net") in members for p in pins):
                    v = _float(d.get("value"))
                    if v:
                        return rail, abs(v)
            return None
        return None

    # --- Panel ---------------------------------------------------------------

    _SERIES_SOURCE_TEXT = {
        "schematic": "Altium schematic (PDN_ROLE=SERIES)",
        "editor": "Set in FYPA (editor SERIES)",
        "auto": "Auto-SERIES (set by FYPA)",
        "merged": "Auto-bridged by FYPA (nets merged)",
        "off": "Auto-SERIES disabled here",
    }

    def _build_series_block(self, lay, info: dict, *,
                            show_terms: bool = True) -> None:
        """Add the series block (and its buttons) to the form layout."""
        t = _T()
        muted = t["fg_muted"]
        lines: list[str] = []
        head = "<b>SERIES</b>"
        if info["resistance"] is not None and not info["merged"]:
            head += f" &middot; {_esc(_ohm(info['resistance']))}"
        lines.append(head)
        lines.append(f"<span style='color:{muted};'>"
                     f"{_esc(self._SERIES_SOURCE_TEXT[info['source']])}"
                     "</span>")
        why = info["why"]
        if why and info["source"] in ("auto", "merged", "off"):
            lines.append(f"<span style='color:{muted};'>Why: {_esc(why)}"
                         "</span>")
        if show_terms and (info["p_net"] or info["n_net"]):
            lines.append(f"P&nbsp;=&nbsp;{_esc(info['p_net'] or '?')} "
                         f"&nbsp; N&nbsp;=&nbsp;{_esc(info['n_net'] or '?')}")
        lines.append("")
        lines.extend(self._series_result_lines(info))
        box = QLabel("<br>".join(lines))
        box.setWordWrap(True)
        box.setTextFormat(Qt.RichText)
        box.setStyleSheet(
            f"QLabel {{ border: 1px solid {t['border']}; border-radius: 4px;"
            f" padding: 6px; background-color: {t['bg_alt']}; }}")
        lay.addWidget(box)
        self._build_series_buttons(lay, info)

    def _series_result_lines(self, info: dict) -> list[str]:
        t = _T()
        muted = t["fg_muted"]
        des = info["designator"]
        if info["source"] == "off":
            return [f"<span style='color:{muted};'>{_esc(des)} is left open "
                    "at DC: no current flows through it in the solve."
                    "</span>"]
        if info["merged"]:
            nets = info["shorted"] or (
                f"{info['p_net'] or '?'} ↔ {info['n_net'] or '?'}")
            return [f"<span style='color:{muted};'>Nets merged ({_esc(nets)}):"
                    " no current or drop is solved through this part. Model "
                    "it as SERIES to see what it carries.</span>"]
        if info["pending"]:
            return [f"<span style='color:{t['warn']};'>Not solved at this "
                    "value yet - Resolve to see its current and drop."
                    "</span>"]
        if not info.get("solved"):
            if info.get("off_mesh"):
                text = ("Its pads are off the solved copper, so no current "
                        "or drop could be read.")
            else:
                text = "Not solved yet."
            return [f"<span style='color:{muted};'>{text}</span>"]

        i = info["current"]
        dv = info["delta_v"]
        loss = info["loss"]
        direction = "P to N" if i >= 0 else "N to P"
        rows = [f"Current&nbsp;&nbsp;<b>{_esc(_si(abs(i), 'A'))}</b> "
                f"<span style='color:{muted};'>({direction})</span>"]
        drop = f"Drop&nbsp;&nbsp;<b>{_esc(_si(abs(dv), 'V'))}</b>"
        if info.get("setpoint"):
            pct = 100.0 * abs(dv) / info["setpoint"]
            drop += (f" <span style='color:{muted};'>({pct:.2g} % of "
                     f"{_esc(info['rail'])} {info['setpoint']:g} V)</span>")
        rows.append(drop)
        loss_txt = f"Loss&nbsp;&nbsp;<b>{_esc(_si(loss, 'W'))}</b>"
        rating = _chip_rating(des, info["footprint"])
        if rating is not None:
            code, watts = rating
            over = loss > watts
            colour = t["err"] if over else muted
            verdict = "over" if over else "within"
            loss_txt += (f" <span style='color:{colour};'>({verdict} the "
                         f"typical {_si(watts, 'W')} for a {code} chip "
                         "resistor)</span>")
        rows.append(loss_txt)
        if getattr(self, "_solve_stale", False):
            rows = [f"<i>{r}</i>" for r in rows]
            rows.append(f"<span style='color:{t['warn']};'>Out of date - "
                        "Resolve to refresh.</span>")
        return rows

    def _build_series_buttons(self, lay, info: dict) -> None:
        des = info["designator"]
        source = info["source"]
        row = info["row"]
        btns: list[QPushButton] = []
        if source in ("auto", "merged"):
            b = QPushButton("Disable auto-SERIES")
            b.setToolTip("Leave this part open at DC on the next Resolve "
                         "(same as Disable in the Bridges tab).")
            b.clicked.connect(lambda: self._on_series_disable_auto(des))
            btns.append(b)
            b = QPushButton("Make it an editor SERIES")
            b.setToolTip("Pin this part as an editor SERIES so changing the "
                         "auto-SERIES limit can't drop it, and its resistance "
                         "can be edited here.")
            b.clicked.connect(lambda: self._on_series_make_editor(des))
            btns.append(b)
        elif source == "off":
            b = QPushButton("Re-enable auto-SERIES")
            b.setToolTip("Let FYPA model this part again on the next "
                         "Resolve.")
            b.clicked.connect(lambda: self._on_series_revert(des))
            btns.append(b)
        if row is not None:
            b = QPushButton("Show in Bridges tab")
            b.clicked.connect(lambda: self._show_in_bridges_tab(des))
            btns.append(b)
        if info["p_net"] and info["n_net"]:
            b = QPushButton("Focus both nets")
            b.setToolTip("Show only the copper of this part's two nets "
                         "(press F to show everything again).")
            p, n = info["p_net"], info["n_net"]
            b.clicked.connect(lambda: self._focus_series_nets(p, n))
            btns.append(b)
        # Two per row so the narrow panel doesn't clip the labels.
        for k in range(0, len(btns), 2):
            line = QHBoxLayout()
            for b in btns[k:k + 2]:
                line.addWidget(b)
            lay.addLayout(line)

    # --- Actions -------------------------------------------------------------

    def _series_bridge_row(self, des: str) -> dict | None:
        key = des.strip().upper()
        return next((r for r in self._bridge_rows()
                     if str(r.get("designator") or "").strip().upper() == key),
                    None)

    def _after_series_action(self) -> None:
        """Redraw the form and markers after a series action edited the
        project (the Bridges helpers already marked it dirty / stale)."""
        self._refresh_solve_stale_overlay()
        self._populate_editor_form()
        self._refresh_editor_markers()

    def _on_series_disable_auto(self, des: str) -> None:
        self._disable_auto_bridge(des)
        self._after_series_action()

    def _on_series_revert(self, des: str) -> None:
        rec = self._series_bridge_row(des)
        if rec is None:
            return
        self._apply_bridge_series(rec, None)
        self._after_series_action()

    def _on_series_make_editor(self, des: str) -> None:
        rec = self._series_bridge_row(des)
        if rec is None:
            return
        r = _float(rec.get("resistance_ohm"))
        if r is None or r <= 0.0:
            # A 0 Ω link or Net Tie has no resistance of its own to model -
            # ask for one rather than refuse.
            value, ok = QInputDialog.getDouble(
                self, f"Model {des} as SERIES",
                "Resistance (mΩ):", 1.0, 1e-6, 1e9, 3)
            if not ok:
                return
            r = value * 1e-3
        self._apply_bridge_series(rec, r)
        self._after_series_action()

    def _show_in_bridges_tab(self, des: str) -> None:
        idx = getattr(self, "_bridges_tab_index", -1)
        if idx < 0:
            return
        self.tabs.setCurrentIndex(idx)
        combo = getattr(self, "bridges_state_combo", None)
        if combo is not None and combo.currentText() != "All parts":
            combo.setCurrentText("All parts")
        table = getattr(self, "bridges_table", None)
        if table is None:
            return
        for i in range(table.rowCount()):
            item = table.item(i, 0)
            if item is not None and item.text() == des:
                if table.isRowHidden(i):
                    self.statusBar().showMessage(
                        f"{des} is hidden by the Bridges column filter.", 4000)
                table.selectRow(i)
                table.scrollToItem(item)
                return

    def _focus_series_nets(self, p_net: str, n_net: str) -> None:
        """Net focus on both of a series part's nets at once."""
        if self._editor_focus_net == p_net:
            self._clear_editor_focus()
            return
        self._editor_focus_net = p_net
        self._editor_focus_nets = (self._connected_nets(p_net)
                                   | self._connected_nets(n_net))
        self._editor_focus_polys = set()
        self._sync_net_focus_column()
        self._update_editor_focus_chip()
        self._render()
        self.statusBar().showMessage(
            f"Focused on {p_net} and {n_net}. Press F to show everything.",
            6000)
