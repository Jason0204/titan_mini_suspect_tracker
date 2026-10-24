from __future__ import annotations

import math

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QPen, QPixmap, QResizeEvent
from PySide6.QtWidgets import QLabel

from suspect_tracker.video import Detection


class VideoDisplay(QLabel):
    # Changed 2026-10-13: Click-to-pick a suspect directly on the live stream (no pause). The
    # signal carries the track ID of the detection that the click refers to.
    # Changed 2026-10-14: The paused-frame drag-selection workflow was removed: stopping the
    # stream enabled it, it could not lock anyone (the hardware reports no new detections while
    # the stream is stopped) and it left a box on the frozen frame that nothing could dismiss.
    # Clicking a pedestrian on the live stream is now the only way to pick a suspect.
    suspect_picked = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self._source_size = (0, 0)
        self._tracking_box: tuple[int, int, int, int] | None = None
        # Changed 2026-10-13: Overlay every hardware detection, not just the marked suspect.
        self._detections: list[Detection] = []
        self._source_pixmap: QPixmap | None = None

    def set_video_image(self, image: QImage) -> None:
        # Changed 2026-09-20: Preserve the original frame for accurate resizing.
        self._source_size = (image.width(), image.height())
        self._source_pixmap = QPixmap.fromImage(image)
        self._update_scaled_pixmap()

    def set_tracking_box(self, bbox: tuple[int, int, int, int] | None) -> None:
        # Changed 2026-09-20: Draw the suspect from each frame's latest hardware bbox.
        self._tracking_box = bbox
        self.update()

    def set_detections(self, detections: list[Detection]) -> None:
        # Changed 2026-10-13: Hardware reports the full detection list per update, so passing an
        # empty list is what clears the overlay once a person leaves the frame.
        self._detections = list(detections or [])
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        # Changed 2026-10-13: A left click inside a detection box locks that track as the suspect
        # without pausing the video.
        if event.button() == Qt.MouseButton.LeftButton:
            track_id = self._detection_at(event.position().toPoint())
            if track_id is not None:
                self.suspect_picked.emit(track_id)
                return
        super().mousePressEvent(event)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        # Changed 2026-10-13: Draw every reported detection first so the marked suspect's red
        # box stays on top of the stack.
        for detection in self._detections:
            rectangle = self._source_rect_to_widget(tuple(detection.bbox))
            if rectangle is None:
                continue
            painter.setPen(QPen(QColor("#35d0a0"), 2, Qt.PenStyle.SolidLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rectangle)
            painter.setPen(QPen(QColor("#35d0a0")))
            painter.drawText(
                rectangle.left() + 2,
                rectangle.top() + 14,
                f"{detection.label} {detection.confidence:.2f}",
            )
        tracking_rectangle = self._source_rect_to_widget(self._tracking_box)
        if tracking_rectangle is not None:
            painter.setPen(QPen(QColor("#ff4d4f"), 3, Qt.PenStyle.SolidLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(tracking_rectangle)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._update_scaled_pixmap()

    def _detection_at(self, widget_point: QPoint) -> int | None:
        # Changed 2026-10-14: The click no longer has to land exactly inside a box. The nearest
        # box within a radius that scales with the box size is picked, which is what makes
        # locking a small or slightly moving pedestrian reliable. The topmost drawn box wins
        # ties because the list is walked back to front.
        display_rect = self._display_rect()
        if display_rect is None:
            return None
        source_width, source_height = self._source_size
        if source_width <= 0 or source_height <= 0:
            return None
        source_x = (widget_point.x() - display_rect.left()) * source_width / display_rect.width()
        source_y = (widget_point.y() - display_rect.top()) * source_height / display_rect.height()
        best_id: int | None = None
        best_distance: float | None = None
        for detection in reversed(self._detections):
            x1, y1, x2, y2 = (float(value) for value in detection.bbox)
            dx = max(x1 - source_x, 0.0, source_x - x2)
            dy = max(y1 - source_y, 0.0, source_y - y2)
            distance = math.hypot(dx, dy)
            radius = max(6.0, 0.35 * min(x2 - x1, y2 - y1))
            if distance > radius:
                continue
            if best_distance is None or distance < best_distance:
                best_distance = distance
                best_id = detection.track_id
        return best_id

    def _update_scaled_pixmap(self) -> None:
        if self._source_pixmap is None:
            return
        self.setPixmap(
            self._source_pixmap.scaled(
                self.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _display_rect(self) -> QRect | None:
        source_width, source_height = self._source_size
        if source_width <= 0 or source_height <= 0:
            return None
        scale = min(self.width() / source_width, self.height() / source_height)
        display_width = round(source_width * scale)
        display_height = round(source_height * scale)
        return QRect(
            (self.width() - display_width) // 2,
            (self.height() - display_height) // 2,
            display_width,
            display_height,
        )

    def _source_rect_to_widget(
        self, source_rectangle: tuple[int, int, int, int] | None
    ) -> QRect | None:
        if source_rectangle is None:
            return None
        display_rect = self._display_rect()
        if display_rect is None:
            return None
        source_width, source_height = self._source_size
        x1, y1, x2, y2 = source_rectangle
        return QRect(
            display_rect.left() + round(x1 * display_rect.width() / source_width),
            display_rect.top() + round(y1 * display_rect.height() / source_height),
            max(1, round((x2 - x1) * display_rect.width() / source_width)),
            max(1, round((y2 - y1) * display_rect.height() / source_height)),
        )
