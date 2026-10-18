r"""Output-path policy for the suspect tracker.

Changed 2026-10-14: Every file the program writes (logs, recordings) lives next to the shipped
program, never in the source tree and never in a temporary extraction directory:

    <program directory>\log\ui_events_YYYY-MM-DD.log
    <program directory>\log\hardware_YYYY-MM-DD.log
    <program directory>\log\startup_YYYY-MM-DD.log
    <program directory>\recordings\suspect_YYYYMMDD_HHMMSS.mp4

For a packaged build the program directory is the one holding the EXE, so a PyInstaller onefile
build launched from ``dist\\TitanSuspectTracker.exe`` writes into ``dist\\log`` and
``dist\\recordings``. When running from a source checkout it is the project root, because that is
the program directory in that case.

Logs are split per calendar day. If the program directory is not writable (for example an install
under ``Program Files``), the ``%LOCALAPPDATA%\\TitanSuspectTracker`` fallback is used instead.

This module deliberately imports nothing but the standard library so the launcher can log startup
failures before cv2/PySide6 are imported.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

__all__ = [
    "append_log",
    "fallback_output_root",
    "hardware_log_path",
    "log_directory",
    "log_openmv_event",
    "log_ui_event",
    "output_root",
    "primary_output_root",
    "recordings_directory",
    "startup_log_path",
    "ui_event_log_path",
]


def primary_output_root() -> Path:
    # A frozen build must use sys.executable: for onefile builds sys._MEIPASS points at a
    # temporary extraction directory that is deleted on exit.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def fallback_output_root() -> Path:
    return Path(os.getenv("LOCALAPPDATA", Path.home())) / "TitanSuspectTracker"


_output_root: Path | None = None


def _directory_is_writable(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write_probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError:
        return False
    return True


def output_root() -> Path:
    global _output_root
    if _output_root is not None:
        return _output_root
    for candidate in (primary_output_root(), fallback_output_root()):
        if _directory_is_writable(candidate):
            _output_root = candidate
            return _output_root
    _output_root = fallback_output_root()
    return _output_root


def log_directory() -> Path:
    return output_root() / "log"


def _dated_log_path(stem: str) -> Path:
    # One file per calendar day so a long-running deployment does not grow a single huge file.
    return log_directory() / f"{stem}_{time.strftime('%Y-%m-%d')}.log"


def ui_event_log_path() -> Path:
    return _dated_log_path("ui_events")


def hardware_log_path() -> Path:
    return _dated_log_path("hardware")


def startup_log_path() -> Path:
    return _dated_log_path("startup")


def append_log(log_path: Path, message: str) -> None:
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def log_openmv_event(message: str) -> None:
    append_log(hardware_log_path(), message)


def log_ui_event(message: str) -> None:
    append_log(ui_event_log_path(), message)


def recordings_directory() -> Path:
    return output_root() / "recordings"
