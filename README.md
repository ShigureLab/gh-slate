# gh-slate

`gh-slate` is a GitHub CLI extension and Python command for named, data-backed
dashboard comments on GitHub Issues and Pull Requests.

The project is currently under implementation. Its CLI, typed-state format,
renderer behavior, safety model, and staged implementation plan are specified
in the [CLI design](docs/cli.md).

<p align="center">
   <a href="https://python.org/" target="_blank"><img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-blue?logo=python&style=flat-square"></a>
   <a href="LICENSE"><img alt="LICENSE" src="https://img.shields.io/github/license/ShigureLab/gh-slate?style=flat-square"></a>
   <br/>
   <a href="https://github.com/astral-sh/uv"><img alt="uv" src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json&style=flat-square"></a>
   <a href="https://github.com/astral-sh/ruff"><img alt="ruff" src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json&style=flat-square"></a>
   <a href="https://gitmoji.dev"><img alt="Gitmoji" src="https://img.shields.io/badge/gitmoji-%20😜%20😍-FFDD67?style=flat-square"></a>
</p>

## Entrypoints

The package and extension expose the same parser with invocation-aware help:

```bash
# Python tool
uv run gh-slate --help

# Repository checkout, using the gh extension spelling
./gh-slate --help
```

The public names map consistently:

```text
PyPI distribution       gh-slate
Python import package   gh_slate
console script          gh-slate
GitHub repository       gh-slate
gh extension command    gh slate
```
