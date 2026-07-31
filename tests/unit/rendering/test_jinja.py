from __future__ import annotations

from dataclasses import replace
from decimal import ROUND_DOWN, Decimal, Inexact, getcontext, localcontext

import pytest

from gh_slate.rendering.errors import RenderingError
from gh_slate.rendering.jinja import (
    DEFAULT_JINJA_LIMITS,
    JinjaLimits,
    SlateContext,
    render_jinja,
)


def slate() -> SlateContext:
    return SlateContext(
        name="ci-summary",
        repository="owner/repo",
        number=42,
        url="https://github.example/owner/repo/issues/42",
    )


def test_renders_only_canonical_data_and_slate_context() -> None:
    rendered = render_jinja(
        "{{ data.summary.passed }}/{{ data.ratio }}/{{ slate.name }}/{{ slate.number }}",
        data={
            "summary": {"passed": 3},
            "ratio": Decimal("1.2300"),
        },
        slate=slate(),
    )

    assert rendered == "3/1.23/ci-summary/42"


def test_optional_slate_context_fields_are_explicit_nulls() -> None:
    rendered = render_jinja(
        "{{ slate.repository }}/{{ slate.number }}/{{ slate.url }}",
        data={},
        slate=SlateContext(name="local"),
    )

    assert rendered == "null/null/null"


def test_rendering_is_deterministic() -> None:
    source = "{{ data.metadata | compact_json }}"
    data = {"metadata": {"z": Decimal("1E+22"), "a": Decimal("-0.00")}}

    first = render_jinja(source, data=data, slate=slate())
    second = render_jinja(source, data=data, slate=slate())

    assert first == second == '{"a":0,"z":1e+22}'


def test_decimal_arithmetic_uses_a_fixed_isolated_context() -> None:
    source = "{{ data.one / data.seven }}"
    data = {"one": Decimal(1), "seven": Decimal(7)}
    expected = "0.1428571428571428571428571429"

    with localcontext() as caller:
        caller.prec = 2
        caller.rounding = ROUND_DOWN
        caller.traps[Inexact] = True

        assert render_jinja(source, data=data, slate=slate()) == expected
        assert getcontext().prec == 2
        assert getcontext().rounding == ROUND_DOWN
        assert getcontext().traps[Inexact] is True


@pytest.mark.parametrize(
    "source",
    [
        "{{ data.__class__ }}",
        "{{ data.items() }}",
        "{{ range(3) }}",
        "{% include 'dashboard.md.j2' %}",
        "{% import 'helpers.md.j2' as helpers %}",
        "{% from 'helpers.md.j2' import helper %}",
        "{% macro helper() %}x{% endmacro %}",
        "{{ data.text + data.text }}",
        "{{ '%100s' % data.text }}",
        "{{ 'x' * 2 }}",
        "{{ 2 ** 8 }}",
        "{{ data.text ~ data.text }}",
    ],
)
def test_rejects_unsafe_or_amplifying_constructs(source: str) -> None:
    with pytest.raises(RenderingError) as caught:
        render_jinja(source, data={"text": "x"}, slate=slate())

    assert caught.value.code in {
        "jinja_construct_forbidden",
        "jinja_security_error",
    }


def test_strict_undefined_has_a_stable_error() -> None:
    with pytest.raises(RenderingError) as caught:
        render_jinja("{{ data.missing }}", data={}, slate=slate())

    assert caught.value.code == "jinja_undefined"


def test_default_jinja_globals_are_not_exposed() -> None:
    with pytest.raises(RenderingError) as caught:
        render_jinja("{{ cycler }}", data={}, slate=slate())

    assert caught.value.code == "jinja_undefined"


def test_direct_container_interpolation_requires_an_explicit_filter() -> None:
    with pytest.raises(RenderingError) as caught:
        render_jinja('{{ data["items"] }}', data={"items": [1, 2]}, slate=slate())

    assert caught.value.code == "jinja_container_interpolation"


def test_compact_json_is_the_only_generic_container_projection() -> None:
    rendered = render_jinja(
        '{{ data["items"] | compact_json }}',
        data={"items": [Decimal("1.00"), {"ok": True}]},
        slate=slate(),
    )

    assert rendered == '[1,{"ok":true}]'


def test_md_table_uses_the_builtin_markdown_renderer() -> None:
    rendered = render_jinja(
        '{{ data.rows | md_table(columns=["name", "ok"]) }}',
        data={"rows": [{"name": "linux|x64", "ok": True}]},
        slate=slate(),
    )

    assert rendered == "| name | ok |\n| --- | --- |\n| linux&#124;x64 | true |"


def test_md_list_uses_the_builtin_markdown_renderer() -> None:
    rendered = render_jinja(
        "{{ data.values | md_list }}",
        data={"values": ["ready", None]},
        slate=slate(),
    )

    assert rendered == ("- <code>&#91;0&#93;</code>: ready\n- <code>&#91;1&#93;</code>: null")


def test_source_limit_is_checked_before_parsing() -> None:
    limits = replace(DEFAULT_JINJA_LIMITS, max_source_bytes=4)

    with pytest.raises(RenderingError) as caught:
        render_jinja("12345", data={}, slate=slate(), limits=limits)

    assert caught.value.code == "jinja_source_limit"


def test_ast_node_limit_is_checked_before_compilation() -> None:
    limits = replace(DEFAULT_JINJA_LIMITS, max_ast_nodes=3)

    with pytest.raises(RenderingError) as caught:
        render_jinja("{{ data.a }}", data={"a": 1}, slate=slate(), limits=limits)

    assert caught.value.code == "jinja_ast_limit"


def test_loop_limit_counts_actual_iterations_even_without_output() -> None:
    limits = replace(DEFAULT_JINJA_LIMITS, max_loop_iterations=2)

    with pytest.raises(RenderingError) as caught:
        render_jinja(
            '{% for item in data["items"] %}{% endfor %}',
            data={"items": [1, 2, 3]},
            slate=slate(),
            limits=limits,
        )

    assert caught.value.code == "jinja_loop_limit"


def test_loop_budget_is_shared_by_nested_and_sequential_loops() -> None:
    limits = replace(DEFAULT_JINJA_LIMITS, max_loop_iterations=3)

    with pytest.raises(RenderingError) as caught:
        render_jinja(
            ('{% for item in data["items"] %}{% endfor %}{% for item in data["items"] %}{% endfor %}'),
            data={"items": [1, 2]},
            slate=slate(),
            limits=limits,
        )

    assert caught.value.code == "jinja_loop_limit"


def test_output_limit_is_checked_while_streaming() -> None:
    limits = replace(DEFAULT_JINJA_LIMITS, max_output_bytes=4)

    with pytest.raises(RenderingError) as caught:
        render_jinja(
            "{{ data.text }}",
            data={"text": "12345"},
            slate=slate(),
            limits=limits,
        )

    assert caught.value.code == "jinja_output_limit"


@pytest.mark.parametrize(
    "source",
    [
        "{{ 1 // 0 }}",
        "{{ data.one / data.zero }}",
    ],
)
def test_arithmetic_failures_are_wrapped(source: str) -> None:
    with pytest.raises(RenderingError) as caught:
        render_jinja(
            source,
            data={"one": Decimal(1), "zero": Decimal(0)},
            slate=slate(),
        )

    assert caught.value.code == "jinja_render_error"
    assert caught.value.details["error_type"] in {
        "DivisionByZero",
        "ZeroDivisionError",
    }


def test_invalid_unicode_generated_by_a_literal_is_wrapped() -> None:
    with pytest.raises(RenderingError) as caught:
        render_jinja(r'{{ "\ud800" }}', data={}, slate=slate())

    assert caught.value.code == "jinja_output_invalid"


def test_pathological_integer_literal_is_wrapped_as_a_resource_error() -> None:
    source = "{{ " + ("9" * 5_000) + " }}"

    with pytest.raises(RenderingError) as caught:
        render_jinja(source, data={}, slate=slate())

    assert caught.value.code == "jinja_resource_limit"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_source_bytes", 0),
        ("max_ast_nodes", True),
        ("max_loop_iterations", -1),
        ("max_output_bytes", 0),
    ],
)
def test_limits_require_positive_integers(field: str, value: object) -> None:
    arguments = {
        "max_source_bytes": 1,
        "max_ast_nodes": 1,
        "max_loop_iterations": 1,
        "max_output_bytes": 1,
    }
    arguments[field] = value

    with pytest.raises(ValueError, match=field):
        JinjaLimits(**arguments)  # type: ignore[arg-type]
