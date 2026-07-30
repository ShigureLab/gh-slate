# GitHub Actions examples

These workflows are copyable starting points, not reusable Actions. Copy the
needed YAML files into `.github/workflows/` and keep the referenced
`examples/actions/scripts`, `schemas`, and `templates` files on the default
branch.

- `issue-dashboard.yml` updates one Issue slate.
- `pull-request-dashboard.yml` handles same-repository Pull Requests without
  running Pull Request code.
- `pull-request-target-reducer.yml` has no repository permissions and never
  checks out code. It reduces selected event fields to a JSON artifact capped at
  16 KiB.
- `pull-request-target-consumer.yml` is the privileged `workflow_run` consumer.
  It redownloads the exact reducer artifact, treats it as untrusted, validates
  its size and fixed shape, checks out only the trusted default branch, and then
  performs one update.

All writers use `cancel-in-progress: false`. GitHub's comment endpoint has no
documented compare-and-swap update, so preserving one writer per target and
slate is part of the safety model.

The examples pin the Python package to `gh-slate==0.1.0`. Update that exact
version intentionally when adopting a later release.
