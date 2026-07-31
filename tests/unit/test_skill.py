from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = ROOT / "skills" / "gh-slate"
CHECKER = ROOT / "scripts" / "check_skill.py"


def _run(skill_dir: Path = SKILL_DIR) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "GH_TOKEN": "",
        "GITHUB_TOKEN": "",
        "PATH": "",
    }
    return subprocess.run(
        [sys.executable, str(CHECKER), str(skill_dir)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def _copy_skill(tmp_path: Path) -> Path:
    installed = tmp_path / ".codex" / "skills" / "gh-slate"
    shutil.copytree(SKILL_DIR, installed)
    return installed


def _resolver_source() -> str:
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    return text.split("```bash\n", 1)[1].split("```", 1)[0]


def _fake_entrypoints(tmp_path: Path) -> Path:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    gh = fake_bin / "gh"
    gh.write_text(
        "#!/bin/bash\n"
        'if [[ "$1" == slate && "$2" == --version ]]; then\n'
        '  printf "%s\\n" "$FAKE_EXTENSION_OUTPUT"\n'
        '  exit "${FAKE_EXTENSION_EXIT:-0}"\n'
        "fi\n"
        "exit 99\n",
        encoding="utf-8",
    )
    direct = fake_bin / "gh-slate"
    direct.write_text(
        "#!/bin/bash\n"
        'if [[ "$1" == --version ]]; then\n'
        '  printf "%s\\n" "$FAKE_DIRECT_OUTPUT"\n'
        '  exit "${FAKE_DIRECT_EXIT:-0}"\n'
        "fi\n"
        "exit 99\n",
        encoding="utf-8",
    )
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    direct.chmod(direct.stat().st_mode | stat.S_IXUSR)
    return fake_bin


def _run_resolver(
    tmp_path: Path,
    *,
    extension: str,
    direct: str,
) -> subprocess.CompletedProcess[str]:
    fake_bin = _fake_entrypoints(tmp_path)
    environment = {
        **os.environ,
        "FAKE_DIRECT_OUTPUT": direct,
        "FAKE_EXTENSION_OUTPUT": extension,
        "PATH": f"{fake_bin}{os.pathsep}/usr/bin:/bin",
    }
    return subprocess.run(
        ["/bin/bash", "-c", _resolver_source()],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def test_bundled_skill_layout_frontmatter_and_parser_contract() -> None:
    result = _run()

    assert result.returncode == 0, result.stderr
    assert result.stdout == ("Checked gh-slate skill: 18 recipes, 14 help routes, 25 flags, 2 prefixes\n")


def test_skill_remains_valid_after_a_plain_install_copy(
    tmp_path: Path,
) -> None:
    installed = _copy_skill(tmp_path)

    result = _run(installed)

    assert result.returncode == 0, result.stderr
    assert (installed / "SKILL.md").is_file()


def test_skill_check_never_needs_credentials_or_external_commands() -> None:
    result = _run()

    assert result.returncode == 0, result.stderr
    assert "2 prefixes" in result.stdout


@pytest.mark.skipif(os.name == "nt" or not Path("/bin/bash").is_file(), reason="resolver recipes require Bash")
def test_skill_resolver_falls_back_from_an_old_extension(tmp_path: Path) -> None:
    result = _run_resolver(
        tmp_path,
        extension="gh slate 0.0.9",
        direct="gh-slate 0.1.0",
    )

    assert result.returncode == 0
    assert result.stdout == "gh-slate 0.1.0\n"
    assert result.stderr == ""


@pytest.mark.skipif(os.name == "nt" or not Path("/bin/bash").is_file(), reason="resolver recipes require Bash")
def test_skill_resolver_falls_back_from_a_malformed_extension(tmp_path: Path) -> None:
    result = _run_resolver(
        tmp_path,
        extension="not a gh-slate version",
        direct="gh-slate 0.2.0",
    )

    assert result.returncode == 0
    assert result.stdout == "gh-slate 0.2.0\n"
    assert result.stderr == ""


@pytest.mark.skipif(os.name == "nt" or not Path("/bin/bash").is_file(), reason="resolver recipes require Bash")
def test_skill_resolver_prefers_a_compatible_extension(tmp_path: Path) -> None:
    result = _run_resolver(
        tmp_path,
        extension="gh slate 0.1.0",
        direct="gh-slate 9.9.9",
    )

    assert result.returncode == 0
    assert result.stdout == "gh slate 0.1.0\n"
    assert result.stderr == ""


@pytest.mark.skipif(os.name == "nt" or not Path("/bin/bash").is_file(), reason="resolver recipes require Bash")
@pytest.mark.parametrize(
    ("extension", "expected"),
    [
        ("gh slate 0.1.0.post1", "gh slate 0.1.0.post1\n"),
        ("gh slate 0.1.0.post1.dev1", "gh slate 0.1.0.post1.dev1\n"),
        ("gh slate 0.1.0-1.dev1", "gh slate 0.1.0-1.dev1\n"),
        ("gh slate 0.1.0.dev1", "gh-slate 0.1.0\n"),
        ("gh slate 0.1.0rc1", "gh-slate 0.1.0\n"),
        ("gh slate 0.1.0.rc1", "gh-slate 0.1.0\n"),
        ("gh slate 0.2.0.dev1", "gh slate 0.2.0.dev1\n"),
        ("gh slate 0.2.0rc1", "gh slate 0.2.0rc1\n"),
        ("gh slate 0.2.0+linux.arm64", "gh slate 0.2.0+linux.arm64\n"),
        ("gh slate 0.2.0.foo", "gh-slate 0.1.0\n"),
        ("gh slate 0.2.0+bad..local", "gh-slate 0.1.0\n"),
    ],
)
def test_skill_resolver_compares_pep440_release_suffixes(
    tmp_path: Path,
    extension: str,
    expected: str,
) -> None:
    result = _run_resolver(
        tmp_path,
        extension=extension,
        direct="gh-slate 0.1.0",
    )

    assert result.returncode == 0
    assert result.stdout == expected
    assert result.stderr == ""


@pytest.mark.skipif(os.name == "nt" or not Path("/bin/bash").is_file(), reason="resolver recipes require Bash")
def test_skill_resolver_rejects_two_old_candidates(tmp_path: Path) -> None:
    result = _run_resolver(
        tmp_path,
        extension="gh slate 0.0.9",
        direct="gh-slate 0.0.99",
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "Install gh-slate >=0.1.0 before continuing.\n"


def test_skill_check_rejects_a_literal_follow_up_prefix(
    tmp_path: Path,
) -> None:
    installed = _copy_skill(tmp_path)
    skill_file = installed / "SKILL.md"
    skill_file.write_text(
        skill_file.read_text(encoding="utf-8").replace(
            '"${GH_SLATE[@]}" doctor --json',
            "gh slate doctor --json",
            1,
        ),
        encoding="utf-8",
    )

    result = _run(installed)

    assert result.returncode == 1
    assert "follow-up commands must use the resolved GH_SLATE array" in result.stderr


def test_skill_check_rejects_exit_status_only_entrypoint_probes(tmp_path: Path) -> None:
    installed = _copy_skill(tmp_path)
    skill_file = installed / "SKILL.md"
    skill_file.write_text(
        skill_file.read_text(encoding="utf-8").replace(
            'if gh_slate_compatible "gh slate" gh slate; then',
            "if gh slate --version >/dev/null 2>&1; then",
            1,
        ),
        encoding="utf-8",
    )

    result = _run(installed)

    assert result.returncode == 1
    assert "entrypoint probe is incomplete" in result.stderr


def test_skill_check_rejects_a_removed_flag(
    tmp_path: Path,
) -> None:
    installed = _copy_skill(tmp_path)
    skill_file = installed / "SKILL.md"
    skill_file.write_text(
        skill_file.read_text(encoding="utf-8").replace(
            "--dry-run",
            "--force",
        ),
        encoding="utf-8",
    )

    result = _run(installed)

    assert result.returncode == 1
    assert "flags absent from the current parser: ['--force']" in result.stderr


def test_skill_check_rejects_an_unexpected_layout_entry(
    tmp_path: Path,
) -> None:
    installed = _copy_skill(tmp_path)
    (installed / "notes.md").write_text("not part of the skill\n", encoding="utf-8")

    result = _run(installed)

    assert result.returncode == 1
    assert "unexpected top-level skill entries: ['notes.md']" in result.stderr


def test_skill_check_rejects_a_stale_minimum_version(
    tmp_path: Path,
) -> None:
    installed = _copy_skill(tmp_path)
    skill_file = installed / "SKILL.md"
    skill_file.write_text(
        skill_file.read_text(encoding="utf-8").replace(
            "minimum-gh-slate-version: 0.1.0",
            "minimum-gh-slate-version: 9.9.9",
        ),
        encoding="utf-8",
    )

    result = _run(installed)

    assert result.returncode == 1
    assert "metadata minimum version must be 0.1.0" in result.stderr
