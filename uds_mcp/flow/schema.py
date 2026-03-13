from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class HookConfig(BaseModel):
    script_path: str | None = None
    function_name: str | None = None
    snippet: str | None = None


class StepExpect(BaseModel):
    response_prefix: str | None = None


class TransferSegment(BaseModel):
    address: int
    data_hex: str


class TransferDataConfig(BaseModel):
    segments: list[TransferSegment] = Field(default_factory=list)
    segments_hook: HookConfig | None = None
    chunk_size: int = 256
    block_counter_start: int = 1
    request_prefix_hex: str = "36"

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
    timeout_ms: int = 1000
    expect: StepExpect | None = None
    breakpoint: bool = False
    tester_present: Literal["inherit", "on", "off"] = "inherit"
    transfer_data: TransferDataConfig | None = None
    before_hook: HookConfig | None = None
    message_hook: HookConfig | None = None
    after_hook: HookConfig | None = None

    @model_validator(mode="after")
    def _validate_request_source(self) -> FlowStep:
        if self.send is None and self.transfer_data is None:
            raise ValueError("step requires send or transfer_data")
        return self


class FlowDefinition(BaseModel):
    name: str
    version: str = "1.0"
    tester_present_policy: Literal["breakpoint_only", "during_flow", "off"] = "breakpoint_only"
    variables: dict[str, Any] = Field(default_factory=dict)
    steps: list[FlowStep]


def load_flow_yaml(path: Path) -> FlowDefinition:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    flow = FlowDefinition.model_validate(data)
    base_dir = path.resolve().parent
    for step in flow.steps:
        _resolve_hook_script_path(step.before_hook, base_dir)
        _resolve_hook_script_path(step.message_hook, base_dir)
        _resolve_hook_script_path(step.after_hook, base_dir)
        if step.transfer_data is not None:
            _resolve_hook_script_path(step.transfer_data.segments_hook, base_dir)
    return flow


def dump_flow_yaml(path: Path, flow: FlowDefinition) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = flow.model_dump(mode="json")
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=False), encoding="utf-8")


def _resolve_hook_script_path(hook: HookConfig | None, base_dir: Path) -> None:
    if hook is None or hook.script_path is None:
        return
    target = Path(hook.script_path)
    if target.is_absolute():
        hook.script_path = str(target.resolve())
        return
    hook.script_path = str((base_dir / target).resolve())
