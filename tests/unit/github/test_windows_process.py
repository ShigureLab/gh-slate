from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING, Any, cast

import pytest

import gh_slate.github._windows_launcher as windows_launcher
import gh_slate.github._windows_process as windows_process
from gh_slate.github._windows_process import WindowsJob

if TYPE_CHECKING:
    from pathlib import Path


class FakeKernel32:
    def __init__(self, *, assign: bool = True, write_error: BaseException | None = None) -> None:
        self.assign = assign
        self.write_error = write_error
        self.assigned: list[tuple[int, int]] = []
        self.writes: list[int] = []
        self.terminated: list[tuple[int, int]] = []
        self.closed: list[int] = []

    def CreateJobObjectW(self, security: object, name: object) -> int:
        return 11

    def SetInformationJobObject(self, *args: object) -> bool:
        return True

    def CreatePipe(
        self,
        read: object,
        write: object,
        security: object,
        size: int,
    ) -> bool:
        windows_process.ctypes.cast(
            cast("Any", read),
            windows_process.ctypes.POINTER(windows_process.wintypes.HANDLE),
        ).contents.value = 22
        windows_process.ctypes.cast(
            cast("Any", write),
            windows_process.ctypes.POINTER(windows_process.wintypes.HANDLE),
        ).contents.value = 23
        return True

    def OpenProcess(self, *args: object) -> int:
        return 33

    def AssignProcessToJobObject(self, job: int, process: int) -> bool:
        self.assigned.append((job, process))
        return self.assign

    def WriteFile(
        self,
        handle: int,
        buffer: object,
        size: int,
        written: object,
        overlapped: object,
    ) -> bool:
        self.writes.append(handle)
        if self.write_error is not None:
            raise self.write_error
        windows_process.ctypes.cast(
            cast("Any", written),
            windows_process.ctypes.POINTER(windows_process.wintypes.DWORD),
        ).contents.value = 1
        return True

    def TerminateJobObject(self, job: int, code: int) -> bool:
        self.terminated.append((job, code))
        return True

    def CloseHandle(self, handle: int) -> bool:
        self.closed.append(handle)
        return True


def test_windows_launcher_waits_for_assignment_before_starting_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []

    class Kernel32:
        def ReadFile(
            self,
            handle: int,
            buffer: object,
            size: int,
            bytes_read: object,
            overlapped: object,
        ) -> bool:
            events.append(("read", handle))
            windows_launcher.ctypes.cast(
                cast("Any", buffer),
                windows_launcher.ctypes.POINTER(windows_launcher.ctypes.c_ubyte),
            ).contents.value = 1
            windows_launcher.ctypes.cast(
                cast("Any", bytes_read),
                windows_launcher.ctypes.POINTER(windows_launcher.wintypes.DWORD),
            ).contents.value = 1
            return True

        def CloseHandle(self, handle: int) -> bool:
            events.append(("close", handle))
            return True

    def run(argv: tuple[str, ...], **kwargs: object) -> object:
        events.append(("run", argv, kwargs))
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(windows_launcher, "_kernel32", Kernel32)
    monkeypatch.setattr(windows_launcher.sys, "argv", ["launcher", "22", "abc", "gh", "version"])
    monkeypatch.setattr(windows_launcher.subprocess, "run", run)

    assert windows_launcher.main() == 0
    assert events[:2] == [("read", 22), ("close", 22)]
    assert events[2] == (
        "run",
        ("gh", "version"),
        {"shell": False, "check": False, "close_fds": False},
    )


def test_windows_launcher_refuses_to_start_after_gate_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reported: list[OSError] = []

    class Kernel32:
        def ReadFile(
            self,
            handle: int,
            buffer: object,
            size: int,
            bytes_read: object,
            overlapped: object,
        ) -> bool:
            return False

        def CloseHandle(self, handle: int) -> bool:
            return True

    monkeypatch.setattr(windows_launcher, "_kernel32", Kernel32)
    monkeypatch.setattr(windows_launcher, "_last_error_number", lambda: 109)
    monkeypatch.setattr(windows_launcher, "_report_launch_error", lambda nonce, error: reported.append(error))
    monkeypatch.setattr(windows_launcher.sys, "argv", ["launcher", "22", "abc", "gh", "version"])
    monkeypatch.setattr(
        windows_launcher.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("the real command must remain gated"),
    )

    assert windows_launcher.main() == 125
    assert len(reported) == 1
    assert reported[0].errno == 109


@pytest.mark.skipif(os.name != "nt", reason="Windows inherited HANDLEs are unavailable")
def test_windows_launcher_exits_without_running_command_after_real_pipe_eof(
    tmp_path: Path,
) -> None:
    kernel32 = windows_process._kernel32()
    gate_read, gate_write = windows_process._create_gate(kernel32)
    marker = tmp_path / "command-ran"
    set_handle_inheritable = os.__dict__["set_handle_inheritable"]
    startupinfo_type = subprocess.__dict__["STARTUPINFO"]
    creationflags = cast("int", subprocess.__dict__["CREATE_NEW_PROCESS_GROUP"])
    set_handle_inheritable(gate_read, True)
    startupinfo = startupinfo_type()
    startupinfo.lpAttributeList = {"handle_list": [gate_read]}
    try:
        process = subprocess.Popen(
            (
                sys.executable,
                "-m",
                "gh_slate.github._windows_launcher",
                str(gate_read),
                "abc",
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('ran')",
                str(marker),
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
    finally:
        set_handle_inheritable(gate_read, False)
        windows_process._close_handle(kernel32, gate_write)
        windows_process._close_handle(kernel32, gate_read)

    stdout, stderr = process.communicate(timeout=5)

    assert process.returncode == 125
    assert stdout == b""
    assert stderr.startswith(b"\0gh-slate-windows-launch-error:abc:OSError:")
    assert not marker.exists()


def test_windows_job_retains_tree_handle_after_leader_exit_and_closes_once() -> None:
    kernel32 = FakeKernel32()
    job = WindowsJob(11, nonce="abc", kernel32=kernel32)

    job.terminate()
    job.close()
    job.close()

    assert kernel32.terminated == [(11, 1)]
    assert kernel32.closed == [11]


def test_windows_job_does_not_reuse_a_handle_after_close_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Kernel32:
        def __init__(self) -> None:
            self.terminate_results = iter((False, False, True))
            self.close_results = iter((False,))
            self.terminated = 0
            self.closed = 0

        def TerminateJobObject(self, handle: int, code: int) -> bool:
            self.terminated += 1
            return next(self.terminate_results)

        def CloseHandle(self, handle: int) -> bool:
            self.closed += 1
            return next(self.close_results)

    kernel32 = Kernel32()
    job = WindowsJob(11, nonce="abc", kernel32=kernel32)
    monkeypatch.setattr(windows_process, "_last_error", lambda: OSError("cleanup failed"))

    with pytest.raises(OSError, match="cleanup failed"):
        job.terminate()
    job.terminate()

    with pytest.raises(OSError, match="cleanup failed"):
        job.close()
    job.close()

    assert kernel32.terminated == 3
    assert kernel32.closed == 1


def test_windows_job_drops_handle_ownership_when_close_is_interrupted() -> None:
    class Kernel32:
        def __init__(self) -> None:
            self.closed = 0

        def CloseHandle(self, handle: int) -> bool:
            self.closed += 1
            raise KeyboardInterrupt

    kernel32 = Kernel32()
    job = WindowsJob(11, nonce="abc", kernel32=kernel32)

    with pytest.raises(KeyboardInterrupt):
        job.close()
    job.close()

    assert kernel32.closed == 1


@pytest.mark.parametrize(
    ("kind", "error_type"),
    [("FileNotFoundError", FileNotFoundError), ("PermissionError", PermissionError), ("OSError", OSError)],
)
def test_windows_job_recovers_gated_launcher_errors(kind: str, error_type: type[OSError]) -> None:
    job = WindowsJob(11, nonce="abc", kernel32=FakeKernel32())

    error = job.launch_error(f"\0gh-slate-windows-launch-error:abc:{kind}:2\n".encode())

    assert isinstance(error, error_type)
    assert error is not None
    assert error.errno == 2


def test_windows_assignment_failure_never_releases_the_gated_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel32 = FakeKernel32(assign=False)
    inherited: list[tuple[int, bool]] = []

    class StartupInfo:
        lpAttributeList: dict[str, list[int]]

    class Process:
        pid = 44
        stdout = None
        stderr = None
        killed = 0
        waited = 0

        def kill(self) -> None:
            self.killed += 1

        def wait(self, timeout: float | None = None) -> int:
            self.waited += 1
            return 1

    process = Process()
    launched: list[tuple[str, ...]] = []

    def popen(argv: tuple[str, ...], **kwargs: object) -> Process:
        launched.append(argv)
        return process

    monkeypatch.setattr(windows_process, "_kernel32", lambda: kernel32)
    monkeypatch.setattr(windows_process, "_last_error", lambda: OSError("assignment failed"))
    monkeypatch.setitem(
        windows_process.os.__dict__,
        "set_handle_inheritable",
        lambda handle, value: inherited.append((cast("int", handle), cast("bool", value))),
    )
    monkeypatch.setitem(windows_process.subprocess.__dict__, "STARTUPINFO", StartupInfo)
    monkeypatch.setitem(windows_process.subprocess.__dict__, "CREATE_NEW_PROCESS_GROUP", 512)
    monkeypatch.setattr(windows_process.subprocess, "Popen", popen)
    monkeypatch.setattr(windows_process.secrets, "token_hex", lambda length: "abc")

    with pytest.raises(OSError, match="assignment failed"):
        windows_process.spawn_windows_process(("gh", "version"), environment=None)

    assert launched and launched[0][-2:] == ("gh", "version")
    assert kernel32.assigned == [(11, 33)]
    assert kernel32.writes == []
    assert process.killed == 1
    assert process.waited == 1
    assert kernel32.terminated == [(11, 1)]
    assert kernel32.closed == [33, 23, 22, 11]
    assert inherited == [(22, True), (22, False)]


def test_windows_release_interruption_is_a_handoff_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel32 = FakeKernel32(write_error=KeyboardInterrupt())
    process = type(
        "Process",
        (),
        {
            "pid": 44,
            "kill": lambda self: None,
            "wait": lambda self, timeout=None: 1,
        },
    )()

    class StartupInfo:
        lpAttributeList: dict[str, list[int]]

    monkeypatch.setattr(windows_process, "_kernel32", lambda: kernel32)
    monkeypatch.setattr(windows_process, "_last_error", lambda: OSError("unused"))
    monkeypatch.setitem(
        windows_process.os.__dict__,
        "set_handle_inheritable",
        lambda handle, value: None,
    )
    monkeypatch.setitem(windows_process.subprocess.__dict__, "STARTUPINFO", StartupInfo)
    monkeypatch.setitem(windows_process.subprocess.__dict__, "CREATE_NEW_PROCESS_GROUP", 512)
    monkeypatch.setattr(windows_process.subprocess, "Popen", lambda argv, **kwargs: process)

    with pytest.raises(windows_process._ProcessHandoffInterrupted) as captured:
        windows_process.spawn_windows_process(("gh", "version"), environment=None)

    assert captured.value.error_type == "KeyboardInterrupt"
    assert captured.value.cleanup_error_type is None
    assert kernel32.writes == [23]
    assert kernel32.terminated == [(11, 1)]
    assert kernel32.closed == [33, 23, 22, 11]


def test_windows_cleanup_interruption_preserves_the_handoff_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel32 = FakeKernel32(write_error=KeyboardInterrupt())
    process = type(
        "Process",
        (),
        {
            "pid": 44,
            "kill": lambda self: None,
            "wait": lambda self, timeout=None: (_ for _ in ()).throw(SystemExit(130)),
        },
    )()

    class StartupInfo:
        lpAttributeList: dict[str, list[int]]

    monkeypatch.setattr(windows_process, "_kernel32", lambda: kernel32)
    monkeypatch.setattr(windows_process, "_last_error", lambda: OSError("unused"))
    monkeypatch.setitem(
        windows_process.os.__dict__,
        "set_handle_inheritable",
        lambda handle, value: None,
    )
    monkeypatch.setitem(windows_process.subprocess.__dict__, "STARTUPINFO", StartupInfo)
    monkeypatch.setitem(windows_process.subprocess.__dict__, "CREATE_NEW_PROCESS_GROUP", 512)
    monkeypatch.setattr(windows_process.subprocess, "Popen", lambda argv, **kwargs: process)

    with pytest.raises(windows_process._ProcessHandoffInterrupted) as captured:
        windows_process.spawn_windows_process(("gh", "version"), environment=None)

    assert captured.value.error_type == "KeyboardInterrupt"
    assert captured.value.cleanup_error_type == "SystemExit"
    assert kernel32.closed == [33, 23, 22, 11]


def test_windows_popen_interruption_cancels_the_pipe_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel32 = FakeKernel32()

    class StartupInfo:
        lpAttributeList: dict[str, list[int]]

    monkeypatch.setattr(windows_process, "_kernel32", lambda: kernel32)
    monkeypatch.setitem(
        windows_process.os.__dict__,
        "set_handle_inheritable",
        lambda handle, value: None,
    )
    monkeypatch.setitem(windows_process.subprocess.__dict__, "STARTUPINFO", StartupInfo)
    monkeypatch.setitem(windows_process.subprocess.__dict__, "CREATE_NEW_PROCESS_GROUP", 512)
    monkeypatch.setattr(
        windows_process.subprocess,
        "Popen",
        lambda argv, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        windows_process.spawn_windows_process(("gh", "version"), environment=None)

    assert kernel32.writes == []
    assert kernel32.terminated == [(11, 1)]
    assert kernel32.closed == [23, 22, 11]
