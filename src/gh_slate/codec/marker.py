from __future__ import annotations

import re
from dataclasses import dataclass

from gh_slate.codec.compression import decode_base64
from gh_slate.codec.errors import CodecError
from gh_slate.codec.limits import (
    DEFAULT_CODEC_LIMITS,
    CodecLimits,
    SizeReport,
    enforce_size_limits,
)
from gh_slate.codec.text import utf8_size

MARKER_ENCODING = "zlib+base64"
MANAGED_MARKER_PREFIX = "<!-- gh-slate:"
NAME_PATTERN = r"[a-z0-9][a-z0-9._-]{0,63}"
STATE_SHA256_PATTERN = r"[0-9a-f]{64}"

_NAME_RE = re.compile(rf"\A{NAME_PATTERN}\Z")
_STATE_SHA256_RE = re.compile(rf"\A{STATE_SHA256_PATTERN}\Z")
_PAYLOAD_RE = re.compile(r"\A[A-Za-z0-9+/]+={0,2}\Z")
_MARKER_RE = re.compile(
    rf"\A<!-- gh-slate: name=(?P<name>{NAME_PATTERN}) "
    rf"encoding=zlib\+base64 state=(?P<state_sha256>{STATE_SHA256_PATTERN})\n"
    r"(?P<payload>[A-Za-z0-9+/]+={0,2})\n"
    r"-->\n\n"
    r"(?P<visible>.*)\Z",
    re.DOTALL,
)


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or _NAME_RE.fullmatch(name) is None or "--" in name:
        raise CodecError(
            "slate name is not valid",
            code="invalid_slate_name",
            details={
                "name": name,
                "pattern": NAME_PATTERN,
                "forbidden_substring": "--",
            },
        )


def _validate_state_sha256(state_sha256: str) -> None:
    if not isinstance(state_sha256, str) or _STATE_SHA256_RE.fullmatch(state_sha256) is None:
        raise CodecError(
            "state hash must be 64 lowercase hexadecimal characters",
            code="invalid_state_hash",
        )


def _validate_payload(payload: str) -> None:
    if not isinstance(payload, str) or _PAYLOAD_RE.fullmatch(payload) is None:
        raise CodecError(
            "marker payload must use the standard Base64 alphabet",
            code="invalid_marker_payload",
        )


def _validate_visible(visible: str) -> None:
    if not isinstance(visible, str):
        raise CodecError(
            "visible Markdown must be text",
            code="invalid_codec_input",
            details={"field": "visible", "type": type(visible).__name__},
        )
    if MANAGED_MARKER_PREFIX in visible:
        raise CodecError(
            "comment contains more than one gh-slate marker",
            code="duplicate_marker",
        )


@dataclass(frozen=True, slots=True)
class Marker:
    name: str
    state_sha256: str
    payload: str
    visible: str
    encoding: str = MARKER_ENCODING

    def __post_init__(self) -> None:
        if self.encoding != MARKER_ENCODING:
            raise CodecError(
                f"unsupported marker encoding: {self.encoding}",
                code="unsupported_marker_encoding",
                details={
                    "encoding": self.encoding,
                    "supported_encoding": MARKER_ENCODING,
                },
            )
        _validate_name(self.name)
        _validate_state_sha256(self.state_sha256)
        _validate_payload(self.payload)
        _validate_visible(self.visible)


def encode_marker(
    marker: Marker,
    *,
    limits: CodecLimits = DEFAULT_CODEC_LIMITS,
) -> str:
    """Encode a marker and its exact visible Markdown remainder."""

    if not isinstance(marker, Marker):
        raise CodecError(
            "marker must be a Marker instance",
            code="invalid_codec_input",
            details={"field": "marker", "type": type(marker).__name__},
        )
    compressed = decode_base64(marker.payload, limits=limits)
    body = (
        f"<!-- gh-slate: name={marker.name} encoding=zlib+base64 "
        f"state={marker.state_sha256}\n"
        f"{marker.payload}\n"
        "-->\n\n"
        f"{marker.visible}"
    )
    enforce_size_limits(
        SizeReport(
            body_bytes=utf8_size(body, field="body"),
            visible_bytes=utf8_size(marker.visible, field="visible"),
            compressed_bytes=len(compressed),
            encoded_bytes=len(marker.payload),
        ),
        limits,
    )
    return body


def parse_marker(
    body: str,
    *,
    limits: CodecLimits = DEFAULT_CODEC_LIMITS,
) -> Marker:
    """Parse the exact marker at the beginning of a comment body."""

    if not isinstance(body, str):
        raise CodecError(
            "comment body must be text",
            code="invalid_codec_input",
            details={"field": "body", "type": type(body).__name__},
        )

    enforce_size_limits(
        SizeReport(body_bytes=utf8_size(body, field="body")),
        limits,
    )

    if not body.startswith(MANAGED_MARKER_PREFIX):
        if MANAGED_MARKER_PREFIX in body:
            raise CodecError(
                "gh-slate marker must begin at the first byte of the comment",
                code="marker_not_at_start",
            )
        raise CodecError(
            "comment does not contain a gh-slate marker",
            code="missing_marker",
        )

    match = _MARKER_RE.fullmatch(body)
    if match is None:
        raise CodecError(
            "comment does not contain an exact gh-slate: marker",
            code="invalid_marker",
        )

    visible = match.group("visible")
    payload = match.group("payload")
    _validate_visible(visible)
    compressed = decode_base64(payload, limits=limits)
    enforce_size_limits(
        SizeReport(
            visible_bytes=utf8_size(visible, field="visible"),
            compressed_bytes=len(compressed),
            encoded_bytes=len(payload),
        ),
        limits,
    )
    return Marker(
        name=match.group("name"),
        state_sha256=match.group("state_sha256"),
        payload=payload,
        visible=visible,
    )
