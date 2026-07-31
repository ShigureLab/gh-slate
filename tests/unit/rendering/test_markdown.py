from __future__ import annotations

from decimal import Decimal
from types import MappingProxyType

import pytest

from gh_slate.rendering.errors import RenderingError
from gh_slate.rendering.markdown import (
    MISSING,
    compact_json,
    escape_markdown_text,
    render_value,
)


def test_scalar_rendering_preserves_json_type_distinctions() -> None:
    assert render_value(MISSING) == "—"
    assert render_value(None) == "null"
    assert render_value("") == "<code>&#34;&#34;</code>"
    assert render_value("null") == "null"
    assert render_value(True) == "true"
    assert render_value(False) == "false"
    assert render_value(Decimal("-0.000")) == "0"
    assert render_value(Decimal("1.2300")) == "1.23"
    assert render_value(Decimal("1e21")) == "1e+21"


def test_markdown_text_neutralizes_all_ascii_punctuation_and_line_endings() -> None:
    assert escape_markdown_text(
        "**bold** [link](https://example.com) ![image](x) ~~strike~~ <b>&|\\`\r\nnext\rline"
    ) == (
        "&#42;&#42;bold&#42;&#42; "
        "&#91;link&#93;&#40;https&#58;&#47;&#47;example&#46;com&#41; "
        "&#33;&#91;image&#93;&#40;x&#41; "
        "&#126;&#126;strike&#126;&#126; "
        "&#60;b&#62;&#38;&#124;&#92;&#96;<br>next<br>line"
    )


def test_containers_use_sorted_compact_canonical_json_in_code() -> None:
    value = MappingProxyType(
        {
            "z": ("a|b", "line\nbreak", "`tick`", "\\"),
            "a": Decimal("1.200"),
        }
    )

    assert compact_json(value) == ('{"a":1.2,"z":["a|b","line\\nbreak","`tick`","\\\\"]}')
    assert render_value(value) == (
        "<code>&#123;&#34;a&#34;&#58;1&#46;2&#44;&#34;z&#34;&#58;&#91;"
        "&#34;a&#124;b&#34;&#44;&#34;line&#92;n"
        "break&#34;&#44;&#34;&#96;tick&#96;&#34;&#44;"
        "&#34;&#92;&#92;&#34;&#93;&#125;</code>"
    )


def test_invalid_python_value_fails_at_rendering_boundary() -> None:
    with pytest.raises(RenderingError) as caught:
        render_value(1.5)

    assert caught.value.code == "render_value_invalid"
