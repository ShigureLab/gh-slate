from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, NoReturn, cast

from referencing import Registry
from referencing.exceptions import NoSuchResource, Unresolvable
from referencing.jsonschema import DRAFT202012

from gh_slate.codec import (
    ControllerV1,
    EncodedComment,
    JsonValue,
    RendererDescriptorV1,
    SchemaSnapshotV1,
    StateV1,
    canonical_json_bytes,
    encode_comment,
    normalize_visible_markdown,
    render_sha256,
    strict_loads,
)
from gh_slate.codec.limits import SizeReport, enforce_size_limits
from gh_slate.codec.model import (
    MAX_GITHUB_LOGIN_BYTES,
    MAX_GITHUB_USER_ID,
    MAX_REVISION,
)
from gh_slate.codec.text import utf8_size
from gh_slate.rendering.errors import RenderingError
from gh_slate.rendering.jinja import SlateContext, render_jinja
from gh_slate.rendering.jq import select_one
from gh_slate.rendering.limits import DEFAULT_RENDER_LIMITS, RenderLimits
from gh_slate.rendering.list import render_list
from gh_slate.rendering.model import (
    ListRendererV1,
    TableRendererV1,
    parse_renderer_descriptor,
)
from gh_slate.rendering.table import render_table, resolve_table_renderer
from gh_slate.schema import validate_data, validate_schema

if TYPE_CHECKING:
    from referencing._core import Resolver

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_MAX_SCHEMA_PROJECTION_PARTS = 2048
_PREFLIGHT_CONTROLLER_LOGIN = "0123456789abcdefghijklmnopqrstuvwxyz-a0"


@dataclass(frozen=True, slots=True)
class RenderResult:
    """One deterministic local render and its canonical inputs."""

    data: Mapping[str, JsonValue]
    data_schema: SchemaSnapshotV1 | None
    renderer: RendererDescriptorV1
    markdown: str
    render_sha256: str


@dataclass(frozen=True, slots=True)
class MaterializedComment:
    """A state whose renderer/hash match the encoded comment body."""

    state: StateV1
    rendered: RenderResult
    encoded: EncodedComment


def jinja_descriptor(source: str) -> RendererDescriptorV1:
    if not isinstance(source, str):
        raise RenderingError(
            "Jinja source must be text",
            code="jinja_source_invalid",
        )
    return RendererDescriptorV1(
        kind="jinja",
        version=1,
        config={"source": source},
    )


def _parse_jinja_source(descriptor: RendererDescriptorV1) -> str:
    if descriptor.version != 1:
        raise RenderingError(
            "renderer version is not supported for rendering",
            code="renderer_unsupported",
            details={
                "kind": descriptor.kind,
                "version": descriptor.version,
            },
        )
    config = descriptor.configuration
    if set(config) != {"source"} or not isinstance(config.get("source"), str):
        raise RenderingError(
            "jinja@1 renderer must contain exactly one text source field",
            code="renderer_config_invalid",
        )
    return cast("str", config["source"])


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


def _selector_path(selector: str) -> tuple[str, ...] | None:
    """Parse the non-transforming jq subset usable for schema projection."""

    selector = selector.strip()
    if selector == ".":
        return ()
    if not selector.startswith("."):
        return None

    path: list[str] = []
    position = 0
    while position < len(selector):
        if selector[position] == ".":
            position += 1
            if position < len(selector) and selector[position] == "[":
                continue
            match = _IDENTIFIER.match(selector, position)
            if match is None:
                return None
            path.append(match.group())
            position = match.end()
            continue
        if not selector.startswith("[", position):
            return None
        token_start = position + 1
        if token_start >= len(selector) or selector[token_start] != '"':
            return None
        cursor = token_start + 1
        escaped = False
        while cursor < len(selector):
            character = selector[cursor]
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                break
            cursor += 1
        closing_quote = cursor
        closing_bracket = closing_quote + 1
        if closing_quote >= len(selector) or closing_bracket >= len(selector) or selector[closing_bracket] != "]":
            return None
        token = selector[token_start : closing_quote + 1]
        try:
            key = strict_loads(token)
        except Exception:
            return None
        if not isinstance(key, str):
            return None
        path.append(key)
        position = closing_bracket + 1
    return tuple(path)


def _deny_schema_retrieve(uri: str) -> NoReturn:
    raise NoSuchResource(ref=uri)


@dataclass(slots=True)
class _SchemaProjection:
    parts_seen: int = 0

    def parts(
        self,
        schema: object,
        resolver: Resolver[object],
        *,
        active: frozenset[int] = frozenset(),
    ) -> tuple[tuple[Mapping[str, object], Resolver[object]], ...]:
        if not isinstance(schema, Mapping):
            return ()
        typed = cast("Mapping[str, object]", schema)
        identity = id(typed)
        if identity in active or self.parts_seen >= _MAX_SCHEMA_PROJECTION_PARTS:
            return ()
        self.parts_seen += 1
        nested_resolver = resolver.in_subresource(
            DRAFT202012.create_resource(typed),
        )
        next_active = active | {identity}
        result: list[tuple[Mapping[str, object], Resolver[object]]] = []

        # Draft 2020-12 permits siblings next to references. Expand the
        # referenced base first, then retain this schema's own annotations.
        for keyword in ("$ref", "$dynamicRef"):
            reference = typed.get(keyword)
            if not isinstance(reference, str):
                continue
            try:
                resolved = nested_resolver.lookup(reference)
            except Unresolvable:
                continue
            result.extend(
                self.parts(
                    resolved.contents,
                    resolved.resolver,
                    active=next_active,
                )
            )

        result.append((typed, nested_resolver))
        for keyword in ("allOf", "oneOf", "anyOf"):
            alternatives = typed.get(keyword)
            if not isinstance(alternatives, tuple):
                continue
            for subschema in alternatives:
                result.extend(
                    self.parts(
                        subschema,
                        nested_resolver,
                        active=next_active,
                    )
                )
        for keyword in ("if", "then", "else"):
            subschema = typed.get(keyword)
            if not isinstance(subschema, Mapping):
                continue
            result.extend(
                self.parts(
                    subschema,
                    nested_resolver,
                    active=next_active,
                )
            )
        return tuple(result)


def _table_item_schema(
    snapshot: SchemaSnapshotV1 | None,
    selector: str,
) -> object:
    if snapshot is None or not isinstance(snapshot.document, Mapping):
        return None
    path = _selector_path(selector)
    if path is None:
        return None

    root = snapshot.document
    projection = _SchemaProjection()
    resolver: Resolver[object] = Registry(
        retrieve=_deny_schema_retrieve,
    ).resolver_with_root(
        DRAFT202012.create_resource(root),
    )
    candidates: tuple[tuple[object, Resolver[object]], ...] = ((root, resolver),)
    for key in path:
        next_candidates: list[tuple[object, Resolver[object]]] = []
        for candidate, candidate_resolver in candidates:
            for fragment, fragment_resolver in projection.parts(
                candidate,
                candidate_resolver,
            ):
                properties = fragment.get("properties")
                if not isinstance(properties, Mapping) or key not in properties:
                    continue
                subschema = cast("Mapping[str, object]", properties)[key]
                if isinstance(subschema, (bool, Mapping)):
                    next_candidates.append((subschema, fragment_resolver))
        if not next_candidates:
            return None
        candidates = tuple(next_candidates)

    item_candidates: list[tuple[object, Resolver[object]]] = []
    for candidate, candidate_resolver in candidates:
        for fragment, fragment_resolver in projection.parts(
            candidate,
            candidate_resolver,
        ):
            items = fragment.get("items")
            if isinstance(items, (bool, Mapping)):
                item_candidates.append((items, fragment_resolver))

    properties_in_order: dict[str, object] = {}
    for candidate, candidate_resolver in item_candidates:
        for fragment, _fragment_resolver in projection.parts(
            candidate,
            candidate_resolver,
        ):
            properties = fragment.get("properties")
            if not isinstance(properties, Mapping):
                continue
            for key in properties:
                if isinstance(key, str):
                    properties_in_order.setdefault(key, {})

    return {"properties": properties_in_order} if properties_in_order else None


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


def _preflight_materialization(result: RenderResult, *, name: str) -> None:
    """Apply the complete comment-envelope limits to a local render.

    Local rendering has no authenticated controller or stored revision yet.
    Maximum-width, low-compressibility controller metadata makes this
    provisional state conservative while reusing the production encoder for
    every wire and reserved-marker boundary.
    """

    if len(_PREFLIGHT_CONTROLLER_LOGIN.encode("ascii")) != MAX_GITHUB_LOGIN_BYTES:  # pragma: no cover
        raise AssertionError("preflight controller login must use the full GitHub login budget")

    encode_comment(
        StateV1(
            name=name,
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


def _render(
    data: object,
    renderer: RendererDescriptorV1,
    *,
    schema: SchemaSnapshotV1 | bool | Mapping[str, object] | None = None,
    slate: SlateContext,
    limits: RenderLimits = DEFAULT_RENDER_LIMITS,
    preflight: bool,
) -> RenderResult:
    canonical, snapshot = _canonical_data(data, schema)
    resolved_descriptor = renderer
    _enforce_components(canonical, snapshot, renderer)

    if renderer.kind == "jinja":
        visible = render_jinja(
            _parse_jinja_source(renderer),
            data=canonical,
            slate=slate,
            render_limits=limits,
        )
    else:
        parsed = parse_renderer_descriptor(renderer)
        selected = select_one(canonical, parsed.selector)
        if isinstance(parsed, TableRendererV1):
            item_schema = (
                None
                if parsed.columns
                else _table_item_schema(
                    snapshot,
                    parsed.selector,
                )
            )
            resolved = resolve_table_renderer(
                parsed,
                selected,
                item_schema,
            )
            resolved_descriptor = resolved.to_descriptor()
            visible = render_table(selected, resolved, limits)
        elif isinstance(parsed, ListRendererV1):
            visible = render_list(selected, parsed, limits)
        else:  # pragma: no cover - exhaustive for the current RendererV1 alias
            raise RenderingError(
                "renderer kind is not supported for rendering",
                code="renderer_unsupported",
            )

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
    limits: RenderLimits = DEFAULT_RENDER_LIMITS,
) -> RenderResult:
    """Validate, canonicalize, and locally preview one slate without I/O."""

    return _render(
        data,
        renderer,
        schema=schema,
        slate=slate,
        limits=limits,
        preflight=True,
    )


def render_state(
    state: StateV1,
    *,
    slate: SlateContext | None = None,
    limits: RenderLimits = DEFAULT_RENDER_LIMITS,
) -> RenderResult:
    if not isinstance(state, StateV1):
        raise RenderingError(
            "render_state requires a StateV1",
            code="render_state_invalid",
        )
    context = SlateContext(name=state.name) if slate is None else slate
    if context.name != state.name:
        raise RenderingError(
            "slate context name does not match state name",
            code="render_context_mismatch",
            details={"context_name": context.name, "state_name": state.name},
        )
    if state.renderer.kind == "jinja":
        missing_fields: list[str] = []
        if not isinstance(context.repository, str) or not context.repository:
            missing_fields.append("repository")
        if isinstance(context.number, bool) or not isinstance(context.number, int) or context.number <= 0:
            missing_fields.append("number")
        if not isinstance(context.url, str) or not context.url:
            missing_fields.append("url")
        if missing_fields:
            raise RenderingError(
                "rerendering a Jinja state requires its complete target context",
                code="render_context_required",
                details={"missing_fields": missing_fields},
                hints=("supply the repository, issue or pull request number, and URL from the comment target",),
            )
    rendered = _render(
        state.data,
        state.renderer,
        schema=state.data_schema,
        slate=context,
        limits=limits,
        preflight=False,
    )
    if state.renderer.kind != "jinja":
        parsed = parse_renderer_descriptor(state.renderer)
        resolved = parse_renderer_descriptor(rendered.renderer)
        if (
            isinstance(parsed, TableRendererV1)
            and not parsed.columns
            and isinstance(resolved, TableRendererV1)
            and resolved.columns
        ):
            # An unresolved table descriptor is not stable state: schema
            # canonicalization can reorder its properties before the next
            # render. Persist the columns chosen from the original schema.
            return rendered

    # Decoding and rerendering an existing resolved state must not silently
    # migrate its renderer descriptor. This preserves permanent state-v1
    # fixtures and unknown default spellings byte-for-byte at the state
    # boundary.
    return replace(rendered, renderer=state.renderer)


def materialize_comment(
    state: StateV1,
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
