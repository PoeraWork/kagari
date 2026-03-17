from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class CanConfig:
    interface: str
    channel: str
    bitrate: int = 500000
    fd: bool = False
    data_bitrate: int | None = None
