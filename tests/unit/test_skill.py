from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

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


def test_bundled_skill_layout_frontmatter_and_parser_contract() -> None:
    result = _run()

    assert result.returncode == 0, result.stderr


def test_skill_remains_valid_after_a_plain_install_copy(
    tmp_path: Path,
) -> None:
    installed = _copy_skill(tmp_path)

    result = _run(installed)

    assert result.returncode == 0, result.stderr
    assert (installed / "SKILL.md").is_file()


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


def test_skill_recipes_accept_the_direct_python_entrypoint(tmp_path: Path) -> None:
    installed = _copy_skill(tmp_path)
    skill_file = installed / "SKILL.md"
    skill_file.write_text(
        skill_file.read_text(encoding="utf-8").replace("gh slate ", "gh-slate "),
        encoding="utf-8",
    )

    result = _run(installed)

    assert result.returncode == 0, result.stderr


def test_skill_check_rejects_an_unknown_command(tmp_path: Path) -> None:
    installed = _copy_skill(tmp_path)
    skill_file = installed / "SKILL.md"
    skill_file.write_text(
        skill_file.read_text(encoding="utf-8").replace("gh slate list ", "gh slate missing ", 1),
        encoding="utf-8",
    )

    result = _run(installed)

    assert result.returncode == 1
    assert "recipe does not name a current command" in result.stderr
