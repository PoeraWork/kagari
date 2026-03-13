from __future__ import annotations

import pytest

from uds_mcp.crypto import aes_cmac_hex, build_security27_key


def test_aes_cmac_hex_known_vector() -> None:
    # RFC 4493 Example 1
    key_hex = "2B7E151628AED2A6ABF7158809CF4F3C"
    assert aes_cmac_hex(key_hex, "") == "BB1D6929E95937287FA37D129B756746"


def test_build_security27_key_default_seed_rule() -> None:
    result = build_security27_key(
        level=0x11,
        seed_hex="01020304",
        key_hex="FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF",
    )

    assert result["key_sub_function"] == 0x12
    assert str(result["request_hex"]).startswith("2712")
    assert len(str(result["derived_key_hex"])) == 8


def test_build_security27_key_rejects_invalid_length() -> None:
    with pytest.raises(ValueError):
        build_security27_key(
            level=0x11,
            seed_hex="0102",
            key_hex="FFFF",
            out_len=0,
        )
