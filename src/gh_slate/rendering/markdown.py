from __future__ import annotations

import html
import re
import string
from collections.abc import Mapping
from decimal import Decimal
from urllib.parse import urlsplit

from gh_slate.codec.errors import CodecError
from gh_slate.codec.json import canonical_json_bytes, canonical_number
from gh_slate.rendering.errors import RenderingError


class Markdown(str):
    """Renderer-created Markdown; never a business-data input type."""


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
    if isinstance(value, Markdown):
        return value
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


def md_text(value: object) -> Markdown:
    # Reapplying a text filter is deliberately literal, including on fragments.
    return Markdown(escape_markdown_text(value) if isinstance(value, str) else render_value(value))


def _text_argument(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise RenderingError(f"{name} must be text", code="jinja_filter_invalid")
    return value


def md_link(label: object, url: object) -> Markdown:
    address = _text_argument(url, "md_link URL")
    try:
        parsed = urlsplit(address)
        valid = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.netloc)
            and parsed.username is None
            and parsed.password is None
            and not any(ord(char) <= 32 or ord(char) == 127 for char in address)
        )
    except ValueError:
        valid = False
    if not valid:
        raise RenderingError("md_link requires an absolute HTTP(S) URL", code="jinja_filter_invalid")
    attribute = html.escape(address, quote=True).replace("|", "%7C")
    return Markdown(f'<a href="{attribute}">{md_text(label)}</a>')


def md_code(value: object) -> Markdown:
    return Markdown(code(_text_argument(value, "md_code value")))


def md_codeblock(value: object, language: object = "") -> Markdown:
    source = _text_argument(value, "md_codeblock value").replace("\r\n", "\n").replace("\r", "\n")
    info = _text_argument(language, "md_codeblock language")
    if re.fullmatch(r"[A-Za-z0-9_+.-]*", info) is None:
        raise RenderingError("md_codeblock language contains invalid characters", code="jinja_filter_invalid")
    fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", source)), default=0))
    return Markdown(f"{fence}{info}\n{source}\n{fence}")


def md_details(value: object, summary: object) -> Markdown:
    body = value if isinstance(value, Markdown) else md_text(value)
    return Markdown(f"<details>\n<summary>{md_text(summary)}</summary>\n\n{body}\n\n</details>")
