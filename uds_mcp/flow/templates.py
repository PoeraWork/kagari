from __future__ import annotations

from pathlib import Path

import yaml

from uds_mcp.flow.schema import FlowDefinition, FlowStep, HookConfig, StepExpect, dump_flow_yaml


_FLOW_PRESETS = ("minimal", "session_did_read")


def list_flow_presets() -> list[str]:
    return list(_FLOW_PRESETS)


def create_flow_template(
    flow_name: str,
    *,
    preset: str = "session_did_read",
    include_dynamic_hook: bool = True,
) -> FlowDefinition:
    if preset not in _FLOW_PRESETS:
        available = ", ".join(_FLOW_PRESETS)
        raise ValueError(f"unsupported preset: {preset}. available: {available}")

    if preset == "minimal":
        steps = [
            FlowStep(
                name="read_data_by_identifier",
                send="22F190",
                timeout_ms=1200,
                expect=StepExpect(response_prefix="62F190"),
            )
        ]
        return FlowDefinition(name=flow_name, steps=steps)

    step_with_hook = FlowStep(
        name="read_did_dynamic",
        send="22F190",
        timeout_ms=1200,
        expect=StepExpect(response_prefix="62F190"),
    )
    if include_dynamic_hook:
        step_with_hook.before_hook = HookConfig(
            script_path="examples/extensions/dynamic_payload.py",
            function_name="build_request",
        )

    return FlowDefinition(
        name=flow_name,
        variables={"did": "F190"},
        steps=[
            FlowStep(
                name="enter_extended_session",
                send="1003",
                timeout_ms=1200,
                expect=StepExpect(response_prefix="5003"),
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
    path: Path | None = None,
    overwrite: bool = False,
) -> tuple[FlowDefinition, str]:
    flow = create_flow_template(
        flow_name,
        preset=preset,
        include_dynamic_hook=include_dynamic_hook,
    )
    yaml_text = render_flow_template_yaml(flow)

    if path is not None:
        if path.exists() and not overwrite:
            raise FileExistsError(f"file exists: {path}")
        dump_flow_yaml(path, flow)

    return flow, yaml_text