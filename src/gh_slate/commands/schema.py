from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

from gh_slate.codec import (
    SchemaSnapshotV1,
    canonical_json_bytes,
    validate_slate_name,
)
from gh_slate.codec.json import DEFAULT_JSON_LIMITS
from gh_slate.commands.render import _read_bytes
from gh_slate.data import format_json_results
from gh_slate.errors import ExitCode
from gh_slate.github.errors import GitHubReadError
from gh_slate.github.lookup import ProcessTargetLookup
from gh_slate.github.process import GhProcess
from gh_slate.github.store import CommentStore
from gh_slate.github.target import ResolvedTarget, resolve_target
from gh_slate.schema import (
    infer_schema,
    validate_data,
    validate_data_json,
)

if TYPE_CHECKING:
    from argparse import Namespace

    from gh_slate.github.models import ManagedSlate


def _new_process() -> GhProcess:
    return GhProcess()


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
    if slate.decoded.drifted:
        print(
            "warning[render_drift]: inspected canonical state despite visible Markdown drift",
            file=sys.stderr,
        )
    return target, slate


def _stored_schema(
    slate: ManagedSlate,
) -> SchemaSnapshotV1:
    schema = slate.decoded.state.data_schema
    if schema is None:
        raise GitHubReadError(
            f"slate '{slate.name}' has no stored schema",
            code="schema_not_found",
            exit_code=ExitCode.NOT_FOUND,
            details={"name": slate.name},
        )
    return schema


def _write_schema(
    schema: SchemaSnapshotV1,
    *,
    compact: bool,
) -> None:
    sys.stdout.write(
        format_json_results(
            (schema.document,),
            compact=compact,
            raw=False,
        )
    )


def run_schema_get(args: Namespace) -> int:
    _target_value, slate = _read_slate(args)
    _write_schema(
        _stored_schema(slate),
        compact=args.compact_output,
    )
    return 0


def run_schema_infer(args: Namespace) -> int:
    _target_value, slate = _read_slate(args)
    schema = infer_schema(slate.decoded.state.data)
    _write_schema(schema, compact=args.compact_output)
    return 0


def run_schema_validate(args: Namespace) -> int:
    candidate = (
        None
        if args.file is None
        else validate_data_json(
            _read_bytes(
                args.file,
                subject="data",
                max_bytes=DEFAULT_JSON_LIMITS.max_input_bytes,
            )
        )
    )
    _target_value, slate = _read_slate(args)
    schema = _stored_schema(slate)
    validate_data(
        slate.decoded.state.data if candidate is None else candidate,
        schema,
    )
    if args.json:
        sys.stdout.write(
            canonical_json_bytes(
                {
                    "valid": True,
                    "name": slate.name,
                    "revision": slate.decoded.state.revision,
                    "source": "stored" if candidate is None else "candidate",
                }
            ).decode("utf-8")
            + "\n"
        )
    else:
        print(f"valid {slate.name} revision={slate.decoded.state.revision}")
    return 0


__all__ = [
    "run_schema_get",
    "run_schema_infer",
    "run_schema_validate",
]
