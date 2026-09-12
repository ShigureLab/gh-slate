from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import cast
from urllib.parse import urlsplit

from gh_slate.codec.errors import CodecError
from gh_slate.codec.json import freeze_json


def _object(value: object, keys: set[str], path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise CodecError(
            f"{path} must contain exactly {', '.join(sorted(keys))}",
            code="meta_invalid",
            details={"path": path},
        )
    return cast("Mapping[str, object]", value)


def _text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CodecError(f"{path} must be non-empty text", code="meta_invalid", details={"path": path})
    return value


@dataclass(frozen=True, slots=True)
class MetaSnapshot:
    """The exact read-only target context used for one render."""

    value: Mapping[str, object]

    def __post_init__(self) -> None:
        value = _object(self.value, {"host", "repository", "target", "slate"}, "meta")
        slate = _object(value["slate"], {"name"}, "meta.slate")
        _text(slate["name"], "meta.slate.name")
        if not all(value[key] is None for key in ("host", "repository", "target")):
            host = _text(value["host"], "meta.host")
            repo = _object(value["repository"], {"owner", "name", "full_name", "url"}, "meta.repository")
            target = _object(value["target"], {"kind", "number", "id", "url"}, "meta.target")
            owner, name = _text(repo["owner"], "meta.repository.owner"), _text(repo["name"], "meta.repository.name")
            repo_url = _text(repo["url"], "meta.repository.url")
            _text(target["id"], "meta.target.id")
            number = target["number"]
            if (
                isinstance(number, bool)
                or not isinstance(number, (int, Decimal))
                or (isinstance(number, Decimal) and not number.is_finite())
                or not 1 <= number <= 2**63 - 1
                or number != int(number)
            ):
                raise CodecError("meta.target.number must be a positive integer", code="meta_invalid")
            route = {"issue": "issues", "pull_request": "pull"}.get(str(target["kind"]))
            try:
                parsed = urlsplit(repo_url)
                valid_url = (
                    parsed.scheme in {"http", "https"}
                    and parsed.netloc == host
                    and parsed.username is None
                    and parsed.password is None
                    and parsed.path == f"/{owner}/{name}"
                    and not parsed.query
                    and not parsed.fragment
                )
            except ValueError:
                valid_url = False
            if (
                not valid_url
                or repo["full_name"] != f"{owner}/{name}"
                or route is None
                or target["url"] != f"{repo_url}/{route}/{int(number)}"
            ):
                raise CodecError("meta fields must describe one GitHub target", code="meta_invalid")
        object.__setattr__(self, "value", cast("Mapping[str, object]", freeze_json(value)))

    @property
    def name(self) -> str:
        return cast("str", cast("Mapping[str, object]", self.value["slate"])["name"])

    @property
    def target_id(self) -> str | None:
        target = self.value["target"]
        return None if target is None else cast("str", cast("Mapping[str, object]", target)["id"])

    def to_json(self) -> dict[str, object]:
        return {key: dict(value) if isinstance(value, Mapping) else value for key, value in self.value.items()}

    @classmethod
    def from_json(cls, value: object) -> MetaSnapshot:
        return cls(_object(value, {"host", "repository", "target", "slate"}, "meta"))

    @classmethod
    def local(cls, name: str) -> MetaSnapshot:
        return cls({"host": None, "repository": None, "target": None, "slate": {"name": name}})
