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
                "delay_ms": 250,
                "expect": {"response_prefix": "5003"},
            }
        ],
    )

    dump_flow_yaml(path, flow)
    loaded = load_flow_yaml(path)

    assert loaded.name == "demo"
    assert loaded.steps[0].name == "session"
    assert loaded.steps[0].send == "1003"
    assert loaded.steps[0].delay_ms == 250
    assert loaded.steps[0].expect is not None
    assert loaded.steps[0].expect.response_prefix == "5003"


def test_step_delay_must_be_non_negative() -> None:
    with pytest.raises(ValueError):
        FlowDefinition(
            name="demo_negative_delay",
            steps=[
                {
                    "name": "session",
                    "send": "1003",
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


def test_step_expect_assertions_accepts_byte_and_range_rules() -> None:
    flow = FlowDefinition(
        name="assertions_demo",
        steps=[
            {
                "name": "read_did",
                "send": "22ABCD",
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
    assert len(flow.steps[0].expect.assertions) == 2


def test_step_expect_assertions_reject_missing_required_fields() -> None:
    with pytest.raises(ValueError, match="requires index and value"):
        FlowDefinition(
            name="assertions_bad",
            steps=[
                {
                    "name": "read_did",
                    "send": "22ABCD",
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
                    "name": "read_did",
                    "send": "22ABCD",
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
                "name": "transfer_block",
                "send": "3601AA",
                "skipped_response": True,
            }
        ],
    )
    assert flow.steps[0].skipped_response is True


def test_step_supports_can_tx_hook() -> None:
    flow = FlowDefinition(
        name="can_tx_hook_flag",
        steps=[
            {
                "name": "transfer_block",
                "send": "3601AA",
                "can_tx_hook": {
                    "snippet": 'result = {"can_frames": [{"arbitration_id": 0x700, "data_hex": "01"}]}'
                },
            }
        ],
    )
    assert flow.steps[0].can_tx_hook is not None


def test_flow_repeat_defaults_to_one() -> None:
    flow = FlowDefinition(
        name="flow_repeat_default",
        steps=[{"name": "session", "send": "1003"}],
    )
    assert flow.repeat == 1


def test_flow_repeat_roundtrip_in_yaml(tmp_path: Path) -> None:
    path = tmp_path / "repeat_flow.yaml"
    flow = FlowDefinition(
        name="repeat_demo",
        repeat=3,
        steps=[{"name": "session", "send": "1003"}],
    )

    dump_flow_yaml(path, flow)
    loaded = load_flow_yaml(path)

    assert loaded.repeat == 3


# ---------------------------------------------------------------------------
# Task 1.6 – Unit tests for schema 变更
# Requirements: 1.1, 1.2, 1.5, 3.1, 3.7, 9.1, 9.2, 9.7
# ---------------------------------------------------------------------------


class TestSubFlowStepCreation:
    """Valid sub_flow step creation."""

    _sub_flow_path = "/flows/child.yaml"

    def test_valid_sub_flow_step(self) -> None:
        flow = FlowDefinition(
            name="parent",
            steps=[{"name": "child", "sub_flow": self._sub_flow_path}],
        )
        assert flow.steps[0].sub_flow == self._sub_flow_path
        assert flow.steps[0].send is None
        assert flow.steps[0].transfer_data is None

    def test_sub_flow_step_with_delay_and_breakpoint(self) -> None:
        flow = FlowDefinition(
            name="parent",
            steps=[
                {
                    "name": "child",
                    "sub_flow": self._sub_flow_path,
                    "delay_ms": 100,
                    "breakpoint": True,
                }
            ],
        )
        assert flow.steps[0].delay_ms == 100
        assert flow.steps[0].breakpoint is True


class TestMutualExclusivity:
    """send / transfer_data / sub_flow must be mutually exclusive."""

    _sf = "/flows/c.yaml"

    def test_send_and_sub_flow_rejected(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            FlowDefinition(
                name="bad",
                steps=[{"name": "s", "send": "1003", "sub_flow": self._sf}],
            )

    def test_transfer_data_and_sub_flow_rejected(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            FlowDefinition(
                name="bad",
                steps=[
                    {
                        "name": "s",
                        "transfer_data": {"segments": [{"address": 0x1000, "data_hex": "AA"}]},
                        "sub_flow": self._sf,
                    }
                ],
            )

    def test_all_three_rejected(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            FlowDefinition(
                name="bad",
                steps=[
                    {
                        "name": "s",
                        "send": "1003",
                        "transfer_data": {"segments": [{"address": 0x1000, "data_hex": "AA"}]},
                        "sub_flow": self._sf,
                    }
                ],
            )

    def test_none_of_three_rejected(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            FlowDefinition(
                name="bad",
                steps=[{"name": "s"}],
            )

    def test_wait_only_step_with_delay_is_allowed(self) -> None:
        flow = FlowDefinition(
            name="wait_only",
            steps=[{"name": "wait_boot", "delay_ms": 300}],
        )
        assert flow.steps[0].send is None
        assert flow.steps[0].transfer_data is None
        assert flow.steps[0].sub_flow is None
        assert flow.steps[0].delay_ms == 300


class TestSubFlowHookRestrictions:
    """sub_flow step must not have hooks or expect."""

    _sf = "/flows/c.yaml"

    def test_sub_flow_with_before_hook_rejected(self) -> None:
        with pytest.raises(ValueError, match="before_hook"):
            FlowDefinition(
                name="bad",
                steps=[
                    {
                        "name": "s",
                        "sub_flow": self._sf,
                        "before_hook": {"snippet": "pass"},
                    }
                ],
            )

    def test_sub_flow_with_message_hook_rejected(self) -> None:
        with pytest.raises(ValueError, match="message_hook"):
            FlowDefinition(
                name="bad",
                steps=[
                    {
                        "name": "s",
                        "sub_flow": self._sf,
                        "message_hook": {"snippet": "pass"},
                    }
                ],
            )

    def test_sub_flow_with_after_hook_rejected(self) -> None:
        with pytest.raises(ValueError, match="after_hook"):
            FlowDefinition(
                name="bad",
                steps=[
                    {
                        "name": "s",
                        "sub_flow": self._sf,
                        "after_hook": {"snippet": "pass"},
                    }
                ],
            )

    def test_sub_flow_with_can_tx_hook_rejected(self) -> None:
        with pytest.raises(ValueError, match="can_tx_hook"):
            FlowDefinition(
                name="bad",
                steps=[
                    {
                        "name": "s",
                        "sub_flow": self._sf,
                        "can_tx_hook": {
                            "snippet": 'result = {"can_frames": [{"arbitration_id": 0x700, "data_hex": "01"}]}'
                        },
                    }
                ],
            )

    def test_sub_flow_with_expect_rejected(self) -> None:
        with pytest.raises(ValueError, match="expect"):
            FlowDefinition(
                name="bad",
                steps=[
                    {
                        "name": "s",
                        "sub_flow": self._sf,
                        "expect": {"response_prefix": "5003"},
                    }
                ],
            )


class TestRepeatBoundaryValues:
    """repeat field boundary validation."""

    def test_repeat_default_is_one(self) -> None:
        flow = FlowDefinition(name="f", steps=[{"name": "s", "send": "1003"}])
        assert flow.steps[0].repeat == 1

    def test_repeat_zero_rejected(self) -> None:
        with pytest.raises(ValueError):
            FlowDefinition(
                name="f",
                steps=[{"name": "s", "send": "1003", "repeat": 0}],
            )

    def test_repeat_negative_rejected(self) -> None:
        with pytest.raises(ValueError):
            FlowDefinition(
                name="f",
                steps=[{"name": "s", "send": "1003", "repeat": -1}],
            )

    def test_repeat_five_accepted(self) -> None:
        flow = FlowDefinition(
            name="f",
            steps=[{"name": "s", "send": "1003", "repeat": 5}],
        )
        assert flow.steps[0].repeat == 5


class TestAddressingModeDefaults:
    """addressing_mode on FlowStep and default_addressing_mode on FlowDefinition."""

    def test_step_addressing_mode_default_is_inherit(self) -> None:
        flow = FlowDefinition(name="f", steps=[{"name": "s", "send": "1003"}])
        assert flow.steps[0].addressing_mode == "inherit"

    def test_flow_default_addressing_mode_is_physical(self) -> None:
        flow = FlowDefinition(name="f", steps=[{"name": "s", "send": "1003"}])
        assert flow.default_addressing_mode == "physical"

    def test_flow_default_addressing_mode_functional(self) -> None:
        flow = FlowDefinition(
            name="f",
            default_addressing_mode="functional",
            steps=[{"name": "s", "send": "1003"}],
        )
        assert flow.default_addressing_mode == "functional"

    def test_step_explicit_addressing_mode(self) -> None:
        flow = FlowDefinition(
            name="f",
            steps=[{"name": "s", "send": "1003", "addressing_mode": "functional"}],
        )
        assert flow.steps[0].addressing_mode == "functional"
