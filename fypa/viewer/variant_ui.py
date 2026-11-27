"""Assembly-variant display shared by the Properties picker and the Bridges
banner, so the two always describe the same thing.

Two states matter and can differ:

* **loaded** - the variant the design on screen was built with, recorded in
  the solve metadata (``metadata["variant"]``, see
  :func:`fypa.altium.variants.variant_metadata`);
* **requested** - the project's ``variant`` setting, which a change in the
  Properties picker updates. It takes effect on the next Solve (Not Fitted
  parts act while annotations are parsed, so the design has to reload).

No Qt here: plain strings and lists, so it is testable without a display.
"""

from __future__ import annotations

from fypa.altium.variants import NO_VARIATIONS, NO_VARIATIONS_LABEL, describe_request
from fypa.viewer.widgets import _esc

# QComboBox item data can't round-trip ``None`` reliably across bindings, so
# "follow Altium" is a sentinel string.
FOLLOW_ALTIUM = "\x00follow"


def loaded_variant(metadata) -> dict | None:
    var = (metadata or {}).get("variant") if isinstance(metadata, dict) else None
    return var if isinstance(var, dict) else None



def requested_variant(project, metadata):
    """The project's request, or - with no project yet - the one the loaded
    design was built with. ``None`` = follow Altium."""
    if project is not None:
        return getattr(project, "variant", None)
    var = loaded_variant(metadata)
    return var.get("requested") if var else None


def resolved_name(request, var: dict) -> str:
    """The variant ``request`` would load, judged from what the loaded
    metadata knows (the available names and Altium's current one)."""
    available = var.get("available") or []
    current = var.get("altium_current")

    def known(name):
        if not name:
            return None
        low = name.lower()
        return next((a for a in available if a.lower() == low), None)

    if request is None:
        return known(current) or NO_VARIATIONS
    if request == NO_VARIATIONS:
        return NO_VARIATIONS
    return known(request) or known(current) or NO_VARIATIONS


def is_pending(request, var: dict | None) -> bool:
    """True when the next Solve would load a different build."""
    if var is None:
        return False
    return (request != var.get("requested")
            and resolved_name(request, var) != var.get("name", ""))


def combo_entries(var: dict | None) -> list[tuple[str, str]]:
    """``(label, data)`` for the picker. ``data`` is :data:`FOLLOW_ALTIUM`,
    ``""`` (base design) or a variant name."""
    current = var.get("altium_current") if var else None
    out = [(describe_request(None, current), FOLLOW_ALTIUM),
           (NO_VARIATIONS_LABEL, NO_VARIATIONS)]
    for name in (var.get("available") or []) if var else []:
        out.append((name, name))
    return out


def request_to_data(request) -> str:
    return FOLLOW_ALTIUM if request is None else request


def data_to_request(data):
    return None if data == FOLLOW_ALTIUM else data


def _label(name: str) -> str:
    return name or NO_VARIATIONS_LABEL


def loaded_summary(var: dict | None) -> str:
    """One line: what the design on screen was built with."""
    if var is None:
        return ""
    name = var.get("name", "")
    nf = len(var.get("not_fitted") or [])
    alt = len(var.get("alternates") or [])
    how = ("following Altium's current variant" if var.get("requested") is None
           else "chosen in FYPA")
    if not name:
        detail = "every part fitted"
    else:
        bits = [f"{nf} part(s) not fitted"]
        if alt:
            bits.append(f"{alt} alternate part(s)")
        detail = ", ".join(bits)
    return f"{_label(name)} ({how}) - {detail}"


def renamed_text(var: dict) -> str:
    """Explains variant entries matched by unique ID, not designator."""
    pairs = var.get("renamed") or []
    eg = ", ".join(f"{a} → {b}" for a, b in pairs[:2])
    return (f"{len(pairs)} entr(ies) name parts by their designator from before "
            f"the board was re-annotated; matched by Altium's unique ID, as "
            f"Altium does (e.g. {eg}).")


def banner_html(var: dict | None, request, theme: dict) -> str:
    """Rich text for the Bridges tab: the active variant, what it does to
    this table, and any change waiting for a Solve."""
    if var is None:
        return (f"<span style='color:{theme['fg_muted']};'>Assembly variant: "
                "none - this design has no Altium variants (or was loaded "
                "before FYPA read them; Solve to refresh).</span>")
    name = var.get("name", "")
    nf = len(var.get("not_fitted") or [])
    parts = [f"<b>Assembly variant:</b> "
             f"<span style='color:{theme['accent']}; font-weight:600;'>"
             f"{_esc(_label(name))}</span>"]
    parts.append(f"<span style='color:{theme['fg_muted']};'>"
                 + ("(following Altium's current variant)"
                    if var.get("requested") is None else "(chosen in FYPA)")
                 + "</span>")
    if name:
        parts.append(f"- {nf} part(s) not fitted: listed below as "
                     "‘not fitted’ and left open, never bridged.")
    else:
        parts.append("- every part fitted.")
    if var.get("note"):
        parts.append(f"<span style='color:{theme['warn']};'>{_esc(var['note'])}.</span>")
    if var.get("renamed"):
        parts.append(f"<span style='color:{theme['fg_muted']};'>"
                     f"{_esc(renamed_text(var))}</span>")
    if is_pending(request, var):
        parts.append(f"<br><span style='color:{theme['warn']}; font-weight:600;'>"
                     f"Changes to {_esc(_label(resolved_name(request, var)))} on "
                     "the next Solve.</span>")
    parts.append(f"<span style='color:{theme['fg_muted']};'>"
                 "Change it in the Properties tab.</span>")
    return " ".join(parts)


def picker_status_html(var: dict | None, request, theme: dict) -> str:
    """Rich text under the Properties picker."""
    if var is None:
        return (f"<span style='color:{theme['fg_muted']};'>This design has no "
                "Altium assembly variants, or was loaded before FYPA read "
                "them - Solve to refresh.</span>")
    lines = [f"<span style='color:{theme['fg_dim']};'>Loaded:</span> "
             f"{_esc(loaded_summary(var))}"]
    if var.get("note"):
        lines.append(f"<span style='color:{theme['warn']};'>{_esc(var['note'])}.</span>")
    if var.get("renamed"):
        lines.append(f"<span style='color:{theme['fg_muted']};'>"
                     f"{_esc(renamed_text(var))}</span>")
    if is_pending(request, var):
        lines.append(f"<span style='color:{theme['warn']}; font-weight:600;'>"
                     f"Will load {_esc(_label(resolved_name(request, var)))} - "
                     "press Solve to apply.</span>")
    if not var.get("available"):
        lines.append(f"<span style='color:{theme['fg_muted']};'>The project "
                     "defines no assembly variants.</span>")
    return "<br>".join(lines)
