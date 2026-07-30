from __future__ import annotations

import os

DEFAULT_DISPLAY_COMMAND = "gh-slate"
DISPLAY_COMMAND_ENV = "GH_SLATE_DISPLAY_CMD"


def display_command() -> str:
    """Return the command spelling that should be shown to the user."""
    configured = os.environ.get(DISPLAY_COMMAND_ENV, "").strip()
    if configured:
        return configured
    return DEFAULT_DISPLAY_COMMAND


def display_command_with(arguments: str) -> str:
    suffix = arguments.strip()
    command = display_command()
    if not suffix:
        return command
    return f"{command} {suffix}"
