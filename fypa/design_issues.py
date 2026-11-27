"""The design's setup problems, as one list of records.

A load or a solve can leave things the user has to act on: a directive that
was skipped, a rail left out of the solve, a source and sink on copper that
is not connected, a mesh that failed. They reach the solve metadata as plain
lists of messages; :func:`collect_design_issues` turns them into
:class:`DesignIssue` records - severity, kind, the message, and the parts and
nets it names - for the viewer's Design issues panel
(:mod:`fypa.viewer.issues_panel`) and the warning icons on the Topology
blocks (:func:`apply_to_topology`).

Setup problems only: results that miss a limit (a load below its minimum
voltage, a via over its current limit) stay with the tabs that show them and
with the design report.

The parts and nets are read back out of the message text, by looking for the
designators and net names the design is known to have. That works on any
solve, including one saved before this module existed. An issue whose text
names no known part or net simply has nowhere to jump to.

No Qt here: the report and the tests can use it without a display.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

ERROR = "error"
WARNING = "warning"
NOTE = "note"

_SEVERITY_ORDER = {ERROR: 0, WARNING: 1, NOTE: 2}

# Annotation warnings that only say what FYPA worked out by itself. Listed
# under Notes: nothing is wrong and there is nothing to do. A board with many
# channels carries hundreds of them.
_NOTE_PATTERNS = re.compile(
    r"resolved local net|expanding to \d+ multi-channel|auto-bridged as "
    r"SERIES|auto-inferred|ignoring pin\(s\)|^.*: Vin=.* was inferred",
    re.IGNORECASE)


@dataclass(frozen=True)
class DesignIssue:
    severity: str                       # ERROR | WARNING | NOTE
    kind: str                           # stable code, e.g. "rail-not-solved"
    label: str                          # the kind in words
    message: str
    designators: tuple[str, ...] = ()   # parts the message names
    nets: tuple[str, ...] = ()          # nets / rails the message names

    @property
    def where(self) -> str:
        """The parts, else the nets, the issue is about - for the panel's
        Where column."""
        names = self.designators or self.nets
        shown = ", ".join(names[:3])
        if len(names) > 3:
            shown += f" +{len(names) - 3}"
        return shown

    @property
    def key(self) -> tuple[str, str, str]:
        """Identity across solves: the same problem reported again."""
        return (self.severity, self.kind, self.message)


# --- what the design is known to contain -------------------------------------

def _terminal_nets(term) -> set[str]:
    if not isinstance(term, dict):
        return set()
    return {str(p.get("net")) for p in term.get("pins") or []
            if isinstance(p, dict) and p.get("net")}


def known_designators(metadata: dict) -> set[str]:
    """Every part name an issue could mention: parts with a directive, parts
    the Bridges tab lists, and every placed component."""
    out: set[str] = set()
    for d in metadata.get("directives") or []:
        for k in ("designator", "label"):
            v = str(d.get(k) or "")
            # Free markers are keyed EDIT_<id>; no message names them so.
            if v and not v.startswith("EDIT_"):
                out.add(v)
    for rec in metadata.get("bridge_candidates") or []:
        if isinstance(rec, dict) and rec.get("designator"):
            out.add(str(rec["designator"]))
    for rec in metadata.get("components") or []:
        if isinstance(rec, dict) and rec.get("designator"):
            out.add(str(rec["designator"]))
    return out


def known_nets(metadata: dict) -> set[str]:
    out: set[str] = set()
    for d in metadata.get("directives") or []:
        for term in (d.get("terminals") or {}).values():
            out |= _terminal_nets(term)
    out |= {str(n) for n in metadata.get("active_nets") or []}
    canon = metadata.get("net_canonical")
    if isinstance(canon, dict):
        out |= {str(k) for k in canon} | {str(v) for v in canon.values()}
    for rec in metadata.get("bridge_candidates") or []:
        if isinstance(rec, dict):
            for k in ("p_net", "n_net", "excluded_net", "canonical_net"):
                if rec.get(k):
                    out.add(str(rec[k]))
    out.discard("")
    return out


_QUOTED = re.compile(r"'([^'\n]{1,80})'")
# Whole words that could be a part name: not inside a longer name (U1 in
# U12, R5 in CR5), not a pin of another part (the 1 in U3-1) and not the
# tail of a net name (the 3V3 in +3V3).
_WORDS = re.compile(r"(?<![\w.+\-])\w+(?!\w)")


def _find_designators(message: str, designators: set[str]) -> tuple[str, ...]:
    """Part names in ``message``, in the order they appear. One pass over
    the words: a board has thousands of parts and a load can leave hundreds
    of messages."""
    out: list[str] = []
    if not designators:
        return ()
    for word in _WORDS.findall(message):
        if word in designators and word not in out:
            out.append(word)
    return tuple(out)


def _find_nets(message: str, nets: set[str]) -> tuple[str, ...]:
    """Net names in ``message``. The loader quotes them (``Rail '+3V3'``),
    which is the only reliable way to tell ``+5V`` or ``GND`` from prose."""
    out: list[str] = []
    for m in _QUOTED.finditer(message):
        name = m.group(1)
        if name in nets and name not in out:
            out.append(name)
    return tuple(out)


# --- collecting ---------------------------------------------------------------

def collect_design_issues(metadata: dict | None, *,
                          bridges_as_warnings: bool = True
                          ) -> list[DesignIssue]:
    """Every setup problem in ``metadata``, errors first.

    ``bridges_as_warnings``: parts that join a solved rail to copper outside
    the solve are advisory - often the part really is open at DC. They count
    as warnings while the user wants to be told about them (Settings >
    General options), and as notes once they have said they don't.
    """
    if not isinstance(metadata, dict):
        return []
    designators = known_designators(metadata)
    nets = known_nets(metadata)
    out: list[DesignIssue] = []
    seen: set[tuple[str, str]] = set()

    def add(severity: str, kind: str, label: str, message, *,
            extra_nets: tuple[str, ...] = ()) -> None:
        text = str(message or "").strip()
        if not text or (kind, text) in seen:
            return
        seen.add((kind, text))
        found_nets = _find_nets(text, nets)
        for n in extra_nets:
            if n and n not in found_nets:
                found_nets += (n,)
        out.append(DesignIssue(
            severity, kind, label, text,
            designators=_find_designators(text, designators),
            nets=found_nets))

    failures = [mf for mf in metadata.get("mesh_failures") or []
                if isinstance(mf, dict)]
    for mf in failures:
        net = str(mf.get("net") or "")
        summary = str(mf.get("summary") or "").strip()
        add(ERROR, "mesh-failed", "Mesh failed",
            (f"The copper of {net} could not be meshed, so its results are "
             "missing. " if net else "Some copper could not be meshed, so its "
             "results are missing. ")
            + (summary + " " if summary else "")
            + "A yellow ring and red disc mark the place on the Board. Fix "
              "the geometry there in Altium, then reload the design.",
            extra_nets=(net,))
    if metadata.get("mesh_failed") and not failures:
        add(ERROR, "mesh-failed", "Mesh failed",
            "Meshing failed, but the copper at fault could not be located, "
            "so there is no marker on the Board. This usually means a "
            "zero-area sliver or a self-intersecting polygon. The Messages "
            "tab names the layer; fix the geometry in Altium, then reload "
            "the design.")

    for msg in metadata.get("annotation_errors") or []:
        add(ERROR, "directive-skipped", "Directive skipped", msg)
    for msg in metadata.get("connectivity_breaks") or []:
        add(ERROR, "copper-break", "Disconnected copper", msg)
    for msg in metadata.get("open_loop_rails") or []:
        add(WARNING, "rail-not-solved", "Rail not solved", msg)

    held = [str(m) for m in metadata.get("setup_held") or [] if m]
    if held:
        n = len(held)
        add(WARNING, "setup-held", "Saved setup not applied",
            f"{n} saved setup entr{'y was' if n == 1 else 'ies were'} not "
            "applied, because the design changed and "
            f"{'it' if n == 1 else 'they'} could not be matched to a part: "
            + "; ".join(held) + ". A load left out makes the results look "
            "better than they are. Review with Help > Review saved setup.")

    bridge_sev = WARNING if bridges_as_warnings else NOTE
    for msg in metadata.get("unannotated_bridges") or []:
        add(bridge_sev, "unannotated-bridge", "Unannotated bridge", msg)

    # The remaining annotation warnings. The lists above are subsets of them.
    for msg in metadata.get("annotation_warnings") or []:
        text = str(msg or "").strip()
        if any(text == i.message for i in out):
            continue
        if _NOTE_PATTERNS.search(text):
            add(NOTE, "annotation-note", "Setup note", text)
        else:
            add(WARNING, "annotation-warning", "Setup warning", text)

    out.sort(key=lambda i: _SEVERITY_ORDER.get(i.severity, 9))
    return out


def count_by_severity(issues) -> dict[str, int]:
    counts = {ERROR: 0, WARNING: 0, NOTE: 0}
    for i in issues:
        if i.severity in counts:
            counts[i.severity] += 1
    return counts


def summary_text(issues) -> str:
    """``"2 errors and 1 warning"`` - errors and warnings only; empty when
    there are neither."""
    c = count_by_severity(issues)
    parts = []
    if c[ERROR]:
        parts.append(f"{c[ERROR]} error{'s' if c[ERROR] != 1 else ''}")
    if c[WARNING]:
        parts.append(f"{c[WARNING]} warning{'s' if c[WARNING] != 1 else ''}")
    return " and ".join(parts)


# --- the Topology diagram -----------------------------------------------------

# Roles a rail-level issue ("Rail '+3V3': has a SOURCE but no SINK") is
# pinned on: the parts that put that rail in the solve. A SERIES part on the
# rail is not what is missing.
_RAIL_ROLES = frozenset({"SOURCE", "SINK", "REGULATOR"})

_POWER_TERMINALS = {
    "SOURCE": ("P",), "SINK": ("P",),
    "REGULATOR": ("OUT_P", "IN_P"),
}


def _directive_power_nets(d: dict) -> set[str]:
    terms = d.get("terminals") or {}
    names = _POWER_TERMINALS.get(str(d.get("role") or "").upper(), ())
    out: set[str] = set()
    for name in names:
        out |= _terminal_nets(terms.get(name))
    return out


def issues_by_designator(issues, directives) -> dict[str, list[DesignIssue]]:
    """``{DESIGNATOR (upper case): issues}`` - the issues that name the part,
    plus, for a source / sink / regulator, the issues that name only the rail
    its supply side sits on. Notes are left out: an icon is for something to
    look at."""
    out: dict[str, list[DesignIssue]] = {}

    def put(des: str, issue: DesignIssue) -> None:
        bucket = out.setdefault(des.upper(), [])
        if issue not in bucket:
            bucket.append(issue)

    rail_parts: list[tuple[str, set[str]]] = []
    for d in directives or []:
        if str(d.get("role") or "").upper() in _RAIL_ROLES:
            des = str(d.get("designator") or d.get("label") or "")
            nets = _directive_power_nets(d)
            if des and nets:
                rail_parts.append((des, nets))
    for issue in issues:
        if issue.severity == NOTE:
            continue
        for des in issue.designators:
            put(des, issue)
        if not issue.designators and issue.nets:
            wanted = set(issue.nets)
            for des, nets in rail_parts:
                if nets & wanted:
                    put(des, issue)
    return out


def apply_to_topology(model, issues, directives) -> int:
    """Mark the diagram's blocks with their issues: ``has_error`` /
    ``has_warning`` for the icon, and the messages appended to the block's
    hover text. Returns how many blocks were marked."""
    by_des = issues_by_designator(issues, directives)
    if not by_des:
        return 0
    marked = 0
    for node in getattr(model, "nodes", None) or []:
        mine = by_des.get(str(getattr(node, "designator", "") or "").upper())
        if not mine:
            continue
        marked += 1
        if any(i.severity == ERROR for i in mine):
            node.has_error = True
        else:
            node.has_warning = True
        lines = [f"{'Error' if i.severity == ERROR else 'Warning'} - "
                 f"{i.label}: {i.message}" for i in mine]
        tip = str(getattr(node, "tooltip", "") or "")
        node.tooltip = (tip + "\n\n" if tip else "") + "\n\n".join(lines)
    return marked


__all__ = [
    "ERROR", "WARNING", "NOTE", "DesignIssue", "apply_to_topology",
    "collect_design_issues", "count_by_severity", "issues_by_designator",
    "known_designators", "known_nets", "summary_text",
]
