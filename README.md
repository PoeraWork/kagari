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

Startup config behavior:

- Prefer `./uds.toml` (or custom path via `UDS_MCP_CONFIG_PATH`).
- Fallback to environment variables if the TOML file does not exist.

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

Used as fallback only when `uds.toml` is absent.

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
- `can_restart`
- `uds_send`
- `crypto_aes_cmac`
- `security27_build_key`
- `flow_load`
- `flow_register_inline`
- `flow_list`
- `flow_template_presets`
- `flow_init_template`
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
- `config_get`
- `config_update`
- `config_load`
- `config_export`

## Flow Hook Context

`before_hook` now receives a context object with:

- `request_hex`: request hex prepared for current step.
- `response_hex`: previous step response hex (`None` on first step).
- `variables`: flow variables snapshot.

Hook outputs can now include `variables` for write-back. Example snippet:

```python
seed = context["response_hex"][4:]
result = {
		"request_hex": "2712" + seed,
		"variables": {"seed": seed},
}
```

## SecurityAccess Helpers

- `crypto_aes_cmac(key_hex, data_hex, out_len=16)`: generic AES-CMAC helper.
- `security27_build_key(level, seed_hex, key_hex, out_len=None, include_level_in_cmac=False)`:
	build derived key and `27 xx + key` request payload for service `0x27` flows.

## AI Collaboration Notes

- Recommend using `uv` for environment management and command execution (`uv sync`, `uv run ...`).
- For SecurityAccess (`0x27`) in flow mode, prefer MCP crypto tools over in-hook crypto imports.
- Use `response_hex` + variable write-back in `before_hook` to chain seed-read and key-send steps.

## Runtime Config Switching

You can modify config during a session and switch profiles without restarting MCP:

- Use `config_get` to inspect current runtime config.
- Use `config_update` to patch selected fields (channel, bitrate, IDs, paths, etc.).
- Use `config_load(path)` to switch to another TOML profile.
- Use `config_export(path)` to persist current runtime config.

Note: reconfiguration is blocked while any flow run is `RUNNING` or `PAUSED`.

## Notes

- `UdsClientService` now uses full `py-uds` client stack (`Client`,
	`PyCanTransportInterface`, `CanAddressingInformation`) on top of `python-can` bus.
