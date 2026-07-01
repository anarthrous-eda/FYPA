"""Live topology preview — merge solve metadata with pending editor edits."""

from __future__ import annotations

# Matches ``fypa.editor_directives._EDITOR_SCHDOC`` (``"(editor)"``) without
# importing the editor module.
_EDITOR_PREVIEW_SCHDOC = "(editor)"


def _editor_directive_to_metadata_dict(ed) -> dict | None:
    """Best-effort metadata directive when no LoadedProject is available."""
    role = getattr(ed, "role", "")
    if role not in ("SOURCE", "SINK", "SERIES"):
        return None
    meta_role = "RESISTOR" if role == "SERIES" else role
    desig = ed.designator or f"EDIT_{ed.id}"
    common: dict = {
        "role": meta_role,
        "designator": desig,
        "channel_index": None,
        "label": desig,
        "schdoc": _EDITOR_PREVIEW_SCHDOC,
    }
    if role == "SOURCE":
        voltage = float(ed.voltage or 0.0)
        common.update({
            "value": voltage,
            "unit": "V",
            "value_str": f"{voltage:.4g} V",
        })
    elif role == "SINK":
        current = float(ed.current or 0.0)
        common.update({
            "value": current,
            "unit": "A",
            "value_str": f"{current * 1000:.4g} mA",
        })
        min_v = getattr(ed, "min_voltage", None)
        if min_v is not None:
            common["min_voltage"] = float(min_v)
    else:
        resistance = float(ed.resistance or 0.0)
        common.update({
            "value": resistance,
            "unit": "Ohm",
            "value_str": f"{resistance * 1000:.4g} mOhm",
        })

    def _terminal(net: str | None, *, ideal: bool = False) -> dict:
        if ideal:
            return {"pin_count": 0, "pins": [], "ideal_return": True}
        pins: list[dict] = []
        if net and ed.kind == "free" and ed.anchor_xy and net == ed.p_net:
            pins.append({
                "pad": "(editor)",
                "layer_id": ed.layer_id,
                "net": net,
                "x_mm": float(ed.anchor_xy[0]),
                "y_mm": float(ed.anchor_xy[1]),
            })
        elif net:
            pins.append({
                "pad": "(editor)",
                "layer_id": ed.layer_id,
                "net": net,
            })
        return {
            "pin_count": len(pins),
            "pins": pins,
            "requested_net": net,
        }

    two_net = (not ed.single_net) or role == "SERIES"
    terms: dict[str, dict] = {"P": _terminal(ed.p_net)}
    if two_net:
        terms["N"] = _terminal(ed.n_net)
    else:
        terms["N"] = _terminal(None, ideal=True)
    common["terminals"] = terms
    return common


def _merge_editor_fallback(
    base: dict,
    editor_directives: list,
) -> dict:
    """Merge pending editor directives into metadata without a LoadedProject."""
    out = dict(base)
    overridden = {
        ed.overrides_designator
        for ed in editor_directives
        if getattr(ed, "overrides_designator", None)
    }
    kept = [
        d for d in (base.get("directives") or [])
        if d.get("designator") not in overridden
        and d.get("schdoc") != _EDITOR_PREVIEW_SCHDOC
    ]
    synth = []
    for ed in editor_directives:
        d = _editor_directive_to_metadata_dict(ed)
        if d is not None:
            synth.append(d)
    out["directives"] = kept + synth
    return out


def metadata_for_topology(
    base: dict | None,
    *,
    loaded=None,
    editor_directives: list | None = None,
    copper_names: list | None = None,
    live_preview: bool = False,
) -> dict | None:
    """Return metadata suitable for :func:`build_topology_model`.

    When ``live_preview`` is true, pending editor directives (and copper
    renames when a ``loaded`` project is available) are applied on top of
    the last-solved metadata bundle so the topology tab tracks unsaved edits.
    """
    if not live_preview:
        return base
    if base is None and loaded is None:
        return None
    base = dict(base or {})

    eds = list(editor_directives or [])
    cns = list(copper_names or [])
    if loaded is None:
        if not eds:
            return base
        return _merge_editor_fallback(base, eds)

    from fypa.altium.loader import build_solve_metadata, clone_loaded_for_edit
    from fypa.editor_directives import apply_copper_names, apply_editor_directives

    loaded_copy = clone_loaded_for_edit(loaded)
    if cns:
        apply_copper_names(loaded_copy, cns)
    apply_editor_directives(loaded_copy, eds)
    fresh = build_solve_metadata(loaded_copy, None)
    out = dict(base)
    out["directives"] = fresh.get("directives") or []
    if fresh.get("net_canonical"):
        out["net_canonical"] = fresh["net_canonical"]
    if fresh.get("annotation_errors") is not None:
        out["annotation_errors"] = fresh.get("annotation_errors")
    return out
