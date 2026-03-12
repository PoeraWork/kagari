from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class AppConfig:
    """Runtime configuration loaded from environment variables."""

    can_interface: str = "socketcan"
    can_channel: str = "vcan0"
    can_bitrate: int = 500000
    uds_tx_id: int = 0x7E0
    uds_rx_id: int = 0x7E8
    flow_repo: Path = Path("./flows")
    extension_whitelist: Path = Path("./extensions")
    tester_present_interval_sec: float = 2.0

    @classmethod
    def from_env(cls) -> AppConfig:
        return cls(
            can_interface=os.getenv("UDS_MCP_CAN_INTERFACE", "socketcan"),
            can_channel=os.getenv("UDS_MCP_CAN_CHANNEL", "vcan0"),
            can_bitrate=int(os.getenv("UDS_MCP_CAN_BITRATE", "500000")),
            uds_tx_id=int(os.getenv("UDS_MCP_UDS_TX_ID", "0x7E0"), 0),
            uds_rx_id=int(os.getenv("UDS_MCP_UDS_RX_ID", "0x7E8"), 0),
            flow_repo=Path(os.getenv("UDS_MCP_FLOW_REPO", "./flows")),
            extension_whitelist=Path(os.getenv("UDS_MCP_EXTENSION_WHITELIST", "./extensions")),
            tester_present_interval_sec=float(os.getenv("UDS_MCP_TESTER_PRESENT_INTERVAL", "2.0")),
        )
