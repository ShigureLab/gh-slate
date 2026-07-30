from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from gh_slate.codec import DecodedComment


@dataclass(frozen=True, slots=True)
class GitHubComment:
    id: int
    body: str
    author: str | None
    url: str
    created_at: str | None = None
    updated_at: str | None = None


SlateReadStatus = Literal["valid", "drifted", "corrupt", "duplicate"]


@dataclass(frozen=True, slots=True)
class SlateCandidate:
    name: str
    comment: GitHubComment
    status: SlateReadStatus
    decoded: DecodedComment | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class ManagedSlate:
    name: str
    comment: GitHubComment
    decoded: DecodedComment

    @property
    def status(self) -> SlateReadStatus:
        return "drifted" if self.decoded.drifted else "valid"


__all__ = [
    "GitHubComment",
    "ManagedSlate",
    "SlateCandidate",
    "SlateReadStatus",
]
