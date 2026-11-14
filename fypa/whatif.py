"""What-If scenarios: change some solve inputs, re-solve, and compare to base.

A scenario is a named set of *overrides* on top of the base design's solve
inputs: board temperature, copper resistivity, via plating, via fill, the
coupled thermal model, and per-layer copper thickness / resistivity. Running
one solves the same design (same directives, same mesh settings) with those
overrides applied, and the result is only ever compared against the base
solve, never against another scenario.

This module is Qt-free so the comparison logic can be tested headless:

* :class:`WhatIfScenario` with ``to_dict`` / ``from_dict`` (stored in the
  ``.fypa`` project file),
* :func:`scenario_settings` / :func:`scenario_stackup_overrides` merge a
  scenario onto the base inputs,
* :func:`base_fingerprint` marks a scenario out of date when the base changes,
* :func:`layer_vertex_field` / :func:`map_field_to_points` let the heatmap
  draw a true per-vertex difference (what-if minus base) even if the two
  meshes are not identical,
* :func:`compare_node_rows` / :func:`summarise` drive the comparison table.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import numpy as np

# ---------------------------------------------------------------------------
# What a scenario may change
# ---------------------------------------------------------------------------

# (SolveSettings attribute, label, unit, kind). ``kind`` is "float", "bool"
# or "fill_mode". Mesh settings are deliberately absent: a scenario with a
# different mesh would mix a numerical change into a physical comparison.
TUNABLE_FIELDS: tuple[tuple[str, str, str, str], ...] = (
    ("temperature_c", "Board temperature", "°C", "float"),
    ("copper_resistivity_20c_microohm_cm",
     "Copper resistivity (at 20 °C)", "µΩ·cm", "float"),
    ("copper_temp_coefficient_per_c",
     "Copper temperature coefficient α", "1/°C", "float"),
    ("plating_thickness_mm", "Via plating: through / PTH", "mm", "float"),
    ("plating_blind_mm", "Via plating: blind", "mm", "float"),
    ("plating_buried_mm", "Via plating: buried", "mm", "float"),
    ("plating_microvia_mm", "Via plating: microvia", "mm", "float"),
    ("conductive_fill_mode", "Via fill mode", "", "fill_mode"),
    ("conductive_fill_resistivity_ohm_mm",
     "Conductive fill resistivity", "Ω·mm", "float"),
    ("electrothermal", "Coupled electro-thermal solve", "", "bool"),
    ("heat_transfer_w_per_m2k",
     "Board heat transfer coefficient", "W/(m²·K)", "float"),
    ("thermal_spreading", "Spread heat across the board", "", "bool"),
)
TUNABLE_KEYS: frozenset[str] = frozenset(f[0] for f in TUNABLE_FIELDS)

FILL_MODE_LABELS: dict[str, str] = {
    "auto": "Auto (IPC-4761 data)",
    "all": "All vias filled",
    "none": "No vias filled",
}

# Copper weights offered as quick picks in the scenario editor (µm).
COPPER_WEIGHT_PRESETS_UM: tuple[tuple[str, float], ...] = (
    ("½ oz (17.5 µm)", 17.5),
    ("1 oz (35 µm)", 35.0),
    ("1½ oz (52.5 µm)", 52.5),
    ("2 oz (70 µm)", 70.0),
    ("3 oz (105 µm)", 105.0),
)

STATUS_NOT_RUN = "not_run"
STATUS_OK = "ok"
STATUS_FAILED = "failed"


@dataclass
class WhatIfScenario:
    """One saved what-if: its overrides plus where its result lives."""

    name: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    notes: str = ""
    # SolveSettings attribute -> value (only keys in TUNABLE_KEYS).
    settings_overrides: dict[str, Any] = field(default_factory=dict)
    # Copper layer id -> thickness (mm). Absent = same as base.
    stackup_thickness_mm: dict[int, float] = field(default_factory=dict)
    # Copper layer id -> resistivity at 20 °C (µΩ·cm). Absent = same as base.
    layer_resistivity: dict[int, float] = field(default_factory=dict)
    # Absolute path of the result pickle (made relative on project save).
    result_pickle: str | None = None
    # base_fingerprint() of the base the result was solved against.
    base_fingerprint: str | None = None
    status: str = STATUS_NOT_RUN
    error: str = ""
    run_at: float | None = None
    # Headline numbers recorded when the run finished (see summarise()), so
    # the panel can show them without loading the result pickle.
    summary: dict[str, Any] = field(default_factory=dict)

    def has_changes(self) -> bool:
        return bool(self.settings_overrides or self.stackup_thickness_mm
                    or self.layer_resistivity)

    def is_stale(self, current_fingerprint: str | None) -> bool:
        return (self.status == STATUS_OK and current_fingerprint is not None
                and self.base_fingerprint != current_fingerprint)

    def copy_as(self, name: str) -> WhatIfScenario:
        """A fresh, un-run copy with the same overrides."""
        return WhatIfScenario(
            name=name, notes=self.notes,
            settings_overrides=dict(self.settings_overrides),
            stackup_thickness_mm=dict(self.stackup_thickness_mm),
            layer_resistivity=dict(self.layer_resistivity),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "notes": self.notes,
            "settings_overrides": dict(self.settings_overrides),
            "stackup_thickness_mm": {
                str(int(k)): float(v)
                for k, v in sorted(self.stackup_thickness_mm.items())},
            "layer_resistivity": {
                str(int(k)): float(v)
                for k, v in sorted(self.layer_resistivity.items())},
            "result_pickle": self.result_pickle,
            "base_fingerprint": self.base_fingerprint,
            "status": self.status,
            "error": self.error,
            "run_at": self.run_at,
            "summary": _json_safe(self.summary),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> WhatIfScenario:
        def _int_map(raw) -> dict[int, float]:
            out: dict[int, float] = {}
            for k, v in (raw or {}).items():
                try:
                    out[int(k)] = float(v)
                except (TypeError, ValueError):
                    continue
            return out

        overrides = {
            str(k): v for k, v in (d.get("settings_overrides") or {}).items()
            if str(k) in TUNABLE_KEYS
        }
        status = str(d.get("status") or STATUS_NOT_RUN)
        if status not in (STATUS_NOT_RUN, STATUS_OK, STATUS_FAILED):
            status = STATUS_NOT_RUN
        run_at = d.get("run_at")
        return cls(
            id=str(d.get("id") or uuid.uuid4().hex[:12]),
            name=str(d.get("name") or "What-if"),
            notes=str(d.get("notes") or ""),
            settings_overrides=overrides,
            stackup_thickness_mm=_int_map(d.get("stackup_thickness_mm")),
            layer_resistivity=_int_map(d.get("layer_resistivity")),
            result_pickle=(None if not d.get("result_pickle")
                           else str(d["result_pickle"])),
            base_fingerprint=(None if d.get("base_fingerprint") is None
                              else str(d["base_fingerprint"])),
            status=status,
            error=str(d.get("error") or ""),
            run_at=(float(run_at) if isinstance(run_at, (int, float))
                    else None),
            summary=dict(d.get("summary") or {}),
        )


def _json_safe(obj):
    """Drop NaN / inf (not valid JSON) and coerce numpy scalars."""
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, (np.floating, float)):
        f = float(obj)
        return f if math.isfinite(f) else None
    if isinstance(obj, np.integer):
        return int(obj)
    return obj


def unique_name(existing: list[str], stem: str = "What-if") -> str:
    """``stem 1``, ``stem 2``, … - the first not already in ``existing``."""
    taken = set(existing)
    n = 1
    while f"{stem} {n}" in taken:
        n += 1
    return f"{stem} {n}"


# ---------------------------------------------------------------------------
# Merging a scenario onto the base inputs
# ---------------------------------------------------------------------------

def scenario_settings(base_settings, scenario: WhatIfScenario):
    """The base ``SolveSettings`` with the scenario's overrides applied.

    Per-layer resistivities merge onto the base's own per-layer map."""
    kwargs: dict[str, Any] = {}
    for key, value in scenario.settings_overrides.items():
        if key not in TUNABLE_KEYS:
            continue
        current = getattr(base_settings, key)
        if isinstance(current, bool):
            kwargs[key] = bool(value)
        elif isinstance(current, str):
            kwargs[key] = str(value)
        else:
            kwargs[key] = float(value)
    layer_rho = dict(getattr(
        base_settings, "layer_copper_resistivity_20c_microohm_cm", None) or {})
    layer_rho.update({int(k): float(v)
                      for k, v in scenario.layer_resistivity.items()})
    kwargs["layer_copper_resistivity_20c_microohm_cm"] = layer_rho
    return dataclasses.replace(base_settings, **kwargs)


def scenario_stackup_overrides(base_overrides: dict[int, float] | None,
                               scenario: WhatIfScenario,
                               design_thickness_mm: dict[int, float]
                               | None = None) -> dict[int, float]:
    """``{layer_id: thickness_mm}`` for the scenario's solve: the base's own
    overrides, then the scenario's on top. A scenario value equal to the
    design thickness drops the override rather than sending a no-op."""
    out = {int(k): float(v) for k, v in (base_overrides or {}).items()}
    design = design_thickness_mm or {}
    for lid, mm in scenario.stackup_thickness_mm.items():
        lid = int(lid)
        orig = design.get(lid)
        if orig is not None and abs(float(mm) - float(orig)) <= 1e-6:
            out.pop(lid, None)
        else:
            out[lid] = float(mm)
    return out


def base_fingerprint(settings, stackup_overrides: dict | None = None,
                     extra: dict[str, Any] | None = None) -> str:
    """Short hash of everything a scenario inherits from the base.

    ``extra`` carries the design identity and directives (prjpcb / pcbdoc
    paths, editor directive dicts, …). A scenario solved against a
    different fingerprint is shown as out of date."""
    if dataclasses.is_dataclass(settings):
        s = dataclasses.asdict(settings)
    else:
        s = dict(settings or {})
    doc = {
        "settings": s,
        "stackup": {str(k): v for k, v in sorted(
            (stackup_overrides or {}).items())},
        "extra": extra or {},
    }
    blob = json.dumps(_json_safe(doc), sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _fmt_num(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{f:.6g}"


def _fmt_value(kind: str, v) -> str:
    if kind == "bool":
        return "on" if v else "off"
    if kind == "fill_mode":
        return FILL_MODE_LABELS.get(str(v), str(v))
    return _fmt_num(v)


def describe_changes(scenario: WhatIfScenario, base_settings,
                     stackup_rows: list[dict] | None = None) -> list[str]:
    """Human-readable ``"Board temperature: 20 → 60 °C"`` lines."""
    lines: list[str] = []
    for key, label, unit, kind in TUNABLE_FIELDS:
        if key not in scenario.settings_overrides:
            continue
        new = scenario.settings_overrides[key]
        old = getattr(base_settings, key, None) if base_settings else None
        u = f" {unit}" if unit else ""
        lines.append(f"{label}: {_fmt_value(kind, old)} → "
                     f"{_fmt_value(kind, new)}{u}")
    names: dict[int, str] = {}
    thick: dict[int, float] = {}
    for row in stackup_rows or []:
        try:
            lid = int(row["layer_id"])
        except (KeyError, TypeError, ValueError):
            continue
        names[lid] = str(row.get("name") or f"L{lid}")
        thick[lid] = float(row.get("copper_thickness_mm") or 0.0)
    for lid, mm in sorted(scenario.stackup_thickness_mm.items()):
        old = thick.get(lid)
        old_txt = f"{old * 1000:g}" if old is not None else "?"
        lines.append(f"{names.get(lid, f'L{lid}')} copper: "
                     f"{old_txt} → {mm * 1000:g} µm "
                     f"({mm / 0.0348:.2g} oz)")
    base_rho = dict(getattr(
        base_settings, "layer_copper_resistivity_20c_microohm_cm", None)
        or {}) if base_settings else {}
    board_rho = getattr(base_settings, "copper_resistivity_20c_microohm_cm",
                        None) if base_settings else None
    for lid, rho in sorted(scenario.layer_resistivity.items()):
        old = base_rho.get(lid, board_rho)
        lines.append(f"{names.get(lid, f'L{lid}')} resistivity: "
                     f"{_fmt_num(old)} → {_fmt_num(rho)} µΩ·cm")
    return lines


# ---------------------------------------------------------------------------
# Per-vertex fields and mapping between meshes (difference heatmap)
# ---------------------------------------------------------------------------

def layer_index_by_name(solution) -> dict[str, int]:
    return {layer.name: i for i, layer in enumerate(solution.problem.layers)}


def layer_vertex_field(solution, layer_index: int, derive_fn, sigma: float
                       ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(xys (N, 2), tris (M, 3), values (N,))`` for one FEM layer.

    Concatenates the layer's meshes with the same skip rule the viewer uses
    (fewer than 3 vertices or no triangles), so a base layer's arrays line
    up vertex for vertex with the viewer's own."""
    ls = solution.layer_solutions[layer_index]
    conductance = solution.problem.layers[layer_index].conductance
    xy_parts, tri_parts, v_parts = [], [], []
    offset = 0
    for xys, tris_local, pot, pd in zip(
        ls.vertex_xys, ls.triangles, ls.potentials, ls.power_densities,
    ):
        xys = np.asarray(xys)
        tris_local = np.asarray(tris_local)
        n = xys.shape[0]
        if n < 3 or tris_local.size == 0:
            continue
        xy_parts.append(xys)
        tri_parts.append(tris_local + offset)
        v_parts.append(np.asarray(
            derive_fn(tris_local, np.asarray(pot),
                      None if pd is None else np.asarray(pd),
                      conductance, n, sigma), dtype=np.float64))
        offset += n
    if not xy_parts:
        return (np.empty((0, 2)), np.empty((0, 3), dtype=np.int32),
                np.empty(0))
    return (np.concatenate(xy_parts), np.concatenate(tri_parts, axis=0),
            np.concatenate(v_parts))


def map_field_to_points(src_xys: np.ndarray, src_tris: np.ndarray,
                        src_vals: np.ndarray, dst_xs: np.ndarray,
                        dst_ys: np.ndarray) -> tuple[np.ndarray, str]:
    """Values of a source per-vertex field at the destination points.

    Returns ``(values, method)``. ``method`` is ``"identical"`` when both
    meshes are the same vertex for vertex (the usual case: a what-if only
    changes physics, so the mesher sees the same geometry), ``"linear"``
    for linear interpolation over the source triangles, and ``"nearest"``
    when that isn't possible. Points off the source mesh take the nearest
    source vertex."""
    n_dst = dst_xs.size
    if n_dst == 0:
        return np.empty(0), "identical"
    if src_vals.size == 0:
        return np.full(n_dst, np.nan), "nearest"
    if src_xys.shape[0] == n_dst:
        dx = np.abs(src_xys[:, 0] - dst_xs)
        dy = np.abs(src_xys[:, 1] - dst_ys)
        if float(max(dx.max(), dy.max())) <= 1e-9:
            return src_vals.copy(), "identical"
    out = None
    method = "linear"
    try:
        from matplotlib.tri import LinearTriInterpolator, Triangulation
        tri = Triangulation(src_xys[:, 0], src_xys[:, 1], src_tris)
        interp = LinearTriInterpolator(tri, src_vals)
        res = interp(dst_xs, dst_ys)
        out = np.asarray(np.ma.filled(res, np.nan), dtype=np.float64)
    except Exception:
        out = None
        method = "nearest"
    if out is None or not np.all(np.isfinite(out)):
        from scipy.spatial import cKDTree
        tree = cKDTree(src_xys)
        if out is None:
            _d, idx = tree.query(np.column_stack([dst_xs, dst_ys]))
            out = src_vals[idx].astype(np.float64)
        else:
            bad = ~np.isfinite(out)
            _d, idx = tree.query(np.column_stack([dst_xs[bad], dst_ys[bad]]))
            out[bad] = src_vals[idx]
    return out, method


def source_return_offsets(sampler) -> dict[str, float]:
    """Per-net potential of that net's SOURCE return pin (see the viewer's
    ``_source_return_offsets``), for any solution, via a
    :class:`fypa.solution_sampling.SolutionSampler`."""
    best: dict[str, tuple[float, float]] = {}
    for d in (sampler.metadata or {}).get("directives", []):
        if d.get("role") != "SOURCE":
            continue
        terms = d.get("terminals") or {}
        p_pins = (terms.get("P") or {}).get("pins", [])
        n_pins = (terms.get("N") or {}).get("pins", [])
        if not p_pins or not n_pins:
            continue
        v_return = None
        for pin in n_pins:
            v_return = sampler.sample_pin(pin)
            if v_return is not None:
                break
        if v_return is None:
            continue
        for pin in p_pins:
            net = pin.get("net", "")
            if not net:
                continue
            v_plus = sampler.sample_pin(pin)
            rank = v_plus if v_plus is not None else float("-inf")
            if net not in best or rank > best[net][0]:
                best[net] = (rank, v_return)
    return {net: off for net, (_r, off) in best.items()}


# ---------------------------------------------------------------------------
# Row comparison and headline summary
# ---------------------------------------------------------------------------

# Comparison metrics for the node table: (key, label, unit, scale,
# direction). ``direction`` +1 = higher is better, -1 = lower is better,
# "abs" = smaller magnitude is better, "terminal" = higher is better on a
# P (supply) pin and lower on an N (return) pin.
COMPARE_METRICS: tuple[tuple[str, str, str, float, Any], ...] = (
    ("drop", "Drop", "mV", 1e3, "abs"),
    ("voltage", "Voltage", "V", 1.0, "terminal"),
    ("margin", "Margin to min V", "mV", 1e3, +1),
    ("current_density", "|J| at pin", "A/mm²", 1.0, -1),
    ("power_density", "Power density", "W/mm²", 1.0, -1),
)

# A change smaller than this fraction of the base value (or this absolute
# floor, for values near zero) counts as "no change".
_SAME_REL = 1e-3
_SAME_ABS = 1e-9


def _verdict(base: float | None, new: float | None, direction,
             terminal: str = "P") -> str:
    """"better" / "worse" / "same" / "" (not comparable)."""
    if base is None or new is None:
        return ""
    if not (math.isfinite(base) and math.isfinite(new)):
        return ""
    delta = new - base
    if abs(delta) <= max(_SAME_ABS, _SAME_REL * abs(base)):
        return "same"
    if direction == "abs":
        good = abs(new) < abs(base)
    elif direction == "terminal":
        good = delta > 0 if terminal != "N" else delta < 0
    else:
        good = (delta > 0) == (direction > 0)
    return "better" if good else "worse"


def _row_key(r: dict) -> tuple:
    return (r.get("role"), r.get("designator"), r.get("terminal"),
            r.get("pad"), r.get("net"), r.get("layer_id"),
            round(float(r.get("x_mm") or 0.0), 4),
            round(float(r.get("y_mm") or 0.0), 4))


def compare_node_rows(base_rows: list[dict], scen_rows: list[dict],
                      metric: str = "drop") -> list[dict]:
    """Join base and what-if node rows and score ``metric`` on each.

    Each output row carries the base row's identity fields plus ``base``,
    ``whatif``, ``delta`` and ``verdict``. Rows present on only one side
    keep ``None`` for the other."""
    direction = next((m[4] for m in COMPARE_METRICS if m[0] == metric), -1)
    scen_by_key = {_row_key(r): r for r in scen_rows}
    out: list[dict] = []
    seen: set[tuple] = set()
    for r in base_rows:
        k = _row_key(r)
        seen.add(k)
        s = scen_by_key.get(k)
        b_val = r.get(metric)
        s_val = s.get(metric) if s is not None else None
        out.append(_compare_row(r, b_val, s_val, direction))
    for k, s in scen_by_key.items():
        if k not in seen:
            out.append(_compare_row(s, None, s.get(metric), direction))
    return out


def _compare_row(ident: dict, b_val, s_val, direction) -> dict:
    delta = (s_val - b_val) if (b_val is not None and s_val is not None) \
        else None
    return {
        "role": ident.get("role"),
        "designator": ident.get("designator"),
        "terminal": ident.get("terminal"),
        "pad": ident.get("pad"),
        "net": ident.get("net"),
        "layer_id": ident.get("layer_id"),
        "base": b_val,
        "whatif": s_val,
        "delta": delta,
        "verdict": _verdict(b_val, s_val, direction,
                            str(ident.get("terminal") or "P")),
    }


def total_copper_loss_w(solution) -> float:
    """Total I²R dissipation in the copper planes and traces (W): per-face
    power density × triangle area, summed over every layer."""
    total = 0.0
    for ls in solution.layer_solutions:
        for xys, tris, pd in zip(ls.vertex_xys, ls.triangles,
                                 ls.power_densities):
            if pd is None:
                continue
            xys = np.asarray(xys)
            tris = np.asarray(tris)
            if tris.size == 0:
                continue
            a = xys[tris[:, 0]]
            b = xys[tris[:, 1]]
            c = xys[tris[:, 2]]
            area = 0.5 * np.abs(
                (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1])
                - (c[:, 0] - a[:, 0]) * (b[:, 1] - a[:, 1]))
            pd = np.asarray(pd, dtype=np.float64)
            if pd.shape[0] != area.shape[0]:
                continue
            val = float(np.nansum(area * pd))
            if math.isfinite(val):
                total += val
    return total


def _node_label(r: dict) -> str:
    return f"{r.get('designator') or '?'}.{r.get('pad') or '?'}"


def summarise(rows: list[dict], solution=None) -> dict[str, Any]:
    """Headline numbers for one solve's node rows (and copper loss).

    Rows may carry a ``rail`` (the rail group the pin's net belongs to);
    the worst cases then record which rail and node they were found on
    (``<key>_rail`` / ``<key>_node``), and ``fails_rails`` lists the rails
    with a min-V failure."""
    sink_rows = [r for r in rows
                 if r.get("drop") is not None and r.get("role") == "SINK"
                 and r.get("terminal") != "N"]
    if not sink_rows:
        sink_rows = [r for r in rows if r.get("drop") is not None]
    margin_rows = [r for r in rows if r.get("margin") is not None]
    worst_drop = min(sink_rows, key=lambda r: r["drop"]) if sink_rows else None
    worst_margin = (min(margin_rows, key=lambda r: r["margin"])
                    if margin_rows else None)
    fail_rows = [r for r in rows if r.get("status") == "FAIL"]
    out: dict[str, Any] = {
        "worst_drop_v": worst_drop["drop"] if worst_drop else None,
        "worst_drop_v_rail": worst_drop.get("rail") if worst_drop else None,
        "worst_drop_v_node": _node_label(worst_drop) if worst_drop else None,
        "worst_margin_v": worst_margin["margin"] if worst_margin else None,
        "worst_margin_v_rail": (worst_margin.get("rail")
                                if worst_margin else None),
        "worst_margin_v_node": (_node_label(worst_margin)
                                if worst_margin else None),
        "fails": len(fail_rows),
        "fails_rails": sorted({str(r["rail"]) for r in fail_rows
                               if r.get("rail")}),
    }
    if solution is not None:
        try:
            out["copper_loss_w"] = total_copper_loss_w(solution)
        except Exception:
            out["copper_loss_w"] = None
        info = getattr(solution, "solver_info", {}) or {}
        rise = info.get("max_temperature_rise_c")
        if isinstance(rise, (int, float)) and rise > 0.0:
            out["max_temp_rise_c"] = float(rise)
    return _json_safe(out)


# (summary key, label, unit, scale, direction) for the headline comparison.
SUMMARY_METRICS: tuple[tuple[str, str, str, float, Any], ...] = (
    ("worst_drop_v", "Worst sink drop", "mV", 1e3, "abs"),
    ("worst_margin_v", "Worst margin to min V", "mV", 1e3, +1),
    ("fails", "Min-V failures", "", 1.0, -1),
    ("copper_loss_w", "Copper I²R loss", "W", 1.0, -1),
    ("max_temp_rise_c", "Max temperature rise", "°C", 1.0, -1),
)


# Headlines that describe the whole board rather than one rail.
_BOARD_WIDE_KEYS = frozenset({"copper_loss_w", "max_temp_rise_c"})


def headline_rail(key: str, base: dict, scen: dict) -> tuple[str, str]:
    """``(rail text, tooltip)`` saying which rail a headline is about.

    A worst case names the what-if's rail, adding the base's when the worst
    case moved to another rail; failures list every failing rail; loss and
    temperature are board-wide. Summaries recorded before rails were
    tracked give ``"-"``."""
    if key in _BOARD_WIDE_KEYS:
        return "All rails", "Whole board - every solved rail."
    if key == "fails":
        rails = sorted(set(scen.get("fails_rails") or [])
                       | set(base.get("fails_rails") or []))
        if not rails:
            none = (scen.get("fails") == 0 and base.get("fails") == 0)
            return ("none" if none else "-"), ""
        return ", ".join(rails), (
            f"Base fails on: {', '.join(base.get('fails_rails') or []) or 'none'}"
            f"\nWhat-if fails on: "
            f"{', '.join(scen.get('fails_rails') or []) or 'none'}")
    s_rail = scen.get(f"{key}_rail")
    b_rail = base.get(f"{key}_rail")
    s_node = scen.get(f"{key}_node")
    b_node = base.get(f"{key}_node")
    tip = (f"Base: {b_node or '?'} on {b_rail or '?'}\n"
           f"What-if: {s_node or '?'} on {s_rail or '?'}")
    if not s_rail and not b_rail:
        return "-", ""
    if s_rail and b_rail and s_rail != b_rail:
        return f"{s_rail} (base: {b_rail})", tip
    return str(s_rail or b_rail), tip


def compare_summaries(base: dict, scen: dict) -> list[dict]:
    """Rows of ``{label, unit, rail, rail_tip, base, whatif, delta,
    verdict}`` for every headline metric either side reports."""
    out: list[dict] = []
    for key, label, unit, scale, direction in SUMMARY_METRICS:
        b = base.get(key)
        s = scen.get(key)
        if b is None and s is None:
            continue
        b_s = None if b is None else float(b) * scale
        s_s = None if s is None else float(s) * scale
        rail, rail_tip = headline_rail(key, base, scen)
        out.append({
            "key": key, "label": label, "unit": unit,
            "rail": rail, "rail_tip": rail_tip,
            "base": b_s, "whatif": s_s,
            "delta": (s_s - b_s) if (b_s is not None and s_s is not None)
            else None,
            "verdict": _verdict(b, s, direction),
        })
    return out


def now() -> float:
    return time.time()
