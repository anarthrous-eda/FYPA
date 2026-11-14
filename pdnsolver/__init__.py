"""pdnsolver - DC PDN finite-element solver, vendored & adapted from padne.

Upstream: https://github.com/atx/padne - GPL-3.0-or-later.
See LICENSE.upstream for the original licence. This vendored copy:

* replaces ``padne._cgal`` (a C++ pybind11 extension wrapping CGAL CDT) with
  the pure-Python :mod:`triangle` package in :mod:`pdnsolver.mesh`;
* drops the KiCad loader (``padne.kicad`` and ``padne.cli``) - FYPA
  ships its own loader and CLI in the project root;
* otherwise keeps :mod:`pdnsolver.problem`, :mod:`pdnsolver.solver`,
  :mod:`pdnsolver.units`, :mod:`pdnsolver.colormaps`, :mod:`pdnsolver.ui`, and
  :mod:`pdnsolver.paraview` byte-for-byte from upstream.
"""

from . import problem, mesh, solver, units, colormaps

__all__ = [
    "problem",
    "mesh",
    "solver",
    "units",
    "colormaps",
]
