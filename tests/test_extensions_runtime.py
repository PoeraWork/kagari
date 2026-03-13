from __future__ import annotations

from pathlib import Path

import pytest

from uds_mcp.extensions.runtime import ExtensionRuntime


def test_run_snippet_import_blocked_by_default() -> None:
    runtime = ExtensionRuntime([Path("examples/extensions").resolve()])
    with pytest.raises(ImportError):
        runtime.run_snippet(code="import Crypto\nresult = {}", context={})


def test_run_snippet_import_allowed_by_whitelist() -> None:
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


def test_run_snippet_non_whitelisted_direct_import_denied() -> None:
    runtime = ExtensionRuntime(
        [Path("examples/extensions").resolve()],
        import_whitelist=("Crypto",),
    )
    with pytest.raises(ImportError):
        runtime.run_snippet(code="import os\nresult = {}", context={})
