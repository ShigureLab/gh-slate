from __future__ import annotations

import base64
import binascii
import ctypes
import os
import sys


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


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 5:
        return _emit_error("internal")
    try:
        encoded_filter, source_limit_text, output_limit_text, memory_text, cpu_text = arguments
        filter_bytes = base64.b64decode(encoded_filter, validate=True)
        filter_text = filter_bytes.decode("utf-8", errors="strict")
        source_limit = int(source_limit_text)
        output_limit = int(output_limit_text)
        memory_limit = int(memory_text)
        cpu_limit = int(cpu_text)
        if min(source_limit, output_limit, memory_limit, cpu_limit) <= 0:
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
    try:
        source.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return _emit_error("internal")

    try:
        import jq  # ty: ignore[unresolved-import]
    except Exception:
        return _emit_error("internal")

    # Newlines keep a trailing jq comment from swallowing the structural
    # wrapper. The parent validates that the response still contains <=2 items.
    wrapped_filter = f"[limit(2; (\n{filter_text}\n))]"
    try:
        compile_jq = jq.compile
        program = compile_jq(wrapped_filter)
    except Exception:
        return _emit_error("compile")
    try:
        result_text = program.input(text=source.decode("utf-8")).text()
    except Exception:
        return _emit_error("runtime")

    try:
        result_bytes = result_text.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return _emit_error("internal")
    if len(result_bytes) > output_limit:
        return _emit_error("output_limit")

    sys.stdout.buffer.write(b'{"ok":true,"results":')
    sys.stdout.buffer.write(result_bytes)
    sys.stdout.buffer.write(b"}")
    _ = memory_guard
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
