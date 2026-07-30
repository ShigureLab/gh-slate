from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, replace

from gh_slate.codec.compression import decode_base64, decode_payload, encode_payload
from gh_slate.codec.errors import CodecError
from gh_slate.codec.hashes import (
    canonical_state_bytes,
    normalize_visible_markdown,
    render_sha256,
    state_sha256,
)
from gh_slate.codec.json import DEFAULT_JSON_LIMITS, JsonLimits, canonical_json_bytes, strict_loads
from gh_slate.codec.limits import (
    DEFAULT_CODEC_LIMITS,
    CodecLimits,
    SizeReport,
    enforce_size_limits,
)
from gh_slate.codec.marker import Marker, encode_marker, parse_marker
from gh_slate.codec.model import StateV1
from gh_slate.codec.text import utf8_size


@dataclass(frozen=True, slots=True)
class EncodedComment:
    body: str
    state_sha256: str
    render_sha256: str
    sizes: SizeReport


@dataclass(frozen=True, slots=True)
class DecodedComment:
    state: StateV1
    visible_markdown: str
    state_sha256: str
    expected_render_sha256: str
    actual_render_sha256: str
    drifted: bool
    sizes: SizeReport


def _component_sizes(state: StateV1) -> tuple[int, int, int]:
    data_bytes = len(canonical_json_bytes(state.data))
    schema_bytes = 0 if state.data_schema is None else len(canonical_json_bytes(state.data_schema.to_json()))
    renderer_bytes = len(canonical_json_bytes(state.renderer.to_json()))
    return data_bytes, schema_bytes, renderer_bytes


def _size_report(
    *,
    state: StateV1,
    state_bytes: bytes,
    payload: str,
    body: str,
    visible: str,
    limits: CodecLimits,
) -> SizeReport:
    data_bytes, schema_bytes, renderer_bytes = _component_sizes(state)
    compressed_bytes = len(decode_base64(payload, limits=limits))
    return SizeReport(
        body_bytes=utf8_size(body, field="comment body"),
        visible_bytes=utf8_size(visible, field="visible Markdown"),
        state_bytes=len(state_bytes),
        compressed_bytes=compressed_bytes,
        encoded_bytes=len(payload.encode("ascii")),
        data_bytes=data_bytes,
        schema_bytes=schema_bytes,
        renderer_bytes=renderer_bytes,
    )


def encode_comment(
    state: StateV1,
    visible_markdown: str,
    *,
    limits: CodecLimits = DEFAULT_CODEC_LIMITS,
) -> EncodedComment:
    """Encode one complete managed comment from canonical typed state."""

    if not isinstance(state, StateV1):
        raise CodecError(
            "comment encoding requires a StateV1",
            code="invalid_state",
            details={"path": "state"},
        )
    if not isinstance(visible_markdown, str):
        raise CodecError(
            "visible Markdown must be a string",
            code="invalid_visible_markdown",
        )
    # Validate the caller's text before normalization, but apply the visible
    # size contract to the normalized representation stored in the comment.
    utf8_size(
        visible_markdown,
        field="visible Markdown",
    )

    visible = normalize_visible_markdown(visible_markdown)
    visible_bytes = utf8_size(visible, field="visible Markdown")
    enforce_size_limits(SizeReport(visible_bytes=visible_bytes), limits)
    actual_render_hash = render_sha256(visible)
    if state.render_sha256 != actual_render_hash:
        raise CodecError(
            "state render hash does not match the supplied visible Markdown",
            code="render_hash_mismatch",
            details={
                "expected": state.render_sha256,
                "actual": actual_render_hash,
            },
        )

    state_bytes = canonical_state_bytes(state)
    data_bytes, schema_bytes, renderer_bytes = _component_sizes(state)
    enforce_size_limits(
        SizeReport(
            visible_bytes=visible_bytes,
            state_bytes=len(state_bytes),
            data_bytes=data_bytes,
            schema_bytes=schema_bytes,
            renderer_bytes=renderer_bytes,
        ),
        limits,
    )

    payload = encode_payload(state_bytes, limits=limits)
    digest = state_sha256(state)
    body = encode_marker(
        Marker(
            name=state.name,
            state_sha256=digest,
            payload=payload,
            visible=visible,
        ),
        limits=limits,
    )
    sizes = _size_report(
        state=state,
        state_bytes=state_bytes,
        payload=payload,
        body=body,
        visible=visible,
        limits=limits,
    )
    enforce_size_limits(sizes, limits)
    return EncodedComment(
        body=body,
        state_sha256=digest,
        render_sha256=actual_render_hash,
        sizes=sizes,
    )


def decode_comment(
    body: str | bytes,
    *,
    limits: CodecLimits = DEFAULT_CODEC_LIMITS,
) -> DecodedComment:
    """Decode and integrity-check one complete managed comment."""

    if isinstance(body, bytes):
        enforce_size_limits(SizeReport(body_bytes=len(body)), limits)
        try:
            body_text = body.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise CodecError(
                "comment body is not valid UTF-8",
                code="invalid_utf8",
                details={"field": "comment body", "position": error.start},
            ) from None
    elif isinstance(body, str):
        body_text = body
        enforce_size_limits(
            SizeReport(body_bytes=utf8_size(body, field="comment body")),
            limits,
        )
    else:
        raise CodecError(
            "comment body must be text or UTF-8 bytes",
            code="invalid_codec_input",
            details={"field": "body", "type": type(body).__name__},
        )

    marker = parse_marker(body_text, limits=limits)
    state_bytes = decode_payload(marker.payload, limits=limits)
    json_limits: JsonLimits = replace(
        DEFAULT_JSON_LIMITS,
        max_input_bytes=limits.max_state_bytes,
    )
    parsed = strict_loads(state_bytes, limits=json_limits)
    canonical_bytes = canonical_json_bytes(parsed, limits=json_limits)
    if canonical_bytes != state_bytes:
        raise CodecError(
            "stored state is valid JSON but is not canonical state-v1 JSON",
            code="non_canonical_state",
            details={
                "stored_sha256": hashlib.sha256(state_bytes).hexdigest(),
                "canonical_sha256": hashlib.sha256(canonical_bytes).hexdigest(),
            },
        )
    if not isinstance(parsed, Mapping):
        raise CodecError(
            "stored state root must be a JSON object",
            code="invalid_state",
            details={"path": "state"},
        )

    state = StateV1.from_json(parsed)
    if state.name != marker.name:
        raise CodecError(
            "marker name does not match the stored state name",
            code="state_name_mismatch",
            details={"marker": marker.name, "state": state.name},
        )

    digest = state_sha256(state)
    if digest != marker.state_sha256:
        raise CodecError(
            "marker state hash does not match the canonical stored state",
            code="state_hash_mismatch",
            details={"marker": marker.state_sha256, "actual": digest},
        )

    actual_render_hash = render_sha256(marker.visible)
    sizes = _size_report(
        state=state,
        state_bytes=state_bytes,
        payload=marker.payload,
        body=body_text,
        visible=marker.visible,
        limits=limits,
    )
    enforce_size_limits(sizes, limits)
    return DecodedComment(
        state=state,
        visible_markdown=marker.visible,
        state_sha256=digest,
        expected_render_sha256=state.render_sha256,
        actual_render_sha256=actual_render_hash,
        drifted=actual_render_hash != state.render_sha256,
        sizes=sizes,
    )
