"""Offscreen smoke test for the reworked UI.

Covers: click-to-lock suspects, the identity keeper (confirmation window, dropout grace),
annotated recording (red suspect box only), automatic-mode recording save, output-path layout
(program directory + per-day log files), localized log text, and i18n completeness.

Run with: ui_venv/Scripts/python.exe probe_ui_smoke.py
"""
from __future__ import annotations

import os
import re
import sys
import time
from datetime import date
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# Changed 2026-10-18: 测试脚本移到 src/tests/，src 变成上一级目录。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
import numpy as np
from PySide6.QtWidgets import QApplication

from suspect_tracker import paths
from suspect_tracker.ui import main_window as main_window_module
from suspect_tracker.ui.i18n import TEXT, diagnostic_text
from suspect_tracker.ui.main_window import MainWindow
from suspect_tracker.video import Detection

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail else ""))
    if not condition:
        FAILURES.append(name)


def frame() -> np.ndarray:
    image = np.zeros((240, 320, 3), dtype=np.uint8)
    image[:, :] = (40, 44, 52)
    return image


def main() -> int:
    app = QApplication([])
    window = MainWindow()
    window.show()
    window.video_source.width, window.video_source.height, window.video_source.fps = 320, 240, 15
    window.stream_running = True

    # --- 1. pick a suspect -------------------------------------------------------------
    window._handle_video_frame(frame(), [Detection(7, (50, 60, 120, 200), 0.82)])
    window._handle_suspect_picked(7)
    check("pick locks the ID", window.suspect_track_id == 7 and window.suspect_marked)
    check(
        "red box is shown immediately",
        window.video_label._tracking_box == (50, 60, 120, 200),
        str(window.video_label._tracking_box),
    )

    # --- 2. the device hands out a NEW id for the same person --------------------------
    # Changed 2026-10-14: with a single plausible candidate the lock moves on the FIRST report
    # (the device churns IDs faster than the report rate at VGA, so a two-report confirmation
    # on the same new ID never completed on hardware).
    window._handle_video_frame(frame(), [Detection(9, (52, 62, 122, 202), 0.80)])
    check(
        "sole candidate moves the lock on the first report",
        window.suspect_track_id == 9,
        f"suspect_track_id={window.suspect_track_id}",
    )
    check("re-lock is announced", "身份已重新确认" in window.event_log.toPlainText())
    check(
        "red box follows the new ID",
        window.video_label._tracking_box == (52, 62, 122, 202),
        str(window.video_label._tracking_box),
    )

    # --- 3. short dropout keeps the last known box, then gives up ----------------------
    # NOTE: the reader hands out the SAME list object until a new device report arrives, so a
    # fresh list must be built per update to simulate separate detection reports.
    window._handle_video_frame(frame(), [Detection(11, (300, 300, 318, 318), 0.4)])
    check(
        "box kept during a short dropout",
        window.video_label._tracking_box == (52, 62, 122, 202),
        str(window.video_label._tracking_box),
    )
    for _ in range(12):
        window._handle_video_frame(frame(), [Detection(11, (300, 300, 318, 318), 0.4)])
    check("box released after the grace period", window.video_label._tracking_box is None)
    check("lock itself survives the dropout", window.suspect_marked)

    # --- 4. clicks are ignored while the stream is stopped -----------------------------
    window._toggle_stream()
    check("stream stopped", not window.stream_running)
    check(
        "stopped stream only describes the frozen frame",
        "视频流已停止" in window.selection_hint_label.text(),
        window.selection_hint_label.text(),
    )
    before = window.suspect_track_id
    window._handle_suspect_picked(11)
    check("clicking a frozen frame cannot change the lock", window.suspect_track_id == before)
    check(
        "no drag-selection API is left on the video surface",
        not hasattr(window.video_label, "set_selection_enabled")
        and not hasattr(window.video_label, "selection_changed"),
    )
    window._toggle_stream()
    check("stream restarted", window.stream_running)

    # --- 5. automatic mode: recording is saved when the suspect is gone ----------------
    window.recording_mode = "automatic"
    window._handle_suspect_picked(9)
    window._handle_video_frame(frame(), [Detection(9, (60, 70, 130, 210), 0.77)])
    check("automatic mode armed the recorder", window.recorder.is_recording)
    automatic_path = Path(window.recorder.output_path) if window.recorder.output_path else None
    check(
        "recording lives next to the program",
        automatic_path is not None and automatic_path.parent == paths.recordings_directory(),
        str(automatic_path),
    )
    check(
        "automatic-mode hint explains how the MP4 is saved",
        "清除标记" in window.event_log.toPlainText(),
    )

    window._suspect_last_seen = (
        time.monotonic() - main_window_module.AUTOMATIC_RECORDING_STOP_SECONDS - 1
    )
    window._check_automatic_recording()
    check("automatic recording stopped after a sustained loss", not window.recorder.is_recording)
    check("automatic recording file kept", automatic_path is not None and automatic_path.is_file())
    check("automatic save is logged", "自动结束并保存录像" in window.event_log.toPlainText())
    check("suspect lock survives the automatic save", window.suspect_marked)

    # --- 6. manual recording burns ONLY the red suspect box ----------------------------
    window.recording_mode = "manual"
    window._start_recording()
    check("recorder armed", window.recorder.is_recording, str(window.recorder.output_path))
    for _ in range(10):
        window._handle_video_frame(
            frame(),
            [Detection(9, (60, 70, 130, 210), 0.77), Detection(12, (200, 80, 250, 200), 0.7)],
        )
    window._stop_recording()

    recorded = Path(window.recorder.output_path) if window.recorder.output_path else None
    check("recording file created", recorded is not None and recorded.is_file(), str(recorded))
    check(
        "a second recording in the same second does not overwrite the first",
        recorded is not None and automatic_path is not None and recorded != automatic_path,
        f"{automatic_path} vs {recorded}",
    )
    check(
        "recordings directory is under the program directory",
        paths.recordings_directory().parent == paths.output_root(),
        str(paths.recordings_directory()),
    )

    if recorded is not None and recorded.is_file():
        capture = cv2.VideoCapture(str(recorded))
        ok, saved = capture.read()
        capture.release()
        check("recording is readable and non-empty", ok and saved is not None)
        if ok and saved is not None:
            red = ((saved[:, :, 2] > 180) & (saved[:, :, 1] < 120) & (saved[:, :, 0] < 120)).sum()
            green = ((saved[:, :, 1] > 150) & (saved[:, :, 2] < 120)).sum()
            check("red suspect box burned into the recording", red > 100, f"red pixels={int(red)}")
            check("bystander boxes are NOT burned in", green == 0, f"green pixels={int(green)}")

    # --- 7. output layout: program directory + per-day log files -----------------------
    root = paths.output_root()
    today = date.today().isoformat()
    log_dir = paths.log_directory()
    check("output root is the program directory", root == paths.primary_output_root(), str(root))
    check("log directory is <program>/log", log_dir.parent == root, str(log_dir))
    check(
        "ui log file is dated",
        paths.ui_event_log_path().name.endswith(f"_{today}.log"),
        paths.ui_event_log_path().name,
    )
    check(
        "hardware/startup logs follow the same layout",
        paths.hardware_log_path().parent == log_dir
        and paths.startup_log_path().parent == log_dir
        and paths.hardware_log_path().name.endswith(f"_{today}.log"),
    )
    check(
        "ui event log file exists",
        paths.ui_event_log_path().is_file(),
        str(paths.ui_event_log_path()),
    )

    # --- 8. localized log text --------------------------------------------------------
    window.language = "zh"
    window._openmv_log("First OpenMV protocol frame decoded: 320x240.")
    text_zh = window.event_log.toPlainText()
    check("driver diagnostics are translated to Chinese", "已解码首帧：320x240" in text_zh)
    check(
        "untranslated driver text is passed through",
        diagnostic_text("zh", "Some brand new driver message") == "Some brand new driver message",
    )
    window.language = "en"
    window._openmv_log("First OpenMV protocol frame decoded: 320x240.")
    check(
        "English keeps the driver wording",
        "First OpenMV protocol frame decoded: 320x240." in window.event_log.toPlainText(),
    )
    window.language = "zh"
    window._log("probe line")
    log_content = paths.ui_event_log_path().read_text(encoding="utf-8")
    check("log file receives the same lines", "probe line" in log_content)

    # Untranslated hard-coded English must not appear in the Chinese UI log.
    english_markers = [
        "Command not sent",
        "Command not delivered",
        "OpenMV camera connected on",
        "Video frame error:",
    ]
    leftovers = [marker for marker in english_markers if marker in text_zh]
    check("no leftover English log templates in Chinese mode", not leftovers, ", ".join(leftovers))

    # --- 8b. duplicate launch -----------------------------------------------------------------
    # The single-instance guard refuses the second process; the running window is asked to come
    # to the front instead, and says so in the log.
    window.activate_from_duplicate_launch()
    check(
        "duplicate launch is reported in the log",
        "重复启动" in window.event_log.toPlainText(),
        window.event_log.toPlainText()[-100:],
    )

    # --- 9. i18n completeness ---------------------------------------------------------
    source = Path(__file__).resolve().parents[1] / "suspect_tracker/ui/main_window.py"
    used = set(
        re.findall(r'text\(\s*self\.language\s*,\s*"([a-z_]+)"', source.read_text(encoding="utf-8"))
    )
    used.update(
        {
            "fps",
            "fps_idle",
            "frame_size",
            "log_file_hint",
            "output_directory",
            "open_output_directory",
            "recording_started",
            "suspect_relocked",
            "suspect_ambiguous",
            "selection_hint_stopped",
        }
    )
    for language in ("zh", "en"):
        missing = sorted(key for key in used if key not in TEXT[language])
        check(f"i18n keys present in {language}", not missing, ", ".join(missing))
    unused_roi_keys = [
        key
        for key in ("roi_selected", "selection_hint_paused", "pause_required_title")
        if key in TEXT["zh"]
    ]
    check("dead paused-frame strings were removed", not unused_roi_keys, ", ".join(unused_roi_keys))

    window.close()
    app.quit()

    # Clean up this run's artifacts so the folder only holds real recordings.
    for created in (recorded, automatic_path):
        if created is not None and created.is_file():
            created.unlink()

    print()
    print("FAILURES:", FAILURES if FAILURES else "none")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
