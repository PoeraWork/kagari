from __future__ import annotations

import os
import tomllib
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
    uds_tx_id_functional: int = 0x7DF
    uds_rx_id_functional: int = 0x7E8
    flow_repo: Path = Path("./flows")
    extension_whitelist: Path = Path("./extensions")
    extension_import_whitelist: tuple[str, ...] = ()
    tester_present_interval_sec: float = 2.0

    @classmethod
    def from_env(cls) -> AppConfig:
        return cls(
            can_interface=os.getenv("UDS_MCP_CAN_INTERFACE", "socketcan"),
            can_channel=os.getenv("UDS_MCP_CAN_CHANNEL", "vcan0"),
            can_bitrate=int(os.getenv("UDS_MCP_CAN_BITRATE", "500000")),
            uds_tx_id=int(os.getenv("UDS_MCP_UDS_TX_ID", "0x7E0"), 0),
            uds_rx_id=int(os.getenv("UDS_MCP_UDS_RX_ID", "0x7E8"), 0),
            uds_tx_id_functional=int(
                os.getenv("UDS_MCP_UDS_TX_FUNCTIONAL_ID", "0x7DF"),
                0,
            ),
            uds_rx_id_functional=int(
                os.getenv("UDS_MCP_UDS_RX_FUNCTIONAL_ID", "0x7E8"),
                0,
            ),
            flow_repo=Path(os.getenv("UDS_MCP_FLOW_REPO", "./flows")),
            extension_whitelist=Path(os.getenv("UDS_MCP_EXTENSION_WHITELIST", "./extensions")),
            extension_import_whitelist=_parse_import_whitelist_env(
                os.getenv("UDS_MCP_EXTENSION_IMPORT_WHITELIST", "")
            ),
            tester_present_interval_sec=float(os.getenv("UDS_MCP_TESTER_PRESENT_INTERVAL", "2.0")),
        )

    @classmethod
    def from_toml_file(cls, path: Path) -> AppConfig:
        with path.open("rb") as f:
            data = tomllib.load(f)
        return cls.from_toml_dict(data)

    @classmethod
    def from_toml_dict(cls, data: dict[str, object]) -> AppConfig:
        can_cfg = data.get("can")
        uds_cfg = data.get("uds")
        app_cfg = data.get("app")

        if can_cfg is not None and not isinstance(can_cfg, dict):
            raise TypeError("[can] must be a table")
        if uds_cfg is not None and not isinstance(uds_cfg, dict):
            raise TypeError("[uds] must be a table")
        if app_cfg is not None and not isinstance(app_cfg, dict):
            raise TypeError("[app] must be a table")

        can_table = can_cfg or {}
        uds_table = uds_cfg or {}
        app_table = app_cfg or {}

        raw_import_whitelist = app_table.get("extension_import_whitelist", [])
        if not isinstance(raw_import_whitelist, list):
            raise TypeError("[app].extension_import_whitelist must be an array of strings")
        if not all(isinstance(item, str) for item in raw_import_whitelist):
            raise TypeError("[app].extension_import_whitelist must be an array of strings")

        return cls(
            can_interface=str(can_table.get("interface", "socketcan")),
            can_channel=str(can_table.get("channel", "vcan0")),
            can_bitrate=int(can_table.get("bitrate", 500000)),
            uds_tx_id=int(uds_table.get("tx_physical_id", 0x7E0)),
            uds_rx_id=int(uds_table.get("rx_physical_id", 0x7E8)),
            uds_tx_id_functional=int(uds_table.get("tx_functional_id", 0x7DF)),
            uds_rx_id_functional=int(uds_table.get("rx_functional_id", 0x7E8)),
            flow_repo=Path(str(app_table.get("flow_repo", "./flows"))),
            extension_whitelist=Path(str(app_table.get("extension_whitelist", "./extensions"))),
            extension_import_whitelist=tuple(raw_import_whitelist),
            tester_present_interval_sec=float(app_table.get("tester_present_interval_sec", 2.0)),
        )

    def to_toml(self) -> str:
        return (
            "[can]\n"
            f"interface = {self.can_interface!r}\n"
            f"channel = {self.can_channel!r}\n"
            f"bitrate = {self.can_bitrate}\n\n"
            "[uds]\n"
            f"tx_physical_id = {hex(self.uds_tx_id)}\n"
            f"rx_physical_id = {hex(self.uds_rx_id)}\n\n"
            f"tx_functional_id = {hex(self.uds_tx_id_functional)}\n"
            f"rx_functional_id = {hex(self.uds_rx_id_functional)}\n\n"
            "[app]\n"
            f"flow_repo = {self.flow_repo.as_posix()!r}\n"
            f"extension_whitelist = {self.extension_whitelist.as_posix()!r}\n"
            f"extension_import_whitelist = {list(self.extension_import_whitelist)!r}\n"
            f"tester_present_interval_sec = {self.tester_present_interval_sec}\n"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "can": {
                "interface": self.can_interface,
                "channel": self.can_channel,
                "bitrate": self.can_bitrate,
            },
            "uds": {
                "tx_physical_id": self.uds_tx_id,
                "rx_physical_id": self.uds_rx_id,
                "tx_functional_id": self.uds_tx_id_functional,
                "rx_functional_id": self.uds_rx_id_functional,
            },
            "app": {
                "flow_repo": self.flow_repo.as_posix(),
                "extension_whitelist": self.extension_whitelist.as_posix(),
                "extension_import_whitelist": list(self.extension_import_whitelist),
                "tester_present_interval_sec": self.tester_present_interval_sec,
            },
        }


def load_config(default_path: Path | None = None) -> tuple[AppConfig, str]:
    config_path = default_path or Path(os.getenv("UDS_MCP_CONFIG_PATH", "./uds.toml"))
    if config_path.exists():
        return AppConfig.from_toml_file(config_path), str(config_path)
    return AppConfig.from_env(), "env"


def _parse_import_whitelist_env(value: str) -> tuple[str, ...]:
    parts = [item.strip() for item in value.split(",")]
    filtered = [item for item in parts if item]
    return tuple(filtered)
