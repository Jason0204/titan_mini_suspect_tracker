"""Regenerate the application icon from its SVG sources.

Outputs (all under ``src/suspect_tracker/assets/``):

* ``icon.ico`` -- multi-size Windows icon. 16/24/32 px come from ``icon-small.svg``
  (no viewfinder brackets: they turn to mud at that size), 48 px and up come from
  ``icon.svg``.  Sizes < 256 are stored as classic DIB entries (maximum
  compatibility, no Pillow needed), the 256 px entry is PNG-compressed as usual.
* ``icon.png`` -- 256x256 preview used by the README / repository.

No third-party dependency beyond PySide6 (SVG rendering); the ICO container is
assembled by hand with ``struct``.

Usage::

    .venv/Scripts/python.exe src/tools/make_icon.py
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

ASSETS = Path(__file__).resolve().parents[1] / "suspect_tracker" / "assets"

# (size, source, store_as_png)
ENTRIES: list[tuple[int, str, bool]] = [
    (16, "icon-small.svg", False),
    (24, "icon-small.svg", False),
    (32, "icon-small.svg", False),
    (48, "icon.svg", False),
    (64, "icon.svg", False),
    (128, "icon.svg", False),
    (256, "icon.svg", True),
]


def render(renderer: QSvgRenderer, size: int) -> QImage:
    """Render one SVG onto a transparent ``size`` x ``size`` canvas."""
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)
    renderer.render(painter, QRectF(0.0, 0.0, size, size))
    painter.end()
    return image


def png_bytes(image: QImage) -> bytes:
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def dib_bytes(image: QImage) -> bytes:
    """Encode as an ICO DIB entry (BITMAPINFOHEADER + XOR BGRA + all-zero AND mask).

    RGBA8888 gives straight (non-premultiplied) alpha, which is what ICO
    bitmaps expect; Qt's Format_ARGB32 is premultiplied and must not be used.
    """
    size = image.width()
    rgba = image.convertToFormat(QImage.Format_RGBA8888)
    stride = rgba.bytesPerLine()
    bits = bytes(rgba.bits())

    header = struct.pack(
        "<IiiHHIIiiII",
        40,             # biSize
        size,           # biWidth
        size * 2,       # biHeight (XOR + AND planes)
        1,              # biPlanes
        32,             # biBitCount
        0,              # biCompression = BI_RGB
        size * size * 4,  # biSizeImage
        0, 0, 0, 0,
    )
    rows = []
    for y in range(size - 1, -1, -1):  # bottom-up
        row = bits[y * stride : y * stride + size * 4]
        bgra = bytearray(row)
        bgra[0::4] = row[2::4]  # B <- R
        bgra[2::4] = row[0::4]  # R <- B
        rows.append(bytes(bgra))
    xor = b"".join(rows)
    and_stride = ((size + 31) // 32) * 4
    and_mask = b"\x00" * (and_stride * size)
    return header + xor + and_mask


def build_ico(images: list[tuple[int, bytes]]) -> bytes:
    count = len(images)
    header = struct.pack("<HHH", 0, 1, count)  # reserved, type=icon, count
    offset = 6 + 16 * count
    directory = b""
    payload = b""
    for size, blob in images:
        side = 0 if size >= 256 else size  # 0 means 256 in the ICO directory
        directory += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        payload += blob
    return header + directory + payload


def main() -> int:
    app = QGuiApplication([])

    renderers: dict[str, QSvgRenderer] = {}
    for _, source, _ in ENTRIES:
        if source not in renderers:
            svg = (ASSETS / source).read_text(encoding="utf-8")
            renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
            if not renderer.isValid():
                raise SystemExit(f"invalid SVG: {source}")
            renderers[source] = renderer

    icons: list[tuple[int, bytes]] = []
    for size, source, as_png in ENTRIES:
        image = render(renderers[source], size)
        blob = png_bytes(image) if as_png else dib_bytes(image)
        icons.append((size, blob))
        print(f"  {size:>3}px  <- {source}  ({'PNG' if as_png else 'DIB'}, {len(blob)} bytes)")

    ico_path = ASSETS / "icon.ico"
    ico_path.write_bytes(build_ico(icons))
    (ASSETS / "icon.png").write_bytes(png_bytes(render(renderers["icon.svg"], 256)))
    print(f"wrote {ico_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
