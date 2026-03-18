from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import can

from uds_mcp.models.events import EventKind, LogEvent

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class BlfExporter:
    """Export CAN events to BLF using python-can writer."""

    def __init__(self) -> None:
        self._streaming_writer: can.BLFWriter | None = None

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

    def start_streaming(self, output_path: Path) -> None:
        """Start streaming mode: create a BLFWriter instance for real-time writing."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self._streaming_writer = can.BLFWriter(str(output_path))

    def on_event(self, event: LogEvent) -> None:
        """EventStore listener callback. Writes CAN_TX/CAN_RX events to BLF."""
        if self._streaming_writer is None:
            return
        if event.kind not in {EventKind.CAN_TX, EventKind.CAN_RX}:
            return
        try:
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
            self._streaming_writer.on_message_received(msg)
        except Exception:
            logger.exception("Failed to write CAN event to BLF stream")

    def stop_streaming(self) -> None:
        """Stop streaming mode: close the BLFWriter."""
        if self._streaming_writer is not None:
            try:
                self._streaming_writer.stop()
            except Exception:
                logger.exception("Failed to close BLF streaming writer")
            self._streaming_writer = None
