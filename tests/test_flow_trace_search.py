"""Unit tests for FlowEngine.trace_search().

Requirements: 5.1, 5.2, 5.4, 5.5, 5.6
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from uds_mcp.extensions.runtime import ExtensionRuntime
from uds_mcp.flow.engine import FlowEngine, FlowStatus
from uds_mcp.flow.schema import FlowDefinition
from uds_mcp.logging.store import EventStore


class _FakeUdsClient:
    def __init__(self) -> None:
        self.tp_events: list[tuple[str, str]] = []

    async def send(
        self, request_hex: str, timeout_ms: int = 1000, *, addressing_mode: str = "physical"
    ) -> dict[str, object]:
        del timeout_ms, addressing_mode
        request_hex = request_hex.upper()
        if request_hex == "2711":
            response_hex = "6711ABCD"
        elif request_hex == "22ABCD":
            response_hex = "62ABCD00"
        else:
            raise ValueError(f"unexpected request: {request_hex}")
        return {"request_hex": request_hex, "response_hex": response_hex, "response_id": 0x7E8}

    async def ensure_tester_present(self) -> None:
        return None

    async def stop_tester_present(self) -> None:
        return None

    async def start_tester_present_owner(
        self, owner: str, *, addressing_mode: str = "physical"
    ) -> dict[str, object]:
        self.tp_events.append(("start", owner))
        return {
            "running": True,
            "addressing_mode": addressing_mode,
            "owners": [owner],
            "interval_sec": 1.0,
        }

    async def stop_tester_present_owner(self, owner: str) -> dict[str, object]:
        self.tp_events.append(("stop", owner))
        return {"running": False, "addressing_mode": None, "owners": [], "interval_sec": 1.0}


async def _wait_done(engine: FlowEngine, run_id: str) -> dict[str, Any]:
    while True:
        status = engine.status(run_id)
        if status["status"] in {
            FlowStatus.DONE.value,
            FlowStatus.FAILED.value,
            FlowStatus.STOPPED.value,
        }:
            return status
        await asyncio.sleep(0.01)


def _make_engine() -> FlowEngine:
    uds = _FakeUdsClient()
    runtime = ExtensionRuntime([Path("examples/extensions").resolve()])
    return FlowEngine(uds, EventStore(), runtime)


def _run_flow_with_steps(steps: list[dict[str, Any]]) -> tuple[FlowEngine, str]:
    """Helper: register, start, and wait for a flow to finish. Returns (engine, run_id)."""
    engine = _make_engine()

    def _inner() -> str:
        async def _run() -> str:
            flow = FlowDefinition(name="search_flow", steps=steps)
            engine.register(flow)
            run_id = await engine.start("search_flow")
            await _wait_done(engine, run_id)
            return run_id

        return asyncio.run(_run())

    run_id = _inner()
    return engine, run_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_trace_search_basic() -> None:
    """Basic search returns matching records.

    Requirements: 5.1, 5.2
    """
    engine, run_id = _run_flow_with_steps(
        [
            {"kind": "uds", "name": "step_a", "request": "2711", "expect": {"response_prefix": "6711"}},
            {"kind": "uds", "name": "step_b", "request": "22ABCD", "expect": {"response_prefix": "62ABCD"}},
        ]
    )

    results = engine.trace_search(run_id, "step_a")
    assert len(results) == 1
    assert results[0]["step"] == "step_a"


def test_trace_search_regex() -> None:
    """Regex pattern matching works.

    Requirements: 5.2
    """
    engine, run_id = _run_flow_with_steps(
        [
            {"kind": "uds", "name": "step_a", "request": "2711", "expect": {"response_prefix": "6711"}},
            {"kind": "uds", "name": "step_b", "request": "22ABCD", "expect": {"response_prefix": "62ABCD"}},
        ]
    )

    # Match any step name starting with "step_"
    results = engine.trace_search(run_id, r"step_[ab]")
    assert len(results) == 2


def test_trace_search_limit() -> None:
    """Limit parameter caps results.

    Requirements: 5.4
    """
    engine, run_id = _run_flow_with_steps(
        [
            {
                "kind": "uds",
                "name": "repeated",
                "request": "2711",
                "repeat": 5,
                "expect": {"response_prefix": "6711"},
            },
        ]
    )

    results = engine.trace_search(run_id, "repeated", limit=3)
    assert len(results) == 3


def test_trace_search_no_match() -> None:
    """Non-matching pattern returns empty list.

    Requirements: 5.2
    """
    engine, run_id = _run_flow_with_steps(
        [
            {"kind": "uds", "name": "step_a", "request": "2711", "expect": {"response_prefix": "6711"}},
        ]
    )

    results = engine.trace_search(run_id, "nonexistent_pattern")
    assert results == []


def test_trace_search_invalid_run_id() -> None:
    """Raises KeyError for unknown run_id.

    Requirements: 5.5
    """
    engine = _make_engine()

    with pytest.raises(KeyError, match="run_id not found"):
        engine.trace_search("nonexistent_run_id", ".*")


def test_trace_search_invalid_regex() -> None:
    """Raises ValueError for bad regex pattern.

    Requirements: 5.6
    """
    engine, run_id = _run_flow_with_steps(
        [
            {"kind": "uds", "name": "step_a", "request": "2711", "expect": {"response_prefix": "6711"}},
        ]
    )

    with pytest.raises(ValueError, match="invalid regex pattern"):
        engine.trace_search(run_id, "[invalid")


def test_trace_search_wildcard() -> None:
    """'.*' returns all records.

    Requirements: 5.2
    """
    engine, run_id = _run_flow_with_steps(
        [
            {"kind": "uds", "name": "step_a", "request": "2711", "expect": {"response_prefix": "6711"}},
            {"kind": "uds", "name": "step_b", "request": "22ABCD", "expect": {"response_prefix": "62ABCD"}},
        ]
    )

    full_trace = engine.get_trace(run_id)
    results = engine.trace_search(run_id, ".*")
    assert len(results) == len(full_trace)
