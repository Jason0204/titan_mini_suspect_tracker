"""Host-side keeper for the locked suspect's identity.

The device runs its own IoU tracker and reports one stable track ID per pedestrian. That
tracker is not perfect: after an occlusion, a fast move, or when two pedestrians cross each
other it either hands out a brand-new ID or keeps the old ID but points it at the *other*
person. The console used to re-bind the lock purely by box overlap (IoU) with the last known
box, which regularly sent the red box onto the pedestrian standing next to the suspect.

This module replaces that with three safer mechanisms:

1. **Motion-predicted geometry.** The last box is advanced by an exponentially smoothed centre
   displacement, so the reference box follows a walking suspect instead of lagging behind, and
   candidates are ranked by ``0.7 * IoU + 0.3 * proximity`` against that prediction.
2. **Appearance gate.** While the suspect is visible a small HSV histogram of the torso band is
   kept and slowly adapted. A candidate whose histogram clearly disagrees with the signature
   ("that is not the person I locked") is heavily discounted, which is what stops the hop onto
   a neighbour that happens to overlap the prediction more.
3. **Refuse to guess.** When two candidates compete, the winner must persist for
   ``confirm_updates`` consecutive reports, and when the two best candidates score almost the
   same the keeper simply waits instead of picking one. The lock itself is never cleared here.

Changed 2026-10-14 (VGA field failure, docs §14): the on-device tracker churns track IDs much
faster at VGA, so the picked ID routinely dies within one second. Two keeper changes:

* **Sole-candidate fast path.** When exactly one candidate clears the geometric floor it takes
  over on the *first* report -- requiring the same new ID to win twice in a row never completes
  when IDs change faster than the report rate.
* **Appearance-only reacquisition.** After the grace period expires the motion prediction is
  stale, so a confident, unique appearance match (still geometrically plausible) re-takes the
  lock immediately.

Two consequences worth knowing:

* A candidate the appearance gate rejects is discounted to at most ``APPEARANCE_DISCOUNT`` of its
  geometry, which is deliberately below ``relock_floor`` -- so "somebody else is standing where
  the suspect stood" can never take the lock, the red box is simply released after the grace
  period until the real suspect shows up again.
* When the ID is still being reported but points at somebody who does not look like the suspect
  (the device moved the ID onto the other pedestrian during a crossing), the keeper ignores the
  motion prediction -- it has been dragged onto the wrong person by that very ID -- and follows
  the appearance instead, again behind the confirmation window.

Everything is pure Python/OpenCV (no Qt) so the behaviour can be tested without a display.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np

Box = tuple[int, int, int, int]

# Appearance correlation below this means "does not look like the locked suspect".
APPEARANCE_FLOOR = 0.45
# A challenger that wants to take an ID away from a still-reported detection must look this
# similar to the suspect, and this much better than the detection it is replacing.
APPEARANCE_ACCEPT = 0.60
APPEARANCE_MARGIN = 0.20
# How much a candidate that fails the appearance gate is discounted when ranking. It must stay
# below SuspectLock.relock_floor so that such a candidate can never reach the floor on its own --
# that invariant is what keeps the lock off the pedestrian who happens to stand where the
# suspect stood.
APPEARANCE_DISCOUNT = 0.25
# Changed 2026-10-14: Last-resort reacquisition. Once the grace period has expired the motion
# prediction is useless (the suspect has been gone too long), so a candidate may re-take the
# lock on appearance alone -- but only a confident match, and it must still be geometrically
# plausible (this fraction of the normal re-lock floor).
REACQUIRE_GEOMETRY = 0.12
# Adaptation speed of the appearance signature (exponential moving average).
SIGNATURE_ALPHA = 0.25
# How fast the motion estimate decays while the suspect is out of sight.
VELOCITY_DECAY = 0.6
# A box that jumped by more than this fraction of its own size in one report is an identity
# re-bind, not real motion, so the motion estimate is reset instead of blended.
VELOCITY_RESET_FRACTION = 0.5

_HISTOGRAM_BINS = (16, 8)
_HISTOGRAM_RANGES = (0, 180, 0, 256)


def _center(box: Box) -> tuple[float, float]:
    x1, y1, x2, y2 = box
    return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)


def _mean_side(box: Box) -> float:
    x1, y1, x2, y2 = box
    return max(1.0, 0.5 * ((x2 - x1) + (y2 - y1)))


def box_iou(first: Box, second: Box) -> float:
    x1 = max(first[0], second[0])
    y1 = max(first[1], second[1])
    x2 = min(first[2], second[2])
    y2 = min(first[3], second[3])
    intersection = max(0, x2 - x1) * max(0, y2 - y1)
    first_area = max(0, first[2] - first[0]) * max(0, first[3] - first[1])
    second_area = max(0, second[2] - second[0]) * max(0, second[3] - second[1])
    union = first_area + second_area - intersection
    return intersection / union if union else 0.0


def proximity(reference: Box, candidate: Box) -> float:
    """1.0 when the centres coincide, 0.0 when they are a box-size apart (or further)."""
    reference_x, reference_y = _center(reference)
    candidate_x, candidate_y = _center(candidate)
    distance = math.hypot(candidate_x - reference_x, candidate_y - reference_y)
    return max(0.0, 1.0 - distance / _mean_side(reference))


def region_histogram(frame: np.ndarray | None, box: Box) -> np.ndarray | None:
    """HSV (hue, saturation) histogram of the torso band inside ``box``.

    The band is inset from the box edges so the surrounding background and the legs -- the least
    discriminative parts of a pedestrian -- do not dominate the signature.
    """
    if frame is None or getattr(frame, "ndim", 0) != 3 or frame.shape[2] < 3:
        return None
    frame_height, frame_width = frame.shape[:2]
    x1 = max(0, min(int(box[0]), frame_width - 1))
    y1 = max(0, min(int(box[1]), frame_height - 1))
    x2 = max(x1 + 1, min(int(box[2]), frame_width))
    y2 = max(y1 + 1, min(int(box[3]), frame_height))
    box_width = x2 - x1
    box_height = y2 - y1
    if box_width < 6 or box_height < 8:
        return None
    torso_x1 = x1 + round(0.2 * box_width)
    torso_x2 = x2 - round(0.2 * box_width)
    torso_y1 = y1 + round(0.15 * box_height)
    torso_y2 = y1 + round(0.70 * box_height)
    patch = frame[torso_y1:torso_y2, torso_x1:torso_x2]
    if patch.size == 0:
        return None
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    histogram = cv2.calcHist([hsv], [0, 1], None, list(_HISTOGRAM_BINS), list(_HISTOGRAM_RANGES))
    cv2.normalize(histogram, histogram, 1.0, 0.0, cv2.NORM_L1)
    return histogram


def histogram_similarity(first: np.ndarray, second: np.ndarray) -> float:
    correlation = float(cv2.compareHist(first, second, cv2.HISTCMP_CORREL))
    if math.isnan(correlation):
        return 0.0
    return max(0.0, min(1.0, correlation))


@dataclass(frozen=True)
class LockState:
    """Result of one detection update, ready for logging and for the on-screen overlay."""

    track_id: int | None = None
    bbox: Box | None = None
    visible: bool = False
    relocked_from: int | None = None
    relocked_to: int | None = None
    lost: bool = False
    reacquired: bool = False
    ambiguous: bool = False

    @property
    def relocked(self) -> bool:
        return self.relocked_to is not None


class SuspectLock:
    """Keeps one suspect's identity stable across the device's track-ID churn."""

    def __init__(
        self,
        *,
        relock_floor: float = 0.30,
        confirm_updates: int = 2,
        ambiguity_ratio: float = 1.15,
        # Changed 2026-10-14: 12 reports (~2 s at the VGA report rate) instead of 8.
        grace_updates: int = 12,
        appearance_floor: float = APPEARANCE_FLOOR,
        appearance_accept: float = APPEARANCE_ACCEPT,
        appearance_margin: float = APPEARANCE_MARGIN,
        appearance_discount: float = APPEARANCE_DISCOUNT,
    ) -> None:
        self.relock_floor = relock_floor
        self.confirm_updates = max(1, int(confirm_updates))
        self.ambiguity_ratio = ambiguity_ratio
        self.grace_updates = max(0, int(grace_updates))
        self.appearance_floor = appearance_floor
        self.appearance_accept = appearance_accept
        self.appearance_margin = appearance_margin
        self.appearance_discount = appearance_discount
        self.reset()

    # -- public state ---------------------------------------------------------
    def reset(self) -> None:
        self._marked = False
        self._track_id: int | None = None
        self._last_bbox: Box | None = None
        self._velocity = (0.0, 0.0)
        self._signature: np.ndarray | None = None
        self._visible = False
        self._grace = 0
        self._pending_id: int | None = None
        self._pending_hits = 0
        self._ambiguous = False

    @property
    def marked(self) -> bool:
        return self._marked

    @property
    def track_id(self) -> int | None:
        return self._track_id

    @property
    def last_bbox(self) -> Box | None:
        return self._last_bbox

    @property
    def visible(self) -> bool:
        return self._visible

    # -- commands -------------------------------------------------------------
    def pick(self, track_id: int, detections=None, frame=None) -> None:
        """Lock ``track_id``; the box is looked up in the latest detection report."""
        self.reset()
        self._marked = True
        self._track_id = int(track_id)
        items = list(detections or [])
        bbox = next((item.bbox for item in items if item.track_id == self._track_id), None)
        if bbox is None and items:
            # Degenerate case: the click matched a track whose box is already gone. Fall back to
            # the largest box so the keeper still has a reference to work with.
            bbox = max(items, key=lambda item: _mean_side(item.bbox)).bbox
        self._last_bbox = tuple(int(value) for value in bbox) if bbox is not None else None
        self._grace = self.grace_updates
        self._visible = True
        if frame is not None and self._last_bbox is not None:
            self._signature = region_histogram(frame, self._last_bbox)

    def clear(self) -> None:
        self.reset()

    # -- per-report update ----------------------------------------------------
    def update(self, detections, frame=None) -> LockState:
        if not self._marked:
            return LockState()
        items = list(detections or [])
        predicted = self._predict()
        geometries: list[float] = []
        appearances: list[float | None] = []
        ranks: list[float] = []
        for item in items:
            geometry = self._geometry(predicted, item.bbox)
            appearance = self._appearance(frame, item.bbox)
            geometries.append(geometry)
            appearances.append(appearance)
            ranks.append(self._rank(geometry, appearance))

        by_id = {item.track_id: index for index, item in enumerate(items)}
        current_index = by_id.get(self._track_id)
        relocked_from: int | None = None
        relocked_to: int | None = None
        ambiguous = False
        winner: int | None = None

        if current_index is not None:
            self._ambiguous = False
            challenger = self._best_other(items, ranks, current_index)
            if self._swap_suspected(appearances, current_index, challenger):
                # The device kept the ID but handed it to the other person; only the appearance
                # signature can spot this, so it needs the confirmation window too.
                if self._confirmed(items[challenger].track_id):
                    winner = challenger
                else:
                    winner = current_index
            else:
                self._forget_pending()
                winner = current_index
        else:
            best, second = self._top_two(ranks)
            if best is None or ranks[best] < self.relock_floor:
                # Changed 2026-10-14: Nobody clears the geometric floor. Two escape hatches,
                # in order: a confident appearance-only reacquisition (the suspect has been gone
                # long enough that the motion prediction no longer helps), otherwise keep the
                # lock, drop the marker after the grace period.
                reacquire = self._appearance_reacquire(items, geometries, appearances)
                if reacquire is not None:
                    # Accepted on the first report: the gates (confident appearance, unique
                    # candidate, geometric plausibility) are strict enough, and a confirmation
                    # window would again be outrun by device-side ID churn.
                    self._ambiguous = False
                    self._forget_pending()
                    winner = reacquire
                else:
                    self._forget_pending()
                    self._ambiguous = False
            elif second is not None and ranks[second] * self.ambiguity_ratio > ranks[best]:
                # Two candidates are equally plausible (people standing side by side). Waiting
                # for a clearer report is what keeps the red box from hopping onto a neighbour.
                self._forget_pending()
                ambiguous = not self._ambiguous
                self._ambiguous = True
            else:
                self._ambiguous = False
                # Changed 2026-10-14: When exactly ONE candidate is above the floor, accept it on
                # the first report. Requiring the *same new ID* to win two reports in a row never
                # completes when the device churns IDs faster than the report rate (measured on
                # hardware at VGA: the picked ID was gone within one second). The confirmation
                # window stays in force only while two candidates compete.
                sole = second is None or ranks[second] < self.relock_floor
                if sole:
                    self._forget_pending()
                    winner = best
                elif self._confirmed(items[best].track_id):
                    winner = best

        if winner is not None:
            detection = items[winner]
            if detection.track_id != self._track_id:
                relocked_from = self._track_id
                relocked_to = detection.track_id
                self._track_id = detection.track_id
                self._forget_pending()
            self._remember(detection.bbox, frame)
            self._grace = self.grace_updates
            visible = True
        else:
            visible = False
            if self._grace > 0:
                self._grace -= 1
            # Bleed off the motion estimate while the suspect is out of sight, otherwise the
            # predicted box would keep flying further away with every missed report.
            self._velocity = (
                self._velocity[0] * VELOCITY_DECAY,
                self._velocity[1] * VELOCITY_DECAY,
            )

        lost = self._visible and not visible
        reacquired = visible and not self._visible
        self._visible = visible
        if visible or self._grace > 0:
            bbox = self._last_bbox
        else:
            bbox = None
        return LockState(
            track_id=self._track_id,
            bbox=bbox,
            visible=visible,
            relocked_from=relocked_from,
            relocked_to=relocked_to,
            lost=lost,
            reacquired=reacquired,
            ambiguous=ambiguous,
        )

    # -- internals ------------------------------------------------------------
    def _predict(self) -> Box | None:
        if self._last_bbox is None:
            return None
        x1, y1, x2, y2 = self._last_bbox
        dx, dy = self._velocity
        return (
            int(round(x1 + dx)),
            int(round(y1 + dy)),
            int(round(x2 + dx)),
            int(round(y2 + dy)),
        )

    @staticmethod
    def _geometry(reference: Box | None, candidate: Box) -> float:
        if reference is None:
            return 0.0
        return 0.7 * box_iou(reference, candidate) + 0.3 * proximity(reference, candidate)

    def _appearance(self, frame: np.ndarray | None, box: Box) -> float | None:
        if self._signature is None or frame is None:
            return None
        histogram = region_histogram(frame, box)
        if histogram is None:
            return None
        return histogram_similarity(self._signature, histogram)

    def _rank(self, geometry: float, appearance: float | None) -> float:
        if appearance is None or appearance >= self.appearance_floor:
            return geometry
        return geometry * self.appearance_discount

    @staticmethod
    def _best_other(items, ranks: list[float], current_index: int) -> int | None:
        best: int | None = None
        current_id = items[current_index].track_id
        for index, rank in enumerate(ranks):
            if items[index].track_id == current_id:
                continue
            if best is None or rank > ranks[best]:
                best = index
        return best

    @staticmethod
    def _top_two(ranks: list[float]) -> tuple[int | None, int | None]:
        order = sorted(range(len(ranks)), key=lambda index: ranks[index], reverse=True)
        first = order[0] if order else None
        second = order[1] if len(order) > 1 else None
        return first, second

    def _appearance_reacquire(
        self, items, geometries: list[float], appearances: list[float | None]
    ) -> int | None:
        # Changed 2026-10-14: Last-resort reacquisition after the grace period. The suspect may
        # have walked away from the (now stale) motion prediction, so no candidate clears the
        # geometric re-lock floor any more. A candidate may take the lock on the appearance
        # signature alone, but only when ALL of these hold:
        #   * it looks confidently like the suspect (>= appearance_accept),
        #   * it is still geometrically plausible (>= REACQUIRE_GEOMETRY of the score), and
        #   * it is the ONLY such candidate (two people wearing the same colours -> wait).
        if self._signature is None:
            return None
        candidates = [
            index
            for index, appearance in enumerate(appearances)
            if appearance is not None
            and appearance >= self.appearance_accept
            and geometries[index] >= REACQUIRE_GEOMETRY
        ]
        return candidates[0] if len(candidates) == 1 else None

    def _swap_suspected(self, appearances: list[float | None], current_index: int, challenger) -> bool:
        if challenger is None or self._signature is None:
            return False
        current_appearance = appearances[current_index]
        challenger_appearance = appearances[challenger]
        if current_appearance is None or challenger_appearance is None:
            return False
        if current_appearance >= self.appearance_floor:
            return False
        # The ID still points at somebody who does not look like the locked suspect: the device
        # moved that ID onto the other pedestrian when they crossed. The motion prediction cannot
        # help here (it has been dragged onto the wrong person by the very ID we no longer
        # trust), so the challenger is judged on appearance alone -- and only when it is a
        # confident match.
        return challenger_appearance >= self.appearance_accept and (
            challenger_appearance >= current_appearance + self.appearance_margin
        )

    def _confirmed(self, track_id: int) -> bool:
        if track_id == self._pending_id:
            self._pending_hits += 1
        else:
            self._pending_id = track_id
            self._pending_hits = 1
        return self._pending_hits >= self.confirm_updates

    def _forget_pending(self) -> None:
        self._pending_id = None
        self._pending_hits = 0

    def _remember(self, bbox, frame: np.ndarray | None) -> None:
        box = tuple(int(value) for value in bbox)
        if self._last_bbox is not None:
            previous_x, previous_y = _center(self._last_bbox)
            current_x, current_y = _center(box)
            shift = math.hypot(current_x - previous_x, current_y - previous_y)
            if shift > _mean_side(self._last_bbox) * VELOCITY_RESET_FRACTION:
                # A jump this large is an identity re-bind (or a re-detected person), not walking
                # motion. Blending it would leave the prediction hanging in mid air.
                self._velocity = (0.0, 0.0)
            else:
                dx, dy = self._velocity
                self._velocity = (
                    0.5 * dx + 0.5 * (current_x - previous_x),
                    0.5 * dy + 0.5 * (current_y - previous_y),
                )
        self._last_bbox = box
        if frame is None:
            return
        histogram = region_histogram(frame, box)
        if histogram is None:
            return
        if self._signature is None:
            self._signature = histogram
        else:
            cv2.addWeighted(
                self._signature,
                1.0 - SIGNATURE_ALPHA,
                histogram,
                SIGNATURE_ALPHA,
                0.0,
                dst=self._signature,
            )
