import sys
from collections.abc import Callable

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from suspect_tracker.resources import asset_path
from suspect_tracker.single_instance import SingleInstance
from suspect_tracker.ui.main_window import MainWindow


def _apply_app_identity(app: QApplication) -> None:
    """Window/taskbar icon + taskbar grouping, shared by source runs and frozen builds."""
    # Changed 2026-10-18: merged from the submission tree. The icon lives in
    # src/suspect_tracker/assets/ and is found through resources.asset_path() so the same
    # code works for `python -m suspect_tracker` and for the onefile EXE (_MEIPASS).
    icon_path = asset_path("icon.ico")
    if icon_path.is_file():
        icon = QIcon(str(icon_path))
        if not icon.isNull():
            app.setWindowIcon(icon)

    # Changed 2026-10-18: give the process its own AppUserModelID, otherwise Windows
    # groups the taskbar button under python.exe / the onefile launcher and shows
    # the interpreter icon instead of ours.
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "TitanMini.SuspectTracker"
            )
        except Exception:  # pragma: no cover - cosmetic only
            pass


# Changed 2026-09-20: Keep one entry point shared by scripts and packaged builds.
# Changed 2026-10-14: The launcher hands over the single-instance guard so this process can
# answer a second launch by raising the existing window instead of ignoring it.
def main(
    startup_log: Callable[[str], object] | None = None,
    instance: SingleInstance | None = None,
) -> int:
    log = startup_log or (lambda _message: None)
    log("Creating QApplication")
    app = QApplication(sys.argv)
    app.setApplicationName("Titan Suspect Tracker")
    app.setOrganizationName("Titan Mini Prototype")
    _apply_app_identity(app)

    # Changed 2026-09-20: Record packaged startup stages to diagnose invisible windows.
    log("Creating MainWindow")
    window = MainWindow()
    window.resize(1320, 820)

    if instance is not None:
        # Changed 2026-10-14: Listen for a second launch and surface this window when it happens.
        listening = instance.start_server(window.activate_from_duplicate_launch)
        log(f"Single-instance activation pipe listening: {listening}")

    log("Showing MainWindow")
    window.show()
    window.raise_()
    window.activateWindow()

    log("Entering Qt event loop")
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
