from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


FrameKind = Literal["command", "event", "ack", "error"]


@dataclass(frozen=True)
class ProtocolFrame:
    kind: FrameKind
    name: str
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_json_line(self) -> bytes:
        body = {
            "kind": self.kind,
            "name": self.name,
            "payload": self.payload,
            "timestamp": self.timestamp,
        }
        return (json.dumps(body, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")

    @classmethod
    def from_json_line(cls, data: bytes | str) -> "ProtocolFrame":
        text = data.decode("utf-8") if isinstance(data, bytes) else data
        body = json.loads(text)
        return cls(
            kind=body["kind"],
            name=body["name"],
            payload=body.get("payload", {}),
            timestamp=body.get("timestamp") or datetime.now(timezone.utc).isoformat(),
        )


def command(name: str, **payload: Any) -> ProtocolFrame:
    # Changed 2026-09-20: Command payloads may include ROI and recording-mode metadata.
    return ProtocolFrame(kind="command", name=name, payload=payload)
