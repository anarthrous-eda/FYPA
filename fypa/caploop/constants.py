"""User knobs and physical constants for the capacitor loop-inductance
analysis.

All geometry throughout :mod:`fypa.caploop` is in millimetres (the repo
convention) and every inductance is computed and stored in henries; only the
GUI converts to nH for display.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass

# Permeability of free space in H/mm (μ0 = 4π×10⁻⁷ H/m).
MU0_H_PER_MM: float = 4.0 * math.pi * 1e-10


@dataclass
class CapLoopSettings:
    """Analysis knobs for capacitor identification and loop-inductance
    modelling.

    These are display/analysis parameters, not solve physics: changing them
    never invalidates a voltage solve, so they persist in the project file's
    ``viewer_settings`` (not :class:`~fypa.altium.loader.SolveSettings`) and
    recomputing after a change is instant for Tier 1.
    """

    # --- escape-via association ------------------------------------------
    # Radius around a cap pad centre searched for candidate same-net vias.
    escape_via_search_mm: float = 3.0
    # Vias beyond this distance are not part of the local escape cluster;
    # if the nearest candidate is beyond it, the cap gets one via and a
    # "long-escape" flag instead of an empty cluster.
    escape_via_max_dist_mm: float = 2.0
    # Cluster membership window: keep candidates within this multiple of the
    # nearest candidate's distance. Rejects stitching fields that happen to
    # fall inside the search radius.
    escape_cluster_slack: float = 1.5

    # --- inductance model -------------------------------------------------
    # Mutual-coupling derating for N parallel via pairs:
    # L_N = L_single * k / N (k = 1 would be fully independent pairs).
    mutual_coupling_factor: float = 0.8
    # Tier-1 closed-form spreading radius when the cap has no target device.
    tier1_r_far_default_mm: float = 5.0
    # Fallback loop inductance for degenerate via geometry (missing drill),
    # mirroring the via-resistance model's fallback discipline.
    fallback_via_loop_nh: float = 1.0

    # --- flag thresholds ---------------------------------------------------
    # Pad-centre → escape-via distance beyond which the "long-escape" flag is
    # raised (the escape trace term starts to dominate the mount).
    long_escape_warn_mm: float = 1.0
    # Mounting-surface → nearest-reference-plane depth beyond which the
    # "far-plane" flag is raised.
    far_plane_warn_mm: float = 0.4
    # Table red-highlight threshold for the best available loop L.
    cap_l_warn_nh: float = 2.0

    # --- Tier-2 cavity geometry --------------------------------------------
    # Extra clearance added around a via bore when punching anti-pads into a
    # cavity sheet that the extraction didn't already perforate.
    plane_antipad_clearance_mm: float = 0.25

    # --- reference-plane detection -------------------------------------------
    # A cavity layer must carry a *sheet* of the net's copper, not just a pad
    # or a trace stub. Sheet-likeness is measured as the fraction of a disc of
    # ``plane_probe_radius_mm`` around the via cluster that the net's copper
    # fills; ``plane_probe_coverage`` is the minimum. The radius must be well
    # clear of an anti-pad (so a perforated plane still reads as a sheet) and
    # well above a pad's size (so a pad does not). Not surfaced in the
    # Settings tab — they discriminate plane-from-pad, not physics.
    plane_probe_radius_mm: float = 2.0
    plane_probe_coverage: float = 0.35

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "CapLoopSettings":
        """Build settings from a ``viewer_settings`` sub-dict, ignoring
        unknown keys and keeping defaults for missing ones (older or newer
        project files round-trip safely)."""
        if not d:
            return cls()
        names = {f.name for f in dataclasses.fields(cls)}
        kwargs = {}
        for k, v in d.items():
            if k in names:
                try:
                    kwargs[k] = float(v)
                except (TypeError, ValueError):
                    continue
        return cls(**kwargs)
