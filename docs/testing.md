# Testing gh-slate

gh-slate separates deterministic offline coverage from explicitly authorized
GitHub tests. The default suite never needs a token and never writes GitHub.

## Offline gates

Run the normal repository checks from the project root:

```bash
just ci-fmt-check
just ci-lint
just ci-test
```

The decoder fuzz tests use bounded Hypothesis inputs and a checked-in regression
corpus under `tests/fixtures/codec/fuzz/`. For every input, decoding must either
succeed or produce the same structured `GhSlateError` on repeated attempts. A
new decoder bug should be reduced to a small corpus entry with its stable error
code.

`tests/integration/test_recovery_e2e.py` runs the real `gh-slate` extension
launcher in subprocesses. A persistent fake `gh` executable records every API
read and write, supports faults before and after a request is committed, and
lets the tests distinguish:

- a successful write with a damaged response that can be recovered by refetch;
- a write that did not commit and whose outcome must not be reported as
  successful;
- a failed read that must remain read-only;
- exact POST, PATCH, and DELETE counts across Issue and Pull Request targets.

The GitHub event fixtures under `tests/fixtures/github/events/` cover Issue and
Pull Request resolution on a non-`github.com` host. They prove only event and
hostname propagation contracts. They do not claim compatibility with a
particular GitHub Enterprise Server release, authentication setup, or live API.

## Opt-in live GitHub test

The live harness is skipped by default. It requires two existing, disposable
targets in a dedicated test repository: one Issue URL and one Pull Request URL.
It creates only uniquely named comments, exercises create/read/query/update,
unchanged, visible-drift repair, and delete, and attempts direct cleanup in a
`finally` block if the normal delete path does not finish.

Use a least-privilege token that may read the repository and write Issue/PR
comments. Do not use production Issues or Pull Requests. Opt in with all three
environment variables and the exact confirmation value:

```bash
export GH_SLATE_LIVE_CONFIRM=I_UNDERSTAND
export GH_SLATE_LIVE_DISPOSABLE_ISSUE_URL=https://github.com/OWNER/REPO/issues/NUMBER
export GH_SLATE_LIVE_DISPOSABLE_PR_URL=https://github.com/OWNER/REPO/pull/NUMBER

uv run pytest -q tests/live/test_github_e2e.py
```

The harness validates that each URL has the expected Issue or Pull Request
shape before writing. It never creates or deletes the Issue or Pull Request
itself. If a run is interrupted outside Python's cleanup path, search the two
targets for a slate name beginning with `live-` and delete that comment before
rerunning.

The repository's regular CI must not set these environment variables. A live
run is release evidence only when its command output and target type are
recorded separately; skipped live tests are not evidence of GitHub or GHES
compatibility.
