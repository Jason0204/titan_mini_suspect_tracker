from __future__ import annotations

import ctypes
import traceback
from datetime import datetime
from pathlib import Path

# Changed 2026-10-14: The startup log shares the program-directory / dated-file layout used by the
# other logs (see suspect_tracker.paths). Only the standard library is imported here so a failure
# inside cv2/PySide6 can still be reported to the user.
from suspect_tracker.paths import startup_log_path
from suspect_tracker.single_instance import SingleInstance, notify_existing_instance


# Changed 2026-09-20: Persist startup stages because windowed EXE builds have no console output.
def _write_startup_log(message: str) -> Path:
    log_path = startup_log_path()
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")
    except OSError:
        pass
    return log_path


def _already_running_message() -> None:
    # Changed 2026-10-14: Deliberately bilingual -- this runs before Qt (and therefore before the
    # language selector) exists, so there is no stored language preference to honour yet.
    ctypes.windll.user32.MessageBoxW(
        0,
        "Titan 嫌疑人追踪系统已经在运行了，不需要重复打开。\n"
        "已为你切换到正在运行的那个窗口。\n\n"
        "Titan Suspect Tracker is already running; the existing window has been brought to "
        "the front. Only one instance may run at a time.",
        "Titan Suspect Tracker",
        0x40,
    )


def _run() -> int:
    _write_startup_log("Launcher started")

    # Changed 2026-10-14: Single instance. There is only one Titan Mini board, so a second window
    # could only steal the COM port from the first one. The check runs before PySide6 is imported
    # so a duplicate launch neither flashes an empty window nor pays the Qt startup cost.
    instance = SingleInstance(log=_write_startup_log)
    if not instance.acquire():
        _write_startup_log("Another instance is already running; handing over and exiting")
        # Ask the running window to come to the front, then leave.
        delivered = notify_existing_instance()
        _write_startup_log(f"Activation request delivered to the running instance: {delivered}")
        _already_running_message()
        return 0

    _write_startup_log("Single-instance mutex acquired")
    try:
        from suspect_tracker.app import main

        _write_startup_log("Application modules imported")
        exit_code = main(_write_startup_log, instance)
        _write_startup_log(f"Application exited with code {exit_code}")
        return exit_code
    except BaseException:
        details = traceback.format_exc()
        log_path = _write_startup_log(details)
        ctypes.windll.user32.MessageBoxW(
            0,
            f"Titan Suspect Tracker failed to start.\n\nDetails: {log_path}",
            "Titan Suspect Tracker",
            0x10,
        )
        return 1


def main() -> int:
    # Changed 2026-10-18: pyproject 的 [project.scripts] 指向 suspect_tracker.__main__:main，
    # 但之前只有 _run()，pip install . 生成的命令行入口会导入失败。补一个公开入口。
    return _run()


if __name__ == "__main__":
    raise SystemExit(_run())
