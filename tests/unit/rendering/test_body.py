from __future__ import annotations

import json
from dataclasses import replace
from html.parser import HTMLParser
from typing import TYPE_CHECKING

import pytest
from cmarkgfm import github_flavored_markdown_to_html
from cmarkgfm.cmark import Options

from gh_slate.cli import run
from gh_slate.codec.meta import MetaSnapshot
from gh_slate.rendering.errors import RenderingError
from gh_slate.rendering.jinja import DEFAULT_JINJA_LIMITS, render_jinja
from gh_slate.rendering.markdown import md_body, md_link

if TYPE_CHECKING:
    from pathlib import Path

REVIEW_URL = "https://github.com/redai-studio/Relax/pull/425#pullrequestreview-5466052458"
SUMMARY = f"第二轮增量审查完成，结论 APPROVE（{REVIEW_URL}）："


class _Document(HTMLParser):
    def __init__(self, markdown: str) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str | None, str]] = []
        self.tags: list[str] = []
        self.text: list[str] = []
        self._label: list[str] | None = None
        self._href: str | None = None
        # gh-slate creates safe inline HTML. cmark's default would omit it;
        # enable HTML so tests can inspect the actual anchors and code elements.
        self.feed(github_flavored_markdown_to_html(markdown, options=Options.CMARK_OPT_UNSAFE))
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._label = []
        if tag == "br":
            self.text.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            assert self._label is not None
            self.links.append((self._href, "".join(self._label)))
            self._label = None

    def handle_data(self, data: str) -> None:
        self.text.append(data)
        if self._label is not None:
            self._label.append(data)

    @property
    def visible_text(self) -> str:
        return "".join(self.text).removesuffix("\n")


def _render(body: str, template: str = "{{ data.body | md_body }}") -> str:
    return render_jinja(template, data={"body": body}, meta=MetaSnapshot.local("review"))


def test_actual_review_summary_has_one_complete_anchor_and_original_display_text() -> None:
    document = _Document(_render(SUMMARY))

    assert document.links == [(REVIEW_URL, REVIEW_URL)]
    assert document.visible_text == SUMMARY
    assert document.tags == ["p", "a", "span"]


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/path?q=a%20b&next=%2Fitems#review-123",
        "http://example.test:8080/path?flag=yes&empty=#fragment",
        "HTTPS://example.test/path?x=1#Fragment",
        "https://例子.测试/路径?q=中文#片段",
        "http://[::1]:8000/a_(b)?x=[v]#frag",
        "https://example.test/O'Reilly?q='x'#review",
        "https://example.test/%29?value=%2E#ending%3F",
        "https://example.test/?x=&quot;&y=&#35;#frag",
        "https://example.test/?email=user@example.test#frag",
    ],
)
def test_body_preserves_url_query_fragment_and_label_in_gfm(url: str) -> None:
    document = _Document(_render(url))

    assert document.links == [(url, url)]
    assert document.visible_text == url


@pytest.mark.parametrize("ending", ["。", "，", "；", "：", "！", "？", ".", ",", ";", ":", "!", "?", "..."])
def test_sentence_punctuation_stays_outside_the_anchor(ending: str) -> None:
    body = f"见{REVIEW_URL}{ending}"
    document = _Document(_render(body))

    assert document.links == [(REVIEW_URL, REVIEW_URL)]
    assert document.visible_text == body


def test_chinese_wrappers_and_multiple_links_preserve_body_text() -> None:
    second = "http://example.test/证据?a=1&b=2#details"
    body = f"结论（{REVIEW_URL}），证据【{second}】；请参阅《{REVIEW_URL}》。"
    document = _Document(_render(body))

    assert document.links == [(REVIEW_URL, REVIEW_URL), (second, second), (REVIEW_URL, REVIEW_URL)]
    assert document.visible_text == body


def test_ascii_wrappers_keep_balanced_brackets_inside_url() -> None:
    url = "https://example.test/a_(b)?q=[value]#frag"
    body = f"See ([{url}]), then '{REVIEW_URL}'."
    document = _Document(_render(body))

    assert document.links == [(url, url), (REVIEW_URL, REVIEW_URL)]
    assert document.visible_text == body


def test_body_escapes_html_markdown_and_entity_text_without_creating_elements() -> None:
    body = "<script>alert(1)</script> [click](javascript:alert(1)) **bold** ![image](x) ~~x~~ | &quot; \\ @name #425 "
    url = "https://example.test/?q=%22%3E%3Cscript%3E&html=%26lt%3B#frag"
    document = _Document(_render(body + url))

    assert document.links == [(url, url)]
    assert document.visible_text == body + url
    assert document.tags == ["p", "a", "span"]


def test_shared_link_builder_keeps_label_literal_without_nested_email_links() -> None:
    label = "Review user@example.test <img> [click] **bold** &quot;"
    document = _Document(md_link(label, REVIEW_URL))

    assert document.links == [(REVIEW_URL, label)]
    assert document.visible_text == label
    assert document.tags == ["p", "a", "span", "span"]


@pytest.mark.parametrize(
    "body",
    [
        "https:///missing-host",
        "https://?q=x",
        "https://user:secret@example.test/private",
        "https://[invalid]/path",
        "https://example.test/\x00hidden",
        "https://example.test/\x7fhidden",
        "javascript:alert(1) ftp://example.test/file /relative/path",
    ],
)
def test_invalid_or_non_http_urls_remain_safe_literal_text(body: str) -> None:
    document = _Document(_render(body))

    assert document.links == []
    # cmark replaces NUL with the Unicode replacement character.
    assert document.visible_text == body.replace("\x00", "�")
    assert set(document.tags) <= {"p", "code"}


def test_invalid_url_does_not_prevent_other_links_from_rendering() -> None:
    body = f"https://user@example.test/private，{REVIEW_URL}"
    document = _Document(_render(body))

    assert document.links == [(REVIEW_URL, REVIEW_URL)]
    assert document.visible_text == body


@pytest.mark.parametrize("template", ["{{ data.body }}", "{{ data.body | md_text }}", "{{ data.body | md_code }}"])
def test_literal_interpolation_and_filters_do_not_linkify_code(template: str) -> None:
    body = f"{REVIEW_URL} <tag> &amp; [x] **bold** `code`"
    document = _Document(_render(body, template))

    assert document.links == []
    assert document.visible_text == body
    assert set(document.tags) <= {"p", "code"}


def test_codeblock_keeps_urls_and_html_as_code_text() -> None:
    body = f"curl '{REVIEW_URL}'\n<tag> &amp; [x] **bold** `code`"
    document = _Document(_render(body, '{{ data.body | md_codeblock("text") }}'))

    assert document.links == []
    assert document.visible_text == body + "\n"
    assert document.tags == ["pre", "code"]


@pytest.mark.parametrize("delimiter", ["`", "``", "```"])
def test_body_leaves_backtick_delimited_text_literal(delimiter: str) -> None:
    body = f"{delimiter}curl {REVIEW_URL}\n<tag> &amp; user@example.test `x` {delimiter} then {REVIEW_URL}"
    document = _Document(_render(body))

    assert document.links == [(REVIEW_URL, REVIEW_URL)]
    assert document.visible_text == body
    assert set(document.tags) <= {"p", "br", "a", "span", "code"}


def test_body_code_with_nested_fences_cannot_expose_html_or_markdown() -> None:
    body = f"```\n````\n  \n<script>alert(1)</script>\n[bad](javascript:alert(1))\n``` then {REVIEW_URL}"
    document = _Document(_render(body))

    assert document.links == [(REVIEW_URL, REVIEW_URL)]
    assert document.visible_text == body
    assert set(document.tags) <= {"p", "br", "a", "span", "code"}


def test_body_normalizes_line_endings_like_literal_text() -> None:
    body = f"first\r\n{REVIEW_URL}\rlast"
    document = _Document(_render(body))

    assert document.links == [(REVIEW_URL, REVIEW_URL)]
    assert document.visible_text == f"first\n{REVIEW_URL}\nlast"


def test_body_composes_with_details_without_double_escaping() -> None:
    document = _Document(_render(SUMMARY, '{{ data.body | md_body | md_details("Summary") }}'))

    assert document.links == [(REVIEW_URL, REVIEW_URL)]
    assert SUMMARY in document.visible_text
    assert "details" in document.tags


def test_body_reuses_link_escaping_and_survives_gfm_table_cells() -> None:
    url = "https://example.test/?q=a|b&next=2#review"
    assert md_body(url) == md_link(url, url)
    markdown = _render(url, "| Body |\n| --- |\n| {{ data.body | md_body }} |")
    document = _Document(markdown)

    assert document.links == [(url.replace("|", "%7C"), url)]
    assert document.tags.count("td") == 1


@pytest.mark.parametrize("value", [None, True, 1, {}, []])
def test_body_requires_text(value: object) -> None:
    with pytest.raises(RenderingError) as caught:
        render_jinja("{{ data.body | md_body }}", data={"body": value}, meta=MetaSnapshot.local("review"))

    assert caught.value.code == "jinja_filter_invalid"


def test_body_expansion_obeys_existing_output_limits() -> None:
    with pytest.raises(RenderingError) as caught:
        render_jinja(
            "{{ data.body | md_body }}",
            data={"body": REVIEW_URL},
            meta=MetaSnapshot.local("review"),
            limits=replace(DEFAULT_JINJA_LIMITS, max_output_bytes=len(REVIEW_URL)),
        )

    assert caught.value.code == "jinja_output_limit"


def test_cli_renders_original_json_summary_as_a_complete_gfm_link(tmp_path: Path, capsys) -> None:
    data = tmp_path / "data.json"
    template = tmp_path / "summary.md.j2"
    data.write_text(json.dumps({"summary": SUMMARY}, ensure_ascii=False), encoding="utf-8")
    template.write_text("{{ data.summary | md_body }}", encoding="utf-8")

    assert run(["render", "review", "--data", str(data), "--template", str(template)]) == 0
    document = _Document(capsys.readouterr().out)

    assert document.links == [(REVIEW_URL, REVIEW_URL)]
    assert document.visible_text == SUMMARY
