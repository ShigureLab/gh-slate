from __future__ import annotations

import io
import json
import sys
from hashlib import sha256
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
    template.write_text("# {{ meta.slate.name }}\n\n{{ data.status }}", encoding="utf-8")

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
    template = tmp_path / "table.j2"
    template.write_text('{{ data.jobs | md_table(columns=["name", "passed"]) }}')
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
                "--template",
                str(template),
            ]
        )
        == 0
    )
    assert capsys.readouterr().out == ("| name | passed |\n| --- | --- |\n| linux | true |\n")


def test_render_rejects_renderer_option_and_stdin_conflicts(capsys) -> None:
    assert run(["render", "ci", "--profile", "ci", "--schema", "x"]) == 2
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


def test_local_and_remote_render_options_are_unambiguous(capsys) -> None:
    assert run(["render", "ci", "--data", "/tmp/data.json"]) == 2
    assert "renderer_required" in capsys.readouterr().err

    assert (
        run(
            [
                "render",
                "ci",
                "--target",
                "42",
                "--repo",
                "owner/repo",
                "--template",
                "template.j2",
            ]
        )
        == 2
    )
    assert "renderer_option_conflict" in capsys.readouterr().err


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


def test_local_render_rejects_an_unmaterializable_envelope_without_stdout(
    tmp_path: Path,
    capsys,
) -> None:
    data = tmp_path / "data.json"
    template = tmp_path / "slate.md.j2"
    high_entropy = "".join(sha256(str(index).encode()).hexdigest() for index in range(1600))
    data.write_text(json.dumps({"blob": high_entropy}), encoding="utf-8")
    template.write_text("ok", encoding="utf-8")

    assert run(["render", "ci", "--data", str(data), "--template", str(template)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "codec_size_limit" in captured.err


def test_local_jinja_render_requires_context_for_target_dependent_templates(
    tmp_path: Path,
    capsys,
) -> None:
    template = tmp_path / "slate.md.j2"
    template.write_text("{{ meta.target.url }}", encoding="utf-8")

    assert run(["render", "ci", "--template", str(template)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "jinja_undefined" in captured.err
    assert "apply --dry-run" in captured.err
