from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class EventKind(StrEnum):
    CAN_TX = "can.tx"
    CAN_RX = "can.rx"
    UDS_TX = "uds.tx"
    UDS_RX = "uds.rx"
    FLOW_STEP = "flow.step"
    FLOW_STATE = "flow.state"
    ERROR = "error"


@dataclass(slots=True)
class LogEvent:
    kind: EventKind
    payload: dict[str, Any]
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "kind": self.kind.value,
            "created_at": self.created_at.isoformat(),
            "payload": self.payload,
        }
