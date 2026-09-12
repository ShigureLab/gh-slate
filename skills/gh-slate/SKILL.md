---
name: gh-slate
description: >-
   Create and maintain named dashboard comments on GitHub Issues and Pull
   Requests with gh-slate. Use for CI, review, or benchmark reports backed by
   structured data, partial updates, verification, drift repair, and cleanup.
license: MIT
metadata:
   primary-tools:
      - gh-slate
      - gh
---

# gh-slate

Use profiles and typed JSON to maintain one named dashboard comment. The CLI
owns encoding, validation, rendering, conflict checks, and publishing.

## Use the installed CLI

Examples use the GitHub CLI extension, `gh slate`. If the Python CLI is
installed instead, use `gh-slate` for the same commands; this is also the
entrypoint for Windows. Use the installed command consistently.

```bash
gh slate --version
```

Installing this skill does not install the CLI. If it is missing, follow the
installation instructions in the repository
[README](https://github.com/ShigureLab/gh-slate#readme).

When authentication or runtime state is uncertain, preflight before inspecting
or mutating a target:

```bash
gh auth status
gh slate doctor --json
```

## Definitions and context

Choose an explicit config path with `--config` or `GH_SLATE_CONFIG` and a named
`--profile`. Paths are relative to the config; there is no special directory or
config search. A profile stores schema plus one template or a JSON Pointer
`view_by` and complete `views`. Direct `--template` plus optional `--schema`
works too; it cannot combine with `--profile`.

Templates use business `data` and read-only `meta`. Apply fetches target
identity from GitHub. Offline rendering can use `--meta FILE`; absent metadata
has null host/repository/target. Store the analyzed revision and run provenance
in `data.source`, not in live metadata. Producers decide outcomes; missing or
unknown routed values fail rather than implying success.

Ordinary strings are escaped. Use `md_text`, `md_link`, `md_code`,
`md_codeblock`, `md_table`, `md_list`, and `md_details` to compose Markdown.
Templates and schemas are public inside the managed comment. Use trusted default-branch
sources in privileged workflows and never embed credentials or private logs.

## Inspect, preview, publish

Resolve `TARGET`, `OWNER_REPO`, and a stable lowercase `NAME` from the task.
Inspect existing data and set REVISION from its revision before updates; never reverse-parse Markdown
into typed data:

```bash
gh slate view "$NAME" --target "$TARGET" --repo "$OWNER_REPO" --json
gh slate state export "$NAME" --target "$TARGET" --repo "$OWNER_REPO"
```

Preview a selected profile, then use the same candidate for publishing within
the user's authorized scope:

```bash
gh slate render "$NAME" --config boards.toml --profile review --data review.json
gh slate render "$NAME" --template report.j2 --schema report.schema.json --data report.json --meta target.json
gh slate apply "$NAME" --target "$TARGET" --repo "$OWNER_REPO" --config boards.toml --profile review --data review.json --dry-run --json
gh slate apply "$NAME" --target "$TARGET" --repo "$OWNER_REPO" --config boards.toml --profile review --data review.json --json
```

Later full snapshots need only data. Definitions are embedded and are reloaded
only when explicitly selected, even in a new process without the original files:

```bash
gh slate apply "$NAME" --target "$TARGET" --repo "$OWNER_REPO" --data next.json --if-revision "$REVISION" --json
gh slate state verify "$NAME" --target "$TARGET" --repo "$OWNER_REPO" --json
```

## Partial updates

Use RFC 6902 operations on the data root. Prefer stable object keys such as
`/findings/F17/status` over moving array positions. Read `REVISION` from the
preceding view. Patch requires that revision and an existing instance:

```bash
gh slate apply "$NAME" --target "$TARGET" --repo "$OWNER_REPO" --patch patch.json --if-revision "$REVISION" --dry-run --json
gh slate apply "$NAME" --target "$TARGET" --repo "$OWNER_REPO" --patch patch.json --if-revision "$REVISION" --json
```

Patch and data are mutually exclusive. A patch cannot change metadata,
controller, or definition; explicitly selecting a new profile can replace the
definition in the same apply. The full batch is validated and routed once after
all operations, so cross-view transitions can remove old fields and add the new
shape together. A failed test, stale revision, invalid schema, or unknown view
performs no write. No-op keeps revision. Use JSON output with external query
tools when needed.

## Conflicts and recovery

Use a single writer per target/name. GitHub has no server-side compare-and-swap and
concurrency groups do not guarantee FIFO event order. For a current dashboard, refetch the current resource
inside the writer. Preserve the actual
analyzed commit for reviews and benchmarks instead of relabelling older work.
See the repository [Actions examples](https://github.com/ShigureLab/gh-slate/tree/main/examples/actions)
for trusted fork reducers and single-writer publication.

For `post_write_verification_unknown`, `write_timeout_unknown`,
`write_outcome_unknown`, `repair_outcome_unknown`, or `delete_outcome_unknown`,
treat the unknown outcome as unresolved and inspect before any later write;
never blindly replay:

```bash
gh slate list --target "$TARGET" --repo "$OWNER_REPO" --json
gh slate view "$NAME" --target "$TARGET" --repo "$OWNER_REPO" --json
```

Stop when the intended state is observed. If an unknown create is still absent,
one read is not proof that GitHub rejected it. Keep the outcome unresolved;
another create requires a deliberate operator decision accepting duplicate risk.
Do not turn duplicate matches into bulk cleanup.

Visible drift blocks normal apply. When the user wants to discard the visible
edit, restore the saved state with the observed revision:

```bash
gh slate repair "$NAME" --from-state --target "$TARGET" --repo "$OWNER_REPO" --if-revision "$REVISION" --json
```

Delete when requested, using the exact name:

```bash
gh slate delete "$NAME" --target "$TARGET" --repo "$OWNER_REPO" --confirm "$NAME" --json
```

Report the confirmed action (`created`, `updated`, `unchanged`, or recovery action),
revision, and comment URL. Distinguish dry-run from
publication. For an unfamiliar flag, read installed command help; the repository
[README](https://github.com/ShigureLab/gh-slate#readme) provides runnable profiles.
