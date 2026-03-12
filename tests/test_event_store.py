from __future__ import annotations

from datetime import timedelta

from uds_mcp.logging.store import EventStore
from uds_mcp.models.events import EventKind, LogEvent


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
