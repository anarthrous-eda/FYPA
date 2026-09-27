"""Application icons, logo pixmaps and Windows taskbar identity."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap, QPolygonF


# Application icon — shown in the title bar AND Windows taskbar.
#
# Two source files live in ``assets/``: ``icon.svg`` (master, scalable)
# and ``icon.ico`` (pre-rendered multi-resolution Windows-native, built
# from the SVG by ``tools/build_icon_ico.py``). We prefer the .ico on
# Windows because Qt's setWindowIcon → WM_SETICON path uses native
# Windows icon resources to populate the taskbar; an SVG-derived
# QPixmap doesn't always make that round-trip reliably.
# assets/ sits at the repo root — and at the bundle root when frozen by
# PyInstaller (the spec maps it there) — i.e. one level up from the fypa/
# package this fypa/viewer/ module sits in, hence ``.parents[2]``.
_ICON_DIR: Path = Path(__file__).resolve().parents[2] / "assets"


_ICON_PATH_ICO: Path = _ICON_DIR / "icon.ico"


_ICON_PATH_SVG: Path = _ICON_DIR / "icon.svg"


# Title-bar variant — text-only "FYPA" wordmark. Used as ICON_SMALL so the
# title bar shows the wordmark while the taskbar (ICON_BIG) keeps the
# full fang logo. Optional: if missing we fall back to icon.ico for both.
_ICON_PATH_ICO_TITLE: Path = _ICON_DIR / "icon_titlebar.ico"


# Cache the multi-resolution QIcon once we've built it.
_ICON_CACHE: QIcon | None = None




def _load_app_icon() -> QIcon | None:
    """Return a multi-resolution :class:`QIcon` for use in
    ``QApplication.setWindowIcon`` / ``QMainWindow.setWindowIcon``.

    Prefers ``assets/icon_titlebar.ico`` (text-only "FYPA" wordmark) so
    that QDialog / QMessageBox popups — which aren't routed through
    :func:`_force_native_window_icon` — inherit the wordmark in their
    title bar instead of the fang logo. Falls back to ``icon.ico`` if
    the title-bar variant is missing, then to rendering ``icon.svg``
    directly when running an unbuilt working tree.
    """
    global _ICON_CACHE
    if _ICON_CACHE is not None:
        return _ICON_CACHE
    # Preferred: text-only wordmark — covers all dialogs and the main
    # window's Qt-side icon. Main windows additionally call
    # _force_native_window_icon to set ICON_BIG to the full fang logo.
    for candidate in (_ICON_PATH_ICO_TITLE, _ICON_PATH_ICO):
        if candidate.is_file():
            icon = QIcon(str(candidate))
            if not icon.isNull():
                _ICON_CACHE = icon
                return icon
    # Fallback: render the SVG into a multi-resolution QIcon ourselves.
    if not _ICON_PATH_SVG.is_file():
        return None
    try:
        from PySide6.QtSvg import QSvgRenderer
    except ImportError:
        return None
    renderer = QSvgRenderer(str(_ICON_PATH_SVG))
    if not renderer.isValid():
        return None
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        painter = QPainter(pm)
        renderer.render(painter)
        painter.end()
        icon.addPixmap(pm)
    _ICON_CACHE = icon
    return icon




_EDITMODE_ICON_CACHE: dict[str, QIcon] = {}



# Tint for the editor-mode toggle icon when the button is CHECKED — the
# checked state shows a light accent background that washes a light glyph
# out, so the checked (On) state uses a near-black glyph instead.
_EDITMODE_ICON_CHECKED_COLOR = "#101010"




def _load_editmode_icon(color: str = "#d8dee9") -> QIcon | None:
    """Return the editor-mode toggle icon (``assets/icon_editmode.svg``).

    The icon carries two states: ``color`` tints the unchecked (Off) glyph
    so it reads against the dark / bluish viewport; the checked (On) glyph
    is tinted near-black so it stays legible against the light accent
    background the button shows while editor mode is active. Qt swaps the
    two automatically with the QToolButton's checked state. Cached per
    ``color``."""
    if color in _EDITMODE_ICON_CACHE:
        return _EDITMODE_ICON_CACHE[color]
    svg_path = _ICON_DIR / "icon_editmode.svg"
    if not svg_path.is_file():
        return None
    try:
        from PySide6.QtSvg import QSvgRenderer
    except ImportError:
        return None
    renderer = QSvgRenderer(str(svg_path))
    if not renderer.isValid():
        return None
    def _tinted(size: int, tint: str) -> QPixmap:
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        painter = QPainter(pm)
        renderer.render(painter)
        # Tint: paint the colour through the rendered glyph's alpha mask.
        painter.setCompositionMode(QPainter.CompositionMode_SourceIn)
        painter.fillRect(pm.rect(), QColor(tint))
        painter.end()
        return pm

    # Two states: ``color`` for the unchecked (Off) button on the dark
    # viewport; a near-black glyph for the checked (On) state, whose light
    # accent background would otherwise wash a light glyph out. Qt swaps
    # them automatically with the QToolButton's checked state.
    icon = QIcon()
    for size in (16, 24, 32, 48, 64):
        icon.addPixmap(_tinted(size, color), QIcon.Normal, QIcon.Off)
        icon.addPixmap(_tinted(size, _EDITMODE_ICON_CHECKED_COLOR),
                       QIcon.Normal, QIcon.On)
    _EDITMODE_ICON_CACHE[color] = icon
    return icon




def _triangle_icon(color: str, up: bool = True) -> QIcon:
    """A small filled-triangle :class:`QIcon` — an up triangle for SOURCE,
    a down triangle for SINK — tinted to ``color`` to match the viewport's
    directive markers (see ``PdnViewer._ROLE_MARKER_STYLE``)."""
    pm = QPixmap(20, 20)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setBrush(QColor(color))
    p.setPen(QColor("#101010"))
    if up:
        pts = [QPointF(10, 3), QPointF(17.5, 16.5), QPointF(2.5, 16.5)]
    else:
        pts = [QPointF(2.5, 3.5), QPointF(17.5, 3.5), QPointF(10, 17)]
    p.drawPolygon(QPolygonF(pts))
    p.end()
    return QIcon(pm)




# FYPA branding assets — stacked above the H2 on the welcome window.
# Both are pre-rendered PNGs because Qt's QSvgRenderer can't handle the
# clipPaths in the master SVGs (the red/blue arrow triangles are
# supposed to be clipped to inside the FYPA letters). The PNGs are
# produced by tools/build_icon_ico.py via Inkscape.
_FYPA_FANGS_PATH_PNG: Path = _ICON_DIR / "fypa_fangs_only.png"


_FYPA_TEXT_PATH_PNG: Path = _ICON_DIR / "fypa_text_only_no_triangles.png"


_FYPA_FANGS_CACHE: dict[int, QPixmap] = {}


_FYPA_TEXT_CACHE: dict[int, QPixmap] = {}




def _load_fypa_fangs_pixmap(height: int) -> QPixmap | None:
    """Return ``assets/fypa_fangs_only.png`` scaled to ``height`` px,
    aspect-preserved. Cached by height. ``None`` if the file is missing.
    """
    cached = _FYPA_FANGS_CACHE.get(height)
    if cached is not None:
        return cached
    if not _FYPA_FANGS_PATH_PNG.is_file():
        return None
    pm = QPixmap(str(_FYPA_FANGS_PATH_PNG))
    if pm.isNull():
        return None
    pm = pm.scaledToHeight(height, Qt.SmoothTransformation)
    _FYPA_FANGS_CACHE[height] = pm
    return pm




def _load_fypa_text_pixmap(height: int) -> QPixmap | None:
    """Return ``assets/fypa_text_only_no_triangles.png`` scaled to
    ``height`` px, aspect-preserved. Cached by height. ``None`` if the
    file is missing.

    The PNG is the Inkscape-rendered, alpha-cropped form of
    ``fypa_text only_no_triangles.svg`` — see ``tools/build_icon_ico.py``.
    We don't render the SVG at runtime because Qt's QSvgRenderer
    silently drops clipPaths.
    """
    cached = _FYPA_TEXT_CACHE.get(height)
    if cached is not None:
        return cached
    if not _FYPA_TEXT_PATH_PNG.is_file():
        return None
    pm = QPixmap(str(_FYPA_TEXT_PATH_PNG))
    if pm.isNull():
        return None
    pm = pm.scaledToHeight(height, Qt.SmoothTransformation)
    _FYPA_TEXT_CACHE[height] = pm
    return pm




_AUMID: str = "cutreedesigns.fypa.viewer"




def _set_windows_app_user_model_id() -> None:
    """Tell Windows we are our own app (not python.exe).

    Without an explicit AppUserModelID, Windows groups the taskbar
    entry under the host interpreter (``python.exe``) and shows its
    icon there. Must be called BEFORE the first window appears.

    Silent no-op on non-Windows platforms. Logs to stderr on failure
    so the icon-not-changing case is debuggable instead of mysterious.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(_AUMID)
    except Exception as e:
        sys.stderr.write(
            f"[altium_viewer] AppUserModelID setup failed ({e}); "
            "taskbar may show python.exe's icon.\n"
        )




def _force_native_window_icon(window) -> None:
    """Push the ``assets/icon.ico`` file straight into the window via
    Win32 ``WM_SETICON`` — bypasses Qt's ``setWindowIcon`` path entirely.

    Qt's setWindowIcon does the equivalent on most setups, but in some
    Python-host configurations it ends up sending an empty/wrong icon
    handle and the taskbar falls back to python.exe's snake. Calling
    ``LoadImageW(.ico)`` + ``WM_SETICON`` ourselves is the documented
    Win32 way and is bulletproof.

    Sizing strategy: load *larger* frames than the bare system metrics
    suggest (256×256 for ICON_BIG, 64×64 for ICON_SMALL by default).
    The taskbar in Windows 10/11 paints icons in slots that are bigger
    than the legacy ICON_SMALL metric (~16px) — pulling a 256/64 frame
    from our multi-res .ico lets Windows scale **down** (sharp) instead
    of scaling our 32/16 frame up (blurry). Override with the env var
    ``FYPA_ICON_BIG`` / ``FYPA_ICON_SMALL`` if needed.

    Must be called AFTER ``window.show()`` so ``winId()`` returns a
    valid HWND.
    """
    if sys.platform != "win32" or not _ICON_PATH_ICO.is_file():
        return
    debug = bool(os.environ.get("FYPA_ICON_DEBUG"))
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadImageW.argtypes = [
            wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        user32.SendMessageW.restype = ctypes.c_ssize_t
        user32.SendMessageW.argtypes = [
            wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t,
        ]
        LR_LOADFROMFILE = 0x0010
        IMAGE_ICON = 1
        WM_SETICON = 0x0080
        ICON_SMALL = 0
        ICON_BIG = 1
        ICON_SMALL2 = 2

        # Default to the largest frame in the .ico for ICON_BIG and a
        # comfortable mid-size for ICON_SMALL. Windows scales down
        # smoothly; scaling up from 32×32 is what produced the blocky
        # taskbar icon users were seeing.
        try:
            big_size = int(os.environ.get("FYPA_ICON_BIG", "256"))
        except ValueError:
            big_size = 256
        try:
            small_size = int(os.environ.get("FYPA_ICON_SMALL", "64"))
        except ValueError:
            small_size = 64

        hwnd = int(window.winId())
        ico_big = str(_ICON_PATH_ICO)
        # ICON_SMALL drives the title bar bitmap; use the text-only
        # wordmark when available so the title bar reads "FYPA" while
        # the taskbar (ICON_BIG) still gets the fang logo.
        ico_small = (str(_ICON_PATH_ICO_TITLE)
                     if _ICON_PATH_ICO_TITLE.is_file() else ico_big)
        hicon_big = user32.LoadImageW(
            None, ico_big, IMAGE_ICON, big_size, big_size, LR_LOADFROMFILE,
        )
        hicon_small = user32.LoadImageW(
            None, ico_small, IMAGE_ICON, small_size, small_size, LR_LOADFROMFILE,
        )
        if debug:
            sys.stderr.write(
                f"[altium_viewer] icon: hwnd=0x{hwnd:x} "
                f"big={big_size}px (hicon={hicon_big!r}) "
                f"small={small_size}px (hicon={hicon_small!r})\n"
            )
        if hicon_big:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon_big)
        if hicon_small:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon_small)
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL2, hicon_small)
    except Exception as e:
        sys.stderr.write(
            f"[altium_viewer] native WM_SETICON failed ({e}); "
            "taskbar icon may not update.\n"
        )




def _set_window_aumid(window) -> None:
    """Bind the window's :data:`_AUMID` via ``SHGetPropertyStoreForWindow``
    + ``PKEY_AppUserModel_ID``. Per-window AUMID overrides the process
    AUMID for taskbar grouping; some Windows versions need this for the
    icon override to take effect for non-pinned launches.

    Must run AFTER ``window.show()`` — needs a valid HWND.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes, POINTER, byref, c_void_p

        # COM constants and types ---------------------------------------
        # IPropertyStore IID  886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99
        IID_IPropertyStore = (ctypes.c_ubyte * 16)(
            0xEB, 0x8E, 0x6D, 0x88,
            0xF2, 0x8C, 0x46, 0x44,
            0x8D, 0x02, 0xCD, 0xBA,
            0x1D, 0xBD, 0xCF, 0x99,
        )
        # PROPERTYKEY: fmtid (GUID) + pid (DWORD). For
        # System.AppUserModel.ID the fmtid is
        # 9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3, pid = 5
        class PROPERTYKEY(ctypes.Structure):
            _fields_ = [
                ("fmtid", ctypes.c_ubyte * 16),
                ("pid", wintypes.DWORD),
            ]
        pkey = PROPERTYKEY()
        pkey.fmtid[:] = (
            0x55, 0x28, 0x4C, 0x9F,
            0x79, 0x9F, 0x39, 0x4B,
            0xA8, 0xD0, 0xE1, 0xD4,
            0x2D, 0xE1, 0xD5, 0xF3,
        )
        pkey.pid = 5

        # PROPVARIANT for VT_LPWSTR. The struct is 16 bytes total; we
        # just need the first 8 to encode the discriminator and pad,
        # then a pointer to the wide-string.
        VT_LPWSTR = 31
        class PROPVARIANT(ctypes.Structure):
            _fields_ = [
                ("vt", wintypes.USHORT),
                ("wReserved1", wintypes.USHORT),
                ("wReserved2", wintypes.USHORT),
                ("wReserved3", wintypes.USHORT),
                ("pwszVal", wintypes.LPWSTR),
                ("padding", wintypes.LARGE_INTEGER),
            ]

        SHGetPropertyStoreForWindow = ctypes.windll.shell32.SHGetPropertyStoreForWindow
        SHGetPropertyStoreForWindow.argtypes = [
            wintypes.HWND, c_void_p, POINTER(c_void_p),
        ]
        SHGetPropertyStoreForWindow.restype = ctypes.HRESULT

        store_ptr = c_void_p()
        hwnd = int(window.winId())
        hr = SHGetPropertyStoreForWindow(
            hwnd, ctypes.cast(IID_IPropertyStore, c_void_p), byref(store_ptr),
        )
        if hr != 0 or not store_ptr:
            return
        # IPropertyStore vtable layout:
        #   0: QueryInterface  1: AddRef  2: Release
        #   3: GetCount  4: GetAt  5: GetValue
        #   6: SetValue(REFPROPERTYKEY, REFPROPVARIANT)  7: Commit
        try:
            vtable = ctypes.cast(
                store_ptr.value, POINTER(POINTER(c_void_p)),
            )[0]
            SetValue = ctypes.WINFUNCTYPE(
                ctypes.HRESULT, c_void_p,
                POINTER(PROPERTYKEY), POINTER(PROPVARIANT),
            )(vtable[6])
            Commit = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p)(vtable[7])
            Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)(vtable[2])
            pv = PROPVARIANT()
            pv.vt = VT_LPWSTR
            pv.pwszVal = _AUMID
            SetValue(store_ptr, byref(pkey), byref(pv))
            Commit(store_ptr)
            Release(store_ptr)
        except Exception as e:
            sys.stderr.write(
                f"[altium_viewer] per-window AUMID setup failed ({e}); "
                "ignoring.\n"
            )
    except Exception as e:
        sys.stderr.write(
            f"[altium_viewer] SHGetPropertyStoreForWindow lookup failed "
            f"({e}); ignoring.\n"
        )
