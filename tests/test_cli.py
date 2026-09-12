from __future__ import annotations

import json

import pytest

from gh_slate.cli import build_parser, run
from gh_slate.errors import ExitCode, GhSlateError
from gh_slate.invocation import DISPLAY_COMMAND_ENV


def test_root_command_prints_help(capsys) -> None:
    assert run([], prog="gh-slate") == 0

    output = capsys.readouterr()
    assert output.err == ""
    assert output.out.startswith("usage: gh-slate")
    assert "dashboard comments" in output.out


def test_extension_display_command_is_used_in_help(monkeypatch, capsys) -> None:
    monkeypatch.setenv(DISPLAY_COMMAND_ENV, "gh slate")

    assert run([]) == 0

    assert capsys.readouterr().out.startswith("usage: gh slate")


def test_unknown_command_is_usage_error() -> None:
    parser = build_parser(prog="gh-slate")

    with pytest.raises(SystemExit) as exit_info:
        parser.parse_args(["unknown"])

    assert exit_info.value.code == 2


def test_json_business_error_is_canonical_json_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from gh_slate.commands import recovery as recovery_commands

    def fail(_args: object) -> int:
        raise GhSlateError(
            "stored revision is stale",
            code="revision_conflict",
            exit_code=ExitCode.CONFLICT,
            details={"expected": 7, "actual": 8},
            hints=("read the slate and retry intentionally",),
        )

    monkeypatch.setattr(recovery_commands, "run_repair", fail)

    assert run(
        [
            "repair",
            "ci",
            "--from-state",
            "--json",
        ],
        prog="gh slate",
    ) == int(ExitCode.CONFLICT)

    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == (
        '{"error":{"code":"revision_conflict",'
        '"details":{"actual":8,"expected":7},'
        '"hints":["read the slate and retry intentionally"],'
        '"message":"stored revision is stale"}}\n'
    )


def test_json_error_normalizes_finite_float_details(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from gh_slate.commands import recovery as recovery_commands

    def fail(_args: object) -> int:
        raise GhSlateError(
            "rate limited",
            code="rate_limited",
            details={
                "retry_after": 1.25,
                "samples": [0.5, 1e100],
            },
        )

    monkeypatch.setattr(recovery_commands, "run_repair", fail)

    assert run(
        ["repair", "ci", "--from-state", "--json"],
        prog="gh slate",
    ) == int(ExitCode.RUNTIME)

    payload = json.loads(capsys.readouterr().err)
    assert payload == {
        "error": {
            "code": "rate_limited",
            "details": {
                "retry_after": 1.25,
                "samples": [0.5, 1e100],
            },
            "message": "rate limited",
        }
    }


@pytest.mark.parametrize(
    "bad_detail",
    [
        object(),
        float("nan"),
        float("inf"),
    ],
)
def test_json_error_falls_back_when_details_are_not_json_safe(
    bad_detail: object,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from gh_slate.commands import recovery as recovery_commands

    def fail(_args: object) -> int:
        raise GhSlateError(
            "original failure",
            code="original_code",
            details={"bad": bad_detail},
            hints=("inspect the slate",),
        )

    monkeypatch.setattr(recovery_commands, "run_repair", fail)

    assert run(
        ["repair", "ci", "--from-state", "--json"],
        prog="gh slate",
    ) == int(ExitCode.RUNTIME)

    assert json.loads(capsys.readouterr().err) == {
        "error": {
            "code": "original_code",
            "hints": ["inspect the slate"],
            "message": "original failure",
        }
    }


def test_json_error_falls_back_for_cyclic_details(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from gh_slate.commands import recovery as recovery_commands

    cycle: list[object] = []
    cycle.append(cycle)

    def fail(_args: object) -> int:
        raise GhSlateError(
            "cyclic failure",
            code="cycle",
            details={"cycle": cycle},
        )

    monkeypatch.setattr(recovery_commands, "run_repair", fail)

    assert run(
        ["repair", "ci", "--from-state", "--json"],
        prog="gh slate",
    ) == int(ExitCode.RUNTIME)
    assert json.loads(capsys.readouterr().err) == {
        "error": {
            "code": "cycle",
            "message": "cyclic failure",
        }
    }


def test_human_business_error_format_is_unchanged_without_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from gh_slate.commands import recovery as recovery_commands

    def fail(_args: object) -> int:
        raise GhSlateError(
            "stored revision is stale",
            code="revision_conflict",
            exit_code=ExitCode.CONFLICT,
            hints=("inspect the slate",),
        )

    monkeypatch.setattr(recovery_commands, "run_repair", fail)

    assert run(
        [
            "repair",
            "ci",
            "--from-state",
        ],
        prog="gh slate",
    ) == int(ExitCode.CONFLICT)

    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == ("error[revision_conflict]: stored revision is stale\nhint: inspect the slate\n")


def test_apply_command_surface_parses_full_snapshot_options() -> None:
    parsed = build_parser(prog="gh slate").parse_args(
        [
            "apply",
            "ci",
            "--target",
            "42",
            "--repo",
            "owner/repo",
            "--controller",
            "ci-bot",
            "--host",
            "ghe.example",
            "--if-revision",
            "7",
            "--mode",
            "update",
            "--dry-run",
            "--json",
            "--data",
            "data.json",
            "--schema",
            "schema.json",
            "--template",
            "template.j2",
        ]
    )

    assert callable(parsed.handler)
    assert parsed.name == "ci"
    assert parsed.target == "42"
    assert parsed.repo == "owner/repo"
    assert parsed.controller == "ci-bot"
    assert parsed.host == "ghe.example"
    assert parsed.if_revision == 7
    assert parsed.mode == "update"
    assert parsed.dry_run is True
    assert parsed.json is True
    assert parsed.quiet is False
    assert parsed.data == "data.json"
    assert parsed.schema == "schema.json"
    assert parsed.template == "template.j2"


@pytest.mark.parametrize(
    "revision",
    ["0", "-1", str(2**63), "not-an-integer"],
)
def test_apply_if_revision_must_be_a_positive_integer(revision: str) -> None:
    parser = build_parser(prog="gh slate")

    with pytest.raises(SystemExit) as exit_info:
        parser.parse_args(["apply", "ci", "--if-revision", revision])

    assert exit_info.value.code == 2


@pytest.mark.parametrize(
    "options",
    [
        ["--template", "slate.j2", "--profile", "ci"],
        ["--data", "data.json", "--patch", "patch.json"],
        ["--json", "--quiet"],
    ],
)
def test_apply_renderer_and_output_modes_are_mutually_exclusive(
    options: list[str],
) -> None:
    parser = build_parser(prog="gh slate")

    with pytest.raises(SystemExit) as exit_info:
        parser.parse_args(["apply", "ci", *options])

    assert exit_info.value.code == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["render", "ci", "--target", "42", "--repo", "owner/repo"],
        ["view", "ci", "--target", "42", "--repo", "owner/repo", "--json"],
        ["list", "--target", "@pr", "--host", "ghe.example", "--json"],
        ["state", "export", "ci", "--target", "42", "-R", "owner/repo"],
        ["state", "verify", "ci", "--target", "@event", "--json"],
        ["doctor", "--host", "ghe.example", "--json"],
    ],
)
def test_read_only_command_surface_is_registered(argv: list[str]) -> None:
    parsed = build_parser(prog="gh slate").parse_args(argv)

    assert callable(parsed.handler)


@pytest.mark.parametrize(
    "argv",
    [
        [
            "repair",
            "ci",
            "--from-state",
            "--target",
            "42",
            "--repo",
            "owner/repo",
            "--if-revision",
            "7",
            "--json",
        ],
        [
            "delete",
            "ci",
            "--target",
            "42",
            "--repo",
            "owner/repo",
            "--confirm",
            "ci",
            "--quiet",
        ],
        [
            "delete",
            "ci",
            "--target",
            "42",
            "--yes",
        ],
    ],
)
def test_recovery_command_surfaces_are_registered(argv: list[str]) -> None:
    parsed = build_parser(prog="gh slate").parse_args(argv)

    assert callable(parsed.handler)


@pytest.mark.parametrize(
    "argv",
    [
        ["repair", "ci"],
        ["delete", "ci"],
        ["delete", "ci", "--confirm", "ci", "--yes"],
        ["repair", "ci", "--from-state", "--json", "--quiet"],
        ["delete", "ci", "--yes", "--json", "--quiet"],
    ],
)
def test_recovery_requires_explicit_source_confirmation_and_output_mode(
    argv: list[str],
) -> None:
    parser = build_parser(prog="gh slate")

    with pytest.raises(SystemExit) as exit_info:
        parser.parse_args(argv)

    assert exit_info.value.code == 2


def test_view_json_and_web_are_mutually_exclusive() -> None:
    parser = build_parser(prog="gh slate")

    with pytest.raises(SystemExit) as exit_info:
        parser.parse_args(
            [
                "view",
                "ci",
                "--target",
                "42",
                "--json",
                "--web",
            ]
        )

    assert exit_info.value.code == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["data", "get", "ci"],
        ["schema", "infer", "ci"],
        ["apply", "ci", "--table", "."],
        ["render", "ci", "--list", "."],
    ],
)
def test_unknown_commands_and_options_are_usage_errors(argv):
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(argv)
    assert error.value.code == 2
