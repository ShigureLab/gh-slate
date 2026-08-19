#!/usr/bin/env python3
"""Validate the bundled gh-slate agent skill against the current parser."""

from __future__ import annotations

import argparse
import io
import re
import shlex
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import NoReturn

import yaml

from gh_slate import __version__
from gh_slate.cli import build_parser

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SKILL_DIR = ROOT / "skills" / "gh-slate"
MINIMUM_VERSION = (0, 1, 0)
PREFIXES = ("gh-slate", "gh slate")
EXTERNAL_INSTALLER_FLAGS = frozenset({"--agent", "--scope", "--skill"})
EXPECTED_RECIPE_ROUTES = frozenset(
    {
        ("--version",),
        ("apply",),
        ("data", "get"),
        ("delete",),
        ("doctor",),
        ("list",),
        ("render",),
        ("repair",),
        ("schema", "infer"),
        ("schema", "validate"),
        ("state", "export"),
        ("state", "verify"),
        ("view",),
    }
)
_FRONTMATTER = re.compile(
    r"\A---\n(?P<header>.*?)\n---\n(?P<body>.*)\Z",
    re.DOTALL,
)
_FENCE = re.compile(
    r"^```(?P<language>[^\n]*)\n(?P<source>.*?)^```\s*$",
    re.DOTALL | re.MULTILINE,
)
_LONG_FLAG = re.compile(r"(?<![A-Za-z0-9])--[a-z][a-z0-9-]*")
_VERSION = re.compile(
    r"\A([0-9]+)\.([0-9]+)\.([0-9]+)"
    r"(?:(?:a|b|rc)[0-9]+)?"
    r"(?:\.post[0-9]+|-[0-9]+)?"
    r"(?:\.dev[0-9]+)?"
    r"(?:\+[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*)?\Z"
)


class SkillCheckError(RuntimeError):
    """One actionable skill contract violation."""


def _fail(message: str) -> NoReturn:
    raise SkillCheckError(message)


def _mapping(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        _fail(f"{field} must be a mapping")
    return value


def _frontmatter(text: str) -> tuple[dict[str, object], str]:
    matched = _FRONTMATTER.fullmatch(text)
    if matched is None:
        _fail("SKILL.md must have one YAML frontmatter block")
    try:
        loaded = yaml.safe_load(matched.group("header"))
    except yaml.YAMLError as error:
        raise SkillCheckError(f"frontmatter is not valid YAML: {error}") from error
    return _mapping(loaded, "frontmatter"), matched.group("body")


def _check_layout(skill_dir: Path) -> Path:
    if skill_dir.name != "gh-slate":
        _fail("skill directory must be named gh-slate")
    if skill_dir.is_symlink() or not skill_dir.is_dir():
        _fail("skill path must be one real directory")
    skill_file = skill_dir / "SKILL.md"
    if not skill_file.is_file() or skill_file.is_symlink():
        _fail("skill directory must contain one regular SKILL.md")

    allowed_top_level = {"SKILL.md", "assets", "references", "scripts"}
    unexpected = sorted(path.name for path in skill_dir.iterdir() if path.name not in allowed_top_level)
    if unexpected:
        _fail(f"unexpected top-level skill entries: {unexpected}")
    symlinks = sorted(str(path.relative_to(skill_dir)) for path in skill_dir.rglob("*") if path.is_symlink())
    if symlinks:
        _fail(f"skill must not contain symlinks: {symlinks}")
    return skill_file


def _check_metadata(metadata: dict[str, object]) -> None:
    if set(metadata) != {
        "name",
        "description",
        "compatibility",
        "license",
        "metadata",
    }:
        _fail("frontmatter fields must be name, description, compatibility, license, and metadata")
    if metadata["name"] != "gh-slate":
        _fail("frontmatter name must be gh-slate")
    description = metadata["description"]
    if not isinstance(description, str):
        _fail("frontmatter description must be text")
    required_trigger_terms = (
        "Use this skill whenever",
        "dashboard",
        "sticky",
        "CI",
        "drift repair",
    )
    for term in required_trigger_terms:
        if term not in description:
            _fail(f"description is missing trigger language: {term!r}")

    compatibility = metadata["compatibility"]
    if not isinstance(compatibility, str) or "gh-slate >=0.1.0" not in compatibility:
        _fail("compatibility must require gh-slate >=0.1.0")
    if metadata["license"] != "MIT":
        _fail("frontmatter license must match the repository MIT license")
    extra = _mapping(metadata["metadata"], "frontmatter.metadata")
    if extra.get("primary-tools") != ["gh-slate", "gh"]:
        _fail("metadata.primary-tools must be [gh-slate, gh]")
    if extra.get("minimum-gh-slate-version") != "0.1.0":
        _fail("metadata minimum version must be 0.1.0")


def _logical_lines(source: str) -> list[str]:
    result: list[str] = []
    pending = ""
    for raw_line in source.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        pending = f"{pending} {stripped}".strip() if pending else stripped
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        result.append(pending)
        pending = ""
    if pending:
        _fail("shell example ends with an incomplete continuation")
    return result


def _bash_lines(body: str) -> list[str]:
    result: list[str] = []
    for matched in _FENCE.finditer(body):
        language = matched.group("language").strip()
        if language in {"bash", "sh", "shell"}:
            result.extend(_logical_lines(matched.group("source")))
    return result


def _substitute_arguments(arguments: list[str]) -> list[str]:
    replacements = {
        "$GITHUB_REPOSITORY": "owner/repo",
        "$NAME": "ci",
        "$OWNER_REPO": "owner/repo",
        "$PR_NUMBER": "42",
        "$REVISION": "7",
        "$TARGET": "42",
    }
    result: list[str] = []
    for argument in arguments:
        resolved = replacements.get(argument, argument)
        if resolved.startswith("$"):
            _fail(f"recipe contains an unsupported shell placeholder: {resolved}")
        result.append(resolved)
    return result


def _recipe_arguments(body: str) -> list[list[str]]:
    recipes: list[list[str]] = []
    lines = _bash_lines(body)
    required_probe_lines = {
        'output="$("$@" --version 2>/dev/null)" || return 1',
        '[[ "$output" == "$expected "* ]] || return 1',
        '[[ "$version" =~ ^([0-9]+)\\.([0-9]+)\\.([0-9]+)((a|b|rc)[0-9]+)?(\\.post[0-9]+|-[0-9]+)?(\\.dev[0-9]+)?(\\+[0-9A-Za-z]+(\\.[0-9A-Za-z]+)*)?$ ]] || return 1',
        'if gh_slate_compatible "gh slate" gh slate; then',
        'elif gh_slate_compatible "gh-slate" gh-slate; then',
        "gh_slate_compatible() {",
        "GH_SLATE=(gh slate)",
        "GH_SLATE=(gh-slate)",
    }
    missing_probes = sorted(required_probe_lines.difference(lines))
    if missing_probes:
        _fail(f"entrypoint probe is incomplete: {missing_probes}")

    for line in lines:
        if re.match(r"\A(?:gh slate|gh-slate)\s+", line):
            _fail("follow-up commands must use the resolved GH_SLATE array")
        try:
            tokens = shlex.split(line)
        except ValueError as error:
            raise SkillCheckError(f"shell example is not parseable: {line!r}: {error}") from error
        if not tokens or tokens[0] != "${GH_SLATE[@]}":
            continue
        recipes.append(_substitute_arguments(tokens[1:]))

    if not recipes:
        _fail("SKILL.md must contain parser-testable GH_SLATE recipes")
    return recipes


def _parser_surface() -> tuple[set[tuple[str, ...]], set[str]]:
    routes: set[tuple[str, ...]] = set()
    flags: set[str] = set()

    def walk(parser: argparse.ArgumentParser, route: tuple[str, ...]) -> None:
        if route:
            routes.add(route)
        for action in parser._actions:
            flags.update(option for option in action.option_strings if option.startswith("--"))
            if isinstance(action, argparse._SubParsersAction):
                for name, child in action.choices.items():
                    walk(child, (*route, name))

    walk(build_parser(prog="gh-slate"), ())
    return routes, flags


def _route(arguments: list[str], routes: set[tuple[str, ...]]) -> tuple[str, ...]:
    if arguments == ["--version"]:
        return ("--version",)
    candidates = [route for route in routes if len(arguments) >= len(route) and tuple(arguments[: len(route)]) == route]
    if not candidates:
        _fail(f"recipe does not name a current command: {arguments!r}")
    return max(candidates, key=len)


def _inline_routes(body: str, routes: set[tuple[str, ...]]) -> set[tuple[str, ...]]:
    prose = _FENCE.sub("", body)
    top_level = {route[0] for route in routes if len(route) == 1}
    discovered: set[tuple[str, ...]] = set()
    for source in re.findall(r"`([^`\n]+)`", prose):
        try:
            tokens = shlex.split(source)
        except ValueError:
            continue
        if not tokens or tokens[0] not in top_level:
            continue
        candidates = [route for route in routes if len(tokens) >= len(route) and tuple(tokens[: len(route)]) == route]
        if not candidates:
            _fail(f"inline command route is stale: {source!r}")
        discovered.add(max(candidates, key=len))
    return discovered


def _parse_success(arguments: list[str], prefix: str) -> None:
    parser = build_parser(prog=prefix)
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            parser.parse_args(arguments)
    except SystemExit as error:
        if error.code == 0 and arguments == ["--version"]:
            return
        _fail(
            f"{prefix} parser rejected recipe {arguments!r}: {stderr.getvalue().strip() or stdout.getvalue().strip()}"
        )


def _help_success(route: tuple[str, ...], prefix: str) -> None:
    parser = build_parser(prog=prefix)
    output = io.StringIO()
    try:
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            parser.parse_args([*route, "--help"])
    except SystemExit as error:
        if error.code != 0:
            _fail(f"{prefix} {' '.join(route)} --help exited {error.code}")
    else:
        _fail(f"{prefix} {' '.join(route)} --help did not terminate")
    rendered = output.getvalue()
    if "usage:" not in rendered or prefix not in rendered:
        _fail(f"{prefix} {' '.join(route)} --help did not render the selected prefix")


def _check_parser_contract(body: str) -> tuple[int, int, int]:
    routes, valid_flags = _parser_surface()
    recipes = _recipe_arguments(body)
    recipe_routes = {_route(arguments, routes) for arguments in recipes}
    missing = sorted(EXPECTED_RECIPE_ROUTES.difference(recipe_routes))
    if missing:
        _fail(f"required recipe routes are missing: {missing}")

    shown_flags = set(_LONG_FLAG.findall(body))
    invalid_flags = sorted(shown_flags.difference(valid_flags).difference(EXTERNAL_INSTALLER_FLAGS))
    if invalid_flags:
        _fail(f"SKILL.md shows flags absent from the current parser: {invalid_flags}")
    exercised_flags = {argument for arguments in recipes for argument in arguments if argument.startswith("--")}
    unexercised_flags = sorted(shown_flags.difference(exercised_flags).difference(EXTERNAL_INSTALLER_FLAGS))
    if unexercised_flags:
        _fail(f"SKILL.md shows flags not exercised by a parser recipe: {unexercised_flags}")
    for external_flag in EXTERNAL_INSTALLER_FLAGS:
        if external_flag not in shown_flags:
            _fail(f"skill installation example is missing {external_flag}")

    help_routes = {route for route in recipe_routes.union(_inline_routes(body, routes)) if route != ("--version",)}
    for prefix in PREFIXES:
        for arguments in recipes:
            _parse_success(arguments, prefix)
        for route in sorted(help_routes):
            _help_success(route, prefix)
    return len(recipes), len(help_routes), len(shown_flags)


def _check_content(text: str, body: str) -> None:
    if len(text.splitlines()) >= 500:
        _fail("SKILL.md must stay below 500 lines")
    required_fragments = (
        "[README](https://github.com/ShigureLab/gh-slate#readme)",
        "npx skills add https://github.com/ShigureLab/gh-slate --skill gh-slate",
        "gh skill install ShigureLab/gh-slate gh-slate --agent codex --scope user",
        "single writer",
        "server-side compare-and-swap",
        "FIFO event order",
        "refetch the current resource",
        "trusted default-branch",
        "never reverse-parse Markdown",
        "unknown outcome",
        "created",
        "updated",
        "unchanged",
    )
    for fragment in required_fragments:
        if fragment not in body:
            _fail(f"SKILL.md is missing required guidance: {fragment!r}")

    version_match = _VERSION.fullmatch(__version__)
    if version_match is None:
        _fail(f"package version is not comparable: {__version__!r}")
    current_version = tuple(int(part) for part in version_match.groups())
    if current_version < MINIMUM_VERSION:
        _fail("current package version is older than the skill minimum")


def check_skill(skill_dir: Path = DEFAULT_SKILL_DIR) -> tuple[int, int, int]:
    skill_file = _check_layout(skill_dir)
    try:
        text = skill_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise SkillCheckError(f"cannot read SKILL.md: {error}") from error
    metadata, body = _frontmatter(text)
    _check_metadata(metadata)
    _check_content(text, body)
    return _check_parser_contract(body)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "skill_dir",
        nargs="?",
        type=Path,
        default=DEFAULT_SKILL_DIR,
    )
    args = parser.parse_args()
    try:
        recipes, help_routes, flags = check_skill(args.skill_dir.resolve())
    except SkillCheckError as error:
        print(f"Skill check failed: {error}", file=sys.stderr)
        return 1
    print(
        f"Checked gh-slate skill: {recipes} recipes, {help_routes} help routes, {flags} flags, {len(PREFIXES)} prefixes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
