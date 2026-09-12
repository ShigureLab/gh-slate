# Business data selects a view

This directory is an example location, with no special discovery behavior.
Select `boards.toml` explicitly. All referenced paths are relative to that file.

The review profile maps `data.outcome` to three different page layouts. The
producer supplies `approved`, `changes_requested`, or `error`. An execution
error never falls back to the approval template. The schema rejects leftover
fields from another outcome, while templates handle local optional sections.

Preview each synthetic example from the repository root:

```bash
gh slate render review --config examples/profiles/boards.toml \
  --profile review --data examples/profiles/review-approved.json
gh slate render review --config examples/profiles/boards.toml \
  --profile review --data examples/profiles/review-changes.json
gh slate render review --config examples/profiles/boards.toml \
  --profile review --data examples/profiles/review-error.json
```

Add `--json` to inspect the selected `view`, data, and metadata provenance.
For a real target preview, use `apply --target <ISSUE_OR_PR_URL> --dry-run`
with the same profile and data arguments. `apply` publishes ordinary comments;
rendered Markdown can separately be passed to an existing review workflow.

After initial publication, `apply --data FILE` selects the view from the new
data using the stored templates. Switching outcomes updates the same comment,
even on a machine without this directory. Pass `--profile` explicitly only
when reloading the definition. Missing, non-string, or unknown outcomes fail
before writing; there is no independent `--view` override.

Findings have stable keys such as `F17`. Resolving a finding changes its status;
the producer still decides whether the complete review can be approved. The
analyzed revision in `data.source.head_sha` is supplied by the producer and is
never replaced with a current head by gh-slate.
