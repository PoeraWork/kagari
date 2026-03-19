from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from uds_mcp.cli import _discover_flow_paths, _load_suite_file
from uds_mcp.flow.report import (
    FlowCaseReport,
    build_suite_report,
    write_html_report,
    write_json_report,
    write_junit_report,
)


def test_build_suite_report_calculates_pass_rate_and_skipped() -> None:
    started = datetime.now(UTC)
    ended = started + timedelta(milliseconds=120)
    report = build_suite_report(
        suite_name="smoke",
        total=3,
        cases=[
            FlowCaseReport(
                flow_name="a",
                flow_path="a.yaml",
                run_id="1",
                status="DONE",
                passed=True,
                duration_ms=40,
                step_count=1,
                message_count=1,
                error=None,
                current_step=None,
            ),
            FlowCaseReport(
                flow_name="b",
                flow_path="b.yaml",
                run_id="2",
                status="FAILED",
                passed=False,
                duration_ms=60,
                step_count=1,
                message_count=1,
                error="boom",
                current_step="step_b",
            ),
        ],
        started_at=started,
        ended_at=ended,
    )

    assert report.total == 3
    assert report.executed == 2
    assert report.passed == 1
    assert report.failed == 1
    assert report.skipped == 1
    assert report.pass_rate == 50.0
    assert report.duration_ms == 120


def test_report_exporters_emit_files(tmp_path: Path) -> None:
    report = build_suite_report(
        suite_name="regression",
        total=1,
        cases=[
            FlowCaseReport(
                flow_name="only",
                flow_path="only.yaml",
                run_id="abc",
                status="DONE",
                passed=True,
                duration_ms=25,
                step_count=2,
                message_count=2,
                error=None,
                current_step=None,
            )
        ],
        started_at=datetime.now(UTC),
        ended_at=datetime.now(UTC),
    )

    json_path = tmp_path / "report.json"
    html_path = tmp_path / "report.html"
    junit_path = tmp_path / "report.xml"

    write_json_report(json_path, report)
    write_html_report(html_path, report)
    write_junit_report(junit_path, report)

    assert '"suite_name": "regression"' in json_path.read_text(encoding="utf-8")
    assert "Flow Suite Report - regression" in html_path.read_text(encoding="utf-8")
    assert "<testsuite" in junit_path.read_text(encoding="utf-8")


def test_discover_flow_paths_with_glob_and_exclude(tmp_path: Path) -> None:
    flow_dir = tmp_path / "flows"
    flow_dir.mkdir()
    keep = flow_dir / "a.yaml"
    skip = flow_dir / "skip_me.yaml"
    keep.write_text("name: a\nsteps: []\n", encoding="utf-8")
    skip.write_text("name: b\nsteps: []\n", encoding="utf-8")

    found = _discover_flow_paths(
        ["flows/*.yaml"],
        exclude_patterns=["*skip_me.yaml"],
        base_dir=tmp_path,
    )

    assert found == [keep.resolve()]


def test_load_suite_file_parses_fields(tmp_path: Path) -> None:
    suite_path = tmp_path / "suite.yaml"
    suite_path.write_text(
        "\n".join(
            [
                "name: smoke",
                "include:",
                "  - flows/*.yaml",
                "exclude:",
                "  - '*skip*'",
                "timeout_s: 2.5",
                "stop_on_fail: true",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    loaded = _load_suite_file(suite_path)

    assert loaded["suite_name"] == "smoke"
    assert loaded["include_specs"] == ["flows/*.yaml"]
    assert loaded["exclude_patterns"] == ["*skip*"]
    assert loaded["timeout_s"] == 2.5
    assert loaded["stop_on_fail"] is True
    assert loaded["base_dir"] == suite_path.resolve().parent
