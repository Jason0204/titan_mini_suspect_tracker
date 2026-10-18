import sys
from collections.abc import Callable

from PySide6.QtWidgets import QApplication

from suspect_tracker.single_instance import SingleInstance
from suspect_tracker.ui.main_window import MainWindow


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
