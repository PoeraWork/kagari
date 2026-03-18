from __future__ import annotations

import asyncio
import time
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
        elif request_hex == "2712ABCD":
            response_hex = "6712"
        elif request_hex == "22ABCD":
            response_hex = "62ABCD00"
        elif request_hex.endswith("BAD0"):
            response_hex = "7F3673"
        elif request_hex.startswith("36") and len(request_hex) >= 4:
            response_hex = f"76{request_hex[2:]}"
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
        trace = engine.get_trace(run_id)
        assert trace[1]["request_hex"] == "2712ABCD"
        assert trace[2]["request_hex"] == "22ABCD"

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
        trace = engine.get_trace(run_id)
        assert trace[1]["request_hex"] == "22ABCD"
        assert trace[2]["request_hex"] == "22ABCD"

    asyncio.run(_run())


def test_before_hook_can_resend_nth_transfer_data_block() -> None:
    async def _run() -> None:
        runtime = ExtensionRuntime([Path("examples/extensions").resolve()])
        engine = FlowEngine(_FakeUdsClient(), EventStore(), runtime)

        flow = FlowDefinition(
            name="transfer_data_resend",
            steps=[
                {
                    "name": "block1",
                    "send": "3601AA",
                    "expect": {"response_prefix": "76"},
                },
                {
                    "name": "block2_resend_once",
                    "send": "3602BB",
                    "before_hook": {
                        "snippet": (
                            'count = len([t for t in context["trace"] if t["request_hex"].startswith("36")]) + 1\n'
                            'req = context["request_hex"]\n'
                            "if count == 2:\n"
                            '    result = {"request_sequence_hex": [req, req]}\n'
                            "else:\n"
                            "    result = {}\n"
                        )
                    },
                    "expect": {"response_prefix": "76"},
                },
                {
                    "name": "block3_mutate",
                    "send": "3603CC",
                    "before_hook": {
                        "snippet": (
                            'count = len([t for t in context["trace"] if t["request_hex"].startswith("36")]) + 1\n'
                            "if count == 4:\n"
                            '    result = {"request_hex": "36FFDD"}\n'
                            "else:\n"
                            "    result = {}\n"
                        )
                    },
                    "expect": {"response_prefix": "76"},
                },
            ],
        )

        engine.register(flow)
        run_id = await engine.start(flow.name)
        final = await _wait_run_done(engine, run_id)

        assert final["status"] == FlowStatus.DONE.value
        trace = engine.get_trace(run_id)
        requests = [item["request_hex"] for item in trace]
        assert requests == ["3601AA", "3602BB", "3602BB", "36FFDD"]

    asyncio.run(_run())


def test_transfer_data_builtin_with_message_hook_can_mutate_nth_block() -> None:
    async def _run() -> None:
        runtime = ExtensionRuntime([Path("examples/extensions").resolve()])
        engine = FlowEngine(_FakeUdsClient(), EventStore(), runtime)

        flow = FlowDefinition(
            name="transfer_data_builtin",
            steps=[
                {
                    "name": "transfer_payload",
                    "transfer_data": {
                        "segments": [
                            {"address": 0x1000, "data_hex": "AABB"},
                            {"address": 0x2000, "data_hex": "CCDD"},
                        ],
                        "chunk_size": 1,
                        "block_counter_start": 1,
                    },
                    "message_hook": {
                        "snippet": (
                            'if context["message_index"] == 2:\n'
                            '    result = {"request_hex": "3603EE"}\n'
                            "else:\n"
                            "    result = {}\n"
                        )
                    },
                    "expect": {"response_prefix": "76"},
                }
            ],
        )

        engine.register(flow)
        run_id = await engine.start(flow.name)
        final = await _wait_run_done(engine, run_id)

        assert final["status"] == FlowStatus.DONE.value
        trace = engine.get_trace(run_id)
        requests = [item["request_hex"] for item in trace]
        assert requests == ["3601AA", "3602BB", "3603EE", "3604DD"]

    asyncio.run(_run())


def test_transfer_data_segments_hook_can_generate_segments() -> None:
    async def _run() -> None:
        runtime = ExtensionRuntime([Path("examples/extensions").resolve()])
        engine = FlowEngine(_FakeUdsClient(), EventStore(), runtime)

        flow = FlowDefinition(
            name="transfer_data_segments_hook",
            variables={"payload": "AABBCC"},
            steps=[
                {
                    "name": "transfer_from_hook",
                    "transfer_data": {
                        "segments_hook": {
                            "snippet": (
                                'p = context["variables"]["payload"]\n'
                                'result = {"segments": [{"address": 0x1000, "data_hex": p}]}'
                            )
                        },
                        "chunk_size": 1,
                        "block_counter_start": 1,
                    },
                    "expect": {"response_prefix": "76"},
                }
            ],
        )

        engine.register(flow)
        run_id = await engine.start(flow.name)
        final = await _wait_run_done(engine, run_id)

        assert final["status"] == FlowStatus.DONE.value
        trace = engine.get_trace(run_id)
        requests = [item["request_hex"] for item in trace]
        assert requests == ["3601AA", "3602BB", "3603CC"]

    asyncio.run(_run())


def test_step_delay_is_non_blocking() -> None:
    async def _run() -> None:
        runtime = ExtensionRuntime([Path("examples/extensions").resolve()])
        engine = FlowEngine(_FakeUdsClient(), EventStore(), runtime)

        flow = FlowDefinition(
            name="step_delay_flow",
            steps=[
                {
                    "name": "enter_programming_session",
                    "send": "2711",
                    "delay_ms": 150,
                    "expect": {"response_prefix": "6711"},
                },
                {
                    "name": "read_after_delay",
                    "send": "22ABCD",
                    "expect": {"response_prefix": "62ABCD"},
                },
            ],
        )

        ticks = 0
        stop_ticks = asyncio.Event()

        async def _ticker() -> None:
            nonlocal ticks
            while not stop_ticks.is_set():
                ticks += 1
                await asyncio.sleep(0.01)

        engine.register(flow)
        ticker_task = asyncio.create_task(_ticker())
        started_at = time.perf_counter()
        try:
            run_id = await engine.start(flow.name)
            final = await _wait_run_done(engine, run_id)
        finally:
            stop_ticks.set()
            await ticker_task

        elapsed = time.perf_counter() - started_at
        assert final["status"] == FlowStatus.DONE.value
        assert elapsed >= 0.09
        assert ticks >= 5
        trace = engine.get_trace(run_id)
        assert [item["step"] for item in trace] == [
            "enter_programming_session",
            "read_after_delay",
        ]

    asyncio.run(_run())


def test_stop_requested_during_step_delay_stops_before_next_step() -> None:
    async def _run() -> None:
        runtime = ExtensionRuntime([Path("examples/extensions").resolve()])
        engine = FlowEngine(_FakeUdsClient(), EventStore(), runtime)

        flow = FlowDefinition(
            name="step_delay_stop_flow",
            steps=[
                {
                    "name": "enter_programming_session",
                    "send": "2711",
                    "delay_ms": 200,
                    "expect": {"response_prefix": "6711"},
                },
                {
                    "name": "read_after_delay",
                    "send": "22ABCD",
                    "expect": {"response_prefix": "62ABCD"},
                },
            ],
        )

        engine.register(flow)
        run_id = await engine.start(flow.name)
        await asyncio.sleep(0.03)
        engine.stop(run_id)
        final = await _wait_run_done(engine, run_id)

        assert final["status"] == FlowStatus.STOPPED.value
        trace = engine.get_trace(run_id)
        assert [item["step"] for item in trace] == ["enter_programming_session"]

    asyncio.run(_run())


def test_wait_only_step_runs_and_records_trace() -> None:
    async def _run() -> None:
        runtime = ExtensionRuntime([Path("examples/extensions").resolve()])
        engine = FlowEngine(_FakeUdsClient(), EventStore(), runtime)

        flow = FlowDefinition(
            name="wait_only_flow",
            steps=[
                {
                    "name": "wait_boot",
                    "delay_ms": 120,
                    "tester_present": "on",
                },
                {
                    "name": "read_after_wait",
                    "send": "22ABCD",
                    "expect": {"response_prefix": "62ABCD"},
                },
            ],
        )

        engine.register(flow)
        started_at = time.perf_counter()
        run_id = await engine.start(flow.name)
        final = await _wait_run_done(engine, run_id)
        elapsed = time.perf_counter() - started_at

        assert final["status"] == FlowStatus.DONE.value
        assert elapsed >= 0.08
        trace = engine.get_trace(run_id)
        assert trace[0]["step"] == "wait_boot"
        assert trace[0]["action"] == "wait"
        assert trace[0]["delay_ms"] == 120
        assert trace[1]["step"] == "read_after_wait"

    asyncio.run(_run())


def test_wait_only_step_tester_present_off_suspends_flow_owner() -> None:
    async def _run() -> None:
        uds = _FakeUdsClient()
        runtime = ExtensionRuntime([Path("examples/extensions").resolve()])
        engine = FlowEngine(uds, EventStore(), runtime)

        flow = FlowDefinition(
            name="wait_tp_off_flow",
            tester_present_policy="during_flow",
            steps=[
                {
                    "name": "wait_without_tp",
                    "delay_ms": 80,
                    "tester_present": "off",
                },
                {
                    "name": "normal_step",
                    "send": "2711",
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


def test_transfer_data_fails_fast_on_unexpected_response_by_default() -> None:
    async def _run() -> None:
        runtime = ExtensionRuntime([Path("examples/extensions").resolve()])
        engine = FlowEngine(_FakeUdsClient(), EventStore(), runtime)

        flow = FlowDefinition(
            name="transfer_data_fail_fast",
            steps=[
                {
                    "name": "transfer_payload",
                    "transfer_data": {
                        "segments": [{"address": 0x1000, "data_hex": "AABBCC"}],
                        "chunk_size": 1,
                        "block_counter_start": 1,
                    },
                    "message_hook": {
                        "snippet": (
                            'if context["message_index"] == 1:\n'
                            '    result = {"request_hex": "3602BAD0"}\n'
                            "else:\n"
                            "    result = {}\n"
                        )
                    },
                    "expect": {"response_prefix": "76"},
                }
            ],
        )

        engine.register(flow)
        run_id = await engine.start(flow.name)
        final = await _wait_run_done(engine, run_id)

        assert final["status"] == FlowStatus.FAILED.value
        assert "transfer_data expect prefix 76" in str(final["error"])

    asyncio.run(_run())


def test_transfer_data_can_disable_per_message_check_for_negative_hook_logic() -> None:
    async def _run() -> None:
        runtime = ExtensionRuntime([Path("examples/extensions").resolve()])
        engine = FlowEngine(_FakeUdsClient(), EventStore(), runtime)

        flow = FlowDefinition(
            name="transfer_data_negative_by_hook",
            steps=[
                {
                    "name": "transfer_payload",
                    "transfer_data": {
                        "segments": [{"address": 0x1000, "data_hex": "AABBCC"}],
                        "chunk_size": 1,
                        "block_counter_start": 1,
                        "check_each_response": False,
                    },
                    "message_hook": {
                        "snippet": (
                            'if context["message_index"] == 1:\n'
                            '    result = {"request_hex": "3602BAD0"}\n'
                            "else:\n"
                            "    result = {}\n"
                        )
                    },
                    "after_hook": {
                        "snippet": (
                            'curr = list(context["trace"])\n'
                            'seen_negative = any(t["response_hex"].startswith("7F") for t in curr)\n'
                            "if not seen_negative:\n"
                            '    raise ValueError("expected negative response in transfer_data")\n'
                            "result = {}\n"
                        )
                    },
                    "expect": {"response_prefix": "76"},
                }
            ],
        )

        engine.register(flow)
        run_id = await engine.start(flow.name)
        final = await _wait_run_done(engine, run_id)

        assert final["status"] == FlowStatus.DONE.value
        trace = engine.get_trace(run_id)
        requests = [item["request_hex"] for item in trace]
        assert requests == ["3601AA", "3602BAD0", "3603CC"]

    asyncio.run(_run())


def test_flow_variables_path_keys_resolve_relative_to_yaml_dir(tmp_path: Path) -> None:
    async def _run() -> None:
        runtime = ExtensionRuntime([tmp_path.resolve()])
        engine = FlowEngine(_FakeUdsClient(), EventStore(), runtime)

        flows_dir = tmp_path / "flows"
        data_dir = tmp_path / "data"
        flows_dir.mkdir(parents=True, exist_ok=True)
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "sbl.s19").write_text("S0TEST\n", encoding="utf-8")

        flow_path = flows_dir / "path_vars.yaml"
        flow_path.write_text(
            "\n".join(
                [
                    "name: path_vars_demo",
                    "variables:",
                    '  sbl_s19_path: "../data/sbl.s19"',
                    "steps:",
                    "  - name: request_seed",
                    '    send: "2711"',
                    "    before_hook:",
                    "      snippet: |",
                    '        v = dict(context["variables"])',
                    '        v["resolved_path"] = v["sbl_s19_path"]',
                    '        result = {"variables": v}',
                    "    expect:",
                    '      response_prefix: "6711"',
                    "  - name: check_abs_path",
                    '    send: "2711"',
                    "    before_hook:",
                    "      snippet: |",
                    "        import os",
                    '        req = "22ABCD" if os.path.isabs(context["variables"]["resolved_path"]) else "2711"',
                    '        result = {"request_hex": req}',
                    "    expect:",
                    '      response_prefix: "62ABCD"',
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        loaded = engine.load(flow_path)
        run_id = await engine.start(loaded.name)
        final = await _wait_run_done(engine, run_id)

        assert final["status"] == FlowStatus.DONE.value
        trace = engine.get_trace(run_id)
        assert trace[1]["request_hex"] == "22ABCD"

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Sub-flow and repeat execution tests (Task 3.12)
# ---------------------------------------------------------------------------


def _write_sub_flow_yaml(path: Path, name: str, steps_yaml: str) -> None:
    """Helper to write a sub-flow YAML file."""
    path.write_text(
        f"name: {name}\nsteps:\n{steps_yaml}",
        encoding="utf-8",
    )


def _make_engine() -> tuple[FlowEngine, _FakeUdsClient]:
    uds = _FakeUdsClient()
    runtime = ExtensionRuntime([Path("examples/extensions").resolve()])
    engine = FlowEngine(uds, EventStore(), runtime)
    return engine, uds


def test_basic_sub_flow_execution(tmp_path: Path) -> None:
    """Parent flow with a sub_flow step executes the child steps.

    Requirements: 2.1
    """

    async def _run() -> None:
        engine, _ = _make_engine()

        sub_flow_path = tmp_path / "child.yaml"
        _write_sub_flow_yaml(
            sub_flow_path,
            "child_flow",
            '  - name: child_step\n    send: "2711"\n    expect:\n      response_prefix: "6711"\n',
        )

        flow = FlowDefinition(
            name="parent_flow",
            steps=[
                {"name": "parent_step", "send": "2711", "expect": {"response_prefix": "6711"}},
                {"name": "call_child", "sub_flow": str(sub_flow_path)},
            ],
        )
        engine.register(flow)
        run_id = await engine.start("parent_flow")
        final = await _wait_run_done(engine, run_id)

        assert final["status"] == FlowStatus.DONE.value
        trace = engine.get_trace(run_id)
        step_names = [t["step"] for t in trace]
        assert step_names == ["parent_step", "child_step"]
        # child step should have sub_flow_depth=1
        assert trace[1]["sub_flow_depth"] == 1
        assert trace[1]["sub_flow_name"] == "child_flow"

    asyncio.run(_run())


def test_nested_sub_flow_execution_two_levels(tmp_path: Path) -> None:
    """Sub-flow containing another sub-flow (2 levels deep).

    Requirements: 2.1, 2.5
    """

    async def _run() -> None:
        engine, _ = _make_engine()

        grandchild_path = tmp_path / "grandchild.yaml"
        _write_sub_flow_yaml(
            grandchild_path,
            "grandchild_flow",
            '  - name: grandchild_step\n    send: "2711"\n    expect:\n      response_prefix: "6711"\n',
        )

        child_path = tmp_path / "child.yaml"
        gc_posix = grandchild_path.as_posix()
        child_path.write_text(
            f"name: child_flow\nsteps:\n"
            f'  - name: child_step\n    send: "2711"\n    expect:\n      response_prefix: "6711"\n'
            f"  - name: call_grandchild\n    sub_flow: {gc_posix}\n",
            encoding="utf-8",
        )

        flow = FlowDefinition(
            name="parent_flow",
            steps=[
                {"name": "parent_step", "send": "2711", "expect": {"response_prefix": "6711"}},
                {"name": "call_child", "sub_flow": str(child_path)},
            ],
        )
        engine.register(flow)
        run_id = await engine.start("parent_flow")
        final = await _wait_run_done(engine, run_id)

        assert final["status"] == FlowStatus.DONE.value
        trace = engine.get_trace(run_id)
        step_names = [t["step"] for t in trace]
        assert step_names == ["parent_step", "child_step", "grandchild_step"]
        assert trace[0]["sub_flow_depth"] == 0
        assert trace[1]["sub_flow_depth"] == 1
        assert trace[2]["sub_flow_depth"] == 2

    asyncio.run(_run())


def test_sub_flow_variable_sharing(tmp_path: Path) -> None:
    """Sub-flow shares variables with parent (same dict reference).

    Requirements: 2.2
    """

    async def _run() -> None:
        engine, _ = _make_engine()

        # Child flow sets a variable via after_hook
        child_path = tmp_path / "child.yaml"
        child_path.write_text(
            "name: child_flow\n"
            "steps:\n"
            "  - name: child_step\n"
            '    send: "2711"\n'
            "    after_hook:\n"
            "      snippet: |\n"
            '        variables = dict(context["variables"])\n'
            '        variables["from_child"] = "hello"\n'
            '        result = {"variables": variables}\n'
            "    expect:\n"
            '      response_prefix: "6711"\n',
            encoding="utf-8",
        )

        flow = FlowDefinition(
            name="parent_flow",
            variables={"parent_var": "world"},
            steps=[
                {"name": "call_child", "sub_flow": str(child_path)},
                {
                    "name": "use_child_var",
                    "send": "2711",
                    "before_hook": {
                        "snippet": (
                            'v = context["variables"]\n'
                            'req = "22ABCD" if v.get("from_child") == "hello" else "2711"\n'
                            'result = {"request_hex": req}'
                        )
                    },
                    "expect": {"response_prefix": "62ABCD"},
                },
            ],
        )
        engine.register(flow)
        run_id = await engine.start("parent_flow")
        final = await _wait_run_done(engine, run_id)

        assert final["status"] == FlowStatus.DONE.value
        # The parent step used the variable set by the child
        trace = engine.get_trace(run_id)
        assert trace[1]["request_hex"] == "22ABCD"

    asyncio.run(_run())


def test_sub_flow_failure_propagation(tmp_path: Path) -> None:
    """Failure in sub-flow propagates to parent flow.

    Requirements: 2.6
    """

    async def _run() -> None:
        engine, _ = _make_engine()

        child_path = tmp_path / "child.yaml"
        _write_sub_flow_yaml(
            child_path,
            "child_flow",
            '  - name: child_ok\n    send: "2711"\n    expect:\n      response_prefix: "6711"\n'
            '  - name: child_fail\n    send: "2711"\n    expect:\n      response_prefix: "FFFF"\n',
        )

        flow = FlowDefinition(
            name="parent_flow",
            steps=[
                {"name": "call_child", "sub_flow": str(child_path)},
                {"name": "should_not_run", "send": "2711", "expect": {"response_prefix": "6711"}},
            ],
        )
        engine.register(flow)
        run_id = await engine.start("parent_flow")
        final = await _wait_run_done(engine, run_id)

        assert final["status"] == FlowStatus.FAILED.value
        trace = engine.get_trace(run_id)
        step_names = [t["step"] for t in trace]
        assert "should_not_run" not in step_names
        assert "child_ok" in step_names
        assert "child_fail" in step_names

    asyncio.run(_run())


def test_sub_flow_stop_signal_propagation(tmp_path: Path) -> None:
    """Stop signal propagates from parent to sub-flow.

    Requirements: 2.7
    """

    async def _run() -> None:
        engine, _ = _make_engine()

        # Child flow has two steps; the parent also has a step after the sub-flow call.
        # We use delay_ms on the parent sub_flow step to give time for stop.
        child_path = tmp_path / "child.yaml"
        child_path.write_text(
            "name: child_flow\n"
            "steps:\n"
            "  - name: child_step1\n"
            '    send: "2711"\n'
            "    expect:\n"
            '      response_prefix: "6711"\n',
            encoding="utf-8",
        )

        flow = FlowDefinition(
            name="parent_flow",
            steps=[
                {"name": "call_child", "sub_flow": str(child_path), "delay_ms": 500},
                {
                    "name": "should_not_run",
                    "send": "2711",
                    "expect": {"response_prefix": "6711"},
                },
            ],
        )
        engine.register(flow)
        run_id = await engine.start("parent_flow")
        await asyncio.sleep(0.1)
        engine.stop(run_id)
        final = await _wait_run_done(engine, run_id)

        assert final["status"] == FlowStatus.STOPPED.value
        trace = engine.get_trace(run_id)
        step_names = [t["step"] for t in trace]
        assert "should_not_run" not in step_names
        assert "child_step1" in step_names

    asyncio.run(_run())


def test_repeat_one_backward_compatible() -> None:
    """repeat=1 behaves identically to no repeat (backward compatible).

    Requirements: 3.7
    """

    async def _run() -> None:
        engine, _ = _make_engine()

        flow = FlowDefinition(
            name="repeat_one_flow",
            steps=[
                {
                    "name": "step_repeat_1",
                    "send": "2711",
                    "repeat": 1,
                    "expect": {"response_prefix": "6711"},
                },
            ],
        )
        engine.register(flow)
        run_id = await engine.start("repeat_one_flow")
        final = await _wait_run_done(engine, run_id)

        assert final["status"] == FlowStatus.DONE.value
        trace = engine.get_trace(run_id)
        assert len(trace) == 1
        assert trace[0]["repeat_index"] == 0
        assert trace[0]["step"] == "step_repeat_1"

    asyncio.run(_run())


def test_repeat_n_normal_execution() -> None:
    """repeat=3 executes the step 3 times with correct repeat_index.

    Requirements: 3.2
    """

    async def _run() -> None:
        engine, _ = _make_engine()

        flow = FlowDefinition(
            name="repeat_n_flow",
            steps=[
                {
                    "name": "repeated_step",
                    "send": "2711",
                    "repeat": 3,
                    "expect": {"response_prefix": "6711"},
                },
            ],
        )
        engine.register(flow)
        run_id = await engine.start("repeat_n_flow")
        final = await _wait_run_done(engine, run_id)

        assert final["status"] == FlowStatus.DONE.value
        trace = engine.get_trace(run_id)
        assert len(trace) == 3
        for i, item in enumerate(trace):
            assert item["step"] == "repeated_step"
            assert item["repeat_index"] == i

    asyncio.run(_run())


def test_repeat_failure_fast_exit() -> None:
    """When a repeated step fails, no further repeats execute.

    Requirements: 3.4
    """

    async def _run() -> None:
        engine, _ = _make_engine()

        # The step expects prefix "FFFF" which will never match -> fails on first attempt
        flow = FlowDefinition(
            name="repeat_fail_flow",
            steps=[
                {
                    "name": "will_fail",
                    "send": "2711",
                    "repeat": 5,
                    "expect": {"response_prefix": "FFFF"},
                },
            ],
        )
        engine.register(flow)
        run_id = await engine.start("repeat_fail_flow")
        final = await _wait_run_done(engine, run_id)

        assert final["status"] == FlowStatus.FAILED.value
        # Only 1 trace entry (the first failed attempt)
        trace = engine.get_trace(run_id)
        assert len(trace) == 1
        assert trace[0]["repeat_index"] == 0

    asyncio.run(_run())


def test_sub_flow_plus_repeat_combination(tmp_path: Path) -> None:
    """Sub-flow step with repeat=2 executes the entire sub-flow twice.

    Requirements: 3.5
    """

    async def _run() -> None:
        engine, _ = _make_engine()

        child_path = tmp_path / "child.yaml"
        _write_sub_flow_yaml(
            child_path,
            "child_flow",
            '  - name: child_step\n    send: "2711"\n    expect:\n      response_prefix: "6711"\n',
        )

        flow = FlowDefinition(
            name="parent_flow",
            steps=[
                {"name": "call_child", "sub_flow": str(child_path), "repeat": 2},
            ],
        )
        engine.register(flow)
        run_id = await engine.start("parent_flow")
        final = await _wait_run_done(engine, run_id)

        assert final["status"] == FlowStatus.DONE.value
        # child_step should appear twice (once per repeat)
        trace = engine.get_trace(run_id)
        step_names = [t["step"] for t in trace]
        assert step_names == ["child_step", "child_step"]

    asyncio.run(_run())


def test_addressing_mode_inherit_uses_flow_default() -> None:
    """Step with addressing_mode='inherit' uses flow's default_addressing_mode.

    Requirements: 9.3, 9.4
    """

    async def _run() -> None:
        uds = _FakeUdsClient()
        runtime = ExtensionRuntime([Path("examples/extensions").resolve()])
        engine = FlowEngine(uds, EventStore(), runtime)

        # Track addressing_mode passed to send
        sent_modes: list[str] = []
        original_send = uds.send

        async def tracking_send(
            request_hex: str, timeout_ms: int = 1000, *, addressing_mode: str = "physical"
        ) -> dict[str, object]:
            sent_modes.append(addressing_mode)
            return await original_send(request_hex, timeout_ms, addressing_mode=addressing_mode)

        uds.send = tracking_send  # type: ignore[assignment]

        flow = FlowDefinition(
            name="addressing_flow",
            default_addressing_mode="functional",
            steps=[
                {
                    "name": "inherit_step",
                    "send": "2711",
                    "expect": {"response_prefix": "6711"},
                },
            ],
        )
        engine.register(flow)
        run_id = await engine.start("addressing_flow")
        final = await _wait_run_done(engine, run_id)

        assert final["status"] == FlowStatus.DONE.value
        assert sent_modes == ["functional"]
        trace = engine.get_trace(run_id)
        assert trace[0]["addressing_mode"] == "functional"

    asyncio.run(_run())


def test_addressing_mode_explicit_overrides_flow_default() -> None:
    """Step with explicit addressing_mode overrides flow default.

    Requirements: 9.3, 9.4
    """

    async def _run() -> None:
        uds = _FakeUdsClient()
        runtime = ExtensionRuntime([Path("examples/extensions").resolve()])
        engine = FlowEngine(uds, EventStore(), runtime)

        sent_modes: list[str] = []
        original_send = uds.send

        async def tracking_send(
            request_hex: str, timeout_ms: int = 1000, *, addressing_mode: str = "physical"
        ) -> dict[str, object]:
            sent_modes.append(addressing_mode)
            return await original_send(request_hex, timeout_ms, addressing_mode=addressing_mode)

        uds.send = tracking_send  # type: ignore[assignment]

        flow = FlowDefinition(
            name="addressing_flow",
            default_addressing_mode="functional",
            steps=[
                {
                    "name": "physical_step",
                    "send": "2711",
                    "addressing_mode": "physical",
                    "expect": {"response_prefix": "6711"},
                },
            ],
        )
        engine.register(flow)
        run_id = await engine.start("addressing_flow")
        final = await _wait_run_done(engine, run_id)

        assert final["status"] == FlowStatus.DONE.value
        assert sent_modes == ["physical"]
        trace = engine.get_trace(run_id)
        assert trace[0]["addressing_mode"] == "physical"

    asyncio.run(_run())


def test_sub_flow_addressing_mode_inherit_uses_sub_flow_default(tmp_path: Path) -> None:
    """Sub-flow step with inherit uses the sub-flow's own default_addressing_mode.

    Requirements: 9.5
    """

    async def _run() -> None:
        uds = _FakeUdsClient()
        runtime = ExtensionRuntime([Path("examples/extensions").resolve()])
        engine = FlowEngine(uds, EventStore(), runtime)

        sent_modes: list[str] = []
        original_send = uds.send

        async def tracking_send(
            request_hex: str, timeout_ms: int = 1000, *, addressing_mode: str = "physical"
        ) -> dict[str, object]:
            sent_modes.append(addressing_mode)
            return await original_send(request_hex, timeout_ms, addressing_mode=addressing_mode)

        uds.send = tracking_send  # type: ignore[assignment]

        child_path = tmp_path / "child.yaml"
        child_path.write_text(
            "name: child_flow\n"
            "default_addressing_mode: functional\n"
            "steps:\n"
            "  - name: child_step\n"
            '    send: "2711"\n'
            "    expect:\n"
            '      response_prefix: "6711"\n',
            encoding="utf-8",
        )

        flow = FlowDefinition(
            name="parent_flow",
            default_addressing_mode="physical",
            steps=[
                {"name": "parent_step", "send": "2711", "expect": {"response_prefix": "6711"}},
                {"name": "call_child", "sub_flow": str(child_path)},
            ],
        )
        engine.register(flow)
        run_id = await engine.start("parent_flow")
        final = await _wait_run_done(engine, run_id)

        assert final["status"] == FlowStatus.DONE.value
        # parent_step uses physical (parent default), child_step uses functional (child default)
        assert sent_modes == ["physical", "functional"]
        trace = engine.get_trace(run_id)
        assert trace[0]["addressing_mode"] == "physical"
        assert trace[1]["addressing_mode"] == "functional"

    asyncio.run(_run())


def test_nesting_depth_limit_exceeded(tmp_path: Path) -> None:
    """Nesting depth exceeding 10 levels raises ValueError.

    Requirements: 2.5
    """

    async def _run() -> None:
        engine, _ = _make_engine()

        # Create a chain of 12 sub-flows that reference each other
        paths: list[Path] = [tmp_path / f"level_{i}.yaml" for i in range(12)]

        # The deepest flow (level_11) is a normal step
        _write_sub_flow_yaml(
            paths[11],
            "level_11",
            '  - name: deep_step\n    send: "2711"\n    expect:\n      response_prefix: "6711"\n',
        )

        # Each intermediate flow calls the next level
        for i in range(10, -1, -1):
            next_posix = paths[i + 1].as_posix()
            _write_sub_flow_yaml(
                paths[i],
                f"level_{i}",
                f"  - name: level_{i}_step\n    sub_flow: {next_posix}\n",
            )

        flow = FlowDefinition(
            name="parent_flow",
            steps=[
                {"name": "call_deep", "sub_flow": str(paths[0])},
            ],
        )
        engine.register(flow)
        run_id = await engine.start("parent_flow")
        final = await _wait_run_done(engine, run_id)

        assert final["status"] == FlowStatus.FAILED.value
        assert "nesting depth exceeded" in str(final["error"]).lower()

    asyncio.run(_run())
