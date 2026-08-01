from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest

import gh_slate.rendering.engine as engine_module
from gh_slate.codec import (
    CodecError,
    ControllerV1,
    RendererDescriptorV1,
    StateV1,
    decode_comment,
    encode_comment,
    strict_loads,
)
from gh_slate.codec.limits import DEFAULT_CODEC_LIMITS
from gh_slate.rendering import (
    RenderingError,
    RenderLimits,
    SlateContext,
    TableColumn,
    TableRendererV1,
    jinja_descriptor,
    materialize_comment,
    render,
    render_state,
)
from gh_slate.schema import validate_schema

_EMPTY_HASH = "0" * 64


def test_table_render_canonicalizes_data_and_persists_resolved_schema_columns() -> None:
    schema = validate_schema(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "jobs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "status": {"type": "string"},
                            "name": {"type": "string"},
                        },
                    },
                }
            },
        }
    )
    descriptor = TableRendererV1(
        selector=".jobs",
        title="Jobs",
        columns=(),
    ).to_descriptor()

    result = render(
        {"jobs": [{"name": "linux", "status": "passed"}], "ratio": Decimal("1.2300")},
        descriptor,
        schema=schema,
        slate=SlateContext(name="ci"),
    )

    assert result.data["ratio"] == Decimal("1.23")
    assert result.markdown == ("## Jobs\n\n| status | name |\n| --- | --- |\n| passed | linux |\n")
    assert result.renderer.to_json()["columns"] == [
        {"path": ["status"], "header": "status"},
        {"path": ["name"], "header": "name"},
    ]


def test_schema_projection_supports_quoted_jq_keys_for_empty_tables() -> None:
    schema = validate_schema(
        {
            "type": "object",
            "properties": {
                "key.with]dot": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "value": {"type": "string"},
                        },
                    },
                }
            },
        }
    )
    descriptor = TableRendererV1(
        selector='  .["key.with]dot"] \n',
        columns=(),
    ).to_descriptor()

    result = render(
        {"key.with]dot": []},
        descriptor,
        schema=schema,
        slate=SlateContext(name="ci"),
    )

    assert result.markdown == "| value |\n| --- |\n"
    assert result.renderer.to_json()["columns"] == [{"path": ["value"], "header": "value"}]


@pytest.mark.parametrize("jobs", [[], [{"a": "first", "z": "last"}]])
def test_schema_projection_uses_prefix_item_row_order(
    jobs: list[dict[str, str]],
) -> None:
    schema = validate_schema(
        {
            "type": "object",
            "properties": {
                "jobs": {
                    "type": "array",
                    "prefixItems": [
                        {
                            "type": "object",
                            "properties": {
                                "z": {"type": "string"},
                                "a": {"type": "string"},
                            },
                        }
                    ],
                    "items": False,
                }
            },
        }
    )

    result = render(
        {"jobs": jobs},
        TableRendererV1(selector=".jobs").to_descriptor(),
        schema=schema,
        slate=SlateContext(name="ci"),
    )

    assert result.renderer.to_json()["columns"] == [
        {"path": ["z"], "header": "z"},
        {"path": ["a"], "header": "a"},
    ]


@pytest.mark.parametrize("jobs", [[], [{"name": "linux", "status": "passing"}]])
def test_schema_projection_follows_additional_properties(
    jobs: list[dict[str, str]],
) -> None:
    schema = validate_schema(
        {
            "type": "object",
            "additionalProperties": {
                "type": "array",
                "items": {
                    "properties": {
                        "status": {"type": "string"},
                        "name": {"type": "string"},
                    }
                },
            },
        }
    )

    result = render(
        {"jobs": jobs},
        TableRendererV1(selector=".jobs").to_descriptor(),
        schema=schema,
        slate=SlateContext(name="ci"),
    )

    assert result.renderer.to_json()["columns"] == [
        {"path": ["status"], "header": "status"},
        {"path": ["name"], "header": "name"},
    ]


@pytest.mark.parametrize(
    ("group_schema", "group_index"),
    [
        ({"prefixItems": [{}]}, 0),
        ({"items": {}}, 0),
        ({"prefixItems": [{}], "items": {}}, 1),
    ],
)
@pytest.mark.parametrize("jobs", [[], [{"name": "linux", "status": "passing"}]])
def test_schema_projection_supports_numeric_jq_indices(
    group_schema: dict[str, object],
    group_index: int,
    jobs: list[dict[str, str]],
) -> None:
    row_schema = {
        "properties": {
            "status": {"type": "string"},
            "name": {"type": "string"},
        }
    }
    selected_group_schema = {
        "properties": {
            "jobs": {
                "type": "array",
                "items": row_schema,
            }
        }
    }
    configured_group_schema = dict(group_schema)
    prefix_items = configured_group_schema.get("prefixItems")
    if isinstance(prefix_items, list):
        prefix_items = list(prefix_items)
        if group_index < len(prefix_items):
            prefix_items[group_index] = selected_group_schema
        configured_group_schema["prefixItems"] = prefix_items
    if "items" in configured_group_schema:
        configured_group_schema["items"] = selected_group_schema
    groups: list[dict[str, object]] = [{} for _ in range(group_index)]
    groups.append({"jobs": jobs})
    schema = validate_schema(
        {
            "type": "object",
            "properties": {
                "groups": {
                    "type": "array",
                    **configured_group_schema,
                }
            },
        }
    )

    result = render(
        {"groups": groups},
        TableRendererV1(selector=f".groups[{group_index}].jobs").to_descriptor(),
        schema=schema,
        slate=SlateContext(name="ci"),
    )

    assert result.renderer.to_json()["columns"] == [
        {"path": ["status"], "header": "status"},
        {"path": ["name"], "header": "name"},
    ]
    if jobs:
        assert result.markdown.startswith("| status | name |\n")
    else:
        assert result.markdown == "| status | name |\n| --- | --- |\n"


@pytest.mark.parametrize(
    ("index_token", "group_index"),
    [
        (" 0 ", 0),
        ("00", 0),
        ("0.0", 0),
        (".0", 0),
        ("1.", 1),
        ("1.e0", 1),
        ("1e0", 1),
        ("-1", 1),
    ],
)
def test_schema_projection_accepts_jq_integer_literal_indices(
    index_token: str,
    group_index: int,
) -> None:
    schema = validate_schema(
        {
            "properties": {
                "groups": {
                    "type": "array",
                    "items": {
                        "properties": {
                            "jobs": {
                                "type": "array",
                                "items": {
                                    "properties": {
                                        "status": {},
                                        "name": {},
                                    }
                                },
                            }
                        }
                    },
                }
            }
        }
    )
    groups = [{"jobs": []} for _ in range(group_index + 1)]

    result = render(
        {"groups": groups},
        TableRendererV1(selector=f".groups[{index_token}].jobs").to_descriptor(),
        schema=schema,
        slate=SlateContext(name="ci"),
    )

    assert result.renderer.to_json()["columns"] == [
        {"path": ["status"], "header": "status"},
        {"path": ["name"], "header": "name"},
    ]


def test_negative_numeric_index_selects_the_matching_prefix_item_schema() -> None:
    schema = validate_schema(
        {
            "properties": {
                "groups": {
                    "type": "array",
                    "prefixItems": [
                        {
                            "properties": {
                                "jobs": {
                                    "type": "array",
                                    "items": {"properties": {"wrong": {}}},
                                }
                            }
                        },
                        {
                            "properties": {
                                "jobs": {
                                    "type": "array",
                                    "items": {
                                        "properties": {
                                            "status": {},
                                            "name": {},
                                        }
                                    },
                                }
                            }
                        },
                    ],
                }
            }
        }
    )

    result = render(
        {"groups": [{"jobs": []}, {"jobs": []}]},
        TableRendererV1(selector=".groups[-1].jobs").to_descriptor(),
        schema=schema,
        slate=SlateContext(name="ci"),
    )

    assert result.renderer.to_json()["columns"] == [
        {"path": ["status"], "header": "status"},
        {"path": ["name"], "header": "name"},
    ]


def test_extreme_numeric_selector_falls_back_without_projection_errors() -> None:
    selector = f".groups[{'9' * 5000}].jobs // .fallback"

    result = render(
        {"groups": [], "fallback": []},
        TableRendererV1(selector=selector).to_descriptor(),
        schema={},
        slate=SlateContext(name="ci"),
    )

    assert result.markdown == "_No data._\n"
    assert result.renderer.to_json()["columns"] == []


@pytest.mark.parametrize(
    "jobs",
    [
        [],
        [{"name": "linux", "status": "passing"}],
    ],
)
def test_table_schema_projection_resolves_local_refs_and_all_of(
    jobs: list[dict[str, str]],
) -> None:
    schema = validate_schema(
        {
            "$defs": {
                "jobs/list": {"$ref": "#/$defs/job-array"},
                "job-array": {
                    "type": "array",
                    "items": {"$ref": "#row"},
                },
                "row": {
                    "$anchor": "row",
                    "type": "object",
                    "allOf": [
                        {
                            "properties": {
                                "status": {"type": "string"},
                            }
                        },
                        {
                            "properties": {
                                "name": {"type": "string"},
                            }
                        },
                    ],
                },
            },
            "allOf": [
                {
                    "type": "object",
                    "properties": {
                        "jobs": {"$ref": "#/$defs/jobs~1list"},
                    },
                }
            ],
        }
    )

    result = render(
        {"jobs": jobs},
        TableRendererV1(selector=".jobs").to_descriptor(),
        schema=schema,
        slate=SlateContext(name="ci"),
    )

    assert result.markdown.startswith("| status | name |\n| --- | --- |\n")
    assert result.renderer.to_json()["columns"] == [
        {"path": ["status"], "header": "status"},
        {"path": ["name"], "header": "name"},
    ]


@pytest.mark.parametrize(
    "jobs",
    [
        [],
        [{"kind": "named", "name": "linux"}],
    ],
)
def test_table_schema_projection_traverses_one_of_and_any_of(
    jobs: list[dict[str, str]],
) -> None:
    schema = validate_schema(
        {
            "$defs": {
                "row": {
                    "type": "object",
                    "anyOf": [
                        {
                            "properties": {
                                "kind": {"const": "named"},
                            }
                        },
                        {
                            "properties": {
                                "name": {"type": "string"},
                            }
                        },
                    ],
                }
            },
            "type": "object",
            "properties": {
                "jobs": {
                    "oneOf": [
                        {
                            "type": "array",
                            "maxItems": 0,
                            "items": {"$ref": "#/$defs/row"},
                        },
                        {
                            "type": "array",
                            "minItems": 1,
                            "items": {"$ref": "#/$defs/row"},
                        },
                    ]
                }
            },
        }
    )

    result = render(
        {"jobs": jobs},
        TableRendererV1(selector=".jobs").to_descriptor(),
        schema=schema,
        slate=SlateContext(name="ci"),
    )

    assert result.markdown.startswith("| kind | name |\n| --- | --- |\n")
    assert result.renderer.to_json()["columns"] == [
        {"path": ["kind"], "header": "kind"},
        {"path": ["name"], "header": "name"},
    ]


@pytest.mark.parametrize("keyword", ["oneOf", "anyOf"])
@pytest.mark.parametrize(
    ("jobs", "columns"),
    [
        (
            [{"kind": "ci", "status": "passing"}],
            ["kind", "status"],
        ),
        (
            [
                {"kind": "ci", "status": "passing"},
                {"kind": "deploy", "environment": "production"},
            ],
            ["kind", "status", "environment"],
        ),
    ],
)
def test_table_schema_projection_uses_only_matching_alternatives(
    keyword: str,
    jobs: list[dict[str, str]],
    columns: list[str],
) -> None:
    schema = validate_schema(
        {
            "properties": {
                "jobs": {
                    "type": "array",
                    "items": {
                        keyword: [
                            {
                                "properties": {
                                    "kind": {"const": "ci"},
                                    "status": {"type": "string"},
                                },
                                "required": ["kind", "status"],
                            },
                            {
                                "properties": {
                                    "kind": {"const": "deploy"},
                                    "environment": {"type": "string"},
                                },
                                "required": ["kind", "environment"],
                            },
                        ]
                    },
                }
            }
        }
    )

    result = render(
        {"jobs": jobs},
        TableRendererV1(selector=".jobs").to_descriptor(),
        schema=schema,
        slate=SlateContext(name="ci"),
    )

    assert result.renderer.to_json()["columns"] == [{"path": [column], "header": column} for column in columns]


@pytest.mark.parametrize("keyword", ["oneOf", "anyOf"])
def test_table_schema_projection_matches_array_level_alternatives(keyword: str) -> None:
    schema = validate_schema(
        {
            "properties": {
                "jobs": {
                    keyword: [
                        {
                            "type": "array",
                            "maxItems": 0,
                            "items": {"properties": {"empty_column": {}}},
                        },
                        {
                            "type": "array",
                            "minItems": 1,
                            "items": {"properties": {"nonempty_column": {}}},
                        },
                    ]
                }
            }
        }
    )

    result = render(
        {"jobs": []},
        TableRendererV1(selector=".jobs").to_descriptor(),
        schema=schema,
        slate=SlateContext(name="ci"),
    )

    assert result.renderer.to_json()["columns"] == [
        {"path": ["empty_column"], "header": "empty_column"},
    ]


@pytest.mark.parametrize("jobs", [[], [{"name": "linux", "status": "passing"}]])
def test_table_schema_projection_follows_pattern_properties(
    jobs: list[dict[str, str]],
) -> None:
    schema = validate_schema(
        {
            "type": "object",
            "patternProperties": {
                "jobs": {
                    "type": "array",
                    "items": {
                        "properties": {
                            "status": {"type": "string"},
                            "name": {"type": "string"},
                        }
                    },
                }
            },
            "additionalProperties": {
                "type": "array",
                "items": {"properties": {"wrong": {}}},
            },
        }
    )

    result = render(
        {"myjobs": jobs},
        TableRendererV1(selector=".myjobs").to_descriptor(),
        schema=schema,
        slate=SlateContext(name="ci"),
    )

    assert result.renderer.to_json()["columns"] == [
        {"path": ["status"], "header": "status"},
        {"path": ["name"], "header": "name"},
    ]


def test_table_schema_projection_combines_property_and_matching_patterns() -> None:
    schema = validate_schema(
        {
            "properties": {
                "jobs": {
                    "type": "array",
                    "items": {"properties": {"explicit": {}}},
                }
            },
            "patternProperties": {
                "^jobs$": {
                    "type": "array",
                    "items": {"properties": {"first_pattern": {}}},
                },
                "jobs": {
                    "type": "array",
                    "items": {"properties": {"second_pattern": {}}},
                },
                "^other$": {
                    "type": "array",
                    "items": {"properties": {"inactive_pattern": {}}},
                },
            },
        }
    )

    result = render(
        {"jobs": []},
        TableRendererV1(selector=".jobs").to_descriptor(),
        schema=schema,
        slate=SlateContext(name="ci"),
    )

    assert result.renderer.to_json()["columns"] == [
        {"path": ["explicit"], "header": "explicit"},
        {"path": ["first_pattern"], "header": "first_pattern"},
        {"path": ["second_pattern"], "header": "second_pattern"},
    ]


@pytest.mark.parametrize(
    ("keyword", "condition"),
    [("then", True), ("else", False)],
)
def test_table_schema_projection_traverses_conditional_subschemas(
    keyword: str,
    condition: bool,
) -> None:
    row = {
        "if": condition,
        keyword: {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "name": {"type": "string"},
            },
        },
    }
    schema = validate_schema(
        {
            "type": "object",
            "if": condition,
            keyword: {
                "properties": {
                    "jobs": {
                        "type": "array",
                        "items": row,
                    }
                }
            },
        }
    )

    result = render(
        {"jobs": []},
        TableRendererV1(selector=".jobs").to_descriptor(),
        schema=schema,
        slate=SlateContext(name="ci"),
    )

    assert result.markdown == "| status | name |\n| --- | --- |\n"
    assert result.renderer.to_json()["columns"] == [
        {"path": ["status"], "header": "status"},
        {"path": ["name"], "header": "name"},
    ]


@pytest.mark.parametrize(
    ("mode", "column"),
    [("ci", "status"), ("deploy", "environment")],
)
def test_table_schema_projection_uses_only_the_active_conditional_branch(
    mode: str,
    column: str,
) -> None:
    schema = validate_schema(
        {
            "type": "object",
            "properties": {"mode": {"type": "string"}},
            "if": {
                "properties": {"mode": {"const": "ci"}},
                "required": ["mode"],
            },
            "then": {
                "properties": {
                    "jobs": {
                        "type": "array",
                        "items": {"properties": {"status": {"type": "string"}}},
                    }
                }
            },
            "else": {
                "properties": {
                    "jobs": {
                        "type": "array",
                        "items": {"properties": {"environment": {"type": "string"}}},
                    }
                }
            },
        }
    )

    result = render(
        {"mode": mode, "jobs": []},
        TableRendererV1(selector=".jobs").to_descriptor(),
        schema=schema,
        slate=SlateContext(name="ci"),
    )

    assert result.markdown == f"| {column} |\n| --- |\n"
    assert result.renderer.to_json()["columns"] == [
        {"path": [column], "header": column},
    ]


@pytest.mark.parametrize(
    ("row", "column"),
    [
        ({"kind": "ci", "status": "passing"}, "status"),
        ({"kind": "deploy", "environment": "production"}, "environment"),
    ],
)
def test_table_schema_projection_evaluates_item_conditions_against_rows(
    row: dict[str, str],
    column: str,
) -> None:
    schema = validate_schema(
        {
            "properties": {
                "jobs": {
                    "type": "array",
                    "items": {
                        "if": {
                            "properties": {"kind": {"const": "ci"}},
                            "required": ["kind"],
                        },
                        "then": {"properties": {"status": {"type": "string"}}},
                        "else": {"properties": {"environment": {"type": "string"}}},
                    },
                }
            }
        }
    )

    result = render(
        {"jobs": [row]},
        TableRendererV1(selector=".jobs").to_descriptor(),
        schema=schema,
        slate=SlateContext(name="ci"),
    )

    assert result.renderer.to_json()["columns"] == [
        {"path": [column], "header": column},
    ]


def test_table_schema_projection_checks_every_row_that_can_be_rendered() -> None:
    rows = [{"kind": "ci", "status": "passing"} for _index in range(500)]
    rows.append({"kind": "deploy", "environment": "production"})
    schema = validate_schema(
        {
            "properties": {
                "jobs": {
                    "type": "array",
                    "items": {
                        "if": {
                            "properties": {"kind": {"const": "ci"}},
                            "required": ["kind"],
                        },
                        "then": {"properties": {"status": {"type": "string"}}},
                        "else": {"properties": {"environment": {"type": "string"}}},
                    },
                }
            }
        }
    )

    result = render(
        {"jobs": rows},
        TableRendererV1(selector=".jobs", max_rows=501).to_descriptor(),
        schema=schema,
        slate=SlateContext(name="ci"),
        limits=RenderLimits(max_table_rows=501),
    )

    assert result.renderer.to_json()["columns"] == [
        {"path": ["status"], "header": "status"},
        {"path": ["environment"], "header": "environment"},
    ]
    assert result.markdown.endswith("| — | production |\n")


def test_table_schema_projection_activates_dependent_schemas_from_data() -> None:
    schema = validate_schema(
        {
            "type": "object",
            "properties": {"mode": {"type": "string"}},
            "dependentSchemas": {
                "mode": {
                    "properties": {
                        "jobs": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "status": {"type": "string"},
                                    "name": {"type": "string"},
                                },
                            },
                        }
                    }
                }
            },
        }
    )
    descriptor = TableRendererV1(selector=".jobs").to_descriptor()

    active = render(
        {"mode": "ci", "jobs": []},
        descriptor,
        schema=schema,
        slate=SlateContext(name="ci"),
    )
    inactive = render(
        {"jobs": []},
        descriptor,
        schema=schema,
        slate=SlateContext(name="ci"),
    )

    assert active.markdown == "| status | name |\n| --- | --- |\n"
    assert active.renderer.to_json()["columns"] == [
        {"path": ["status"], "header": "status"},
        {"path": ["name"], "header": "name"},
    ]
    assert inactive.markdown == "_No data._\n"
    assert inactive.renderer.to_json()["columns"] == []


def test_jinja_filters_obey_the_shared_builtin_render_limits() -> None:
    descriptor = jinja_descriptor('{{ data.rows | md_table(columns=["name"]) }}')

    result = render(
        {"rows": [{"name": "first"}, {"name": "second"}]},
        descriptor,
        slate=SlateContext(name="ci"),
        limits=RenderLimits(max_table_rows=1),
    )

    assert "| first |" in result.markdown
    assert "| second |" not in result.markdown
    assert "_1 additional row(s) omitted._" in result.markdown


def test_render_rejects_a_template_that_cannot_fit_its_stored_descriptor() -> None:
    descriptor = jinja_descriptor("\n" * (64 * 1024))

    with pytest.raises(CodecError) as caught:
        render(
            {},
            descriptor,
            slate=SlateContext(name="ci"),
        )

    assert caught.value.code == "codec_size_limit"
    exceeded = caught.value.details["exceeded"]
    assert isinstance(exceeded, Mapping)
    assert "renderer_bytes" in exceeded


def test_render_preflights_compressed_comment_envelope() -> None:
    high_entropy = "".join(sha256(str(index).encode()).hexdigest() for index in range(1600))

    with pytest.raises(CodecError) as caught:
        render(
            {"blob": high_entropy},
            jinja_descriptor("ok"),
            slate=SlateContext(name="ci"),
        )

    assert caught.value.code == "codec_size_limit"
    exceeded = caught.value.details["exceeded"]
    assert isinstance(exceeded, Mapping)
    assert "compressed_bytes" in exceeded


def test_render_preflight_uses_a_maximum_width_low_compressibility_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hashes = [sha256(str(index).encode()).hexdigest() for index in range(620)]
    data = {"blob": "".join(hashes[:619]) + hashes[619][:4]}
    descriptor = jinja_descriptor("ok")
    relaxed = replace(
        DEFAULT_CODEC_LIMITS,
        max_compressed_bytes=64 * 1024,
    )
    captured: list[StateV1] = []

    def capture(state: StateV1, markdown: str):
        captured.append(state)
        return encode_comment(state, markdown, limits=relaxed)

    monkeypatch.setattr(engine_module, "DEFAULT_CODEC_LIMITS", relaxed)
    monkeypatch.setattr(engine_module, "encode_comment", capture)
    rendered = render(
        data,
        descriptor,
        slate=SlateContext(name="ci"),
    )

    assert len(captured) == 1
    provisional = captured[0]
    assert len(provisional.controller.login.encode("ascii")) == 39
    sentinel_size = encode_comment(
        provisional,
        rendered.markdown,
        limits=relaxed,
    ).sizes.compressed_bytes
    legacy_size = encode_comment(
        replace(
            provisional,
            controller=ControllerV1(login="gh-slate-local-preview"),
        ),
        rendered.markdown,
        limits=relaxed,
    ).sizes.compressed_bytes
    assert sentinel_size > legacy_size

    tight = replace(
        DEFAULT_CODEC_LIMITS,
        max_compressed_bytes=legacy_size,
    )
    monkeypatch.setattr(
        engine_module,
        "encode_comment",
        lambda state, markdown: encode_comment(state, markdown, limits=tight),
    )

    with pytest.raises(CodecError) as caught:
        render(
            data,
            descriptor,
            slate=SlateContext(name="ci"),
        )

    assert caught.value.code == "codec_size_limit"
    exceeded = caught.value.details["exceeded"]
    assert isinstance(exceeded, Mapping)
    assert "compressed_bytes" in exceeded


def test_render_preflight_reserves_for_data_dependent_login_compression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = engine_module._PREFLIGHT_CONTROLLER_LOGIN
    data = {
        "blob": ((sentinel + "|") * 128)
        + "".join(sha256(f"padding-{index}".encode()).hexdigest() for index in range(64))
    }
    relaxed = replace(
        DEFAULT_CODEC_LIMITS,
        max_compressed_bytes=64 * 1024,
    )
    captured: list[StateV1] = []

    def capture(state: StateV1, markdown: str):
        captured.append(state)
        return encode_comment(state, markdown, limits=relaxed)

    monkeypatch.setattr(engine_module, "encode_comment", capture)
    rendered = render(
        data,
        jinja_descriptor("ok"),
        slate=SlateContext(name="ci"),
    )

    provisional = captured[0]
    provisional_size = encode_comment(
        provisional,
        rendered.markdown,
        limits=relaxed,
    ).sizes.compressed_bytes
    real_size = encode_comment(
        replace(
            provisional,
            controller=ControllerV1(
                login="z" * 39,
                id=provisional.controller.id,
            ),
        ),
        rendered.markdown,
        limits=relaxed,
    ).sizes.compressed_bytes
    assert real_size > provisional_size

    tight = replace(
        DEFAULT_CODEC_LIMITS,
        max_compressed_bytes=real_size - 1,
    )
    assert provisional_size <= tight.max_compressed_bytes
    monkeypatch.setattr(engine_module, "DEFAULT_CODEC_LIMITS", tight)
    monkeypatch.setattr(
        engine_module,
        "encode_comment",
        lambda state, markdown: encode_comment(state, markdown, limits=tight),
    )

    with pytest.raises(CodecError) as caught:
        render(
            data,
            jinja_descriptor("ok"),
            slate=SlateContext(name="ci"),
        )

    assert caught.value.code == "codec_size_limit"
    exceeded = caught.value.details["exceeded"]
    assert isinstance(exceeded, Mapping)
    assert "compressed_bytes" in exceeded


def test_render_rejects_a_reserved_marker_in_visible_markdown() -> None:
    with pytest.raises(CodecError) as caught:
        render(
            {},
            jinja_descriptor("<!-- gh-slate:v1 forged -->"),
            slate=SlateContext(name="ci"),
        )

    assert caught.value.code == "duplicate_marker"


def test_materialized_comment_round_trips_and_rerenders_byte_identically() -> None:
    state = StateV1(
        name="ci",
        revision=1,
        controller=ControllerV1(login="ci-bot"),
        data={"jobs": [{"name": "linux"}]},
        renderer=TableRendererV1(
            selector=".jobs",
            title=None,
            columns=(TableColumn(path=("name",), header="name"),),
        ).to_descriptor(),
        render_sha256=_EMPTY_HASH,
    )

    materialized = materialize_comment(state)
    decoded = decode_comment(materialized.encoded.body)

    assert decoded.state == materialized.state
    assert decoded.visible_markdown == materialized.rendered.markdown
    assert not decoded.drifted
    assert render_state(decoded.state).markdown == decoded.visible_markdown


def test_stored_state_render_skips_provisional_materialization_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = StateV1(
        name="ci",
        revision=1,
        controller=ControllerV1(login="ci-bot"),
        data={"jobs": [{"name": "linux"}]},
        renderer=TableRendererV1(
            selector=".jobs",
            title=None,
            columns=(TableColumn(path=("name",), header="name"),),
        ).to_descriptor(),
        render_sha256=_EMPTY_HASH,
    )

    def reject_provisional_preflight(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("stored states must use their actual metadata")

    monkeypatch.setattr(engine_module, "_preflight_materialization", reject_provisional_preflight)

    rendered = render_state(state)
    materialized = materialize_comment(state)

    assert rendered.markdown == materialized.rendered.markdown
    assert decode_comment(materialized.encoded.body).state == materialized.state


def test_materialization_persists_schema_resolved_table_columns() -> None:
    state = StateV1(
        name="ci",
        revision=1,
        controller=ControllerV1(login="ci-bot"),
        data={"jobs": [{"name": "linux", "status": "passed"}]},
        data_schema=validate_schema(
            {
                "type": "object",
                "properties": {
                    "jobs": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "status": {"type": "string"},
                                "name": {"type": "string"},
                            },
                        },
                    }
                },
            }
        ),
        renderer=TableRendererV1(selector=".jobs").to_descriptor(),
        render_sha256=_EMPTY_HASH,
    )

    materialized = materialize_comment(state)
    decoded = decode_comment(materialized.encoded.body)

    expected_columns = [
        {"path": ["status"], "header": "status"},
        {"path": ["name"], "header": "name"},
    ]
    assert materialized.state.renderer.to_json()["columns"] == expected_columns
    assert decoded.state.renderer.to_json()["columns"] == expected_columns
    assert not decoded.drifted
    assert render_state(decoded.state).markdown == decoded.visible_markdown


def test_render_state_does_not_silently_migrate_legacy_renderer_descriptor() -> None:
    descriptor = RendererDescriptorV1(
        kind="builtin-table",
        version=1,
        config={"selector": ".jobs", "columns": ["name"]},
    )
    state = StateV1(
        name="ci",
        revision=1,
        controller=ControllerV1(login="ci-bot"),
        data={"jobs": [{"name": "linux"}]},
        renderer=descriptor,
        render_sha256=_EMPTY_HASH,
    )

    rendered = render_state(state)

    assert rendered.renderer is descriptor
    assert rendered.markdown == ("| name |\n| --- |\n| linux |\n")


def test_permanent_builtin_fixture_rerenders_to_visible_markdown() -> None:
    fixture = Path(__file__).resolve().parents[2] / "fixtures" / "wire" / "state-v1" / "minimal"
    state = StateV1.from_json(strict_loads((fixture / "state.canonical.json").read_bytes()))

    assert render_state(state).markdown == (fixture / "visible.md").read_text(encoding="utf-8")


def test_jinja_descriptor_is_exact_and_context_name_must_match_state() -> None:
    bad = RendererDescriptorV1(
        kind="jinja",
        version=1,
        config={"source": "{{ data.ok }}", "extra": True},
    )
    with pytest.raises(RenderingError) as config_error:
        render(
            {"ok": True},
            bad,
            slate=SlateContext(name="ci"),
        )
    assert config_error.value.code == "renderer_config_invalid"

    state = StateV1(
        name="ci",
        revision=1,
        controller=ControllerV1(login="ci-bot"),
        data={"ok": True},
        renderer=jinja_descriptor("{{ data.ok }}"),
        render_sha256=_EMPTY_HASH,
    )
    with pytest.raises(RenderingError) as missing_context:
        render_state(state)
    assert missing_context.value.code == "render_context_required"

    incomplete_contexts = (
        SlateContext(name="ci"),
        SlateContext(
            name="ci",
            repository="owner/repo",
            number=42,
        ),
        SlateContext(
            name="ci",
            repository="owner/repo",
            url="https://github.com/owner/repo/issues/42",
        ),
        SlateContext(
            name="ci",
            number=42,
            url="https://github.com/owner/repo/issues/42",
        ),
    )
    for context in incomplete_contexts:
        with pytest.raises(RenderingError) as incomplete:
            render_state(state, slate=context)
        assert incomplete.value.code == "render_context_required"

    with pytest.raises(RenderingError) as context_error:
        render_state(
            state,
            slate=SlateContext(
                name="other",
                repository="owner/repo",
                number=42,
                url="https://github.com/owner/repo/issues/42",
            ),
        )
    assert context_error.value.code == "render_context_mismatch"

    complete = render_state(
        state,
        slate=SlateContext(
            name="ci",
            repository="owner/repo",
            number=42,
            url="https://github.com/owner/repo/issues/42",
        ),
    )
    assert complete.markdown == "true\n"

    target_preview = render(
        {},
        jinja_descriptor("{{ slate.url }}"),
        slate=SlateContext(
            name="ci",
            repository="owner/repo",
            number=42,
            url="https://github.com/owner/repo/issues/42",
        ),
    )
    assert target_preview.markdown == "https://github.com/owner/repo/issues/42\n"


@pytest.mark.parametrize("selector", [".missing | empty", ".[]"])
def test_render_rejects_selectors_without_exactly_one_result(selector: str) -> None:
    descriptor = TableRendererV1(
        selector=selector,
        title=None,
        columns=(),
    ).to_descriptor()

    with pytest.raises(RenderingError) as caught:
        render(
            {"first": [], "second": []},
            descriptor,
            slate=SlateContext(name="ci"),
        )

    assert caught.value.code in {"jq_no_result", "jq_multiple_results"}
