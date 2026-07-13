"""Decoupling-capacitor loop-inductance analysis.

Implements the second classic PDN parameter (after DC resistance / IR drop):
per-capacitor mounted loop inductance, per TI SWPA222A §3. Three tiers:

* Tier 1 (:mod:`fypa.caploop.tier1`) — closed-form mounted-inductance ranking
  from pad geometry, escape-via spacing, and stackup heights. Pure geometry,
  no solve.
* Tier 2 (:mod:`fypa.caploop.tier2_fem`) — plane-pair spreading inductance
  solved with the existing 2-D Laplace FEM (spreading-resistance ↔
  spreading-inductance duality), honest on split/perforated planes.
* Tier 3 (:mod:`fypa.caploop.tier3`) — full cap→plane→IC loop totals.

:mod:`fypa.caploop.identify` finds the decoupling caps and bundles the
geometry each tier consumes; :mod:`fypa.caploop.constants` holds the user
knobs and physical constants.
"""

from fypa.caploop.constants import CapLoopSettings, MU0_H_PER_MM
from fypa.caploop.identify import (
    CapInstance,
    CavityRef,
    EscapeVia,
    has_flag,
    identify_capacitors,
)
from fypa.caploop.tier1 import Tier1Result, mounted_inductance

__all__ = [
    "CapLoopSettings",
    "MU0_H_PER_MM",
    "CapInstance",
    "CavityRef",
    "EscapeVia",
    "has_flag",
    "identify_capacitors",
    "Tier1Result",
    "mounted_inductance",
]

# tier2_fem / tier3 are deliberately NOT re-exported here: importing
# tier2_fem pulls in pdnsolver (and scipy), which the identification and
# Tier-1 paths don't need. Import them directly where the solve is wanted.
