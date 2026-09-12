---
name: gh-slate
description: >-
   Create and safely maintain named, data-backed dashboard comments on GitHub
   Issues and Pull Requests with gh-slate. Use this skill whenever the user asks
   for a sticky or Codecov/Codspeed-style report, CI or benchmark dashboard,
   Markdown table/list comment, structured comment data query or update, slate
   verification, drift repair, or managed-comment cleanup, even if they do not
   say "gh-slate".
license: MIT
metadata:
   compatibility: Requires gh, authenticated GitHub access, and gh-slate >=0.1.0. Dual-prefix recipes use Bash; on Windows use the gh-slate Python CLI directly.
   primary-tools:
      - gh-slate
      - gh
   minimum-gh-slate-version: 0.1.0
---

# gh-slate

Use profiles and typed JSON to maintain one named dashboard comment. The CLI
owns encoding, validation, rendering, conflict checks, and publishing.

## Resolve one command prefix

Resolve the entrypoint once per shell, then keep the selected array for every
follow-up command:

```bash
gh_slate_compatible() {
  local expected="$1"
  shift
  local output version major minor patch pre post dev
  output="$("$@" --version 2>/dev/null)" || return 1
  [[ "$output" == "$expected "* ]] || return 1
  version="${output#"$expected "}"
  [[ "$version" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)((a|b|rc)[0-9]+)?(\.post[0-9]+|-[0-9]+)?(\.dev[0-9]+)?(\+[0-9A-Za-z]+(\.[0-9A-Za-z]+)*)?$ ]] || return 1
  major="${BASH_REMATCH[1]}"
  minor="${BASH_REMATCH[2]}"
  patch="${BASH_REMATCH[3]}"
  pre="${BASH_REMATCH[4]}"
  post="${BASH_REMATCH[6]}"
  dev="${BASH_REMATCH[7]}"
  major="${major#"${major%%[!0]*}"}"
  minor="${minor#"${minor%%[!0]*}"}"
  patch="${patch#"${patch%%[!0]*}"}"
  if [[ -n "$major" ]]; then return 0; fi
  if [[ -z "$minor" ]]; then return 1; fi
  if [[ "$minor" != 1 ]]; then return 0; fi
  if [[ -n "$patch" ]]; then return 0; fi
  [[ -z "$pre" && ( -z "$dev" || -n "$post" ) ]]
}

if gh_slate_compatible "gh slate" gh slate; then
  GH_SLATE=(gh slate)
elif gh_slate_compatible "gh-slate" gh-slate; then
  GH_SLATE=(gh-slate)
else
  echo "Install gh-slate >=0.1.0 before continuing." >&2
  exit 1
fi

"${GH_SLATE[@]}" --version
```

Require version `0.1.0` or newer. A missing, malformed, prerelease-only, or
older extension probe falls through to the direct Python CLI before the skill
stops. If neither candidate is compatible, offer one CLI installation path;
installing the CLI and installing this skill are separate:

```bash
# Python CLI after the package is published:
uv tool install 'gh-slate>=0.1.0'

# Python CLI from a pre-release checkout, including on Windows:
uv tool install .

# Or the GitHub CLI extension:
gh extension install ShigureLab/gh-slate

# Agent skill, installed separately from either CLI:
npx skills add https://github.com/ShigureLab/gh-slate --skill gh-slate
```

With GitHub CLI 2.96 or newer, the equivalent native skill-only install
(currently a preview command) is:

```bash
gh skill install ShigureLab/gh-slate gh-slate --agent codex --scope user
```

The repository extension launcher and the array resolver above require Bash.
On Windows/PowerShell, install the Python CLI, verify `gh-slate --version`, and
replace `"${GH_SLATE[@]}"` in the recipes with the direct `gh-slate` command.

When authentication or runtime state is uncertain, preflight before inspecting
or mutating a target:

```bash
gh auth status
"${GH_SLATE[@]}" doctor --json
```

## Definitions and context

Check installed apply help for `--profile` and `--patch` before using these
recipes. Earlier pre-release checkouts may share version 0.1.0 while exposing
the former command surface; update that installation first.

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
Inspect existing data and revision before partial changes; never reverse-parse Markdown
into typed data:

```bash
"${GH_SLATE[@]}" view "$NAME" --target "$TARGET" --repo "$OWNER_REPO" --json
"${GH_SLATE[@]}" state export "$NAME" --target "$TARGET" --repo "$OWNER_REPO"
```

Preview a selected profile, then use the same candidate for publishing within
the user's authorized scope:

```bash
"${GH_SLATE[@]}" render "$NAME" --config boards.toml --profile review --data review.json
"${GH_SLATE[@]}" render "$NAME" --template report.j2 --schema report.schema.json --data report.json --meta target.json
"${GH_SLATE[@]}" apply "$NAME" --target "$TARGET" --repo "$OWNER_REPO" --config boards.toml --profile review --data review.json --dry-run --json
"${GH_SLATE[@]}" apply "$NAME" --target "$TARGET" --repo "$OWNER_REPO" --config boards.toml --profile review --data review.json --json
```

Later full snapshots need only data. Definitions are embedded and are reloaded
only when explicitly selected, even in a new process without the original files:

```bash
"${GH_SLATE[@]}" apply "$NAME" --target "$TARGET" --repo "$OWNER_REPO" --data next.json --json
"${GH_SLATE[@]}" state verify "$NAME" --target "$TARGET" --repo "$OWNER_REPO" --json
```

## Partial updates

Use RFC 6902 operations on the data root. Prefer stable object keys such as
`/findings/F17/status` over moving array positions. Read `REVISION` from the
preceding view. Patch requires that revision and an existing instance:

```bash
"${GH_SLATE[@]}" apply "$NAME" --target "$TARGET" --repo "$OWNER_REPO" --patch patch.json --if-revision "$REVISION" --dry-run --json
"${GH_SLATE[@]}" apply "$NAME" --target "$TARGET" --repo "$OWNER_REPO" --patch patch.json --if-revision "$REVISION" --json
```

Patch and data are mutually exclusive. A patch cannot change metadata,
controller, or definition; explicitly selecting a new profile can replace the
definition in the same apply. The full batch is validated and routed once after
all operations, so cross-view transitions can remove old fields and add the new
shape together. A failed test, stale revision, invalid schema, or unknown view
performs no write. No-op keeps revision. There are no embedded jq mutations,
editor wrappers, or Schema inference commands; use JSON output with external
query tools when needed.

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
"${GH_SLATE[@]}" list --target "$TARGET" --repo "$OWNER_REPO" --json
"${GH_SLATE[@]}" view "$NAME" --target "$TARGET" --repo "$OWNER_REPO" --json
```

Stop when the intended state is observed. If an unknown create is still absent,
one read is not proof that GitHub rejected it. Keep the outcome unresolved;
another create requires a deliberate operator decision accepting duplicate risk.
Do not turn duplicate matches into bulk cleanup.

Visible drift blocks normal apply. When the user wants to discard the visible
edit, restore V2 state with the observed revision:

```bash
"${GH_SLATE[@]}" repair "$NAME" --from-state --target "$TARGET" --repo "$OWNER_REPO" --if-revision "$REVISION" --json
```

V1 remains readable/exportable/deletable; verification covers its envelope only.
Update or repair requires explicit migration by applying a V2 profile/template.
Translate old `slate.*` variables to `meta.*`; do not execute or guess a legacy
jq renderer. A drifted V1 still requires resolving the drift deliberately.

Delete when requested, using the exact name:

```bash
"${GH_SLATE[@]}" delete "$NAME" --target "$TARGET" --repo "$OWNER_REPO" --confirm "$NAME" --json
```

Report the confirmed action (`created`, `updated`, `unchanged`, or recovery action),
revision, and comment URL. Distinguish dry-run from
publication. For an unfamiliar flag, read installed command help; the repository
[README](https://github.com/ShigureLab/gh-slate#readme) provides runnable profiles.
