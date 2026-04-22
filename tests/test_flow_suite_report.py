from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from uds_mcp.flow.report import (
    FlowCaseReport,
    assemble_suite_report,
    build_suite_report,
    derive_case_diagnostics,
    write_html_report,
    write_json_report,
    write_junit_report,
    write_reports,
)
from uds_mcp.flow.suite import _discover_flow_paths, load_suite


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


def test_failure_diagnostics_extracts_expectation_context() -> None:
    status = {
        "status": "FAILED",
        "current_step": "security",
        "error": "step security: expect prefix 6701, got 7F2735",
        "failed_step_trace": [
            {
                "step": "security",
                "request_hex": "2701",
                "response_hex": "7F2735",
            }
        ],
    }

    diag = derive_case_diagnostics(status)

    assert diag["failure_reason"] == "response prefix mismatch: expected 6701, got 7F2735"
    assert diag["failure_step"] == "security"
    assert diag["expected_prefix"] == "6701"
    assert diag["actual_prefix"] == "7F2735"
    assert diag["last_request_hex"] == "2701"
    assert diag["last_response_hex"] == "7F2735"
    assert diag["assertions"][0]["name"] == "response_prefix"
    assert diag["assertions"][0]["passed"] is False


def test_failure_diagnostics_extracts_response_prefix_format() -> None:
    """The regex should also match 'expect response_prefix ...' format from engine."""
    status = {
        "status": "FAILED",
        "current_step": "sa",
        "error": "step sa [expect_response] fatal assertion failed: expect response_prefix 6701, got 7F2735",
    }

    diag = derive_case_diagnostics(status)

    assert diag["expected_prefix"] == "6701"
    assert diag["actual_prefix"] == "7F2735"
    assert diag["failure_reason"] == "response prefix mismatch: expected 6701, got 7F2735"


def test_derive_diagnostics_structured_detail() -> None:
    """When assertion_details contains structured data, use it directly."""
    status = {
        "status": "FAILED",
        "current_step": "sa",
        "error": "step sa [expect_response] fatal assertion failed: expect response_prefix 6701, got 7F2735",
        "assertion_details": [
            {
                "step": "sa",
                "phase": "expect_response",
                "name": "expect.response_prefix",
                "on_fail": "fatal",
                "ok": False,
                "message": "expect response_prefix 6701, got 7F2735",
                "request_hex": "2701",
                "response_hex": "7F2735",
                "detail": {"expected": "6701", "actual": "7F2735"},
            }
        ],
    }

    diag = derive_case_diagnostics(status)

    assert diag["expected_prefix"] == "6701"
    assert diag["actual_prefix"] == "7F2735"
    assert diag["failure_step"] == "sa"
    assert diag["last_request_hex"] == "2701"
    assert diag["last_response_hex"] == "7F2735"


def test_report_exporters_include_failure_reason_and_assertions(tmp_path: Path) -> None:
    report = build_suite_report(
        suite_name="failures",
        total=1,
        cases=[
            FlowCaseReport(
                flow_name="sa",
                flow_path="sa.yaml",
                run_id="r1",
                status="FAILED",
                passed=False,
                duration_ms=15,
                step_count=1,
                message_count=1,
                error="step sa: expect prefix 6701, got 7F2735",
                current_step="sa",
                failure_reason="response prefix mismatch: expected 6701, got 7F2735",
                failure_step="sa",
                expected_prefix="6701",
                actual_prefix="7F2735",
                last_request_hex="2701",
                last_response_hex="7F2735",
                assertions=[
                    {
                        "name": "response_prefix",
                        "passed": False,
                        "expected": "6701",
                        "actual": "7F2735",
                    }
                ],
            )
        ],
        started_at=datetime.now(UTC),
        ended_at=datetime.now(UTC),
    )

    json_path = tmp_path / "report_fail.json"
    html_path = tmp_path / "report_fail.html"
    junit_path = tmp_path / "report_fail.xml"

    write_json_report(json_path, report)
    write_html_report(html_path, report)
    write_junit_report(junit_path, report)

    assert "response prefix mismatch" in json_path.read_text(encoding="utf-8")
    assert "assertions" in html_path.read_text(encoding="utf-8")
    assert "failure_reason" in junit_path.read_text(encoding="utf-8")


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
        "name: smoke\n"
        "include:\n"
        "  - flows/*.yaml\n"
        "exclude:\n"
        "  - '*skip*'\n"
        "timeout_s: 2.5\n"
        "stop_on_fail: true\n",
        encoding="utf-8",
    )

    loaded = load_suite(suite_path)

    assert loaded.name == "smoke"
    assert loaded.include == ["flows/*.yaml"]
    assert loaded.exclude == ["*skip*"]
    assert loaded.timeout_s == 2.5
    assert loaded.stop_on_fail is True


def test_write_html_report_uses_template(tmp_path: Path) -> None:
    report = build_suite_report(
        suite_name="template-test",
        total=1,
        cases=[
            FlowCaseReport(
                flow_name="f1",
                flow_path="f1.yaml",
                run_id="r1",
                status="DONE",
                passed=True,
                duration_ms=10,
                step_count=1,
                message_count=1,
                error=None,
                current_step=None,
            )
        ],
        started_at=datetime.now(UTC),
        ended_at=datetime.now(UTC),
    )

    html_path = tmp_path / "report.html"
    write_html_report(html_path, report)

    content = html_path.read_text(encoding="utf-8")
    assert "Flow Suite Report - template-test" in content
    assert "f1" in content
    assert "PASS" in content


def test_assemble_suite_report_from_status_list() -> None:
    started = datetime.now(UTC)
    ended = started + timedelta(milliseconds=100)
    status_list = [
        {
            "flow_name": "a",
            "flow_path": "a.yaml",
            "run_id": "r1",
            "status": "DONE",
            "current_step": None,
            "error": None,
            "step_count": 2,
            "message_count": 2,
            "duration_ms": 50,
        },
        {
            "flow_name": "b",
            "flow_path": "b.yaml",
            "run_id": "r2",
            "status": "FAILED",
            "current_step": "step_b",
            "error": "oops",
            "step_count": 1,
            "message_count": 1,
            "duration_ms": 30,
        },
    ]

    report = assemble_suite_report(
        suite_name="assembled",
        total=2,
        status_list=status_list,
        started_at=started,
        ended_at=ended,
    )

    assert report.suite_name == "assembled"
    assert report.total == 2
    assert report.passed == 1
    assert report.failed == 1
    assert len(report.cases) == 2
    assert report.cases[0].passed is True
    assert report.cases[1].passed is False
    assert report.cases[1].failure_reason == "oops"


def test_write_reports_all_formats(tmp_path: Path) -> None:
    report = build_suite_report(
        suite_name="all-fmt",
        total=1,
        cases=[
            FlowCaseReport(
                flow_name="x",
                flow_path="x.yaml",
                run_id="rx",
                status="DONE",
                passed=True,
                duration_ms=5,
                step_count=1,
                message_count=1,
                error=None,
                current_step=None,
            )
        ],
        started_at=datetime.now(UTC),
        ended_at=datetime.now(UTC),
    )

    outputs = write_reports(
        report,
        json_path=tmp_path / "r.json",
        html_path=tmp_path / "r.html",
        junit_path=tmp_path / "r.xml",
    )

    assert "json" in outputs
    assert "html" in outputs
    assert "junit" in outputs
    assert (tmp_path / "r.json").exists()
    assert (tmp_path / "r.html").exists()
    assert (tmp_path / "r.xml").exists()


def test_write_reports_partial(tmp_path: Path) -> None:
    report = build_suite_report(
        suite_name="partial",
        total=0,
        cases=[],
        started_at=datetime.now(UTC),
        ended_at=datetime.now(UTC),
    )

    outputs = write_reports(report, json_path=tmp_path / "only.json")

    assert list(outputs.keys()) == ["json"]
    assert not (tmp_path / "r.html").exists()
