from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from threading import Condition, Lock, Thread
from typing import TYPE_CHECKING, Literal

from uds.addressing import AddressingType
from uds.can import (
    CanAddressingFormat,
    CanAddressingInformation,
    CanVersion,
    PyCanTransportInterface,
)
from uds.can.frame import CanDlcHandler
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
    dlc: int = 8
    min_dlc: int = 8
    tester_present_interval_sec: float = 2.0

    def __post_init__(self) -> None:
        _validate_discrete_dlc_bytes("dlc", self.dlc)

        if self.use_data_optimization:
            _validate_discrete_dlc_bytes("min_dlc", self.min_dlc)
            if self.min_dlc > self.dlc:
                raise ValueError(
                    "min_dlc must be less than or equal to dlc when use_data_optimization=true"
                )

        if not self.can_fd:
            if self.dlc > 8:
                raise ValueError("dlc > 8 requires CAN FD")
            if self.use_data_optimization and self.min_dlc > 8:
                raise ValueError("min_dlc > 8 requires CAN FD")


class _TesterPresentController:
    """Periodic 0x3E TesterPresent sender with owner-counted activation.

    Cadence is anchored to the monotonic time of the last actual frame sent,
    not to owner acquire/release boundaries. A brief owner drop (e.g. a flow
    step with ``tester_present: off`` followed by an ``on`` step) will not
    reset the timer: the next send still fires at ``last_send + interval``.

    The worker thread is lazily created on the first acquire and reused for
    the controller's lifetime, pausing on a Condition when the owner set is
    empty. Only :meth:`shutdown` tears down the thread and clears the cadence
    anchor.
    """

    def __init__(
        self,
        *,
        can: CanInterface,
        event_store: EventStore,
        tx_id_physical: int,
        tx_id_functional: int,
        interval_sec: float,
    ) -> None:
        self._can = can
        self._event_store = event_store
        self._tx_id_physical = tx_id_physical
        self._tx_id_functional = tx_id_functional
        self._interval_sec = max(interval_sec, 0.05)

        self._cond = Condition(Lock())
        self._owners: set[str] = set()
        self._mode: AddressingType | None = None
        self._thread: Thread | None = None
        self._shutdown = False
        self._last_send_monotonic: float | None = None

    # ---- public API (invoked via asyncio.to_thread) ----

    def acquire(self, owner: str, addressing_mode: str) -> dict[str, object]:
        requested = _parse_addressing_mode(addressing_mode)
        with self._cond:
            was_idle = not self._owners
            self._owners.add(owner)
            if was_idle:
                # Reactivating from idle — the first reacquiring owner sets
                # the addressing mode for this active period.
                self._mode = requested
                self._ensure_worker_locked()
            self._cond.notify_all()
            return self._status_payload_locked()

    def release(self, owner: str) -> dict[str, object]:
        with self._cond:
            self._owners.discard(owner)
            if not self._owners:
                self._mode = None
            self._cond.notify_all()
            return self._status_payload_locked()

    def status(self) -> dict[str, object]:
        with self._cond:
            return self._status_payload_locked()

    def shutdown(self) -> None:
        """Tear down the worker and reset the global cadence anchor."""
        with self._cond:
            self._shutdown = True
            self._owners.clear()
            self._mode = None
            self._last_send_monotonic = None
            thread = self._thread
            self._thread = None
            self._cond.notify_all()
        if thread is not None:
            thread.join(timeout=1.0)

    # ---- internals ----

    def _status_payload_locked(self) -> dict[str, object]:
        mode_label: str | None = None
        if self._mode is not None:
            mode_label = (
                "functional" if self._mode == AddressingType.FUNCTIONAL else "physical"
            )
        return {
            "running": bool(self._owners),
            "addressing_mode": mode_label,
            "owners": sorted(self._owners),
            "interval_sec": self._interval_sec,
        }

    def _ensure_worker_locked(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._shutdown = False
        thread = Thread(
            target=self._run,
            name="uds-mcp-tester-present",
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def _run(self) -> None:
        while True:
            with self._cond:
                # Pause while idle.
                while not self._shutdown and not self._owners:
                    self._cond.wait()
                if self._shutdown:
                    return

                # Compute an absolute deadline for the next send so that
                # spurious wakeups (owner add/remove) don't reset the wait.
                if self._last_send_monotonic is None:
                    deadline = time.monotonic() + self._interval_sec
                else:
                    deadline = self._last_send_monotonic + self._interval_sec

                while not self._shutdown and self._owners:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0.0:
                        break
                    self._cond.wait(timeout=remaining)

                if self._shutdown:
                    return
                if not self._owners:
                    # Became idle during the wait — loop back and pause.
                    continue

                mode = self._mode or AddressingType.PHYSICAL
                arbitration_id = (
                    self._tx_id_functional
                    if mode == AddressingType.FUNCTIONAL
                    else self._tx_id_physical
                )

            # Send outside the lock.
            try:
                self._can.send_frame(arbitration_id, b"\x3e\x80")
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

            # Advance the cadence anchor even on failure to avoid a hot-loop.
            with self._cond:
                self._last_send_monotonic = time.monotonic()


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
            "dlc": _encode_dlc_from_bytes(self._config.dlc, field_name="dlc"),
        }
        if self._config.use_data_optimization:
            transport_kwargs["min_dlc"] = _encode_dlc_from_bytes(
                self._config.min_dlc,
                field_name="min_dlc",
            )
        self._transport = PyCanTransportInterface(
            network_manager=bus,
            addressing_information=addressing_information,
            **transport_kwargs,
        )
        self._client = Client(transport_interface=self._transport)
        self._tester_present = _TesterPresentController(
            can=self._can,
            event_store=self._event_store,
            tx_id_physical=self._config.tx_id,
            tx_id_functional=self._config.tx_functional_id,
            interval_sec=self._config.tester_present_interval_sec,
        )

    async def send(
        self,
        request_hex: str,
        timeout_ms: int = 1000,
        *,
        addressing_mode: Literal["physical", "functional"] = "physical",
    ) -> dict[str, object]:
        payload = bytes.fromhex(request_hex)
        return await asyncio.to_thread(self._send_sync, payload, timeout_ms, addressing_mode)

    async def send_no_response(
        self,
        request_hex: str,
        *,
        addressing_mode: Literal["physical", "functional"] = "physical",
    ) -> dict[str, object]:
        payload = bytes.fromhex(request_hex)
        return await asyncio.to_thread(self._send_no_response_sync, payload, addressing_mode)

    async def send_can_frames(self, frames: list[dict[str, object]]) -> dict[str, object]:
        return await asyncio.to_thread(self._send_can_frames_sync, frames)

    async def start_tester_present_owner(
        self,
        owner: str,
        *,
        addressing_mode: Literal["physical", "functional"] = "physical",
    ) -> dict[str, object]:
        return await asyncio.to_thread(self._tester_present.acquire, owner, addressing_mode)

    async def stop_tester_present_owner(self, owner: str) -> dict[str, object]:
        return await asyncio.to_thread(self._tester_present.release, owner)

    async def start_manual_tester_present(
        self,
        *,
        addressing_mode: Literal["physical", "functional"] = "physical",
    ) -> dict[str, object]:
        return await self.start_tester_present_owner("manual", addressing_mode=addressing_mode)

    async def stop_manual_tester_present(self) -> dict[str, object]:
        return await self.stop_tester_present_owner("manual")

    async def tester_present_status(self) -> dict[str, object]:
        return await asyncio.to_thread(self._tester_present.status)

    def close(self) -> None:
        self._tester_present.shutdown()
        with self._client_lock:
            notifier = getattr(self._transport, "notifier", None)
            if notifier is not None and hasattr(notifier, "stop"):
                notifier.stop()

    def _send_sync(
        self, payload: bytes, timeout_ms: int, addressing_mode: str
    ) -> dict[str, object]:
        addressing_type = _parse_addressing_mode(addressing_mode)
        with self._client_lock:
            request = UdsMessage(payload=payload, addressing_type=addressing_type)
            previous_timeouts = (
                self._client.p2_client_timeout,
                self._client.p2_ext_client_timeout,
                self._client.p3_client_physical,
                self._client.p3_client_functional,
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
                    self._client.p3_client_physical,
                    self._client.p3_client_functional,
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

    def _send_no_response_sync(self, payload: bytes, addressing_mode: str) -> dict[str, object]:
        addressing_type = _parse_addressing_mode(addressing_mode)
        with self._client_lock:
            request = UdsMessage(payload=payload, addressing_type=addressing_type)
            request_record = self._client._send_request(request)

        self._log_uds_request(request_record)
        return {
            "request_hex": payload.hex().upper(),
            "response_hex": None,
            "response_id": None,
            "addressing_mode": addressing_mode,
        }

    def _send_can_frames_sync(self, frames: list[dict[str, object]]) -> dict[str, object]:
        sent = 0
        with self._client_lock:
            for frame in frames:
                raw_arbitration_id = frame.get("arbitration_id")
                raw_data_hex = frame.get("data_hex")
                raw_is_extended_id = frame.get("is_extended_id", False)

                if not isinstance(raw_arbitration_id, int):
                    raise TypeError("send_can_frames requires int arbitration_id")
                if not isinstance(raw_data_hex, str):
                    raise TypeError("send_can_frames requires str data_hex")
                if not isinstance(raw_is_extended_id, bool):
                    raise TypeError("send_can_frames requires bool is_extended_id")

                arbitration_id = raw_arbitration_id
                data_hex = raw_data_hex
                is_extended_id = raw_is_extended_id
                self._can.send_frame(
                    arbitration_id,
                    bytes.fromhex(data_hex),
                    is_extended_id=is_extended_id,
                )
                sent += 1
        return {"sent": sent}

    def _log_uds_exchange(
        self,
        request_record: UdsMessageRecord,
        response_records: tuple[UdsMessageRecord, ...],
    ) -> None:
        self._log_uds_request(request_record)

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

    def _log_uds_request(self, request_record: UdsMessageRecord) -> None:
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

def _parse_addressing_mode(value: str) -> AddressingType:
    normalized = value.strip().lower()
    if normalized == "physical":
        return AddressingType.PHYSICAL
    if normalized == "functional":
        return AddressingType.FUNCTIONAL
    raise ValueError("addressing_mode must be 'physical' or 'functional'")


def _encode_dlc_from_bytes(value: int, *, field_name: str) -> int:
    try:
        return CanDlcHandler.encode_dlc(value)
    except Exception as exc:
        raise ValueError(
            f"{field_name}={value} is not a valid CAN(-FD) discrete data length in bytes"
        ) from exc


def _validate_discrete_dlc_bytes(field_name: str, value: int) -> None:
    _encode_dlc_from_bytes(value, field_name=field_name)
