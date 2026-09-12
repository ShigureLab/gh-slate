from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from gh_slate.codec import RendererDescriptorV1, SchemaSnapshotV1, canonical_json_bytes
from gh_slate.codec.json import DEFAULT_JSON_LIMITS
from gh_slate.codec.limits import SizeReport, enforce_size_limits
from gh_slate.inputs import read_bytes, template_source
from gh_slate.rendering.errors import RenderingError
from gh_slate.rendering.jinja import validate_jinja_source
from gh_slate.rendering.routing import validate_views
from gh_slate.schema import validate_schema_json

MAX_CONFIG_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class ProfileDefinition:
    renderer: RendererDescriptorV1
    schema: SchemaSnapshotV1 | None


def _path(value: object, *, base: Path, field: str) -> Path:
    if not isinstance(value, str) or not value or value == "-" or "://" in value:
        raise RenderingError(f"{field} must name a local file", code="config_invalid")
    path = Path(value)
    return path if path.is_absolute() else base / path


def load_profile(
    name: str,
    *,
    config: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> ProfileDefinition:
    """Load exactly one explicit config and snapshot the chosen definition."""
    environment = os.environ if environ is None else environ
    location = config if config is not None else environment.get("GH_SLATE_CONFIG")
    if not location:
        raise RenderingError(
            "--profile requires --config FILE or GH_SLATE_CONFIG",
            code="config_required",
        )
    config_path = _path(location, base=Path.cwd(), field="config")
    raw = read_bytes(str(config_path), subject="config", max_bytes=MAX_CONFIG_BYTES)
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise RenderingError("configuration must be valid UTF-8 TOML", code="config_invalid") from error
    if set(document) != {"version", "profiles"} or type(document.get("version")) is not int or document["version"] != 1:
        raise RenderingError("configuration requires version = 1 and profiles", code="config_invalid")
    profiles = document["profiles"]
    if not isinstance(profiles, Mapping) or name not in profiles:
        raise RenderingError("profile was not found", code="profile_not_found", details={"profile": name})
    if not name or len(name.encode("utf-8")) > 128:
        raise RenderingError("profile name must be 1 to 128 UTF-8 bytes", code="config_invalid")
    profile = profiles[name]
    if not isinstance(profile, Mapping):
        raise RenderingError("profile must be a TOML table", code="config_invalid")
    profile = cast("Mapping[str, object]", profile)
    if set(profile) - {"template", "schema", "views", "view_by"}:
        raise RenderingError("profile contains unknown fields", code="config_invalid")
    base = config_path.parent
    definition: dict[str, object] = {"profile": name}
    if "template" in profile:
        if "views" in profile or "view_by" in profile:
            raise RenderingError("template is exclusive with views and view_by", code="config_invalid")
        source = template_source(str(_path(profile["template"], base=base, field="template")))
        validate_jinja_source(source)
        definition["source"] = source
    else:
        _, paths = validate_views(profile.get("view_by"), profile.get("views"))
        sources: dict[str, str] = {}
        for view, location in paths.items():
            source = template_source(str(_path(location, base=base, field=f"views.{view}")))
            validate_jinja_source(source)
            sources[view] = source
        definition.update({"view_by": profile["view_by"], "views": sources})
    schema = None
    if "schema" in profile:
        schema = validate_schema_json(
            read_bytes(
                str(_path(profile["schema"], base=base, field="schema")),
                subject="schema",
                max_bytes=DEFAULT_JSON_LIMITS.max_input_bytes,
            )
        )
    renderer = RendererDescriptorV1(kind="jinja", version=2, config=definition)
    enforce_size_limits(
        SizeReport(
            renderer_bytes=len(canonical_json_bytes(renderer.to_json())),
            schema_bytes=0 if schema is None else len(canonical_json_bytes(schema.to_json())),
        )
    )
    return ProfileDefinition(renderer=renderer, schema=schema)


__all__ = ["ProfileDefinition", "load_profile"]
