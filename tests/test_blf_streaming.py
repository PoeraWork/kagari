"""Unit tests for BLF streaming mode in BlfExporter.

Requirements: 8.3, 8.4, 8.5
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import can

from uds_mcp.logging.exporters.blf import BlfExporter
from uds_mcp.logging.store import EventStore
from uds_mcp.models.events import EventKind, LogEvent

if TYPE_CHECKING:
    from pathlib import Path


def _make_can_event(
    kind: EventKind = EventKind.CAN_TX,
    arb_id: int = 0x7E0,
    data_hex: str = "1003",
    *,
    is_extended_id: bool = False,
) -> LogEvent:
    return LogEvent(
        kind=kind,
        payload={
            "arbitration_id": arb_id,
            "data_hex": data_hex,
            "is_extended_id": is_extended_id,
        },
    )


def _read_blf_messages(blf_path: Path) -> list[can.Message]:
    return list(can.BLFReader(str(blf_path)))


def test_start_stop_streaming(tmp_path: Path) -> None:
    """start_streaming creates a writer; stop_streaming closes it."""
    exporter = BlfExporter()
    blf_path = tmp_path / "test.blf"

    assert exporter._streaming_writer is None

    exporter.start_streaming(blf_path)
    assert exporter._streaming_writer is not None
    assert blf_path.exists()

    exporter.stop_streaming()
    assert exporter._streaming_writer is None


def test_on_event_writes_can_events(tmp_path: Path) -> None:
    """CAN_TX and CAN_RX events are written to the BLF file."""
    exporter = BlfExporter()
    blf_path = tmp_path / "output.blf"

    exporter.start_streaming(blf_path)

    tx_event = _make_can_event(EventKind.CAN_TX, 0x7E0, "1003")
    rx_event = _make_can_event(EventKind.CAN_RX, 0x7E8, "5003")

    exporter.on_event(tx_event)
    exporter.on_event(rx_event)
    exporter.stop_streaming()

    msgs = _read_blf_messages(blf_path)
    assert len(msgs) == 2

    assert msgs[0].arbitration_id == 0x7E0
    assert msgs[0].data == bytes.fromhex("1003")

    assert msgs[1].arbitration_id == 0x7E8
    assert msgs[1].data == bytes.fromhex("5003")


def test_on_event_ignores_non_can_events(tmp_path: Path) -> None:
    """Non-CAN events (UDS, FLOW, ERROR) are not written to BLF."""
    exporter = BlfExporter()
    blf_path = tmp_path / "output.blf"

    exporter.start_streaming(blf_path)

    exporter.on_event(LogEvent(kind=EventKind.UDS_TX, payload={"request_hex": "1003"}))
    exporter.on_event(LogEvent(kind=EventKind.FLOW_STEP, payload={"step": "s1"}))
    exporter.on_event(LogEvent(kind=EventKind.ERROR, payload={"msg": "oops"}))
    # Only this one should be written
    exporter.on_event(_make_can_event(EventKind.CAN_TX, 0x7E0, "1003"))

    exporter.stop_streaming()

    msgs = _read_blf_messages(blf_path)
    assert len(msgs) == 1
    assert msgs[0].arbitration_id == 0x7E0


def test_on_event_without_streaming_is_noop() -> None:
    """Calling on_event without start_streaming does nothing (no error)."""
    exporter = BlfExporter()
    event = _make_can_event()
    # Should not raise
    exporter.on_event(event)


def test_stop_streaming_without_start_is_noop() -> None:
    """Calling stop_streaming without start_streaming is safe (no error)."""
    exporter = BlfExporter()
    # Should not raise
    exporter.stop_streaming()


def test_streaming_with_event_store_listener(tmp_path: Path) -> None:
    """Full integration: EventStore.add_listener → append events → verify BLF."""
    exporter = BlfExporter()
    blf_path = tmp_path / "stream.blf"
    store = EventStore()

    exporter.start_streaming(blf_path)
    store.add_listener(exporter.on_event)

    try:
        # Append CAN events through the store
        store.append(_make_can_event(EventKind.CAN_TX, 0x7E0, "1003"))
        store.append(_make_can_event(EventKind.CAN_RX, 0x7E8, "5003"))
        # Non-CAN event should be ignored by BLF exporter
        store.append(LogEvent(kind=EventKind.UDS_TX, payload={"request_hex": "1003"}))
        store.append(_make_can_event(EventKind.CAN_TX, 0x7E0, "2201"))
    finally:
        store.remove_listener(exporter.on_event)
        exporter.stop_streaming()
        store.close()

    msgs = _read_blf_messages(blf_path)
    assert len(msgs) == 3

    assert msgs[0].arbitration_id == 0x7E0
    assert msgs[0].data == bytes.fromhex("1003")

    assert msgs[1].arbitration_id == 0x7E8
    assert msgs[1].data == bytes.fromhex("5003")

    assert msgs[2].arbitration_id == 0x7E0
    assert msgs[2].data == bytes.fromhex("2201")


def test_export_still_works(tmp_path: Path) -> None:
    """Existing export() batch method still works after adding streaming support."""
    exporter = BlfExporter()
    batch_path = tmp_path / "batch.blf"

    events = [
        _make_can_event(EventKind.CAN_TX, 0x7E0, "1003"),
        _make_can_event(EventKind.CAN_RX, 0x7E8, "5003"),
        LogEvent(kind=EventKind.UDS_TX, payload={"request_hex": "1003"}),
    ]

    count = exporter.export(batch_path, events)
    assert count == 2  # Only CAN events

    msgs = _read_blf_messages(batch_path)
    assert len(msgs) == 2
    assert msgs[0].arbitration_id == 0x7E0
    assert msgs[1].arbitration_id == 0x7E8
