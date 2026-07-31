from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes
from typing import Any

_RELEASE_BYTE = 1
_ERROR_PREFIX = b"\0gh-slate-windows-launch-error:"


def _kernel32() -> Any:
    win_dll = ctypes.__dict__["WinDLL"]
    library = win_dll("kernel32", use_last_error=True)
    library.ReadFile.argtypes = (
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    )
    library.ReadFile.restype = wintypes.BOOL
    library.CloseHandle.argtypes = (wintypes.HANDLE,)
    library.CloseHandle.restype = wintypes.BOOL
    return library


def _report_launch_error(nonce: str, error: OSError) -> None:
    kind = type(error).__name__
    error_number = 0 if error.errno is None else error.errno
    payload = _ERROR_PREFIX + nonce.encode("ascii") + f":{kind}:{error_number}\n".encode("ascii")
    try:
        os.write(2, payload)
    except OSError:
        pass


def _last_error_number() -> int:
    get_last_error = ctypes.__dict__["get_last_error"]
    return int(get_last_error())


def main() -> int:
    if len(sys.argv) < 4:
        return 125
    gate_handle = int(sys.argv[1])
    nonce = sys.argv[2]
    argv = tuple(sys.argv[3:])
    kernel32 = _kernel32()
    release = ctypes.c_ubyte()
    bytes_read = wintypes.DWORD()
    released = kernel32.ReadFile(
        gate_handle,
        ctypes.byref(release),
        1,
        ctypes.byref(bytes_read),
        None,
    )
    read_error = 0 if released else _last_error_number()
    closed = kernel32.CloseHandle(gate_handle)
    if not released or not closed or bytes_read.value != 1 or release.value != _RELEASE_BYTE:
        error_number = read_error or _last_error_number()
        _report_launch_error(nonce, OSError(error_number, "Windows process gate did not release"))
        return 125

    try:
        # The launcher's standard handles are the bounded pipes owned by the
        # parent runner. Windows requires handle inheritance here so the real
        # command and all of its descendants remain observable until cleanup.
        completed = subprocess.run(argv, shell=False, check=False, close_fds=False)
    except OSError as error:
        _report_launch_error(nonce, error)
        return 125
    return completed.returncode


if __name__ == "__main__":  # pragma: no cover - executed by the Windows child
    raise SystemExit(main())
