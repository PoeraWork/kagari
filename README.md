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

Direct CLI (without MCP client):

```bash
uv run uds-mcp-cli config-show
uv run uds-mcp-cli uds-send 1003 --timeout-ms 1200 --addressing-mode physical
uv run uds-mcp-cli flow-run ./examples/flows/demo_virtual_can_flow.yaml
uv run uds-mcp-cli flow-run ./examples/flows/demo_virtual_can_flow.yaml --config ./uds.toml
```

Startup config behavior:

- Prefer `./uds.toml` (or custom path via `UDS_MCP_CONFIG_PATH`).
- Fallback to environment variables if the TOML file does not exist.
- For TOML config, relative `flow_repo` and `extension_whitelist` are resolved against the TOML file directory.

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
- `UDS_MCP_UDS_TX_FUNCTIONAL_ID` default: `0x7DF`
- `UDS_MCP_UDS_RX_FUNCTIONAL_ID` default: `0x7E8`
- `UDS_MCP_FLOW_REPO` default: `./flows`
- `UDS_MCP_EXTENSION_WHITELIST` default: `./extensions`
- `UDS_MCP_EXTENSION_IMPORT_WHITELIST` legacy compatibility option (no longer enforced)
- `UDS_MCP_TESTER_PRESENT_INTERVAL` default: `2.0`

## Implemented MCP Tools

- `can_send`
- `can_tail`
- `can_restart`
- `uds_send`
- `tester_present_start`
- `tester_present_stop`
- `tester_present_status`
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
- `trace`: read-only historical step records (`step/request_hex/response_hex`).

Hook outputs can now include `variables` for write-back. Example snippet:

```python
seed = context["response_hex"][4:]
result = {
		"request_hex": "2712" + seed,
		"variables": {"seed": seed},
}
```

`before_hook` can also return `request_sequence_hex` (`list[str]`) to send multiple requests
within one step (for block-level fault injection such as deliberate resend):

```python
req = context["request_hex"]
result = {"request_sequence_hex": [req, req]}
```

For built-in `0x36` TransferData, use standardized `segments` (address + data_hex).
OEM-specific file parsing should be done by external trusted tools, then flow consumes normalized segments.

Static segments example:

```yaml
steps:
	- name: transfer_payload
		transfer_data:
			segments:
				- address: 0x1000
					data_hex: "AABB"
				- address: 0x2000
					data_hex: "CCDD"
			chunk_size: 1
			block_counter_start: 1
```

Dynamic segments via Python hook (avoids huge YAML payloads):

```yaml
steps:
	- name: transfer_payload
		transfer_data:
			segments_hook:
				snippet: |
					# Parse/prepare externally, then return standardized segments
					p = context["variables"]["payload_hex"]
					result = {"segments": [{"address": 0x1000, "data_hex": p}]}
			chunk_size: 256
			block_counter_start: 1
		message_hook:
			snippet: |
				if context["message_index"] == 2:
					result = {"request_hex": "3603EE"}
				else:
					result = {}
```

`message_hook` context includes `message_index`, `message_total`, `step_name`, `request_hex`,
`response_hex`, `variables`, and read-only `trace`.

`after_hook` is also supported per step. It receives current `request_hex`, current `response_hex`,
`variables`, and read-only `trace`. Hook output can include:

- `variables`: write-back variables for next steps.
- `response_hex`: optional response override before `expect` checks.

## UDS Addressing And TesterPresent

- `uds_send` supports `addressing_mode`: `physical` (default) or `functional`.
- `tester_present_start(addressing_mode=...)` and `tester_present_stop()` are available for manual control.
- Flow breakpoint pause uses an internal TesterPresent owner and no longer conflicts with manual start/stop.
- `tester_present_status()` reports whether periodic sending is running, current addressing mode, and active owners.

## Flow-Level TesterPresent Policy

Flow YAML supports `tester_present_policy` with default `breakpoint_only`:

- `breakpoint_only`: only keepalive while paused on breakpoint (backward-compatible behavior).
- `during_flow`: keep keepalive active for full flow run.
- `off`: no automatic keepalive at flow level.

Each step can override with `tester_present`:

- `inherit` (default)
- `on` (enable keepalive for this step)
- `off` (disable flow-level keepalive for this step)

Example:

```yaml
name: security_access_flow
tester_present_policy: during_flow
steps:
	- name: request_seed
		send: "2711"
		tester_present: inherit
	- name: send_key_without_tp
		send: "2712ABCD"
		tester_present: off
```

## SecurityAccess Helpers

- `crypto_aes_cmac(key_hex, data_hex, out_len=16)`: generic AES-CMAC helper.
- `security27_build_key(level, seed_hex, key_hex, out_len=None, include_level_in_cmac=False)`:
	build derived key and `27 xx + key` request payload for service `0x27` flows.

## AI Collaboration Notes

- Recommend using `uv` for environment management and command execution (`uv sync`, `uv run ...`).
- For SecurityAccess (`0x27`) in flow mode, prefer MCP crypto tools over in-hook crypto imports.
- Use `response_hex` + variable write-back in `before_hook` to chain seed-read and key-send steps.

## Hook Runtime Safety

- Hook runtime now executes with unrestricted Python imports and builtins.
- Users are responsible for hook code safety and dependency governance.
- `extension_import_whitelist` is retained for backward compatibility, but no longer enforced.

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
