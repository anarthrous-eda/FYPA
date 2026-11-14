#!/usr/bin/env python3
"""FYPA launcher shim.

The implementation lives in the :mod:`fypa` package - see ``fypa/cli.py`` and
its sibling modules. This three-line shim stays at the repository root so that
``python FYPA.py <subcommand> ...``, the PyInstaller spec
(``packaging/FYPA.spec``) and the Altium launcher (``packaging/Run_FYPA.pas``)
keep working unchanged while the modules themselves are tidied away under
``fypa/``.
"""
import sys

if __name__ == "__main__":
    # Required on Windows when frozen with PyInstaller. pdnsolver runs the
    # mesher in a ProcessPoolExecutor; on Windows the workers re-launch this
    # same .exe under the 'spawn' start method, and without freeze_support()
    # each child would re-enter main() instead of the worker bootstrap -
    # an infinite-loop GUI spawn. No-op in a dev checkout and on POSIX.
    import multiprocessing

    multiprocessing.freeze_support()

    from fypa.cli import main

    sys.exit(main())
