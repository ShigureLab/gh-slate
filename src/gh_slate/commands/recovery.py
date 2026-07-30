from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

from gh_slate.codec import JsonValue, canonical_json_bytes
from gh_slate.github.lookup import ProcessTargetLookup, TargetProcess
from gh_slate.github.process import GhProcess
from gh_slate.github.recovery import (
    DeleteRequest,
    RecoveryResult,
    RepairRequest,
    delete,
    repair,
    validate_delete_confirmation,
)
from gh_slate.github.target import resolve_target
from gh_slate.github.write import GhWriteProcess

if TYPE_CHECKING:
    from argparse import Namespace
    from collections.abc import Mapping


class RecoverySession(Protocol):
    @property
    def reader(self) -> TargetProcess: ...

    def repair(self, request: RepairRequest) -> RecoveryResult: ...

    def delete(self, request: DeleteRequest) -> RecoveryResult: ...


@dataclass(slots=True)
class _CoreSession:
    reader: GhProcess
    writer: GhWriteProcess

    def repair(self, request: RepairRequest) -> RecoveryResult:
        return repair(
            request,
            reader=self.reader,
            writer=self.writer,
        )

    def delete(self, request: DeleteRequest) -> RecoveryResult:
        return delete(
            request,
            reader=self.reader,
            writer=self.writer,
        )


def _new_session() -> RecoverySession:
    return _CoreSession(
        reader=GhProcess(),
        writer=GhWriteProcess(),
    )


def _write_result(
    result: RecoveryResult,
    *,
    as_json: bool,
    quiet: bool,
) -> None:
    if quiet:
        return
    if as_json:
        value = cast("Mapping[str, JsonValue]", result.to_json())
        sys.stdout.write(canonical_json_bytes(value).decode("utf-8") + "\n")
        return
    print(f"{result.action} {result.name} -> {result.url}")


def _resolved_target(
    args: Namespace,
    session: RecoverySession,
):
    return resolve_target(
        args.target,
        repo=args.repo,
        host=args.host,
        lookup=ProcessTargetLookup(session.reader),
        environ=os.environ,
    )


def run_repair(args: Namespace) -> int:
    session = _new_session()
    request = RepairRequest(
        target=_resolved_target(args, session),
        name=args.name,
        from_state=args.from_state,
        controller=args.controller,
        if_revision=args.if_revision,
    )
    result = session.repair(request)
    _write_result(
        result,
        as_json=args.json,
        quiet=args.quiet,
    )
    return 0


def run_delete(args: Namespace) -> int:
    # Validate destructive intent before target resolution can perform a
    # repository or pull-request lookup.
    name = validate_delete_confirmation(
        args.name,
        confirm=args.confirm,
        yes=args.yes,
    )
    session = _new_session()
    request = DeleteRequest(
        target=_resolved_target(args, session),
        name=name,
        confirm=args.confirm,
        yes=args.yes,
        controller=args.controller,
        if_revision=args.if_revision,
    )
    result = session.delete(request)
    _write_result(
        result,
        as_json=args.json,
        quiet=args.quiet,
    )
    return 0


__all__ = [
    "DeleteRequest",
    "RecoveryResult",
    "RecoverySession",
    "RepairRequest",
    "run_delete",
    "run_repair",
]
