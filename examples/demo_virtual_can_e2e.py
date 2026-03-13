from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from time import sleep

import can

from uds_mcp.can.config import CanConfig
from uds_mcp.can.interface import CanInterface
from uds_mcp.extensions.runtime import ExtensionRuntime
from uds_mcp.flow.engine import FlowEngine, FlowStatus
from uds_mcp.flow.schema import load_flow_yaml
from uds_mcp.logging.exporters.blf import BlfExporter
from uds_mcp.logging.store import EventStore
from uds_mcp.models.events import EventKind
from uds_mcp.uds.client import UdsClientService, UdsConfig


@dataclass(slots=True)
class DemoConfig:
    channel: str = "uds-demo-virtual"
    tx_id: int = 0x7E0
    rx_id: int = 0x7E8


class VirtualEcuResponder:
    """Simple ECU simulator attached to python-can virtual bus."""

    def __init__(self, config: DemoConfig) -> None:
        self._config = config
        self._stop_event = Event()
        self._bus = can.Bus(
            interface="virtual",
            channel=config.channel,
            receive_own_messages=False,
        )
        self._thread = Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=1.0)
        self._bus.shutdown()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            frame = self._bus.recv(timeout=0.1)
            if frame is None:
                continue
            if int(frame.arbitration_id) != self._config.tx_id:
                continue

            request = self._decode_iso_tp_single_frame(bytes(frame.data))
            if request is None:
                continue

            uds_response = self._build_response(request)
            response = self._encode_iso_tp_single_frame(uds_response)
            tx = can.Message(
                arbitration_id=self._config.rx_id,
                data=response,
                is_extended_id=False,
            )
            self._bus.send(tx)

    @staticmethod
    def _decode_iso_tp_single_frame(frame_data: bytes) -> bytes | None:
        if not frame_data:
            return None
        pci = frame_data[0]
        frame_type = (pci & 0xF0) >> 4
        if frame_type != 0x0:
            return None
        payload_len = pci & 0x0F
        if payload_len == 0 or len(frame_data) < 1 + payload_len:
            return None
        return frame_data[1 : 1 + payload_len]

    @staticmethod
    def _encode_iso_tp_single_frame(uds_payload: bytes) -> bytes:
        if not 0 < len(uds_payload) <= 7:
            raise ValueError("demo responder supports ISO-TP single frame only")
        frame = bytes([len(uds_payload)]) + uds_payload
        return frame.ljust(8, b"\x00")

    @staticmethod
    def _build_response(request: bytes) -> bytes:
        if not request:
            return bytes.fromhex("7F0013")

        sid = request[0]
        if sid == 0x10 and len(request) >= 2:
            return bytes([0x50, request[1], 0x00, 0x32, 0x01, 0xF4])
        if sid == 0x22 and len(request) >= 3:
            did = request[1:3]
            return bytes([0x62, *did, 0x12, 0x34, 0x56, 0x78])
        if sid == 0x3E:
            return bytes.fromhex("7E00")
        return bytes([0x7F, sid, 0x11])


async def main() -> None:
    demo = DemoConfig()
    event_store = EventStore()

    responder = VirtualEcuResponder(demo)
    responder.start()

    can_interface = CanInterface(
        CanConfig(interface="virtual", channel=demo.channel, bitrate=500000),
        event_store,
    )
    uds_client = UdsClientService(
        can_interface,
        UdsConfig(
            tx_id=demo.tx_id,
            rx_id=demo.rx_id,
            tx_functional_id=0x7DF,
            rx_functional_id=demo.rx_id,
            tester_present_interval_sec=1.0,
        ),
        event_store,
    )

    runtime = ExtensionRuntime([Path("examples/extensions").resolve()])
    flow_engine = FlowEngine(uds_client, event_store, runtime)

    flow_path = Path("examples/flows/demo_virtual_can_flow.yaml")
    flow = load_flow_yaml(flow_path)
    flow_engine.register(flow)

    start_time = datetime.now(UTC)
    run_id = await flow_engine.start(flow.name)

    try:
        while True:
            status = flow_engine.status(run_id)
            current = status["status"]
            if current in {
                FlowStatus.DONE.value,
                FlowStatus.FAILED.value,
                FlowStatus.STOPPED.value,
            }:
                break
            await asyncio.sleep(0.05)

        final = flow_engine.status(run_id)
        print("Run ID:", run_id)
        print("Status:", final["status"])
        print("Error:", final["error"])
        print("Trace:")
        for item in final["trace"]:
            print("  ", item)

        if final["status"] == FlowStatus.FAILED.value:
            errors = event_store.query(kinds=[EventKind.ERROR])
            print("Error events:")
            for evt in errors[-5:]:
                print("  ", evt.payload)

        end_time = datetime.now(UTC)
        can_events = event_store.query(
            start=start_time,
            end=end_time,
            kinds=[EventKind.CAN_TX, EventKind.CAN_RX],
        )
        output_path = Path("examples/output/demo_virtual_can.blf")
        exported = BlfExporter().export(output_path, can_events)
        print(f"BLF exported: {output_path} ({exported} frames)")

    finally:
        # Give py-uds worker threads a short grace period before bus shutdown.
        sleep(0.1)
        responder.stop()
        can_interface.close()


if __name__ == "__main__":
    asyncio.run(main())
