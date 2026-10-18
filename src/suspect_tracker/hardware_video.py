from __future__ import annotations

import struct
import sys
import time
import os
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import QObject, QThread, QTimer, Signal

from suspect_tracker.openmv_protocol import (
    PIXFORMAT_GRAYSCALE,
    PIXFORMAT_JPEG,
    PIXFORMAT_RGB565,
    OpenMVProtocolClient,
    OpenMVProtocolError,
)


class ControlChannelUnavailable(OpenMVProtocolError):
    # Changed 2026-10-12: Raised when the firmware physically cannot host the custom control
    # channel, so callers can tell an expected downgrade from a real delivery failure.
    pass
from suspect_tracker.protocol import ProtocolFrame
from suspect_tracker.video import Detection

# Changed 2026-10-14: All log/recording paths live in suspect_tracker.paths so the launcher can
# use them without importing cv2/PySide6. Re-exported here for existing callers.
from suspect_tracker.paths import (
    append_log,
    fallback_output_root,
    hardware_log_path,
    log_directory,
    log_openmv_event,
    log_ui_event,
    output_root,
    recordings_directory,
    ui_event_log_path,
)


# Changed 2026-10-14: Frame sizes this sensor accepts, measured on hardware. HVGA/480x320 is
# rejected by the sensor ("Sensor control failed"). VGA quadruples the pixels but costs about
# half the frame rate on the wire (measured: QVGA ~34 fps vs VGA ~12 fps for raw capture,
# 15.6 vs 8.0 fps with detection running).
FRAME_SIZE_OPTIONS: dict[str, tuple[int, int]] = {
    "QVGA": (320, 240),
    "VGA": (640, 480),
}
DEFAULT_FRAME_SIZE = "QVGA"


class _OpenMVFrameReader(QThread):
    # Changed 2026-10-13: detections now travel with the frame so the overlay is drawn for the
    # same frame it belongs to instead of lagging one event behind the video.
    frame_received = Signal(object, object)
    event_received = Signal(object)
    read_error = Signal(str)
    diagnostic = Signal(str)
    # Changed 2026-10-14: Report measured frame rates to the UI: host fps (frames the console
    # actually received) and device fps (reported by the capture script in tracks_update).
    stats_updated = Signal(float, float)

    # Changed 2026-10-12: A single transient fault used to terminate this thread, which left the
    # video panel black with no obvious cause. Retry a few times before declaring a hard failure.
    MAX_CONSECUTIVE_ERRORS = 10

    # Changed 2026-10-13: Device detections arrive as JSON lines on the stdout channel. Polling
    # every frame cost ~9 fps on hardware (each poll is a serial round trip), so the interval is
    # deliberately much larger than the device report rate; a late box is harmless, a slow video
    # stream is not.
    STDOUT_POLL_INTERVAL = 0.05

    def __init__(self, client: OpenMVProtocolClient) -> None:
        super().__init__()
        self._client = client
        self._stopping = False
        # Changed 2026-10-13: `stdout` is the only device->host text channel this firmware
        # exposes (it always exists), so script print() output is polled from there. The
        # custom `tracker` channel never existed on the RA8P1 Titan Mini.
        self._event_channel: str | None = "stdout" if "stdout" in client.channels else None
        # None means "the device has not reported yet"; an empty list means "no faces in view".
        self._latest_detections: list[Detection] | None = None
        self._next_stdout_poll = 0.0
        # Changed 2026-10-14: Rolling one-second window for the host-side frame rate.
        self._fps_window_start = time.monotonic()
        self._fps_window_frames = 0
        self._device_fps = 0.0

    def prepare_restart(self) -> None:
        # Changed 2026-10-12: One reader instance is reused across every stream start/stop cycle.
        self._stopping = False

    def run(self) -> None:
        consecutive_errors = 0
        while not self._stopping:
            try:
                self._poll_events()
                frame_packet = self._client.read_frame_packet()
                if frame_packet is None:
                    # Changed 2026-10-12: 2ms poll instead of 5ms. The camera produces a frame
                    # every ~30ms; a 5ms sleep let up to 14ms of extra latency accumulate per
                    # frame once the host could keep up with the device rate.
                    time.sleep(0.002)
                    continue
                image = self._decode_frame(frame_packet)
                consecutive_errors = 0
                self.frame_received.emit(image, self._latest_detections)
                # Changed 2026-10-14: Emit the measured rates once per second.
                self._fps_window_frames += 1
                now = time.monotonic()
                window = now - self._fps_window_start
                if window >= 1.0:
                    self.stats_updated.emit(self._fps_window_frames / window, self._device_fps)
                    self._fps_window_start = now
                    self._fps_window_frames = 0
            except TimeoutError:
                time.sleep(0.002)
                continue
            except Exception as exc:
                if self._stopping:
                    return
                consecutive_errors += 1
                if consecutive_errors >= self.MAX_CONSECUTIVE_ERRORS:
                    self.read_error.emit(str(exc))
                    return
                self.diagnostic.emit(
                    f"OpenMV frame read failed ({consecutive_errors}/{self.MAX_CONSECUTIVE_ERRORS}): {exc}"
                )
                time.sleep(0.02)

    def _poll_events(self) -> None:
        channel = self._event_channel
        if channel is None:
            return
        now = time.monotonic()
        if now < self._next_stdout_poll:
            return
        self._next_stdout_poll = now + self.STDOUT_POLL_INTERVAL
        try:
            events = self._client.read_channel(channel)
        except OpenMVProtocolError as exc:
            # Changed 2026-10-12: Losing the control channel must never stop video output.
            self._event_channel = None
            self.diagnostic.emit(
                f"OpenMV control channel {channel!r} is unavailable ({exc}); "
                "video continues without hardware events."
            )
            return
        except TimeoutError:
            return
        for event_line in events.splitlines():
            if not event_line:
                continue
            try:
                frame = ProtocolFrame.from_json_line(event_line)
            except Exception:
                # Changed 2026-10-13: stdout also carries REPL banners and tracebacks, so a
                # line that is not one of our JSON events is expected and simply skipped.
                continue
            if frame.name == "tracks_update":
                self._latest_detections = self._parse_detections(frame)
                # Changed 2026-10-14: The script reports its own capture rate; keep the latest.
                try:
                    self._device_fps = float(frame.payload.get("fps") or 0.0)
                except (TypeError, ValueError, AttributeError):
                    self._device_fps = 0.0
                continue
            self.event_received.emit(frame)

    @staticmethod
    def _parse_detections(frame: ProtocolFrame) -> list[Detection]:
        # Changed 2026-10-13: Device reports faces as {track_id, bbox[x1,y1,x2,y2], confidence}.
        detections: list[Detection] = []
        for item in frame.payload.get("tracks", []):
            try:
                detections.append(
                    Detection(
                        track_id=int(item["track_id"]),
                        bbox=tuple(int(value) for value in item["bbox"]),
                        confidence=float(item["confidence"]),
                        label=str(item.get("label", "face")),
                    )
                )
            except Exception:
                continue
        return detections

    @staticmethod
    def _decode_frame(frame_packet: tuple[int, int, int, int, bytes]) -> np.ndarray:
        width, height, pixel_format, _depth, payload = frame_packet
        if width <= 0 or height <= 0:
            raise ValueError(f"Invalid OpenMV frame dimensions: {width}x{height}")
        if pixel_format == PIXFORMAT_JPEG:
            image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError("OpenMV stream returned an invalid JPEG frame")
        elif pixel_format == PIXFORMAT_RGB565:
            if len(payload) != width * height * 2:
                raise ValueError(f"Invalid RGB565 frame size: {len(payload)} bytes")
            rgb565 = np.frombuffer(payload, dtype="<u2").reshape(height, width)
            red = (((rgb565 >> 11) & 0x1F).astype(np.uint16) * 255 // 31).astype(np.uint8)
            green = (((rgb565 >> 5) & 0x3F).astype(np.uint16) * 255 // 63).astype(np.uint8)
            blue = (((rgb565 & 0x1F).astype(np.uint16) * 255 // 31)).astype(np.uint8)
            image = np.dstack((blue, green, red))
        elif pixel_format == PIXFORMAT_GRAYSCALE:
            if len(payload) != width * height:
                raise ValueError(f"Invalid grayscale frame size: {len(payload)} bytes")
            gray = np.frombuffer(payload, dtype=np.uint8).reshape(height, width)
            image = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        else:
            raise ValueError(f"Unsupported OpenMV pixel format: 0x{pixel_format:08X}")
        return np.ascontiguousarray(image)

    def stop(self) -> None:
        self._stopping = True


# Changed 2026-09-20: Define the binary frame contract used for Titan hardware video input.
MAGIC = b"TVF1"
HEADER_STRUCT = struct.Struct("<4sBBHIHHIIQ")
HEADER_SIZE = HEADER_STRUCT.size
MAX_PAYLOAD_SIZE = 32 * 1024 * 1024


class PixelFormat(IntEnum):
    BGR24 = 1
    RGB24 = 2
    JPEG = 3
    YUYV422 = 4
    NV12 = 5


@dataclass(frozen=True)
class HardwareFrame:
    frame_id: int
    timestamp_us: int
    width: int
    height: int
    stride: int
    pixel_format: PixelFormat
    payload: bytes


def parse_frame_packet(packet: bytes) -> HardwareFrame:
    if len(packet) < HEADER_SIZE:
        raise ValueError(f"Frame packet is shorter than the {HEADER_SIZE}-byte header")

    magic, version, format_id, header_size, payload_size, width, height, stride, frame_id, timestamp_us = (
        HEADER_STRUCT.unpack_from(packet)
    )
    if magic != MAGIC:
        raise ValueError(f"Invalid frame magic: {magic!r}")
    if version != 1:
        raise ValueError(f"Unsupported frame protocol version: {version}")
    if header_size != HEADER_SIZE:
        raise ValueError(f"Unsupported frame header size: {header_size}")
    if len(packet) != header_size + payload_size:
        raise ValueError(
            f"Frame payload length mismatch: expected {payload_size}, got {len(packet) - header_size}"
        )

    return HardwareFrame(
        frame_id=frame_id,
        timestamp_us=timestamp_us,
        width=width,
        height=height,
        stride=stride,
        pixel_format=PixelFormat(format_id),
        payload=packet[header_size:],
    )


def build_frame_packet(frame: HardwareFrame) -> bytes:
    header = HEADER_STRUCT.pack(
        MAGIC,
        1,
        int(frame.pixel_format),
        HEADER_SIZE,
        len(frame.payload),
        frame.width,
        frame.height,
        frame.stride,
        frame.frame_id,
        frame.timestamp_us,
    )
    return header + frame.payload


def decode_hardware_frame(frame: HardwareFrame) -> np.ndarray:
    if frame.width <= 0 or frame.height <= 0:
        raise ValueError("Frame width and height must be positive")

    if frame.pixel_format == PixelFormat.JPEG:
        encoded = np.frombuffer(frame.payload, dtype=np.uint8)
        image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("Unable to decode JPEG frame")
        if image.shape[:2] != (frame.height, frame.width):
            raise ValueError(
                f"JPEG dimensions {image.shape[1]}x{image.shape[0]} do not match header "
                f"{frame.width}x{frame.height}"
            )
        return np.ascontiguousarray(image)

    if frame.pixel_format in (PixelFormat.BGR24, PixelFormat.RGB24):
        row_bytes = frame.width * 3
        stride = frame.stride or row_bytes
        if stride < row_bytes or len(frame.payload) != stride * frame.height:
            raise ValueError("Invalid BGR24/RGB24 payload size or stride")
        rows = np.frombuffer(frame.payload, dtype=np.uint8).reshape(frame.height, stride)
        image = rows[:, :row_bytes].reshape(frame.height, frame.width, 3)
        if frame.pixel_format == PixelFormat.RGB24:
            image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
        return np.ascontiguousarray(image)

    if frame.pixel_format == PixelFormat.YUYV422:
        row_bytes = frame.width * 2
        stride = frame.stride or row_bytes
        if frame.width % 2 or stride < row_bytes or len(frame.payload) != stride * frame.height:
            raise ValueError("Invalid YUYV422 payload size, width, or stride")
        rows = np.frombuffer(frame.payload, dtype=np.uint8).reshape(frame.height, stride)
        yuyv = rows[:, :row_bytes].reshape(frame.height, frame.width, 2)
        return cv2.cvtColor(yuyv, cv2.COLOR_YUV2BGR_YUY2)

    if frame.pixel_format == PixelFormat.NV12:
        expected_size = frame.width * frame.height * 3 // 2
        if frame.width % 2 or frame.height % 2 or len(frame.payload) != expected_size:
            raise ValueError("Invalid NV12 payload size or dimensions")
        nv12 = np.frombuffer(frame.payload, dtype=np.uint8).reshape(frame.height * 3 // 2, frame.width)
        return cv2.cvtColor(nv12, cv2.COLOR_YUV2BGR_NV12)

    raise ValueError(f"Unsupported pixel format: {frame.pixel_format}")


class FramePacketAssembler:
    # Changed 2026-09-20: Reassemble complete TVF1 frames from fragmented HS USB reads.
    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buffer.extend(chunk)
        packets: list[bytes] = []

        while True:
            magic_offset = self._buffer.find(MAGIC)
            if magic_offset < 0:
                self._buffer = self._buffer[-(len(MAGIC) - 1) :]
                break
            if magic_offset:
                del self._buffer[:magic_offset]
            if len(self._buffer) < HEADER_SIZE:
                break

            _, version, _, header_size, payload_size, *_ = HEADER_STRUCT.unpack_from(self._buffer)
            if version != 1 or header_size != HEADER_SIZE or payload_size > MAX_PAYLOAD_SIZE:
                del self._buffer[0]
                continue

            packet_size = header_size + payload_size
            if len(self._buffer) < packet_size:
                break
            packets.append(bytes(self._buffer[:packet_size]))
            del self._buffer[:packet_size]

        return packets


class HardwareVideoSource(QObject):
    # Changed 2026-09-20: Bridge hardware packets to the existing Qt frame_ready pipeline.
    frame_ready = Signal(object, object)
    frame_error = Signal(str)
    packet_received = Signal(bytes, object)

    def __init__(self, width: int = 1280, height: int = 720, fps: int = 30) -> None:
        super().__init__()
        self.width = width
        self.height = height
        self.fps = fps
        self._running = False
        self._assembler = FramePacketAssembler()
        self.packet_received.connect(self._decode_packet)

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    def submit_packet(self, packet: bytes, detections: list[Detection] | None = None) -> None:
        self.packet_received.emit(packet, detections or [])

    def submit_usb_chunk(self, chunk: bytes, detections: list[Detection] | None = None) -> None:
        for packet in self._assembler.feed(chunk):
            self.submit_packet(packet, detections)

    def _decode_packet(self, packet: bytes, detections: list[Detection]) -> None:
        if not self._running:
            return
        try:
            hardware_frame = parse_frame_packet(packet)
            image = decode_hardware_frame(hardware_frame)
        except (ValueError, cv2.error) as exc:
            self.frame_error.emit(str(exc))
            return
        self.width = hardware_frame.width
        self.height = hardware_frame.height
        self.frame_ready.emit(image, detections)


class _SerialReader(QThread):
    chunk_received = Signal(bytes)
    read_error = Signal(str)

    def __init__(self, serial_port) -> None:
        super().__init__()
        self._serial_port = serial_port
        self._stopping = False

    def run(self) -> None:
        while not self._stopping:
            try:
                chunk = self._serial_port.read(4096)
            except Exception as exc:
                if not self._stopping:
                    self.read_error.emit(str(exc))
                return
            if chunk:
                self.chunk_received.emit(chunk)

    def stop(self) -> None:
        self._stopping = True


class OpenMVSerialVideoSource(HardwareVideoSource):
    diagnostic = Signal(str)
    # Changed 2026-10-14: Measured frame rates for the UI (host fps, device fps).
    stats = Signal(float, float)

    def __init__(self, width: int = 320, height: int = 240, fps: int = 15) -> None:
        super().__init__(width=width, height=height, fps=fps)
        self._serial_port = None
        self._frame_count = 0
        self._client: OpenMVProtocolClient | None = None
        self._frame_reader: _OpenMVFrameReader | None = None
        # Changed 2026-10-14: Resolution the capture script was told to use (see
        # FRAME_SIZE_OPTIONS). Applied on the next connect, because the host uploads the
        # script text at connection time.
        self.frame_size = DEFAULT_FRAME_SIZE
        # Changed 2026-10-12: Not every firmware can host a custom channel; video still works.
        self.control_available = False
        # Changed 2026-10-12: Watchdog state. A connected-but-black panel used to stay silent
        # forever; now it escalates automatically and records every step in hardware.log.
        self._last_frame_count = 0
        self._stall_attempts = 0
        self._diagnostic_timer = QTimer(self)
        self._diagnostic_timer.setInterval(3000)
        self._diagnostic_timer.timeout.connect(self._report_missing_video)

    @staticmethod
    def available_ports() -> list[str]:
        from serial.tools import list_ports

        return [port.device for port in list_ports.comports()]

    def connect_device(self, port: str, baudrate: int = 921600, frame_size: str = DEFAULT_FRAME_SIZE) -> None:
        if self._serial_port is not None:
            raise RuntimeError("OpenMV device is already connected")
        if not port:
            raise ValueError("Select the OpenMV USB COM port before connecting")
        import serial

        frame_size = frame_size if frame_size in FRAME_SIZE_OPTIONS else DEFAULT_FRAME_SIZE
        self.frame_size = frame_size
        self.width, self.height = FRAME_SIZE_OPTIONS[frame_size]

        serial_port = serial.Serial(port, baudrate, timeout=2.0)
        try:
            log_openmv_event(
                f"Opened {port} at {baudrate} baud using OpenMV client default control lines "
                f"(DTR={serial_port.dtr}, RTS={serial_port.rts})"
            )
            time.sleep(0.25)
            script_path = self._openmv_script_path()
            if not script_path.is_file():
                raise FileNotFoundError(f"OpenMV capture script not found: {script_path}")
            self.diagnostic.emit("Connecting with the OpenMV camera CDC protocol.")
            client = OpenMVProtocolClient(serial_port, timeout=2.0, trace=log_openmv_event)
            channels = client.connect()
            log_openmv_event(f"OpenMV protocol connected; channels={channels}")
            client.stop_script()
            log_openmv_event(
                f"OpenMV existing script stopped; uploading AE3 capture script "
                f"(frame size {frame_size} = {self.width}x{self.height})"
            )
            time.sleep(0.5)
            client.run_script(self._script_text())
            # Changed 2026-10-12: Give the device time to finish starting the script and
            # resetting its protocol counters before the host polls the channel list.
            time.sleep(0.3)
            self._resolve_control_channel(client)
            log_openmv_event("AE3 capture script executed")
        except Exception as exc:
            log_openmv_event(f"Connection failed on {port}: {type(exc).__name__}: {exc}")
            if serial_port.is_open:
                serial_port.close()
            raise
        self._serial_port = serial_port
        self._client = client
        # Changed 2026-10-12: Build the reader once here; start() only starts it, so the signal
        # wiring cannot be lost by a second construction.
        self._frame_reader = self._build_frame_reader(client)
        self._diagnostic_timer.start()
        try:
            self.start()
        except Exception:
            try:
                self.disconnect_device()
            except Exception as cleanup_error:
                log_openmv_event(f"OpenMV cleanup after startup failure also failed: {cleanup_error}")
            raise
        self.diagnostic.emit(f"OpenMV protocol connected on {port}; camera script is running.")

    def _resolve_control_channel(self, client: OpenMVProtocolClient) -> None:
        # Changed 2026-10-12: The RA8P1 Titan Mini firmware has no `protocol` module, so the
        # capture script cannot register a custom channel. Video streaming is unaffected; the
        # suspect commands simply have nowhere to go, which must not fail the connection.
        try:
            client.wait_for_channel("tracker", timeout=1.5)
        except (TimeoutError, OpenMVProtocolError) as exc:
            self.control_available = False
            message = (
                f"AE3 script did not register the 'tracker' control channel ({exc}); "
                f"suspect commands are disabled. visible channels={sorted(client.channels)}"
            )
            log_openmv_event(message)
            self.diagnostic.emit(
                "Firmware exposes no 'tracker' control channel; "
                "video works but suspect commands cannot be delivered."
            )
        else:
            self.control_available = True
            log_openmv_event("AE3 script registered the 'tracker' control channel")

    def _build_frame_reader(self, client: OpenMVProtocolClient) -> _OpenMVFrameReader:
        # Changed 2026-10-12: Wire the reader's signals in exactly one place.
        reader = _OpenMVFrameReader(client)
        reader.frame_received.connect(self._handle_openmv_frame)
        reader.event_received.connect(self._handle_openmv_event)
        reader.read_error.connect(self._handle_openmv_read_error)
        reader.diagnostic.connect(self._handle_openmv_diagnostic)
        # Changed 2026-10-14: Forward measured frame rates to the UI.
        reader.stats_updated.connect(self.stats.emit)
        return reader

    def start(self) -> None:
        was_running = self._running
        super().start()
        if self._client is None or was_running:
            return
        # Changed 2026-10-12: On this firmware the stream channel is driven directly by the host,
        # so enabling streaming here is what actually starts the camera pushing frames.
        self._client.set_streaming(True)
        # Changed 2026-10-12: Streaming transitions must be visible in hardware.log; trace is
        # silenced while frames flow, so these markers are the only record of the state.
        log_openmv_event("OpenMV streaming enabled (STREAM_CTRL=1)")
        self._stall_attempts = 0
        self._last_frame_count = self._frame_count
        reader = self._frame_reader
        if reader is None:
            reader = self._build_frame_reader(self._client)
            self._frame_reader = reader
        if not reader.isRunning():
            reader.prepare_restart()
            reader.start()

    def stop(self) -> None:
        was_running = self._running
        super().stop()
        reader = self._frame_reader
        if reader is not None and reader.isRunning():
            reader.stop()
            reader.wait(2000)
        if self._client is not None and was_running:
            self._client.set_streaming(False)
            log_openmv_event("OpenMV streaming disabled (STREAM_CTRL=0)")

    def _restart_reader(self) -> None:
        # Changed 2026-10-12: Used by the no-frame watchdog to recover a dead/parked reader.
        reader = self._frame_reader
        if reader is None:
            if self._client is None:
                return
            self._frame_reader = self._build_frame_reader(self._client)
            self._frame_reader.start()
            return
        if reader.isRunning():
            reader.stop()
            reader.wait(2500)
        reader.prepare_restart()
        reader.start()

    def _handle_openmv_frame(self, image: np.ndarray, detections: list[Detection] | None) -> None:
        self.width = image.shape[1]
        self.height = image.shape[0]
        self._frame_count += 1
        if self._frame_count == 1:
            message = f"First OpenMV protocol frame decoded: {self.width}x{self.height}."
            log_openmv_event(message)
            self.diagnostic.emit(message)
        elif self._frame_count % 150 == 0:
            # Changed 2026-10-12: A sparse heartbeat proves the pipeline is alive without
            # flooding hardware.log at frame rate.
            log_openmv_event(f"OpenMV frame heartbeat: {self._frame_count} frames decoded")
        # Changed 2026-10-13: Forward the device's face boxes with the frame. `None` means the
        # device has not reported yet and must not wipe an existing overlay.
        self.frame_ready.emit(image, detections)

    def _handle_openmv_read_error(self, message: str) -> None:
        log_openmv_event(f"OpenMV stream reader failed: {message}")
        self.frame_error.emit(message)

    def _handle_openmv_diagnostic(self, message: str) -> None:
        log_openmv_event(message)
        self.diagnostic.emit(message)

    def _handle_openmv_event(self, frame: ProtocolFrame) -> None:
        self.control_event.emit(frame)

    control_event = Signal(object)

    def send_command(self, frame: ProtocolFrame) -> None:
        if self._client is None:
            raise RuntimeError("OpenMV camera is not connected")
        if not self.control_available:
            # Changed 2026-10-12: Fail loudly with the real reason instead of a channel lookup error.
            raise ControlChannelUnavailable(
                "this firmware does not expose a 'tracker' control channel, so "
                f"the {frame.name} command cannot be delivered"
            )
        self._client.write_channel("tracker", frame.to_json_line())
        log_openmv_event(f"OpenMV control command sent: {frame.name} {frame.payload}")

    @staticmethod
    def _openmv_script_path() -> Path:
        # Changed 2026-10-18: 设备端脚本从 examples/ 移到 src/device/，源码树里不再有
        # 与 src 平级的散落目录；PyInstaller 的 --add-data 目标目录同步改成 device。
        relative_path = Path("device") / "openmv_ae3_raw_jpeg.py"
        if getattr(sys, "frozen", False):
            bundle_path = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / relative_path
            if bundle_path.is_file():
                return bundle_path
        # 源码运行：本文件位于 src/suspect_tracker/，parents[1] 即 src/
        return Path(__file__).resolve().parents[1] / relative_path

    def _script_text(self) -> str:
        # Changed 2026-10-14: The capture script ships with QVGA; the selected frame size is
        # substituted into the text before it is uploaded (the script stays self-contained).
        source = self._openmv_script_path().read_text(encoding="utf-8")
        return source.replace("csi0.framesize(csi.QVGA)", f"csi0.framesize(csi.{self.frame_size})")

    def _report_missing_video(self) -> None:
        # Changed 2026-10-12: A connected-but-black panel used to stay silent forever. Now the
        # watchdog escalates: re-assert stream control, then re-upload the capture script, and
        # only then report failure. Every step lands in hardware.log.
        if not self._running or self._client is None:
            return
        if self._frame_count != self._last_frame_count:
            self._last_frame_count = self._frame_count
            self._stall_attempts = 0
            return
        self._stall_attempts += 1
        try:
            if self._stall_attempts == 1:
                log_openmv_event("OpenMV watchdog: no new frames for 3s; re-asserting stream control")
                self.diagnostic.emit("No frames from the camera; re-asserting the video stream.")
                self._client.set_streaming(True)
                self._restart_reader()
            elif self._stall_attempts == 2:
                log_openmv_event("OpenMV watchdog: still no frames; re-uploading the AE3 capture script")
                self.diagnostic.emit("Still no frames; re-uploading the AE3 capture script.")
                self._client.stop_script()
                time.sleep(0.3)
                # Changed 2026-10-14: Re-upload the same frame size the UI selected.
                self._client.run_script(self._script_text())
                time.sleep(0.3)
                self._client.set_streaming(True)
                self._restart_reader()
            else:
                self._diagnostic_timer.stop()
                message = (
                    "OpenMV is connected but the camera produced no frames; "
                    "check the AE3 capture script and the CSI camera module."
                )
                log_openmv_event(f"OpenMV watchdog gave up: {message}")
                self.frame_error.emit(message)
        except Exception as exc:
            log_openmv_event(f"OpenMV watchdog recovery failed: {type(exc).__name__}: {exc}")
            self.diagnostic.emit(f"OpenMV recovery attempt failed: {exc}")

    def disconnect_device(self) -> None:
        self._diagnostic_timer.stop()
        if self._client is not None:
            try:
                self.stop()
            except Exception as exc:
                log_openmv_event(f"OpenMV stream stop during disconnect failed: {exc}")
        reader = self._frame_reader
        if reader is not None and reader.isRunning():
            reader.stop()
            reader.wait(1500)
        client = self._client
        if client is not None:
            try:
                client.set_streaming(False)
                client.stop_script()
            except Exception as exc:
                log_openmv_event(f"Failed to stop OpenMV camera script during disconnect: {exc}")
        self._frame_reader = None
        self._client = None
        serial_port = self._serial_port
        if serial_port is not None:
            serial_port.close()
            self._serial_port = None

    def _decode_packet(self, packet: bytes, detections: list[Detection]) -> None:
        if not self._running:
            return
        try:
            hardware_frame = parse_frame_packet(packet)
            image = decode_hardware_frame(hardware_frame)
        except (ValueError, cv2.error) as exc:
            self.frame_error.emit(str(exc))
            return
        self.width = hardware_frame.width
        self.height = hardware_frame.height
        self._frame_count += 1
        if self._frame_count == 1:
            self.diagnostic.emit(f"First TVF1 frame decoded: {self.width}x{self.height}.")
        self.frame_ready.emit(image, detections)
