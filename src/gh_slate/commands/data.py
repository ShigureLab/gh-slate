from __future__ import annotations

import math
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol, cast

from gh_slate.codec import validate_slate_name
from gh_slate.commands.apply import _write_result
from gh_slate.data import (
    DataError,
    delete_paths,
    edit_json,
    format_json_results,
    json_exit_status,
    load_value,
    merge_jq_arguments,
    resolve_exact_path,
    set_path,
)
from gh_slate.github.apply import ApplyResult, ApplyTransaction
from gh_slate.github.lookup import ProcessTargetLookup
from gh_slate.github.mutation import (
    MutationDraft,
    MutationRequest,
    MutationSnapshot,
    MutationTransaction,
    MutationTransform,
)
from gh_slate.github.process import GhProcess
from gh_slate.github.store import CommentStore
from gh_slate.github.target import ResolvedTarget, resolve_target
from gh_slate.github.write import GhWriteProcess
from gh_slate.rendering import (
    DEFAULT_JQ_LIMITS,
    RenderingError,
    evaluate,
)
from gh_slate.schema import validate_data

if TYPE_CHECKING:
    from argparse import Namespace

    from gh_slate.codec import JsonValue
    from gh_slate.errors import GhSlateError
    from gh_slate.github.models import ManagedSlate


class DataMutationSession(Protocol):
    @property
    def reader(self) -> GhProcess: ...

    def mutate(self, request: MutationRequest) -> ApplyResult: ...


_JQ_SAFE_INTEGER_MAX = 2**53 - 1
_JQ_NUMBER_LITERAL = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")


@dataclass(slots=True)
class _CoreMutationSession:
    reader: GhProcess
    transaction: MutationTransaction

    def mutate(self, request: MutationRequest) -> ApplyResult:
        return self.transaction.mutate(request)


def _new_process() -> GhProcess:
    return GhProcess()


def _new_mutation_session() -> DataMutationSession:
    reader = GhProcess()
    return _CoreMutationSession(
        reader=reader,
        transaction=MutationTransaction(
            reader=reader,
            applier=ApplyTransaction(
                reader=reader,
                writer=GhWriteProcess(),
            ),
        ),
    )


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


def _mutation_request(
    args: Namespace,
    session: DataMutationSession,
    transform: MutationTransform,
) -> MutationRequest:
    name = validate_slate_name(args.name)
    target = _target(args, session.reader)
    return MutationRequest(
        target=target,
        name=name,
        transform=transform,
        controller=args.controller,
        if_revision=args.if_revision,
    )


def _write_mutation_result(
    args: Namespace,
    result: ApplyResult,
) -> None:
    _write_result(
        result,
        dry_run=False,
        as_json=args.json,
        quiet=args.quiet,
    )


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


def run_data_set(args: Namespace) -> int:
    value = load_value(
        value=args.value,
        value_string=args.value_string,
        value_file=args.value_file,
    )
    session = _new_mutation_session()

    def transform(snapshot: MutationSnapshot) -> MutationDraft:
        path = resolve_exact_path(
            snapshot.data,
            args.path,
            evaluator=evaluate,
        )
        return MutationDraft(
            set_path(
                cast("JsonValue", snapshot.data),
                path,
                value,
            ),
        )

    result = session.mutate(_mutation_request(args, session, transform))
    _write_mutation_result(args, result)
    return 0


def run_data_delete(args: Namespace) -> int:
    session = _new_mutation_session()

    def transform(snapshot: MutationSnapshot) -> MutationDraft:
        paths = tuple(
            resolve_exact_path(
                snapshot.data,
                expression,
                evaluator=evaluate,
            )
            for expression in args.paths
        )
        return MutationDraft(
            delete_paths(
                cast("JsonValue", snapshot.data),
                paths,
                ignore_missing=args.ignore_missing,
            )
        )

    result = session.mutate(_mutation_request(args, session, transform))
    _write_mutation_result(args, result)
    return 0


def _arguments(args: Namespace) -> Mapping[str, JsonValue]:
    return merge_jq_arguments(
        args.arg,
        args.argjson,
        max_bytes=DEFAULT_JQ_LIMITS.max_source_bytes,
    )


def _unsafe_jq_integer_path(
    value: JsonValue,
    path: tuple[str | int, ...] = (),
) -> tuple[str | int, ...] | None:
    if isinstance(value, Decimal):
        if value == value.to_integral_value() and abs(value) > _JQ_SAFE_INTEGER_MAX:
            return path
        return None
    if isinstance(value, Mapping):
        value_object = cast("Mapping[str, JsonValue]", value)
        for key, item in value_object.items():
            unsafe = _unsafe_jq_integer_path(
                item,
                (*path, key),
            )
            if unsafe is not None:
                return unsafe
        return None
    if isinstance(value, tuple):
        for index, item in enumerate(value):
            unsafe = _unsafe_jq_integer_path(
                item,
                (*path, index),
            )
            if unsafe is not None:
                return unsafe
    return None


def _require_jq_safe_integers(
    value: JsonValue,
    *,
    phase: str,
) -> None:
    path = _unsafe_jq_integer_path(value)
    if path is None:
        return
    raise DataError(
        (
            "jq update cannot safely preserve an integer outside the "
            "IEEE-754 exact range; use data set/delete or store it as a string"
        ),
        code="data_update_unsafe_integer",
        details={
            "phase": phase,
            "path": list(path),
            "maximum_exact_integer": _JQ_SAFE_INTEGER_MAX,
        },
    )


def _require_jq_identity_roundtrip(
    value: JsonValue,
    *,
    phase: str,
) -> None:
    results = evaluate(
        value,
        ".",
        max_results=1,
    )
    if len(results) == 1 and results[0] == value:
        return
    raise DataError(
        (
            "jq update would change numeric precision before the filter runs; "
            "use data set/delete or store precision-sensitive numbers as strings"
        ),
        code="data_update_precision_loss",
        details={"phase": phase},
    )


def _jq_number_literals(filter_text: str) -> tuple[tuple[str, int], ...]:
    literals: list[tuple[str, int]] = []

    def scan_string(index: int) -> int:
        while index < len(filter_text):
            char = filter_text[index]
            if char == '"':
                return index + 1
            if char != "\\":
                index += 1
                continue
            if index + 1 >= len(filter_text):
                return len(filter_text)
            if filter_text[index + 1] == "(":
                index = scan_code(index + 2, interpolation=True)
            else:
                index += 2
        return index

    def scan_code(index: int, *, interpolation: bool) -> int:
        parentheses = 0
        while index < len(filter_text):
            char = filter_text[index]
            if char == "#":
                newline = filter_text.find("\n", index + 1)
                if newline < 0:
                    return len(filter_text)
                index = newline + 1
                continue
            if char == '"':
                index = scan_string(index + 1)
                continue
            if char == "(":
                parentheses += 1
                index += 1
                continue
            if char == ")" and interpolation:
                if parentheses == 0:
                    return index + 1
                parentheses -= 1
                index += 1
                continue
            if char == "$" or char == "_" or char.isalpha():
                end = index + 1
                while end < len(filter_text) and (filter_text[end] == "_" or filter_text[end].isalnum()):
                    end += 1
                index = end
                continue
            if char.isdigit() or (char == "-" and index + 1 < len(filter_text) and filter_text[index + 1].isdigit()):
                matched = _JQ_NUMBER_LITERAL.match(filter_text, index)
                if matched is not None:
                    literals.append((matched.group(), index))
                    index = matched.end()
                    continue
            index += 1
        return index

    scan_code(0, interpolation=False)
    return tuple(literals)


def _require_jq_filter_number_roundtrips(filter_text: str) -> None:
    for literal, position in _jq_number_literals(filter_text):
        exact = Decimal(literal)
        if exact == exact.to_integral_value() and abs(exact) > _JQ_SAFE_INTEGER_MAX:
            raise DataError(
                ("jq update filter contains an integer outside the IEEE-754 exact range"),
                code="data_update_unsafe_integer",
                details={
                    "phase": "filter",
                    "position": position,
                    "literal": literal,
                    "maximum_exact_integer": _JQ_SAFE_INTEGER_MAX,
                },
            )
        approximate = float(exact)
        if math.isfinite(approximate) and Decimal(str(approximate)) == exact:
            continue
        raise DataError(
            (
                "jq update filter contains a numeric literal that cannot be "
                "represented exactly by the embedded jq runtime"
            ),
            code="data_update_precision_loss",
            details={
                "phase": "filter",
                "position": position,
                "literal": literal,
            },
        )


def run_data_update(args: Namespace) -> int:
    bindings = _arguments(args)
    bindings_value = cast("JsonValue", bindings)
    _require_jq_safe_integers(
        bindings_value,
        phase="argument",
    )
    _require_jq_identity_roundtrip(
        bindings_value,
        phase="argument",
    )
    _require_jq_filter_number_roundtrips(args.filter)
    session = _new_mutation_session()

    def transform(snapshot: MutationSnapshot) -> MutationDraft:
        input_data = cast("JsonValue", snapshot.data)
        _require_jq_safe_integers(
            input_data,
            phase="input",
        )
        _require_jq_identity_roundtrip(
            input_data,
            phase="input",
        )
        try:
            results = evaluate(
                input_data,
                args.filter,
                args=bindings,
                max_results=1,
            )
        except RenderingError as error:
            if error.code != "jq_result_limit":
                raise
            raise DataError(
                "jq update filter produced multiple values",
                code="data_update_multiple_results",
            ) from None
        if not results:
            raise DataError(
                "jq update filter produced no value",
                code="data_update_no_result",
            )
        result = results[0]
        if not isinstance(result, Mapping):
            raise DataError(
                "jq update filter must produce one JSON object",
                code="data_update_root_not_object",
                details={"value_type": type(result).__name__},
            )
        _require_jq_safe_integers(
            result,
            phase="output",
        )
        return MutationDraft(result)

    result = session.mutate(_mutation_request(args, session, transform))
    _write_mutation_result(args, result)
    return 0


def _retry_edit(error: GhSlateError, attempt: int) -> bool:
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        return False
    print(
        f"error[{error.code}]: {error.message}",
        file=sys.stderr,
    )
    sys.stderr.write(f"reopen editor after invalid attempt {attempt}? [Y/n] ")
    sys.stderr.flush()
    answer = sys.stdin.readline(16)
    if not answer.endswith(("\n", "\r")) and len(answer) == 16:
        return False
    return answer.strip().casefold() in {"", "y", "yes"}


def run_data_edit(args: Namespace) -> int:
    session = _new_mutation_session()

    def transform(snapshot: MutationSnapshot) -> MutationDraft:
        edited = edit_json(
            snapshot.data,
            validate=lambda value: validate_data(
                value,
                snapshot.data_schema,
            ),
            retry=_retry_edit,
        )
        return MutationDraft(edited)

    result = session.mutate(_mutation_request(args, session, transform))
    _write_mutation_result(args, result)
    return 0


__all__ = [
    "DataMutationSession",
    "run_data_delete",
    "run_data_edit",
    "run_data_get",
    "run_data_set",
    "run_data_update",
]
