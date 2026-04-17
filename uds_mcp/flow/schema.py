from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class HookConfig(BaseModel):
    script_path: str | None = None
    function_name: str | None = None
    snippet: str | None = None


class StepAssertion(BaseModel):
    name: str | None = None
    kind: Literal["hex_prefix", "hex_equals", "byte_eq", "bytes_int_range"]
    source: Literal["response_hex", "request_hex"] = "response_hex"
    on_fail: Literal["record", "fail", "fatal"] = "fail"
    message: str | None = None

    prefix: str | None = None
    expected_hex: str | None = None
    index: int | None = Field(default=None, ge=0)
    value: int | None = Field(default=None, ge=0, le=0xFF)
    start: int | None = Field(default=None, ge=0)
    length: int | None = Field(default=None, ge=1)
    min_value: int | None = None
    max_value: int | None = None
    endian: Literal["big", "little"] = "big"

    @model_validator(mode="after")
    def _validate_kind_fields(self) -> StepAssertion:
        if self.kind == "hex_prefix" and self.prefix is None:
            raise ValueError("assertion kind hex_prefix requires prefix")
        if self.kind == "hex_equals" and self.expected_hex is None:
            raise ValueError("assertion kind hex_equals requires expected_hex")
        if self.kind == "byte_eq" and (self.index is None or self.value is None):
            raise ValueError("assertion kind byte_eq requires index and value")
        if self.kind == "bytes_int_range":
            if self.start is None or self.length is None:
                raise ValueError("assertion kind bytes_int_range requires start and length")
            if self.min_value is None and self.max_value is None:
                raise ValueError("assertion kind bytes_int_range requires min_value or max_value")
        return self


class StepExpect(BaseModel):
    response_prefix: str | None = None
    response_regex: str | None = None
    response_equals: str | None = None
    response_on_fail: Literal["record", "fail", "fatal"] = "fatal"
    assertions: list[StepAssertion] = Field(default_factory=list)
    apply_each_response: bool = False

    @model_validator(mode="after")
    def _validate_response_matchers(self) -> StepExpect:
        matcher_count = sum(
            [
                self.response_prefix is not None,
                self.response_regex is not None,
                self.response_equals is not None,
            ]
        )
        if matcher_count > 1:
            raise ValueError(
                "expect response matcher requires at most one of "
                "response_prefix, response_regex, response_equals"
            )
        return self


class TransferSegment(BaseModel):
    address: int
    data_hex: str


class TransferDataConfig(BaseModel):
    segments: list[TransferSegment] = Field(default_factory=list)
    segments_hook: HookConfig | None = None
    chunk_size: int = 256
    block_counter_start: int = 1
    request_prefix_hex: str = "36"
    check_each_response: bool = True

    @model_validator(mode="after")
    def _validate_source(self) -> TransferDataConfig:
        if not self.segments and self.segments_hook is None:
            raise ValueError("transfer_data requires segments or segments_hook")
        if self.chunk_size < 1:
            raise ValueError("transfer_data.chunk_size must be >= 1")
        if not 0 <= self.block_counter_start <= 0xFF:
            raise ValueError("transfer_data.block_counter_start must be in range 0..255")
        return self


class FlowStep(BaseModel):
    name: str
    send: str | None = Field(default=None, description="UDS request hex string")
    skipped_response: bool = False
    sub_flow: str | None = None
    repeat: int = Field(default=1, ge=1)
    timeout_ms: int = 1000
    delay_ms: int = Field(
        default=0,
        ge=0,
        description="Non-blocking delay after step success; if used alone it becomes a wait-only step",
    )
    expect: StepExpect | None = None
    breakpoint: bool = False
    tester_present: Literal["inherit", "on", "off"] = "inherit"
    tester_present_addressing_mode: Literal["inherit", "physical", "functional"] = "inherit"
    addressing_mode: Literal["physical", "functional", "inherit"] = "inherit"
    transfer_data: TransferDataConfig | None = None
    before_hook: HookConfig | None = None
    message_hook: HookConfig | None = None
    can_tx_hook: HookConfig | None = None
    after_hook: HookConfig | None = None

    @model_validator(mode="after")
    def _validate_request_source(self) -> FlowStep:
        sources = [self.send is not None, self.transfer_data is not None, self.sub_flow is not None]
        count = sum(sources)
        if count == 0:
            if self.delay_ms > 0:
                return self
            raise ValueError("step requires exactly one of send, transfer_data, or sub_flow")
        if count > 1:
            raise ValueError("step requires exactly one of send, transfer_data, or sub_flow")
        if self.sub_flow is not None:
            if self.before_hook is not None:
                raise ValueError("sub_flow step must not set before_hook")
            if self.message_hook is not None:
                raise ValueError("sub_flow step must not set message_hook")
            if self.can_tx_hook is not None:
                raise ValueError("sub_flow step must not set can_tx_hook")
            if self.after_hook is not None:
                raise ValueError("sub_flow step must not set after_hook")
            if self.expect is not None:
                raise ValueError("sub_flow step must not set expect")
        return self


class FlowDefinition(BaseModel):
    name: str
    version: str = "1.0"
    repeat: int = Field(default=1, ge=1)
    tester_present_policy: Literal["breakpoint_only", "during_flow", "off"] = "breakpoint_only"
    tester_present_addressing_mode: Literal["physical", "functional"] = "physical"
    default_addressing_mode: Literal["physical", "functional"] = "physical"
    variables: dict[str, Any] = Field(default_factory=dict)
    steps: list[FlowStep]


def load_flow_yaml(path: Path) -> FlowDefinition:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    flow = FlowDefinition.model_validate(data)
    base_dir = path.resolve().parent
    for step in flow.steps:
        _resolve_sub_flow_path(step, base_dir)
        _resolve_hook_script_path(step.before_hook, base_dir)
        _resolve_hook_script_path(step.message_hook, base_dir)
        _resolve_hook_script_path(step.can_tx_hook, base_dir)
        _resolve_hook_script_path(step.after_hook, base_dir)
        if step.transfer_data is not None:
            _resolve_hook_script_path(step.transfer_data.segments_hook, base_dir)
    return flow


def dump_flow_yaml(path: Path, flow: FlowDefinition) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    base_dir = path.resolve().parent
    data = flow.model_dump(mode="json")
    for step_data in data.get("steps", []):
        _relativize_sub_flow_path(step_data, base_dir)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=False), encoding="utf-8")


def _resolve_hook_script_path(hook: HookConfig | None, base_dir: Path) -> None:
    if hook is None or hook.script_path is None:
        return
    target = Path(hook.script_path)
    if target.is_absolute():
        hook.script_path = str(target.resolve())
        return
    hook.script_path = str((base_dir / target).resolve())


def _resolve_sub_flow_path(step: FlowStep, base_dir: Path) -> None:
    """Resolve a step's sub_flow relative path to an absolute path."""
    if step.sub_flow is None:
        return
    target = Path(step.sub_flow)
    if target.is_absolute():
        step.sub_flow = str(target.resolve())
        return
    step.sub_flow = str((base_dir / target).resolve())


def _relativize_sub_flow_path(step_data: dict[str, Any], base_dir: Path) -> None:
    """Convert a step dict's sub_flow absolute path back to a relative path."""
    sub_flow = step_data.get("sub_flow")
    if sub_flow is None:
        return
    try:
        step_data["sub_flow"] = str(Path(sub_flow).relative_to(base_dir))
    except ValueError:
        # Path is not relative to base_dir; use os.path.relpath as fallback
        step_data["sub_flow"] = os.path.relpath(sub_flow, base_dir)
