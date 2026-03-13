from __future__ import annotations

from pathlib import Path

import pytest

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
    assert flow.steps[1].before_hook.script_path == "examples/extensions/dynamic_payload.py"


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
