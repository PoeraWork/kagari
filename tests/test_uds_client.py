from __future__ import annotations

import asyncio
from threading import Event, Thread

from uds.addressing import AddressingType

from uds_mcp.logging.store import EventStore
from uds_mcp.models.events import EventKind
from uds_mcp.uds.client import UdsClientService, UdsConfig


class _FakeNotifier:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class _FakeTransport:
    def __init__(
        self,
        network_manager: object,
        addressing_information: object,
        **configuration_params: object,
    ) -> None:
        self.network_manager = network_manager
        self.addressing_information = addressing_information
        self.configuration_params = configuration_params
        self.notifier = _FakeNotifier()


class _FakeClient:
    def __init__(self, transport_interface: object) -> None:
        self.transport_interface = transport_interface
        self.p3_client_physical = 100.0
        self.p3_client_functional = 100.0
        self.s3_client = 2000.0
        self._tp_event = Event()
        self._tp_thread: Thread | None = None
        self._tp_addressing = AddressingType.PHYSICAL

    def send_request_receive_responses(self, request: object) -> tuple[object, tuple[object, ...]]:
        raise RuntimeError("not used in this test")

    def _send_request(self, request: object) -> object:
        payload = getattr(request, "payload")
        addressing_type = getattr(request, "addressing_type", AddressingType.PHYSICAL)
        can_id = 0x70D if addressing_type == AddressingType.PHYSICAL else 0x7DF
        return _FakeUdsMessageRecord(payload, can_id=can_id)

    @property
    def is_tester_present_sent(self) -> bool:
        return self._tp_event.is_set()

    def start_tester_present(
        self,
        addressing_type: AddressingType = AddressingType.FUNCTIONAL,
        sprmib: bool = True,
    ) -> None:
        if self._tp_event.is_set():
            return
        if not sprmib:
            raise AssertionError("tests expect SPRMIB to be enabled")
        self._tp_addressing = addressing_type
        self._tp_event.set()
        self._tp_thread = Thread(target=self._tp_loop, daemon=True)
        self._tp_thread.start()

    def stop_tester_present(self) -> None:
        self._tp_event.clear()
        if self._tp_thread is not None:
            self._tp_thread.join(timeout=1.0)
            self._tp_thread = None

    def _tp_loop(self) -> None:
        while self._tp_event.wait(self.s3_client / 1000.0):
            addressing = self.transport_interface.addressing_information
            arbitration_id = (
                addressing.tx_functional_params["can_id"]
                if self._tp_addressing == AddressingType.FUNCTIONAL
                else addressing.tx_physical_params["can_id"]
            )
            self.transport_interface.network_manager.send_frame(arbitration_id, b"\x3E\x80")


class _FakeCanInterface:
    def __init__(self) -> None:
        self.sent_frames: list[tuple[int, bytes, bool]] = []

    def get_bus(self) -> object:
        return self

    def send_frame(self, arbitration_id: int, data: bytes, *, is_extended_id: bool = False) -> None:
        self.sent_frames.append((arbitration_id, data, is_extended_id))


class _FakeFrame:
    def __init__(self) -> None:
        self.channel = "test"
        self.is_extended_id = False


class _FakePacketRecord:
    def __init__(self, can_id: int, data: bytes) -> None:
        self.can_id = can_id
        self.raw_frame_data = data
        self.frame = _FakeFrame()


class _FakeUdsMessageRecord:
    def __init__(self, payload: bytes, can_id: int) -> None:
        self.payload = payload
        self.packets_records = [_FakePacketRecord(can_id, payload)]


class _TimeoutMutatingFakeClient(_FakeClient):
    def __init__(self, transport_interface: object) -> None:
        super().__init__(transport_interface)
        self._p2_client_timeout = 100.0
        self.p2_ext_client_timeout = 5050.0
        self.p6_client_timeout = 10000.0
        self.p6_ext_client_timeout = 50000.0

    @property
    def p2_client_timeout(self) -> float:
        return self._p2_client_timeout

    @p2_client_timeout.setter
    def p2_client_timeout(self, value: float) -> None:
        self._p2_client_timeout = value
        # Mimic py-uds behavior: raising P2 can implicitly raise P3.
        if value > self.p3_client_physical:
            self.p3_client_physical = value
        if value > self.p3_client_functional:
            self.p3_client_functional = value

    def send_request_receive_responses(self, request: object) -> tuple[object, tuple[object, ...]]:
        payload = getattr(request, "payload")
        request_record = _FakeUdsMessageRecord(payload, can_id=0x70D)
        response_record = _FakeUdsMessageRecord(bytes.fromhex("5003"), can_id=0x78D)
        return request_record, (response_record,)


def test_tester_present_periodic_send_physical_mode(monkeypatch) -> None:
    async def _run() -> None:
        import uds_mcp.uds.client as uds_client_module

        monkeypatch.setattr(uds_client_module, "PyCanTransportInterface", _FakeTransport)
        monkeypatch.setattr(uds_client_module, "Client", _FakeClient)

        can_if = _FakeCanInterface()
        service = UdsClientService(
            can_if,
            UdsConfig(
                tx_id=0x70D,
                rx_id=0x78D,
                tx_functional_id=0x7DF,
                rx_functional_id=0x7E8,
                tester_present_interval_sec=0.1,
            ),
            EventStore(),
        )

        status = await service.start_manual_tester_present(addressing_mode="physical")
        assert status["running"] is True
        await asyncio.sleep(0.26)
        status = await service.stop_manual_tester_present()
        assert status["running"] is False
        service.close()

        assert len(can_if.sent_frames) >= 2
        assert all(frame[0] == 0x70D for frame in can_if.sent_frames)
        assert all(frame[1] == b"\x3E\x80" for frame in can_if.sent_frames)

    asyncio.run(_run())


def test_tester_present_periodic_send_functional_mode(monkeypatch) -> None:
    async def _run() -> None:
        import uds_mcp.uds.client as uds_client_module

        monkeypatch.setattr(uds_client_module, "PyCanTransportInterface", _FakeTransport)
        monkeypatch.setattr(uds_client_module, "Client", _FakeClient)

        can_if = _FakeCanInterface()
        service = UdsClientService(
            can_if,
            UdsConfig(
                tx_id=0x70D,
                rx_id=0x78D,
                tx_functional_id=0x7DF,
                rx_functional_id=0x7E8,
                tester_present_interval_sec=0.1,
            ),
            EventStore(),
        )

        status = await service.start_manual_tester_present(addressing_mode="functional")
        assert status["addressing_mode"] == "functional"
        await asyncio.sleep(0.16)
        await service.stop_manual_tester_present()
        service.close()

        assert len(can_if.sent_frames) >= 1
        assert all(frame[0] == 0x7DF for frame in can_if.sent_frames)
        assert all(frame[1] == b"\x3E\x80" for frame in can_if.sent_frames)

    asyncio.run(_run())


def test_transport_config_for_can_fd_and_optimization(monkeypatch) -> None:
    async def _run() -> None:
        import uds_mcp.uds.client as uds_client_module

        monkeypatch.setattr(uds_client_module, "PyCanTransportInterface", _FakeTransport)
        monkeypatch.setattr(uds_client_module, "Client", _FakeClient)

        can_if = _FakeCanInterface()
        service = UdsClientService(
            can_if,
            UdsConfig(
                tx_id=0x70D,
                rx_id=0x78D,
                tx_functional_id=0x7DF,
                rx_functional_id=0x7E8,
                can_fd=True,
                use_data_optimization=True,
                dlc=32,
                min_dlc=16,
                tester_present_interval_sec=0.1,
            ),
            EventStore(),
        )

        transport = service._transport  # noqa: SLF001
        params = transport.configuration_params

        assert str(params["can_version"]) == "CanVersion.CAN_FD"
        assert params["use_data_optimization"] is True
        assert params["dlc"] == 13
        assert params["min_dlc"] == 10

        service.close()

    asyncio.run(_run())


def test_transport_config_fixed_dlc_without_optimization(monkeypatch) -> None:
    async def _run() -> None:
        import uds_mcp.uds.client as uds_client_module

        monkeypatch.setattr(uds_client_module, "PyCanTransportInterface", _FakeTransport)
        monkeypatch.setattr(uds_client_module, "Client", _FakeClient)

        can_if = _FakeCanInterface()
        service = UdsClientService(
            can_if,
            UdsConfig(
                tx_id=0x70D,
                rx_id=0x78D,
                tx_functional_id=0x7DF,
                rx_functional_id=0x7E8,
                can_fd=True,
                use_data_optimization=False,
                dlc=24,
                min_dlc=16,
                tester_present_interval_sec=0.1,
            ),
            EventStore(),
        )

        params = service._transport.configuration_params  # noqa: SLF001

        assert str(params["can_version"]) == "CanVersion.CAN_FD"
        assert params["use_data_optimization"] is False
        assert params["dlc"] == 12
        assert "min_dlc" not in params

        service.close()

    asyncio.run(_run())


def test_transport_config_raises_for_non_discrete_dlc_bytes(monkeypatch) -> None:
    import uds_mcp.uds.client as uds_client_module

    monkeypatch.setattr(uds_client_module, "PyCanTransportInterface", _FakeTransport)
    monkeypatch.setattr(uds_client_module, "Client", _FakeClient)

    can_if = _FakeCanInterface()
    try:
        UdsClientService(
            can_if,
            UdsConfig(
                tx_id=0x70D,
                rx_id=0x78D,
                tx_functional_id=0x7DF,
                rx_functional_id=0x7E8,
                can_fd=True,
                use_data_optimization=False,
                dlc=10,
                min_dlc=8,
                tester_present_interval_sec=0.1,
            ),
            EventStore(),
        )
    except ValueError as exc:
        assert "dlc=10" in str(exc)
    else:
        raise AssertionError("Expected ValueError for non-discrete dlc bytes")


def test_transport_config_raises_when_min_dlc_exceeds_dlc(monkeypatch) -> None:
    import uds_mcp.uds.client as uds_client_module

    monkeypatch.setattr(uds_client_module, "PyCanTransportInterface", _FakeTransport)
    monkeypatch.setattr(uds_client_module, "Client", _FakeClient)

    can_if = _FakeCanInterface()
    try:
        UdsClientService(
            can_if,
            UdsConfig(
                tx_id=0x70D,
                rx_id=0x78D,
                tx_functional_id=0x7DF,
                rx_functional_id=0x7E8,
                can_fd=True,
                use_data_optimization=True,
                dlc=16,
                min_dlc=24,
                tester_present_interval_sec=0.1,
            ),
            EventStore(),
        )
    except ValueError as exc:
        assert "min_dlc must be less than or equal to dlc" in str(exc)
    else:
        raise AssertionError("Expected ValueError when min_dlc exceeds dlc")


def test_send_restores_p3_after_temporary_timeout_override(monkeypatch) -> None:
    async def _run() -> None:
        import uds_mcp.uds.client as uds_client_module

        monkeypatch.setattr(uds_client_module, "PyCanTransportInterface", _FakeTransport)
        monkeypatch.setattr(uds_client_module, "Client", _TimeoutMutatingFakeClient)

        can_if = _FakeCanInterface()
        service = UdsClientService(
            can_if,
            UdsConfig(
                tx_id=0x70D,
                rx_id=0x78D,
                tx_functional_id=0x7DF,
                rx_functional_id=0x7E8,
                tester_present_interval_sec=0.1,
            ),
            EventStore(),
        )

        client = service._client  # noqa: SLF001
        assert client.p3_client_physical == 100.0
        assert client.p3_client_functional == 100.0

        result = await service.send("1003", timeout_ms=1000)
        assert result["response_hex"] == "5003"

        # Critical regression check: temporary timeout must not permanently enlarge P3.
        assert client.p3_client_physical == 100.0
        assert client.p3_client_functional == 100.0

        service.close()

    asyncio.run(_run())


def test_send_no_response_logs_tx_without_rx(monkeypatch) -> None:
    async def _run() -> None:
        import uds_mcp.uds.client as uds_client_module

        monkeypatch.setattr(uds_client_module, "PyCanTransportInterface", _FakeTransport)
        monkeypatch.setattr(uds_client_module, "Client", _FakeClient)

        can_if = _FakeCanInterface()
        store = EventStore()
        service = UdsClientService(
            can_if,
            UdsConfig(
                tx_id=0x70D,
                rx_id=0x78D,
                tx_functional_id=0x7DF,
                rx_functional_id=0x7E8,
                tester_present_interval_sec=0.1,
            ),
            store,
        )

        result = await service.send_no_response("3601AA")

        assert result["request_hex"] == "3601AA"
        assert result["response_hex"] is None
        assert result["response_id"] is None

        tx_events = store.query(kinds=[EventKind.UDS_TX])
        rx_events = store.query(kinds=[EventKind.UDS_RX])
        assert len(tx_events) == 1
        assert len(rx_events) == 0

        service.close()

    asyncio.run(_run())


def test_send_can_frames_batch(monkeypatch) -> None:
    async def _run() -> None:
        import uds_mcp.uds.client as uds_client_module

        monkeypatch.setattr(uds_client_module, "PyCanTransportInterface", _FakeTransport)
        monkeypatch.setattr(uds_client_module, "Client", _FakeClient)

        can_if = _FakeCanInterface()
        service = UdsClientService(
            can_if,
            UdsConfig(
                tx_id=0x70D,
                rx_id=0x78D,
                tx_functional_id=0x7DF,
                rx_functional_id=0x7E8,
                tester_present_interval_sec=0.1,
            ),
            EventStore(),
        )

        result = await service.send_can_frames(
            [
                {"arbitration_id": 0x700, "data_hex": "0102"},
                {"arbitration_id": 0x701, "data_hex": "AABB", "is_extended_id": True},
            ]
        )

        assert result == {"sent": 2}
        assert can_if.sent_frames == [
            (0x700, bytes.fromhex("0102"), False),
            (0x701, bytes.fromhex("AABB"), True),
        ]

        service.close()

    asyncio.run(_run())
