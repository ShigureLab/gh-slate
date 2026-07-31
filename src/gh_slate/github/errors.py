from __future__ import annotations

from typing import TYPE_CHECKING

from gh_slate.errors import ExitCode, GhSlateError

if TYPE_CHECKING:
    from collections.abc import Mapping


class GitHubReadError(GhSlateError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        exit_code: ExitCode = ExitCode.RUNTIME,
        details: Mapping[str, object] | None = None,
        hints: tuple[str, ...] = (),
    ) -> None:
        super().__init__(
            message,
            code=code,
            exit_code=exit_code,
            details={} if details is None else details,
            hints=hints,
        )


__all__ = ["GitHubReadError"]
