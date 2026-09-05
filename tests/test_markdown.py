import inspect
import json
from dataclasses import FrozenInstanceError, fields

import pytest

import google_docs_mcp.markdown as markdown_module
from google_docs_mcp.client import DocsMCPError, utf16_index, utf16_length
from google_docs_mcp.markdown import (
    DocumentModel,
    HeadingRange,
    InlineContent,
    LinkRange,
    TableBlock,
    TextRange,
    parse_markdown,
)


def assert_invalid_markdown(error: DocsMCPError, *canaries: str) -> None:
    assert error.code == "invalid_markdown"
    assert len(error.message) <= 200
    public_graph = "\n".join(
        (
            str(error),
            repr(error),
            repr(error.args),
            repr(vars(error)),
            repr(error.as_result()),
        )
    )
    for canary in canaries:
        assert canary not in public_graph
    assert error.__context__ is None
    assert error.__cause__ is None


def test_models_have_exact_frozen_shapes_and_tuple_defaults() -> None:
    assert [field.name for field in fields(TextRange)] == ["start", "end"]
    assert [field.name for field in fields(HeadingRange)] == ["start", "end", "level"]
    assert [field.name for field in fields(LinkRange)] == ["start", "end", "url"]
    assert [field.name for field in fields(InlineContent)] == ["text", "bold", "links"]
    assert [field.name for field in fields(TableBlock)] == ["marker", "rows"]
    assert [field.name for field in fields(DocumentModel)] == [
        "text",
        "headings",
        "bold",
        "links",
        "tables",
    ]
    assert issubclass(HeadingRange, TextRange)
    assert issubclass(LinkRange, TextRange)

    inline = InlineContent("cell")
    table = TableBlock("⟦TABLE-0001⟧", ((inline,),))
    document = DocumentModel("cell\n", (), (), (), (table,))

    assert inline.bold == ()
    assert inline.links == ()
    assert isinstance(inline.bold, tuple)
    assert isinstance(inline.links, tuple)
    assert isinstance(table.rows, tuple)
    assert isinstance(table.rows[0], tuple)
    assert isinstance(document.headings, tuple)
    assert isinstance(document.bold, tuple)
    assert isinstance(document.links, tuple)
    assert isinstance(document.tables, tuple)
    assert not hasattr(table, "has_header")
    with pytest.raises(FrozenInstanceError):
        inline.text = "changed"  # type: ignore[misc]


def test_frontmatter_is_removed_after_newline_normalization() -> None:
    model = parse_markdown("  ---\r\ntitle: ignored\r --- \r# Head\rBody")

    assert model == DocumentModel(
        text="Head\nBody\n",
        headings=(HeadingRange(0, 4, 1),),
        bold=(),
        links=(),
        tables=(),
    )


def test_unclosed_frontmatter_is_preserved() -> None:
    model = parse_markdown("---\rmeta: yes\r# Kept")

    assert model.text == "---\nmeta: yes\nKept\n"
    assert model.headings == (HeadingRange(14, 18, 1),)


def test_all_six_heading_levels_have_codepoint_ranges_excluding_newlines() -> None:
    source = "\n".join(
        (
            "# h1",
            "## h2",
            "### h3",
            "#### h4",
            "##### h5",
            "###### h6",
            "####### seven",
            "#no",
        )
    )

    model = parse_markdown(source)

    assert model.text == "h1\nh2\nh3\nh4\nh5\nh6\n####### seven\n#no\n"
    assert model.headings == (
        HeadingRange(0, 2, 1),
        HeadingRange(3, 5, 2),
        HeadingRange(6, 8, 3),
        HeadingRange(9, 11, 4),
        HeadingRange(12, 14, 5),
        HeadingRange(15, 17, 6),
    )


def test_bold_and_http_links_have_exact_overlapping_ranges() -> None:
    source = (
        "**bold** [plain](https://p.test) [**label**](https://a.test) "
        "**[linked](http://b.test)**"
    )

    model = parse_markdown(source)

    assert model.text == "bold plain label linked\n"
    assert model.bold == (
        TextRange(0, 4),
        TextRange(11, 16),
        TextRange(17, 23),
    )
    assert model.links == (
        LinkRange(5, 10, "https://p.test"),
        LinkRange(11, 16, "https://a.test"),
        LinkRange(17, 23, "http://b.test"),
    )


def test_unmatched_or_unsupported_delimiters_remain_plain_and_visible() -> None:
    source = "a **open [label](ftp://bad) [open](https://x `code [[target"

    model = parse_markdown(source)

    assert model.text == source + "\n"
    assert model.bold == ()
    assert model.links == ()


def test_unmatched_delimiter_probes_have_linearly_bounded_scan_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repeated_openers = "[x" * 256 + "[[" * 256
    source = repeated_openers + "\n**" + repeated_openers
    scan_windows = 0
    original_link = markdown_module._match_link_at
    original_obsidian = markdown_module._match_obsidian_at

    def counted_link(candidate: str, start: int, end: int):
        nonlocal scan_windows
        if candidate[start] == "[" and not candidate.startswith("[[", start):
            scan_windows += end - start
        return original_link(candidate, start, end)

    def counted_obsidian(candidate: str, start: int, end: int):
        nonlocal scan_windows
        if candidate.startswith("[[", start):
            scan_windows += end - start
        return original_obsidian(candidate, start, end)

    monkeypatch.setattr(markdown_module, "_match_link_at", counted_link)
    monkeypatch.setattr(markdown_module, "_match_obsidian_at", counted_obsidian)

    model = parse_markdown(source)

    assert model == DocumentModel(source + "\n", (), (), (), ())
    assert scan_windows <= 4 * len(source)


@pytest.mark.parametrize(
    "candidate",
    (
        "[x](" * 256 + ")",
        "[label](https://example.test/a b/" + "[x](" * 256 + ")",
    ),
    ids=("invalid-scheme", "whitespace"),
)
def test_invalid_link_candidates_have_linearly_bounded_scan_windows(
    candidate: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = candidate + "\n**" + candidate
    scan_windows = 0
    original_link = markdown_module._match_link_at

    def counted_link(candidate_source: str, start: int, end: int):
        nonlocal scan_windows
        scan_windows += end - start
        return original_link(candidate_source, start, end)

    monkeypatch.setattr(markdown_module, "_match_link_at", counted_link)

    model = parse_markdown(source)

    assert model == DocumentModel(source + "\n", (), (), (), ())
    assert scan_windows <= 4 * len(source)


def test_inline_code_emits_inner_text_without_formatting() -> None:
    source = "`**not** [x](https://x.test) [[target|label]] \\*` and **yes**"
    literal = "**not** [x](https://x.test) [[target|label]] \\*"

    model = parse_markdown(source)

    assert model.text == literal + " and yes\n"
    yes_start = len(literal) + len(" and ")
    assert model.bold == (TextRange(yes_start, yes_start + len("yes")),)
    assert model.links == ()


def test_obsidian_forms_and_backslash_escaping_use_the_closed_subset() -> None:
    source = (
        r"[[target|label]] [[target#section]] [[target]] "
        r"\*\*plain\*\* \[x\]\(https://x.test\) tail"
        + "\\"
    )

    model = parse_markdown(source)

    assert model.text == "label section target **plain** [x](https://x.test) tail\\\n"
    assert model.bold == ()
    assert model.links == ()


def test_checkbox_and_bullet_lines_render_exactly() -> None:
    model = parse_markdown("- [ ] todo\n- [x] done\n- [X] upper\n- item")

    assert model.text == "☐ todo\n☑ done\n☑ upper\n• item\n"


def test_fences_callouts_and_horizontal_rules_remain_plain() -> None:
    source = "---\n```python\ncode\n```\n> [!NOTE]\n"

    model = parse_markdown(source)

    assert model.text == source
    assert model.bold == ()
    assert model.links == ()
    assert model.tables == ()


def test_table_cells_use_local_inline_ranges_and_unescaped_pipes() -> None:
    source = (
        "😀 before\n"
        "| H\\|1 | **H2** |\n"
        "| --- | :---: |\n"
        "| 😀 [go](https://e.test) **x** | a\\|b |"
    )

    model = parse_markdown(source)

    assert model.text == "😀 before\n⟦TABLE-0001⟧\n"
    assert model.headings == ()
    assert model.bold == ()
    assert model.links == ()
    assert model.tables == (
        TableBlock(
            marker="⟦TABLE-0001⟧",
            rows=(
                (
                    InlineContent("H|1"),
                    InlineContent("H2", bold=(TextRange(0, 2),)),
                ),
                (
                    InlineContent(
                        "😀 go x",
                        bold=(TextRange(5, 6),),
                        links=(LinkRange(2, 4, "https://e.test"),),
                    ),
                    InlineContent("a|b"),
                ),
            ),
        ),
    )
    cell = model.tables[0].rows[1][0]
    assert (utf16_index(cell.text, cell.links[0].start), utf16_index(cell.text, cell.links[0].end)) == (3, 5)
    assert (utf16_index(cell.text, cell.bold[0].start), utf16_index(cell.text, cell.bold[0].end)) == (6, 7)
    marker_start = model.text.index(model.tables[0].marker)
    assert marker_start == 9
    assert utf16_index(model.text, marker_start) == 10
    assert isinstance(model.tables[0].rows, tuple)
    assert all(isinstance(row, tuple) for row in model.tables[0].rows)


def test_multiple_tables_receive_deterministic_markers() -> None:
    source = (
        "| A |\n| --- |\n| one |\n"
        "between\n"
        "| B |\n| :---: |\n| two |"
    )

    first = parse_markdown(source)
    second = parse_markdown(source)

    assert first == second
    assert first.text == "⟦TABLE-0001⟧\nbetween\n⟦TABLE-0002⟧\n"
    assert tuple(table.marker for table in first.tables) == (
        "⟦TABLE-0001⟧",
        "⟦TABLE-0002⟧",
    )
    assert first.tables[0].rows == (
        (InlineContent("A"),),
        (InlineContent("one"),),
    )
    assert first.tables[1].rows == (
        (InlineContent("B"),),
        (InlineContent("two"),),
    )


def test_invalid_separator_does_not_create_a_table() -> None:
    source = "| heading |\n| -- |\n| data |"

    model = parse_markdown(source)

    assert model.text == source + "\n"
    assert model.tables == ()


def test_separator_without_leading_pipe_does_not_create_a_table() -> None:
    source = "| H |\nx---|"

    model = parse_markdown(source)

    assert model.text == source + "\n"
    assert model.tables == ()


@pytest.mark.parametrize(
    "source, canary",
    (
        ("⟦TABLE-0000⟧ COLLISION_CANARY", "COLLISION_CANARY"),
        ("\\⟦TABLE-12345⟧ ESCAPED_CANARY", "ESCAPED_CANARY"),
    ),
)
def test_generated_table_marker_collisions_fail_closed_and_sanitized(
    source: str, canary: str
) -> None:
    with pytest.raises(DocsMCPError) as caught:
        parse_markdown(source)

    assert_invalid_markdown(caught.value, source, canary)


@pytest.mark.parametrize(
    "source, canary",
    (
        (
            "| H |\n| --- |\n| x |\n⟦TABLE-\\0\\0\\0\\1⟧ "
            "ESCAPED_DIGIT_SYNTHESIS_CANARY",
            "ESCAPED_DIGIT_SYNTHESIS_CANARY",
        ),
        (
            "| H |\n| --- |\n| x |\n⟦TABLE-**0001**⟧ "
            "ORDINARY_FORMAT_SYNTHESIS_CANARY",
            "ORDINARY_FORMAT_SYNTHESIS_CANARY",
        ),
        (
            "| ⟦TABLE-**0001**⟧ | TABLE_CELL_SYNTHESIS_CANARY |\n"
            "| --- | --- |\n"
            "| x | y |",
            "TABLE_CELL_SYNTHESIS_CANARY",
        ),
    ),
    ids=("escaped-digits", "ordinary-formatting", "table-cell-formatting"),
)
def test_post_transform_table_marker_collisions_fail_closed_and_sanitized(
    source: str, canary: str
) -> None:
    assert "⟦TABLE-0001⟧" not in source

    with pytest.raises(DocsMCPError) as caught:
        parse_markdown(source)

    assert_invalid_markdown(caught.value, source, canary)
    assert caught.value.message == "Markdown contains a reserved table marker."


@pytest.mark.parametrize("value", (None, 1, True, b"markdown"))
def test_parser_reuses_public_markdown_type_validation(value: object) -> None:
    with pytest.raises(DocsMCPError) as caught:
        parse_markdown(value)  # type: ignore[arg-type]

    assert_invalid_markdown(caught.value)
    assert caught.value.message == (
        "Markdown must be a string of at most 500000 characters without NUL."
    )


@pytest.mark.parametrize(
    "source, canary",
    (
        ("NUL_CANARY\x00", "NUL_CANARY"),
        ("LENGTH_CANARY" + "x" * 500_001, "LENGTH_CANARY"),
    ),
)
def test_parser_reuses_nul_and_length_validation_without_source_echo(
    source: str, canary: str
) -> None:
    with pytest.raises(DocsMCPError) as caught:
        parse_markdown(source)

    assert_invalid_markdown(caught.value, source, canary)


def test_astral_offsets_stay_codepoint_based_until_utf16_boundary() -> None:
    source = "# شروع 😀\nاین **مهم** است؛ [منبع](https://example.com).\n"

    model = parse_markdown(source)

    assert model.text == "شروع 😀\nاین مهم است؛ منبع.\n"
    assert model.headings == (HeadingRange(0, 6, 1),)
    assert model.bold == (TextRange(11, 14),)
    assert model.links == (LinkRange(20, 24, "https://example.com"),)
    assert (utf16_index(model.text, 11), utf16_index(model.text, 14)) == (12, 15)
    assert (utf16_index(model.text, 20), utf16_index(model.text, 24)) == (21, 25)

    prefixed_heading = parse_markdown("😀\n# H")
    heading = prefixed_heading.headings[0]
    assert heading == HeadingRange(2, 3, 1)
    assert (
        utf16_index(prefixed_heading.text, heading.start),
        utf16_index(prefixed_heading.text, heading.end),
    ) == (3, 4)


def test_process_control_baseexception_propagates_from_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ProcessControl(BaseException):
        pass

    def stop(_value: object) -> str:
        raise ProcessControl

    monkeypatch.setattr(markdown_module, "validate_markdown", stop)

    with pytest.raises(ProcessControl):
        parse_markdown("safe")


def _task7_model() -> DocumentModel:
    return DocumentModel(
        text="😀 H\n😀 B\n😀 L\n",
        headings=(HeadingRange(2, 3, 2),),
        bold=(TextRange(6, 7),),
        links=(LinkRange(10, 11, "https://example.test/source"),),
        tables=(),
    )


def _task7_nested_keys(value: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            keys.add(key)
            keys.update(_task7_nested_keys(nested))
    elif isinstance(value, list):
        for nested in value:
            keys.update(_task7_nested_keys(nested))
    return keys


def _task7_assert_tab_id(value: object, tab_id: str) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in {"range", "location"}:
                assert isinstance(nested, dict)
                assert nested["tabId"] == tab_id
                assert "segmentId" not in nested
                assert "tabsCriteria" not in nested
            _task7_assert_tab_id(nested, tab_id)
    elif isinstance(value, list):
        for nested in value:
            _task7_assert_tab_id(nested, tab_id)


def test_task7_public_helpers_have_exact_positional_or_keyword_signatures() -> None:
    helpers = {
        "replacement_requests": (
            ("model", "end_index", "tab_id", "profile"),
            (inspect.Parameter.empty,) * 3 + ("persian",),
            (DocumentModel, int, str, str),
            list[dict],
        ),
        "inline_style_requests": (
            ("model", "tab_id"),
            (inspect.Parameter.empty,) * 2,
            (DocumentModel, str),
            list[dict],
        ),
        "paragraph_style_request": (
            ("end_index", "tab_id"),
            (inspect.Parameter.empty,) * 2,
            (int, str),
            dict | None,
        ),
        "heading_font_requests": (
            ("model", "tab_id"),
            (inspect.Parameter.empty,) * 2,
            (DocumentModel, str),
            list[dict],
        ),
    }

    for name, (names, defaults, annotations, return_annotation) in helpers.items():
        helper = getattr(markdown_module, name)
        parameters = tuple(inspect.signature(helper).parameters.values())
        assert tuple(parameter.name for parameter in parameters) == names
        assert all(
            parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
            for parameter in parameters
        )
        assert tuple(parameter.default for parameter in parameters) == defaults
        assert tuple(parameter.annotation for parameter in parameters) == annotations
        assert inspect.signature(helper).return_annotation == return_annotation
        assert name in markdown_module.__all__


def test_task7_inline_styles_use_exact_utf16_ranges_and_semantic_order() -> None:
    assert markdown_module.inline_style_requests(_task7_model(), "semantic-tab") == [
        {
            "updateParagraphStyle": {
                "range": {
                    "startIndex": 4,
                    "endIndex": 5,
                    "tabId": "semantic-tab",
                },
                "paragraphStyle": {"namedStyleType": "HEADING_2"},
                "fields": "namedStyleType",
            }
        },
        {
            "updateTextStyle": {
                "range": {
                    "startIndex": 9,
                    "endIndex": 10,
                    "tabId": "semantic-tab",
                },
                "textStyle": {"bold": True},
                "fields": "bold",
            }
        },
        {
            "updateTextStyle": {
                "range": {
                    "startIndex": 14,
                    "endIndex": 15,
                    "tabId": "semantic-tab",
                },
                "textStyle": {"link": {"url": "https://example.test/source"}},
                "fields": "link",
            }
        },
    ]


def test_task7_paragraph_profile_has_four_independent_properties() -> None:
    assert markdown_module.paragraph_style_request(16, "paragraph-tab") == {
        "updateParagraphStyle": {
            "range": {
                "startIndex": 1,
                "endIndex": 16,
                "tabId": "paragraph-tab",
            },
            "paragraphStyle": {
                "direction": "RIGHT_TO_LEFT",
                "alignment": "END",
                "indentStart": {"magnitude": 0, "unit": "PT"},
                "indentEnd": {"magnitude": 0, "unit": "PT"},
            },
            "fields": "direction,alignment,indentStart,indentEnd",
        }
    }
    assert markdown_module.paragraph_style_request(1, "paragraph-tab") is None
    assert markdown_module.paragraph_style_request(0, "paragraph-tab") is None


def test_task7_heading_font_reinforcement_uses_utf16_and_skips_empty_ranges() -> None:
    model = DocumentModel(
        text="😀 H\n",
        headings=(HeadingRange(2, 3, 1), HeadingRange(3, 3, 2)),
        bold=(),
        links=(),
        tables=(),
    )

    assert markdown_module.heading_font_requests(model, "heading-tab") == [
        {
            "updateTextStyle": {
                "range": {
                    "startIndex": 4,
                    "endIndex": 5,
                    "tabId": "heading-tab",
                },
                "textStyle": {
                    "weightedFontFamily": {"fontFamily": "Vazirmatn"},
                    "bold": True,
                },
                "fields": "weightedFontFamily,bold",
            }
        }
    ]


def test_task7_persian_replacement_is_one_exact_ordered_request_plan() -> None:
    model = _task7_model()
    before = _task7_model()

    requests = markdown_module.replacement_requests(model, 50, "t.1")

    assert requests == [
        {
            "deleteContentRange": {
                "range": {"startIndex": 1, "endIndex": 49, "tabId": "t.1"}
            }
        },
        {
            "insertText": {
                "location": {"index": 1, "tabId": "t.1"},
                "text": "😀 H\n😀 B\n😀 L",
            }
        },
        {
            "updateParagraphStyle": {
                "range": {"startIndex": 1, "endIndex": 16, "tabId": "t.1"},
                "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                "fields": "namedStyleType",
            }
        },
        {
            "updateParagraphStyle": {
                "range": {"startIndex": 4, "endIndex": 5, "tabId": "t.1"},
                "paragraphStyle": {"namedStyleType": "HEADING_2"},
                "fields": "namedStyleType",
            }
        },
        {
            "updateTextStyle": {
                "range": {"startIndex": 1, "endIndex": 16, "tabId": "t.1"},
                "textStyle": {"bold": False},
                "fields": "bold,link",
            }
        },
        {
            "updateTextStyle": {
                "range": {"startIndex": 1, "endIndex": 16, "tabId": "t.1"},
                "textStyle": {
                    "weightedFontFamily": {"fontFamily": "Vazirmatn"}
                },
                "fields": "weightedFontFamily",
            }
        },
        {
            "updateParagraphStyle": {
                "range": {"startIndex": 1, "endIndex": 16, "tabId": "t.1"},
                "paragraphStyle": {
                    "direction": "RIGHT_TO_LEFT",
                    "alignment": "END",
                    "indentStart": {"magnitude": 0, "unit": "PT"},
                    "indentEnd": {"magnitude": 0, "unit": "PT"},
                },
                "fields": "direction,alignment,indentStart,indentEnd",
            }
        },
        {
            "updateTextStyle": {
                "range": {"startIndex": 4, "endIndex": 5, "tabId": "t.1"},
                "textStyle": {
                    "weightedFontFamily": {"fontFamily": "Vazirmatn"},
                    "bold": True,
                },
                "fields": "weightedFontFamily,bold",
            }
        },
        {
            "updateTextStyle": {
                "range": {"startIndex": 9, "endIndex": 10, "tabId": "t.1"},
                "textStyle": {"bold": True},
                "fields": "bold",
            }
        },
        {
            "updateTextStyle": {
                "range": {"startIndex": 14, "endIndex": 15, "tabId": "t.1"},
                "textStyle": {"link": {"url": "https://example.test/source"}},
                "fields": "link",
            }
        },
    ]
    assert model == before
    _task7_assert_tab_id(requests, "t.1")


def test_task7_plain_profile_omits_profile_formatting_but_keeps_semantics() -> None:
    requests = markdown_module.replacement_requests(
        _task7_model(), 2, "plain-tab", "plain"
    )

    assert requests == [
        {
            "insertText": {
                "location": {"index": 1, "tabId": "plain-tab"},
                "text": "😀 H\n😀 B\n😀 L",
            }
        },
        {
            "updateParagraphStyle": {
                "range": {"startIndex": 1, "endIndex": 16, "tabId": "plain-tab"},
                "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                "fields": "namedStyleType",
            }
        },
        {
            "updateParagraphStyle": {
                "range": {
                    "startIndex": 4,
                    "endIndex": 5,
                    "tabId": "plain-tab",
                },
                "paragraphStyle": {"namedStyleType": "HEADING_2"},
                "fields": "namedStyleType",
            }
        },
        {
            "updateTextStyle": {
                "range": {"startIndex": 1, "endIndex": 16, "tabId": "plain-tab"},
                "textStyle": {"bold": False},
                "fields": "bold,link",
            }
        },
        {
            "updateTextStyle": {
                "range": {
                    "startIndex": 9,
                    "endIndex": 10,
                    "tabId": "plain-tab",
                },
                "textStyle": {"bold": True},
                "fields": "bold",
            }
        },
        {
            "updateTextStyle": {
                "range": {
                    "startIndex": 14,
                    "endIndex": 15,
                    "tabId": "plain-tab",
                },
                "textStyle": {"link": {"url": "https://example.test/source"}},
                "fields": "link",
            }
        },
    ]
    assert _task7_nested_keys(requests).isdisjoint(
        {
            "direction",
            "alignment",
            "indentStart",
            "indentEnd",
            "weightedFontFamily",
        }
    )
    _task7_assert_tab_id(requests, "plain-tab")


def test_task7_plan_is_flat_deterministic_and_has_no_client_envelope() -> None:
    first = markdown_module.replacement_requests(_task7_model(), 50, "flat-tab")
    second = markdown_module.replacement_requests(_task7_model(), 50, "flat-tab")

    assert first == second
    assert isinstance(first, list)
    assert all(isinstance(request, dict) and len(request) == 1 for request in first)
    assert not any(
        isinstance(nested, list)
        for request in first
        for nested in request.values()
    )
    assert _task7_nested_keys(first).isdisjoint(
        {
            "requests",
            "batchUpdate",
            "writeControl",
            "requiredRevisionId",
            "client",
        }
    )
    _task7_assert_tab_id(first, "flat-tab")


def test_task7_empty_models_and_zero_length_semantic_ranges_are_safe() -> None:
    empty = DocumentModel("", (), (), (), ())
    assert markdown_module.replacement_requests(empty, 2, "empty-tab") == []
    assert markdown_module.replacement_requests(empty, 50, "empty-tab") == [
        {
            "deleteContentRange": {
                "range": {
                    "startIndex": 1,
                    "endIndex": 49,
                    "tabId": "empty-tab",
                }
            }
        }
    ]

    zero_spans = DocumentModel(
        "abc",
        (HeadingRange(1, 1, 1),),
        (TextRange(2, 2),),
        (LinkRange(3, 3, "https://example.test/zero"),),
        (),
    )
    assert markdown_module.inline_style_requests(zero_spans, "zero-tab") == []
    assert markdown_module.heading_font_requests(zero_spans, "zero-tab") == []
    assert markdown_module.replacement_requests(
        zero_spans, 2, "zero-tab", "plain"
    ) == [
        {
            "insertText": {
                "location": {"index": 1, "tabId": "zero-tab"},
                "text": "abc",
            }
        },
        {
            "updateParagraphStyle": {
                "range": {"startIndex": 1, "endIndex": 4, "tabId": "zero-tab"},
                "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                "fields": "namedStyleType",
            }
        },
        {
            "updateTextStyle": {
                "range": {"startIndex": 1, "endIndex": 4, "tabId": "zero-tab"},
                "textStyle": {"bold": False},
                "fields": "bold,link",
            }
        },
    ]


@pytest.mark.parametrize("profile", ("plain", "persian"))
@pytest.mark.parametrize("span_kind", ("heading", "bold", "link"))
def test_task7_empty_text_does_not_bypass_invalid_semantic_spans(
    span_kind: str, profile: str
) -> None:
    heading = (
        (HeadingRange("bad", 1, 1),)  # type: ignore[arg-type]
        if span_kind == "heading"
        else ()
    )
    bold = (
        (TextRange("bad", 1),)  # type: ignore[arg-type]
        if span_kind == "bold"
        else ()
    )
    links = (
        (LinkRange("bad", 1, "https://invalid.example.test"),)  # type: ignore[arg-type]
        if span_kind == "link"
        else ()
    )
    model = DocumentModel("", heading, bold, links, ())

    with pytest.raises(DocsMCPError) as caught:
        markdown_module.replacement_requests(
            model, 2, "EMPTY_INVALID_TAB_CANARY", profile
        )

    assert caught.value.code == "invalid_utf16_offset"
    assert caught.value.message == (
        "Codepoint offset must be an integer within the text bounds."
    )


def test_task7_empty_invalid_model_rejects_cap_before_shared_utf16_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = DocumentModel(
        "", (), (TextRange("bad", 1),), (), ()  # type: ignore[arg-type]
    )
    utf16_calls: list[tuple[str, object]] = []

    def forbidden_utf16_index(text: str, offset: object) -> int:
        utf16_calls.append((text, offset))
        raise AssertionError("UTF-16 validator ran before request-cap rejection")

    monkeypatch.setattr(markdown_module, "_MAX_REPLACEMENT_REQUESTS", 0)
    monkeypatch.setattr(
        markdown_module.client, "utf16_index", forbidden_utf16_index
    )

    with pytest.raises(DocsMCPError) as caught:
        markdown_module.replacement_requests(
            model, 2, "EMPTY_CAP_TAB_CANARY", "plain"
        )

    assert utf16_calls == []
    assert caught.value.message == "Markdown produces too many formatting requests."
    assert_invalid_markdown(
        caught.value,
        "EMPTY_CAP_TAB_CANARY",
        "bad",
    )


@pytest.mark.parametrize("profile", ("PERSIAN", "secret-profile", ""))
def test_task7_unknown_profile_fails_closed_without_echo(profile: str) -> None:
    with pytest.raises(ValueError) as caught:
        markdown_module.replacement_requests(_task7_model(), 2, "tab", profile)

    assert caught.value.args == ("Unsupported replacement profile.",)
    if profile:
        assert profile not in str(caught.value)
        assert profile not in repr(caught.value)


@pytest.mark.parametrize(
    "end_index",
    (None, True, False, "3", 3.5, -1, 0, 1),
    ids=(
        "none",
        "true",
        "false",
        "string",
        "float",
        "negative",
        "zero",
        "one",
    ),
)
def test_task7_invalid_document_end_indexes_fail_closed_without_echo(
    end_index: object,
) -> None:
    with pytest.raises(ValueError) as caught:
        markdown_module.replacement_requests(
            _task7_model(),
            end_index,  # type: ignore[arg-type]
            "END_INDEX_TAB_CANARY",
            "plain",
        )

    assert caught.value.args == ("Invalid document end index.",)
    assert "END_INDEX_TAB_CANARY" not in str(caught.value)


def test_task7_table_marker_text_is_inserted_without_table_orchestration() -> None:
    marker = "⟦TABLE-0001⟧"
    table = TableBlock(marker, ((InlineContent("cell"),),))
    model = DocumentModel(marker + "\n", (), (), (), (table,))

    assert markdown_module.replacement_requests(model, 2, "table-tab", "plain") == [
        {
            "insertText": {
                "location": {"index": 1, "tabId": "table-tab"},
                "text": marker,
            }
        },
        {
            "updateParagraphStyle": {
                "range": {"startIndex": 1, "endIndex": 14, "tabId": "table-tab"},
                "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                "fields": "namedStyleType",
            }
        },
        {
            "updateTextStyle": {
                "range": {"startIndex": 1, "endIndex": 14, "tabId": "table-tab"},
                "textStyle": {"bold": False},
                "fields": "bold,link",
            }
        },
    ]


def _task7_marker_table(marker: object) -> TableBlock:
    return TableBlock(
        marker,  # type: ignore[arg-type]
        ((InlineContent("cell"),),),
    )


@pytest.mark.parametrize(
    ("text", "tables"),
    (
        ("⟦TABLE-0001⟧\n", ()),
        ("", (_task7_marker_table("⟦TABLE-0001⟧"),)),
        (
            "⟦TABLE-0001⟧\n",
            (_task7_marker_table("⟦TABLE-0002⟧"),),
        ),
        (
            "⟦TABLE-0001⟧\n⟦TABLE-0001⟧\n",
            (_task7_marker_table("⟦TABLE-0001⟧"),),
        ),
        (
            "⟦TABLE-0001⟧\n",
            (
                _task7_marker_table("⟦TABLE-0001⟧"),
                _task7_marker_table("⟦TABLE-0001⟧"),
            ),
        ),
        (
            "⟦TABLE-0002⟧\n⟦TABLE-0001⟧\n",
            (
                _task7_marker_table("⟦TABLE-0001⟧"),
                _task7_marker_table("⟦TABLE-0002⟧"),
            ),
        ),
        ("", (_task7_marker_table("CANARY_INVALID_TABLE_MARKER"),)),
        ("", (_task7_marker_table(None),)),
    ),
    ids=(
        "orphan-text-marker",
        "missing-text-marker",
        "mismatched-marker",
        "duplicate-text-marker",
        "duplicate-table-marker",
        "out-of-order-markers",
        "invalid-table-marker-syntax",
        "non-string-table-marker",
    ),
)
def test_task7_inconsistent_table_marker_state_fails_closed_without_echo(
    text: str, tables: tuple[TableBlock, ...]
) -> None:
    model = DocumentModel(text, (), (), (), tables)

    with pytest.raises(DocsMCPError) as caught:
        markdown_module.replacement_requests(
            model, 2, "TABLE_STATE_TAB_CANARY", "plain"
        )

    assert caught.value.message == "Markdown table marker state is invalid."
    assert_invalid_markdown(
        caught.value,
        "TABLE_STATE_TAB_CANARY",
        "CANARY_INVALID_TABLE_MARKER",
    )


def test_task7_multiple_matching_table_markers_remain_a_flat_no_table_plan() -> None:
    first = "⟦TABLE-0001⟧"
    second = "⟦TABLE-0002⟧"
    text = f"{first}\nbody\n{second}\n"
    model = DocumentModel(
        text,
        (),
        (),
        (),
        (_task7_marker_table(first), _task7_marker_table(second)),
    )

    assert markdown_module.replacement_requests(
        model, 2, "matching-table-tab", "plain"
    ) == [
        {
            "insertText": {
                "location": {"index": 1, "tabId": "matching-table-tab"},
                "text": text[:-1],
            }
        },
        {
            "updateParagraphStyle": {
                "range": {"startIndex": 1, "endIndex": 32, "tabId": "matching-table-tab"},
                "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                "fields": "namedStyleType",
            }
        },
        {
            "updateTextStyle": {
                "range": {"startIndex": 1, "endIndex": 32, "tabId": "matching-table-tab"},
                "textStyle": {"bold": False},
                "fields": "bold,link",
            }
        },
    ]


def _task7_codepoint_at_utf16(text: str, target: int) -> int:
    units = 0
    for index, character in enumerate(text):
        if units == target:
            return index
        units += len(character.encode("utf-16-le")) // 2
    if units == target:
        return len(text)
    raise AssertionError(f"UTF-16 offset {target} splits a code point")


def _task7_apply_text_operations(body: str, requests: list[dict]) -> str:
    assert body.endswith("\n")
    for request in requests:
        if "deleteContentRange" in request:
            text_range = request["deleteContentRange"]["range"]
            start = _task7_codepoint_at_utf16(body, text_range["startIndex"] - 1)
            end = _task7_codepoint_at_utf16(body, text_range["endIndex"] - 1)
            body = body[:start] + body[end:]
        elif "insertText" in request:
            insertion = request["insertText"]
            index = _task7_codepoint_at_utf16(
                body, insertion["location"]["index"] - 1
            )
            body = body[:index] + insertion["text"] + body[index:]
        assert body.endswith("\n")
    return body


@pytest.mark.parametrize(
    "source",
    ("x", "\n"),
    ids=("visible-text", "structural-newline-only"),
)
def test_task7_replacement_reuses_docs_terminal_newline_exactly(source: str) -> None:
    existing_body = "old 😀 body\n"
    model = parse_markdown(source)

    requests = markdown_module.replacement_requests(
        model,
        1 + utf16_length(existing_body),
        "terminal-newline-tab",
        "plain",
    )
    replaced_body = _task7_apply_text_operations(existing_body, requests)

    assert replaced_body == model.text
    expected_insert = model.text[:-1]
    assert [
        request["insertText"]["text"]
        for request in requests
        if "insertText" in request
    ] == ([expected_insert] if expected_insert else [])


def test_task7_astral_heading_styles_are_bounded_by_replaced_body() -> None:
    existing_body = "قدیمی 😀\n"
    model = parse_markdown("# 😀 سرآغاز")

    requests = markdown_module.replacement_requests(
        model, 1 + utf16_length(existing_body), "astral-heading-tab"
    )
    replaced_body = _task7_apply_text_operations(existing_body, requests)
    content_end = 1 + utf16_length(replaced_body)
    style_ranges = [
        operation["range"]
        for request in requests
        for name, operation in request.items()
        if name in {"updateParagraphStyle", "updateTextStyle"}
    ]

    assert replaced_body == model.text
    assert style_ranges
    assert all(
        1 <= text_range["startIndex"] < text_range["endIndex"] <= content_end
        for text_range in style_ranges
    )
    global_range = {
        "startIndex": 1,
        "endIndex": content_end,
        "tabId": "astral-heading-tab",
    }
    assert [
        request
        for request in requests
        for name, operation in request.items()
        if name in {"updateParagraphStyle", "updateTextStyle"}
        and operation["range"] == global_range
    ] == [
        {
            "updateParagraphStyle": {
                "range": global_range,
                "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                "fields": "namedStyleType",
            }
        },
        {
            "updateTextStyle": {
                "range": global_range,
                "textStyle": {"bold": False},
                "fields": "bold,link",
            }
        },
        {
            "updateTextStyle": {
                "range": global_range,
                "textStyle": {"weightedFontFamily": {"fontFamily": "Vazirmatn"}},
                "fields": "weightedFontFamily",
            }
        },
        {
            "updateParagraphStyle": {
                "range": global_range,
                "paragraphStyle": {
                    "direction": "RIGHT_TO_LEFT",
                    "alignment": "END",
                    "indentStart": {"magnitude": 0, "unit": "PT"},
                    "indentEnd": {"magnitude": 0, "unit": "PT"},
                },
                "fields": "direction,alignment,indentStart,indentEnd",
            }
        },
    ]
    assert max(text_range["endIndex"] for text_range in style_ranges) == content_end


def test_task7_manual_non_newline_text_preserves_its_last_character() -> None:
    model = DocumentModel("manual-last😀", (), (), (), ())

    requests = markdown_module.replacement_requests(
        model, 2, "manual-no-newline-tab", "plain"
    )

    assert requests == [
        {
            "insertText": {
                "location": {"index": 1, "tabId": "manual-no-newline-tab"},
                "text": model.text,
            }
        },
        {
            "updateParagraphStyle": {
                "range": {"startIndex": 1, "endIndex": 14, "tabId": "manual-no-newline-tab"},
                "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                "fields": "namedStyleType",
            }
        },
        {
            "updateTextStyle": {
                "range": {"startIndex": 1, "endIndex": 14, "tabId": "manual-no-newline-tab"},
                "textStyle": {"bold": False},
                "fields": "bold,link",
            }
        },
    ]
    assert _task7_apply_text_operations("\n", requests) == model.text + "\n"


def _task7_expected_utf16_range(
    text: str, span: TextRange, tab_id: str
) -> dict[str, object]:
    return {
        "startIndex": 1 + len(text[: span.start].encode("utf-16-le")) // 2,
        "endIndex": 1 + len(text[: span.end].encode("utf-16-le")) // 2,
        "tabId": tab_id,
    }


def test_task7_many_heading_plans_do_not_reencode_prefix_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "\n".join(f"# 😀 heading-{index:04d}" for index in range(256))
    model = parse_markdown(source)
    prefix_windows: list[int] = []
    original_utf16_index = markdown_module.client.utf16_index

    def counted_utf16_index(text: str, codepoint_offset: object) -> int:
        if isinstance(codepoint_offset, int) and not isinstance(codepoint_offset, bool):
            prefix_windows.append(codepoint_offset)
        return original_utf16_index(text, codepoint_offset)

    monkeypatch.setattr(markdown_module.client, "utf16_index", counted_utf16_index)

    inline_requests = markdown_module.inline_style_requests(model, "many-heading-tab")
    font_requests = markdown_module.heading_font_requests(model, "many-heading-tab")

    assert len(inline_requests) == len(model.headings) == 256
    assert len(font_requests) == len(model.headings)
    for request, heading in (
        (inline_requests[0], model.headings[0]),
        (inline_requests[-1], model.headings[-1]),
        (font_requests[0], model.headings[0]),
        (font_requests[-1], model.headings[-1]),
    ):
        operation = next(iter(request.values()))
        assert operation["range"] == _task7_expected_utf16_range(
            model.text, heading, "many-heading-tab"
        )

    assert (len(prefix_windows), sum(prefix_windows)) == (0, 0)


def test_task7_astral_prefix_overlapping_bold_and_link_share_exact_utf16_range() -> None:
    model = parse_markdown("😀 **[x](https://overlap.example.test)**")

    requests = markdown_module.inline_style_requests(model, "overlap-tab")

    assert model.bold == (TextRange(2, 3),)
    assert model.links == (
        LinkRange(2, 3, "https://overlap.example.test"),
    )
    assert [
        request["updateTextStyle"]["range"] for request in requests
    ] == [
        {"startIndex": 4, "endIndex": 5, "tabId": "overlap-tab"},
        {"startIndex": 4, "endIndex": 5, "tabId": "overlap-tab"},
    ]


@pytest.mark.parametrize(
    "span",
    (
        TextRange(False, 1),
        TextRange(0, True),
        TextRange(-1, 1),
        TextRange(0, 3),
        TextRange(0.5, 1),  # type: ignore[arg-type]
        TextRange("bad", 1),  # type: ignore[arg-type]
        TextRange(0, None),  # type: ignore[arg-type]
    ),
    ids=(
        "bool-start",
        "bool-end",
        "negative",
        "past-end",
        "comparable-non-integer",
        "incomparable-string-start",
        "incomparable-none-end",
    ),
)
@pytest.mark.parametrize(
    "request_path",
    ("inline-heading", "inline-bold", "inline-link", "heading-font"),
)
def test_task7_linear_maps_preserve_utf16_offset_validation(
    request_path: str, span: TextRange
) -> None:
    heading = HeadingRange(span.start, span.end, 1)
    if request_path == "inline-heading":
        model = DocumentModel("ab", (heading,), (), (), ())
        helper = markdown_module.inline_style_requests
    elif request_path == "inline-bold":
        model = DocumentModel("ab", (), (span,), (), ())
        helper = markdown_module.inline_style_requests
    elif request_path == "inline-link":
        model = DocumentModel(
            "ab",
            (),
            (),
            (LinkRange(span.start, span.end, "https://invalid.example.test"),),
            (),
        )
        helper = markdown_module.inline_style_requests
    else:
        model = DocumentModel("ab", (heading,), (), (), ())
        helper = markdown_module.heading_font_requests

    with pytest.raises(DocsMCPError) as caught:
        helper(model, "invalid-offset-tab")

    assert caught.value.code == "invalid_utf16_offset"
    assert caught.value.message == (
        "Codepoint offset must be an integer within the text bounds."
    )


@pytest.mark.parametrize(
    ("span", "expected_offset"),
    (
        (TextRange("bad", 1), "bad"),  # type: ignore[arg-type]
        (TextRange(0, None), None),  # type: ignore[arg-type]
    ),
    ids=("string-start", "none-end"),
)
@pytest.mark.parametrize(
    "request_path",
    ("inline-heading", "inline-bold", "inline-link", "heading-font"),
)
def test_task7_incomparable_offsets_reach_shared_utf16_validator(
    request_path: str,
    span: TextRange,
    expected_offset: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []
    original = markdown_module.client.utf16_index

    def recording_utf16_index(text: str, offset: object) -> int:
        calls.append((text, offset))
        return original(text, offset)

    monkeypatch.setattr(
        markdown_module.client, "utf16_index", recording_utf16_index
    )
    heading = HeadingRange(span.start, span.end, 1)
    if request_path == "inline-heading":
        model = DocumentModel("ab", (heading,), (), (), ())
        helper = markdown_module.inline_style_requests
    elif request_path == "inline-bold":
        model = DocumentModel("ab", (), (span,), (), ())
        helper = markdown_module.inline_style_requests
    elif request_path == "inline-link":
        model = DocumentModel(
            "ab",
            (),
            (),
            (LinkRange(span.start, span.end, "https://invalid.example.test"),),
            (),
        )
        helper = markdown_module.inline_style_requests
    else:
        model = DocumentModel("ab", (heading,), (), (), ())
        helper = markdown_module.heading_font_requests

    with pytest.raises(DocsMCPError) as caught:
        helper(model, "shared-validator-tab")

    assert calls == [("ab", expected_offset)]
    assert caught.value.code == "invalid_utf16_offset"
    assert caught.value.message == (
        "Codepoint offset must be an integer within the text bounds."
    )


@pytest.mark.parametrize("helper_name", ("inline_style_requests", "heading_font_requests"))
def test_task7_linear_maps_preserve_invalid_unicode_failure(helper_name: str) -> None:
    if helper_name == "inline_style_requests":
        model = DocumentModel("\ud800x", (), (TextRange(0, 1),), (), ())
    else:
        model = DocumentModel(
            "\ud800x", (HeadingRange(0, 1, 1),), (), (), ()
        )

    with pytest.raises(UnicodeEncodeError):
        getattr(markdown_module, helper_name)(model, "invalid-unicode-tab")


def _task7_high_complexity_model() -> DocumentModel:
    canary = "REQUEST_SOURCE_CANARY_"
    text = canary + "x" * 10_001
    first = len(canary)
    headings = tuple(
        HeadingRange(first + index, first + index + 1, 1)
        for index in range(10_001)
    )
    return DocumentModel(text, headings, (), (), ())


def test_task7_replacement_limits_have_exact_contract_values() -> None:
    assert markdown_module._MAX_REPLACEMENT_REQUESTS == 10_000
    assert markdown_module._MAX_REPLACEMENT_PAYLOAD_BYTES == 8_000_000


@pytest.mark.parametrize("profile", ("plain", "persian"))
def test_task7_high_complexity_replacement_rejects_before_plan_construction(
    profile: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _task7_high_complexity_model()
    attempted_helpers: list[str] = []

    def forbidden_inline(*_args: object) -> list[dict]:
        attempted_helpers.append("inline")
        raise AssertionError("formatting helper called before overflow rejection")

    def forbidden_heading(*_args: object) -> list[dict]:
        attempted_helpers.append("heading")
        raise AssertionError("formatting helper called before overflow rejection")

    monkeypatch.setattr(markdown_module, "inline_style_requests", forbidden_inline)
    monkeypatch.setattr(markdown_module, "heading_font_requests", forbidden_heading)

    with pytest.raises(DocsMCPError) as caught:
        markdown_module.replacement_requests(
            model, 50, "REQUEST_TAB_CANARY", profile
        )

    assert attempted_helpers == []
    assert caught.value.message == "Markdown produces too many formatting requests."
    assert_invalid_markdown(
        caught.value,
        "REQUEST_SOURCE_CANARY",
        "REQUEST_TAB_CANARY",
        profile,
    )


@pytest.mark.parametrize(
    "helper_name", ("inline_style_requests", "heading_font_requests")
)
def test_task7_standalone_high_complexity_plans_reject_before_utf16_work(
    helper_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _task7_high_complexity_model()
    utf16_calls = 0

    def cheap_utf16_index(_text: str, codepoint_offset: object) -> int:
        nonlocal utf16_calls
        utf16_calls += 1
        assert isinstance(codepoint_offset, int)
        return codepoint_offset

    monkeypatch.setattr(markdown_module.client, "utf16_index", cheap_utf16_index)
    helper = getattr(markdown_module, helper_name)

    try:
        requests = helper(model, "STANDALONE_TAB_CANARY")
    except DocsMCPError as error:
        caught = error
    else:
        pytest.fail(
            f"{helper_name} constructed {len(requests)} requests after "
            f"{utf16_calls} UTF-16 prefix conversions"
        )

    assert utf16_calls == 0
    assert caught.message == "Markdown produces too many formatting requests."
    assert_invalid_markdown(
        caught,
        "REQUEST_SOURCE_CANARY",
        "STANDALONE_TAB_CANARY",
    )


@pytest.mark.parametrize(
    "profile, exact_count",
    (("plain", 7), ("persian", 10)),
)
def test_task7_replacement_request_cap_counts_every_eventual_operation(
    profile: str, exact_count: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        markdown_module, "_MAX_REPLACEMENT_REQUESTS", exact_count, raising=False
    )
    accepted = markdown_module.replacement_requests(
        _task7_model(), 50, "COUNT_TAB_CANARY", profile
    )
    assert len(accepted) == exact_count

    monkeypatch.setattr(
        markdown_module, "_MAX_REPLACEMENT_REQUESTS", exact_count - 1
    )
    with pytest.raises(DocsMCPError) as caught:
        markdown_module.replacement_requests(
            _task7_model(), 50, "COUNT_TAB_CANARY", profile
        )

    assert caught.value.message == "Markdown produces too many formatting requests."
    assert_invalid_markdown(
        caught.value,
        "https://example.test/source",
        "COUNT_TAB_CANARY",
        profile,
    )


def _task7_payload_builder(case: str):
    tab_id = "PAYLOAD_TAB_CANARY"
    if case == "inline":
        model = DocumentModel(
            "PAYLOAD_SOURCE_CANARY 😀\n", (), (TextRange(0, 1),), (), ()
        )
        return lambda: markdown_module.inline_style_requests(model, tab_id)
    if case == "heading-font":
        model = DocumentModel(
            "PAYLOAD_SOURCE_CANARY 😀\n",
            (HeadingRange(0, 1, 1),),
            (),
            (),
            (),
        )
        return lambda: markdown_module.heading_font_requests(model, tab_id)

    model = DocumentModel("PAYLOAD_SOURCE_CANARY 😀", (), (), (), ())
    profile = "plain" if case == "replacement-plain" else "persian"
    return lambda: markdown_module.replacement_requests(model, 50, tab_id, profile)


@pytest.mark.parametrize(
    "case",
    ("inline", "heading-font", "replacement-plain", "replacement-persian"),
)
def test_task7_exported_lists_enforce_exact_compact_json_payload_budget(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        markdown_module,
        "_MAX_REPLACEMENT_PAYLOAD_BYTES",
        8_000_000,
        raising=False,
    )
    build = _task7_payload_builder(case)
    expected = build()
    payload_bytes = len(
        json.dumps(expected, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    )

    monkeypatch.setattr(
        markdown_module, "_MAX_REPLACEMENT_PAYLOAD_BYTES", payload_bytes
    )
    assert build() == expected

    monkeypatch.setattr(
        markdown_module, "_MAX_REPLACEMENT_PAYLOAD_BYTES", payload_bytes - 1
    )
    with pytest.raises(DocsMCPError) as caught:
        build()

    assert caught.value.message == "Markdown produces too many formatting requests."
    assert_invalid_markdown(
        caught.value,
        "PAYLOAD_SOURCE_CANARY",
        "PAYLOAD_TAB_CANARY",
    )


def _task8_table_node(
    starts: tuple[tuple[int, ...], ...], tab_id: str
) -> dict:
    return {
        "tabId": tab_id,
        "table": {
            "tableRows": [
                {
                    "tableCells": [
                        {
                            "content": [
                                {
                                    "startIndex": start,
                                    "endIndex": start + 1,
                                    "paragraph": {"elements": []},
                                }
                            ]
                        }
                        for start in row
                    ]
                }
                for row in starts
            ]
        },
    }


def test_task8_table_request_helpers_have_exact_signatures_and_exports() -> None:
    helpers = {
        "insert_table_structure_requests": (
            ("model", "tab_id"),
            (DocumentModel, str),
        ),
        "table_cell_insert_requests": (
            ("table_node", "rows"),
            (dict, tuple[tuple[InlineContent, ...], ...]),
        ),
        "table_cell_style_requests": (
            ("table_node", "rows", "profile"),
            (dict, tuple[tuple[InlineContent, ...], ...], str),
        ),
    }

    for name, (names, annotations) in helpers.items():
        helper = getattr(markdown_module, name)
        parameters = tuple(inspect.signature(helper).parameters.values())
        assert tuple(parameter.name for parameter in parameters) == names
        assert all(
            parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
            and parameter.default == (
                "plain" if parameter.name == "profile" else inspect.Parameter.empty
            )
            for parameter in parameters
        )
        assert tuple(parameter.annotation for parameter in parameters) == annotations
        assert inspect.signature(helper).return_annotation == list[dict]
        assert name in markdown_module.__all__


def test_task8_table_structures_delete_markers_then_insert_highest_index_first() -> None:
    first_marker = "⟦TABLE-0001⟧"
    second_marker = "⟦TABLE-0002⟧"
    text = f"😀 before\n{first_marker}\nbetween\n{second_marker}\n"
    first = TableBlock(
        first_marker,
        (
            (InlineContent("A"), InlineContent("B")),
            (InlineContent("C"),),
        ),
    )
    second = TableBlock(second_marker, ((InlineContent("D"),),))
    model = DocumentModel(text, (), (), (), (first, second))
    first_index = 1 + utf16_index(text, text.index(first_marker))
    second_index = 1 + utf16_index(text, text.index(second_marker))

    assert markdown_module.insert_table_structure_requests(
        model, "table-structure-tab"
    ) == [
        {
            "deleteContentRange": {
                "range": {
                    "startIndex": second_index,
                    "endIndex": second_index + utf16_length(second_marker),
                    "tabId": "table-structure-tab",
                }
            }
        },
        {
            "insertTable": {
                "rows": 1,
                "columns": 1,
                "location": {
                    "index": second_index,
                    "tabId": "table-structure-tab",
                },
            }
        },
        {
            "deleteContentRange": {
                "range": {
                    "startIndex": first_index,
                    "endIndex": first_index + utf16_length(first_marker),
                    "tabId": "table-structure-tab",
                }
            }
        },
        {
            "insertTable": {
                "rows": 2,
                "columns": 2,
                "location": {
                    "index": first_index,
                    "tabId": "table-structure-tab",
                },
            }
        },
    ]


def test_task8_table_cell_insertions_use_actual_indexes_in_reverse_order() -> None:
    rows = (
        (InlineContent("r1c1"), InlineContent("😀 r1c2")),
        (InlineContent(""), InlineContent("r2c2")),
    )
    table_node = _task8_table_node(((10, 20), (30, 40)), "cell-insert-tab")

    assert markdown_module.table_cell_insert_requests(table_node, rows) == [
        {
            "insertText": {
                "location": {"index": 40, "tabId": "cell-insert-tab"},
                "text": "r2c2",
            }
        },
        {
            "insertText": {
                "location": {"index": 20, "tabId": "cell-insert-tab"},
                "text": "😀 r1c2",
            }
        },
        {
            "insertText": {
                "location": {"index": 10, "tabId": "cell-insert-tab"},
                "text": "r1c1",
            }
        },
    ]


def test_task8_table_cell_styles_use_actual_indexes_and_local_utf16_ranges() -> None:
    text = "😀 xy"
    cell = InlineContent(
        text,
        bold=(TextRange(2, 4),),
        links=(LinkRange(0, 1, "https://cell.example.test"),),
    )
    rows = ((cell,),)
    table_node = _task8_table_node(((101,),), "cell-style-tab")

    assert markdown_module.table_cell_style_requests(table_node, rows) == [
        {
            "updateTextStyle": {
                "range": {
                    "startIndex": 101 + utf16_index(text, 2),
                    "endIndex": 101 + utf16_index(text, 4),
                    "tabId": "cell-style-tab",
                },
                "textStyle": {"bold": True},
                "fields": "bold",
            }
        },
        {
            "updateTextStyle": {
                "range": {
                    "startIndex": 101 + utf16_index(text, 0),
                    "endIndex": 101 + utf16_index(text, 1),
                    "tabId": "cell-style-tab",
                },
                "textStyle": {"link": {"url": "https://cell.example.test"}},
                "fields": "link",
            }
        },
    ]


def assert_semantic_verification_failed(
    error: DocsMCPError, *canaries: str
) -> None:
    assert error.code == "verification_failed"
    assert len(error.message) <= 200
    public_graph = "\n".join(
        (
            str(error),
            repr(error),
            repr(error.args),
            repr(vars(error)),
            repr(error.as_result()),
        )
    )
    for canary in canaries:
        assert canary not in public_graph
    assert error.__context__ is None
    assert error.__cause__ is None


def _task9_text_run(
    content: str, *, bold: bool = False, link: str | None = None
) -> dict:
    style: dict[str, object] = {}
    if bold:
        style["bold"] = True
    if link is not None:
        style["link"] = {"url": link}
    return {"textRun": {"content": content, "textStyle": style}}


def _task9_paragraph(*runs: dict, style: str = "NORMAL_TEXT") -> dict:
    return {
        "paragraph": {
            "paragraphStyle": {"namedStyleType": style},
            "elements": list(runs),
        }
    }


def _task9_cell(*runs: dict) -> dict:
    return {"content": [_task9_paragraph(*runs)]}


@pytest.mark.parametrize("profile", ["persian", "plain"])
def test_task14_heading_semantics_are_profile_aware_without_changing_raw_model(profile: str) -> None:
    model = parse_markdown("## عنوان **درشت** [پیوند](https://example.test) 😀\n\nبدنه **پررنگ**")
    raw = markdown_module.candidate_semantic(model)
    result = markdown_module.candidate_semantic(model, profile=profile)
    assert result["blocks"][1:] == raw["blocks"][1:]
    heading = result["blocks"][0]
    assert heading["heading"] == 2
    assert any(run["link"] == "https://example.test" for run in heading["runs"])
    if profile == "persian":
        assert all(run["bold"] for run in heading["runs"])
        assert any(not run["bold"] for run in raw["blocks"][0]["runs"])
    else:
        assert result == raw
    assert markdown_module.candidate_semantic(model) == raw


def test_task14_font_reset_cannot_erase_final_inline_or_heading_bold() -> None:
    model = parse_markdown("## عنوان 😀\n\nبدنه **پررنگ** [پیوند](https://example.test)")
    requests = markdown_module.replacement_requests(model, 2, "t.render")
    # Model only the observed non-commuting effects: font resets bold;
    # named styles reset paragraph/font. No speculative inherited-style model.
    styles = [{} for _ in range(1 + utf16_length(model.text))]
    for request in requests:
        kind, update = next(iter(request.items()))
        if kind not in {"updateTextStyle", "updateParagraphStyle"}:
            continue
        start, end = update["range"]["startIndex"], update["range"]["endIndex"]
        for index in range(start, end):
            if kind == "updateParagraphStyle":
                if "namedStyleType" in update["paragraphStyle"]:
                    styles[index].clear()
                styles[index].update(update["paragraphStyle"])
            else:
                if "weightedFontFamily" in update["textStyle"]:
                    styles[index]["bold"] = False
                styles[index].update(update["textStyle"])
    for span in (*model.headings, *model.bold):
        for index in range(1 + utf16_index(model.text, span.start), 1 + utf16_index(model.text, span.end)):
            assert styles[index]["bold"] is True
    for index in range(1, len(styles)):
        assert styles[index]["weightedFontFamily"] == {"fontFamily": "Vazirmatn"}
        assert styles[index]["direction"] == "RIGHT_TO_LEFT"
        assert styles[index]["alignment"] == "END"


@pytest.mark.parametrize("profile", ["persian", "plain"])
def test_task14_cell_profile_covers_empty_padded_and_utf16_text_before_emphasis(profile: str) -> None:
    rows = ((InlineContent("😀 x", bold=(TextRange(2, 3),), links=(LinkRange(0, 1, "https://example.test"),)), InlineContent("")), (InlineContent("متن"),))
    node = _task8_table_node(((101, 111), (121, 131)), "t.cells")
    requests = markdown_module.table_cell_style_requests(node, rows, profile=profile)
    if profile == "plain":
        assert requests == markdown_module.table_cell_style_requests(node, rows)
        assert [r["updateTextStyle"]["fields"] for r in requests] == ["bold", "link"]
        return
    assert len(requests) == 10
    for start, length in [(101, 5), (111, 1), (121, 4), (131, 1)]:
        expected_range = {"startIndex": start, "endIndex": start + length, "tabId": "t.cells"}
        paragraph = next(r["updateParagraphStyle"] for r in requests if r.get("updateParagraphStyle", {}).get("range") == expected_range)
        assert paragraph["paragraphStyle"] == {
            "direction": "RIGHT_TO_LEFT", "alignment": "END",
            "indentStart": {"magnitude": 0, "unit": "PT"},
            "indentEnd": {"magnitude": 0, "unit": "PT"},
        }
        assert paragraph["fields"] == "direction,alignment,indentStart,indentEnd"
        font_index = next(i for i, r in enumerate(requests) if r.get("updateTextStyle", {}).get("range") == expected_range)
        assert requests[font_index]["updateTextStyle"]["textStyle"] == {"weightedFontFamily": {"fontFamily": "Vazirmatn"}}
        assert requests[font_index]["updateTextStyle"]["fields"] == "weightedFontFamily"
        for i, request in enumerate(requests):
            update = request.get("updateTextStyle", {})
            if update.get("fields") in {"bold", "link"}:
                assert i > font_index if start == 101 else True
    bold = next(r["updateTextStyle"] for r in requests if r.get("updateTextStyle", {}).get("fields") == "bold")
    assert bold["range"] == {"startIndex": 104, "endIndex": 105, "tabId": "t.cells"}


@pytest.mark.parametrize("profile", ["plain", "persian"])
@pytest.mark.parametrize("source", ["Replacement\n", "## Heading\n\nNew **bold**"])
def test_task14_replacement_clears_inherited_heading_and_emphasis(profile, source):
    model = parse_markdown(source)
    requests = markdown_module.replacement_requests(model, 20, "t.reset", profile)
    styles = [
        {"namedStyleType": "HEADING_1", "bold": True, "link": {"url": "https://old.example"}}
        for _ in range(1 + utf16_length(model.text))
    ]
    for request in requests:
        for kind in ("updateParagraphStyle", "updateTextStyle"):
            if kind not in request:
                continue
            update = request[kind]
            values = update["paragraphStyle" if kind == "updateParagraphStyle" else "textStyle"]
            for index in range(update["range"]["startIndex"], update["range"]["endIndex"]):
                if "weightedFontFamily" in values:
                    styles[index]["bold"] = False
                for field in update["fields"].split(","):
                    styles[index].pop(field, None)
                styles[index].update(values)
    heading_indexes = {i for h in model.headings for i in range(1 + utf16_index(model.text, h.start), 1 + utf16_index(model.text, h.end))}
    bold_indexes = {i for b in model.bold for i in range(1 + utf16_index(model.text, b.start), 1 + utf16_index(model.text, b.end))}
    for index, value in enumerate(styles[1:], 1):
        if index not in heading_indexes:
            assert value["namedStyleType"] == "NORMAL_TEXT"
        assert value.get("bold", False) == (index in bold_indexes or profile == "persian" and index in heading_indexes)
        assert value.get("link") is None


def test_task9_semantic_helpers_have_exact_signatures_and_exports() -> None:
    helpers = {
        "candidate_semantic": (("model", "profile"), (DocumentModel, str), dict),
        "remote_semantic": (("body",), (dict,), dict),
        "semantic_sha256": (("value",), (dict,), str),
    }

    for name, (names, annotations, return_annotation) in helpers.items():
        helper = getattr(markdown_module, name)
        parameters = tuple(inspect.signature(helper).parameters.values())
        assert tuple(parameter.name for parameter in parameters) == names
        assert all(
            parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
            and parameter.default == (
                "plain" if parameter.name == "profile" else inspect.Parameter.empty
            )
            for parameter in parameters
        )
        assert tuple(parameter.annotation for parameter in parameters) == annotations
        assert inspect.signature(helper).return_annotation is return_annotation
        assert name in markdown_module.__all__


def test_task9_candidate_and_remote_semantics_match_exact_rich_structure() -> None:
    model = parse_markdown(
        "# عنوان\n"
        "این **مهم** و [منبع](https://source.example.test) است.\n"
        "\n"
        "| **A** | [B](https://table.example.test) |\n"
        "|---|---|\n"
        "| C |\n"
    )
    body = {
        "content": [
            {"sectionBreak": {"sectionStyle": {}}},
            _task9_paragraph(_task9_text_run("عنوان\n"), style="HEADING_1"),
            _task9_paragraph(
                _task9_text_run("ای"),
                _task9_text_run("ن "),
                _task9_text_run("مهم", bold=True),
                _task9_text_run(" و "),
                _task9_text_run(
                    "منبع", link="https://source.example.test"
                ),
                _task9_text_run(" است.\n"),
            ),
            _task9_paragraph(_task9_text_run("\n")),
            {
                "table": {
                    "tableRows": [
                        {
                            "tableCells": [
                                _task9_cell(_task9_text_run("A\n", bold=True)),
                                _task9_cell(
                                    _task9_text_run(
                                        "B\n",
                                        link="https://table.example.test",
                                    )
                                ),
                            ]
                        },
                        {
                            "tableCells": [
                                _task9_cell(_task9_text_run("C\n")),
                                _task9_cell(_task9_text_run("\n")),
                            ]
                        },
                    ]
                }
            },
        ]
    }
    expected = {
        "schema": 2,
        "blocks": [
            {
                "type": "paragraph",
                "heading": 1,
                "runs": [{"text": "عنوان", "bold": False, "link": None}],
            },
            {
                "type": "paragraph",
                "heading": None,
                "runs": [
                    {"text": "این ", "bold": False, "link": None},
                    {"text": "مهم", "bold": True, "link": None},
                    {"text": " و ", "bold": False, "link": None},
                    {
                        "text": "منبع",
                        "bold": False,
                        "link": "https://source.example.test",
                    },
                    {"text": " است.", "bold": False, "link": None},
                ],
            },
            {
                "type": "table",
                "rows": [
                    [
                        {"runs": [{"text": "A", "bold": True, "link": None}]},
                        {
                            "runs": [
                                {
                                    "text": "B",
                                    "bold": False,
                                    "link": "https://table.example.test",
                                }
                            ]
                        },
                    ],
                    [
                        {"runs": [{"text": "C", "bold": False, "link": None}]},
                        {"runs": []},
                    ],
                ],
            },
        ],
    }

    assert markdown_module.candidate_semantic(model) == expected
    assert markdown_module.remote_semantic(body) == expected


def test_task9_remote_unsupported_elements_use_noncolliding_bounded_markers() -> None:
    paragraph_canary = "PARAGRAPH_KIND_CANARY_" + ("x" * 500)
    structural_canary = "STRUCTURAL_KIND_CANARY_" + ("y" * 500)
    cell_canary = "CELL_KIND_CANARY_" + ("z" * 500)
    body = {
        "content": [
            _task9_paragraph({paragraph_canary: {}}),
            {"startIndex": 2, structural_canary: {}},
            {
                "table": {
                    "tableRows": [
                        {
                            "tableCells": [
                                {"content": [{cell_canary: {}}]},
                            ]
                        }
                    ]
                }
            },
        ]
    }

    semantic = markdown_module.remote_semantic(body)

    assert semantic == {
        "schema": 2,
        "blocks": [
            {
                "type": "paragraph",
                "heading": None,
                "runs": [{"unsupported": "paragraph:unknown"}],
            },
            {"type": "unsupported", "marker": "structural:unknown"},
            {
                "type": "table",
                "rows": [
                    [
                        {"runs": [{"unsupported": "cell:unknown"}]},
                    ]
                ],
            },
        ],
    }
    public_value = json.dumps(semantic, ensure_ascii=False, sort_keys=True)
    assert paragraph_canary not in public_value
    assert structural_canary not in public_value
    assert cell_canary not in public_value
    assert markdown_module.candidate_semantic(
        parse_markdown("⟦UNSUPPORTED:paragraph:unknown⟧\n")
    ) != semantic


def test_task9_semantic_sha_is_compact_sorted_utf8_and_order_independent() -> None:
    value = {
        "schema": 2,
        "blocks": [
            {
                "type": "paragraph",
                "heading": None,
                "runs": [
                    {"text": "سلام 😀", "bold": False, "link": None}
                ],
            }
        ],
    }
    reordered = {"blocks": value["blocks"], "schema": value["schema"]}

    assert markdown_module.semantic_sha256(value) == (
        "fee0fbc7e4b45e60e31cb43e803b57080fe2416e51788556f83adb6f694e6d5e"
    )
    assert markdown_module.semantic_sha256(reordered) == (
        "fee0fbc7e4b45e60e31cb43e803b57080fe2416e51788556f83adb6f694e6d5e"
    )


def test_task9_semantic_model_and_remote_bounds_fail_closed_without_echo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(markdown_module, "_MAX_SEMANTIC_NODES", 1, raising=False)
    node_canary = "SEMANTIC_NODE_CANARY"
    node_body = {
        "content": [
            _task9_paragraph(_task9_text_run(node_canary + " one\n")),
            _task9_paragraph(_task9_text_run(node_canary + " two\n")),
        ]
    }
    with pytest.raises(DocsMCPError) as node_caught:
        markdown_module.remote_semantic(node_body)
    assert_semantic_verification_failed(node_caught.value, node_canary)

    monkeypatch.setattr(markdown_module, "_MAX_SEMANTIC_NODES", 100)
    monkeypatch.setattr(markdown_module, "_MAX_SEMANTIC_CHARS", 4, raising=False)
    remote_canary = "REMOTE_SEMANTIC_BUDGET_CANARY"
    with pytest.raises(DocsMCPError) as remote_caught:
        markdown_module.remote_semantic(
            {
                "content": [
                    _task9_paragraph(_task9_text_run(remote_canary + "\n"))
                ]
            }
        )
    assert_semantic_verification_failed(remote_caught.value, remote_canary)

    candidate_canary = "CANDIDATE_SEMANTIC_BUDGET_CANARY"
    with pytest.raises(DocsMCPError) as candidate_caught:
        markdown_module.candidate_semantic(
            DocumentModel(candidate_canary + "\n", (), (), (), ())
        )
    assert_semantic_verification_failed(candidate_caught.value, candidate_canary)


def test_task9_remote_semantic_rejects_malformed_shape_without_echo() -> None:
    shape_canary = "REMOTE_SEMANTIC_SHAPE_CANARY"

    with pytest.raises(DocsMCPError) as caught:
        markdown_module.remote_semantic({"content": shape_canary})

    assert_semantic_verification_failed(caught.value, shape_canary)


def test_task9_candidate_semantic_rejects_partial_heading_range() -> None:
    heading_canary = "PARTIAL_HEADING_RANGE_CANARY"
    model = DocumentModel(
        heading_canary + "\n",
        (HeadingRange(1, len(heading_canary) - 1, 1),),
        (),
        (),
        (),
    )

    with pytest.raises(DocsMCPError) as caught:
        markdown_module.candidate_semantic(model)

    assert_semantic_verification_failed(caught.value, heading_canary)


@pytest.mark.parametrize(
    ("field", "span"),
    (
        ("headings", HeadingRange(0, 1, 1)),
        ("bold", TextRange(0, 1)),
        ("links", LinkRange(0, 1, "https://span.example.test")),
    ),
)
def test_task9_candidate_semantic_rejects_span_overflow_before_iteration(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    span: TextRange,
) -> None:
    class CountingTuple(tuple):
        iterations = 0

        def __iter__(self):
            for value in super().__iter__():
                self.iterations += 1
                yield value

    monkeypatch.setattr(markdown_module, "_MAX_SEMANTIC_NODES", 3)
    spans = CountingTuple((span, span, span))
    fields = {"headings": (), "bold": (), "links": ()}
    fields[field] = spans
    model = DocumentModel("x\n", tables=(), **fields)

    with pytest.raises(DocsMCPError) as caught:
        markdown_module.candidate_semantic(model)

    assert_semantic_verification_failed(caught.value, "span.example.test")
    assert spans.iterations == 0


def test_task9_candidate_semantic_charges_link_urls_to_character_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    link_canary = "CANDIDATE_LINK_URL_BUDGET_CANARY"
    monkeypatch.setattr(markdown_module, "_MAX_SEMANTIC_CHARS", 8)
    model = DocumentModel(
        "x\n",
        (),
        (),
        (LinkRange(0, 1, link_canary),),
        (),
    )

    with pytest.raises(DocsMCPError) as caught:
        markdown_module.candidate_semantic(model)

    assert_semantic_verification_failed(caught.value, link_canary)


def test_task9_remote_semantic_charges_link_urls_to_character_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    link_canary = "REMOTE_LINK_URL_BUDGET_CANARY"
    monkeypatch.setattr(markdown_module, "_MAX_SEMANTIC_CHARS", 8)
    body = {
        "content": [
            _task9_paragraph(_task9_text_run("x\n", link=link_canary)),
        ]
    }

    with pytest.raises(DocsMCPError) as caught:
        markdown_module.remote_semantic(body)

    assert_semantic_verification_failed(caught.value, link_canary)


def test_task9_semantic_sha_stops_before_unneeded_nodes_after_byte_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CountingList(list):
        iterations = 0

        def __iter__(self):
            for value in super().__iter__():
                self.iterations += 1
                yield value

    hash_canary = "HASH_PAYLOAD_BUDGET_CANARY"
    trailing_canary = "HASH_TRAILING_ITERATION_CANARY"
    trailing = CountingList([trailing_canary])
    monkeypatch.setattr(markdown_module, "_MAX_SEMANTIC_HASH_BYTES", 16)

    with pytest.raises(DocsMCPError) as caught:
        markdown_module.semantic_sha256(
            {"aaa": hash_canary, "zzz": trailing}
        )

    assert_semantic_verification_failed(
        caught.value, hash_canary, trailing_canary
    )
    assert trailing.iterations == 0


@pytest.mark.parametrize(
    "non_finite", (float("nan"), float("inf"), float("-inf"))
)
def test_task9_semantic_sha_rejects_non_finite_json_without_echo(
    non_finite: float,
) -> None:
    with pytest.raises(DocsMCPError) as caught:
        markdown_module.semantic_sha256({"value": non_finite})

    assert_semantic_verification_failed(caught.value)
