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

Pull requests run the complete deterministic suite on every supported Python
version on Linux, plus lint, formatting, and package smoke checks. Superseded
runs for the same pull request are cancelled.

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
It creates only uniquely named comments, exercises profile creation, stored-state
updates after deleting local definitions, revision-pinned JSON Patch, all three
review views, unchanged, visible-drift repair, and delete. It attempts cleanup in a
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

Set `GH_SLATE_LIVE_COMMENT_SUFFIX` to append an attribution or other required
notice to every generated template and intentional drift edit.

The repository's regular CI must not set these environment variables. A live
run is release evidence only when its command output and target type are
recorded separately; skipped live tests are not evidence of GitHub or GHES
compatibility.

## Profile-stack acceptance (2026-09-12)

The final V2 profile/patch runtime was validated on macOS with Python 3.12:

- `just ci-test`: 940 passed, 6 skipped (including the opt-in live cases).
- `just ci-lint` and `just ci-fmt-check`: passed.
- Isolated Python 3.10 configuration, profile, and patch tests: 46 passed.
- Wheel/sdist build and installed-artifact smoke: passed. A separate clean
  environment rendered a table helper successfully with no jq package installed.
- The credentialed live harness completed both
  [disposable Issue #32](https://github.com/ShigureLab/gh-slate/issues/32) and
  [disposable PR #33](https://github.com/ShigureLab/gh-slate/pull/33):
  `2 passed in 140.51s`. Each ran create, stable-key patch, error/approval view
  transitions, no-op, drift repair, verify, and delete. Every subsequent command
  was a new process after the local definition directory had been removed.

The first attempt stopped before writing because proxy-routed GitHub reads
returned EOF. The successful run used a process-local direct connection;
system proxy settings were unchanged. Test comments were deleted and temporary
targets closed. This verifies GitHub.com ordinary Issue/PR comments, not GHES,
a tagged release, or PyPI publication.

## Tagged release gate

The private Free-plan repository cannot use a protected Environment with the
review and tag-policy guarantees the release needs. Its enforceable trust root
is therefore deliberately smaller: only release maintainers may have write or
admin access to any repository ref. Everyone else must contribute through a
fork and pull request. Keep the repository's default Actions token read-only
and disable workflow approval through that token. A user who can write an
arbitrary ref can otherwise add a same-named workflow, request PyPI OIDC or a
write-scoped `GITHUB_TOKEN`, and bypass an in-repository approval convention.

The release path uses one explicit tag-triggered workflow:

1. The build job requires the `v*` tag commit to be in current default-branch
   history, runs every deterministic gate, builds the wheel and sdist once, and
   verifies the Python and extension artifacts plus their SHA256 manifest.
2. The live job uses a job-scoped `GITHUB_TOKEN` with only Issue/Pull Request
   comment permissions. Configure the same-repository disposable targets as
   `GH_SLATE_LIVE_DISPOSABLE_ISSUE_URL` and
   `GH_SLATE_LIVE_DISPOSABLE_PR_URL` repository variables; there are no release
   Actions secrets.
3. After the live and extension gates pass, a write-scoped job stages the exact
   verified files in a draft GitHub Release and compares the staged bytes.
4. The PyPI job follows the official minimal shape: download the verified
   artifact set, then invoke `pypa/gh-action-pypi-publish` with job-scoped OIDC.
   The official action publishes attestations by default.
5. Only after PyPI succeeds does the final write-scoped job make the draft
   GitHub Release stable. The workflow rechecks that the tag has not moved.

Configure the PyPI Trusted Publisher with owner `ShigureLab`, repository
`gh-slate`, workflow filename `release.yml`, and no Environment claim. PyPI's
workflow identity does not replace repository access control: do not grant a
non-release-maintainer write access while this same-repository design is in
use. Before doing so, move publishing to a separately controlled release
repository or enable a paid/public protection boundary with equivalent
external approval. A missing live-target variable, mismatched or moved tag,
unexpected existing prerelease, changed staged artifact, or absent Trusted
Publisher fails the release closed.

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
