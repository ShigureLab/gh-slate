from __future__ import annotations

import argparse
import ast
import gzip
import hashlib
import io
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import default
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from email.message import Message

PROJECT_NAME = "gh-slate"
DIST_NAME = "gh_slate"
EXTENSION_PLATFORMS = (
    "darwin-amd64",
    "darwin-arm64",
    "linux-amd64",
    "linux-arm64",
)
_SAFE_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+!-]*")
_PROJECT_SECTION = re.compile(r"^\s*\[project\]\s*(?:#.*)?$")
_ANY_SECTION = re.compile(r"^\s*\[[^]]+]\s*(?:#.*)?$")
_VERSION_ASSIGNMENT = re.compile(r'^\s*version\s*=\s*"([^"]+)"\s*(?:#.*)?$')


class ReleaseVerificationError(RuntimeError):
    """A release input is incomplete, inconsistent, or unsafe to publish."""


@dataclass(frozen=True, slots=True)
class PythonArtifacts:
    wheel: Path
    sdist: Path

    @property
    def paths(self) -> tuple[Path, Path]:
        return (self.wheel, self.sdist)


def _error(message: str) -> ReleaseVerificationError:
    return ReleaseVerificationError(message)


def read_project_version(project_root: Path) -> str:
    path = project_root / "pyproject.toml"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise _error(f"could not read {path}: {type(error).__name__}") from None

    in_project = False
    versions: list[str] = []
    for line in lines:
        if _PROJECT_SECTION.fullmatch(line):
            in_project = True
            continue
        if in_project and _ANY_SECTION.fullmatch(line):
            break
        if not in_project:
            continue
        match = _VERSION_ASSIGNMENT.fullmatch(line)
        if match is not None:
            versions.append(match.group(1))
    if len(versions) != 1:
        raise _error("pyproject.toml must contain exactly one literal project version")
    version = versions[0]
    if _SAFE_VERSION.fullmatch(version) is None:
        raise _error("project version is unsafe for a release tag or artifact name")
    return version


def read_module_version(project_root: Path) -> str:
    path = project_root / "src" / "gh_slate" / "__init__.py"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeError) as error:
        raise _error(f"could not parse {path}: {type(error).__name__}") from None

    versions: list[str] = []
    for statement in tree.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        if not any(isinstance(target, ast.Name) and target.id == "__version__" for target in targets):
            continue
        value = statement.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            versions.append(value.value)
    if len(versions) != 1:
        raise _error("gh_slate.__version__ must be exactly one literal string")
    return versions[0]


def verify_version_contract(
    project_root: Path,
    *,
    tag: str | None,
) -> str:
    project_version = read_project_version(project_root)
    module_version = read_module_version(project_root)
    if module_version != project_version:
        raise _error(f"project and module versions disagree: pyproject={project_version!r}, module={module_version!r}")
    if tag is not None and tag != f"v{project_version}":
        raise _error(f"release tag {tag!r} must exactly equal {f'v{project_version}'!r}")
    return project_version


def discover_python_artifacts(dist_dir: Path) -> PythonArtifacts:
    if not dist_dir.is_dir():
        raise _error(f"distribution directory does not exist: {dist_dir}")
    wheels = sorted(dist_dir.glob("*.whl"))
    sdists = sorted(dist_dir.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise _error(
            "release verification requires exactly one wheel and one sdist; "
            f"found wheels={len(wheels)}, sdists={len(sdists)}"
        )
    ignored_build_files = {
        path
        for path in dist_dir.iterdir()
        if path.name == ".gitignore" and path.is_file() and path.read_bytes() == b"*"
    }
    unexpected = sorted(
        path.name for path in dist_dir.iterdir() if path not in {*wheels, *sdists, *ignored_build_files}
    )
    if unexpected:
        raise _error("Python distribution directory contains unexpected files: " + ", ".join(unexpected))
    return PythonArtifacts(
        wheel=wheels[0],
        sdist=sdists[0],
    )


def _metadata(raw: bytes, *, source: str) -> Message:
    try:
        parsed = BytesParser(policy=default).parsebytes(raw)
    except Exception as error:
        raise _error(f"{source} metadata could not be parsed: {type(error).__name__}") from None
    return parsed


def _verify_metadata(
    metadata: Message,
    *,
    version: str,
    source: str,
) -> None:
    if metadata.get("Name") != PROJECT_NAME:
        raise _error(f"{source} project name is not {PROJECT_NAME!r}")
    if metadata.get("Version") != version:
        raise _error(f"{source} version {metadata.get('Version')!r} does not match {version!r}")
    if not metadata.get("Requires-Python"):
        raise _error(f"{source} is missing Requires-Python metadata")


def _safe_member_name(name: str, *, source: str) -> PurePosixPath:
    path = PurePosixPath(name)
    comparable_name = name.removesuffix("/")
    if (
        "\0" in name
        or "\\" in name
        or path.is_absolute()
        or not path.parts
        or "." in path.parts
        or ".." in path.parts
        or path.parts[0].endswith(":")
    ):
        raise _error(f"{source} contains an unsafe archive member: {name!r}")
    if path.as_posix() != comparable_name:
        raise _error(f"{source} contains a non-canonical archive member: {name!r}")
    return path


def verify_python_artifacts(
    artifacts: PythonArtifacts,
    *,
    version: str,
) -> None:
    expected_wheel = f"{DIST_NAME}-{version}-py3-none-any.whl"
    expected_sdist = f"{DIST_NAME}-{version}.tar.gz"
    if artifacts.wheel.name != expected_wheel:
        raise _error(f"wheel filename {artifacts.wheel.name!r} must equal {expected_wheel!r}")
    if artifacts.sdist.name != expected_sdist:
        raise _error(f"sdist filename {artifacts.sdist.name!r} must equal {expected_sdist!r}")

    try:
        with zipfile.ZipFile(artifacts.wheel) as archive:
            entries = tuple(archive.infolist())
            names = tuple(entry.filename for entry in entries)
            canonical_names = tuple(_safe_member_name(entry.filename, source="wheel") for entry in entries)
            if len(canonical_names) != len(set(canonical_names)):
                raise _error("wheel contains duplicate canonical archive members")
            for entry in entries:
                mode = entry.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise _error("wheel must not contain symbolic links")
            metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
            entry_point_names = [name for name in names if name.endswith(".dist-info/entry_points.txt")]
            if len(metadata_names) != 1 or len(entry_point_names) != 1:
                raise _error("wheel must contain exactly one METADATA and entry_points.txt")
            wheel_metadata = _metadata(
                archive.read(metadata_names[0]),
                source="wheel",
            )
            entry_points = archive.read(entry_point_names[0]).decode(
                "utf-8",
                errors="strict",
            )
    except ReleaseVerificationError:
        raise
    except (OSError, UnicodeError, zipfile.BadZipFile) as error:
        raise _error(f"wheel could not be inspected: {type(error).__name__}") from None

    _verify_metadata(
        wheel_metadata,
        version=version,
        source="wheel",
    )
    if "gh_slate/py.typed" not in names:
        raise _error("wheel is missing gh_slate/py.typed")
    if "[console_scripts]" not in entry_points or "gh-slate = gh_slate.__main__:main" not in entry_points:
        raise _error("wheel does not expose the gh-slate console script")

    expected_root = f"{DIST_NAME}-{version}"
    try:
        with tarfile.open(artifacts.sdist, mode="r:gz") as archive:
            members = tuple(archive.getmembers())
            paths = tuple(_safe_member_name(member.name, source="sdist") for member in members)
            if len(paths) != len(set(paths)):
                raise _error("sdist contains duplicate archive members")
            if any(not (member.isfile() or member.isdir()) for member in members):
                raise _error("sdist must contain only regular files and directories")
            if any(path.parts[0] != expected_root for path in paths):
                raise _error("sdist members must stay inside the versioned root")
            pkg_info_members = [
                member for member in members if PurePosixPath(member.name) == PurePosixPath(expected_root, "PKG-INFO")
            ]
            if len(pkg_info_members) != 1:
                raise _error("sdist must contain exactly one root PKG-INFO")
            pkg_info_file = archive.extractfile(pkg_info_members[0])
            if pkg_info_file is None:
                raise _error("sdist PKG-INFO is not a regular file")
            sdist_metadata = _metadata(
                pkg_info_file.read(),
                source="sdist",
            )
    except ReleaseVerificationError:
        raise
    except (OSError, tarfile.TarError) as error:
        raise _error(f"sdist could not be inspected: {type(error).__name__}") from None

    _verify_metadata(
        sdist_metadata,
        version=version,
        source="sdist",
    )
    required_sdist_paths = {
        PurePosixPath(expected_root, "pyproject.toml"),
        PurePosixPath(expected_root, "src", "gh_slate", "py.typed"),
    }
    missing = sorted(str(path) for path in required_sdist_paths - set(paths))
    if missing:
        raise _error("sdist is missing required files: " + ", ".join(missing))


def _venv_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _venv_entrypoint(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "gh-slate.exe"
    return venv / "bin" / "gh-slate"


def _run(
    argv: list[str],
    *,
    env: dict[str, str],
    cwd: Path | None = None,
    timeout: float = 300,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            env=env,
            cwd=cwd,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise _error(f"release smoke command could not complete: {type(error).__name__}") from None
    if result.returncode != 0:
        raise _error(
            f"release smoke command failed with status {result.returncode}: "
            + " ".join(Path(argument).name for argument in argv[:3])
        )
    return result


def smoke_python_artifact(
    artifact: Path,
    *,
    version: str,
    uv_executable: str,
) -> None:
    environment = os.environ.copy()
    for name in (
        "GH_SLATE_DISPLAY_CMD",
        "PYTHONHOME",
        "PYTHONPATH",
        "UV_PROJECT_ENVIRONMENT",
        "VIRTUAL_ENV",
    ):
        environment.pop(name, None)
    with tempfile.TemporaryDirectory(prefix="gh-slate-release-venv-") as directory:
        venv = Path(directory) / "venv"
        _run(
            [
                uv_executable,
                "venv",
                "--python",
                sys.executable,
                str(venv),
            ],
            env=environment,
            cwd=Path(directory),
        )
        python = _venv_python(venv)
        _run(
            [
                uv_executable,
                "pip",
                "install",
                "--no-cache",
                "--python",
                str(python),
                str(artifact.resolve()),
            ],
            env=environment,
            cwd=Path(directory),
        )
        imported = _run(
            [
                str(python),
                "-c",
                (f"import gh_slate,sys;sys.exit(0 if gh_slate.__version__ == {version!r} else 9)"),
            ],
            env=environment,
            cwd=Path(directory),
        )
        if imported.stdout or imported.stderr:
            raise _error("clean import smoke unexpectedly produced output")
        entrypoint = _venv_entrypoint(venv)
        version_result = _run(
            [str(entrypoint), "--version"],
            env=environment,
            cwd=Path(directory),
        )
        if version_result.stdout != f"gh-slate {version}\n" or version_result.stderr:
            raise _error(f"{artifact.name} console version output does not match {version!r}")
        help_result = _run(
            [str(entrypoint), "--help"],
            env=environment,
            cwd=Path(directory),
        )
        if not help_result.stdout.startswith("usage: gh-slate") or help_result.stderr:
            raise _error(f"{artifact.name} console help smoke failed")


def _bundle_files(project_root: Path) -> tuple[Path, ...]:
    relative_files = [
        Path("gh-slate"),
        Path("LICENSE"),
        Path("README.md"),
        Path("pyproject.toml"),
        Path("uv.lock"),
    ]
    source_root = project_root / "src" / "gh_slate"
    if not source_root.is_dir():
        raise _error("extension bundle source tree is missing src/gh_slate")
    relative_files.extend(
        path.relative_to(project_root)
        for path in sorted(source_root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}
    )
    result: list[Path] = []
    for relative in relative_files:
        path = project_root / relative
        if path.is_symlink() or not path.is_file():
            raise _error(f"extension bundle input must be a regular file: {relative}")
        result.append(relative)
    if Path("src/gh_slate/py.typed") not in result:
        raise _error("extension bundle is missing src/gh_slate/py.typed")
    return tuple(sorted(set(result)))


def _extension_payload(project_root: Path) -> tuple[bytes, tuple[Path, ...]]:
    relative_files = _bundle_files(project_root)
    tar_buffer = io.BytesIO()
    with tarfile.open(
        fileobj=tar_buffer,
        mode="w",
        format=tarfile.PAX_FORMAT,
    ) as archive:
        for relative in relative_files:
            path = project_root / relative
            data = path.read_bytes()
            info = tarfile.TarInfo(relative.as_posix())
            info.size = len(data)
            info.mode = 0o755 if relative == Path("gh-slate") else 0o644
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = 0
            archive.addfile(info, io.BytesIO(data))
    return (
        gzip.compress(
            tar_buffer.getvalue(),
            compresslevel=9,
            mtime=0,
        ),
        relative_files,
    )


def _extension_header(
    *,
    version: str,
    payload_sha256: str,
    payload_offset: int,
) -> bytes:
    text = f"""#!/usr/bin/env bash
set -euo pipefail

payload_offset={payload_offset:020d}
payload_sha256={payload_sha256}
cache_home="${{XDG_CACHE_HOME:-${{HOME:?HOME must be set}}/.cache}}"
install_root="${{cache_home}}/gh-slate-extension-v2"
install_dir="${{install_root}}/{version}-${{payload_sha256}}"
ready_file="${{install_dir}}/.ready"

mkdir -p "${{install_root}}"
if [[ -L "${{install_dir}}" && ! -f "${{ready_file}}" ]]; then
  rm -f "${{install_dir}}"
fi
if [[ ! -f "${{ready_file}}" ]]; then
  stage_dir="$(mktemp -d "${{install_root}}/.{version}-${{payload_sha256}}.stage.XXXXXX")"
  payload_file="${{stage_dir}}/.payload"
  preserve_stage=0
  cleanup() {{
    rm -f "${{payload_file}}"
    if (( ! preserve_stage )); then
      rm -rf "${{stage_dir}}"
    fi
  }}
  terminate() {{
    exit 1
  }}
  trap cleanup EXIT
  trap terminate HUP INT TERM
  tail -c "+$((10#${{payload_offset}} + 1))" "$0" > "${{payload_file}}"
  if command -v sha256sum >/dev/null 2>&1; then
    actual_payload_sha256="$(sha256sum "${{payload_file}}")"
  elif command -v shasum >/dev/null 2>&1; then
    actual_payload_sha256="$(shasum -a 256 "${{payload_file}}")"
  else
    echo "error: sha256sum or shasum is required to verify the gh-slate extension payload" >&2
    exit 1
  fi
  actual_payload_sha256="${{actual_payload_sha256%% *}}"
  if [[ "${{actual_payload_sha256}}" != "${{payload_sha256}}" ]]; then
    echo "error: gh-slate extension payload checksum mismatch" >&2
    exit 1
  fi
  tar -xzf "${{payload_file}}" -C "${{stage_dir}}"
  rm -f "${{payload_file}}"
  chmod u+x "${{stage_dir}}/gh-slate"
  test -f "${{stage_dir}}/pyproject.toml"
  test -f "${{stage_dir}}/uv.lock"
  : > "${{stage_dir}}/.ready"
  preserve_stage=1
  if ! ln -sn "${{stage_dir}}" "${{install_dir}}" 2>/dev/null; then
    preserve_stage=0
  fi
  trap - HUP INT TERM
  trap - EXIT
  cleanup
fi

if [[ ! -f "${{ready_file}}" ]]; then
  echo "error: gh-slate extension cache publication failed" >&2
  exit 1
fi

resolved_install_dir="$(cd -- "${{install_dir}}" && pwd -P)"
exec "${{resolved_install_dir}}/gh-slate" "$@"
"""
    return text.encode("ascii")


def build_extension_assets(
    project_root: Path,
    extension_dir: Path,
    *,
    version: str,
) -> tuple[Path, ...]:
    if extension_dir.exists() and any(extension_dir.iterdir()):
        raise _error(f"extension artifact directory must be empty: {extension_dir}")
    extension_dir.mkdir(parents=True, exist_ok=True)
    payload, _relative_files = _extension_payload(project_root)
    payload_sha256 = hashlib.sha256(payload).hexdigest()
    preliminary = _extension_header(
        version=version,
        payload_sha256=payload_sha256,
        payload_offset=0,
    )
    header = _extension_header(
        version=version,
        payload_sha256=payload_sha256,
        payload_offset=len(preliminary),
    )
    if len(header) != len(preliminary):
        raise _error("extension launcher header length is not stable")
    content = header + payload
    assets: list[Path] = []
    for platform_name in EXTENSION_PLATFORMS:
        path = extension_dir / f"gh-slate-{platform_name}"
        path.write_bytes(content)
        path.chmod(0o755)
        assets.append(path)
    return tuple(assets)


def verify_extension_assets(
    project_root: Path,
    extension_dir: Path,
    *,
    version: str,
) -> tuple[Path, ...]:
    expected_names = {f"gh-slate-{platform_name}" for platform_name in EXTENSION_PLATFORMS}
    actual_paths = tuple(sorted(extension_dir.iterdir()))
    actual_names = {path.name for path in actual_paths if path.is_file()}
    if actual_names != expected_names or len(actual_paths) != len(expected_names):
        raise _error("extension artifact set does not exactly match supported platforms")
    expected_members = {path.as_posix() for path in _bundle_files(project_root)}
    contents: set[bytes] = set()
    for path in actual_paths:
        if path.is_symlink() or not path.is_file():
            raise _error(f"extension asset is not a regular file: {path.name}")
        if os.name != "nt" and not path.stat().st_mode & stat.S_IXUSR:
            raise _error(f"extension asset is not executable: {path.name}")
        content = path.read_bytes()
        contents.add(content)
        offset_match = re.search(
            rb"(?m)^payload_offset=([0-9]{20})$",
            content[:4096],
        )
        digest_match = re.search(
            rb"(?m)^payload_sha256=([0-9a-f]{64})$",
            content[:4096],
        )
        if offset_match is None or digest_match is None:
            raise _error(f"extension asset header is invalid: {path.name}")
        offset = int(offset_match.group(1))
        payload = content[offset:]
        digest = hashlib.sha256(payload).hexdigest().encode("ascii")
        if digest != digest_match.group(1):
            raise _error(f"extension payload hash is invalid: {path.name}")
        try:
            with tarfile.open(
                fileobj=io.BytesIO(payload),
                mode="r:gz",
            ) as archive:
                members = tuple(archive.getmembers())
        except tarfile.TarError:
            raise _error(f"extension payload is not a valid archive: {path.name}") from None
        member_names = {
            _safe_member_name(
                member.name,
                source="extension bundle",
            ).as_posix()
            for member in members
        }
        if len(member_names) != len(members):
            raise _error("extension payload contains duplicate canonical archive members")
        if any(not member.isfile() or member.issym() or member.islnk() for member in members):
            raise _error("extension payload must contain only regular files")
        if member_names != expected_members:
            raise _error("extension payload file set is incomplete or unexpected")
    if len(contents) != 1:
        raise _error("platform extension assets must contain the same tested bundle")
    return actual_paths


def _current_extension_platform() -> str | None:
    system = platform.system().lower()
    machine = platform.machine().lower()
    architecture = {
        "aarch64": "arm64",
        "amd64": "amd64",
        "arm64": "arm64",
        "x86_64": "amd64",
    }.get(machine)
    if system not in {"darwin", "linux"} or architecture is None:
        return None
    return f"{system}-{architecture}"


def smoke_extension_asset(
    extension_dir: Path,
    *,
    version: str,
) -> None:
    platform_name = _current_extension_platform()
    if platform_name is None:
        return
    asset = extension_dir / f"gh-slate-{platform_name}"
    environment = os.environ.copy()
    environment.pop("GH_SLATE_DISPLAY_CMD", None)
    with tempfile.TemporaryDirectory(prefix="gh-slate-extension-smoke-") as directory:
        root = Path(directory)
        environment["HOME"] = str(root / "home")
        environment["XDG_CACHE_HOME"] = str(root / "cache")
        (root / "home").mkdir()
        version_result = _run(
            [str(asset), "--version"],
            env=environment,
        )
        if version_result.stdout != f"gh slate {version}\n":
            raise _error("extension bundle version smoke failed")
        help_result = _run(
            [str(asset), "--help"],
            env=environment,
        )
        if not help_result.stdout.startswith("usage: gh slate"):
            raise _error("extension bundle help smoke failed")


def write_sha256_manifest(
    files: tuple[Path, ...],
    *,
    base_dir: Path,
    destination: Path,
) -> None:
    lines: list[str] = []
    base = base_dir.resolve()
    for path in sorted(files):
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(base)
        except ValueError:
            raise _error(f"manifest input is outside its artifact root: {path}") from None
        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
        lines.append(f"{digest}  {relative.as_posix()}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines) + "\n", encoding="ascii")


def verify_release(
    project_root: Path,
    dist_dir: Path,
    *,
    tag: str | None,
    extension_dir: Path | None,
    manifest: Path | None,
    smoke_installs: bool = True,
    uv_executable: str = "uv",
) -> str:
    root = project_root.resolve()
    version = verify_version_contract(root, tag=tag)
    artifacts = discover_python_artifacts(dist_dir.resolve())
    verify_python_artifacts(
        artifacts,
        version=version,
    )

    extension_assets: tuple[Path, ...] = ()
    if extension_dir is not None:
        if tag is None:
            raise _error("extension release assets require an exact version tag")
        extension_assets = build_extension_assets(
            root,
            extension_dir.resolve(),
            version=version,
        )
        verify_extension_assets(
            root,
            extension_dir.resolve(),
            version=version,
        )

    if smoke_installs:
        executable = shutil.which(uv_executable)
        if executable is None:
            raise _error("uv is required for clean artifact installation smoke")
        for artifact in artifacts.paths:
            smoke_python_artifact(
                artifact,
                version=version,
                uv_executable=executable,
            )
        if extension_dir is not None:
            smoke_extension_asset(
                extension_dir.resolve(),
                version=version,
            )

    if manifest is not None:
        artifact_root = manifest.resolve().parent
        write_sha256_manifest(
            (*artifacts.paths, *extension_assets),
            base_dir=artifact_root,
            destination=manifest.resolve(),
        )
    return version


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify exact gh-slate release artifacts, clean-install both Python "
            "distributions, and optionally build verified gh extension assets."
        )
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument(
        "--dist-dir",
        type=Path,
        required=True,
        help="directory containing exactly one wheel and one sdist",
    )
    parser.add_argument(
        "--tag",
        help="exact release tag; must equal v<project-version>",
    )
    parser.add_argument(
        "--extension-dir",
        type=Path,
        help="empty output directory for verified macOS/Linux gh assets",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help="write SHA256SUMS relative to this file's parent",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        version = verify_release(
            args.project_root,
            args.dist_dir,
            tag=args.tag,
            extension_dir=args.extension_dir,
            manifest=args.manifest,
        )
    except ReleaseVerificationError as error:
        print(f"release verification failed: {error}", file=sys.stderr)
        return 1
    print(f"verified gh-slate release artifacts for {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
