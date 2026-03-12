from __future__ import annotations

from pathlib import Path

import can

from uds_mcp.models.events import EventKind, LogEvent


class BlfExporter:
    """Export CAN events to BLF using python-can writer."""

    def export(self, output_path: Path, events: list[LogEvent]) -> int:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with can.BLFWriter(str(output_path)) as writer:
            for event in events:
                if event.kind not in {EventKind.CAN_TX, EventKind.CAN_RX}:
                    continue
                payload = event.payload
                data = bytes.fromhex(str(payload["data_hex"]))
                msg = can.Message(
                    timestamp=event.created_at.timestamp(),
                    arbitration_id=int(payload["arbitration_id"]),
                    is_extended_id=bool(payload.get("is_extended_id", False)),
                    is_rx=event.kind == EventKind.CAN_RX,
                    data=data,
                    channel=payload.get("channel"),
                )
                writer.on_message_received(msg)
                count += 1
        return count
