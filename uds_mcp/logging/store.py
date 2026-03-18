from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from threading import Lock
from typing import IO, TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

    from uds_mcp.models.events import EventKind, LogEvent

logger = logging.getLogger(__name__)


class EventStore:
    """Thread-safe in-memory event store with optional JSON Lines persistence."""

    def __init__(self, *, persist_dir: Path | None = None) -> None:
        self._lock = Lock()
        self._events: list[LogEvent] = []
        self._persist_dir = persist_dir
        self._persist_file: IO[str] | None = None
        self._listeners: list[Callable[[LogEvent], None]] = []
        if persist_dir is not None:
            persist_dir.mkdir(parents=True, exist_ok=True)
            filename = f"events_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.jsonl"
            self._persist_file = (persist_dir / filename).open("a", encoding="utf-8")

    def add_listener(self, callback: Callable[[LogEvent], None]) -> None:
        """Register an event listener (e.g. for BLF streaming)."""
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[LogEvent], None]) -> None:
        """Remove a previously registered event listener."""
        self._listeners.remove(callback)

    def append(self, event: LogEvent) -> None:
        with self._lock:
            self._events.append(event)
            if self._persist_file is not None:
                try:
                    self._persist_file.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
                    self._persist_file.flush()
                except OSError:
                    logger.exception("Failed to persist event to disk")
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                logger.exception("Event listener error")

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

    def close(self) -> None:
        """Close the persistence file handle if open."""
        with self._lock:
            if self._persist_file is not None:
                try:
                    self._persist_file.flush()
                    self._persist_file.close()
                except OSError:
                    logger.exception("Failed to close persist file")
                self._persist_file = None
