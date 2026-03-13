from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from uds_mcp.extensions.runtime import ExtensionRuntime
from uds_mcp.flow.schema import FlowDefinition, dump_flow_yaml, load_flow_yaml
from uds_mcp.logging.store import EventStore
from uds_mcp.models.events import EventKind, LogEvent
from uds_mcp.uds.client import UdsClientService


class FlowStatus(StrEnum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    FAILED = "FAILED"
    DONE = "DONE"


@dataclass(slots=True)
class FlowRun:
    run_id: str
    flow_name: str
    status: FlowStatus = FlowStatus.RUNNING
    current_step: str | None = None
    error: str | None = None
    trace: list[dict[str, Any]] = field(default_factory=list)
    stop_requested: bool = False
    pause_event: asyncio.Event = field(default_factory=asyncio.Event)


class FlowEngine:
    def __init__(
        self,
        uds_client: UdsClientService,
        event_store: EventStore,
        runtime: ExtensionRuntime,
    ) -> None:
        self._uds_client = uds_client
        self._event_store = event_store
        self._runtime = runtime
        self._flows: dict[str, FlowDefinition] = {}
        self._runs: dict[str, FlowRun] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def register(self, flow: FlowDefinition) -> None:
        self._flows[flow.name] = flow

    def load(self, path: Path) -> FlowDefinition:
        flow = load_flow_yaml(path)
        self.register(flow)
        return flow

    def save(self, flow_name: str, path: Path) -> None:
        flow = self._flows[flow_name]
        dump_flow_yaml(path, flow)

    def list_flows(self) -> list[str]:
        return sorted(self._flows.keys())

    def has_active_runs(self) -> bool:
        for run in self._runs.values():
            if run.status in {FlowStatus.RUNNING, FlowStatus.PAUSED}:
                return True
        return False

    def set_uds_client(self, uds_client: UdsClientService) -> None:
        self._uds_client = uds_client

    def set_runtime(self, runtime: ExtensionRuntime) -> None:
        self._runtime = runtime

    async def start(self, flow_name: str) -> str:
        flow = self._flows[flow_name]
        run_id = uuid.uuid4().hex
        run = FlowRun(run_id=run_id, flow_name=flow_name)
        run.pause_event.set()
        self._runs[run_id] = run
        self._tasks[run_id] = asyncio.create_task(self._run_flow(run, flow))
        self._log_state(run)
        return run_id

    def status(self, run_id: str) -> dict[str, Any]:
        run = self._runs[run_id]
        return {
            "run_id": run.run_id,
            "flow_name": run.flow_name,
            "status": run.status.value,
            "current_step": run.current_step,
            "error": run.error,
            "trace": run.trace,
        }

    def stop(self, run_id: str) -> None:
        run = self._runs[run_id]
        run.stop_requested = True
        run.pause_event.set()

    def resume(self, run_id: str) -> None:
        run = self._runs[run_id]
        run.pause_event.set()

    def set_breakpoint(self, flow_name: str, step_name: str, enabled: bool) -> None:
        flow = self._flows[flow_name]
        for step in flow.steps:
            if step.name == step_name:
                step.breakpoint = enabled
                return
        raise KeyError(f"step not found: {step_name}")

    def patch_step(
        self,
        flow_name: str,
        step_name: str,
        *,
        send_hex: str | None = None,
        expect_prefix: str | None = None,
    ) -> None:
        flow = self._flows[flow_name]
        for step in flow.steps:
            if step.name != step_name:
                continue
            if send_hex is not None:
                step.send = send_hex
            if expect_prefix is not None:
                if step.expect is None:
                    from uds_mcp.flow.schema import StepExpect

                    step.expect = StepExpect()
                step.expect.response_prefix = expect_prefix
            return
        raise KeyError(f"step not found: {step_name}")

    async def inject_once(self, request_hex: str, timeout_ms: int) -> dict[str, Any]:
        return await self._uds_client.send(request_hex, timeout_ms=timeout_ms)

    async def _run_flow(self, run: FlowRun, flow: FlowDefinition) -> None:
        variables = dict(flow.variables)
        try:
            for step in flow.steps:
                if run.stop_requested:
                    run.status = FlowStatus.STOPPED
                    self._log_state(run)
                    return

                run.current_step = step.name
                if step.breakpoint:
                    run.status = FlowStatus.PAUSED
                    self._log_state(run)
                    run.pause_event.clear()
                    await self._uds_client.ensure_tester_present()
                    await run.pause_event.wait()
                    await self._uds_client.stop_tester_present()
                    run.status = FlowStatus.RUNNING
                    self._log_state(run)

                request_hex = step.send
                if step.before_hook:
                    request_hex = self._apply_hook(
                        step.before_hook.script_path,
                        step.before_hook.function_name,
                        step.before_hook.snippet,
                        request_hex,
                        variables,
                    )

                response = await self._uds_client.send(request_hex, timeout_ms=step.timeout_ms)
                response_hex = str(response["response_hex"])

                if step.expect and step.expect.response_prefix:
                    expected = step.expect.response_prefix.upper()
                    if not response_hex.startswith(expected):
                        raise ValueError(
                            f"step {step.name}: expect prefix {expected}, got {response_hex}"
                        )

                item = {"step": step.name, "request_hex": request_hex, "response_hex": response_hex}
                run.trace.append(item)
                self._event_store.append(LogEvent(kind=EventKind.FLOW_STEP, payload=item))

            run.status = FlowStatus.DONE
            self._log_state(run)
        except Exception as exc:
            run.status = FlowStatus.FAILED
            run.error = str(exc)
            self._event_store.append(
                LogEvent(
                    kind=EventKind.ERROR,
                    payload={
                        "source": "flow_engine",
                        "flow": run.flow_name,
                        "run_id": run.run_id,
                        "error": str(exc),
                    },
                )
            )
            self._log_state(run)

    def _apply_hook(
        self,
        script_path: str | None,
        function_name: str | None,
        snippet: str | None,
        request_hex: str,
        variables: dict[str, Any],
    ) -> str:
        context = {"request_hex": request_hex, "variables": variables}
        updates: dict[str, Any] = {}
        if script_path and function_name:
            updates = self._runtime.run_hook(
                script_path=script_path,
                function_name=function_name,
                context=context,
            )
        elif snippet:
            updates = self._runtime.run_snippet(code=snippet, context=context)

        updated = updates.get("request_hex", request_hex)
        if not isinstance(updated, str):
            raise TypeError("hook output request_hex must be str")
        return updated

    def _log_state(self, run: FlowRun) -> None:
        self._event_store.append(
            LogEvent(
                kind=EventKind.FLOW_STATE,
                payload={
                    "run_id": run.run_id,
                    "flow_name": run.flow_name,
                    "status": run.status.value,
                    "step": run.current_step,
                    "error": run.error,
                },
            )
        )
