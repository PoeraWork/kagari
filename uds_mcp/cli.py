from __future__ import annotations

import argparse
import asyncio
import glob
import json
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import yaml

from uds_mcp.can.config import CanConfig
from uds_mcp.can.interface import CanInterface
from uds_mcp.config import AppConfig, load_config
from uds_mcp.extensions.runtime import ExtensionRuntime
from uds_mcp.flow.engine import FlowEngine, FlowStatus
from uds_mcp.flow.report import (
    FlowCaseReport,
    FlowSuiteReport,
    build_suite_report,
    write_html_report,
    write_json_report,
    write_junit_report,
)
from uds_mcp.logging.exporters.blf import BlfExporter
from uds_mcp.logging.store import EventStore
from uds_mcp.uds.client import UdsClientService, UdsConfig


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    config_value = getattr(args, "config", None)
    config_path = Path(config_value) if config_value else None
    config, source = load_config(default_path=config_path)
    config.flow_repo.mkdir(parents=True, exist_ok=True)
    config.extension_whitelist.mkdir(parents=True, exist_ok=True)

    if args.command == "config-show":
        _print_json({"source": source, "config": config.to_dict()})
        return

    app = _CliRuntime(config)
    try:
        if args.command == "can-send":
            app.can.send_frame(
                arbitration_id=_parse_int(args.arbitration_id),
                data=bytes.fromhex(args.data_hex),
                is_extended_id=args.extended,
            )
            _print_json({"ok": True})
            return

        if args.command == "uds-send":
            result = asyncio.run(
                app.uds.send(
                    args.request_hex,
                    timeout_ms=args.timeout_ms,
                    addressing_mode=args.addressing_mode,
                )
            )
            _print_json(result)
            return

        if args.command == "flow-run":
            result = asyncio.run(
                _run_flow_once(
                    app.flow_engine,
                    Path(args.path),
                    wait=not args.no_wait,
                    timeout_s=args.timeout_s,
                    blf_output=Path(args.blf_output) if args.blf_output else None,
                    verbose=args.verbose,
                    event_store=app.event_store,
                )
            )
            _print_json(result)
            return

        if args.command == "flow-suite":
            suite = _resolve_flow_suite(args, config.flow_repo)
            result = asyncio.run(
                _run_flow_suite(
                    app.flow_engine,
                    suite["flow_paths"],
                    suite_name=suite["suite_name"],
                    timeout_s=suite["timeout_s"],
                    stop_on_fail=suite["stop_on_fail"],
                    verbose=args.verbose,
                )
            )

            json_path = Path(args.report_json)
            write_json_report(json_path, result)
            outputs: dict[str, str] = {"json": json_path.as_posix()}

            if args.report_html:
                html_path = Path(args.report_html)
                write_html_report(html_path, result)
                outputs["html"] = html_path.as_posix()

            if args.report_junit:
                junit_path = Path(args.report_junit)
                write_junit_report(junit_path, result)
                outputs["junit"] = junit_path.as_posix()

            _print_json(
                {
                    "suite": result.to_dict(),
                    "reports": outputs,
                }
            )
            return

        raise ValueError(f"unsupported command: {args.command}")
    finally:
        app.close()


class _CliRuntime:
    def __init__(self, config: AppConfig) -> None:
        self.event_store = EventStore()
        self.can = CanInterface(
            CanConfig(
                interface=config.can_interface,
                channel=config.can_channel,
                bitrate=config.can_bitrate,
                fd=config.can_fd,
                data_bitrate=config.can_data_bitrate,
            ),
            self.event_store,
        )
        self.uds = UdsClientService(
            self.can,
            UdsConfig(
                tx_id=config.uds_tx_id,
                rx_id=config.uds_rx_id,
                tx_functional_id=config.uds_tx_id_functional,
                rx_functional_id=config.uds_rx_id_functional,
                can_fd=config.can_fd,
                use_data_optimization=config.uds_use_data_optimization,
                dlc=config.uds_dlc,
                min_dlc=config.uds_min_dlc,
                tester_present_interval_sec=config.tester_present_interval_sec,
            ),
            self.event_store,
        )
        self.runtime = ExtensionRuntime([config.extension_whitelist.resolve()])
        self.flow_engine = FlowEngine(self.uds, self.event_store, self.runtime)

    def close(self) -> None:
        self.uds.close()
        self.can.close()


async def _run_flow_once(
    flow_engine: FlowEngine,
    path: Path,
    *,
    wait: bool,
    timeout_s: float | None,
    blf_output: Path | None = None,
    verbose: bool = False,
    event_store: EventStore | None = None,
) -> dict[str, Any]:
    blf_exporter: BlfExporter | None = None
    if blf_output is not None and event_store is not None:
        blf_exporter = BlfExporter()
        blf_exporter.start_streaming(blf_output)
        event_store.add_listener(blf_exporter.on_event)

    try:
        flow = flow_engine.load(path)
        run_id = await flow_engine.start(flow.name)
        if not wait:
            return {"ok": True, "run_id": run_id, "status": "STARTED"}

        timeout = timeout_s if timeout_s is not None else 0.0
        started = asyncio.get_running_loop().time()
        while True:
            status = flow_engine.status(run_id)
            current = str(status["status"])
            if current in {
                FlowStatus.DONE.value,
                FlowStatus.FAILED.value,
                FlowStatus.STOPPED.value,
            }:
                if verbose:
                    status["trace"] = flow_engine.get_trace(run_id)
                return status

            if timeout > 0 and (asyncio.get_running_loop().time() - started) > timeout:
                flow_engine.stop(run_id)
                stopped = flow_engine.status(run_id)
                result: dict[str, Any] = {
                    "run_id": run_id,
                    "flow_name": stopped["flow_name"],
                    "status": "TIMEOUT",
                    "current_step": stopped["current_step"],
                    "error": f"flow timeout after {timeout:.3f}s",
                    "step_count": stopped.get("step_count", 0),
                    "message_count": stopped.get("message_count", 0),
                }
                if verbose:
                    result["trace"] = flow_engine.get_trace(run_id)
                return result
            await asyncio.sleep(0.05)
    finally:
        if blf_exporter is not None and event_store is not None:
            event_store.remove_listener(blf_exporter.on_event)
            blf_exporter.stop_streaming()


async def _run_flow_suite(
    flow_engine: FlowEngine,
    paths: list[Path],
    *,
    suite_name: str,
    timeout_s: float,
    stop_on_fail: bool,
    verbose: bool,
) -> FlowSuiteReport:
    started_at = datetime.now(UTC)
    cases: list[FlowCaseReport] = []

    for path in paths:
        case_started = datetime.now(UTC)
        status = await _run_flow_once(
            flow_engine,
            path,
            wait=True,
            timeout_s=timeout_s,
            verbose=verbose,
        )
        case_ended = datetime.now(UTC)
        case_status = str(status["status"])
        passed = case_status == FlowStatus.DONE.value
        cases.append(
            FlowCaseReport(
                flow_name=str(status.get("flow_name") or path.stem),
                flow_path=path.as_posix(),
                run_id=str(status.get("run_id") or ""),
                status=case_status,
                passed=passed,
                duration_ms=max(int((case_ended - case_started).total_seconds() * 1000), 0),
                step_count=int(status.get("step_count") or 0),
                message_count=int(status.get("message_count") or 0),
                error=(str(status["error"]) if status.get("error") is not None else None),
                current_step=(
                    str(status["current_step"]) if status.get("current_step") is not None else None
                ),
                trace=(status.get("trace") if isinstance(status.get("trace"), list) else None),
            )
        )
        if stop_on_fail and not passed:
            break

    ended_at = datetime.now(UTC)
    return build_suite_report(
        suite_name=suite_name,
        total=len(paths),
        cases=cases,
        started_at=started_at,
        ended_at=ended_at,
    )


def _resolve_flow_suite(args: argparse.Namespace, flow_repo: Path) -> dict[str, Any]:
    include_specs: list[str] = list(getattr(args, "paths", []) or [])
    include_specs.extend(list(getattr(args, "glob", []) or []))
    exclude_patterns: list[str] = []
    suite_name = str(args.suite_name or "flow-suite")
    timeout_s = float(args.timeout_s)
    stop_on_fail = bool(args.stop_on_fail)
    base_dir = Path.cwd()

    if args.suite:
        suite_cfg = _load_suite_file(Path(args.suite))
        include_specs.extend(suite_cfg["include_specs"])
        exclude_patterns.extend(suite_cfg["exclude_patterns"])
        if args.suite_name is None:
            suite_name = suite_cfg["suite_name"]
        if args.timeout_s == 0.0 and suite_cfg["timeout_s"] is not None:
            timeout_s = suite_cfg["timeout_s"]
        if not args.stop_on_fail:
            stop_on_fail = suite_cfg["stop_on_fail"]
        base_dir = suite_cfg["base_dir"]

    if not include_specs:
        include_specs.append(str(flow_repo / "*.yaml"))

    flow_paths = _discover_flow_paths(
        include_specs,
        exclude_patterns=exclude_patterns,
        base_dir=base_dir,
    )
    if not flow_paths:
        raise ValueError("no flow files discovered for suite execution")

    return {
        "suite_name": suite_name,
        "flow_paths": flow_paths,
        "timeout_s": timeout_s,
        "stop_on_fail": stop_on_fail,
    }


def _load_suite_file(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError("suite file must be a mapping")

    include_raw = data.get("include", [])
    if not isinstance(include_raw, list) or not all(isinstance(v, str) for v in include_raw):
        raise TypeError("suite include must be an array of strings")

    exclude_raw = data.get("exclude", [])
    if not isinstance(exclude_raw, list) or not all(isinstance(v, str) for v in exclude_raw):
        raise TypeError("suite exclude must be an array of strings")

    name_raw = data.get("name", path.stem)
    if not isinstance(name_raw, str):
        raise TypeError("suite name must be a string")

    timeout_raw = data.get("timeout_s")
    timeout_s: float | None = None
    if timeout_raw is not None:
        if not isinstance(timeout_raw, int | float):
            raise TypeError("suite timeout_s must be a number")
        timeout_s = float(timeout_raw)

    stop_on_fail_raw = data.get("stop_on_fail", False)
    if not isinstance(stop_on_fail_raw, bool):
        raise TypeError("suite stop_on_fail must be boolean")

    return {
        "suite_name": name_raw,
        "include_specs": include_raw,
        "exclude_patterns": exclude_raw,
        "timeout_s": timeout_s,
        "stop_on_fail": stop_on_fail_raw,
        "base_dir": path.resolve().parent,
    }


def _discover_flow_paths(
    include_specs: list[str],
    *,
    exclude_patterns: list[str],
    base_dir: Path,
) -> list[Path]:
    discovered: set[Path] = set()
    for spec in include_specs:
        candidate = Path(spec)
        if not candidate.is_absolute():
            candidate = (base_dir / candidate).resolve()

        if _has_glob_meta(spec):
            pattern = str(candidate) if candidate.is_absolute() else spec
            for matched in glob.glob(pattern, recursive=True):
                found = Path(matched)
                resolved = found.resolve()
                if resolved.is_file() and resolved.suffix.lower() in {".yaml", ".yml"}:
                    discovered.add(resolved)
            continue

        if candidate.is_dir():
            for found in candidate.rglob("*.yaml"):
                discovered.add(found.resolve())
            for found in candidate.rglob("*.yml"):
                discovered.add(found.resolve())
            continue

        if candidate.is_file() and candidate.suffix.lower() in {".yaml", ".yml"}:
            discovered.add(candidate)

    if not exclude_patterns:
        return sorted(discovered)

    return sorted(
        item
        for item in discovered
        if not any(fnmatch(item.as_posix(), pattern) for pattern in exclude_patterns)
    )


def _has_glob_meta(value: str) -> bool:
    return any(token in value for token in ("*", "?", "[", "]"))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Direct CLI for uds-mcp runtime operations.")
    parser.add_argument(
        "-c",
        "--config",
        help="Path to TOML config. Defaults to UDS_MCP_CONFIG_PATH or ./uds.toml.",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    config_show = sub.add_parser("config-show", help="Show effective config and source.")
    config_show.add_argument(
        "-c",
        "--config",
        help="Path to TOML config. Defaults to UDS_MCP_CONFIG_PATH or ./uds.toml.",
    )

    can_send = sub.add_parser("can-send", help="Send one CAN frame.")
    can_send.add_argument(
        "-c",
        "--config",
        help="Path to TOML config. Defaults to UDS_MCP_CONFIG_PATH or ./uds.toml.",
    )
    can_send.add_argument("arbitration_id", help="Arbitration ID (e.g. 0x7E0).")
    can_send.add_argument("data_hex", help="Payload hex string.")
    can_send.add_argument("--extended", action="store_true", help="Use extended CAN ID.")

    uds_send = sub.add_parser("uds-send", help="Send one UDS request.")
    uds_send.add_argument(
        "-c",
        "--config",
        help="Path to TOML config. Defaults to UDS_MCP_CONFIG_PATH or ./uds.toml.",
    )
    uds_send.add_argument("request_hex", help="UDS request hex string.")
    uds_send.add_argument("--timeout-ms", type=int, default=1000, help="Timeout in milliseconds.")
    uds_send.add_argument(
        "--addressing-mode",
        choices=["physical", "functional"],
        default="physical",
        help="UDS addressing mode.",
    )

    flow_run = sub.add_parser("flow-run", help="Load and execute a flow YAML once.")
    flow_run.add_argument(
        "-c",
        "--config",
        help="Path to TOML config. Defaults to UDS_MCP_CONFIG_PATH or ./uds.toml.",
    )
    flow_run.add_argument("path", help="Path to flow YAML file.")
    flow_run.add_argument("--no-wait", action="store_true", help="Return immediately after start.")
    flow_run.add_argument("--timeout-s", type=float, default=0.0, help="Optional timeout seconds.")
    flow_run.add_argument(
        "--blf-output", help="Path to BLF file for streaming CAN events during flow execution."
    )
    flow_run.add_argument(
        "--verbose",
        action="store_true",
        help="Output full trace instead of simplified status summary.",
    )

    flow_suite = sub.add_parser("flow-suite", help="Run multiple flow YAML files and export report.")
    flow_suite.add_argument(
        "-c",
        "--config",
        help="Path to TOML config. Defaults to UDS_MCP_CONFIG_PATH or ./uds.toml.",
    )
    flow_suite.add_argument(
        "--suite",
        help="Path to suite YAML with fields: name/include/exclude/timeout_s/stop_on_fail.",
    )
    flow_suite.add_argument(
        "--path",
        dest="paths",
        action="append",
        default=[],
        help="Flow file path or directory (repeatable).",
    )
    flow_suite.add_argument(
        "--glob",
        action="append",
        default=[],
        help="Glob pattern to discover flow files (repeatable).",
    )
    flow_suite.add_argument("--suite-name", help="Override suite name in report.")
    flow_suite.add_argument("--timeout-s", type=float, default=0.0, help="Per-flow timeout seconds.")
    flow_suite.add_argument(
        "--stop-on-fail",
        action="store_true",
        help="Stop suite execution immediately when one flow fails.",
    )
    flow_suite.add_argument(
        "--report-json",
        default="./flow-report.json",
        help="Output JSON report path.",
    )
    flow_suite.add_argument("--report-html", help="Optional output HTML report path.")
    flow_suite.add_argument("--report-junit", help="Optional output JUnit XML report path.")
    flow_suite.add_argument(
        "--verbose",
        action="store_true",
        help="Store detailed trace in case records.",
    )

    return parser


def _parse_int(value: str) -> int:
    return int(value, 0)


def _print_json(data: dict[str, Any]) -> None:
    print(json.dumps(data, ensure_ascii=True, indent=2, default=_json_default))  # noqa: T201


def _json_default(value: object) -> object:
    if isinstance(value, Path):
        return value.as_posix()
    return str(value)
