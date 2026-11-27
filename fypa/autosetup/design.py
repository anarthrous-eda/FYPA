"""A flat view of the design for auto setup.

Joins three sources the extraction already has:

* PCB pads → the physical net each pin is on (the names editor directives
  must use);
* the compiled schematic netlist → each pin's *name* (``VIN_3``, ``OUT``,
  ``VCC(A)``), the main structural signal for classifying a part;
* schematic component parameters → part number, description, value;

plus the power-port symbols drawn on each net (altium_monkey), which mark
rails and ground.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from fypa.autosetup.nets import designator_prefix, is_ground

log = logging.getLogger(__name__)

# Parameter names tried, in order, for the manufacturer part number.
_MPN_KEYS = (
    "manufacturer part number", "manufacturer part number 1", "mpn",
    "mfr part number", "mfr. part number", "part number", "manufacturer_pn",
    "comment", "part name", "value",
)
_DESC_KEYS = ("description", "ds", "part description", "category", "comment")
_VALUE_KEYS = ("resistance", "value", "real value", "comment", "dc resistance", "dcr")


@dataclass
class Pin:
    pad: str
    name: str
    net: str        # PCB net name; "" when the pad has no net
    # The net before the loader's low-Ω merge folded 0 Ω links together
    # (``VOUT_PS1V8`` where ``net`` reads ``+PS_1V8``). Same as ``net`` for a
    # pad the merge didn't move.
    sch_net: str = ""

    def __post_init__(self) -> None:
        if not self.sch_net:
            self.sch_net = self.net


@dataclass
class Part:
    designator: str                 # physical (PCB) designator
    logical: str                    # schematic designator
    params: dict[str, str]
    pins: list[Pin] = field(default_factory=list)

    @property
    def prefix(self) -> str:
        return designator_prefix(self.logical or self.designator)

    def param(self, *keys: str) -> str:
        low = {k.lower(): v for k, v in self.params.items()}
        for k in keys:
            v = (low.get(k) or "").strip()
            if v and v not in ("*", "-", "~", "?"):
                return v
        return ""

    @property
    def mpn(self) -> str:
        return self.param(*_MPN_KEYS)

    @property
    def description(self) -> str:
        return self.param(*_DESC_KEYS)

    @property
    def value_text(self) -> str:
        return self.param(*_VALUE_KEYS)

    def nets(self) -> set[str]:
        return {p.net for p in self.pins if p.net}

    def pins_on(self, net: str) -> list[Pin]:
        return [p for p in self.pins if p.net == net]


@dataclass
class DesignView:
    parts: list[Part]
    net_names: list[str]
    # net name (upper) → power-port symbol styles drawn on it
    power_ports: dict[str, set[int]] = field(default_factory=dict)
    # designator (upper) → short description of its existing PDN setup
    configured: dict[str, str] = field(default_factory=dict)
    # nets driven by existing setup (source / regulator output), upper-cased
    driven: set[str] = field(default_factory=set)
    # net pairs joined by existing SERIES directives / 0 Ω links, upper-cased
    bridges: list[tuple[str, str]] = field(default_factory=list)
    # 0 Ω links the loader absorbed into a net merge, as (net, net) pairs of
    # pre-merge names, upper-cased. Only the schematic view uses these.
    merge_links: list[tuple[str, str]] = field(default_factory=list)
    # pre-merge net name (upper) → the net it was merged into (PCB name)
    merged_into: dict[str, str] = field(default_factory=dict)

    def canonical(self, net: str | None) -> str | None:
        """The PCB net ``net`` is on now (itself unless the merge moved it)."""
        if not net:
            return net
        return self.merged_into.get(net.upper(), net)

    def schematic_view(self) -> DesignView:
        """This view with every pin on its pre-merge net.

        The loader folds nets joined by 0 Ω links into one, so a module's
        output (``VOUT_PS1V8``) and the rail it feeds through a link
        (``+PS_1V8``, also on a connector) read as the same net - the
        regulator then has no separate output, and the connector looks like
        the rail's only driver. Classifying on the original nets keeps the
        structure; the absorbed links come back as bridges, and editor
        directives naming either side resolve to the merged net."""
        if not self.merged_into:
            return self
        parts = [Part(designator=p.designator, logical=p.logical, params=p.params,
                      pins=[Pin(pad=x.pad, name=x.name, net=x.sch_net)
                            for x in p.pins])
                 for p in self.parts]
        names = list(self.net_names)
        have = {n.upper() for n in names}
        names += sorted({x.sch_net for p in self.parts for x in p.pins
                         if x.sch_net and x.sch_net.upper() not in have})
        return DesignView(parts=parts, net_names=names,
                          power_ports=self.power_ports, configured=self.configured,
                          driven=self.driven,
                          bridges=list(self.bridges) + list(self.merge_links),
                          merged_into=self.merged_into)

    def part(self, designator: str) -> Part | None:
        key = designator.upper()
        return next((p for p in self.parts if p.designator.upper() == key), None)

    def parts_on(self, net: str) -> list[Part]:
        return [p for p in self.parts if net in p.nets()]

    def is_gnd(self, net: str) -> bool:
        return is_ground(net, frozenset(self.power_ports.get(net.upper(), ())))

    def gnd_net_of(self, part: Part) -> str | None:
        """The ground net this part has the most pins on."""
        counts: dict[str, int] = defaultdict(int)
        for p in part.pins:
            if p.net and self.is_gnd(p.net):
                counts[p.net] += 1
        return max(counts, key=lambda n: (counts[n], n)) if counts else None


def _pin_name_map(netlist) -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    for net in getattr(netlist, "nets", None) or ():
        for t in getattr(net, "terminals", ()) or ():
            des = (getattr(t, "designator", "") or "").upper()
            pins = (getattr(t, "pin", "") or "").upper()
            name = getattr(t, "pin_name", "") or ""
            if not (des and pins and name):
                continue
            # One schematic pin can map to several pads ("7,8,9" for a
            # module's ganged OUT pads).
            for pin in (p.strip() for p in pins.split(",")):
                if pin:
                    out.setdefault((des, pin), name)
    return out


def _netlist_power_ports(netlist) -> dict[str, set[int]]:
    """Nets the compiled netlist saw a power-port symbol on (style unknown)."""
    out: dict[str, set[int]] = {}
    for net in getattr(netlist, "nets", None) or ():
        graphical = getattr(net, "graphical", None)
        if graphical is not None and getattr(graphical, "power_ports", None):
            out.setdefault((net.name or "").upper(), set())
    return out


def scan_power_ports(schdoc_paths, progress: Callable[[str], None] | None = None,
                     ) -> dict[str, set[int]]:
    """Power-port symbols on every SchDoc via altium_monkey: name → styles.

    Best effort - an unreadable sheet is logged and skipped."""
    out: dict[str, set[int]] = {}
    try:
        from altium_monkey import AltiumSchDoc
    except ImportError:  # pragma: no cover - altium_monkey is a hard dependency
        return out
    for path in sorted(set(schdoc_paths)):
        if progress is not None:
            progress(f"Reading power ports: {Path(path).name}")
        try:
            doc = AltiumSchDoc(path)
            for pp in doc.get_power_ports():
                text = (pp.text or "").strip()
                if text:
                    out.setdefault(text.upper(), set()).add(int(pp.style))
        except Exception:  # noqa: BLE001 - one bad sheet shouldn't stop setup
            log.warning("Auto setup: could not read power ports from %s", path,
                        exc_info=True)
    return out


def _spec_nets(term, nets) -> set[str]:
    names: set[str] = set()
    for pin in getattr(term, "pins", ()) or ():
        idx = getattr(pin, "net_index", -1)
        if 0 <= idx < len(nets):
            name = getattr(nets[idx], "name", None)
            if name:
                names.add(name.upper())
    rn = getattr(term, "requested_net", None)
    if rn and not names:
        names.add(rn.upper())
    return names


def build_design_view(
    loaded,
    *,
    editor_directives=(),
    scan_schdocs: bool = True,
    progress: Callable[[str], None] | None = None,
) -> DesignView:
    """Build a :class:`DesignView` from a :class:`~fypa.altium.loader.LoadedProject`."""
    ex = loaded.extracted
    nets = list(getattr(ex, "nets", ()) or ())
    net_names = [getattr(n, "name", "") or "" for n in nets]
    pin_names = _pin_name_map(getattr(ex, "compiled_netlist", None))

    sch_params: dict[str, dict[str, str]] = {}
    for sc in getattr(ex, "sch_components", ()) or ():
        sch_params.setdefault(sc.designator.upper(), dict(sc.parameters or {}))

    premerge = getattr(loaded, "premerge_pad_nets", None) or {}
    merged_into: dict[str, str] = {}

    parts: list[Part] = []
    by_index: dict[int, Part] = {}
    for i, pc in enumerate(getattr(ex, "pcb_components", ()) or ()):
        logical = getattr(pc, "source_designator", "") or pc.designator
        params = dict(getattr(pc, "parameters", {}) or {})
        params.update(sch_params.get(logical.upper(), {})
                      or sch_params.get(pc.designator.upper(), {}))
        part = Part(designator=pc.designator, logical=logical, params=params)
        parts.append(part)
        by_index[i] = part
    seen: set[tuple[int, str, str]] = set()
    for pad in getattr(ex, "pads", ()) or ():
        ci = getattr(pad, "component_index", -1)
        part = by_index.get(ci)
        if part is None:
            continue
        idx = getattr(pad, "net_index", -1)
        net = net_names[idx] if 0 <= idx < len(net_names) else ""
        pad_name = getattr(pad, "designator", "") or ""
        if (ci, pad_name, net) in seen:
            continue  # the same pad on several layers
        seen.add((ci, pad_name, net))
        name = (pin_names.get((part.designator.upper(), pad_name.upper()))
                or pin_names.get((part.logical.upper(), pad_name.upper()))
                # A KiCad board names its pins itself (no netlist).
                or getattr(pad, "pin_name", "")
                or pad_name)
        orig = premerge.get((ci, pad_name.strip().upper()))
        sch_net = net
        if orig is not None and 0 <= orig < len(net_names) and net_names[orig]:
            sch_net = net_names[orig]
            if sch_net != net and net:
                merged_into[sch_net.upper()] = net
        part.pins.append(Pin(pad=pad_name, name=name, net=net, sch_net=sch_net))

    power_ports = _netlist_power_ports(getattr(ex, "compiled_netlist", None))
    for name in getattr(ex, "power_port_nets", ()) or ():
        power_ports.setdefault(name.upper(), set())     # KiCad power symbols
    if scan_schdocs:
        for name, styles in scan_power_ports(
                (getattr(ex, "schdoc_paths", {}) or {}).values(), progress).items():
            power_ports.setdefault(name, set()).update(styles)

    view = DesignView(parts=parts, net_names=net_names, power_ports=power_ports,
                      merged_into=merged_into)
    for b in getattr(loaded, "absorbed_bridges", ()) or ():
        a, c = getattr(b, "p_net_index", -1), getattr(b, "n_net_index", -1)
        if 0 <= a < len(net_names) and 0 <= c < len(net_names) and a != c:
            view.merge_links.append((net_names[a].upper(), net_names[c].upper()))
    _record_existing_setup(view, loaded, nets, editor_directives)
    return view


def _record_existing_setup(view: DesignView, loaded, nets, editor_directives) -> None:
    """What is already set up - so suggestions don't duplicate it, and so
    existing sources / regulators count as drivers of their rails."""
    from fypa.altium.annotations import (
        RegulatorSpec,
        ResistorSpec,
        SinkSpec,
        SourceSpec,
    )

    for d in getattr(getattr(loaded, "annotations", None), "directives", ()) or ():
        des = (getattr(d, "designator", "") or "").upper()
        if isinstance(d, SourceSpec):
            view.configured[des] = "SOURCE (schematic)"
            view.driven |= _spec_nets(d.p, nets)
        elif isinstance(d, RegulatorSpec):
            view.configured[des] = "REGULATOR (schematic)"
            view.driven |= _spec_nets(d.out_p, nets)
        elif isinstance(d, SinkSpec):
            view.configured[des] = "SINK (schematic)"
        elif isinstance(d, ResistorSpec):
            view.configured[des] = "SERIES (schematic)"
            a, b = _spec_nets(d.p, nets), _spec_nets(d.n, nets)
            for x in a:
                for y in b:
                    view.bridges.append((x, y))
        elif des:
            view.configured.setdefault(des, f"{type(d).__name__.removesuffix('Spec').upper()} (schematic)")
    for ed in editor_directives or ():
        des = (ed.designator or "").upper()
        if des and ed.kind == "component":
            view.configured[des] = f"{ed.role} (FYPA editor)"
        if ed.role == "SOURCE" and ed.p_net:
            view.driven.add(ed.p_net.upper())
        elif ed.role == "REGULATOR" and ed.p_net:
            view.driven.add(ed.p_net.upper())
        elif ed.role == "SERIES" and ed.p_net and ed.n_net:
            view.bridges.append((ed.p_net.upper(), ed.n_net.upper()))
