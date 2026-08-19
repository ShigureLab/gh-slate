from __future__ import annotations

import hashlib
import importlib.util
import io
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
VERSION = "0.1.0"
_SPEC = importlib.util.spec_from_file_location(
    "gh_slate_release_verify",
    ROOT / "scripts" / "release_verify.py",
)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
release_verify = cast("Any", _MODULE)


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
source="${@: -2:1}"
if [[ "${destination}" == "${GH_SLATE_DELAYED_RECOVERY_TERMINAL}" ]]; then
  if mkdir "${GH_SLATE_RECOVERY_CLAIM_GATE}" 2>/dev/null; then
    sleep 0.25
    exec "${GH_SLATE_REAL_LN}" "$@"
  fi
  exit 1
fi
if [[ "${destination}" == */.publish && "${source}" == "${destination%/.publish}" && ! -L "${GH_SLATE_DELAYED_RECOVERY_TERMINAL}" ]]; then
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
def test_extension_asset_preserves_multihop_install_chain_when_publication_fails(
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
    first_bridge = install.resolve(strict=True)
    terminal_bridge = install_root / f".{VERSION}-{digest}.stage.multihop"
    recovery = Path(f"{install}.recover")
    recovery.symlink_to(terminal_bridge)
    shutil.rmtree(first_bridge)
    first_bridge.symlink_to(terminal_bridge)
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
source="${@: -2:1}"
if [[ "${destination}" == */.publish && "${source}" == "${destination%/.publish}" ]]; then
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

    interrupted = invoke()

    assert interrupted.returncode != 0
    recovered_stage = install.resolve(strict=True)
    assert (install / ".ready").is_file()
    assert first_bridge.is_symlink()
    assert terminal_bridge.is_symlink()
    assert recovery.resolve(strict=True) == recovered_stage

    environment["PATH"] = original_path
    environment.pop("GH_SLATE_REAL_LN")
    recovered = invoke()
    assert recovered.returncode == 0, recovered.stderr
    assert recovered.stdout == "gh slate test\n"


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


def test_release_workflow_has_one_tag_trigger_and_a_minimal_oidc_job() -> None:
    workflow = _workflow()
    assert workflow["on"] == {"push": {"tags": ["v*"]}}
    assert workflow["permissions"] == {}
    jobs = workflow["jobs"]
    assert list(jobs) == [
        "build",
        "live-gate",
        "extension-smoke",
        "stage-release",
        "publish-pypi",
        "publish-release",
    ]
    assert jobs["build"]["permissions"] == {"contents": "read"}
    publisher = jobs["publish-pypi"]
    assert publisher["needs"] == "stage-release"
    assert publisher["permissions"] == {"id-token": "write"}
    assert len(publisher["steps"]) == 2
    assert publisher["steps"][1]["uses"].startswith("pypa/gh-action-pypi-publish@")


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="workflow shell validation requires Unix Bash",
)
def test_release_workflow_shell_blocks_are_syntactically_valid() -> None:
    for job in _workflow()["jobs"].values():
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
