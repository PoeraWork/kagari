# uds-mcp

UDS MCP server skeleton based on official Python MCP SDK `mcp` (`FastMCP`),
with `py-uds + python-can` as communication foundation.

## Current MVP Scope

- Send CAN frame and inspect CAN TX/RX logs.
- Send UDS request and inspect UDS response.
- Load/save shared YAML flow definitions.
- Start/stop/resume flow, inspect async run status.
- Set breakpoints, inject one-off UDS requests, patch flow step send/expect.
- Keep diagnostic session alive on breakpoint pause using `0x3E` TesterPresent.
- Export logs in BLF format by time window.

## Install

```bash
uv sync
```

or

```bash
pip install -e .
```

## Run

```bash
uv run uds-mcp
```

The server uses stdio transport by default (`FastMCP.run()`).

## Quality Checks

Lint:

```bash
uv run ruff check .
```

Format:

```bash
uv run ruff format .
```

Test:

```bash
uv run pytest
```

## Environment Variables

- `UDS_MCP_CAN_INTERFACE` default: `socketcan`
- `UDS_MCP_CAN_CHANNEL` default: `vcan0`
- `UDS_MCP_CAN_BITRATE` default: `500000`
- `UDS_MCP_UDS_TX_ID` default: `0x7E0`
- `UDS_MCP_UDS_RX_ID` default: `0x7E8`
- `UDS_MCP_FLOW_REPO` default: `./flows`
- `UDS_MCP_EXTENSION_WHITELIST` default: `./extensions`
- `UDS_MCP_TESTER_PRESENT_INTERVAL` default: `2.0`

## Implemented MCP Tools

- `can_send`
- `can_tail`
- `uds_send`
- `flow_load`
- `flow_register_inline`
- `flow_list`
- `flow_start`
- `flow_status`
- `flow_stop`
- `flow_resume`
- `flow_breakpoint`
- `flow_patch_step`
- `flow_inject_uds`
- `flow_save`
- `log_export_blf`
- `log_query`

## Notes

- `UdsClientService` now uses full `py-uds` client stack (`Client`,
	`PyCanTransportInterface`, `CanAddressingInformation`) on top of `python-can` bus.
