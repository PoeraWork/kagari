from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import MappingProxyType
from typing import Any


class ExtensionRuntime:
    """Load and execute whitelisted Python extension hooks."""

    def __init__(self, whitelist_dirs: list[Path]) -> None:
        self._whitelist_dirs = [path.resolve() for path in whitelist_dirs]

    def run_hook(
        self,
        *,
        script_path: str,
        function_name: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        target = Path(script_path).resolve()
        if not self._is_allowed(target):
            raise PermissionError(f"script path not in whitelist: {target}")

        globals_dict: dict[str, Any] = {"__builtins__": _safe_builtins()}
        locals_dict: dict[str, Any] = {}
        code = target.read_text(encoding="utf-8")
        exec(compile(code, str(target), "exec"), globals_dict, locals_dict)
        fn = _get_callable(locals_dict, function_name)
        result = fn(MappingProxyType(context))
        if result is None:
            return {}
        if not isinstance(result, dict):
            raise TypeError("extension hook must return dict or None")
        return result

    def run_snippet(self, *, code: str, context: dict[str, Any]) -> dict[str, Any]:
        globals_dict: dict[str, Any] = {"__builtins__": _safe_builtins()}
        locals_dict: dict[str, Any] = {"context": context, "result": {}}
        exec(code, globals_dict, locals_dict)
        result = locals_dict.get("result", {})
        if not isinstance(result, dict):
            raise TypeError("snippet must set dict variable: result")
        return result

    def _is_allowed(self, target: Path) -> bool:
        for allowed in self._whitelist_dirs:
            if target.is_relative_to(allowed):
                return True
        return False


def _get_callable(namespace: dict[str, Any], function_name: str) -> Callable[..., Any]:
    value = namespace.get(function_name)
    if value is None or not callable(value):
        raise ValueError(f"function not found: {function_name}")
    return value


def _safe_builtins() -> dict[str, Any]:
    allowed = {
        "abs": abs,
        "all": all,
        "any": any,
        "bool": bool,
        "dict": dict,
        "enumerate": enumerate,
        "float": float,
        "hex": hex,
        "int": int,
        "len": len,
        "list": list,
        "max": max,
        "min": min,
        "range": range,
        "round": round,
        "set": set,
        "str": str,
        "sum": sum,
        "tuple": tuple,
    }
    return allowed
