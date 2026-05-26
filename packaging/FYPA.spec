# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for FYPA  --  onedir build
#
# Run via packaging/build_dist.bat (venv activated), or manually from the
# project root:
#   pyinstaller packaging/FYPA.spec
#
# This spec lives in packaging/, but the project files it references sit one
# level up at the repo root. SPECPATH (injected by PyInstaller) is this file's
# own directory, so _REPO_ROOT below resolves correctly no matter what the
# current working directory is.

import os

from PyInstaller.utils.hooks import collect_submodules

_REPO_ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))

# altium_monkey loads ~16 submodules via importlib.import_module at package
# import time (_mark_declared_public_surfaces) plus more in its lazy __getattr__.
# Static analysis misses these, so pull the whole package in explicitly.
_altium_monkey_submodules = collect_submodules('altium_monkey')

a = Analysis(
    [os.path.join(_REPO_ROOT, 'FYPA.py')],
    pathex=[],
    binaries=[],
    datas=[
        # Icon files used by the app window / taskbar
        (os.path.join(_REPO_ROOT, 'assets'), 'assets'),
    ],
    hiddenimports=[
        # PyOpenGL resolves its platform backend and array handlers at runtime
        # via string-based imports — static analysis cannot see them.
        'OpenGL.platform.win32',
        'OpenGL.arrays.numpymodule',
        'OpenGL.arrays.ctypespointers',
        # PySide6 OpenGL modules imported in pdnsolver/ui.py and gl_mesh_viewer.py
        'PySide6.QtOpenGL',
        'PySide6.QtOpenGLWidgets',
        # scipy uses lazy sub-module loading; hooks sometimes miss these
        'scipy.sparse.linalg',
        'scipy.sparse.csgraph',
        # matplotlib non-interactive backend used internally for colormap work
        'matplotlib.backends.backend_agg',
    ] + _altium_monkey_submodules,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Trim ~400 MB of unused dependencies. altium_monkey has a deferred
    # `import cadquery` inside compute_step_model_bounds_mils for STEP-file
    # 3D-model bounds inference -- FYPA only does 2D copper FEM, so the
    # cadquery / OpenCASCADE / VTK / casadi stack is dead weight.
    # The cadquery import is guarded by try/except ImportError, so removing
    # it just means a clean RuntimeError if STEP bounds inference is ever
    # triggered (which FYPA's read-only PDN analysis never does).
    excludes=[
        'tkinter',           # ~10 MB, never used
        # ---- cadquery / OpenCASCADE stack: ~370 MB ----
        'cadquery',
        'OCP',               # cadquery_ocp Python bindings
        'cadquery_ocp',
        'vtk',
        'vtkmodules',        # 3D viz used by cadquery, not by FYPA
        'casadi',            # constraint solver used by cadquery
        # ---- unused PySide6 modules: ~18 MB ----
        # FYPA only uses QtCore / QtGui / QtWidgets / QtOpenGL /
        # QtOpenGLWidgets / QtSvg. Everything else is QML / PDF /
        # virtual-keyboard infrastructure we never touch.
        'PySide6.QtQml',
        'PySide6.QtQmlModels',
        'PySide6.QtQmlWorkerScript',
        'PySide6.QtQuick',
        'PySide6.QtQuickWidgets',
        'PySide6.QtPdf',
        'PySide6.QtPdfWidgets',
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='FYPA',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # GUI launcher — no terminal window. CLI subcommands still run but their
    # stdout/stderr is discarded; rely on the file logger for diagnostics.
    console=False,
    icon=os.path.join(_REPO_ROOT, 'assets', 'icon_titlebar.ico'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name='FYPA',
)
