from __future__ import annotations

from uds_mcp.config import AppConfig
from uds_mcp.server import build_server


def main() -> None:
    config = AppConfig.from_env()
    config.flow_repo.mkdir(parents=True, exist_ok=True)
    config.extension_whitelist.mkdir(parents=True, exist_ok=True)

    mcp = build_server(config)
    mcp.run()
