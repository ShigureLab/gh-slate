from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "gh-slate"


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="the Bash-based gh extension launcher requires Unix",
)
def test_extension_launcher_forwards_arguments_and_display_command(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/usr/bin/env bash\n"
        'printf "display=%s\\n" "${GH_SLATE_DISPLAY_CMD:-}"\n'
        'printf "arg=%s\\n" "$@"\n'
        'exit "${FAKE_UV_EXIT:-0}"\n',
        encoding="utf-8",
    )
    fake_uv.chmod(fake_uv.stat().st_mode | stat.S_IXUSR)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"

    result = subprocess.run(
        [LAUNCHER, "apply", "ci report", "--json"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        cwd=tmp_path,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout.splitlines() == [
        "display=gh slate",
        "arg=run",
        "arg=--frozen",
        "arg=--no-group",
        "arg=dev",
        "arg=--project",
        f"arg={ROOT}",
        "arg=gh-slate",
        "arg=apply",
        "arg=ci report",
        "arg=--json",
    ]

    environment["FAKE_UV_EXIT"] = "23"
    exit_result = subprocess.run(
        [LAUNCHER, "--version"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        cwd=tmp_path,
    )
    assert exit_result.returncode == 23


@pytest.mark.skipif(os.name == "nt", reason="the Bash-based gh extension launcher requires Unix")
def test_extension_launcher_is_executable() -> None:
    assert LAUNCHER.stat().st_mode & stat.S_IXUSR


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("uv") is None,
    reason="the Bash-based extension launcher requires Unix and uv",
)
def test_real_entrypoints_use_their_own_command_spelling() -> None:
    environment = os.environ.copy()
    environment.pop("GH_SLATE_DISPLAY_CMD", None)

    console_help = subprocess.run(
        [shutil.which("uv") or "uv", "run", "--frozen", "gh-slate", "--help"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        cwd=ROOT,
    )
    extension_help = subprocess.run(
        [LAUNCHER, "--help"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        cwd=ROOT,
    )
    console_version = subprocess.run(
        [shutil.which("uv") or "uv", "run", "--frozen", "gh-slate", "--version"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        cwd=ROOT,
    )
    extension_version = subprocess.run(
        [LAUNCHER, "--version"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        cwd=ROOT,
    )

    assert console_help.returncode == extension_help.returncode == 0
    assert console_help.stderr == extension_help.stderr == ""
    assert console_help.stdout.startswith("usage: gh-slate")
    assert extension_help.stdout.startswith("usage: gh slate")
    assert console_version.stdout == "gh-slate 0.1.0\n"
    assert extension_version.stdout == "gh slate 0.1.0\n"

    console_error = subprocess.run(
        [shutil.which("uv") or "uv", "run", "--frozen", "gh-slate", "not-a-command"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        cwd=ROOT,
    )
    extension_error = subprocess.run(
        [LAUNCHER, "not-a-command"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        cwd=ROOT,
    )
    assert console_error.returncode == extension_error.returncode == 2
    assert "usage: gh-slate" in console_error.stderr
    assert "usage: gh slate" in extension_error.stderr


@pytest.mark.skipif(not Path("/bin/bash").exists(), reason="requires /bin/bash")
def test_extension_launcher_reports_missing_uv() -> None:
    environment = os.environ.copy()
    environment["PATH"] = "/usr/bin:/bin"

    result = subprocess.run(
        ["/bin/bash", LAUNCHER],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == ("error: uv is required to run this extension. Install from https://docs.astral.sh/uv/\n")
