"""Tests for uds_mcp.flow.suite — suite definition loading and resolution."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from uds_mcp.flow.suite import (
    SuiteCase,
    SuiteDefinition,
    _discover_flow_paths,
    load_suite,
    resolve_suite,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_flow(path: Path, name: str) -> Path:
    path.write_text(f"name: {name}\nsteps: []\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# SuiteDefinition parsing
# ---------------------------------------------------------------------------


def test_load_suite_minimal(tmp_path: Path) -> None:
    suite_path = tmp_path / "suite.yaml"
    suite_path.write_text("name: minimal\ninclude:\n  - '*.yaml'\n", encoding="utf-8")

    sd = load_suite(suite_path)

    assert sd.name == "minimal"
    assert sd.include == ["*.yaml"]
    assert sd.exclude == []
    assert sd.cases == []
    assert sd.setup is None
    assert sd.teardown is None
    assert sd.timeout_s is None
    assert sd.stop_on_fail is False


def test_load_suite_full(tmp_path: Path) -> None:
    suite_path = tmp_path / "suite.yaml"
    suite_path.write_text(
        "name: full\n"
        "setup: flows/setup.yaml\n"
        "teardown: flows/teardown.yaml\n"
        "timeout_s: 30\n"
        "stop_on_fail: true\n"
        "tags:\n  - smoke\n"
        "include:\n  - flows/*.yaml\n"
        "exclude:\n  - '*wip*'\n"
        "cases:\n"
        "  - flow: flows/a.yaml\n"
        "    tags: [smoke, did]\n"
        "    timeout_s: 10\n"
        "  - flow: flows/b.yaml\n"
        "    variables:\n"
        "      key: value\n"
        "    retry: 2\n",
        encoding="utf-8",
    )

    sd = load_suite(suite_path)

    assert sd.name == "full"
    assert sd.setup == "flows/setup.yaml"
    assert sd.teardown == "flows/teardown.yaml"
    assert sd.timeout_s == 30
    assert sd.stop_on_fail is True
    assert sd.tags == ["smoke"]
    assert sd.include == ["flows/*.yaml"]
    assert sd.exclude == ["*wip*"]
    assert len(sd.cases) == 2
    assert sd.cases[0].flow == "flows/a.yaml"
    assert sd.cases[0].tags == ["smoke", "did"]
    assert sd.cases[0].timeout_s == 10
    assert sd.cases[1].retry == 2
    assert sd.cases[1].variables == {"key": "value"}


def test_load_suite_rejects_non_mapping(tmp_path: Path) -> None:
    suite_path = tmp_path / "suite.yaml"
    suite_path.write_text("- item\n", encoding="utf-8")

    with pytest.raises(TypeError, match="mapping"):
        load_suite(suite_path)


def test_load_suite_backward_compat(tmp_path: Path) -> None:
    """Old-format suite YAML (only include/exclude/name) still loads."""
    suite_path = tmp_path / "suite.yaml"
    suite_path.write_text(
        "name: compat\n"
        "include:\n  - flows/*.yaml\n"
        "exclude:\n  - '*skip*'\n"
        "timeout_s: 2.5\n"
        "stop_on_fail: true\n",
        encoding="utf-8",
    )

    sd = load_suite(suite_path)

    assert sd.name == "compat"
    assert sd.include == ["flows/*.yaml"]
    assert sd.exclude == ["*skip*"]
    assert sd.timeout_s == 2.5
    assert sd.stop_on_fail is True
    assert sd.cases == []
    assert sd.setup is None


# ---------------------------------------------------------------------------
# resolve_suite
# ---------------------------------------------------------------------------


def test_resolve_suite_explicit_cases(tmp_path: Path) -> None:
    flows_dir = tmp_path / "flows"
    flows_dir.mkdir()
    a = _write_flow(flows_dir / "a.yaml", "a")
    b = _write_flow(flows_dir / "b.yaml", "b")

    sd = SuiteDefinition(
        name="explicit",
        cases=[
            SuiteCase(flow="flows/b.yaml", tags=["smoke"]),
            SuiteCase(flow="flows/a.yaml", tags=["regression"], timeout_s=5, retry=1),
        ],
    )

    resolved = resolve_suite(sd, base_dir=tmp_path)

    assert len(resolved) == 2
    assert resolved[0].flow_path == b.resolve()
    assert resolved[0].tags == ["smoke"]
    assert resolved[1].flow_path == a.resolve()
    assert resolved[1].timeout_s == 5
    assert resolved[1].retry == 1


def test_resolve_suite_include_discovery(tmp_path: Path) -> None:
    flows_dir = tmp_path / "flows"
    flows_dir.mkdir()
    _write_flow(flows_dir / "a.yaml", "a")
    _write_flow(flows_dir / "b.yaml", "b")

    sd = SuiteDefinition(name="disc", include=["flows/*.yaml"])

    resolved = resolve_suite(sd, base_dir=tmp_path)

    assert len(resolved) == 2
    names = [r.flow_path.stem for r in resolved]
    assert "a" in names
    assert "b" in names


def test_resolve_suite_cases_and_include_merged(tmp_path: Path) -> None:
    """Explicit cases come first, include adds non-duplicates."""
    flows_dir = tmp_path / "flows"
    flows_dir.mkdir()
    a = _write_flow(flows_dir / "a.yaml", "a")
    _write_flow(flows_dir / "b.yaml", "b")
    _write_flow(flows_dir / "c.yaml", "c")

    sd = SuiteDefinition(
        name="merged",
        cases=[SuiteCase(flow="flows/a.yaml", tags=["smoke"])],
        include=["flows/*.yaml"],
    )

    resolved = resolve_suite(sd, base_dir=tmp_path)

    assert resolved[0].flow_path == a.resolve()
    assert resolved[0].tags == ["smoke"]
    assert len(resolved) == 3


def test_resolve_suite_exclude(tmp_path: Path) -> None:
    flows_dir = tmp_path / "flows"
    flows_dir.mkdir()
    _write_flow(flows_dir / "a.yaml", "a")
    _write_flow(flows_dir / "wip_b.yaml", "b")

    sd = SuiteDefinition(name="excl", include=["flows/*.yaml"], exclude=["*wip*"])

    resolved = resolve_suite(sd, base_dir=tmp_path)

    assert len(resolved) == 1
    assert resolved[0].flow_path.stem == "a"


# ---------------------------------------------------------------------------
# Tag filtering
# ---------------------------------------------------------------------------


def test_resolve_suite_tag_filter(tmp_path: Path) -> None:
    flows_dir = tmp_path / "flows"
    flows_dir.mkdir()
    _write_flow(flows_dir / "a.yaml", "a")
    _write_flow(flows_dir / "b.yaml", "b")

    sd = SuiteDefinition(
        name="tagged",
        cases=[
            SuiteCase(flow="flows/a.yaml", tags=["smoke"]),
            SuiteCase(flow="flows/b.yaml", tags=["regression"]),
        ],
    )

    resolved = resolve_suite(sd, base_dir=tmp_path, tag_filter=["smoke"])

    assert len(resolved) == 1
    assert resolved[0].flow_path.stem == "a"


def test_resolve_suite_tag_filter_empty_result(tmp_path: Path) -> None:
    flows_dir = tmp_path / "flows"
    flows_dir.mkdir()
    _write_flow(flows_dir / "a.yaml", "a")

    sd = SuiteDefinition(
        name="empty",
        cases=[SuiteCase(flow="flows/a.yaml", tags=["smoke"])],
    )

    resolved = resolve_suite(sd, base_dir=tmp_path, tag_filter=["nonexistent"])

    assert len(resolved) == 0


# ---------------------------------------------------------------------------
# Per-case variables
# ---------------------------------------------------------------------------


def test_resolved_case_variables(tmp_path: Path) -> None:
    flows_dir = tmp_path / "flows"
    flows_dir.mkdir()
    _write_flow(flows_dir / "a.yaml", "a")

    sd = SuiteDefinition(
        name="vars",
        cases=[SuiteCase(flow="flows/a.yaml", variables={"key": "val"})],
    )

    resolved = resolve_suite(sd, base_dir=tmp_path)

    assert resolved[0].variables == {"key": "val"}


# ---------------------------------------------------------------------------
# discover_flow_paths
# ---------------------------------------------------------------------------


def test_discover_flow_paths_glob_and_exclude(tmp_path: Path) -> None:
    flow_dir = tmp_path / "flows"
    flow_dir.mkdir()
    keep = _write_flow(flow_dir / "a.yaml", "a")
    _write_flow(flow_dir / "skip_me.yaml", "b")

    found = _discover_flow_paths(
        ["flows/*.yaml"],
        exclude_patterns=["*skip_me.yaml"],
        base_dir=tmp_path,
    )

    assert found == [keep.resolve()]


def test_resolve_suite_missing_case_file(tmp_path: Path) -> None:
    sd = SuiteDefinition(
        name="missing",
        cases=[SuiteCase(flow="nonexistent.yaml")],
    )

    with pytest.raises(FileNotFoundError, match="nonexistent"):
        resolve_suite(sd, base_dir=tmp_path)
