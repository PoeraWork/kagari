from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from uds_mcp.can.config import CanConfig
from uds_mcp.can.interface import CanInterface
from uds_mcp.config import AppConfig
from uds_mcp.extensions.runtime import ExtensionRuntime
from uds_mcp.flow.engine import FlowEngine
from uds_mcp.flow.schema import FlowDefinition
from uds_mcp.logging.exporters.blf import BlfExporter
from uds_mcp.logging.store import EventStore
from uds_mcp.models.events import EventKind
from uds_mcp.uds.client import UdsClientService, UdsConfig


class AppState:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
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
                tester_present_interval_sec=config.tester_present_interval_sec,
            ),
            self.event_store,
        )
        self.extension_runtime = ExtensionRuntime([config.extension_whitelist.resolve()])
        self.flow_engine = FlowEngine(self.uds, self.event_store, self.extension_runtime)
        self.blf_exporter = BlfExporter()


def build_server(config: AppConfig) -> FastMCP:
    state = AppState(config)
    mcp = FastMCP(
        "uds-mcp",
        instructions=(
            "UDS MCP server backed by py-uds and python-can. "
            "Supports CAN/UDS send, flow orchestration, breakpoints, injection, and BLF export."
        ),
        json_response=True,
    )

    @mcp.tool()
    def can_send(
        arbitration_id: int,
        data_hex: str,
        is_extended_id: bool = False,
    ) -> dict[str, Any]:
        state.can.send_frame(arbitration_id, bytes.fromhex(data_hex), is_extended_id=is_extended_id)
        return {
            "ok": True,
            "arbitration_id": arbitration_id,
            "data_hex": data_hex.upper(),
            "is_extended_id": is_extended_id,
        }

    @mcp.tool()
    def can_tail(limit: int = 100) -> list[dict[str, object]]:
        return [
            item.to_dict()
            for item in state.event_store.query(
                kinds=[EventKind.CAN_TX, EventKind.CAN_RX],
                limit=limit,
            )
        ]

    @mcp.tool()
    async def uds_send(request_hex: str, timeout_ms: int = 1000) -> dict[str, object]:
        return await state.uds.send(request_hex, timeout_ms=timeout_ms)

    @mcp.tool()
    def flow_load(path: str) -> dict[str, object]:
        flow = state.flow_engine.load(Path(path))
        return {"ok": True, "flow": flow.name, "steps": len(flow.steps)}

    @mcp.tool()
    def flow_register_inline(
        name: str,
        steps: list[dict[str, object]],
        variables: dict[str, object] | None = None,
    ) -> dict[str, object]:
        flow = FlowDefinition(name=name, variables=variables or {}, steps=steps)
        state.flow_engine.register(flow)
        return {"ok": True, "flow": flow.name, "steps": len(flow.steps)}

    @mcp.tool()
    def flow_list() -> list[str]:
        return state.flow_engine.list_flows()

    @mcp.tool()
    async def flow_start(flow_name: str) -> dict[str, object]:
        run_id = await state.flow_engine.start(flow_name)
        return {"ok": True, "run_id": run_id}

    @mcp.tool()
    def flow_status(run_id: str) -> dict[str, object]:
        return state.flow_engine.status(run_id)

    @mcp.tool()
    def flow_stop(run_id: str) -> dict[str, object]:
        state.flow_engine.stop(run_id)
        return {"ok": True}

    @mcp.tool()
    def flow_resume(run_id: str) -> dict[str, object]:
        state.flow_engine.resume(run_id)
        return {"ok": True}

    @mcp.tool()
    def flow_breakpoint(flow_name: str, step_name: str, enabled: bool = True) -> dict[str, object]:
        state.flow_engine.set_breakpoint(flow_name, step_name, enabled)
        return {"ok": True}

    @mcp.tool()
    def flow_patch_step(
        flow_name: str,
        step_name: str,
        send_hex: str | None = None,
        expect_prefix: str | None = None,
    ) -> dict[str, object]:
        state.flow_engine.patch_step(
            flow_name,
            step_name,
            send_hex=send_hex,
            expect_prefix=expect_prefix,
        )
        return {"ok": True}

    @mcp.tool()
    async def flow_inject_uds(request_hex: str, timeout_ms: int = 1000) -> dict[str, object]:
        return await state.flow_engine.inject_once(request_hex, timeout_ms)

    @mcp.tool()
    def flow_save(flow_name: str, path: str) -> dict[str, object]:
        state.flow_engine.save(flow_name, Path(path))
        return {"ok": True}

    @mcp.tool()
    def log_export_blf(output_path: str, start_iso: str, end_iso: str) -> dict[str, object]:
        start = _parse_dt(start_iso)
        end = _parse_dt(end_iso)
        events = state.event_store.query(
            start=start,
            end=end,
            kinds=[EventKind.CAN_TX, EventKind.CAN_RX],
        )
        exported = state.blf_exporter.export(Path(output_path), events)
        return {"ok": True, "output_path": output_path, "frame_count": exported}

    @mcp.tool()
    def log_query(
        start_iso: str | None = None,
        end_iso: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, object]]:
        start = _parse_dt(start_iso) if start_iso else None
        end = _parse_dt(end_iso) if end_iso else None
        return [
            item.to_dict() for item in state.event_store.query(start=start, end=end, limit=limit)
        ]

    return mcp


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError("datetime must include timezone, e.g. 2026-03-13T10:00:00+08:00")
    return dt
