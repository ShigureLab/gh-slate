from __future__ import annotations

from decimal import Decimal
from html import unescape

import pytest

from gh_slate.codec import Controller, MetaSnapshot, State, decode_comment
from gh_slate.rendering import RenderingError, jinja_descriptor, materialize_comment
from gh_slate.rendering.jinja import render_jinja


def _render(source: str, data: object = None) -> str:
    return render_jinja(source, data={} if data is None else data, meta=MetaSnapshot.local("ci"))


def test_interpolation_is_literal_and_helpers_compose_in_tables() -> None:
    source = '{{ [{"result": data.label | md_link(data.url), "code": data.code | md_code}] | md_table(columns=["code", "result"]) }}'
    output = _render(source, {"label": "A|B <img> [x]", "url": "https://example.com/a?x=1&y=2|3", "code": "`x|y`"})
    assert '<a href="https://example.com/a?x=1&amp;y=2%7C3">' in output
    assert "<img>" not in output
    assert "<code>&#96;x&#124;y&#96;</code>" in output
    assert output.splitlines()[2].count("|") == 3
    assert "A|B <img> [x]" in unescape(output)
    assert _render("{{ data.text }}", {"text": "<img> | **x**"}) == "&#60;img&#62; &#124; &#42;&#42;x&#42;&#42;"
    assert (
        _render("{{ data.text | md_text }}", {"text": "<img> | **x**"}) == "&#60;img&#62; &#124; &#42;&#42;x&#42;&#42;"
    )


def test_codeblock_fence_and_details_keep_literal_payload() -> None:
    output = _render(
        '{{ data.log | md_codeblock("python") | md_details(data.title) }}',
        {"log": 'print("ok")\n```\n<script>', "title": "<bad>"},
    )
    assert "<summary>&#60;bad&#62;</summary>" in output
    assert '\n\n````python\nprint("ok")\n```\n<script>\n````\n\n</details>' in output


@pytest.mark.parametrize(
    "url", ["javascript:alert(1)", "file:///tmp/key", "//example.com", "https://a\nb", "https://u:p@example.com", 1]
)
def test_link_rejects_invalid_or_non_web_urls(url) -> None:
    with pytest.raises(RenderingError):
        _render('{{ "link" | md_link(data.url) }}', {"url": url})


@pytest.mark.parametrize(
    "source",
    [
        "{% set meta = data %}{{ meta }}",
        "{% for data in [1] %}{{ data }}{% endfor %}",
        "{{ data.items() }}",
        "{{ data.__class__ }}",
        "{{ data.x | safe }}",
        "{{ slate.name }}",
    ],
)
def test_retains_sandbox_and_reserves_new_context(source) -> None:
    with pytest.raises(RenderingError):
        _render(source)


def test_canonical_values_and_local_missing_meta() -> None:
    assert (
        _render(
            "{{ data.value }} / {{ data.flag }} / {{ meta.slate.name }} / {{ meta.target }}",
            {"value": Decimal("1.2300"), "flag": True},
        )
        == "1.23 / true / ci / null"
    )
    with pytest.raises(RenderingError) as error:
        _render("{{ meta.target.number }}")
    assert error.value.code == "jinja_undefined"
    assert "--meta" in error.value.hints[0]
    assert (
        _render("{% for key, value in data | dictsort %}{{ key }}={{ value }};{% endfor %}", {"b": 2, "a": 1})
        == "a=1;b=2;"
    )


def test_state_roundtrip_preserves_snapshot_and_hash() -> None:
    state = State(
        name="ci",
        revision=1,
        controller=Controller(login="bot", id=1),
        data={"x": "<value>"},
        renderer=jinja_descriptor("{{ meta.slate.name }} {{ data.x }}"),
        format="gh-slate/state",
        meta=MetaSnapshot.local("ci"),
        render_sha256="0" * 64,
    )
    materialized = materialize_comment(state)
    decoded = decode_comment(materialized.encoded.body)
    assert materialize_comment(decoded.state).encoded.body == materialized.encoded.body
    assert decoded.state.meta == state.meta
