from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from gh_slate.errors import ExitCode, GhSlateError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


@dataclass(frozen=True, slots=True)
class SchemaDiagnostic:
    """One stable, JSON-serializable schema diagnostic."""

    code: str
    message: str
    data_pointer: str
    schema_pointer: str
    keyword: str | None
    data_pointer_truncated: bool = False
    schema_pointer_truncated: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "data_pointer": self.data_pointer,
            "schema_pointer": self.schema_pointer,
            "keyword": self.keyword,
            "data_pointer_truncated": self.data_pointer_truncated,
            "schema_pointer_truncated": self.schema_pointer_truncated,
        }


class SchemaError(GhSlateError):
    """A schema boundary failure with no validator implementation details."""

    diagnostics: tuple[SchemaDiagnostic, ...]
    truncated: bool

    def __init__(
        self,
        message: str,
        *,
        code: str,
        diagnostics: Sequence[SchemaDiagnostic] = (),
        truncated: bool = False,
        details: Mapping[str, object] | None = None,
    ) -> None:
        self.diagnostics = tuple(diagnostics)
        self.truncated = truncated
        structured_details: dict[str, object] = {} if details is None else dict(details)
        if self.diagnostics:
            structured_details["diagnostics"] = [diagnostic.as_dict() for diagnostic in self.diagnostics]
            structured_details["truncated"] = truncated
        super().__init__(
            message,
            code=code,
            exit_code=ExitCode.VALIDATION,
            details=structured_details,
        )
