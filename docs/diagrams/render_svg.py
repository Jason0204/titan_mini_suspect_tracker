"""把 docs/diagrams/src/*.svg 渲染成 2 倍分辨率的 PNG。

用 Qt 的 QSvgRenderer（ui_venv 里已有 PySide6），不依赖 cairosvg / Inkscape。

    ui_venv/Scripts/python.exe docs/diagrams/render_svg.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

HERE = Path(__file__).resolve().parent
SRC = HERE / "src"
SCALE = 2.0


def render(svg_path: Path) -> Path:
    renderer = QSvgRenderer(str(svg_path))
    if not renderer.isValid():
        raise RuntimeError(f"invalid SVG: {svg_path}")
    size: QSize = renderer.defaultSize()
    image = QImage(
        int(size.width() * SCALE),
        int(size.height() * SCALE),
        QImage.Format.Format_ARGB32_Premultiplied,
    )
    image.fill(QColor("#ffffff"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    renderer.render(painter)
    painter.end()
    out = HERE / (svg_path.stem + ".png")
    if not image.save(str(out)):
        raise RuntimeError(f"cannot save {out}")
    return out


def main() -> int:
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)
    assert app is not None
    files = sorted(SRC.glob("*.svg"))
    if not files:
        raise SystemExit(f"no SVG found in {SRC}")
    for svg in files:
        out = render(svg)
        print(f"{svg.name} -> {out.name} ({out.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
