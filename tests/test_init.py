from __future__ import annotations

import json
from typing import TYPE_CHECKING

from uds_mcp.init import (
    CONFIG_FILENAME,
    SCHEMA_FILENAME,
    generate_default_config,
    generate_flow_schema,
    project_init,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_generate_flow_schema_creates_file(tmp_path: Path) -> None:
    out = tmp_path / SCHEMA_FILENAME
    status = generate_flow_schema(out)
    assert status == "created"
    assert out.exists()
    schema = json.loads(out.read_text(encoding="utf-8"))
    assert schema.get("type") == "object"
    assert "properties" in schema
    assert "steps" in schema["properties"]


def test_generate_flow_schema_skips_existing(tmp_path: Path) -> None:
    out = tmp_path / SCHEMA_FILENAME
    out.write_text("{}", encoding="utf-8")
    status = generate_flow_schema(out)
    assert status == "skipped"
    assert out.read_text(encoding="utf-8") == "{}"


def test_generate_flow_schema_overwrites(tmp_path: Path) -> None:
    out = tmp_path / SCHEMA_FILENAME
    out.write_text("{}", encoding="utf-8")
    status = generate_flow_schema(out, overwrite=True)
    assert status == "overwritten"
    schema = json.loads(out.read_text(encoding="utf-8"))
    assert "properties" in schema


def test_generate_default_config_creates_file(tmp_path: Path) -> None:
    out = tmp_path / CONFIG_FILENAME
    status = generate_default_config(out)
    assert status == "created"
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert "[can]" in content
    assert "[uds]" in content
    assert "[app]" in content


def test_generate_default_config_skips_existing(tmp_path: Path) -> None:
    out = tmp_path / CONFIG_FILENAME
    out.write_text("existing", encoding="utf-8")
    status = generate_default_config(out)
    assert status == "skipped"
    assert out.read_text(encoding="utf-8") == "existing"


def test_generate_default_config_overwrites(tmp_path: Path) -> None:
    out = tmp_path / CONFIG_FILENAME
    out.write_text("existing", encoding="utf-8")
    status = generate_default_config(out, overwrite=True)
    assert status == "overwritten"
    assert "[can]" in out.read_text(encoding="utf-8")


def test_project_init_creates_both(tmp_path: Path) -> None:
    result = project_init(tmp_path)
    assert result["ok"] is True
    assert result["schema"]["status"] == "created"
    assert result["config"]["status"] == "created"
    assert (tmp_path / SCHEMA_FILENAME).exists()
    assert (tmp_path / CONFIG_FILENAME).exists()


def test_project_init_schema_only(tmp_path: Path) -> None:
    result = project_init(tmp_path, schema_only=True)
    assert "schema" in result
    assert "config" not in result
    assert (tmp_path / SCHEMA_FILENAME).exists()
    assert not (tmp_path / CONFIG_FILENAME).exists()


def test_project_init_config_only(tmp_path: Path) -> None:
    result = project_init(tmp_path, config_only=True)
    assert "config" in result
    assert "schema" not in result
    assert (tmp_path / CONFIG_FILENAME).exists()
    assert not (tmp_path / SCHEMA_FILENAME).exists()


def test_project_init_skips_existing(tmp_path: Path) -> None:
    project_init(tmp_path)
    result = project_init(tmp_path)
    assert result["schema"]["status"] == "skipped"
    assert result["config"]["status"] == "skipped"


def test_project_init_force_overwrites(tmp_path: Path) -> None:
    project_init(tmp_path)
    result = project_init(tmp_path, overwrite=True)
    assert result["schema"]["status"] == "overwritten"
    assert result["config"]["status"] == "overwritten"
