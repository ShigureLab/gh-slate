from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

if TYPE_CHECKING:
    from collections.abc import Mapping


STATE_ENV = "GH_SLATE_FAKE_STATE"


def _write_state(path: Path, state: Mapping[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


def load_state(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError("fake-gh state root must be an object")
    return value


def save_state(path: Path, state: Mapping[str, object]) -> None:
    _write_state(path, state)


def initialize_state(
    path: Path,
    *,
    target_url: str,
    actor: str = "ci-bot",
    actor_id: int = 101,
) -> None:
    parsed = urlsplit(target_url)
    parts = parsed.path.strip("/").split("/")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or len(parts) != 4:
        raise ValueError("target_url must be one complete Issue or Pull Request URL")
    owner, repository, kind, number_text = parts
    if kind not in {"issues", "pull"} or not number_text.isdecimal():
        raise ValueError("target_url must be one complete Issue or Pull Request URL")
    _write_state(
        path,
        {
            "actor": actor,
            "actor_id": actor_id,
            "comments": [],
            "events": [],
            "faults": [],
            "host": parsed.netloc,
            "kind": kind,
            "next_comment_id": 100,
            "number": int(number_text),
            "repository": f"{owner}/{repository}",
            "target_url": target_url.rstrip("/"),
        },
    )


def install_executable(path: Path) -> None:
    source = Path(__file__).resolve()
    path.write_text(
        f"#!{sys.executable}\nfrom runpy import run_path\nrun_path({str(source)!r}, run_name='__main__')\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def inject_fault(
    path: Path,
    *,
    method: str,
    endpoint: str,
    phase: str,
    mode: str,
    count: int = 1,
    returncode: int = 1,
    stderr: str = "injected fake-gh fault",
) -> None:
    if phase not in {"before", "after"}:
        raise ValueError("fault phase must be before or after")
    if mode not in {"exit", "invalid_json", "empty"}:
        raise ValueError("fault mode must be exit, invalid_json, or empty")
    if count < 1:
        raise ValueError("fault count must be positive")
    state = load_state(path)
    state["faults"].append(
        {
            "count": count,
            "endpoint": endpoint,
            "method": method.upper(),
            "mode": mode,
            "phase": phase,
            "returncode": returncode,
            "stderr": stderr,
        }
    )
    _write_state(path, state)


def replace_visible_markdown(
    path: Path,
    *,
    comment_id: int,
    markdown: str,
) -> None:
    state = load_state(path)
    for comment in state["comments"]:
        if comment["id"] != comment_id:
            continue
        body = comment["body"]
        marker, separator, _visible = body.partition("\n-->\n\n")
        if not separator:
            raise AssertionError("selected comment does not contain a complete marker")
        comment["body"] = f"{marker}{separator}{markdown}"
        _write_state(path, state)
        return
    raise AssertionError(f"comment {comment_id} does not exist")


def committed_write_methods(path: Path) -> list[str]:
    state = load_state(path)
    return [event["method"] for event in state["events"] if event.get("committed") is True]


def _argument_value(
    arguments: list[str],
    name: str,
) -> str | None:
    if name in arguments:
        index = arguments.index(name)
        if index + 1 >= len(arguments):
            raise AssertionError(f"{name} is missing a value")
        return arguments[index + 1]
    prefix = f"{name}="
    for argument in arguments:
        if argument.startswith(prefix):
            return argument.removeprefix(prefix)
    return None


def _api_request(arguments: list[str]) -> tuple[str, str, bool, str | None]:
    method = (_argument_value(arguments, "--method") or "GET").upper()
    hostname = _argument_value(arguments, "--hostname") or os.environ.get("GH_HOST")
    value_options = {"--hostname", "--method", "--input", "--jq"}
    endpoints: list[str] = []
    index = 1
    while index < len(arguments):
        argument = arguments[index]
        if argument in value_options:
            index += 2
            continue
        if any(argument.startswith(f"{option}=") for option in value_options):
            index += 1
            continue
        if argument in {"--paginate", "--slurp"}:
            index += 1
            continue
        if not argument.startswith("-"):
            endpoints.append(argument)
        index += 1
    if len(endpoints) != 1:
        raise AssertionError(f"expected one API endpoint, received {endpoints!r}")
    return method, endpoints[0], "--slurp" in arguments, hostname


def _consume_fault(
    state: dict[str, Any],
    *,
    method: str,
    endpoint: str,
    phase: str,
) -> dict[str, Any] | None:
    faults = state["faults"]
    for index, fault in enumerate(faults):
        if fault["method"] != method or fault["endpoint"] not in {endpoint, "*"} or fault["phase"] != phase:
            continue
        fault["count"] -= 1
        selected = dict(fault)
        if fault["count"] == 0:
            faults.pop(index)
        return selected
    return None


def _emit_json(value: object) -> None:
    sys.stdout.write(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def _emit_fault(fault: Mapping[str, object]) -> int:
    mode = fault["mode"]
    if mode == "exit":
        sys.stderr.write(str(fault["stderr"]))
        returncode = fault["returncode"]
        assert isinstance(returncode, int) and not isinstance(returncode, bool)
        return returncode
    if mode == "invalid_json":
        sys.stdout.write("{invalid-json")
    return 0


def _comment_record(
    state: Mapping[str, Any],
    *,
    identifier: int,
    body: str,
) -> dict[str, object]:
    return {
        "body": body,
        "created_at": "2026-07-31T00:00:00Z",
        "html_url": f"{state['target_url']}#issuecomment-{identifier}",
        "id": identifier,
        "updated_at": "2026-07-31T00:00:00Z",
        "user": {
            "id": state["actor_id"],
            "login": state["actor"],
        },
    }


def _body_from_stdin(arguments: list[str]) -> str:
    if _argument_value(arguments, "--input") != "-":
        raise AssertionError("fake-gh writes require --input -")
    payload = json.loads(sys.stdin.buffer.read())
    if not isinstance(payload, dict) or set(payload) != {"body"} or not isinstance(payload["body"], str):
        raise AssertionError("fake-gh writes require exactly one string body")
    return payload["body"]


def _perform_api(
    state: dict[str, Any],
    arguments: list[str],
    *,
    method: str,
    endpoint: str,
    slurp: bool,
) -> object:
    repository = state["repository"]
    number = state["number"]
    comments_endpoint = f"repos/{repository}/issues/{number}/comments?per_page=100"
    comment_route = f"repos/{repository}/issues/comments/"

    if method == "GET":
        if endpoint == "user":
            return {
                "id": state["actor_id"],
                "login": state["actor"],
            }
        if endpoint.startswith("users/"):
            login = unquote(endpoint.removeprefix("users/"))
            if login.casefold() != str(state["actor"]).casefold():
                raise AssertionError(f"unexpected actor lookup: {login}")
            return {
                "id": state["actor_id"],
                "login": state["actor"],
            }
        if endpoint == f"repos/{repository}/issues/{number}":
            return {"html_url": state["target_url"], "node_id": "I_fake_target"}
        if endpoint == comments_endpoint:
            return [state["comments"]] if slurp else state["comments"]
        if endpoint.startswith(comment_route):
            identifier = int(endpoint.removeprefix(comment_route))
            for comment in state["comments"]:
                if comment["id"] == identifier:
                    return comment
            raise AssertionError(f"comment {identifier} does not exist")
        raise AssertionError(f"unexpected GET endpoint: {endpoint}")

    if method == "POST":
        if endpoint != f"repos/{repository}/issues/{number}/comments":
            raise AssertionError(f"unexpected POST endpoint: {endpoint}")
        identifier = state["next_comment_id"]
        state["next_comment_id"] += 1
        comment = _comment_record(
            state,
            identifier=identifier,
            body=_body_from_stdin(arguments),
        )
        state["comments"].append(comment)
        return comment

    if method == "PATCH":
        if not endpoint.startswith(comment_route):
            raise AssertionError(f"unexpected PATCH endpoint: {endpoint}")
        identifier = int(endpoint.removeprefix(comment_route))
        body = _body_from_stdin(arguments)
        for comment in state["comments"]:
            if comment["id"] == identifier:
                comment["body"] = body
                comment["updated_at"] = "2026-07-31T00:01:00Z"
                return comment
        raise AssertionError(f"comment {identifier} does not exist")

    if method == "DELETE":
        if not endpoint.startswith(comment_route):
            raise AssertionError(f"unexpected DELETE endpoint: {endpoint}")
        identifier = int(endpoint.removeprefix(comment_route))
        for index, comment in enumerate(state["comments"]):
            if comment["id"] == identifier:
                state["comments"].pop(index)
                return {}
        raise AssertionError(f"comment {identifier} does not exist")

    raise AssertionError(f"unexpected API method: {method}")


def _run_api(
    state_path: Path,
    state: dict[str, Any],
    arguments: list[str],
) -> int:
    method, endpoint, slurp, hostname = _api_request(arguments)
    event: dict[str, object] = {
        "committed": False,
        "endpoint": endpoint,
        "hostname": hostname,
        "method": method,
    }
    state["events"].append(event)
    before = _consume_fault(
        state,
        method=method,
        endpoint=endpoint,
        phase="before",
    )
    if before is not None:
        event["fault"] = before["mode"]
        _write_state(state_path, state)
        return _emit_fault(before)

    response = _perform_api(
        state,
        arguments,
        method=method,
        endpoint=endpoint,
        slurp=slurp,
    )
    event["committed"] = method in {"POST", "PATCH", "DELETE"}
    after = _consume_fault(
        state,
        method=method,
        endpoint=endpoint,
        phase="after",
    )
    if after is not None:
        event["fault"] = after["mode"]
        _write_state(state_path, state)
        return _emit_fault(after)

    _write_state(state_path, state)
    if method != "DELETE":
        _emit_json(response)
    return 0


def main(arguments: list[str] | None = None) -> int:
    argv = sys.argv[1:] if arguments is None else arguments
    state_value = os.environ.get(STATE_ENV)
    if not state_value:
        sys.stderr.write(f"{STATE_ENV} is required\n")
        return 64
    state_path = Path(state_value)
    state = load_state(state_path)
    if not argv:
        sys.stderr.write("fake gh command is required\n")
        return 64

    try:
        if argv[0] == "api":
            return _run_api(state_path, state, argv)
        if argv[0:2] == ["repo", "view"]:
            _emit_json(
                {
                    "isPrivate": False,
                    "nameWithOwner": state["repository"],
                    "url": f"https://{state['host']}/{state['repository']}",
                }
            )
            return 0
        if argv[0:2] == ["pr", "view"]:
            _emit_json(
                {
                    "baseRefName": "main",
                    "baseRefOid": "0" * 40,
                    "headRefName": "test",
                    "headRefOid": "1" * 40,
                    "number": state["number"],
                    "state": "OPEN",
                    "url": state["target_url"],
                }
            )
            return 0
        if argv[0:2] == ["auth", "status"]:
            _emit_json({"hosts": {state["host"]: {"state": "success"}}})
            return 0
        if argv[0] == "version":
            sys.stdout.write("gh version 2.80.0 (fake)\n")
            return 0
        raise AssertionError(f"unexpected gh command: {argv!r}")
    except (AssertionError, KeyError, TypeError, ValueError) as error:
        sys.stderr.write(f"fake-gh contract error: {error}\n")
        return 65


if __name__ == "__main__":
    raise SystemExit(main())
