from __future__ import annotations

import tomllib
from pathlib import Path

from uds_mcp.config import AppConfig, load_config


def test_app_config_from_toml_dict() -> None:
    cfg = AppConfig.from_toml_dict(
        {
            "can": {
                "interface": "virtual",
                "channel": "ch-1",
                "bitrate": 250000,
                "fd": True,
                "data_bitrate": 2000000,
            },
            "uds": {
                "tx_physical_id": 0x700,
                "rx_physical_id": 0x708,
                "tx_functional_id": 0x7DF,
                "rx_functional_id": 0x7E9,
                "use_data_optimization": True,
                "dlc": 32,
                "min_dlc": 16,
            },
            "app": {
                "flow_repo": "./flows2",
                "extension_whitelist": "./extensions2",
                "extension_import_whitelist": ["Crypto"],
                "tester_present_interval_sec": 1.5,
            },
        }
    )

    assert cfg.can_interface == "virtual"
    assert cfg.can_channel == "ch-1"
    assert cfg.can_bitrate == 250000
    assert cfg.can_fd is True
    assert cfg.can_data_bitrate == 2000000
    assert cfg.uds_tx_id == 0x700
    assert cfg.uds_rx_id == 0x708
    assert cfg.uds_tx_id_functional == 0x7DF
    assert cfg.uds_rx_id_functional == 0x7E9
    assert cfg.uds_use_data_optimization is True
    assert cfg.uds_dlc == 32
    assert cfg.uds_min_dlc == 16
    assert cfg.flow_repo == Path("./flows2")
    assert cfg.extension_whitelist == Path("./extensions2")
    assert cfg.extension_import_whitelist == ("Crypto",)
    assert cfg.tester_present_interval_sec == 1.5


def test_app_config_to_toml_roundtrip() -> None:
    cfg = AppConfig(
        can_interface="virtual",
        can_channel="uds-mcp-test",
        can_bitrate=500000,
        can_fd=True,
        can_data_bitrate=2000000,
        uds_tx_id=0x7E0,
        uds_rx_id=0x7E8,
        uds_tx_id_functional=0x7DF,
        uds_rx_id_functional=0x7E8,
        uds_use_data_optimization=True,
        uds_dlc=32,
        uds_min_dlc=16,
        flow_repo=Path("./flows"),
        extension_whitelist=Path("./extensions"),
        extension_import_whitelist=("Crypto",),
        tester_present_interval_sec=2.0,
    )

    parsed = tomllib.loads(cfg.to_toml())

    assert parsed["can"]["interface"] == "virtual"
    assert parsed["can"]["channel"] == "uds-mcp-test"
    assert parsed["can"]["bitrate"] == 500000
    assert parsed["can"]["fd"] is True
    assert parsed["can"]["data_bitrate"] == 2000000
    assert parsed["uds"]["tx_physical_id"] == 0x7E0
    assert parsed["uds"]["rx_physical_id"] == 0x7E8
    assert parsed["uds"]["tx_functional_id"] == 0x7DF
    assert parsed["uds"]["rx_functional_id"] == 0x7E8
    assert parsed["uds"]["use_data_optimization"] is True
    assert parsed["uds"]["dlc"] == 32
    assert parsed["uds"]["min_dlc"] == 16
    assert parsed["app"]["extension_import_whitelist"] == ["Crypto"]


def test_load_config_prefers_toml(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "uds.toml"
    path.write_text(
        """
[can]
interface = "virtual"
channel = "runtime-1"
bitrate = 500000
fd = true
data_bitrate = 2000000

[uds]
tx_physical_id = 0x701
rx_physical_id = 0x709
tx_functional_id = 0x7df
rx_functional_id = 0x7ea
use_data_optimization = true
dlc = 32
min_dlc = 16

[app]
flow_repo = "./flows"
extension_whitelist = "./extensions"
extension_import_whitelist = ["Crypto"]
tester_present_interval_sec = 2.0
""".strip()
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("UDS_MCP_CONFIG_PATH", str(path))
    cfg, source = load_config()

    assert cfg.can_interface == "virtual"
    assert cfg.can_channel == "runtime-1"
    assert cfg.can_fd is True
    assert cfg.can_data_bitrate == 2000000
    assert cfg.uds_tx_id == 0x701
    assert cfg.uds_tx_id_functional == 0x7DF
    assert cfg.uds_rx_id_functional == 0x7EA
    assert cfg.uds_use_data_optimization is True
    assert cfg.uds_dlc == 32
    assert cfg.uds_min_dlc == 16
    assert cfg.flow_repo == (tmp_path / "flows").resolve()
    assert cfg.extension_whitelist == (tmp_path / "extensions").resolve()
    assert source == str(path)


def test_load_config_fallback_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("UDS_MCP_CONFIG_PATH", str(tmp_path / "missing.toml"))
    monkeypatch.setenv("UDS_MCP_CAN_INTERFACE", "virtual")
    monkeypatch.setenv("UDS_MCP_CAN_CHANNEL", "env-channel")
    monkeypatch.setenv("UDS_MCP_CAN_BITRATE", "250000")
    monkeypatch.setenv("UDS_MCP_CAN_FD", "true")
    monkeypatch.setenv("UDS_MCP_CAN_DATA_BITRATE", "2000000")
    monkeypatch.setenv("UDS_MCP_UDS_TX_FUNCTIONAL_ID", "0x7DF")
    monkeypatch.setenv("UDS_MCP_UDS_RX_FUNCTIONAL_ID", "0x7EA")
    monkeypatch.setenv("UDS_MCP_UDS_USE_DATA_OPTIMIZATION", "true")
    monkeypatch.setenv("UDS_MCP_UDS_DLC", "24")
    monkeypatch.setenv("UDS_MCP_UDS_MIN_DLC", "12")
    monkeypatch.setenv("UDS_MCP_EXTENSION_IMPORT_WHITELIST", "Crypto,hashlib")

    cfg, source = load_config()

    assert cfg.can_interface == "virtual"
    assert cfg.can_channel == "env-channel"
    assert cfg.can_bitrate == 250000
    assert cfg.can_fd is True
    assert cfg.can_data_bitrate == 2000000
    assert cfg.uds_tx_id_functional == 0x7DF
    assert cfg.uds_rx_id_functional == 0x7EA
    assert cfg.uds_use_data_optimization is True
    assert cfg.uds_dlc == 24
    assert cfg.uds_min_dlc == 12
    assert cfg.extension_import_whitelist == ("Crypto", "hashlib")
    assert source == "env"
