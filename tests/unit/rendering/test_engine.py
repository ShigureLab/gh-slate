from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest

import gh_slate.rendering.engine as engine_module
from gh_slate.codec import (
    CodecError,
    ControllerV1,
    RendererDescriptorV1,
    StateV1,
    decode_comment,
    encode_comment,
    strict_loads,
)
from gh_slate.codec.limits import DEFAULT_CODEC_LIMITS
from gh_slate.rendering import (
    RenderingError,
    RenderLimits,
    SlateContext,
    TableColumn,
    TableRendererV1,
    jinja_descriptor,
    materialize_comment,
    render,
    render_state,
)
from gh_slate.schema import validate_schema

_EMPTY_HASH = "0" * 64


def test_table_render_canonicalizes_data_and_persists_resolved_schema_columns() -> None:
    schema = validate_schema(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "jobs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "status": {"type": "string"},
                            "name": {"type": "string"},
                        },
                    },
                }
            },
        }
    )
    descriptor = TableRendererV1(
        selector=".jobs",
        title="Jobs",
        columns=(),
    ).to_descriptor()

    result = render(
        {"jobs": [{"name": "linux", "status": "passed"}], "ratio": Decimal("1.2300")},
        descriptor,
        schema=schema,
        slate=SlateContext(name="ci"),
    )

    assert result.data["ratio"] == Decimal("1.23")
    assert result.markdown == ("## Jobs\n\n| status | name |\n| --- | --- |\n| passed | linux |\n")
    assert result.renderer.to_json()["columns"] == [
        {"path": ["status"], "header": "status"},
        {"path": ["name"], "header": "name"},
    ]


def test_schema_projection_supports_quoted_jq_keys_for_empty_tables() -> None:
    schema = validate_schema(
        {
            "type": "object",
            "properties": {
                "key.with]dot": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "value": {"type": "string"},
                        },
                    },
                }
            },
        }
    )
    descriptor = TableRendererV1(
        selector='  .["key.with]dot"] \n',
        columns=(),
    ).to_descriptor()

    result = render(
        {"key.with]dot": []},
        descriptor,
        schema=schema,
        slate=SlateContext(name="ci"),
    )

    assert result.markdown == "| value |\n| --- |\n"
    assert result.renderer.to_json()["columns"] == [{"path": ["value"], "header": "value"}]


@pytest.mark.parametrize(
    "jobs",
    [
        [],
        [{"name": "linux", "status": "passing"}],
    ],
)
def test_table_schema_projection_resolves_local_refs_and_all_of(
    jobs: list[dict[str, str]],
) -> None:
    schema = validate_schema(
        {
            "$defs": {
                "jobs/list": {"$ref": "#/$defs/job-array"},
                "job-array": {
                    "type": "array",
                    "items": {"$ref": "#row"},
                },
                "row": {
                    "$anchor": "row",
                    "type": "object",
                    "allOf": [
                        {
                            "properties": {
                                "status": {"type": "string"},
                            }
                        },
                        {
                            "properties": {
                                "name": {"type": "string"},
                            }
                        },
                    ],
                },
            },
            "allOf": [
                {
                    "type": "object",
                    "properties": {
                        "jobs": {"$ref": "#/$defs/jobs~1list"},
                    },
                }
            ],
        }
    )

    result = render(
        {"jobs": jobs},
        TableRendererV1(selector=".jobs").to_descriptor(),
        schema=schema,
        slate=SlateContext(name="ci"),
    )

    assert result.markdown.startswith("| status | name |\n| --- | --- |\n")
    assert result.renderer.to_json()["columns"] == [
        {"path": ["status"], "header": "status"},
        {"path": ["name"], "header": "name"},
    ]


@pytest.mark.parametrize(
    "jobs",
    [
        [],
        [{"kind": "named", "name": "linux"}],
    ],
)
def test_table_schema_projection_traverses_one_of_and_any_of(
    jobs: list[dict[str, str]],
) -> None:
    schema = validate_schema(
        {
            "$defs": {
                "row": {
                    "type": "object",
                    "anyOf": [
                        {
                            "properties": {
                                "kind": {"const": "named"},
                            }
                        },
                        {
                            "properties": {
                                "name": {"type": "string"},
                            }
                        },
                    ],
                }
            },
            "type": "object",
            "properties": {
                "jobs": {
                    "oneOf": [
                        {
                            "type": "array",
                            "maxItems": 0,
                            "items": {"$ref": "#/$defs/row"},
                        },
                        {
                            "type": "array",
                            "minItems": 1,
                            "items": {"$ref": "#/$defs/row"},
                        },
                    ]
                }
            },
        }
    )

    result = render(
        {"jobs": jobs},
        TableRendererV1(selector=".jobs").to_descriptor(),
        schema=schema,
        slate=SlateContext(name="ci"),
    )

    assert result.markdown.startswith("| kind | name |\n| --- | --- |\n")
    assert result.renderer.to_json()["columns"] == [
        {"path": ["kind"], "header": "kind"},
        {"path": ["name"], "header": "name"},
    ]


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
    hashes = [sha256(str(index).encode()).hexdigest() for index in range(625)]
    data = {"blob": "".join(hashes[:624]) + hashes[624][:4]}
    descriptor = jinja_descriptor("ok")
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
        revision=1,
        controller=ControllerV1(login="ci-bot"),
        data={"jobs": [{"name": "linux"}]},
        renderer=TableRendererV1(
            selector=".jobs",
            title=None,
            columns=(TableColumn(path=("name",), header="name"),),
        ).to_descriptor(),
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
        revision=1,
        controller=ControllerV1(login="ci-bot"),
        data={"jobs": [{"name": "linux"}]},
        renderer=TableRendererV1(
            selector=".jobs",
            title=None,
            columns=(TableColumn(path=("name",), header="name"),),
        ).to_descriptor(),
        render_sha256=_EMPTY_HASH,
    )

    def reject_provisional_preflight(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("stored states must use their actual metadata")

    monkeypatch.setattr(engine_module, "_preflight_materialization", reject_provisional_preflight)

    rendered = render_state(state)
    materialized = materialize_comment(state)

    assert rendered.markdown == materialized.rendered.markdown
    assert decode_comment(materialized.encoded.body).state == materialized.state


def test_materialization_persists_schema_resolved_table_columns() -> None:
    state = StateV1(
        name="ci",
        revision=1,
        controller=ControllerV1(login="ci-bot"),
        data={"jobs": [{"name": "linux", "status": "passed"}]},
        data_schema=validate_schema(
            {
                "type": "object",
                "properties": {
                    "jobs": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "status": {"type": "string"},
                                "name": {"type": "string"},
                            },
                        },
                    }
                },
            }
        ),
        renderer=TableRendererV1(selector=".jobs").to_descriptor(),
        render_sha256=_EMPTY_HASH,
    )

    materialized = materialize_comment(state)
    decoded = decode_comment(materialized.encoded.body)

    expected_columns = [
        {"path": ["status"], "header": "status"},
        {"path": ["name"], "header": "name"},
    ]
    assert materialized.state.renderer.to_json()["columns"] == expected_columns
    assert decoded.state.renderer.to_json()["columns"] == expected_columns
    assert not decoded.drifted
    assert render_state(decoded.state).markdown == decoded.visible_markdown


def test_render_state_does_not_silently_migrate_legacy_renderer_descriptor() -> None:
    descriptor = RendererDescriptorV1(
        kind="builtin-table",
        version=1,
        config={"selector": ".jobs", "columns": ["name"]},
    )
    state = StateV1(
        name="ci",
        revision=1,
        controller=ControllerV1(login="ci-bot"),
        data={"jobs": [{"name": "linux"}]},
        renderer=descriptor,
        render_sha256=_EMPTY_HASH,
    )

    rendered = render_state(state)

    assert rendered.renderer is descriptor
    assert rendered.markdown == ("| name |\n| --- |\n| linux |\n")


def test_permanent_builtin_fixture_rerenders_to_visible_markdown() -> None:
    fixture = Path(__file__).resolve().parents[2] / "fixtures" / "wire" / "state-v1" / "minimal"
    state = StateV1.from_json(strict_loads((fixture / "state.canonical.json").read_bytes()))

    assert render_state(state).markdown == (fixture / "visible.md").read_text(encoding="utf-8")


def test_jinja_descriptor_is_exact_and_context_name_must_match_state() -> None:
    bad = RendererDescriptorV1(
        kind="jinja",
        version=1,
        config={"source": "{{ data.ok }}", "extra": True},
    )
    with pytest.raises(RenderingError) as config_error:
        render(
            {"ok": True},
            bad,
            slate=SlateContext(name="ci"),
        )
    assert config_error.value.code == "renderer_config_invalid"

    state = StateV1(
        name="ci",
        revision=1,
        controller=ControllerV1(login="ci-bot"),
        data={"ok": True},
        renderer=jinja_descriptor("{{ data.ok }}"),
        render_sha256=_EMPTY_HASH,
    )
    with pytest.raises(RenderingError) as missing_context:
        render_state(state)
    assert missing_context.value.code == "render_context_required"

    incomplete_contexts = (
        SlateContext(name="ci"),
        SlateContext(
            name="ci",
            repository="owner/repo",
            number=42,
        ),
        SlateContext(
            name="ci",
            repository="owner/repo",
            url="https://github.com/owner/repo/issues/42",
        ),
        SlateContext(
            name="ci",
            number=42,
            url="https://github.com/owner/repo/issues/42",
        ),
    )
    for context in incomplete_contexts:
        with pytest.raises(RenderingError) as incomplete:
            render_state(state, slate=context)
        assert incomplete.value.code == "render_context_required"

    with pytest.raises(RenderingError) as context_error:
        render_state(
            state,
            slate=SlateContext(
                name="other",
                repository="owner/repo",
                number=42,
                url="https://github.com/owner/repo/issues/42",
            ),
        )
    assert context_error.value.code == "render_context_mismatch"

    complete = render_state(
        state,
        slate=SlateContext(
            name="ci",
            repository="owner/repo",
            number=42,
            url="https://github.com/owner/repo/issues/42",
        ),
    )
    assert complete.markdown == "true\n"


@pytest.mark.parametrize("selector", [".missing | empty", ".[]"])
def test_render_rejects_selectors_without_exactly_one_result(selector: str) -> None:
    descriptor = TableRendererV1(
        selector=selector,
        title=None,
        columns=(),
    ).to_descriptor()

    with pytest.raises(RenderingError) as caught:
        render(
            {"first": [], "second": []},
            descriptor,
            slate=SlateContext(name="ci"),
        )

    assert caught.value.code in {"jq_no_result", "jq_multiple_results"}
