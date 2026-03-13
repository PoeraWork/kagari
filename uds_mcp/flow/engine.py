from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from uds_mcp.flow.schema import (
    FlowDefinition,
    FlowStep,
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

    async def _run_flow(self, run: FlowRun, flow: FlowDefinition) -> None:
        flow_path = self._flow_sources.get(flow.name)
        flow_dir = flow_path.parent if flow_path is not None else Path.cwd().resolve()
        variables = _resolve_flow_variables(flow.variables, flow_dir=flow_dir)
        previous_response_hex: str | None = None
        flow_owner_active = False
        try:
            if flow.tester_present_policy == "during_flow":
                await self._uds_client.start_tester_present_owner("flow-run")
                flow_owner_active = True

            for step in flow.steps:
                step_owner_active = False
                flow_owner_suspended = False
                if run.stop_requested:
                    run.status = FlowStatus.STOPPED
                    self._log_state(run)
                    return

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

                    request_sequence = self._build_step_request_sequence(
                        step,
                        variables,
                        run.trace,
                        flow_dir,
                        flow_path,
                    )
                    base_request_hex = request_sequence[0]
                    if step.before_hook:
                        request_sequence = self._apply_before_hook(
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

                    expected_prefix = None
                    if step.expect and step.expect.response_prefix:
                        expected_prefix = step.expect.response_prefix.upper()
                    check_each_response = (
                        step.transfer_data is not None
                        and step.transfer_data.check_each_response
                        and expected_prefix is not None
                    )

                    response_hex = ""
                    sent_count = 0
                    for base_index, request_hex in enumerate(request_sequence):
                        per_message_sequence = [request_hex]
                        if step.message_hook:
                            per_message_sequence = self._apply_message_hook(
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

                        for request_hex in per_message_sequence:
                            response = await self._uds_client.send(
                                request_hex, timeout_ms=step.timeout_ms
                            )
                            response_hex = str(response["response_hex"])

                            item = {
                                "step": step.name,
                                "request_hex": request_hex,
                                "response_hex": response_hex,
                                "request_index": sent_count,
                                "request_total": len(request_sequence),
                            }
                            run.trace.append(item)
                            self._event_store.append(
                                LogEvent(kind=EventKind.FLOW_STEP, payload=item)
                            )
                            previous_response_hex = response_hex

                            if check_each_response and not response_hex.startswith(expected_prefix):
                                raise ValueError(
                                    "step "
                                    f"{step.name}: transfer_data expect prefix {expected_prefix}, "
                                    f"got {response_hex} at request_index {sent_count}"
                                )

                            sent_count += 1

                    if step.after_hook:
                        response_hex = self._apply_after_hook(
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

                    if (
                        expected_prefix is not None
                        and not check_each_response
                        and not response_hex.startswith(expected_prefix)
                    ):
                        raise ValueError(
                            f"step {step.name}: expect prefix {expected_prefix}, got {response_hex}"
                        )
                finally:
                    if step_owner_active:
                        await self._uds_client.stop_tester_present_owner("flow-step")
                    if flow_owner_suspended:
                        await self._uds_client.start_tester_present_owner("flow-run")

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
    ) -> list[str]:
        context_variables = dict(variables)
        context = self._build_hook_context(
            request_hex=request_hex,
            response_hex=response_hex,
            variables=context_variables,
            trace=trace,
            flow_dir=flow_dir,
            flow_path=flow_path,
        )
        updates = self._run_hook(script_path, function_name, snippet, context)

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
        return updated_sequence

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
    ) -> list[str]:
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
        updates = self._run_hook(script_path, function_name, snippet, context)

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
        return updated_sequence

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
    ) -> str:
        context_variables = dict(variables)
        context = self._build_hook_context(
            request_hex=request_hex,
            response_hex=response_hex,
            variables=context_variables,
            trace=trace,
            flow_dir=flow_dir,
            flow_path=flow_path,
        )
        updates = self._run_hook(script_path, function_name, snippet, context)

        updated = updates.get("response_hex", response_hex)
        if not isinstance(updated, str):
            raise TypeError("hook output response_hex must be str")

        self._apply_variable_updates(variables, context_variables, updates)
        return updated

    def _run_hook(
        self,
        script_path: str | None,
        function_name: str | None,
        snippet: str | None,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        if script_path and function_name:
            updates = self._runtime.run_hook(
                script_path=script_path,
                function_name=function_name,
                context=context,
            )
        elif snippet:
            updates = self._runtime.run_snippet(code=snippet, context=context)
        return updates

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
        updates = self._run_hook(
            hook.script_path,
            hook.function_name,
            hook.snippet,
            context,
        )
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
