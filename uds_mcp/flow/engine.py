from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

import yaml

from uds_mcp.flow.schema import (
    FlowDefinition,
    FlowStep,
    StepAssertion,
    StepExpect,
    TransferDataConfig,
    TransferSegment,
    dump_flow_yaml,
    load_flow_yaml,
)
from uds_mcp.models.events import EventKind, LogEvent

if TYPE_CHECKING:
    from uds_mcp.extensions.runtime import ExtensionRuntime
    from uds_mcp.logging.store import EventStore
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


class FlowAssertionError(RuntimeError):
    pass


class FlowAssertionFatalError(FlowAssertionError):
    pass


@dataclass(slots=True)
class _AssertionResult:
    ok: bool
    on_fail: Literal["record", "fail", "fatal"]
    message: str
    name: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


class _HookAssertions:
    def __init__(self, context: dict[str, Any]) -> None:
        self._context = context
        self._results: list[_AssertionResult] = []

    @property
    def results(self) -> list[_AssertionResult]:
        return self._results

    def true(
        self,
        *,
        condition: bool,
        name: str | None = None,
        on_fail: Literal["record", "fail", "fatal"] = "fail",
        message: str | None = None,
    ) -> None:
        if condition:
            return
        self._results.append(
            _AssertionResult(
                ok=False,
                on_fail=on_fail,
                name=name,
                message=message or "hook assertion condition is false",
            )
        )

    def response_byte_eq(
        self,
        index: int,
        value: int,
        *,
        name: str | None = None,
        on_fail: Literal["record", "fail", "fatal"] = "fail",
        message: str | None = None,
    ) -> None:
        self.byte_eq(
            hex_value=self._current_hex("response_hex"),
            index=index,
            value=value,
            name=name,
            on_fail=on_fail,
            message=message,
            source="response_hex",
        )

    def response_bytes_int_range(
        self,
        start: int,
        length: int,
        *,
        min_value: int | None = None,
        max_value: int | None = None,
        endian: Literal["big", "little"] = "big",
        name: str | None = None,
        on_fail: Literal["record", "fail", "fatal"] = "fail",
        message: str | None = None,
    ) -> None:
        self.bytes_int_range(
            hex_value=self._current_hex("response_hex"),
            start=start,
            length=length,
            min_value=min_value,
            max_value=max_value,
            endian=endian,
            name=name,
            on_fail=on_fail,
            message=message,
            source="response_hex",
        )

    def byte_eq(
        self,
        *,
        hex_value: str,
        index: int,
        value: int,
        name: str | None = None,
        on_fail: Literal["record", "fail", "fatal"] = "fail",
        message: str | None = None,
        source: str = "hex_value",
    ) -> None:
        data = bytes.fromhex(_normalize_hex(hex_value, field_name=source))
        if index >= len(data):
            self._results.append(
                _AssertionResult(
                    ok=False,
                    on_fail=on_fail,
                    name=name,
                    message=message
                    or f"{source} byte index out of range: index={index}, length={len(data)}",
                )
            )
            return

        actual = data[index]
        if actual == value:
            return

        self._results.append(
            _AssertionResult(
                ok=False,
                on_fail=on_fail,
                name=name,
                message=message
                or f"{source} byte[{index}] expected 0x{value:02X}, got 0x{actual:02X}",
                detail={"actual": actual, "expected": value, "index": index, "source": source},
            )
        )

    def bytes_int_range(
        self,
        *,
        hex_value: str,
        start: int,
        length: int,
        min_value: int | None = None,
        max_value: int | None = None,
        endian: Literal["big", "little"] = "big",
        name: str | None = None,
        on_fail: Literal["record", "fail", "fatal"] = "fail",
        message: str | None = None,
        source: str = "hex_value",
    ) -> None:
        data = bytes.fromhex(_normalize_hex(hex_value, field_name=source))
        end = start + length
        if end > len(data):
            self._results.append(
                _AssertionResult(
                    ok=False,
                    on_fail=on_fail,
                    name=name,
                    message=message
                    or f"{source} bytes range out of range: start={start}, length={length}, total={len(data)}",
                )
            )
            return

        actual = int.from_bytes(data[start:end], byteorder=endian)
        if min_value is not None and actual < min_value:
            self._results.append(
                _AssertionResult(
                    ok=False,
                    on_fail=on_fail,
                    name=name,
                    message=message
                    or f"{source} value {actual} is smaller than min_value {min_value}",
                    detail={"actual": actual, "min_value": min_value, "source": source},
                )
            )
            return
        if max_value is not None and actual > max_value:
            self._results.append(
                _AssertionResult(
                    ok=False,
                    on_fail=on_fail,
                    name=name,
                    message=message
                    or f"{source} value {actual} is greater than max_value {max_value}",
                    detail={"actual": actual, "max_value": max_value, "source": source},
                )
            )

    def _current_hex(self, key: str) -> str:
        value = self._context.get(key)
        if value is None:
            return ""
        if not isinstance(value, str):
            raise TypeError(f"hook context field {key} must be str or None")
        return value


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
        self._flow_sources: dict[str, Path] = {}
        self._runs: dict[str, FlowRun] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def register(self, flow: FlowDefinition) -> None:
        self._flows[flow.name] = flow

    def load(self, path: Path) -> FlowDefinition:
        flow = load_flow_yaml(path)
        self.register(flow)
        self._flow_sources[flow.name] = path.resolve()
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
        result: dict[str, Any] = {
            "run_id": run.run_id,
            "flow_name": run.flow_name,
            "status": run.status.value,
            "current_step": run.current_step,
            "error": run.error,
            "step_count": self._count_steps(run.trace),
            "message_count": len(run.trace),
        }
        if run.status == FlowStatus.FAILED:
            result["failed_step_trace"] = run.trace[-50:]
        return result

    def get_trace(self, run_id: str) -> list[dict[str, Any]]:
        """Return the full trace for a flow run."""
        return self._runs[run_id].trace

    def trace_search(self, run_id: str, pattern: str, limit: int = 100) -> list[dict[str, Any]]:
        """Search trace records by regex pattern."""
        if run_id not in self._runs:
            raise KeyError(f"run_id not found: {run_id}")
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"invalid regex pattern: {pattern}") from exc

        run = self._runs[run_id]
        results: list[dict[str, Any]] = []
        for entry in run.trace:
            text = " ".join(str(v) for v in entry.values() if isinstance(v, str))
            if compiled.search(text):
                results.append(entry)
                if len(results) >= limit:
                    break
        return results

    def _count_steps(self, trace: list[dict[str, Any]]) -> int:
        """Count unique step names in the trace."""
        return len({entry["step"] for entry in trace if "step" in entry})

    def stop(self, run_id: str) -> None:
        run = self._runs[run_id]
        run.stop_requested = True
        run.pause_event.set()

    def resume(self, run_id: str) -> None:
        run = self._runs[run_id]
        run.pause_event.set()

    def set_breakpoint(self, flow_name: str, step_name: str, *, enabled: bool) -> None:
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
                    step.expect = StepExpect()
                step.expect.response_prefix = expect_prefix
            return
        raise KeyError(f"step not found: {step_name}")

    async def inject_once(self, request_hex: str, timeout_ms: int) -> dict[str, Any]:
        return await self._uds_client.send(request_hex, timeout_ms=timeout_ms)

    async def _wait_step_delay(self, run: FlowRun, delay_ms: int) -> None:
        remaining_sec = delay_ms / 1000
        while remaining_sec > 0:
            if run.stop_requested:
                return
            sleep_sec = min(remaining_sec, 0.05)
            await asyncio.sleep(sleep_sec)
            remaining_sec -= sleep_sec

    async def _run_flow(self, run: FlowRun, flow: FlowDefinition) -> None:
        flow_path = self._flow_sources.get(flow.name)
        flow_dir = flow_path.parent if flow_path is not None else Path.cwd().resolve()
        variables = _resolve_flow_variables(flow.variables, flow_dir=flow_dir)
        flow_owner_active = False
        try:
            if flow.tester_present_policy == "during_flow":
                await self._uds_client.start_tester_present_owner("flow-run")
                flow_owner_active = True

            for step in flow.steps:
                if run.stop_requested:
                    run.status = FlowStatus.STOPPED
                    self._log_state(run)
                    return

                for repeat_i in range(step.repeat):
                    if run.stop_requested:
                        run.status = FlowStatus.STOPPED
                        self._log_state(run)
                        return

                    if step.sub_flow is not None:
                        await self._run_sub_flow(
                            run,
                            step.sub_flow,
                            variables,
                            flow,
                            depth=1,
                            repeat_index=repeat_i,
                            flow_owner_active=flow_owner_active,
                        )
                        if step.delay_ms > 0:
                            await self._wait_step_delay(run, step.delay_ms)
                            if run.stop_requested:
                                run.status = FlowStatus.STOPPED
                                self._log_state(run)
                                return
                    else:
                        await self._run_step(
                            run,
                            step,
                            variables,
                            flow,
                            flow_dir,
                            flow_path,
                            repeat_index=repeat_i,
                            flow_owner_active=flow_owner_active,
                        )

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
        finally:
            if flow_owner_active:
                await self._uds_client.stop_tester_present_owner("flow-run")

    async def _run_sub_flow(
        self,
        run: FlowRun,
        sub_flow_path: str,
        variables: dict[str, Any],
        parent_flow: FlowDefinition,
        *,
        depth: int,
        repeat_index: int = 0,  # noqa: ARG002
        flow_owner_active: bool = False,
    ) -> None:
        """Load and recursively execute a sub-flow."""
        if depth > 10:
            raise ValueError(f"sub_flow nesting depth exceeded limit (10), current: {depth}")

        path = Path(sub_flow_path)
        if not path.exists():  # noqa: ASYNC240
            raise FileNotFoundError(f"sub_flow file not found: {sub_flow_path}")

        sub_flow = load_flow_yaml(path)
        sub_flow_dir = path.resolve().parent  # noqa: ASYNC240

        # Inherit parent's tester_present_policy unless sub-flow YAML explicitly sets its own
        raw_data = yaml.safe_load(path.read_text(encoding="utf-8"))  # noqa: ASYNC240
        if "tester_present_policy" not in raw_data:
            sub_flow.tester_present_policy = parent_flow.tester_present_policy

        sub_flow_name = sub_flow.name

        for step in sub_flow.steps:
            if run.stop_requested:
                run.status = FlowStatus.STOPPED
                self._log_state(run)
                return

            for repeat_i in range(step.repeat):
                if run.stop_requested:
                    run.status = FlowStatus.STOPPED
                    self._log_state(run)
                    return

                if step.sub_flow is not None:
                    await self._run_sub_flow(
                        run,
                        step.sub_flow,
                        variables,
                        sub_flow,
                        depth=depth + 1,
                        repeat_index=repeat_i,
                        flow_owner_active=flow_owner_active,
                    )
                    if step.delay_ms > 0:
                        await self._wait_step_delay(run, step.delay_ms)
                        if run.stop_requested:
                            run.status = FlowStatus.STOPPED
                            self._log_state(run)
                            return
                else:
                    await self._run_step(
                        run,
                        step,
                        variables,
                        sub_flow,
                        sub_flow_dir,
                        path.resolve(),  # noqa: ASYNC240
                        depth=depth,
                        repeat_index=repeat_i,
                        flow_owner_active=flow_owner_active,
                        sub_flow_name=sub_flow_name,
                    )

    async def _run_step(
        self,
        run: FlowRun,
        step: FlowStep,
        variables: dict[str, Any],
        flow: FlowDefinition,
        flow_dir: Path,
        flow_path: Path | None,
        *,
        depth: int = 0,
        repeat_index: int = 0,
        flow_owner_active: bool = False,
        sub_flow_name: str | None = None,
    ) -> None:
        step_owner_active = False
        flow_owner_suspended = False

        addressing_mode = _resolve_addressing_mode(step, flow)

        if step.tester_present == "on":
            await self._uds_client.start_tester_present_owner("flow-step")
            step_owner_active = True
        elif step.tester_present == "off" and flow_owner_active:
            await self._uds_client.stop_tester_present_owner("flow-run")
            flow_owner_suspended = True

        try:
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

            if step.send is None and step.transfer_data is None:
                if step.delay_ms <= 0:
                    raise ValueError(f"step {step.name}: wait-only step requires delay_ms > 0")

                wait_item: dict[str, Any] = {
                    "step": step.name,
                    "action": "wait",
                    "delay_ms": step.delay_ms,
                    "repeat_index": repeat_index,
                    "sub_flow_depth": depth,
                    "sub_flow_name": sub_flow_name,
                    "addressing_mode": addressing_mode,
                }
                run.trace.append(wait_item)
                self._event_store.append(LogEvent(kind=EventKind.FLOW_STEP, payload=wait_item))

                await self._wait_step_delay(run, step.delay_ms)
                if run.stop_requested:
                    run.status = FlowStatus.STOPPED
                    self._log_state(run)
                return

            request_sequence = self._build_step_request_sequence(
                step,
                variables,
                run.trace,
                flow_dir,
                flow_path,
            )
            base_request_hex = request_sequence[0]
            previous_response_hex = run.trace[-1].get("response_hex") if run.trace else None
            if step.before_hook:
                request_sequence, hook_assertions = self._apply_before_hook(
                    step.before_hook.script_path,
                    step.before_hook.function_name,
                    step.before_hook.snippet,
                    base_request_hex,
                    previous_response_hex,
                    variables,
                    run.trace,
                    flow_dir,
                    flow_path,
                )
                self._handle_assertion_results(
                    run,
                    step,
                    hook_assertions,
                    phase="before_hook",
                )

            check_each_response = (
                step.transfer_data is not None
                and step.transfer_data.check_each_response
                and self._has_expect_response_matcher(step.expect)
            )

            response_hex = ""
            sent_count = 0
            for base_index, request_hex in enumerate(request_sequence):
                per_message_sequence = [request_hex]
                if step.message_hook:
                    per_message_sequence, hook_assertions = self._apply_message_hook(
                        step.message_hook.script_path,
                        step.message_hook.function_name,
                        step.message_hook.snippet,
                        request_hex,
                        previous_response_hex,
                        variables,
                        run.trace,
                        flow_dir,
                        flow_path,
                        message_index=base_index,
                        message_total=len(request_sequence),
                        step_name=step.name,
                    )
                    self._handle_assertion_results(
                        run,
                        step,
                        hook_assertions,
                        phase="message_hook",
                    )

                for request_hex in per_message_sequence:
                    response = await self._uds_client.send(
                        request_hex,
                        timeout_ms=step.timeout_ms,
                        addressing_mode=addressing_mode,
                    )
                    response_hex = str(response["response_hex"])

                    item: dict[str, Any] = {
                        "step": step.name,
                        "request_hex": request_hex,
                        "response_hex": response_hex,
                        "request_index": sent_count,
                        "request_total": len(request_sequence),
                        "repeat_index": repeat_index,
                        "sub_flow_depth": depth,
                        "sub_flow_name": sub_flow_name,
                        "addressing_mode": addressing_mode,
                    }
                    run.trace.append(item)
                    self._event_store.append(LogEvent(kind=EventKind.FLOW_STEP, payload=item))
                    previous_response_hex = response_hex

                    if check_each_response and step.expect is not None:
                        matcher_results = self._evaluate_expect_response_match(
                            step.expect,
                            response_hex=response_hex,
                        )
                        self._handle_assertion_results(
                            run,
                            step,
                            matcher_results,
                            phase="expect_response",
                            request_hex=request_hex,
                            response_hex=response_hex,
                        )

                    if step.expect and step.expect.assertions and step.expect.apply_each_response:
                        assertion_results = self._evaluate_step_assertions(
                            step.expect.assertions,
                            request_hex=request_hex,
                            response_hex=response_hex,
                        )
                        self._handle_assertion_results(
                            run,
                            step,
                            assertion_results,
                            phase="expect",
                            request_hex=request_hex,
                            response_hex=response_hex,
                        )

                    sent_count += 1

            if step.after_hook:
                response_hex, hook_assertions = self._apply_after_hook(
                    step.after_hook.script_path,
                    step.after_hook.function_name,
                    step.after_hook.snippet,
                    request_hex,
                    response_hex,
                    variables,
                    run.trace,
                    flow_dir,
                    flow_path,
                )
                self._handle_assertion_results(
                    run,
                    step,
                    hook_assertions,
                    phase="after_hook",
                    request_hex=request_hex,
                    response_hex=response_hex,
                )

            if step.expect and step.expect.assertions and not step.expect.apply_each_response:
                assertion_results = self._evaluate_step_assertions(
                    step.expect.assertions,
                    request_hex=request_hex,
                    response_hex=response_hex,
                )
                self._handle_assertion_results(
                    run,
                    step,
                    assertion_results,
                    phase="expect",
                    request_hex=request_hex,
                    response_hex=response_hex,
                )

            if step.expect is not None and not check_each_response:
                matcher_results = self._evaluate_expect_response_match(
                    step.expect,
                    response_hex=response_hex,
                )
                self._handle_assertion_results(
                    run,
                    step,
                    matcher_results,
                    phase="expect_response",
                    request_hex=request_hex,
                    response_hex=response_hex,
                )

            if step.delay_ms > 0:
                await self._wait_step_delay(run, step.delay_ms)
                if run.stop_requested:
                    run.status = FlowStatus.STOPPED
                    self._log_state(run)
                    return
        finally:
            if step_owner_active:
                await self._uds_client.stop_tester_present_owner("flow-step")
            if flow_owner_suspended:
                await self._uds_client.start_tester_present_owner("flow-run")

    def _apply_before_hook(
        self,
        script_path: str | None,
        function_name: str | None,
        snippet: str | None,
        request_hex: str,
        response_hex: str | None,
        variables: dict[str, Any],
        trace: list[dict[str, Any]],
        flow_dir: Path,
        flow_path: Path | None,
    ) -> tuple[list[str], list[_AssertionResult]]:
        context_variables = dict(variables)
        context = self._build_hook_context(
            request_hex=request_hex,
            response_hex=response_hex,
            variables=context_variables,
            trace=trace,
            flow_dir=flow_dir,
            flow_path=flow_path,
        )
        updates, assertions = self._run_hook(script_path, function_name, snippet, context)

        sequence = updates.get("request_sequence_hex")
        if sequence is not None:
            if not isinstance(sequence, list) or not sequence:
                raise TypeError("hook output request_sequence_hex must be a non-empty list[str]")
            if not all(isinstance(item, str) for item in sequence):
                raise TypeError("hook output request_sequence_hex must be a non-empty list[str]")
            updated_sequence = sequence
        else:
            updated = updates.get("request_hex", request_hex)
            if not isinstance(updated, str):
                raise TypeError("hook output request_hex must be str")
            updated_sequence = [updated]

        self._apply_variable_updates(variables, context_variables, updates)
        return updated_sequence, assertions

    def _apply_message_hook(
        self,
        script_path: str | None,
        function_name: str | None,
        snippet: str | None,
        request_hex: str,
        response_hex: str | None,
        variables: dict[str, Any],
        trace: list[dict[str, Any]],
        flow_dir: Path,
        flow_path: Path | None,
        *,
        message_index: int,
        message_total: int,
        step_name: str,
    ) -> tuple[list[str], list[_AssertionResult]]:
        context_variables = dict(variables)
        context = self._build_hook_context(
            request_hex=request_hex,
            response_hex=response_hex,
            variables=context_variables,
            trace=trace,
            flow_dir=flow_dir,
            flow_path=flow_path,
            message_index=message_index,
            message_total=message_total,
            step_name=step_name,
        )
        updates, assertions = self._run_hook(script_path, function_name, snippet, context)

        sequence = updates.get("request_sequence_hex")
        if sequence is not None:
            if not isinstance(sequence, list) or not sequence:
                raise TypeError("hook output request_sequence_hex must be a non-empty list[str]")
            if not all(isinstance(item, str) for item in sequence):
                raise TypeError("hook output request_sequence_hex must be a non-empty list[str]")
            updated_sequence = sequence
        else:
            updated = updates.get("request_hex", request_hex)
            if not isinstance(updated, str):
                raise TypeError("hook output request_hex must be str")
            updated_sequence = [updated]

        self._apply_variable_updates(variables, context_variables, updates)
        return updated_sequence, assertions

    def _apply_after_hook(
        self,
        script_path: str | None,
        function_name: str | None,
        snippet: str | None,
        request_hex: str,
        response_hex: str,
        variables: dict[str, Any],
        trace: list[dict[str, Any]],
        flow_dir: Path,
        flow_path: Path | None,
    ) -> tuple[str, list[_AssertionResult]]:
        context_variables = dict(variables)
        context = self._build_hook_context(
            request_hex=request_hex,
            response_hex=response_hex,
            variables=context_variables,
            trace=trace,
            flow_dir=flow_dir,
            flow_path=flow_path,
        )
        updates, assertions = self._run_hook(script_path, function_name, snippet, context)

        updated = updates.get("response_hex", response_hex)
        if not isinstance(updated, str):
            raise TypeError("hook output response_hex must be str")

        self._apply_variable_updates(variables, context_variables, updates)
        return updated, assertions

    def _run_hook(
        self,
        script_path: str | None,
        function_name: str | None,
        snippet: str | None,
        context: dict[str, Any],
    ) -> tuple[dict[str, Any], list[_AssertionResult]]:
        updates: dict[str, Any] = {}
        assertions = _HookAssertions(context)
        hook_context = dict(context)
        hook_context["assertions"] = assertions
        if script_path and function_name:
            updates = self._runtime.run_hook(
                script_path=script_path,
                function_name=function_name,
                context=hook_context,
            )
        elif snippet:
            updates = self._runtime.run_snippet(code=snippet, context=hook_context)
        return updates, assertions.results

    def _build_hook_context(
        self,
        *,
        request_hex: str,
        response_hex: str | None,
        variables: dict[str, Any],
        trace: list[dict[str, Any]],
        flow_dir: Path,
        flow_path: Path | None,
        message_index: int | None = None,
        message_total: int | None = None,
        step_name: str | None = None,
    ) -> dict[str, Any]:
        context: dict[str, Any] = {
            "request_hex": request_hex,
            "response_hex": response_hex,
            "variables": variables,
            "trace": _readonly_trace(trace),
            "flow_dir": str(flow_dir),
            "flow_path": str(flow_path) if flow_path is not None else None,
        }
        if message_index is not None:
            context["message_index"] = message_index
        if message_total is not None:
            context["message_total"] = message_total
        if step_name is not None:
            context["step_name"] = step_name
        return context

    def _apply_variable_updates(
        self,
        variables: dict[str, Any],
        context_variables: dict[str, Any],
        updates: dict[str, Any],
    ) -> None:
        original_variables = dict(variables)
        updated_variables = updates.get("variables", context_variables)
        if not isinstance(updated_variables, dict):
            raise TypeError("hook output variables must be dict")
        if updated_variables != original_variables:
            variables.clear()
            variables.update(updated_variables)

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

    def _build_step_request_sequence(
        self,
        step: FlowStep,
        variables: dict[str, Any],
        trace: list[dict[str, Any]],
        flow_dir: Path,
        flow_path: Path | None,
    ) -> list[str]:
        if step.transfer_data is None:
            if step.send is None:
                raise ValueError(
                    f"step {step.name}: send is required when transfer_data is not set"
                )
            return [step.send]

        cfg = step.transfer_data
        segments = list(cfg.segments)
        if cfg.segments_hook is not None:
            segments = self._resolve_transfer_segments_from_hook(
                step.name,
                cfg,
                variables,
                trace,
                flow_dir,
                flow_path,
            )

        prefix = _normalize_hex(cfg.request_prefix_hex, field_name="request_prefix_hex").upper()
        if len(prefix) != 2:
            raise ValueError("transfer_data.request_prefix_hex must be exactly one byte")

        requests: list[str] = []
        block_counter = cfg.block_counter_start
        for segment in segments:
            segment_bytes = _segment_data_bytes(segment)
            for offset in range(0, len(segment_bytes), cfg.chunk_size):
                chunk = segment_bytes[offset : offset + cfg.chunk_size]
                request_hex = f"{prefix}{block_counter:02X}{chunk.hex().upper()}"
                requests.append(request_hex)
                block_counter = (block_counter + 1) & 0xFF

        if not requests:
            raise ValueError(f"step {step.name}: transfer_data payload is empty")

        return requests

    def _resolve_transfer_segments_from_hook(
        self,
        step_name: str,
        cfg: TransferDataConfig,
        variables: dict[str, Any],
        trace: list[dict[str, Any]],
        flow_dir: Path,
        flow_path: Path | None,
    ) -> list[TransferSegment]:
        hook = cfg.segments_hook
        if hook is None:
            return list(cfg.segments)

        context_variables = dict(variables)
        context = self._build_hook_context(
            request_hex="",
            response_hex=None,
            variables=context_variables,
            trace=trace,
            flow_dir=flow_dir,
            flow_path=flow_path,
            step_name=step_name,
        )
        context["transfer_data"] = {
            "chunk_size": cfg.chunk_size,
            "block_counter_start": cfg.block_counter_start,
            "request_prefix_hex": cfg.request_prefix_hex,
        }
        updates, assertions = self._run_hook(
            hook.script_path,
            hook.function_name,
            hook.snippet,
            context,
        )
        if assertions:
            raise ValueError("segments_hook assertions are not supported; use step hooks")
        self._apply_variable_updates(variables, context_variables, updates)

        raw_segments = updates.get("segments")
        if raw_segments is None:
            raise ValueError("segments_hook must return {'segments': [...]} in hook result")
        if not isinstance(raw_segments, list):
            raise TypeError("segments_hook result.segments must be list")

        segments: list[TransferSegment] = []
        for item in raw_segments:
            if isinstance(item, TransferSegment):
                segments.append(item)
            elif isinstance(item, dict):
                segments.append(TransferSegment.model_validate(item))
            else:
                raise TypeError("segments_hook result.segments items must be dict")
        if not segments:
            raise ValueError("segments_hook result.segments must not be empty")
        return segments

    def _evaluate_step_assertions(
        self,
        assertions: list[StepAssertion],
        *,
        request_hex: str,
        response_hex: str,
    ) -> list[_AssertionResult]:
        results: list[_AssertionResult] = []
        for assertion in assertions:
            try:
                actual_hex = response_hex if assertion.source == "response_hex" else request_hex
                normalized = _normalize_hex(actual_hex, field_name=assertion.source)
                data = bytes.fromhex(normalized)
            except ValueError as exc:
                results.append(
                    _AssertionResult(
                        ok=False,
                        on_fail=assertion.on_fail,
                        name=assertion.name,
                        message=assertion.message or f"invalid hex for assertion source: {exc}",
                    )
                )
                continue

            if assertion.kind == "hex_prefix":
                prefix = _normalize_hex(
                    assertion.prefix or "", field_name="assertion.prefix"
                ).upper()
                if normalized.upper().startswith(prefix):
                    continue
                results.append(
                    _AssertionResult(
                        ok=False,
                        on_fail=assertion.on_fail,
                        name=assertion.name,
                        message=assertion.message
                        or f"expected {assertion.source} to start with {prefix}, got {normalized.upper()}",
                    )
                )
                continue

            if assertion.kind == "hex_equals":
                expected_hex = _normalize_hex(
                    assertion.expected_hex or "", field_name="assertion.expected_hex"
                ).upper()
                if normalized.upper() == expected_hex:
                    continue
                results.append(
                    _AssertionResult(
                        ok=False,
                        on_fail=assertion.on_fail,
                        name=assertion.name,
                        message=assertion.message
                        or f"expected {assertion.source} == {expected_hex}, got {normalized.upper()}",
                    )
                )
                continue

            if assertion.kind == "byte_eq":
                if assertion.index is None or assertion.value is None:
                    results.append(
                        _AssertionResult(
                            ok=False,
                            on_fail=assertion.on_fail,
                            name=assertion.name,
                            message=assertion.message or "byte_eq requires index and value",
                        )
                    )
                    continue
                if assertion.index >= len(data):
                    results.append(
                        _AssertionResult(
                            ok=False,
                            on_fail=assertion.on_fail,
                            name=assertion.name,
                            message=assertion.message
                            or f"byte_eq index out of range: {assertion.index} >= {len(data)}",
                        )
                    )
                    continue
                actual = data[assertion.index]
                if actual == assertion.value:
                    continue
                results.append(
                    _AssertionResult(
                        ok=False,
                        on_fail=assertion.on_fail,
                        name=assertion.name,
                        message=assertion.message
                        or f"byte_eq failed at index {assertion.index}: expected {assertion.value}, got {actual}",
                        detail={
                            "actual": actual,
                            "expected": assertion.value,
                            "index": assertion.index,
                        },
                    )
                )
                continue

            if assertion.kind == "bytes_int_range":
                if assertion.start is None or assertion.length is None:
                    results.append(
                        _AssertionResult(
                            ok=False,
                            on_fail=assertion.on_fail,
                            name=assertion.name,
                            message=assertion.message
                            or "bytes_int_range requires start and length",
                        )
                    )
                    continue
                end = assertion.start + assertion.length
                if end > len(data):
                    results.append(
                        _AssertionResult(
                            ok=False,
                            on_fail=assertion.on_fail,
                            name=assertion.name,
                            message=assertion.message
                            or "bytes_int_range slice out of range: "
                            f"start={assertion.start}, length={assertion.length}, total={len(data)}",
                        )
                    )
                    continue
                actual = int.from_bytes(data[assertion.start : end], byteorder=assertion.endian)
                if assertion.min_value is not None and actual < assertion.min_value:
                    results.append(
                        _AssertionResult(
                            ok=False,
                            on_fail=assertion.on_fail,
                            name=assertion.name,
                            message=assertion.message
                            or f"bytes_int_range actual {actual} < min_value {assertion.min_value}",
                            detail={"actual": actual, "min_value": assertion.min_value},
                        )
                    )
                    continue
                if assertion.max_value is not None and actual > assertion.max_value:
                    results.append(
                        _AssertionResult(
                            ok=False,
                            on_fail=assertion.on_fail,
                            name=assertion.name,
                            message=assertion.message
                            or f"bytes_int_range actual {actual} > max_value {assertion.max_value}",
                            detail={"actual": actual, "max_value": assertion.max_value},
                        )
                    )

        return results

    def _has_expect_response_matcher(self, expect: StepExpect | None) -> bool:
        if expect is None:
            return False
        return any(
            [
                expect.response_prefix is not None,
                expect.response_regex is not None,
                expect.response_equals is not None,
            ]
        )

    def _evaluate_expect_response_match(
        self,
        expect: StepExpect,
        *,
        response_hex: str,
    ) -> list[_AssertionResult]:
        if not self._has_expect_response_matcher(expect):
            return []

        normalized = _normalize_hex(response_hex, field_name="response_hex").upper()

        if expect.response_prefix is not None:
            expected = _normalize_hex(
                expect.response_prefix, field_name="expect.response_prefix"
            ).upper()
            if normalized.startswith(expected):
                return []
            return [
                _AssertionResult(
                    ok=False,
                    on_fail=expect.response_on_fail,
                    name="expect.response_prefix",
                    message=f"expect response_prefix {expected}, got {normalized}",
                )
            ]

        if expect.response_equals is not None:
            expected = _normalize_hex(
                expect.response_equals, field_name="expect.response_equals"
            ).upper()
            if normalized == expected:
                return []
            return [
                _AssertionResult(
                    ok=False,
                    on_fail=expect.response_on_fail,
                    name="expect.response_equals",
                    message=f"expect response_equals {expected}, got {normalized}",
                )
            ]

        if expect.response_regex is not None:
            pattern = expect.response_regex
            try:
                matched = re.fullmatch(pattern, normalized) is not None
            except re.error as exc:
                return [
                    _AssertionResult(
                        ok=False,
                        on_fail=expect.response_on_fail,
                        name="expect.response_regex",
                        message=f"invalid expect.response_regex pattern: {exc}",
                    )
                ]
            if matched:
                return []
            return [
                _AssertionResult(
                    ok=False,
                    on_fail=expect.response_on_fail,
                    name="expect.response_regex",
                    message=f"expect response_regex {pattern!r}, got {normalized}",
                )
            ]

        return []

    def _handle_assertion_results(
        self,
        run: FlowRun,
        step: FlowStep,
        results: list[_AssertionResult],
        *,
        phase: str,
        request_hex: str | None = None,
        response_hex: str | None = None,
    ) -> None:
        for result in results:
            if result.ok:
                continue

            payload: dict[str, Any] = {
                "source": "flow_assertion",
                "flow": run.flow_name,
                "run_id": run.run_id,
                "step": step.name,
                "phase": phase,
                "name": result.name,
                "on_fail": result.on_fail,
                "message": result.message,
                "request_hex": request_hex,
                "response_hex": response_hex,
                "detail": result.detail,
            }
            self._event_store.append(LogEvent(kind=EventKind.ERROR, payload=payload))

            if result.on_fail == "record":
                continue
            if result.on_fail == "fatal":
                raise FlowAssertionFatalError(
                    f"step {step.name} [{phase}] fatal assertion failed: {result.message}"
                )
            raise FlowAssertionError(
                f"step {step.name} [{phase}] assertion failed: {result.message}"
            )


def _readonly_trace(trace: list[dict[str, Any]]) -> tuple[MappingProxyType[str, Any], ...]:
    items: list[MappingProxyType[str, Any]] = [MappingProxyType(dict(entry)) for entry in trace]
    return tuple(items)


def _segment_data_bytes(segment: TransferSegment) -> bytes:
    normalized = _normalize_hex(segment.data_hex, field_name="transfer_data.segments[].data_hex")
    return bytes.fromhex(normalized)


def _normalize_hex(value: str, *, field_name: str) -> str:
    normalized = value.strip().replace(" ", "")
    if normalized.startswith(("0x", "0X")):
        normalized = normalized[2:]
    if len(normalized) % 2 != 0:
        raise ValueError(f"{field_name} must contain an even number of hex chars")
    try:
        bytes.fromhex(normalized)
    except ValueError as exc:
        raise ValueError(f"{field_name} is not valid hex") from exc
    return normalized


def _resolve_flow_variables(values: dict[str, Any], *, flow_dir: Path) -> dict[str, Any]:
    resolved = dict(values)
    for key, value in list(resolved.items()):
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if not key.endswith("_path"):
            continue
        path_value = Path(value)
        if path_value.is_absolute():
            resolved[key] = str(path_value.resolve())
            continue
        resolved[key] = str((flow_dir / path_value).resolve())
    return resolved


def _resolve_addressing_mode(
    step: FlowStep,
    flow: FlowDefinition,
) -> Literal["physical", "functional"]:
    """Resolve the effective addressing mode for a step.

    When the step's addressing_mode is "inherit", use the flow's
    default_addressing_mode.  Otherwise use the step's own value.
    """
    if step.addressing_mode != "inherit":
        return step.addressing_mode
    return flow.default_addressing_mode
