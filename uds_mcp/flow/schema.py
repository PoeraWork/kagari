from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any, Literal, Union

import yaml
from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Hook configuration
# ---------------------------------------------------------------------------


class HookConfig(BaseModel):
    """Reference to a user-provided hook.

    Exactly one of ``script`` + ``function`` (external script) or ``inline``
    (inline snippet) must be provided.
    """

    script: str | None = None
    function: str | None = None
    inline: str | None = None

    @model_validator(mode="after")
    def _validate_source(self) -> HookConfig:
        has_script = self.script is not None
        has_function = self.function is not None
        has_inline = self.inline is not None

        if has_inline and (has_script or has_function):
            raise ValueError("HookConfig: inline cannot be combined with script/function")
        if has_script != has_function:
            raise ValueError("HookConfig: script and function must both be set or both be unset")
        if not has_inline and not has_script:
            raise ValueError("HookConfig: provide either inline or script+function")
        return self


# ---------------------------------------------------------------------------
# Hook return models (typed per phase)
# ---------------------------------------------------------------------------


class RequestItem(BaseModel):
    request_hex: str
    skipped_response: bool | None = None


class CanFrameSpec(BaseModel):
    arbitration_id: int = Field(ge=0)
    data_hex: str
    is_extended_id: bool = False


class BeforeHookReturn(BaseModel):
    model_config = {"extra": "forbid"}

    request_hex: str | None = None
    request_sequence_hex: list[str] | None = None
    request_items: list[RequestItem] | None = None
    variables: dict[str, Any] | None = None


class MessageHookReturn(BaseModel):
    model_config = {"extra": "forbid"}

    request_hex: str | None = None
    request_sequence_hex: list[str] | None = None
    request_items: list[RequestItem] | None = None
    variables: dict[str, Any] | None = None


class AfterHookReturn(BaseModel):
    model_config = {"extra": "forbid"}

    response_hex: str | None = None
    variables: dict[str, Any] | None = None


class CanTxHookReturn(BaseModel):
    model_config = {"extra": "forbid"}

    can_frames: list[CanFrameSpec] = Field(default_factory=list)
    variables: dict[str, Any] | None = None


class SegmentsHookReturn(BaseModel):
    model_config = {"extra": "forbid"}

    segments: list[TransferSegment]  # forward ref resolved below
    variables: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Assertions & expect
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Transfer data (shared by TransferStep)
# ---------------------------------------------------------------------------


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


# Resolve forward reference for SegmentsHookReturn.segments
SegmentsHookReturn.model_rebuild()


# ---------------------------------------------------------------------------
# Step kinds (discriminated union)
# ---------------------------------------------------------------------------


StepTesterPresent = Literal["inherit", "off", "physical", "functional"]
StepAddressingMode = Literal["inherit", "physical", "functional"]


class _BaseStep(BaseModel):
    name: str
    repeat: int = Field(default=1, ge=1)
    timeout_ms: int = 1000
    delay_ms: int = Field(
        default=0,
        ge=0,
        description="Non-blocking delay after step completion",
    )
    breakpoint: bool = False
    tester_present: StepTesterPresent = "inherit"
    addressing_mode: StepAddressingMode = "inherit"


class UdsStep(_BaseStep):
    kind: Literal["uds"] = "uds"
    request: str = Field(description="UDS request hex string")
    skipped_response: bool = False
    expect: StepExpect | None = None
    before_hook: HookConfig | None = None
    message_hook: HookConfig | None = None
    can_tx_hook: HookConfig | None = None
    after_hook: HookConfig | None = None


class TransferStep(_BaseStep):
    kind: Literal["transfer"] = "transfer"
    transfer_data: TransferDataConfig
    skipped_response: bool = False
    expect: StepExpect | None = None
    before_hook: HookConfig | None = None
    message_hook: HookConfig | None = None
    can_tx_hook: HookConfig | None = None
    after_hook: HookConfig | None = None


class SubflowStep(_BaseStep):
    kind: Literal["subflow"] = "subflow"
    subflow: str = Field(description="Path to a sub-flow YAML file")


class CanStep(_BaseStep):
    """Standalone CAN frame emission step (no UDS request)."""

    kind: Literal["can"] = "can"
    can_tx_hook: HookConfig = Field(description="Hook producing CAN frames to send")


class WaitStep(_BaseStep):
    kind: Literal["wait"] = "wait"
    delay_ms: int = Field(ge=1, description="Wait duration in ms; must be > 0")


FlowStep = Annotated[
    Union[UdsStep, TransferStep, SubflowStep, CanStep, WaitStep],
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# Flow definition
# ---------------------------------------------------------------------------


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
        if isinstance(step, SubflowStep):
            step.subflow = _resolve_path(step.subflow, base_dir)
        if isinstance(step, UdsStep | TransferStep):
            _resolve_hook_script(step.before_hook, base_dir)
            _resolve_hook_script(step.message_hook, base_dir)
            _resolve_hook_script(step.can_tx_hook, base_dir)
            _resolve_hook_script(step.after_hook, base_dir)
        if isinstance(step, CanStep):
            _resolve_hook_script(step.can_tx_hook, base_dir)
        if isinstance(step, TransferStep):
            _resolve_hook_script(step.transfer_data.segments_hook, base_dir)
    return flow


def dump_flow_yaml(path: Path, flow: FlowDefinition) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    base_dir = path.resolve().parent
    data = flow.model_dump(mode="json")
    for step_data in data.get("steps", []):
        _relativize_subflow_path(step_data, base_dir)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=False), encoding="utf-8")


def _resolve_path(raw: str, base_dir: Path) -> str:
    target = Path(raw)
    if target.is_absolute():
        return str(target.resolve())
    return str((base_dir / target).resolve())


def _resolve_hook_script(hook: HookConfig | None, base_dir: Path) -> None:
    if hook is None or hook.script is None:
        return
    hook.script = _resolve_path(hook.script, base_dir)


def _relativize_subflow_path(step_data: dict[str, Any], base_dir: Path) -> None:
    """Convert a subflow step's absolute path back to relative for readable YAML."""
    if step_data.get("kind") != "subflow":
        return
    subflow = step_data.get("subflow")
    if subflow is None:
        return
    try:
        step_data["subflow"] = str(Path(subflow).relative_to(base_dir))
    except ValueError:
        step_data["subflow"] = os.path.relpath(subflow, base_dir)
