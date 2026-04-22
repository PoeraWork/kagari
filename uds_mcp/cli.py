from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

if TYPE_CHECKING:
    from uds_mcp.flow.suite import ResolvedCase

from uds_mcp.can.config import CanConfig
from uds_mcp.can.interface import CanInterface
from uds_mcp.config import AppConfig, load_config
from uds_mcp.extensions.runtime import ExtensionRuntime
from uds_mcp.flow.engine import FlowEngine, FlowStatus
from uds_mcp.flow.report import (
    FlowCaseReport,
    FlowSuiteReport,
    build_suite_report,
    derive_case_diagnostics,
    write_reports,
)
from uds_mcp.init import project_init
from uds_mcp.logging.exporters.blf import BlfExporter
from uds_mcp.logging.store import EventStore
from uds_mcp.uds.client import UdsClientService, UdsConfig


def main() -> None:
    cli(standalone_mode=True)


_CONFIG_HELP = "Path to TOML config. Defaults to UDS_MCP_CONFIG_PATH or ./uds.toml."


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli() -> None:
    """Direct CLI for uds-mcp runtime operations."""


@cli.command("init")
@click.option(
    "--dir",
    "target_dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=".",
    show_default=True,
    help="Target directory for generated files.",
)
@click.option("-f", "--force", is_flag=True, default=False, help="Overwrite existing files.")
def init(target_dir: Path, *, force: bool) -> None:
    """Initialize project: generate uds.toml and flow-schema.json."""
    result = project_init(target_dir.resolve(), overwrite=force)
    _print_json(result)


@cli.command("config-show")
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help=_CONFIG_HELP,
)
def config_show(config_path: Path | None) -> None:
    config, source = _load_and_prepare_config(config_path)
    _print_json({"source": source, "config": config.to_dict()})


@cli.command("can-send")
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help=_CONFIG_HELP,
)
@click.argument("arbitration_id")
@click.argument("data_hex")
@click.option("--extended", is_flag=True, default=False, help="Use extended CAN ID.")
def can_send(
    config_path: Path | None, arbitration_id: str, data_hex: str, *, extended: bool
) -> None:
    config, _ = _load_and_prepare_config(config_path)
    app = _CliRuntime(config)
    try:
        app.can.send_frame(
            arbitration_id=_parse_int(arbitration_id),
            data=bytes.fromhex(data_hex),
            is_extended_id=extended,
        )
        _print_json({"ok": True})
    finally:
        app.close()


@cli.command("uds-send")
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help=_CONFIG_HELP,
)
@click.argument("request_hex")
@click.option(
    "--timeout-ms", type=int, default=1000, show_default=True, help="Timeout in milliseconds."
)
@click.option(
    "--addressing-mode",
    type=click.Choice(["physical", "functional"], case_sensitive=True),
    default="physical",
    show_default=True,
    help="UDS addressing mode.",
)
def uds_send(
    config_path: Path | None,
    request_hex: str,
    *,
    timeout_ms: int,
    addressing_mode: str,
) -> None:
    config, _ = _load_and_prepare_config(config_path)
    app = _CliRuntime(config)
    try:
        result = asyncio.run(
            app.uds.send(
                request_hex,
                timeout_ms=timeout_ms,
                addressing_mode=addressing_mode,
            )
        )
        _print_json(result)
    finally:
        app.close()


@cli.command("flow-run")
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help=_CONFIG_HELP,
)
@click.argument("path", type=click.Path(path_type=Path, dir_okay=False, exists=True))
@click.option("--no-wait", is_flag=True, default=False, help="Return immediately after start.")
@click.option(
    "--timeout-s", type=float, default=0.0, show_default=True, help="Optional timeout seconds."
)
@click.option(
    "--blf-output",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Path to BLF file for streaming CAN events during flow execution.",
)
@click.option(
    "--verbose",
    is_flag=True,
    default=False,
    help="Output full trace instead of simplified status summary.",
)
def flow_run(
    config_path: Path | None,
    path: Path,
    *,
    no_wait: bool,
    timeout_s: float,
    blf_output: Path | None,
    verbose: bool,
) -> None:
    config, _ = _load_and_prepare_config(config_path)
    app = _CliRuntime(config)
    try:
        result = asyncio.run(
            _run_flow_once(
                app.flow_engine,
                path,
                wait=not no_wait,
                timeout_s=timeout_s,
                blf_output=blf_output,
                verbose=verbose,
                event_store=app.event_store,
            )
        )
        _print_json(result)
    finally:
        app.close()


@cli.command("flow-suite")
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help=_CONFIG_HELP,
)
@click.option(
    "--suite",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    default=None,
    help="Path to suite YAML with fields: name/include/exclude/timeout_s/stop_on_fail.",
)
@click.option(
    "--path",
    "paths",
    multiple=True,
    type=str,
    help="Flow file path or directory (repeatable).",
)
@click.option(
    "--glob",
    "globs",
    multiple=True,
    type=str,
    help="Glob pattern to discover flow files (repeatable).",
)
@click.argument("trailing_specs", nargs=-1)
@click.option("--suite-name", type=str, default=None, help="Override suite name in report.")
@click.option(
    "--timeout-s", type=float, default=0.0, show_default=True, help="Per-flow timeout seconds."
)
@click.option(
    "--stop-on-fail",
    is_flag=True,
    default=False,
    help="Stop suite execution immediately when one flow fails.",
)
@click.option(
    "--report-json",
    type=click.Path(path_type=Path, dir_okay=False),
    default=Path("./flow-report.json"),
    show_default=True,
    help="Output JSON report path.",
)
@click.option(
    "--report-html",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Optional output HTML report path.",
)
@click.option(
    "--report-junit",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Optional output JUnit XML report path.",
)
@click.option(
    "--verbose",
    is_flag=True,
    default=False,
    help="Store detailed trace in case records.",
)
@click.option(
    "--tag",
    "tags",
    multiple=True,
    type=str,
    help="Only run cases matching these tags (repeatable). Requires suite with cases.",
)
def flow_suite(
    config_path: Path | None,
    suite: Path | None,
    paths: tuple[str, ...],
    globs: tuple[str, ...],
    trailing_specs: tuple[str, ...],
    suite_name: str | None,
    timeout_s: float,
    *,
    stop_on_fail: bool,
    report_json: Path,
    report_html: Path | None,
    report_junit: Path | None,
    verbose: bool,
    tags: tuple[str, ...],
) -> None:
    config, _ = _load_and_prepare_config(config_path)
    merged_paths = list(paths)
    merged_paths.extend(trailing_specs)
    suite_payload = _resolve_flow_suite(
        suite=suite,
        paths=merged_paths,
        globs=list(globs),
        suite_name=suite_name,
        timeout_s=timeout_s,
        stop_on_fail=stop_on_fail,
        flow_repo=config.flow_repo,
        tag_filter=list(tags) if tags else None,
    )

    app = _CliRuntime(config)
    try:
        result = asyncio.run(
            _run_flow_suite(
                app.flow_engine,
                suite_payload["resolved_cases"],
                suite_name=suite_payload["suite_name"],
                timeout_s=suite_payload["timeout_s"],
                stop_on_fail=suite_payload["stop_on_fail"],
                verbose=verbose,
                setup_path=suite_payload.get("setup"),
                teardown_path=suite_payload.get("teardown"),
            )
        )

        outputs = write_reports(
            result,
            json_path=report_json,
            html_path=report_html,
            junit_path=report_junit,
        )

        _print_json(
            {
                "suite": result.to_dict(),
                "reports": outputs,
            }
        )
    finally:
        app.close()


def _load_and_prepare_config(config_path: Path | None) -> tuple[AppConfig, str]:
    config, source = load_config(default_path=config_path)
    config.flow_repo.mkdir(parents=True, exist_ok=True)
    config.extension_whitelist.mkdir(parents=True, exist_ok=True)
    return config, source


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
    variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    blf_exporter: BlfExporter | None = None
    if blf_output is not None and event_store is not None:
        blf_exporter = BlfExporter()
        blf_exporter.start_streaming(blf_output)
        event_store.add_listener(blf_exporter.on_event)

    try:
        flow = flow_engine.load(path)
        run_id = await flow_engine.start(flow.name, variables=variables)
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
    resolved_cases: list[ResolvedCase],
    *,
    suite_name: str,
    timeout_s: float,
    stop_on_fail: bool,
    verbose: bool,
    setup_path: Path | None = None,
    teardown_path: Path | None = None,
) -> FlowSuiteReport:
    started_at = datetime.now(UTC)
    cases: list[FlowCaseReport] = []

    if setup_path is not None:
        await _run_flow_once(flow_engine, setup_path, wait=True, timeout_s=timeout_s)

    try:
        for rc in resolved_cases:
            case_timeout = rc.timeout_s if rc.timeout_s is not None else timeout_s
            max_attempts = rc.retry + 1
            status: dict[str, Any] = {}
            case_started = datetime.now(UTC)

            for attempt in range(max_attempts):
                status = await _run_flow_once(
                    flow_engine,
                    rc.flow_path,
                    wait=True,
                    timeout_s=case_timeout,
                    verbose=verbose,
                    variables=rc.variables or None,
                )
                case_status = str(status["status"])
                if case_status == FlowStatus.DONE.value or attempt == max_attempts - 1:
                    break

            case_ended = datetime.now(UTC)
            case_status = str(status["status"])
            passed = case_status == FlowStatus.DONE.value
            diagnostics = derive_case_diagnostics(status)
            cases.append(
                FlowCaseReport(
                    flow_name=str(status.get("flow_name") or rc.flow_path.stem),
                    flow_path=rc.flow_path.as_posix(),
                    run_id=str(status.get("run_id") or ""),
                    status=case_status,
                    passed=passed,
                    duration_ms=max(int((case_ended - case_started).total_seconds() * 1000), 0),
                    step_count=int(status.get("step_count") or 0),
                    message_count=int(status.get("message_count") or 0),
                    error=(str(status["error"]) if status.get("error") is not None else None),
                    current_step=(
                        str(status["current_step"])
                        if status.get("current_step") is not None
                        else None
                    ),
                    trace=(
                        status.get("trace") if isinstance(status.get("trace"), list) else None
                    ),
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
            if stop_on_fail and not passed:
                break
    finally:
        if teardown_path is not None:
            await _run_flow_once(flow_engine, teardown_path, wait=True, timeout_s=timeout_s)

    ended_at = datetime.now(UTC)
    return build_suite_report(
        suite_name=suite_name,
        total=len(resolved_cases),
        cases=cases,
        started_at=started_at,
        ended_at=ended_at,
    )


def _resolve_flow_suite(
    *,
    suite: Path | None,
    paths: list[str],
    globs: list[str],
    suite_name: str | None,
    timeout_s: float,
    stop_on_fail: bool,
    flow_repo: Path,
    tag_filter: list[str] | None = None,
) -> dict[str, Any]:
    from uds_mcp.flow.suite import (  # noqa: PLC0415
        SuiteDefinition,
        load_suite,
        resolve_suite,
    )

    suite_def: SuiteDefinition | None = None
    base_dir = Path.cwd()
    resolved_suite_name = str(suite_name or "flow-suite")
    resolved_timeout_s = float(timeout_s)
    resolved_stop_on_fail = bool(stop_on_fail)
    setup_path: Path | None = None
    teardown_path: Path | None = None

    if suite is not None:
        suite_def = load_suite(suite)
        base_dir = suite.resolve().parent
        if suite_name is None:
            resolved_suite_name = suite_def.name
        if timeout_s == 0.0 and suite_def.timeout_s is not None:
            resolved_timeout_s = suite_def.timeout_s
        if not stop_on_fail:
            resolved_stop_on_fail = suite_def.stop_on_fail

        if suite_def.setup is not None:
            sp = Path(suite_def.setup)
            setup_path = sp if sp.is_absolute() else (base_dir / sp).resolve()
        if suite_def.teardown is not None:
            tp = Path(suite_def.teardown)
            teardown_path = tp if tp.is_absolute() else (base_dir / tp).resolve()

    # Merge CLI include specs into suite definition
    include_specs: list[str] = list(paths)
    include_specs.extend(globs)

    if suite_def is not None:
        suite_def = suite_def.model_copy(
            update={"include": list(suite_def.include) + include_specs}
        )
    else:
        if not include_specs:
            include_specs.append(str(flow_repo / "*.yaml"))
        suite_def = SuiteDefinition(
            name=resolved_suite_name,
            include=include_specs,
        )

    resolved_cases = resolve_suite(suite_def, base_dir=base_dir, tag_filter=tag_filter)
    if not resolved_cases:
        raise click.ClickException("no flow files discovered for suite execution")

    return {
        "suite_name": resolved_suite_name,
        "resolved_cases": resolved_cases,
        "timeout_s": resolved_timeout_s,
        "stop_on_fail": resolved_stop_on_fail,
        "setup": setup_path,
        "teardown": teardown_path,
    }


def _parse_int(value: str) -> int:
    return int(value, 0)


def _print_json(data: dict[str, Any]) -> None:
    print(json.dumps(data, ensure_ascii=True, indent=2, default=_json_default))  # noqa: T201


def _json_default(value: object) -> object:
    if isinstance(value, Path):
        return value.as_posix()
    return str(value)
