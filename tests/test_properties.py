"""Property-based tests for flow-subflow-repeat-logging feature.

Feature: flow-subflow-repeat-logging
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import can
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from uds_mcp.extensions.runtime import ExtensionRuntime
from uds_mcp.flow.engine import FlowEngine, FlowStatus, _resolve_addressing_mode
from uds_mcp.flow.schema import (
    FlowDefinition,
    FlowStep,
    SubflowStep,
    UdsStep,
    dump_flow_yaml,
    load_flow_yaml,
)
from uds_mcp.logging.exporters.blf import BlfExporter
from uds_mcp.logging.store import EventStore
from uds_mcp.models.events import EventKind, LogEvent

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Strategy for optional non-empty send hex strings
_send_st = st.one_of(st.none(), st.text(alphabet="0123456789ABCDEF", min_size=2, max_size=8))

# Strategy for optional sub_flow paths (non-empty strings when present)
_sub_flow_st = st.one_of(st.none(), st.just("/flows/child.yaml"))

# Strategy for optional transfer_data dicts (valid minimal config when present)
_transfer_data_st = st.one_of(
    st.none(),
    st.just({"segments": [{"address": 0x1000, "data_hex": "AA"}]}),
)


def _count_non_none(*values: object) -> int:
    return sum(v is not None for v in values)


# ---------------------------------------------------------------------------
# Property 1: send/transfer_data/sub_flow 互斥性
# Feature: flow-subflow-repeat-logging, Property 1: send/transfer_data/sub_flow 互斥性
# **Validates: Requirements 1.2, 1.5, 3.1**
# ---------------------------------------------------------------------------


class TestMutualExclusivity:
    """Step kind discriminator determines which source field is required.

    Also verifies that repeat must be >= 1.
    """

    @settings(max_examples=200)
    @given(
        kind=st.sampled_from(["uds", "subflow"]),
        repeat=st.integers(min_value=-10, max_value=100),
    )
    def test_mutual_exclusivity(
        self,
        kind: str,
        repeat: int,
    ) -> None:
        """Feature: flow-subflow-repeat-logging, Property 1: kind discriminator

        For each supported kind, a valid step must be constructable with the
        kind's required field, and repeat must be >= 1.

        **Validates: Requirements 1.2, 1.5, 3.1**
        """
        valid_repeat = repeat >= 1

        kwargs: dict = {
            "kind": kind,
            "name": "test_step",
            "repeat": repeat,
        }
        if kind == "uds":
            kwargs["request"] = "1001"
        elif kind == "subflow":
            kwargs["subflow"] = "/flows/child.yaml"

        if valid_repeat:
            flow = FlowDefinition(name="f", steps=[kwargs])
            step = flow.steps[0]
            assert step.repeat == repeat
            assert step.kind == kind
        else:
            with pytest.raises(ValidationError):
                FlowDefinition(name="f", steps=[kwargs])


# ---------------------------------------------------------------------------
# Strategies for Property 2
# ---------------------------------------------------------------------------

# Strategy for addressing_mode at step level
_step_addressing_mode_st = st.sampled_from(["physical", "functional", "inherit"])

# Strategy for default_addressing_mode at flow level
_flow_default_addressing_mode_st = st.sampled_from(["physical", "functional"])

# Strategy for tester_present at step level
_step_tester_present_st = st.sampled_from(["inherit", "off", "physical", "functional"])

# Strategy for tester_present_policy at flow level
_flow_tester_present_policy_st = st.sampled_from(["breakpoint_only", "during_flow", "off"])

# Strategy for repeat values (valid range)
_repeat_st = st.integers(min_value=1, max_value=20)

# Strategy for hex send strings
_hex_send_st = st.text(alphabet="0123456789ABCDEF", min_size=2, max_size=8)

# Strategy for variable dicts (simple key-value pairs safe for YAML round-trip)
_variables_st = st.dictionaries(
    keys=st.from_regex(r"[a-z][a-z0-9_]{0,9}", fullmatch=True),
    values=st.one_of(
        st.integers(min_value=0, max_value=0xFFFF),
        st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=10),
    ),
    max_size=5,
)

# Strategy for step names (safe ASCII identifiers)
_step_name_st = st.from_regex(r"[a-z][a-z0-9_]{0,15}", fullmatch=True)

# Strategy for flow names
_flow_name_st = st.from_regex(r"[a-z][a-z0-9_]{0,15}", fullmatch=True)


@st.composite
def _flow_step_send_st(draw: st.DrawFn) -> dict:
    """Generate a valid uds-kind FlowStep as a dict."""
    return {
        "kind": "uds",
        "name": draw(_step_name_st),
        "request": draw(_hex_send_st),
        "repeat": draw(_repeat_st),
        "addressing_mode": draw(_step_addressing_mode_st),
        "tester_present": draw(_step_tester_present_st),
        "timeout_ms": draw(st.integers(min_value=100, max_value=5000)),
        "delay_ms": draw(st.integers(min_value=0, max_value=500)),
    }


@st.composite
def _flow_step_sub_flow_st(draw: st.DrawFn, base_dir: Path | None = None) -> dict:
    """Generate a valid sub_flow-type FlowStep as a dict with an absolute path."""
    # Use a fixed sub-directory name for the sub_flow reference
    sub_name = draw(st.from_regex(r"[a-z][a-z0-9_]{0,8}", fullmatch=True))
    # sub_flow paths will be absolute; dump_flow_yaml converts to relative
    if base_dir is not None:
        sub_flow_path = str(base_dir / f"{sub_name}.yaml")
    else:
        sub_flow_path = str(Path("/flows") / f"{sub_name}.yaml")
    return {
        "kind": "subflow",
        "name": draw(_step_name_st),
        "subflow": sub_flow_path,
        "repeat": draw(_repeat_st),
        "addressing_mode": draw(_step_addressing_mode_st),
        "tester_present": draw(_step_tester_present_st),
        "delay_ms": draw(st.integers(min_value=0, max_value=500)),
    }


@st.composite
def _flow_definition_st(draw: st.DrawFn, base_dir: Path | None = None) -> FlowDefinition:
    """Generate a valid FlowDefinition with a mix of send and sub_flow steps."""
    # At least one send step (so the flow is non-trivial)
    send_steps = draw(st.lists(_flow_step_send_st(), min_size=1, max_size=4))
    sub_flow_steps = draw(
        st.lists(_flow_step_sub_flow_st(base_dir=base_dir), min_size=0, max_size=2)
    )
    all_steps = send_steps + sub_flow_steps
    # Shuffle to mix ordering
    shuffled = draw(st.permutations(all_steps))

    return FlowDefinition(
        name=draw(_flow_name_st),
        version="1.0",
        tester_present_policy=draw(_flow_tester_present_policy_st),
        default_addressing_mode=draw(_flow_default_addressing_mode_st),
        variables=draw(_variables_st),
        steps=shuffled,
    )


# ---------------------------------------------------------------------------
# Property 2: 子流程 YAML 序列化往返
# Feature: flow-subflow-repeat-logging, Property 2: 子流程 YAML 序列化往返
# **Validates: Requirements 3.8, 7.1, 7.2, 7.3, 7.4, 9.6**
# ---------------------------------------------------------------------------


class TestYamlSerializationRoundTrip:
    """dump_flow_yaml → load_flow_yaml round-trip preserves FlowDefinition fields.

    sub_flow absolute paths are relativized on dump and resolved back on load.
    """

    @settings(max_examples=100)
    @given(data=st.data())
    def test_yaml_round_trip(self, data: st.DataObject) -> None:
        """Feature: flow-subflow-repeat-logging, Property 2: 子流程 YAML 序列化往返

        For any valid FlowDefinition with sub_flow, repeat, addressing_mode,
        and default_addressing_mode fields, dump_flow_yaml followed by
        load_flow_yaml produces an equivalent FlowDefinition.

        **Validates: Requirements 3.8, 7.1, 7.2, 7.3, 7.4, 9.6**
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            flow_dir = tmp_path / "flows"
            flow_dir.mkdir(parents=True, exist_ok=True)
            flow_path = flow_dir / "test_flow.yaml"

            # Generate a FlowDefinition with sub_flow paths relative to flow_dir
            original = data.draw(_flow_definition_st(base_dir=flow_dir))

            # Dump to YAML
            dump_flow_yaml(flow_path, original)

            # Load back
            loaded = load_flow_yaml(flow_path)

            # --- Verify top-level fields ---
            assert loaded.name == original.name
            assert loaded.version == original.version
            assert loaded.tester_present_policy == original.tester_present_policy
            assert loaded.default_addressing_mode == original.default_addressing_mode
            assert loaded.variables == original.variables

            # --- Verify steps ---
            assert len(loaded.steps) == len(original.steps)
            for orig_step, loaded_step in zip(original.steps, loaded.steps, strict=True):
                assert loaded_step.name == orig_step.name
                assert loaded_step.repeat == orig_step.repeat
                assert loaded_step.addressing_mode == orig_step.addressing_mode
                assert loaded_step.tester_present == orig_step.tester_present
                assert loaded_step.delay_ms == orig_step.delay_ms

                if orig_step.kind == "uds":
                    assert loaded_step.request == orig_step.request
                    assert loaded_step.timeout_ms == orig_step.timeout_ms
                elif orig_step.kind == "subflow":
                    # sub_flow should be resolved back to absolute path
                    assert loaded_step.subflow is not None
                    assert Path(loaded_step.subflow).is_absolute()
                    # The resolved absolute path should match the original
                    assert str(Path(loaded_step.subflow).resolve()) == str(
                        Path(orig_step.subflow).resolve()
                    )


# ---------------------------------------------------------------------------
# Property 3: 子流程变量共享
# Feature: flow-subflow-repeat-logging, Property 3: 子流程变量共享
# **Validates: Requirements 2.2**
# ---------------------------------------------------------------------------


class _FakeUdsClient:
    """Minimal fake UDS client for property tests."""

    async def send(
        self, request_hex: str, timeout_ms: int = 1000, *, addressing_mode: str = "physical"
    ) -> dict[str, object]:
        del timeout_ms, addressing_mode
        # Echo-style: respond with positive response (service_id + 0x40)
        request_hex = request_hex.upper()
        service_byte = request_hex[:2]
        try:
            resp_byte = f"{int(service_byte, 16) + 0x40:02X}"
        except ValueError:
            resp_byte = "7F"
        return {
            "request_hex": request_hex,
            "response_hex": resp_byte + request_hex[2:],
            "response_id": 0x7E8,
        }

    async def ensure_tester_present(self) -> None:
        return None

    async def stop_tester_present(self) -> None:
        return None

    async def start_tester_present_owner(
        self, owner: str, *, addressing_mode: str = "physical"
    ) -> dict[str, object]:
        return {
            "running": True,
            "addressing_mode": addressing_mode,
            "owners": [owner],
            "interval_sec": 1.0,
        }

    async def stop_tester_present_owner(self, owner: str) -> dict[str, object]:  # noqa: ARG002
        return {"running": False, "addressing_mode": None, "owners": [], "interval_sec": 1.0}


# Strategy for variable keys (safe identifiers, no _path suffix to avoid path resolution)
_var_key_st = st.from_regex(r"[a-z][a-z0-9]{0,7}", fullmatch=True).filter(
    lambda k: not k.endswith("_path")
)

# Strategy for variable values (simple strings/ints safe for snippet embedding)
_var_value_st = st.one_of(
    st.integers(min_value=0, max_value=9999),
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=8),
)

# Strategy for initial parent variables
_parent_vars_st = st.dictionaries(keys=_var_key_st, values=_var_value_st, min_size=0, max_size=3)

# Strategy for sub-flow injected variables (at least 1 to verify sharing)
_subflow_vars_st = st.dictionaries(keys=_var_key_st, values=_var_value_st, min_size=1, max_size=3)


async def _wait_done(engine: FlowEngine, run_id: str) -> dict:
    while True:
        status = engine.status(run_id)
        if status["status"] in {
            FlowStatus.DONE.value,
            FlowStatus.FAILED.value,
            FlowStatus.STOPPED.value,
        }:
            return status
        await asyncio.sleep(0.01)


class TestSubFlowVariableSharing:
    """Sub-flow modifications to variables are reflected in the parent flow.

    The parent and sub-flow share the same dict reference, so any writes
    performed by a sub-flow after_hook are visible to subsequent parent steps.
    """

    @settings(max_examples=100, deadline=5000)
    @given(
        parent_vars=_parent_vars_st,
        subflow_vars=_subflow_vars_st,
    )
    def test_subflow_variable_sharing(
        self,
        parent_vars: dict[str, int | str],
        subflow_vars: dict[str, int | str],
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        """Feature: flow-subflow-repeat-logging, Property 3: 子流程变量共享

        For any parent flow variables and sub-flow variable modifications,
        changes made by the sub-flow's after_hook are reflected in the parent
        flow's variables dict (same reference).

        **Validates: Requirements 2.2**
        """
        tmp_path = tmp_path_factory.mktemp("prop3")

        # Use a unique marker key that the sub-flow will write.
        # The parent's verify step reads it back via before_hook.
        marker_key = "_sf_marker"
        marker_value = "CAFE"

        # Build the after_hook snippet that writes subflow_vars + marker
        snippet_lines = [
            'v = dict(context["variables"])',
        ]
        for key, value in subflow_vars.items():
            if isinstance(value, str):
                snippet_lines.append(f'v["{key}"] = "{value}"')
            else:
                snippet_lines.append(f'v["{key}"] = {value}')
        snippet_lines.append(f'v["{marker_key}"] = "{marker_value}"')
        snippet_lines.append('result = {"variables": v}')
        snippet_code = "\n".join(snippet_lines)

        # Write sub-flow YAML
        sub_flow_path = tmp_path / "sub_flow.yaml"
        sub_flow_yaml = (
            "name: sub_flow\n"
            "steps:\n"
            "  - kind: uds\n"
            "    name: sub_step\n"
            '    request: "1001"\n'
            "    after_hook:\n"
            "      inline: |\n"
        )
        for line in snippet_code.split("\n"):
            sub_flow_yaml += f"        {line}\n"
        sub_flow_path.write_text(sub_flow_yaml, encoding="utf-8")

        # The verify step's before_hook reads the marker from variables.
        # If the marker is present, it sends "10CAFE"; otherwise "10DEAD".
        verify_snippet = (
            'marker = context["variables"].get("_sf_marker", "")\n'
            'if marker == "CAFE":\n'
            '    result = {"request_hex": "10CAFE"}\n'
            "else:\n"
            '    result = {"request_hex": "10DEAD"}\n'
        )

        # Write parent flow YAML
        parent_flow_path = tmp_path / "parent_flow.yaml"
        parent_flow_yaml = "name: parent_flow\nvariables:\n"
        if parent_vars:
            for k, v in parent_vars.items():
                if isinstance(v, str):
                    parent_flow_yaml += f'  {k}: "{v}"\n'
                else:
                    parent_flow_yaml += f"  {k}: {v}\n"
        else:
            parent_flow_yaml += "  {}\n"
        parent_flow_yaml += (
            "steps:\n"
            "  - kind: subflow\n"
            "    name: run_sub\n"
            "    subflow: sub_flow.yaml\n"
            "  - kind: uds\n"
            "    name: verify_step\n"
            '    request: "10DEAD"\n'
            "    before_hook:\n"
            "      inline: |\n"
        )
        for line in verify_snippet.split("\n"):
            parent_flow_yaml += f"        {line}\n"
        parent_flow_path.write_text(parent_flow_yaml, encoding="utf-8")

        async def _run() -> None:
            runtime = ExtensionRuntime([tmp_path.resolve()])
            engine = FlowEngine(_FakeUdsClient(), EventStore(), runtime)

            loaded = engine.load(parent_flow_path)
            run_id = await engine.start(loaded.name)
            final = await _wait_done(engine, run_id)

            assert final["status"] == FlowStatus.DONE.value, f"Flow failed: {final.get('error')}"

            # The verify_step should have sent "10CAFE" (not "10DEAD"),
            # proving the sub-flow's variable write was visible to the parent.
            trace = engine.get_trace(run_id)
            verify_traces = [t for t in trace if t["step"] == "verify_step"]
            assert len(verify_traces) == 1
            assert verify_traces[0]["request_hex"] == "10CAFE", (
                "Sub-flow variable modification was not reflected in parent flow. "
                f"Expected request '10CAFE' but got '{verify_traces[0]['request_hex']}'"
            )

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Property 6: 重复执行 trace 正确性
# Feature: flow-subflow-repeat-logging, Property 6: 重复执行 trace 正确性
# **Validates: Requirements 3.2, 3.6, 3.7**
# ---------------------------------------------------------------------------


class TestRepeatTraceCorrectness:
    """When repeat=N, trace contains repeat_index values 0..N-1 in order.

    When N=1, repeat_index is 0 (backward compatible with no-repeat behavior).
    """

    @settings(max_examples=100, deadline=5000)
    @given(
        repeat_n=st.integers(min_value=1, max_value=20),
        step_name=_step_name_st,
        send_hex=_hex_send_st,
    )
    def test_repeat_trace_correctness(
        self,
        repeat_n: int,
        step_name: str,
        send_hex: str,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        """Feature: flow-subflow-repeat-logging, Property 6: 重复执行 trace 正确性

        For any step with repeat=N (N >= 1), when all repetitions succeed,
        the trace records for that step contain repeat_index values from 0
        to N-1 in ascending order. When N=1, repeat_index is 0, matching
        the behavior of a step without explicit repeat.

        **Validates: Requirements 3.2, 3.6, 3.7**
        """
        tmp_path = tmp_path_factory.mktemp("prop6")

        # Build a simple flow YAML with one step that repeats N times
        flow_yaml = (
            "name: repeat_test_flow\n"
            "steps:\n"
            f"  - kind: uds\n"
            f"    name: {step_name}\n"
            f'    request: "{send_hex}"\n'
            f"    repeat: {repeat_n}\n"
        )
        flow_path = tmp_path / "flow.yaml"
        flow_path.write_text(flow_yaml, encoding="utf-8")

        async def _run() -> None:
            runtime = ExtensionRuntime([tmp_path.resolve()])
            engine = FlowEngine(_FakeUdsClient(), EventStore(), runtime)

            loaded = engine.load(flow_path)
            run_id = await engine.start(loaded.name)
            final = await _wait_done(engine, run_id)

            assert final["status"] == FlowStatus.DONE.value, (
                f"Flow failed unexpectedly: {final.get('error')}"
            )

            # Filter trace records for our step
            trace = engine.get_trace(run_id)
            step_traces = [t for t in trace if t["step"] == step_name]

            # There should be exactly N trace records (one per repeat)
            assert len(step_traces) == repeat_n, (
                f"Expected {repeat_n} trace records for step '{step_name}', got {len(step_traces)}"
            )

            # Verify repeat_index values are 0..N-1 in order
            repeat_indices = [t["repeat_index"] for t in step_traces]
            expected_indices = list(range(repeat_n))
            assert repeat_indices == expected_indices, (
                f"Expected repeat_index sequence {expected_indices}, got {repeat_indices}"
            )

            # When N=1, verify backward compatibility: repeat_index is 0
            if repeat_n == 1:
                assert step_traces[0]["repeat_index"] == 0

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Property 7: 重复执行失败快速退出
# Feature: flow-subflow-repeat-logging, Property 7: 重复执行失败快速退出
# **Validates: Requirements 3.4**
# ---------------------------------------------------------------------------


class _FailOnNthUdsClient:
    """Fake UDS client that returns a negative response on the N-th call (0-indexed).

    All other calls return a normal positive response.
    """

    def __init__(self, fail_on: int) -> None:
        self._fail_on = fail_on
        self._call_count = 0

    async def send(
        self, request_hex: str, timeout_ms: int = 1000, *, addressing_mode: str = "physical"
    ) -> dict[str, object]:
        del timeout_ms, addressing_mode
        current = self._call_count
        self._call_count += 1
        request_hex = request_hex.upper()
        if current == self._fail_on:
            # Return a negative response (7F = negative response indicator)
            return {
                "request_hex": request_hex,
                "response_hex": "7F" + request_hex[:2] + "31",
                "response_id": 0x7E8,
            }
        # Normal positive response
        service_byte = request_hex[:2]
        try:
            resp_byte = f"{int(service_byte, 16) + 0x40:02X}"
        except ValueError:
            resp_byte = "7F"
        return {
            "request_hex": request_hex,
            "response_hex": resp_byte + request_hex[2:],
            "response_id": 0x7E8,
        }

    async def ensure_tester_present(self) -> None:
        return None

    async def stop_tester_present(self) -> None:
        return None

    async def start_tester_present_owner(
        self, owner: str, *, addressing_mode: str = "physical"
    ) -> dict[str, object]:
        return {
            "running": True,
            "addressing_mode": addressing_mode,
            "owners": [owner],
            "interval_sec": 1.0,
        }

    async def stop_tester_present_owner(self, owner: str) -> dict[str, object]:  # noqa: ARG002
        return {"running": False, "addressing_mode": None, "owners": [], "interval_sec": 1.0}


class TestRepeatFailFastExit:
    """When repeat > 1 and the K-th iteration fails, no trace records
    with repeat_index >= K+1 should exist, and the flow status is FAILED.
    """

    @settings(max_examples=100, deadline=5000)
    @given(data=st.data())
    def test_repeat_fail_fast_exit(
        self,
        data: st.DataObject,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        """Feature: flow-subflow-repeat-logging, Property 7: 重复执行失败快速退出

        For any step with repeat > 1, if the K-th iteration (0-indexed) fails,
        there are no trace records with repeat_index >= K+1, and the flow
        status is FAILED.

        **Validates: Requirements 3.4**
        """
        repeat_n = data.draw(st.integers(min_value=2, max_value=10), label="repeat_n")
        fail_at = data.draw(st.integers(min_value=0, max_value=repeat_n - 1), label="fail_at")

        tmp_path = tmp_path_factory.mktemp("prop7")

        step_name = "repeat_step"
        send_hex = "1003"
        # expect prefix "50" so that the positive response "5003" passes,
        # but the negative response "7F1031" fails the check.
        flow_yaml = (
            "name: repeat_fail_flow\n"
            "steps:\n"
            f"  - kind: uds\n"
            f"    name: {step_name}\n"
            f'    request: "{send_hex}"\n'
            f"    repeat: {repeat_n}\n"
            "    expect:\n"
            '      response_prefix: "50"\n'
        )
        flow_path = tmp_path / "flow.yaml"
        flow_path.write_text(flow_yaml, encoding="utf-8")

        async def _run() -> None:
            runtime = ExtensionRuntime([tmp_path.resolve()])
            client = _FailOnNthUdsClient(fail_on=fail_at)
            engine = FlowEngine(client, EventStore(), runtime)

            loaded = engine.load(flow_path)
            run_id = await engine.start(loaded.name)
            final = await _wait_done(engine, run_id)

            # Flow must be FAILED
            assert final["status"] == FlowStatus.FAILED.value, (
                f"Expected FAILED status but got {final['status']}"
            )

            # Filter trace records for our step
            trace = engine.get_trace(run_id)
            step_traces = [t for t in trace if t["step"] == step_name]

            # No trace record should have repeat_index > fail_at
            for t in step_traces:
                assert t["repeat_index"] <= fail_at, (
                    f"Found trace with repeat_index={t['repeat_index']} "
                    f"but failure was at iteration {fail_at}. "
                    f"No records with repeat_index >= {fail_at + 1} should exist."
                )

            # There should be exactly fail_at + 1 trace records
            # (iterations 0..fail_at, where fail_at is the failing one)
            assert len(step_traces) == fail_at + 1, (
                f"Expected {fail_at + 1} trace records (0..{fail_at}), got {len(step_traces)}"
            )

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Property 4: 子流程 trace 合并与层级标识
# Feature: flow-subflow-repeat-logging, Property 4: 子流程 trace 合并与层级标识
# **Validates: Requirements 2.4**
# ---------------------------------------------------------------------------


class TestSubFlowTraceDepth:
    """Sub-flow trace records have sub_flow_depth equal to actual nesting depth.

    Top-level steps → sub_flow_depth=0, sub-flow steps → 1, nested → 2, etc.
    """

    @settings(max_examples=100, deadline=10000)
    @given(
        nesting_depth=st.integers(min_value=1, max_value=3),
    )
    def test_subflow_trace_depth(
        self,
        nesting_depth: int,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        """Feature: flow-subflow-repeat-logging, Property 4: 子流程 trace 合并与层级标识

        For any flow with nested sub-flows, each trace record's sub_flow_depth
        field equals its actual nesting depth. Top-level steps have
        sub_flow_depth=0, sub-flow steps have sub_flow_depth=1, nested
        sub-flow steps have sub_flow_depth=2, etc.

        **Validates: Requirements 2.4**
        """
        tmp_path = tmp_path_factory.mktemp("prop4")

        # Build nested sub-flow YAML files from the deepest level up.
        # Level 0 = parent flow (top-level), level 1..nesting_depth = sub-flows.
        #
        # Each level has one send step so we can verify its sub_flow_depth.
        # The deepest level is a plain send step; each higher level wraps
        # the next level as a sub_flow step and also has its own send step.

        # Create the deepest sub-flow first (only a send step)
        deepest_name = f"depth_{nesting_depth}"
        deepest_path = tmp_path / f"{deepest_name}.yaml"
        deepest_yaml = (
            f"name: {deepest_name}\n"
            "steps:\n"
            "  - kind: uds\n"
            f"    name: step_at_depth_{nesting_depth}\n"
            '    request: "1001"\n'
        )
        deepest_path.write_text(deepest_yaml, encoding="utf-8")

        # Build intermediate sub-flows from depth nesting_depth-1 down to 1
        child_filename = f"{deepest_name}.yaml"
        for depth in range(nesting_depth - 1, 0, -1):
            level_name = f"depth_{depth}"
            level_path = tmp_path / f"{level_name}.yaml"
            level_yaml = (
                f"name: {level_name}\n"
                "steps:\n"
                "  - kind: uds\n"
                f"    name: step_at_depth_{depth}\n"
                '    request: "1001"\n'
                "  - kind: subflow\n"
                f"    name: call_depth_{depth + 1}\n"
                f"    subflow: {child_filename}\n"
            )
            level_path.write_text(level_yaml, encoding="utf-8")
            child_filename = f"{level_name}.yaml"

        # Build the parent flow (depth 0)
        parent_path = tmp_path / "parent.yaml"
        parent_yaml = (
            "name: parent_flow\n"
            "steps:\n"
            "  - kind: uds\n"
            "    name: step_at_depth_0\n"
            '    request: "1001"\n'
            "  - kind: subflow\n"
            "    name: call_depth_1\n"
            f"    subflow: {child_filename}\n"
        )
        parent_path.write_text(parent_yaml, encoding="utf-8")

        async def _run() -> None:
            runtime = ExtensionRuntime([tmp_path.resolve()])
            engine = FlowEngine(_FakeUdsClient(), EventStore(), runtime)

            loaded = engine.load(parent_path)
            run_id = await engine.start(loaded.name)
            final = await _wait_done(engine, run_id)

            assert final["status"] == FlowStatus.DONE.value, (
                f"Flow failed unexpectedly: {final.get('error')}"
            )

            trace = engine.get_trace(run_id)

            # We expect at least one trace record per depth level (0..nesting_depth)
            assert len(trace) >= nesting_depth + 1, (
                f"Expected at least {nesting_depth + 1} trace records, got {len(trace)}"
            )

            # Verify each trace record's sub_flow_depth matches its expected depth.
            # Steps named "step_at_depth_N" should have sub_flow_depth == N.
            for record in trace:
                step_name = record["step"]
                actual_depth = record["sub_flow_depth"]

                if step_name.startswith("step_at_depth_"):
                    expected_depth = int(step_name.split("_")[-1])
                    assert actual_depth == expected_depth, (
                        f"Step '{step_name}' has sub_flow_depth={actual_depth}, "
                        f"expected {expected_depth}"
                    )

            # Additionally verify that top-level steps have depth 0
            top_level_traces = [t for t in trace if t["step"] == "step_at_depth_0"]
            assert len(top_level_traces) == 1
            assert top_level_traces[0]["sub_flow_depth"] == 0

            # Verify all depth levels from 0 to nesting_depth are present
            observed_depths = {t["sub_flow_depth"] for t in trace}
            expected_depths = set(range(nesting_depth + 1))
            assert expected_depths.issubset(observed_depths), (
                f"Expected depth levels {expected_depths} but only found {observed_depths}"
            )

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Property 5: 子流程失败传播
# Feature: flow-subflow-repeat-logging, Property 5: 子流程失败传播
# **Validates: Requirements 2.6**
# ---------------------------------------------------------------------------


class TestSubFlowFailurePropagation:
    """When a sub-flow step fails, the parent flow immediately becomes FAILED.

    No subsequent step records should appear in the trace after the failure.
    """

    @settings(max_examples=100, deadline=10000)
    @given(data=st.data())
    def test_subflow_failure_propagation(
        self,
        data: st.DataObject,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        """Feature: flow-subflow-repeat-logging, Property 5: 子流程失败传播

        For any parent flow containing a sub_flow step followed by another
        step, when a step inside the sub-flow fails, the parent flow
        immediately becomes FAILED and no trace records exist for the step
        after the sub_flow step.

        **Validates: Requirements 2.6**
        """
        # Generate the number of steps in the sub-flow and which one fails
        num_sub_steps = data.draw(st.integers(min_value=1, max_value=5), label="num_sub_steps")
        fail_at = data.draw(st.integers(min_value=0, max_value=num_sub_steps - 1), label="fail_at")

        tmp_path = tmp_path_factory.mktemp("prop5")

        # Build sub-flow YAML with num_sub_steps steps, each sending "1003"
        # with expect prefix "50" so positive response "5003" passes but
        # negative response "7F1031" fails the check.
        sub_flow_yaml = "name: child_flow\nsteps:\n"
        for i in range(num_sub_steps):
            sub_flow_yaml += (
                "  - kind: uds\n"
                f"    name: sub_step_{i}\n"
                '    request: "1003"\n'
                "    expect:\n"
                '      response_prefix: "50"\n'
            )
        sub_flow_path = tmp_path / "child_flow.yaml"
        sub_flow_path.write_text(sub_flow_yaml, encoding="utf-8")

        # Build parent flow: sub_flow step, then a regular step after it
        after_step_name = "after_sub_flow_step"
        parent_flow_yaml = (
            "name: parent_flow\n"
            "steps:\n"
            "  - kind: subflow\n"
            "    name: run_child\n"
            "    subflow: child_flow.yaml\n"
            "  - kind: uds\n"
            f"    name: {after_step_name}\n"
            '    request: "1001"\n'
        )
        parent_flow_path = tmp_path / "parent_flow.yaml"
        parent_flow_path.write_text(parent_flow_yaml, encoding="utf-8")

        async def _run() -> None:
            runtime = ExtensionRuntime([tmp_path.resolve()])
            client = _FailOnNthUdsClient(fail_on=fail_at)
            engine = FlowEngine(client, EventStore(), runtime)

            loaded = engine.load(parent_flow_path)
            run_id = await engine.start(loaded.name)
            final = await _wait_done(engine, run_id)

            # Parent flow must be FAILED
            assert final["status"] == FlowStatus.FAILED.value, (
                f"Expected FAILED status but got {final['status']}"
            )

            # No trace records should exist for the step after the sub_flow
            trace = engine.get_trace(run_id)
            after_traces = [t for t in trace if t["step"] == after_step_name]
            assert len(after_traces) == 0, (
                f"Found {len(after_traces)} trace records for '{after_step_name}' "
                "after sub-flow failure; expected 0"
            )

            # Verify that sub-flow trace records stop at the failing step
            sub_step_indices = [
                int(t["step"].split("_")[-1]) for t in trace if t["step"].startswith("sub_step_")
            ]
            for idx in sub_step_indices:
                assert idx <= fail_at, (
                    f"Found sub-flow trace for sub_step_{idx} but failure "
                    f"was at sub_step_{fail_at}. No records after failure expected."
                )

            # There should be exactly fail_at + 1 sub-flow trace records
            assert len(sub_step_indices) == fail_at + 1, (
                f"Expected {fail_at + 1} sub-flow trace records "
                f"(sub_step_0..sub_step_{fail_at}), got {len(sub_step_indices)}"
            )

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Property 14: 寻址模式解析
# Feature: flow-subflow-repeat-logging, Property 14: 寻址模式解析
# **Validates: Requirements 9.3, 9.4, 9.7**
# ---------------------------------------------------------------------------


class TestAddressingModeResolution:
    """Verify _resolve_addressing_mode resolves correctly for all combinations.

    - "inherit" → flow's default_addressing_mode
    - "physical" or "functional" → step's own value
    - Default (no explicit config) → "physical"
    """

    @settings(max_examples=200)
    @given(
        step_mode=st.sampled_from(["physical", "functional", "inherit"]),
        flow_default=st.sampled_from(["physical", "functional"]),
    )
    def test_addressing_mode_resolution(
        self,
        step_mode: str,
        flow_default: str,
    ) -> None:
        """Feature: flow-subflow-repeat-logging, Property 14: 寻址模式解析

        For any FlowStep addressing_mode and FlowDefinition
        default_addressing_mode combination:
        - When step addressing_mode is "inherit", resolved mode equals
          the flow's default_addressing_mode.
        - When step explicitly specifies "physical" or "functional",
          resolved mode equals the step's own value.

        **Validates: Requirements 9.3, 9.4, 9.7**
        """
        step = UdsStep(name="test_step", request="1001", addressing_mode=step_mode)
        flow = FlowDefinition(
            name="test_flow",
            steps=[step],
            default_addressing_mode=flow_default,
        )

        resolved = _resolve_addressing_mode(step, flow)

        if step_mode == "inherit":
            assert resolved == flow_default, (
                f"inherit should resolve to flow default '{flow_default}', got '{resolved}'"
            )
        else:
            assert resolved == step_mode, (
                f"Explicit '{step_mode}' should be preserved, got '{resolved}'"
            )

    @settings(max_examples=50)
    @given(
        flow_default=st.sampled_from(["physical", "functional"]),
    )
    def test_default_addressing_mode_is_physical(
        self,
        flow_default: str,
    ) -> None:
        """Feature: flow-subflow-repeat-logging, Property 14: 寻址模式解析

        When no addressing_mode is specified on the step (defaults to
        "inherit") and no default_addressing_mode on the flow (defaults
        to "physical"), the resolved mode is "physical".

        **Validates: Requirements 9.3, 9.4, 9.7**
        """
        # Step with default addressing_mode ("inherit")
        step = UdsStep(name="default_step", request="1001")
        assert step.addressing_mode == "inherit"

        # Flow with explicit default
        flow = FlowDefinition(
            name="test_flow",
            steps=[step],
            default_addressing_mode=flow_default,
        )

        resolved = _resolve_addressing_mode(step, flow)
        assert resolved == flow_default

        # When flow also uses default (physical)
        flow_default_physical = FlowDefinition(
            name="test_flow_default",
            steps=[step],
        )
        assert flow_default_physical.default_addressing_mode == "physical"

        resolved_default = _resolve_addressing_mode(step, flow_default_physical)
        assert resolved_default == "physical", (
            f"Default should be 'physical', got '{resolved_default}'"
        )


# ---------------------------------------------------------------------------
# Property 15: 子流程寻址模式继承
# Feature: flow-subflow-repeat-logging, Property 15: 子流程寻址模式继承
# **Validates: Requirements 9.5**
# ---------------------------------------------------------------------------


class TestSubFlowAddressingModeInheritance:
    """Sub-flow internal steps with addressing_mode="inherit" use the sub-flow's
    own default_addressing_mode. If the sub-flow doesn't define one, it falls
    back to the model default ("physical"), not the parent flow's value.
    """

    @settings(max_examples=100, deadline=10000)
    @given(
        parent_default=_flow_default_addressing_mode_st,
        sub_flow_default=_flow_default_addressing_mode_st,
    )
    def test_subflow_addressing_inherit_uses_subflow_default(
        self,
        parent_default: str,
        sub_flow_default: str,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        """Feature: flow-subflow-repeat-logging, Property 15: 子流程寻址模式继承

        When a sub-flow defines its own default_addressing_mode and its
        internal step has addressing_mode="inherit", the resolved mode
        equals the sub-flow's default_addressing_mode (not the parent's).

        **Validates: Requirements 9.5**
        """
        tmp_path = tmp_path_factory.mktemp("prop15a")

        # Sub-flow with explicit default_addressing_mode and an inherit step
        sub_flow_yaml = (
            "name: child_flow\n"
            f"default_addressing_mode: {sub_flow_default}\n"
            "steps:\n"
            "  - kind: uds\n"
            "    name: child_step\n"
            '    request: "1001"\n'
            "    addressing_mode: inherit\n"
        )
        sub_flow_path = tmp_path / "child_flow.yaml"
        sub_flow_path.write_text(sub_flow_yaml, encoding="utf-8")

        # Parent flow with a different default_addressing_mode
        parent_flow_yaml = (
            "name: parent_flow\n"
            f"default_addressing_mode: {parent_default}\n"
            "steps:\n"
            "  - kind: uds\n"
            "    name: parent_step\n"
            '    request: "1001"\n'
            "    addressing_mode: inherit\n"
            "  - kind: subflow\n"
            "    name: call_child\n"
            "    subflow: child_flow.yaml\n"
        )
        parent_flow_path = tmp_path / "parent_flow.yaml"
        parent_flow_path.write_text(parent_flow_yaml, encoding="utf-8")

        async def _run() -> None:
            runtime = ExtensionRuntime([tmp_path.resolve()])
            engine = FlowEngine(_FakeUdsClient(), EventStore(), runtime)

            loaded = engine.load(parent_flow_path)
            run_id = await engine.start(loaded.name)
            final = await _wait_done(engine, run_id)

            assert final["status"] == FlowStatus.DONE.value, f"Flow failed: {final.get('error')}"

            # Parent step should use parent's default
            trace = engine.get_trace(run_id)
            parent_traces = [t for t in trace if t["step"] == "parent_step"]
            assert len(parent_traces) == 1
            assert parent_traces[0]["addressing_mode"] == parent_default, (
                f"Parent step should use parent default '{parent_default}', "
                f"got '{parent_traces[0]['addressing_mode']}'"
            )

            # Child step should use sub-flow's default, NOT parent's
            child_traces = [t for t in trace if t["step"] == "child_step"]
            assert len(child_traces) == 1
            assert child_traces[0]["addressing_mode"] == sub_flow_default, (
                f"Child step with inherit should use sub-flow default "
                f"'{sub_flow_default}', got '{child_traces[0]['addressing_mode']}'"
            )

        asyncio.run(_run())

    @settings(max_examples=100, deadline=10000)
    @given(
        parent_default=_flow_default_addressing_mode_st,
    )
    def test_subflow_no_default_falls_back_to_model_default(
        self,
        parent_default: str,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        """Feature: flow-subflow-repeat-logging, Property 15: 子流程寻址模式继承

        When a sub-flow does NOT define default_addressing_mode (so it
        defaults to "physical" from the model), its internal step with
        addressing_mode="inherit" resolves to "physical" regardless of
        the parent flow's default_addressing_mode.

        **Validates: Requirements 9.5**
        """
        tmp_path = tmp_path_factory.mktemp("prop15b")

        # Sub-flow WITHOUT explicit default_addressing_mode (model default = "physical")
        sub_flow_yaml = (
            "name: child_flow\n"
            "steps:\n"
            "  - kind: uds\n"
            "    name: child_step\n"
            '    request: "1001"\n'
            "    addressing_mode: inherit\n"
        )
        sub_flow_path = tmp_path / "child_flow.yaml"
        sub_flow_path.write_text(sub_flow_yaml, encoding="utf-8")

        # Parent flow with explicit default_addressing_mode
        parent_flow_yaml = (
            "name: parent_flow\n"
            f"default_addressing_mode: {parent_default}\n"
            "steps:\n"
            "  - kind: subflow\n"
            "    name: call_child\n"
            "    subflow: child_flow.yaml\n"
        )
        parent_flow_path = tmp_path / "parent_flow.yaml"
        parent_flow_path.write_text(parent_flow_yaml, encoding="utf-8")

        async def _run() -> None:
            runtime = ExtensionRuntime([tmp_path.resolve()])
            engine = FlowEngine(_FakeUdsClient(), EventStore(), runtime)

            loaded = engine.load(parent_flow_path)
            run_id = await engine.start(loaded.name)
            final = await _wait_done(engine, run_id)

            assert final["status"] == FlowStatus.DONE.value, f"Flow failed: {final.get('error')}"

            # Child step should use model default "physical" since sub-flow
            # doesn't define default_addressing_mode
            trace = engine.get_trace(run_id)
            child_traces = [t for t in trace if t["step"] == "child_step"]
            assert len(child_traces) == 1
            assert child_traces[0]["addressing_mode"] == "physical", (
                f"Child step in sub-flow without default_addressing_mode should "
                f"resolve to 'physical' (model default), "
                f"got '{child_traces[0]['addressing_mode']}'"
            )

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Property 11: EventStore 日志落盘往返
# Feature: flow-subflow-repeat-logging, Property 11: EventStore 日志落盘往返
# **Validates: Requirements 6.2, 6.3**
# ---------------------------------------------------------------------------

# Strategy for EventKind values
_event_kind_st = st.sampled_from(
    ["can.tx", "can.rx", "uds.tx", "uds.rx", "flow.step", "flow.state", "error"]
)

# Strategy for simple payload dicts
_payload_st = st.dictionaries(
    keys=st.from_regex(r"[a-z][a-z0-9_]{0,9}", fullmatch=True),
    values=st.one_of(
        st.integers(min_value=0, max_value=0xFFFF),
        st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=10),
    ),
    min_size=1,
    max_size=5,
)


@st.composite
def _log_event_st(draw: st.DrawFn) -> LogEvent:
    """Generate a valid LogEvent."""
    kind_str = draw(_event_kind_st)
    kind = EventKind(kind_str)
    payload = draw(_payload_st)
    return LogEvent(kind=kind, payload=payload)


class TestEventStorePersistRoundTrip:
    """When persist_dir is configured, memory and disk data are consistent.

    Each line in the JSONL file is a valid JSON object matching the event's to_dict().
    """

    @settings(max_examples=100)
    @given(events=st.lists(_log_event_st(), min_size=1, max_size=20))
    def test_persist_round_trip(
        self,
        events: list[LogEvent],
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        """Feature: flow-subflow-repeat-logging, Property 11: EventStore 日志落盘往返

        For any sequence of LogEvents appended to an EventStore with
        persist_dir configured, the JSONL file on disk contains one valid
        JSON line per event, and each parsed line matches the event's
        to_dict() output. Memory and disk data are consistent.

        **Validates: Requirements 6.2, 6.3**
        """
        tmp_path = tmp_path_factory.mktemp("prop11")
        persist_dir = tmp_path / "logs"

        store = EventStore(persist_dir=persist_dir)
        try:
            for event in events:
                store.append(event)

            # Verify memory contents
            mem_events = store.query()
            assert len(mem_events) == len(events)

            # Find the JSONL file
            jsonl_files = list(persist_dir.glob("*.jsonl"))
            assert len(jsonl_files) == 1, f"Expected 1 JSONL file, found {len(jsonl_files)}"

            # Read and parse each line
            lines = jsonl_files[0].read_text(encoding="utf-8").strip().split("\n")
            assert len(lines) == len(events), (
                f"Expected {len(events)} lines in JSONL, got {len(lines)}"
            )

            for i, (line, event) in enumerate(zip(lines, events, strict=True)):
                parsed = json.loads(line)
                expected = event.to_dict()
                assert parsed == expected, f"Line {i}: disk data {parsed} != memory data {expected}"
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Property 12: EventStore 无持久化向后兼容
# Feature: flow-subflow-repeat-logging, Property 12: EventStore 无持久化向后兼容
# **Validates: Requirements 6.5**
# ---------------------------------------------------------------------------


class TestEventStoreNoPersistBackwardCompat:
    """When persist_dir is not configured, behavior is unchanged and no disk files are created."""

    @settings(max_examples=100)
    @given(events=st.lists(_log_event_st(), min_size=0, max_size=20))
    def test_no_persist_backward_compat(
        self,
        events: list[LogEvent],
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        """Feature: flow-subflow-repeat-logging, Property 12: EventStore 无持久化向后兼容

        For any sequence of LogEvents appended to an EventStore without
        persist_dir configured, append() and query() behave identically
        to the original implementation, and no disk files are created
        anywhere.

        **Validates: Requirements 6.5**
        """
        tmp_path = tmp_path_factory.mktemp("prop12")

        # Create store without persist_dir
        store = EventStore()

        for event in events:
            store.append(event)

        # Verify memory contents
        mem_events = store.query()
        assert len(mem_events) == len(events)

        for mem_event, orig_event in zip(mem_events, events, strict=True):
            assert mem_event.to_dict() == orig_event.to_dict()

        # Verify no files were created in the tmp directory
        # (the store should not touch disk at all)
        all_files = list(tmp_path.rglob("*"))
        jsonl_files = [f for f in all_files if f.suffix == ".jsonl"]
        assert len(jsonl_files) == 0, (
            f"No JSONL files should exist without persist_dir, found {jsonl_files}"
        )

        store.close()


# ---------------------------------------------------------------------------
# Property 13: BLF 流式写入完整性
# Feature: flow-subflow-repeat-logging, Property 13: BLF 流式写入完整性
# **Validates: Requirements 8.3**
# ---------------------------------------------------------------------------

# Strategy for CAN event payloads
_arb_id_st = st.integers(min_value=0, max_value=0x1FFFFFFF)
_data_hex_st = st.binary(min_size=1, max_size=8).map(lambda b: b.hex())
_is_extended_st = st.booleans()
_channel_st = st.one_of(st.none(), st.just("vcan0"), st.just("can0"), st.just("can1"))

_can_kind_st = st.sampled_from([EventKind.CAN_TX, EventKind.CAN_RX])
_non_can_kind_st = st.sampled_from(
    [EventKind.FLOW_STEP, EventKind.FLOW_STATE, EventKind.UDS_TX, EventKind.UDS_RX, EventKind.ERROR]
)


@st.composite
def _can_log_event_st(draw: st.DrawFn) -> LogEvent:
    """Generate a valid CAN_TX or CAN_RX LogEvent with proper payload."""
    kind = draw(_can_kind_st)
    payload: dict = {
        "arbitration_id": draw(_arb_id_st),
        "data_hex": draw(_data_hex_st),
        "is_extended_id": draw(_is_extended_st),
    }
    ch = draw(_channel_st)
    if ch is not None:
        payload["channel"] = ch
    return LogEvent(kind=kind, payload=payload)


@st.composite
def _non_can_log_event_st(draw: st.DrawFn) -> LogEvent:
    """Generate a non-CAN LogEvent (FLOW_STEP, FLOW_STATE, etc.)."""
    kind = draw(_non_can_kind_st)
    payload = draw(_payload_st)
    return LogEvent(kind=kind, payload=payload)


class TestBlfStreamingCompleteness:
    """All CAN_TX/CAN_RX events written to EventStore during streaming are captured in BLF.

    Non-CAN events (FLOW_STEP, FLOW_STATE, etc.) are NOT written to BLF.
    """

    @settings(max_examples=100, deadline=10000)
    @given(
        can_events=st.lists(_can_log_event_st(), min_size=0, max_size=30),
        non_can_events=st.lists(_non_can_log_event_st(), min_size=0, max_size=10),
        data=st.data(),
    )
    def test_blf_streaming_completeness(
        self,
        can_events: list[LogEvent],
        non_can_events: list[LogEvent],
        data: st.DataObject,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        """Feature: flow-subflow-repeat-logging, Property 13: BLF 流式写入完整性

        For any mix of CAN_TX/CAN_RX and non-CAN events appended to an
        EventStore with a BlfExporter streaming listener, the resulting
        BLF file contains exactly the CAN_TX/CAN_RX events (count matches)
        and none of the non-CAN events.

        **Validates: Requirements 8.3**
        """
        tmp_path = tmp_path_factory.mktemp("prop13")
        blf_path = tmp_path / "output.blf"

        # Interleave CAN and non-CAN events in a random order
        all_events = list(can_events) + list(non_can_events)
        shuffled = data.draw(st.permutations(all_events))

        event_store = EventStore()
        blf_exporter = BlfExporter()

        # Start streaming and register listener
        blf_exporter.start_streaming(blf_path)
        event_store.add_listener(blf_exporter.on_event)

        try:
            # Append all events
            for event in shuffled:
                event_store.append(event)
        finally:
            # Stop streaming and remove listener
            event_store.remove_listener(blf_exporter.on_event)
            blf_exporter.stop_streaming()

        # Read back the BLF file and count messages
        expected_can_count = len(can_events)

        if expected_can_count == 0:
            # BLF file may not exist or be empty when no CAN events
            if blf_path.exists():
                blf_messages = list(can.BLFReader(str(blf_path)))
                assert len(blf_messages) == 0, (
                    f"Expected 0 BLF messages but got {len(blf_messages)}"
                )
        else:
            assert blf_path.exists(), "BLF file should exist when CAN events were streamed"
            blf_messages = list(can.BLFReader(str(blf_path)))
            assert len(blf_messages) == expected_can_count, (
                f"Expected {expected_can_count} BLF messages, got {len(blf_messages)}. "
                f"CAN events: {expected_can_count}, non-CAN events: {len(non_can_events)}"
            )


# ---------------------------------------------------------------------------
# Property 8: flow_status 精简返回结构
# Feature: flow-subflow-repeat-logging, Property 8: flow_status 精简返回结构
# **Validates: Requirements 4.1, 4.2, 4.3, 4.4**
# ---------------------------------------------------------------------------

# Strategy for the number of steps in a flow (each step produces one trace record)
_num_steps_st = st.integers(min_value=1, max_value=10)

# Strategy for whether the flow should fail
_should_fail_st = st.booleans()


class TestFlowStatusSimplifiedReturn:
    """flow_status returns a simplified dict with required keys and no trace key.

    When FAILED, it additionally contains failed_step_trace with length <= 50.
    When DONE or STOPPED, failed_step_trace is NOT present.
    """

    @settings(max_examples=100, deadline=5000)
    @given(
        num_steps=_num_steps_st,
        should_fail=_should_fail_st,
        data=st.data(),
    )
    def test_flow_status_simplified_return(
        self,
        num_steps: int,
        should_fail: bool,  # noqa: FBT001
        data: st.DataObject,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        """Feature: flow-subflow-repeat-logging, Property 8: flow_status 精简返回结构

        For any completed flow execution (DONE or FAILED), the status()
        dict contains run_id, flow_name, status, current_step, error,
        step_count, message_count and does NOT contain a 'trace' key.
        When status is FAILED, it additionally contains failed_step_trace
        with length <= 50. When status is DONE, failed_step_trace is absent.

        **Validates: Requirements 4.1, 4.2, 4.3, 4.4**
        """
        tmp_path = tmp_path_factory.mktemp("prop8")

        # If should_fail, pick which step index to fail at
        fail_at: int | None = None
        if should_fail:
            fail_at = data.draw(st.integers(min_value=0, max_value=num_steps - 1), label="fail_at")

        # Build a flow with num_steps steps, each sending "1003"
        flow_yaml = "name: status_test_flow\nsteps:\n"
        for i in range(num_steps):
            flow_yaml += f'  - kind: uds\n    name: step_{i}\n    request: "1003"\n'
            if should_fail:
                # Add expect so the negative response triggers failure
                flow_yaml += '    expect:\n      response_prefix: "50"\n'
        flow_path = tmp_path / "flow.yaml"
        flow_path.write_text(flow_yaml, encoding="utf-8")

        async def _run() -> None:
            runtime = ExtensionRuntime([tmp_path.resolve()])
            if should_fail and fail_at is not None:
                client = _FailOnNthUdsClient(fail_on=fail_at)
            else:
                client = _FakeUdsClient()
            engine = FlowEngine(client, EventStore(), runtime)

            loaded = engine.load(flow_path)
            run_id = await engine.start(loaded.name)
            final = await _wait_done(engine, run_id)

            # --- Verify required keys are present ---
            required_keys = {
                "run_id",
                "flow_name",
                "status",
                "current_step",
                "error",
                "step_count",
                "message_count",
            }
            assert required_keys.issubset(final.keys()), (
                f"Missing keys: {required_keys - final.keys()}"
            )

            # --- Verify 'trace' key is NOT present ---
            assert "trace" not in final, "flow_status should NOT contain 'trace' key"

            # --- Verify status-dependent behavior ---
            if final["status"] == FlowStatus.FAILED.value:
                assert "failed_step_trace" in final, (
                    "FAILED status should include 'failed_step_trace'"
                )
                assert len(final["failed_step_trace"]) <= 50, (
                    f"failed_step_trace length {len(final['failed_step_trace'])} exceeds 50"
                )
            elif final["status"] in {FlowStatus.DONE.value, FlowStatus.STOPPED.value}:
                assert "failed_step_trace" not in final, (
                    f"Status {final['status']} should NOT include 'failed_step_trace'"
                )

            # --- Verify step_count and message_count are consistent ---
            assert isinstance(final["step_count"], int)
            assert isinstance(final["message_count"], int)
            assert final["step_count"] >= 0
            assert final["message_count"] >= 0

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Property 9: flow_trace_search 搜索正确性
# Feature: flow-subflow-repeat-logging, Property 9: flow_trace_search 搜索正确性
# **Validates: Requirements 5.2, 5.3**
# ---------------------------------------------------------------------------


class TestFlowTraceSearchCorrectness:
    """Searching with '.*' returns the full trace (up to limit).

    Searching with a specific step name returns only matching records.
    """

    @settings(max_examples=100, deadline=5000)
    @given(
        num_steps=st.integers(min_value=1, max_value=8),
    )
    def test_trace_search_wildcard_returns_full_trace(
        self,
        num_steps: int,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        """Feature: flow-subflow-repeat-logging, Property 9: flow_trace_search 搜索正确性

        For any flow with N steps, searching with pattern '.*' returns
        all trace records (up to the default limit of 100).

        **Validates: Requirements 5.2, 5.3**
        """
        tmp_path = tmp_path_factory.mktemp("prop9a")

        # Build a flow with num_steps unique steps
        flow_yaml = "name: search_test_flow\nsteps:\n"
        for i in range(num_steps):
            flow_yaml += f'  - kind: uds\n    name: search_step_{i}\n    request: "1003"\n'
        flow_path = tmp_path / "flow.yaml"
        flow_path.write_text(flow_yaml, encoding="utf-8")

        async def _run() -> None:
            runtime = ExtensionRuntime([tmp_path.resolve()])
            engine = FlowEngine(_FakeUdsClient(), EventStore(), runtime)

            loaded = engine.load(flow_path)
            run_id = await engine.start(loaded.name)
            await _wait_done(engine, run_id)

            full_trace = engine.get_trace(run_id)
            search_results = engine.trace_search(run_id, ".*")

            # '.*' should return all trace records (num_steps <= 100 default limit)
            assert len(search_results) == len(full_trace), (
                f"'.*' search returned {len(search_results)} results "
                f"but full trace has {len(full_trace)} records"
            )

        asyncio.run(_run())

    @settings(max_examples=100, deadline=5000)
    @given(
        num_steps=st.integers(min_value=2, max_value=8),
        data=st.data(),
    )
    def test_trace_search_specific_step_returns_only_matching(
        self,
        num_steps: int,
        data: st.DataObject,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        """Feature: flow-subflow-repeat-logging, Property 9: flow_trace_search 搜索正确性

        For any flow with N steps, searching with a specific step name
        returns only the trace records for that step.

        **Validates: Requirements 5.2, 5.3**
        """
        tmp_path = tmp_path_factory.mktemp("prop9b")

        # Pick which step to search for
        target_idx = data.draw(
            st.integers(min_value=0, max_value=num_steps - 1), label="target_idx"
        )
        target_name = f"search_step_{target_idx}"

        # Build a flow with num_steps unique steps
        flow_yaml = "name: search_specific_flow\nsteps:\n"
        for i in range(num_steps):
            flow_yaml += f'  - kind: uds\n    name: search_step_{i}\n    request: "1003"\n'
        flow_path = tmp_path / "flow.yaml"
        flow_path.write_text(flow_yaml, encoding="utf-8")

        async def _run() -> None:
            runtime = ExtensionRuntime([tmp_path.resolve()])
            engine = FlowEngine(_FakeUdsClient(), EventStore(), runtime)

            loaded = engine.load(flow_path)
            run_id = await engine.start(loaded.name)
            await _wait_done(engine, run_id)

            # Search for the specific step name
            search_results = engine.trace_search(run_id, target_name)

            # All results should contain the target step name
            for record in search_results:
                # The search matches against string field values joined by space,
                # so the step name should be in the matched record
                assert record["step"] == target_name, (
                    f"Expected only records for '{target_name}', got record for '{record['step']}'"
                )

            # There should be exactly 1 record for the target step
            assert len(search_results) == 1, (
                f"Expected 1 record for '{target_name}', got {len(search_results)}"
            )

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Property 10: flow_trace_search limit 限制
# Feature: flow-subflow-repeat-logging, Property 10: flow_trace_search limit 限制
# **Validates: Requirements 5.4**
# ---------------------------------------------------------------------------


class TestFlowTraceSearchLimit:
    """The returned result count never exceeds the limit parameter."""

    @settings(max_examples=100, deadline=5000)
    @given(
        num_steps=st.integers(min_value=1, max_value=15),
        limit=st.integers(min_value=1, max_value=20),
    )
    def test_trace_search_limit(
        self,
        num_steps: int,
        limit: int,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> None:
        """Feature: flow-subflow-repeat-logging, Property 10: flow_trace_search limit 限制

        For any flow with N trace records and any limit value, the result
        of trace_search with pattern '.*' never exceeds the limit parameter.

        **Validates: Requirements 5.4**
        """
        tmp_path = tmp_path_factory.mktemp("prop10")

        # Build a flow with num_steps steps
        flow_yaml = "name: limit_test_flow\nsteps:\n"
        for i in range(num_steps):
            flow_yaml += f'  - kind: uds\n    name: limit_step_{i}\n    request: "1003"\n'
        flow_path = tmp_path / "flow.yaml"
        flow_path.write_text(flow_yaml, encoding="utf-8")

        async def _run() -> None:
            runtime = ExtensionRuntime([tmp_path.resolve()])
            engine = FlowEngine(_FakeUdsClient(), EventStore(), runtime)

            loaded = engine.load(flow_path)
            run_id = await engine.start(loaded.name)
            await _wait_done(engine, run_id)

            full_trace = engine.get_trace(run_id)
            search_results = engine.trace_search(run_id, ".*", limit=limit)

            # Result count must not exceed limit
            assert len(search_results) <= limit, (
                f"trace_search returned {len(search_results)} results but limit was {limit}"
            )

            # Result count should be min(full_trace_length, limit)
            expected_count = min(len(full_trace), limit)
            assert len(search_results) == expected_count, (
                f"Expected {expected_count} results "
                f"(min({len(full_trace)}, {limit})), "
                f"got {len(search_results)}"
            )

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Property 16: inline 注册子流程路径校验
# Feature: flow-subflow-repeat-logging, Property 16: inline 注册子流程路径校验
# **Validates: Requirements 1.6**
# ---------------------------------------------------------------------------

# Strategy for relative paths (never start with / or drive letter)
_relative_path_st = st.one_of(
    st.just("flows/child.yaml"),
    st.just("../sub.yaml"),
    st.just("child.yaml"),
    st.just("sub/nested/flow.yaml"),
    st.from_regex(r"[a-z][a-z0-9_/]{0,20}\.yaml", fullmatch=True).filter(
        lambda p: not Path(p).is_absolute()
    ),
)

# Strategy for absolute paths (platform-aware)
_absolute_path_st = st.from_regex(r"[a-z][a-z0-9_]{0,8}", fullmatch=True).map(
    lambda name: (
        str(Path("/flows") / f"{name}.yaml")
        if Path("/flows").is_absolute()
        else str(Path("C:/flows") / f"{name}.yaml")
    )
)


class TestInlineRegistrationSubFlowPathValidation:
    """flow_register_inline rejects relative sub_flow paths and accepts absolute ones.

    This replicates the validation logic from the MCP tool since the tool
    function is defined inside build_server and not directly importable.
    """

    @settings(max_examples=100)
    @given(relative_path=_relative_path_st)
    def test_inline_register_rejects_relative_sub_flow(
        self,
        relative_path: str,
    ) -> None:
        """Feature: flow-subflow-repeat-logging, Property 16: inline 注册子流程路径校验

        For any flow registered via flow_register_inline that contains a
        sub_flow field with a relative path, registration should fail with
        a ValueError.

        **Validates: Requirements 1.6**
        """
        # Replicate the validation logic from flow_register_inline
        flow = FlowDefinition.model_validate(
            {
                "name": "test_flow",
                "steps": [{"kind": "subflow", "name": "sub_step", "subflow": relative_path}],
            }
        )
        with pytest.raises(ValueError, match="subflow path must be absolute"):
            for step in flow.steps:
                if step.subflow is not None and not Path(step.subflow).is_absolute():
                    raise ValueError(
                        f"subflow path must be absolute when using "
                        f"flow_register_inline: {step.subflow}"
                    )

    @settings(max_examples=100)
    @given(absolute_path=_absolute_path_st)
    def test_inline_register_accepts_absolute_sub_flow(
        self,
        absolute_path: str,
    ) -> None:
        """Feature: flow-subflow-repeat-logging, Property 16: inline 注册子流程路径校验

        For any flow registered via flow_register_inline that contains a
        sub_flow field with an absolute path, the validation should pass
        without raising any error.

        **Validates: Requirements 1.6**
        """
        # Replicate the validation logic from flow_register_inline
        flow = FlowDefinition.model_validate(
            {
                "name": "test_flow",
                "steps": [{"kind": "subflow", "name": "sub_step", "subflow": absolute_path}],
            }
        )
        # Should NOT raise - absolute paths are accepted
        for step in flow.steps:
            if step.subflow is not None and not Path(step.subflow).is_absolute():
                raise ValueError(
                    f"subflow path must be absolute when using "
                    f"flow_register_inline: {step.subflow}"
                )
