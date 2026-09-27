"""Locale-independent numeric validation and parsing for editable fields."""
from __future__ import annotations

import re
from PySide6.QtCore import QLocale
from PySide6.QtGui import QDoubleValidator
from PySide6.QtWidgets import QLineEdit, QStyledItemDelegate


# Decimal places a numeric field accepts. Must be at least the widest fixed-
# form output ``_fmt_settings_value``'s ``%.6g`` can produce (9 places, e.g.
# 0.000123457), or the app writes text its own validator marks Invalid.
_SETTINGS_VALUE_DECIMALS = 12




class _CLocaleDoubleValidator(QDoubleValidator):
    """QDoubleValidator that keeps a typed comma visible instead of eating it.

    Qt validates the *resulting string* and silently drops any keystroke that
    would make it Invalid. So a validator that simply refuses the comma turns a
    German user's ``0,5`` into ``05`` — 5.0, a silent 10x error, which is no
    improvement on the 1.234 that rewriting the comma to a dot produced.

    Returning Intermediate instead lets the character land: the field still
    reads ``0,5``, ``hasAcceptableInput()`` is False, and the commit path's
    :func:`float` raises the ValueError callers already turn into a "not a
    number" dialog. The entry is refused *loudly*, which is the only outcome
    that cannot quietly scale a value by 10 or 1000.
    """

    def validate(self, text, pos):
        if "," in text:
            state, _text, _pos = super().validate(text.replace(",", ""), pos)
            if state == QDoubleValidator.Invalid:
                return QDoubleValidator.Invalid, text, pos
            # Typeable, never committable — see the class docstring.
            return QDoubleValidator.Intermediate, text, pos
        return super().validate(text, pos)




def _numeric_validator(parent=None, *, bottom: float | None = 0.0,
                       top: float | None = None,
                       decimals: int = _SETTINGS_VALUE_DECIMALS):
    """A QDoubleValidator that accepts exactly what this app reads back.

    Qt gives a validator the *system* locale, while ``_fmt_settings_value``
    and :func:`float` both use the C locale. On a comma-decimal system that
    inverts the intended behaviour — the field accepts ``0,5`` and rejects
    ``0.5`` — which is the bug the German-locale report describes. Pinning the
    validator to C, and refusing the group separator, makes a comma wrong *at
    the keystroke* instead of silently readable as something else later.

    ``ScientificNotation`` is required, not optional: ``_fmt_settings_value``
    formats with ``%g`` and emits exponent form for small magnitudes, so a
    standard-notation validator would reject the app's own output — and would
    eat the ``e`` and ``-`` out of scientific input the user types, committing
    ``1e-3`` as 13.

    ``bottom=None`` leaves the field unbounded below (a signed coordinate or
    scale limit); ``top=None`` leaves it unbounded above.
    """
    v = _CLocaleDoubleValidator(parent)
    v.setNotation(QDoubleValidator.ScientificNotation)
    if bottom is not None:
        v.setBottom(bottom)
    if top is not None:
        v.setTop(top)
    v.setDecimals(decimals)
    loc = QLocale.c()
    loc.setNumberOptions(QLocale.RejectGroupSeparator)
    v.setLocale(loc)
    return v




class _NumericCellDelegate(QStyledItemDelegate):
    """Give an editable table cell the same numeric validator the line edits
    use, so a comma decimal is caught while typing rather than only on commit.
    """

    def __init__(self, parent=None, *, bottom: float | None = 0.0,
                 top: float | None = None):
        super().__init__(parent)
        self._bottom = bottom
        self._top = top

    def createEditor(self, parent, option, index):
        editor = super().createEditor(parent, option, index)
        if isinstance(editor, QLineEdit):
            editor.setValidator(
                _numeric_validator(editor, bottom=self._bottom, top=self._top),
            )
        return editor




def _parse_numeric_text(text: str) -> float:
    """Parse a numeric field's text in the C locale.

    Fields carry :func:`_numeric_validator`, which is pinned to C and rejects
    the group separator, so text arriving here uses a dot decimal.

    A comma is deliberately *not* translated to a dot. On a comma-grouping
    system ``1,234`` means one thousand two hundred and thirty-four; reading it
    as 1.234 would be a silent 1000x error, where :func:`float` raises a
    ValueError the caller already reports to the user.
    """
    return float(text.strip())




# Capacitance suffixes accepted by the Capacitors tab's value editor, in
# farads. The empty key is the bare number, which means the unit the column
# is labelled with — µF.
_CAPACITANCE_UNIT_F: dict[str, float] = {
    "": 1e-6,
    "f": 1.0,
    "mf": 1e-3, "m": 1e-3,
    "uf": 1e-6, "u": 1e-6, "µf": 1e-6, "µ": 1e-6,
    "nf": 1e-9, "n": 1e-9,
    "pf": 1e-12, "p": 1e-12,
}



_CAPACITANCE_INPUT_RE = re.compile(
    r"([0-9][0-9.]*(?:[eE][+\-]?[0-9]+)?)\s*([a-zA-Zµ]*)")




def _parse_capacitance_f(text: str) -> float:
    """Farads from a Capacitors-tab capacitance entry.

    A bare number is µF, the unit the column is labelled with. A unit
    suffix overrides that, because "100n" typed into a µF field means
    100 nF and never 100 µF — and silently reading it as µF would be
    exactly the 1000x error this editor exists to correct.

    Raises ``ValueError`` on anything else, including a comma decimal: on a
    comma-grouping locale ``1,234`` is one thousand two hundred and thirty
    four, so translating it would be another silent 1000x error (see
    :func:`_parse_numeric_text`).
    """
    stripped = text.strip().replace("μ", "µ")
    m = _CAPACITANCE_INPUT_RE.fullmatch(stripped)
    if m is None:
        raise ValueError(f"not a capacitance: {text!r}")
    scale = _CAPACITANCE_UNIT_F.get(m.group(2).lower())
    if scale is None:
        raise ValueError(f"unknown capacitance unit: {m.group(2)!r}")
    return float(m.group(1)) * scale
