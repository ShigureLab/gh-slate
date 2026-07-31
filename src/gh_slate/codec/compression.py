from __future__ import annotations

import base64
import binascii
import re
import zlib

from gh_slate.codec.errors import CodecError
from gh_slate.codec.limits import (
    DEFAULT_CODEC_LIMITS,
    CodecLimits,
    SizeReport,
    enforce_size_limits,
)

ZLIB_LEVEL = 9
ZLIB_WBITS = zlib.MAX_WBITS
ZLIB_MEM_LEVEL = 9
ZLIB_STRATEGY = zlib.Z_FIXED
_STANDARD_BASE64_RE = re.compile(rb"[A-Za-z0-9+/]*={0,2}")


def _require_bytes(value: object, *, field: str) -> bytes:
    if not isinstance(value, bytes):
        raise CodecError(
            f"{field} must be bytes",
            code="invalid_codec_input",
            details={"field": field, "type": type(value).__name__},
        )
    return value


def compress_bytes(
    state: bytes,
    *,
    limits: CodecLimits = DEFAULT_CODEC_LIMITS,
) -> bytes:
    """Compress canonical state bytes with the frozen state-v1 zlib profile."""

    state = _require_bytes(state, field="state")
    enforce_size_limits(SizeReport(state_bytes=len(state)), limits)

    compressor = zlib.compressobj(
        level=ZLIB_LEVEL,
        method=zlib.DEFLATED,
        wbits=ZLIB_WBITS,
        memLevel=ZLIB_MEM_LEVEL,
        strategy=ZLIB_STRATEGY,
    )
    compressed = compressor.compress(state) + compressor.flush(zlib.Z_FINISH)
    enforce_size_limits(SizeReport(compressed_bytes=len(compressed)), limits)
    return compressed


def decompress_bytes(
    compressed: bytes,
    *,
    limits: CodecLimits = DEFAULT_CODEC_LIMITS,
) -> bytes:
    """Strictly decompress one bounded, complete zlib-wrapped stream."""

    compressed = _require_bytes(compressed, field="compressed")
    enforce_size_limits(SizeReport(compressed_bytes=len(compressed)), limits)

    decompressor = zlib.decompressobj(wbits=ZLIB_WBITS)
    try:
        state = decompressor.decompress(compressed, limits.max_state_bytes + 1)
    except zlib.error as error:
        raise CodecError(
            "compressed state is not a valid zlib stream",
            code="invalid_zlib",
        ) from error

    if len(state) > limits.max_state_bytes or decompressor.unconsumed_tail:
        actual = max(len(state), limits.max_state_bytes + 1)
        enforce_size_limits(SizeReport(state_bytes=actual), limits)
        raise CodecError(
            "compressed state could not be consumed completely",
            code="invalid_zlib",
        )

    if decompressor.unused_data:
        raise CodecError(
            "compressed state has trailing or concatenated data",
            code="trailing_zlib_data",
            details={"trailing_bytes": len(decompressor.unused_data)},
        )

    if not decompressor.eof:
        raise CodecError(
            "compressed state is truncated",
            code="truncated_zlib",
        )

    try:
        flushed = decompressor.flush()
    except zlib.error as error:
        raise CodecError(
            "compressed state is not a valid zlib stream",
            code="invalid_zlib",
        ) from error

    if flushed:
        state += flushed
        enforce_size_limits(SizeReport(state_bytes=len(state)), limits)

    return state


def encode_base64(
    compressed: bytes,
    *,
    limits: CodecLimits = DEFAULT_CODEC_LIMITS,
) -> str:
    """Encode bytes as canonical padded standard Base64 text."""

    compressed = _require_bytes(compressed, field="compressed")
    enforce_size_limits(SizeReport(compressed_bytes=len(compressed)), limits)
    encoded = base64.b64encode(compressed).decode("ascii")
    enforce_size_limits(SizeReport(encoded_bytes=len(encoded)), limits)
    return encoded


def decode_base64(
    encoded: str,
    *,
    limits: CodecLimits = DEFAULT_CODEC_LIMITS,
) -> bytes:
    """Decode only canonical padded standard Base64 text."""

    if not isinstance(encoded, str):
        raise CodecError(
            "encoded state must be text",
            code="invalid_codec_input",
            details={"field": "encoded", "type": type(encoded).__name__},
        )

    # A valid Base64 string is ASCII, so its character count is also its byte
    # count. Check this lower bound before allocating a second full-size copy.
    enforce_size_limits(SizeReport(encoded_bytes=len(encoded)), limits)
    try:
        encoded_bytes = encoded.encode("ascii")
    except UnicodeEncodeError as error:
        raise CodecError(
            "encoded state is not ASCII Base64",
            code="invalid_base64",
        ) from error

    if len(encoded_bytes) % 4 != 0 or _STANDARD_BASE64_RE.fullmatch(encoded_bytes) is None:
        raise CodecError(
            "encoded state is not valid standard Base64",
            code="invalid_base64",
        )
    padding = len(encoded_bytes) - len(encoded_bytes.rstrip(b"="))
    estimated_compressed_bytes = (len(encoded_bytes) // 4) * 3 - padding
    enforce_size_limits(
        SizeReport(compressed_bytes=estimated_compressed_bytes),
        limits,
    )
    try:
        compressed = base64.b64decode(encoded_bytes, validate=True)
    except (binascii.Error, ValueError) as error:
        raise CodecError(
            "encoded state is not valid standard Base64",
            code="invalid_base64",
        ) from error

    canonical = base64.b64encode(compressed)
    if canonical != encoded_bytes:
        raise CodecError(
            "encoded state is not canonical padded Base64",
            code="non_canonical_base64",
        )

    enforce_size_limits(SizeReport(compressed_bytes=len(compressed)), limits)
    return compressed


def encode_payload(
    state: bytes,
    *,
    limits: CodecLimits = DEFAULT_CODEC_LIMITS,
) -> str:
    return encode_base64(compress_bytes(state, limits=limits), limits=limits)


def decode_payload(
    encoded: str,
    *,
    limits: CodecLimits = DEFAULT_CODEC_LIMITS,
) -> bytes:
    return decompress_bytes(decode_base64(encoded, limits=limits), limits=limits)
