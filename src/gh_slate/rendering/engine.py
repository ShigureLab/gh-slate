from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import cast

from gh_slate.codec import (
    ControllerV1,
    EncodedComment,
    JsonValue,
    MetaSnapshot,
    RendererDescriptorV1,
    SchemaSnapshotV1,
    State,
    canonical_json_bytes,
    encode_comment,
    normalize_visible_markdown,
    render_sha256,
    strict_loads,
)
from gh_slate.codec.limits import DEFAULT_CODEC_LIMITS, SizeReport, enforce_size_limits
from gh_slate.codec.model import (
    MAX_GITHUB_LOGIN_BYTES,
    MAX_GITHUB_USER_ID,
    MAX_REVISION,
    STATE_FORMAT_V1,
    STATE_FORMAT_V2,
)
from gh_slate.codec.text import utf8_size
from gh_slate.rendering.errors import RenderingError
from gh_slate.rendering.jinja import SlateContext, render_jinja
from gh_slate.rendering.limits import DEFAULT_RENDER_LIMITS, RenderLimits
from gh_slate.rendering.routing import selected_view
from gh_slate.schema import validate_data, validate_schema

_PREFLIGHT_CONTROLLER_LOGIN = "0123456789abcdefghijklmnopqrstuvwxyz-a0"
_PREFLIGHT_CONTROLLER_COMPRESSED_RESERVE = 4 * MAX_GITHUB_LOGIN_BYTES
_PREFLIGHT_CONTROLLER_ENCODED_RESERVE = 4 * ((_PREFLIGHT_CONTROLLER_COMPRESSED_RESERVE + 2) // 3) + 4


@dataclass(frozen=True, slots=True)
class RenderResult:
    """One deterministic local render and its canonical inputs."""

    data: Mapping[str, JsonValue]
    data_schema: SchemaSnapshotV1 | None
    renderer: RendererDescriptorV1
    markdown: str
    render_sha256: str
    meta: MetaSnapshot | None = None
    view: str | None = None


@dataclass(frozen=True, slots=True)
class MaterializedComment:
    """A state whose renderer/hash match the encoded comment body."""

    state: State
    rendered: RenderResult
    encoded: EncodedComment


def migration_required() -> RenderingError:
    return RenderingError(
        "legacy state requires an explicit V2 definition before rendering or updating",
        code="state_migration_required",
        hints=("use apply NAME --profile PROFILE --config FILE or apply NAME --template FILE to migrate stored data",),
    )


def jinja_descriptor(source: str, *, version: int = 2) -> RendererDescriptorV1:
    if not isinstance(source, str):
        raise RenderingError(
            "Jinja source must be text",
            code="jinja_source_invalid",
        )
    return RendererDescriptorV1(
        kind="jinja",
        version=version,
        config={"source": source},
    )


def _parse_jinja_source(descriptor: RendererDescriptorV1, data: object) -> tuple[str, str | None]:
    if descriptor.kind != "jinja" or descriptor.version != 2:
        raise RenderingError(
            "renderer version is not supported for rendering",
            code="renderer_unsupported",
            details={
                "kind": descriptor.kind,
                "version": descriptor.version,
            },
        )
    config = descriptor.configuration
    allowed = {"source", "profile", "views", "view_by"}
    if set(config) - allowed:
        raise RenderingError("Jinja renderer contains unknown fields", code="renderer_config_invalid")
    if "profile" in config and (
        not isinstance(config["profile"], str) or not config["profile"] or len(config["profile"].encode("utf-8")) > 128
    ):
        raise RenderingError("invalid profile name in renderer snapshot", code="renderer_config_invalid")
    if "source" in config:
        if not isinstance(config["source"], str) or "views" in config or "view_by" in config:
            raise RenderingError("Jinja source is exclusive with views and view_by", code="renderer_config_invalid")
        return config["source"], None
    if descriptor.version == 2 and "views" in config:
        view = selected_view(descriptor, data)
        assert view is not None
        return cast("Mapping[str, str]", config["views"])[view], view
    raise RenderingError("Jinja renderer requires source or routed views", code="renderer_config_invalid")


def _canonical_data(
    data: object,
    schema: SchemaSnapshotV1 | bool | Mapping[str, object] | None,
) -> tuple[Mapping[str, JsonValue], SchemaSnapshotV1 | None]:
    snapshot: SchemaSnapshotV1 | None
    if schema is None:
        snapshot = None
    elif isinstance(schema, SchemaSnapshotV1):
        snapshot = validate_schema(
            schema.document,
            dialect=schema.dialect,
        )
    else:
        snapshot = validate_schema(schema)

    validated = validate_data(data, snapshot)
    # Rendering always consumes the canonical JSON interpretation. This avoids
    # a first render observing Decimal("1.2300") while the decoded state later
    # contains Decimal("1.23").
    canonical = strict_loads(canonical_json_bytes(validated))
    if not isinstance(canonical, Mapping):  # validated already enforces this
        raise RenderingError(
            "renderer data root must be an object",
            code="render_data_invalid",
        )
    return cast("Mapping[str, JsonValue]", canonical), snapshot


def _enforce_components(
    data: Mapping[str, JsonValue],
    schema: SchemaSnapshotV1 | None,
    renderer: RendererDescriptorV1,
    *,
    markdown: str | None = None,
) -> None:
    enforce_size_limits(
        SizeReport(
            data_bytes=len(canonical_json_bytes(data)),
            schema_bytes=(0 if schema is None else len(canonical_json_bytes(schema.to_json()))),
            renderer_bytes=len(canonical_json_bytes(renderer.to_json())),
            visible_bytes=(0 if markdown is None else utf8_size(markdown, field="rendered Markdown")),
        )
    )


def _enforce_output(markdown: str, limits: RenderLimits) -> None:
    actual = utf8_size(markdown, field="rendered Markdown")
    if actual > limits.max_output_bytes:
        raise RenderingError(
            "rendered Markdown exceeds the configured byte limit",
            code="render_output_limit",
            details={
                "actual_bytes": actual,
                "max_bytes": limits.max_output_bytes,
            },
        )


def _preflight_materialization(
    result: RenderResult,
    *,
    name: str,
) -> None:
    """Apply the complete comment-envelope limits to a local render.

    Local rendering has no authenticated controller or stored revision yet.
    Maximum-width, low-compressibility controller metadata makes this
    provisional state conservative while reusing the production encoder for
    every wire and reserved-marker boundary.
    """

    if len(_PREFLIGHT_CONTROLLER_LOGIN.encode("ascii")) != MAX_GITHUB_LOGIN_BYTES:  # pragma: no cover
        raise AssertionError("preflight controller login must use the full GitHub login budget")

    encoded = encode_comment(
        State(
            name=name,
            format=STATE_FORMAT_V2 if result.meta is not None else STATE_FORMAT_V1,
            meta=result.meta,
            revision=MAX_REVISION,
            controller=ControllerV1(
                login=_PREFLIGHT_CONTROLLER_LOGIN,
                id=MAX_GITHUB_USER_ID,
            ),
            data=result.data,
            data_schema=result.data_schema,
            renderer=result.renderer,
            render_sha256=result.render_sha256,
        ),
        result.markdown,
    )
    # The fixed preview login cannot be a compression worst case: matching
    # user data may turn it into a short LZ reference. Hold a fixed margin
    # outside the compressed payload so every valid real login has room even
    # when the provisional value compresses unusually well.
    enforce_size_limits(
        SizeReport(
            compressed_bytes=(encoded.sizes.compressed_bytes + _PREFLIGHT_CONTROLLER_COMPRESSED_RESERVE),
            encoded_bytes=(encoded.sizes.encoded_bytes + _PREFLIGHT_CONTROLLER_ENCODED_RESERVE),
            body_bytes=(encoded.sizes.body_bytes + _PREFLIGHT_CONTROLLER_ENCODED_RESERVE),
        ),
        DEFAULT_CODEC_LIMITS,
    )


def _render(
    data: object,
    renderer: RendererDescriptorV1,
    *,
    schema: SchemaSnapshotV1 | bool | Mapping[str, object] | None = None,
    slate: SlateContext,
    meta: MetaSnapshot | None = None,
    limits: RenderLimits = DEFAULT_RENDER_LIMITS,
    preflight: bool,
) -> RenderResult:
    if renderer.kind != "jinja" or renderer.version != 2:
        raise migration_required()
    meta = MetaSnapshot.local(slate.name) if meta is None else meta
    if meta.name != slate.name:
        raise RenderingError("meta.slate.name must match the slate name", code="render_context_mismatch")
    canonical, snapshot = _canonical_data(data, schema)
    resolved_descriptor = renderer
    _enforce_components(canonical, snapshot, renderer)
    source, view = _parse_jinja_source(renderer, canonical)
    visible = render_jinja(source, data=canonical, slate=slate, meta=meta, render_limits=limits)

    markdown = normalize_visible_markdown(visible)
    _enforce_output(markdown, limits)
    _enforce_components(
        canonical,
        snapshot,
        resolved_descriptor,
        markdown=markdown,
    )
    result = RenderResult(
        data=canonical,
        data_schema=snapshot,
        renderer=resolved_descriptor,
        markdown=markdown,
        render_sha256=render_sha256(markdown),
        meta=meta,
        view=view,
    )
    if preflight:
        _preflight_materialization(result, name=slate.name)
    return result


def render(
    data: object,
    renderer: RendererDescriptorV1,
    *,
    schema: SchemaSnapshotV1 | bool | Mapping[str, object] | None = None,
    slate: SlateContext,
    meta: MetaSnapshot | None = None,
    limits: RenderLimits = DEFAULT_RENDER_LIMITS,
) -> RenderResult:
    """Validate, canonicalize, and locally preview one slate without I/O."""

    return _render(
        data,
        renderer,
        schema=schema,
        slate=slate,
        meta=meta,
        limits=limits,
        preflight=True,
    )


def render_state(
    state: State,
    *,
    slate: SlateContext | None = None,
    limits: RenderLimits = DEFAULT_RENDER_LIMITS,
) -> RenderResult:
    if not isinstance(state, State):
        raise RenderingError(
            "render_state requires a State",
            code="render_state_invalid",
        )
    context = SlateContext(name=state.name) if slate is None else slate
    if context.name != state.name:
        raise RenderingError(
            "slate context name does not match state name",
            code="render_context_mismatch",
            details={"context_name": context.name, "state_name": state.name},
        )
    if state.format != STATE_FORMAT_V2:
        raise migration_required()
    rendered = _render(
        state.data,
        state.renderer,
        schema=state.data_schema,
        slate=context,
        meta=state.meta,
        limits=limits,
        preflight=False,
    )
    return replace(rendered, renderer=state.renderer)


def materialize_comment(
    state: State,
    *,
    slate: SlateContext | None = None,
    limits: RenderLimits = DEFAULT_RENDER_LIMITS,
) -> MaterializedComment:
    rendered = render_state(state, slate=slate, limits=limits)
    materialized = replace(
        state,
        data=rendered.data,
        data_schema=rendered.data_schema,
        renderer=rendered.renderer,
        render_sha256=rendered.render_sha256,
    )
    encoded = encode_comment(materialized, rendered.markdown)
    return MaterializedComment(
        state=materialized,
        rendered=rendered,
        encoded=encoded,
    )


__all__ = [
    "MaterializedComment",
    "RenderResult",
    "jinja_descriptor",
    "materialize_comment",
    "render",
    "render_state",
]
