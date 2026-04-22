from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from string import Template
from typing import Any
from xml.etree import ElementTree as ET

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


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


# ---------------------------------------------------------------------------
# Diagnostics: extract structured failure info from engine status dict
# ---------------------------------------------------------------------------


def derive_case_diagnostics(status: dict[str, Any]) -> dict[str, Any]:
    """Extract structured failure diagnostics from an engine status dict.

    Prefers structured ``assertion_details`` when available, falling back to
    regex parsing of the error string for backward compatibility.
    """
    error = str(status["error"]) if status.get("error") is not None else None
    state = str(status.get("status") or "")

    failed_trace_raw = status.get("failed_step_trace")
    failed_trace = failed_trace_raw if isinstance(failed_trace_raw, list) else []
    last_item: dict[str, Any] | None = None
    for item in reversed(failed_trace):
        if isinstance(item, dict):
            last_item = item
            break

    failure_step = None
    last_request_hex = None
    last_response_hex = None
    if last_item is not None:
        failure_step = _safe_str(last_item.get("step"))
        last_request_hex = _safe_str(last_item.get("request_hex"))
        last_response_hex = _safe_str(last_item.get("response_hex"))

    # --- try structured assertion_details first ---
    expected_prefix = None
    actual_prefix = None
    assertion_details: list[dict[str, Any]] = status.get("assertion_details") or []
    for ad in assertion_details:
        if ad.get("ok"):
            continue
        detail = ad.get("detail") or {}
        name = ad.get("name") or ""
        if "response_prefix" in name and detail.get("expected") and detail.get("actual"):
            expected_prefix = str(detail["expected"]).upper()
            actual_prefix = str(detail["actual"]).upper()
            if failure_step is None:
                failure_step = _safe_str(ad.get("step"))
            if last_request_hex is None:
                last_request_hex = _safe_str(ad.get("request_hex"))
            if last_response_hex is None:
                last_response_hex = _safe_str(ad.get("response_hex"))
            break

    # --- fallback: regex parse error string ---
    if expected_prefix is None and error is not None:
        match = re.search(
            r"expect (?:response_)?prefix\s+([0-9A-Fa-f]+),\s+got\s+([0-9A-Fa-f]+)", error
        )
        if match:
            expected_prefix = match.group(1).upper()
            actual_prefix = match.group(2).upper()

    # --- build assertions list ---
    assertions: list[dict[str, Any]] = []
    if expected_prefix is not None or actual_prefix is not None:
        assertions.append(
            {
                "name": "response_prefix",
                "passed": False,
                "expected": expected_prefix,
                "actual": actual_prefix,
                "message": error,
            }
        )
    elif state == "TIMEOUT":
        assertions.append(
            {
                "name": "flow_timeout",
                "passed": False,
                "expected": "flow completed before timeout",
                "actual": "timeout reached",
                "message": error,
            }
        )
    elif state in {"FAILED", "STOPPED"}:
        assertions.append(
            {
                "name": "flow_status",
                "passed": False,
                "expected": "DONE",
                "actual": state,
                "message": error,
            }
        )

    failure_reason = None
    if state == "DONE":
        failure_reason = None
    elif expected_prefix is not None and actual_prefix is not None:
        failure_reason = (
            f"response prefix mismatch: expected {expected_prefix}, got {actual_prefix}"
        )
    elif state == "TIMEOUT":
        failure_reason = error or "flow timeout"
    else:
        failure_reason = error or f"flow ended with status {state}"

    return {
        "failure_reason": failure_reason,
        "failure_step": failure_step or _safe_str(status.get("current_step")),
        "expected_prefix": expected_prefix,
        "actual_prefix": actual_prefix,
        "last_request_hex": last_request_hex,
        "last_response_hex": last_response_hex,
        "failed_step_trace": failed_trace or None,
        "assertions": assertions,
    }


def _safe_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


# ---------------------------------------------------------------------------
# High-level report assembly
# ---------------------------------------------------------------------------


def assemble_suite_report(
    *,
    suite_name: str,
    total: int,
    status_list: list[dict[str, Any]],
    started_at: datetime,
    ended_at: datetime,
) -> FlowSuiteReport:
    """Build a ``FlowSuiteReport`` from a list of raw engine status dicts."""
    cases: list[FlowCaseReport] = []
    for status in status_list:
        diagnostics = derive_case_diagnostics(status)
        case_status = str(status["status"])
        passed = case_status == "DONE"
        cases.append(
            FlowCaseReport(
                flow_name=str(status.get("flow_name") or ""),
                flow_path=str(status.get("flow_path") or ""),
                run_id=str(status.get("run_id") or ""),
                status=case_status,
                passed=passed,
                duration_ms=int(status.get("duration_ms") or 0),
                step_count=int(status.get("step_count") or 0),
                message_count=int(status.get("message_count") or 0),
                error=(str(status["error"]) if status.get("error") is not None else None),
                current_step=(
                    str(status["current_step"])
                    if status.get("current_step") is not None
                    else None
                ),
                trace=status.get("trace") if isinstance(status.get("trace"), list) else None,
                failure_reason=diagnostics["failure_reason"],
                failure_step=diagnostics["failure_step"],
                expected_prefix=diagnostics["expected_prefix"],
                actual_prefix=diagnostics["actual_prefix"],
                last_request_hex=diagnostics["last_request_hex"],
                last_response_hex=diagnostics["last_response_hex"],
                failed_step_trace=diagnostics["failed_step_trace"],
                assertions=diagnostics["assertions"],
            )
        )
    return build_suite_report(
        suite_name=suite_name,
        total=total,
        cases=cases,
        started_at=started_at,
        ended_at=ended_at,
    )


def write_reports(
    report: FlowSuiteReport,
    *,
    json_path: Path | None = None,
    html_path: Path | None = None,
    junit_path: Path | None = None,
) -> dict[str, str]:
    """Write report to all requested formats and return a mapping of format→path."""
    outputs: dict[str, str] = {}
    if json_path is not None:
        write_json_report(json_path, report)
        outputs["json"] = json_path.as_posix()
    if html_path is not None:
        write_html_report(html_path, report)
        outputs["html"] = html_path.as_posix()
    if junit_path is not None:
        write_junit_report(junit_path, report)
        outputs["junit"] = junit_path.as_posix()
    return outputs


# ---------------------------------------------------------------------------
# Individual report writers
# ---------------------------------------------------------------------------


def write_json_report(path: Path, report: FlowSuiteReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report.to_dict(), ensure_ascii=True, indent=2)
    path.write_text(payload + "\n", encoding="utf-8")


def write_junit_report(path: Path, report: FlowSuiteReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    suite = ET.Element(
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
        testcase = ET.SubElement(
            suite,
            "testcase",
            {
                "classname": report.suite_name,
                "name": case.flow_name,
                "time": f"{case.duration_ms / 1000:.3f}",
            },
        )
        properties = ET.SubElement(testcase, "properties")
        ET.SubElement(
            properties,
            "property",
            {"name": "flow_path", "value": case.flow_path},
        )
        ET.SubElement(
            properties,
            "property",
            {"name": "run_id", "value": case.run_id},
        )
        ET.SubElement(
            properties,
            "property",
            {"name": "status", "value": case.status},
        )
        if case.failure_reason is not None:
            ET.SubElement(
                properties,
                "property",
                {"name": "failure_reason", "value": case.failure_reason},
            )
        if case.failure_step is not None:
            ET.SubElement(
                properties,
                "property",
                {"name": "failure_step", "value": case.failure_step},
            )
        if case.expected_prefix is not None:
            ET.SubElement(
                properties,
                "property",
                {"name": "expected_prefix", "value": case.expected_prefix},
            )
        if case.actual_prefix is not None:
            ET.SubElement(
                properties,
                "property",
                {"name": "actual_prefix", "value": case.actual_prefix},
            )

        if not case.passed:
            failure = ET.SubElement(
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

    xml_bytes = ET.tostring(suite, encoding="utf-8", xml_declaration=True)
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

    template_path = _TEMPLATE_DIR / "report.html"
    template_text = template_path.read_text(encoding="utf-8")
    html = Template(template_text).safe_substitute(
        suite_name=escape(report.suite_name),
        generated_at=escape(report.generated_at),
        total=str(report.total),
        executed=str(report.executed),
        passed=str(report.passed),
        failed=str(report.failed),
        skipped=str(report.skipped),
        pass_rate=f"{report.pass_rate:.2f}",
        duration_ms=str(report.duration_ms),
        rows="\n".join(rows),
    )
    path.write_text(html, encoding="utf-8")
