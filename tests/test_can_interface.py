from __future__ import annotations

import can

from uds_mcp.can.config import CanConfig
from uds_mcp.can.interface import CanInterface
from uds_mcp.logging.store import EventStore


def test_open_retries_without_bitrate_when_channel_has_active_incompatible_settings(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class _FakeBus:
        def __init__(self, **kwargs: object) -> None:
            calls.append(kwargs)
            if len(calls) == 1:
                raise can.exceptions.CanInitializationError(
                    "Another application might have set incompatible settings. "
                    "These are the currently active settings: bitrate: 500000"
                )

    monkeypatch.setattr(can, "Bus", _FakeBus)

    can_if = CanInterface(
        CanConfig(interface="vector", channel="1", bitrate=250000),
        EventStore(),
    )

    can_if.open()

    assert len(calls) == 2
    assert calls[0]["bitrate"] == 250000
    assert "bitrate" not in calls[1]


def test_open_retry_keeps_fd_enabled(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class _FakeBus:
        def __init__(self, **kwargs: object) -> None:
            calls.append(kwargs)
            if len(calls) == 1:
                raise can.exceptions.CanInitializationError(
                    "Another application might have set incompatible settings. "
                    "These are the currently active settings: bitrate: 500000"
                )

    monkeypatch.setattr(can, "Bus", _FakeBus)

    can_if = CanInterface(
        CanConfig(
            interface="vector",
            channel="1",
            bitrate=500000,
            fd=True,
            data_bitrate=2000000,
        ),
        EventStore(),
    )

    can_if.open()

    assert len(calls) == 2
    assert calls[0]["fd"] is True
    assert "bitrate" not in calls[1]
    assert "data_bitrate" not in calls[1]
    assert calls[1]["fd"] is True


def test_open_does_not_retry_for_other_initialization_errors(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class _FakeBus:
        def __init__(self, **kwargs: object) -> None:
            calls.append(kwargs)
            raise can.exceptions.CanInitializationError("hardware disconnected")

    monkeypatch.setattr(can, "Bus", _FakeBus)

    can_if = CanInterface(
        CanConfig(interface="vector", channel="1", bitrate=250000),
        EventStore(),
    )

    try:
        can_if.open()
    except can.exceptions.CanInitializationError:
        pass
    else:
        raise AssertionError("expected CanInitializationError")

    assert len(calls) == 1
    assert calls[0]["bitrate"] == 250000


def test_send_frame_marks_can_fd(monkeypatch) -> None:
    created_messages: list[dict[str, object]] = []

    class _FakeMessage:
        def __init__(self, **kwargs: object) -> None:
            created_messages.append(kwargs)

    class _FakeBus:
        def __init__(self, **kwargs: object) -> None:
            self._kwargs = kwargs

        def send(self, msg: object) -> None:
            _ = msg

    monkeypatch.setattr(can, "Bus", _FakeBus)
    monkeypatch.setattr(can, "Message", _FakeMessage)

    can_if = CanInterface(
        CanConfig(
            interface="vector",
            channel="1",
            bitrate=500000,
            fd=True,
            data_bitrate=2000000,
        ),
        EventStore(),
    )

    can_if.send_frame(0x123, bytes.fromhex("112233445566778899AA"), is_extended_id=False)

    assert len(created_messages) == 1
    assert created_messages[0]["is_fd"] is True
    assert created_messages[0]["bitrate_switch"] is True
