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
from pydantic import ValidationError

from uds_mcp.flow.schema import (
    AfterHookReturn,
    BeforeHookReturn,
    CanStep,
    CanTxHookReturn,
    FlowDefinition,
    FlowStep,
    HookConfig,
    MessageHookReturn,
    SegmentsHookReturn,
    StepAssertion,
    StepExpect,
    SubflowStep,
    TransferDataConfig,
    TransferSegment,
    TransferStep,
    UdsStep,
    WaitStep,
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
    assertion_details: list[dict[str, Any]] = field(default_factory=list)
    stop_requested: bool = False
    pause_event: asyncio.Event = field(default_factory=asyncio.Event)


class FlowAssertionError(RuntimeError):
    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.detail: dict[str, Any] | None = detail


class FlowAssertionFatalError(FlowAssertionError):
    pass


@dataclass(slots=True)
class _AssertionResult:
    ok: bool
    on_fail: Literal["record", "fail", "fatal"]
    message: str
    name: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class _RequestDispatchItem:
    request_hex: str
    skipped_response: bool = False


@dataclass(slots=True)
class _CanFrameDispatchItem:
    arbitration_id: int
    data_hex: str
    is_extended_id: bool = False


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
            "assertion_details": list(run.assertion_details),
        }
        if run.status == FlowStatus.FAILED:
            result["failed_step_trace"] = run.trace[-50:]
        return result

    def get_trace(self, run_id: str) -> list[dict[str, Any]]:
        return self._runs[run_id].trace

    def trace_search(self, run_id: str, pattern: str, limit: int = 100) -> list[dict[str, Any]]:
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
        request_hex: str | None = None,
        expect_prefix: str | None = None,
    ) -> None:
        flow = self._flows[flow_name]
        for step in flow.steps:
            if step.name != step_name:
                continue
            if not isinstance(step, UdsStep):
                raise TypeError(
                    f"patch_step only supports uds-kind steps (got {step.kind!r} for {step_name!r})"
                )
            if request_hex is not None:
                step.request = request_hex
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
                await self._uds_client.start_tester_present_owner(
                    "flow-run", addressing_mode=flow.tester_present_addressing_mode
                )
                flow_owner_active = True

            for _ in range(flow.repeat):
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

                        if isinstance(step, SubflowStep):
                            await self._run_sub_flow(
                                run,
                                step.subflow,
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
        if depth > 10:
            raise ValueError(f"sub_flow nesting depth exceeded limit (10), current: {depth}")

        path = Path(sub_flow_path)
        if not path.exists():  # noqa: ASYNC240
            raise FileNotFoundError(f"sub_flow file not found: {sub_flow_path}")

        sub_flow = load_flow_yaml(path)
        sub_flow_dir = path.resolve().parent  # noqa: ASYNC240

        raw_data = yaml.safe_load(path.read_text(encoding="utf-8"))  # noqa: ASYNC240
        if "tester_present_policy" not in raw_data:
            sub_flow.tester_present_policy = parent_flow.tester_present_policy

        sub_flow_name = sub_flow.name

        for _ in range(sub_flow.repeat):
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

                    if isinstance(step, SubflowStep):
                        await self._run_sub_flow(
                            run,
                            step.subflow,
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
        tester_present_mode = _resolve_tester_present_mode(step, flow)

        # Step-level TP override: a concrete physical/functional value means
        # this step wants its own TP owner (taking precedence over any
        # flow-run owner). "off" explicitly suspends any TP for this step.
        if tester_present_mode is not None and step.tester_present != "inherit":
            if flow_owner_active:
                await self._uds_client.stop_tester_present_owner("flow-run")
                flow_owner_suspended = True
            await self._uds_client.start_tester_present_owner(
                "flow-step", addressing_mode=tester_present_mode
            )
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
                await self._uds_client.start_tester_present_owner(
                    "flow-breakpoint",
                    addressing_mode=flow.tester_present_addressing_mode,
                )
                await run.pause_event.wait()
                await self._uds_client.stop_tester_present_owner("flow-breakpoint")
                run.status = FlowStatus.RUNNING
                self._log_state(run)

            if isinstance(step, WaitStep):
                await self._execute_wait_step(
                    run,
                    step,
                    repeat_index=repeat_index,
                    depth=depth,
                    sub_flow_name=sub_flow_name,
                    addressing_mode=addressing_mode,
                )
                return

            if isinstance(step, CanStep):
                await self._execute_can_step(
                    run,
                    step,
                    variables,
                    flow_dir,
                    flow_path,
                    repeat_index=repeat_index,
                    depth=depth,
                    sub_flow_name=sub_flow_name,
                    addressing_mode=addressing_mode,
                )
                if step.delay_ms > 0:
                    await self._wait_step_delay(run, step.delay_ms)
                    if run.stop_requested:
                        run.status = FlowStatus.STOPPED
                        self._log_state(run)
                        return
                return

            # UdsStep or TransferStep
            assert isinstance(step, UdsStep | TransferStep)
            await self._execute_uds_or_transfer_step(
                run,
                step,
                variables,
                flow_dir,
                flow_path,
                repeat_index=repeat_index,
                depth=depth,
                sub_flow_name=sub_flow_name,
                addressing_mode=addressing_mode,
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
                await self._uds_client.start_tester_present_owner(
                    "flow-run", addressing_mode=flow.tester_present_addressing_mode
                )

    async def _execute_wait_step(
        self,
        run: FlowRun,
        step: WaitStep,
        *,
        repeat_index: int,
        depth: int,
        sub_flow_name: str | None,
        addressing_mode: Literal["physical", "functional"],
    ) -> None:
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

    async def _execute_can_step(
        self,
        run: FlowRun,
        step: CanStep,
        variables: dict[str, Any],
        flow_dir: Path,
        flow_path: Path | None,
        *,
        repeat_index: int,
        depth: int,
        sub_flow_name: str | None,
        addressing_mode: Literal["physical", "functional"],
    ) -> None:
        previous_response_hex = run.trace[-1].get("response_hex") if run.trace else None
        can_frames, hook_assertions = self._apply_can_tx_hook(
            step.can_tx_hook,
            request_hex="",
            response_hex=previous_response_hex,
            variables=variables,
            trace=run.trace,
            flow_dir=flow_dir,
            flow_path=flow_path,
            message_index=0,
            message_total=1,
            step_name=step.name,
            skipped_response=True,
            addressing_mode=addressing_mode,
        )
        self._handle_assertion_results(
            run,
            step,
            hook_assertions,
            phase="can_tx_hook",
        )

        if can_frames:
            await self._uds_client.send_can_frames(
                [
                    {
                        "arbitration_id": frame.arbitration_id,
                        "data_hex": frame.data_hex,
                        "is_extended_id": frame.is_extended_id,
                    }
                    for frame in can_frames
                ]
            )

        item: dict[str, Any] = {
            "step": step.name,
            "action": "can_tx",
            "can_frame_count": len(can_frames),
            "repeat_index": repeat_index,
            "sub_flow_depth": depth,
            "sub_flow_name": sub_flow_name,
            "addressing_mode": addressing_mode,
        }
        run.trace.append(item)
        self._event_store.append(LogEvent(kind=EventKind.FLOW_STEP, payload=item))

    async def _execute_uds_or_transfer_step(
        self,
        run: FlowRun,
        step: UdsStep | TransferStep,
        variables: dict[str, Any],
        flow_dir: Path,
        flow_path: Path | None,
        *,
        repeat_index: int,
        depth: int,
        sub_flow_name: str | None,
        addressing_mode: Literal["physical", "functional"],
    ) -> None:
        request_sequence = self._build_step_request_sequence(
            step,
            variables,
            run.trace,
            flow_dir,
            flow_path,
        )
        request_items = [
            _RequestDispatchItem(
                request_hex=request_hex,
                skipped_response=step.skipped_response,
            )
            for request_hex in request_sequence
        ]
        base_request_hex = request_items[0].request_hex
        previous_response_hex = run.trace[-1].get("response_hex") if run.trace else None

        if step.before_hook is not None:
            request_items, hook_assertions = self._apply_before_hook(
                step.before_hook,
                base_request_hex,
                previous_response_hex,
                variables,
                run.trace,
                flow_dir,
                flow_path,
                default_skipped_response=step.skipped_response,
            )
            self._handle_assertion_results(
                run,
                step,
                hook_assertions,
                phase="before_hook",
            )

        check_each_response = (
            isinstance(step, TransferStep)
            and step.transfer_data.check_each_response
            and self._has_expect_response_matcher(step.expect)
        )

        response_hex: str | None = None
        sent_count = 0
        last_request_hex = ""
        for base_index, request_item in enumerate(request_items):
            per_message_items = [request_item]
            if step.message_hook is not None:
                per_message_items, hook_assertions = self._apply_message_hook(
                    step.message_hook,
                    request_item.request_hex,
                    previous_response_hex,
                    variables,
                    run.trace,
                    flow_dir,
                    flow_path,
                    message_index=base_index,
                    message_total=len(request_items),
                    step_name=step.name,
                    default_skipped_response=request_item.skipped_response,
                )
                self._handle_assertion_results(
                    run,
                    step,
                    hook_assertions,
                    phase="message_hook",
                )

            for dispatch_item in per_message_items:
                request_hex = dispatch_item.request_hex
                skipped_response = dispatch_item.skipped_response
                hook_message_total = len(request_items)
                if skipped_response:
                    await self._uds_client.send_no_response(
                        request_hex,
                        addressing_mode=addressing_mode,
                    )
                    response_hex = None
                else:
                    response = await self._uds_client.send(
                        request_hex,
                        timeout_ms=step.timeout_ms,
                        addressing_mode=addressing_mode,
                    )
                    response_hex = str(response["response_hex"])
                    previous_response_hex = response_hex

                last_request_hex = request_hex

                item: dict[str, Any] = {
                    "step": step.name,
                    "request_hex": request_hex,
                    "response_hex": response_hex,
                    "skipped_response": skipped_response,
                    "request_index": sent_count,
                    "request_total": len(request_items),
                    "repeat_index": repeat_index,
                    "sub_flow_depth": depth,
                    "sub_flow_name": sub_flow_name,
                    "addressing_mode": addressing_mode,
                }
                run.trace.append(item)
                self._event_store.append(LogEvent(kind=EventKind.FLOW_STEP, payload=item))

                if step.can_tx_hook is not None:
                    can_frames, hook_assertions = self._apply_can_tx_hook(
                        step.can_tx_hook,
                        request_hex=request_hex,
                        response_hex=response_hex,
                        variables=variables,
                        trace=run.trace,
                        flow_dir=flow_dir,
                        flow_path=flow_path,
                        message_index=base_index,
                        message_total=hook_message_total,
                        step_name=step.name,
                        skipped_response=skipped_response,
                        addressing_mode=addressing_mode,
                    )
                    self._handle_assertion_results(
                        run,
                        step,
                        hook_assertions,
                        phase="can_tx_hook",
                        request_hex=request_hex,
                        response_hex=response_hex,
                    )
                    if can_frames:
                        await self._uds_client.send_can_frames(
                            [
                                {
                                    "arbitration_id": frame.arbitration_id,
                                    "data_hex": frame.data_hex,
                                    "is_extended_id": frame.is_extended_id,
                                }
                                for frame in can_frames
                            ]
                        )

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

        request_hex = last_request_hex

        if step.after_hook is not None:
            response_hex, hook_assertions = self._apply_after_hook(
                step.after_hook,
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

    # -----------------------------------------------------------------
    # Hook application wrappers
    # -----------------------------------------------------------------

    def _apply_before_hook(
        self,
        hook: HookConfig,
        request_hex: str,
        response_hex: str | None,
        variables: dict[str, Any],
        trace: list[dict[str, Any]],
        flow_dir: Path,
        flow_path: Path | None,
        *,
        default_skipped_response: bool,
    ) -> tuple[list[_RequestDispatchItem], list[_AssertionResult]]:
        context_variables = dict(variables)
        context = self._build_hook_context(
            request_hex=request_hex,
            response_hex=response_hex,
            variables=context_variables,
            trace=trace,
            flow_dir=flow_dir,
            flow_path=flow_path,
        )
        raw_updates, assertions = self._run_hook(hook, context)
        parsed = _validate_hook_return(BeforeHookReturn, raw_updates, phase="before_hook")

        updated_sequence = self._resolve_request_dispatch_items(
            parsed,
            default_request_hex=request_hex,
            default_skipped_response=default_skipped_response,
        )

        self._apply_variable_updates(variables, context_variables, parsed.variables)
        return updated_sequence, assertions

    def _apply_message_hook(
        self,
        hook: HookConfig,
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
        default_skipped_response: bool,
    ) -> tuple[list[_RequestDispatchItem], list[_AssertionResult]]:
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
        raw_updates, assertions = self._run_hook(hook, context)
        parsed = _validate_hook_return(MessageHookReturn, raw_updates, phase="message_hook")

        updated_sequence = self._resolve_request_dispatch_items(
            parsed,
            default_request_hex=request_hex,
            default_skipped_response=default_skipped_response,
        )

        self._apply_variable_updates(variables, context_variables, parsed.variables)
        return updated_sequence, assertions

    def _apply_after_hook(
        self,
        hook: HookConfig,
        request_hex: str,
        response_hex: str | None,
        variables: dict[str, Any],
        trace: list[dict[str, Any]],
        flow_dir: Path,
        flow_path: Path | None,
    ) -> tuple[str | None, list[_AssertionResult]]:
        context_variables = dict(variables)
        context = self._build_hook_context(
            request_hex=request_hex,
            response_hex=response_hex,
            variables=context_variables,
            trace=trace,
            flow_dir=flow_dir,
            flow_path=flow_path,
        )
        raw_updates, assertions = self._run_hook(hook, context)
        parsed = _validate_hook_return(AfterHookReturn, raw_updates, phase="after_hook")

        updated = parsed.response_hex if parsed.response_hex is not None else response_hex
        self._apply_variable_updates(variables, context_variables, parsed.variables)
        return updated, assertions

    def _apply_can_tx_hook(
        self,
        hook: HookConfig,
        *,
        request_hex: str,
        response_hex: str | None,
        variables: dict[str, Any],
        trace: list[dict[str, Any]],
        flow_dir: Path,
        flow_path: Path | None,
        message_index: int,
        message_total: int,
        step_name: str,
        skipped_response: bool,
        addressing_mode: Literal["physical", "functional"],
    ) -> tuple[list[_CanFrameDispatchItem], list[_AssertionResult]]:
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
            skipped_response=skipped_response,
            addressing_mode=addressing_mode,
        )
        raw_updates, assertions = self._run_hook(hook, context)
        parsed = _validate_hook_return(CanTxHookReturn, raw_updates, phase="can_tx_hook")
        self._apply_variable_updates(variables, context_variables, parsed.variables)

        frames = [
            _CanFrameDispatchItem(
                arbitration_id=frame.arbitration_id,
                data_hex=_normalize_hex(
                    frame.data_hex, field_name="can_frames[].data_hex"
                ).upper(),
                is_extended_id=frame.is_extended_id,
            )
            for frame in parsed.can_frames
        ]
        return frames, assertions

    def _resolve_request_dispatch_items(
        self,
        parsed: BeforeHookReturn | MessageHookReturn,
        *,
        default_request_hex: str,
        default_skipped_response: bool,
    ) -> list[_RequestDispatchItem]:
        if parsed.request_items is not None:
            if not parsed.request_items:
                raise ValueError("hook output request_items must be non-empty")
            return [
                _RequestDispatchItem(
                    request_hex=item.request_hex,
                    skipped_response=(
                        item.skipped_response
                        if item.skipped_response is not None
                        else default_skipped_response
                    ),
                )
                for item in parsed.request_items
            ]

        if parsed.request_sequence_hex is not None:
            if not parsed.request_sequence_hex:
                raise ValueError("hook output request_sequence_hex must be non-empty")
            return [
                _RequestDispatchItem(
                    request_hex=request_hex,
                    skipped_response=default_skipped_response,
                )
                for request_hex in parsed.request_sequence_hex
            ]

        request_hex = (
            parsed.request_hex if parsed.request_hex is not None else default_request_hex
        )
        return [
            _RequestDispatchItem(
                request_hex=request_hex,
                skipped_response=default_skipped_response,
            )
        ]

    def _run_hook(
        self,
        hook: HookConfig,
        context: dict[str, Any],
    ) -> tuple[dict[str, Any], list[_AssertionResult]]:
        updates: dict[str, Any] = {}
        assertions = _HookAssertions(context)
        hook_context = dict(context)
        hook_context["assertions"] = assertions
        if hook.script and hook.function:
            updates = self._runtime.run_hook(
                script_path=hook.script,
                function_name=hook.function,
                context=hook_context,
            )
        elif hook.inline:
            updates = self._runtime.run_snippet(code=hook.inline, context=hook_context)
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
        skipped_response: bool | None = None,
        addressing_mode: Literal["physical", "functional"] | None = None,
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
        if skipped_response is not None:
            context["skipped_response"] = skipped_response
        if addressing_mode is not None:
            context["addressing_mode"] = addressing_mode
        return context

    def _apply_variable_updates(
        self,
        variables: dict[str, Any],
        context_variables: dict[str, Any],
        updated_variables: dict[str, Any] | None,
    ) -> None:
        original_variables = dict(variables)
        effective = updated_variables if updated_variables is not None else context_variables
        if effective != original_variables:
            variables.clear()
            variables.update(effective)

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
        step: UdsStep | TransferStep,
        variables: dict[str, Any],
        trace: list[dict[str, Any]],
        flow_dir: Path,
        flow_path: Path | None,
    ) -> list[str]:
        if isinstance(step, UdsStep):
            return [step.request]

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
        raw_updates, assertions = self._run_hook(hook, context)
        if assertions:
            raise ValueError("segments_hook assertions are not supported; use step hooks")
        parsed = _validate_hook_return(SegmentsHookReturn, raw_updates, phase="segments_hook")
        self._apply_variable_updates(variables, context_variables, parsed.variables)

        if not parsed.segments:
            raise ValueError("segments_hook result.segments must not be empty")
        return list(parsed.segments)

    def _evaluate_step_assertions(
        self,
        assertions: list[StepAssertion],
        *,
        request_hex: str,
        response_hex: str | None,
    ) -> list[_AssertionResult]:
        results: list[_AssertionResult] = []
        for assertion in assertions:
            try:
                actual_hex = response_hex if assertion.source == "response_hex" else request_hex
                if actual_hex is None:
                    results.append(
                        _AssertionResult(
                            ok=False,
                            on_fail=assertion.on_fail,
                            name=assertion.name,
                            message=(
                                assertion.message
                                or "response_hex is missing for assertion; request used skipped_response=true"
                            ),
                        )
                    )
                    continue
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
        response_hex: str | None,
    ) -> list[_AssertionResult]:
        if not self._has_expect_response_matcher(expect):
            return []

        if response_hex is None:
            return [
                _AssertionResult(
                    ok=False,
                    on_fail=expect.response_on_fail,
                    name="expect.response_missing",
                    message=(
                        "expect response matcher cannot run: response_hex is missing "
                        "(request used skipped_response=true)"
                    ),
                )
            ]

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
                    detail={"expected": expected, "actual": normalized},
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
                    detail={"expected": expected, "actual": normalized},
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
                    detail={"expected": pattern, "actual": normalized},
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
            detail_record: dict[str, Any] = {
                "step": step.name,
                "phase": phase,
                "name": result.name,
                "on_fail": result.on_fail,
                "ok": result.ok,
                "message": result.message,
                "request_hex": request_hex,
                "response_hex": response_hex,
                "detail": result.detail,
            }
            run.assertion_details.append(detail_record)

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
                    f"step {step.name} [{phase}] fatal assertion failed: {result.message}",
                    detail=result.detail,
                )
            raise FlowAssertionError(
                f"step {step.name} [{phase}] assertion failed: {result.message}",
                detail=result.detail,
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
    if step.addressing_mode != "inherit":
        return step.addressing_mode
    return flow.default_addressing_mode


def _resolve_tester_present_mode(
    step: FlowStep,
    flow: FlowDefinition,
) -> Literal["physical", "functional"] | None:
    """Resolve the effective TesterPresent addressing mode for a step.

    Returns None when TP should NOT run for this step. Returns "physical" or
    "functional" when TP should run with that addressing.

    - ``tester_present="off"`` → TP disabled for this step
    - ``tester_present="physical"|"functional"`` → step overrides flow; use that mode
    - ``tester_present="inherit"`` → follow flow's ``tester_present_policy``:
        - "during_flow" → run with ``flow.tester_present_addressing_mode``
        - "breakpoint_only" or "off" → None (only runs during breakpoint, handled separately)
    """
    if step.tester_present == "off":
        return None
    if step.tester_present in ("physical", "functional"):
        return step.tester_present
    # inherit
    if flow.tester_present_policy == "during_flow":
        return flow.tester_present_addressing_mode
    return None


def _validate_hook_return(
    model_cls: type,
    raw: dict[str, Any],
    *,
    phase: str,
) -> Any:
    try:
        return model_cls.model_validate(raw or {})
    except ValidationError as exc:
        raise ValueError(f"{phase} hook returned invalid payload: {exc}") from exc
