from __future__ import annotations

from gh_slate.codec.errors import CodecError


def utf8_size(value: str, *, field: str) -> int:
    """Return the exact UTF-8 byte size without allocating an encoded copy."""

    if not isinstance(value, str):
        raise CodecError(
            f"{field} must be text",
            code="invalid_codec_input",
            details={"field": field, "type": type(value).__name__},
        )

    total = 0
    for position, character in enumerate(value):
        codepoint = ord(character)
        if codepoint <= 0x7F:
            total += 1
        elif codepoint <= 0x7FF:
            total += 2
        elif 0xD800 <= codepoint <= 0xDFFF:
            raise CodecError(
                f"{field} is not valid UTF-8 text",
                code="invalid_utf8",
                details={"field": field, "position": position},
            )
        elif codepoint <= 0xFFFF:
            total += 3
        else:
            total += 4
    return total


__all__ = ["utf8_size"]
