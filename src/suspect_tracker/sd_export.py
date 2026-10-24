"""Export the guard's log and recordings off the SD card over USB MSC.

The on-card files are only reachable through two channels: the serial protocol
(too slow for whole segments) and the stale D: mapping Windows keeps after the
device reclaims the card -- that one lists directories but every read fails, so
anything fetched that way arrives corrupted. The reliable path is the
firmware's MSC mode: the device hands the card to the host as a mass-storage
drive, the host copies the files, and the device resets itself back to the
guard afterwards.

Flow (driven by export_files()):
    1. open a private protocol client and stop whatever page is running;
    2. run the MSC window script: usb_msc(True) -> sleep MSC_WINDOW_S ->
       usb_msc(False) -> machine.reset();
    3. poll the drive letters for a card that passes the read test (the stale
       mapping cannot read files, a fresh mount can);
    4. copy /tracker/log and /tracker/rec into the destination folder;
    5. wait for the drive to disappear again and the COM port to come back.

The device never needs to be told to stop: the fixed window plus self-reset
keeps the whole sequence safe even if the host crashes mid-copy.

Changed 2026-10-24: extracted from the UI task so the flow is testable without
Qt and the device script stays in one reviewable place.
"""
from __future__ import annotations

import ctypes
import os
import shutil
import time
from pathlib import Path
from typing import Callable

from suspect_tracker.openmv_protocol import OpenMVProtocolClient
from suspect_tracker.paths import log_openmv_event

__all__ = ["export_files", "list_removable_drives"]

# The device keeps MSC active for this long before it disables it and resets.
# A demo card holds a handful of segments (copies take seconds); the remaining
# window is dead time the operator waits out, so the window stays short. If a
# very full card is still copying when the window ends the drive vanishes and
# the export reports a partial copy -- clicking export again gets the rest.
MSC_WINDOW_S = 45.0
MOUNT_TIMEOUT_S = 30.0
RESET_TIMEOUT_S = 45.0

# device script: hand the card to the host for a fixed window, then self-reset
# so the guard autostarts again (boot repair heals any unclosed segment).
MSC_SCRIPT = """
import board, time, machine
board.usb_msc(True)
time.sleep({window:.1f})
try:
    board.usb_msc(False)
except Exception:
    pass
time.sleep(2)
machine.reset()
""".format(window=MSC_WINDOW_S)


def list_removable_drives() -> list[str]:
    """Drive letters (with trailing backslash) Windows calls removable."""
    if os.name != "nt":
        return []
    kernel32 = ctypes.windll.kernel32
    mask = kernel32.GetLogicalDrives()
    drives: list[str] = []
    for index in range(26):
        if mask >> index & 1:
            root = f"{chr(ord('A') + index)}:\\"
            # DRIVE_REMOVABLE == 2. Some card readers report fixed, so this is
            # a preference, not a gate -- _drive_is_alive() is the real test.
            if kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)) == 2:
                drives.append(root)
    return drives


def _drive_is_alive(drive: str) -> bool:
    """True when this drive really reads files (the stale MSC cache does not).

    The stale mapping lists directories fine but every file read fails -- that
    is exactly how a healthy card is told apart from the zombie D: entry that
    stays after the device reclaims the card.
    """
    marker = Path(drive) / "tracker" / "sd_capacity_gb.txt"
    try:
        with open(marker, "rb") as handle:
            return bool(handle.read(1))
    except OSError:
        return False


def _port_available(port: str) -> bool:
    from serial.tools import list_ports

    return any(info.device == port for info in list_ports.comports())


def _wait_for_card(before: set[str], timeout: float, log: Callable[[str], None]) -> str | None:
    """Poll for a drive whose tracker directory reads. Prefers newly seen ones."""
    deadline = time.monotonic() + timeout
    known = set(before)
    while time.monotonic() < deadline:
        for drive in list_removable_drives():
            if drive in known and not _drive_is_alive(drive):
                continue  # the stale entry: lists, cannot read
            if _drive_is_alive(drive):
                return drive
        time.sleep(0.5)
    log(f"no readable tracker card within {timeout:.0f}s (drives seen: {sorted(known)})")
    return None


def _copy_tree(source: Path, dest: Path, progress: Callable[[int, int, str], None]) -> tuple[int, int]:
    """Copy every file under source into dest. Returns (copied, failed)."""
    copied = failed = 0
    try:
        names = sorted(os.listdir(source))
    except OSError as exc:
        progress(0, 0, f"{source}: {exc}")
        return 0, 0
    dest.mkdir(parents=True, exist_ok=True)
    total = len(names)
    for index, name in enumerate(names):
        target = dest / name
        try:
            shutil.copyfile(source / name, target)
            copied += 1
        except OSError:
            failed += 1
        progress(index + 1, total, name)
    return copied, failed


def export_files(
    port: str,
    dest_dir: Path,
    baudrate: int = 921600,
    log: Callable[[str], None] | None = None,
    stage: Callable[[str], None] | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, object]:
    """Run the whole MSC export. Returns a summary dict.

    Raises RuntimeError with an operator-readable message when the device or
    the card does not cooperate; per-file copy errors are counted, not raised.
    """
    log = log or (lambda message: None)
    stage = stage or (lambda message: None)
    progress = progress or (lambda done, total, name: None)
    dest_dir = Path(dest_dir)

    before = set(list_removable_drives())
    log(f"drives before switch: {sorted(before)}")

    import serial

    stage("serial")
    serial_port = serial.Serial(port, baudrate, timeout=2.0)
    client = OpenMVProtocolClient(serial_port, timeout=3.0, trace=log_openmv_event)
    try:
        client.connect()
        client.stop_script_and_wait()
        stage("msc")
        # The script keeps MSC up for MSC_WINDOW_S, then disables it and resets
        # the board -- no further host interaction is needed or possible.
        client.run_script_recovering(MSC_SCRIPT)
    finally:
        serial_port.close()
    log("MSC window started; device will self-reset when the window ends")

    stage("mount")
    drive = _wait_for_card(before, MOUNT_TIMEOUT_S, log)
    if drive is None:
        raise RuntimeError("timeout waiting for the SD card to mount as a readable drive")
    log(f"card mounted at {drive}")

    stage("copy")
    copied: dict[str, int] = {}
    failed = 0
    for sub in ("log", "rec"):
        done, bad = _copy_tree(
            Path(drive) / "tracker" / sub, dest_dir / sub, progress)
        copied[sub] = done
        failed += bad
        log(f"{sub}: copied {done}, failed {bad}")

    stage("reset")
    # Wait for the device to end the window: the drive disappears, then the COM
    # port re-enumerates after the reset.
    deadline = time.monotonic() + MSC_WINDOW_S + 20.0
    while time.monotonic() < deadline and _drive_is_alive(drive):
        time.sleep(0.5)
    back = None
    deadline = time.monotonic() + RESET_TIMEOUT_S
    while time.monotonic() < deadline:
        if _port_available(port):
            back = port
            break
        time.sleep(0.5)
    if back is None:
        log(f"COM port {port} did not come back within {RESET_TIMEOUT_S:.0f}s; "
            "reconnect manually once the panel shows the guard page")

    return {
        "drive": drive,
        "dest": str(dest_dir),
        "log_files": copied["log"],
        "rec_files": copied["rec"],
        "failed": failed,
        "port_back": back is not None,
    }
