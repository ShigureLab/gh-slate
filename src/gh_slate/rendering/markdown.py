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
_BODY_TOKENS = re.compile(
    r"(?P<code>(?<!`)(?P<ticks>`+)(?!`)[\s\S]*?(?<!`)(?P=ticks)(?!`))"
    r'|(?P<url>(?<![A-Za-z0-9_])https?://[^\s<>"`。，、；：！？（）【】《》「」『』“”‘’…]+)',
    re.IGNORECASE,
)


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
    # A child element keeps GitHub from shortening URL labels to PR references.
    # Isolate @ as well so GFM cannot create a nested email link in the label.
    text = md_text(label).replace("&#64;", "<span>&#64;</span>")
    return Markdown(f'<a href="{attribute}"><span>{text}</span></a>')


def _body_url(address: str) -> str:
    # Keep balanced URL brackets (including IPv6 hosts), but leave prose wrappers
    # and sentence punctuation outside the link. Encode ambiguous URL endings.
    unmatched = {closing: address.count(closing) - address.count(opening) for opening, closing in ("()", "[]", "{}")}
    end = len(address)
    while end:
        last = address[end - 1]
        if last in ".,;:!?'":
            end -= 1
        elif unmatched.get(last, 0) > 0:
            unmatched[last] -= 1
            end -= 1
        else:
            break
    return address[:end]


def _body_code(value: str) -> str:
    # Real GFM code spans suppress autolinks; raw HTML <code> tags do not.
    # Split lines to preserve newlines instead of GFM's code-span normalization.
    lines = []
    for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not line:
            lines.append("<code></code>")
            continue
        fence = "`" * (1 + max((len(run) for run in re.findall(r"`+", line)), default=0))
        padded = f" {line} " if line.strip(" ") else line
        lines.append(f"{fence}{padded}{fence}")
    return "<br>".join(lines)


def md_body(value: object) -> Markdown:
    """Escape plain prose, linking validated bare HTTP(S) URLs outside backticks."""
    source = _text_argument(value, "md_body value")
    chunks: list[str] = []
    offset = 0
    for token in _BODY_TOKENS.finditer(source):
        chunks.append(escape_markdown_text(source[offset : token.start()]))
        text = token.group()
        if token.group("url") is None:
            chunks.append(_body_code(text))
        else:
            address = _body_url(text)
            try:
                chunks.append(md_link(address, address))
            except RenderingError:
                # An invalid token may contain an email or GitHub reference.
                chunks.append(_body_code(address))
            chunks.append(escape_markdown_text(text[len(address) :]))
        offset = token.end()
    chunks.append(escape_markdown_text(source[offset:]))
    return Markdown("".join(chunks))


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
