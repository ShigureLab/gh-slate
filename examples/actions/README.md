# GitHub Actions examples

These workflows are copyable starting points, not reusable Actions. Copy the
needed YAML files into `.github/workflows/` and keep the referenced
`examples/actions/scripts`, `schemas`, and `templates` files on the default
branch.

- `issue-dashboard.yml` updates one Issue slate.
- `pull-request-dashboard.yml` handles ordinary same-repository Pull Requests
  without running Pull Request code. It skips forks and Dependabot because
  their `pull_request` tokens cannot write comments.
- `pull-request-target-reducer.yml` has no repository permissions and never
  checks out code. It reduces only repository and target identity to a JSON
  artifact capped at 16 KiB.
- `pull-request-target-consumer.yml` is the privileged `workflow_run` consumer.
  It redownloads the exact reducer artifact, treats it as untrusted, validates
  its size and fixed shape, checks out only the trusted default branch, refetches
  the current Pull Request, and then performs one update.

Use the reducer/consumer pair when fork or Dependabot Pull Requests need a
dashboard; do not try to make their direct `pull_request` token writable.

The Issue and Pull Request event payloads select which target to update; they
are not rendered as current state. Every writer fetches the current GitHub
resource immediately before `gh-slate apply`, so a late run for an older event
cannot restore that event's stale title, state, draft flag, or head SHA. The
display intentionally omits the webhook `action` and the resource's broad
`updated_at` timestamp. The workflows subscribe to every supported
same-repository event that can change a displayed field; unrelated assignment,
label, milestone, review-request, and lock activity therefore does not require
a dashboard rewrite.

The Issue example intentionally does not subscribe to `transferred`. That
event runs with the source repository identity and repository-scoped
`GITHUB_TOKEN`, while the moved Issue must be read and updated in the
destination repository. If the same workflow is installed there, a later
supported event in the destination will refresh the transferred dashboard.
Immediate cross-repository refresh requires a separately designed GitHub App
or fine-grained token with destination write access, plus explicit
destination-identity validation; do not add `transferred` to the copyable
workflow while it uses `GITHUB_TOKEN`.

All writers also use `cancel-in-progress: false`. GitHub concurrency groups do
not promise FIFO ordering, and the comment endpoint has no documented
compare-and-swap update. Serializing one writer per target and slate, then
reading current state inside that serialized job, is part of the safety model.
