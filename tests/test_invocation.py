from __future__ import annotations

from gh_slate.invocation import (
    DEFAULT_DISPLAY_COMMAND,
    DISPLAY_COMMAND_ENV,
    display_command,
    display_command_with,
)


def test_display_command_uses_executable_name(monkeypatch) -> None:
    monkeypatch.delenv(DISPLAY_COMMAND_ENV, raising=False)

    assert display_command() == "gh-slate"
    assert display_command() == DEFAULT_DISPLAY_COMMAND


def test_blank_display_override_is_ignored(monkeypatch) -> None:
    monkeypatch.setenv(DISPLAY_COMMAND_ENV, "   ")

    assert display_command() == "gh-slate"


def test_display_command_prefers_extension_override(monkeypatch) -> None:
    monkeypatch.setenv(DISPLAY_COMMAND_ENV, "  gh slate  ")

    assert display_command() == "gh slate"
    assert display_command_with("apply ci") == "gh slate apply ci"
    assert display_command_with("   ") == "gh slate"
