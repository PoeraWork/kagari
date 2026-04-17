from __future__ import annotations

from pathlib import Path

import pytest

from uds_mcp.flow.schema import (
    FlowDefinition,
    SubflowStep,
    TransferStep,
    UdsStep,
    WaitStep,
    dump_flow_yaml,
    load_flow_yaml,
)


def test_dump_and_load_flow_yaml(tmp_path: Path) -> None:
    path = tmp_path / "demo_flow.yaml"
    flow = FlowDefinition(
        name="demo",
        variables={"seed": 1},
        steps=[
            {
                "kind": "uds",
                "name": "session",
                "request": "1003",
                "timeout_ms": 1000,
                "delay_ms": 250,
                "expect": {"response_prefix": "5003"},
            }
        ],
    )

    dump_flow_yaml(path, flow)
    loaded = load_flow_yaml(path)

    assert loaded.name == "demo"
    step = loaded.steps[0]
    assert isinstance(step, UdsStep)
    assert step.name == "session"
    assert step.request == "1003"
    assert step.delay_ms == 250
    assert step.expect is not None
    assert step.expect.response_prefix == "5003"


def test_step_delay_must_be_non_negative() -> None:
    with pytest.raises(ValueError):
        FlowDefinition(
            name="demo_negative_delay",
            steps=[
                {
                    "kind": "uds",
                    "name": "session",
                    "request": "1003",
                    "delay_ms": -1,
                }
            ],
        )


def test_transfer_data_requires_non_empty_segments() -> None:
    with pytest.raises(ValueError):
        FlowDefinition(
            name="demo_transfer",
            steps=[
                {
                    "kind": "transfer",
                    "name": "transfer",
                    "transfer_data": {"segments": []},
                }
            ],
        )


def test_transfer_data_allows_segments_hook_without_segments() -> None:
    flow = FlowDefinition(
        name="demo_transfer_hook",
        steps=[
            {
                "kind": "transfer",
                "name": "transfer",
                "transfer_data": {
                    "segments_hook": {
                        "inline": 'result = {"segments": [{"address": 0x1000, "data_hex": "AA"}]}'
                    }
                },
            }
        ],
    )
    step = flow.steps[0]
    assert isinstance(step, TransferStep)
    assert step.transfer_data is not None


def test_load_flow_yaml_resolves_hook_path_relative_to_yaml_file(tmp_path: Path) -> None:
    flow_path = tmp_path / "flows" / "demo.yaml"
    flow_path.parent.mkdir(parents=True, exist_ok=True)
    yaml_text = "\n".join(
        [
            "name: demo_relative",
            "steps:",
            "  - kind: uds",
            "    name: s1",
            '    request: "1003"',
            "    before_hook:",
            '      script: "../extensions/demo_hook.py"',
            '      function: "build"',
        ]
    )
    flow_path.write_text(yaml_text + "\n", encoding="utf-8")

    loaded = load_flow_yaml(flow_path)
    step = loaded.steps[0]
    assert isinstance(step, UdsStep)
    hook = step.before_hook
    assert hook is not None
    assert hook.script == str((tmp_path / "extensions" / "demo_hook.py").resolve())


def test_step_expect_assertions_accepts_byte_and_range_rules() -> None:
    flow = FlowDefinition(
        name="assertions_demo",
        steps=[
            {
                "kind": "uds",
                "name": "read_did",
                "request": "22ABCD",
                "expect": {
                    "response_prefix": "62",
                    "assertions": [
                        {
                            "name": "sid_ok",
                            "kind": "byte_eq",
                            "source": "response_hex",
                            "index": 0,
                            "value": 0x62,
                            "on_fail": "fatal",
                        },
                        {
                            "name": "payload_range",
                            "kind": "bytes_int_range",
                            "source": "response_hex",
                            "start": 1,
                            "length": 2,
                            "min_value": 0xABCD,
                            "max_value": 0xABCD,
                            "on_fail": "record",
                        },
                    ],
                },
            }
        ],
    )
    step = flow.steps[0]
    assert isinstance(step, UdsStep)
    assert step.expect is not None
    assert len(step.expect.assertions) == 2


def test_step_expect_assertions_reject_missing_required_fields() -> None:
    with pytest.raises(ValueError, match="requires index and value"):
        FlowDefinition(
            name="assertions_bad",
            steps=[
                {
                    "kind": "uds",
                    "name": "read_did",
                    "request": "22ABCD",
                    "expect": {
                        "assertions": [
                            {
                                "kind": "byte_eq",
                                "source": "response_hex",
                                "index": 0,
                            }
                        ]
                    },
                }
            ],
        )


def test_step_expect_response_matchers_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="at most one"):
        FlowDefinition(
            name="bad_matchers",
            steps=[
                {
                    "kind": "uds",
                    "name": "read_did",
                    "request": "22ABCD",
                    "expect": {
                        "response_prefix": "62",
                        "response_regex": "^62.*$",
                    },
                }
            ],
        )


def test_step_supports_skipped_response_flag() -> None:
    flow = FlowDefinition(
        name="skip_resp_flag",
        steps=[
            {
                "kind": "uds",
                "name": "transfer_block",
                "request": "3601AA",
                "skipped_response": True,
            }
        ],
    )
    step = flow.steps[0]
    assert isinstance(step, UdsStep)
    assert step.skipped_response is True


def test_step_supports_can_tx_hook() -> None:
    flow = FlowDefinition(
        name="can_tx_hook_flag",
        steps=[
            {
                "kind": "uds",
                "name": "transfer_block",
                "request": "3601AA",
                "can_tx_hook": {
                    "inline": 'result = {"can_frames": [{"arbitration_id": 0x700, "data_hex": "01"}]}'
                },
            }
        ],
    )
    step = flow.steps[0]
    assert isinstance(step, UdsStep)
    assert step.can_tx_hook is not None


def test_flow_repeat_defaults_to_one() -> None:
    flow = FlowDefinition(
        name="flow_repeat_default",
        steps=[{"kind": "uds", "name": "session", "request": "1003"}],
    )
    assert flow.repeat == 1


def test_flow_repeat_roundtrip_in_yaml(tmp_path: Path) -> None:
    path = tmp_path / "repeat_flow.yaml"
    flow = FlowDefinition(
        name="repeat_demo",
        repeat=3,
        steps=[{"kind": "uds", "name": "session", "request": "1003"}],
    )

    dump_flow_yaml(path, flow)
    loaded = load_flow_yaml(path)

    assert loaded.repeat == 3


# ---------------------------------------------------------------------------
# Discriminated union coverage
# ---------------------------------------------------------------------------


class TestSubflowStep:
    _subflow_path = "/flows/child.yaml"

    def test_valid_subflow_step(self) -> None:
        flow = FlowDefinition(
            name="parent",
            steps=[{"kind": "subflow", "name": "child", "subflow": self._subflow_path}],
        )
        step = flow.steps[0]
        assert isinstance(step, SubflowStep)
        assert step.subflow == self._subflow_path

    def test_subflow_step_with_delay_and_breakpoint(self) -> None:
        flow = FlowDefinition(
            name="parent",
            steps=[
                {
                    "kind": "subflow",
                    "name": "child",
                    "subflow": self._subflow_path,
                    "delay_ms": 100,
                    "breakpoint": True,
                }
            ],
        )
        step = flow.steps[0]
        assert isinstance(step, SubflowStep)
        assert step.delay_ms == 100
        assert step.breakpoint is True


class TestDiscriminator:
    def test_missing_kind_rejected(self) -> None:
        with pytest.raises(ValueError, match="discriminator"):
            FlowDefinition(
                name="bad",
                steps=[{"name": "s", "request": "1003"}],
            )

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(ValueError):
            FlowDefinition(
                name="bad",
                steps=[{"kind": "mystery", "name": "s"}],
            )

    def test_uds_missing_request_rejected(self) -> None:
        with pytest.raises(ValueError):
            FlowDefinition(
                name="bad",
                steps=[{"kind": "uds", "name": "s"}],
            )

    def test_subflow_missing_subflow_rejected(self) -> None:
        with pytest.raises(ValueError):
            FlowDefinition(
                name="bad",
                steps=[{"kind": "subflow", "name": "s"}],
            )

    def test_wait_requires_positive_delay(self) -> None:
        with pytest.raises(ValueError):
            FlowDefinition(
                name="bad",
                steps=[{"kind": "wait", "name": "w", "delay_ms": 0}],
            )

    def test_wait_step_accepts_positive_delay(self) -> None:
        flow = FlowDefinition(
            name="wait_only",
            steps=[{"kind": "wait", "name": "wait_boot", "delay_ms": 300}],
        )
        step = flow.steps[0]
        assert isinstance(step, WaitStep)
        assert step.delay_ms == 300

    def test_can_step_requires_can_tx_hook(self) -> None:
        with pytest.raises(ValueError):
            FlowDefinition(
                name="bad",
                steps=[{"kind": "can", "name": "c"}],
            )

    def test_can_step_accepts_can_tx_hook(self) -> None:
        flow = FlowDefinition(
            name="ok",
            steps=[
                {
                    "kind": "can",
                    "name": "c",
                    "can_tx_hook": {
                        "inline": (
                            'result = {"can_frames": '
                            '[{"arbitration_id": 0x700, "data_hex": "01"}]}'
                        )
                    },
                }
            ],
        )
        assert flow.steps[0].kind == "can"


class TestRepeatBoundaryValues:
    def test_repeat_default_is_one(self) -> None:
        flow = FlowDefinition(
            name="f", steps=[{"kind": "uds", "name": "s", "request": "1003"}]
        )
        assert flow.steps[0].repeat == 1

    def test_repeat_zero_rejected(self) -> None:
        with pytest.raises(ValueError):
            FlowDefinition(
                name="f",
                steps=[{"kind": "uds", "name": "s", "request": "1003", "repeat": 0}],
            )

    def test_repeat_negative_rejected(self) -> None:
        with pytest.raises(ValueError):
            FlowDefinition(
                name="f",
                steps=[{"kind": "uds", "name": "s", "request": "1003", "repeat": -1}],
            )

    def test_repeat_five_accepted(self) -> None:
        flow = FlowDefinition(
            name="f",
            steps=[{"kind": "uds", "name": "s", "request": "1003", "repeat": 5}],
        )
        assert flow.steps[0].repeat == 5


class TestAddressingModeDefaults:
    def test_step_addressing_mode_default_is_inherit(self) -> None:
        flow = FlowDefinition(
            name="f", steps=[{"kind": "uds", "name": "s", "request": "1003"}]
        )
        assert flow.steps[0].addressing_mode == "inherit"

    def test_flow_default_addressing_mode_is_physical(self) -> None:
        flow = FlowDefinition(
            name="f", steps=[{"kind": "uds", "name": "s", "request": "1003"}]
        )
        assert flow.default_addressing_mode == "physical"

    def test_flow_default_addressing_mode_functional(self) -> None:
        flow = FlowDefinition(
            name="f",
            default_addressing_mode="functional",
            steps=[{"kind": "uds", "name": "s", "request": "1003"}],
        )
        assert flow.default_addressing_mode == "functional"

    def test_step_explicit_addressing_mode(self) -> None:
        flow = FlowDefinition(
            name="f",
            steps=[
                {
                    "kind": "uds",
                    "name": "s",
                    "request": "1003",
                    "addressing_mode": "functional",
                }
            ],
        )
        assert flow.steps[0].addressing_mode == "functional"
