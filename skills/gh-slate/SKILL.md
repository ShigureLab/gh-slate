---
name: gh-slate
description: >-
   Create and safely maintain named, data-backed dashboard comments on GitHub
   Issues and Pull Requests with gh-slate. Use this skill whenever the user asks
   for a sticky or Codecov/Codspeed-style report, CI or benchmark dashboard,
   Markdown table/list comment, structured comment data query or update, slate
   verification, drift repair, or managed-comment cleanup, even if they do not
   say "gh-slate".
compatibility: Requires gh, authenticated GitHub access, and gh-slate >=0.1.0. Dual-prefix recipes use Bash; on Windows use the gh-slate Python CLI directly.
license: MIT
metadata:
   primary-tools:
      - gh-slate
      - gh
   minimum-gh-slate-version: 0.1.0
---

# gh-slate

Coordinate the CLI; do not recreate its state codec, jq path semantics,
renderer, schema validator, or GitHub client.

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
# Python CLI:
uv tool install 'gh-slate>=0.1.0'

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

## Safe operating workflow

1. Resolve an explicit `OWNER_REPO`, Issue/PR `TARGET`, and stable lowercase
   `NAME`. Keep one name for one producer and purpose.
2. Inspect an existing slate before mutation. Read its revision, state hash,
   drift status, renderer, and URL.
3. Validate candidate data against the stored schema when one exists. Preview a
   new renderer or material layout change locally and with `apply --dry-run`.
4. Prefer one complete `apply --data FILE` snapshot, especially in CI.
   Incremental jq mutations are for a single writer and should pin the revision
   read in step 2.
5. Treat the returned JSON as the write evidence. Distinguish `created`,
   `updated`, `repaired`, `deleted`, and `unchanged`; do not claim success
   before the command returns a confirmed result.
6. On conflict or unknown outcome, refetch and report what is observed. Do not
   blindly replay a non-idempotent jq update.

Inspect and validate:

```bash
"${GH_SLATE[@]}" view "$NAME" \
  --target "$TARGET" \
  --repo "$OWNER_REPO" \
  --json

"${GH_SLATE[@]}" state verify "$NAME" \
  --target "$TARGET" \
  --repo "$OWNER_REPO" \
  --json

"${GH_SLATE[@]}" schema validate "$NAME" candidate.json \
  --target "$TARGET" \
  --repo "$OWNER_REPO" \
  --json
```

## Trust boundaries

- Keep credentials, tokens, private logs, and secrets out of data, schemas, and
  templates. Put large or sensitive artifacts elsewhere and publish a bounded
  summary plus links.
- Treat visible Markdown as a projection. Query canonical state with
  `data get` or `state export`; never reverse-parse Markdown into typed data.
- Treat Jinja source and jq filters as executable input. A privileged workflow
  must use trusted default-branch templates, schemas, filters, and reducer code,
  never a fork checkout.
- Fail closed on drift, duplicate markers, schema/render errors, stale
  revisions, or an unknown remote outcome. There is no generic force path.
- GitHub comment updates do not provide server-side compare-and-swap.
  Revision checks and second reads reduce risk but do not make concurrent
  incremental writes atomic.
- GitHub Actions concurrency groups serialize matching jobs but do not promise
  FIFO event order. Treat webhook payloads as wake-ups and target identity, not
  as current Issue or Pull Request state. When the slate mirrors GitHub fields,
  refetch the current resource inside the serialized writer immediately before
  `apply`; do not persist an old event's action, title, state, draft flag, or
  head SHA.

## Copy-ready recipes

### Preview and apply a table snapshot

Render locally first:

```bash
"${GH_SLATE[@]}" render "$NAME" \
  --data ci.json \
  --schema ci.schema.json \
  --table '.jobs' \
  --columns name,status,duration_ms \
  --title 'CI matrix'
```

Then exercise the remote read/validation path without writing:

```bash
"${GH_SLATE[@]}" apply "$NAME" \
  --target "$TARGET" \
  --repo "$OWNER_REPO" \
  --mode upsert \
  --data ci.json \
  --schema ci.schema.json \
  --table '.jobs' \
  --columns name,status,duration_ms \
  --title 'CI matrix' \
  --dry-run \
  --json
```

Apply exactly one complete snapshot after the preview is correct:

```bash
"${GH_SLATE[@]}" apply "$NAME" \
  --target "$TARGET" \
  --repo "$OWNER_REPO" \
  --mode upsert \
  --data ci.json \
  --schema ci.schema.json \
  --table '.jobs' \
  --columns name,status,duration_ms \
  --title 'CI matrix' \
  --json
```

For another presentation, preview exactly one trusted renderer:

```bash
"${GH_SLATE[@]}" render "$NAME" \
  --data release.json \
  --list '.changes' \
  --title 'Release notes'

"${GH_SLATE[@]}" render "$NAME" \
  --data dashboard.json \
  --schema dashboard.schema.json \
  --template .github/slates/dashboard.md.j2
```

### Query structured data

Queries read the embedded typed state, not the visible Markdown:

```bash
"${GH_SLATE[@]}" data get "$NAME" \
  '.jobs[] | select(.status == "failed")' \
  --target "$TARGET" \
  --repo "$OWNER_REPO" \
  --compact-output \
  --exit-status
```

### Perform a revision-pinned typed update

Set `REVISION` from the preceding `view --json` result. Keep identifiers or
large integers that jq must preserve exactly as strings.

```bash
"${GH_SLATE[@]}" data update "$NAME" \
  '.jobs |= map(if .name == $name then . + $result else . end)' \
  --arg name linux \
  --argjson result @linux-result.json \
  --target "$TARGET" \
  --repo "$OWNER_REPO" \
  --if-revision "$REVISION" \
  --json
```

For static paths, retain JSON types explicitly. Use `data update` when path
selection itself must be computed from current data:

```bash
"${GH_SLATE[@]}" data set "$NAME" '.coverage' \
  --value-file coverage.json \
  --target "$TARGET" \
  --repo "$OWNER_REPO" \
  --if-revision "$REVISION" \
  --json

"${GH_SLATE[@]}" data delete "$NAME" '.legacy' \
  --ignore-missing \
  --target "$TARGET" \
  --repo "$OWNER_REPO" \
  --if-revision "$REVISION" \
  --json
```

### Publish one full snapshot from GitHub Actions

Aggregate parallel job outputs before this single writer:

```bash
"${GH_SLATE[@]}" apply ci \
  --target "$PR_NUMBER" \
  --repo "$GITHUB_REPOSITORY" \
  --data ci.json \
  --schema ci.schema.json \
  --table '.jobs' \
  --columns name,status,duration_ms \
  --json
```

Pair it with the narrowest target permission and a target/name concurrency key:

```yaml
permissions:
   contents: read
   pull-requests: write

concurrency:
   group: gh-slate-${{ github.repository_id }}-${{ github.event.pull_request.number }}-ci
   cancel-in-progress: false
```

Concurrency alone is not a freshness check. If `ci.json` includes Issue or
Pull Request fields copied from a webhook, fetch those fields again inside this
serialized job immediately before the apply. The repository's direct and
fork-safe examples implement that pattern.

For fork Pull Requests, use the bounded reducer and trusted consumer pattern in
the repository's
[Actions examples](https://github.com/ShigureLab/gh-slate/tree/main/examples/actions);
never execute a downloaded artifact.

### Resolve visible drift

Offer the operator two explicit choices. Repair discards only the visible edit
and rerenders canonical state:

```bash
"${GH_SLATE[@]}" repair "$NAME" \
  --from-state \
  --target "$TARGET" \
  --repo "$OWNER_REPO" \
  --if-revision "$REVISION" \
  --json
```

Edit canonical data when the visible change represented an intended data
change:

```bash
"${GH_SLATE[@]}" data edit "$NAME" \
  --target "$TARGET" \
  --repo "$OWNER_REPO" \
  --if-revision "$REVISION" \
  --json
```

### Delete a managed slate

Delete only when the operator explicitly requests removal. Repeat the exact
case-sensitive name; do not turn duplicate matches into bulk deletion:

```bash
"${GH_SLATE[@]}" delete "$NAME" \
  --target "$TARGET" \
  --repo "$OWNER_REPO" \
  --confirm "$NAME" \
  --json
```

## Report the result

For a write, report the returned `action`, `revision`, `state_sha256`, and
comment URL. Say when a live write was not attempted. A skipped credentialed
test, dry run, or parser check is not evidence that GitHub was updated.

For an unfamiliar option, inspect the installed command's help output first. The
repository [README](https://github.com/ShigureLab/gh-slate#readme) is the user
guide; `docs/cli.md` is the early protocol and implementation record, not a
required operating manual.
