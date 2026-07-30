from __future__ import annotations

import base64
import gzip
import zlib
from dataclasses import replace
from typing import cast

import pytest

from gh_slate.codec.compression import (
    compress_bytes,
    decode_base64,
    decode_payload,
    decompress_bytes,
    encode_base64,
    encode_payload,
)
from gh_slate.codec.errors import CodecError
from gh_slate.codec.limits import DEFAULT_CODEC_LIMITS


def test_payload_round_trip_is_deterministic() -> None:
    state = '{"message":"你好","nested":[null,true,1]}'.encode()

    first = encode_payload(state)
    second = encode_payload(state)

    assert first == second
    assert decode_payload(first) == state


def test_empty_state_has_a_stable_fixed_profile_encoding() -> None:
    assert encode_payload(b"") == "eAEDAAAAAAE="


@pytest.mark.parametrize(
    "encoded",
    [
        "Zh==",  # Non-zero pad bits; decodes to b"f", whose canonical form is Zg==.
        "Zg=",  # Missing padding.
        "Zg===",  # Excess padding.
        "Z g==",  # Whitespace.
        "_w==",  # URL-safe alphabet.
        "你好",
    ],
)
def test_base64_decoder_rejects_non_canonical_or_non_standard_text(
    encoded: str,
) -> None:
    with pytest.raises(CodecError) as caught:
        decode_base64(encoded)

    assert caught.value.code in {"invalid_base64", "non_canonical_base64"}


def test_base64_round_trip_uses_standard_padded_alphabet() -> None:
    compressed = bytes(range(256))

    encoded = encode_base64(compressed)

    assert encoded == base64.standard_b64encode(compressed).decode("ascii")
    assert decode_base64(encoded) == compressed


def test_decompress_rejects_a_truncated_stream() -> None:
    compressed = compress_bytes(b"state")

    with pytest.raises(CodecError) as caught:
        decompress_bytes(compressed[:-1])

    assert caught.value.code == "truncated_zlib"


@pytest.mark.parametrize("suffix", [b"x", zlib.compress(b"another stream")])
def test_decompress_rejects_trailing_and_concatenated_data(suffix: bytes) -> None:
    compressed = compress_bytes(b"state")

    with pytest.raises(CodecError) as caught:
        decompress_bytes(compressed + suffix)

    assert caught.value.code == "trailing_zlib_data"


def test_decompress_rejects_gzip_and_raw_deflate() -> None:
    raw_compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    raw = raw_compressor.compress(b"state") + raw_compressor.flush()

    for compressed in (gzip.compress(b"state"), raw):
        with pytest.raises(CodecError) as caught:
            decompress_bytes(compressed)

        assert caught.value.code == "invalid_zlib"


def test_decompression_is_bounded_before_a_bomb_can_expand() -> None:
    compressed = compress_bytes(b"x" * 1024)
    limits = replace(DEFAULT_CODEC_LIMITS, max_state_bytes=16)

    with pytest.raises(CodecError) as caught:
        decompress_bytes(compressed, limits=limits)

    assert caught.value.code == "codec_size_limit"
    assert caught.value.details["exceeded"] == {"state_bytes": {"actual_bytes": 17, "max_bytes": 16}}


def test_all_transport_stages_enforce_their_own_limits() -> None:
    with pytest.raises(CodecError, match="state_bytes"):
        compress_bytes(
            b"too large",
            limits=replace(DEFAULT_CODEC_LIMITS, max_state_bytes=4),
        )

    with pytest.raises(CodecError, match="compressed_bytes"):
        encode_base64(
            b"too large",
            limits=replace(DEFAULT_CODEC_LIMITS, max_compressed_bytes=4),
        )

    with pytest.raises(CodecError, match="encoded_bytes"):
        decode_base64(
            "eAEDAAD//wAAAAE=",
            limits=replace(DEFAULT_CODEC_LIMITS, max_encoded_bytes=4),
        )


def test_base64_preflights_decoded_size_before_allocating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_decode(*args: object, **kwargs: object) -> bytes:
        pytest.fail("base64 decoder must not run after the size preflight fails")

    monkeypatch.setattr(base64, "b64decode", unexpected_decode)

    with pytest.raises(CodecError, match="compressed_bytes"):
        decode_base64(
            "eAEDAAD//wAAAAE=",
            limits=replace(
                DEFAULT_CODEC_LIMITS,
                max_encoded_bytes=1024,
                max_compressed_bytes=1,
            ),
        )


def test_public_functions_reject_the_wrong_input_types() -> None:
    with pytest.raises(CodecError, match="must be bytes"):
        compress_bytes(cast("bytes", "state"))
    with pytest.raises(CodecError, match="must be bytes"):
        decompress_bytes(cast("bytes", "state"))
    with pytest.raises(CodecError, match="must be text"):
        decode_base64(cast("str", b"state"))
