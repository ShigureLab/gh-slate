from __future__ import annotations

from gh_slate.errors import ExitCode, GhSlateError, format_error


def test_exit_codes_match_the_cli_contract() -> None:
    assert {member.name: int(member) for member in ExitCode} == {
        "SUCCESS": 0,
        "RUNTIME": 1,
        "VALIDATION": 2,
        "NOT_FOUND": 3,
        "CONFLICT": 4,
    }


def test_structured_error_has_stable_human_and_json_forms() -> None:
    error = GhSlateError(
        "the slate has visible drift",
        code="slate_drift",
        exit_code=ExitCode.CONFLICT,
        hints=("inspect it first", "repair from stored state"),
    )

    assert str(error) == "the slate has visible drift"
    assert int(error.exit_code) == 4
    assert format_error(error) == (
        "error[slate_drift]: the slate has visible drift",
        "hint: inspect it first",
        "hint: repair from stored state",
    )
    assert error.as_dict() == {
        "error": {
            "code": "slate_drift",
            "message": "the slate has visible drift",
            "hints": ["inspect it first", "repair from stored state"],
        }
    }
