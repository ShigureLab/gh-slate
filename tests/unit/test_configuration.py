from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from gh_slate.cli import run
from gh_slate.configuration import load_profile
from gh_slate.rendering import RenderingError

if TYPE_CHECKING:
    from pathlib import Path


def _config(tmp_path: Path, *, template: str = "{{ data.message }}", schema: bool = True) -> Path:
    directory = tmp_path / "any-directory"
    directory.mkdir()
    (directory / "layout.j2").write_text(template)
    (directory / "schema.json").write_text(json.dumps({"type": "object", "required": ["message"]}))
    path = directory / "boards.toml"
    path.write_text(
        'version = 1\n[profiles.summary]\ntemplate = "layout.j2"\n' + ('schema = "schema.json"\n' if schema else "")
    )
    return path


def test_profile_paths_are_relative_to_explicit_configuration(tmp_path, monkeypatch):
    path = _config(tmp_path)
    initial = load_profile("summary", config=str(path))
    elsewhere = tmp_path / "another-cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert load_profile("summary", config=str(path)) == initial
    assert initial.renderer.configuration == {"profile": "summary", "source": "{{ data.message }}"}
    assert str(path) not in str(initial)
    assert initial.schema is not None


def test_cli_config_wins_over_environment_and_absence_does_not_search(tmp_path, monkeypatch):
    path = _config(tmp_path)
    monkeypatch.setenv("GH_SLATE_CONFIG", "/missing/config.toml")
    assert load_profile("summary", config=str(path)).schema is not None
    monkeypatch.setenv("GH_SLATE_CONFIG", str(path))
    assert load_profile("summary").schema is not None
    monkeypatch.delenv("GH_SLATE_CONFIG")
    monkeypatch.chdir(path.parent)
    with pytest.raises(RenderingError) as error:
        load_profile("summary")
    assert error.value.code == "config_required"


def test_profile_accepts_absolute_and_parent_paths_without_expansion(tmp_path):
    path = _config(tmp_path)
    outer = tmp_path / "parent.j2"
    outer.write_text("parent")
    for location in ("../parent.j2", str(outer)):
        path.write_text(f'version = 1\n[profiles.summary]\ntemplate = "{location}"\n')
        assert load_profile("summary", config=str(path)).renderer.configuration["source"] == "parent"
    path.write_text('version = 1\n[profiles.summary]\ntemplate = "$HOME/layout.j2"\n')
    with pytest.raises(RenderingError) as error:
        load_profile("summary", config=str(path))
    assert error.value.code == "input_read_failed"


@pytest.mark.parametrize(
    "document",
    [
        "version = true\n[profiles.summary]\ntemplate = 'x'",
        "version = 2\n[profiles.summary]\ntemplate = 'x'",
        "version = 1\nunknown = true\n[profiles.summary]\ntemplate = 'x'",
        "version = 1\n[profiles.summary]\ntemplate = 7",
        "version = 1\n[profiles.summary]\ntemplate = 'https://example.com/x'",
        "version = 1\n[profiles.summary]\nschema = 'schema.json'",
        "version = 1\n[profiles.summary]\ntemplate = 'layout.j2'\nunknown = true",
        "version = 1\nversion = 1",
    ],
)
def test_configuration_rejects_invalid_definitions(tmp_path, document):
    path = _config(tmp_path)
    path.write_text(document)
    with pytest.raises(RenderingError):
        load_profile("summary", config=str(path))


def test_unknown_profile_and_invalid_source_fail_at_load(tmp_path):
    path = _config(tmp_path)
    with pytest.raises(RenderingError) as error:
        load_profile("absent", config=str(path))
    assert error.value.code == "profile_not_found"
    (path.parent / "layout.j2").write_text("{% if %}")
    with pytest.raises(RenderingError) as error:
        load_profile("summary", config=str(path))
    assert error.value.code == "jinja_syntax_error"


def test_profile_option_conflicts_are_rejected_before_target_access(tmp_path, capsys):
    path = _config(tmp_path)
    assert run(["apply", "ci", "--config", str(path)]) == 2
    assert "renderer_option_conflict" in capsys.readouterr().err
    assert run(["apply", "ci", "--config", str(path), "--profile", "summary", "--schema", "missing"]) == 2
    assert "renderer_option_conflict" in capsys.readouterr().err
    assert run(["render", "ci", "--config", str(path), "--profile", "summary", "--schema", "override"]) == 2
    assert "renderer_option_conflict" in capsys.readouterr().err


def test_local_profile_render(tmp_path, capsys):
    path = _config(tmp_path)
    data = tmp_path / "data.json"
    data.write_text('{"message":"ready"}')
    assert run(["render", "ci", "--config", str(path), "--profile", "summary", "--data", str(data), "--json"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["markdown"] == "ready\n"
    assert preview["profile"] == "summary"
