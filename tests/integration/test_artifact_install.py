from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from gh_slate import __version__

ROOT = Path(__file__).resolve().parents[2]
ROOT_LAUNCHER = ROOT / "gh-slate"


def _run(
    arguments: list[str | Path],
    *,
    cwd: Path,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(argument) for argument in arguments],
        check=False,
        capture_output=True,
        text=True,
        cwd=cwd,
        env=environment,
        timeout=60,
    )


@pytest.mark.skipif(
    shutil.which("uv") is None,
    reason="artifact build and install contract requires uv",
)
def test_offline_artifact_layout_and_installed_cli_match_the_root_launcher(
    tmp_path: Path,
) -> None:
    uv = shutil.which("uv") or "uv"
    artifacts = tmp_path / "dist"
    environment = os.environ.copy()
    environment["UV_OFFLINE"] = "1"
    environment.pop("GH_SLATE_DISPLAY_CMD", None)

    built = _run(
        [
            uv,
            "build",
            "--offline",
            "--out-dir",
            artifacts,
            ROOT,
        ],
        cwd=tmp_path,
        environment=environment,
    )
    assert built.returncode == 0, built.stderr
    wheels = list(artifacts.glob("gh_slate-*.whl"))
    sdists = list(artifacts.glob("gh_slate-*.tar.gz"))
    assert len(wheels) == len(sdists) == 1
    wheel = wheels[0]
    sdist = sdists[0]

    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        entry_points_name = next(name for name in names if name.endswith(".dist-info/entry_points.txt"))
        entry_points = archive.read(entry_points_name).decode("utf-8")
        assert "gh-slate = gh_slate.__main__:main" in entry_points
        assert "gh_slate/py.typed" in names

    with tarfile.open(sdist, mode="r:gz") as archive:
        names = archive.getnames()
        assert any(name.endswith("/README.md") for name in names)
        assert any(name.endswith("/pyproject.toml") for name in names)
        assert any(name.endswith("/src/gh_slate/__main__.py") for name in names)

    installed = tmp_path / "installed"
    installation = _run(
        [
            uv,
            "pip",
            "install",
            "--offline",
            "--no-deps",
            "--target",
            installed,
            wheel,
        ],
        cwd=tmp_path,
        environment=environment,
    )
    assert installation.returncode == 0, installation.stderr

    neutral = tmp_path / "neutral"
    neutral.mkdir()
    installed_environment = environment.copy()
    installed_environment["PYTHONPATH"] = str(installed)
    installed_script = next(
        (
            candidate
            for candidate in (
                installed / "bin" / "gh-slate",
                installed / "Scripts" / "gh-slate.exe",
                installed / "Scripts" / "gh-slate-script.py",
            )
            if candidate.exists()
        ),
        None,
    )
    if installed_script is None:
        installed_command: list[str | Path] = [
            sys.executable,
            "-m",
            "gh_slate",
        ]
    else:
        if os.name != "nt":
            assert installed_script.stat().st_mode & 0o111
        installed_command = (
            [sys.executable, installed_script] if installed_script.suffix == ".py" else [installed_script]
        )

    installed_version = _run(
        [*installed_command, "--version"],
        cwd=neutral,
        environment=installed_environment,
    )
    assert installed_version.returncode == 0, installed_version.stderr
    assert installed_version.stdout == f"gh-slate {__version__}\n"

    installed_help = _run(
        [*installed_command, "--help"],
        cwd=neutral,
        environment=installed_environment,
    )
    assert installed_help.returncode == 0, installed_help.stderr
    assert installed_help.stdout.startswith("usage: gh-slate")
    assert "{render,apply,view,list,repair,delete,data,schema,state,doctor}" in (installed_help.stdout)

    if os.name == "nt" or shutil.which("bash") is None:
        return

    assert ROOT_LAUNCHER.stat().st_mode & 0o111
    extension_version = _run(
        [ROOT_LAUNCHER, "--version"],
        cwd=neutral,
        environment=environment,
    )
    assert extension_version.returncode == 0, extension_version.stderr
    assert extension_version.stdout == f"gh slate {__version__}\n"
