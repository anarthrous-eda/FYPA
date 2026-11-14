"""Render the app's SVG icons into multi-resolution Windows .ico files
and the launcher's branding PNGs.

Two icons are produced:

* ``assets/icon.svg`` → ``assets/icon.ico`` - the full FYPA fang logo.
  Used by PyInstaller as the .exe icon, and by ``_force_native_window_icon``
  as ``ICON_BIG`` (the bitmap Windows downscales for the taskbar slot).
* ``assets/icon_titlebar.svg`` → ``assets/icon_titlebar.ico`` - the
  text-only "FYPA" wordmark. Used as ``ICON_SMALL`` so the title bar
  shows the wordmark while the taskbar keeps the fang logo.

One launcher PNG is also produced:

* ``assets/fypa_text only_no_triangles.svg`` →
  ``assets/fypa_text_only_no_triangles.png`` - the wordmark used by
  the LauncherWindow welcome screen. Pre-rendered for the same reason
  as the icons (Qt's QSvgRenderer drops clipPaths). The no-triangles
  variant is used so the wordmark reads cleanly as plain letterforms
  on the welcome panel. Rendered at 1024 px tall and cropped to the
  alpha bounding box.

Run this any time any of these SVGs change.

Rendering uses Inkscape, not Qt's QSvgRenderer. The current icon.svg
uses a clipPath to shape the gradient into the FYPA fang logo, and
Qt's SVG renderer silently drops that clip - producing a flat
unclipped gradient rectangle. Inkscape (which authored the file)
renders it correctly.

We assemble the .ico file by hand: Pillow's ICO writer has a bug where
it silently drops all but the first frame when passed multiple sizes,
which produces a single-size .ico that Windows can't use for the
taskbar (Windows looks up 16/32/48 specifically and falls back to the
host .exe's icon when those are missing).

Usage (from the project root with the venv activated):

    .venv\\Scripts\\python.exe tools\\build_icon_ico.py
"""
from __future__ import annotations

import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path


# Sizes Windows wants in a .ico. 16/32/48 are the must-haves for the
# legacy small/large icon slots; 64/128/256 give crisp scaling at
# Hi-DPI and on the modern Alt-Tab thumbnail strip.
_SIZES: tuple[int, ...] = (16, 24, 32, 48, 64, 128, 256)


def _find_inkscape() -> str | None:
    """Return a usable path to inkscape.exe, or None.

    Search order: PATH, then the standard Windows install locations.
    """
    on_path = shutil.which("inkscape")
    if on_path:
        return on_path
    candidates = [
        Path(r"C:\Program Files\Inkscape\bin\inkscape.exe"),
        Path(r"C:\Program Files\Inkscape\inkscape.exe"),
        Path(r"C:\Program Files (x86)\Inkscape\bin\inkscape.exe"),
        Path(r"C:\Program Files (x86)\Inkscape\inkscape.exe"),
    ]
    for p in candidates:
        if p.is_file():
            return str(p)
    return None


def _render_png_inkscape(inkscape: str, svg_path: Path,
                         size: int) -> bytes:
    """Render the SVG to a square PNG of ``size`` px using Inkscape.

    Returns the PNG bytes (suitable for embedding in a .ico). Uses a
    temp file because Inkscape's --export-filename writes to disk -
    there's no clean stdout PNG mode that respects the export size.
    """
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
        out_path = Path(tf.name)
    try:
        subprocess.run(
            [
                inkscape,
                "--export-type=png",
                f"--export-filename={out_path}",
                f"--export-width={size}",
                f"--export-height={size}",
                "--export-background-opacity=0",
                str(svg_path),
            ],
            check=True,
            capture_output=True,
        )
        return out_path.read_bytes()
    finally:
        try:
            out_path.unlink()
        except OSError:
            pass


def _assemble_ico(png_blobs: list[tuple[int, bytes]]) -> bytes:
    """Pack ``(size_px, png_bytes)`` entries into a Windows .ico blob.

    ICO format reminder:

    * 6-byte ``ICONDIR``: ``reserved=0, type=1, count=N``
    * ``N`` × 16-byte ``ICONDIRENTRY``: width / height (1 byte each,
      0 means 256), 4 reserved bytes, 2-byte planes, 2-byte bpp,
      4-byte image-size, 4-byte image-offset.
    * Concatenated image payloads. We use PNG (Vista+ supported).
    """
    n = len(png_blobs)
    header = struct.pack("<HHH", 0, 1, n)
    entry_size = 16
    payload_offset = len(header) + n * entry_size
    entries = bytearray()
    payloads = bytearray()
    for size_px, png in png_blobs:
        wh_byte = 0 if size_px == 256 else size_px
        entries += struct.pack(
            "<BBBBHHII",
            wh_byte, wh_byte,
            0,
            0,
            1,
            32,
            len(png),
            payload_offset,
        )
        payloads += png
        payload_offset += len(png)
    return bytes(header + entries + payloads)


def _build_one(inkscape: str, svg_path: Path, ico_path: Path) -> None:
    """Render ``svg_path`` at every size in ``_SIZES`` and write ``ico_path``."""
    print(f"\n{svg_path.name} -> {ico_path.name}")
    png_blobs: list[tuple[int, bytes]] = []
    for size in _SIZES:
        png = _render_png_inkscape(inkscape, svg_path, size)
        png_blobs.append((size, png))
        print(f"  rendered {size}x{size}  ({len(png)} bytes)")
    ico_bytes = _assemble_ico(png_blobs)
    ico_path.write_bytes(ico_bytes)
    print(f"  wrote {ico_path}  ({ico_path.stat().st_size} bytes)")


def _render_png_inkscape_height(inkscape: str, svg_path: Path,
                                  height: int) -> bytes:
    """Render the SVG at the given pixel height (width follows the
    viewBox aspect ratio). Used for the launcher wordmark, where the
    asset is much wider than it is tall."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
        out_path = Path(tf.name)
    try:
        subprocess.run(
            [
                inkscape,
                "--export-type=png",
                f"--export-filename={out_path}",
                f"--export-height={height}",
                "--export-background-opacity=0",
                str(svg_path),
            ],
            check=True,
            capture_output=True,
        )
        return out_path.read_bytes()
    finally:
        try:
            out_path.unlink()
        except OSError:
            pass


def _build_wordmark_png(inkscape: str, svg_path: Path, png_path: Path,
                         height: int = 1024) -> None:
    """Render the wordmark SVG, alpha-bbox-crop it with Pillow, and
    save to ``png_path``. The crop matters because the source SVG has
    a 45 mm × 45 mm viewBox with the wordmark in a horizontal slice;
    naive rendering leaves vertical blank space the launcher layout
    can't hide."""
    print(f"\n{svg_path.name} -> {png_path.name}")
    raw_png = _render_png_inkscape_height(inkscape, svg_path, height)
    try:
        from PIL import Image
        import numpy as np
    except ImportError as e:
        print(
            f"error: Pillow + numpy required to crop the wordmark PNG ({e})",
            file=sys.stderr,
        )
        raise
    import io
    im = Image.open(io.BytesIO(raw_png)).convert("RGBA")
    arr = np.array(im)
    alpha = arr[..., 3]
    rows = np.where(alpha.any(axis=1))[0]
    cols = np.where(alpha.any(axis=0))[0]
    if rows.size and cols.size:
        im = im.crop((
            int(cols[0]), int(rows[0]),
            int(cols[-1] + 1), int(rows[-1] + 1),
        ))
    im.save(png_path, optimize=True)
    print(f"  wrote {png_path}  ({png_path.stat().st_size} bytes, "
          f"{im.size[0]}x{im.size[1]})")


def main() -> int:
    here = Path(__file__).resolve().parent.parent
    assets = here / "assets"
    ico_jobs = [
        (assets / "icon.svg",          assets / "icon.ico"),
        (assets / "icon_titlebar.svg", assets / "icon_titlebar.ico"),
    ]
    png_jobs = [
        (assets / "fypa_text only_no_triangles.svg",
         assets / "fypa_text_only_no_triangles.png"),
    ]
    missing = [src for src, _ in (ico_jobs + png_jobs) if not src.is_file()]
    if missing:
        for src in missing:
            print(f"error: {src} not found", file=sys.stderr)
        return 1

    inkscape = _find_inkscape()
    if inkscape is None:
        print(
            "error: inkscape.exe not found on PATH or in standard install "
            "locations. Install Inkscape (https://inkscape.org) - it's "
            "needed because Qt's SVG renderer drops the clipPath used by "
            "icon.svg, which would produce a flat unclipped gradient "
            "instead of the FYPA fang logo.",
            file=sys.stderr,
        )
        return 1
    print(f"Using Inkscape: {inkscape}")

    for svg_path, ico_path in ico_jobs:
        _build_one(inkscape, svg_path, ico_path)
    for svg_path, png_path in png_jobs:
        _build_wordmark_png(inkscape, svg_path, png_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
