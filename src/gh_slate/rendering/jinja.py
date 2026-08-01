from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, fields
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)
from types import MappingProxyType
from typing import cast

from jinja2 import StrictUndefined, TemplateError, Undefined, UndefinedError, nodes
from jinja2.sandbox import ImmutableSandboxedEnvironment, SecurityError
from jinja2.visitor import NodeTransformer

from gh_slate.codec.errors import CodecError
from gh_slate.codec.json import (
    canonical_json_bytes,
    canonical_number,
    strict_loads,
)
from gh_slate.codec.text import utf8_size
from gh_slate.rendering.errors import RenderingError
from gh_slate.rendering.limits import DEFAULT_RENDER_LIMITS, RenderLimits
from gh_slate.rendering.list import render_list
from gh_slate.rendering.markdown import compact_json
from gh_slate.rendering.model import (
    ListRendererV1,
    TableColumn,
    TableRendererV1,
)
from gh_slate.rendering.table import render_table, resolve_table_renderer


@dataclass(frozen=True, slots=True)
class JinjaLimits:
    max_source_bytes: int = 64 * 1024
    max_ast_nodes: int = 2_000
    max_loop_iterations: int = 1_000
    max_output_bytes: int = DEFAULT_RENDER_LIMITS.max_output_bytes

    def __post_init__(self) -> None:
        hard_limits = {
            "max_source_bytes": 64 * 1024,
            "max_ast_nodes": 10_000,
            "max_loop_iterations": 10_000,
            "max_output_bytes": DEFAULT_RENDER_LIMITS.max_output_bytes,
        }
        for item in fields(self):
            value = getattr(self, item.name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{item.name} must be a positive integer")
            if value > hard_limits[item.name]:
                raise ValueError(f"{item.name} must be less than or equal to {hard_limits[item.name]}")


DEFAULT_JINJA_LIMITS = JinjaLimits()
_JINJA_DECIMAL_CONTEXT = Context(
    prec=28,
    rounding=ROUND_HALF_EVEN,
    Emin=-999_999,
    Emax=999_999,
    capitals=1,
    clamp=0,
    flags=[],
    traps=[InvalidOperation, DivisionByZero, Overflow],
)


@dataclass(frozen=True, slots=True)
class SlateContext:
    name: str
    repository: str | None = None
    number: int | None = None
    url: str | None = None

    def to_mapping(self) -> Mapping[str, object]:
        value: dict[str, object] = {
            "name": self.name,
            "repository": self.repository,
            "number": self.number,
            "url": self.url,
        }
        return MappingProxyType(value)


class _SlateSandbox(ImmutableSandboxedEnvironment):
    def getattr(self, obj: object, attribute: str) -> object:
        if attribute.startswith("_"):
            return self.unsafe_undefined(obj, attribute)
        if isinstance(obj, Mapping) and attribute in obj:
            return cast("Mapping[str, object]", obj)[attribute]
        return super().getattr(obj, attribute)

    def getitem(self, obj: object, argument: object) -> object:
        if isinstance(argument, str) and argument.startswith("_"):
            return self.unsafe_undefined(obj, argument)
        return super().getitem(obj, argument)

    def is_safe_attribute(
        self,
        obj: object,
        attr: str,
        value: object,
    ) -> bool:
        del obj, attr, value
        return False

    def is_safe_callable(self, obj: object) -> bool:
        del obj
        return False


_PUBLIC_FILTERS = frozenset({"compact_json", "md_list", "md_table"})
_LOOP_GUARD_FILTER = "__gh_slate_loop_guard"
_MAX_AST_DEPTH = 64
_TARGET_CONTEXT_FIELDS = frozenset({"repository", "number", "url"})
_RESERVED_CONTEXT_NAMES = frozenset({"data", "slate"})

_FORBIDDEN_NODES = (
    nodes.Add,
    nodes.AssignBlock,
    nodes.Call,
    nodes.CallBlock,
    nodes.Concat,
    nodes.Extends,
    nodes.FilterBlock,
    nodes.FromImport,
    nodes.Import,
    nodes.Include,
    nodes.Macro,
    nodes.Mod,
    nodes.Mul,
    nodes.Pow,
)


def _walk(node: nodes.Node, *, limit: int) -> list[nodes.Node]:
    result: list[nodes.Node] = []
    pending: list[tuple[nodes.Node, int]] = [(node, 0)]
    while pending:
        current, depth = pending.pop()
        if depth > _MAX_AST_DEPTH:
            raise RenderingError(
                "Jinja template exceeds the AST depth limit",
                code="jinja_ast_limit",
                details={"max_depth": _MAX_AST_DEPTH},
            )
        result.append(current)
        if len(result) > limit:
            raise RenderingError(
                "Jinja template exceeds the AST node limit",
                code="jinja_ast_limit",
                details={"max_nodes": limit},
            )
        pending.extend((child, depth + 1) for child in current.iter_child_nodes())
    return result


@dataclass(slots=True)
class _LoopBudget:
    maximum: int
    consumed: int = 0

    def guard(self, value: object) -> object:
        try:
            iterator = iter(cast("Iterable[object]", value))
        except TypeError:
            return value

        def guarded() -> Iterator[object]:
            for item in iterator:
                self.consumed += 1
                if self.consumed > self.maximum:
                    raise RenderingError(
                        "Jinja template exceeds the loop iteration limit",
                        code="jinja_loop_limit",
                        details={"max_iterations": self.maximum},
                    )
                yield item

        return guarded()


class _GuardLoops(NodeTransformer):
    def visit_For(
        self,
        node: nodes.For,
        *args: object,
        **kwargs: object,
    ) -> nodes.Node:
        transformed = self.generic_visit(node, *args, **kwargs)
        assert isinstance(transformed, nodes.For)
        guarded = nodes.Filter(
            transformed.iter,
            _LOOP_GUARD_FILTER,
            [],
            [],
            None,
            None,
        )
        guarded.set_lineno(transformed.lineno)
        transformed.iter = guarded
        return transformed


def _finalize(value: object) -> object:
    if isinstance(value, Undefined):
        return value
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, Decimal):
        return canonical_number(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return canonical_number(value)
    if isinstance(value, (Mapping, tuple, list)):
        raise RenderingError(
            "containers must use md_table, md_list, or compact_json",
            code="jinja_container_interpolation",
        )
    if isinstance(value, str):
        return value
    raise RenderingError(
        "Jinja expressions must produce JSON scalar values or filter output",
        code="jinja_value_invalid",
        details={"value_type": type(value).__name__},
    )


def _canonical_mapping(data: object, *, subject: str) -> Mapping[str, object]:
    try:
        value = strict_loads(canonical_json_bytes(data))
    except CodecError as error:
        raise RenderingError(
            f"Jinja {subject} is not a canonical JSON object",
            code="jinja_data_invalid",
        ) from error
    if not isinstance(value, Mapping):
        raise RenderingError(
            f"Jinja {subject} root must be an object",
            code="jinja_data_invalid",
        )
    return cast("Mapping[str, object]", value)


def _environment(
    limits: JinjaLimits,
    loop_budget: _LoopBudget,
    render_limits: RenderLimits,
) -> _SlateSandbox:
    environment = _SlateSandbox(
        loader=None,
        autoescape=False,
        undefined=StrictUndefined,
        finalize=_finalize,
        enable_async=False,
    )
    defined_test = environment.tests["defined"]
    undefined_test = environment.tests["undefined"]
    none_test = environment.tests["none"]
    environment.globals.clear()
    environment.filters.clear()
    environment.tests.clear()
    environment.tests.update(
        {
            "defined": defined_test,
            "undefined": undefined_test,
            "none": none_test,
        }
    )
    environment.filters["compact_json"] = compact_json
    environment.filters[_LOOP_GUARD_FILTER] = loop_budget.guard

    def md_table(value: object, columns: object = None) -> str:
        typed_columns: tuple[TableColumn, ...] = ()
        if columns is not None:
            if not isinstance(columns, (tuple, list)) or not all(isinstance(column, str) for column in columns):
                raise RenderingError(
                    "md_table columns must be an array of strings",
                    code="jinja_filter_invalid",
                )
            column_names = cast("tuple[str, ...]", tuple(columns))
            typed_columns = tuple(TableColumn(path=(column,), header=column) for column in column_names)
        row_count = len(value) if isinstance(value, (tuple, list)) else 1
        renderer = resolve_table_renderer(
            TableRendererV1(
                selector=".",
                title=None,
                columns=typed_columns,
                max_rows=max(
                    1,
                    min(row_count, DEFAULT_RENDER_LIMITS.max_table_rows),
                ),
            ),
            value,
        )
        return render_table(value, renderer, render_limits)

    def md_list(value: object) -> str:
        return render_list(
            value,
            ListRendererV1(
                selector=".",
                title=None,
                max_depth=min(4, limits.max_loop_iterations),
                max_items=min(500, limits.max_loop_iterations),
            ),
            render_limits,
        )

    environment.filters["md_table"] = md_table
    environment.filters["md_list"] = md_list
    return environment


def _validated_syntax_tree(
    source: str,
    *,
    environment: _SlateSandbox,
    limits: JinjaLimits,
) -> tuple[nodes.Template, list[nodes.Node]]:
    if not isinstance(source, str):
        raise RenderingError(
            "Jinja source must be text",
            code="jinja_source_invalid",
        )
    try:
        source_bytes = utf8_size(source, field="Jinja source")
    except CodecError as error:
        raise RenderingError(
            "Jinja source is not valid UTF-8 text",
            code="jinja_source_invalid",
        ) from error
    if source_bytes > limits.max_source_bytes:
        raise RenderingError(
            "Jinja source exceeds the configured byte limit",
            code="jinja_source_limit",
            details={
                "actual_bytes": source_bytes,
                "max_bytes": limits.max_source_bytes,
            },
        )
    try:
        syntax_tree = environment.parse(source)
    except TemplateError as error:
        raise RenderingError(
            "Jinja template could not be parsed",
            code="jinja_syntax_error",
        ) from error
    except (MemoryError, OverflowError, RecursionError, ValueError) as error:
        raise RenderingError(
            "Jinja template exceeded a parsing resource limit",
            code="jinja_resource_limit",
        ) from error
    all_nodes = _walk(syntax_tree, limit=limits.max_ast_nodes)
    forbidden = next(
        (node for node in all_nodes if isinstance(node, _FORBIDDEN_NODES)),
        None,
    )
    if forbidden is not None:
        raise RenderingError(
            "Jinja template uses a disabled construct",
            code="jinja_construct_forbidden",
            details={"node": type(forbidden).__name__},
        )
    recursive_loop = next(
        (node for node in all_nodes if isinstance(node, nodes.For) and node.recursive),
        None,
    )
    if recursive_loop is not None:
        raise RenderingError(
            "Jinja template uses a disabled construct",
            code="jinja_construct_forbidden",
            details={"node": "RecursiveFor"},
        )
    shadowed_context = next(
        (
            node
            for node in all_nodes
            if isinstance(node, nodes.Name) and node.ctx in {"param", "store"} and node.name in _RESERVED_CONTEXT_NAMES
        ),
        None,
    )
    if shadowed_context is not None:
        raise RenderingError(
            "Jinja template cannot shadow a reserved context name",
            code="jinja_construct_forbidden",
            details={"name": shadowed_context.name},
        )
    unknown_filter = next(
        (node.name for node in all_nodes if isinstance(node, nodes.Filter) and node.name not in _PUBLIC_FILTERS),
        None,
    )
    if unknown_filter is not None:
        raise RenderingError(
            "Jinja template uses a disabled filter",
            code="jinja_construct_forbidden",
            details={"filter": unknown_filter},
        )
    return syntax_tree, all_nodes


def jinja_target_fields(
    source: str,
    *,
    limits: JinjaLimits = DEFAULT_JINJA_LIMITS,
) -> frozenset[str]:
    """Return target-dependent ``slate`` fields referenced by a template."""

    environment = _environment(
        limits,
        _LoopBudget(limits.max_loop_iterations),
        DEFAULT_RENDER_LIMITS,
    )
    _syntax_tree, all_nodes = _validated_syntax_tree(
        source,
        environment=environment,
        limits=limits,
    )
    fields: set[str] = set()
    direct_bases: set[int] = set()
    for node in all_nodes:
        field: object = None
        base: nodes.Node | None = None
        if isinstance(node, nodes.Getattr):
            base = node.node
            field = node.attr
        elif isinstance(node, nodes.Getitem):
            base = node.node
            field = node.arg.value if isinstance(node.arg, nodes.Const) else None
        if not isinstance(base, nodes.Name) or base.name != "slate":
            continue
        direct_bases.add(id(base))
        if field in _TARGET_CONTEXT_FIELDS:
            fields.add(cast("str", field))
        elif field != "name":
            fields.update(_TARGET_CONTEXT_FIELDS)

    if any(
        isinstance(node, nodes.Name) and node.name == "slate" and id(node) not in direct_bases for node in all_nodes
    ):
        fields.update(_TARGET_CONTEXT_FIELDS)
    return frozenset(fields)


def render_jinja(
    source: str,
    *,
    data: object,
    slate: SlateContext,
    limits: JinjaLimits = DEFAULT_JINJA_LIMITS,
    render_limits: RenderLimits = DEFAULT_RENDER_LIMITS,
) -> str:
    loop_budget = _LoopBudget(limits.max_loop_iterations)
    environment = _environment(limits, loop_budget, render_limits)
    syntax_tree, _all_nodes = _validated_syntax_tree(
        source,
        environment=environment,
        limits=limits,
    )
    canonical_data = _canonical_mapping(data, subject="data")
    canonical_slate = _canonical_mapping(slate.to_mapping(), subject="slate context")

    try:
        with localcontext(_JINJA_DECIMAL_CONTEXT):
            guarded_tree = _GuardLoops().visit(syntax_tree)
            code = environment.compile(guarded_tree)
            template = environment.template_class.from_code(
                environment,
                code,
                environment.globals,
                None,
            )
            chunks: list[str] = []
            output_bytes = 0
            for chunk in template.generate(
                data=canonical_data,
                slate=canonical_slate,
            ):
                output_bytes += utf8_size(chunk, field="Jinja output")
                output_limit = min(
                    limits.max_output_bytes,
                    render_limits.max_output_bytes,
                )
                if output_bytes > output_limit:
                    raise RenderingError(
                        "Jinja output exceeds the configured byte limit",
                        code="jinja_output_limit",
                        details={
                            "actual_bytes": output_bytes,
                            "max_bytes": output_limit,
                        },
                    )
                chunks.append(chunk)
            return "".join(chunks)
    except RenderingError:
        raise
    except UndefinedError:
        raise RenderingError(
            "Jinja template references an undefined value",
            code="jinja_undefined",
        ) from None
    except SecurityError:
        raise RenderingError(
            "Jinja template attempted an unsafe operation",
            code="jinja_security_error",
        ) from None
    except CodecError:
        raise RenderingError(
            "Jinja output is not valid UTF-8 text",
            code="jinja_output_invalid",
        ) from None
    except (MemoryError, OverflowError, RecursionError):
        raise RenderingError(
            "Jinja template exceeded a rendering resource limit",
            code="jinja_resource_limit",
        ) from None
    except (ArithmeticError, TemplateError, TypeError, ValueError) as error:
        raise RenderingError(
            "Jinja template could not be rendered safely",
            code="jinja_render_error",
            details={"error_type": type(error).__name__},
        ) from None


__all__ = [
    "DEFAULT_JINJA_LIMITS",
    "JinjaLimits",
    "SlateContext",
    "jinja_target_fields",
    "render_jinja",
]
