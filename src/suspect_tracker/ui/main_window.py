from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl, Qt
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QLabel,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from suspect_tracker.hardware_video import (
    FRAME_SIZE_OPTIONS,
    ControlChannelUnavailable,
    OpenMVSerialVideoSource,
    log_openmv_event,
)
from suspect_tracker.paths import (
    log_ui_event,
    output_root,
    recordings_directory,
    ui_event_log_path,
)
from suspect_tracker.protocol import ProtocolFrame, command
from suspect_tracker.suspect_lock import SuspectLock
from suspect_tracker.ui.i18n import LANGUAGES, diagnostic_text, text
from suspect_tracker.ui.themes import THEMES, build_stylesheet
from suspect_tracker.ui.video_display import VideoDisplay
from suspect_tracker.video import (
    Detection,
    MockVideoSource,
    VideoRecorder,
    annotate_frame,
    frame_to_qimage,
)

# Changed 2026-10-14: In automatic mode the MP4 is only finalised when recording stops (OpenCV
# writes the MP4 index at release time), so a recording left running after the suspect walked away
# produced a file that could not be played. Recording is now saved automatically once the suspect
# has been missing for this long. The suspect lock itself is still never cleared.
AUTOMATIC_RECORDING_STOP_SECONDS = 15.0


class MainWindow(QMainWindow):
    def __init__(self, video_source=None) -> None:
        super().__init__()
        self.setWindowTitle("Titan Mini Board Suspect Tracker")
        # Changed 2026-09-20: Allow hardware tests to inject HardwareVideoSource.
        self.video_source = video_source or MockVideoSource()
        self.recorder = VideoRecorder()
        self.current_track_id: int | None = None
        self.suspect_track_id: int | None = None
        self.trajectory: list[tuple[int, int]] = []
        self.language = "zh"
        self.theme_key = "graphite"
        self.camera_connected = False
        self.stream_running = False
        # Changed 2026-10-14: Suspects are always auto-labelled now, so the operator no longer
        # types a name; the label is kept only for the tables and the log.
        self.suspect_label = "suspect"
        self.suspect_marked = False
        # Changed 2026-10-13: Remember whether the locked suspect was visible in the last
        # detection update so appearing/disappearing can be reported exactly once.
        # Changed 2026-10-14: The identity work moved into SuspectLock (motion prediction,
        # appearance signature, ambiguity guard, confirmation window). Two pedestrians standing
        # side by side used to make the red box hop onto the neighbour because the lock was
        # re-bound by plain IoU.
        self._suspect_lock = SuspectLock()
        # Changed 2026-10-14: The latest frame is kept so the appearance signature can be sampled
        # at pick time, and the last drawn suspect box so the recording matches the live view.
        self._latest_frame = None
        self._suspect_display_bbox: tuple[int, int, int, int] | None = None
        # Changed 2026-10-14: Frame rates measured by the hardware source.
        self.host_fps = 0.0
        self.device_fps = 0.0
        # Changed 2026-09-20: Retain paused-frame detections for the information panels.
        self.last_detections: list[Detection] = []
        # Changed 2026-09-20: Support operator-selected manual or suspect-triggered recording.
        self.recording_mode = "manual"
        # Changed 2026-10-14: When the locked suspect was last seen, so automatic-mode recording
        # can be finalised after the person has been gone for a while.
        self._suspect_last_seen = time.monotonic()
        self._automatic_recording_saved = False

        self._build_ui()
        self._wire_signals()
        # Changed 2026-10-14: Watch automatic recordings so an MP4 is still saved when the suspect
        # walks away and the operator forgets to clear the mark.
        self._recording_watchdog = QTimer(self)
        self._recording_watchdog.setInterval(1000)
        self._recording_watchdog.timeout.connect(self._check_automatic_recording)
        self._recording_watchdog.start()
        # Changed 2026-10-14: Tell the operator where the persisted event log lives.
        self._log(text(self.language, "output_directory", path=output_root()))
        self._log(text(self.language, "log_file_hint", path=ui_event_log_path()))

    def _build_ui(self) -> None:
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(14, 14, 14, 14)
        root_layout.setSpacing(10)

        self.title_label = QLabel()
        self.title_label.setObjectName("Title")
        root_layout.addWidget(self.title_label)

        settings_box = QGroupBox()
        settings_layout = QGridLayout(settings_box)
        self.theme_label = QLabel()
        self.theme_select = QComboBox()
        for theme in THEMES.values():
            self.theme_select.addItem(theme.display_name, theme.key)
        self.language_label = QLabel()
        self.language_select = QComboBox()
        for key, display_name in LANGUAGES.items():
            self.language_select.addItem(display_name, key)
        settings_layout.addWidget(self.theme_label, 0, 0)
        settings_layout.addWidget(self.theme_select, 0, 1)
        settings_layout.addWidget(self.language_label, 0, 2)
        settings_layout.addWidget(self.language_select, 0, 3)
        root_layout.addWidget(settings_box)
        self.settings_box = settings_box

        splitter = QSplitter(Qt.Orientation.Horizontal)
        root_layout.addWidget(splitter, 1)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 8, 0)
        left_layout.setSpacing(10)

        self.video_label = VideoDisplay()
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Changed 2026-10-14: The right-hand panels need more room, so the video keeps a
        # smaller minimum and the splitter starts further left.
        self.video_label.setMinimumSize(720, 420)
        self.video_label.setObjectName("VideoSurface")
        left_layout.addWidget(self.video_label, 1)
        # Changed 2026-09-20: Guide operators through stream, pause, ROI, and resume states.
        self.selection_hint_label = QLabel()
        self.selection_hint_label.setObjectName("SelectionHint")
        left_layout.addWidget(self.selection_hint_label)

        self.control_box = QGroupBox()
        control_layout = QGridLayout(self.control_box)
        self.openmv_port = QComboBox()
        self.openmv_port.setEditable(True)
        self.openmv_port.setToolTip("OpenMV USB serial port, for example COM4")
        self._refresh_openmv_ports()
        # Changed 2026-10-14: The AI-profile picker and the manual label box are gone. The
        # profile slot now selects the capture resolution, which is what the operator actually
        # needs: it is applied when the camera is (re)connected.
        self.frame_size_select = QComboBox()
        for size, (width, height) in FRAME_SIZE_OPTIONS.items():
            self.frame_size_select.addItem(f"{size}  {width}x{height}", size)
        self.frame_size_select.setToolTip(
            "采集分辨率；连接相机时生效。VGA 画面更大更清晰，但帧率约减半。"
        )
        self.recording_mode_select = QComboBox()
        self.recording_mode_select.addItem("", "manual")
        self.recording_mode_select.addItem("", "automatic")
        self.camera_port_label = QLabel()
        self.frame_size_label = QLabel()
        self.recording_mode_label = QLabel()
        self.connect_button = QPushButton()
        self.stream_button = QPushButton()
        self.clear_button = QPushButton()
        self.record_button = QPushButton()
        # Changed 2026-10-14: Logs and recordings now live in the program directory, so give the
        # operator a shortcut to open it instead of hunting for the folder.
        self.output_button = QPushButton()
        for button in (
            self.connect_button,
            self.stream_button,
            self.clear_button,
            self.record_button,
            self.output_button,
        ):
            button.setMinimumHeight(30)
            button.setMinimumWidth(120)

        control_layout.addWidget(self.camera_port_label, 0, 0)
        control_layout.addWidget(self.openmv_port, 0, 1)
        control_layout.addWidget(self.frame_size_label, 0, 2)
        control_layout.addWidget(self.frame_size_select, 0, 3)
        control_layout.addWidget(self.connect_button, 1, 0)
        control_layout.addWidget(self.stream_button, 1, 1)
        control_layout.addWidget(self.clear_button, 1, 2)
        control_layout.addWidget(self.recording_mode_label, 2, 0)
        control_layout.addWidget(self.recording_mode_select, 2, 1)
        control_layout.addWidget(self.record_button, 2, 2, 1, 2)
        control_layout.addWidget(self.output_button, 3, 0, 1, 4)
        left_layout.addWidget(self.control_box)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(8, 0, 0, 0)
        right_layout.setSpacing(10)

        self.status_box = QGroupBox()
        status_layout = QVBoxLayout(self.status_box)
        self.status_label = QLabel()
        self.active_target_label = QLabel()
        self.recording_label = QLabel()
        # Changed 2026-10-14: Show the measured frame rates (host transport and device capture).
        self.fps_label = QLabel()
        status_layout.addWidget(self.status_label)
        status_layout.addWidget(self.active_target_label)
        status_layout.addWidget(self.fps_label)
        status_layout.addWidget(self.recording_label)
        right_layout.addWidget(self.status_box)

        # Changed 2026-10-14: The three information panels live in a draggable splitter and get
        # most of the vertical space, so the track table and the trajectory list are readable
        # without scrolling instead of being squeezed to equal thirds.
        self.info_splitter = QSplitter(Qt.Orientation.Vertical)
        self.track_table = QTableWidget(0, 4)
        self.track_table.verticalHeader().setVisible(False)
        self.track_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.track_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.track_table.setMinimumHeight(180)
        # Changed 2026-10-14: Give the bbox column the leftover width so coordinates are never
        # truncated with "..." -- the operator asked to see the whole row at a glance.
        track_header = self.track_table.horizontalHeader()
        track_header.setSectionResizeMode(QHeaderView.ResizeToContents)
        track_header.setSectionResizeMode(3, QHeaderView.Stretch)
        self.live_tracks_box = self._wrap(self.track_table)
        self.info_splitter.addWidget(self.live_tracks_box)

        self.path_list = QListWidget()
        self.path_list.setMinimumHeight(160)
        self.trajectory_box = self._wrap(self.path_list)
        self.info_splitter.addWidget(self.trajectory_box)

        self.event_log = QPlainTextEdit()
        self.event_log.setReadOnly(True)
        self.event_log.setMinimumHeight(150)
        self.event_log_box = self._wrap(self.event_log)
        self.info_splitter.addWidget(self.event_log_box)
        self.info_splitter.setSizes([220, 200, 200])
        right_layout.addWidget(self.info_splitter, 1)

        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setSizes([760, 640])
        self.setCentralWidget(root)
        self._apply_theme()
        self._refresh_language()

    def _wrap(self, widget: QWidget) -> QGroupBox:
        box = QGroupBox()
        layout = QVBoxLayout(box)
        layout.addWidget(widget)
        return box

    def _wire_signals(self) -> None:
        self.connect_button.clicked.connect(self._toggle_camera)
        self.stream_button.clicked.connect(self._toggle_stream)
        self.clear_button.clicked.connect(self._clear_suspect)
        self.record_button.clicked.connect(self._toggle_recording)
        # Changed 2026-10-14: Open the folder that holds the logs and the recordings.
        self.output_button.clicked.connect(self._open_output_directory)
        self.recording_mode_select.currentIndexChanged.connect(self._change_recording_mode)
        self.theme_select.currentIndexChanged.connect(self._change_theme)
        self.language_select.currentIndexChanged.connect(self._change_language)
        self.video_source.frame_ready.connect(self._handle_video_frame)
        # Changed 2026-10-13: Lock a suspect by clicking its box on the live stream.
        # Changed 2026-10-14: This is now the only selection workflow -- the paused-frame ROI
        # drag-selection was removed from VideoDisplay.
        self.video_label.suspect_picked.connect(self._handle_suspect_picked)
        # Changed 2026-10-14: Show the measured frame rates as soon as the source reports them.
        if hasattr(self.video_source, "stats"):
            self.video_source.stats.connect(self._handle_stats)
        # Changed 2026-10-14: Show optional hardware frame decode errors in the event log, in the
        # language selected in the UI.
        if hasattr(self.video_source, "frame_error"):
            self.video_source.frame_error.connect(
                lambda message: self._log(
                    text(self.language, "log_video_error", detail=diagnostic_text(self.language, message))
                )
            )

    def _openmv_log(self, message: str) -> None:
        # Changed 2026-10-14: The OpenMV driver reports diagnostics in English; re-render the
        # known messages so the on-screen log follows the selected language.
        self._log(
            text(self.language, "log_device_info", detail=diagnostic_text(self.language, message))
        )

    def _open_output_directory(self) -> None:
        # Changed 2026-10-14: Logs and recordings are written next to the program; open that
        # folder so the operator can find them without knowing the deployment layout.
        directory = output_root()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory))):
            self._log(text(self.language, "open_output_directory_failed", path=directory))

    def _toggle_camera(self) -> None:
        if not self.camera_connected:
            try:
                port = self.openmv_port.currentText().strip()
                frame_size = self.frame_size_select.currentData()
                log_openmv_event(
                    f"UI requested OpenMV connection for port {port!r} frame_size={frame_size!r}"
                )
                source = OpenMVSerialVideoSource()
                source.frame_ready.connect(self._handle_video_frame)
                source.frame_error.connect(
                    lambda message: self._log(
                        text(
                            self.language,
                            "log_device_error",
                            detail=diagnostic_text(self.language, message),
                        )
                    )
                )
                source.diagnostic.connect(self._openmv_log)
                source.control_event.connect(self._handle_frame)
                # Changed 2026-10-14: Show the measured frame rates as soon as they are reported.
                source.stats.connect(self._handle_stats)
                # Changed 2026-10-14: The capture resolution is baked into the script text that
                # the host uploads, so it must be passed at connection time.
                source.connect_device(port, frame_size=frame_size)
                self.video_source = source
                self.camera_connected = True
                self.stream_running = True
                self.openmv_port.setEnabled(False)
                self.frame_size_select.setEnabled(False)
                width, height = source.width, source.height
                self._log(
                    text(
                        self.language,
                        "log_camera_connected",
                        port=port,
                        frame_size=frame_size,
                        width=width,
                        height=height,
                    )
                )
            except Exception as exc:
                log_openmv_event(f"UI connection exception: {type(exc).__name__}: {exc}")
                self._log(
                    text(
                        self.language,
                        "log_connection_failed",
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                )
                QMessageBox.critical(self, "Connection failed", str(exc))
        else:
            if self.stream_running:
                self.video_source.stop()
                self.stream_running = False
            self.video_source.disconnect_device()
            self.video_source = MockVideoSource()
            self.video_source.frame_ready.connect(self._handle_video_frame)
            self.camera_connected = False
            self.openmv_port.setEnabled(True)
            self.frame_size_select.setEnabled(True)
            # Changed 2026-10-14: Drop the stale rates so the panel does not show frozen numbers.
            self._handle_stats(0.0, 0.0)
        self._refresh_language()

    def _refresh_openmv_ports(self) -> None:
        current_port = self.openmv_port.currentText() if hasattr(self, "openmv_port") else ""
        ports = OpenMVSerialVideoSource.available_ports()
        self.openmv_port.clear()
        self.openmv_port.addItems(ports)
        if current_port and self.openmv_port.findText(current_port) < 0:
            self.openmv_port.setEditText(current_port)

    def _toggle_stream(self) -> None:
        if not self.stream_running:
            self.video_source.start()
            self.stream_running = True
            # Changed 2026-09-20: Advertise the injected hardware source's actual stream settings.
            self._send(
                command(
                    "start_stream",
                    width=self.video_source.width,
                    height=self.video_source.height,
                    fps=self.video_source.fps,
                )
            )
        else:
            self.video_source.stop()
            self.stream_running = False
            self._send(command("stop_stream"))
            # Changed 2026-10-14: A stopped stream produces no frames, so any open recording would
            # never receive a last frame and OpenCV would leave the MP4 unfinalised (and therefore
            # unplayable). Finalise it here instead of on exit.
            if self.recorder.is_recording:
                self._stop_recording()
        self._refresh_language()

    def _clear_suspect(self) -> None:
        if self.suspect_marked:
            # Changed 2026-09-20: Keep the downlink command for completeness even though this
            # firmware cannot receive it (see ControlChannelUnavailable).
            self._send(command("clear_suspect", track_id=self.suspect_track_id))
        # Changed 2026-10-14: Clearing the mark is what finalises (and therefore saves) the MP4
        # that automatic follow recording started.
        if self.recording_mode == "automatic" and self.recorder.is_recording:
            self._stop_recording()
        # Changed 2026-10-14: All identity state lives in the keeper now.
        self._suspect_lock.clear()
        self.suspect_track_id = None
        self.suspect_marked = False
        self._suspect_display_bbox = None
        self._automatic_recording_saved = False
        self.video_label.set_tracking_box(None)
        self.trajectory.clear()
        self.path_list.clear()
        self._refresh_language()

    def _toggle_recording(self) -> None:
        if self.recorder.is_recording:
            self._stop_recording()
            return

        self._start_recording()

    def _start_recording(self) -> None:
        if self.recorder.is_recording:
            return

        # Changed 2026-10-14: Recording needs a live stream. The old "arm while paused" path is
        # gone with the paused-frame workflow: it could only ever produce an empty file.
        if not self.stream_running:
            QMessageBox.information(
                self,
                text(self.language, "recording_unavailable_title"),
                text(self.language, "recording_unavailable_message"),
            )
            return

        output_path = self._recording_path()
        try:
            # Changed 2026-09-20: Keep MP4 timing aligned with the active hardware video source.
            self.recorder.start(
                output_path,
                self.video_source.width,
                self.video_source.height,
                self.video_source.fps,
            )
        except RuntimeError as exc:
            QMessageBox.critical(self, text(self.language, "recording_error"), str(exc))
            return
        # Changed 2026-10-14: Report the absolute path so the operator can find the file.
        self._log(text(self.language, "recording_started", path=output_path.resolve()))
        # Changed 2026-10-14: OpenCV only writes the MP4 index at release() time, so the file is
        # not playable until recording stops. Say explicitly what stops it.
        if self.recording_mode == "automatic":
            self._log(text(self.language, "automatic_recording_hint"))
        self._suspect_last_seen = time.monotonic()
        self._automatic_recording_saved = False
        self._send(command("start_recording", path=str(output_path)))
        self._refresh_language()

    @staticmethod
    def _recording_path() -> Path:
        # Changed 2026-10-14: Two recordings started within the same second used to share a file
        # name, so the second one silently overwrote the first. Add a suffix until it is free.
        directory = recordings_directory()
        stamp = f"{datetime.now():%Y%m%d_%H%M%S}"
        candidate = directory / f"suspect_{stamp}.mp4"
        index = 2
        while candidate.exists():
            candidate = directory / f"suspect_{stamp}_{index}.mp4"
            index += 1
        return candidate

    def _stop_recording(self) -> None:
        path = self.recorder.stop()
        self._send(command("stop_recording"))
        self._refresh_language()
        if path is None:
            self._log(text(self.language, "recording_empty"))
        else:
            self._log(text(self.language, "recording_saved", path=path.resolve()))

    def _change_recording_mode(self) -> None:
        # Changed 2026-09-20: Keep recording controls consistent with manual/automatic mode.
        self.recording_mode = self.recording_mode_select.currentData()
        self.record_button.setEnabled(self.recording_mode == "manual")
        self._send(command("set_recording_mode", mode=self.recording_mode))
        if (
            self.recording_mode == "automatic"
            and self.suspect_marked
            and self.stream_running
            and not self.recorder.is_recording
        ):
            self._start_recording()
        self._refresh_language()

    def _send(self, frame: ProtocolFrame) -> None:
        if not self.camera_connected or not isinstance(self.video_source, OpenMVSerialVideoSource):
            self._log(text(self.language, "log_command_not_sent", name=frame.name))
            return
        try:
            self.video_source.send_command(frame)
            self._log(
                text(self.language, "log_tx", kind=frame.kind, name=frame.name) + f" {frame.payload}"
            )
        except ControlChannelUnavailable as exc:
            # Changed 2026-10-12: This firmware cannot host the control channel. The stream is
            # driven directly by the host, so a skipped command is an expected downgrade.
            self._log(text(self.language, "log_command_not_delivered", detail=exc))
        except Exception as exc:
            self._log(text(self.language, "log_command_failed", name=frame.name, detail=exc))
            log_openmv_event(f"OpenMV command failed: {frame.name}: {type(exc).__name__}: {exc}")

    def _handle_frame(self, frame: ProtocolFrame) -> None:
        self._log(
            text(self.language, "log_rx", kind=frame.kind, name=frame.name) + f" {frame.payload}"
        )
        if frame.name == "track_update":
            detection = Detection(
                track_id=int(frame.payload["track_id"]),
                bbox=tuple(frame.payload["bbox"]),
                confidence=float(frame.payload["confidence"]),
            )
            self._update_tracks([detection])
        # Changed 2026-09-20: Accept one hardware event containing all faces in a frame.
        elif frame.name == "tracks_update":
            detections = [
                Detection(
                    track_id=int(item["track_id"]),
                    bbox=tuple(item["bbox"]),
                    confidence=float(item["confidence"]),
                    label=str(item.get("label", "person")),
                )
                for item in frame.payload.get("tracks", [])
            ]
            self._update_tracks(detections)
        # Changed 2026-09-20: Let hardware finish an automatically recorded trajectory.
        elif frame.name == "tracking_complete" and self.recording_mode == "automatic":
            if self.recorder.is_recording:
                self._stop_recording()
        # Changed 2026-09-20: Bind an ROI-only selection to the track ID assigned by hardware.
        elif frame.name == "suspect_acquired":
            self.suspect_track_id = int(frame.payload["track_id"])
            self.suspect_marked = True
            self._refresh_language()

    def _handle_video_frame(self, frame, detections: list[Detection] | None) -> None:
        # Changed 2026-10-13: The device detector reports the complete box list on every update,
        # so an empty list means "no pedestrians in view" and must clear the overlay. `None`
        # still means "the device has not reported yet" and leaves any bbox that arrived on the
        # control channel alone. The identity check skips the table rebuild on frames that carry
        # no fresh report (the reader hands out the same list object until a new one arrives).
        self._latest_frame = frame
        if detections is not None and detections is not self.last_detections:
            self.last_detections = detections
            self._update_tracks(detections, frame)
        if self.recorder.is_recording:
            # Changed 2026-10-14: Burn the red suspect box into the recorded frames, otherwise
            # the saved MP4 shows no indication of who was locked.
            self.recorder.write(
                annotate_frame(
                    frame,
                    self._suspect_display_bbox,
                    suspect_track_id=self.suspect_track_id,
                    suspect_label=self.suspect_label,
                )
            )
        image = frame_to_qimage(frame)
        # Changed 2026-09-20: Let VideoDisplay preserve and rescale the original frame.
        self.video_label.set_video_image(image)

    def _handle_suspect_picked(self, track_id: int) -> None:
        # Changed 2026-10-13: Live-stream suspect selection. The device keeps every
        # pedestrian's track ID stable with its on-device IoU tracker, so locking one ID is
        # enough for the host to follow the person without any downlink command. This avoids
        # the frozen-frame workflow entirely -- pausing to draw an ROI takes too long and the
        # suspect leaves the view before the stream resumes.
        # Changed 2026-10-14: Picking only makes sense on a live stream (a frozen frame reports no
        # new detections), so clicks are ignored while the stream is stopped.
        if not self.stream_running:
            return
        # Changed 2026-10-14: Suspects are auto-labelled, so no operator-typed name is involved.
        self.suspect_label = "suspect"
        self._suspect_lock.pick(track_id, self.last_detections, self._latest_frame)
        self.suspect_track_id = self._suspect_lock.track_id
        self.suspect_marked = True
        self._suspect_display_bbox = self._suspect_lock.last_bbox
        # Changed 2026-10-14: Restart the automatic-recording watchdog window for the new pick.
        self._suspect_last_seen = time.monotonic()
        self._automatic_recording_saved = False
        # Changed 2026-10-14: Show the red box immediately instead of waiting for the next
        # detection report (~8 reports/s), so a click gives instant visual feedback.
        self.video_label.set_tracking_box(self._suspect_display_bbox)
        self._log(text(self.language, "suspect_picked", track_id=track_id, label=self.suspect_label))
        self._refresh_language()
        if self.recording_mode == "automatic" and not self.recorder.is_recording:
            self._start_recording()

    def _update_tracks(self, detections: list[Detection], frame=None) -> None:
        # Changed 2026-10-13: Mirror the hardware detection list onto the video overlay.
        self.video_label.set_detections(detections)

        # Changed 2026-10-14: The host-side identity keeper decides which detection still *is*
        # the locked suspect. It combines a motion prediction, box overlap and an appearance
        # signature, refuses to move the lock while two candidates look equally plausible, and
        # requires a new ID to win for two consecutive reports. Re-binding on plain IoU used to
        # send the red box onto the pedestrian standing next to the suspect.
        state = self._suspect_lock.update(detections, frame)
        self.suspect_track_id = state.track_id
        self.suspect_marked = self._suspect_lock.marked
        if state.relocked:
            self._log(
                text(
                    self.language,
                    "suspect_relocked",
                    old_id=state.relocked_from,
                    new_id=state.relocked_to,
                )
            )
        if state.ambiguous:
            self._log(text(self.language, "suspect_ambiguous"))
        if state.lost:
            self._log(text(self.language, "suspect_lost", track_id=self.suspect_track_id))
        if state.reacquired:
            self._log(text(self.language, "suspect_reacquired", track_id=self.suspect_track_id))
        if state.visible:
            # Remember when the suspect was last actually visible so the automatic-mode
            # recording can be finalised after a sustained loss.
            self._suspect_last_seen = time.monotonic()

        self.track_table.setRowCount(len(detections))
        for row, detection in enumerate(detections):
            self.current_track_id = detection.track_id
            label = self.suspect_label if detection.track_id == self.suspect_track_id else detection.label
            bbox_text = ", ".join(str(value) for value in detection.bbox)
            values = [str(detection.track_id), label, f"{detection.confidence:.2f}", bbox_text]
            for column, value in enumerate(values):
                self.track_table.setItem(row, column, QTableWidgetItem(value))

            if state.visible and detection.track_id == self.suspect_track_id:
                x1, y1, x2, y2 = detection.bbox
                center = ((x1 + x2) // 2, (y1 + y2) // 2)
                self.trajectory.append(center)
                self.path_list.addItem(f"{datetime.now():%H:%M:%S}  {center[0]}, {center[1]}")

        # Changed 2026-09-20: Draw the moving suspect box from the latest bbox.
        # Changed 2026-10-14: During a short dropout the keeper keeps returning the last known box
        # for a few reports instead of blanking the marker, and returns None once it gives up.
        self._suspect_display_bbox = state.bbox
        self.video_label.set_tracking_box(state.bbox)

    def _handle_stats(self, host_fps: float, device_fps: float) -> None:
        # Changed 2026-10-14: Surface the measured capture rate in the status panel.
        self.host_fps = host_fps
        self.device_fps = device_fps
        self._refresh_fps_label()

    def _refresh_fps_label(self) -> None:
        if not self.stream_running:
            self.fps_label.setText(text(self.language, "fps_idle"))
        else:
            self.fps_label.setText(
                text(self.language, "fps", host=self.host_fps, device=self.device_fps)
            )

    def _check_automatic_recording(self) -> None:
        # Changed 2026-10-14: Automatic follow recording must not stay open forever after the
        # suspect walks away: OpenCV only writes the MP4 index when the writer is released, so an
        # unfinalised file cannot be played. Once the suspect has been missing for
        # AUTOMATIC_RECORDING_STOP_SECONDS the recording is stopped and saved. The suspect lock
        # itself is deliberately kept (it is never cleared automatically).
        if (
            self.recording_mode != "automatic"
            or not self.recorder.is_recording
            or self._automatic_recording_saved
        ):
            return
        missing_for = time.monotonic() - self._suspect_last_seen
        if missing_for < AUTOMATIC_RECORDING_STOP_SECONDS:
            return
        path = self.recorder.output_path
        # Mark first so a slow stop cannot trigger a second save.
        self._automatic_recording_saved = True
        self._stop_recording()
        if path is not None:
            self._log(
                text(
                    self.language,
                    "automatic_recording_saved",
                    seconds=AUTOMATIC_RECORDING_STOP_SECONDS,
                    path=path.resolve(),
                )
            )

    def _log(self, message: str) -> None:
        self.event_log.appendPlainText(f"{datetime.now():%H:%M:%S} {message}")
        # Changed 2026-10-14: Mirror the console log into a file so past sessions can be reviewed.
        log_ui_event(message)

    # Changed 2026-10-14: A second launch is refused by the single-instance guard; rather than
    # doing nothing it asks this window to come to the front, so the operator sees that the
    # program is already up instead of wondering why the click had no effect.
    def activate_from_duplicate_launch(self) -> None:
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()
        # Windows can refuse a focus steal from a background process, so also flash the taskbar.
        QApplication.alert(self)
        self._log(text(self.language, "duplicate_launch_blocked"))

    def closeEvent(self, event: QCloseEvent) -> None:
        # Changed 2026-09-20: Finalize MP4 metadata and release hardware resources on exit.
        self._recording_watchdog.stop()
        if self.recorder.is_recording:
            # Changed 2026-10-14: Log where the pending recording was saved so the operator can
            # find it after the window is gone.
            path = self.recorder.output_path
            saved = self.recorder.stop()
            if saved is not None:
                self._log(text(self.language, "recording_saved_on_exit", path=saved.resolve()))
            elif path is not None:
                self._log(text(self.language, "recording_empty"))
        if self.stream_running:
            self.video_source.stop()
        if self.camera_connected:
            self.video_source.disconnect_device()
        super().closeEvent(event)

    def _change_theme(self) -> None:
        self.theme_key = self.theme_select.currentData()
        self._apply_theme()

    def _change_language(self) -> None:
        self.language = self.language_select.currentData()
        self._refresh_language()

    def _apply_theme(self) -> None:
        self.setStyleSheet(build_stylesheet(THEMES[self.theme_key]))

    def _refresh_language(self) -> None:
        self.setWindowTitle(text(self.language, "window_title"))
        self.title_label.setText(text(self.language, "title"))
        if self.video_label.pixmap() is None:
            self.video_label.setText(text(self.language, "video_idle"))
        if self.stream_running:
            self.selection_hint_label.setText(text(self.language, "selection_hint_running"))
        elif self.video_label.pixmap() is None:
            self.selection_hint_label.setText(text(self.language, "selection_hint_empty"))
        else:
            # Changed 2026-10-14: A stopped stream is a plain frozen frame now; the drag-to-select
            # fallback was removed, so the hint only points at the running stream.
            self.selection_hint_label.setText(text(self.language, "selection_hint_stopped"))
        self.settings_box.setTitle(text(self.language, "settings"))
        self.theme_label.setText(text(self.language, "theme"))
        self.language_label.setText(text(self.language, "language"))
        self.control_box.setTitle(text(self.language, "control"))
        self.camera_port_label.setText(text(self.language, "camera_port"))
        # Changed 2026-10-14: The old AI-profile / manual-label controls are gone; the same grid
        # cells now carry the capture-resolution picker.
        self.frame_size_label.setText(text(self.language, "frame_size"))
        self.recording_mode_label.setText(text(self.language, "recording_mode"))
        self.recording_mode_select.setItemText(0, text(self.language, "recording_manual"))
        self.recording_mode_select.setItemText(1, text(self.language, "recording_automatic"))
        self.connect_button.setText(
            text(self.language, "disconnect" if self.camera_connected else "connect")
        )
        self.stream_button.setText(
            text(self.language, "stop_stream" if self.stream_running else "start_stream")
        )
        self.clear_button.setText(text(self.language, "clear_mark"))
        self.clear_button.setEnabled(self.suspect_marked)
        self.record_button.setText(
            text(self.language, "stop_recording" if self.recorder.is_recording else "start_recording")
        )
        self.output_button.setText(text(self.language, "open_output_directory"))
        self.status_box.setTitle(text(self.language, "status"))
        self.status_label.setText(
            text(self.language, "camera_connected" if self.camera_connected else "camera_disconnected")
        )
        self._refresh_fps_label()
        if not self.suspect_marked:
            self.active_target_label.setText(text(self.language, "active_target_none"))
        elif self.suspect_track_id is None:
            self.active_target_label.setText(
                text(self.language, "active_target_roi", label=self.suspect_label)
            )
        else:
            self.active_target_label.setText(
                text(
                    self.language,
                    "active_target",
                    track_id=self.suspect_track_id,
                    label=self.suspect_label,
                )
            )
        if self.recorder.is_recording and self.recorder.output_path is not None:
            self.recording_label.setText(
                text(self.language, "recording_on", path=self.recorder.output_path.resolve())
            )
        else:
            self.recording_label.setText(text(self.language, "recording_off"))
        self.live_tracks_box.setTitle(text(self.language, "live_tracks"))
        self.trajectory_box.setTitle(text(self.language, "trajectory_path"))
        self.event_log_box.setTitle(text(self.language, "event_log"))
        self.track_table.setHorizontalHeaderLabels(
            [
                text(self.language, "track_id"),
                text(self.language, "table_label"),
                text(self.language, "confidence"),
                text(self.language, "bbox"),
            ]
        )
