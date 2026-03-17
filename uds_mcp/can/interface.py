from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING

import can

from uds_mcp.models.events import EventKind, LogEvent

if TYPE_CHECKING:
    from uds_mcp.can.config import CanConfig
    from uds_mcp.logging.store import EventStore


class CanInterface:
    """Thin wrapper around python-can bus APIs with event logging."""

    def __init__(self, config: CanConfig, event_store: EventStore) -> None:
        self._config = config
        self._event_store = event_store
        self._bus: can.BusABC | None = None
        self._lock = Lock()

    def open(self) -> None:
        with self._lock:
            if self._bus is not None:
                return
            bus_kwargs: dict[str, object] = {
                "interface": self._config.interface,
                "channel": self._config.channel,
                "bitrate": self._config.bitrate,
            }
            try:
                self._bus = can.Bus(**bus_kwargs)
            except can.exceptions.CanInitializationError as exc:
                if not self._should_retry_with_active_settings(exc):
                    raise

                retry_kwargs = dict(bus_kwargs)
                retry_kwargs.pop("bitrate", None)
                self._bus = can.Bus(**retry_kwargs)

    @staticmethod
    def _should_retry_with_active_settings(exc: can.exceptions.CanInitializationError) -> bool:
        message = str(exc).lower()
        return (
            "another application might have set incompatible settings" in message
            and "currently active settings" in message
        )

    def get_bus(self) -> can.BusABC:
        """Get an opened python-can bus instance."""
        self.open()
        assert self._bus is not None
        return self._bus

    def close(self) -> None:
        with self._lock:
            if self._bus is None:
                return
            self._bus.shutdown()
            self._bus = None

    def send_frame(self, arbitration_id: int, data: bytes, *, is_extended_id: bool = False) -> None:
        self.open()
        assert self._bus is not None
        message = can.Message(
            arbitration_id=arbitration_id,
            data=data,
            is_extended_id=is_extended_id,
            channel=self._config.channel,
            is_rx=False,
        )
        self._bus.send(message)
        self._event_store.append(
            LogEvent(
                kind=EventKind.CAN_TX,
                payload={
                    "channel": self._config.channel,
                    "arbitration_id": arbitration_id,
                    "is_extended_id": is_extended_id,
                    "data_hex": data.hex().upper(),
                },
            )
        )

    def recv_frame(self, timeout_sec: float) -> can.Message | None:
        self.open()
        assert self._bus is not None
        message = self._bus.recv(timeout=timeout_sec)
        if message is None:
            return None
        self._event_store.append(
            LogEvent(
                kind=EventKind.CAN_RX,
                payload={
                    "channel": str(message.channel),
                    "arbitration_id": int(message.arbitration_id),
                    "is_extended_id": bool(message.is_extended_id),
                    "data_hex": bytes(message.data).hex().upper(),
                },
            )
        )
        return message
