from __future__ import annotations

from pathlib import Path

import pytest

from gh_slate.codec import ControllerV1, State, decode_comment, strict_loads
from gh_slate.configuration import load_profile
from gh_slate.rendering import SlateContext, materialize_comment, render, render_state

PROFILES = Path(__file__).resolve().parents[2] / "examples" / "profiles"


@pytest.mark.parametrize(
    ("profile", "fixture", "heading", "view"),
    [
        ("ci", "ci.json", "CI analysis", None),
        ("benchmark", "benchmark.json", "Benchmark comparison", None),
        ("review", "review-approved.json", "Review passed", "approved"),
        ("review", "review-changes.json", "Review needs changes", "changes_requested"),
        ("review", "review-error.json", "Review did not complete", "error"),
    ],
)
def test_profiles_render_and_reproduce_embedded_definition(profile, fixture, heading, view):
    definition = load_profile(profile, config=str(PROFILES / "boards.toml"))
    result = render(
        strict_loads((PROFILES / fixture).read_bytes()),
        definition.renderer,
        schema=definition.schema,
        slate=SlateContext(name=profile),
    )
    assert heading in result.markdown
    assert result.view == view
    assert result.meta is not None
    state = State(
        name=profile,
        revision=1,
        format="gh-slate/state-v2",
        meta=result.meta,
        controller=ControllerV1(login="example", id=1),
        data=result.data,
        data_schema=result.data_schema,
        renderer=result.renderer,
        render_sha256=result.render_sha256,
    )
    stored = decode_comment(materialize_comment(state).encoded.body).state
    assert render_state(stored).markdown == result.markdown
    assert stored.data["source"] == result.data["source"]
