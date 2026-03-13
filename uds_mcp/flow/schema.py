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


class TransferDataConfig(BaseModel):
    data_hex: str | None = None
    data_file: str | None = None
    chunk_size: int = 256
    block_counter_start: int = 1
    request_prefix_hex: str = "36"

    @model_validator(mode="after")
    def _validate_source(self) -> TransferDataConfig:
        has_hex = bool(self.data_hex)
        has_file = bool(self.data_file)
        if has_hex == has_file:
            raise ValueError("transfer_data requires exactly one of data_hex or data_file")
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
    return FlowDefinition.model_validate(data)


def dump_flow_yaml(path: Path, flow: FlowDefinition) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = flow.model_dump(mode="json")
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=False), encoding="utf-8")
