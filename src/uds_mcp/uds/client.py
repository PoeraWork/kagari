from __future__ import annotations

import asyncio
from dataclasses import dataclass
from threading import Lock

from uds.addressing import AddressingType
from uds.can import CanAddressingFormat, CanAddressingInformation, PyCanTransportInterface
from uds.client import Client
from uds.message import UdsMessage, UdsMessageRecord

from uds_mcp.can.interface import CanInterface
from uds_mcp.logging.store import EventStore
from uds_mcp.models.events import EventKind, LogEvent


@dataclass(slots=True)
class UdsConfig:
    tx_id: int
    rx_id: int
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
            rx_functional_params={"can_id": self._config.rx_id},
            tx_functional_params={"can_id": self._config.tx_id},
        )
        self._transport = PyCanTransportInterface(
            network_manager=bus,
            addressing_information=addressing_information,
        )
        self._client = Client(transport_interface=self._transport)

    async def send(self, request_hex: str, timeout_ms: int = 1000) -> dict[str, object]:
        payload = bytes.fromhex(request_hex)
        return await asyncio.to_thread(self._send_sync, payload, timeout_ms)

    async def ensure_tester_present(self) -> None:
        await asyncio.to_thread(self._start_tester_present_sync)

    async def stop_tester_present(self) -> None:
        await asyncio.to_thread(self._stop_tester_present_sync)

    def _send_sync(self, payload: bytes, timeout_ms: int) -> dict[str, object]:
        with self._client_lock:
            request = UdsMessage(payload=payload, addressing_type=AddressingType.PHYSICAL)
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

    def _start_tester_present_sync(self) -> None:
        with self._client_lock:
            if self._client.is_tester_present_sent:
                return
            self._client.s3_client = self._config.tester_present_interval_sec * 1000.0
            self._client.start_tester_present(addressing_type=AddressingType.PHYSICAL, sprmib=True)

    def _stop_tester_present_sync(self) -> None:
        with self._client_lock:
            if not self._client.is_tester_present_sent:
                return
            self._client.stop_tester_present()
