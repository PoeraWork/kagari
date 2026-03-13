from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field


class HookConfig(BaseModel):
    script_path: str | None = None
    function_name: str | None = None
    snippet: str | None = None


class StepExpect(BaseModel):
    response_prefix: str | None = None


class FlowStep(BaseModel):
    name: str
    send: str = Field(description="UDS request hex string")
    timeout_ms: int = 1000
    expect: StepExpect | None = None
    breakpoint: bool = False
    tester_present: Literal["inherit", "on", "off"] = "inherit"
    before_hook: HookConfig | None = None
    after_hook: HookConfig | None = None


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
