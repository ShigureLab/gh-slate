from __future__ import annotations

from collections.abc import Mapping

import pytest

from gh_slate.codec import CodecError, RendererDescriptor
from gh_slate.codec.meta import MetaSnapshot
from gh_slate.configuration import load_profile
from gh_slate.rendering import RenderingError, render
from gh_slate.rendering.routing import selected_view
from gh_slate.schema import SchemaError


def _renderer(pointer="/outcome", views=None):
    return RendererDescriptor(
        config={
            "profile": "review",
            "view_by": pointer,
            "views": views
            or {
                "approved": "Passed: {{ data.summary }}",
                "changes_requested": "Findings: {{ data.findings | md_list }}",
                "error": "Failed: {{ data.error.message }}",
            },
        },
    )


@pytest.mark.parametrize(
    ("data", "view", "prefix"),
    [
        ({"outcome": "approved", "summary": "ready"}, "approved", "Passed:"),
        ({"outcome": "changes_requested", "findings": {"F17": "fix"}}, "changes_requested", "Findings:"),
        ({"outcome": "error", "error": {"message": "unavailable"}}, "error", "Failed:"),
    ],
)
def test_only_the_selected_view_is_rendered(data, view, prefix):
    result = render(data, _renderer(), meta=MetaSnapshot.local("ci"))
    assert result.view == view
    assert result.markdown.startswith(prefix)
    views = result.renderer.config["views"]
    assert isinstance(views, Mapping)
    assert views["error"] == "Failed: {{ data.error.message }}"


@pytest.mark.parametrize(
    ("data", "code"),
    [
        ({}, "view_route_missing"),
        ({"outcome": "unknown"}, "view_not_found"),
        ({"outcome": False}, "view_route_type"),
        ({"outcome": None}, "view_route_type"),
    ],
)
def test_route_failure_is_explicit_without_a_fallback(data, code):
    with pytest.raises(RenderingError) as error:
        render(data, _renderer(), meta=MetaSnapshot.local("ci"))
    assert error.value.code == code


def test_json_pointer_escaping_and_array_access_are_standard():
    assert selected_view(_renderer("/a~1b/~0outcome/0"), {"a/b": {"~outcome": ["approved"]}}) == "approved"
    assert (
        render(
            {"a/b": {"~outcome": ["approved"]}},
            _renderer("/a~1b/~0outcome/0", {"approved": "ok"}),
            meta=MetaSnapshot.local("ci"),
        ).view
        == "approved"
    )
    for pointer in ("outcome", "/bad~2key", "#/outcome"):
        with pytest.raises(RenderingError) as error:
            selected_view(_renderer(pointer), {"outcome": "approved"})
        assert error.value.code == "view_route_invalid"


def test_schema_checks_the_candidate_before_route_selection():
    with pytest.raises(SchemaError) as error:
        render(
            {"outcome": "unknown", "summary": 42},
            _renderer(),
            meta=MetaSnapshot.local("ci"),
            schema={"properties": {"summary": {"type": "string"}}},
        )
    assert error.value.code == "schema_validation_failed"


@pytest.mark.parametrize("extra", ["template = 'approved.j2'", "unexpected = true"])
def test_multi_view_profile_rejects_conflicting_or_unknown_fields(tmp_path, extra):
    config = tmp_path / "boards.toml"
    config.write_text(
        '[profiles.review]\nview_by = "/outcome"\n' + extra + '\n[profiles.review.views]\napproved = "approved.j2"\n'
    )
    with pytest.raises(RenderingError):
        load_profile("review", config=str(config))


def test_loading_checks_all_views_even_an_unselected_branch(tmp_path):
    config = tmp_path / "boards.toml"
    config.write_text(
        '[profiles.review]\nview_by = "/outcome"\n[profiles.review.views]\napproved = "approved.j2"\nerror = "error.j2"\n'
    )
    (tmp_path / "approved.j2").write_text("Approved")
    (tmp_path / "error.j2").write_text("{% if %}")
    with pytest.raises(RenderingError) as error:
        load_profile("review", config=str(config))
    assert error.value.code == "jinja_syntax_error"


def test_views_share_the_existing_renderer_component_budget(tmp_path):
    config = tmp_path / "boards.toml"
    config.write_text(
        '[profiles.review]\nview_by = "/mode"\n[profiles.review.views]\ncompact = "compact.j2"\ndetailed = "detailed.j2"\n'
    )
    for name in ("compact", "detailed"):
        (tmp_path / f"{name}.j2").write_text("x" * 40_000)
    with pytest.raises(CodecError) as error:
        load_profile("review", config=str(config))
    assert error.value.code == "codec_size_limit"


def test_router_has_no_builtin_business_outcomes():
    result = render(
        {"presentation": {"style": "compact"}},
        _renderer("/presentation/style", {"compact": "Summary"}),
        meta=MetaSnapshot.local("benchmark"),
    )
    assert result.view == "compact"
    assert result.markdown == "Summary\n"
