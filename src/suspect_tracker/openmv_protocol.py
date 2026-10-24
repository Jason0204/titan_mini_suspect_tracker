from __future__ import annotations

import struct
import threading
import time


SYNC_WORD = 0xD5AA
HEADER = struct.Struct("<HBBBBHH")
HEADER_WITHOUT_CRC = struct.Struct("<HBBBBH")
HEADER_SIZE = HEADER.size
MAX_INITIAL_PAYLOAD = 52
DEFAULT_MAX_PAYLOAD = 4096

FLAG_ACK = 1 << 0
FLAG_NAK = 1 << 1
FLAG_RTX = 1 << 2
FLAG_ACK_REQ = 1 << 3
FLAG_FRAGMENT = 1 << 4
FLAG_EVENT = 1 << 5

PROTO_SYNC = 0x00
PROTO_GET_CAPS = 0x01
PROTO_SET_CAPS = 0x02
CHANNEL_LIST = 0x20
CHANNEL_LOCK = 0x22
CHANNEL_UNLOCK = 0x23
CHANNEL_SIZE = 0x25
CHANNEL_READ = 0x26
CHANNEL_WRITE = 0x27
CHANNEL_IOCTL = 0x28

IOCTL_STREAM_CTRL = 0x00
IOCTL_STDIN_STOP = 0x01
IOCTL_STDIN_EXEC = 0x02
IOCTL_STDIN_RESET = 0x03
IOCTL_STREAM_RAW_CTRL = 0x01

PIXFORMAT_GRAYSCALE = 0x08020001
PIXFORMAT_RGB565 = 0x0C030002
PIXFORMAT_JPEG = 0x06060000


def _build_msb_table(poly: int, width: int) -> list[int]:
    mask = (1 << width) - 1
    top_bit = 1 << (width - 1)
    table = []
    for index in range(256):
        crc = index << (width - 8)
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & mask if crc & top_bit else (crc << 1) & mask
        table.append(crc)
    return table


_CRC16_TABLE = _build_msb_table(0xF94F, 16)
_CRC32_TABLE = _build_msb_table(0xFA567D89, 32)


def crc16(data: bytes | bytearray | memoryview, initial: int = 0xFFFF) -> int:
    # Changed 2026-10-12: Table-driven implementation (was bit-by-bit). Produces identical
    # results but is an order of magnitude faster, which matters because every stream frame
    # payload is CRC-checked on the hot frame-reader path.
    crc = initial
    for value in data:
        crc = ((crc << 8) & 0xFFFF) ^ _CRC16_TABLE[((crc >> 8) ^ value) & 0xFF]
    return crc


def crc32(data: bytes | bytearray | memoryview, initial: int = 0xFFFFFFFF) -> int:
    # Changed 2026-10-12: Table-driven implementation (was bit-by-bit). Measured on-device
    # traffic: a 17KB JPEG frame took ~26ms to verify per byte-loop, capping the pipeline at
    # ~16 FPS even though the camera delivers ~33 FPS. The table version removes that limit.
    crc = initial
    for value in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC32_TABLE[((crc >> 24) ^ value) & 0xFF]
    return crc


class OpenMVProtocolError(RuntimeError):
    pass


class OpenMVSequenceError(OpenMVProtocolError):
    # Changed 2026-10-12: Distinguish a recoverable sequence desync from a hard protocol fault.
    pass


class OpenMVProtocolClient:
    def __init__(
        self,
        serial_port,
        timeout: float = 2.0,
        max_payload: int = DEFAULT_MAX_PAYLOAD,
        trace=None,
    ) -> None:
        self.serial = serial_port
        self.timeout = timeout
        self.max_payload = min(max_payload, DEFAULT_MAX_PAYLOAD)
        self.sequence = 0
        self._rx_buffer = bytearray()
        self.channels: dict[str, int] = {}
        self._io_lock = threading.RLock()
        self.trace = trace or (lambda _message: None)
        self._trace_enabled = True
        # Changed 2026-10-12: Sequence checks start strict and can be relaxed for a device
        # that answers with a fixed sequence number (see _command).
        self._strict_sequence = True

    def _flush_input(self) -> None:
        # Changed 2026-10-12: Clear the driver FIFO and the partial-packet parser buffer together.
        try:
            self.serial.reset_input_buffer()
        except Exception as exc:
            self.trace(f"OpenMV could not flush the serial input buffer: {exc}")
        self._rx_buffer.clear()

    def connect(self) -> dict[str, int]:
        self._flush_input()
        self.max_payload = MAX_INITIAL_PAYLOAD
        sync_error: Exception | None = None
        for attempt in range(1, 4):
            self.sequence = 0
            # Changed 2026-10-12: Never let bytes from an earlier attempt desync this handshake.
            self._flush_input()
            self.trace(f"OpenMV PROTO_SYNC attempt {attempt}/3")
            try:
                self._command(PROTO_SYNC, resync_budget=0)
                sync_error = None
                self.trace(f"OpenMV PROTO_SYNC succeeded on attempt {attempt}")
                break
            except (TimeoutError, OpenMVProtocolError) as exc:
                sync_error = exc
                self.trace(f"OpenMV PROTO_SYNC attempt {attempt} failed: {exc}")
        if sync_error is not None:
            raise TimeoutError(f"OpenMV PROTO_SYNC failed after 3 attempts: {sync_error}") from sync_error
        self.sequence = 0

        self.trace("OpenMV protocol stage: get capabilities")
        capabilities = self._command(PROTO_GET_CAPS)
        if len(capabilities) < 6:
            raise OpenMVProtocolError(f"Invalid PROTO_GET_CAPS response: {capabilities!r}")
        _, device_max_payload = struct.unpack_from("<IH", capabilities)
        self.max_payload = min(DEFAULT_MAX_PAYLOAD, device_max_payload or DEFAULT_MAX_PAYLOAD)
        self.max_payload = min(max(self.max_payload, 64), DEFAULT_MAX_PAYLOAD)

        requested_flags = 0x0F
        self.trace(f"OpenMV protocol stage: set capabilities, max_payload={self.max_payload}")
        self._command(PROTO_SET_CAPS, data=struct.pack("<IH10x", requested_flags, self.max_payload))
        self.trace("OpenMV protocol stage: list channels")
        self.channels = self._read_channels()
        for required_channel in ("stdin", "stream"):
            if required_channel not in self.channels:
                raise OpenMVProtocolError(
                    f"OpenMV protocol connected, but the firmware did not register the {required_channel!r} channel"
                )
        # Changed 2026-10-12: Keep tracing through script upload and channel polling; it is
        # switched off in set_streaming() so the 15-30 FPS frame reader cannot flood the log.
        return self.channels

    def close(self) -> None:
        if self.serial and self.serial.is_open:
            self.serial.close()

    def stop_script(self) -> None:
        self._channel_ioctl(self.channels["stdin"], IOCTL_STDIN_STOP)

    def stop_script_and_wait(self, timeout: float = 5.0) -> float | None:
        # Changed 2026-10-18g: `stop_script()` only *schedules* a KeyboardInterrupt. A script
        # sitting in a slow `finally` -- the offline guard closing an MJPEG segment on the SD
        # card is the real case -- keeps the interpreter busy for another second or more.
        # Uploading inside that window makes the EXEC ioctl schedule a *second* interrupt,
        # which lands in the finally and aborts it halfway (measured: the marker the finally
        # writes last never appeared). The next page then inherits a camera that was never
        # shut down, and the panel shows the old sensor pipeline fighting the new one: visible
        # flicker. The friendly REPL prints ">>> " only once the script has fully unwound, so
        # that prompt is the hand-over signal. Returns the settled time, or None on timeout.
        tracing = self._trace_enabled
        # The polling below is dozens of tiny channel reads; keep them out of hardware.log.
        self._trace_enabled = False
        try:
            self._drain_stdout(0.2)
            started = time.monotonic()
            try:
                self.stop_script()
            except (OpenMVProtocolError, TimeoutError) as exc:
                self.trace(f"OpenMV stop_script failed: {exc}")
                return None
            buffer = bytearray()
            while time.monotonic() - started < timeout:
                try:
                    chunk = self.read_channel("stdout")
                except (OpenMVProtocolError, TimeoutError):
                    chunk = b""
                if chunk:
                    buffer += chunk
                    if b">>>" in buffer:
                        elapsed = time.monotonic() - started
                        self.trace(f"OpenMV previous script unwound in {elapsed:.2f}s")
                        return elapsed
                else:
                    time.sleep(0.01)
            self.trace(f"OpenMV previous script did not return to the REPL within {timeout:.1f}s")
            return None
        finally:
            self._trace_enabled = tracing

    def _drain_stdout(self, seconds: float) -> None:
        # Drop whatever the previous page already printed, so the ">>> " that
        # stop_script_and_wait() looks for can only be the one the interrupt produces.
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                if not self.read_channel("stdout"):
                    time.sleep(0.01)
            except (OpenMVProtocolError, TimeoutError):
                return

    def run_script(self, script: str) -> None:
        stdin_channel = self.channels["stdin"]
        self._channel_ioctl(stdin_channel, IOCTL_STDIN_RESET)
        script_data = script.encode("utf-8")
        chunk_size = max(1, self.max_payload - 8)
        for offset in range(0, len(script_data), chunk_size):
            chunk = script_data[offset : offset + chunk_size]
            self._command(
                CHANNEL_WRITE,
                stdin_channel,
                struct.pack("<II", offset, len(chunk)) + chunk,
            )
        self._channel_ioctl(stdin_channel, IOCTL_STDIN_EXEC)
        # Changed 2026-10-12: Executing a script restarts the device protocol state, so the
        # device counter can drop back to 0 while the host is still counting upwards.
        self.trace("OpenMV script executed; device protocol counters may have been reset")

    def run_script_recovering(self, script: str) -> bool:
        # Changed 2026-10-18f: stopping a running page (or letting it finish on its own) can
        # leave the device's stdin handling in a state where every write is answered with
        # NAK 0x06 (SEQUENCE), no matter how often we re-sync -- the board simply refuses the
        # upload. A full re-handshake from scratch does clear it, and that is what connect()
        # does. Callers that upload a page mid-session (language switch, watchdog re-upload,
        # offline-guard handover) use this instead of run_script so one wedged attempt cannot
        # silently leave the panel dead. Returns True when the fallback was needed.
        try:
            self.run_script(script)
            return False
        except OpenMVProtocolError as exc:
            self.trace(
                f"OpenMV script upload refused ({type(exc).__name__}: {exc}); "
                "re-handshaking with the device and retrying"
            )
            self._strict_sequence = True
            self.connect()
            self.run_script(script)
            return True

    def set_streaming(self, enabled: bool) -> None:
        stream_channel = self.channels["stream"]
        self._channel_ioctl(stream_channel, IOCTL_STREAM_RAW_CTRL, struct.pack("<I", 0))
        self._channel_ioctl(stream_channel, IOCTL_STREAM_CTRL, struct.pack("<I", int(enabled)))
        # Changed 2026-10-12: Only silence tracing once the high-rate frame reader takes over.
        self._trace_enabled = not enabled

    def refresh_channels(self) -> dict[str, int]:
        self.channels = self._read_channels()
        return self.channels

    def wait_for_channel(self, name: str, timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        polls = 0
        while time.monotonic() < deadline:
            if name in self.channels:
                return
            self.refresh_channels()
            polls += 1
            if name not in self.channels:
                # Changed 2026-10-12: Record what the device does expose while we wait.
                if polls == 1 or polls % 20 == 0:
                    self.trace(
                        f"OpenMV waiting for channel {name!r}; visible channels={sorted(self.channels)}"
                    )
                time.sleep(0.05)
        raise TimeoutError(
            f"OpenMV script did not register the {name!r} channel; "
            f"visible channels={sorted(self.channels)}"
        )

    def write_channel(self, name: str, data: bytes, offset: int = 0) -> None:
        channel = self.channels.get(name)
        if channel is None:
            raise OpenMVProtocolError(f"OpenMV channel is not registered: {name}")
        chunk_size = max(1, self.max_payload - 8)
        for start in range(0, len(data), chunk_size):
            chunk = data[start : start + chunk_size]
            self._command(
                CHANNEL_WRITE,
                channel,
                struct.pack("<II", offset + start, len(chunk)) + chunk,
            )

    def read_channel(self, name: str) -> bytes:
        channel = self.channels.get(name)
        if channel is None:
            raise OpenMVProtocolError(f"OpenMV channel is not registered: {name}")
        size_response = self._command(CHANNEL_SIZE, channel)
        if not isinstance(size_response, bytes) or len(size_response) < 4:
            return b""
        size = struct.unpack_from("<I", size_response)[0]
        if size <= 0:
            return b""
        result = self._command(CHANNEL_READ, channel, struct.pack("<II", 0, size))
        return result if isinstance(result, bytes) else b""

    def read_frame_packet(self) -> tuple[int, int, int, int, bytes] | None:
        with self._io_lock:
            stream_channel = self.channels["stream"]
            if not self._command(CHANNEL_LOCK, stream_channel):
                return None
            try:
                size_response = self._command(CHANNEL_SIZE, stream_channel)
                if len(size_response) < 4:
                    raise OpenMVProtocolError(f"Invalid stream-size response: {size_response!r}")
                stream_size = struct.unpack_from("<I", size_response)[0]
                if stream_size <= 20:
                    return None
                stream_data = self._command(CHANNEL_READ, stream_channel, struct.pack("<II", 0, stream_size))
                if len(stream_data) < 20:
                    raise OpenMVProtocolError(f"Incomplete stream frame header: {len(stream_data)} bytes")
                width, height, pixel_format, depth, data_offset = struct.unpack_from("<IIIII", stream_data)
                if data_offset < 20 or data_offset > len(stream_data):
                    raise OpenMVProtocolError(f"Invalid stream frame data offset: {data_offset}")
                return width, height, pixel_format, depth, stream_data[data_offset:]
            finally:
                self._channel_command(CHANNEL_UNLOCK, stream_channel)

    def _read_channels(self) -> dict[str, int]:
        payload = self._command(CHANNEL_LIST)
        channels: dict[str, int] = {}
        for offset in range(0, len(payload) - 15, 16):
            channel_id, _flags, raw_name = struct.unpack_from("<BB14s", payload, offset)
            name = raw_name.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
            if name:
                channels[name] = channel_id
        return channels

    def _channel_ioctl(self, channel: int, ioctl: int, args: bytes = b"") -> bytes:
        return self._command(CHANNEL_IOCTL, channel, struct.pack("<I", ioctl) + args)

    def _resync(self, reason: str) -> None:
        # Changed 2026-10-12: A sequence desync is recoverable. PROTO_SYNC resets the sequence
        # counter on both sides, so drop whatever is in flight and re-sync instead of aborting.
        self.trace(f"OpenMV resync requested: {reason}")
        self._flush_input()
        self.sequence = 0
        self._command(PROTO_SYNC, resync_budget=0)
        # Changed 2026-10-18f: The device answers PROTO_SYNC with sequence 0 and then expects
        # the host's next request to carry sequence 0 again; _receive_response already bumped
        # our counter to 1, so every retried command was rejected with NAK 0x06 (SEQUENCE)
        # forever. connect() compensates for exactly this after its handshake -- do the same
        # here, otherwise a resync can never restore alignment. Symptom on hardware: the
        # offline guard upload after "disconnect"/window close failed in 3 out of 4 runs.
        self.sequence = 0
        self.trace("OpenMV resync complete; host sequence counter reset to 0")

    def _command(
        self,
        opcode: int,
        channel: int = 0,
        data: bytes = b"",
        resync_budget: int = 1,
        allow_relax: bool = True,
    ) -> bytes | bool:
        with self._io_lock:
            if not self._strict_sequence:
                # Changed 2026-10-12: Once sequence checks are relaxed, drop leftovers first so
                # the next matching packet can only belong to the command we are about to send.
                self._flush_input()
            sequence = self.sequence
            if self._trace_enabled:
                self.trace(f"OpenMV TX opcode=0x{opcode:02X} channel={channel} seq={sequence} len={len(data)}")
            self._send_packet(opcode, channel, 0, data)
            try:
                response = self._receive_response(opcode, channel)
            except OpenMVSequenceError as exc:
                # Changed 2026-10-12: The device restarts its protocol counters when a script
                # starts or aborts, so a stale counter must not kill the session.
                if resync_budget > 0:
                    self._resync(f"opcode=0x{opcode:02X} channel={channel}: {exc}")
                    return self._command(
                        opcode, channel, data, resync_budget - 1, allow_relax
                    )
                if allow_relax:
                    # Changed 2026-10-12: PROTO_SYNC did not restore alignment. Some firmware keeps
                    # answering with an off-by-N sequence; stop enforcing the counter. CRC16/CRC32
                    # and opcode/channel matching still guard the link. allow_relax flips to False
                    # here so this branch can never be re-entered.
                    self._strict_sequence = False
                    self.trace(
                        f"OpenMV resync did not restore alignment ({exc}); "
                        "relaxing sequence checks for this session"
                    )
                    return self._command(opcode, channel, data, resync_budget=1, allow_relax=False)
                raise
            except TimeoutError as exc:
                available = self.serial.in_waiting
                self.trace(
                    f"OpenMV RX timeout opcode=0x{opcode:02X} channel={channel} seq={sequence}; "
                    f"serial_waiting={available}; parser_buffer_tail={bytes(self._rx_buffer[-32:]).hex()}; {exc}"
                )
                raise
            if self._trace_enabled:
                self.trace(f"OpenMV RX opcode=0x{opcode:02X} channel={channel} seq={sequence} response={response!r}")
            return response

    def _channel_command(self, opcode: int, channel: int) -> bytes | bool:
        return self._command(opcode, channel)

    def _send_packet(
        self,
        opcode: int,
        channel: int,
        flags: int,
        data: bytes = b"",
        sequence: int | None = None,
    ) -> None:
        if len(data) > self.max_payload:
            raise OpenMVProtocolError(f"OpenMV protocol payload too large: {len(data)} > {self.max_payload}")
        packet_sequence = self.sequence if sequence is None else sequence
        header_prefix = HEADER_WITHOUT_CRC.pack(
            SYNC_WORD, packet_sequence, channel, flags, opcode, len(data)
        )
        packet = header_prefix + struct.pack("<H", crc16(header_prefix))
        if data:
            packet += data + struct.pack("<I", crc32(data))
        written = self.serial.write(packet)
        if written != len(packet):
            raise OpenMVProtocolError(f"Partial OpenMV serial write: {written}/{len(packet)} bytes")

    def _receive_response(self, expected_opcode: int, expected_channel: int) -> bytes | bool:
        fragments = bytearray()
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            packet = self._receive_packet(deadline)
            if packet is None:
                continue

            flags = packet["flags"]
            if flags & FLAG_RTX and packet["sequence"] != self.sequence:
                if flags & FLAG_ACK_REQ:
                    self._send_packet(packet["opcode"], packet["channel"], FLAG_ACK, sequence=packet["sequence"])
                continue
            if flags & FLAG_ACK_REQ:
                self._send_packet(packet["opcode"], packet["channel"], FLAG_ACK, sequence=packet["sequence"])
            if flags & FLAG_EVENT:
                continue
            if packet["opcode"] != expected_opcode or packet["channel"] != expected_channel:
                if self._trace_enabled:
                    self.trace(
                        f"OpenMV ignored unrelated response opcode=0x{packet['opcode']:02X} "
                        f"channel={packet['channel']} seq={packet['sequence']}"
                    )
                continue
            if packet["sequence"] != self.sequence:
                # Changed 2026-10-12: Log the full offending packet; a valid payload with a wrong
                # sequence means the device counter is off, not that the link is broken.
                detail = (
                    f"opcode=0x{packet['opcode']:02X} channel={packet['channel']} "
                    f"flags=0x{packet['flags']:02X} payload={len(packet['payload'])}B "
                    f"head={bytes(packet['payload'])[:32].hex()}"
                )
                if self._strict_sequence:
                    self.trace(
                        f"OpenMV rejecting out-of-order response seq={packet['sequence']} "
                        f"expected={self.sequence} ({detail})"
                    )
                    raise OpenMVSequenceError(
                        f"OpenMV sequence mismatch: expected {self.sequence}, got {packet['sequence']}"
                    )
                self.trace(
                    f"OpenMV accepting out-of-order response seq={packet['sequence']} "
                    f"expected={self.sequence} ({detail})"
                )
            self.sequence = (self.sequence + 1) & 0xFF
            if flags & FLAG_NAK:
                raw_status = bytes(packet["payload"])
                status = struct.unpack_from("<H", raw_status or b"\x09\x00")[0]
                # Changed 2026-10-12: Record the raw NAK payload; status codes need to be told
                # apart from a genuinely broken link when diagnosing a device.
                self.trace(
                    f"OpenMV NAK opcode=0x{packet['opcode']:02X} channel={packet['channel']} "
                    f"seq={packet['sequence']} status=0x{status:02X} raw={raw_status[:16].hex()}"
                )
                if status == 4:
                    return False
                if status == 6:
                    # Changed 2026-10-12: Status 0x06 is SEQUENCE. The device is telling us the
                    # counters drifted (typical right after a script starts or aborts), so treat
                    # it as a recoverable desync and let _command re-sync and replay.
                    raise OpenMVSequenceError(
                        "OpenMV device rejected the command sequence (status 0x06)"
                    )
                raise OpenMVProtocolError(f"OpenMV command rejected with status 0x{status:02X}")
            fragments.extend(packet["payload"])
            if flags & FLAG_FRAGMENT:
                deadline = time.monotonic() + self.timeout
                continue
            return bytes(fragments) if fragments else True
        raise TimeoutError("OpenMV protocol response timed out")

    def _receive_packet(self, deadline: float) -> dict[str, int | bytes] | None:
        while time.monotonic() < deadline:
            # Changed 2026-10-12: Only sleep when no bytes arrived this iteration. During a
            # multi-fragment stream read the device sends packets back-to-back; sleeping 1ms
            # per iteration added ~5-10ms per 17KB frame and capped the pipeline below the
            # camera's native rate.
            waiting = self.serial.in_waiting
            if waiting:
                self._rx_buffer.extend(self.serial.read(waiting))

            sync_offset = self._rx_buffer.find(struct.pack("<H", SYNC_WORD))
            if sync_offset < 0:
                self._rx_buffer[:] = self._rx_buffer[-1:]
                if not waiting:
                    time.sleep(0.001)
                continue
            if sync_offset:
                del self._rx_buffer[:sync_offset]
            if len(self._rx_buffer) < HEADER_SIZE:
                if not waiting:
                    time.sleep(0.001)
                continue

            sync, sequence, channel, flags, opcode, length, header_crc = HEADER.unpack_from(self._rx_buffer)
            if length > self.max_payload:
                del self._rx_buffer[0]
                continue
            if crc16(memoryview(self._rx_buffer)[:8]) != header_crc:
                del self._rx_buffer[0]
                continue

            packet_size = HEADER_SIZE + length + (4 if length else 0)
            if len(self._rx_buffer) < packet_size:
                if not waiting:
                    time.sleep(0.001)
                continue
            payload = bytes(self._rx_buffer[HEADER_SIZE : HEADER_SIZE + length])
            if length:
                received_crc = struct.unpack_from("<I", self._rx_buffer, HEADER_SIZE + length)[0]
                if crc32(payload) != received_crc:
                    del self._rx_buffer[0]
                    continue
            del self._rx_buffer[:packet_size]
            return {
                "sequence": sequence,
                "channel": channel,
                "flags": flags,
                "opcode": opcode,
                "payload": payload,
            }
        return None