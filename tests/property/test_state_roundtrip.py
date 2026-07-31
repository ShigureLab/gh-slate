from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings, strategies as st

from gh_slate.codec.comment import decode_comment, encode_comment
from gh_slate.codec.errors import CodecError
from gh_slate.codec.hashes import (
    canonical_state_bytes,
    normalize_visible_markdown,
    render_sha256,
)
from gh_slate.codec.model import (
    JSON_SCHEMA_DIALECT_2020_12,
    MAX_REVISION,
    ControllerV1,
    RendererDescriptorV1,
    SchemaSnapshotV1,
    StateV1,
)

SAFE_TEXT = st.text(
    alphabet=st.characters(exclude_categories=("Cs",)),
    max_size=40,
)
LINE_TEXT = st.text(
    alphabet=st.characters(exclude_categories=("Cs",), exclude_characters="\r\n"),
    max_size=40,
).filter(lambda value: "<!-- gh-slate:" not in value)
FINITE_DECIMALS = (
    st.integers(
        min_value=-(2**63),
        max_value=2**63 - 1,
    ).map(Decimal)
    | st.decimals(
        min_value=Decimal("-1e24"),
        max_value=Decimal("1e24"),
        allow_nan=False,
        allow_infinity=False,
        places=6,
    )
    | st.sampled_from(
        (
            Decimal("-0"),
            Decimal("-0.000"),
            Decimal("1e-7"),
            Decimal("0.000001"),
            Decimal("1e20"),
            Decimal("1e21"),
            Decimal("-1.20e30"),
        )
    )
)
JSON_SCALARS = st.none() | st.booleans() | SAFE_TEXT | FINITE_DECIMALS
JSON_VALUES = st.recursive(
    JSON_SCALARS,
    lambda children: st.lists(children, max_size=4) | st.dictionaries(SAFE_TEXT, children, max_size=4),
    max_leaves=16,
)
DATA_OBJECTS = st.dictionaries(SAFE_TEXT, JSON_VALUES, max_size=5)
SCHEMA_DOCUMENTS = st.booleans() | st.dictionaries(SAFE_TEXT, JSON_VALUES, max_size=5)
RENDERER_CONFIGS = st.builds(
    lambda value, rows: {
        "selector": ".data",
        "options": {
            "nested": {
                "value": value,
                "rows": rows,
            }
        },
    },
    JSON_VALUES,
    st.lists(JSON_VALUES, max_size=3),
)
REVISIONS = st.one_of(
    st.sampled_from((1, MAX_REVISION)),
    st.integers(min_value=1, max_value=MAX_REVISION),
)


@st.composite
def visible_markdown(draw: st.DrawFn) -> str:
    lines = draw(st.lists(LINE_TEXT, max_size=4))
    separator = draw(st.sampled_from(("\n", "\r\n", "\r")))
    trailing_line_ending = draw(st.booleans())
    value = separator.join(lines)
    if trailing_line_ending:
        value += separator
    return value


@given(
    data=DATA_OBJECTS,
    required_number=FINITE_DECIMALS,
    schema_document=SCHEMA_DOCUMENTS,
    renderer_config=RENDERER_CONFIGS,
    revision=REVISIONS,
    visible=visible_markdown(),
)
@settings(max_examples=60)
def test_comment_round_trip_property(
    data: dict[str, object],
    required_number: Decimal,
    schema_document: bool | dict[str, object],
    renderer_config: dict[str, object],
    revision: int,
    visible: str,
) -> None:
    data_with_number = {**data, "_required_number": required_number}
    state = StateV1(
        name="property",
        revision=revision,
        controller=ControllerV1(login="tester", id=1),
        data=data_with_number,
        data_schema=SchemaSnapshotV1(
            dialect=JSON_SCHEMA_DIALECT_2020_12,
            document=schema_document,
        ),
        renderer=RendererDescriptorV1(
            kind="fixture",
            version=1,
            config=renderer_config,
        ),
        render_sha256=render_sha256(visible),
    )

    encoded = encode_comment(state, visible)
    decoded = decode_comment(encoded.body)

    assert canonical_state_bytes(decoded.state) == canonical_state_bytes(state)
    assert decoded.state_sha256 == encoded.state_sha256
    assert decoded.visible_markdown == normalize_visible_markdown(visible)
    assert decoded.state.revision == revision
    assert decoded.state.data_schema == state.data_schema
    assert decoded.state.renderer.configuration == state.renderer.configuration
    assert decoded.state.data["_required_number"] == required_number
    assert decoded.drifted is False


@given(
    revision=st.sampled_from((1, MAX_REVISION)),
    line_ending=st.sampled_from(("\r\n", "\r")),
)
def test_revision_bounds_and_non_lf_markdown_round_trip_property(
    revision: int,
    line_ending: str,
) -> None:
    visible = f"heading{line_ending}body{line_ending}"
    state = StateV1(
        name="boundaries",
        revision=revision,
        controller=ControllerV1(login="tester", id=1),
        data={"negative_zero": Decimal("-0")},
        data_schema=SchemaSnapshotV1(
            dialect=JSON_SCHEMA_DIALECT_2020_12,
            document=True,
        ),
        renderer=RendererDescriptorV1(
            kind="fixture",
            version=1,
            config={"options": {"nested": {"empty": []}}},
        ),
        render_sha256=render_sha256(visible),
    )

    decoded = decode_comment(encode_comment(state, visible).body)

    assert decoded.state.revision == revision
    assert decoded.state.data["negative_zero"] == Decimal(0)
    assert decoded.visible_markdown == "heading\nbody\n"


@given(suffix=st.binary(max_size=512))
@settings(max_examples=50)
def test_invalid_utf8_never_leaks_a_unicode_error(suffix: bytes) -> None:
    with pytest.raises(CodecError) as error:
        decode_comment(b"\xff" + suffix)

    assert error.value.code == "invalid_utf8"
