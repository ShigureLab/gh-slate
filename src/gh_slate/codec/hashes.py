from __future__ import annotations

import hashlib

from gh_slate.codec.errors import CodecError
from gh_slate.codec.json import canonical_json_bytes
from gh_slate.codec.model import State, StateDraft


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


def canonical_state_bytes(state: State) -> bytes:
    if not isinstance(state, State):
        raise CodecError(
            "state_sha256 requires a State",
            code="invalid_state",
            details={"path": "state"},
        )
    return canonical_json_bytes(state.to_json())


def functional_state_bytes(state: State | StateDraft) -> bytes:
    """Return canonical functional bytes, deliberately excluding revision."""

    if isinstance(state, State):
        value = state.to_json()
        del value["revision"]
    elif isinstance(state, StateDraft):
        value = state.to_json()
    else:
        raise CodecError(
            "functional state requires State or StateDraft",
            code="invalid_state",
            details={"path": "state"},
        )
    return canonical_json_bytes(value)


def state_sha256(state: State) -> str:
    return hashlib.sha256(canonical_state_bytes(state)).hexdigest()
