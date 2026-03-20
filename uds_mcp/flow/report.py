from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


@dataclass(slots=True)
class FlowCaseReport:
    flow_name: str
    flow_path: str
    run_id: str
    status: str
    passed: bool
    duration_ms: int
    step_count: int
    message_count: int
    error: str | None
    current_step: str | None
    trace: list[dict[str, Any]] | None = None
    failure_reason: str | None = None
    failure_step: str | None = None
    expected_prefix: str | None = None
    actual_prefix: str | None = None
    last_request_hex: str | None = None
    last_response_hex: str | None = None
    failed_step_trace: list[dict[str, Any]] | None = None
    assertions: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class FlowSuiteReport:
    suite_name: str
    generated_at: str
    total: int
    executed: int
    passed: int
    failed: int
    skipped: int
    pass_rate: float
    duration_ms: int
    cases: list[FlowCaseReport]

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite_name": self.suite_name,
            "generated_at": self.generated_at,
            "total": self.total,
            "executed": self.executed,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "pass_rate": self.pass_rate,
            "duration_ms": self.duration_ms,
            "cases": [asdict(case) for case in self.cases],
        }


def build_suite_report(
    *,
    suite_name: str,
    total: int,
    cases: list[FlowCaseReport],
    started_at: datetime,
    ended_at: datetime,
) -> FlowSuiteReport:
    executed = len(cases)
    passed = sum(1 for case in cases if case.passed)
    failed = executed - passed
    skipped = max(total - executed, 0)
    pass_rate = round((passed / executed) * 100.0, 2) if executed > 0 else 0.0
    duration_ms = max(int((ended_at - started_at).total_seconds() * 1000), 0)
    return FlowSuiteReport(
        suite_name=suite_name,
        generated_at=ended_at.astimezone(UTC).isoformat(),
        total=total,
        executed=executed,
        passed=passed,
        failed=failed,
        skipped=skipped,
        pass_rate=pass_rate,
        duration_ms=duration_ms,
        cases=cases,
    )


def write_json_report(path: Path, report: FlowSuiteReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report.to_dict(), ensure_ascii=True, indent=2)
    path.write_text(payload + "\n", encoding="utf-8")


def write_junit_report(path: Path, report: FlowSuiteReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    suite = ElementTree.Element(
        "testsuite",
        {
            "name": report.suite_name,
            "tests": str(report.executed),
            "failures": str(report.failed),
            "errors": "0",
            "skipped": str(report.skipped),
            "time": f"{report.duration_ms / 1000:.3f}",
        },
    )

    for case in report.cases:
        testcase = ElementTree.SubElement(
            suite,
            "testcase",
            {
                "classname": report.suite_name,
                "name": case.flow_name,
                "time": f"{case.duration_ms / 1000:.3f}",
            },
        )
        properties = ElementTree.SubElement(testcase, "properties")
        ElementTree.SubElement(
            properties,
            "property",
            {"name": "flow_path", "value": case.flow_path},
        )
        ElementTree.SubElement(
            properties,
            "property",
            {"name": "run_id", "value": case.run_id},
        )
        ElementTree.SubElement(
            properties,
            "property",
            {"name": "status", "value": case.status},
        )
        if case.failure_reason is not None:
            ElementTree.SubElement(
                properties,
                "property",
                {"name": "failure_reason", "value": case.failure_reason},
            )
        if case.failure_step is not None:
            ElementTree.SubElement(
                properties,
                "property",
                {"name": "failure_step", "value": case.failure_step},
            )
        if case.expected_prefix is not None:
            ElementTree.SubElement(
                properties,
                "property",
                {"name": "expected_prefix", "value": case.expected_prefix},
            )
        if case.actual_prefix is not None:
            ElementTree.SubElement(
                properties,
                "property",
                {"name": "actual_prefix", "value": case.actual_prefix},
            )

        if not case.passed:
            failure = ElementTree.SubElement(
                testcase,
                "failure",
                {
                    "message": case.failure_reason
                    or case.error
                    or f"flow ended with status {case.status}",
                    "type": "FlowFailure",
                },
            )
            lines = [
                f"status={case.status}",
                f"current_step={case.current_step}",
                f"failure_step={case.failure_step}",
                f"failure_reason={case.failure_reason}",
                f"expected_prefix={case.expected_prefix}",
                f"actual_prefix={case.actual_prefix}",
                f"last_request_hex={case.last_request_hex}",
                f"last_response_hex={case.last_response_hex}",
                f"error={case.error}",
            ]
            if case.assertions:
                lines.append("assertions=" + json.dumps(case.assertions, ensure_ascii=True))
            failure.text = "\n".join(lines)

    xml_bytes = ElementTree.tostring(suite, encoding="utf-8", xml_declaration=True)
    path.write_bytes(xml_bytes)


def write_html_report(path: Path, report: FlowSuiteReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[str] = []
    for case in report.cases:
        status_class = "ok" if case.passed else "bad"
        rows.append(
            "<tr>"
            f"<td>{escape(case.flow_name)}</td>"
            f"<td>{escape(case.status)}</td>"
            f"<td class='{status_class}'>{'PASS' if case.passed else 'FAIL'}</td>"
            f"<td>{case.duration_ms}</td>"
            f"<td>{case.step_count}</td>"
            f"<td>{case.message_count}</td>"
            f"<td>{escape(case.current_step or '-')}</td>"
            "<td>"
            f"<div>{escape(case.failure_reason or case.error or '-')}</div>"
            f"<div><small>step={escape(case.failure_step or '-')} req={escape(case.last_request_hex or '-')} rsp={escape(case.last_response_hex or '-')}</small></div>"
            f"<details><summary>assertions</summary><pre>{escape(json.dumps(case.assertions, ensure_ascii=True, indent=2))}</pre></details>"
            "</td>"
            "</tr>"
        )

        html = """<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Flow Suite Report</title>
  <style>
    :root {
      --bg: #f6f8fa;
      --card: #ffffff;
      --text: #0f172a;
      --ok: #15803d;
      --bad: #b91c1c;
      --muted: #475569;
      --border: #dbe3eb;
    }
    body {
      margin: 0;
      font-family: "Segoe UI", "Noto Sans", sans-serif;
      background: linear-gradient(140deg, #eef4ff, var(--bg));
      color: var(--text);
    }
    .wrap { max-width: 1080px; margin: 24px auto; padding: 0 16px; }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 18px;
      box-shadow: 0 8px 24px rgba(15, 23, 42, 0.08);
    }
    h1 { margin: 0 0 8px; font-size: 22px; }
    .muted { color: var(--muted); margin: 0 0 16px; }
    .kpi {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 10px;
      margin-bottom: 18px;
    }
    .kpi div { border: 1px solid var(--border); border-radius: 10px; padding: 10px; }
    table { width: 100%; border-collapse: collapse; font-size: 14px; }
    th, td { border-bottom: 1px solid var(--border); text-align: left; padding: 8px; vertical-align: top; }
    th { background: #f8fbff; }
    .ok { color: var(--ok); font-weight: 600; }
    .bad { color: var(--bad); font-weight: 600; }
  </style>
</head>
<body>
  <div class=\"wrap\">
    <div class=\"card\">
      <h1>Flow Suite Report - __SUITE_NAME__</h1>
      <p class=\"muted\">Generated at __GENERATED_AT__</p>
      <div class=\"kpi\">
        <div><strong>Total</strong><br/>__TOTAL__</div>
        <div><strong>Executed</strong><br/>__EXECUTED__</div>
        <div><strong>Passed</strong><br/>__PASSED__</div>
        <div><strong>Failed</strong><br/>__FAILED__</div>
        <div><strong>Skipped</strong><br/>__SKIPPED__</div>
        <div><strong>Pass Rate</strong><br/>__PASS_RATE__%</div>
        <div><strong>Duration</strong><br/>__DURATION_MS__ ms</div>
      </div>
      <table>
        <thead>
          <tr>
            <th>Flow</th>
            <th>Status</th>
            <th>Result</th>
            <th>Duration (ms)</th>
            <th>Steps</th>
            <th>Messages</th>
            <th>Current Step</th>
            <th>Error</th>
          </tr>
        </thead>
        <tbody>
          __ROWS__
        </tbody>
      </table>
    </div>
  </div>
</body>
</html>
"""
        html = (
            html.replace("__SUITE_NAME__", escape(report.suite_name))
            .replace("__GENERATED_AT__", escape(report.generated_at))
            .replace("__TOTAL__", str(report.total))
            .replace("__EXECUTED__", str(report.executed))
            .replace("__PASSED__", str(report.passed))
            .replace("__FAILED__", str(report.failed))
            .replace("__SKIPPED__", str(report.skipped))
            .replace("__PASS_RATE__", f"{report.pass_rate:.2f}")
            .replace("__DURATION_MS__", str(report.duration_ms))
            .replace("__ROWS__", "\n".join(rows))
        )
        path.write_text(html, encoding="utf-8")
