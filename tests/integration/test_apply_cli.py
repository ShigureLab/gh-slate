from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from gh_slate.cli import run
from gh_slate.commands import apply as apply_commands, read as read_commands
from gh_slate.github.process import GhProcess, ProcessResult
from gh_slate.github.write import GhWriteProcess

if TYPE_CHECKING:
    from pathlib import Path


HOST = "github.example"
REPOSITORY = "owner/repo"
NUMBER = 42
ACTOR = "ci-bot"
ACTOR_ID = 101


def _result(value: object) -> ProcessResult:
    return ProcessResult(
        returncode=0,
        stdout=json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode(),
        stderr=b"",
    )


@dataclass(slots=True)
class FakeGhBackend:
    page_url: str
    comments: list[dict[str, object]] = field(default_factory=list)
    events: list[tuple[str, str, str | None]] = field(default_factory=list)
    next_identifier: int = 100

    @property
    def comments_endpoint(self) -> str:
        return f"repos/{REPOSITORY}/issues/{NUMBER}/comments?per_page=100"

    def get(
        self,
        endpoint: str,
        hostname: str | None,
        *,
        paginate: bool,
    ) -> ProcessResult:
        self.events.append(("GET", endpoint, hostname))
        if endpoint == "user":
            return _result({"id": ACTOR_ID, "login": ACTOR})
        if endpoint == f"repos/{REPOSITORY}/issues/{NUMBER}":
            return _result({"html_url": self.page_url, "node_id": "I_target"})
        if endpoint == self.comments_endpoint:
            pages: object = [self.comments] if paginate else self.comments
            return _result(pages)
        raise AssertionError(f"unexpected GET endpoint: {endpoint}")

    def write(
        self,
        method: str,
        endpoint: str,
        stdin: bytes,
        hostname: str | None,
    ) -> ProcessResult:
        self.events.append((method, endpoint, hostname))
        payload = json.loads(stdin)
        body = payload["body"]
        assert isinstance(body, str)
        if method == "POST":
            assert endpoint == (f"repos/{REPOSITORY}/issues/{NUMBER}/comments")
            identifier = self.next_identifier
            self.next_identifier += 1
            record: dict[str, object] = {
                "id": identifier,
                "body": body,
                "html_url": (f"{self.page_url}#issuecomment-{identifier}"),
                "user": {"id": ACTOR_ID, "login": ACTOR},
                "created_at": "2026-07-31T00:00:00Z",
                "updated_at": "2026-07-31T00:00:00Z",
            }
            self.comments.append(record)
            return _result(record)

        assert method == "PATCH"
        identifier = int(endpoint.rsplit("/", 1)[1])
        for record in self.comments:
            if record["id"] == identifier:
                record["body"] = body
                record["updated_at"] = "2026-07-31T00:01:00Z"
                return _result(record)
        raise AssertionError("PATCH selected a missing comment")


@dataclass(slots=True)
class ReadRunner:
    backend: FakeGhBackend

    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout: float,
        hostname: str | None,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> ProcessResult:
        assert timeout > 0
        assert max_stdout_bytes > 0
        assert max_stderr_bytes > 0
        assert argv[0:2] == ("gh", "api")
        assert argv[argv.index("--method") + 1] == "GET"
        return self.backend.get(
            argv[-1],
            hostname,
            paginate="--paginate" in argv,
        )


@dataclass(slots=True)
class WriteRunner:
    backend: FakeGhBackend

    def run(
        self,
        argv: tuple[str, ...],
        *,
        stdin: bytes,
        timeout: float,
        hostname: str | None,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> ProcessResult:
        assert timeout > 0
        assert max_stdout_bytes > 0
        assert max_stderr_bytes > 0
        assert argv[0:2] == ("gh", "api")
        assert argv[argv.index("--input") + 1] == "-"
        method = argv[argv.index("--method") + 1]
        assert method in {"POST", "PATCH"}
        return self.backend.write(
            method,
            argv[-1],
            stdin,
            hostname,
        )


@pytest.mark.parametrize("kind", ["issues", "pull"])
def test_cli_to_gh_contract_create_update_unchanged_and_readback(
    kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    page_url = f"https://{HOST}/{REPOSITORY}/{kind}/{NUMBER}"
    backend = FakeGhBackend(page_url=page_url)
    reader = GhProcess(runner=ReadRunner(backend))
    writer = GhWriteProcess(runner=WriteRunner(backend))
    transaction = apply_commands._CoreTransaction(
        reader=reader,
        writer=writer,
    )
    monkeypatch.setattr(
        apply_commands,
        "_new_transaction",
        lambda: transaction,
    )
    monkeypatch.setattr(
        read_commands,
        "_new_process",
        lambda: reader,
    )

    data = tmp_path / "data.json"
    data.write_text(
        '{"jobs":[{"name":"linux","status":"pass"}]}',
        encoding="utf-8",
    )
    target = page_url
    create = [
        "apply",
        "ci",
        "--target",
        target,
        "--mode",
        "create",
        "--data",
        str(data),
        "--table",
        ".jobs",
        "--columns",
        "name,status",
        "--json",
    ]
    assert run(create, prog="gh slate") == 0
    created = json.loads(capsys.readouterr().out)
    assert created["action"] == "created"
    assert created["revision"] == 1

    data.write_text(
        '{"jobs":[{"name":"linux","status":"fail"}]}',
        encoding="utf-8",
    )
    update = [
        "apply",
        "ci",
        "--target",
        target,
        "--mode",
        "update",
        "--data",
        str(data),
        "--if-revision",
        "1",
        "--json",
    ]
    assert run(update, prog="gh slate") == 0
    updated = json.loads(capsys.readouterr().out)
    assert updated["action"] == "updated"
    assert updated["revision"] == 2

    update.remove("--if-revision")
    update.remove("1")
    assert run(update, prog="gh slate") == 0
    unchanged = json.loads(capsys.readouterr().out)
    assert unchanged["action"] == "unchanged"
    assert unchanged["revision"] == 2

    assert (
        run(
            [
                "view",
                "ci",
                "--target",
                target,
                "--json",
            ],
            prog="gh slate",
        )
        == 0
    )
    readback = json.loads(capsys.readouterr().out)
    assert readback["revision"] == 2
    assert readback["status"] == "valid"

    writes = [event for event in backend.events if event[0] in {"POST", "PATCH"}]
    assert [event[0] for event in writes] == ["POST", "PATCH"]
    assert all(event[2] == HOST for event in backend.events)
    for index, event in enumerate(backend.events):
        if event[0] in {"POST", "PATCH"}:
            assert backend.events[index - 1] == (
                "GET",
                backend.comments_endpoint,
                HOST,
            )
            assert backend.events[index + 1] == (
                "GET",
                backend.comments_endpoint,
                HOST,
            )


@pytest.mark.parametrize("kind", ["issues", "pull"])
def test_v2_target_preview_matches_offline_fixture_and_stored_render(kind, tmp_path, monkeypatch, capsys):
    page_url = f"https://{HOST}/{REPOSITORY}/{kind}/{NUMBER}"
    backend = FakeGhBackend(page_url=page_url)
    reader = GhProcess(runner=ReadRunner(backend))
    transaction = apply_commands._CoreTransaction(reader=reader, writer=GhWriteProcess(runner=WriteRunner(backend)))
    monkeypatch.setattr(apply_commands, "_new_transaction", lambda: transaction)
    monkeypatch.setattr(read_commands, "_new_process", lambda: reader)
    template = tmp_path / "template.j2"
    template.write_text(
        "# {{ meta.slate.name }}\n{{ meta.repository.full_name | md_link(meta.target.url) }} #{{ meta.target.number }}\n{{ data.message }}"
    )
    data = tmp_path / "data.json"
    data.write_text('{"message":"A|B <img> `code`"}')
    args = ["apply", "ci", "--target", page_url, "--template", str(template), "--data", str(data)]
    assert run([*args, "--dry-run"]) == 0
    preview = capsys.readouterr().out
    assert not backend.comments
    assert run([*args, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "created"
    assert run(["view", "ci", "--target", page_url, "--json"]) == 0
    stored = json.loads(capsys.readouterr().out)
    assert stored["data"]["message"] == "A|B <img> `code`"
    meta = tmp_path / "target.json"
    meta.write_text(json.dumps(stored["meta"]))
    assert run(["render", "ci", "--template", str(template), "--data", str(data), "--meta", str(meta)]) == 0
    assert capsys.readouterr().out == preview
    assert run(["render", "ci", "--template", str(template), "--data", str(data), "--meta", str(meta), "--json"]) == 0
    local = json.loads(capsys.readouterr().out)
    assert local["meta_source"] == "fixture"
    assert local["meta"] == stored["meta"]
    assert local["markdown"] == preview
    assert run([*args, "--dry-run", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["meta_source"] == "github"
    template.unlink()
    assert run(["render", "ci", "--target", page_url]) == 0
    assert capsys.readouterr().out == preview
    assert run(["render", "ci", "--target", page_url, "--json"]) == 0
    remote = json.loads(capsys.readouterr().out)
    assert remote["meta_source"] == "stored"
    assert remote["markdown"] == preview
    assert run(["apply", "ci", "--target", page_url, "--data", str(data), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "unchanged"
    assert [event[0] for event in backend.events if event[0] in {"POST", "PATCH"}] == ["POST"]


def test_profile_updates_use_stored_definition_until_explicit_reload(tmp_path, monkeypatch, capsys):
    page_url = f"https://{HOST}/{REPOSITORY}/issues/{NUMBER}"
    backend = FakeGhBackend(page_url=page_url)
    reader = GhProcess(runner=ReadRunner(backend))
    transaction = apply_commands._CoreTransaction(reader=reader, writer=GhWriteProcess(runner=WriteRunner(backend)))
    monkeypatch.setattr(apply_commands, "_new_transaction", lambda: transaction)
    monkeypatch.setattr(read_commands, "_new_process", lambda: reader)
    config = tmp_path / "boards.toml"
    template = tmp_path / "template.j2"
    schema = tmp_path / "schema.json"
    schema.write_text(
        '{"type":"object","required":["message"],"additionalProperties":false,"properties":{"message":{"type":"string"}}}'
    )
    config.write_text('version = 1\n[profiles.summary]\ntemplate = "template.j2"\nschema = "schema.json"\n')
    template.write_text("Original: {{ data.message }}")
    data = tmp_path / "data.json"
    data.write_text('{"message":"first"}')
    base = ["apply", "ci", "--target", page_url, "--json"]
    definition = ["--config", str(config), "--profile", "summary"]
    assert run([*base, *definition, "--data", str(data)]) == 0
    assert json.loads(capsys.readouterr().out)["revision"] == 1
    template.write_text("Reloaded: {{ data.message }}")
    data.write_text('{"message":"second"}')
    assert run([*base, "--data", str(data)]) == 0
    assert json.loads(capsys.readouterr().out)["revision"] == 2
    assert run(["render", "ci", "--target", page_url]) == 0
    assert capsys.readouterr().out == "Original: second\n"
    assert run([*base, *definition]) == 0
    assert json.loads(capsys.readouterr().out)["revision"] == 3
    assert run(["render", "ci", "--target", page_url]) == 0
    assert capsys.readouterr().out == "Reloaded: second\n"
    schema.write_text('{"required":["different"]}')
    writes = len([event for event in backend.events if event[0] in {"POST", "PATCH"}])
    assert run([*base, *definition]) == 2
    capsys.readouterr()
    assert len([event for event in backend.events if event[0] in {"POST", "PATCH"}]) == writes
    config.write_text('version = 1\n[profiles.summary]\ntemplate = "template.j2"\n')
    data.write_text('{"message":"third","extra":true}')
    assert run([*base, *definition, "--data", str(data)]) == 0
    assert json.loads(capsys.readouterr().out)["revision"] == 4
    config.unlink()
    template.unlink()
    schema.unlink()
    monkeypatch.setenv("GH_SLATE_CONFIG", str(config))
    data.write_text('{"message":"fourth"}')
    assert run([*base, "--data", str(data)]) == 0
    assert json.loads(capsys.readouterr().out)["revision"] == 5
    assert len(backend.comments) == 1
    assert run(["view", "ci", "--target", page_url, "--json"]) == 0
    stored = json.loads(capsys.readouterr().out)
    assert stored["profile"] == "summary"
    assert stored["schema"] is False


def test_multi_view_updates_switch_one_comment_without_local_files(tmp_path, monkeypatch, capsys):
    page_url = f"https://{HOST}/{REPOSITORY}/pull/{NUMBER}"
    backend = FakeGhBackend(page_url=page_url)
    reader = GhProcess(runner=ReadRunner(backend))
    transaction = apply_commands._CoreTransaction(reader=reader, writer=GhWriteProcess(runner=WriteRunner(backend)))
    monkeypatch.setattr(apply_commands, "_new_transaction", lambda: transaction)
    monkeypatch.setattr(read_commands, "_new_process", lambda: reader)
    config = tmp_path / "boards.toml"
    config.write_text(
        'version = 1\n[profiles.review]\nview_by = "/outcome"\n[profiles.review.views]\napproved = "approved.j2"\nchanges_requested = "changes.j2"\nerror = "error.j2"'
    )
    for filename, source in {
        "approved.j2": "Passed: {{ data.summary }}",
        "changes.j2": "Findings: {{ data.findings | md_list }}",
        "error.j2": "Failed: {{ data.error.message }}",
    }.items():
        (tmp_path / filename).write_text(source)
    data = tmp_path / "data.json"
    data.write_text('{"outcome":"changes_requested","findings":{"F17":"fix"}}')
    base = ["apply", "review", "--target", page_url, "--data", str(data), "--json"]
    assert run([*base, "--config", str(config), "--profile", "review"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["view"] == "changes_requested"
    for path in (config, *tmp_path.glob("*.j2")):
        path.unlink()
    for outcome, payload in (("error", {"error": {"message": "unavailable"}}), ("approved", {"summary": "resolved"})):
        data.write_text(json.dumps({"outcome": outcome, **payload}))
        assert run([*base, "--dry-run"]) == 0
        preview = json.loads(capsys.readouterr().out)
        assert preview["view"] == outcome
        assert run(base) == 0
        updated = json.loads(capsys.readouterr().out)
        assert updated["view"] == outcome
        assert updated["comment_id"] == created["comment_id"]
    assert run(base) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "unchanged"
    assert run(["view", "review", "--target", page_url, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["view"] == "approved"
    writes = [event for event in backend.events if event[0] in {"POST", "PATCH"}]
    assert len(writes) == 3
    for invalid in ({}, {"outcome": "unknown"}, {"outcome": False}):
        data.write_text(json.dumps(invalid))
        assert run(base) == 2
        capsys.readouterr()
    assert len([event for event in backend.events if event[0] in {"POST", "PATCH"}]) == len(writes)


def test_patch_cli_cross_view_final_validation_and_stdin(tmp_path, monkeypatch, capsys):
    from io import StringIO
    from pathlib import Path
    from shutil import copytree

    backend = FakeGhBackend(page_url=f"https://{HOST}/{REPOSITORY}/pull/{NUMBER}")
    transaction = apply_commands._CoreTransaction(
        reader=GhProcess(runner=ReadRunner(backend)), writer=GhWriteProcess(runner=WriteRunner(backend))
    )
    monkeypatch.setattr(apply_commands, "_new_transaction", lambda: transaction)
    profiles = tmp_path / "definitions"
    copytree(Path(__file__).parents[2] / "examples/profiles", profiles)
    base = ["apply", "review", "--target", backend.page_url, "--json"]
    assert (
        run(
            [
                *base,
                "--config",
                str(profiles / "boards.toml"),
                "--profile",
                "review",
                "--data",
                str(profiles / "review-changes.json"),
            ]
        )
        == 0
    )
    created = json.loads(capsys.readouterr().out)
    operations = [
        {"op": "replace", "path": "/outcome", "value": "approved"},
        {"op": "remove", "path": "/findings"},
        {"op": "add", "path": "/summary", "value": "All findings resolved"},
    ]
    patch = tmp_path / "patch.json"
    patch.write_text(json.dumps(operations))
    update = [*base, "--patch", str(patch), "--if-revision", "1"]
    assert run([*update, "--dry-run"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["changes"]["view"] == {"before": "changes_requested", "after": "approved"}
    assert preview["data"] == {
        "source": json.loads((profiles / "review-changes.json").read_text())["source"],
        "outcome": "approved",
        "summary": "All findings resolved",
    }
    assert len(backend.comments) == 1
    assert run(update) == 0
    updated = json.loads(capsys.readouterr().out)
    assert updated["comment_id"] == created["comment_id"]
    assert updated["revision"] == 2
    assert [event[0] for event in backend.events if event[0] in {"POST", "PATCH"}] == ["POST", "PATCH"]
    assert run([*base, "--patch", str(patch)]) == 2
    capsys.readouterr()
    with pytest.raises(SystemExit) as error:
        run([*update, "--data", str(patch)])
    assert error.value.code == 2
    capsys.readouterr()
    monkeypatch.setattr("sys.stdin", StringIO('[{"op":"test","path":"/outcome","value":"approved"}]'))
    assert run([*base, "--patch", "-", "--if-revision", "2"]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "unchanged"
    monkeypatch.setattr("sys.stdin", StringIO('[{"op":"test","op":"add","path":"/outcome","value":"approved"}]'))
    assert run([*base, "--patch", "-", "--if-revision", "2"]) == 2
    capsys.readouterr()
    assert [event[0] for event in backend.events if event[0] in {"POST", "PATCH"}] == ["POST", "PATCH"]
