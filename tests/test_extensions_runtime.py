from __future__ import annotations

from pathlib import Path

from uds_mcp.extensions.runtime import ExtensionRuntime


def test_run_snippet_import_allowed_by_default() -> None:
    runtime = ExtensionRuntime([Path("examples/extensions").resolve()])
    result = runtime.run_snippet(code="import os\nresult = {'ok': bool(os.name)}", context={})
    assert result["ok"]


def test_run_snippet_import_allowed_with_whitelist_config_present() -> None:
    runtime = ExtensionRuntime(
        [Path("examples/extensions").resolve()],
        import_whitelist=("Crypto",),
    )
    result = runtime.run_snippet(
        code=(
            "from Crypto.Hash import CMAC\n"
            "from Crypto.Cipher import AES\n"
            "c = CMAC.new(bytes.fromhex('00' * 16), ciphermod=AES)\n"
            "c.update(bytes.fromhex('0102'))\n"
            "result = {'cmac_hex': c.digest().hex().upper()}\n"
        ),
        context={},
    )
    assert result["cmac_hex"]


def test_run_snippet_can_import_non_whitelisted_module() -> None:
    runtime = ExtensionRuntime(
        [Path("examples/extensions").resolve()],
        import_whitelist=("Crypto",),
    )
    result = runtime.run_snippet(code="import os\nresult = {'cwd': bool(os.getcwd())}", context={})
    assert result["cwd"]
