from __future__ import annotations

from decimal import Decimal
from typing import cast

import pytest

from gh_slate.codec.errors import CodecError
from gh_slate.codec.model import (
    JSON_SCHEMA_DIALECT_2020_12,
    MAX_RENDERER_VERSION,
    MAX_REVISION,
    STATE_FORMAT_V1,
    ControllerV1,
    RendererDescriptorV1,
    SchemaSnapshotV1,
    StateDraftV1,
    StateV1,
)

_DIGEST = "a" * 64


def _state_json() -> dict[str, object]:
    return {
        "format": STATE_FORMAT_V1,
        "name": "ci-summary",
        "revision": 7,
        "controller": {"login": "github-actions[bot]"},
        "data": {
            "jobs": [{"name": "linux", "passed": True, "note": None}],
            "empty": [],
        },
        "data_schema": {
            "dialect": JSON_SCHEMA_DIALECT_2020_12,
            "document": False,
        },
        "renderer": {
            "kind": "future-dashboard",
            "version": 99,
            "selector": ".jobs",
            "future": {"enabled": True, "values": [1, "001"]},
        },
        "render_sha256": _DIGEST,
    }


def test_state_round_trips_and_preserves_unknown_renderer_configuration() -> None:
    raw = _state_json()

    state = StateV1.from_json(raw)

    assert state.to_json() == raw
    assert state.renderer.kind == "future-dashboard"
    assert state.renderer.version == 99
    assert state.renderer.configuration["future"] == {"enabled": True, "values": (1, "001")}
    assert StateV1.from_json(state.to_json()) == state


def test_boolean_schema_is_a_valid_snapshot() -> None:
    false_schema = SchemaSnapshotV1(
        dialect=JSON_SCHEMA_DIALECT_2020_12,
        document=False,
    )
    true_schema = SchemaSnapshotV1.from_json(
        {
            "dialect": JSON_SCHEMA_DIALECT_2020_12,
            "document": True,
        }
    )

    assert false_schema.to_json()["document"] is False
    assert true_schema.document is True


@pytest.mark.parametrize(
    "name",
    [
        "",
        "Uppercase",
        "-leading",
        "has--delimiter",
        "space here",
        "a" * 65,
    ],
)
def test_state_rejects_invalid_names(name: str) -> None:
    raw = _state_json()
    raw["name"] = name

    with pytest.raises(CodecError, match="name must match|non-empty"):
        StateV1.from_json(raw)


@pytest.mark.parametrize("revision", [0, -1, MAX_REVISION + 1, True, "1"])
def test_state_rejects_invalid_revision(revision: object) -> None:
    raw = _state_json()
    raw["revision"] = revision

    with pytest.raises(CodecError, match="revision must be"):
        StateV1.from_json(raw)


def test_state_accepts_revision_bounds() -> None:
    low = _state_json()
    high = _state_json()
    low["revision"] = 1
    high["revision"] = MAX_REVISION

    assert StateV1.from_json(low).revision == 1
    assert StateV1.from_json(high).revision == MAX_REVISION


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("revision", Decimal("1e+999999999")),
        (
            "renderer.version",
            Decimal("1e+999999999"),
        ),
    ],
)
def test_wire_integers_reject_huge_exponents_before_integer_materialization(
    field: str,
    value: Decimal,
) -> None:
    raw = _state_json()
    if field == "revision":
        raw["revision"] = value
    else:
        renderer = cast("dict[str, object]", raw["renderer"])
        renderer["version"] = value

    with pytest.raises(CodecError, match="must be between") as captured:
        StateV1.from_json(raw)

    assert captured.value.code == "invalid_state"


def test_renderer_version_has_explicit_wire_bounds() -> None:
    raw = _state_json()
    renderer = cast("dict[str, object]", raw["renderer"])
    renderer["version"] = MAX_RENDERER_VERSION

    assert StateV1.from_json(raw).renderer.version == MAX_RENDERER_VERSION

    renderer["version"] = MAX_RENDERER_VERSION + 1
    with pytest.raises(CodecError, match="renderer.version must be between"):
        StateV1.from_json(raw)


def test_state_rejects_unknown_top_level_fields() -> None:
    raw = _state_json()
    raw["state_sha256"] = "b" * 64

    with pytest.raises(CodecError) as captured:
        StateV1.from_json(raw)

    assert captured.value.code == "unknown_state_field"
    assert captured.value.details["fields"] == ["state_sha256"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("format", "gh-slate/state-v2", "unsupported state format"),
        ("render_sha256", "A" * 64, "lowercase 64-character"),
        ("render_sha256", "short", "lowercase 64-character"),
        ("data", [], "data must be a JSON object"),
    ],
)
def test_state_rejects_invalid_wire_fields(field: str, value: object, message: str) -> None:
    raw = _state_json()
    raw[field] = value

    with pytest.raises(CodecError, match=message):
        StateV1.from_json(raw)


def test_draft_round_trips_without_a_revision_and_materializes_state() -> None:
    raw = _state_json()
    del raw["revision"]

    draft = StateDraftV1.from_json(raw)
    state = draft.with_revision(3)

    assert draft.to_json() == raw
    assert "revision" not in draft.to_json()
    assert state.revision == 3
    assert state.to_draft() == draft


def test_absent_schema_has_one_canonical_null_representation() -> None:
    raw = _state_json()
    raw["data_schema"] = None

    state = StateV1.from_json(raw)

    assert state.data_schema is None
    assert state.to_json()["data_schema"] is None

    del raw["data_schema"]
    with pytest.raises(CodecError) as captured:
        StateV1.from_json(raw)

    assert captured.value.code == "missing_state_field"


def test_nested_structures_are_frozen_from_caller_mutation() -> None:
    data = {"items": [{"value": 1}]}
    state = StateV1(
        name="immutable",
        revision=1,
        controller=ControllerV1(login="octocat"),
        data=data,
        renderer=RendererDescriptorV1(
            kind="builtin-list",
            version=1,
            config={"selector": ".items"},
        ),
        render_sha256=_DIGEST,
    )

    data["items"] = []

    assert state.to_json()["data"] == {"items": [{"value": 1}]}
