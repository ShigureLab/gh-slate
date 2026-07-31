from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from gh_slate.codec import DecodedComment

MAX_GITHUB_USER_ID = 2**63 - 1


@dataclass(frozen=True, slots=True)
class GitHubActor:
    id: int
    login: str

    def __post_init__(self) -> None:
        if isinstance(self.id, bool) or not isinstance(self.id, int) or not 1 <= self.id <= MAX_GITHUB_USER_ID:
            raise ValueError("GitHub actor id must be a positive 63-bit integer")
        if not isinstance(self.login, str) or not self.login or "\0" in self.login:
            raise ValueError("GitHub actor login must be non-empty text without NUL bytes")


@dataclass(frozen=True, slots=True)
class GitHubComment:
    id: int
    body: str
    author: str | None
    url: str
    author_id: int | None = None
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
    "MAX_GITHUB_USER_ID",
    "GitHubActor",
    "GitHubComment",
    "ManagedSlate",
    "SlateCandidate",
    "SlateReadStatus",
]
