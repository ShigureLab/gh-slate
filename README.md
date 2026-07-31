# gh-slate

`gh-slate` creates named, data-backed dashboard comments on GitHub Issues and
Pull Requests. It keeps typed JSON, an optional JSON Schema, and the renderer
definition inside the managed comment, then projects that state as Markdown
with a built-in table/list renderer or a sandboxed Jinja template.

This is a pre-release implementation. The offline codec, renderer, GitHub
adapter, recovery, fault-injection, packaging, and Actions paths are tested,
but the credentialed GitHub.com Issue/PR and live GHES gates have not been run.
The project does not yet claim stable or GA status.

<p align="center">
   <a href="https://python.org/" target="_blank"><img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-blue?logo=python&style=flat-square"></a>
   <a href="LICENSE"><img alt="LICENSE" src="https://img.shields.io/github/license/ShigureLab/gh-slate?style=flat-square"></a>
   <br/>
   <a href="https://github.com/astral-sh/uv"><img alt="uv" src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json&style=flat-square"></a>
   <a href="https://github.com/astral-sh/ruff"><img alt="ruff" src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json&style=flat-square"></a>
</p>

## Install

Requirements:

- Python 3.10 or newer;
- `gh`, authenticated for the target host;
- `uv` for the Python tool installation and, on supported Unix platforms, the
  repository-backed GitHub CLI extension launcher.

After a release is published to PyPI, install the Python tool with:

```bash
uv tool install gh-slate
gh-slate --help
```

The PyPI package and `gh-slate` Python CLI are runtime-tested on Linux, macOS,
and Windows. On macOS/Linux or another Unix environment with Bash and `uv`, the
repository can instead be installed as a GitHub CLI extension:

```bash
gh extension install ShigureLab/gh-slate
gh slate --help
```

The current root extension launcher is a Bash script and is not a supported
Windows entrypoint; use the Python CLI on Windows. On supported platforms both
entrypoints expose the same parser. The public names are:

```text
PyPI distribution       gh-slate
Python import package   gh_slate
console script          gh-slate
GitHub repository       gh-slate
gh extension command    gh slate
```

### Install the agent skill separately

The bundled skill teaches an agent the safe inspect/dry-run/mutate/verify
workflow. Installing the CLI does not install the skill, and installing the
skill does not install the CLI:

```bash
npx skills add https://github.com/ShigureLab/gh-slate --skill gh-slate
```

GitHub CLI 2.96 or newer can install the same skill through its native,
currently preview, skill command:

```bash
gh skill install ShigureLab/gh-slate gh-slate --agent codex --scope user
```

## Preflight

Check GitHub authentication first, then exercise the same gh, jq, Jinja, and
JSON Schema runtime used by normal commands:

```bash
gh auth status
gh slate doctor --json
```

Use `gh-slate doctor --json` instead when installed as a Python tool. A full
Issue or Pull Request URL is the least ambiguous target; numeric targets also
accept `--repo OWNER/REPO`, while Actions may use `@event`.

## Command map

| What you want to do                           | Command family                    |
| --------------------------------------------- | --------------------------------- |
| Preview local data as Markdown                | `render`                          |
| Create or replace one complete slate snapshot | `apply`                           |
| Read one slate or list all names on a target  | `view`, `list`                    |
| Query or mutate embedded typed JSON           | `data get/set/delete/update/edit` |
| Inspect, infer, validate, or replace a schema | `schema get/infer/validate/set`   |
| Export or integrity-check the hidden state    | `state export/verify`             |
| Restore drifted Markdown or remove a comment  | `repair`, `delete`                |
| Check the local runtime and GitHub access     | `doctor`                          |

Run `gh slate COMMAND --help` for every flag. The examples below use the
extension spelling; replace `gh slate` with `gh-slate` when using the Python
tool.

## Quick start

Assume `report.json` contains:

```json
{
   "jobs": [
      { "name": "linux", "status": "passed" },
      { "name": "windows", "status": "running" }
   ],
   "summary": "2 jobs"
}
```

Preview a table without writing, then upsert exactly one named slate. A name is
unique only within one target and controller, so `ci-summary` can be reused on
another Issue or Pull Request:

```bash
gh slate apply ci-summary --target https://github.com/OWNER/REPO/issues/42 --mode upsert --data report.json --table '.jobs' --columns name,status --title 'CI summary' --dry-run
gh slate apply ci-summary --target https://github.com/OWNER/REPO/issues/42 --mode upsert --data report.json --table '.jobs' --columns name,status --title 'CI summary' --json
```

List all managed slates on the target:

```bash
gh slate list --target https://github.com/OWNER/REPO/issues/42 --json
```

For the next complete update, pass only the new data. Omitting renderer and
schema options reuses the versions embedded in the existing slate:

```bash
gh slate apply ci-summary --target https://github.com/OWNER/REPO/issues/42 --data report.json --json
```

Use `--mode create` when an existing name must be an error, or `--mode update`
when a missing name must be an error. The default is `upsert`.

### List and Jinja renderers

The list renderer stores the same typed state and only changes its Markdown
projection:

```bash
gh slate apply release-items --target https://github.com/OWNER/REPO/issues/42 --mode create --data release.json --list '.items' --title 'Release items' --json
```

For a custom layout, pass trusted Jinja source. Template source is embedded in
the state, so a later update does not depend on the original checkout:

```bash
gh slate apply deployment --target https://github.com/OWNER/REPO/pull/42 --mode create --data deployment.json --schema deployment.schema.json --template deployment.md.j2 --dry-run
```

Remove `--dry-run` only after reviewing the rendered Markdown.

### Add or change a JSON Schema

Schemas use JSON Schema draft 2020-12 and are stored with the data. Validate a
candidate against the current schema, infer a permissive starting point, or
replace the schema explicitly:

```bash
gh slate schema validate ci-summary report.json --target https://github.com/OWNER/REPO/issues/42 --json
gh slate schema infer ci-summary --target https://github.com/OWNER/REPO/issues/42
gh slate schema set ci-summary report.schema.json --target https://github.com/OWNER/REPO/issues/42 --if-revision 1 --json
```

`schema infer` only prints by default; add `--apply` and an observed
`--if-revision` to store the inferred schema.

### Query and update typed data

Queries use jq syntax and never scrape the visible table:

```bash
gh slate data get ci-summary '.jobs[] | select(.status != "passed") | .name' --target https://github.com/OWNER/REPO/issues/42 --raw-output
```

First inspect the slate with `view --json`. If it reports revision `1`, choose
one of these writes: mutate one static path, or transform the complete data
object with jq. Both pin the observed revision and reject an already-stale read:

```bash
gh slate data set ci-summary '.jobs[1].status' --target https://github.com/OWNER/REPO/issues/42 --value-string passed --if-revision 1 --json
gh slate data update ci-summary '.jobs |= map(if .name == $name then .status = "passed" else . end)' --target https://github.com/OWNER/REPO/issues/42 --arg name windows --if-revision 1 --json
```

`data set` and `data delete` accept static jq-compatible paths such as
`.status`, `.jobs[1].status`, and `.["key.with.dot"]`. A path cannot depend on
the current data, arithmetic, a pipe, or interpolation; use `data update` for
computed transforms. This keeps path selection independent of jq's IEEE-754
number projection while untouched JSON numbers remain exact.

For an interactive typed edit, set `GH_EDITOR`, `GIT_EDITOR`, `VISUAL`, or
`EDITOR`, then run:

```bash
gh slate data edit ci-summary --target https://github.com/OWNER/REPO/issues/42 --if-revision 1 --json
```

Refetch before any subsequent write. Inspect and verify the result:

```bash
gh slate view ci-summary --target https://github.com/OWNER/REPO/issues/42 --json
gh slate state verify ci-summary --target https://github.com/OWNER/REPO/issues/42 --json
```

### Repair visible drift and delete a slate

A manual edit to visible Markdown is drift, not new canonical data. Mutations
fail closed until the projection is explicitly restored:

```bash
gh slate repair ci-summary --from-state --target https://github.com/OWNER/REPO/issues/42 --if-revision 2 --json
```

Repair rerenders stored state and may keep the same functional revision.
Deleting the whole managed comment requires the exact name or an explicit
non-interactive confirmation:

```bash
gh slate delete ci-summary --target https://github.com/OWNER/REPO/issues/42 --confirm ci-summary --json
gh slate delete ci-summary --target https://github.com/OWNER/REPO/issues/42 --yes --quiet
```

## State and safety model

The hidden typed `StateV1` envelope is the source of truth. Visible Markdown is
a deterministic, one-way projection:

```text
managed comment envelope  <-- encode/decode -->  typed StateV1
                                                    |
                                                  render
                                                    v
                                             visible Markdown
```

Tables and lists cannot losslessly represent JSON types such as `null`, numeric
versus string `001`, nested objects, or missing fields. `gh-slate` therefore
decodes data and schema metadata from the hidden envelope; it does not claim a
Markdown-to-state round trip. Integrity hashes detect manual projection edits.

Important operational boundaries:

- Do not store tokens, credentials, private logs, or other secrets in data,
  schemas, or templates. Hidden comment metadata is still GitHub comment data.
- Use only trusted jq and Jinja source, especially in privileged Actions
  workflows. Never evaluate a template supplied by an untrusted fork under
  `pull_request_target`.
- Prefer a complete `apply --data FILE` snapshot and one writer in CI.
  Repository workflow `concurrency` prevents more races than incremental
  updates from multiple jobs.
- Actions concurrency does not guarantee FIFO event ordering. If a dashboard
  mirrors Issue or Pull Request fields, use the webhook only to identify the
  target and refetch the current resource inside the serialized job immediately
  before `apply`; do not render an old event snapshot. The checked-in examples
  follow this pattern.
- `--if-revision` plus the pre-write refetch detects observed stale state, but
  GitHub issue-comment updates provide no atomic compare-and-swap (CAS).
  It is an optimistic guard, not a lock; two writers can still race after their
  final reads.
- Treat `created`, `updated`, `repaired`, `deleted`, and `unchanged` as distinct
  successful outcomes. Do not claim a remote write succeeded until the command
  returns its verified result.
- Drift, duplicate names, corrupt state, unknown write outcomes, and revision
  conflicts fail closed; there is no generic `--force` escape hatch.

See the checked-in [Actions examples](examples/actions/README.md) for minimal
permissions and trusted-data patterns.

## Verification status

The default suite is offline. It includes canonical state round trips, bounded
decoder fuzzing, renderer/schema/jq coverage, Issue and Pull Request subprocess
tests through a persistent fake `gh`, recovery fault injection, Actions static
validation, and offline artifact layout/entrypoint checks.

Run the local packaging gate against the exact wheel and sdist:

```bash
just clean-builds
just build
just release-verify
```

`just release` adds all deterministic gates and tag/version verification, then
pushes only that version tag. The tag runs an unprivileged Release Candidate
workflow with a read-only token and no secrets. A separate `workflow_run`
publisher is pinned to its trusted workflow commit, checks the triggering
workflow ID, path, run attempt, repository, commit, and tag through the Actions
API, then independently rebuilds the Python distributions and extension
assets. Only byte-identical, source-bound artifacts can reach write or PyPI
OIDC jobs. Every referenced Action is pinned to a full commit SHA. The
publisher runs the live gate with a job-scoped `GITHUB_TOKEN`, stages a draft
GitHub Release, publishes the verified Python files, and only then makes the
exact draft stable. Direct `just publish` is disabled so it cannot bypass this
ordering.

This private repository's release trust boundary is exclusive write access:
only release maintainers may have write or admin permission, while all other
contributors must use fork-based pull requests. Keep the default Actions token
read-only and do not store release credentials as Actions secrets. Configure
the PyPI Trusted Publisher for `ShigureLab/gh-slate` and the top-level
`.github/workflows/release.yml` without an Environment claim. Before granting
any non-release-maintainer write access, move publishing to a separately
controlled repository or enable repository protections that provide an
equivalent external approval boundary. See [testing](docs/testing.md) for the
full model and disposable target variables. Before the first release, also set
the repository's default Actions token to read-only, disable pull-request
approval through that token, configure both disposable live-target variables,
and register the exact PyPI Trusted Publisher. Release one tag at a time; wait
for its publisher run to finish, and do not move the tag or edit its draft
release while that run is active.

An opt-in live harness exists for disposable GitHub.com Issue and Pull Request
targets, but it is skipped unless the exact confirmation and both target URLs
are supplied. It has not been executed as current release evidence. The GHES
fixtures prove event and hostname contracts only, not live server
compatibility. See [testing](docs/testing.md) for the exact gate and cleanup
procedure.

This README is the user guide. [CLI design](docs/cli.md) is the early protocol
and implementation record; normal use should not require it.

## License

[MIT](LICENSE)
