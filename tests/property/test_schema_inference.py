from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from types import MappingProxyType

from hypothesis import given, settings, strategies as st

from gh_slate.codec.json import canonical_json_bytes, strict_loads
from gh_slate.codec.model import SchemaSnapshotV1
from gh_slate.schema.inference import infer_schema
from gh_slate.schema.validation import validate_data

SAFE_TEXT = st.text(
    alphabet=st.characters(exclude_categories=("Cs",)),
    max_size=30,
)
FINITE_DECIMALS = st.one_of(
    st.integers(min_value=-(2**63), max_value=2**63 - 1).map(Decimal),
    st.decimals(
        min_value=Decimal("-1e18"),
        max_value=Decimal("1e18"),
        allow_nan=False,
        allow_infinity=False,
        places=6,
    ),
)
JSON_SCALARS = st.none() | st.booleans() | SAFE_TEXT | FINITE_DECIMALS
JSON_VALUES = st.recursive(
    JSON_SCALARS,
    lambda children: st.lists(children, max_size=5) | st.dictionaries(SAFE_TEXT, children, max_size=5),
    max_leaves=30,
)
DATA_OBJECTS = st.dictionaries(SAFE_TEXT, JSON_VALUES, max_size=8)


def _immutable_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _immutable_json(item) for key, item in reversed(tuple(value.items()))})
    if isinstance(value, (list, tuple)):
        return tuple(_immutable_json(item) for item in reversed(value))
    return value


@given(data=DATA_OBJECTS)
@settings(max_examples=100, deadline=None)
def test_inferred_schema_always_accepts_the_observed_data(
    data: dict[str, object],
) -> None:
    schema = infer_schema(data)
    wire_schema = SchemaSnapshotV1.from_json(strict_loads(canonical_json_bytes(schema.to_json())))

    validate_data(data, schema)
    validate_data(data, wire_schema)


@given(data=DATA_OBJECTS)
@settings(max_examples=100, deadline=None)
def test_inference_is_deterministic_for_immutable_values_and_observation_order(
    data: dict[str, object],
) -> None:
    reordered = _immutable_json(data)
    original_schema = infer_schema(data)
    reordered_schema = infer_schema(reordered)

    assert canonical_json_bytes(original_schema.to_json()) == canonical_json_bytes(reordered_schema.to_json())
    validate_data(reordered, original_schema)
