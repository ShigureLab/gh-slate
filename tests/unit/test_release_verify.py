from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
CANDIDATE_WORKFLOW = ROOT / ".github" / "workflows" / "release-candidate.yml"
JUSTFILE = ROOT / "justfile"
VERSION = "0.1.0"
CHECKOUT_ACTION = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
SETUP_UV_ACTION = "astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9"
UPLOAD_ARTIFACT_ACTION = "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
DOWNLOAD_ARTIFACT_ACTION = "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"
RELEASE_ACTION = "softprops/action-gh-release@3d0d9888cb7fd7b750713d6e236d1fcb99157228"
PYPI_ACTION = "pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33"
_SPEC = importlib.util.spec_from_file_location(
    "gh_slate_release_verify",
    ROOT / "scripts" / "release_verify.py",
)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
release_verify = cast("Any", _MODULE)
sys.modules.setdefault("release_verify", _MODULE)
_INTAKE_SPEC = importlib.util.spec_from_file_location(
    "gh_slate_intake_release_candidate",
    ROOT / "scripts" / "intake_release_candidate.py",
)
assert _INTAKE_SPEC is not None and _INTAKE_SPEC.loader is not None
_INTAKE_MODULE = importlib.util.module_from_spec(_INTAKE_SPEC)
sys.modules[_INTAKE_SPEC.name] = _INTAKE_MODULE
_INTAKE_SPEC.loader.exec_module(_INTAKE_MODULE)
candidate_intake = cast("Any", _INTAKE_MODULE)


def _add_tar_bytes(
    archive: tarfile.TarFile,
    name: str,
    value: bytes,
) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(value)
    info.mode = 0o644
    archive.addfile(info, io.BytesIO(value))


def _project(tmp_path: Path, *, version: str = VERSION) -> Path:
    root = tmp_path / "project"
    package = root / "src" / "gh_slate"
    package.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        (f'[project]\nname = "gh-slate"\nversion = "{version}"\nrequires-python = ">=3.10"\n'),
        encoding="utf-8",
    )
    (package / "__init__.py").write_text(
        f'__version__ = "{version}"\n',
        encoding="utf-8",
    )
    (package / "__main__.py").write_text(
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    (package / "py.typed").write_bytes(b"")
    launcher = root / "gh-slate"
    launcher.write_text(
        "#!/usr/bin/env bash\nprintf 'gh slate test\\n'\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    (root / "uv.lock").write_text(
        "version = 1\nrevision = 3\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text("# gh-slate\n", encoding="utf-8")
    (root / "LICENSE").write_text("MIT\n", encoding="utf-8")
    return root


def _artifacts(
    tmp_path: Path,
    *,
    filename_version: str = VERSION,
    metadata_version: str = VERSION,
) -> release_verify.PythonArtifacts:
    dist = tmp_path / "dist"
    dist.mkdir(parents=True)
    wheel = dist / f"gh_slate-{filename_version}-py3-none-any.whl"
    wheel_metadata = (
        f"Metadata-Version: 2.4\nName: gh-slate\nVersion: {metadata_version}\nRequires-Python: >=3.10\n\n"
    ).encode()
    with zipfile.ZipFile(wheel, mode="w") as archive:
        archive.writestr(
            f"gh_slate-{filename_version}.dist-info/METADATA",
            wheel_metadata,
        )
        archive.writestr(
            f"gh_slate-{filename_version}.dist-info/entry_points.txt",
            ("[console_scripts]\ngh-slate = gh_slate.__main__:main\n"),
        )
        archive.writestr("gh_slate/py.typed", b"")

    sdist = dist / f"gh_slate-{filename_version}.tar.gz"
    root = f"gh_slate-{filename_version}"
    with tarfile.open(sdist, mode="w:gz") as archive:
        _add_tar_bytes(
            archive,
            f"{root}/PKG-INFO",
            wheel_metadata,
        )
        _add_tar_bytes(
            archive,
            f"{root}/pyproject.toml",
            b"[project]\nname='gh-slate'\n",
        )
        _add_tar_bytes(
            archive,
            f"{root}/src/gh_slate/py.typed",
            b"",
        )
    return release_verify.PythonArtifacts(
        wheel=wheel,
        sdist=sdist,
    )


def _workflow() -> dict[str, Any]:
    value = yaml.load(
        WORKFLOW.read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    assert isinstance(value, dict)
    return value


def _candidate_workflow() -> dict[str, Any]:
    value = yaml.load(
        CANDIDATE_WORKFLOW.read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    assert isinstance(value, dict)
    return value


def _run_text(job: dict[str, Any]) -> str:
    return "\n".join(str(step.get("run", "")) for step in job["steps"])


def test_version_contract_requires_project_module_and_tag_to_match(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)

    assert (
        release_verify.verify_version_contract(
            project,
            tag="v0.1.0",
        )
        == VERSION
    )

    with pytest.raises(
        release_verify.ReleaseVerificationError,
        match="must exactly equal",
    ):
        release_verify.verify_version_contract(
            project,
            tag="0.1.0",
        )

    (project / "src" / "gh_slate" / "__init__.py").write_text(
        '__version__ = "0.1.1"\n',
        encoding="utf-8",
    )
    with pytest.raises(
        release_verify.ReleaseVerificationError,
        match="versions disagree",
    ):
        release_verify.verify_version_contract(
            project,
            tag=None,
        )


def test_exact_wheel_and_sdist_metadata_are_verified(
    tmp_path: Path,
) -> None:
    artifacts = _artifacts(tmp_path)

    discovered = release_verify.discover_python_artifacts(artifacts.wheel.parent)
    assert discovered == artifacts
    release_verify.verify_python_artifacts(
        discovered,
        version=VERSION,
    )


def test_distribution_set_and_metadata_fail_closed(
    tmp_path: Path,
) -> None:
    artifacts = _artifacts(
        tmp_path,
        metadata_version="0.1.1",
    )
    with pytest.raises(
        release_verify.ReleaseVerificationError,
        match="does not match",
    ):
        release_verify.verify_python_artifacts(
            artifacts,
            version=VERSION,
        )

    extra = artifacts.wheel.parent / "unexpected.txt"
    extra.write_text("not a release artifact", encoding="utf-8")
    with pytest.raises(
        release_verify.ReleaseVerificationError,
        match="unexpected files",
    ):
        release_verify.discover_python_artifacts(artifacts.wheel.parent)


def test_wheel_rejects_noncanonical_member_aliases(
    tmp_path: Path,
) -> None:
    artifacts = _artifacts(tmp_path)
    with zipfile.ZipFile(artifacts.wheel, mode="a") as archive:
        archive.writestr("gh_slate/./py.typed", b"alias")

    with pytest.raises(
        release_verify.ReleaseVerificationError,
        match="non-canonical archive member",
    ):
        release_verify.verify_python_artifacts(
            artifacts,
            version=VERSION,
        )


def test_extension_assets_are_exact_executable_self_extracting_bundles(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    extension_dir = tmp_path / "release-artifacts" / "github"

    assets = release_verify.build_extension_assets(
        project,
        extension_dir,
        version=VERSION,
    )
    verified = release_verify.verify_extension_assets(
        project,
        extension_dir,
        version=VERSION,
    )

    assert verified == tuple(sorted(assets))
    assert {path.name for path in assets} == {
        f"gh-slate-{platform_name}" for platform_name in release_verify.EXTENSION_PLATFORMS
    }
    contents = {path.read_bytes() for path in assets}
    assert len(contents) == 1
    content = next(iter(contents))
    assert content.startswith(b"#!/usr/bin/env bash\n")
    offset_match = re.search(rb"(?m)^payload_offset=([0-9]{20})$", content[:4096])
    assert offset_match is not None
    header = content[: int(offset_match.group(1))]
    assert b"tail -c" in header
    assert b"sha256sum" in header
    assert b"shasum -a 256" in header
    assert b"extension payload checksum mismatch" in header
    assert b"BASHPID" not in header
    assert b"lock_dir=" not in header
    assert b"timed out waiting" not in header
    assert b"gh-slate-extension-v2" in header
    assert b"mktemp -d" in header
    assert b'payload_file="${stage_dir}/.payload"' in header
    assert b'rm -f "${install_dir}"' not in header
    assert b'recovery_link="${install_dir}.recover"' in header
    assert b'publication_link="${stage_dir}/.recover-publish"' in header
    assert b"preserve_stage=1" in header
    assert b'ln -sn "${stage_dir}" "${install_dir}"' in header
    assert b'mv -fh -- "${publication_link}" "${install_dir}"' in header
    assert b'mv -fT -- "${publication_link}" "${install_dir}"' in header
    assert b"pwd -P)" in header
    if os.name != "nt":
        assert all(path.stat().st_mode & stat.S_IXUSR for path in assets)

    corrupted = assets[0]
    corrupted.write_bytes(corrupted.read_bytes() + b"x")
    with pytest.raises(
        release_verify.ReleaseVerificationError,
        match="payload hash",
    ):
        release_verify.verify_extension_assets(
            project,
            extension_dir,
            version=VERSION,
        )


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="the self-extracting extension assets require Bash and Unix symlinks",
)
def test_extension_asset_ignores_stale_legacy_lock_and_orphan_stage(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    assets = release_verify.build_extension_assets(
        project,
        tmp_path / "assets",
        version=VERSION,
    )
    content = assets[0].read_bytes()
    digest_match = re.search(
        rb"(?m)^payload_sha256=([0-9a-f]{64})$",
        content[:4096],
    )
    assert digest_match is not None
    digest = digest_match.group(1).decode("ascii")

    cache = tmp_path / "cache"
    legacy_lock = cache / "gh-slate-extension" / f"{VERSION}-{digest}.lock"
    legacy_lock.mkdir(parents=True)
    install_root = cache / "gh-slate-extension-v2"
    orphan = install_root / f".{VERSION}-{digest}.stage.orphan"
    orphan.mkdir(parents=True)
    environment = os.environ.copy()
    environment["HOME"] = str(tmp_path / "home")
    environment["XDG_CACHE_HOME"] = str(cache)
    (tmp_path / "home").mkdir()

    result = subprocess.run(
        [assets[0], "--version"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "gh slate test\n"
    assert result.stderr == ""
    install = install_root / f"{VERSION}-{digest}"
    assert install.is_symlink()
    assert (install / ".ready").is_file()
    assert legacy_lock.is_dir()
    assert orphan.is_dir()


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="the self-extracting extension assets require Bash and Unix symlinks",
)
def test_extension_asset_concurrently_publishes_one_ready_cache(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    assets = release_verify.build_extension_assets(
        project,
        tmp_path / "assets",
        version=VERSION,
    )
    content = assets[0].read_bytes()
    digest_match = re.search(
        rb"(?m)^payload_sha256=([0-9a-f]{64})$",
        content[:4096],
    )
    assert digest_match is not None
    digest = digest_match.group(1).decode("ascii")

    cache = tmp_path / "cache"
    environment = os.environ.copy()
    environment["HOME"] = str(tmp_path / "home")
    environment["XDG_CACHE_HOME"] = str(cache)
    (tmp_path / "home").mkdir()

    def invoke() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [assets[0], "--version"],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=20,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = tuple(executor.map(lambda _index: invoke(), range(8)))

    assert all(result.returncode == 0 for result in results)
    assert all(result.stdout == "gh slate test\n" for result in results)
    assert all(result.stderr == "" for result in results)
    install_root = cache / "gh-slate-extension-v2"
    install = install_root / f"{VERSION}-{digest}"
    assert install.is_symlink()
    assert (install / ".ready").is_file()
    published_stage = install.resolve()
    private_stages = tuple(
        path.resolve()
        for path in install_root.iterdir()
        if path.name.startswith(f".{VERSION}-{digest}.stage.") and path.is_dir()
    )
    assert private_stages == (published_stage,)
    assert not (published_stage / ".payload").exists()


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="the self-extracting extension assets require Bash and Unix symlinks",
)
def test_extension_asset_recovers_a_dangling_published_cache(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    assets = release_verify.build_extension_assets(
        project,
        tmp_path / "assets",
        version=VERSION,
    )
    content = assets[0].read_bytes()
    digest_match = re.search(
        rb"(?m)^payload_sha256=([0-9a-f]{64})$",
        content[:4096],
    )
    assert digest_match is not None
    digest = digest_match.group(1).decode("ascii")

    cache = tmp_path / "cache"
    install_root = cache / "gh-slate-extension-v2"
    install_root.mkdir(parents=True)
    install = install_root / f"{VERSION}-{digest}"
    install.symlink_to(install_root / "missing-stage")
    environment = os.environ.copy()
    environment["HOME"] = str(tmp_path / "home")
    environment["XDG_CACHE_HOME"] = str(cache)
    (tmp_path / "home").mkdir()

    result = subprocess.run(
        [assets[0], "--version"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "gh slate test\n"
    assert install.is_symlink()
    assert (install / ".ready").is_file()


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="the self-extracting extension assets require Bash and Unix symlinks",
)
def test_extension_asset_concurrently_recovers_one_dangling_cache(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    assets = release_verify.build_extension_assets(
        project,
        tmp_path / "assets",
        version=VERSION,
    )
    content = assets[0].read_bytes()
    digest_match = re.search(
        rb"(?m)^payload_sha256=([0-9a-f]{64})$",
        content[:4096],
    )
    assert digest_match is not None
    digest = digest_match.group(1).decode("ascii")

    cache = tmp_path / "cache"
    install_root = cache / "gh-slate-extension-v2"
    install_root.mkdir(parents=True)
    install = install_root / f"{VERSION}-{digest}"
    install.symlink_to(install_root / "missing-stage")
    environment = os.environ.copy()
    environment["HOME"] = str(tmp_path / "home")
    environment["XDG_CACHE_HOME"] = str(cache)
    (tmp_path / "home").mkdir()

    def invoke() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [assets[0], "--version"],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=20,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = tuple(executor.map(lambda _index: invoke(), range(8)))

    assert all(result.returncode == 0 for result in results)
    assert all(result.stdout == "gh slate test\n" for result in results)
    assert all(result.stderr == "" for result in results)
    assert install.is_symlink()
    published_stage = install.resolve()
    assert (published_stage / ".ready").is_file()
    recovery = Path(f"{install}.recover")
    assert recovery.is_symlink()
    assert recovery.resolve() == published_stage
    private_stages = tuple(
        path.resolve()
        for path in install_root.iterdir()
        if path.name.startswith(f".{VERSION}-{digest}.stage.") and path.is_dir()
    )
    assert private_stages == (published_stage,)


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="the self-extracting extension assets require Bash and Unix symlinks",
)
def test_extension_asset_finishes_a_previously_elected_recovery(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    assets = release_verify.build_extension_assets(
        project,
        tmp_path / "assets",
        version=VERSION,
    )
    content = assets[0].read_bytes()
    digest_match = re.search(
        rb"(?m)^payload_sha256=([0-9a-f]{64})$",
        content[:4096],
    )
    assert digest_match is not None
    digest = digest_match.group(1).decode("ascii")

    cache = tmp_path / "cache"
    environment = os.environ.copy()
    environment["HOME"] = str(tmp_path / "home")
    environment["XDG_CACHE_HOME"] = str(cache)
    (tmp_path / "home").mkdir()
    initial = subprocess.run(
        [assets[0], "--version"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=15,
    )
    assert initial.returncode == 0, initial.stderr

    install_root = cache / "gh-slate-extension-v2"
    install = install_root / f"{VERSION}-{digest}"
    published_stage = install.resolve()
    recovery = Path(f"{install}.recover")
    recovery.symlink_to(published_stage)
    install.unlink()
    install.symlink_to(install_root / "missing-stage")

    recovered = subprocess.run(
        [assets[0], "--version"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=15,
    )

    assert recovered.returncode == 0, recovered.stderr
    assert recovered.stdout == "gh slate test\n"
    assert recovered.stderr == ""
    assert install.resolve() == published_stage
    assert recovery.resolve() == published_stage
    private_stages = tuple(
        path.resolve()
        for path in install_root.iterdir()
        if path.name.startswith(f".{VERSION}-{digest}.stage.") and path.is_dir()
    )
    assert private_stages == (published_stage,)


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="the self-extracting extension assets require Bash and Unix symlinks",
)
def test_extension_asset_re_elects_a_dangling_recovery_chain(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    assets = release_verify.build_extension_assets(
        project,
        tmp_path / "assets",
        version=VERSION,
    )
    digest_match = re.search(
        rb"(?m)^payload_sha256=([0-9a-f]{64})$",
        assets[0].read_bytes()[:4096],
    )
    assert digest_match is not None
    digest = digest_match.group(1).decode("ascii")

    cache = tmp_path / "cache"
    environment = os.environ.copy()
    environment["HOME"] = str(tmp_path / "home")
    environment["XDG_CACHE_HOME"] = str(cache)
    (tmp_path / "home").mkdir()

    def invoke() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [assets[0], "--version"],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=20,
        )

    initial = invoke()
    assert initial.returncode == 0, initial.stderr
    install_root = cache / "gh-slate-extension-v2"
    install = install_root / f"{VERSION}-{digest}"
    first_stage = install.resolve()
    recovery = Path(f"{install}.recover")
    recovery.symlink_to(first_stage)
    shutil.rmtree(first_stage)
    assert install.is_symlink() and not install.exists()
    assert recovery.is_symlink() and not recovery.exists()

    real_ln = shutil.which("ln")
    assert real_ln is not None
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_ln = fake_bin / "ln"
    fake_ln.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
destination="${@: -1}"
if [[ "${destination}" == "${GH_SLATE_DELAYED_RECOVERY_TERMINAL}" ]]; then
  if mkdir "${GH_SLATE_RECOVERY_CLAIM_GATE}" 2>/dev/null; then
    sleep 0.25
    exec "${GH_SLATE_REAL_LN}" "$@"
  fi
  exit 1
fi
exec "${GH_SLATE_REAL_LN}" "$@"
""",
        encoding="utf-8",
    )
    fake_ln.chmod(0o755)
    original_path = environment["PATH"]
    environment["PATH"] = f"{fake_bin}{os.pathsep}{original_path}"
    environment["GH_SLATE_REAL_LN"] = real_ln
    environment["GH_SLATE_DELAYED_RECOVERY_TERMINAL"] = str(first_stage)
    environment["GH_SLATE_RECOVERY_CLAIM_GATE"] = str(tmp_path / "recovery-claim-gate")

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = tuple(executor.map(lambda _index: invoke(), range(8)))

    assert all(result.returncode == 0 for result in results)
    assert all(result.stdout == "gh slate test\n" for result in results)
    assert all(result.stderr == "" for result in results)
    second_stage = install.resolve(strict=True)
    assert install.readlink() == second_stage
    assert recovery.resolve(strict=True) == second_stage
    assert recovery.readlink() == second_stage
    stage_entries = tuple(
        path for path in install_root.iterdir() if path.name.startswith(f".{VERSION}-{digest}.stage.")
    )
    assert stage_entries == (second_stage,)

    environment["PATH"] = original_path
    environment.pop("GH_SLATE_REAL_LN")
    environment.pop("GH_SLATE_DELAYED_RECOVERY_TERMINAL")
    environment.pop("GH_SLATE_RECOVERY_CLAIM_GATE")

    current_stage = second_stage
    for _attempt in range(20):
        shutil.rmtree(current_stage)
        with ThreadPoolExecutor(max_workers=4) as executor:
            repeated = tuple(executor.map(lambda _index: invoke(), range(4)))

        assert all(result.returncode == 0 for result in repeated), repeated
        assert all(result.stdout == "gh slate test\n" for result in repeated)
        assert all(result.stderr == "" for result in repeated)
        next_stage = install.resolve(strict=True)
        assert next_stage != current_stage
        assert install.readlink() == next_stage
        assert recovery.resolve(strict=True) == next_stage
        assert recovery.readlink() == next_stage
        stage_entries = tuple(
            path for path in install_root.iterdir() if path.name.startswith(f".{VERSION}-{digest}.stage.")
        )
        assert stage_entries == (next_stage,)
        current_stage = next_stage


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="the self-extracting extension assets require Bash and Unix symlinks",
)
def test_extension_asset_rejects_an_out_of_cache_recovery_target(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    assets = release_verify.build_extension_assets(
        project,
        tmp_path / "assets",
        version=VERSION,
    )
    digest_match = re.search(
        rb"(?m)^payload_sha256=([0-9a-f]{64})$",
        assets[0].read_bytes()[:4096],
    )
    assert digest_match is not None
    digest = digest_match.group(1).decode("ascii")
    cache = tmp_path / "cache"
    install_root = cache / "gh-slate-extension-v2"
    install_root.mkdir(parents=True)
    install = install_root / f"{VERSION}-{digest}"
    install.symlink_to(install_root / "missing-stage")
    outside = tmp_path / "outside" / "missing-stage"
    recovery = Path(f"{install}.recover")
    recovery.symlink_to(outside)
    environment = os.environ.copy()
    environment["HOME"] = str(tmp_path / "home")
    environment["XDG_CACHE_HOME"] = str(cache)
    (tmp_path / "home").mkdir()

    result = subprocess.run(
        [assets[0], "--version"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=20,
    )

    assert result.returncode != 0
    assert "extension cache publication failed" in result.stderr
    assert recovery.is_symlink()
    assert recovery.readlink() == outside
    assert not outside.exists()


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None or shutil.which("ln") is None,
    reason="the signal-race test requires Bash, ln, and Unix symlinks",
)
def test_extension_asset_preserves_a_stage_when_signalled_during_publish(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    assets = release_verify.build_extension_assets(
        project,
        tmp_path / "assets",
        version=VERSION,
    )
    content = assets[0].read_bytes()
    digest_match = re.search(
        rb"(?m)^payload_sha256=([0-9a-f]{64})$",
        content[:4096],
    )
    assert digest_match is not None
    digest = digest_match.group(1).decode("ascii")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_ln = fake_bin / "ln"
    fake_ln.write_text(
        ('#!/usr/bin/env bash\nset -euo pipefail\n"${GH_SLATE_REAL_LN}" "$@"\nkill -TERM "${PPID}"\n'),
        encoding="utf-8",
    )
    fake_ln.chmod(0o755)

    cache = tmp_path / "cache"
    environment = os.environ.copy()
    environment["HOME"] = str(tmp_path / "home")
    environment["XDG_CACHE_HOME"] = str(cache)
    environment["GH_SLATE_REAL_LN"] = cast("str", shutil.which("ln"))
    environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
    (tmp_path / "home").mkdir()

    interrupted = subprocess.run(
        [assets[0], "--version"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=15,
    )

    assert interrupted.returncode != 0
    install_root = cache / "gh-slate-extension-v2"
    install = install_root / f"{VERSION}-{digest}"
    assert install.is_symlink()
    published_stage = install.resolve()
    assert (published_stage / ".ready").is_file()
    assert not (published_stage / ".payload").exists()

    retried = subprocess.run(
        [assets[0], "--version"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=15,
    )

    assert retried.returncode == 0, retried.stderr
    assert retried.stdout == "gh slate test\n"
    assert install.resolve() == published_stage


def test_release_verification_writes_hashes_for_the_same_artifact_set(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    release_root = tmp_path / "release-artifacts"
    source_artifacts = _artifacts(tmp_path / "source")
    python_dir = release_root / "python"
    python_dir.mkdir(parents=True)
    wheel = python_dir / source_artifacts.wheel.name
    sdist = python_dir / source_artifacts.sdist.name
    wheel.write_bytes(source_artifacts.wheel.read_bytes())
    sdist.write_bytes(source_artifacts.sdist.read_bytes())
    manifest = release_root / "SHA256SUMS"

    version = release_verify.verify_release(
        project,
        python_dir,
        tag="v0.1.0",
        extension_dir=release_root / "github",
        manifest=manifest,
        smoke_installs=False,
    )

    assert version == VERSION
    lines = manifest.read_text(encoding="ascii").splitlines()
    assert len(lines) == 6
    assert any(line.endswith("  python/" + wheel.name) for line in lines)
    assert any(line.endswith("  python/" + sdist.name) for line in lines)
    for line in lines:
        digest, relative = line.split("  ", 1)
        assert digest == hashlib.sha256((release_root / relative).read_bytes()).hexdigest()


def test_candidate_intake_revalidates_identity_artifacts_and_manifest(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    release_root = tmp_path / "candidate"
    python_dir = release_root / "python"
    python_dir.mkdir(parents=True)
    source_artifacts = _artifacts(tmp_path / "source")
    artifacts = tuple(python_dir / source.name for source in (source_artifacts.wheel, source_artifacts.sdist))
    for source, destination in zip(source_artifacts.paths, artifacts, strict=True):
        shutil.copy2(source, destination)
    rebuilt_python = tmp_path / "rebuilt-python"
    rebuilt_python.mkdir()
    for source in artifacts:
        shutil.copy2(source, rebuilt_python / source.name)
    extensions = release_verify.build_extension_assets(
        project,
        release_root / "github",
        version=VERSION,
    )
    release_verify.write_sha256_manifest(
        (*artifacts, *extensions),
        base_dir=release_root,
        destination=release_root / "SHA256SUMS",
    )
    context = {
        "repository": "owner/gh-slate",
        "repository_id": "42",
        "run_id": "99",
        "run_attempt": "2",
        "sha": "a" * 40,
        "tag": "v0.1.0",
    }
    (release_root / "release-context.json").write_text(
        json.dumps(context, separators=(",", ":")) + "\n",
        encoding="ascii",
    )

    accepted = candidate_intake.verify_candidate(
        artifact_root=release_root,
        rebuilt_python_dir=rebuilt_python,
        project_root=project,
        expected_repository="owner/gh-slate",
        expected_repository_id="42",
        expected_run_id="99",
        expected_run_attempt="2",
        expected_sha="a" * 40,
        expected_tag="v0.1.0",
    )

    assert accepted.tag == "v0.1.0"
    (release_root / "SHA256SUMS").write_text("0" * 64 + "  unexpected\n", encoding="ascii")
    with pytest.raises(candidate_intake.CandidateIntakeError, match="manifest"):
        candidate_intake.verify_candidate(
            artifact_root=release_root,
            rebuilt_python_dir=rebuilt_python,
            project_root=project,
            expected_repository="owner/gh-slate",
            expected_repository_id="42",
            expected_run_id="99",
            expected_run_attempt="2",
            expected_sha="a" * 40,
            expected_tag="v0.1.0",
        )


def test_candidate_intake_binds_artifacts_to_the_independent_source_rebuild(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    release_root = tmp_path / "candidate"
    python_dir = release_root / "python"
    python_dir.mkdir(parents=True)
    source_artifacts = _artifacts(tmp_path / "source")
    candidate_artifacts = tuple(python_dir / source.name for source in source_artifacts.paths)
    rebuilt_python = tmp_path / "rebuilt-python"
    rebuilt_python.mkdir()
    for source, candidate in zip(
        source_artifacts.paths,
        candidate_artifacts,
        strict=True,
    ):
        shutil.copy2(source, candidate)
        shutil.copy2(source, rebuilt_python / source.name)
    extensions = release_verify.build_extension_assets(
        project,
        release_root / "github",
        version=VERSION,
    )
    candidate_artifacts[0].write_bytes(candidate_artifacts[0].read_bytes() + b"replaced")
    release_verify.write_sha256_manifest(
        (*candidate_artifacts, *extensions),
        base_dir=release_root,
        destination=release_root / "SHA256SUMS",
    )
    context = {
        "repository": "owner/gh-slate",
        "repository_id": "42",
        "run_id": "99",
        "run_attempt": "2",
        "sha": "a" * 40,
        "tag": "v0.1.0",
    }
    (release_root / "release-context.json").write_text(
        json.dumps(context, separators=(",", ":")) + "\n",
        encoding="ascii",
    )

    with pytest.raises(candidate_intake.CandidateIntakeError, match="source rebuild"):
        candidate_intake.verify_candidate(
            artifact_root=release_root,
            rebuilt_python_dir=rebuilt_python,
            project_root=project,
            expected_repository="owner/gh-slate",
            expected_repository_id="42",
            expected_run_id="99",
            expected_run_attempt="2",
            expected_sha="a" * 40,
            expected_tag="v0.1.0",
        )

    shutil.copy2(source_artifacts.wheel, candidate_artifacts[0])
    replacement_project = _project(tmp_path / "replacement")
    (replacement_project / "src" / "gh_slate" / "__main__.py").write_text(
        "raise SystemExit('replacement payload')\n",
        encoding="utf-8",
    )
    shutil.rmtree(release_root / "github")
    replacement_extensions = release_verify.build_extension_assets(
        replacement_project,
        release_root / "github",
        version=VERSION,
    )
    release_verify.write_sha256_manifest(
        (*candidate_artifacts, *replacement_extensions),
        base_dir=release_root,
        destination=release_root / "SHA256SUMS",
    )

    with pytest.raises(
        candidate_intake.CandidateIntakeError,
        match="candidate extension artifact is not the source rebuild",
    ):
        candidate_intake.verify_candidate(
            artifact_root=release_root,
            rebuilt_python_dir=rebuilt_python,
            project_root=project,
            expected_repository="owner/gh-slate",
            expected_repository_id="42",
            expected_run_id="99",
            expected_run_attempt="2",
            expected_sha="a" * 40,
            expected_tag="v0.1.0",
        )


def test_candidate_intake_binds_the_tag_to_the_canonical_triggering_run(
    tmp_path: Path,
) -> None:
    workflow = tmp_path / "workflow.json"
    run = tmp_path / "run.json"
    workflow.write_text(
        json.dumps(
            {
                "id": 123,
                "path": ".github/workflows/release-candidate.yml",
                "state": "active",
            }
        ),
        encoding="utf-8",
    )
    run_payload = {
        "id": 99,
        "workflow_id": 123,
        "run_attempt": 2,
        "path": ".github/workflows/release-candidate.yml",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "head_sha": "a" * 40,
        "head_branch": "v0.1.0",
        "repository": {"id": 42, "full_name": "owner/gh-slate"},
        "head_repository": {"id": 42, "full_name": "owner/gh-slate"},
    }
    run.write_text(json.dumps(run_payload), encoding="utf-8")

    assert (
        candidate_intake.verify_triggering_run(
            workflow_path=workflow,
            run_path=run,
            expected_repository="owner/gh-slate",
            expected_repository_id="42",
            expected_workflow_id="123",
            expected_run_id="99",
            expected_run_attempt="2",
            expected_sha="a" * 40,
        )
        == "v0.1.0"
    )

    run_payload["workflow_id"] = 124
    run.write_text(json.dumps(run_payload), encoding="utf-8")
    with pytest.raises(candidate_intake.CandidateIntakeError, match="identity"):
        candidate_intake.verify_triggering_run(
            workflow_path=workflow,
            run_path=run,
            expected_repository="owner/gh-slate",
            expected_repository_id="42",
            expected_workflow_id="123",
            expected_run_id="99",
            expected_run_attempt="2",
            expected_sha="a" * 40,
        )


def test_release_candidate_tests_before_build_and_never_holds_publish_credentials() -> None:
    candidate = _candidate_workflow()
    jobs = candidate["jobs"]
    gate = jobs["release-gate"]
    names = [step["name"] for step in gate["steps"] if "name" in step]

    assert names.index("Run formatting and static gates") < names.index("Build wheel and sdist once")
    assert names.index("Run the complete deterministic suite") < names.index("Build wheel and sdist once")
    assert names.index("Build wheel and sdist once") < names.index(
        "Verify metadata, clean installs, CLIs, and extension bundles"
    )
    assert names.index("Verify metadata, clean installs, CLIs, and extension bundles") < names.index(
        "Upload the one verified candidate artifact set"
    )

    all_run_text = "\n".join(_run_text(job) for job in jobs.values())
    assert all_run_text.count("uv build") == 1
    assert "uv build --no-sources --offline" in all_run_text
    assert "uv publish" not in all_run_text
    assert "scripts/release_verify.py" in _run_text(gate)
    assert "gh skill publish --dry-run ." in _run_text(gate)
    assert "gh skill install . gh-slate" in _run_text(gate)
    assert "--from-local" in _run_text(gate)
    assert '--dir "${SKILL_INSTALL_DIR}"' in _run_text(gate)
    assert "scripts/check_skill.py" in _run_text(gate)
    assert '"${SKILL_INSTALL_DIR}/gh-slate"' in _run_text(gate)
    assert gate["permissions"] == {"contents": "read"}
    setup_uv = next(step for step in gate["steps"] if step.get("uses") == SETUP_UV_ACTION)
    assert setup_uv["with"]["version"] == "0.11.28"
    build = next(step for step in gate["steps"] if step.get("name") == "Build wheel and sdist once")
    assert build["env"] == {"SOURCE_DATE_EPOCH": "0"}
    assert "${{ secrets." not in repr(candidate)
    assert "id-token" not in repr(candidate)
    assert "contents: write" not in CANDIDATE_WORKFLOW.read_text(encoding="utf-8")


def test_release_workflow_runs_only_from_the_default_branch_and_revalidates_upstream() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    intake = jobs["intake"]
    source_rebuild = jobs["source-rebuild"]

    assert workflow["on"] == {
        "workflow_run": {
            "workflows": ["Release Candidate"],
            "types": ["completed"],
        }
    }
    assert workflow["permissions"] == {}
    assert workflow["concurrency"] == {
        "group": "gh-slate-release",
        "cancel-in-progress": "false",
    }
    assert "workflow_run.conclusion == 'success'" in intake["if"]
    assert "workflow_run.event == 'push'" in intake["if"]
    assert "head_repository.full_name == github.repository" in intake["if"]
    assert intake["needs"] == "source-rebuild"
    assert intake["permissions"] == {"actions": "read", "contents": "read"}
    checkout = next(step for step in intake["steps"] if step.get("uses") == CHECKOUT_ACTION)
    assert checkout["with"] == {
        "ref": "${{ github.workflow_sha }}",
        "fetch-depth": "0",
        "persist-credentials": "false",
    }
    intake_text = _run_text(intake)
    assert 'git merge-base --is-ancestor "${CANDIDATE_SHA}" "${default_ref}"' in intake_text
    assert 'git worktree add --detach candidate-source "${CANDIDATE_SHA}"' in intake_text
    assert "scripts/intake_release_candidate.py" in intake_text
    assert "actions/workflows/release-candidate.yml" in intake_text
    assert "actions/runs/${CANDIDATE_RUN_ID}" in intake_text
    assert "--workflow-metadata candidate-workflow.json" in intake_text
    assert "--workflow-run-metadata triggering-run.json" in intake_text
    assert '--expected-workflow-id "${CANDIDATE_WORKFLOW_ID}"' in intake_text
    assert '--expected-run-attempt "${CANDIDATE_RUN_ATTEMPT}"' in intake_text
    upstream_download = next(
        step for step in intake["steps"] if step.get("name") == "Download only the triggering run's candidate"
    )
    assert upstream_download["uses"] == DOWNLOAD_ARTIFACT_ACTION
    assert upstream_download["with"]["run-id"] == "${{ github.event.workflow_run.id }}"
    assert upstream_download["with"]["github-token"] == "${{ github.token }}"
    assert "git/ref/tags/${RELEASE_TAG}" in intake_text
    assert '.object.type == "commit"' in intake_text

    assert source_rebuild["permissions"] == {"contents": "read"}
    rebuild_checkout = next(step for step in source_rebuild["steps"] if step.get("uses") == CHECKOUT_ACTION)
    assert rebuild_checkout["with"] == {
        "ref": "${{ github.event.workflow_run.head_sha }}",
        "persist-credentials": "false",
    }
    rebuild_text = _run_text(source_rebuild)
    assert "uv build --offline --no-sources" in rebuild_text
    assert "source-rebuilt-python" in repr(source_rebuild["steps"])
    rebuild_uv = next(step for step in source_rebuild["steps"] if step.get("uses") == SETUP_UV_ACTION)
    assert rebuild_uv["with"]["version"] == "0.11.28"
    rebuilt_download = next(
        step for step in intake["steps"] if step.get("name") == "Download the independent source rebuild"
    )
    assert rebuilt_download["uses"] == DOWNLOAD_ARTIFACT_ACTION
    assert "${{ secrets." not in repr(workflow)
    assert "environment" not in repr(workflow)


def test_release_workflow_consumes_one_verified_set_in_safe_order() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    staged_release = jobs["stage-release"]
    pypi = jobs["publish-pypi"]
    github_release = jobs["publish-release"]
    extension_smoke = jobs["extension-smoke"]

    assert extension_smoke["needs"] == "intake"
    assert staged_release["needs"] == [
        "intake",
        "live-gate",
        "extension-smoke",
    ]
    assert pypi["needs"] == [
        "intake",
        "stage-release",
        "extension-smoke",
        "live-gate",
    ]
    assert github_release["needs"] == [
        "intake",
        "stage-release",
        "extension-smoke",
        "live-gate",
        "publish-pypi",
    ]

    for job in (staged_release, pypi, github_release):
        uses = [step.get("uses", "") for step in job["steps"]]
        assert DOWNLOAD_ARTIFACT_ACTION in uses
        assert "trusted-release-artifacts" in repr(job["steps"])
        assert "sha256sum --check SHA256SUMS" in _run_text(job)
        assert "uv build" not in _run_text(job)

    staged_step = next(step for step in staged_release["steps"] if step.get("uses") == RELEASE_ACTION)
    assert staged_step["with"]["draft"] == "true"
    assert staged_step["with"]["prerelease"] == "false"
    assert staged_step["with"]["make_latest"] == "false"
    assert staged_step["with"]["target_commitish"] == "${{ needs.intake.outputs.sha }}"
    assert "release-artifacts/github/gh-slate-*" in staged_step["with"]["files"]
    staged_text = _run_text(staged_release)
    assert "immutable-releases" not in staged_text
    assert "X-GitHub-Api-Version: 2026-03-10" in staged_text
    assert "staged-assets.txt" in staged_text
    assert "expected_state=$'true\\tfalse'" in staged_text
    assert "expected_state=$'false\\tfalse'" in staged_text

    smoke_text = _run_text(extension_smoke)
    assert "gh-slate-linux-amd64" in smoke_text
    assert 'test "$("${asset}" --version)"' in smoke_text
    assert '"${asset}" --help' in smoke_text

    promotion_text = _run_text(github_release)
    assert "gh release download" in promotion_text
    assert "expected_assets=(" in promotion_text
    assert "released-assets.txt" in promotion_text
    assert "cmp expected-assets.txt released-assets.txt" in promotion_text
    assert "cmp " in promotion_text
    assert "gh release edit" in promotion_text
    assert "--draft=false" in promotion_text
    assert "--latest" in promotion_text
    assert "softprops/action-gh-release" not in repr(github_release["steps"])

    assert pypi["permissions"] == {
        "actions": "read",
        "contents": "read",
        "id-token": "write",
    }
    assert not any(step.get("uses") == CHECKOUT_ACTION for step in pypi["steps"])
    for job in (staged_release, github_release):
        assert not any(step.get("uses") == CHECKOUT_ACTION for step in job["steps"])


def test_release_workflow_rerun_preserves_an_existing_stable_release() -> None:
    jobs = _workflow()["jobs"]
    staged_release = jobs["stage-release"]
    pypi = jobs["publish-pypi"]
    github_release = jobs["publish-release"]

    assert staged_release["outputs"]["already_stable"] == ("${{ steps.release_state.outputs.already_stable }}")
    inspect = next(step for step in staged_release["steps"] if step.get("id") == "release_state")
    inspect_text = str(inspect["run"])
    assert "releases/tags/${RELEASE_TAG}" in inspect_text
    assert 'echo "already_stable=true"' in inspect_text
    assert "absent|draft)" in inspect_text
    assert "prerelease)" in inspect_text

    guarded_names = {
        "Stage a draft release without executing candidate code",
    }
    guarded_steps = [step for step in staged_release["steps"] if step.get("name") in guarded_names]
    assert len(guarded_steps) == len(guarded_names)
    assert all(step["if"] == "steps.release_state.outputs.already_stable != 'true'" for step in guarded_steps)
    for name in (
        "Download the trusted artifact set",
        "Recheck exact artifact hashes",
        "Prove the GitHub Release contains only the accepted files",
    ):
        step = next(item for item in staged_release["steps"] if item.get("name") == name)
        assert "if" not in step

    pypi_publish = next(step for step in pypi["steps"] if step.get("uses") == PYPI_ACTION)
    assert pypi_publish["with"]["skip-existing"] == "true"
    pypi_text = _run_text(pypi)
    assert "https://pypi.org/pypi/" in pypi_text
    assert "hashlib.sha256" in pypi_text
    assert "actual == expected" in pypi_text

    promotion = next(
        step for step in github_release["steps"] if step.get("name") == "Publish the draft only after every gate"
    )
    assert promotion["if"] == ("needs.stage-release.outputs.already_stable != 'true'")
    assert any(step.get("name") == "Assert the tagged release is stable" for step in github_release["steps"])


def test_workflow_dispatch_can_verify_a_candidate_but_cannot_enter_the_publisher() -> None:
    candidate = _candidate_workflow()
    assert "workflow_dispatch" in candidate["on"]
    assert candidate["on"]["push"] == {"tags": ["v*"]}

    publisher = _workflow()
    assert set(publisher["on"]) == {"workflow_run"}
    gate_text = _run_text(candidate["jobs"]["release-gate"])
    assert ('if [[ "${GITHUB_EVENT_NAME}" == "push" && "${GITHUB_REF_TYPE}" == "tag" ]]') in gate_text
    assert "release-context.json" in gate_text


def test_all_repository_workflow_actions_use_immutable_commit_shas() -> None:
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        workflow = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        assert isinstance(workflow, dict)
        permissions = workflow.get("permissions")
        assert permissions is not None, f"{path.name} has no explicit top-level permissions"
        jobs = workflow.get("jobs")
        assert isinstance(jobs, dict)
        for job in jobs.values():
            assert isinstance(job, dict)
            steps = job.get("steps", [])
            assert isinstance(steps, list)
            for step in steps:
                assert isinstance(step, dict)
                reference = step.get("uses")
                if reference is None:
                    continue
                assert isinstance(reference, str)
                assert re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", reference), (
                    f"{path.name} contains a mutable action reference: {reference}"
                )


@pytest.mark.skipif(shutil.which("bash") is None, reason="workflow shell validation requires Bash")
def test_release_workflow_shell_blocks_are_syntactically_valid() -> None:
    for workflow in (_candidate_workflow(), _workflow()):
        for job in workflow["jobs"].values():
            for step in job["steps"]:
                source = step.get("run")
                if not isinstance(source, str):
                    continue
                result = subprocess.run(
                    ["bash", "-n"],
                    input=source,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                assert result.returncode == 0, result.stderr


def test_local_release_requires_main_and_a_clean_worktree() -> None:
    justfile = JUSTFILE.read_text(encoding="utf-8")
    release_recipe = justfile.split("\nrelease:\n", 1)[1].split("\npublish:\n", 1)[0]

    clean_guard = "git status --porcelain=v1 --untracked-files=all"
    branch_guard = 'git branch --show-current)" = "main"'
    assert branch_guard in release_recipe
    assert clean_guard in release_recipe
    assert release_recipe.index(branch_guard) < release_recipe.index(clean_guard)
    assert release_recipe.index(clean_guard) < release_recipe.index("just ci-fmt-check")
    assert 'git push origin "v{{VERSION}}"' in release_recipe


def test_extension_bundle_input_rejects_symlinks(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    launcher = project / "gh-slate"
    launcher.unlink()
    try:
        launcher.symlink_to(project / "README.md")
    except OSError:
        pytest.skip("filesystem does not support symlinks")

    with pytest.raises(
        release_verify.ReleaseVerificationError,
        match="regular file",
    ):
        release_verify.build_extension_assets(
            project,
            tmp_path / "github",
            version=VERSION,
        )


def test_manifest_rejects_paths_outside_the_release_root(
    tmp_path: Path,
) -> None:
    inside = tmp_path / "release" / "inside"
    inside.parent.mkdir()
    inside.write_bytes(b"inside")
    outside = tmp_path / "outside"
    outside.write_bytes(os.urandom(8))

    with pytest.raises(
        release_verify.ReleaseVerificationError,
        match="outside",
    ):
        release_verify.write_sha256_manifest(
            (inside, outside),
            base_dir=inside.parent,
            destination=inside.parent / "SHA256SUMS",
        )
