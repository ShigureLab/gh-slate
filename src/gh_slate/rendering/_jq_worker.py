from __future__ import annotations

import base64
import binascii
import ctypes
import json
import math
import os
import re
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

_ARGUMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_MAX_RESULTS = 100_000


def _emit_error(kind: str) -> int:
    sys.stdout.write(f'{{"ok":false,"kind":"{kind}"}}')
    return 0


def _apply_unix_limits(memory_bytes: int, cpu_seconds: int) -> None:
    try:
        import resource
    except ImportError:  # pragma: no cover - Windows
        return

    for limit_name in ("RLIMIT_AS", "RLIMIT_DATA"):
        resource_id = getattr(resource, limit_name, None)
        if resource_id is None:
            continue
        try:
            resource.setrlimit(resource_id, (memory_bytes, memory_bytes))
        except (OSError, ValueError):
            pass
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    except (OSError, ValueError):
        pass


def _apply_windows_memory_limit(memory_bytes: int) -> object:
    """Keep one Windows worker below a hard per-process commit limit."""

    from ctypes import WinDLL, WinError, get_last_error, wintypes  # ty: ignore[unresolved-import]

    class _JobObjectBasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JobObjectExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JobObjectBasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.argtypes = ()
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise WinError(get_last_error())
    information = _JobObjectExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = 0x00000100
    information.ProcessMemoryLimit = memory_bytes
    if not kernel32.SetInformationJobObject(
        job,
        9,
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        error = get_last_error()
        kernel32.CloseHandle(job)
        raise WinError(error)
    if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
        error = get_last_error()
        kernel32.CloseHandle(job)
        raise WinError(error)
    return job


def _apply_process_limits(
    memory_bytes: int,
    cpu_seconds: int,
    *,
    platform: str | None = None,
) -> object | None:
    current_platform = os.name if platform is None else platform
    if current_platform == "nt":
        return _apply_windows_memory_limit(memory_bytes)
    _apply_unix_limits(memory_bytes, cpu_seconds)
    return None


def _reject_constant(token: str) -> object:
    raise ValueError(f"non-finite JSON number: {token}")


def _parse_float(token: str) -> float:
    value = float(token)
    if not math.isfinite(value):
        raise ValueError("non-finite JSON number")
    return value


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _valid_unicode(value: object) -> bool:
    if isinstance(value, str):
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            return False
        return True
    if isinstance(value, list):
        return all(_valid_unicode(item) for item in value)
    if isinstance(value, dict):
        return all(_valid_unicode(key) and _valid_unicode(item) for key, item in value.items())
    return True


def _bounded_json_array(
    results: Iterator[object],
    *,
    max_results: int,
    max_bytes: int,
) -> bytes | None:
    """Encode at most ``max_results + 1`` jq values into a bounded buffer."""

    encoded = bytearray(b"[")
    encoder = json.JSONEncoder(
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    count = 0
    while count <= max_results:
        try:
            result = next(results)
        except StopIteration:
            break
        if not _valid_unicode(result):
            return None
        if count:
            encoded.extend(b",")
        try:
            for chunk in encoder.iterencode(result):
                chunk_bytes = chunk.encode("utf-8", errors="strict")
                if len(encoded) + len(chunk_bytes) + 1 > max_bytes:
                    raise OverflowError
                encoded.extend(chunk_bytes)
        except (TypeError, ValueError):
            return None
        count += 1
    if len(encoded) + 1 > max_bytes:
        raise OverflowError
    encoded.extend(b"]")
    return bytes(encoded)


def _decode_request(source: bytes) -> tuple[object, dict[str, object]] | None:
    try:
        text = source.decode("utf-8", errors="strict")
        request = json.loads(
            text,
            parse_float=_parse_float,
            parse_constant=_reject_constant,
            object_pairs_hook=_object_without_duplicates,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(request, dict) or set(request) != {"args", "data"}:
        return None
    arguments = request["args"]
    if not isinstance(arguments, dict):
        return None
    if not all(_ARGUMENT_NAME.fullmatch(name) is not None for name in arguments):
        return None
    if not _valid_unicode(request):
        return None
    return request["data"], arguments


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 6:
        return _emit_error("internal")
    try:
        encoded_filter, max_results_text, source_limit_text, output_limit_text, memory_text, cpu_text = arguments
        filter_bytes = base64.b64decode(encoded_filter, validate=True)
        filter_text = filter_bytes.decode("utf-8", errors="strict")
        max_results = int(max_results_text)
        source_limit = int(source_limit_text)
        output_limit = int(output_limit_text)
        memory_limit = int(memory_text)
        cpu_limit = int(cpu_text)
        if min(max_results, source_limit, output_limit, memory_limit, cpu_limit) <= 0 or max_results > _MAX_RESULTS:
            return _emit_error("internal")
    except (binascii.Error, UnicodeError, ValueError):
        return _emit_error("internal")

    os.environ.clear()
    try:
        memory_guard = _apply_process_limits(memory_limit, cpu_limit)
    except OSError:
        return _emit_error("internal")

    source = sys.stdin.buffer.read(source_limit + 1)
    if len(source) > source_limit:
        return _emit_error("source_limit")
    request = _decode_request(source)
    if request is None:
        return _emit_error("protocol")
    data, jq_args = request

    try:
        import jq  # ty: ignore[unresolved-import]
    except Exception:
        return _emit_error("internal")

    try:
        compile_jq = jq.compile
        program = compile_jq(filter_text, args=jq_args)
    except Exception:
        return _emit_error("compile")
    try:
        result_bytes = _bounded_json_array(
            iter(program.input_value(data)),
            max_results=max_results,
            max_bytes=output_limit,
        )
    except OverflowError:
        return _emit_error("output_limit")
    except Exception:
        return _emit_error("runtime")
    if result_bytes is None:
        return _emit_error("internal")

    sys.stdout.buffer.write(b'{"ok":true,"results":')
    sys.stdout.buffer.write(result_bytes)
    sys.stdout.buffer.write(b"}")
    _ = memory_guard
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
