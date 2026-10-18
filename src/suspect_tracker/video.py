from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QImage


@dataclass(frozen=True)
class Detection:
    track_id: int
    bbox: tuple[int, int, int, int]
    confidence: float
    label: str = "person"


class MockVideoSource(QObject):
    frame_ready = Signal(object, object)

    def __init__(self, width: int = 960, height: int = 540, fps: int = 30) -> None:
        super().__init__()
        self.width = width
        self.height = height
        # Changed 2026-09-20: Expose FPS so UI commands and recordings match the active source.
        self.fps = fps
        self._timer = QTimer(self)
        self._timer.setInterval(max(1, int(1000 / fps)))
        self._timer.timeout.connect(self._next_frame)
        self._frame_index = 0

    def start(self) -> None:
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _next_frame(self) -> None:
        self._frame_index += 1
        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        frame[:, :] = (24, 29, 36)

        grid_color = (42, 52, 63)
        for x in range(0, self.width, 80):
            cv2.line(frame, (x, 0), (x, self.height), grid_color, 1)
        for y in range(0, self.height, 60):
            cv2.line(frame, (0, y), (self.width, y), grid_color, 1)

        center_x = 190 + (self._frame_index * 7) % (self.width - 380)
        center_y = 160 + int(70 * np.sin(self._frame_index / 23))
        bbox = (center_x, center_y, center_x + 110, center_y + 180)
        detection = Detection(track_id=1001, bbox=bbox, confidence=0.91)

        cv2.rectangle(frame, bbox[:2], bbox[2:], (35, 186, 122), 2)
        cv2.putText(
            frame,
            f"ID {detection.track_id}  {detection.confidence:.2f}",
            (bbox[0], max(24, bbox[1] - 10)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (184, 239, 212),
            2,
            cv2.LINE_AA,
        )
        cv2.putText(
            frame,
            "YOLO-fast prototype stream",
            (24, 40),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.85,
            (225, 232, 238),
            2,
            cv2.LINE_AA,
        )

        self.frame_ready.emit(frame, [detection])


class VideoRecorder:
    def __init__(self) -> None:
        self._writer: cv2.VideoWriter | None = None
        self.output_path: Path | None = None
        self.frame_count = 0
        self._size = (0, 0)

    @property
    def is_recording(self) -> bool:
        return self._writer is not None

    def start(self, output_path: Path, width: int, height: int, fps: int = 30) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(str(output_path), fourcc, fps, (width, height))
        if not writer.isOpened():
            raise RuntimeError(f"Unable to open video writer: {output_path}")
        self._writer = writer
        self.output_path = output_path
        self.frame_count = 0
        self._size = (int(width), int(height))

    def write(self, frame: np.ndarray) -> None:
        if self._writer is None:
            return
        # Changed 2026-10-14: OpenCV silently discards frames whose size does not match the
        # writer, which produced an empty (then deleted) MP4. Resize instead of losing the frame.
        if frame.shape[1] != self._size[0] or frame.shape[0] != self._size[1]:
            frame = cv2.resize(frame, self._size)
        self._writer.write(np.ascontiguousarray(frame))
        self.frame_count += 1

    def stop(self) -> Path | None:
        if self._writer is not None:
            self._writer.release()
            self._writer = None
        output_path = self.output_path
        if output_path is not None and self.frame_count == 0:
            output_path.unlink(missing_ok=True)
            self.output_path = None
            return None
        return output_path


def annotate_frame(
    frame: np.ndarray,
    suspect_box: tuple[int, int, int, int] | None,
    suspect_track_id: int | None = None,
    suspect_label: str = "suspect",
) -> np.ndarray:
    # Changed 2026-10-14: Only the locked suspect is burned into the saved recording. The first
    # version also drew every other detection in green, but the operator asked for a clean
    # recording that highlights just the suspect, so bystander boxes are no longer drawn.
    # NOTE: the caption stays ASCII on purpose -- cv2.putText cannot render CJK glyphs.
    if suspect_box is None:
        return frame
    annotated = frame.copy()
    x1, y1, x2, y2 = (int(value) for value in suspect_box)
    colour = (79, 77, 255)  # BGR of the on-screen #ff4d4f red
    cv2.rectangle(annotated, (x1, y1), (x2, y2), colour, 3)
    caption = f"{suspect_label} ID {suspect_track_id}" if suspect_track_id is not None else suspect_label
    cv2.putText(
        annotated,
        caption,
        (x1, max(16, y1 - 8)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        colour,
        2,
        cv2.LINE_AA,
    )
    return annotated


def frame_to_qimage(frame: np.ndarray) -> QImage:
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    height, width, channels = rgb.shape
    bytes_per_line = channels * width
    return QImage(rgb.data, width, height, bytes_per_line, QImage.Format.Format_RGB888).copy()
