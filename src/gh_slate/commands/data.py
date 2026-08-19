from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

from gh_slate.codec import validate_slate_name
from gh_slate.data import format_json_results, json_exit_status
from gh_slate.github.lookup import ProcessTargetLookup
from gh_slate.github.process import GhProcess
from gh_slate.github.store import CommentStore
from gh_slate.github.target import ResolvedTarget, resolve_target
from gh_slate.rendering import evaluate

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
            "warning[render_drift]: queried canonical data despite visible Markdown drift",
            file=sys.stderr,
        )
    return target, slate


def run_data_get(args: Namespace) -> int:
    _target_value, slate = _read_slate(args)
    results = evaluate(
        slate.decoded.state.data,
        args.filter,
    )
    sys.stdout.write(
        format_json_results(
            results,
            compact=args.compact_output,
            raw=args.raw_output,
        )
    )
    return json_exit_status(results) if args.exit_status else 0


__all__ = ["run_data_get"]
