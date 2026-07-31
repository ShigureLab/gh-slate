from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import NoReturn, cast

from gh_slate.codec.errors import CodecError
from gh_slate.codec.json import freeze_json

STATE_FORMAT_V1 = "gh-slate/state-v1"
JSON_SCHEMA_DIALECT_2020_12 = "https://json-schema.org/draft/2020-12/schema"
MAX_REVISION = 2**63 - 1
MAX_RENDERER_VERSION = 2**31 - 1
MAX_GITHUB_USER_ID = 2**63 - 1
MAX_GITHUB_LOGIN_BYTES = 39

_SLATE_NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _invalid(message: str, *, path: str, value: object | None = None) -> NoReturn:
    details: dict[str, object] = {"path": path}
    if value is not None:
        details["value"] = value
    raise CodecError(message, code="invalid_state", details=details)


def _require_object(value: object, *, path: str) -> Mapping[str, object]:
    frozen = freeze_json(value)
    if not isinstance(frozen, Mapping):
        _invalid(f"{path} must be a JSON object", path=path)
    return cast("Mapping[str, object]", frozen)


def _reject_unknown_fields(
    value: Mapping[str, object],
    *,
    allowed: frozenset[str],
    path: str,
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise CodecError(
            f"{path} contains unknown fields: {', '.join(unknown)}",
            code="unknown_state_field",
            details={"path": path, "fields": unknown},
        )


def _require_fields(
    value: Mapping[str, object],
    *,
    required: frozenset[str],
    path: str,
) -> None:
    missing = sorted(required - set(value))
    if missing:
        raise CodecError(
            f"{path} is missing required fields: {', '.join(missing)}",
            code="missing_state_field",
            details={"path": path, "fields": missing},
        )


def _require_string(value: object, *, path: str, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        _invalid(f"{path} must be a non-empty string", path=path)
    return value


def _require_integer(value: object, *, path: str, minimum: int, maximum: int | None = None) -> int:
    if isinstance(value, bool):
        _invalid(f"{path} must be an integer", path=path)

    if isinstance(value, int):
        integer = value
    elif isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        if value < minimum or (maximum is not None and value > maximum):
            limit = (
                f" between {minimum} and {maximum}" if maximum is not None else f" greater than or equal to {minimum}"
            )
            _invalid(
                f"{path} must be{limit}",
                path=path,
                value=str(value),
            )
        integer = int(value)
    else:
        _invalid(f"{path} must be an integer", path=path)

    if integer < minimum or (maximum is not None and integer > maximum):
        limit = f" between {minimum} and {maximum}" if maximum is not None else f" greater than or equal to {minimum}"
        _invalid(f"{path} must be{limit}", path=path, value=integer)
    return integer


def validate_slate_name(name: object) -> str:
    """Validate and return one marker-safe public slate identifier."""

    result = _require_string(name, path="name")
    if _SLATE_NAME_RE.fullmatch(result) is None or "--" in result:
        _invalid(
            "name must match [a-z0-9][a-z0-9._-]{0,63} and must not contain consecutive hyphens",
            path="name",
            value=result,
        )
    return result


def _validate_sha256(value: object, *, path: str) -> str:
    result = _require_string(value, path=path)
    if _SHA256_RE.fullmatch(result) is None:
        _invalid(f"{path} must be a lowercase 64-character SHA-256 digest", path=path)
    return result


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class ControllerV1:
    login: str
    id: int | None = None

    def __post_init__(self) -> None:
        login = _require_string(self.login, path="controller.login")
        try:
            login_bytes = login.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            _invalid("controller.login must be valid UTF-8 text", path="controller.login")
        if len(login_bytes) > MAX_GITHUB_LOGIN_BYTES:
            _invalid(
                f"controller.login must be at most {MAX_GITHUB_LOGIN_BYTES} UTF-8 bytes",
                path="controller.login",
            )
        object.__setattr__(self, "login", login)
        if self.id is not None:
            object.__setattr__(
                self,
                "id",
                _require_integer(
                    self.id,
                    path="controller.id",
                    minimum=1,
                    maximum=MAX_GITHUB_USER_ID,
                ),
            )

    def to_json(self) -> dict[str, object]:
        result: dict[str, object] = {"login": self.login}
        if self.id is not None:
            result["id"] = self.id
        return result

    @classmethod
    def from_json(cls, value: object) -> ControllerV1:
        obj = _require_object(value, path="controller")
        fields = frozenset({"id", "login"})
        _reject_unknown_fields(obj, allowed=fields, path="controller")
        _require_fields(obj, required=frozenset({"login"}), path="controller")
        return cls(
            login=_require_string(obj["login"], path="controller.login"),
            id=(
                None
                if "id" not in obj
                else _require_integer(
                    obj["id"],
                    path="controller.id",
                    minimum=1,
                    maximum=MAX_GITHUB_USER_ID,
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class RendererDescriptorV1:
    """A lossless renderer descriptor.

    The codec understands only the common ``kind`` and ``version`` fields.
    Renderer adapters validate their own configuration. Keeping the remaining
    fields opaque lets read-only commands export state created by a newer
    renderer without silently discarding its options.
    """

    kind: str
    version: int
    config: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _require_string(self.kind, path="renderer.kind"))
        object.__setattr__(
            self,
            "version",
            _require_integer(
                self.version,
                path="renderer.version",
                minimum=1,
                maximum=MAX_RENDERER_VERSION,
            ),
        )
        frozen = _require_object(self.config, path="renderer.config")
        collisions = sorted({"kind", "version"} & set(frozen))
        if collisions:
            raise CodecError(
                "renderer config must not redefine kind or version",
                code="invalid_renderer_config",
                details={"fields": collisions},
            )
        object.__setattr__(self, "config", frozen)

    @property
    def configuration(self) -> Mapping[str, object]:
        return self.config

    def to_json(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "version": self.version,
            **{key: _thaw_json(value) for key, value in self.config.items()},
        }

    @classmethod
    def from_json(cls, value: object) -> RendererDescriptorV1:
        obj = _require_object(value, path="renderer")
        _require_fields(obj, required=frozenset({"kind", "version"}), path="renderer")
        config = {key: item for key, item in obj.items() if key not in {"kind", "version"}}
        return cls(
            kind=_require_string(obj["kind"], path="renderer.kind"),
            version=_require_integer(
                obj["version"],
                path="renderer.version",
                minimum=1,
                maximum=MAX_RENDERER_VERSION,
            ),
            config=config,
        )


@dataclass(frozen=True, slots=True)
class SchemaSnapshotV1:
    dialect: str
    document: bool | Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "dialect",
            _require_string(self.dialect, path="data_schema.dialect"),
        )
        frozen = freeze_json(self.document)
        if not isinstance(frozen, (bool, Mapping)):
            _invalid(
                "data_schema.document must be a JSON object or boolean schema",
                path="data_schema.document",
            )
        object.__setattr__(self, "document", frozen)

    def to_json(self) -> dict[str, object]:
        return {
            "dialect": self.dialect,
            "document": _thaw_json(self.document),
        }

    @classmethod
    def from_json(cls, value: object) -> SchemaSnapshotV1:
        obj = _require_object(value, path="data_schema")
        fields = frozenset({"dialect", "document"})
        _reject_unknown_fields(obj, allowed=fields, path="data_schema")
        _require_fields(obj, required=fields, path="data_schema")
        document = obj["document"]
        if not isinstance(document, (bool, Mapping)):
            _invalid(
                "data_schema.document must be a JSON object or boolean schema",
                path="data_schema.document",
            )
        return cls(
            dialect=_require_string(obj["dialect"], path="data_schema.dialect"),
            document=cast("bool | Mapping[str, object]", document),
        )


@dataclass(frozen=True, slots=True)
class StateDraftV1:
    name: str
    controller: ControllerV1
    data: Mapping[str, object]
    renderer: RendererDescriptorV1
    render_sha256: str
    data_schema: SchemaSnapshotV1 | None = None
    format: str = STATE_FORMAT_V1

    def __post_init__(self) -> None:
        object.__setattr__(self, "format", _require_string(self.format, path="format"))
        if self.format != STATE_FORMAT_V1:
            raise CodecError(
                f"unsupported state format: {self.format}",
                code="unsupported_state_format",
                details={"format": self.format},
            )
        object.__setattr__(self, "name", validate_slate_name(self.name))
        if not isinstance(self.controller, ControllerV1):
            _invalid("controller must be a ControllerV1", path="controller")
        object.__setattr__(self, "data", _require_object(self.data, path="data"))
        if not isinstance(self.renderer, RendererDescriptorV1):
            _invalid("renderer must be a RendererDescriptorV1", path="renderer")
        if self.data_schema is not None and not isinstance(self.data_schema, SchemaSnapshotV1):
            _invalid("data_schema must be a SchemaSnapshotV1 or null", path="data_schema")
        object.__setattr__(
            self,
            "render_sha256",
            _validate_sha256(self.render_sha256, path="render_sha256"),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "format": self.format,
            "name": self.name,
            "controller": self.controller.to_json(),
            "data": _thaw_json(self.data),
            "data_schema": self.data_schema.to_json() if self.data_schema is not None else None,
            "renderer": self.renderer.to_json(),
            "render_sha256": self.render_sha256,
        }

    def with_revision(self, revision: int) -> StateV1:
        return StateV1(
            format=self.format,
            name=self.name,
            revision=revision,
            controller=self.controller,
            data=self.data,
            data_schema=self.data_schema,
            renderer=self.renderer,
            render_sha256=self.render_sha256,
        )

    @classmethod
    def from_json(cls, value: object) -> StateDraftV1:
        obj = _require_object(value, path="state")
        required = frozenset(
            {
                "format",
                "name",
                "controller",
                "data",
                "data_schema",
                "renderer",
                "render_sha256",
            }
        )
        _reject_unknown_fields(obj, allowed=required, path="state")
        _require_fields(obj, required=required, path="state")
        return cls(
            format=_require_string(obj["format"], path="format"),
            name=validate_slate_name(obj["name"]),
            controller=ControllerV1.from_json(obj["controller"]),
            data=_require_object(obj["data"], path="data"),
            data_schema=(SchemaSnapshotV1.from_json(obj["data_schema"]) if obj["data_schema"] is not None else None),
            renderer=RendererDescriptorV1.from_json(obj["renderer"]),
            render_sha256=_validate_sha256(obj["render_sha256"], path="render_sha256"),
        )


@dataclass(frozen=True, slots=True)
class StateV1:
    name: str
    revision: int
    controller: ControllerV1
    data: Mapping[str, object]
    renderer: RendererDescriptorV1
    render_sha256: str
    data_schema: SchemaSnapshotV1 | None = None
    format: str = STATE_FORMAT_V1

    def __post_init__(self) -> None:
        object.__setattr__(self, "format", _require_string(self.format, path="format"))
        if self.format != STATE_FORMAT_V1:
            raise CodecError(
                f"unsupported state format: {self.format}",
                code="unsupported_state_format",
                details={"format": self.format},
            )
        object.__setattr__(self, "name", validate_slate_name(self.name))
        object.__setattr__(
            self,
            "revision",
            _require_integer(
                self.revision,
                path="revision",
                minimum=1,
                maximum=MAX_REVISION,
            ),
        )
        if not isinstance(self.controller, ControllerV1):
            _invalid("controller must be a ControllerV1", path="controller")
        object.__setattr__(self, "data", _require_object(self.data, path="data"))
        if not isinstance(self.renderer, RendererDescriptorV1):
            _invalid("renderer must be a RendererDescriptorV1", path="renderer")
        if self.data_schema is not None and not isinstance(self.data_schema, SchemaSnapshotV1):
            _invalid("data_schema must be a SchemaSnapshotV1 or null", path="data_schema")
        object.__setattr__(
            self,
            "render_sha256",
            _validate_sha256(self.render_sha256, path="render_sha256"),
        )

    def to_json(self) -> dict[str, object]:
        value = self.to_draft().to_json()
        value["revision"] = self.revision
        return value

    def to_draft(self) -> StateDraftV1:
        return StateDraftV1(
            format=self.format,
            name=self.name,
            controller=self.controller,
            data=self.data,
            data_schema=self.data_schema,
            renderer=self.renderer,
            render_sha256=self.render_sha256,
        )

    @classmethod
    def from_json(cls, value: object) -> StateV1:
        obj = _require_object(value, path="state")
        required = frozenset(
            {
                "format",
                "name",
                "revision",
                "controller",
                "data",
                "data_schema",
                "renderer",
                "render_sha256",
            }
        )
        _reject_unknown_fields(obj, allowed=required, path="state")
        _require_fields(obj, required=required, path="state")
        return cls(
            format=_require_string(obj["format"], path="format"),
            name=validate_slate_name(obj["name"]),
            revision=_require_integer(
                obj["revision"],
                path="revision",
                minimum=1,
                maximum=MAX_REVISION,
            ),
            controller=ControllerV1.from_json(obj["controller"]),
            data=_require_object(obj["data"], path="data"),
            data_schema=(SchemaSnapshotV1.from_json(obj["data_schema"]) if obj["data_schema"] is not None else None),
            renderer=RendererDescriptorV1.from_json(obj["renderer"]),
            render_sha256=_validate_sha256(obj["render_sha256"], path="render_sha256"),
        )
