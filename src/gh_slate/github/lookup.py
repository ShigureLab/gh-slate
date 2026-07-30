from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, cast
from urllib.parse import urlsplit

from gh_slate.errors import ExitCode
from gh_slate.github.errors import GitHubReadError


class TargetProcess(Protocol):
    def repo_view(self, hostname: str | None = None) -> object: ...

    def pr_view(
        self,
        repository: str | None = None,
        hostname: str | None = None,
    ) -> object: ...


def _object(value: object, *, subject: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise GitHubReadError(
            f"{subject} returned a non-object response",
            code="target_lookup_invalid",
            exit_code=ExitCode.VALIDATION,
        )
    return cast("Mapping[str, object]", value)


def _positive_integer(value: object, *, subject: str) -> int:
    if isinstance(value, bool):
        result = 0
    elif isinstance(value, int):
        result = value
    elif isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        result = int(value)
    else:
        result = 0
    if result <= 0:
        raise GitHubReadError(
            f"{subject} response is missing a positive number",
            code="target_lookup_invalid",
            exit_code=ExitCode.VALIDATION,
        )
    return result


def _url(
    value: object,
    *,
    subject: str,
) -> tuple[str, tuple[str, ...]]:
    if not isinstance(value, str):
        raise GitHubReadError(
            f"{subject} response is missing its URL",
            code="target_lookup_invalid",
            exit_code=ExitCode.VALIDATION,
        )
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        parsed = urlsplit("")
        port = None
    parts = tuple(parsed.path.strip("/").split("/"))
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port is not None
        and not 1 <= port <= 65535
    ):
        raise GitHubReadError(
            f"{subject} response contains an invalid URL",
            code="target_lookup_invalid",
            exit_code=ExitCode.VALIDATION,
        )
    return parsed.netloc.lower(), parts


@dataclass(frozen=True, slots=True)
class ProcessTargetLookup:
    process: TargetProcess

    def current_repository(self, host: str | None) -> tuple[str, str]:
        record = _object(
            self.process.repo_view(host),
            subject="current repository lookup",
        )
        repository = record.get("nameWithOwner")
        if not isinstance(repository, str) or not repository:
            raise GitHubReadError(
                "current repository response is missing nameWithOwner",
                code="target_lookup_invalid",
                exit_code=ExitCode.VALIDATION,
            )
        url_host, parts = _url(
            record.get("url"),
            subject="current repository lookup",
        )
        if (
            len(parts) != 2
            or f"{parts[0]}/{parts[1]}".casefold() != repository.casefold()
            or host is not None
            and url_host != host.casefold()
        ):
            raise GitHubReadError(
                "current repository response URL disagrees with its identity",
                code="target_lookup_invalid",
                exit_code=ExitCode.VALIDATION,
            )
        return url_host, repository

    def current_pull_request(
        self,
        host: str,
        repository: str,
    ) -> int:
        record = _object(
            self.process.pr_view(
                repository=repository,
                hostname=host,
            ),
            subject="current Pull Request lookup",
        )
        number = _positive_integer(
            record.get("number"),
            subject="current Pull Request lookup",
        )
        url_host, parts = _url(
            record.get("url"),
            subject="current Pull Request lookup",
        )
        if (
            len(parts) != 4
            or parts[2] != "pull"
            or f"{parts[0]}/{parts[1]}".casefold() != repository.casefold()
            or not parts[3].isdecimal()
            or int(parts[3]) != number
            or url_host != host.casefold()
        ):
            raise GitHubReadError(
                "current Pull Request response URL disagrees with its identity",
                code="target_lookup_invalid",
                exit_code=ExitCode.VALIDATION,
            )
        return number


__all__ = ["ProcessTargetLookup", "TargetProcess"]
