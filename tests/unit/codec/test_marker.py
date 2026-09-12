from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from typing import TYPE_CHECKING

import pytest

from gh_slate.codec.compression import encode_payload
from gh_slate.codec.errors import CodecError
from gh_slate.codec.limits import DEFAULT_CODEC_LIMITS
from gh_slate.codec.marker import Marker, encode_marker, parse_marker

if TYPE_CHECKING:
    from collections.abc import Callable

STATE_SHA256 = "0123456789abcdef" * 4
PAYLOAD = encode_payload(b'{"format":"gh-slate/state"}')


def make_marker(
    *,
    name: str = "ci-summary",
    state_sha256: str = STATE_SHA256,
    payload: str = PAYLOAD,
    visible: str = "## CI summary\n\n| Job | Status |\n",
) -> Marker:
    return Marker(
        name=name,
        state_sha256=state_sha256,
        payload=payload,
        visible=visible,
    )


def test_marker_round_trip_preserves_the_exact_visible_remainder() -> None:
    marker = make_marker(visible="## 你好  \n\nbody without a final newline")

    body = encode_marker(marker)

    assert body == (
        f"<!-- gh-slate: name=ci-summary encoding=zlib+base64 "
        f"state={STATE_SHA256}\n"
        f"{PAYLOAD}\n"
        "-->\n\n"
        "## 你好  \n\nbody without a final newline"
    )
    assert parse_marker(body) == marker


def test_marker_is_immutable() -> None:
    marker = make_marker()

    field = "name"
    with pytest.raises(FrozenInstanceError):
        setattr(marker, field, "other")


def test_marker_rejects_an_empty_payload() -> None:
    with pytest.raises(CodecError) as caught:
        make_marker(payload="")

    assert caught.value.code == "invalid_marker_payload"


@pytest.mark.parametrize(
    "name",
    [
        "",
        "Uppercase",
        "-leading",
        "has space",
        "has--delimiter",
        "a" * 65,
        "emoji-🐱",
    ],
)
def test_marker_rejects_invalid_slate_names(name: str) -> None:
    with pytest.raises(CodecError) as caught:
        make_marker(name=name)

    assert caught.value.code == "invalid_slate_name"


@pytest.mark.parametrize(
    "state_sha256",
    [
        "0" * 63,
        "0" * 65,
        "G" * 64,
        "A" * 64,
    ],
)
def test_marker_requires_a_canonical_sha256(state_sha256: str) -> None:
    with pytest.raises(CodecError) as caught:
        make_marker(state_sha256=state_sha256)

    assert caught.value.code == "invalid_state_hash"


def test_parser_requires_the_marker_at_the_first_byte() -> None:
    body = "\n" + encode_marker(make_marker())

    with pytest.raises(CodecError) as caught:
        parse_marker(body)

    assert caught.value.code == "marker_not_at_start"


def test_parser_distinguishes_a_missing_marker() -> None:
    with pytest.raises(CodecError) as caught:
        parse_marker("ordinary Markdown")

    assert caught.value.code == "missing_marker"


@pytest.mark.parametrize(
    "rewrite",
    [
        lambda body: body.replace(" name=", "  name=", 1),
        lambda body: body.replace(" encoding=", " state=x encoding=", 1),
        lambda body: body.replace("\n-->\n\n", "\r\n-->\r\n\r\n", 1),
        lambda body: body.replace(STATE_SHA256, STATE_SHA256.upper(), 1),
    ],
)
def test_parser_rejects_any_non_exact_marker_format(
    rewrite: Callable[[str], str],
) -> None:
    body = encode_marker(make_marker())
    malformed = rewrite(body)

    with pytest.raises(CodecError) as caught:
        parse_marker(malformed)

    assert caught.value.code == "invalid_marker"


def test_parser_rejects_non_canonical_base64_inside_an_exact_marker() -> None:
    body = encode_marker(make_marker()).replace(PAYLOAD, PAYLOAD + "=", 1)

    with pytest.raises(CodecError) as caught:
        parse_marker(body)

    assert caught.value.code == "invalid_base64"


def test_parser_rejects_an_additional_managed_marker_anywhere_in_visible_text() -> None:
    visible = "Report\n\n```md\n<!-- gh-slate: malformed -->\n```\n"

    with pytest.raises(CodecError) as caught:
        make_marker(visible=visible)

    assert caught.value.code == "duplicate_marker"


def test_parser_rejects_a_second_complete_marker() -> None:
    first = encode_marker(make_marker(visible="Report"))
    second = encode_marker(make_marker(name="other", visible="Other"))

    with pytest.raises(CodecError) as caught:
        parse_marker(first + "\n" + second)

    assert caught.value.code == "duplicate_marker"


def test_empty_visible_remainder_round_trips() -> None:
    marker = make_marker(visible="")

    assert parse_marker(encode_marker(marker)) == marker


def test_encode_and_parse_enforce_visible_and_body_byte_limits() -> None:
    marker = make_marker(visible="🐱")
    visible_limits = replace(DEFAULT_CODEC_LIMITS, max_visible_bytes=3)

    with pytest.raises(CodecError, match="visible_bytes") as caught:
        encode_marker(marker, limits=visible_limits)

    assert caught.value.details["exceeded"] == {"visible_bytes": {"actual_bytes": 4, "max_bytes": 3}}

    body = encode_marker(marker)
    body_limits = replace(
        DEFAULT_CODEC_LIMITS,
        max_body_bytes=len(body.encode("utf-8")) - 1,
    )
    with pytest.raises(CodecError, match="body_bytes"):
        parse_marker(body, limits=body_limits)


def test_encode_marker_enforces_a_custom_compressed_limit() -> None:
    with pytest.raises(CodecError, match="compressed_bytes") as caught:
        encode_marker(
            make_marker(),
            limits=replace(
                DEFAULT_CODEC_LIMITS,
                max_compressed_bytes=1,
            ),
        )

    assert caught.value.code == "codec_size_limit"


def test_marker_rejects_unpaired_surrogates_as_non_utf8_text() -> None:
    marker = make_marker(visible="\ud800")

    with pytest.raises(CodecError) as caught:
        encode_marker(marker)

    assert caught.value.code == "invalid_utf8"
