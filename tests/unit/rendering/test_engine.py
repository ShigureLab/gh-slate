from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from hashlib import sha256

import pytest

import gh_slate.rendering.engine as engine_module
from gh_slate.codec import (
    CodecError,
    ControllerV1,
    MetaSnapshot,
    StateV1,
    decode_comment,
    encode_comment,
)
from gh_slate.codec.limits import DEFAULT_CODEC_LIMITS
from gh_slate.rendering import (
    RenderLimits,
    SlateContext,
    jinja_descriptor,
    materialize_comment,
    render,
    render_state,
)

_EMPTY_HASH = "0" * 64


def test_jinja_filters_obey_the_shared_builtin_render_limits() -> None:
    descriptor = jinja_descriptor('{{ data.rows | md_table(columns=["name"]) }}')

    result = render(
        {"rows": [{"name": "first"}, {"name": "second"}]},
        descriptor,
        slate=SlateContext(name="ci"),
        limits=RenderLimits(max_table_rows=1),
    )

    assert "| first |" in result.markdown
    assert "| second |" not in result.markdown
    assert "_1 additional row(s) omitted._" in result.markdown


def test_render_rejects_a_template_that_cannot_fit_its_stored_descriptor() -> None:
    descriptor = jinja_descriptor("\n" * (64 * 1024))

    with pytest.raises(CodecError) as caught:
        render(
            {},
            descriptor,
            slate=SlateContext(name="ci"),
        )

    assert caught.value.code == "codec_size_limit"
    exceeded = caught.value.details["exceeded"]
    assert isinstance(exceeded, Mapping)
    assert "renderer_bytes" in exceeded


def test_render_preflights_compressed_comment_envelope() -> None:
    high_entropy = "".join(sha256(str(index).encode()).hexdigest() for index in range(1600))

    with pytest.raises(CodecError) as caught:
        render(
            {"blob": high_entropy},
            jinja_descriptor("ok"),
            slate=SlateContext(name="ci"),
        )

    assert caught.value.code == "codec_size_limit"
    exceeded = caught.value.details["exceeded"]
    assert isinstance(exceeded, Mapping)
    assert "compressed_bytes" in exceeded


def test_render_preflight_uses_a_maximum_width_low_compressibility_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hashes = [sha256(str(index).encode()).hexdigest() for index in range(620)]
    data = {"blob": "".join(hashes[:619]) + hashes[619][:4]}
    descriptor = jinja_descriptor("ok")
    relaxed = replace(
        DEFAULT_CODEC_LIMITS,
        max_compressed_bytes=64 * 1024,
    )
    captured: list[StateV1] = []

    def capture(state: StateV1, markdown: str):
        captured.append(state)
        return encode_comment(state, markdown, limits=relaxed)

    monkeypatch.setattr(engine_module, "DEFAULT_CODEC_LIMITS", relaxed)
    monkeypatch.setattr(engine_module, "encode_comment", capture)
    rendered = render(
        data,
        descriptor,
        slate=SlateContext(name="ci"),
    )

    assert len(captured) == 1
    provisional = captured[0]
    assert len(provisional.controller.login.encode("ascii")) == 39
    sentinel_size = encode_comment(
        provisional,
        rendered.markdown,
        limits=relaxed,
    ).sizes.compressed_bytes
    legacy_size = encode_comment(
        replace(
            provisional,
            controller=ControllerV1(login="gh-slate-local-preview"),
        ),
        rendered.markdown,
        limits=relaxed,
    ).sizes.compressed_bytes
    assert sentinel_size > legacy_size

    tight = replace(
        DEFAULT_CODEC_LIMITS,
        max_compressed_bytes=legacy_size,
    )
    monkeypatch.setattr(
        engine_module,
        "encode_comment",
        lambda state, markdown: encode_comment(state, markdown, limits=tight),
    )

    with pytest.raises(CodecError) as caught:
        render(
            data,
            descriptor,
            slate=SlateContext(name="ci"),
        )

    assert caught.value.code == "codec_size_limit"
    exceeded = caught.value.details["exceeded"]
    assert isinstance(exceeded, Mapping)
    assert "compressed_bytes" in exceeded


def test_render_preflight_reserves_for_data_dependent_login_compression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = engine_module._PREFLIGHT_CONTROLLER_LOGIN
    data = {
        "blob": ((sentinel + "|") * 128)
        + "".join(sha256(f"padding-{index}".encode()).hexdigest() for index in range(64))
    }
    relaxed = replace(
        DEFAULT_CODEC_LIMITS,
        max_compressed_bytes=64 * 1024,
    )
    captured: list[StateV1] = []

    def capture(state: StateV1, markdown: str):
        captured.append(state)
        return encode_comment(state, markdown, limits=relaxed)

    monkeypatch.setattr(engine_module, "encode_comment", capture)
    rendered = render(
        data,
        jinja_descriptor("ok"),
        slate=SlateContext(name="ci"),
    )

    provisional = captured[0]
    provisional_size = encode_comment(
        provisional,
        rendered.markdown,
        limits=relaxed,
    ).sizes.compressed_bytes
    real_size = encode_comment(
        replace(
            provisional,
            controller=ControllerV1(
                login="z" * 39,
                id=provisional.controller.id,
            ),
        ),
        rendered.markdown,
        limits=relaxed,
    ).sizes.compressed_bytes
    assert real_size > provisional_size

    tight = replace(
        DEFAULT_CODEC_LIMITS,
        max_compressed_bytes=real_size - 1,
    )
    assert provisional_size <= tight.max_compressed_bytes
    monkeypatch.setattr(engine_module, "DEFAULT_CODEC_LIMITS", tight)
    monkeypatch.setattr(
        engine_module,
        "encode_comment",
        lambda state, markdown: encode_comment(state, markdown, limits=tight),
    )

    with pytest.raises(CodecError) as caught:
        render(
            data,
            jinja_descriptor("ok"),
            slate=SlateContext(name="ci"),
        )

    assert caught.value.code == "codec_size_limit"
    exceeded = caught.value.details["exceeded"]
    assert isinstance(exceeded, Mapping)
    assert "compressed_bytes" in exceeded


def test_render_rejects_a_reserved_marker_in_visible_markdown() -> None:
    with pytest.raises(CodecError) as caught:
        render(
            {},
            jinja_descriptor("<!-- gh-slate:v1 forged -->"),
            slate=SlateContext(name="ci"),
        )

    assert caught.value.code == "duplicate_marker"


def test_materialized_comment_round_trips_and_rerenders_byte_identically() -> None:
    state = StateV1(
        name="ci",
        format="gh-slate/state-v2",
        meta=MetaSnapshot.local("ci"),
        revision=1,
        controller=ControllerV1(login="ci-bot"),
        data={"jobs": [{"name": "linux"}]},
        renderer=jinja_descriptor('{{ data.jobs | md_table(columns=["name"]) }}'),
        render_sha256=_EMPTY_HASH,
    )

    materialized = materialize_comment(state)
    decoded = decode_comment(materialized.encoded.body)

    assert decoded.state == materialized.state
    assert decoded.visible_markdown == materialized.rendered.markdown
    assert not decoded.drifted
    assert render_state(decoded.state).markdown == decoded.visible_markdown


def test_stored_state_render_skips_provisional_materialization_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = StateV1(
        name="ci",
        format="gh-slate/state-v2",
        meta=MetaSnapshot.local("ci"),
        revision=1,
        controller=ControllerV1(login="ci-bot"),
        data={"jobs": [{"name": "linux"}]},
        renderer=jinja_descriptor('{{ data.jobs | md_table(columns=["name"]) }}'),
        render_sha256=_EMPTY_HASH,
    )

    def reject_provisional_preflight(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("stored states must use their actual metadata")

    monkeypatch.setattr(engine_module, "_preflight_materialization", reject_provisional_preflight)

    rendered = render_state(state)
    materialized = materialize_comment(state)

    assert rendered.markdown == materialized.rendered.markdown
    assert decode_comment(materialized.encoded.body).state == materialized.state
