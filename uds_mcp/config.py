from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast


@dataclass(slots=True)
class AppConfig:
    """Runtime configuration loaded from environment variables."""

    can_interface: str = "socketcan"
    can_channel: str = "vcan0"
    can_bitrate: int = 500000
    can_fd: bool = False
    can_data_bitrate: int | None = None
    uds_tx_id: int = 0x7E0
    uds_rx_id: int = 0x7E8
    uds_tx_id_functional: int = 0x7DF
    uds_rx_id_functional: int = 0x7E8
    uds_use_data_optimization: bool = False
    uds_min_dlc: int = 8
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
            can_fd=_parse_bool_env(os.getenv("UDS_MCP_CAN_FD", "false")),
            can_data_bitrate=_parse_optional_int_env(os.getenv("UDS_MCP_CAN_DATA_BITRATE")),
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
            uds_use_data_optimization=_parse_bool_env(
                os.getenv("UDS_MCP_UDS_USE_DATA_OPTIMIZATION", "false")
            ),
            uds_min_dlc=int(os.getenv("UDS_MCP_UDS_MIN_DLC", "8")),
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
        cfg = cls.from_toml_dict(data)
        return cfg.resolve_paths(base_dir=path.resolve().parent)

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

        can_table = cast("dict[str, object]", can_cfg or {})
        uds_table = cast("dict[str, object]", uds_cfg or {})
        app_table = cast("dict[str, object]", app_cfg or {})

        raw_import_whitelist = _parse_str_list(
            app_table.get("extension_import_whitelist", []),
            field_name="[app].extension_import_whitelist",
        )

        return cls(
            can_interface=str(can_table.get("interface", "socketcan")),
            can_channel=str(can_table.get("channel", "vcan0")),
            can_bitrate=_parse_int_like(can_table.get("bitrate", 500000), field_name="[can].bitrate"),
            can_fd=_parse_bool_like(can_table.get("fd", False), field_name="[can].fd"),
            can_data_bitrate=(
                _parse_int_like(can_table["data_bitrate"], field_name="[can].data_bitrate")
                if "data_bitrate" in can_table
                else None
            ),
            uds_tx_id=_parse_int_like(
                uds_table.get("tx_physical_id", 0x7E0), field_name="[uds].tx_physical_id"
            ),
            uds_rx_id=_parse_int_like(
                uds_table.get("rx_physical_id", 0x7E8), field_name="[uds].rx_physical_id"
            ),
            uds_tx_id_functional=_parse_int_like(
                uds_table.get("tx_functional_id", 0x7DF), field_name="[uds].tx_functional_id"
            ),
            uds_rx_id_functional=_parse_int_like(
                uds_table.get("rx_functional_id", 0x7E8), field_name="[uds].rx_functional_id"
            ),
            uds_use_data_optimization=_parse_bool_like(
                uds_table.get("use_data_optimization", False),
                field_name="[uds].use_data_optimization",
            ),
            uds_min_dlc=_parse_int_like(uds_table.get("min_dlc", 8), field_name="[uds].min_dlc"),
            flow_repo=Path(str(app_table.get("flow_repo", "./flows"))),
            extension_whitelist=Path(str(app_table.get("extension_whitelist", "./extensions"))),
            extension_import_whitelist=tuple(raw_import_whitelist),
            tester_present_interval_sec=_parse_float_like(
                app_table.get("tester_present_interval_sec", 2.0),
                field_name="[app].tester_present_interval_sec",
            ),
        )

    def to_toml(self) -> str:
        can_data_bitrate_line = ""
        if self.can_data_bitrate is not None:
            can_data_bitrate_line = f"data_bitrate = {self.can_data_bitrate}\n"
        return (
            "[can]\n"
            f"interface = {self.can_interface!r}\n"
            f"channel = {self.can_channel!r}\n"
            f"bitrate = {self.can_bitrate}\n"
            f"fd = {'true' if self.can_fd else 'false'}\n"
            f"{can_data_bitrate_line}\n"
            "[uds]\n"
            f"tx_physical_id = {hex(self.uds_tx_id)}\n"
            f"rx_physical_id = {hex(self.uds_rx_id)}\n\n"
            f"tx_functional_id = {hex(self.uds_tx_id_functional)}\n"
            f"rx_functional_id = {hex(self.uds_rx_id_functional)}\n\n"
            f"use_data_optimization = {'true' if self.uds_use_data_optimization else 'false'}\n"
            f"min_dlc = {self.uds_min_dlc}\n\n"
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
                "fd": self.can_fd,
                "data_bitrate": self.can_data_bitrate,
            },
            "uds": {
                "tx_physical_id": self.uds_tx_id,
                "rx_physical_id": self.uds_rx_id,
                "tx_functional_id": self.uds_tx_id_functional,
                "rx_functional_id": self.uds_rx_id_functional,
                "use_data_optimization": self.uds_use_data_optimization,
                "min_dlc": self.uds_min_dlc,
            },
            "app": {
                "flow_repo": self.flow_repo.as_posix(),
                "extension_whitelist": self.extension_whitelist.as_posix(),
                "extension_import_whitelist": list(self.extension_import_whitelist),
                "tester_present_interval_sec": self.tester_present_interval_sec,
            },
        }

    def resolve_paths(self, *, base_dir: Path) -> AppConfig:
        flow_repo = self.flow_repo
        extension_whitelist = self.extension_whitelist
        if not flow_repo.is_absolute():
            flow_repo = (base_dir / flow_repo).resolve()
        if not extension_whitelist.is_absolute():
            extension_whitelist = (base_dir / extension_whitelist).resolve()
        return replace(
            self,
            flow_repo=flow_repo,
            extension_whitelist=extension_whitelist,
        )


def load_config(default_path: Path | None = None) -> tuple[AppConfig, str]:
    config_path = default_path or Path(os.getenv("UDS_MCP_CONFIG_PATH", "./uds.toml"))
    if config_path.exists():
        return AppConfig.from_toml_file(config_path), str(config_path)
    return AppConfig.from_env(), "env"


def _parse_import_whitelist_env(value: str) -> tuple[str, ...]:
    parts = [item.strip() for item in value.split(",")]
    filtered = [item for item in parts if item]
    return tuple(filtered)


def _parse_bool_env(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized in {"1", "true", "yes", "on"}


def _parse_optional_int_env(value: str | None) -> int | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return int(stripped)


def _parse_int_like(value: object, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        return int(value, 0)
    raise TypeError(f"{field_name} must be an integer")


def _parse_float_like(value: object, *, field_name: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be a number")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return float(value)
    raise TypeError(f"{field_name} must be a number")


def _parse_bool_like(value: object, *, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise TypeError(f"{field_name} must be a boolean")


def _parse_str_list(value: object, *, field_name: str) -> list[str]:
    if not isinstance(value, list):
        raise TypeError(f"{field_name} must be an array of strings")
    if not all(isinstance(item, str) for item in value):
        raise TypeError(f"{field_name} must be an array of strings")
    return [str(item) for item in value]
