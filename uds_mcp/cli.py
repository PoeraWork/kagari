from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from uds_mcp.can.config import CanConfig
from uds_mcp.can.interface import CanInterface
from uds_mcp.config import AppConfig, load_config
from uds_mcp.extensions.runtime import ExtensionRuntime
from uds_mcp.flow.engine import FlowEngine, FlowStatus
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
                )
            )
            _print_json(result)
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
) -> dict[str, Any]:
    flow = flow_engine.load(path)
    run_id = await flow_engine.start(flow.name)
    if not wait:
        return {"ok": True, "run_id": run_id, "status": "STARTED"}

    timeout = timeout_s if timeout_s is not None else 0.0
    started = asyncio.get_running_loop().time()
    while True:
        status = flow_engine.status(run_id)
        current = str(status["status"])
        if current in {FlowStatus.DONE.value, FlowStatus.FAILED.value, FlowStatus.STOPPED.value}:
            return status

        if timeout > 0 and (asyncio.get_running_loop().time() - started) > timeout:
            flow_engine.stop(run_id)
            stopped = flow_engine.status(run_id)
            return {
                "run_id": run_id,
                "flow_name": stopped["flow_name"],
                "status": "TIMEOUT",
                "current_step": stopped["current_step"],
                "error": f"flow timeout after {timeout:.3f}s",
                "trace": stopped["trace"],
            }
        await asyncio.sleep(0.05)


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

    return parser


def _parse_int(value: str) -> int:
    return int(value, 0)


def _print_json(data: dict[str, Any]) -> None:
    print(json.dumps(data, ensure_ascii=True, indent=2, default=_json_default))  # noqa: T201


def _json_default(value: object) -> object:
    if isinstance(value, Path):
        return value.as_posix()
    return str(value)
