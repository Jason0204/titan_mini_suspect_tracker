"""Behaviour test for the host-side suspect identity keeper (no Qt, no hardware).

Reproduces the failure the operator reported: with two pedestrians close to each other the red
box used to hop onto the neighbour, because the lock was re-bound purely by box overlap with the
last known box. The keeper must instead keep the lock on the person that was picked.

Run with: ui_venv/Scripts/python.exe probe_suspect_lock.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Changed 2026-10-18: 测试脚本移到 src/tests/，src 变成上一级目录。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from suspect_tracker.suspect_lock import SuspectLock, box_iou, region_histogram
from suspect_tracker.video import Detection

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail else ""))
    if not condition:
        FAILURES.append(name)


BACKGROUND = (60, 60, 60)  # BGR


def scene(*people: tuple[tuple[int, int, int, int], tuple[int, int, int]]) -> np.ndarray:
    """A frame with one flat-coloured body per (box, BGR colour)."""
    frame = np.zeros((240, 320, 3), dtype=np.uint8)
    frame[:, :] = BACKGROUND
    for box, colour in people:
        x1, y1, x2, y2 = box
        frame[y1:y2, x1:x2] = colour
    return frame


RED = (40, 40, 200)
BLUE = (200, 90, 40)


def main() -> int:
    # --- 1. the histogram really does separate two people ---------------------------------
    red_frame = scene(((50, 60, 120, 200), RED))
    blue_frame = scene(((50, 60, 120, 200), BLUE))
    red_signature = region_histogram(red_frame, (50, 60, 120, 200))
    from suspect_tracker.suspect_lock import histogram_similarity

    check(
        "appearance signature matches the same person",
        histogram_similarity(red_signature, region_histogram(red_frame, (52, 62, 122, 202))) > 0.9,
    )
    check(
        "appearance signature rejects another person",
        histogram_similarity(red_signature, region_histogram(blue_frame, (50, 60, 120, 200))) < 0.4,
    )

    # --- 2. plain tracking: the picked ID is simply followed ------------------------------
    lock = SuspectLock()
    frame = scene(((50, 60, 120, 200), RED))
    lock.pick(7, [Detection(7, (50, 60, 120, 200), 0.9)], frame)
    state = lock.update([Detection(7, (54, 64, 124, 204), 0.9)], frame)
    check("picked ID stays locked while it is reported", state.track_id == 7 and state.visible)
    check("box follows the person", state.bbox == (54, 64, 124, 204), str(state.bbox))

    # --- 3. the device hands out a NEW id for the same person -----------------------------
    # Changed 2026-10-14: with a SINGLE plausible candidate the lock moves on the FIRST report.
    # Requiring the same new ID to win two reports in a row never completed on hardware when
    # the device churned IDs faster than the report rate (VGA failure, docs §14). The
    # two-report confirmation window only applies while two candidates compete (test 3b).
    frame = scene(((58, 68, 128, 208), RED))
    state = lock.update([Detection(9, (58, 68, 128, 208), 0.85)], frame)
    check("sole candidate re-binds on the first report", state.track_id == 9, str(state.track_id))
    check("re-lock is reported with both IDs", state.relocked_from == 7 and state.relocked_to == 9)
    check("red box follows the new ID", state.bbox == (58, 68, 128, 208), str(state.bbox))

    # --- 3b. two candidates competing: the confirmation window still applies --------------
    lock = SuspectLock()
    frame = scene(((50, 60, 120, 200), RED))
    lock.pick(1, [Detection(1, (50, 60, 120, 200), 0.9)], frame)
    # The old ID is gone and BOTH candidates are plausible and close in score (both well above
    # the re-lock floor). The better one must win only after winning twice -- a single report
    # must not decide between two live possibilities.
    first = (48, 62, 118, 202)
    second = (70, 62, 140, 202)
    frame = scene((first, RED), (second, RED))
    state = lock.update([Detection(5, first, 0.8), Detection(6, second, 0.8)], frame)
    check("no re-bind while candidates compete (report 1)", state.track_id == 1, str(state.track_id))
    state = lock.update([Detection(5, first, 0.8), Detection(6, second, 0.8)], frame)
    check("better candidate wins after confirmation", state.track_id == 5, str(state.track_id))

    # --- 3c. ID churn: a NEW different id every single report (VGA hardware behaviour) ----
    # The picked ID dies at once and every follow-up report carries a brand-new ID; the keeper
    # must follow the person (sole plausible candidate each time) instead of losing them.
    lock = SuspectLock()
    frame = scene(((50, 60, 120, 200), RED))
    lock.pick(1, [Detection(1, (50, 60, 120, 200), 0.9)], frame)
    x = 52
    ids_locked = []
    for index in range(10):
        x += 4
        frame = scene(((x, 62, x + 70, 202), RED))
        state = lock.update([Detection(100 + index, (x, 62, x + 70, 202), 0.85)], frame)
        ids_locked.append(state.track_id)
        if not state.visible:
            break
    check(
        "keeper follows through an ID churn every report",
        state.visible and len(set(ids_locked)) >= 5,
        f"ids: {ids_locked}",
    )

    # --- 4. the neighbour must NOT steal the lock ----------------------------------------
    # The suspect (red) walked right; the neighbour (blue) stands exactly where the suspect was,
    # so the neighbour overlaps the old box MORE than the suspect does. Plain IoU re-binding
    # picked the neighbour here; the appearance signature must reject it.
    lock = SuspectLock()
    frame = scene(((50, 60, 120, 200), RED))
    lock.pick(1, [Detection(1, (50, 60, 120, 200), 0.9)], frame)
    stale = (50, 60, 120, 200)
    suspect_now = (95, 60, 165, 200)
    neighbour = (55, 60, 125, 200)
    check(
        "neighbour overlaps the old box more than the suspect does (the old failure mode)",
        box_iou(stale, neighbour) > box_iou(stale, suspect_now),
        f"neighbour={box_iou(stale, neighbour):.2f} suspect={box_iou(stale, suspect_now):.2f}",
    )
    frame = scene((suspect_now, RED), (neighbour, BLUE))
    for _ in range(4):
        state = lock.update(
            [Detection(4, neighbour, 0.8), Detection(3, suspect_now, 0.8)], frame
        )
    check(
        "lock moved to the suspect and never onto the neighbour",
        state.track_id == 3,
        f"locked={state.track_id}",
    )
    check("box is on the suspect", state.bbox == suspect_now, str(state.bbox))

    # --- 5. two equally plausible candidates: refuse to guess ----------------------------
    # Both people look the same colour here and sit symmetrically around the last known box, so
    # the keeper should wait (keeping the last known box) rather than pick one.
    lock = SuspectLock()
    frame = scene(((50, 60, 120, 200), RED))
    lock.pick(1, [Detection(1, (50, 60, 120, 200), 0.9)], frame)
    twin_left = (44, 62, 114, 202)
    twin_right = (56, 62, 126, 202)
    frame = scene((twin_left, RED), (twin_right, RED))
    moved = 0
    ambiguous_reports = 0
    for _ in range(3):
        state = lock.update(
            [Detection(5, twin_left, 0.8), Detection(6, twin_right, 0.8)], frame
        )
        if state.track_id != 1:
            moved += 1
        if state.ambiguous:
            ambiguous_reports += 1
    check("ambiguous twins do not move the lock", moved == 0, f"moved {moved} times")
    check("ambiguity is reported exactly once", ambiguous_reports == 1, str(ambiguous_reports))
    check("lock is kept while ambiguous", lock.marked and lock.track_id == 1)

    # --- 6. a swap where the ID stays but points at the other person ---------------------
    # Geometry alone cannot see this; the appearance gate must.
    lock = SuspectLock()
    frame = scene(((50, 60, 120, 200), RED))
    lock.pick(1, [Detection(1, (50, 60, 120, 200), 0.9)], frame)
    swapped = (95, 60, 165, 200)
    real = (54, 62, 124, 202)
    frame = scene((swapped, BLUE), (real, RED))
    for _ in range(3):
        state = lock.update(
            [Detection(2, swapped, 0.8), Detection(8, real, 0.85)], frame
        )
    check(
        "ID kept but pointing at the wrong person is corrected",
        state.track_id == 8,
        f"locked={state.track_id}",
    )

    # --- 7. a bystander standing exactly where the suspect stood must not take the lock -----
    # The neighbour (blue) is reported right on top of the last known box, so a pure overlap
    # rule would lock onto them as soon as the suspect's ID disappears. The appearance gate has
    # to reject them and the lock has to stay with the (temporarily invisible) suspect.
    lock = SuspectLock()
    frame = scene(((50, 60, 120, 200), RED))
    lock.pick(1, [Detection(1, (50, 60, 120, 200), 0.9)], frame)
    bystander = scene(((54, 62, 124, 202), BLUE))
    for _ in range(12):
        state = lock.update([Detection(9, (54, 62, 124, 202), 0.8)], bystander)
    check(
        "a differently-looking bystander cannot take the lock",
        state.track_id == 1,
        f"locked={state.track_id}",
    )
    check(
        "the red box is never drawn on the bystander",
        state.bbox != (54, 62, 124, 202),
        str(state.bbox),
    )

    # --- 8. dropout: keep the box briefly, then drop it (lock survives) ------------------
    lock = SuspectLock()
    frame = scene(((50, 60, 120, 200), RED))
    lock.pick(1, [Detection(1, (50, 60, 120, 200), 0.9)], frame)
    far = scene()
    state = lock.update([Detection(9, (280, 200, 310, 235), 0.4)], far)
    check("box kept during a short dropout", state.bbox == (50, 60, 120, 200), str(state.bbox))
    check("dropout is reported once", state.lost)
    for _ in range(12):
        state = lock.update([Detection(9, (280, 200, 310, 235), 0.4)], far)
    check("box released after the grace period", state.bbox is None, str(state.bbox))
    check("lock itself is never cleared", lock.marked and lock.track_id == 1)
    check("lock can be cleared explicitly", (lock.clear() or not lock.marked))

    print()
    print("FAILURES:", FAILURES if FAILURES else "none")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
