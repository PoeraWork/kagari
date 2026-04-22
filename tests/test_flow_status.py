"""Unit tests for FlowEngine.status() simplified return.

Requirements: 4.1, 4.2, 4.3
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_status_done_has_required_keys() -> None:
    """DONE status contains all required keys.

    Requirements: 4.1
    """

    async def _run() -> None:
        engine = _make_engine()
        flow = FlowDefinition(
            name="simple_flow",
            steps=[{"kind": "uds", "name": "step1", "request": "2711", "expect": {"response_prefix": "6711"}}],
        )
        engine.register(flow)
        run_id = await engine.start("simple_flow")
        result = await _wait_done(engine, run_id)

        required_keys = {
            "run_id",
            "flow_name",
            "status",
            "current_step",
            "error",
            "step_count",
            "message_count",
        }
        assert required_keys.issubset(result.keys())

    asyncio.run(_run())


def test_status_done_no_trace_key() -> None:
    """DONE status does NOT have 'trace' key.

    Requirements: 4.2
    """

    async def _run() -> None:
        engine = _make_engine()
        flow = FlowDefinition(
            name="simple_flow",
            steps=[{"kind": "uds", "name": "step1", "request": "2711", "expect": {"response_prefix": "6711"}}],
        )
        engine.register(flow)
        run_id = await engine.start("simple_flow")
        result = await _wait_done(engine, run_id)

        assert "trace" not in result

    asyncio.run(_run())


def test_status_done_no_failed_step_trace() -> None:
    """DONE status does NOT have 'failed_step_trace'.

    Requirements: 4.3
    """

    async def _run() -> None:
        engine = _make_engine()
        flow = FlowDefinition(
            name="simple_flow",
            steps=[{"kind": "uds", "name": "step1", "request": "2711", "expect": {"response_prefix": "6711"}}],
        )
        engine.register(flow)
        run_id = await engine.start("simple_flow")
        result = await _wait_done(engine, run_id)

        assert result["status"] == FlowStatus.DONE.value
        assert "failed_step_trace" not in result

    asyncio.run(_run())


def test_status_failed_has_failed_step_trace() -> None:
    """FAILED status has 'failed_step_trace'.

    Requirements: 4.3
    """

    async def _run() -> None:
        engine = _make_engine()
        flow = FlowDefinition(
            name="fail_flow",
            steps=[{"kind": "uds", "name": "will_fail", "request": "2711", "expect": {"response_prefix": "FFFF"}}],
        )
        engine.register(flow)
        run_id = await engine.start("fail_flow")
        result = await _wait_done(engine, run_id)

        assert result["status"] == FlowStatus.FAILED.value
        assert "failed_step_trace" in result
        assert isinstance(result["failed_step_trace"], list)
        assert len(result["failed_step_trace"]) > 0

    asyncio.run(_run())


def test_status_failed_step_trace_max_50() -> None:
    """failed_step_trace length <= 50 even with many trace records.

    Requirements: 4.3, 4.4
    """

    async def _run() -> None:
        engine = _make_engine()
        # Create a flow with repeat=60 that will succeed, then a step that fails.
        # This gives us 60 trace records before failure + 1 for the failing step = 61 total.
        flow = FlowDefinition(
            name="many_trace_flow",
            steps=[
                {
                    "kind": "uds",
                    "name": "repeated",
                    "request": "2711",
                    "repeat": 60,
                    "expect": {"response_prefix": "6711"},
                },
                {"kind": "uds", "name": "will_fail", "request": "2711", "expect": {"response_prefix": "FFFF"}},
            ],
        )
        engine.register(flow)
        run_id = await engine.start("many_trace_flow")
        result = await _wait_done(engine, run_id)

        assert result["status"] == FlowStatus.FAILED.value
        assert len(result["failed_step_trace"]) <= 50

    asyncio.run(_run())


def test_status_step_count_counts_unique_steps() -> None:
    """step_count counts unique step names.

    Requirements: 4.1
    """

    async def _run() -> None:
        engine = _make_engine()
        flow = FlowDefinition(
            name="multi_step_flow",
            steps=[
                {"kind": "uds", "name": "step_a", "request": "2711", "expect": {"response_prefix": "6711"}},
                {"kind": "uds", "name": "step_b", "request": "22ABCD", "expect": {"response_prefix": "62ABCD"}},
                {
                    "kind": "uds",
                    "name": "step_a_again",
                    "request": "2711",
                    "repeat": 2,
                    "expect": {"response_prefix": "6711"},
                },
            ],
        )
        engine.register(flow)
        run_id = await engine.start("multi_step_flow")
        result = await _wait_done(engine, run_id)

        assert result["status"] == FlowStatus.DONE.value
        # 3 unique step names: step_a, step_b, step_a_again
        assert result["step_count"] == 3

    asyncio.run(_run())


def test_status_message_count_equals_trace_length() -> None:
    """message_count equals total trace records.

    Requirements: 4.1
    """

    async def _run() -> None:
        engine = _make_engine()
        flow = FlowDefinition(
            name="count_flow",
            steps=[
                {"kind": "uds", "name": "step_a", "request": "2711", "expect": {"response_prefix": "6711"}},
                {
                    "kind": "uds",
                    "name": "step_b",
                    "request": "2711",
                    "repeat": 3,
                    "expect": {"response_prefix": "6711"},
                },
            ],
        )
        engine.register(flow)
        run_id = await engine.start("count_flow")
        result = await _wait_done(engine, run_id)

        assert result["status"] == FlowStatus.DONE.value
        trace = engine.get_trace(run_id)
        assert result["message_count"] == len(trace)
        # 1 + 3 = 4 total trace records
        assert result["message_count"] == 4

    asyncio.run(_run())


def test_status_contains_assertion_details_on_success() -> None:
    """DONE status includes assertion_details (empty for passing flow)."""

    async def _run() -> None:
        engine = _make_engine()
        flow = FlowDefinition(
            name="pass_flow",
            steps=[{"kind": "uds", "name": "s1", "request": "2711", "expect": {"response_prefix": "6711"}}],
        )
        engine.register(flow)
        run_id = await engine.start("pass_flow")
        result = await _wait_done(engine, run_id)

        assert result["status"] == FlowStatus.DONE.value
        assert "assertion_details" in result
        assert isinstance(result["assertion_details"], list)

    asyncio.run(_run())


def test_status_assertion_details_on_failure() -> None:
    """FAILED status includes structured assertion_details with detail dict."""

    async def _run() -> None:
        engine = _make_engine()
        flow = FlowDefinition(
            name="fail_detail_flow",
            steps=[{"kind": "uds", "name": "sa", "request": "2711", "expect": {"response_prefix": "FFFF"}}],
        )
        engine.register(flow)
        run_id = await engine.start("fail_detail_flow")
        result = await _wait_done(engine, run_id)

        assert result["status"] == FlowStatus.FAILED.value
        details = result["assertion_details"]
        assert len(details) >= 1
        failed = [d for d in details if not d["ok"]]
        assert len(failed) >= 1
        assert "detail" in failed[0]
        assert "expected" in failed[0]["detail"]
        assert "actual" in failed[0]["detail"]

    asyncio.run(_run())
