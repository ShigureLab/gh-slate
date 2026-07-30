from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path

import pytest

from gh_slate.codec import (
    CodecError,
    ControllerV1,
    RendererDescriptorV1,
    StateV1,
    decode_comment,
    strict_loads,
)
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

    with pytest.raises(RenderingError) as context_error:
        render_state(state, slate=SlateContext(name="other"))
    assert context_error.value.code == "render_context_mismatch"


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
