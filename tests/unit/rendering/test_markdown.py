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
    assert render_value("") == '<code>""</code>'
    assert render_value("null") == "null"
    assert render_value(True) == "true"
    assert render_value(False) == "false"
    assert render_value(Decimal("-0.000")) == "0"
    assert render_value(Decimal("1.2300")) == "1.23"
    assert render_value(Decimal("1e21")) == "1e+21"


def test_markdown_text_escapes_html_table_syntax_and_line_endings() -> None:
    assert escape_markdown_text("<b>&|\\`\r\nnext\rline") == r"&lt;b&gt;&amp;\|\\&#96;<br>next<br>line"


def test_containers_use_sorted_compact_canonical_json_in_code() -> None:
    value = MappingProxyType(
        {
            "z": ("a|b", "line\nbreak", "`tick`", "\\"),
            "a": Decimal("1.200"),
        }
    )

    assert compact_json(value) == ('{"a":1.2,"z":["a|b","line\\nbreak","`tick`","\\\\"]}')
    assert render_value(value) == ('<code>{"a":1.2,"z":["a\\|b","line\\\\nbreak","&#96;tick&#96;","\\\\\\\\"]}</code>')


def test_invalid_python_value_fails_at_rendering_boundary() -> None:
    with pytest.raises(RenderingError) as caught:
        render_value(1.5)

    assert caught.value.code == "render_value_invalid"
