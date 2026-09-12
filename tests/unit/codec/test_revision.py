from __future__ import annotations

import pytest

from gh_slate.codec.errors import CodecError
from gh_slate.codec.meta import MetaSnapshot
from gh_slate.codec.model import (
    MAX_REVISION,
    Controller,
    RendererDescriptor,
    StateDraft,
)
from gh_slate.codec.revision import resolve_revision


def _draft(
    *,
    data: dict[str, object] | None = None,
    render_hash: str = "a" * 64,
) -> StateDraft:
    return StateDraft(
        meta=MetaSnapshot.local("ci"),
        name="ci",
        controller=Controller(login="octocat", id=1),
        data={"value": 1} if data is None else data,
        renderer=RendererDescriptor(
            config={"source": "{{ data | md_list }}"},
        ),
        render_sha256=render_hash,
    )


def test_new_state_starts_at_revision_one() -> None:
    result = resolve_revision(_draft())

    assert result.changed is True
    assert result.state.revision == 1


def test_functionally_identical_state_reuses_previous_state() -> None:
    previous = _draft(data={"a": 1, "b": 2}).with_revision(41)
    reordered = _draft(data={"b": 2, "a": 1})

    result = resolve_revision(reordered, previous=previous)

    assert result.changed is False
    assert result.state is previous
    assert result.state.revision == 41


def test_functional_change_increments_previous_revision() -> None:
    previous = _draft(data={"value": 1}).with_revision(9)

    result = resolve_revision(_draft(data={"value": 2}), previous=previous)

    assert result.changed is True
    assert result.state.revision == 10
    assert result.state.data["value"] == 2


def test_render_hash_is_part_of_functional_state() -> None:
    previous = _draft(render_hash="a" * 64).with_revision(4)

    result = resolve_revision(
        _draft(render_hash="b" * 64),
        previous=previous,
    )

    assert result.changed is True
    assert result.state.revision == 5


def test_revision_overflow_fails_instead_of_wrapping() -> None:
    previous = _draft(data={"value": 1}).with_revision(MAX_REVISION)

    with pytest.raises(CodecError) as captured:
        resolve_revision(_draft(data={"value": 2}), previous=previous)

    assert captured.value.code == "revision_overflow"
