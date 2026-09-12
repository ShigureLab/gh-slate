from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from gh_slate.cli import run
from gh_slate.codec import (
    ControllerV1,
    MetaSnapshot,
    RendererDescriptorV1,
    StateV1,
    encode_comment,
    render_sha256,
)
from gh_slate.commands import read as read_commands
from gh_slate.github.process import GhProcess, ProcessResult
from gh_slate.rendering import (
    MaterializedComment,
    SlateContext,
    jinja_descriptor,
    materialize_comment,
)

if TYPE_CHECKING:
    import pytest


HOST = "github.example"
REPOSITORY = "owner/repo"
NUMBER = 42
TARGET_URL = f"https://{HOST}/{REPOSITORY}/issues/{NUMBER}"
ACTOR = "ci-bot"
ACTOR_ID = 101
OTHER_ACTOR_ID = 202
EMPTY_HASH = "0" * 64

ProcessCall = tuple[tuple[str, ...], float, str | None, int, int]


@dataclass(slots=True)
class RecordingRunner:
    pages: list[list[dict[str, object]]]
    actor: str = ACTOR
    calls: list[ProcessCall] = field(default_factory=list)

    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout: float,
        hostname: str | None,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> ProcessResult:
        self.calls.append(
            (
                argv,
                timeout,
                hostname,
                max_stdout_bytes,
                max_stderr_bytes,
            )
        )
        if argv == ("gh", "version"):
            return _success("gh version 2.96.0\n")
        if argv[1:3] == ("auth", "status"):
            return _json_success(
                {
                    "hosts": {
                        HOST: {
                            "state": "logged_in",
                        }
                    }
                }
            )
        if argv[1:3] == ("repo", "view"):
            return _json_success(
                {
                    "nameWithOwner": REPOSITORY,
                    "url": f"https://{HOST}/{REPOSITORY}",
                    "isPrivate": False,
                }
            )
        if argv[1:3] == ("pr", "view"):
            return _json_success(
                {
                    "number": NUMBER,
                    "url": f"https://{HOST}/{REPOSITORY}/pull/{NUMBER}",
                }
            )
        if len(argv) > 1 and argv[1] == "api":
            endpoint = argv[-1]
            if endpoint == "user":
                return _json_success({"id": ACTOR_ID, "login": self.actor})
            if endpoint.casefold() == (f"repos/{REPOSITORY}/issues/{NUMBER}/comments?per_page=100".casefold()):
                return _json_success(self.pages)
        raise AssertionError(f"unexpected gh invocation: {argv!r}")


def _success(stdout: str) -> ProcessResult:
    return ProcessResult(
        returncode=0,
        stdout=stdout.encode(),
        stderr=b"",
    )


def _json_success(value: object) -> ProcessResult:
    return _success(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _install_process(
    monkeypatch: pytest.MonkeyPatch,
    pages: list[list[dict[str, object]]],
) -> RecordingRunner:
    runner = RecordingRunner(pages=pages)
    process = GhProcess(runner=runner)
    monkeypatch.setattr(read_commands, "_new_process", lambda: process)
    return runner


def _materialized(
    name: str = "ci",
    *,
    status: str = "ready",
    target_url: str = TARGET_URL,
) -> MaterializedComment:
    state = StateV1(
        name=name,
        format="gh-slate/state-v2",
        meta=MetaSnapshot.from_json(
            {
                "host": HOST,
                "repository": {
                    "owner": "owner",
                    "name": "repo",
                    "full_name": REPOSITORY,
                    "url": f"https://{HOST}/{REPOSITORY}",
                },
                "target": {
                    "kind": "pull_request" if "/pull/" in target_url else "issue",
                    "number": NUMBER,
                    "id": "I_example",
                    "url": target_url,
                },
                "slate": {"name": name},
            }
        ),
        revision=7,
        controller=ControllerV1(login=ACTOR, id=ACTOR_ID),
        data={"status": status},
        renderer=jinja_descriptor(
            "# {{ meta.slate.name }}\n\n"
            "{{ meta.repository.full_name }}#{{ meta.target.number }}\n\n"
            "{{ meta.target.url }}\n\n"
            "status={{ data.status }}"
        ),
        render_sha256=EMPTY_HASH,
    )
    return materialize_comment(
        state,
        slate=SlateContext(
            name=name,
            repository=REPOSITORY,
            number=NUMBER,
            url=target_url,
        ),
    )


def _comment(
    identifier: int,
    body: str,
    *,
    author: str = ACTOR,
    author_id: int = ACTOR_ID,
    target_url: str = TARGET_URL,
) -> dict[str, object]:
    return {
        "id": identifier,
        "body": body,
        "html_url": f"{target_url}#issuecomment-{identifier}",
        "user": {"id": author_id, "login": author},
        "created_at": "2026-07-31T00:00:00Z",
        "updated_at": "2026-07-31T00:01:00Z",
    }


def _drifted(materialized: MaterializedComment) -> str:
    return materialized.encoded.body.replace(
        f"status={materialized.state.data['status']}\n",
        "status=manually-edited\n",
        1,
    )


def _corrupt(materialized: MaterializedComment) -> str:
    return materialized.encoded.body.replace(
        f"state={materialized.encoded.state_sha256}",
        f"state={EMPTY_HASH}",
        1,
    )


def _assert_only_read_calls(runner: RecordingRunner) -> None:
    assert runner.calls
    for argv, timeout, hostname, max_stdout_bytes, max_stderr_bytes in runner.calls:
        assert argv[0] == "gh"
        assert timeout > 0
        assert max_stdout_bytes > 0
        assert max_stderr_bytes > 0
        if len(argv) > 1 and argv[1] == "api":
            method_index = argv.index("--method")
            assert argv[method_index + 1] == "GET"
            assert "--hostname" in argv
            assert argv[argv.index("--hostname") + 1] == hostname == HOST
            assert not {"POST", "PATCH", "DELETE", "PUT"}.intersection(argv)
            assert not {"--input", "-f", "--raw-field", "-F", "--field"}.intersection(argv)
            continue
        assert argv == ("gh", "version") or argv[1:3] in {
            ("auth", "status"),
            ("pr", "view"),
            ("repo", "view"),
        }
        assert not {"create", "edit", "delete", "close", "merge"}.intersection(argv)


def test_view_default_json_and_web_are_read_only(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    managed = _materialized()
    comment = _comment(101, managed.encoded.body)
    runner = _install_process(monkeypatch, [[comment]])

    assert run(["view", "ci", "--target", TARGET_URL], prog="gh slate") == 0
    output = capsys.readouterr()
    assert output.out == managed.rendered.markdown
    assert output.err == ""

    assert run(["view", "ci", "--target", TARGET_URL, "--json"], prog="gh slate") == 0
    output = capsys.readouterr()
    record = json.loads(output.out)
    assert record == {
        "actual_render_sha256": managed.rendered.render_sha256,
        "comment_id": 101,
        "controller": ACTOR,
        "host": HOST,
        "name": "ci",
        "number": NUMBER,
        "render_sha256": managed.rendered.render_sha256,
        "renderer": {
            "kind": "jinja",
            "version": 2,
        },
        "repository": REPOSITORY,
        "revision": 7,
        "schema": False,
        "profile": None,
        "view": None,
        "data": {"status": "ready"},
        "meta": managed.state.to_json()["meta"],
        "state_sha256": managed.encoded.state_sha256,
        "status": "valid",
        "url": comment["html_url"],
    }
    assert output.err == ""

    opened: list[str] = []

    def open_browser(url: str) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(read_commands.webbrowser, "open", open_browser)
    assert run(["view", "ci", "--target", TARGET_URL, "--web"], prog="gh slate") == 0
    output = capsys.readouterr()
    assert output.out == f"opened {comment['html_url']}\n"
    assert output.err == ""
    assert opened == [comment["html_url"]]
    _assert_only_read_calls(runner)


def test_list_classifies_valid_drift_corrupt_and_duplicate_comments(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    valid = _materialized("ci")
    drifted = _materialized("drift")
    corrupt = _materialized("broken")
    duplicate = _materialized("dupe")
    forged = _materialized("forged")
    runner = _install_process(
        monkeypatch,
        [
            [
                _comment(101, valid.encoded.body),
                _comment(102, _drifted(drifted)),
                _comment(104, duplicate.encoded.body),
                _comment(
                    106,
                    forged.encoded.body,
                    author="attacker",
                    author_id=OTHER_ACTOR_ID,
                ),
            ],
            [
                _comment(103, _corrupt(corrupt)),
                _comment(105, duplicate.encoded.body),
            ],
        ],
    )

    assert run(["list", "--target", TARGET_URL, "--json"], prog="gh slate") == 0
    output = capsys.readouterr()
    records = {record["name"]: record for record in json.loads(output.out)}

    assert output.err == ""
    assert set(records) == {"broken", "ci", "drift", "dupe"}
    assert records["ci"]["status"] == "valid"
    assert records["drift"]["status"] == "drifted"
    assert records["broken"]["status"] == "corrupt"
    assert records["broken"]["error_code"] == "state_hash_mismatch"
    assert records["dupe"]["comment_ids"] == [104, 105]
    assert records["dupe"]["status"] == "duplicate"
    assert [match["status"] for match in records["dupe"]["matches"]] == ["valid", "valid"]
    comments_call = runner.calls[-1][0]
    assert comments_call[1:] == (
        "api",
        "--hostname",
        HOST,
        "--method",
        "GET",
        "--paginate",
        "--slurp",
        f"repos/{REPOSITORY}/issues/{NUMBER}/comments?per_page=100",
    )
    _assert_only_read_calls(runner)


def test_remote_render_uses_canonical_state_and_target_context_during_drift(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    managed = _materialized()
    runner = _install_process(
        monkeypatch,
        [[_comment(101, _drifted(managed))]],
    )

    assert run(["render", "ci", "--target", TARGET_URL], prog="gh slate") == 0
    output = capsys.readouterr()
    assert output.out == managed.rendered.markdown
    assert "warning[render_drift]" in output.err
    assert "rendered canonical state" in output.err
    _assert_only_read_calls(runner)


def test_remote_render_normalizes_equivalent_pull_request_targets(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pull_url = f"https://{HOST}/{REPOSITORY}/pull/{NUMBER}"
    managed = _materialized(target_url=pull_url)
    runner = _install_process(
        monkeypatch,
        [[_comment(101, managed.encoded.body, target_url=pull_url)]],
    )

    invocations = [
        [
            "render",
            "ci",
            "--target",
            str(NUMBER),
            "--repo",
            REPOSITORY.upper(),
            "--host",
            HOST,
        ],
        ["render", "ci", "--target", pull_url],
        ["render", "ci", "--target", "@pr", "--host", HOST],
    ]
    for invocation in invocations:
        assert run(invocation, prog="gh slate") == 0
        output = capsys.readouterr()
        assert output.out == managed.rendered.markdown
        assert output.err == ""

    _assert_only_read_calls(runner)


def test_state_export_and_verify_preserve_canonical_state_and_report_drift(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    managed = _materialized()
    clean = _comment(101, managed.encoded.body)
    drifted = _comment(101, _drifted(managed))
    runner = _install_process(monkeypatch, [[drifted]])

    assert run(["state", "export", "ci", "--target", TARGET_URL], prog="gh slate") == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == managed.state.to_json()
    assert "warning[render_drift]" in output.err
    assert "exported canonical state" in output.err

    runner.pages = [[clean]]
    assert run(["state", "verify", "ci", "--target", TARGET_URL], prog="gh slate") == 0
    output = capsys.readouterr()
    assert output.out == f"verified (state_and_render) ci revision=7 state={managed.encoded.state_sha256}\n"
    assert output.err == ""

    runner.pages = [[drifted]]
    assert run(["state", "verify", "ci", "--target", TARGET_URL], prog="gh slate") == 4
    output = capsys.readouterr()
    assert output.out == ""
    assert "error[render_drift]" in output.err

    assert run(["state", "verify", "ci", "--target", TARGET_URL, "--json"], prog="gh slate") == 4
    output = capsys.readouterr()
    record = json.loads(output.out)
    assert record["status"] == "drifted"
    assert record["verified"] is False
    assert record["render_sha256"] == managed.rendered.render_sha256
    assert record["actual_render_sha256"] != managed.rendered.render_sha256
    assert output.err == ""
    _assert_only_read_calls(runner)


def test_doctor_checks_host_auth_actor_and_local_runtime_without_writes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = _install_process(monkeypatch, [])

    assert run(["doctor", "--host", HOST], prog="gh slate") == 0
    output = capsys.readouterr()
    assert output.out == (
        f"ok gh: gh version 2.96.0\nok auth: {ACTOR} ({ACTOR_ID})\nok Jinja, JSON Schema draft 2020-12\n"
    )
    assert output.err == ""

    assert run(["doctor", "--host", HOST, "--json"], prog="gh slate") == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {
        "actor": ACTOR,
        "actor_id": ACTOR_ID,
        "gh": "gh version 2.96.0",
        "host": HOST,
        "jinja": "ok",
        "json_schema": "draft-2020-12",
        "ok": True,
    }
    assert output.err == ""

    monkeypatch.setenv("GITHUB_SERVER_URL", f"https://{HOST}")
    assert run(["doctor", "--json"], prog="gh slate") == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["host"] == HOST
    assert output.err == ""

    assert [call[0] for call in runner.calls] == [
        ("gh", "version"),
        ("gh", "auth", "status", "--active", "--hostname", HOST),
        ("gh", "api", "--hostname", HOST, "--method", "GET", "user"),
        ("gh", "version"),
        ("gh", "auth", "status", "--active", "--hostname", HOST),
        ("gh", "api", "--hostname", HOST, "--method", "GET", "user"),
        ("gh", "version"),
        ("gh", "auth", "status", "--active", "--hostname", HOST),
        ("gh", "api", "--hostname", HOST, "--method", "GET", "user"),
    ]
    _assert_only_read_calls(runner)


def test_unknown_renderer_remains_exportable_but_cannot_rerender(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    visible = "# Stored by a future renderer\n"
    state = StateV1(
        name="future",
        revision=1,
        controller=ControllerV1(login=ACTOR, id=ACTOR_ID),
        data={"status": "ready"},
        renderer=RendererDescriptorV1(
            kind="future-dashboard",
            version=9,
            config={"layout": "v9"},
        ),
        render_sha256=render_sha256(visible),
    )
    encoded = encode_comment(state, visible)
    runner = _install_process(
        monkeypatch,
        [[_comment(201, encoded.body)]],
    )

    assert run(["view", "future", "--target", TARGET_URL]) == 0
    assert capsys.readouterr().out == visible
    assert run(["state", "export", "future", "--target", TARGET_URL]) == 0
    assert json.loads(capsys.readouterr().out) == state.to_json()

    assert run(["render", "future", "--target", TARGET_URL]) == 2
    assert "state_migration_required" in capsys.readouterr().err
    assert run(["state", "verify", "future", "--target", TARGET_URL, "--json"]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["verification_scope"] == "envelope"
    assert verified["migration_required"] is True
    _assert_only_read_calls(runner)
