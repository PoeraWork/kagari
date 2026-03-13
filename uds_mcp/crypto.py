from __future__ import annotations

from Crypto.Cipher import AES
from Crypto.Hash import CMAC


def aes_cmac_hex(key_hex: str, data_hex: str, out_len: int = 16) -> str:
    """Compute AES-CMAC and return uppercase hex output."""
    key = _parse_hex(key_hex, field_name="key_hex")
    data = _parse_hex(data_hex, field_name="data_hex")
    _validate_out_len(out_len)

    cmac = CMAC.new(key, ciphermod=AES)
    cmac.update(data)
    return cmac.digest()[:out_len].hex().upper()


def build_security27_key(
    level: int,
    seed_hex: str,
    key_hex: str,
    *,
    out_len: int | None = None,
    include_level_in_cmac: bool = False,
) -> dict[str, object]:
    """Derive SecurityAccess(0x27) key with configurable CMAC input rule."""
    if not 0 <= level <= 0xFF:
        raise ValueError("level must be in range 0..255")

    seed = _parse_hex(seed_hex, field_name="seed_hex")
    key = _parse_hex(key_hex, field_name="key_hex")

    derived_len = len(seed) if out_len is None else out_len
    _validate_out_len(derived_len)

    cmac_input = bytes([level]) + seed if include_level_in_cmac else seed
    cmac = CMAC.new(key, ciphermod=AES)
    cmac.update(cmac_input)
    derived = cmac.digest()[:derived_len]

    key_sub_function = level if level % 2 == 0 else level + 1
    request_hex = f"27{key_sub_function:02X}{derived.hex().upper()}"

    return {
        "level": level,
        "key_sub_function": key_sub_function,
        "seed_hex": seed.hex().upper(),
        "derived_key_hex": derived.hex().upper(),
        "request_hex": request_hex,
        "include_level_in_cmac": include_level_in_cmac,
        "out_len": derived_len,
    }


def _parse_hex(value: str, *, field_name: str) -> bytes:
    normalized = value.strip().replace(" ", "")
    if normalized.startswith(("0x", "0X")):
        normalized = normalized[2:]
    if len(normalized) % 2 != 0:
        raise ValueError(f"{field_name} must contain an even number of hex chars")
    try:
        return bytes.fromhex(normalized)
    except ValueError as exc:
        raise ValueError(f"{field_name} is not valid hex") from exc


def _validate_out_len(out_len: int) -> None:
    if not 1 <= out_len <= 16:
        raise ValueError("out_len must be in range 1..16")
