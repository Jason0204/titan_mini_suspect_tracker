"""Cross-process test for the single-instance guard (probe_single_instance.py).

The real guard uses a Windows kernel mutex plus a named pipe, so a same-process unit test is
not enough: the interesting behaviour only shows up between two processes. This probe spawns a
child that owns the guard and then plays the part of a second launch.

Uses its own mutex/pipe names so a running copy of the real application is not disturbed.

Run:  ui_venv/Scripts/python.exe probe_single_instance.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Changed 2026-10-18: 测试脚本移到 src/tests/，SRC 是上一级目录（src/）。
SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

from suspect_tracker.single_instance import SingleInstance, notify_existing_instance

PROBE_MUTEX = "TitanSuspectTracker.SingleInstance.probe"
PROBE_PIPE = "TitanSuspectTracker-instance-probe"

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"   <- {detail}"))
    if not ok:
        FAILURES.append(name)


def wait_for(path: Path, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


def child_main(state_dir: Path) -> int:
    """Owns the guard, serves the activation pipe and records what it received."""
    from PySide6.QtCore import QCoreApplication, QTimer

    app = QCoreApplication(sys.argv)
    guard = SingleInstance(mutex_name=PROBE_MUTEX, server_name=PROBE_PIPE)
    if not guard.acquire():
        (state_dir / "child-failed").write_text("mutex busy", encoding="utf-8")
        return 2
    activated = state_dir / "child-activated"
    listening = guard.start_server(
        lambda: activated.write_text("activate received", encoding="utf-8")
    )
    (state_dir / "child-ready").write_text(f"listening={listening}", encoding="utf-8")
    # Never hang the test: quit on our own after a while.
    QTimer.singleShot(25_000, app.quit)
    return app.exec()


def run_tests() -> int:
    # --- 1. same-process semantics -----------------------------------------------------
    first = SingleInstance(mutex_name=PROBE_MUTEX, server_name=PROBE_PIPE)
    check("first acquire succeeds", first.acquire() is True)

    duplicate = SingleInstance(mutex_name=PROBE_MUTEX, server_name=PROBE_PIPE)
    check("second acquire in the same process is refused", duplicate.acquire() is False)

    started = time.monotonic()
    check(
        "notify without a running pipe fails fast",
        notify_existing_instance(timeout_ms=200, server_name=PROBE_PIPE) is False,
    )
    check("notify without a pipe does not block", time.monotonic() - started < 3.0)
    first.release()

    # --- 2. cross-process ---------------------------------------------------------------
    # Changed 2026-10-18: 交接用的临时文件放到系统临时目录，不再在仓库里造 pic1013/。
    state_dir = Path(tempfile.gettempdir()) / "titan_si_probe"
    state_dir.mkdir(parents=True, exist_ok=True)
    for leftover in ("child-ready", "child-activated", "child-failed"):
        (state_dir / leftover).unlink(missing_ok=True)

    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env["QT_QPA_PLATFORM"] = "offscreen"

    child = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--child", str(state_dir)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    ready = wait_for(state_dir / "child-ready")
    check("child process started and owns the guard", ready)
    if ready:
        detail = (state_dir / "child-ready").read_text(encoding="utf-8")
        check("child is serving the activation pipe", "listening=True" in detail, detail)

    second_launch = SingleInstance(mutex_name=PROBE_MUTEX, server_name=PROBE_PIPE)
    check("duplicate launch is refused while the child runs", second_launch.acquire() is False)

    delivered = notify_existing_instance(timeout_ms=1500, server_name=PROBE_PIPE)
    check("duplicate launch can reach the running window", delivered is True)
    # The child's event loop has to service the pipe before the marker file appears.
    activated = wait_for(state_dir / "child-activated", timeout=5.0)
    check("running instance received the activation request", activated)

    # --- 3. the OS releases the guard when the owner dies (crash safety) ----------------
    child.terminate()
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=10)
    check("child process exited", child.poll() is not None, str(child.poll()))

    # The OS tears a killed process down asynchronously, so a restart immediately after a hard
    # kill can still see the mutex for a moment. Retry briefly and report the real delay: the
    # assertion is "released", not "released within microseconds".
    after_exit = SingleInstance(mutex_name=PROBE_MUTEX, server_name=PROBE_PIPE)
    started = time.monotonic()
    reacquired = after_exit.acquire()
    while not reacquired and time.monotonic() - started < 5.0:
        time.sleep(0.05)
        reacquired = after_exit.acquire()
    check(
        "guard is free again after the owner exits",
        reacquired is True,
        f"still busy after {time.monotonic() - started:.2f}s",
    )
    after_exit.release()

    print()
    if FAILURES:
        print(f"FAILURES: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("all single-instance checks passed")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--child":
        raise SystemExit(child_main(Path(sys.argv[2])))
    raise SystemExit(run_tests())
