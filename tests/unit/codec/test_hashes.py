from __future__ import annotations

import hashlib
from typing import cast

import pytest

from gh_slate.codec.errors import CodecError
from gh_slate.codec.hashes import (
    canonical_state_bytes,
    functional_state_bytes,
    normalize_visible_markdown,
    render_sha256,
    state_sha256,
)
from gh_slate.codec.meta import MetaSnapshot
from gh_slate.codec.model import Controller, RendererDescriptor, State


def _state(*, revision: int = 1, data: dict[str, object] | None = None) -> State:
    return State(
        meta=MetaSnapshot.local("ci"),
        name="ci",
        revision=revision,
        controller=Controller(login="octocat", id=1),
        data={"value": 1} if data is None else data,
        renderer=RendererDescriptor(
            config={"source": "{{ data | md_list }}"},
        ),
        render_sha256="a" * 64,
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("", "\n"),
        ("one line", "one line\n"),
        ("already\n", "already\n"),
        ("two\n\n", "two\n\n"),
        ("windows\r\nline\r\n", "windows\nline\n"),
        ("classic\rline", "classic\nline\n"),
        ("  spaces  ", "  spaces  \n"),
    ],
)
def test_visible_markdown_normalization_is_deliberately_narrow(
    source: str,
    expected: str,
) -> None:
    assert normalize_visible_markdown(source) == expected


def test_render_hash_uses_normalized_utf8_markdown() -> None:
    expected = hashlib.sha256("你好\n".encode()).hexdigest()

    assert render_sha256("你好\r\n") == expected
    assert render_sha256("你好") == expected


def test_render_hash_rejects_non_string_input() -> None:
    with pytest.raises(CodecError) as captured:
        render_sha256(cast("str", 42))

    assert captured.value.code == "invalid_visible_markdown"


def test_render_hash_wraps_unpaired_surrogates() -> None:
    with pytest.raises(CodecError) as captured:
        render_sha256("\ud800")

    assert captured.value.code == "invalid_utf8"


def test_state_hash_covers_revision_but_functional_bytes_do_not() -> None:
    revision_one = _state(revision=1)
    revision_two = _state(revision=2)

    assert canonical_state_bytes(revision_one) != canonical_state_bytes(revision_two)
    assert state_sha256(revision_one) != state_sha256(revision_two)
    assert functional_state_bytes(revision_one) == functional_state_bytes(revision_two)


def test_state_hash_is_sha256_of_complete_canonical_state() -> None:
    state = _state()

    assert state_sha256(state) == hashlib.sha256(canonical_state_bytes(state)).hexdigest()
    assert b'"revision":1' in canonical_state_bytes(state)


def test_canonical_state_is_independent_of_input_object_order() -> None:
    first = _state(data={"a": 1, "b": {"x": True, "y": None}})
    second = _state(data={"b": {"y": None, "x": True}, "a": 1})

    assert canonical_state_bytes(first) == canonical_state_bytes(second)
    assert state_sha256(first) == state_sha256(second)
