from __future__ import annotations

import asyncio

from uds_mcp.logging.store import EventStore
from uds_mcp.uds.client import UdsClientService, UdsConfig


class _FakeNotifier:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class _FakeTransport:
    def __init__(self, network_manager: object, addressing_information: object) -> None:
        self.network_manager = network_manager
        self.addressing_information = addressing_information
        self.notifier = _FakeNotifier()


class _FakeClient:
    def __init__(self, transport_interface: object) -> None:
        self.transport_interface = transport_interface

    def send_request_receive_responses(self, request: object) -> tuple[object, tuple[object, ...]]:
        raise RuntimeError("not used in this test")


class _FakeCanInterface:
    def __init__(self) -> None:
        self.sent_frames: list[tuple[int, bytes, bool]] = []

    def get_bus(self) -> object:
        return object()

    def send_frame(self, arbitration_id: int, data: bytes, *, is_extended_id: bool = False) -> None:
        self.sent_frames.append((arbitration_id, data, is_extended_id))


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
                tester_present_interval_sec=0.05,
            ),
            EventStore(),
        )

        status = await service.start_manual_tester_present(addressing_mode="physical")
        assert status["running"] is True
        await asyncio.sleep(0.16)
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
                tester_present_interval_sec=0.05,
            ),
            EventStore(),
        )

        status = await service.start_manual_tester_present(addressing_mode="functional")
        assert status["addressing_mode"] == "functional"
        await asyncio.sleep(0.12)
        await service.stop_manual_tester_present()
        service.close()

        assert len(can_if.sent_frames) >= 1
        assert all(frame[0] == 0x7DF for frame in can_if.sent_frames)
        assert all(frame[1] == b"\x3E\x80" for frame in can_if.sent_frames)

    asyncio.run(_run())
