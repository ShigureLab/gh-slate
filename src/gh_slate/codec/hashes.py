from __future__ import annotations

import hashlib

from gh_slate.codec.errors import CodecError
from gh_slate.codec.json import canonical_json_bytes
from gh_slate.codec.model import StateDraftV1, StateV1


def normalize_visible_markdown(markdown: str) -> str:
    """Normalize only line endings and the required terminal line feed."""

    if not isinstance(markdown, str):
        raise CodecError(
            "visible Markdown must be a string",
            code="invalid_visible_markdown",
        )
    normalized = markdown.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.endswith("\n"):
        normalized += "\n"
    return normalized


def render_sha256(markdown: str) -> str:
    normalized = normalize_visible_markdown(markdown)
    try:
        rendered = normalized.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise CodecError(
            "visible Markdown is not valid UTF-8 text",
            code="invalid_utf8",
            details={"field": "visible Markdown", "position": error.start},
        ) from None
    return hashlib.sha256(rendered).hexdigest()


def canonical_state_bytes(state: StateV1) -> bytes:
    if not isinstance(state, StateV1):
        raise CodecError(
            "state_sha256 requires a StateV1",
            code="invalid_state",
            details={"path": "state"},
        )
    return canonical_json_bytes(state.to_json())


def functional_state_bytes(state: StateV1 | StateDraftV1) -> bytes:
    """Return canonical functional bytes, deliberately excluding revision."""

    if isinstance(state, StateV1):
        value = state.to_json()
        del value["revision"]
    elif isinstance(state, StateDraftV1):
        value = state.to_json()
    else:
        raise CodecError(
            "functional state requires StateV1 or StateDraftV1",
            code="invalid_state",
            details={"path": "state"},
        )
    return canonical_json_bytes(value)


def state_sha256(state: StateV1) -> str:
    return hashlib.sha256(canonical_state_bytes(state)).hexdigest()
