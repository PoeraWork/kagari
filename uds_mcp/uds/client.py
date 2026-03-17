from __future__ import annotations

import asyncio
from dataclasses import dataclass
from threading import Event, Lock, Thread
from typing import TYPE_CHECKING, Literal

from uds.addressing import AddressingType
from uds.can import CanAddressingFormat, CanAddressingInformation, CanVersion, PyCanTransportInterface
from uds.client import Client
from uds.message import UdsMessage, UdsMessageRecord

from uds_mcp.models.events import EventKind, LogEvent

if TYPE_CHECKING:
    from uds_mcp.can.interface import CanInterface
    from uds_mcp.logging.store import EventStore


@dataclass(slots=True)
class UdsConfig:
    tx_id: int
    rx_id: int
    tx_functional_id: int
    rx_functional_id: int
    can_fd: bool = False
    use_data_optimization: bool = False
    min_dlc: int = 8
    tester_present_interval_sec: float = 2.0


class UdsClientService:
    """UDS client service built on full py-uds transport/session stack."""

    def __init__(
        self,
        can_interface: CanInterface,
        config: UdsConfig,
        event_store: EventStore,
    ) -> None:
        self._can = can_interface
        self._config = config
        self._event_store = event_store
        self._client_lock = Lock()

        bus = self._can.get_bus()
        addressing_information = CanAddressingInformation(
            addressing_format=CanAddressingFormat.NORMAL_ADDRESSING,
            rx_physical_params={"can_id": self._config.rx_id},
            tx_physical_params={"can_id": self._config.tx_id},
            rx_functional_params={"can_id": self._config.rx_functional_id},
            tx_functional_params={"can_id": self._config.tx_functional_id},
        )
        transport_kwargs: dict[str, object] = {
            "can_version": CanVersion.CAN_FD if self._config.can_fd else CanVersion.CLASSIC_CAN,
            "use_data_optimization": self._config.use_data_optimization,
            "min_dlc": self._config.min_dlc,
        }
        self._transport = PyCanTransportInterface(
            network_manager=bus,
            addressing_information=addressing_information,
            **transport_kwargs,
        )
        self._client = Client(transport_interface=self._transport)
        self._tester_present_owners: set[str] = set()
        self._tester_present_mode: AddressingType | None = None
        self._tester_present_stop_event: Event | None = None
        self._tester_present_thread: Thread | None = None

    async def send(
        self,
        request_hex: str,
        timeout_ms: int = 1000,
        *,
        addressing_mode: Literal["physical", "functional"] = "physical",
    ) -> dict[str, object]:
        payload = bytes.fromhex(request_hex)
        return await asyncio.to_thread(self._send_sync, payload, timeout_ms, addressing_mode)

    async def ensure_tester_present(self) -> None:
        await self.start_tester_present_owner("flow-breakpoint", addressing_mode="physical")

    async def stop_tester_present(self) -> None:
        await self.stop_tester_present_owner("flow-breakpoint")

    async def start_tester_present_owner(
        self,
        owner: str,
        *,
        addressing_mode: Literal["physical", "functional"] = "physical",
    ) -> dict[str, object]:
        return await asyncio.to_thread(self._acquire_tester_present_sync, owner, addressing_mode)

    async def stop_tester_present_owner(self, owner: str) -> dict[str, object]:
        return await asyncio.to_thread(self._release_tester_present_sync, owner)

    async def start_manual_tester_present(
        self,
        *,
        addressing_mode: Literal["physical", "functional"] = "physical",
    ) -> dict[str, object]:
        return await self.start_tester_present_owner("manual", addressing_mode=addressing_mode)

    async def stop_manual_tester_present(self) -> dict[str, object]:
        return await self.stop_tester_present_owner("manual")

    async def tester_present_status(self) -> dict[str, object]:
        return await asyncio.to_thread(self._tester_present_status_sync)

    def close(self) -> None:
        thread_to_join: Thread | None = None
        with self._client_lock:
            thread_to_join = self._stop_tester_present_worker_locked()
            self._tester_present_owners.clear()

            notifier = getattr(self._transport, "notifier", None)
            if notifier is not None and hasattr(notifier, "stop"):
                notifier.stop()

        if thread_to_join is not None:
            thread_to_join.join(timeout=1.0)

    def _send_sync(
        self, payload: bytes, timeout_ms: int, addressing_mode: str
    ) -> dict[str, object]:
        addressing_type = _parse_addressing_mode(addressing_mode)
        with self._client_lock:
            request = UdsMessage(payload=payload, addressing_type=addressing_type)
            previous_timeouts = (
                self._client.p2_client_timeout,
                self._client.p2_ext_client_timeout,
                self._client.p6_client_timeout,
                self._client.p6_ext_client_timeout,
            )
            self._client.p2_client_timeout = timeout_ms
            self._client.p2_ext_client_timeout = timeout_ms
            self._client.p6_client_timeout = timeout_ms
            self._client.p6_ext_client_timeout = timeout_ms

            try:
                request_record, response_records = self._client.send_request_receive_responses(
                    request
                )
            finally:
                (
                    self._client.p2_client_timeout,
                    self._client.p2_ext_client_timeout,
                    self._client.p6_client_timeout,
                    self._client.p6_ext_client_timeout,
                ) = previous_timeouts

        if not response_records:
            raise TimeoutError(f"UDS response timeout after {timeout_ms}ms")

        final_response = response_records[-1]
        self._log_uds_exchange(request_record, response_records)

        response_hex = final_response.payload.hex().upper()
        response_id = int(final_response.packets_records[-1].can_id)
        return {
            "request_hex": payload.hex().upper(),
            "response_hex": response_hex,
            "response_id": response_id,
            "addressing_mode": addressing_mode,
        }

    def _log_uds_exchange(
        self,
        request_record: UdsMessageRecord,
        response_records: tuple[UdsMessageRecord, ...],
    ) -> None:
        self._event_store.append(
            LogEvent(
                kind=EventKind.UDS_TX,
                payload={
                    "request_hex": request_record.payload.hex().upper(),
                    "tx_id": int(request_record.packets_records[-1].can_id),
                },
            )
        )
        for packet in request_record.packets_records:
            self._event_store.append(
                LogEvent(
                    kind=EventKind.CAN_TX,
                    payload={
                        "channel": str(packet.frame.channel),
                        "arbitration_id": int(packet.can_id),
                        "is_extended_id": bool(packet.frame.is_extended_id),
                        "data_hex": packet.raw_frame_data.hex().upper(),
                    },
                )
            )

        for response in response_records:
            self._event_store.append(
                LogEvent(
                    kind=EventKind.UDS_RX,
                    payload={
                        "response_hex": response.payload.hex().upper(),
                        "rx_id": int(response.packets_records[-1].can_id),
                    },
                )
            )
            for packet in response.packets_records:
                self._event_store.append(
                    LogEvent(
                        kind=EventKind.CAN_RX,
                        payload={
                            "channel": str(packet.frame.channel),
                            "arbitration_id": int(packet.can_id),
                            "is_extended_id": bool(packet.frame.is_extended_id),
                            "data_hex": packet.raw_frame_data.hex().upper(),
                        },
                    )
                )

    def _acquire_tester_present_sync(self, owner: str, addressing_mode: str) -> dict[str, object]:
        requested = _parse_addressing_mode(addressing_mode)
        with self._client_lock:
            self._tester_present_owners.add(owner)

            if self._tester_present_thread is not None:
                active_mode = self._tester_present_mode or AddressingType.PHYSICAL
                return self._tester_present_status_payload(active_mode)

            self._start_tester_present_worker_locked(requested)
            return self._tester_present_status_payload(requested)

    def _release_tester_present_sync(self, owner: str) -> dict[str, object]:
        with self._client_lock:
            self._tester_present_owners.discard(owner)

            if self._tester_present_owners:
                active_mode = self._tester_present_mode or AddressingType.PHYSICAL
                return self._tester_present_status_payload(active_mode)

            thread_to_join = self._stop_tester_present_worker_locked()

        if thread_to_join is not None:
            thread_to_join.join(timeout=1.0)

        with self._client_lock:
            return self._tester_present_status_payload(None)

    def _tester_present_status_sync(self) -> dict[str, object]:
        with self._client_lock:
            mode = self._tester_present_mode if self._tester_present_thread is not None else None
            return self._tester_present_status_payload(mode)

    def _tester_present_status_payload(self, mode: AddressingType | None) -> dict[str, object]:
        mode_label = None
        if mode is not None:
            mode_label = "functional" if mode == AddressingType.FUNCTIONAL else "physical"
        return {
            "running": self._tester_present_thread is not None,
            "addressing_mode": mode_label,
            "owners": sorted(self._tester_present_owners),
            "interval_sec": self._config.tester_present_interval_sec,
        }

    def _start_tester_present_worker_locked(self, mode: AddressingType) -> None:
        interval_sec = max(self._config.tester_present_interval_sec, 0.05)
        stop_event = Event()
        thread = Thread(
            target=self._tester_present_loop,
            args=(stop_event, mode, interval_sec),
            name="uds-mcp-tester-present",
            daemon=True,
        )
        self._tester_present_mode = mode
        self._tester_present_stop_event = stop_event
        self._tester_present_thread = thread
        thread.start()

    def _stop_tester_present_worker_locked(self) -> Thread | None:
        thread = self._tester_present_thread
        if thread is None:
            self._tester_present_mode = None
            self._tester_present_stop_event = None
            return None
        stop_event = self._tester_present_stop_event
        self._tester_present_thread = None
        self._tester_present_mode = None
        self._tester_present_stop_event = None
        if stop_event is not None:
            stop_event.set()
        return thread

    def _tester_present_loop(
        self,
        stop_event: Event,
        addressing_type: AddressingType,
        interval_sec: float,
    ) -> None:
        arbitration_id = (
            self._config.tx_functional_id
            if addressing_type == AddressingType.FUNCTIONAL
            else self._config.tx_id
        )

        while not stop_event.wait(interval_sec):
            try:
                # 0x80 suppresses positive response, so this should be fire-and-forget.
                self._can.send_frame(arbitration_id, b"\x3E\x80")
            except Exception as exc:
                self._event_store.append(
                    LogEvent(
                        kind=EventKind.ERROR,
                        payload={
                            "source": "uds_client",
                            "error": f"tester_present send failed: {exc}",
                        },
                    )
                )


def _parse_addressing_mode(value: str) -> AddressingType:
    normalized = value.strip().lower()
    if normalized == "physical":
        return AddressingType.PHYSICAL
    if normalized == "functional":
        return AddressingType.FUNCTIONAL
    raise ValueError("addressing_mode must be 'physical' or 'functional'")
