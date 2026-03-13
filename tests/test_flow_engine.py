from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from uds_mcp.extensions.runtime import ExtensionRuntime
from uds_mcp.flow.engine import FlowEngine, FlowStatus
from uds_mcp.flow.schema import FlowDefinition
from uds_mcp.logging.store import EventStore


class _FakeUdsClient:
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
