from __future__ import annotations

import string
from collections.abc import Mapping
from decimal import Decimal

from gh_slate.codec.errors import CodecError
from gh_slate.codec.json import canonical_json_bytes, canonical_number
from gh_slate.rendering.errors import RenderingError


class _Missing:
    pass


MISSING = _Missing()
_ASCII_PUNCTUATION = frozenset(string.punctuation)


def compact_json(value: object) -> str:
    try:
        return canonical_json_bytes(value).decode("utf-8")
    except CodecError:
        raise RenderingError(
            "value cannot be rendered as canonical JSON",
            code="render_value_invalid",
        ) from None


def escape_markdown_text(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(
        "<br>" if character == "\n" else f"&#{ord(character)};" if character in _ASCII_PUNCTUATION else character
        for character in normalized
    )


def code(value: str) -> str:
    return f"<code>{escape_markdown_text(value)}</code>"


def render_value(value: object, *, missing: str = "—") -> str:
    if value is MISSING:
        return escape_markdown_text(missing)
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, Decimal):
        return canonical_number(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return canonical_number(value)
    if isinstance(value, str):
        return code('""') if value == "" else escape_markdown_text(value)
    if isinstance(value, (Mapping, tuple, list)):
        return code(compact_json(value))
    raise RenderingError(
        "renderer received a non-JSON value",
        code="render_value_invalid",
        details={"value_type": type(value).__name__},
    )


__all__ = [
    "MISSING",
    "code",
    "compact_json",
    "escape_markdown_text",
    "render_value",
]
