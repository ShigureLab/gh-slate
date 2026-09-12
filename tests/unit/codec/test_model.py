from __future__ import annotations

from decimal import Decimal
from typing import cast

import pytest

from gh_slate.codec.errors import CodecError
from gh_slate.codec.meta import MetaSnapshot
from gh_slate.codec.model import (
    JSON_SCHEMA_DIALECT_2020_12,
    MAX_GITHUB_LOGIN_BYTES,
    MAX_GITHUB_USER_ID,
    MAX_REVISION,
    STATE_FORMAT,
    Controller,
    RendererDescriptor,
    SchemaSnapshot,
    State,
    StateDraft,
)

_DIGEST = "a" * 64


def _state_json() -> dict[str, object]:
    return {
        "format": STATE_FORMAT,
        "name": "ci-summary",
        "meta": MetaSnapshot.local("ci-summary").to_json(),
        "revision": 7,
        "controller": {
            "id": 41898282,
            "login": "github-actions[bot]",
        },
        "data": {
            "jobs": [{"name": "linux", "passed": True, "note": None}],
            "empty": [],
        },
        "data_schema": {
            "dialect": JSON_SCHEMA_DIALECT_2020_12,
            "document": False,
        },
        "renderer": {"source": "{{ data.jobs | md_table }}", "profile": "ci"},
        "render_sha256": _DIGEST,
    }


@pytest.mark.parametrize(
    "login",
    [
        "a" * (MAX_GITHUB_LOGIN_BYTES + 1),
        "猫" * 14,
    ],
)
def test_controller_login_is_bounded_by_the_github_actor_contract(
    login: str,
) -> None:
    assert Controller(id=1, login="a" * MAX_GITHUB_LOGIN_BYTES).login == ("a" * MAX_GITHUB_LOGIN_BYTES)

    with pytest.raises(CodecError) as caught:
        Controller(id=1, login=login)

    assert caught.value.code == "invalid_state"
    assert caught.value.details == {"path": "controller.login"}


def test_state_round_trips_with_stored_template() -> None:
    raw = _state_json()

    state = State.from_json(raw)

    assert state.to_json() == raw
    assert state.renderer.config == {"source": "{{ data.jobs | md_table }}", "profile": "ci"}
    assert State.from_json(state.to_json()) == state


def test_controller_requires_stable_identity() -> None:
    raw = _state_json()
    controller = cast("dict[str, object]", raw["controller"])
    del controller["id"]

    with pytest.raises(CodecError) as error:
        State.from_json(raw)
    assert error.value.code == "missing_state_field"
    assert error.value.details["fields"] == ["id"]


@pytest.mark.parametrize(
    "controller_id",
    [None, 0, -1, True, "1", MAX_GITHUB_USER_ID + 1],
)
def test_controller_id_has_explicit_wire_bounds(controller_id: object) -> None:
    raw = _state_json()
    controller = cast("dict[str, object]", raw["controller"])
    controller["id"] = controller_id

    with pytest.raises(CodecError, match="controller.id must be"):
        State.from_json(raw)


def test_boolean_schema_is_a_valid_snapshot() -> None:
    false_schema = SchemaSnapshot(
        dialect=JSON_SCHEMA_DIALECT_2020_12,
        document=False,
    )
    true_schema = SchemaSnapshot.from_json(
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
        State.from_json(raw)


@pytest.mark.parametrize("revision", [0, -1, MAX_REVISION + 1, True, "1"])
def test_state_rejects_invalid_revision(revision: object) -> None:
    raw = _state_json()
    raw["revision"] = revision

    with pytest.raises(CodecError, match="revision must be"):
        State.from_json(raw)


def test_state_accepts_revision_bounds() -> None:
    low = _state_json()
    high = _state_json()
    low["revision"] = 1
    high["revision"] = MAX_REVISION

    assert State.from_json(low).revision == 1
    assert State.from_json(high).revision == MAX_REVISION


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("revision", Decimal("1e+999999999")),
        (
            "controller.id",
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
        controller = cast("dict[str, object]", raw["controller"])
        controller["id"] = value

    with pytest.raises(CodecError, match="must be between") as captured:
        State.from_json(raw)

    assert captured.value.code == "invalid_state"


def test_state_rejects_unknown_top_level_fields() -> None:
    raw = _state_json()
    raw["state_sha256"] = "b" * 64

    with pytest.raises(CodecError) as captured:
        State.from_json(raw)

    assert captured.value.code == "unknown_state_field"
    assert captured.value.details["fields"] == ["state_sha256"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("format", "foreign/state", "unsupported state format"),
        ("render_sha256", "A" * 64, "lowercase 64-character"),
        ("render_sha256", "short", "lowercase 64-character"),
        ("data", [], "data must be a JSON object"),
    ],
)
def test_state_rejects_invalid_wire_fields(field: str, value: object, message: str) -> None:
    raw = _state_json()
    raw[field] = value

    with pytest.raises(CodecError, match=message):
        State.from_json(raw)


def test_draft_round_trips_without_a_revision_and_materializes_state() -> None:
    raw = _state_json()
    del raw["revision"]

    draft = StateDraft.from_json(raw)
    state = draft.with_revision(3)

    assert draft.to_json() == raw
    assert "revision" not in draft.to_json()
    assert state.revision == 3
    assert state.to_draft() == draft


def test_absent_schema_has_one_canonical_null_representation() -> None:
    raw = _state_json()
    raw["data_schema"] = None

    state = State.from_json(raw)

    assert state.data_schema is None
    assert state.to_json()["data_schema"] is None

    del raw["data_schema"]
    with pytest.raises(CodecError) as captured:
        State.from_json(raw)

    assert captured.value.code == "missing_state_field"


def test_nested_structures_are_frozen_from_caller_mutation() -> None:
    data = {"items": [{"value": 1}]}
    state = State(
        meta=MetaSnapshot.local("immutable"),
        name="immutable",
        revision=1,
        controller=Controller(login="octocat", id=1),
        data=data,
        renderer=RendererDescriptor(
            config={"source": "{{ data.items | md_list }}"},
        ),
        render_sha256=_DIGEST,
    )

    data["items"] = []

    assert state.to_json()["data"] == {"items": [{"value": 1}]}


@pytest.mark.parametrize("meta", [None, {}, MetaSnapshot.local("another").to_json()])
def test_state_requires_metadata_for_the_same_slate(meta):
    raw = _state_json()
    raw["meta"] = meta
    with pytest.raises(CodecError):
        State.from_json(raw)


def test_state_requires_metadata_on_the_wire():
    raw = _state_json()
    del raw["meta"]
    with pytest.raises(CodecError) as error:
        State.from_json(raw)
    assert error.value.code == "missing_state_field"
    assert error.value.details["fields"] == ["meta"]


def test_renderer_rejects_unknown_configuration_fields():
    with pytest.raises(CodecError) as error:
        RendererDescriptor.from_json({"source": "ok", "unknown": True})
    assert error.value.code == "unknown_state_field"
    assert error.value.details["fields"] == ["unknown"]
