from __future__ import annotations

import io
import json
import sys
from typing import TYPE_CHECKING

from gh_slate.cli import run

if TYPE_CHECKING:
    from pathlib import Path


def test_local_jinja_render_writes_only_markdown(
    tmp_path: Path,
    capsys,
) -> None:
    data = tmp_path / "data.json"
    template = tmp_path / "slate.md.j2"
    data.write_text(json.dumps({"status": "passing"}), encoding="utf-8")
    template.write_text("# {{ slate.name }}\n\n{{ data.status }}", encoding="utf-8")

    assert (
        run(
            [
                "render",
                "ci",
                "--data",
                str(data),
                "--template",
                str(template),
            ],
            prog="gh slate",
        )
        == 0
    )
    assert capsys.readouterr().out == "# ci\n\npassing\n"


def test_local_table_render_supports_typed_columns(
    tmp_path: Path,
    capsys,
) -> None:
    data = tmp_path / "data.json"
    data.write_text(
        '{"jobs":[{"name":"linux","passed":true}]}',
        encoding="utf-8",
    )

    assert (
        run(
            [
                "render",
                "ci",
                "--data",
                str(data),
                "--table",
                ".jobs",
                "--columns",
                "name,passed",
            ]
        )
        == 0
    )
    assert capsys.readouterr().out == ("| name | passed |\n| --- | --- |\n| linux | true |\n")


def test_render_rejects_renderer_option_and_stdin_conflicts(capsys) -> None:
    assert run(["render", "ci", "--template", "x", "--columns", "name"]) == 2
    first = capsys.readouterr()
    assert "renderer_option_conflict" in first.err

    assert (
        run(
            [
                "render",
                "ci",
                "--data",
                "-",
                "--template",
                "-",
            ]
        )
        == 2
    )
    second = capsys.readouterr()
    assert "stdin_conflict" in second.err


def test_template_file_and_stdin_reads_are_bounded(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    oversized = tmp_path / "oversized.md.j2"
    oversized.write_text("x" * (64 * 1024 + 1), encoding="utf-8")

    assert run(["render", "ci", "--template", str(oversized)]) == 2
    assert "input_size_limit" in capsys.readouterr().err

    monkeypatch.setattr(sys, "stdin", io.StringIO("x" * (64 * 1024 + 1)))
    assert run(["render", "ci", "--template", "-"]) == 2
    assert "input_size_limit" in capsys.readouterr().err
