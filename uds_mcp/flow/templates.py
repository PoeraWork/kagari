from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import yaml

from uds_mcp.flow.schema import (
    FlowDefinition,
    HookConfig,
    StepExpect,
    UdsStep,
    dump_flow_yaml,
)

if TYPE_CHECKING:
    from pathlib import Path

_FLOW_PRESETS = ("minimal", "session_did_read")


def list_flow_presets() -> list[str]:
    return list(_FLOW_PRESETS)


def create_flow_template(
    flow_name: str,
    *,
    preset: str = "session_did_read",
    include_dynamic_hook: bool = True,
    tester_present_policy: Literal["breakpoint_only", "during_flow", "off"] = "breakpoint_only",
    default_step_tester_present: Literal[
        "inherit", "off", "physical", "functional"
    ] = "inherit",
    default_addressing_mode: Literal["physical", "functional"] = "physical",
) -> FlowDefinition:
    if preset not in _FLOW_PRESETS:
        available = ", ".join(_FLOW_PRESETS)
        raise ValueError(f"unsupported preset: {preset}. available: {available}")

    if preset == "minimal":
        steps = [
            UdsStep(
                name="read_data_by_identifier",
                request="22F190",
                timeout_ms=1200,
                expect=StepExpect(response_prefix="62F190"),
                tester_present=default_step_tester_present,
            )
        ]
        return FlowDefinition(
            name=flow_name,
            tester_present_policy=tester_present_policy,
            default_addressing_mode=default_addressing_mode,
            steps=steps,
        )

    step_with_hook = UdsStep(
        name="read_did_dynamic",
        request="22F190",
        timeout_ms=1200,
        expect=StepExpect(response_prefix="62F190"),
        tester_present=default_step_tester_present,
    )
    if include_dynamic_hook:
        step_with_hook.before_hook = HookConfig(
            script="../extensions/dynamic_payload.py",
            function="build_request",
        )

    return FlowDefinition(
        name=flow_name,
        tester_present_policy=tester_present_policy,
        default_addressing_mode=default_addressing_mode,
        variables={"did": "F190"},
        steps=[
            UdsStep(
                name="enter_extended_session",
                request="1003",
                timeout_ms=1200,
                expect=StepExpect(response_prefix="5003"),
                tester_present=default_step_tester_present,
            ),
            step_with_hook,
        ],
    )


def render_flow_template_yaml(flow: FlowDefinition) -> str:
    data = flow.model_dump(mode="json")
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=False)


def init_flow_template(
    flow_name: str,
    *,
    preset: str = "session_did_read",
    include_dynamic_hook: bool = True,
    tester_present_policy: Literal["breakpoint_only", "during_flow", "off"] = "breakpoint_only",
    default_step_tester_present: Literal[
        "inherit", "off", "physical", "functional"
    ] = "inherit",
    default_addressing_mode: Literal["physical", "functional"] = "physical",
    path: Path | None = None,
    overwrite: bool = False,
) -> tuple[FlowDefinition, str]:
    flow = create_flow_template(
        flow_name,
        preset=preset,
        include_dynamic_hook=include_dynamic_hook,
        tester_present_policy=tester_present_policy,
        default_step_tester_present=default_step_tester_present,
        default_addressing_mode=default_addressing_mode,
    )
    yaml_text = render_flow_template_yaml(flow)

    if path is not None:
        if path.exists() and not overwrite:
            raise FileExistsError(f"file exists: {path}")
        dump_flow_yaml(path, flow)

    return flow, yaml_text
