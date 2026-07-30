from __future__ import annotations

import os
import sys
import webbrowser
from collections import defaultdict
from typing import TYPE_CHECKING

from gh_slate.codec import canonical_json_bytes, validate_slate_name
from gh_slate.errors import ExitCode
from gh_slate.github.errors import GitHubReadError
from gh_slate.github.lookup import ProcessTargetLookup
from gh_slate.github.process import GhProcess
from gh_slate.github.store import CommentStore
from gh_slate.github.target import (
    ResolvedTarget,
    resolve_host_context,
    resolve_target,
    target_from_comment_url,
)
from gh_slate.rendering import SlateContext, render_state, select_one

if TYPE_CHECKING:
    from argparse import Namespace

    from gh_slate.github.models import ManagedSlate, SlateCandidate


def _new_process() -> GhProcess:
    return GhProcess()


def _write_json(value: object) -> None:
    sys.stdout.write(canonical_json_bytes(value).decode("utf-8") + "\n")


def _warning(code: str, message: str) -> None:
    print(f"warning[{code}]: {message}", file=sys.stderr)


def _target(
    args: Namespace,
    process: GhProcess,
) -> ResolvedTarget:
    return resolve_target(
        args.target,
        repo=args.repo,
        host=args.host,
        lookup=ProcessTargetLookup(process),
        environ=os.environ,
    )


def _read_slate(
    args: Namespace,
) -> tuple[ResolvedTarget, ManagedSlate]:
    name = validate_slate_name(args.name)
    process = _new_process()
    target = _target(args, process)
    slate = CommentStore(process).find(
        target,
        name,
        controller=args.controller,
    )
    return target, slate


def _context(
    target: ResolvedTarget,
    slate: ManagedSlate,
) -> SlateContext:
    canonical = target_from_comment_url(
        slate.comment.url,
        expected=target,
    )
    return SlateContext(
        name=slate.name,
        repository=canonical.repository,
        number=canonical.number,
        url=canonical.url,
    )


def _view_record(
    target: ResolvedTarget,
    slate: ManagedSlate,
) -> dict[str, object]:
    state = slate.decoded.state
    return {
        "name": slate.name,
        "host": target.host,
        "repository": target.repository,
        "number": target.number,
        "comment_id": slate.comment.id,
        "url": slate.comment.url,
        "controller": state.controller.login,
        "revision": state.revision,
        "status": slate.status,
        "state_sha256": slate.decoded.state_sha256,
        "render_sha256": slate.decoded.expected_render_sha256,
        "actual_render_sha256": slate.decoded.actual_render_sha256,
        "schema": state.data_schema is not None,
        "renderer": {
            "kind": state.renderer.kind,
            "version": state.renderer.version,
        },
    }


def run_view(args: Namespace) -> int:
    target, slate = _read_slate(args)
    if args.web:
        if not webbrowser.open(slate.comment.url):
            raise GitHubReadError(
                "the managed comment URL could not be opened",
                code="browser_open_failed",
            )
        print(f"opened {slate.comment.url}")
        return 0
    if args.json:
        _write_json(_view_record(target, slate))
    else:
        if slate.decoded.drifted:
            _warning(
                "render_drift",
                "visible Markdown differs from the stored render hash",
            )
        sys.stdout.write(slate.decoded.visible_markdown)
    return 0


def _candidate_record(
    candidate: SlateCandidate,
    *,
    intrinsic_status: bool = False,
) -> dict[str, object]:
    status = candidate.status
    if intrinsic_status and status == "duplicate":
        status = "corrupt" if candidate.decoded is None else "drifted" if candidate.decoded.drifted else "valid"
    value: dict[str, object] = {
        "name": candidate.name,
        "status": status,
        "comment_id": candidate.comment.id,
        "url": candidate.comment.url,
        "controller": candidate.comment.author,
    }
    if candidate.decoded is not None:
        value.update(
            {
                "revision": candidate.decoded.state.revision,
                "state_sha256": candidate.decoded.state_sha256,
                "render_sha256": (candidate.decoded.expected_render_sha256),
                "actual_render_sha256": (candidate.decoded.actual_render_sha256),
            }
        )
    if candidate.error_code is not None:
        value["error_code"] = candidate.error_code
    return value


def _group_candidates(
    candidates: tuple[SlateCandidate, ...],
) -> list[dict[str, object]]:
    grouped: defaultdict[str, list[SlateCandidate]] = defaultdict(list)
    for candidate in candidates:
        grouped[candidate.name].append(candidate)

    records: list[dict[str, object]] = []
    for name in sorted(grouped):
        group = grouped[name]
        if len(group) == 1:
            records.append(_candidate_record(group[0]))
            continue
        records.append(
            {
                "name": name,
                "status": "duplicate",
                "comment_ids": [candidate.comment.id for candidate in group],
                "urls": [candidate.comment.url for candidate in group],
                "matches": [
                    _candidate_record(
                        candidate,
                        intrinsic_status=True,
                    )
                    for candidate in group
                ],
            }
        )
    return records


def run_list(args: Namespace) -> int:
    process = _new_process()
    target = _target(args, process)
    records = _group_candidates(
        CommentStore(process).candidates(
            target,
            controller=args.controller,
        )
    )
    if args.json:
        _write_json(records)
    else:
        for record in records:
            suffix = f"revision={record['revision']} " if "revision" in record else ""
            if "error_code" in record:
                suffix += f"error={record['error_code']} "
            locator = record.get("url", record.get("comment_ids", ""))
            print(f"{record['name']}\t{record['status']}\t{suffix}{locator}")
    return 0


def _verified_render(
    target: ResolvedTarget,
    slate: ManagedSlate,
) -> str:
    rendered = render_state(
        slate.decoded.state,
        slate=_context(target, slate),
    )
    if rendered.render_sha256 != slate.decoded.expected_render_sha256:
        raise GitHubReadError(
            "stored state no longer renders to its recorded hash",
            code="rerender_mismatch",
            exit_code=ExitCode.VALIDATION,
            details={
                "expected": slate.decoded.expected_render_sha256,
                "actual": rendered.render_sha256,
            },
        )
    return rendered.markdown


def run_remote_render(args: Namespace) -> int:
    target, slate = _read_slate(args)
    markdown = _verified_render(target, slate)
    if slate.decoded.drifted:
        _warning(
            "render_drift",
            "rendered canonical state; the stored visible Markdown is drifted",
        )
    sys.stdout.write(markdown)
    return 0


def run_state_export(args: Namespace) -> int:
    _target_value, slate = _read_slate(args)
    if slate.decoded.drifted:
        _warning(
            "render_drift",
            "exported canonical state despite visible Markdown drift",
        )
    _write_json(slate.decoded.state.to_json())
    return 0


def run_state_verify(args: Namespace) -> int:
    target, slate = _read_slate(args)
    _verified_render(target, slate)
    record = {
        **_view_record(target, slate),
        "verified": not slate.decoded.drifted,
    }
    if args.json:
        _write_json(record)
    elif slate.decoded.drifted:
        raise GitHubReadError(
            "visible Markdown differs from the stored render hash",
            code="render_drift",
            exit_code=ExitCode.CONFLICT,
            details={
                "expected": slate.decoded.expected_render_sha256,
                "actual": slate.decoded.actual_render_sha256,
            },
            hints=("use repair --from-state to restore the projection, or edit canonical typed data instead",),
        )
    else:
        print(f"verified {slate.name} revision={slate.decoded.state.revision} state={slate.decoded.state_sha256}")
    return 0 if not slate.decoded.drifted else int(ExitCode.CONFLICT)


def run_doctor(args: Namespace) -> int:
    process = _new_process()
    host = resolve_host_context(args.host, environ=os.environ)
    version = process.version()
    process.auth_status(host)
    actor = process.current_actor(host)
    # Exercise the installed native jq binding through the same isolated
    # adapter used by renderers, not a separate jq executable.
    select_one({"ready": True}, ".ready")
    record = {
        "ok": True,
        "gh": version,
        "host": host,
        "actor": actor,
        "jq": "ok",
        "jinja": "ok",
        "json_schema": "draft-2020-12",
    }
    if args.json:
        _write_json(record)
    else:
        print(f"ok gh: {version}")
        print(f"ok auth: {actor}")
        print("ok jq, Jinja, JSON Schema draft 2020-12")
    return 0


__all__ = [
    "run_doctor",
    "run_list",
    "run_remote_render",
    "run_state_export",
    "run_state_verify",
    "run_view",
]
