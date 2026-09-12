from __future__ import annotations

from decimal import Decimal

import pytest

from gh_slate.codec import CodecError, MetaSnapshot


def _meta() -> dict:
    return {
        "host": "github.com",
        "repository": {
            "owner": "owner",
            "name": "repo",
            "full_name": "owner/repo",
            "url": "https://github.com/owner/repo",
        },
        "target": {"kind": "issue", "number": 42, "id": "I_example", "url": "https://github.com/owner/repo/issues/42"},
        "slate": {"name": "ci"},
    }


def test_meta_freezes_input_and_returns_independent_json() -> None:
    original = _meta()
    snapshot = MetaSnapshot(original)
    original["target"]["id"] = "other"
    assert snapshot.target_id == "I_example"
    exported = snapshot.to_json()
    target = exported["target"]
    assert isinstance(target, dict)
    target["id"] = "another"
    assert snapshot.target_id == "I_example"
    assert MetaSnapshot.from_json(snapshot.to_json()) == snapshot


@pytest.mark.parametrize("number", [True, 0, -1, 1.5, "42", Decimal("NaN"), Decimal("Infinity"), Decimal("1.5"), 2**63])
def test_meta_rejects_non_integer_target_numbers(number) -> None:
    value = _meta()
    value["target"]["number"] = number
    with pytest.raises(CodecError):
        MetaSnapshot(value)


@pytest.mark.parametrize(
    ("field", "key", "value"),
    [
        ("target", "kind", "review"),
        ("target", "id", 42),
        ("target", "url", "https://github.com/owner/repo/issues/99"),
        ("repository", "full_name", "another/repo"),
        ("repository", "url", "https://elsewhere/owner/repo"),
    ],
)
def test_meta_fields_must_describe_one_target(field, key, value) -> None:
    document = _meta()
    document[field][key] = value
    with pytest.raises(CodecError):
        MetaSnapshot(document)


def test_meta_has_no_extension_or_partial_identity_fields() -> None:
    value = _meta()
    value["revision"] = 1
    with pytest.raises(CodecError):
        MetaSnapshot(value)
    value = MetaSnapshot.local("ci").to_json()
    value["host"] = "github.com"
    with pytest.raises(CodecError):
        MetaSnapshot(value)
