from __future__ import annotations

import sys
from pathlib import Path

from gh_slate.rendering.errors import RenderingError
from gh_slate.rendering.jinja import DEFAULT_JINJA_LIMITS


def _input_size_error(
    *,
    subject: str,
    actual_bytes: int,
    max_bytes: int,
) -> RenderingError:
    return RenderingError(
        f"{subject} exceeds the configured input byte limit",
        code="input_size_limit",
        details={
            "actual_bytes": actual_bytes,
            "max_bytes": max_bytes,
        },
    )


def _stdin_bytes(*, subject: str, max_bytes: int) -> bytes:
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    value = stream.read(max_bytes + 1)
    encoded = value.encode("utf-8") if isinstance(value, str) else value
    if len(encoded) > max_bytes:
        raise _input_size_error(
            subject=subject,
            actual_bytes=len(encoded),
            max_bytes=max_bytes,
        )
    return encoded


def read_bytes(
    location: str,
    *,
    subject: str,
    max_bytes: int,
) -> bytes:
    if location == "-":
        return _stdin_bytes(subject=subject, max_bytes=max_bytes)
    try:
        path = Path(location)
        size = path.stat().st_size
        if size > max_bytes:
            raise _input_size_error(
                subject=subject,
                actual_bytes=size,
                max_bytes=max_bytes,
            )
        with path.open("rb") as stream:
            value = stream.read(max_bytes + 1)
        if len(value) > max_bytes:
            raise _input_size_error(
                subject=subject,
                actual_bytes=len(value),
                max_bytes=max_bytes,
            )
        return value
    except RenderingError:
        raise
    except OSError as error:
        raise RenderingError(
            f"{subject} could not be read",
            code="input_read_failed",
            details={
                "path": location,
                "error_type": type(error).__name__,
            },
        ) from None


def template_source(location: str) -> str:
    source = read_bytes(
        location,
        subject="template",
        max_bytes=DEFAULT_JINJA_LIMITS.max_source_bytes,
    )
    try:
        decoded = source.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise RenderingError(
            "template is not valid UTF-8",
            code="jinja_source_invalid",
            details={"position": error.start},
        ) from None
    return decoded.replace("\r\n", "\n").replace("\r", "\n")
