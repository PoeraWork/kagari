from __future__ import annotations

from pathlib import Path

import pytest

from uds_mcp.flow.schema import FlowDefinition
from uds_mcp.flow.templates import create_flow_template, init_flow_template, list_flow_presets


def test_list_flow_presets() -> None:
    presets = list_flow_presets()
    assert "minimal" in presets
    assert "session_did_read" in presets


def test_create_flow_template_session_did_read() -> None:
    flow = create_flow_template("demo")
    assert flow.name == "demo"
    assert flow.tester_present_policy == "breakpoint_only"
    assert flow.steps[0].name == "enter_extended_session"
    assert flow.steps[0].tester_present == "inherit"
    assert flow.steps[1].before_hook is not None
    assert flow.steps[1].before_hook.script_path == "../extensions/dynamic_payload.py"


def test_create_flow_template_without_hook() -> None:
    flow = create_flow_template("demo", include_dynamic_hook=False)
    assert flow.steps[1].before_hook is None


def test_create_flow_template_with_tester_present_options() -> None:
    flow = create_flow_template(
        "demo",
        tester_present_policy="during_flow",
        default_step_tester_present="on",
    )
    assert flow.tester_present_policy == "during_flow"
    assert all(step.tester_present == "on" for step in flow.steps)


def test_init_flow_template_write_file(tmp_path: Path) -> None:
    path = tmp_path / "demo.yaml"
    _, yaml_text = init_flow_template("demo", path=path)

    assert path.exists()
    assert "name: demo" in yaml_text
    assert "tester_present_policy: breakpoint_only" in yaml_text


def test_init_flow_template_respects_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "demo.yaml"
    path.write_text("name: old\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        init_flow_template("demo", path=path)

    _, yaml_text = init_flow_template("demo", path=path, overwrite=True)
    assert "name: demo" in yaml_text


def test_create_flow_template_rejects_unknown_preset() -> None:
    with pytest.raises(ValueError):
        create_flow_template("demo", preset="unknown")


# ---------------------------------------------------------------------------
# Tests for default_addressing_mode in create_flow_template
# Requirements: 9.6
# ---------------------------------------------------------------------------


def test_template_default_addressing_mode_physical() -> None:
    """Default addressing mode is 'physical' when not specified."""
    flow = create_flow_template("demo")
    assert flow.default_addressing_mode == "physical"


def test_template_default_addressing_mode_functional() -> None:
    """Passing 'functional' as default_addressing_mode works correctly."""
    flow = create_flow_template("demo", default_addressing_mode="functional")
    assert flow.default_addressing_mode == "functional"


def test_template_minimal_preset_default_addressing_mode() -> None:
    """Minimal preset also respects default_addressing_mode parameter."""
    flow = create_flow_template("demo", preset="minimal", default_addressing_mode="functional")
    assert flow.default_addressing_mode == "functional"


def test_init_flow_template_default_addressing_mode(tmp_path: Path) -> None:
    """init_flow_template passes default_addressing_mode through to the flow."""
    path = tmp_path / "demo.yaml"
    flow, _yaml_text = init_flow_template("demo", path=path, default_addressing_mode="functional")
    assert flow.default_addressing_mode == "functional"


# ---------------------------------------------------------------------------
# Tests for inline registration sub_flow path validation
# Requirements: 1.6
# ---------------------------------------------------------------------------


def test_register_inline_rejects_relative_sub_flow() -> None:
    """Relative sub_flow path raises ValueError in inline registration validation."""
    flow = FlowDefinition.model_validate(
        {
            "name": "test_flow",
            "steps": [{"name": "sub_step", "sub_flow": "flows/child.yaml"}],
        }
    )
    with pytest.raises(ValueError, match="sub_flow path must be absolute"):
        for step in flow.steps:
            if step.sub_flow is not None and not Path(step.sub_flow).is_absolute():
                raise ValueError(
                    f"sub_flow path must be absolute when using "
                    f"flow_register_inline: {step.sub_flow}"
                )


def test_register_inline_accepts_absolute_sub_flow() -> None:
    """Absolute sub_flow path is accepted in inline registration validation."""
    # Use a platform-aware absolute path
    abs_path = (
        str(Path("C:/flows/child.yaml")) if not Path("/x").is_absolute() else "/flows/child.yaml"
    )
    flow = FlowDefinition.model_validate(
        {
            "name": "test_flow",
            "steps": [{"name": "sub_step", "sub_flow": abs_path}],
        }
    )
    # Should NOT raise
    for step in flow.steps:
        if step.sub_flow is not None and not Path(step.sub_flow).is_absolute():
            raise ValueError(
                f"sub_flow path must be absolute when using flow_register_inline: {step.sub_flow}"
            )
