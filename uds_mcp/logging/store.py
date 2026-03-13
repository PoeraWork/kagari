from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

    from uds_mcp.models.events import EventKind, LogEvent


class EventStore:
    """Thread-safe in-memory event store for MVP."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._events: list[LogEvent] = []

    def append(self, event: LogEvent) -> None:
        with self._lock:
            self._events.append(event)

    def query(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        kinds: Iterable[EventKind] | None = None,
        limit: int | None = None,
    ) -> list[LogEvent]:
        kind_set = set(kinds) if kinds else None
        with self._lock:
            events = list(self._events)

        filtered: list[LogEvent] = []
        for event in events:
            if start and event.created_at < start:
                continue
            if end and event.created_at > end:
                continue
            if kind_set and event.kind not in kind_set:
                continue
            filtered.append(event)

        if limit is not None:
            return filtered[-limit:]
        return filtered

    def as_dicts(self, **kwargs: object) -> list[dict[str, object]]:
        return [event.to_dict() for event in self.query(**kwargs)]
