from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


class ExtensionRuntime:
    """Load and execute whitelisted Python extension hooks."""

    def __init__(
        self,
        whitelist_dirs: list[Path],
        *,
        import_whitelist: tuple[str, ...] = (),
    ) -> None:
        self._whitelist_dirs = [path.resolve() for path in whitelist_dirs]
        # Keep argument for backward compatibility; sandbox import restrictions are disabled.
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

        namespace: dict[str, Any] = {"__name__": "__hook__"}
        code = target.read_text(encoding="utf-8")
        exec(compile(code, str(target), "exec"), namespace, namespace)  # noqa: S102
        fn = _get_callable(namespace, function_name)
        result = fn(MappingProxyType(context))
        if result is None:
            return {}
        if not isinstance(result, dict):
            raise TypeError("extension hook must return dict or None")
        return result

    def run_snippet(self, *, code: str, context: dict[str, Any]) -> dict[str, Any]:
        namespace: dict[str, Any] = {
            "__name__": "__hook__",
            "context": context,
            "assertions": context.get("assertions"),
            "result": {},
        }
        exec(code, namespace, namespace)  # noqa: S102
        result = namespace.get("result", {})
        if not isinstance(result, dict):
            raise TypeError("snippet must set dict variable: result")
        return result

    def _is_allowed(self, target: Path) -> bool:
        return any(target.is_relative_to(allowed) for allowed in self._whitelist_dirs)


def _get_callable(namespace: dict[str, Any], function_name: str) -> Callable[..., Any]:
    value = namespace.get(function_name)
    if value is None or not callable(value):
        raise ValueError(f"function not found: {function_name}")
    return value
