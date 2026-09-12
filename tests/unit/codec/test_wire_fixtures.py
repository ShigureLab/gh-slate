from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from gh_slate.codec.comment import decode_comment
from gh_slate.codec.hashes import canonical_state_bytes, render_sha256, state_sha256
from gh_slate.codec.json import canonical_json_bytes, strict_loads
from gh_slate.codec.model import MAX_REVISION, State
from gh_slate.rendering import render_state

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "wire" / "state"
MANIFEST = FIXTURE_ROOT / "manifest.json"
ROOT_FILES = {"README.md", "manifest.json"}
REQUIRED_FIXTURE_FILES = {
    "comment.md",
    "expected.json",
    "state.canonical.json",
    "visible.md",
}
OPTIONAL_FIXTURE_FILES = {"source.state.json"}


def _manifest() -> dict[str, object]:
    value = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _fixture_names() -> tuple[str, ...]:
    fixtures = _manifest()["fixtures"]
    assert isinstance(fixtures, dict)
    typed_fixtures = cast("dict[str, object]", fixtures)
    return tuple(sorted(typed_fixtures))


def _fixture_dir(name: str) -> Path:
    return FIXTURE_ROOT / name


def _canonical_fixture(name: str) -> bytes:
    stored = _fixture_dir(name).joinpath("state.canonical.json").read_bytes()
    assert stored.endswith(b"\n")
    assert not stored.endswith(b"\n\n")
    return stored.removesuffix(b"\n")


def test_state_fixture_files_match_the_manifest() -> None:
    manifest = _manifest()
    fixtures = manifest["fixtures"]
    assert isinstance(fixtures, dict)
    typed_fixtures = cast("dict[str, dict[str, str]]", fixtures)

    assert manifest["format"] == "gh-slate/state"
    assert {path.name for path in FIXTURE_ROOT.iterdir() if path.is_file()} == ROOT_FILES
    assert {path.name for path in FIXTURE_ROOT.iterdir() if path.is_dir()} == set(typed_fixtures)

    for fixture_name, registered_files in typed_fixtures.items():
        assert isinstance(fixture_name, str)
        assert isinstance(registered_files, dict)
        fixture_dir = _fixture_dir(fixture_name)
        actual_files = {path.name for path in fixture_dir.iterdir() if path.is_file()}
        assert all(path.is_file() for path in fixture_dir.iterdir())
        assert REQUIRED_FIXTURE_FILES <= actual_files
        assert actual_files <= REQUIRED_FIXTURE_FILES | OPTIONAL_FIXTURE_FILES
        assert set(registered_files) == actual_files
        for name, expected_sha256 in registered_files.items():
            assert isinstance(name, str)
            assert isinstance(expected_sha256, str)
            assert hashlib.sha256(fixture_dir.joinpath(name).read_bytes()).hexdigest() == expected_sha256


@pytest.mark.parametrize("fixture_name", _fixture_names())
def test_state_wire_fixture_remains_decodable_with_stable_canonical_state(
    fixture_name: str,
) -> None:
    fixture_dir = _fixture_dir(fixture_name)
    canonical_fixture = _canonical_fixture(fixture_name)
    visible = fixture_dir.joinpath("visible.md").read_text(encoding="utf-8")
    stored_comment = fixture_dir.joinpath("comment.md").read_text(encoding="utf-8")
    expected = json.loads(fixture_dir.joinpath("expected.json").read_text(encoding="utf-8"))

    state = State.from_json(strict_loads(canonical_fixture))
    decoded = decode_comment(stored_comment)

    assert canonical_state_bytes(state) == canonical_fixture
    assert state_sha256(state) == expected["state_sha256"]
    assert render_sha256(visible) == expected["render_sha256"]
    assert canonical_state_bytes(decoded.state) == canonical_fixture
    assert decoded.visible_markdown == visible
    assert decoded.state_sha256 == expected["state_sha256"]
    assert decoded.expected_render_sha256 == expected["render_sha256"]
    assert decoded.actual_render_sha256 == expected["render_sha256"]
    assert asdict(decoded.sizes) == expected["sizes"]
    assert decoded.drifted is False
    assert render_state(decoded.state).markdown == visible


@pytest.mark.parametrize("fixture_name", _fixture_names())
def test_optional_source_state_canonicalizes_to_the_wire_contract(
    fixture_name: str,
) -> None:
    fixture_dir = _fixture_dir(fixture_name)
    source = fixture_dir / "source.state.json"
    if not source.exists():
        pytest.skip("fixture has no intentionally non-canonical source state")

    assert canonical_json_bytes(strict_loads(source.read_bytes())) == _canonical_fixture(fixture_name)


def test_full_types_fixture_preserves_the_type_and_presence_matrix() -> None:
    decoded = decode_comment(_fixture_dir("full-types").joinpath("comment.md").read_text(encoding="utf-8"))
    state = decoded.state
    data = state.data
    nested = data["nested"]
    assert isinstance(nested, Mapping)
    typed_nested = cast("Mapping[str, object]", nested)
    items = typed_nested["items"]
    assert isinstance(items, tuple)
    assert isinstance(items[0], Mapping)
    assert isinstance(items[1], Mapping)
    first_item = cast("Mapping[str, object]", items[0])
    second_item = cast("Mapping[str, object]", items[1])

    assert state.revision == MAX_REVISION
    assert state.data_schema is not None
    assert state.data_schema.document
    assert state.renderer.config["source"]
    assert data["unicode"] == "雪 · café · 👩‍💻"
    assert data["bool_true"] is True
    assert data["bool_false"] is False
    assert data["empty_array"] == ()
    assert data["empty_object"] == {}
    assert data["null_value"] is None
    assert first_item["note"] is None
    assert "note" not in second_item
    assert data["negative_zero"] == Decimal(0)
    assert data["plain_small_boundary"] == Decimal("0.000001")
    assert data["exponent_small"] == Decimal("1e-7")
    assert data["plain_large_boundary"] == Decimal("1e20")
    assert data["exponent_large"] == Decimal("1e21")
