from __future__ import annotations

import ctypes
import os
import secrets
import subprocess
import sys
import threading
from ctypes import wintypes
from typing import TYPE_CHECKING, Any, cast

from gh_slate.github.process import _ProcessHandoffInterrupted

if TYPE_CHECKING:
    from collections.abc import Mapping

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_RELEASE_BYTE = 1
_ERROR_PREFIX = b"\0gh-slate-windows-launch-error:"


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


def _kernel32() -> Any:
    win_dll = ctypes.__dict__["WinDLL"]
    library = win_dll("kernel32", use_last_error=True)
    library.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    library.CreateJobObjectW.restype = wintypes.HANDLE
    library.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    library.SetInformationJobObject.restype = wintypes.BOOL
    library.CreatePipe.argtypes = (
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    library.CreatePipe.restype = wintypes.BOOL
    library.WriteFile.argtypes = (
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    )
    library.WriteFile.restype = wintypes.BOOL
    library.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    library.OpenProcess.restype = wintypes.HANDLE
    library.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    library.AssignProcessToJobObject.restype = wintypes.BOOL
    library.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    library.TerminateJobObject.restype = wintypes.BOOL
    library.CloseHandle.argtypes = (wintypes.HANDLE,)
    library.CloseHandle.restype = wintypes.BOOL
    return library


def _handle_value(handle: object) -> int:
    if handle is None:
        return 0
    if isinstance(handle, int):
        return handle
    value = getattr(handle, "value", None)
    return value if isinstance(value, int) else 0


def _last_error() -> OSError:
    get_last_error = ctypes.__dict__["get_last_error"]
    win_error = ctypes.__dict__["WinError"]
    return cast("OSError", win_error(get_last_error()))


class WindowsJob:
    def __init__(self, handle: int, *, nonce: str, kernel32: Any) -> None:
        self._handle: int | None = handle
        self._nonce = nonce
        self._kernel32 = kernel32
        self._lock = threading.Lock()

    def terminate(self) -> None:
        with self._lock:
            if self._handle is None:
                return
            if self._kernel32.TerminateJobObject(self._handle, 1):
                return
            error = _last_error()
            if not self._kernel32.TerminateJobObject(self._handle, 1):
                raise error

    def close(self) -> None:
        with self._lock:
            handle = self._handle
            if handle is None:
                return
            # Relinquish ownership before entering CloseHandle. An asynchronous
            # exception can arrive after the kernel closes the handle but before
            # Python observes the return value; retaining the numeric value in
            # that case could close an unrelated handle after Windows reuses it.
            self._handle = None
            if not self._kernel32.CloseHandle(handle):
                raise _last_error()

    def launch_error(self, stderr: bytes) -> OSError | None:
        prefix = _ERROR_PREFIX + self._nonce.encode("ascii") + b":"
        if not stderr.startswith(prefix):
            return None
        fields = stderr[len(prefix) :].split(b":", 1)
        if len(fields) != 2:
            return OSError("Windows command launcher failed")
        kind = fields[0].decode("ascii", errors="replace")
        try:
            error_number = int(fields[1].splitlines()[0])
        except ValueError:
            error_number = 0
        error_type: type[OSError]
        if kind == "FileNotFoundError":
            error_type = FileNotFoundError
        elif kind == "PermissionError":
            error_type = PermissionError
        else:
            error_type = OSError
        return error_type(error_number, "unable to start the requested command")

    def assign(self, process_handle: int) -> None:
        with self._lock:
            if self._handle is None or not self._kernel32.AssignProcessToJobObject(
                self._handle,
                process_handle,
            ):
                raise _last_error()


def _create_job(kernel32: Any, *, nonce: str) -> WindowsJob:
    raw_job = kernel32.CreateJobObjectW(None, None)
    job_handle = _handle_value(raw_job)
    if not job_handle:
        raise _last_error()
    information = _JobObjectExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        job_handle,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        error = _last_error()
        kernel32.CloseHandle(job_handle)
        raise error
    return WindowsJob(job_handle, nonce=nonce, kernel32=kernel32)


def _create_gate(kernel32: Any) -> tuple[int, int]:
    raw_read = wintypes.HANDLE()
    raw_write = wintypes.HANDLE()
    if not kernel32.CreatePipe(
        ctypes.byref(raw_read),
        ctypes.byref(raw_write),
        None,
        0,
    ):
        raise _last_error()
    read_handle = _handle_value(raw_read)
    write_handle = _handle_value(raw_write)
    if not read_handle or not write_handle:  # pragma: no cover - Win32 contract
        raise OSError("Windows process gate returned an invalid handle")
    return read_handle, write_handle


def _close_handle(kernel32: Any, handle: int) -> None:
    if not kernel32.CloseHandle(handle):
        raise _last_error()


def _release_gate(kernel32: Any, handle: int) -> None:
    release = ctypes.c_ubyte(_RELEASE_BYTE)
    bytes_written = wintypes.DWORD()
    if not kernel32.WriteFile(
        handle,
        ctypes.byref(release),
        1,
        ctypes.byref(bytes_written),
        None,
    ):
        raise _last_error()
    if bytes_written.value != 1:  # pragma: no cover - synchronous pipe contract
        raise OSError("Windows process gate release was incomplete")


def spawn_windows_process(
    argv: tuple[str, ...],
    *,
    environment: Mapping[str, str] | None,
    stdin: int = subprocess.DEVNULL,
) -> tuple[subprocess.Popen[bytes], WindowsJob]:
    """Start a command only after its gated launcher is contained in a Job."""

    kernel32 = _kernel32()
    nonce = secrets.token_hex(16)
    job = _create_job(kernel32, nonce=nonce)
    try:
        gate_read, gate_write = _create_gate(kernel32)
    except BaseException as error:
        try:
            job.close()
        except BaseException as close_error:
            raise close_error from error
        raise

    process: subprocess.Popen[bytes] | None = None
    gate_read_open = True
    gate_write_open = True
    release_attempted = False

    def close_gate() -> None:
        nonlocal gate_read_open, gate_write_open
        first_error: BaseException | None = None
        if gate_write_open:
            # Invalidate ownership first for the same asynchronous-exception
            # reason as WindowsJob.close(). Never retry a numeric HANDLE.
            gate_write_open = False
            try:
                _close_handle(kernel32, gate_write)
            except BaseException as error:
                first_error = error
        if gate_read_open:
            gate_read_open = False
            try:
                _close_handle(kernel32, gate_read)
            except BaseException as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error

    try:
        set_handle_inheritable = os.__dict__["set_handle_inheritable"]
        set_handle_inheritable(gate_read, True)
        startupinfo_type = subprocess.__dict__["STARTUPINFO"]
        startupinfo = startupinfo_type()
        startupinfo.lpAttributeList = {"handle_list": [gate_read]}
        launcher = (
            sys.executable,
            "-m",
            "gh_slate.github._windows_launcher",
            str(gate_read),
            nonce,
            *argv,
        )
        try:
            process = subprocess.Popen(
                launcher,
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                env=environment,
                close_fds=True,
                startupinfo=startupinfo,
                creationflags=cast("int", subprocess.__dict__["CREATE_NEW_PROCESS_GROUP"]),
            )
        finally:
            set_handle_inheritable(gate_read, False)

        raw_process = kernel32.OpenProcess(
            _PROCESS_SET_QUOTA | _PROCESS_TERMINATE,
            False,
            process.pid,
        )
        process_handle = _handle_value(raw_process)
        if not process_handle:
            raise _last_error()
        try:
            job.assign(process_handle)
        finally:
            _close_handle(kernel32, process_handle)

        # Set this before entering WriteFile: an interruption during the C call
        # cannot prove whether the child observed the release byte.
        release_attempted = True
        _release_gate(kernel32, gate_write)
        close_gate()
        return process, job
    except BaseException as error:
        cleanup_error: BaseException | None = None
        try:
            close_gate()
        except BaseException as caught:
            cleanup_error = caught
        try:
            job.terminate()
        except BaseException as caught:
            if cleanup_error is None:
                cleanup_error = caught
        if process is not None:
            try:
                process.kill()
            except BaseException as caught:
                if cleanup_error is None:
                    cleanup_error = caught
            try:
                process.wait(timeout=1.0)
            except BaseException as caught:
                if cleanup_error is None:
                    cleanup_error = caught
        try:
            job.close()
        except BaseException as caught:
            if cleanup_error is None:
                cleanup_error = caught
        if release_attempted:
            cleanup_error_type = None if cleanup_error is None else type(cleanup_error).__name__
            raise _ProcessHandoffInterrupted(
                type(error).__name__,
                cleanup_error_type=cleanup_error_type,
            ) from error
        if cleanup_error is not None:
            raise cleanup_error from error
        raise


__all__ = ["WindowsJob", "spawn_windows_process"]
