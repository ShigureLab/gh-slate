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

Until the first PyPI release, install the Python CLI from a checkout. This is
also the supported development install on Windows:

```bash
git clone https://github.com/ShigureLab/gh-slate.git
cd gh-slate
uv tool install .
gh-slate --help
```

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
skill does not install the CLI. With Node.js/npm available, install it through
the cross-agent `skills` CLI:

```bash
npx skills add https://github.com/ShigureLab/gh-slate --skill gh-slate
```

GitHub CLI 2.96 or newer can install the same skill through its native,
currently preview, skill command:

```bash
gh skill install ShigureLab/gh-slate gh-slate --agent codex --scope user
```

## Preflight

Check GitHub authentication first. `doctor` verifies the `gh` version and
authenticated actor, loads the renderer/schema dependencies, and runs a jq
smoke query through the isolated worker used by normal commands:

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

There is no separate `init` command: the first `apply --mode create` or
`apply --mode upsert` initializes a slate. Names are lowercase, at most 64
characters, match `[a-z0-9][a-z0-9._-]{0,63}`, and cannot contain `--`.

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

and `report.schema.json` contains:

```json
{
   "$schema": "https://json-schema.org/draft/2020-12/schema",
   "type": "object",
   "properties": {
      "jobs": {
         "type": "array",
         "items": {
            "type": "object",
            "properties": {
               "name": { "type": "string" },
               "status": { "type": "string" }
            },
            "required": ["name", "status"]
         }
      },
      "summary": { "type": "string" }
   },
   "required": ["jobs", "summary"]
}
```

Preview a table without writing, then upsert exactly one named slate. A name is
unique only within one target and controller, so `ci-summary` can be reused on
another Issue or Pull Request:

```bash
gh slate apply ci-summary --target https://github.com/OWNER/REPO/issues/42 --mode upsert --data report.json --schema report.schema.json --table '.jobs' --columns name,status --title 'CI summary' --dry-run
gh slate apply ci-summary --target https://github.com/OWNER/REPO/issues/42 --mode upsert --data report.json --schema report.schema.json --table '.jobs' --columns name,status --title 'CI summary' --json
```

List all managed slates on the target:

```bash
gh slate list --target https://github.com/OWNER/REPO/issues/42 --json
```

The corresponding read-only commands render local input, print the remote
Markdown, open the exact comment, export the hidden typed state, or print the
stored schema:

```bash
gh slate render ci-summary --data report.json --schema report.schema.json --table '.jobs' --columns name,status --title 'CI summary'
gh slate view ci-summary --target https://github.com/OWNER/REPO/issues/42
gh slate view ci-summary --target https://github.com/OWNER/REPO/issues/42 --web
gh slate state export ci-summary --target https://github.com/OWNER/REPO/issues/42
gh slate schema get ci-summary --target https://github.com/OWNER/REPO/issues/42
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

```jinja2
## Deployment: {{ slate.name }}

Target: [{{ slate.repository }}#{{ slate.number }}]({{ slate.url }})

{{ data.checks | md_table(columns=["name", "status"]) }}
{{ data.notes | md_list }}

Metadata: `{{ data.metadata | compact_json }}`
```

Save that source as `deployment.md.j2`, then preview it against the real target:

```bash
gh slate apply deployment --target https://github.com/OWNER/REPO/pull/42 --mode create --data deployment.json --schema deployment.schema.json --template deployment.md.j2 --dry-run
```

Remove `--dry-run` only after reviewing the rendered Markdown. Pure
`gh slate render` can preview target-independent templates; when a template
references `slate.repository`, `slate.number`, or `slate.url`, use the
target-aware `apply --dry-run` form above so size and branch checks use the real
Issue or Pull Request context.

Templates receive only the canonical typed `data` object and `slate.name`,
`slate.repository`, `slate.number`, and `slate.url`. JSON scalars interpolate
directly. Objects and arrays must use the deterministic `md_table`, `md_list`,
or `compact_json` filters shown above; arbitrary calls, imports, includes,
filesystem access, environment variables, and default Jinja globals are not
available.

Jinja autoescaping is disabled because the output is Markdown, not HTML.
Direct scalar interpolation and `compact_json` do not escape Markdown syntax:
for example, untrusted backticks can break a code span. `md_table` and
`md_list` apply the built-in Markdown text escaping rules; for values placed
directly into links, code spans, headings, or raw prose, validate or escape them
for that exact context in the trusted template.

### Add or change a JSON Schema

Schemas use JSON Schema draft 2020-12 and are stored with the data. `$ref`
and `$dynamicRef` may only point to an empty or same-document `#...`
fragment; remote URLs, files, and relative registry references are rejected
without I/O. Validate a candidate against the current schema, infer a
permissive starting point, or replace the schema explicitly:

```bash
gh slate schema validate ci-summary report.json --target https://github.com/OWNER/REPO/issues/42 --json
gh slate schema infer ci-summary --target https://github.com/OWNER/REPO/issues/42
gh slate schema set ci-summary report.schema.json --target https://github.com/OWNER/REPO/issues/42 --if-revision 1 --json
```

`schema infer` only prints by default; add `--apply` and an observed
`--if-revision` to store the inferred schema. The `schema set` example
assumes you edited `report.schema.json` after the initial apply; setting an
identical schema is intentionally reported as `unchanged`.

### Query and update typed data

Queries use jq syntax and never scrape the visible table:

```bash
gh slate data get ci-summary '.jobs[] | select(.status != "passed") | .name' --target https://github.com/OWNER/REPO/issues/42 --raw-output
```

First inspect the slate with `view --json`. If it reports revision `1`, choose
one of these writes: mutate one static path, delete paths, or transform the
complete data object with jq. Each example below is an alternative write from
revision `1`; all pin that observation and reject an already-stale read:

```bash
gh slate data set ci-summary '.jobs[1].status' --target https://github.com/OWNER/REPO/issues/42 --value-string passed --if-revision 1 --json
gh slate data set ci-summary '.coverage' --target https://github.com/OWNER/REPO/issues/42 --value 91.7 --if-revision 1 --json
gh slate data set ci-summary '.jobs[1]' --target https://github.com/OWNER/REPO/issues/42 --value-file windows-result.json --if-revision 1 --json
gh slate data delete ci-summary '.jobs[1]' --target https://github.com/OWNER/REPO/issues/42 --if-revision 1 --json
gh slate data update ci-summary '.jobs |= map(if .name == $name then .status = "passed" else . end)' --target https://github.com/OWNER/REPO/issues/42 --arg name windows --if-revision 1 --json
gh slate data update ci-summary '.jobs[$index] = $result' --target https://github.com/OWNER/REPO/issues/42 --argjson index 1 --argjson result @windows-result.json --if-revision 1 --json
```

`data set` and `data delete` accept static jq-compatible paths such as
`.status`, `.jobs[1].status`, and `.["key.with.dot"]`. A path cannot depend on
the current data, arithmetic, a pipe, or interpolation; use `data update` for
computed transforms. This keeps path selection independent of jq's IEEE-754
number projection while untouched JSON numbers remain exact.

Value sources are explicit and mutually exclusive: `--value` parses one strict
JSON value, `--value-string` stores exact text, and `--value-file` parses one
JSON document, so `91`, `"91"`, `true`, and `"true"` remain distinct. For
`data update`, `--arg` binds text while `--argjson` binds typed JSON; prefix its
value with `@` to load a JSON file. Deleting an absent path is an error unless
`--ignore-missing` is requested.

jq runs in a bounded isolated subprocess. Environment access, extra inputs,
imports/includes/modules, and host-introspection builtins are unavailable;
deterministic renderer selectors additionally reject time/date and
platform-dependent math builtins. libjq projects numbers through IEEE-754, so
`data get` and renderer selectors can round integers outside the exact range.
Store precision-sensitive IDs and large integers as strings when they must pass
through jq.

`data update` is stricter because it writes canonical state. Its input,
`--argjson` values, numeric filter literals, and output must preserve identity
through jq's number model, otherwise the command fails before writing. The
filter must produce exactly one JSON object; zero results, multiple results,
scalars, and arrays are errors. Use `data set` or `data delete` when exact
large numbers must remain numeric.

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

### Recover an interrupted or ambiguous write

Mutating commands perform at most one remote write. If the response or
verification handoff is interrupted, gh-slate does a read-only refetch instead
of replaying that write. When the intended revision is found and verified, the
JSON result reports `"recovered": true`; treat that as a successful observed
write.

If the command still fails with
`post_write_verification_unknown`, `write_timeout_unknown`,
`write_outcome_unknown`, `repair_outcome_unknown`, or
`delete_outcome_unknown`, do not blindly retry. Observe the target first:

```bash
gh slate list --target https://github.com/OWNER/REPO/issues/42 --json
gh slate view ci-summary --target https://github.com/OWNER/REPO/issues/42 --json
gh slate state verify ci-summary --target https://github.com/OWNER/REPO/issues/42 --json
```

If the intended state is already present, stop. For an update, repair, or
delete whose current outcome is now unambiguous, choose any next operation from
the newly observed revision and pin that revision with `--if-revision`; never
replay a mutation pinned to the old observation.

An unknown `create` or `upsert` needs extra care. If the slate was missing
before the write and remains absent on the first refetch, that absence does not
prove the create POST failed: GitHub's comment listing may not have exposed it
yet, and there is no revision to pin. Do not recreate solely from that one
missing read. Continue read-only observation with `list`, inspect the target's
comments or API for the original managed comment, and retry `view`/`state verify`
once it appears. Create again only after an explicit human decision that
accepts the duplicate-comment risk.

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
pushes only that version tag. One tag-triggered workflow requires the commit to
be on the default branch, runs the full deterministic and live gates, builds
and verifies the release artifacts once, and stages those exact files in a
draft GitHub Release. Its PyPI OIDC job has only two steps: download the
verified artifact set and invoke the pinned official PyPI publishing action.
Only after PyPI succeeds does the workflow make the draft GitHub Release
stable. Every referenced Action is pinned to a full commit SHA. Direct
`just publish` is disabled so it cannot bypass this ordering.

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

The [incremental redesign proposal](docs/redesign.md) describes configurable
profiles, multiple views, data/meta context, and a staged implementation plan.
It is a proposal, not the current command contract.

## License

[MIT](LICENSE)
