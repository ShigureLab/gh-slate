from __future__ import annotations

from typing import TYPE_CHECKING

from gh_slate.errors import ExitCode, GhSlateError

if TYPE_CHECKING:
    from collections.abc import Mapping


class DataError(GhSlateError):
    """A stable validation failure at the local typed-data boundary."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(
            message,
            code=code,
            exit_code=ExitCode.VALIDATION,
            details={} if details is None else details,
        )


__all__ = ["DataError"]
