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

    async def send(self, request_hex: str, timeout_ms: int = 1000) -> dict[str, object]:
        del timeout_ms
        request_hex = request_hex.upper()
        if request_hex == "2711":
            response_hex = "6711ABCD"
        elif request_hex == "2712ABCD":
            response_hex = "6712"
        elif request_hex == "22ABCD":
            response_hex = "62ABCD00"
        else:
            raise ValueError(f"unexpected request: {request_hex}")
        return {
            "request_hex": request_hex,
            "response_hex": response_hex,
            "response_id": 0x7E8,
        }

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
        return {
            "running": False,
            "addressing_mode": None,
            "owners": [],
            "interval_sec": 1.0,
        }


async def _wait_run_done(engine: FlowEngine, run_id: str) -> dict[str, Any]:
    while True:
        status = engine.status(run_id)
        if status["status"] in {
            FlowStatus.DONE.value,
            FlowStatus.FAILED.value,
            FlowStatus.STOPPED.value,
        }:
            return status
        await asyncio.sleep(0.01)


def test_before_hook_can_read_previous_response_and_write_variables() -> None:
    async def _run() -> None:
        runtime = ExtensionRuntime([Path("examples/extensions").resolve()])
        engine = FlowEngine(_FakeUdsClient(), EventStore(), runtime)

        flow = FlowDefinition(
            name="security_flow",
            variables={},
            steps=[
                {
                    "name": "request_seed",
                    "send": "2711",
                    "expect": {"response_prefix": "6711"},
                },
                {
                    "name": "send_key",
                    "send": "2712FFFF",
                    "before_hook": {
                        "snippet": (
                            'seed = context["response_hex"][4:]\n'
                            'result = {"request_hex": "2712" + seed, "variables": {"seed": seed}}'
                        )
                    },
                    "expect": {"response_prefix": "6712"},
                },
                {
                    "name": "reuse_seed_variable",
                    "send": "22F190",
                    "before_hook": {
                        "snippet": 'result = {"request_hex": "22" + context["variables"]["seed"]}'
                    },
                    "expect": {"response_prefix": "62ABCD"},
                },
            ],
        )

        engine.register(flow)
        run_id = await engine.start(flow.name)
        final = await _wait_run_done(engine, run_id)

        assert final["status"] == FlowStatus.DONE.value
        assert final["trace"][1]["request_hex"] == "2712ABCD"
        assert final["trace"][2]["request_hex"] == "22ABCD"

    asyncio.run(_run())


def test_tester_present_policy_during_flow_with_step_off() -> None:
    async def _run() -> None:
        uds = _FakeUdsClient()
        runtime = ExtensionRuntime([Path("examples/extensions").resolve()])
        engine = FlowEngine(uds, EventStore(), runtime)

        flow = FlowDefinition(
            name="tp_policy_flow",
            tester_present_policy="during_flow",
            steps=[
                {
                    "name": "normal_step",
                    "send": "2711",
                    "expect": {"response_prefix": "6711"},
                },
                {
                    "name": "tp_off_step",
                    "send": "2711",
                    "tester_present": "off",
                    "expect": {"response_prefix": "6711"},
                },
            ],
        )

        engine.register(flow)
        run_id = await engine.start(flow.name)
        final = await _wait_run_done(engine, run_id)

        assert final["status"] == FlowStatus.DONE.value
        assert uds.tp_events == [
            ("start", "flow-run"),
            ("stop", "flow-run"),
            ("start", "flow-run"),
            ("stop", "flow-run"),
        ]

    asyncio.run(_run())


def test_tester_present_step_on_when_policy_off() -> None:
    async def _run() -> None:
        uds = _FakeUdsClient()
        runtime = ExtensionRuntime([Path("examples/extensions").resolve()])
        engine = FlowEngine(uds, EventStore(), runtime)

        flow = FlowDefinition(
            name="tp_step_on_flow",
            tester_present_policy="off",
            steps=[
                {
                    "name": "tp_on_step",
                    "send": "2711",
                    "tester_present": "on",
                    "expect": {"response_prefix": "6711"},
                }
            ],
        )

        engine.register(flow)
        run_id = await engine.start(flow.name)
        final = await _wait_run_done(engine, run_id)

        assert final["status"] == FlowStatus.DONE.value
        assert uds.tp_events == [
            ("start", "flow-step"),
            ("stop", "flow-step"),
        ]

    asyncio.run(_run())


def test_before_hook_trace_and_after_hook_variable_writeback() -> None:
    async def _run() -> None:
        runtime = ExtensionRuntime([Path("examples/extensions").resolve()])
        engine = FlowEngine(_FakeUdsClient(), EventStore(), runtime)

        flow = FlowDefinition(
            name="trace_after_hook_flow",
            variables={},
            steps=[
                {
                    "name": "request_seed",
                    "send": "2711",
                    "expect": {"response_prefix": "6711"},
                },
                {
                    "name": "read_via_trace",
                    "send": "22F190",
                    "before_hook": {
                        "snippet": (
                            'seed = context["trace"][-1]["response_hex"][4:]\n'
                            'result = {"request_hex": "22" + seed}'
                        )
                    },
                    "after_hook": {
                        "snippet": (
                            'variables = dict(context["variables"])\n'
                            'variables["did"] = context["response_hex"][2:6]\n'
                            'result = {"variables": variables}'
                        )
                    },
                    "expect": {"response_prefix": "62ABCD"},
                },
                {
                    "name": "reuse_after_hook_variable",
                    "send": "22F190",
                    "before_hook": {
                        "snippet": 'result = {"request_hex": "22" + context["variables"]["did"]}'
                    },
                    "expect": {"response_prefix": "62ABCD"},
                },
            ],
        )

        engine.register(flow)
        run_id = await engine.start(flow.name)
        final = await _wait_run_done(engine, run_id)

        assert final["status"] == FlowStatus.DONE.value
        assert final["trace"][1]["request_hex"] == "22ABCD"
        assert final["trace"][2]["request_hex"] == "22ABCD"

    asyncio.run(_run())
