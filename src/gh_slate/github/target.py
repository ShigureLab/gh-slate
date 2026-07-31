from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import SplitResult, urlsplit, urlunsplit

from gh_slate.codec import CodecError
from gh_slate.codec.json import JsonLimits, strict_loads
from gh_slate.errors import ExitCode
from gh_slate.github.errors import GitHubReadError

MAX_EVENT_BYTES = 1024 * 1024
MAX_TARGET_NUMBER = 2**63 - 1
DEFAULT_HOST = "github.com"
_COMMENT_FRAGMENT = re.compile(r"\Aissuecomment-(?P<identifier>[1-9][0-9]*)\Z")


class TargetLookup(Protocol):
    """The two read-only context lookups target resolution may require."""

    def current_repository(self, host: str | None) -> tuple[str, str]: ...

    def current_pull_request(self, host: str, repository: str) -> int: ...


class TargetIdentity(Protocol):
    @property
    def host(self) -> str: ...

    @property
    def repository(self) -> str: ...

    @property
    def number(self) -> int: ...


@dataclass(frozen=True, slots=True)
class _BaseUrl:
    scheme: str
    host: str

    @property
    def origin(self) -> str:
        return f"{self.scheme}://{self.host}"


@dataclass(frozen=True, slots=True)
class _ParsedTargetUrl:
    base: _BaseUrl
    repository: str
    number: int
    kind: str

    @property
    def url(self) -> str:
        return f"{self.base.origin}/{self.repository}/{self.kind}/{self.number}"


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    host: str
    repository: str
    number: int
    url: str

    def __post_init__(self) -> None:
        host = _host(self.host, field="target.host")
        repository = _repository(
            self.repository,
            field="target.repository",
        )
        number = _positive_integer(
            self.number,
            field="target.number",
        )
        parsed = _target_url(self.url)
        if parsed.base.host != host or not _same_repository(parsed.repository, repository) or parsed.number != number:
            raise _conflict(
                "resolved target fields do not describe the same target",
                fields=["host", "repository", "number", "url"],
            )
        object.__setattr__(self, "host", host)
        object.__setattr__(self, "repository", repository)
        object.__setattr__(self, "number", number)
        object.__setattr__(self, "url", parsed.url)


def _error(
    message: str,
    *,
    code: str = "target_invalid",
    details: Mapping[str, object] | None = None,
    hints: tuple[str, ...] = (),
) -> GitHubReadError:
    return GitHubReadError(
        message,
        code=code,
        exit_code=ExitCode.VALIDATION,
        details=details,
        hints=hints,
    )


def _conflict(message: str, **details: object) -> GitHubReadError:
    return GitHubReadError(
        message,
        code="target_conflict",
        exit_code=ExitCode.CONFLICT,
        details=details,
    )


def _positive_integer(value: object, *, field: str) -> int:
    if isinstance(value, bool):
        raise _error(
            f"{field} must be a positive integer",
            details={"field": field},
        )
    if isinstance(value, int):
        result = value
    elif isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        result = int(value)
    else:
        raise _error(
            f"{field} must be a positive integer",
            details={"field": field},
        )
    if not 1 <= result <= MAX_TARGET_NUMBER:
        raise _error(
            f"{field} must be between 1 and {MAX_TARGET_NUMBER}",
            details={"field": field},
        )
    return result


def _positive_decimal_text(
    value: object,
    *,
    field: str,
    code: str = "target_invalid",
) -> int:
    if not isinstance(value, str) or not value or not value.isascii() or not value.isdecimal():
        raise _error(
            f"{field} must be a positive integer",
            code=code,
            details={"field": field},
        )
    significant = value.lstrip("0")
    maximum = str(MAX_TARGET_NUMBER)
    if (
        not significant
        or len(significant) > len(maximum)
        or (len(significant) == len(maximum) and significant > maximum)
    ):
        raise _error(
            f"{field} must be between 1 and {MAX_TARGET_NUMBER}",
            code=code,
            details={"field": field},
        )
    return int(significant)


def _repository(value: object, *, field: str) -> str:
    if not isinstance(value, str) or value != value.strip() or value.count("/") != 1:
        raise _error(
            f"{field} must be in OWNER/REPO form",
            details={"field": field},
        )
    owner, name = value.split("/", 1)
    if (
        not owner
        or not name
        or owner in {".", ".."}
        or name in {".", ".."}
        or any(character.isspace() or ord(character) < 0x20 or character in "?#" for character in value)
    ):
        raise _error(
            f"{field} must be in OWNER/REPO form",
            details={"field": field},
        )
    return value


def _same_repository(left: str, right: str) -> bool:
    return left.casefold() == right.casefold()


def _split_url(value: str, *, field: str) -> SplitResult:
    if not value or value != value.strip():
        raise _error(
            f"{field} must be an absolute HTTP(S) URL",
            details={"field": field},
        )
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise _error(
            f"{field} must be an absolute HTTP(S) URL",
            details={"field": field},
        ) from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise _error(
            f"{field} must be an absolute HTTP(S) URL",
            details={"field": field},
        )
    return parsed


def _url_base(parsed: SplitResult) -> _BaseUrl:
    return _BaseUrl(
        scheme=parsed.scheme.lower(),
        host=parsed.netloc.lower(),
    )


def _host(value: object, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "://" in value
        or any(character in value for character in "/?#@")
    ):
        raise _error(
            f"{field} must be a bare GitHub hostname",
            details={"field": field},
        )
    try:
        parsed = urlsplit(f"//{value}")
        port = parsed.port
    except ValueError:
        raise _error(
            f"{field} must be a bare GitHub hostname",
            details={"field": field},
        ) from None
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise _error(
            f"{field} must be a bare GitHub hostname",
            details={"field": field},
        )
    return parsed.netloc.lower()


def _base_from_host(value: object, *, field: str) -> _BaseUrl:
    return _BaseUrl(scheme="https", host=_host(value, field=field))


def _server_base(value: object, *, field: str) -> _BaseUrl:
    if not isinstance(value, str):
        raise _error(
            f"{field} must be an absolute HTTP(S) URL",
            details={"field": field},
        )
    parsed = _split_url(value, field=field)
    if parsed.path not in {"", "/"}:
        raise _error(
            f"{field} must not contain a path",
            details={"field": field},
        )
    return _url_base(parsed)


def _base_hint(
    explicit_host: str | None,
    environ: Mapping[str, str],
) -> _BaseUrl | None:
    if explicit_host is not None:
        return _base_from_host(explicit_host, field="host")
    gh_host = environ.get("GH_HOST")
    if gh_host:
        return _base_from_host(gh_host, field="GH_HOST")
    server_url = environ.get("GITHUB_SERVER_URL")
    if server_url:
        return _server_base(server_url, field="GITHUB_SERVER_URL")
    return None


def _default_base(
    explicit_host: str | None,
    environ: Mapping[str, str],
) -> _BaseUrl:
    return _base_hint(explicit_host, environ) or _BaseUrl(scheme="https", host=DEFAULT_HOST)


def _target_url(value: object) -> _ParsedTargetUrl:
    if not isinstance(value, str):
        raise _error("target URL must be a string")
    parsed = _split_url(value, field="target")
    parts = parsed.path.strip("/").split("/")
    if len(parts) != 4 or parts[2] not in {"issues", "pull"}:
        raise _error(
            "target URL must identify one GitHub Issue or Pull Request",
            details={"field": "target"},
        )
    repository = _repository(
        f"{parts[0]}/{parts[1]}",
        field="target.repository",
    )
    number = _positive_decimal_text(
        parts[3],
        field="target.number",
    )
    return _ParsedTargetUrl(
        base=_url_base(parsed),
        repository=repository,
        number=number,
        kind=parts[2],
    )


def target_from_comment_url(
    value: str,
    *,
    expected: TargetIdentity | None = None,
    expected_comment_id: int | None = None,
) -> ResolvedTarget:
    """Recover canonical target context from a GitHub comment HTML URL."""

    if not isinstance(value, str) or not value or value != value.strip():
        raise _error(
            "comment URL must be an absolute GitHub comment URL",
            code="comment_url_invalid",
        )
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise _error(
            "comment URL must be an absolute GitHub comment URL",
            code="comment_url_invalid",
        ) from None
    fragment = _COMMENT_FRAGMENT.fullmatch(parsed.fragment)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or port is not None
        and not 1 <= port <= 65535
        or fragment is None
    ):
        raise _error(
            "comment URL must be an absolute GitHub comment URL",
            code="comment_url_invalid",
        )
    comment_id = _positive_decimal_text(
        fragment.group("identifier"),
        field="comment.id",
        code="comment_url_invalid",
    )
    if expected_comment_id is not None and comment_id != _positive_integer(
        expected_comment_id,
        field="comment.id",
    ):
        raise _conflict(
            "comment URL fragment disagrees with the comment identifier",
            comment_url=value,
            expected_comment_id=expected_comment_id,
            actual_comment_id=comment_id,
        )
    page_url = urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            "",
            "",
        )
    )
    resolved = _resolved(_target_url(page_url))
    if expected is not None and (
        resolved.host != expected.host
        or not _same_repository(
            resolved.repository,
            expected.repository,
        )
        or resolved.number != expected.number
    ):
        raise _conflict(
            "comment URL does not belong to the resolved target",
            comment_url=value,
            target_host=expected.host,
            target_repository=expected.repository,
            target_number=expected.number,
        )
    return resolved


def _repository_url(value: object, *, field: str) -> tuple[_BaseUrl, str]:
    if not isinstance(value, str):
        raise _error(
            f"{field} must be a repository URL",
            details={"field": field},
        )
    parsed = _split_url(value, field=field)
    parts = parsed.path.strip("/").split("/")
    if len(parts) != 2:
        raise _error(
            f"{field} must be a repository URL",
            details={"field": field},
        )
    return (
        _url_base(parsed),
        _repository(f"{parts[0]}/{parts[1]}", field=field),
    )


def _resolved(parsed: _ParsedTargetUrl) -> ResolvedTarget:
    return ResolvedTarget(
        host=parsed.base.host,
        repository=parsed.repository,
        number=parsed.number,
        url=parsed.url,
    )


def _build_target(
    *,
    base: _BaseUrl,
    repository: str,
    number: int,
    kind: str,
) -> ResolvedTarget:
    return _resolved(
        _ParsedTargetUrl(
            base=base,
            repository=repository,
            number=number,
            kind=kind,
        )
    )


def _check_requested_context(
    parsed: _ParsedTargetUrl,
    *,
    requested_repo: str | None,
    requested_host: str | None,
) -> None:
    if requested_repo is not None and not _same_repository(parsed.repository, requested_repo):
        raise _conflict(
            "target URL and requested repository disagree",
            target_repository=parsed.repository,
            requested_repository=requested_repo,
        )
    if requested_host is not None and parsed.base.host != _host(requested_host, field="host"):
        raise _conflict(
            "target URL and requested host disagree",
            target_host=parsed.base.host,
            requested_host=requested_host,
        )


def _lookup_repository(
    lookup: TargetLookup | None,
    *,
    base_hint: _BaseUrl | None,
) -> tuple[_BaseUrl, str]:
    if lookup is None:
        raise _error(
            "the current repository is required to resolve this target",
            code="target_context_unavailable",
            hints=("pass --repo OWNER/REPO or use a full Issue/PR URL",),
        )
    result = lookup.current_repository(None if base_hint is None else base_hint.host)
    if not isinstance(result, tuple) or len(result) != 2 or not all(isinstance(item, str) for item in result):
        raise _error(
            "current repository lookup returned an invalid result",
            code="target_lookup_invalid",
        )
    lookup_host, lookup_repo = result
    resolved_base = _base_from_host(
        lookup_host,
        field="lookup.host",
    )
    if base_hint is not None:
        if base_hint.host != resolved_base.host:
            raise _conflict(
                "current repository and requested host disagree",
                current_host=resolved_base.host,
                requested_host=base_hint.host,
            )
        resolved_base = base_hint
    return (
        resolved_base,
        _repository(lookup_repo, field="lookup.repository"),
    )


def _read_event(environ: Mapping[str, str]) -> Mapping[str, object]:
    event_path = environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        raise _error(
            "GITHUB_EVENT_PATH is required to resolve @event",
            code="target_event_unavailable",
        )
    path = Path(event_path)
    try:
        with path.open("rb") as event_file:
            source = event_file.read(MAX_EVENT_BYTES + 1)
    except (OSError, ValueError) as error:
        raise _error(
            "GitHub event payload could not be read",
            code="target_event_unavailable",
            details={"os_error": type(error).__name__},
        ) from None
    if len(source) > MAX_EVENT_BYTES:
        raise _error(
            "GitHub event payload exceeds the byte limit",
            code="target_event_limit",
            details={
                "actual_bytes": len(source),
                "max_bytes": MAX_EVENT_BYTES,
            },
        )
    try:
        payload = strict_loads(
            source,
            limits=JsonLimits(max_input_bytes=MAX_EVENT_BYTES),
        )
    except CodecError as error:
        raise _error(
            "GitHub event payload is not strict JSON",
            code="target_event_invalid",
            details={"cause": error.code},
        ) from None
    if not isinstance(payload, Mapping):
        raise _error(
            "GitHub event payload root must be an object",
            code="target_event_invalid",
        )
    return cast("Mapping[str, object]", payload)


def _event_target(
    *,
    requested_repo: str | None,
    requested_host: str | None,
    environ: Mapping[str, str],
) -> ResolvedTarget:
    payload = _read_event(environ)
    pull_request = payload.get("pull_request")
    issue = payload.get("issue")
    if pull_request is not None:
        if not isinstance(pull_request, Mapping):
            raise _error(
                "event.pull_request must be an object",
                code="target_event_invalid",
            )
        record = cast("Mapping[str, object]", pull_request)
        default_kind = "pull"
    elif issue is not None:
        if not isinstance(issue, Mapping):
            raise _error(
                "event.issue must be an object",
                code="target_event_invalid",
            )
        record = cast("Mapping[str, object]", issue)
        if "pull_request" in record:
            marker = record["pull_request"]
            if not isinstance(marker, Mapping):
                raise _error(
                    "event.issue.pull_request must be an object",
                    code="target_event_invalid",
                )
            default_kind = "pull"
        else:
            default_kind = "issues"
    else:
        raise _error(
            "GitHub event does not contain an Issue or Pull Request",
            code="target_event_unsupported",
        )

    number = _positive_integer(
        record.get("number", payload.get("number")),
        field="event.number",
    )
    target_html_url = record.get("html_url")
    parsed_target = _target_url(target_html_url) if target_html_url is not None else None
    if parsed_target is not None:
        if parsed_target.kind != default_kind:
            raise _conflict(
                "event target kind and URL disagree",
                event_kind=default_kind,
                url_kind=parsed_target.kind,
            )
        if parsed_target.number != number:
            raise _conflict(
                "event target URL and number disagree",
                url_number=parsed_target.number,
                event_number=number,
            )
        _check_requested_context(
            parsed_target,
            requested_repo=requested_repo,
            requested_host=requested_host,
        )

    repository_record = payload.get("repository")
    event_repository: str | None = None
    repository_base: _BaseUrl | None = None
    if repository_record is not None:
        if not isinstance(repository_record, Mapping):
            raise _error(
                "event.repository must be an object",
                code="target_event_invalid",
            )
        typed_repository = cast(
            "Mapping[str, object]",
            repository_record,
        )
        full_name = typed_repository.get("full_name")
        if full_name is not None:
            event_repository = _repository(
                full_name,
                field="event.repository.full_name",
            )
        html_url = typed_repository.get("html_url")
        if html_url is not None:
            repository_base, html_repository = _repository_url(
                html_url,
                field="event.repository.html_url",
            )
            if event_repository is not None and not _same_repository(
                event_repository,
                html_repository,
            ):
                raise _conflict(
                    "event repository name and URL disagree",
                    repository=event_repository,
                    url_repository=html_repository,
                )
            event_repository = html_repository

    environment_repository = environ.get("GITHUB_REPOSITORY")
    if environment_repository:
        environment_repository = _repository(
            environment_repository,
            field="GITHUB_REPOSITORY",
        )
        if event_repository is not None and not _same_repository(
            event_repository,
            environment_repository,
        ):
            raise _conflict(
                "event payload and GITHUB_REPOSITORY disagree",
                event_repository=event_repository,
                environment_repository=environment_repository,
            )
        event_repository = environment_repository

    if parsed_target is not None:
        if event_repository is not None and not _same_repository(
            parsed_target.repository,
            event_repository,
        ):
            raise _conflict(
                "event target URL and repository disagree",
                target_repository=parsed_target.repository,
                event_repository=event_repository,
            )
        if repository_base is not None and repository_base.host != parsed_target.base.host:
            raise _conflict(
                "event target and repository hosts disagree",
                target_host=parsed_target.base.host,
                repository_host=repository_base.host,
            )
        server_url = environ.get("GITHUB_SERVER_URL")
        if server_url:
            server_base = _server_base(
                server_url,
                field="GITHUB_SERVER_URL",
            )
            if server_base.host != parsed_target.base.host:
                raise _conflict(
                    "event target and GITHUB_SERVER_URL hosts disagree",
                    target_host=parsed_target.base.host,
                    server_host=server_base.host,
                )
        return _resolved(parsed_target)

    repository = requested_repo or event_repository
    if repository is None:
        raise _error(
            "GitHub event does not identify a repository",
            code="target_event_invalid",
        )
    repository = _repository(repository, field="event.repository")
    if (
        requested_repo is not None
        and event_repository is not None
        and not _same_repository(requested_repo, event_repository)
    ):
        raise _conflict(
            "event payload and requested repository disagree",
            event_repository=event_repository,
            requested_repository=requested_repo,
        )

    base_hint = _base_hint(requested_host, environ)
    base = base_hint or _BaseUrl(scheme="https", host=DEFAULT_HOST)
    if repository_base is not None:
        if base_hint is not None and repository_base.host != base_hint.host:
            raise _conflict(
                "event repository URL and host context disagree",
                event_host=repository_base.host,
                requested_host=base_hint.host,
            )
        base = repository_base
    return _build_target(
        base=base,
        repository=repository,
        number=number,
        kind=default_kind,
    )


def resolve_target(
    target: str | int | None,
    *,
    repo: str | None = None,
    repository: str | None = None,
    host: str | None = None,
    lookup: TargetLookup | None = None,
    environ: Mapping[str, str] | None = None,
) -> ResolvedTarget:
    """Resolve one Issue/PR target without performing a write request."""

    environment = os.environ if environ is None else environ
    if repo is not None:
        repo = _repository(repo, field="repo")
    if repository is not None:
        repository = _repository(
            repository,
            field="repository",
        )
    if repo is not None and repository is not None and not _same_repository(repo, repository):
        raise _conflict(
            "repo and repository arguments disagree",
            repo=repo,
            repository=repository,
        )
    requested_repo = repo or repository
    if target is None:
        if environment.get("GITHUB_ACTIONS", "").casefold() == "true":
            target = "@event"
        else:
            raise _error(
                "target is required outside GitHub Actions",
                code="target_required",
                hints=("pass an Issue/PR number or URL, @event, or @pr",),
            )

    if isinstance(target, bool):
        raise _error("target must identify an Issue or Pull Request")
    if isinstance(target, int):
        target_number = _positive_integer(
            target,
            field="target",
        )
    elif isinstance(target, str) and target.isdecimal():
        target_number = _positive_integer(
            int(target),
            field="target",
        )
    else:
        target_number = None

    if target_number is not None:
        base_hint = _base_hint(host, environment)
        if requested_repo is None:
            base, requested_repo = _lookup_repository(
                lookup,
                base_hint=base_hint,
            )
        else:
            base = base_hint or _BaseUrl(
                scheme="https",
                host=DEFAULT_HOST,
            )
        return _build_target(
            base=base,
            repository=requested_repo,
            number=target_number,
            kind="issues",
        )

    if not isinstance(target, str):
        raise _error("target must identify an Issue or Pull Request")
    if target == "@event":
        return _event_target(
            requested_repo=requested_repo,
            requested_host=host,
            environ=environment,
        )
    if target == "@pr":
        base_hint = _base_hint(host, environment)
        if requested_repo is None:
            base, requested_repo = _lookup_repository(
                lookup,
                base_hint=base_hint,
            )
        else:
            base = base_hint or _BaseUrl(
                scheme="https",
                host=DEFAULT_HOST,
            )
        if lookup is None:
            raise _error(
                "@pr requires current Pull Request context",
                code="target_context_unavailable",
            )
        number = _positive_integer(
            lookup.current_pull_request(
                base.host,
                requested_repo,
            ),
            field="lookup.pull_request.number",
        )
        return _build_target(
            base=base,
            repository=requested_repo,
            number=number,
            kind="pull",
        )
    if "://" in target:
        parsed = _target_url(target)
        _check_requested_context(
            parsed,
            requested_repo=requested_repo,
            requested_host=host,
        )
        return _resolved(parsed)
    raise _error(
        "target must be a positive number, Issue/PR URL, @event, or @pr",
        details={"target": target},
    )


def resolve_host_context(
    host: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve an explicit or environment-provided host without guessing."""

    environment = os.environ if environ is None else environ
    hint = _base_hint(host, environment)
    return None if hint is None else hint.host


__all__ = [
    "DEFAULT_HOST",
    "MAX_EVENT_BYTES",
    "MAX_TARGET_NUMBER",
    "ResolvedTarget",
    "TargetIdentity",
    "TargetLookup",
    "resolve_host_context",
    "resolve_target",
    "target_from_comment_url",
]
