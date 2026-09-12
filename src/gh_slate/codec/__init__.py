"""Versioned, deterministic state codec for managed gh-slate comments."""

from __future__ import annotations

from gh_slate.codec.comment import (
    DecodedComment,
    EncodedComment,
    decode_comment,
    encode_comment,
)
from gh_slate.codec.errors import CodecError
from gh_slate.codec.hashes import (
    canonical_state_bytes,
    functional_state_bytes,
    normalize_visible_markdown,
    render_sha256,
    state_sha256,
)
from gh_slate.codec.json import (
    JsonLimits,
    JsonValue,
    canonical_json_bytes,
    canonical_number,
    freeze_json,
    strict_loads,
    strict_loads_object,
)
from gh_slate.codec.limits import CodecLimits, SizeReport
from gh_slate.codec.meta import MetaSnapshot
from gh_slate.codec.model import (
    ControllerV1,
    RendererDescriptorV1,
    SchemaSnapshotV1,
    State,
    StateDraft,
    StateDraftV1,
    StateV1,
    validate_slate_name,
)
from gh_slate.codec.revision import RevisionResult, resolve_revision

__all__ = [
    "CodecError",
    "CodecLimits",
    "ControllerV1",
    "DecodedComment",
    "EncodedComment",
    "JsonLimits",
    "JsonValue",
    "RendererDescriptorV1",
    "RevisionResult",
    "SchemaSnapshotV1",
    "SizeReport",
    "StateDraft",
    "State",
    "StateV1",
    "StateDraftV1",
    "MetaSnapshot",
    "canonical_json_bytes",
    "canonical_number",
    "canonical_state_bytes",
    "decode_comment",
    "encode_comment",
    "freeze_json",
    "functional_state_bytes",
    "normalize_visible_markdown",
    "render_sha256",
    "resolve_revision",
    "state_sha256",
    "strict_loads",
    "strict_loads_object",
    "validate_slate_name",
]
