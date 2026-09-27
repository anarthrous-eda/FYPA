"""Build a :class:`~fypa.report.model.Report` from a solved design.

Headless: needs only the solution, its metadata dict and the settings. The
viewer adds what only it holds (capacitor / impedance results, the Messages
log, whether the solve is stale) through keyword arguments.

Definitions used throughout the report
--------------------------------------
* **Setpoint** — the voltage of the rail's SOURCE directive, or the output
  voltage of the REGULATOR that drives it. With several, the highest.
* **Voltage at a load** — the load's lowest supply (P) pin minus its highest
  return (N) pin: the worst differential voltage the part can see. A load on
  an ideal return (single-net directive) is referenced to its source return.
* **Drop** — setpoint minus the voltage at the load, so it includes both the
  supply and the return path; the return path's share is reported alongside.
"""
from __future__ import annotations

import datetime as _dt
import logging
import math
import re
from typing import cast

import numpy as np

from fypa.rail_groups import compute_rail_groups
from fypa.report import checks
from fypa.report.model import (
    PASS,
    SEVERITY_ORDER,
    DecouplingResult,
    LayerResult,
    LoadResult,
    RailReport,
    RegulatorResult,
    Report,
    ReportSettings,
    SeriesResult,
    ViaResult,
)
from fypa.topology.metadata_schema import TopologyMetadata
from fypa.solution_sampling import (
    SolutionSampler,
    compute_via_rows,
    pin_display_pad,
)

log = logging.getLogger(__name__)

# Peak current density is read at this percentile of the per-face values, the
# same clip the viewer's colour scale uses: the raw maximum sits on a
# pinned-voltage vertex (an FEM singularity) and would overstate the peak.
_PEAK_PERCENTILE = 99.9

_SUPPLY_TERMS = {"SINK": ("P", "N"), "REGULATOR": ("IN_P", "IN_N")}


def build_report(
    solution,
    metadata: dict,
    settings: ReportSettings | None = None,
    *,
    decoupling: dict[str, DecouplingResult] | None = None,
    decoupling_note: str = "",
    solve_stale: bool = False,
    messages: list[tuple[str, str]] | None = None,
    source_kind: str = "altium",
    fypa_version: str | None = None,
    render_figures: bool = True,
    topology: bool = True,
) -> Report:
    """Assemble the whole report. ``decoupling`` maps rail → result for the
    rails the capacitor analysis covered; any other rail gets
    ``decoupling_note`` as its "not analysed" reason."""
    settings = settings or ReportSettings()
    metadata = metadata or {}
    sampler = SolutionSampler(solution, metadata)
    rail_names, rail_to_members = compute_rail_groups(
        cast("TopologyMetadata", metadata))
    net_to_rail = {n: rail for rail, members in rail_to_members.items()
                   for n in members}
    directives = [d for d in metadata.get("directives", [])
                  if d.get("role") != "AUTO_BRIDGE"]
    pin_v = _sample_directive_pins(sampler, directives)

    ctx = _Context(sampler, metadata, directives, pin_v, rail_to_members,
                   net_to_rail, settings)
    via_rows = compute_via_rows(sampler)

    rails: list[RailReport] = []
    for name in rail_names:
        rail = ctx.build_rail(name, via_rows)
        if rail is None:
            continue
        dec = (decoupling or {}).get(name)
        if dec is None and rail.kind == "supply":
            if decoupling is None:
                dec = DecouplingResult(
                    analysed=False,
                    reason=decoupling_note or "Not run for this report.")
            else:
                dec = DecouplingResult(
                    analysed=False, flag_missing=True,
                    reason="No decoupling capacitors were identified on "
                    "this rail.")
        rail.decoupling = dec
        checks.rail_findings(rail, settings)
        if render_figures and rail.kind == "supply":
            try:
                ctx.draw_figures(rail)
            except Exception:
                log.warning("Report figures failed for rail %s", name,
                            exc_info=True)
        rails.append(rail)

    order = {s: i for i, s in enumerate(SEVERITY_ORDER)}
    rails.sort(key=lambda r: (
        r.kind != "supply", order.get(r.status, 9),
        -(r.nominal_v or 0.0), r.name))

    if fypa_version is None:
        try:
            from fypa.cli import __version__ as fypa_version
        except Exception:
            fypa_version = "?"

    report = Report(
        project_name=str(metadata.get("project_name") or "Untitled"),
        prjpcb_path=str(metadata.get("prjpcb_path") or ""),
        pcbdoc_path=str(metadata.get("pcbdoc_path") or ""),
        generated=_dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        fypa_version=str(fypa_version),
        settings=settings,
        rails=rails,
        design_findings=checks.design_findings(metadata, solution,
                                               solve_stale),
        solve_stale=solve_stale,
        source_kind=source_kind,
        analyses=_analyses_label(rails, metadata),
        setup_rows=_setup_rows(metadata, solution, settings, solve_stale),
        stackup_rows=_stackup_rows(metadata),
        messages=list(messages or []),
    )
    if topology and settings.include_topology:
        report.topology_svg = _topology_svg(metadata)
    return report


# --- sampling -----------------------------------------------------------------

def _sample_directive_pins(sampler: SolutionSampler, directives: list[dict]
                           ) -> dict[tuple[int, str], list[tuple[dict, float | None]]]:
    """``{(directive index, terminal): [(pin, voltage), …]}`` in one batch."""
    requests: dict[tuple[str, str], list[tuple[object, float, float]]] = {}
    keyed: dict[tuple[int, str], list[tuple[dict, object]]] = {}
    for di, d in enumerate(directives):
        for term_name, term in (d.get("terminals") or {}).items():
            lst = keyed.setdefault((di, term_name), [])
            for pi, pin in enumerate(term.get("pins", [])):
                key = (di, term_name, pi)
                lst.append((pin, key))
                phys = sampler.id_to_phys.get(pin.get("layer_id"))
                x, y = pin.get("x_mm"), pin.get("y_mm")
                if phys is None or x is None or y is None:
                    continue
                requests.setdefault((phys, pin.get("net", "")), []).append(
                    (key, float(x), float(y)))
    samples = sampler.sample_batch(requests)
    return {k: [(pin, samples.get(key, (None,))[0]) for pin, key in lst]
            for k, lst in keyed.items()}


def _pin_nets(d: dict, term: str) -> set[str]:
    return {p.get("net") for p in ((d.get("terminals") or {}).get(term) or {})
            .get("pins", []) if p.get("net")}


def _mean(vals) -> float | None:
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def _float(x) -> float | None:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _regulator_kind(d: dict) -> str:
    rtype = d.get("regulator_type")
    topo = d.get("smps_topology")
    if rtype == "SMPS" and topo:
        return f"SMPS {topo.lower()}"
    return rtype or "Regulator"


def _series_kind(designator: str) -> str:
    m = re.match(r"[A-Za-z]+", designator or "")
    prefix = (m.group(0).upper() if m else "")
    return {"FB": "Ferrite", "L": "Inductor", "R": "Resistor", "F": "Fuse",
            "J": "Connector", "JP": "Jumper", "NT": "Net tie",
            "Q": "Transistor"}.get(prefix, "Series part")


class _Context:
    """Shared lookups for building every rail."""

    def __init__(self, sampler, metadata, directives, pin_v, rail_to_members,
                 net_to_rail, settings):
        self.sampler = sampler
        self.metadata = metadata
        self.directives = directives
        self.pin_v = pin_v
        self.rail_to_members = rail_to_members
        self.net_to_rail = net_to_rail
        self.settings = settings
        self._load_cache: dict[str, float | None] = {}
        self._layer_cache: dict[tuple[str, str], dict | None] = {}
        self.stackup_by_name = {r.get("name"): r
                                for r in metadata.get("stackup", [])}
        # Rail name -> absolute potential of its source's return pins.
        self.src_ret: dict[str, float] = {}

    # --- directive helpers --------------------------------------------------

    def volts(self, di: int, term: str) -> list[float]:
        return [v for _p, v in self.pin_v.get((di, term), []) if v is not None]

    def rail_of(self, d: dict, term: str) -> str | None:
        for net in _pin_nets(d, term):
            if net in self.net_to_rail:
                return self.net_to_rail[net]
        return None

    def sources_of(self, rail: str) -> list[tuple[int, dict, float]]:
        members = set(self.rail_to_members.get(rail, [rail]))
        out = []
        for di, d in enumerate(self.directives):
            role = d.get("role")
            term = {"SOURCE": "P", "REGULATOR": "OUT_P"}.get(role)
            if term and _pin_nets(d, term) & members:
                v = _float(d.get("value"))
                if v is not None:
                    out.append((di, d, v))
        return out

    def source_return_v(self, di: int, d: dict) -> float:
        """Absolute potential at a source's return pins (0 V for an ideal
        return). Referencing loads to this removes the global datum's offset."""
        term = "N" if d.get("role") == "SOURCE" else "OUT_N"
        v = _mean(self.volts(di, term))
        return 0.0 if v is None else v

    def rail_load(self, rail: str, _stack: frozenset = frozenset()
                  ) -> float | None:
        """Total DC current drawn from a rail: its SINK currents plus the
        estimated input current of every regulator it feeds (KCL)."""
        if rail in self._load_cache:
            return self._load_cache[rail]
        if rail in _stack:
            return None
        members = set(self.rail_to_members.get(rail, [rail]))
        total, found = 0.0, False
        for d in self.directives:
            role = d.get("role")
            if role == "SINK" and _pin_nets(d, "P") & members:
                total += _float(d.get("value")) or 0.0
                found = True
            elif role == "REGULATOR" and _pin_nets(d, "IN_P") & members:
                i_in = self.regulator_input_current(d, _stack | {rail})
                if i_in is not None:
                    total += i_in
                    found = True
        result = total if found else None
        self._load_cache[rail] = result
        return result

    def regulator_input_current(self, d: dict, _stack=frozenset()
                                ) -> float | None:
        out_rail = self.rail_of(d, "OUT_P")
        if out_rail is None:
            return None
        i_out = self.rail_load(out_rail, _stack)
        if i_out is None:
            return None
        gain = _float(d.get("gain"))
        gain = 1.0 if gain is None else gain
        return gain * i_out + (_float(d.get("quiescent_current")) or 0.0)

    # --- per-layer mesh stats -----------------------------------------------

    def layer_stats(self, phys: str, net: str) -> dict | None:
        key = (phys, net)
        if key in self._layer_cache:
            return self._layer_cache[key]
        li = self.sampler.index_by_pair.get(key)
        if li is None:
            self._layer_cache[key] = None
            return None
        ls = self.sampler.solution.layer_solutions[li]
        cond = float(self.sampler.solution.problem.layers[li].conductance)
        parts = []
        loss = 0.0
        v_min = (math.inf, None)
        v_max = -math.inf
        j_faces, j_xy = [], []
        for xys, tri, pot, pd in zip(ls.vertex_xys, ls.triangles,
                                     ls.potentials, ls.power_densities):
            xys = np.asarray(xys)
            tri = np.asarray(tri)
            pot = np.asarray(pot, dtype=np.float64)
            if xys.shape[0] == 0 or tri.size == 0:
                continue
            used = np.unique(tri.ravel())
            vu = pot[used]
            i_lo = int(np.argmin(vu))
            if vu[i_lo] < v_min[0]:
                v_min = (float(vu[i_lo]), tuple(xys[used[i_lo]]))
            v_max = max(v_max, float(vu.max()))
            p = xys[tri]
            area = 0.5 * np.abs(
                (p[:, 1, 0] - p[:, 0, 0]) * (p[:, 2, 1] - p[:, 0, 1])
                - (p[:, 2, 0] - p[:, 0, 0]) * (p[:, 1, 1] - p[:, 0, 1]))
            pd_arr = (np.asarray(pd, dtype=np.float64) if pd is not None
                      else np.zeros(tri.shape[0]))
            loss += float(np.nansum(pd_arr * area))
            j_faces.append(np.sqrt(np.maximum(pd_arr * cond, 0.0)))
            j_xy.append(p.mean(axis=1))
            parts.append((xys, tri, pot, pd_arr, cond))
        if not parts:
            self._layer_cache[key] = None
            return None
        j_all = np.concatenate(j_faces)
        xy_all = np.concatenate(j_xy)
        if j_all.size >= 100:
            peak = float(np.percentile(j_all, _PEAK_PERCENTILE))
        else:
            peak = float(j_all.max())
        under = np.flatnonzero(j_all <= peak)
        i_pk = int(under[np.argmax(j_all[under])]) if under.size else 0
        stats = {
            "parts": parts, "loss": loss, "v_min": v_min[0],
            "v_min_xy": v_min[1], "v_max": v_max, "peak_j": peak,
            "peak_j_xy": (float(xy_all[i_pk, 0]), float(xy_all[i_pk, 1])),
        }
        self._layer_cache[key] = stats
        return stats

    def rail_layers(self, rail: str) -> list[tuple[str, str]]:
        members = set(self.rail_to_members.get(rail, [rail]))
        pairs = [pair for pair in self.sampler.index_by_pair
                 if pair[1] in members]
        order = {r.get("name"): i
                 for i, r in enumerate(self.metadata.get("stackup", []))}
        return sorted(pairs, key=lambda p: (order.get(p[0], 1 << 20), p[1]))

    def copper_label(self, phys: str) -> str:
        row = self.stackup_by_name.get(phys) or {}
        oz = _float(row.get("copper_thickness_oz"))
        if oz is None:
            um = _float(row.get("copper_thickness_mm"))
            return f"{um * 1000:.0f} µm" if um else "—"
        return f"{oz:.2g} oz"

    # --- rail assembly --------------------------------------------------------

    def build_rail(self, name: str, via_rows: list[dict]) -> RailReport | None:
        members = list(self.rail_to_members.get(name, [name]))
        sources = self.sources_of(name)
        if sources:
            kind = "supply"
        elif any(_pin_nets(d, t) & set(members)
                 for d in self.directives
                 for t in ("N", "OUT_N", "IN_N")):
            kind = "return"
        else:
            # Sinks with no source: the rail was never solved (open loop);
            # the design findings already say so.
            return None

        rail = RailReport(name=name, kind=kind, members=members,
                          nominal_v=None, source_label="", load_a=None)
        if kind == "supply":
            src_di, src, setpoint = max(sources, key=lambda s: s[2])
            rail.nominal_v = setpoint
            src_ret = self.source_return_v(src_di, src)
            label = str(src.get("label") or src.get("designator"))
            if src.get("role") == "REGULATOR":
                in_rail = self.rail_of(src, "IN_P")
                rail.source_label = (f"{label} ({_regulator_kind(src)}"
                                     + (f", from {in_rail})" if in_rail
                                        else ")"))
            else:
                rail.source_label = f"{label} (source)"
            if len(sources) > 1:
                rail.source_label += f" + {len(sources) - 1} more"
            rail.load_a = self.rail_load(name)
            self._loads(rail, setpoint, src_ret)
            self._series_and_regulators(rail)
        else:
            rail.nominal_v = 0.0
            rail.source_label = "Return path"

        self._copper(rail)
        self._vias(rail, via_rows)
        self._thermal(rail)
        return rail

    def _loads(self, rail: RailReport, setpoint: float, src_ret: float) -> None:
        members = set(rail.members)
        for di, d in enumerate(self.directives):
            role = d.get("role")
            if role not in _SUPPLY_TERMS:
                continue
            p_term, n_term = _SUPPLY_TERMS[role]
            if not (_pin_nets(d, p_term) & members):
                continue
            p_pins = [(p, v) for p, v in self.pin_v.get((di, p_term), [])
                      if v is not None]
            n_volts = self.volts(di, n_term)
            label = str(d.get("label") or d.get("designator") or "?")
            nets = sorted(_pin_nets(d, p_term) & members)
            if role == "SINK":
                current = _float(d.get("value"))
                kind = "sink"
                min_v = _float(d.get("min_voltage"))
            else:
                current = self.regulator_input_current(d)
                out_rail = self.rail_of(d, "OUT_P")
                kind = "regulator input"
                label = f"{label} → {out_rail}" if out_rail else label
                min_v = None
            load = LoadResult(
                label=label, kind=kind, net=", ".join(nets),
                pin_count=len(self.pin_v.get((di, p_term), [])),
                current_a=current, v_load=None, drop_v=None,
                return_drop_v=None, min_v=min_v, margin_v=None,
                status=PASS)
            if p_pins:
                worst_pin, v_p = min(p_pins, key=lambda pv: pv[1])
                v_ret = max(n_volts) if n_volts else src_ret
                load.v_load = v_p - v_ret
                load.drop_v = setpoint - load.v_load
                load.return_drop_v = (max(n_volts) - src_ret) if n_volts else 0.0
                load.worst_pin = pin_display_pad(worst_pin)
                load.worst_layer = self.sampler.layer_name(
                    worst_pin.get("layer_id"))
                load.worst_xy = (_float(worst_pin.get("x_mm")) or 0.0,
                                 _float(worst_pin.get("y_mm")) or 0.0)
                if min_v is not None:
                    load.margin_v = load.v_load - min_v
            rail.loads.append(load)
            for pin, v in self.pin_v.get((di, p_term), []):
                rail.pin_rows.append({
                    "load": label, "pad": pin_display_pad(pin),
                    "net": pin.get("net", ""),
                    "layer": self.sampler.layer_name(pin.get("layer_id")),
                    "x_mm": pin.get("x_mm"), "y_mm": pin.get("y_mm"),
                    "v_abs": v,
                    "v_load": None if v is None else v - (
                        max(n_volts) if n_volts else src_ret),
                })
        v_loads = [ld.v_load for ld in rail.loads if ld.v_load is not None]
        drops = [ld.drop_v for ld in rail.loads if ld.drop_v is not None]
        if v_loads and drops:
            rail.lowest_v = min(v_loads)
            rail.worst_drop_v = max(drops)
            if setpoint:
                rail.worst_drop_pct = 100.0 * rail.worst_drop_v / setpoint
        margins = [ld.margin_v for ld in rail.loads if ld.margin_v is not None]
        rail.min_margin_v = min(margins) if margins else None
        self.src_ret[rail.name] = src_ret

    def _series_and_regulators(self, rail: RailReport) -> None:
        members = set(rail.members)
        for di, d in enumerate(self.directives):
            role = d.get("role")
            if role == "RESISTOR" and (
                    (_pin_nets(d, "P") | _pin_nets(d, "N")) & members):
                r = _float(d.get("value"))
                vp = _mean(self.volts(di, "P"))
                vn = _mean(self.volts(di, "N"))
                dv = None if vp is None or vn is None else vp - vn
                i = dv / r if dv is not None and r else None
                nets = " → ".join(sorted(_pin_nets(d, "P")) +
                                  sorted(_pin_nets(d, "N")))
                desig = str(d.get("label") or d.get("designator") or "?")
                rail.series.append(SeriesResult(
                    designator=desig, kind=_series_kind(desig),
                    resistance_ohm=r or 0.0, nets=nets, current_a=i,
                    delta_v=dv,
                    loss_w=None if i is None or r is None else i * i * r))
            elif role == "REGULATOR" and _pin_nets(d, "OUT_P") & members:
                v_in_p = _mean(self.volts(di, "IN_P"))
                v_in_n = _mean(self.volts(di, "IN_N"))
                v_in = (None if v_in_p is None
                        else v_in_p - (v_in_n if v_in_n is not None else 0.0))
                v_out_p = _mean(self.volts(di, "OUT_P"))
                v_out_n = _mean(self.volts(di, "OUT_N"))
                v_out = (None if v_out_p is None
                         else v_out_p - (v_out_n or 0.0))
                i_out = self.rail_load(rail.name)
                i_in = self.regulator_input_current(d)
                loss = None
                if (v_in is not None and i_in is not None
                        and v_out is not None and i_out is not None):
                    loss = v_in * i_in - v_out * i_out
                rail.regulators.append(RegulatorResult(
                    designator=str(d.get("label") or d.get("designator")),
                    kind=_regulator_kind(d),
                    input_rail=self.rail_of(d, "IN_P"),
                    output_rail=rail.name,
                    v_out_set=_float(d.get("value")) or 0.0,
                    v_in=v_in, i_out=i_out, i_in=i_in, loss_w=loss,
                    gain=_float(d.get("gain"))))

    def _copper(self, rail: RailReport) -> None:
        total_loss = 0.0
        results = []
        worst = None
        v_hi, v_lo = -math.inf, math.inf
        for phys, net in self.rail_layers(rail.name):
            st = self.layer_stats(phys, net)
            if st is None:
                continue
            total_loss += st["loss"]
            v_hi = max(v_hi, st["v_max"])
            v_lo = min(v_lo, st["v_min"])
            if worst is None or st["v_min"] < worst[0]:
                worst = (st["v_min"], phys, st["v_min_xy"])
            results.append((phys, net, st))
        # One row per physical layer, merging the rail's nets on it.
        by_phys: dict[str, LayerResult] = {}
        v_range: dict[str, tuple[float, float]] = {}
        for phys, net, st in results:
            lr = by_phys.get(phys)
            if lr is None:
                by_phys[phys] = LayerResult(
                    layer=phys, nets=[net], copper=self.copper_label(phys),
                    drop_across_v=st["v_max"] - st["v_min"],
                    peak_j_a_per_mm=st["peak_j"], peak_j_xy=st["peak_j_xy"],
                    loss_w=st["loss"], share=0.0)
                v_range[phys] = (st["v_min"], st["v_max"])
            else:
                lr.nets.append(net)
                lo = min(v_range[phys][0], st["v_min"])
                hi = max(v_range[phys][1], st["v_max"])
                v_range[phys] = (lo, hi)
                lr.drop_across_v = hi - lo
                lr.loss_w += st["loss"]
                if st["peak_j"] > lr.peak_j_a_per_mm:
                    lr.peak_j_a_per_mm = st["peak_j"]
                    lr.peak_j_xy = st["peak_j_xy"]
        for lr in by_phys.values():
            lr.share = lr.loss_w / total_loss if total_loss > 0 else 0.0
        rail.layers = list(by_phys.values())
        rail.loss_w = total_loss if results else None
        rail.peak_j_a_per_mm = max(
            (lr.peak_j_a_per_mm for lr in rail.layers), default=None)
        if worst is not None and worst[2] is not None:
            rail.worst_point = (worst[1], float(worst[2][0]),
                                float(worst[2][1]))
        if rail.kind == "return" and results:
            rail.ground_rise_v = v_hi - v_lo

    def _vias(self, rail: RailReport, via_rows: list[dict]) -> None:
        members = set(rail.members)
        rows = sorted((r for r in via_rows if r["net"] in members),
                      key=lambda r: -r["current"])
        limit = self.settings.via_limit_a
        rail.vias_total = len(rows)
        # Strictly above, with a hair of tolerance: a via sized to carry
        # exactly the limit (a 1 A test structure) is at it, not over it.
        over = limit * (1.0 + 1e-6)
        rail.vias_over = sum(1 for r in rows if r["current"] > over)
        rail.max_via_a = rows[0]["current"] if rows else None
        n = max(self.settings.top_vias, rail.vias_over)
        rail.vias = [ViaResult(
            x_mm=r["x_mm"], y_mm=r["y_mm"], net=r["net"], span=r["layer_span"],
            drill_mm=r.get("hole_diameter_mm"),
            fill=(r.get("ipc4761_label") or "—"),
            current_a=r["current"], delta_v=r["delta_v"], power_w=r["power"],
            over_limit=r["current"] > over) for r in rows[:n]]

    def _thermal(self, rail: RailReport) -> None:
        cfg = self.metadata.get("thermal_config") or {}
        if not cfg.get("enabled"):
            rail.thermal_note = ("Not analysed. Electrothermal coupling was "
                                 "off for this solve.")
            return
        h = _float(cfg.get("heat_transfer_w_per_m2k"))
        if not h:
            rail.thermal_note = "Not analysed: no heat-transfer coefficient."
            return
        peak_pd = 0.0
        for phys, net in self.rail_layers(rail.name):
            st = self.layer_stats(phys, net)
            if st is None:
                continue
            pds = np.concatenate([p[3] for p in st["parts"]])
            if pds.size:
                pk = (float(np.percentile(pds, _PEAK_PERCENTILE))
                      if pds.size >= 100 else float(pds.max()))
                peak_pd = max(peak_pd, pk)
        # Local lumped model, as in the solve: ΔT = p · 1e6 / h
        # (p in W/mm², h in W/m²K).
        rail.max_temp_rise_c = peak_pd * 1e6 / h
        rail.thermal_note = (
            f"Local temperature-rise estimate from copper loss alone, "
            f"h = {h:g} W/m²K, no lateral spreading. Resistivity was "
            "iterated with temperature in the solve.")

    # --- figures ---------------------------------------------------------------

    def draw_figures(self, rail: RailReport) -> None:
        from fypa.report import figures

        sections = set(self.settings.sections)
        if "power_path" in sections and rail.loads:
            loads = []
            for ld in rail.loads:
                via = ", ".join(s.designator for s in rail.series
                                if ld.net and ld.net in s.nets
                                and not s.nets.startswith(ld.net))
                loads.append({
                    "label": ld.label,
                    "value": ("—" if ld.v_load is None
                              else f"{ld.v_load:.3f} V"),
                    "status": ld.status, "via": via,
                })
            sub = (f"{rail.nominal_v:.4g} V setpoint"
                   if rail.nominal_v is not None else "")
            rail.figures["power_path"] = figures.power_path_png(
                rail.source_label.split(" (")[0], sub, rail.load_a, loads)
            rail.captions["power_path"] = (
                "Source and loads on this rail with the solved voltage at "
                "each load. Series parts on the way are shown inline.")

        if "heatmaps" not in sections or not rail.layers:
            return
        setpoint = rail.nominal_v or 0.0
        src_ret = self.src_ret.get(rail.name, 0.0)
        # Drop map on the layer holding the rail's lowest voltage.
        if rail.worst_point is not None:
            drop_layer, wx, wy = rail.worst_point
            parts = []
            for phys, net in self.rail_layers(rail.name):
                if phys != drop_layer:
                    continue
                st = self.layer_stats(phys, net)
                for xys, tri, pot, _pd, _c in (st or {}).get("parts", []):
                    drop = setpoint - (pot - src_ret)
                    parts.append((xys, tri, drop[tri].mean(axis=1)))
            pins = [(ld.worst_xy[0], ld.worst_xy[1], ld.label)
                    for ld in rail.loads
                    if ld.worst_xy and ld.worst_layer == drop_layer]
            lo = min((np.min(p[2]) for p in parts), default=0.0)
            hi = max((np.max(p[2]) for p in parts), default=0.0)
            rail.figures["drop_map"] = figures.mesh_map_png(
                parts, title=f"Voltage drop · {drop_layer}",
                unit_label="Drop from setpoint (mV)", cmap="YlOrRd",
                marker=(wx, wy, f"lowest point · {hi * 1e3:.3g} mV"),
                pins=pins, scale=1e3,
                vmin=min(lo, 0.0), vmax=hi)
            share = next((lr.share for lr in rail.layers
                          if lr.layer == drop_layer), None)
            rail.captions["drop_map"] = (
                f"Voltage drop on {drop_layer}, the layer with the rail's "
                "lowest voltage" + (f" ({share:.0%} of the rail's copper loss)"
                                    if share is not None else "")
                + ". Squares mark load pins; the circle marks the lowest point.")
        # Current-density map on the layer with the highest peak.
        top = max(rail.layers, key=lambda lr: lr.peak_j_a_per_mm)
        parts = []
        for phys, net in self.rail_layers(rail.name):
            if phys != top.layer:
                continue
            st = self.layer_stats(phys, net)
            for xys, tri, _pot, pd, cond in (st or {}).get("parts", []):
                parts.append((xys, tri, np.sqrt(np.maximum(pd * cond, 0.0))))
        marker = None
        if top.peak_j_xy:
            marker = (top.peak_j_xy[0], top.peak_j_xy[1],
                      f"peak {top.peak_j_a_per_mm:.3g} A/mm")
        rail.figures["j_map"] = figures.mesh_map_png(
            parts, title=f"Current density · {top.layer}",
            unit_label="|J| (A/mm)", cmap="inferno", vmin=0.0,
            vmax=top.peak_j_a_per_mm, marker=marker)
        rail.captions["j_map"] = (
            f"Current density on {top.layer}. The scale is clipped at the "
            f"{_PEAK_PERCENTILE:g}th percentile so pin singularities don't "
            "hide the rest of the copper.")


# --- design-level appendix ------------------------------------------------------

def _analyses_label(rails: list[RailReport], metadata: dict) -> str:
    parts = ["DC IR drop", "via current"]
    if any(r.decoupling and r.decoupling.analysed for r in rails):
        parts.append("decoupling impedance")
    if (metadata.get("thermal_config") or {}).get("enabled"):
        parts.append("electrothermal")
    return ", ".join(parts)


def _stackup_rows(metadata: dict) -> list[dict]:
    out = []
    for r in metadata.get("stackup", []):
        out.append({
            "name": r.get("name", ""),
            "copper_oz": _float(r.get("copper_thickness_oz")),
            "copper_um": (_float(r.get("copper_thickness_mm")) or 0.0) * 1000,
            "sheet_mohm_sq": _float(r.get("sheet_resistance_milliohm_per_sq")),
            "dielectric_mm": _float(r.get("dielectric_thickness_mm")),
            "plane": (r.get("plane_net_name") or "") if r.get("is_plane")
            else "",
        })
    return out


def _setup_rows(metadata: dict, solution, settings: ReportSettings,
                solve_stale: bool) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    stack = metadata.get("stackup", [])
    if stack:
        rows.append(("Stackup", f"{len(stack)} copper layers"))
    phys = metadata.get("physics_constants") or {}
    if phys:
        t = _float(phys.get("temperature_c"))
        rho = _float(phys.get("copper_resistivity_ohm_m"))
        rows.append(("Copper", (f"{t:g} °C" if t is not None else "")
                     + (f" · ρ = {rho:.3g} Ω·m" if rho else "")
                     + (f" · plating {phys['plating_thickness_mm'] * 1000:g} µm"
                        if _float(phys.get("plating_thickness_mm")) else "")))
    mesher = metadata.get("mesher_config") or {}
    n_tri = sum(int(np.asarray(t).shape[0])
                for ls in solution.layer_solutions for t in ls.triangles)
    rows.append(("Mesh", f"{n_tri:,} triangles"
                 + (f" · min angle {mesher.get('minimum_angle_deg', 0):g}°"
                    f" · max size {mesher.get('maximum_size_mm', 0):g} mm"
                    if mesher else "")))
    stats = metadata.get("solver_stats") or {}
    info = getattr(solution, "solver_info", None) or {}
    if stats or info:
        res = _float(stats.get("residual_norm", info.get("residual_norm")))
        gnd = _float(stats.get("ground_node_current_A",
                               info.get("ground_node_current")))
        rows.append(("Solver", (info.get("method") or "")
                     + (f" · residual {res:.2g}" if res is not None else "")
                     + (f" · ground-node current {gnd * 1e3:.3g} mA"
                        if gnd is not None else "")))
    thermal = metadata.get("thermal_config") or {}
    rows.append(("Electrothermal", "On" if thermal.get("enabled") else "Off"))
    rows.append(("Via current limit", f"{settings.via_limit_a:g} A per via"))
    rules = ["Load voltage ≥ PDN_MIN_V (FAIL)",
             f"margin ≥ {settings.margin_warn_pct:g} % of nominal (WARN)",
             "via |I| < limit (FAIL)", "|Z| ≤ target up to F_MAX (FAIL)",
             "capacitor flags or loop L over its warning level (WARN)"]
    if settings.drop_budget_pct is not None:
        rules.insert(2, f"drop ≤ {settings.drop_budget_pct:g} % of nominal "
                        "(FAIL)")
    if settings.j_limit_a_per_mm is not None:
        rules.append(f"peak |J| ≤ {settings.j_limit_a_per_mm:g} A/mm (WARN)")
    rows.append(("Pass rules", " · ".join(rules)))
    rows.append(("Definitions",
                 "Voltage at a load = lowest supply pin minus highest return "
                 "pin. Drop = setpoint minus that voltage, so it includes the "
                 "return path. Load current = sum of the rail's sinks plus "
                 "the estimated input current of any regulator it feeds."))
    rows.append(("Solve status", "Stale: the design was edited after this "
                 "solve" if solve_stale else "Current"))
    return rows


# Colours for the embedded topology diagram — a light variant of the viewer's
# palette, matching the report page.
_TOPOLOGY_THEME = {
    "bg": "#ffffff", "bg_alt": "#f6f7f9", "fg": "#1d1d1f",
    "fg_dim": "#5f6670", "err": "#b3261e", "border": "#b8b8b8",
    "accent": "#1a5fbf",
}


def _topology_svg(metadata: dict) -> str | None:
    try:
        from fypa.topology import build_topology_model, render_topology_svg
        model = build_topology_model(metadata)
        if not getattr(model, "nodes", None):
            return None
        return render_topology_svg(model, theme=_TOPOLOGY_THEME)
    except Exception:
        log.warning("Topology diagram unavailable for the report",
                    exc_info=True)
        return None
