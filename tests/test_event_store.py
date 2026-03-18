from __future__ import annotations

import json
from datetime import timedelta
from typing import TYPE_CHECKING

from uds_mcp.logging.store import EventStore
from uds_mcp.models.events import EventKind, LogEvent

if TYPE_CHECKING:
    from pathlib import Path


def test_event_store_query_by_kind() -> None:
    store = EventStore()
    store.append(LogEvent(kind=EventKind.CAN_TX, payload={"data_hex": "0102"}))
    store.append(LogEvent(kind=EventKind.UDS_TX, payload={"request_hex": "1003"}))

    result = store.query(kinds=[EventKind.CAN_TX])

    assert len(result) == 1
    assert result[0].kind == EventKind.CAN_TX


def test_event_store_query_by_time_window() -> None:
    store = EventStore()
    first = LogEvent(kind=EventKind.CAN_TX, payload={"data_hex": "01"})
    second = LogEvent(kind=EventKind.CAN_RX, payload={"data_hex": "02"})
    third = LogEvent(kind=EventKind.CAN_TX, payload={"data_hex": "03"})

    second.created_at = first.created_at + timedelta(seconds=1)
    third.created_at = second.created_at + timedelta(seconds=1)

    store.append(first)
    store.append(second)
    store.append(third)

    result = store.query(start=second.created_at, end=third.created_at)

    assert [item.payload["data_hex"] for item in result] == ["02", "03"]


def test_persist_dir_creates_jsonl_file(tmp_path: Path) -> None:
    """When persist_dir is configured, a JSONL file is created in that directory."""
    persist_dir = tmp_path / "logs"
    store = EventStore(persist_dir=persist_dir)
    try:
        assert persist_dir.exists()
        jsonl_files = list(persist_dir.glob("*.jsonl"))
        assert len(jsonl_files) == 1
        assert jsonl_files[0].name.startswith("events_")
    finally:
        store.close()


def test_persist_dir_jsonl_format(tmp_path: Path) -> None:
    """Each line in the JSONL file is valid JSON matching event.to_dict()."""
    persist_dir = tmp_path / "logs"
    store = EventStore(persist_dir=persist_dir)
    try:
        e1 = LogEvent(kind=EventKind.CAN_TX, payload={"data_hex": "0102"})
        e2 = LogEvent(kind=EventKind.UDS_TX, payload={"request_hex": "1003"})
        store.append(e1)
        store.append(e2)

        jsonl_file = next(persist_dir.glob("*.jsonl"))
        lines = jsonl_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2

        parsed_1 = json.loads(lines[0])
        parsed_2 = json.loads(lines[1])
        assert parsed_1 == e1.to_dict()
        assert parsed_2 == e2.to_dict()
    finally:
        store.close()


def test_close_method_flushes_and_closes(tmp_path: Path) -> None:
    """close() flushes data and closes the file handle; subsequent close is safe."""
    persist_dir = tmp_path / "logs"
    store = EventStore(persist_dir=persist_dir)
    store.append(LogEvent(kind=EventKind.CAN_TX, payload={"data_hex": "AA"}))
    store.close()

    # File should contain the event
    jsonl_file = next(persist_dir.glob("*.jsonl"))
    lines = jsonl_file.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1

    # Double close should not raise
    store.close()


def test_no_persist_dir_backward_compat() -> None:
    """Without persist_dir, EventStore works as pure in-memory store."""
    store = EventStore()
    e1 = LogEvent(kind=EventKind.CAN_TX, payload={"data_hex": "0102"})
    e2 = LogEvent(kind=EventKind.UDS_RX, payload={"response_hex": "5003"})
    store.append(e1)
    store.append(e2)

    result = store.query()
    assert len(result) == 2
    assert result[0].to_dict() == e1.to_dict()
    assert result[1].to_dict() == e2.to_dict()

    # close() should be safe even without persist_dir
    store.close()


def test_no_persist_dir_creates_no_files(tmp_path: Path) -> None:
    """Without persist_dir, no disk files are created."""
    store = EventStore()
    store.append(LogEvent(kind=EventKind.CAN_TX, payload={"data_hex": "FF"}))

    jsonl_files = list(tmp_path.rglob("*.jsonl"))
    assert len(jsonl_files) == 0
    store.close()


def test_listener_add_remove() -> None:
    """Listeners receive events on append and can be removed."""
    store = EventStore()
    received: list[LogEvent] = []

    def listener(event: LogEvent) -> None:
        received.append(event)

    store.add_listener(listener)

    e1 = LogEvent(kind=EventKind.CAN_TX, payload={"data_hex": "01"})
    store.append(e1)
    assert len(received) == 1
    assert received[0] is e1

    store.remove_listener(listener)

    e2 = LogEvent(kind=EventKind.CAN_RX, payload={"data_hex": "02"})
    store.append(e2)
    # Listener was removed, so received should still have only 1 event
    assert len(received) == 1

    store.close()


def test_listener_error_does_not_block(tmp_path: Path) -> None:
    """A failing listener does not prevent other listeners or persistence."""
    persist_dir = tmp_path / "logs"
    store = EventStore(persist_dir=persist_dir)
    good_received: list[LogEvent] = []

    def bad_listener(event: LogEvent) -> None:  # noqa: ARG001
        msg = "listener error"
        raise RuntimeError(msg)

    def good_listener(event: LogEvent) -> None:
        good_received.append(event)

    store.add_listener(bad_listener)
    store.add_listener(good_listener)

    try:
        e1 = LogEvent(kind=EventKind.CAN_TX, payload={"data_hex": "AB"})
        store.append(e1)

        # Good listener should still receive the event
        assert len(good_received) == 1

        # Memory store should have the event
        assert len(store.query()) == 1

        # Disk should have the event
        jsonl_file = next(persist_dir.glob("*.jsonl"))
        lines = jsonl_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
    finally:
        store.close()
