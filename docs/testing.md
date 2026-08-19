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
target/confirmation variables plus `GH_TOKEN`:

```bash
export GH_TOKEN=YOUR_DEDICATED_TEST_REPOSITORY_TOKEN
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

## Tagged release gate

The private Free-plan repository cannot use a protected Environment with the
review and tag-policy guarantees the release needs. Its enforceable trust root
is therefore deliberately smaller: only release maintainers may have write or
admin access to any repository ref. Everyone else must contribute through a
fork and pull request. Keep the repository's default Actions token read-only
and disable workflow approval through that token. A user who can write an
arbitrary ref can otherwise add a same-named workflow, request PyPI OIDC or a
write-scoped `GITHUB_TOKEN`, and bypass an in-repository approval convention.

The release path separates unprivileged candidate execution from publishing:

1. A `v*` tag runs `release-candidate.yml` from the tagged commit with only
   `contents: read`. It has no secrets, write permission, or OIDC permission,
   and every Action reference is a full commit SHA.
2. Only a successful candidate run can trigger `release.yml`. Its read-only
   source-rebuild job checks out the exact candidate SHA and uses pinned
   `uv 0.12.5`, offline mode, and a fixed source epoch to rebuild the wheel and
   sdist independently.
3. The intake verifier is checked out at the immutable `workflow_sha`, not the
   moving default-branch tip. It fetches the canonical candidate workflow and
   triggering run through the Actions API, then binds workflow ID, path, run
   attempt, repository ID, commit, and tag to the artifact context. It requires
   the candidate SHA in current default-branch history, requires the lightweight
   tag to still point to that SHA, byte-compares both Python distributions with
   the independent rebuild, deterministically rebuilds all extension assets
   from the accepted source, and rechecks the exact SHA256 manifest.
4. Jobs that execute candidate code never receive PyPI OIDC or
   `contents: write`. The live job uses its job-scoped `GITHUB_TOKEN` with only
   Issue/Pull Request comment permissions. Configure the same-repository
   disposable targets as `GH_SLATE_LIVE_DISPOSABLE_ISSUE_URL` and
   `GH_SLATE_LIVE_DISPOSABLE_PR_URL` repository variables; there are no release
   Actions secrets.
5. Write-scoped jobs only download and hash-check the accepted files. They do
   not check out or execute candidate code. The first creates or updates a
   draft release, PyPI publishes the same wheel and sdist with OIDC, and the
   final job compares every staged asset before publishing the draft as stable.
   The draft-to-published sequence is compatible with immutable releases.

Configure the PyPI Trusted Publisher with owner `ShigureLab`, repository
`gh-slate`, workflow filename `release.yml`, and no Environment claim. PyPI's
workflow identity does not replace repository access control: do not grant a
non-release-maintainer write access while this same-repository design is in
use. Before doing so, move publishing to a separately controlled release
repository or enable a paid/public protection boundary with equivalent
external approval. A missing live-target variable, mismatched tag, stale
candidate SHA, changed artifact, unexpected existing prerelease, or absent
Trusted Publisher fails the release closed.

Before the first release, change the repository Actions default token to
read-only and disable pull-request approval through that token. The checked-in
workflows also declare explicit minimum permissions, so a later repository
default cannot silently widen them. Configure both disposable target variables
and verify the PyPI Trusted Publisher before pushing a tag. If repository or
organization policy supports required SHA pinning, enable it in addition to the
checked-in full-SHA references.

GitHub concurrency is mutual exclusion, not a durable FIFO queue: a newer
pending publisher can replace an older pending one. Push only one release tag,
wait for its `Release` workflow to finish, and only then start another release.
During that window, release maintainers must not move or delete the tag, edit
the draft release, or start a second release. The private Free-plan design has
no external tag lock, so these are explicit exclusive-writer operating rules.
