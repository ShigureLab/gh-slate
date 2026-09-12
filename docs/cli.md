# CLI and state reference

This document describes the implemented profile-based V2 interface. The
[redesign plan](redesign.md) records the staged transition. The
[README](../README.md) and [profiles](../examples/profiles) are the starting points
for routine use; [testing](testing.md) records verification and release gates.

## Commands

```text
gh slate
├── apply NAME                full snapshot or RFC 6902 data patch
├── render NAME               local or stored-state Markdown
├── view NAME                 stored Markdown, metadata, or browser
├── list                      all managed slates on a target
├── repair NAME --from-state  restore V2 visible Markdown
├── delete NAME               remove one complete comment
├── state
│   ├── export NAME           complete canonical state as JSON
│   └── verify NAME           integrity and V2 render reproduction
└── doctor                    gh, authentication, Jinja, JSON Schema
```

The Python command `gh-slate` exposes the same parser. Remote commands use
`--target` (Issue/PR URL, number, `@event`, or `@pr`), `--repo OWNER/REPO`, optional
`--host`, and optional `--controller LOGIN`. The current token actor is the
default controller; immutable actor ID determines comment ownership, while
login supports display and lookup. Ordinary Pull Request timeline comments use
the issue-comment API.

Names match `[a-z0-9][a-z0-9._-]{0,63}` and cannot contain `--`. A name identifies
one comment within its host, repository, target, and controller. Ambiguous or
corrupt matches are never silently selected.

## Apply

`apply NAME` creates, replaces, or rerenders one desired snapshot. `--mode`
accepts `upsert` (default), `create`, and `update`. `--if-revision N` requires the
observed revision; it is mandatory with `--patch`. `--json` returns the action,
revision, state hash, comment ID/URL, and selected view. `--quiet` suppresses
successful output. `--dry-run` executes reads and candidate validation without
writing, printing Markdown or a JSON preview.

A create needs `--profile` or `--template`. Missing data defaults to `{}`.
Updates reuse omitted data and definition components from stored state.
`--data FILE` supplies an entire strict JSON object; `--patch FILE` transforms
stored data. `--template FILE` embeds Jinja source and `--schema FILE` replaces
the direct definition's optional schema. `--profile NAME` replaces both the
renderer and schema, including clearing an old schema if the new profile has
none. No config path is stored in the comment.

Each input file is bounded before parsing. `-` means stdin for data, patch,
schema, or template; at most one input can consume it. JSON preserves numbers
through Decimal canonicalization, rejects duplicate keys and non-finite
values, and keeps null, missing fields, arrays, objects, booleans, and strings
distinct. Data root must be an object.

## Explicit profiles

Only `--config FILE` or `GH_SLATE_CONFIG` locates configuration. CLI wins over
the environment. There is no directory convention, search, merge, or variable
expansion. All schema/template paths resolve relative to that config file.
`--config` without `--profile` is an error; an environment variable alone does
not reload any stored definition.

```toml
version = 1

[profiles.review]
schema = "review.schema.json"
view_by = "/outcome"

[profiles.review.views]
approved = "review-approved.md.j2"
changes_requested = "review-changes.md.j2"
error = "review-error.md.j2"

[profiles.benchmark]
schema = "benchmark.schema.json"
template = "benchmark.md.j2"
```

A profile has either `template`, or `view_by` plus nonempty `views`, and an
optional `schema`. Profile selection is exclusive with direct schema/template
overrides. Unknown fields, invalid paths, missing files, unsupported syntax,
and oversized definitions fail before writing. Every view source is compiled
at load time and embedded, even when not selected for this render.

`view_by` is an RFC 6901 JSON Pointer into business data. After final Schema
validation it must resolve to a string matching exactly one configured view.
Missing, non-string, and unknown results are errors; there is no implicit view
or `--view` override. Only the selected view executes. Use JSON Schema
`oneOf`/`const` branches for different data shapes. The core assigns no business
meaning to the view names.

## Rendering context

New templates use `data` and `meta`. Ordinary string interpolation is escaped;
`compact_json` explicitly serializes containers. The Markdown helpers return
internal fragments that compose without double escaping:

| Filter                    | Purpose                                    |
| ------------------------- | ------------------------------------------ |
| `md_text`                 | Escape ordinary text                       |
| `md_link(url)`            | Text label linked to an HTTP(S) URL        |
| `md_code`                 | Inline code, preserving special characters |
| `md_codeblock(language)`  | Fenced code with safe fence length         |
| `md_table(columns=[...])` | Table with explicit ordered field names    |
| `md_list`                 | Bounded nested list                        |
| `md_details(summary)`     | Collapsible section                        |
| `length`, `dictsort`      | Bounded iteration and counting helpers     |

Jinja uses strict undefined values and an immutable sandbox with no loader,
include/import, arbitrary Python calls, access to private attributes, or
unbounded recursive loops. Limits cover template bytes, AST nodes, loop
iterations, output, and stored component/envelope sizes. Template literals
control layout; business strings cannot inject formatting through interpolation.

```json
{
   "host": "github.com",
   "repository": {
      "owner": "example",
      "name": "project",
      "full_name": "example/project",
      "url": "https://github.com/example/project"
   },
   "target": {
      "kind": "pull_request",
      "number": 42,
      "id": "PR_example",
      "url": "https://github.com/example/project/pull/42"
   },
   "slate": { "name": "review" }
}
```

Apply gets metadata from the target's GitHub API resource, including the
GraphQL node ID, and verifies stored target identity before writing. Rendering
stored V2 state uses its snapshot, so readback and repair are reproducible.
Revision, comment ID/URL, current time, labels, title, and current head are not
render inputs. Put analyzed commits, runs, and timestamps in `data.source`;
they belong to the producer and are never silently refreshed by gh-slate.

Local `render` accepts `--data`, a definition, and optional `--meta FILE` with
the shape above. Without a fixture, host/repository/target are null and slate
name is known; guard access with `meta.target is not none`. A fixture's slate
name must match. `--json` reports `meta_source` as `fixture` or `local`.
Local inputs cannot combine with remote target options. Apply has no `--meta`
override. For a real target preview use `apply --dry-run` (`meta_source: github`).
Remote `render --target` reproduces stored state (`meta_source: stored`).

## Persistence and compatibility

The comment's hidden envelope is the reversible state boundary. Visible
Markdown is a deterministic projection and is never parsed back into data.
The outer `gh-slate:v1` marker, compression, hashes, and size limits remain
stable; its inner format is `gh-slate/state-v2` for new states. V2 stores:

- name, immutable controller identity, and revision;
- canonical business data and optional JSON Schema;
- read-only metadata snapshot;
- Jinja renderer version 2, optional profile name, and all definition sources;
- render hash.

Codec decoding preserves permanent V1 wire fixtures. V1 can be displayed,
exported, checked for envelope integrity, and explicitly deleted. `state verify`
returns `verification_scope: envelope` and `migration_required: true`; it does
not claim to execute an old renderer. V2 verification reports
`verification_scope: state_and_render`. Visible drift still makes verification
fail in both formats.

Direct V1 updates and repairs return `state_migration_required`. Explicit
`apply --profile ... --config ...` or `apply --template ...` migrates the same
comment using stored or supplied candidate data. New schema validation applies
to the final candidate; old renderer code is never executed. Change legacy
`slate.name`, `slate.repository`, `slate.number`, and `slate.url` to
`meta.slate.name`, `meta.repository.full_name`, `meta.target.number`, and
`meta.target.url`. Migrate old jq selectors to Jinja data access explicitly.

The former data/schema command trees, editor integration, Schema inference,
and jq renderer runtime are removed. Use `view --json` for data access,
`state export` for the full definition, external queries for inspection,
`apply --data` or `--patch` for changes, and template helpers for tables/lists.

## Writer, drift, and recovery

Apply validates the candidate before a second read and one POST/PATCH. Stable
functional content is a no-op and keeps revision unchanged. Stored revision,
identity, and hashes are verified on readback. A timeout or malformed response
may be recovered by observing the intended state; otherwise the command
returns an unknown-outcome error. It never automatically replays a patch or
assumes one missing read proves an earlier POST was rejected.

GitHub does not provide atomic compare-and-swap for comment bodies. The second
read and `--if-revision` reduce race exposure but cannot eliminate the final
read/write gap. Serialize producers externally. After conflict or an unknown
outcome, inspect the target before deciding on a later operation.

Manual visible edits produce drift. Ordinary apply refuses to overwrite them.
`repair --from-state` restores V2 Markdown without changing data or revision.
Delete requires `--confirm NAME` (exact name) or `--yes`, selects one owned
managed comment, and may delete a drifted V1/V2 comment. Corrupt/duplicate
matches are not an invitation for bulk deletion.

Use trusted definitions/reducer code in privileged Actions workflows and keep
secrets out of the publicly readable envelope. Publish large artifacts
separately and keep bounded summaries with links. Local rendering can produce
Markdown for other review workflows; managed publishing uses ordinary comments.

## Exit codes

| Code | Meaning                                                                  |
| ---- | ------------------------------------------------------------------------ |
| 0    | Success, including unchanged                                             |
| 1    | Runtime, authentication, network, or unresolved write outcome            |
| 2    | Usage, JSON, schema, template, routing, patch, or size validation        |
| 3    | Requested instance not found                                             |
| 4    | Drift, duplicate/conflicting state, stale revision, or failed patch test |

With `--json`, business errors use a structured stderr object with `code`,
`message`, optional `details`, and `hints`. Parser usage errors retain argparse's
normal stderr format. Successful JSON goes to stdout.

## JSON Patch through apply

`apply NAME --patch FILE --if-revision N` applies an RFC 6902 array to the
stored **data root**. It requires an existing slate and is mutually exclusive
with `--data` and `--mode create`. Use `-` for stdin; only one input may use
stdin. All six operations are supported: `add`, `remove`, `replace`, `move`,
`copy`, and `test`. Paths use JSON Pointer (`~0` for `~`, `~1` for `/`); array
indices follow RFC 6902, including `-` for append. Prefer stable object keys
for independently updated findings or jobs.

```sh
gh slate view review --target owner/repo#42 --json
gh slate apply review --target owner/repo#42 \
  --patch examples/profiles/resolve-finding.patch.json --if-revision 3 \
  --dry-run --json
```

The patch touches only business data. It cannot change the stored controller,
revision, renderer, or metadata. All operations run on a private candidate;
only the final object is validated against the selected schema and routed to
a view. An explicit `--profile` or `--template` may replace the definition in
the same transaction. Intermediate business-schema violations are allowed,
but JSON resource limits still apply. Failed `test` or stale revision returns
conflict (exit 4); malformed operations and final validation failures return
validation (exit 2). Each error includes the failing operation index where
available. Failure performs no write.

With `--dry-run --json`, output includes candidate `data`, `meta`, `markdown`,
and `changes`: an RFC 6902 data diff, definition/meta change flags, and views
before and after. Plain dry-run prints Markdown. No-op preserves revision.
The normal single-comment writer handles the final candidate; an uncertain
response triggers readback, never patch replay. GitHub offers no atomic
compare-and-swap for comments: serialize publishers externally.
