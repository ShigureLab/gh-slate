# GitHub Actions examples

These workflows are copyable starting points, not reusable Actions. Copy the
needed YAML files into `.github/workflows/` and keep the referenced
`examples/actions/scripts`, `schemas`, and `templates` files on the default
branch.

- `issue-dashboard.yml` updates one Issue slate.
- `pull-request-dashboard.yml` handles same-repository Pull Requests without
  running Pull Request code.
- `pull-request-target-reducer.yml` has no repository permissions and never
  checks out code. It reduces only repository and target identity to a JSON
  artifact capped at 16 KiB.
- `pull-request-target-consumer.yml` is the privileged `workflow_run` consumer.
  It redownloads the exact reducer artifact, treats it as untrusted, validates
  its size and fixed shape, checks out only the trusted default branch, refetches
  the current Pull Request, and then performs one update.

The Issue and Pull Request event payloads select which target to update; they
are not rendered as current state. Every writer fetches the current GitHub
resource immediately before `gh-slate apply`, so a late run for an older event
cannot restore that event's stale title, state, draft flag, or head SHA. The
display intentionally omits the webhook `action` and the resource's broad
`updated_at` timestamp. The workflows subscribe to every event that can change
a displayed field; unrelated assignment, label, milestone, review-request, and
lock activity therefore does not require a dashboard rewrite.

All writers also use `cancel-in-progress: false`. GitHub concurrency groups do
not promise FIFO ordering, and the comment endpoint has no documented
compare-and-swap update. Serializing one writer per target and slate, then
reading current state inside that serialized job, is part of the safety model.

The examples pin the Python package to `gh-slate==0.1.0`. Update that exact
version intentionally when adopting a later release.
