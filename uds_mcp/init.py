from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from uds_mcp.config import AppConfig
from uds_mcp.flow.schema import FlowDefinition

if TYPE_CHECKING:
    from pathlib import Path

SCHEMA_FILENAME = "flow-schema.json"
CONFIG_FILENAME = "uds.toml"


def generate_flow_schema(output: Path, *, overwrite: bool = False) -> str:
    """Generate JSON Schema for FlowDefinition. Returns 'created', 'overwritten', or 'skipped'."""
    if output.exists() and not overwrite:
        return "skipped"
    status = "overwritten" if output.exists() else "created"
    schema = FlowDefinition.model_json_schema()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return status


def generate_default_config(output: Path, *, overwrite: bool = False) -> str:
    """Generate default uds.toml. Returns 'created', 'overwritten', or 'skipped'."""
    if output.exists() and not overwrite:
        return "skipped"
    toml_text = AppConfig().to_toml()
    output.parent.mkdir(parents=True, exist_ok=True)
    status = "overwritten" if output.exists() else "created"
    output.write_text(toml_text, encoding="utf-8")
    return status


def project_init(
    target_dir: Path,
    *,
    overwrite: bool = False,
    schema_only: bool = False,
    config_only: bool = False,
) -> dict[str, Any]:
    """Initialize project files. Returns a summary dict."""
    results: dict[str, Any] = {"ok": True, "target_dir": target_dir.as_posix()}
    target_dir.mkdir(parents=True, exist_ok=True)

    if not config_only:
        schema_path = target_dir / SCHEMA_FILENAME
        results["schema"] = {
            "path": schema_path.as_posix(),
            "status": generate_flow_schema(schema_path, overwrite=overwrite),
        }

    if not schema_only:
        config_path = target_dir / CONFIG_FILENAME
        results["config"] = {
            "path": config_path.as_posix(),
            "status": generate_default_config(config_path, overwrite=overwrite),
        }

    return results
