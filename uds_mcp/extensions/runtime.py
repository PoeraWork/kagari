from __future__ import annotations

import builtins
from collections.abc import Callable
from pathlib import Path
from types import MappingProxyType
from typing import Any


class ExtensionRuntime:
    """Load and execute whitelisted Python extension hooks."""

    def __init__(
        self,
        whitelist_dirs: list[Path],
        *,
        import_whitelist: tuple[str, ...] = (),
    ) -> None:
        self._whitelist_dirs = [path.resolve() for path in whitelist_dirs]
        self._import_whitelist = tuple(root for root in import_whitelist if root)

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

        globals_dict: dict[str, Any] = {
            "__builtins__": _safe_builtins(self._import_whitelist),
            "__name__": "__hook__",
        }
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
        globals_dict: dict[str, Any] = {
            "__builtins__": _safe_builtins(self._import_whitelist),
            "__name__": "__hook__",
        }
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


def _safe_builtins(import_whitelist: tuple[str, ...]) -> dict[str, Any]:
    allowed = {
        "abs": abs,
        "all": all,
        "any": any,
        "bool": bool,
        "bytes": bytes,
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
        "__import__": _restricted_import(import_whitelist),
    }
    return allowed


def _restricted_import(import_whitelist: tuple[str, ...]) -> Callable[..., Any]:
    allowed_roots = set(import_whitelist)

    def _import(
        name: str,
        globals_: dict[str, Any] | None = None,
        locals_: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] | list[str] = (),
        level: int = 0,
    ) -> Any:
        del locals_

        target_root = _module_root(name)
        importer_name = ""
        if globals_:
            importer_name = str(globals_.get("__name__", ""))
        importer_root = _module_root(importer_name)

        # Allow direct imports from whitelist only. Internal imports of
        # whitelisted packages are also allowed to keep third-party packages usable.
        if target_root not in allowed_roots and importer_root not in allowed_roots:
            raise ImportError(f"import not allowed in hook sandbox: {name}")

        return builtins.__import__(name, globals_, {}, fromlist, level)

    return _import


def _module_root(module_name: str) -> str:
    if not module_name:
        return ""
    return module_name.split(".", maxsplit=1)[0]
