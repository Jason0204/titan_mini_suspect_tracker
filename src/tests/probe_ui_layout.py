"""Grab a real-platform screenshot of the reworked UI (no hardware required).

Run with: ui_venv/Scripts/python.exe probe_ui_layout.py [out.png]
"""
from __future__ import annotations

import sys
from pathlib import Path

# Changed 2026-10-18: 测试脚本移到 src/tests/，src 变成上一级目录。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from suspect_tracker.ui.main_window import MainWindow
from suspect_tracker.video import Detection


def main() -> int:
    # Changed 2026-10-18: 默认输出到当前目录，不再写进已删除的 pic1013/。
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("ui_layout.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    app = QApplication(sys.argv)
    window = MainWindow()
    window.resize(1320, 820)
    window.show()

    image = np.zeros((240, 320, 3), dtype=np.uint8)
    image[:, :] = (34, 38, 46)
    detections = [
        Detection(3, (30, 40, 90, 200), 0.71),
        Detection(1, (120, 60, 190, 220), 0.88, "person"),
        Detection(7, (215, 55, 275, 205), 0.63),
    ]
    window.video_source.width, window.video_source.height, window.video_source.fps = 320, 240, 15
    window.stream_running = True
    window._handle_stats(17.4, 16.1)
    window._handle_video_frame(image, detections)
    window._handle_suspect_picked(1)
    # A fresh list object, otherwise the view treats it as "no new report" (see _handle_video_frame).
    window._handle_video_frame(image, list(detections))
    window._refresh_language()

    def grab() -> None:
        label = window.video_label
        label.grab().save(str(out.with_name("ui_layout_video.png")))
        window.grab().save(str(out))
        print("saved", out.resolve())
        print("saved", out.with_name("ui_layout_video.png").resolve())
        app.quit()

    QTimer.singleShot(700, grab)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
