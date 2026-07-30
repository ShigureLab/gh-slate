VERSION := `uv run python -c "import sys; from gh_slate import __version__ as version; sys.stdout.write(version)"`

install:
  uv sync --all-extras --dev

test:
  uv run pytest

fmt:
  uv run ruff format .
  prettier --write '**/*.md'

lint:
  uv run ty check --error-on-warning src/gh_slate tests
  uv run ruff check .

check-actions-examples:
  uv run python scripts/check_actions_examples.py

check-skill:
  uv run python scripts/check_skill.py

fmt-docs:
  prettier --write '**/*.md'

build:
  uv build --no-sources

release-verify:
  uv run python scripts/release_verify.py --dist-dir dist

release:
  @test "$$(git branch --show-current)" = "main" || (echo 'error: release tags must be created from the main branch.' >&2; exit 1)
  @test -z "$$(git status --porcelain=v1 --untracked-files=all)" || (echo 'error: release requires a clean worktree and index so the tested source equals the tagged commit.' >&2; exit 1)
  just ci-fmt-check
  just ci-lint
  just ci-test
  just clean-builds
  just build
  uv run python scripts/release_verify.py --dist-dir dist --tag "v{{VERSION}}"
  @echo 'Tagging v{{VERSION}}...'
  git tag "v{{VERSION}}"
  @echo 'Pushing v{{VERSION}} to trigger the gated release workflow...'
  git push origin "v{{VERSION}}"

publish:
  @echo 'Direct publishing is disabled; use `just release` so verified artifacts pass the tagged workflow.' >&2
  @exit 1

clean:
  find . -name "*.pyc" -print0 | xargs -0 rm -f
  rm -rf .pytest_cache/
  rm -rf .mypy_cache/
  find . -maxdepth 3 -type d -empty -print0 | xargs -0 -r rm -r

clean-builds:
  rm -rf build/
  rm -rf dist/
  rm -rf *.egg-info/

ci-install:
  uv sync --locked --all-extras --dev

ci-fmt-check:
  uv run ruff format --check --diff .
  prettier --check '**/*.md'

ci-lint:
  just lint
  just check-actions-examples
  just check-skill

ci-test:
  uv run pytest --reruns 3 --reruns-delay 1
