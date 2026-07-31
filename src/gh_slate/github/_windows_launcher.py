from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes
from typing import Any

_WAIT_OBJECT_0 = 0
_INFINITE = 0xFFFFFFFF
_ERROR_PREFIX = b"\0gh-slate-windows-launch-error:"


def _kernel32() -> Any:
    win_dll = ctypes.__dict__["WinDLL"]
    library = win_dll("kernel32", use_last_error=True)
    library.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    library.WaitForSingleObject.restype = wintypes.DWORD
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


def main() -> int:
    if len(sys.argv) < 4:
        return 125
    event_handle = int(sys.argv[1])
    nonce = sys.argv[2]
    argv = tuple(sys.argv[3:])
    kernel32 = _kernel32()
    try:
        if kernel32.WaitForSingleObject(event_handle, _INFINITE) != _WAIT_OBJECT_0:
            return 125
    finally:
        kernel32.CloseHandle(event_handle)

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
