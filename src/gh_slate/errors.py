from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping


class ExitCode(IntEnum):
    SUCCESS = 0
    RUNTIME = 1
    VALIDATION = 2
    NOT_FOUND = 3
    CONFLICT = 4


@dataclass(slots=True)
class GhSlateError(Exception):
    message: str
    code: str = "runtime_error"
    exit_code: ExitCode = ExitCode.RUNTIME
    hints: tuple[str, ...] = ()
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.hints:
            payload["hints"] = list(self.hints)
        if self.details:
            payload["details"] = dict(self.details)
        return {"error": payload}


def format_error(error: GhSlateError) -> tuple[str, ...]:
    lines = [f"error[{error.code}]: {error.message}"]
    lines.extend(f"hint: {hint}" for hint in error.hints)
    return tuple(lines)
