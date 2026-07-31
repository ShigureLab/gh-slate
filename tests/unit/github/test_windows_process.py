from __future__ import annotations

from typing import cast

import pytest

import gh_slate.github._windows_launcher as windows_launcher
import gh_slate.github._windows_process as windows_process
from gh_slate.github._windows_process import WindowsJob


class FakeKernel32:
    def __init__(self, *, assign: bool = True) -> None:
        self.assign = assign
        self.assigned: list[tuple[int, int]] = []
        self.set_events: list[int] = []
        self.terminated: list[tuple[int, int]] = []
        self.closed: list[int] = []

    def CreateJobObjectW(self, security: object, name: object) -> int:
        return 11

    def SetInformationJobObject(self, *args: object) -> bool:
        return True

    def CreateEventW(self, *args: object) -> int:
        return 22

    def OpenProcess(self, *args: object) -> int:
        return 33

    def AssignProcessToJobObject(self, job: int, process: int) -> bool:
        self.assigned.append((job, process))
        return self.assign

    def SetEvent(self, event: int) -> bool:
        self.set_events.append(event)
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
        def WaitForSingleObject(self, handle: int, timeout: int) -> int:
            events.append(("wait", handle, timeout))
            return 0

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
    assert events[:2] == [("wait", 22, 0xFFFFFFFF), ("close", 22)]
    assert events[2] == (
        "run",
        ("gh", "version"),
        {"shell": False, "check": False, "close_fds": False},
    )


def test_windows_job_retains_tree_handle_after_leader_exit_and_closes_once() -> None:
    kernel32 = FakeKernel32()
    job = WindowsJob(11, nonce="abc", kernel32=kernel32)

    job.terminate()
    job.close()
    job.close()

    assert kernel32.terminated == [(11, 1)]
    assert kernel32.closed == [11]


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
    assert kernel32.set_events == []
    assert process.killed == 1
    assert process.waited == 1
    assert kernel32.terminated == [(11, 1)]
    assert kernel32.closed == [33, 11, 22]
    assert inherited == [(22, True), (22, False)]
