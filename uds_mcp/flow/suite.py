"""Suite definition models and resolution logic.

A suite YAML file describes which flow files to execute and how.
It supports both batch discovery (``include``/``exclude``) and
explicit per-case control (``cases`` list with tags, variables,
timeout, and retry overrides).
"""

from __future__ import annotations

import glob as _glob
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class SuiteCase(BaseModel):
    """A single explicit test case entry in a suite file."""

    flow: str
    tags: list[str] = Field(default_factory=list)
    timeout_s: float | None = None
    variables: dict[str, Any] = Field(default_factory=dict)
    retry: int = Field(default=0, ge=0)


class SuiteDefinition(BaseModel):
    """Parsed representation of a suite YAML file."""

    name: str = "flow-suite"
    setup: str | None = None
    teardown: str | None = None
    cases: list[SuiteCase] = Field(default_factory=list)
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    timeout_s: float | None = None
    stop_on_fail: bool = False
    tags: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Resolved case – ready for execution
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ResolvedCase:
    """A fully resolved case with path and per-case overrides."""

    flow_path: Path
    tags: list[str] = field(default_factory=list)
    timeout_s: float | None = None
    variables: dict[str, Any] = field(default_factory=dict)
    retry: int = 0


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_suite(path: Path) -> SuiteDefinition:
    """Parse a suite YAML file into a ``SuiteDefinition``."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError("suite file must be a mapping")
    return SuiteDefinition.model_validate(data)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_suite(
    suite: SuiteDefinition,
    *,
    base_dir: Path,
    tag_filter: list[str] | None = None,
) -> list[ResolvedCase]:
    """Merge explicit ``cases`` with ``include``/``exclude`` discovery.

    Returns a list of :class:`ResolvedCase` in execution order.
    Explicit cases come first (preserving order), followed by any
    include-discovered files not already listed.
    """
    resolved: list[ResolvedCase] = []
    seen_paths: set[Path] = set()

    # --- explicit cases ---
    for sc in suite.cases:
        p = Path(sc.flow)
        if not p.is_absolute():
            p = (base_dir / p).resolve()
        if not p.is_file():
            raise FileNotFoundError(f"suite case flow not found: {sc.flow}")
        if p in seen_paths:
            continue
        seen_paths.add(p)
        resolved.append(
            ResolvedCase(
                flow_path=p,
                tags=sc.tags,
                timeout_s=sc.timeout_s,
                variables=dict(sc.variables),
                retry=sc.retry,
            )
        )

    # --- include / exclude discovery ---
    discovered = _discover_flow_paths(suite.include, exclude_patterns=suite.exclude, base_dir=base_dir)
    for p in discovered:
        if p in seen_paths:
            continue
        seen_paths.add(p)
        resolved.append(ResolvedCase(flow_path=p))

    # --- tag filtering ---
    if tag_filter:
        tag_set = set(tag_filter)
        resolved = [c for c in resolved if tag_set.intersection(c.tags)]

    return resolved


# ---------------------------------------------------------------------------
# Flow path discovery (moved from cli.py for reuse)
# ---------------------------------------------------------------------------


def _has_glob_meta(value: str) -> bool:
    return any(token in value for token in ("*", "?", "[", "]"))


def _discover_flow_paths(
    include_specs: list[str],
    *,
    exclude_patterns: list[str],
    base_dir: Path,
) -> list[Path]:
    discovered: set[Path] = set()
    for spec in include_specs:
        candidate = Path(spec)
        if not candidate.is_absolute():
            candidate = (base_dir / candidate).resolve()

        if _has_glob_meta(spec):
            pattern = str(candidate) if candidate.is_absolute() else spec
            for matched in _glob.glob(pattern, recursive=True):  # noqa: PTH207
                found = Path(matched)
                resolved = found.resolve()
                if resolved.is_file() and resolved.suffix.lower() in {".yaml", ".yml"}:
                    discovered.add(resolved)
            continue

        if candidate.is_dir():
            for found in candidate.rglob("*.yaml"):
                discovered.add(found.resolve())
            for found in candidate.rglob("*.yml"):
                discovered.add(found.resolve())
            continue

        if candidate.is_file() and candidate.suffix.lower() in {".yaml", ".yml"}:
            discovered.add(candidate)

    if not exclude_patterns:
        return sorted(discovered)

    return sorted(
        item
        for item in discovered
        if not any(fnmatch(item.as_posix(), pattern) for pattern in exclude_patterns)
    )
