from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings, strategies as st

from gh_slate.codec import decode_comment
from gh_slate.errors import GhSlateError

if TYPE_CHECKING:
    from collections.abc import Mapping


ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "tests" / "fixtures" / "codec" / "fuzz" / "corpus.json"
VALID_COMMENT = ROOT / "tests" / "fixtures" / "wire" / "state" / "minimal" / "comment.md"


def _outcome(source: str | bytes) -> tuple[object, ...]:
    try:
        decoded = decode_comment(source)
    except GhSlateError as error:
        return (
            "error",
            type(error).__name__,
            error.code,
            int(error.exit_code),
            error.as_dict(),
        )
    except Exception as error:  # pragma: no cover - the assertion explains a fuzzer finding
        pytest.fail(f"decoder leaked {type(error).__name__}: {error}")
    return (
        "success",
        decoded.state_sha256,
        decoded.actual_render_sha256,
        decoded.drifted,
        decoded.state.to_json(),
    )


def _corpus_source(record: Mapping[str, object]) -> str | bytes:
    kind = record["kind"]
    if kind == "text":
        value = record["value"]
        assert isinstance(value, str)
        return value
    if kind == "hex":
        value = record["value"]
        assert isinstance(value, str)
        return bytes.fromhex(value)

    valid = VALID_COMMENT.read_bytes()
    if kind == "valid_truncate":
        keep_bytes = record["keep_bytes"]
        assert isinstance(keep_bytes, int)
        return valid[:keep_bytes]
    if kind in {
        "valid_payload_char_flip",
        "valid_payload_bit_flip",
    }:
        lines = valid.splitlines(keepends=True)
        payload = bytearray(lines[1])
        payload_index = record["payload_index"]
        assert isinstance(payload_index, int)
        if kind == "valid_payload_char_flip":
            replacement = record["replacement"]
            assert isinstance(replacement, str)
            encoded = replacement.encode("ascii")
            assert len(encoded) == 1
            payload[payload_index] = encoded[0]
        else:
            xor = record["xor"]
            assert isinstance(xor, int)
            payload[payload_index] ^= xor
        lines[1] = bytes(payload)
        return b"".join(lines)
    if kind == "valid_duplicate_splice":
        return valid + b"\n" + valid
    if kind == "valid_pad_to_body_bytes":
        body_bytes = record["body_bytes"]
        assert isinstance(body_bytes, int)
        assert body_bytes >= len(valid)
        return valid + b"x" * (body_bytes - len(valid))
    raise AssertionError(f"unknown fuzz corpus kind: {kind!r}")


def test_decoder_regression_corpus_has_stable_error_contracts() -> None:
    records = json.loads(CORPUS.read_text(encoding="utf-8"))
    assert isinstance(records, list)
    assert records

    for raw_record in records:
        assert isinstance(raw_record, dict)
        record = raw_record
        source = _corpus_source(record)
        first = _outcome(source)
        second = _outcome(source)

        assert first == second, record["id"]
        assert first[0] == "error", record["id"]
        assert first[2] == record["error_code"], record["id"]


@given(
    source=st.one_of(
        st.binary(max_size=8192),
        st.text(
            alphabet=st.characters(exclude_categories=("Cs",)),
            max_size=4096,
        ),
    )
)
@settings(max_examples=120, deadline=1000)
def test_bounded_arbitrary_decoder_input_is_success_or_stable_error(
    source: str | bytes,
) -> None:
    assert _outcome(source) == _outcome(source)


@given(
    start=st.integers(min_value=0, max_value=4096),
    width=st.integers(min_value=0, max_value=512),
    replacement=st.binary(max_size=256),
)
@settings(max_examples=100, deadline=1000)
def test_bounded_mutations_of_a_valid_comment_are_stable(
    start: int,
    width: int,
    replacement: bytes,
) -> None:
    original = VALID_COMMENT.read_bytes()
    bounded_start = min(start, len(original))
    bounded_end = min(bounded_start + width, len(original))
    source = original[:bounded_start] + replacement + original[bounded_end:]

    assert _outcome(source) == _outcome(source)
