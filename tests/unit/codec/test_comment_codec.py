from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from gh_slate.codec.comment import decode_comment, encode_comment
from gh_slate.codec.compression import encode_payload
from gh_slate.codec.errors import CodecError
from gh_slate.codec.hashes import canonical_state_bytes, render_sha256, state_sha256
from gh_slate.codec.json import canonical_json_bytes
from gh_slate.codec.limits import CodecLimits
from gh_slate.codec.marker import Marker, encode_marker
from gh_slate.codec.model import (
    JSON_SCHEMA_DIALECT_2020_12,
    ControllerV1,
    RendererDescriptorV1,
    SchemaSnapshotV1,
    StateV1,
)

VISIBLE = """\
## CI summary

| Job | Passed |
| --- | --- |
| linux | true |
"""


def make_state(
    *,
    visible: str = VISIBLE,
    name: str = "ci-summary",
    renderer_version: int = 1,
) -> StateV1:
    return StateV1(
        name=name,
        revision=7,
        controller=ControllerV1(login="github-actions[bot]"),
        data={
            "jobs": (
                {
                    "name": "linux",
                    "passed": True,
                    "duration_ms": Decimal("12.50"),
                    "note": "你好 👋",
                    "optional": None,
                },
            ),
            "empty": {},
        },
        data_schema=None,
        renderer=RendererDescriptorV1(
            kind="builtin-table",
            version=renderer_version,
            config={
                "selector": ".jobs",
                "columns": ("name", "passed"),
            },
        ),
        render_sha256=render_sha256(visible),
    )


def test_comment_round_trip_preserves_typed_state() -> None:
    state = make_state()

    encoded = encode_comment(state, VISIBLE)
    decoded = decode_comment(encoded.body)

    assert decoded.state == state
    assert decoded.visible_markdown == VISIBLE
    assert decoded.state_sha256 == state_sha256(state)
    assert decoded.expected_render_sha256 == render_sha256(VISIBLE)
    assert decoded.actual_render_sha256 == render_sha256(VISIBLE)
    assert decoded.drifted is False
    assert decoded.sizes == encoded.sizes
    assert decoded.sizes.data_bytes > 0
    assert decoded.sizes.schema_bytes == 0
    assert decoded.sizes.renderer_bytes > 0


def test_encode_normalizes_line_endings_and_terminal_newline() -> None:
    visible = "## Result\r\n\r\npassed"
    state = make_state(visible="## Result\n\npassed\n")

    encoded = encode_comment(state, visible)

    assert encoded.body.endswith("## Result\n\npassed\n")
    assert decode_comment(encoded.body).drifted is False


def test_visible_limit_applies_after_line_ending_normalization() -> None:
    state = make_state(visible="\n")
    limits = replace(
        CodecLimits(),
        max_visible_bytes=1,
    )

    encoded = encode_comment(state, "\r\n", limits=limits)

    assert decode_comment(encoded.body, limits=limits).visible_markdown == "\n"


def test_visible_drift_keeps_canonical_state_readable() -> None:
    state = make_state()
    encoded = encode_comment(state, VISIBLE)
    manually_edited = encoded.body.replace("| linux | true |", "| linux | false |")

    decoded = decode_comment(manually_edited)

    assert decoded.state == state
    assert decoded.drifted is True
    assert decoded.expected_render_sha256 != decoded.actual_render_sha256


def test_unknown_renderer_remains_readable() -> None:
    state = make_state(renderer_version=999)

    decoded = decode_comment(encode_comment(state, VISIBLE).body)

    assert decoded.state.renderer.version == 999
    assert decoded.state.renderer.config["selector"] == ".jobs"


def test_encode_rejects_render_hash_mismatch() -> None:
    state = make_state()

    with pytest.raises(CodecError, match="render hash") as error:
        encode_comment(state, "different")

    assert error.value.code == "render_hash_mismatch"


def test_encode_wraps_visible_unicode_errors_before_hashing() -> None:
    state = make_state()

    with pytest.raises(CodecError, match="UTF-8") as error:
        encode_comment(state, "\ud800")

    assert error.value.code == "invalid_utf8"


def test_decode_rejects_marker_name_mismatch() -> None:
    encoded = encode_comment(make_state(), VISIBLE)
    tampered = encoded.body.replace("name=ci-summary", "name=other", 1)

    with pytest.raises(CodecError, match="name does not match") as error:
        decode_comment(tampered)

    assert error.value.code == "state_name_mismatch"


def test_decode_rejects_marker_hash_mismatch() -> None:
    encoded = encode_comment(make_state(), VISIBLE)
    tampered = encoded.body.replace(
        f"state={encoded.state_sha256}",
        f"state={'0' * 64}",
        1,
    )

    with pytest.raises(CodecError, match="state hash") as error:
        decode_comment(tampered)

    assert error.value.code == "state_hash_mismatch"


def test_decode_rejects_semantically_valid_noncanonical_state() -> None:
    state = make_state()
    noncanonical = b" " + canonical_state_bytes(state)
    body = encode_marker(
        Marker(
            name=state.name,
            state_sha256=state_sha256(state),
            payload=encode_payload(noncanonical),
            visible=VISIBLE,
        )
    )

    with pytest.raises(CodecError, match="not canonical") as error:
        decode_comment(body)

    assert error.value.code == "non_canonical_state"


def test_decode_wraps_invalid_utf8() -> None:
    with pytest.raises(CodecError, match="UTF-8") as error:
        decode_comment(b"\xff")

    assert error.value.code == "invalid_utf8"


def test_decode_rejects_unknown_marker_major_version_explicitly() -> None:
    encoded = encode_comment(make_state(), VISIBLE)
    newer = encoded.body.replace("gh-slate:v1", "gh-slate:v2", 1)

    with pytest.raises(CodecError, match="unsupported marker version") as error:
        decode_comment(newer)

    assert error.value.code == "unsupported_marker_version"


def test_decode_wraps_an_extremely_long_marker_version() -> None:
    encoded = encode_comment(make_state(), VISIBLE)
    newer = encoded.body.replace("gh-slate:v1", f"gh-slate:v{'9' * 5000}", 1)

    with pytest.raises(CodecError, match="unsupported marker version") as error:
        decode_comment(newer)

    assert error.value.code == "unsupported_marker_version"
    assert error.value.details["version_truncated"] is True


def test_decode_rejects_unknown_state_major_version_explicitly() -> None:
    state = make_state()
    state_document = state.to_json()
    state_document["format"] = "gh-slate/state-v2"
    body = encode_marker(
        Marker(
            name=state.name,
            state_sha256="0" * 64,
            payload=encode_payload(canonical_json_bytes(state_document)),
            visible=VISIBLE,
        )
    )

    with pytest.raises(CodecError, match="unsupported state format") as error:
        decode_comment(body)

    assert error.value.code == "unsupported_state_format"


def test_component_limit_failure_includes_full_breakdown() -> None:
    state = make_state()
    limits = CodecLimits(
        max_body_bytes=4096,
        max_visible_bytes=4096,
        max_state_bytes=4096,
        max_compressed_bytes=4096,
        max_encoded_bytes=4096,
        max_data_bytes=1,
        max_schema_bytes=4096,
        max_renderer_bytes=4096,
    )

    with pytest.raises(CodecError, match="size limit") as error:
        encode_comment(state, VISIBLE, limits=limits)

    assert error.value.code == "codec_size_limit"
    exceeded = error.value.details["exceeded"]
    assert isinstance(exceeded, dict)
    assert "data_bytes" in exceeded


@pytest.mark.parametrize(
    ("report_field", "limit_field"),
    [
        ("body_bytes", "max_body_bytes"),
        ("visible_bytes", "max_visible_bytes"),
        ("state_bytes", "max_state_bytes"),
        ("compressed_bytes", "max_compressed_bytes"),
        ("encoded_bytes", "max_encoded_bytes"),
        ("data_bytes", "max_data_bytes"),
        ("schema_bytes", "max_schema_bytes"),
        ("renderer_bytes", "max_renderer_bytes"),
    ],
)
def test_real_comment_components_accept_exact_limits_and_reject_one_byte_less(
    report_field: str,
    limit_field: str,
) -> None:
    state = replace(
        make_state(),
        data_schema=SchemaSnapshotV1(
            dialect=JSON_SCHEMA_DIALECT_2020_12,
            document={"type": "object"},
        ),
    )
    encoded = encode_comment(state, VISIBLE)
    actual = getattr(encoded.sizes, report_field)
    assert actual > 0

    exact_limits = replace(CodecLimits(), **{limit_field: actual})
    assert encode_comment(state, VISIBLE, limits=exact_limits).state_sha256 == encoded.state_sha256
    assert decode_comment(encoded.body, limits=exact_limits).state == state

    too_small = replace(CodecLimits(), **{limit_field: actual - 1})
    with pytest.raises(CodecError) as encode_error:
        encode_comment(state, VISIBLE, limits=too_small)
    with pytest.raises(CodecError) as decode_error:
        decode_comment(encoded.body, limits=too_small)

    assert encode_error.value.code == "codec_size_limit"
    assert decode_error.value.code == "codec_size_limit"
    encode_exceeded = encode_error.value.details["exceeded"]
    decode_exceeded = decode_error.value.details["exceeded"]
    assert isinstance(encode_exceeded, dict)
    assert isinstance(decode_exceeded, dict)
    assert report_field in encode_exceeded
    assert report_field in decode_exceeded
