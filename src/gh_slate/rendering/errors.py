from __future__ import annotations

from typing import TYPE_CHECKING

from gh_slate.errors import ExitCode, GhSlateError

if TYPE_CHECKING:
    from collections.abc import Mapping


class RenderingError(GhSlateError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        details: Mapping[str, object] | None = None,
        hints: tuple[str, ...] = (),
    ) -> None:
        super().__init__(
            message,
            code=code,
            exit_code=ExitCode.VALIDATION,
            hints=hints,
            details={} if details is None else details,
        )


__all__ = ["RenderingError"]
