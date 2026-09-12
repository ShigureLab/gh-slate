# gh-slate

`gh-slate` turns typed JSON into named dashboard comments on GitHub Issues and
Pull Requests. A profile combines an optional JSON Schema with a Jinja template
or several complete views. The managed comment stores data, metadata, schema,
and all template sources, so later updates work without the original checkout.

This is a pre-release implementation. See [testing](docs/testing.md) for offline
and live acceptance evidence and the remaining release gates.

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

## Quick start

Check authentication and local dependencies:

```bash
gh auth status
gh slate doctor --json
```

Use the checked-in [CI, review, and benchmark profiles](examples/profiles).
Preview a review locally, then against a real target before publishing:

```bash
gh slate render review --config examples/profiles/boards.toml --profile review --data examples/profiles/review-changes.json
gh slate apply review --target https://github.com/OWNER/REPO/pull/42 --config examples/profiles/boards.toml --profile review --data examples/profiles/review-changes.json --dry-run --json
gh slate apply review --target https://github.com/OWNER/REPO/pull/42 --config examples/profiles/boards.toml --profile review --data examples/profiles/review-changes.json --json
```

Replace the target URL before publishing. The examples contain synthetic data.
Use `gh-slate` in place of `gh slate` with the Python CLI.

Read the current data and revision, or switch to the approval layout by sending
a new snapshot. Omitting the profile reuses the definition stored on GitHub:

```bash
gh slate view review --target https://github.com/OWNER/REPO/pull/42 --json
gh slate apply review --target https://github.com/OWNER/REPO/pull/42 --data examples/profiles/review-approved.json --json
gh slate list --target https://github.com/OWNER/REPO/pull/42 --json
gh slate state export review --target https://github.com/OWNER/REPO/pull/42
gh slate state verify review --target https://github.com/OWNER/REPO/pull/42 --json
```

The review profile maps `data.outcome` to `approved`, `changes_requested`, or
`error`. Those names belong to the profile; the core only follows its JSON
Pointer and exact view map. Missing or unknown outcomes fail before writing.
Each view can have a different layout and schema branch.

For a partial edit, use a standard RFC 6902 patch and the revision read from
`view --json`. For example, while the review contains finding F17:

```bash
gh slate apply review --target https://github.com/OWNER/REPO/pull/42 --patch examples/profiles/resolve-finding.patch.json --if-revision 3 --dry-run --json
```

Replace `3` with the observed revision. The dry-run reports candidate data,
Markdown, data changes, definition/meta changes, and view transitions. Remove
`--dry-run` to publish. Patch and full data use the same writer; failure performs
no write and a no-op preserves revision.

## Definitions and templates

Configuration is explicit: `--config FILE` overrides `GH_SLATE_CONFIG`.
There is no default directory search or config merging. File paths are relative
to the TOML file, and `--profile NAME` selects a definition:

```toml
version = 1

[profiles.ci]
schema = "ci.schema.json"
template = "ci.md.j2"

[profiles.review]
schema = "review.schema.json"
view_by = "/outcome"

[profiles.review.views]
approved = "approved.md.j2"
changes_requested = "changes.md.j2"
error = "error.md.j2"
```

Single-template definitions also work directly with `--template FILE` and
optional `--schema FILE`. Profile and direct definition overrides are mutually
exclusive. Pass `--profile` explicitly to reload a changed definition; normal
data updates never read local config files.

Templates receive business `data` and read-only `meta`:

```jinja2
## {{ meta.slate.name }}
{% if meta.target is not none %}
Target: {{ meta.repository.full_name | md_link(meta.target.url) }} #{{ meta.target.number }}
{% endif %}

{{ data.jobs | md_table(columns=["name", "status"]) }}
{{ data.notes | md_list }}
```

Ordinary interpolated strings are escaped. Use `md_text`, `md_link`, `md_code`,
`md_codeblock`, `md_table`, `md_list`, and `md_details` for composable Markdown.
Sandbox limits bound template source, loops, output, and the complete comment.
There is no include loader, filesystem access, or arbitrary Python call.

`meta` contains host, repository identity, target kind/number/node ID/URL, and
slate name. Apply obtains it from GitHub and stores the exact render snapshot.
Local render sets host/repository/target to null unless given `--meta FILE`.
Use `apply --dry-run` for a preview with real target metadata. An analyzed
commit, run ID, or timestamp belongs in `data.source`; gh-slate never replaces
it with a newer revision merely because the target changed.

## Read, recover, and migrate

| Task                                             | Command                          |
| ------------------------------------------------ | -------------------------------- |
| Local or stored-state preview                    | `render`                         |
| Publish a full snapshot or RFC 6902 patch        | `apply --data` / `apply --patch` |
| Read current data, metadata, revision, and view  | `view --json`                    |
| List managed comments                            | `list --json`                    |
| Export the complete embedded definition and data | `state export`                   |
| Verify integrity and reproduce V2 rendering      | `state verify`                   |
| Restore visible Markdown from V2 state           | `repair --from-state`            |
| Remove one managed comment                       | `delete --confirm NAME`          |
| Check authentication and local dependencies      | `doctor`                         |

```bash
gh slate render review --target https://github.com/OWNER/REPO/pull/42
gh slate view review --target https://github.com/OWNER/REPO/pull/42 --web
gh slate repair review --target https://github.com/OWNER/REPO/pull/42 --from-state --if-revision 3 --json
gh slate delete review --target https://github.com/OWNER/REPO/pull/42 --confirm review --json
```

V1 comments remain readable, exportable, integrity-checkable, and deletable.
`state verify` reports `verification_scope: "envelope"` for V1; it does not
claim to reproduce an old renderer. Updating or repairing V1 requires explicit
migration with `apply --profile ... --config ...` or `apply --template ...`.
Migration reuses stored data unless new data or a patch is supplied, then
validates and publishes V2 in the same comment. Convert legacy `slate.*` template
variables to `meta.*`; arbitrary jq selectors are not translated automatically.

The former `data`/`schema` command trees, editor wrapper, Schema inference, and
`--table`/`--list` renderers have been removed. Use `view --json` or `state export`
for data access, external tools for queries, `apply --patch` for partial edits,
`apply --schema` for direct schema replacement, and Jinja helpers for tables/lists.

## Publishing guarantees

Names are scoped to target and controller; they match
`[a-z0-9][a-z0-9._-]{0,63}` and cannot contain `--`. Default apply mode is `upsert`;
`create` rejects an existing instance and `update` rejects a missing one.

A managed comment is one state container. Decode, schema, rendering, identity,
size, drift, duplicate-name, and revision checks run before publishing. An
uncertain write response triggers readback and never automatically replays a
patch. GitHub has no atomic compare-and-swap for comments: serialize publishers
by target/name and refetch before a later update after a conflict.

Visible Markdown is a projection. Manual edits produce drift and block normal
updates; repair explicitly restores the stored projection. A V2 state copied
to a different target cannot be adopted for publishing. Embedded state is
public to anyone who can read the comment, including template sources; keep
credentials and private logs out of it.

For automation, aggregate parallel jobs into one snapshot and publish through
one writer. Use trusted templates and reducer code in privileged workflows.
The [Actions examples](examples/actions) demonstrate current-resource refetch,
serialization, and fork-safe reduction. gh-slate publishes ordinary comments;
its local Markdown output can also feed a separate review workflow.

See [CLI and state reference](docs/cli.md), [testing and release gates](docs/testing.md),
and the [redesign plan](docs/redesign.md).
