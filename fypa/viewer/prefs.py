"""Persisted user preferences (QSettings) and the recent-projects list."""
from __future__ import annotations

import logging
import math
import os
from pathlib import Path


# QSettings keys. The org/app pair is also used by anything else in the
# project that wants a persistent preference.
_THEME_QS_ORG = "CopperTree"


_THEME_QS_APP = "FYPA"


_THEME_QS_KEY = "ui/theme"


# High-quality-zoom (supersampling / SSAA) preference for the heatmap
# canvas. Persisted so the choice survives a relaunch; defaults to on.
_SSAA_QS_KEY = "ui/supersampling"


# When enabled, File > Import Altium Design (non-clean) runs extract + FEM
# solve (with solve-cache fast path). When disabled, only design info loads
# and the user presses ↻ Solve in the viewer.
_AUTO_SOLVE_IMPORT_QS_KEY = "import/auto_solve_on_altium"


# Opacity (0..1) of via-barrel sections that carry no rail current - the 3D
# cylinder fade. Persisted so the choice survives a relaunch; defaults to 0.4.
_VIA_NO_CURRENT_OPACITY_QS_KEY = "ui/via_no_current_opacity"


_VIA_NO_CURRENT_OPACITY_DEFAULT = 0.4


_ADAPTIVE_REGULATOR_GAIN_QS_KEY = "solve/adaptive_regulator_gain"


# When enabled, the heavy mesh+solve+package runs in a child process so a cancel
# just kills the child (no QThread.terminate() that could orphan solver locks).
# Off by default; the env var FYPA_SOLVE_SUBPROCESS also forces it on.
_SOLVE_SUBPROCESS_QS_KEY = "solve/use_subprocess"


# Performance tunables that were previously env-var-only (see
# _apply_performance_prefs). fuse backend: "clipper" (default) / "shapely" /
# "verify". mesh workers: worker-process cap for meshing. minres budget:
# iterative-fallback wall-clock timeout (s).
_FUSE_BACKEND_QS_KEY = "perf/fuse_backend"


_MESH_WORKERS_QS_KEY = "perf/mesh_max_workers"


_MINRES_BUDGET_QS_KEY = "perf/minres_budget_s"


# Recent-projects list shown in the launcher's Recent panel.
# Entry kinds: ``altium`` (.PrjPcb import), ``gerber`` (Gerber file set + the
# layer mapping / stackup confirmed in the import dialogs, so it can be
# re-imported without them), ``fypa`` (project file) and ``solution``
# (a manually loaded solution ``.pkl``). Older builds skip kinds they don't
# know, so adding one is backward compatible.
_RECENT_PROJECTS_QS_KEY = "projects/recent"


_MAX_RECENT_PROJECTS = 10


# Whether the launcher's Recent side panel was left expanded. Collapsed until
# the user first opens it; remembered across launches after that.
_RECENT_PANEL_EXPANDED_QS_KEY = "ui/launcher_recent_panel_expanded"


# Whether the "Unannotated net bridges" notice pops up after a solve. On by
# default; the Bridges tab and the Messages tab list the parts either way.
_BRIDGE_NOTICE_QS_KEY = "ui/show_bridge_notice"




def _recent_settings():
    from PySide6.QtCore import QSettings
    return QSettings(_THEME_QS_ORG, _THEME_QS_APP)




def _normalize_recent_path(path: str | None) -> str | None:
    if not path or not isinstance(path, str):
        return None
    path = path.strip()
    if not path:
        return None
    try:
        return str(Path(path).resolve())
    except (OSError, ValueError):
        # ValueError: malformed path from corrupt settings (e.g. embedded
        # NUL) - keep the raw string rather than crash menu population.
        return path




def _normalize_recent_gerber_entry(entry: dict) -> dict | None:
    """Validate a ``gerber`` recent entry. Any malformed field rejects the
    whole entry - a half-valid file set can't be re-imported anyway."""
    folder = _normalize_recent_path(entry.get("folder"))
    copper_raw = entry.get("copper")
    if not folder or not isinstance(copper_raw, list) or not copper_raw:
        return None
    copper: list[list] = []
    for item in copper_raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            return None
        try:
            lid = int(item[0])
        except (TypeError, ValueError):
            return None
        p = _normalize_recent_path(item[1])
        if not p:
            return None
        copper.append([lid, p])
    drill_raw = entry.get("drill") or []
    if not isinstance(drill_raw, list):
        return None
    drill: list[str] = []
    for d in drill_raw:
        p = _normalize_recent_path(d)
        if not p:
            return None
        drill.append(p)
    stackup_raw = entry.get("stackup")
    if not isinstance(stackup_raw, list) or not stackup_raw:
        return None
    stackup: list[dict] = []
    for row in stackup_raw:
        if not isinstance(row, dict):
            return None
        try:
            lid = int(row["layer_id"])
            cu = float(row["copper_thickness_mm"])
            diel = float(row["dielectric_thickness_mm"])
        except (KeyError, TypeError, ValueError):
            return None
        if not (math.isfinite(cu) and math.isfinite(diel)):
            return None
        stackup.append({
            "layer_id": lid,
            "name": str(row.get("name") or f"Layer {lid}"),
            "copper_thickness_mm": cu,
            "dielectric_thickness_mm": diel,
        })
    out: dict = {
        "kind": "gerber", "folder": folder,
        "copper": copper, "drill": drill, "stackup": stackup,
    }
    for key in ("outline", "silk_top", "silk_bot"):
        p = _normalize_recent_path(entry.get(key))
        if p:
            out[key] = p
    return out




def _normalize_recent_entry(entry: dict) -> dict | None:
    kind = entry.get("kind")
    if kind in ("fypa", "solution"):
        path = _normalize_recent_path(entry.get("path"))
        if not path:
            return None
        return {"kind": kind, "path": path}
    if kind == "gerber":
        return _normalize_recent_gerber_entry(entry)
    if kind == "altium":
        prjpcb = _normalize_recent_path(entry.get("prjpcb_path"))
        if not prjpcb:
            return None
        out: dict[str, str] = {"kind": "altium", "prjpcb_path": prjpcb}
        pcbdoc = _normalize_recent_path(entry.get("pcbdoc_path"))
        if pcbdoc:
            out["pcbdoc_path"] = pcbdoc
        return out
    return None




def _recent_entry_key(entry: dict) -> tuple:
    kind = entry.get("kind")
    if kind in ("fypa", "solution"):
        return (kind, entry.get("path"))
    if kind == "altium":
        return ("altium", entry.get("prjpcb_path"))
    if kind == "gerber":
        # One entry per folder: the import's pseudo-PrjPcb (and so its cache
        # key) is derived from the folder, so a re-pick there replaces it.
        return ("gerber", entry.get("folder"))
    return (kind,)




# Callbacks run after every write to the recent list, so live views (the
# launcher's Recent panel) follow records, removals and Clear from any path.
_RECENT_LISTENERS: list = []




def add_recent_projects_listener(callback) -> None:
    if callback not in _RECENT_LISTENERS:
        _RECENT_LISTENERS.append(callback)




def remove_recent_projects_listener(callback) -> None:
    try:
        _RECENT_LISTENERS.remove(callback)
    except ValueError:
        pass




def _notify_recent_listeners() -> None:
    for cb in list(_RECENT_LISTENERS):
        try:
            cb()
        except RuntimeError:
            # The listening widget's C++ side is gone - forget it.
            remove_recent_projects_listener(cb)
        except Exception as e:
            logging.getLogger(__name__).debug(
                "Recent-projects listener failed (%s); ignoring.", e,
            )




def _save_recent_projects(entries: list[dict]) -> None:
    import json
    try:
        qs = _recent_settings()
        qs.setValue(_RECENT_PROJECTS_QS_KEY, json.dumps(entries))
    except Exception as e:
        logging.getLogger(__name__).debug(
            "Could not persist recent projects (%s); ignoring.", e,
        )
    _notify_recent_listeners()




def load_recent_projects() -> list[dict]:
    """Return the persisted recent-project list (newest first)."""
    import json
    try:
        qs = _recent_settings()
        raw = qs.value(_RECENT_PROJECTS_QS_KEY, "[]")
        if not isinstance(raw, str):
            return []
        data = json.loads(raw)
        if not isinstance(data, list):
            return []
        out: list[dict] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            norm = _normalize_recent_entry(item)
            if norm is not None:
                out.append(norm)
        return out[:_MAX_RECENT_PROJECTS]
    except Exception as e:
        logging.getLogger(__name__).debug(
            "Could not read recent projects (%s); using empty list.", e,
        )
    return []




def record_recent_project(entry: dict) -> None:
    """Push *entry* to the front of the recent list, deduplicating by path."""
    norm = _normalize_recent_entry(entry)
    if norm is None:
        return
    entries = load_recent_projects()
    key = _recent_entry_key(norm)
    entries = [e for e in entries if _recent_entry_key(e) != key]
    entries.insert(0, norm)
    _save_recent_projects(entries[:_MAX_RECENT_PROJECTS])




def clear_recent_projects() -> None:
    _save_recent_projects([])




def _recent_entry_exists(entry: dict) -> bool:
    kind = entry.get("kind")
    if kind in ("fypa", "solution"):
        return Path(entry["path"]).exists()
    if kind == "altium":
        return Path(entry["prjpcb_path"]).exists()
    if kind == "gerber":
        return not recent_gerber_missing_files(entry)
    return False




def recent_gerber_missing_files(entry: dict) -> list[str]:
    """Files a ``gerber`` entry needs for a re-import that no longer exist.
    Silk layers are optional to the import, so they don't count."""
    needed = [p for _lid, p in entry.get("copper", [])]
    needed += list(entry.get("drill", []))
    if entry.get("outline"):
        needed.append(entry["outline"])
    return [p for p in needed if not Path(p).exists()]




def prune_missing_recent_projects() -> list[dict]:
    """Drop entries whose files no longer exist; persist the trimmed list."""
    entries = load_recent_projects()
    kept = [e for e in entries if _recent_entry_exists(e)]
    if len(kept) != len(entries):
        _save_recent_projects(kept)
    return kept




def _remove_recent_project_entry(entry: dict) -> None:
    norm = _normalize_recent_entry(entry)
    if norm is None:
        return
    key = _recent_entry_key(norm)
    kept = [e for e in load_recent_projects() if _recent_entry_key(e) != key]
    _save_recent_projects(kept)




def recent_project_label(entry: dict) -> str:
    kind = entry.get("kind")
    if kind in ("fypa", "solution"):
        p = Path(entry["path"])
        parent = p.parent.name
        return f"{p.name} - {parent}" if parent else p.name
    if kind == "gerber":
        folder = Path(entry["folder"])
        parent = folder.parent.name
        label = f"{folder.name} - {parent}" if parent else folder.name
        n = len(entry.get("copper", []))
        return f"{label} [Gerber, {n} layer{'s' if n != 1 else ''}]"
    if kind == "altium":
        p = Path(entry["prjpcb_path"])
        parent = p.parent.name
        label = f"{p.name} - {parent}" if parent else p.name
        pcbdoc = entry.get("pcbdoc_path")
        if pcbdoc:
            label += f" [{Path(pcbdoc).name}]"
        return label
    return "?"




def recent_project_tooltip(entry: dict) -> str:
    kind = entry.get("kind")
    if kind in ("fypa", "solution"):
        return entry["path"]
    if kind == "altium":
        parts = [entry["prjpcb_path"]]
        pcbdoc = entry.get("pcbdoc_path")
        if pcbdoc:
            parts.append(pcbdoc)
        return "\n".join(parts)
    if kind == "gerber":
        parts = [entry["folder"]]
        parts += [Path(p).name for _lid, p in entry.get("copper", [])]
        parts += [Path(p).name for p in entry.get("drill", [])]
        if entry.get("outline"):
            parts.append(Path(entry["outline"]).name)
        return "\n".join(parts)
    return ""




def _record_recent_altium_from_metadata(metadata: dict | None) -> None:
    if not metadata:
        return
    prjpcb = metadata.get("prjpcb_path")
    if not prjpcb:
        return
    entry: dict[str, str] = {"kind": "altium", "prjpcb_path": str(prjpcb)}
    pcbdoc = metadata.get("pcbdoc_path")
    if pcbdoc:
        entry["pcbdoc_path"] = str(pcbdoc)
    record_recent_project(entry)




def recent_gerber_entry_from_result(result, pseudo_prjpcb: Path) -> dict:
    """Build a ``gerber`` recent entry from a ``GerberImportResult`` (duck-
    typed so this module stays free of the Gerber/Qt-dialog imports)."""
    def _opt(p) -> str | None:
        return str(p) if p else None
    entry: dict = {
        "kind": "gerber",
        "folder": str(Path(pseudo_prjpcb).parent),
        "copper": [[int(lid), str(p)] for lid, p in result.copper_files.items()],
        "drill": [str(p) for p in result.drill_files],
        "stackup": [
            {
                "layer_id": int(s.layer_id),
                "name": s.name,
                "copper_thickness_mm": float(s.copper_thickness_mm),
                "dielectric_thickness_mm": float(s.dielectric_thickness_mm),
            }
            for s in result.stackup
        ],
    }
    for key, val in (("outline", result.outline_file),
                     ("silk_top", result.silk_top_file),
                     ("silk_bot", result.silk_bot_file)):
        if _opt(val):
            entry[key] = _opt(val)
    return entry




def record_recent_gerber(result, pseudo_prjpcb: Path) -> None:
    """Record a finished Gerber import so it can be re-run from Recent."""
    try:
        record_recent_project(recent_gerber_entry_from_result(result, pseudo_prjpcb))
    except Exception as e:
        # Never let bookkeeping break a successful import.
        logging.getLogger(__name__).debug(
            "Could not record recent Gerber import (%s); ignoring.", e,
        )




def load_recent_panel_expanded() -> bool:
    """Whether the launcher's Recent side panel was left expanded."""
    try:
        val = _recent_settings().value(_RECENT_PANEL_EXPANDED_QS_KEY, False)
        if isinstance(val, str):
            return val.strip().lower() in ("1", "true", "yes", "on")
        return bool(val)
    except Exception:
        return False




def save_recent_panel_expanded(expanded: bool) -> None:
    try:
        _recent_settings().setValue(_RECENT_PANEL_EXPANDED_QS_KEY, bool(expanded))
    except Exception as e:
        logging.getLogger(__name__).debug(
            "Could not persist recent-panel state (%s); ignoring.", e,
        )




def load_supersampling_enabled() -> bool:
    """Read the persisted high-quality-zoom (SSAA) preference.

    Defaults to ``True`` - the supersampled canvas is the better
    experience and its cost is only paid while a viewer window is open.
    QSettings round-trips a bool as a native string on some backends, so
    accept ``"true"`` / ``1`` etc. as well as a real bool."""
    try:
        from PySide6.QtCore import QSettings
        qs = QSettings(_THEME_QS_ORG, _THEME_QS_APP)
        val = qs.value(_SSAA_QS_KEY, True)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.strip().lower() in ("1", "true", "yes", "on")
        return bool(int(val))
    except Exception as e:
        logging.getLogger(__name__).debug(
            "Could not read saved supersampling preference (%s); using "
            "default.", e,
        )
    return True




def save_supersampling_enabled(enabled: bool) -> None:
    """Persist the high-quality-zoom (SSAA) preference for next launch."""
    try:
        from PySide6.QtCore import QSettings
        qs = QSettings(_THEME_QS_ORG, _THEME_QS_APP)
        qs.setValue(_SSAA_QS_KEY, bool(enabled))
    except Exception as e:
        logging.getLogger(__name__).debug(
            "Could not persist supersampling preference (%s); ignoring.", e,
        )




def load_auto_solve_on_import() -> bool:
    """Read whether Import Altium Design should run the solver automatically.

    Defaults to ``True`` - classic extract + solve (with solve-cache fast path).
    """
    try:
        from PySide6.QtCore import QSettings
        qs = QSettings(_THEME_QS_ORG, _THEME_QS_APP)
        val = qs.value(_AUTO_SOLVE_IMPORT_QS_KEY, True)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.strip().lower() in ("1", "true", "yes", "on")
        return bool(int(val))
    except Exception as e:
        logging.getLogger(__name__).debug(
            "Could not read auto-solve-on-import preference (%s); using "
            "default.", e,
        )
    return True




def save_auto_solve_on_import(enabled: bool) -> None:
    """Persist the auto-solve-on-import preference for next launch."""
    try:
        from PySide6.QtCore import QSettings
        qs = QSettings(_THEME_QS_ORG, _THEME_QS_APP)
        qs.setValue(_AUTO_SOLVE_IMPORT_QS_KEY, bool(enabled))
    except Exception as e:
        logging.getLogger(__name__).debug(
            "Could not persist auto-solve-on-import preference (%s); ignoring.",
            e,
        )




def load_show_bridge_notice() -> bool:
    """Read whether the unannotated-bridges notice pops up. Defaults to
    ``True``."""
    try:
        from PySide6.QtCore import QSettings
        qs = QSettings(_THEME_QS_ORG, _THEME_QS_APP)
        val = qs.value(_BRIDGE_NOTICE_QS_KEY, True)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.strip().lower() in ("1", "true", "yes", "on")
        return bool(int(val))
    except Exception as e:
        logging.getLogger(__name__).debug(
            "Could not read bridge-notice preference (%s); using default.", e,
        )
    return True




def save_show_bridge_notice(enabled: bool) -> None:
    """Persist whether the unannotated-bridges notice pops up."""
    try:
        from PySide6.QtCore import QSettings
        qs = QSettings(_THEME_QS_ORG, _THEME_QS_APP)
        qs.setValue(_BRIDGE_NOTICE_QS_KEY, bool(enabled))
    except Exception as e:
        logging.getLogger(__name__).debug(
            "Could not persist bridge-notice preference (%s); ignoring.", e,
        )


# Topology tab: "rows" (one row per power origin) or "columns" (classic), and
# whether the rows layout lists auto-bridges that sit off the power tree.
_TOPOLOGY_LAYOUT_QS_KEY = "ui/topology_layout"


_TOPOLOGY_BRIDGES_QS_KEY = "ui/topology_show_bridges"


def load_topology_layout() -> str:
    """Read the Topology tab layout. Defaults to ``"rows"``."""
    try:
        from PySide6.QtCore import QSettings
        qs = QSettings(_THEME_QS_ORG, _THEME_QS_APP)
        val = str(qs.value(_TOPOLOGY_LAYOUT_QS_KEY, "rows") or "rows")
        return val if val in ("rows", "columns") else "rows"
    except Exception as e:
        logging.getLogger(__name__).debug(
            "Could not read topology layout preference (%s); using default.", e,
        )
    return "rows"


def save_topology_layout(layout: str) -> None:
    """Persist the Topology tab layout (``"rows"`` or ``"columns"``)."""
    try:
        from PySide6.QtCore import QSettings
        qs = QSettings(_THEME_QS_ORG, _THEME_QS_APP)
        qs.setValue(_TOPOLOGY_LAYOUT_QS_KEY, layout)
    except Exception as e:
        logging.getLogger(__name__).debug(
            "Could not persist topology layout preference (%s); ignoring.", e,
        )


def load_topology_show_bridges() -> bool:
    """Read whether the rows layout lists off-tree auto-bridges. Defaults to ``False``."""
    try:
        from PySide6.QtCore import QSettings
        qs = QSettings(_THEME_QS_ORG, _THEME_QS_APP)
        val = qs.value(_TOPOLOGY_BRIDGES_QS_KEY, False)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.strip().lower() in ("1", "true", "yes", "on")
        return bool(int(val))
    except Exception as e:
        logging.getLogger(__name__).debug(
            "Could not read topology bridges preference (%s); using default.", e,
        )
    return False


def save_topology_show_bridges(enabled: bool) -> None:
    """Persist whether the rows layout lists off-tree auto-bridges."""
    try:
        from PySide6.QtCore import QSettings
        qs = QSettings(_THEME_QS_ORG, _THEME_QS_APP)
        qs.setValue(_TOPOLOGY_BRIDGES_QS_KEY, bool(enabled))
    except Exception as e:
        logging.getLogger(__name__).debug(
            "Could not persist topology bridges preference (%s); ignoring.", e,
        )


_TOPOLOGY_ADD_HANDLES_QS_KEY = "ui/topology_show_add_handles"


def load_topology_show_add_handles() -> bool:
    """Read whether the rows layout draws "+ SINK" handles. Defaults to ``True``."""
    try:
        from PySide6.QtCore import QSettings
        qs = QSettings(_THEME_QS_ORG, _THEME_QS_APP)
        val = qs.value(_TOPOLOGY_ADD_HANDLES_QS_KEY, True)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.strip().lower() in ("1", "true", "yes", "on")
        return bool(int(val))
    except Exception as e:
        logging.getLogger(__name__).debug(
            "Could not read topology add-handles preference (%s); using default.", e,
        )
    return True


def save_topology_show_add_handles(enabled: bool) -> None:
    """Persist whether the rows layout draws "+ SINK" handles."""
    try:
        from PySide6.QtCore import QSettings
        qs = QSettings(_THEME_QS_ORG, _THEME_QS_APP)
        qs.setValue(_TOPOLOGY_ADD_HANDLES_QS_KEY, bool(enabled))
    except Exception as e:
        logging.getLogger(__name__).debug(
            "Could not persist topology add-handles preference (%s); ignoring.", e,
        )


_AUTOSETUP_DIR_QS_KEY = "autosetup/datasheet_dir"


def load_autosetup_datasheet_dir() -> str:
    """Last datasheet folder used by the Topology tab's Auto setup ("" if none)."""
    try:
        from PySide6.QtCore import QSettings
        qs = QSettings(_THEME_QS_ORG, _THEME_QS_APP)
        return str(qs.value(_AUTOSETUP_DIR_QS_KEY, "") or "")
    except Exception as e:
        logging.getLogger(__name__).debug(
            "Could not read auto-setup datasheet folder (%s); using none.", e)
    return ""


def save_autosetup_datasheet_dir(path: str) -> None:
    try:
        from PySide6.QtCore import QSettings
        qs = QSettings(_THEME_QS_ORG, _THEME_QS_APP)
        qs.setValue(_AUTOSETUP_DIR_QS_KEY, path or "")
    except Exception as e:
        logging.getLogger(__name__).debug(
            "Could not persist auto-setup datasheet folder (%s); ignoring.", e)



def load_via_no_current_opacity() -> float:
    """Read the persisted opacity (0..1) for no-current via-barrel sections.

    Defaults to :data:`_VIA_NO_CURRENT_OPACITY_DEFAULT`. Clamped to [0, 1] so
    a corrupt / out-of-range stored value can't produce an invalid alpha."""
    try:
        from PySide6.QtCore import QSettings
        qs = QSettings(_THEME_QS_ORG, _THEME_QS_APP)
        val = qs.value(_VIA_NO_CURRENT_OPACITY_QS_KEY,
                       _VIA_NO_CURRENT_OPACITY_DEFAULT)
        f = float(val)
        if not math.isfinite(f):
            return _VIA_NO_CURRENT_OPACITY_DEFAULT
        return min(1.0, max(0.0, f))
    except Exception as e:
        logging.getLogger(__name__).debug(
            "Could not read via no-current opacity preference (%s); using "
            "default.", e,
        )
    return _VIA_NO_CURRENT_OPACITY_DEFAULT




def save_via_no_current_opacity(opacity: float) -> None:
    """Persist the no-current via-barrel opacity for next launch."""
    try:
        from PySide6.QtCore import QSettings
        qs = QSettings(_THEME_QS_ORG, _THEME_QS_APP)
        qs.setValue(_VIA_NO_CURRENT_OPACITY_QS_KEY,
                    min(1.0, max(0.0, float(opacity))))
    except Exception as e:
        logging.getLogger(__name__).debug(
            "Could not persist via no-current opacity preference (%s); "
            "ignoring.", e,
        )




def load_adaptive_regulator_gain() -> bool:
    """Read whether manual solves should iterate SMPS regulator gain."""
    try:
        from PySide6.QtCore import QSettings
        qs = QSettings(_THEME_QS_ORG, _THEME_QS_APP)
        val = qs.value(_ADAPTIVE_REGULATOR_GAIN_QS_KEY, False)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.strip().lower() in ("1", "true", "yes", "on")
        return bool(int(val))
    except Exception as e:
        logging.getLogger(__name__).debug(
            "Could not read adaptive-regulator-gain preference (%s); "
            "using default (off).", e,
        )
    return False




def save_adaptive_regulator_gain(enabled: bool) -> None:
    """Persist the adaptive SMPS gain checkbox for next launch."""
    try:
        from PySide6.QtCore import QSettings
        qs = QSettings(_THEME_QS_ORG, _THEME_QS_APP)
        qs.setValue(_ADAPTIVE_REGULATOR_GAIN_QS_KEY, bool(enabled))
    except Exception as e:
        logging.getLogger(__name__).debug(
            "Could not persist adaptive-regulator-gain preference (%s); "
            "ignoring.", e,
        )




def load_solve_in_subprocess() -> bool:
    """Read the persisted "run the solve in a subprocess" preference.

    Defaults to ``False`` (the in-process QThread solve). The env var
    ``FYPA_SOLVE_SUBPROCESS`` is an independent override checked alongside this
    at worker construction, so a power user can force it on without the UI."""
    try:
        from PySide6.QtCore import QSettings
        qs = QSettings(_THEME_QS_ORG, _THEME_QS_APP)
        val = qs.value(_SOLVE_SUBPROCESS_QS_KEY, False)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.strip().lower() in ("1", "true", "yes", "on")
        return bool(int(val))
    except Exception as e:
        logging.getLogger(__name__).debug(
            "Could not read solve-in-subprocess preference (%s); using "
            "default (off).", e,
        )
    return False




def save_solve_in_subprocess(enabled: bool) -> None:
    """Persist the solve-in-subprocess preference for next launch."""
    try:
        from PySide6.QtCore import QSettings
        qs = QSettings(_THEME_QS_ORG, _THEME_QS_APP)
        qs.setValue(_SOLVE_SUBPROCESS_QS_KEY, bool(enabled))
    except Exception as e:
        logging.getLogger(__name__).debug(
            "Could not persist solve-in-subprocess preference (%s); ignoring.",
            e,
        )




_FUSE_BACKENDS = ("clipper", "shapely", "verify")


# Subset offered in the Settings dialog. "verify" stays a valid backend for the
# env var / tools/bench_fuse.py qualification path, but is not a user-facing
# runtime choice (it runs both engines every bucket - a validation mode, not a
# setting to leave on).
_FUSE_BACKENDS_UI = ("clipper", "shapely")




def load_fuse_backend() -> str:
    """Read the persisted geometry-fusion backend preference.

    Defaults to the current ``FYPA_FUSE_BACKEND`` env value (or ``clipper``) so
    an externally-set env var is reflected until the user overrides it in the
    UI."""
    default = (os.environ.get("FYPA_FUSE_BACKEND", "clipper").strip().lower()
               or "clipper")
    if default not in _FUSE_BACKENDS:
        default = "clipper"
    try:
        from PySide6.QtCore import QSettings
        qs = QSettings(_THEME_QS_ORG, _THEME_QS_APP)
        val = str(qs.value(_FUSE_BACKEND_QS_KEY, default)).strip().lower()
        if val in _FUSE_BACKENDS:
            return val
    except Exception as e:
        logging.getLogger(__name__).debug(
            "Could not read fuse-backend preference (%s); using default.", e)
    return default




def save_fuse_backend(mode: str) -> None:
    """Persist the geometry-fusion backend preference."""
    try:
        from PySide6.QtCore import QSettings
        qs = QSettings(_THEME_QS_ORG, _THEME_QS_APP)
        qs.setValue(_FUSE_BACKEND_QS_KEY, str(mode).strip().lower())
    except Exception as e:
        logging.getLogger(__name__).debug(
            "Could not persist fuse-backend preference (%s); ignoring.", e)




def load_mesh_max_workers() -> int:
    """Read the persisted mesh worker-process cap. Defaults to pdnsolver's
    physical-core heuristic.

    The QSettings read is done first, and ``pdnsolver.solver`` (which drags in
    the whole scipy/pdnsolver stack) is imported only when there is no stored
    value to fall back on. This keeps launcher cold-start off the heavy import
    for every returning user who has already picked a worker count."""
    try:
        from PySide6.QtCore import QSettings
        qs = QSettings(_THEME_QS_ORG, _THEME_QS_APP)
        raw = qs.value(_MESH_WORKERS_QS_KEY, None)
        if raw is not None:
            val = int(raw)
            if val >= 1:
                return val
    except Exception as e:
        logging.getLogger(__name__).debug(
            "Could not read mesh-workers preference (%s); using default.", e)
    # No usable stored value → fall back to pdnsolver's heuristic. This is the
    # only branch that pays the heavy import (first launch, or a cleared pref).
    try:
        from pdnsolver.solver import default_mesh_max_workers
        return default_mesh_max_workers()
    except Exception as e:
        logging.getLogger(__name__).debug(
            "Could not compute default mesh workers (%s); using 1.", e)
        return 1




def save_mesh_max_workers(n: int) -> None:
    """Persist the mesh worker-process cap."""
    try:
        from PySide6.QtCore import QSettings
        qs = QSettings(_THEME_QS_ORG, _THEME_QS_APP)
        qs.setValue(_MESH_WORKERS_QS_KEY, int(n))
    except Exception as e:
        logging.getLogger(__name__).debug(
            "Could not persist mesh-workers preference (%s); ignoring.", e)




def load_minres_budget_s() -> int:
    """Read the persisted iterative-fallback timeout (seconds). Default 180."""
    try:
        from PySide6.QtCore import QSettings
        qs = QSettings(_THEME_QS_ORG, _THEME_QS_APP)
        val = int(qs.value(_MINRES_BUDGET_QS_KEY, 180))
        if val > 0:
            return val
    except Exception as e:
        logging.getLogger(__name__).debug(
            "Could not read solver-timeout preference (%s); using default.", e)
    return 180




def save_minres_budget_s(seconds: int) -> None:
    """Persist the iterative-fallback timeout (seconds)."""
    try:
        from PySide6.QtCore import QSettings
        qs = QSettings(_THEME_QS_ORG, _THEME_QS_APP)
        qs.setValue(_MINRES_BUDGET_QS_KEY, int(seconds))
    except Exception as e:
        logging.getLogger(__name__).debug(
            "Could not persist solver-timeout preference (%s); ignoring.", e)




def _apply_performance_prefs() -> None:
    """Apply the persisted Performance preferences to both execution paths:

    * ``os.environ`` - so a spawned subprocess solve (which reads these at
      import) inherits them; and, for the fuse backend, so ``_clipper_fuse``
      picks it up per call.
    * pdnsolver module globals via its setters - for the in-process solve.

    Called once at startup and again whenever a Performance control changes."""
    fb = load_fuse_backend()
    os.environ["FYPA_FUSE_BACKEND"] = fb

    mw = load_mesh_max_workers()
    os.environ["PDNSOLVER_MESH_MAX_WORKERS"] = str(mw)

    mb = load_minres_budget_s()
    os.environ["PDNSOLVER_MINRES_BUDGET_S"] = str(mb)

    try:
        from pdnsolver import solver as _solver
        _solver.set_mesh_max_workers(mw)
        _solver.set_minres_time_budget(mb)
    except Exception as e:
        logging.getLogger(__name__).debug(
            "Could not apply performance prefs to pdnsolver (%s); ignoring.", e)
