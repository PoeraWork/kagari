from __future__ import annotations

from pathlib import Path

import pytest

from uds_mcp.flow.schema import FlowDefinition, dump_flow_yaml, load_flow_yaml


def test_dump_and_load_flow_yaml(tmp_path: Path) -> None:
    path = tmp_path / "demo_flow.yaml"
    flow = FlowDefinition(
        name="demo",
        variables={"seed": 1},
        steps=[
            {
                "name": "session",
                "send": "1003",
                "timeout_ms": 1000,
                "expect": {"response_prefix": "5003"},
            }
        ],
    )

    dump_flow_yaml(path, flow)
    loaded = load_flow_yaml(path)

    assert loaded.name == "demo"
    assert loaded.steps[0].name == "session"
    assert loaded.steps[0].send == "1003"
    assert loaded.steps[0].expect is not None
    assert loaded.steps[0].expect.response_prefix == "5003"


def test_transfer_data_requires_non_empty_segments() -> None:
    with pytest.raises(ValueError):
        FlowDefinition(
            name="demo_transfer",
            steps=[
                {
                    "name": "transfer",
                    "transfer_data": {
                        "segments": [],
                    },
                }
            ],
        )


def test_transfer_data_allows_segments_hook_without_segments() -> None:
    flow = FlowDefinition(
        name="demo_transfer_hook",
        steps=[
            {
                "name": "transfer",
                "transfer_data": {
                    "segments_hook": {
                        "snippet": 'result = {"segments": [{"address": 0x1000, "data_hex": "AA"}]}'
                    }
                },
            }
        ],
    )
    assert flow.steps[0].transfer_data is not None


def test_load_flow_yaml_resolves_hook_path_relative_to_yaml_file(tmp_path: Path) -> None:
    flow_path = tmp_path / "flows" / "demo.yaml"
    flow_path.parent.mkdir(parents=True, exist_ok=True)
    yaml_text = "\n".join(
        [
            "name: demo_relative",
            "steps:",
            "  - name: s1",
            '    send: "1003"',
            "    before_hook:",
            '      script_path: "../extensions/demo_hook.py"',
            '      function_name: "build"',
        ]
    )
    flow_path.write_text(yaml_text + "\n", encoding="utf-8")

    loaded = load_flow_yaml(flow_path)
    hook = loaded.steps[0].before_hook
    assert hook is not None
    assert hook.script_path == str((tmp_path / "extensions" / "demo_hook.py").resolve())
