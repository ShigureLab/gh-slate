from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
from typing import Protocol, cast

from gh_slate.codec import CodecError, decode_comment
from gh_slate.codec.marker import MANAGED_MARKER_PREFIX, NAME_PATTERN
from gh_slate.errors import ExitCode
from gh_slate.github.errors import GitHubReadError
from gh_slate.github.models import (
    GitHubActor,
    GitHubComment,
    ManagedSlate,
    SlateCandidate,
)
from gh_slate.github.target import target_from_comment_url

_MARKER_NAME = re.compile(rf"\A{re.escape(MANAGED_MARKER_PREFIX)} name=(?P<name>{NAME_PATTERN})(?: |\n)")


class TargetLike(Protocol):
    @property
    def host(self) -> str: ...

    @property
    def repository(self) -> str: ...

    @property
    def number(self) -> int: ...


class ReadClient(Protocol):
    def api_get(
        self,
        endpoint: str,
        *,
        hostname: str | None = None,
        paginate: bool = False,
    ) -> object: ...

    def current_actor(self, hostname: str | None = None) -> GitHubActor: ...

    def resolve_actor(
        self,
        login: str,
        hostname: str | None = None,
    ) -> GitHubActor: ...


def _integer(value: object, *, field: str) -> int:
    if isinstance(value, bool):
        raise GitHubReadError(
            "GitHub returned an invalid comment identifier",
            code="github_response_invalid",
            details={"field": field},
        )
    if isinstance(value, int):
        result = value
    elif isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        result = int(value)
    else:
        raise GitHubReadError(
            "GitHub returned an invalid comment identifier",
            code="github_response_invalid",
            details={"field": field},
        )
    if result <= 0:
        raise GitHubReadError(
            "GitHub returned an invalid comment identifier",
            code="github_response_invalid",
            details={"field": field},
        )
    return result


def _optional_text(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise GitHubReadError(
            "GitHub returned an invalid comment record",
            code="github_response_invalid",
            details={"field": field},
        )
    return value


def _comment(
    value: object,
    *,
    target: TargetLike,
) -> GitHubComment:
    if not isinstance(value, Mapping):
        raise GitHubReadError(
            "GitHub returned a non-object comment record",
            code="github_response_invalid",
        )
    record = cast("Mapping[str, object]", value)
    body = record.get("body")
    url = record.get("html_url")
    if not isinstance(body, str) or not isinstance(url, str):
        raise GitHubReadError(
            "GitHub returned an invalid comment record",
            code="github_response_invalid",
            details={"required_fields": ["body", "html_url"]},
        )
    identifier = _integer(
        record.get("id"),
        field="comments[].id",
    )
    try:
        target_from_comment_url(
            url,
            expected=target,
            expected_comment_id=identifier,
        )
    except GitHubReadError as error:
        raise GitHubReadError(
            "GitHub returned a comment URL outside the resolved target",
            code="github_response_invalid",
            details={
                "field": "comments[].html_url",
                "cause_code": error.code,
            },
        ) from None
    user = record.get("user")
    author: str | None = None
    author_id: int | None = None
    if user is not None:
        if not isinstance(user, Mapping):
            raise GitHubReadError(
                "GitHub returned an invalid comment author",
                code="github_response_invalid",
            )
        user_record = cast("Mapping[str, object]", user)
        login = user_record.get("login")
        if not isinstance(login, str) or not login:
            raise GitHubReadError(
                "GitHub returned an invalid comment author",
                code="github_response_invalid",
            )
        try:
            actor = GitHubActor(
                id=_integer(
                    user_record.get("id"),
                    field="comments[].user.id",
                ),
                login=login,
            )
        except ValueError:
            raise GitHubReadError(
                "GitHub returned an invalid comment author",
                code="github_response_invalid",
            ) from None
        author = actor.login
        author_id = actor.id
    return GitHubComment(
        id=identifier,
        body=body,
        author=author,
        url=url,
        author_id=author_id,
        created_at=_optional_text(
            record.get("created_at"),
            field="comments[].created_at",
        ),
        updated_at=_optional_text(
            record.get("updated_at"),
            field="comments[].updated_at",
        ),
    )


def _flatten_pages(value: object) -> tuple[object, ...]:
    if not isinstance(value, (tuple, list)):
        raise GitHubReadError(
            "GitHub comment pagination returned a non-array response",
            code="github_response_invalid",
        )
    sequence = tuple(value)
    if not sequence:
        return ()
    if all(isinstance(page, (tuple, list)) for page in sequence):
        flattened: list[object] = []
        for page in sequence:
            flattened.extend(cast("tuple[object, ...] | list[object]", page))
        return tuple(flattened)
    return sequence


def _marker_name(body: str) -> str | None:
    match = _MARKER_NAME.match(body)
    return None if match is None else match.group("name")


class CommentStore:
    """Read and classify managed comments without exposing a mutation method."""

    def __init__(self, client: ReadClient) -> None:
        self._client = client

    def controller(
        self,
        target: TargetLike,
        requested: str | GitHubActor | None = None,
    ) -> GitHubActor:
        if isinstance(requested, GitHubActor):
            return requested
        if requested is not None:
            if not isinstance(requested, str) or not requested:
                raise GitHubReadError(
                    "controller login must not be empty",
                    code="controller_invalid",
                    exit_code=ExitCode.VALIDATION,
                )
            return self._client.resolve_actor(requested, target.host)
        return self._client.current_actor(target.host)

    def comments(self, target: TargetLike) -> tuple[GitHubComment, ...]:
        endpoint = f"repos/{target.repository}/issues/{target.number}/comments?per_page=100"
        response = self._client.api_get(
            endpoint,
            hostname=target.host,
            paginate=True,
        )
        return tuple(_comment(item, target=target) for item in _flatten_pages(response))

    def candidates(
        self,
        target: TargetLike,
        *,
        controller: str | GitHubActor | None = None,
        name: str | None = None,
    ) -> tuple[SlateCandidate, ...]:
        selected_controller = self.controller(target, controller)
        candidates: list[SlateCandidate] = []
        for comment in self.comments(target):
            if comment.author_id != selected_controller.id:
                continue
            marker_name = _marker_name(comment.body)
            if marker_name is None or (name is not None and marker_name != name):
                continue
            try:
                decoded = decode_comment(comment.body)
                stored_controller_id = decoded.state.controller.id
                if stored_controller_id != selected_controller.id:
                    raise GitHubReadError(
                        "stored controller does not match the comment author",
                        code="controller_mismatch",
                        exit_code=ExitCode.VALIDATION,
                    )
                candidates.append(
                    SlateCandidate(
                        name=marker_name,
                        comment=comment,
                        status=("drifted" if decoded.drifted else "valid"),
                        decoded=decoded,
                    )
                )
            except (CodecError, GitHubReadError) as error:
                candidates.append(
                    SlateCandidate(
                        name=marker_name,
                        comment=comment,
                        status="corrupt",
                        error_code=error.code,
                    )
                )

        duplicate_names = {candidate_name for candidate_name, count in _counts(candidates).items() if count > 1}
        return tuple(
            replace(candidate, status="duplicate") if candidate.name in duplicate_names else candidate
            for candidate in candidates
        )

    def find(
        self,
        target: TargetLike,
        name: str,
        *,
        controller: str | GitHubActor | None = None,
    ) -> ManagedSlate:
        candidates = self.candidates(
            target,
            controller=controller,
            name=name,
        )
        if not candidates:
            raise GitHubReadError(
                f"slate '{name}' was not found on the target",
                code="slate_not_found",
                exit_code=ExitCode.NOT_FOUND,
                details={"name": name},
            )
        if len(candidates) > 1:
            raise GitHubReadError(
                f"multiple comments match slate '{name}'",
                code="duplicate_slate",
                exit_code=ExitCode.CONFLICT,
                details={
                    "name": name,
                    "comment_ids": [candidate.comment.id for candidate in candidates],
                },
                hints=("inspect the matching comments and remove the duplicate before mutating this slate",),
            )
        candidate = candidates[0]
        if candidate.decoded is None:
            raise GitHubReadError(
                f"slate '{name}' contains corrupt or unsupported state",
                code="slate_corrupt",
                exit_code=ExitCode.VALIDATION,
                details={
                    "name": name,
                    "comment_id": candidate.comment.id,
                    "cause_code": candidate.error_code,
                },
                hints=("inspect the raw comment and delete it with exact confirmation if it cannot be recovered",),
            )
        return ManagedSlate(
            name=name,
            comment=candidate.comment,
            decoded=candidate.decoded,
        )


def _counts(
    candidates: list[SlateCandidate],
) -> dict[str, int]:
    counts: defaultdict[str, int] = defaultdict(int)
    for candidate in candidates:
        counts[candidate.name] += 1
    return dict(counts)


__all__ = ["CommentStore", "ReadClient", "TargetLike"]
