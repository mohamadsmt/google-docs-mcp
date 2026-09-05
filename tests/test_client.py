import inspect
import io
import json
import os
import stat
import time
import zipfile
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, SupportsIndex

import pytest
import requests

import google_docs_mcp.client as client_module
import google_docs_mcp.markdown as markdown_module
from google_docs_mcp.client import (
    DocsMCPError,
    parse_document_id,
    utf16_index,
    utf16_length,
    validate_markdown,
    validate_max_chars,
    validate_new_text,
    validate_old_text,
    validate_replacement_count,
    validate_title,
)
from google_docs_mcp.markdown import (
    DocumentModel,
    InlineContent,
    LinkRange,
    TableBlock,
    TextRange,
)


VALID_ID = "abcDEF_123-xyz"


def paragraph_nodes(
    content: str,
    *,
    style: str = "NORMAL_TEXT",
    extra_elements: list[dict] | None = None,
) -> list[dict]:
    elements = [{"textRun": {"content": content}}]
    if extra_elements is not None:
        elements.extend(extra_elements)
    return [
        {
            "paragraph": {
                "paragraphStyle": {"namedStyleType": style},
                "elements": elements,
            }
        }
    ]


def _table_cell(*paragraphs: str) -> dict:
    content: list[dict] = []
    for paragraph in paragraphs:
        content.extend(paragraph_nodes(paragraph))
    return {"content": content}


def table_nodes() -> list[dict]:
    return [
        {
            "table": {
                "tableRows": [
                    {
                        "tableCells": [
                            _table_cell("سرستون ۱\n"),
                            _table_cell("سرستون ۲\n"),
                        ]
                    },
                    {
                        "tableCells": [
                            _table_cell("الف\n", "ب\n"),
                            _table_cell("یک|دو\n"),
                        ]
                    },
                    {"tableCells": [_table_cell("کوتاه\n")]},
                ]
            }
        }
    ]


MULTI_TAB_DOC = {
    "documentId": "abcDEF_123-xyz",
    "title": "سند تست",
    "revisionId": "rev-1",
    "tabs": [
        {
            "tabProperties": {"tabId": "t.1", "title": "اصلی", "index": 0},
            "documentTab": {"body": {"content": paragraph_nodes("سلام\n")}},
            "childTabs": [
                {
                    "tabProperties": {
                        "tabId": "t.2",
                        "title": "پیوست",
                        "index": 0,
                        "parentTabId": "t.1",
                    },
                    "documentTab": {"body": {"content": table_nodes()}},
                    "childTabs": [],
                }
            ],
        }
    ],
}


def assert_error_code(code: str, function: object, *args: object) -> DocsMCPError:
    with pytest.raises(DocsMCPError) as caught:
        function(*args)  # type: ignore[operator]
    assert caught.value.code == code
    assert len(caught.value.message) <= 200
    return caught.value


def assert_error_sanitized(error: DocsMCPError, *canaries: str) -> None:
    public_text = f"{error!s}\n{error!r}\n{error.as_result()!r}"
    for canary in canaries:
        assert canary not in public_text
    assert error.__context__ is None
    assert error.__cause__ is None


class ReverseTrackingList(list):
    def __init__(self, values: list[object]) -> None:
        super().__init__(values)
        self.reversed_calls = 0

    def __reversed__(self):
        self.reversed_calls += 1
        return super().__reversed__()


def test_tab_flattening_is_preorder_and_nested_selection_is_exact() -> None:
    tabs = client_module.flatten_tabs(MULTI_TAB_DOC["tabs"])

    assert [tab.tab_id for tab in tabs] == ["t.1", "t.2"]
    assert [(tab.title, tab.parent_tab_id) for tab in tabs] == [
        ("اصلی", None),
        ("پیوست", "t.1"),
    ]
    selected = client_module.select_tab(MULTI_TAB_DOC, "t.2")
    assert selected is tabs[1] or selected == tabs[1]
    assert selected.body is MULTI_TAB_DOC["tabs"][0]["childTabs"][0]["documentTab"][
        "body"
    ]
    with pytest.raises(FrozenInstanceError):
        selected.title = "تغییر"  # type: ignore[misc]


def test_tab_flattening_branching_forest_is_exact_depth_first_preorder() -> None:
    def tab(
        tab_id: str,
        parent_tab_id: str | None = None,
        child_tabs: list[dict] | None = None,
    ) -> dict:
        return {
            "tabProperties": {
                "tabId": tab_id,
                "title": f"title-{tab_id}",
                **({} if parent_tab_id is None else {"parentTabId": parent_tab_id}),
            },
            "documentTab": {"body": {"content": []}},
            "childTabs": [] if child_tabs is None else child_tabs,
        }

    tabs = [
        tab(
            "root-a",
            child_tabs=[
                tab("a-1", "root-a", [tab("a-1-i", "a-1")]),
                tab("a-2", "root-a"),
            ],
        ),
        tab("root-b", child_tabs=[tab("b-1", "root-b")]),
    ]

    flattened = client_module.flatten_tabs(tabs)

    assert [item.tab_id for item in flattened] == [
        "root-a",
        "a-1",
        "a-1-i",
        "a-2",
        "root-b",
        "b-1",
    ]
    assert [item.parent_tab_id for item in flattened] == [
        None,
        "root-a",
        "a-1",
        "root-a",
        None,
        "root-b",
    ]


def test_tab_multi_without_id_deliberately_derives_metadata_only_result() -> None:
    tabs = client_module.flatten_tabs(MULTI_TAB_DOC["tabs"])
    selected = client_module.select_tab(MULTI_TAB_DOC, None)

    content = None if selected is None else client_module.render_body(selected.body)[0]
    multiple_tabs = len(tabs) > 1
    assert selected is None
    assert content is None
    assert multiple_tabs is True


def test_tab_modern_single_without_id_selects_only_tab() -> None:
    document = {"title": "تک", "tabs": [MULTI_TAB_DOC["tabs"][0]["childTabs"][0]]}

    selected = client_module.select_tab(document, None)

    assert selected is not None
    assert selected.tab_id == "t.2"
    assert selected.title == "پیوست"


def test_tab_unknown_id_is_stable_sanitized_and_legacy_body_stays_readable() -> None:
    marker = "CANARY_UNKNOWN_TAB_SECRET"
    error = assert_error_code("tab_not_found", client_module.select_tab, MULTI_TAB_DOC, marker)
    assert_error_sanitized(error, marker)

    legacy_body = {"content": paragraph_nodes("قدیمی\n")}
    legacy_document = {"title": "سند قدیمی", "body": legacy_body}
    selected = client_module.select_tab(legacy_document, None)
    assert selected is not None
    assert selected.title == "سند قدیمی"
    assert selected.parent_tab_id is None
    assert selected.body is legacy_body
    assert client_module.render_body(selected.body) == ("قدیمی\n", [])

    legacy_error = assert_error_code(
        "tab_not_found", client_module.select_tab, legacy_document, marker
    )
    assert_error_sanitized(legacy_error, marker)


def test_tab_flattening_handles_more_than_1500_nested_children_iteratively() -> None:
    current: dict | None = None
    for index in range(1600, -1, -1):
        current = {
            "tabProperties": {
                "tabId": f"deep-{index}",
                "title": f"لایه {index}",
                **({} if index == 0 else {"parentTabId": f"deep-{index - 1}"}),
            },
            "documentTab": {"body": {"content": []}},
            "childTabs": [] if current is None else [current],
        }

    flattened = client_module.flatten_tabs([current])

    assert len(flattened) == 1601
    assert flattened[0].tab_id == "deep-0"
    assert flattened[-1].tab_id == "deep-1600"


def test_tab_node_limit_does_not_bulk_reverse_wide_sibling_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "CANARY_TAB_BULK_SCHEDULING_SECRET"
    tabs = ReverseTrackingList(
        [
            {
                "tabProperties": {
                    "tabId": f"wide-{index}",
                    "title": canary if index == 5 else f"tab-{index}",
                },
                "documentTab": {"body": {"content": []}},
                "childTabs": [],
            }
            for index in range(6)
        ]
    )
    monkeypatch.setattr(client_module, "_MAX_TAB_NODES", 2)

    error = assert_error_code("google_unavailable", client_module.flatten_tabs, tabs)

    assert_error_sanitized(error, canary)
    assert tabs.reversed_calls == 0


def test_render_body_preserves_text_outline_tables_toc_and_non_text_markers() -> None:
    body = {
        "content": [
            *paragraph_nodes("پیشگفتار\n"),
            *paragraph_nodes("عنوان | ویژه\n", style="HEADING_2"),
            *table_nodes(),
            {
                "tableOfContents": {
                    "content": paragraph_nodes("فهرست\n"),
                }
            },
            *paragraph_nodes(
                "نشانه",
                extra_elements=[{"inlineObjectElement": {"inlineObjectId": "secret"}}],
            ),
        ]
    }

    text, outline = client_module.render_body(body)

    assert text == (
        "پیشگفتار\n"
        "عنوان | ویژه\n"
        "| سرستون ۱ | سرستون ۲ |\n"
        "| --- | --- |\n"
        "| الف<br>ب | یک\\|دو |\n"
        "| کوتاه |  |\n"
        "فهرست\n"
        "نشانه⟦NON_TEXT:inlineObjectElement⟧\n"
    )
    assert outline == [{"level": 2, "text": "عنوان | ویژه"}]


def test_render_selected_tab_table_is_exact_and_short_rows_are_padded() -> None:
    selected = client_module.select_tab(MULTI_TAB_DOC, "t.2")
    assert selected is not None

    assert client_module.render_body(selected.body) == (
        "| سرستون ۱ | سرستون ۲ |\n"
        "| --- | --- |\n"
        "| الف<br>ب | یک\\|دو |\n"
        "| کوتاه |  |\n",
        [],
    )


def test_render_table_nested_under_toc_preserves_exact_order_and_outline() -> None:
    body = {
        "content": [
            *paragraph_nodes("قبل\n"),
            {
                "tableOfContents": {
                    "content": [
                        *paragraph_nodes("عنوان تو در تو\n", style="HEADING_3"),
                        *table_nodes(),
                        *paragraph_nodes("بعد از جدول\n"),
                    ]
                }
            },
            *paragraph_nodes("پایان\n"),
        ]
    }

    assert client_module.render_body(body) == (
        "قبل\n"
        "عنوان تو در تو\n"
        "| سرستون ۱ | سرستون ۲ |\n"
        "| --- | --- |\n"
        "| الف<br>ب | یک\\|دو |\n"
        "| کوتاه |  |\n"
        "بعد از جدول\n"
        "پایان\n",
        [{"level": 3, "text": "عنوان تو در تو"}],
    )


def test_render_sparse_wide_table_preflights_before_consuming_cell_buffers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "CANARY_SPARSE_TABLE_PREFLIGHT_SECRET"

    def buffer(text: str = "") -> client_module._RenderBuffer:
        return client_module._RenderBuffer([] if not text else [text], len(text))

    rows = [
        [buffer(canary), *(buffer() for _ in range(7))],
        [buffer()],
        [buffer()],
        [buffer()],
    ]
    active_chars = [sum(cell.length for row in rows for cell in row)]
    take_calls = 0
    original_take_render = client_module._take_render

    def tracking_take_render(
        cell: client_module._RenderBuffer,
        active: list[int],
    ) -> str:
        nonlocal take_calls
        take_calls += 1
        return original_take_render(cell, active)

    monkeypatch.setattr(client_module, "_MAX_RENDER_CHARS", 100)
    monkeypatch.setattr(client_module, "_take_render", tracking_take_render)

    error = assert_error_code(
        "google_unavailable",
        client_module._render_table,
        rows,
        client_module._RenderBuffer([]),
        active_chars,
    )

    assert_error_sanitized(error, canary)
    assert take_calls == 0


def test_render_node_limit_does_not_bulk_reverse_structural_sequences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracked_sequences: list[ReverseTrackingList] = []

    def tracked(values: list[object]) -> ReverseTrackingList:
        sequence = ReverseTrackingList(values)
        tracked_sequences.append(sequence)
        return sequence

    def paragraph(text: str) -> dict:
        return {
            "paragraph": {
                "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                "elements": tracked([{"textRun": {"content": text}}]),
            }
        }

    canary = "CANARY_RENDER_BULK_SCHEDULING_SECRET"
    cell_content = tracked([paragraph("cell\n")])
    table_cells = tracked([{"content": cell_content}])
    table_rows = tracked([{"tableCells": table_cells}])
    toc_content = tracked(
        [paragraph("toc-1\n"), paragraph(canary + "\n"), paragraph("toc-3\n")]
    )
    root_content = tracked(
        [
            paragraph("root\n"),
            {"table": {"tableRows": table_rows}},
            {"tableOfContents": {"content": toc_content}},
        ]
    )
    monkeypatch.setattr(client_module, "_MAX_RENDER_NODES", 11)

    error = assert_error_code(
        "google_unavailable", client_module.render_body, {"content": root_content}
    )

    assert_error_sanitized(error, canary)
    assert sum(sequence.reversed_calls for sequence in tracked_sequences) == 0


def test_render_unknown_hostile_element_kind_is_bounded_and_not_echoed() -> None:
    marker = "CANARY_HOSTILE_KIND_" + ("x" * 1000)
    body = {"content": [{"paragraph": {"elements": [{marker: {}}]}}]}

    text, outline = client_module.render_body(body)

    assert text == "⟦NON_TEXT:unknown⟧\n"
    assert marker not in text
    assert outline == []


def test_render_deep_toc_is_iterative_and_node_and_output_limits_are_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nested_content = paragraph_nodes("کوتاه فارسی 😀\n")
    for _ in range(1600):
        nested_content = [{"tableOfContents": {"content": nested_content}}]
    body = {"content": nested_content}

    assert client_module.render_body(body) == ("کوتاه فارسی 😀\n", [])

    node_canary = "CANARY_NODE_LIMIT_SECRET"
    limited_content = paragraph_nodes(node_canary + "\n")
    for _ in range(20):
        limited_content = [{"tableOfContents": {"content": limited_content}}]
    monkeypatch.setattr(client_module, "_MAX_RENDER_NODES", 8)
    node_error = assert_error_code(
        "google_unavailable", client_module.render_body, {"content": limited_content}
    )
    assert_error_sanitized(node_error, node_canary)

    monkeypatch.setattr(client_module, "_MAX_RENDER_NODES", 100)
    monkeypatch.setattr(client_module, "_MAX_RENDER_CHARS", 8)
    output_canary = "CANARY_OUTPUT_LIMIT_SECRET"
    output_error = assert_error_code(
        "google_unavailable",
        client_module.render_body,
        {"content": paragraph_nodes(output_canary)},
    )
    assert_error_sanitized(output_error, output_canary)


def test_pagination_first_middle_final_and_empty_tail_pages_are_exact() -> None:
    text = "abcdefghij"

    assert client_module.paginate(text, 0, 4) == {
        "content": "abcd",
        "start": 0,
        "end": 4,
        "total_chars": 10,
        "next_start": 4,
    }
    assert client_module.paginate(text, 4, 4) == {
        "content": "efgh",
        "start": 4,
        "end": 8,
        "total_chars": 10,
        "next_start": 8,
    }
    assert client_module.paginate(text, 8, 4) == {
        "content": "ij",
        "start": 8,
        "end": 10,
        "total_chars": 10,
        "next_start": None,
    }
    assert client_module.paginate(text, 10, 4) == {
        "content": "",
        "start": 10,
        "end": 10,
        "total_chars": 10,
        "next_start": None,
    }


@pytest.mark.parametrize("text", [None, True, b"text"])
def test_pagination_rejects_non_string_text(text: object) -> None:
    assert_error_code("invalid_text", client_module.paginate, text, 0, 1)


@pytest.mark.parametrize("start", [-1, 11, True, False, 1.0, "1", None])
def test_pagination_rejects_invalid_start(start: object) -> None:
    assert_error_code("invalid_start", client_module.paginate, "abcdefghij", start, 4)


@pytest.mark.parametrize("max_chars", [0, -1, True, False, 100_001])
def test_pagination_enforces_max_chars_hard_cap(max_chars: object) -> None:
    assert_error_code(
        "invalid_max_chars", client_module.paginate, "abcdefghij", 0, max_chars
    )


def test_pagination_page_never_exceeds_100000_characters() -> None:
    page = client_module.paginate("x" * 100_001, 0, 100_000)

    assert len(page["content"]) == 100_000
    assert page["end"] == 100_000
    assert page["next_start"] == 100_000


@pytest.mark.parametrize(
    "process_error",
    [KeyboardInterrupt("CANARY_RENDER_INTERRUPT"), SystemExit("CANARY_RENDER_EXIT")],
)
def test_render_process_control_base_exception_is_not_swallowed(
    process_error: BaseException,
) -> None:
    class ExplodingBody(dict):
        def get(self, key: object, default: object = None) -> object:
            raise process_error

    with pytest.raises(type(process_error)) as caught:
        client_module.render_body(ExplodingBody())

    assert caught.value is process_error


class FakeCredential:
    def __init__(
        self,
        *,
        valid: bool,
        expired: bool,
        refresh_token: str | None,
        refreshed_json: str = '{"state":"CANARY_REFRESHED"}',
        refresh_error: Exception | None = None,
        serialization_error: Exception | None = None,
        valid_after_refresh: bool = True,
    ) -> None:
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token
        self.refreshed_json = refreshed_json
        self.refresh_error = refresh_error
        self.serialization_error = serialization_error
        self.valid_after_refresh = valid_after_refresh
        self.refresh_requests: list[object] = []
        self.to_json_calls = 0

    def refresh(self, request: object) -> None:
        self.refresh_requests.append(request)
        if self.refresh_error is not None:
            raise self.refresh_error
        self.expired = False
        self.valid = self.valid_after_refresh

    def to_json(self) -> str:
        self.to_json_calls += 1
        if self.serialization_error is not None:
            raise self.serialization_error
        return self.refreshed_json


def install_fake_loader(
    monkeypatch: pytest.MonkeyPatch,
    *,
    credential: object | None = None,
    error: BaseException | None = None,
    on_load: Callable[[str], None] | None = None,
) -> list[str]:
    calls: list[str] = []

    class FakeCredentialsAPI:
        @classmethod
        def from_authorized_user_file(cls, path: str) -> object:
            calls.append(path)
            if on_load is not None:
                on_load(path)
            if error is not None:
                raise error
            assert credential is not None
            return credential

    monkeypatch.setattr(client_module, "Credentials", FakeCredentialsAPI, raising=False)
    return calls


@pytest.mark.parametrize("retryable", [False, True])
def test_error_result_has_exact_shape_and_retryable_value(retryable: bool) -> None:
    error = DocsMCPError("temporary_failure", "Operation failed.", retryable=retryable)

    assert isinstance(error, RuntimeError)
    assert error.code == "temporary_failure"
    assert error.message == "Operation failed."
    assert error.retryable is retryable
    assert error.as_result() == {
        "ok": False,
        "error": {
            "code": "temporary_failure",
            "message": "Operation failed.",
            "retryable": retryable,
        },
    }


def test_error_retryable_defaults_to_false() -> None:
    assert DocsMCPError("invalid_input", "Invalid input.").retryable is False


def test_parses_canonical_google_docs_url() -> None:
    value = "https://docs.google.com/document/d/abcDEF_123-xyz/edit?tab=t.1"

    assert parse_document_id(value) == VALID_ID


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (VALID_ID, VALID_ID),
        ("a" * 10, "a" * 10),
        ("Z" * 256, "Z" * 256),
        (f"https://docs.google.com/document/d/{VALID_ID}", VALID_ID),
        (f"https://docs.google.com/document/d/{VALID_ID}/edit", VALID_ID),
    ],
)
def test_accepts_valid_bare_ids_and_url_path_boundaries(
    value: str, expected: str
) -> None:
    assert parse_document_id(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        f"http://docs.google.com/document/d/{VALID_ID}/edit",
        f"https://evil.example/document/d/{VALID_ID}/edit",
        f"https://user@docs.google.com/document/d/{VALID_ID}/edit",
        f"https://docs.google.com:443/document/d/{VALID_ID}/edit",
        f"https://docs.google.com.evil.example/document/d/{VALID_ID}/edit",
        f"https://DOCS.GOOGLE.COM/document/d/{VALID_ID}/edit",
        f"https://docs.googIe.com/document/d/{VALID_ID}/edit",
        f"https://docs.google.com/spreadsheets/d/{VALID_ID}/edit",
        f"https://docs.google.com/document/d/{VALID_ID}.evil",
        f"https://docs.google.com/document/d/{'a' * 256}evil",
        f"https://docs.google.com/document/d/{VALID_ID}/edit/../copy",
        f"https://docs.google.com/document/d/../{VALID_ID}",
        f"https://docs.google.com/document/d/{VALID_ID}%2Fevil/edit",
        f"https://docs.google.com/document/d/abcDEF_123%2Dxyz/edit",
        "../abcDEF_123-xyz",
        "short",
        "a" * 257,
        "abc\x00DEF_123-xyz",
    ],
    ids=[
        "http",
        "evil-host",
        "userinfo",
        "port",
        "extra-host",
        "authority-case",
        "confusable-host",
        "sheets-path",
        "missing-id-boundary",
        "suffix-after-max-id",
        "traversal-after-id",
        "traversal-as-id",
        "percent-encoded-separator",
        "percent-encoded-id",
        "bare-traversal",
        "too-short",
        "too-long",
        "nul",
    ],
)
def test_rejects_invalid_document_references(value: str) -> None:
    assert_error_code("invalid_document_reference", parse_document_id, value)


@pytest.mark.parametrize("value", [None, 123, b"abcDEF_123-xyz", [VALID_ID]])
def test_rejects_non_string_document_references(value: object) -> None:
    assert_error_code("invalid_document_reference", parse_document_id, value)


def test_rejects_nonprinting_control_in_google_docs_url_suffix() -> None:
    value = f"https://docs.google.com/document/d/{VALID_ID}/edit\x7fhidden"

    assert_error_code("invalid_document_reference", parse_document_id, value)


def test_document_reference_error_does_not_echo_rejected_input() -> None:
    secret = "DO_NOT_ECHO.invalid/reference"

    error = assert_error_code("invalid_document_reference", parse_document_id, secret)

    assert "DO_NOT_ECHO" not in error.message
    assert "DO_NOT_ECHO" not in str(error)


def test_document_reference_parser_failure_has_no_exception_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "CANARY_DOCUMENT_REFERENCE_CONTEXT"

    def fail_split(_value: str) -> object:
        raise ValueError(marker)

    monkeypatch.setattr(client_module, "urlsplit", fail_split)

    error = assert_error_code(
        "invalid_document_reference",
        parse_document_id,
        "https://docs.google.com/document/d/invalid",
    )
    assert_error_sanitized(error, marker)


def test_utf16_indexes_account_for_astral_characters() -> None:
    text = "الف😀ب"

    assert utf16_length(text) == 6
    assert utf16_index(text, 4) == 5
    assert utf16_index(text, 0) == 0
    assert utf16_index(text, len(text)) == 6


@pytest.mark.parametrize("offset", [-1, 6, True, False, 1.0, "1", None])
def test_utf16_index_rejects_invalid_offsets(offset: object) -> None:
    assert_error_code("invalid_utf16_offset", utf16_index, "الف😀ب", offset)


def test_max_chars_uses_default_and_accepts_bounds() -> None:
    assert validate_max_chars(None) == 30_000
    assert validate_max_chars(1) == 1
    assert validate_max_chars(100_000) == 100_000


@pytest.mark.parametrize("value", [True, False, "100", 1.0, 0, -1, 100_001])
def test_max_chars_rejects_invalid_types_and_bounds(value: object) -> None:
    assert_error_code("invalid_max_chars", validate_max_chars, value)


def test_title_accepts_exact_boundary_without_normalizing() -> None:
    assert validate_title("x" * 200) == "x" * 200
    assert validate_title("  Kept verbatim  ") == "  Kept verbatim  "


@pytest.mark.parametrize("value", ["", "   \t\n", "x" * 201, "title\x00suffix", None, 3])
def test_title_rejects_invalid_values(value: object) -> None:
    assert_error_code("invalid_title", validate_title, value)


def test_title_error_does_not_echo_rejected_input() -> None:
    value = "DO_NOT_ECHO" + ("x" * 201)

    error = assert_error_code("invalid_title", validate_title, value)

    assert "DO_NOT_ECHO" not in error.message


def test_markdown_accepts_empty_and_exact_boundary() -> None:
    assert validate_markdown("") == ""
    assert validate_markdown("x" * 500_000) == "x" * 500_000


@pytest.mark.parametrize("value", ["x" * 500_001, "before\x00after", None, 3])
def test_markdown_rejects_invalid_values(value: object) -> None:
    assert_error_code("invalid_markdown", validate_markdown, value)


@pytest.mark.parametrize("count", [1, 100])
def test_replacement_count_accepts_bounds(count: int) -> None:
    assert validate_replacement_count(count) == count


@pytest.mark.parametrize("count", [0, 101, -1, True, False, 1.0, "1", None])
def test_replacement_count_rejects_invalid_values(count: object) -> None:
    assert_error_code("invalid_replacement_count", validate_replacement_count, count)


def test_replacement_text_validators_preserve_allowed_values() -> None:
    assert validate_old_text("old") == "old"
    assert validate_old_text(" ") == " "
    assert validate_new_text("") == ""
    assert validate_new_text("new") == "new"


@pytest.mark.parametrize("value", ["", "old\x00text", None, 3])
def test_old_text_rejects_empty_non_string_and_nul(value: object) -> None:
    assert_error_code("invalid_old_text", validate_old_text, value)


@pytest.mark.parametrize("value", ["new\x00text", None, 3])
def test_new_text_rejects_non_string_and_nul(value: object) -> None:
    assert_error_code("invalid_new_text", validate_new_text, value)


def test_token_path_uses_path_home_at_call_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    current_home = {"path": tmp_path / "first-home"}
    monkeypatch.setattr(
        Path, "home", classmethod(lambda cls: current_home["path"])
    )

    assert client_module.default_token_path() == (
        current_home["path"] / ".hermes/google_token.json"
    )

    current_home["path"] = tmp_path / "second-home"
    assert client_module.default_token_path() == (
        current_home["path"] / ".hermes/google_token.json"
    )


def test_atomic_private_write_creates_exact_utf8_bytes_with_0600_under_open_umask(
    tmp_path: Path,
) -> None:
    path = tmp_path / "token.json"
    data = '{"state":"CANARY_سلام😀"}\n'
    previous_umask = os.umask(0)
    try:
        client_module.atomic_write_private(path, data)
    finally:
        os.umask(previous_umask)

    assert path.read_bytes() == data.encode("utf-8")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["token.json"]


def test_atomic_private_write_replaces_regular_target_exactly_at_0600(
    tmp_path: Path,
) -> None:
    path = tmp_path / "token.json"
    path.write_bytes(b"CANARY_OLD_BYTES")
    path.chmod(0o644)
    old_inode = path.stat().st_ino
    data = '{"state":"CANARY_NEW_BYTES"}'

    client_module.atomic_write_private(path, data)

    assert path.read_bytes() == data.encode("utf-8")
    assert path.stat().st_ino != old_inode
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["token.json"]


def test_atomic_private_write_handles_short_os_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "token.json"
    data = "CANARY_SHORT_WRITE_سلام😀"
    real_write = os.write
    write_calls = 0

    def short_write(fd: int, chunk: bytes) -> int:
        nonlocal write_calls
        write_calls += 1
        return real_write(fd, chunk[:3])

    monkeypatch.setattr(client_module.os, "write", short_write)

    client_module.atomic_write_private(path, data)

    assert write_calls > 1
    assert path.read_bytes() == data.encode("utf-8")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_atomic_private_write_uses_exclusive_no_follow_close_on_exec_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "token.json"
    real_open = os.open
    open_calls: list[tuple[Path, int, int, int | None]] = []

    def recording_open(
        opened_path: object,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        open_calls.append((Path(opened_path), flags, mode, dir_fd))
        if dir_fd is None:
            return real_open(opened_path, flags, mode)  # type: ignore[arg-type]
        return real_open(  # type: ignore[arg-type]
            opened_path, flags, mode, dir_fd=dir_fd
        )

    monkeypatch.setattr(client_module.os, "open", recording_open)

    client_module.atomic_write_private(path, "CANARY_FLAGS")

    assert len(open_calls) == 1
    opened_path, flags, mode, dir_fd = open_calls[0]
    required_flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_CLOEXEC
        | os.O_NOFOLLOW
    )
    assert opened_path.parent == tmp_path
    assert opened_path != path
    assert flags & required_flags == required_flags
    assert flags & os.O_ACCMODE == os.O_WRONLY
    assert mode == 0o600
    assert dir_fd is None


def test_atomic_private_replace_failure_preserves_original_and_cleans_owned_temp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "CANARY_PATH_token.json"
    original = b"CANARY_ORIGINAL_BYTES"
    replacement = "CANARY_REPLACEMENT_DATA"
    path.write_bytes(original)
    path.chmod(0o644)
    unrelated = tmp_path / "CANARY_UNRELATED_FILE"
    unrelated.write_bytes(b"CANARY_UNRELATED_BYTES")
    before_names = sorted(entry.name for entry in tmp_path.iterdir())

    def fail_replace(source: object, target: object) -> None:
        raise OSError("CANARY_REPLACE_FAILURE")

    monkeypatch.setattr(client_module.os, "replace", fail_replace)

    error = assert_error_code(
        "credential_storage_failed",
        client_module.atomic_write_private,
        path,
        replacement,
    )

    assert_error_sanitized(
        error,
        str(path),
        replacement,
        "CANARY_REPLACE_FAILURE",
    )
    assert path.read_bytes() == original
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert unrelated.read_bytes() == b"CANARY_UNRELATED_BYTES"
    assert sorted(entry.name for entry in tmp_path.iterdir()) == before_names


def test_atomic_private_write_rejects_symlink_target_without_touching_referent(
    tmp_path: Path,
) -> None:
    referent = tmp_path / "CANARY_REFERENT"
    original = b"CANARY_REFERENT_BYTES"
    referent.write_bytes(original)
    path = tmp_path / "CANARY_LINK_token.json"
    path.symlink_to(referent)
    before_names = sorted(entry.name for entry in tmp_path.iterdir())

    error = assert_error_code(
        "credential_storage_failed",
        client_module.atomic_write_private,
        path,
        "CANARY_REPLACEMENT_DATA",
    )

    assert_error_sanitized(
        error,
        str(path),
        "CANARY_REFERENT_BYTES",
        "CANARY_REPLACEMENT_DATA",
    )
    assert path.is_symlink()
    assert referent.read_bytes() == original
    assert sorted(entry.name for entry in tmp_path.iterdir()) == before_names


def test_atomic_private_write_rejects_symlink_parent_without_creating_target(
    tmp_path: Path,
) -> None:
    real_parent = tmp_path / "CANARY_REAL_PARENT"
    real_parent.mkdir()
    linked_parent = tmp_path / "CANARY_LINKED_PARENT"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    path = linked_parent / "CANARY_token.json"

    error = assert_error_code(
        "credential_storage_failed",
        client_module.atomic_write_private,
        path,
        "CANARY_REPLACEMENT_DATA",
    )

    assert_error_sanitized(error, str(path), "CANARY_REPLACEMENT_DATA")
    assert linked_parent.is_symlink()
    assert list(real_parent.iterdir()) == []


def test_atomic_private_write_rejects_non_directory_parent_without_modifying_it(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "CANARY_NOT_A_DIRECTORY"
    original = b"CANARY_PARENT_BYTES"
    parent.write_bytes(original)
    path = parent / "CANARY_token.json"

    error = assert_error_code(
        "credential_storage_failed",
        client_module.atomic_write_private,
        path,
        "CANARY_REPLACEMENT_DATA",
    )

    assert_error_sanitized(error, str(path), "CANARY_REPLACEMENT_DATA")
    assert parent.read_bytes() == original


def test_atomic_private_write_rejects_missing_parent_without_creating_it(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "CANARY_MISSING_PARENT"
    path = parent / "CANARY_token.json"

    error = assert_error_code(
        "credential_storage_failed",
        client_module.atomic_write_private,
        path,
        "CANARY_REPLACEMENT_DATA",
    )

    assert_error_sanitized(error, str(path), "CANARY_REPLACEMENT_DATA")
    assert not parent.exists()


def test_atomic_private_write_rejects_nonregular_leaf_without_modifying_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "CANARY_TOKEN_DIRECTORY"
    path.mkdir()
    child = path / "CANARY_CHILD"
    child.write_bytes(b"CANARY_CHILD_BYTES")
    before_names = sorted(entry.name for entry in tmp_path.iterdir())

    error = assert_error_code(
        "credential_storage_failed",
        client_module.atomic_write_private,
        path,
        "CANARY_REPLACEMENT_DATA",
    )

    assert_error_sanitized(error, str(path), "CANARY_REPLACEMENT_DATA")
    assert path.is_dir()
    assert child.read_bytes() == b"CANARY_CHILD_BYTES"
    assert sorted(entry.name for entry in tmp_path.iterdir()) == before_names


@pytest.mark.parametrize("flag_name", ["O_CLOEXEC", "O_NOFOLLOW"])
@pytest.mark.parametrize("remove", [False, True], ids=["zero", "missing"])
def test_atomic_private_write_fails_closed_for_unusable_security_flag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    flag_name: str,
    remove: bool,
) -> None:
    path = tmp_path / "CANARY_token.json"
    if remove:
        monkeypatch.delattr(client_module.os, flag_name, raising=False)
    else:
        monkeypatch.setattr(client_module.os, flag_name, 0, raising=False)

    error = assert_error_code(
        "credential_storage_failed",
        client_module.atomic_write_private,
        path,
        "CANARY_REPLACEMENT_DATA",
    )

    assert_error_sanitized(error, str(path), "CANARY_REPLACEMENT_DATA")
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_auth_failure_for_missing_token_path_is_sanitized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "CANARY_MISSING_TOKEN_PATH.json"
    credential = FakeCredential(valid=True, expired=False, refresh_token=None)
    loader_calls = install_fake_loader(monkeypatch, credential=credential)

    error = assert_error_code(
        "google_needs_reauth", client_module.load_credentials, path
    )

    assert_error_sanitized(error, str(path), "CANARY_MISSING_TOKEN_PATH")
    assert loader_calls == []


def test_auth_failure_for_symlink_token_path_is_sanitized_and_nonmutating(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    referent = tmp_path / "CANARY_TOKEN_REFERENT"
    original = b"CANARY_TOKEN_CONTENT"
    referent.write_bytes(original)
    referent.chmod(0o644)
    path = tmp_path / "CANARY_TOKEN_LINK.json"
    path.symlink_to(referent)
    credential = FakeCredential(valid=True, expired=False, refresh_token=None)
    loader_calls = install_fake_loader(monkeypatch, credential=credential)

    error = assert_error_code(
        "google_needs_reauth", client_module.load_credentials, path
    )

    assert_error_sanitized(error, str(path), original.decode("ascii"))
    assert loader_calls == []
    assert path.is_symlink()
    assert referent.read_bytes() == original
    assert stat.S_IMODE(referent.stat().st_mode) == 0o644


def test_auth_failure_for_nonregular_token_path_is_sanitized_and_nonmutating(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "CANARY_TOKEN_DIRECTORY"
    path.mkdir()
    child = path / "CANARY_CHILD"
    child.write_bytes(b"CANARY_CHILD_CONTENT")
    credential = FakeCredential(valid=True, expired=False, refresh_token=None)
    loader_calls = install_fake_loader(monkeypatch, credential=credential)

    error = assert_error_code(
        "google_needs_reauth", client_module.load_credentials, path
    )

    assert_error_sanitized(error, str(path), "CANARY_CHILD_CONTENT")
    assert loader_calls == []
    assert child.read_bytes() == b"CANARY_CHILD_CONTENT"


def test_auth_failure_for_wrong_owner_token_is_sanitized_and_nonmutating(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "CANARY_WRONG_OWNER_TOKEN.json"
    original = b"CANARY_WRONG_OWNER_CONTENT"
    path.write_bytes(original)
    path.chmod(0o644)
    credential = FakeCredential(valid=True, expired=False, refresh_token=None)
    loader_calls = install_fake_loader(monkeypatch, credential=credential)
    real_lstat = os.lstat
    wrong_uid = os.getuid() + 1

    def wrong_owner_lstat(target: object, *args: object, **kwargs: object) -> os.stat_result:
        result = real_lstat(target, *args, **kwargs)  # type: ignore[arg-type]
        if os.fspath(target) != os.fspath(path):  # type: ignore[arg-type]
            return result
        values = list(result)
        values[4] = wrong_uid
        return os.stat_result(values)

    monkeypatch.setattr(client_module.os, "lstat", wrong_owner_lstat)

    error = assert_error_code(
        "google_needs_reauth", client_module.load_credentials, path
    )

    assert_error_sanitized(error, str(path), original.decode("ascii"))
    assert loader_calls == []
    assert path.read_bytes() == original
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_token_path_is_hardened_to_0600_before_exact_google_loader_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "CANARY_ACCEPTED_TOKEN.json"
    original = b"CANARY_ACCEPTED_CONTENT"
    path.write_bytes(original)
    path.chmod(0o644)
    credential = FakeCredential(valid=True, expired=False, refresh_token=None)
    observed_modes: list[int] = []
    loader_calls = install_fake_loader(
        monkeypatch,
        credential=credential,
        on_load=lambda loaded_path: observed_modes.append(
            stat.S_IMODE(Path(loaded_path).stat().st_mode)
        ),
    )

    result = client_module.load_credentials(path)

    assert result is credential
    assert loader_calls == [str(path)]
    assert observed_modes == [0o600]
    assert path.read_bytes() == original
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_auth_failure_from_loader_is_typed_sanitized_and_hardened_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "CANARY_LOADER_PATH.json"
    token_content = "CANARY_TOKEN_CONTENT"
    path.write_text(token_content, encoding="utf-8")
    path.chmod(0o644)
    loader_failure = (
        "CANARY_SECRET CANARY_CLIENT_ID CANARY_EMAIL CANARY_LOADER_FAILURE"
    )
    loader_calls = install_fake_loader(
        monkeypatch, error=ValueError(loader_failure)
    )

    error = assert_error_code(
        "google_needs_reauth", client_module.load_credentials, path
    )

    assert loader_calls == [str(path)]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert_error_sanitized(
        error,
        str(path),
        token_content,
        "CANARY_SECRET",
        "CANARY_CLIENT_ID",
        "CANARY_EMAIL",
        "CANARY_LOADER_FAILURE",
    )


def test_auth_failure_for_invalid_nonexpired_credentials_does_not_rewrite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "CANARY_INVALID_TOKEN.json"
    original = b"CANARY_INVALID_CONTENT"
    path.write_bytes(original)
    path.chmod(0o600)
    old_inode = path.stat().st_ino
    credential = FakeCredential(valid=False, expired=False, refresh_token=None)
    loader_calls = install_fake_loader(monkeypatch, credential=credential)

    error = assert_error_code(
        "google_needs_reauth", client_module.load_credentials, path
    )

    assert_error_sanitized(error, str(path), original.decode("ascii"))
    assert loader_calls == [str(path)]
    assert credential.refresh_requests == []
    assert credential.to_json_calls == 0
    assert path.read_bytes() == original
    assert path.stat().st_ino == old_inode


@pytest.mark.parametrize("refresh_token", [None, ""], ids=["none", "empty"])
def test_auth_failure_for_expired_credentials_without_refresh_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    refresh_token: str | None,
) -> None:
    path = tmp_path / "CANARY_EXPIRED_WITHOUT_REFRESH.json"
    original = b"CANARY_EXPIRED_CONTENT"
    path.write_bytes(original)
    path.chmod(0o600)
    old_inode = path.stat().st_ino
    credential = FakeCredential(
        valid=False,
        expired=True,
        refresh_token=refresh_token,
    )
    loader_calls = install_fake_loader(monkeypatch, credential=credential)

    error = assert_error_code(
        "google_needs_reauth", client_module.load_credentials, path
    )

    assert_error_sanitized(error, str(path), original.decode("ascii"))
    assert loader_calls == [str(path)]
    assert credential.refresh_requests == []
    assert credential.to_json_calls == 0
    assert path.read_bytes() == original
    assert path.stat().st_ino == old_inode


@pytest.mark.parametrize(
    "process_error",
    [KeyboardInterrupt("CANARY_INTERRUPT"), SystemExit("CANARY_EXIT")],
    ids=["keyboard-interrupt", "system-exit"],
)
def test_auth_failure_does_not_catch_process_control_exceptions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    process_error: BaseException,
) -> None:
    path = tmp_path / "CANARY_PROCESS_CONTROL_TOKEN.json"
    path.write_bytes(b"CANARY_PROCESS_CONTROL_CONTENT")
    path.chmod(0o600)
    install_fake_loader(monkeypatch, error=process_error)

    with pytest.raises(type(process_error)) as caught:
        client_module.load_credentials(path)

    assert caught.value is process_error


def test_refresh_is_not_called_or_rewritten_for_valid_nonexpired_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "CANARY_VALID_TOKEN.json"
    original = b"CANARY_VALID_CONTENT"
    path.write_bytes(original)
    path.chmod(0o644)
    before = path.stat()
    credential = FakeCredential(
        valid=True,
        expired=False,
        refresh_token="CANARY_UNUSED_REFRESH",
    )
    loader_calls = install_fake_loader(monkeypatch, credential=credential)

    result = client_module.load_credentials(path)

    after = path.stat()
    assert result is credential
    assert loader_calls == [str(path)]
    assert credential.refresh_requests == []
    assert credential.to_json_calls == 0
    assert path.read_bytes() == original
    assert after.st_ino == before.st_ino
    assert after.st_mtime_ns == before.st_mtime_ns
    assert stat.S_IMODE(after.st_mode) == 0o600


def test_refresh_calls_request_once_and_atomically_persists_exact_json_at_0600(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "CANARY_REFRESH_TOKEN.json"
    original = b"CANARY_PRE_REFRESH_CONTENT"
    refreshed_json = '{"state":"CANARY_REFRESHED_سلام😀"}'
    path.write_bytes(original)
    path.chmod(0o644)
    old_inode = path.stat().st_ino
    credential = FakeCredential(
        valid=False,
        expired=True,
        refresh_token="CANARY_REFRESH_AVAILABLE",
        refreshed_json=refreshed_json,
    )
    loader_calls = install_fake_loader(monkeypatch, credential=credential)
    request_instances: list[object] = []

    class FakeRequest:
        def __init__(self) -> None:
            request_instances.append(self)

    monkeypatch.setattr(client_module, "Request", FakeRequest, raising=False)

    result = client_module.load_credentials(path)

    assert result is credential
    assert loader_calls == [str(path)]
    assert len(request_instances) == 1
    assert credential.refresh_requests == request_instances
    assert credential.to_json_calls == 1
    assert credential.valid is True
    assert path.read_bytes() == refreshed_json.encode("utf-8")
    assert path.stat().st_ino != old_inode
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sorted(entry.name for entry in tmp_path.iterdir()) == [path.name]


@pytest.mark.parametrize("failure_stage", ["refresh", "serialization"])
def test_refresh_failure_is_sanitized_and_preserves_original_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_stage: str,
) -> None:
    path = tmp_path / f"CANARY_{failure_stage.upper()}_PATH.json"
    original = b"CANARY_ORIGINAL_TOKEN_CONTENT"
    path.write_bytes(original)
    path.chmod(0o644)
    old_inode = path.stat().st_ino
    before_names = sorted(entry.name for entry in tmp_path.iterdir())
    failure = ValueError(
        "CANARY_SECRET CANARY_CLIENT_ID CANARY_EMAIL CANARY_FAILURE_DETAIL"
    )
    credential = FakeCredential(
        valid=False,
        expired=True,
        refresh_token="CANARY_REFRESH_AVAILABLE",
        refresh_error=failure if failure_stage == "refresh" else None,
        serialization_error=(
            failure if failure_stage == "serialization" else None
        ),
    )
    loader_calls = install_fake_loader(monkeypatch, credential=credential)

    class FakeRequest:
        pass

    monkeypatch.setattr(client_module, "Request", FakeRequest, raising=False)

    error = assert_error_code(
        "google_needs_reauth", client_module.load_credentials, path
    )

    assert loader_calls == [str(path)]
    assert len(credential.refresh_requests) == 1
    assert credential.to_json_calls == (
        1 if failure_stage == "serialization" else 0
    )
    assert path.read_bytes() == original
    assert path.stat().st_ino == old_inode
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sorted(entry.name for entry in tmp_path.iterdir()) == before_names
    assert_error_sanitized(
        error,
        str(path),
        original.decode("ascii"),
        "CANARY_SECRET",
        "CANARY_CLIENT_ID",
        "CANARY_EMAIL",
        "CANARY_FAILURE_DETAIL",
    )


def test_refresh_requires_valid_credentials_before_serializing_or_replacing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "CANARY_INVALID_AFTER_REFRESH_PATH.json"
    original = b"CANARY_INVALID_AFTER_REFRESH_CONTENT"
    path.write_bytes(original)
    path.chmod(0o600)
    old_inode = path.stat().st_ino
    credential = FakeCredential(
        valid=False,
        expired=True,
        refresh_token="CANARY_REFRESH_AVAILABLE",
        valid_after_refresh=False,
    )
    loader_calls = install_fake_loader(monkeypatch, credential=credential)

    class FakeRequest:
        pass

    monkeypatch.setattr(client_module, "Request", FakeRequest, raising=False)

    error = assert_error_code(
        "google_needs_reauth", client_module.load_credentials, path
    )

    assert loader_calls == [str(path)]
    assert len(credential.refresh_requests) == 1
    assert credential.to_json_calls == 0
    assert path.read_bytes() == original
    assert path.stat().st_ino == old_inode
    assert sorted(entry.name for entry in tmp_path.iterdir()) == [path.name]
    assert_error_sanitized(error, str(path), original.decode("ascii"))


_UNSET = object()


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        *,
        json_result: object = _UNSET,
        headers: dict[str, str] | None = None,
        content: object = b"",
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self.json_result = {} if json_result is _UNSET else json_result
        self.headers = {} if headers is None else headers
        self.content = content
        self.text = text

    def json(self) -> object:
        if isinstance(self.json_result, BaseException):
            raise self.json_result
        return self.json_result

    def raise_for_status(self) -> None:
        raise AssertionError("response.raise_for_status must not be called")


class FakeSession:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def request(self, method: str, url: str, **kwargs: object) -> object:
        self.calls.append((method, url, dict(kwargs)))
        if not self.outcomes:
            raise AssertionError("unexpected extra request")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def make_google_client(session: FakeSession) -> object:
    return client_module.GoogleDocsClient(session)  # type: ignore[attr-defined]


def record_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    return sleeps


def assert_transport_error_sanitized(
    error: DocsMCPError, *canaries: str
) -> None:
    assert_error_sanitized(error, *canaries)
    public_graph = repr(
        (
            error.args,
            vars(error),
            error.as_result(),
            error.__context__,
            error.__cause__,
        )
    )
    for canary in canaries:
        assert canary not in public_graph


def test_nonretryable_400_uses_one_attempt_and_stable_error() -> None:
    response = FakeResponse(400, text="CANARY_BAD_REQUEST_BODY")
    session = FakeSession([response])
    google = make_google_client(session)

    error = assert_error_code(
        "google_unavailable", google.get_document, VALID_ID  # type: ignore[attr-defined]
    )

    assert error.retryable is False
    assert len(session.calls) == 1
    assert_transport_error_sanitized(error, "CANARY_BAD_REQUEST_BODY")


def test_retry_429_honors_decimal_retry_after_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession(
        [
            FakeResponse(429, headers={"Retry-After": "2"}),
            FakeResponse(200, json_result={"documentId": VALID_ID}),
        ]
    )
    sleeps = record_sleeps(monkeypatch)
    google = make_google_client(session)

    result = google.get_document(VALID_ID)  # type: ignore[attr-defined]

    assert result == {"documentId": VALID_ID}
    assert sleeps == [2]
    assert len(session.calls) == 2


def test_retry_five_503_responses_stops_after_exact_backoff_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession([FakeResponse(503) for _ in range(6)])
    sleeps = record_sleeps(monkeypatch)
    google = make_google_client(session)

    error = assert_error_code(
        "google_unavailable", google.get_document, VALID_ID  # type: ignore[attr-defined]
    )

    assert error.retryable is True
    assert sleeps == [1, 2, 4, 8]
    assert len(session.calls) == 5
    assert len(session.outcomes) == 1


def test_retry_exhausted_429_is_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession([FakeResponse(429) for _ in range(5)])
    sleeps = record_sleeps(monkeypatch)
    google = make_google_client(session)

    error = assert_error_code(
        "rate_limited", google.get_document, VALID_ID  # type: ignore[attr-defined]
    )

    assert error.retryable is True
    assert sleeps == [1, 2, 4, 8]
    assert len(session.calls) == 5


def test_retry_network_exhaustion_is_sanitized_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "CANARY_NETWORK_EXCEPTION"
    failures: list[object] = [requests.ConnectionError(marker) for _ in range(5)]
    session = FakeSession(failures)
    sleeps = record_sleeps(monkeypatch)
    google = make_google_client(session)

    error = assert_error_code(
        "google_unavailable", google.get_document, VALID_ID  # type: ignore[attr-defined]
    )

    assert error.retryable is True
    assert sleeps == [1, 2, 4, 8]
    assert len(session.calls) == 5
    assert_transport_error_sanitized(error, marker)


@pytest.mark.parametrize(
    "network_error",
    [
        pytest.param(
            requests.ReadTimeout("CANARY_CREATE_READ_TIMEOUT"), id="read-timeout"
        ),
        pytest.param(
            requests.ConnectionError("CANARY_CREATE_CONNECTION_ERROR"),
            id="connection-error",
        ),
    ],
)
def test_create_document_ambiguous_network_failure_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, network_error: requests.RequestException
) -> None:
    success = FakeResponse(200, json_result={"documentId": VALID_ID})
    session = FakeSession([network_error, success])
    sleeps = record_sleeps(monkeypatch)
    google = make_google_client(session)

    error = assert_error_code(
        "google_unavailable", google.create_document, "A title"  # type: ignore[attr-defined]
    )

    assert error.retryable is False
    assert sleeps == []
    assert len(session.calls) == 1
    assert session.outcomes == [success]
    assert_transport_error_sanitized(error, str(network_error))


def test_create_document_503_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "CANARY_CREATE_503_BODY"
    success = FakeResponse(200, json_result={"documentId": VALID_ID})
    session = FakeSession([FakeResponse(503, text=marker), success])
    sleeps = record_sleeps(monkeypatch)
    google = make_google_client(session)

    error = assert_error_code(
        "google_unavailable", google.create_document, "A title"  # type: ignore[attr-defined]
    )

    assert error.retryable is False
    assert sleeps == []
    assert len(session.calls) == 1
    assert session.outcomes == [success]
    assert_transport_error_sanitized(error, marker)


def test_create_document_429_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body_marker = "CANARY_CREATE_429_BODY"
    header_marker = "CANARY_CREATE_429_HEADER"
    success = FakeResponse(200, json_result={"documentId": VALID_ID})
    session = FakeSession(
        [
            FakeResponse(
                429,
                text=body_marker,
                headers={"Retry-After": header_marker},
            ),
            success,
        ]
    )
    sleeps = record_sleeps(monkeypatch)
    google = make_google_client(session)

    error = assert_error_code(
        "rate_limited", google.create_document, "A title"  # type: ignore[attr-defined]
    )

    assert error.retryable is True
    assert sleeps == []
    assert len(session.calls) == 1
    assert session.outcomes == [success]
    assert_transport_error_sanitized(error, body_marker, header_marker)


def test_file_not_found_error_is_nonretryable_and_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "CANARY_DETERMINISTIC_FILE_NOT_FOUND"
    success = FakeResponse(200, json_result={"documentId": VALID_ID})
    session = FakeSession([FileNotFoundError(marker), success])
    sleeps = record_sleeps(monkeypatch)
    google = make_google_client(session)

    error = assert_error_code(
        "google_unavailable", google.get_document, VALID_ID  # type: ignore[attr-defined]
    )

    assert error.retryable is False
    assert sleeps == []
    assert len(session.calls) == 1
    assert session.outcomes == [success]
    assert_transport_error_sanitized(error, marker)


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "google_needs_reauth"), (403, "permission_denied"), (404, "document_not_found")],
)
def test_permission_and_resource_statuses_are_exact_and_not_retried(
    status: int, code: str
) -> None:
    session = FakeSession([FakeResponse(status), FakeResponse(200)])
    google = make_google_client(session)

    error = assert_error_code(
        code, google.get_document, VALID_ID  # type: ignore[attr-defined]
    )

    assert error.retryable is False
    assert len(session.calls) == 1


def test_nonretryable_error_body_never_enters_public_error_graph() -> None:
    marker = "CANARY_SECRET"
    session = FakeSession([FakeResponse(400, text=marker)])
    google = make_google_client(session)

    error = assert_error_code(
        "google_unavailable", google.get_document, VALID_ID  # type: ignore[attr-defined]
    )

    assert_transport_error_sanitized(error, marker)


@pytest.mark.parametrize(
    ("retry_after", "expected_sleep"),
    [
        ("bogus", 1),
        ("-1", 1),
        ("2.5", 1),
        ("999", 60),
    ],
)
def test_retry_after_malformed_falls_back_and_oversized_caps(
    monkeypatch: pytest.MonkeyPatch,
    retry_after: str,
    expected_sleep: int,
) -> None:
    session = FakeSession(
        [
            FakeResponse(503, headers={"Retry-After": retry_after}),
            FakeResponse(200, json_result={"ok": True}),
        ]
    )
    sleeps = record_sleeps(monkeypatch)
    google = make_google_client(session)

    assert google.get_document(VALID_ID) == {"ok": True}  # type: ignore[attr-defined]
    assert sleeps == [expected_sleep]


@pytest.mark.parametrize(
    "process_error",
    [KeyboardInterrupt("CANARY_HTTP_INTERRUPT"), SystemExit("CANARY_HTTP_EXIT")],
)
def test_fixed_method_process_control_exception_propagates_unchanged(
    process_error: BaseException,
) -> None:
    session = FakeSession([process_error])
    google = make_google_client(session)

    with pytest.raises(type(process_error)) as caught:
        google.get_document(VALID_ID)  # type: ignore[attr-defined]

    assert caught.value is process_error
    assert len(session.calls) == 1


def test_nonretryable_ordinary_session_exception_is_sanitized_once() -> None:
    marker = "CANARY_THIRD_PARTY_FAILURE"
    session = FakeSession([ValueError(marker), FakeResponse(200)])
    google = make_google_client(session)

    error = assert_error_code(
        "google_unavailable", google.get_document, VALID_ID  # type: ignore[attr-defined]
    )

    assert error.retryable is False
    assert len(session.calls) == 1
    assert_transport_error_sanitized(error, marker)


def test_fixed_method_constants_and_public_surface_are_bounded() -> None:
    client_type = client_module.GoogleDocsClient  # type: ignore[attr-defined]

    assert client_type.DOCS_BASE == "https://docs.googleapis.com/v1"
    assert client_type.DRIVE_BASE == "https://www.googleapis.com/drive/v3"
    assert client_type.RETRYABLE == {408, 429, 500, 502, 503, 504}
    public_methods = {
        name
        for name, value in vars(client_type).items()
        if not name.startswith("_") and callable(value)
    }
    assert public_methods == {
        "get_document",
        "create_document",
        "batch_update",
        "drive_metadata",
        "export_file",
        "delete_file",
    }
    assert "request" not in vars(client_type)
    for name in public_methods:
        parameters = inspect.signature(getattr(client_type, name)).parameters
        assert not ({"url", "method", "header", "headers"} & set(parameters))


def test_fixed_method_get_document_builds_exact_request() -> None:
    payload = {"documentId": VALID_ID, "title": "A title"}
    session = FakeSession([FakeResponse(200, json_result=payload)])
    google = make_google_client(session)

    assert google.get_document(VALID_ID) == payload  # type: ignore[attr-defined]
    assert session.calls == [
        (
            "GET",
            f"https://docs.googleapis.com/v1/documents/{VALID_ID}",
            {"params": {"includeTabsContent": "true"}, "timeout": (20, 180)},
        )
    ]


def test_fixed_method_create_document_builds_exact_request() -> None:
    session = FakeSession(
        [FakeResponse(200, json_result={"documentId": VALID_ID, "title": "A title"})]
    )
    google = make_google_client(session)

    assert google.create_document("A title") == VALID_ID  # type: ignore[attr-defined]
    assert session.calls == [
        (
            "POST",
            "https://docs.googleapis.com/v1/documents",
            {"json": {"title": "A title"}, "timeout": (20, 180)},
        )
    ]


def test_fixed_method_batch_update_builds_exact_write_control() -> None:
    update_requests = [{"insertText": {"text": "hello", "endOfSegmentLocation": {}}}]
    payload = {"documentId": VALID_ID, "replies": [{}]}
    session = FakeSession([FakeResponse(200, json_result=payload)])
    google = make_google_client(session)

    assert google.batch_update(VALID_ID, update_requests, "revision-7") == payload  # type: ignore[attr-defined]
    assert session.calls == [
        (
            "POST",
            f"https://docs.googleapis.com/v1/documents/{VALID_ID}:batchUpdate",
            {
                "json": {
                    "requests": update_requests,
                    "writeControl": {"requiredRevisionId": "revision-7"},
                },
                "timeout": (20, 180),
            },
        )
    ]


def test_fixed_method_empty_batch_update_skips_http() -> None:
    session = FakeSession([])
    google = make_google_client(session)

    assert google.batch_update(VALID_ID, [], "revision-7") == {}  # type: ignore[attr-defined]
    assert session.calls == []


def test_fixed_method_drive_metadata_uses_fixed_fields_projection() -> None:
    payload = {"id": VALID_ID, "name": "A title"}
    session = FakeSession([FakeResponse(200, json_result=payload)])
    google = make_google_client(session)

    assert google.drive_metadata(VALID_ID) == payload  # type: ignore[attr-defined]
    assert session.calls == [
        (
            "GET",
            f"https://www.googleapis.com/drive/v3/files/{VALID_ID}",
            {
                "params": {
                    "fields": "id,name,mimeType,modifiedTime,version,webViewLink",
                },
                "timeout": (20, 180),
            },
        )
    ]


def test_fixed_method_export_returns_exact_bytes_and_params() -> None:
    exported = b"%PDF-CANARY"
    session = FakeSession([FakeResponse(200, content=exported)])
    google = make_google_client(session)

    assert google.export_file(VALID_ID, "application/pdf") == exported  # type: ignore[attr-defined]
    assert session.calls == [
        (
            "GET",
            f"https://www.googleapis.com/drive/v3/files/{VALID_ID}/export",
            {
                "params": {"mimeType": "application/pdf"},
                "timeout": (20, 180),
            },
        )
    ]


def test_fixed_method_delete_returns_none_and_builds_exact_request() -> None:
    session = FakeSession([FakeResponse(204)])
    google = make_google_client(session)

    assert google.delete_file(VALID_ID) is None  # type: ignore[attr-defined]
    assert session.calls == [
        (
            "DELETE",
            f"https://www.googleapis.com/drive/v3/files/{VALID_ID}",
            {"timeout": (20, 180)},
        )
    ]


@pytest.mark.parametrize(
    "json_result",
    [ValueError("CANARY_JSON_DECODE"), [], None, "not-a-dict"],
)
def test_malformed_success_document_json_is_sanitized(
    json_result: object,
) -> None:
    session = FakeSession([FakeResponse(200, json_result=json_result)])
    google = make_google_client(session)

    error = assert_error_code(
        "google_unavailable", google.get_document, VALID_ID  # type: ignore[attr-defined]
    )

    assert error.retryable is False
    assert_transport_error_sanitized(error, "CANARY_JSON_DECODE")


@pytest.mark.parametrize(
    "json_result",
    [{}, {"documentId": None}, {"documentId": 123}, {"documentId": ""}],
)
def test_malformed_success_create_shape_is_sanitized(
    json_result: object,
) -> None:
    session = FakeSession([FakeResponse(200, json_result=json_result)])
    google = make_google_client(session)

    error = assert_error_code(
        "google_unavailable", google.create_document, "A title"  # type: ignore[attr-defined]
    )

    assert error.retryable is False
    assert_transport_error_sanitized(error)


def test_malformed_success_export_shape_is_sanitized() -> None:
    session = FakeSession([FakeResponse(200, content="CANARY_NOT_BYTES")])
    google = make_google_client(session)

    error = assert_error_code(
        "google_unavailable", google.export_file, VALID_ID, "application/pdf"  # type: ignore[attr-defined]
    )

    assert error.retryable is False
    assert_transport_error_sanitized(error, "CANARY_NOT_BYTES")


class RecoveryExportClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.payloads = {
            "text/plain": b"RECOVERY_TEXT_CANARY",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (
                b"RECOVERY_DOCX_CANARY"
            ),
        }

    def export_file(self, document_id: str, mime_type: str) -> bytes:
        self.calls.append((document_id, mime_type))
        return self.payloads[mime_type]


def test_task8_recovery_helpers_have_exact_signatures_and_frozen_shape() -> None:
    backup_type = client_module.RecoveryBackup
    assert tuple(backup_type.__dataclass_fields__) == (
        "path",
        "text_path",
        "docx_path",
    )

    make_parameters = tuple(
        inspect.signature(client_module.make_recovery_backup).parameters.values()
    )
    assert tuple(parameter.name for parameter in make_parameters) == (
        "client",
        "document_id",
        "root",
    )
    assert all(
        parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        and parameter.default is inspect.Parameter.empty
        for parameter in make_parameters
    )
    assert inspect.signature(client_module.make_recovery_backup).return_annotation is backup_type

    purge_parameters = tuple(
        inspect.signature(client_module.purge_old_recovery).parameters.values()
    )
    assert tuple(parameter.name for parameter in purge_parameters) == ("root", "now")
    assert tuple(parameter.annotation for parameter in purge_parameters) == (
        Path,
        datetime,
    )
    assert inspect.signature(client_module.purge_old_recovery).return_annotation == list[Path]


def test_task8_recovery_backup_exports_exact_bytes_with_private_modes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "recovery"
    client = RecoveryExportClient()
    previous_umask = os.umask(0)
    try:
        backup = client_module.make_recovery_backup(client, VALID_ID, root)
    finally:
        os.umask(previous_umask)

    assert client.calls == [
        (VALID_ID, "text/plain"),
        (
            VALID_ID,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
    ]
    assert backup.path.parent == root
    assert backup.path.name.startswith("recovery-")
    assert backup.text_path == backup.path / "document.txt"
    assert backup.docx_path == backup.path / "document.docx"
    assert backup.text_path.read_bytes() == b"RECOVERY_TEXT_CANARY"
    assert backup.docx_path.read_bytes() == b"RECOVERY_DOCX_CANARY"
    assert sorted(path.name for path in backup.path.iterdir()) == [
        "document.docx",
        "document.txt",
    ]
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(backup.path.stat().st_mode) == 0o700
    assert stat.S_IMODE(backup.text_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.docx_path.stat().st_mode) == 0o600
    with pytest.raises(FrozenInstanceError):
        backup.path = tmp_path  # type: ignore[misc]


def test_task8_recovery_backup_fsyncs_files_and_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "recovery"
    client = RecoveryExportClient()
    regular_syncs = 0
    directory_syncs = 0
    original_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        nonlocal regular_syncs, directory_syncs
        mode = os.fstat(fd).st_mode
        if stat.S_ISREG(mode):
            regular_syncs += 1
        elif stat.S_ISDIR(mode):
            directory_syncs += 1
        original_fsync(fd)

    monkeypatch.setattr(client_module.os, "fsync", recording_fsync)

    client_module.make_recovery_backup(client, VALID_ID, root)

    assert regular_syncs == 2
    assert directory_syncs >= 2


def test_task8_recovery_backup_fsyncs_parent_when_creating_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "recovery"
    synced_directories: list[Path] = []
    original_sync = client_module._fsync_recovery_directory

    def recording_sync(path: Path) -> None:
        synced_directories.append(Path(path))
        original_sync(path)

    monkeypatch.setattr(
        client_module, "_fsync_recovery_directory", recording_sync
    )

    client_module.make_recovery_backup(RecoveryExportClient(), VALID_ID, root)

    assert tmp_path in synced_directories


def test_task8_recovery_backup_rejects_symlink_root_before_export(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    root = tmp_path / "recovery"
    root.symlink_to(target, target_is_directory=True)
    client = RecoveryExportClient()

    error = assert_error_code(
        "google_unavailable",
        client_module.make_recovery_backup,
        client,
        VALID_ID,
        root,
    )

    assert client.calls == []
    assert list(target.iterdir()) == []
    assert_error_sanitized(error, str(root), "RECOVERY_TEXT_CANARY")


def test_task8_recovery_purge_removes_only_backups_older_than_seven_days(
    tmp_path: Path,
) -> None:
    root = tmp_path / "recovery"
    client = RecoveryExportClient()
    old = client_module.make_recovery_backup(client, VALID_ID, root)
    boundary = client_module.make_recovery_backup(client, VALID_ID, root)
    recent = client_module.make_recovery_backup(client, VALID_ID, root)
    unknown = root / "recovery-unknown"
    unknown.mkdir(mode=0o700)
    (unknown / "unexpected").write_text("KEEP_CANARY", encoding="utf-8")
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    for path, age in (
        (old.path, timedelta(days=8)),
        (boundary.path, timedelta(days=7)),
        (recent.path, timedelta(days=6)),
        (unknown, timedelta(days=30)),
    ):
        timestamp = (now - age).timestamp()
        os.utime(path, (timestamp, timestamp))

    removed = client_module.purge_old_recovery(root, now)

    assert removed == [old.path]
    assert not old.path.exists()
    assert boundary.path.is_dir()
    assert recent.path.is_dir()
    assert unknown.is_dir()
    assert (unknown / "unexpected").read_text(encoding="utf-8") == "KEEP_CANARY"


def test_task8_recovery_purge_removes_owned_partial_backup(
    tmp_path: Path,
) -> None:
    root = tmp_path / "recovery"
    backup = client_module.make_recovery_backup(
        RecoveryExportClient(), VALID_ID, root
    )
    partial = backup.path
    backup.docx_path.unlink()
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    timestamp = (now - timedelta(days=8)).timestamp()
    os.utime(partial, (timestamp, timestamp))

    assert client_module.purge_old_recovery(root, now) == [partial]
    assert not partial.exists()


def test_task8_recovery_purge_preserves_unowned_allowed_name_subsets(
    tmp_path: Path,
) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    names = (
        "recovery-hostile",
        "recovery-20260831T120000.000000Z-0000000000000001",
        "recovery-20260831T120000.000000Z-0000000000000002",
        "recovery-20260831T120000.000000Z-0000000000000003",
    )
    invalid_name, permissive_file, permissive_directory, linked_file = (
        root / name for name in names
    )
    for path in (invalid_name, permissive_file, permissive_directory, linked_file):
        path.mkdir(mode=0o700)
    (invalid_name / "document.txt").write_bytes(b"HOSTILE_INVALID_NAME")
    invalid_name.joinpath("document.txt").chmod(0o600)
    (permissive_file / "document.txt").write_bytes(b"HOSTILE_FILE_MODE")
    permissive_file.joinpath("document.txt").chmod(0o644)
    (permissive_directory / "document.txt").write_bytes(b"HOSTILE_DIR_MODE")
    permissive_directory.joinpath("document.txt").chmod(0o600)
    permissive_directory.chmod(0o755)
    outside = tmp_path / "outside-hardlink"
    outside.write_bytes(b"HOSTILE_LINK_COUNT")
    outside.chmod(0o600)
    os.link(outside, linked_file / "document.txt")
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    timestamp = (now - timedelta(days=8)).timestamp()
    for path in (invalid_name, permissive_file, permissive_directory, linked_file):
        os.utime(path, (timestamp, timestamp))

    assert client_module.purge_old_recovery(root, now) == []
    assert all(path.is_dir() for path in (
        invalid_name,
        permissive_file,
        permissive_directory,
        linked_file,
    ))
    assert outside.read_bytes() == b"HOSTILE_LINK_COUNT"


def _task8_phase_table_node_row(starts: tuple[int, ...]) -> dict:
    return {
        "startIndex": starts[0] - 1,
        "endIndex": starts[-1] + 5,
        "table": {
            "tableRows": [
                {
                    "tableCells": [
                        {
                            "content": [
                                {
                                    "startIndex": start_index,
                                    "endIndex": start_index + 1,
                                    "paragraph": {"elements": []},
                                }
                            ]
                        }
                        for start_index in starts
                    ]
                }
            ]
        },
    }


def _task8_phase_table_node(start_index: int) -> dict:
    return _task8_phase_table_node_row((start_index,))


def _task8_phase_document(revision: str, selected_start: int) -> dict:
    return {
        "documentId": VALID_ID,
        "revisionId": revision,
        "tabs": [
            {
                "tabProperties": {"tabId": "t.decoy", "title": "Decoy"},
                "documentTab": {
                    "body": {"content": [_task8_phase_table_node(10)]}
                },
                "childTabs": [],
            },
            {
                "tabProperties": {"tabId": "t.selected", "title": "Selected"},
                "documentTab": {
                    "body": {
                        "content": [_task8_phase_table_node(selected_start)]
                    }
                },
                "childTabs": [],
            },
        ],
    }


def _task8_phase_model() -> DocumentModel:
    marker = "⟦TABLE-0001⟧"
    cell = InlineContent(
        "😀 x",
        bold=(TextRange(2, 3),),
        links=(LinkRange(0, 1, "https://phase.example.test"),),
    )
    return DocumentModel(
        text=marker + "\n",
        headings=(),
        bold=(),
        links=(),
        tables=(TableBlock(marker, ((cell,),)),),
    )


def _task8_multi_table_model(
    width: int, *, styled: bool = False, include_text: bool = True
) -> DocumentModel:
    markers = ("⟦TABLE-0001⟧", "⟦TABLE-0002⟧")

    def cell(table_index: int, column: int) -> InlineContent:
        text = f"cell-{table_index}-{column}" if include_text else ""
        bold = (TextRange(0, 1), TextRange(1, 2)) if styled else ()
        return InlineContent(text, bold=bold)

    tables = tuple(
        TableBlock(
            marker,
            (tuple(cell(table_index, column) for column in range(width)),),
        )
        for table_index, marker in enumerate(markers)
    )
    return DocumentModel(
        text="\n".join(markers) + "\n",
        headings=(),
        bold=(),
        links=(),
        tables=tables,
    )


def _task8_multi_table_document(revision: str, width: int) -> dict:
    return {
        "documentId": VALID_ID,
        "revisionId": revision,
        "tabs": [
            {
                "tabProperties": {"tabId": "t.selected", "title": "Selected"},
                "documentTab": {
                    "body": {
                        "content": [
                            _task8_phase_table_node_row(
                                tuple(base + 10 * column for column in range(width))
                            )
                            for base in (100, 1000)
                        ]
                    }
                },
                "childTabs": [],
            }
        ],
    }


class TablePhaseClient(RecoveryExportClient):
    def __init__(
        self,
        *,
        revisions: list[str],
        documents: list[dict],
        fail_on_batch: int | None = None,
    ) -> None:
        super().__init__()
        self.revisions = list(revisions)
        self.documents = list(documents)
        self.fail_on_batch = fail_on_batch
        self.batch_calls: list[tuple[list[dict], str]] = []
        self.events: list[str] = []

    def export_file(self, document_id: str, mime_type: str) -> bytes:
        self.events.append(f"export:{mime_type}")
        return super().export_file(document_id, mime_type)

    def batch_update(
        self, document_id: str, requests_body: list[dict], revision: str
    ) -> dict:
        assert document_id == VALID_ID
        self.batch_calls.append((requests_body, revision))
        self.events.append(f"batch:{revision}")
        if self.fail_on_batch == len(self.batch_calls):
            raise DocsMCPError("stale_revision", "STALE_REVISION_CANARY")
        return {
            "writeControl": {"requiredRevisionId": self.revisions.pop(0)}
        }

    def get_document(self, document_id: str) -> dict:
        assert document_id == VALID_ID
        self.events.append("get_document")
        return self.documents.pop(0)


def test_task8_table_readback_uses_only_the_selected_tab() -> None:
    document = _task8_phase_document("rev-2", 100)

    nodes = client_module._table_nodes_for_tab(
        document, "t.selected", expected_count=1
    )

    assert len(nodes) == 1
    assert nodes[0]["tabId"] == "t.selected"
    assert nodes[0]["startIndex"] == 99
    assert nodes[0]["table"]["tableRows"][0]["tableCells"][0]["content"][0][
        "startIndex"
    ] == 100
    assert "tabId" not in document["tabs"][1]["documentTab"]["body"]["content"][0]


def test_task8_phase_revision_chain_consumes_each_response_and_cleans_backup(
    tmp_path: Path,
) -> None:
    client = TablePhaseClient(
        revisions=["rev-2", "rev-3", "rev-4"],
        documents=[
            _task8_phase_document("rev-2", 100),
            _task8_phase_document("rev-3", 100),
        ],
    )
    recovery_root = tmp_path / "recovery"
    backup = client_module.make_recovery_backup(client, VALID_ID, recovery_root)

    final_revision = client_module._run_table_phases(
        client,
        document_id=VALID_ID,
        model=_task8_phase_model(),
        tab_id="t.selected",
        revision="rev-1",
        backup=backup,
    )

    assert final_revision == "rev-4"
    assert [revision for _, revision in client.batch_calls] == [
        "rev-1",
        "rev-2",
        "rev-3",
    ]
    assert [next(iter(request)) for request in client.batch_calls[0][0]] == [
        "deleteContentRange",
        "insertTable",
    ]
    assert client.batch_calls[1][0] == [
        {
            "insertText": {
                "location": {"index": 100, "tabId": "t.selected"},
                "text": "😀 x",
            }
        }
    ]
    assert [next(iter(request)) for request in client.batch_calls[2][0]] == [
        "updateTextStyle",
        "updateTextStyle",
    ]
    assert client.events[:3] == [
        "export:text/plain",
        (
            "export:application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        ),
        "batch:rev-1",
    ]
    assert recovery_root.is_dir()
    assert list(recovery_root.iterdir()) == []


def test_task8_phase_revision_stale_phase2_stops_phase3_and_retains_backup(
    tmp_path: Path,
) -> None:
    client = TablePhaseClient(
        revisions=["rev-2"],
        documents=[_task8_phase_document("rev-2", 100)],
        fail_on_batch=2,
    )
    recovery_root = tmp_path / "recovery"
    backup = client_module.make_recovery_backup(client, VALID_ID, recovery_root)

    with pytest.raises(DocsMCPError) as caught:
        client_module._run_table_phases(
            client,
            document_id=VALID_ID,
            model=_task8_phase_model(),
            tab_id="t.selected",
            revision="rev-1",
            backup=backup,
        )

    error = caught.value
    assert error.code == "partial_write_requires_recovery"
    result = error.as_result()
    assert result["error"]["phase"] == "table_cell_text"  # type: ignore[index]
    assert result["error"]["revision_id"] == "rev-2"  # type: ignore[index]
    assert result["error"]["recovery_action"] == (  # type: ignore[index]
        "Restore document.txt or document.docx from recovery_path, then delete that directory."
    )
    recovery_path = Path(result["error"]["recovery_path"])  # type: ignore[index]
    assert recovery_path.is_dir()
    assert sorted(path.name for path in recovery_path.iterdir()) == [
        "document.docx",
        "document.txt",
    ]
    assert len(client.batch_calls) == 2
    assert [revision for _, revision in client.batch_calls] == ["rev-1", "rev-2"]
    assert error.__context__ is None
    assert error.__cause__ is None
    assert_error_sanitized(
        error,
        "STALE_REVISION_CANARY",
        "RECOVERY_TEXT_CANARY",
        "RECOVERY_DOCX_CANARY",
    )


def test_task8_phase_aggregate_count_rejects_before_structure_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _task8_multi_table_model(width=3)
    client = TablePhaseClient(
        revisions=["rev-2", "rev-3"],
        documents=[
            _task8_multi_table_document("rev-2", 3),
            _task8_multi_table_document("rev-3", 3),
        ],
    )
    backup = client_module.make_recovery_backup(
        client, VALID_ID, tmp_path / "recovery"
    )
    monkeypatch.setattr(markdown_module, "_MAX_REPLACEMENT_REQUESTS", 4)

    with pytest.raises(DocsMCPError) as caught:
        client_module._run_table_phases(
            client,
            document_id=VALID_ID,
            model=model,
            tab_id="t.selected",
            revision="rev-1",
            backup=backup,
        )

    assert caught.value.code == "invalid_markdown"
    assert caught.value.message == "Markdown produces too many formatting requests."
    assert client.batch_calls == []
    assert backup.path.is_dir()


def test_task8_phase_aggregate_payload_rejects_before_cell_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _task8_multi_table_model(width=1)
    documents = [
        _task8_multi_table_document("rev-2", 1),
        _task8_multi_table_document("rev-3", 1),
    ]
    table_nodes = []
    for node in documents[0]["tabs"][0]["documentTab"]["body"]["content"]:
        enriched = dict(node)
        enriched["tabId"] = "t.selected"
        table_nodes.append(enriched)
    individual_plans = [
        markdown_module.table_cell_insert_requests(node, table.rows)
        for node, table in zip(table_nodes, model.tables, strict=True)
    ]
    cap = max(
        len(
            json.dumps(plan, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        for plan in individual_plans
    )

    class PayloadLimitClient(TablePhaseClient):
        def batch_update(
            self, document_id: str, requests_body: list[dict], revision: str
        ) -> dict:
            result = super().batch_update(document_id, requests_body, revision)
            if len(self.batch_calls) == 1:
                monkeypatch.setattr(
                    markdown_module, "_MAX_REPLACEMENT_PAYLOAD_BYTES", cap
                )
            return result

    client = PayloadLimitClient(
        revisions=["rev-2", "rev-3"], documents=documents
    )
    backup = client_module.make_recovery_backup(
        client, VALID_ID, tmp_path / "recovery"
    )

    with pytest.raises(DocsMCPError) as caught:
        client_module._run_table_phases(
            client,
            document_id=VALID_ID,
            model=model,
            tab_id="t.selected",
            revision="rev-1",
            backup=backup,
        )

    assert caught.value.code == "partial_write_requires_recovery"
    assert caught.value.as_result()["error"]["phase"] == "table_cell_text"  # type: ignore[index]
    assert len(client.batch_calls) == 1
    assert backup.path.is_dir()


def test_task8_phase_aggregate_style_count_rejects_before_structure_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _task8_multi_table_model(width=2, styled=True)
    client = TablePhaseClient(
        revisions=["rev-2", "rev-3", "rev-4"],
        documents=[
            _task8_multi_table_document("rev-2", 2),
            _task8_multi_table_document("rev-3", 2),
        ],
    )
    backup = client_module.make_recovery_backup(
        client, VALID_ID, tmp_path / "recovery"
    )
    monkeypatch.setattr(markdown_module, "_MAX_REPLACEMENT_REQUESTS", 4)

    with pytest.raises(DocsMCPError) as caught:
        client_module._run_table_phases(
            client,
            document_id=VALID_ID,
            model=model,
            tab_id="t.selected",
            revision="rev-1",
            backup=backup,
        )

    assert caught.value.code == "invalid_markdown"
    assert caught.value.message == "Markdown produces too many formatting requests."
    assert client.batch_calls == []
    assert backup.path.is_dir()


@pytest.mark.parametrize("profile", ["plain", "persian"])
def test_task8_phase_aggregate_style_payload_rejects_before_style_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    model = _task8_multi_table_model(width=1, styled=True)
    documents = [
        _task8_multi_table_document("rev-2", 1),
        _task8_multi_table_document("rev-3", 1),
    ]
    table_nodes = []
    for node in documents[1]["tabs"][0]["documentTab"]["body"]["content"]:
        enriched = dict(node)
        enriched["tabId"] = "t.selected"
        table_nodes.append(enriched)
    individual_plans = [
        markdown_module.table_cell_style_requests(node, table.rows, profile=profile)
        for node, table in zip(table_nodes, model.tables, strict=True)
    ]
    cap = max(
        len(
            json.dumps(plan, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        for plan in individual_plans
    )

    class StylePayloadLimitClient(TablePhaseClient):
        def batch_update(
            self, document_id: str, requests_body: list[dict], revision: str
        ) -> dict:
            result = super().batch_update(document_id, requests_body, revision)
            if len(self.batch_calls) == 2:
                monkeypatch.setattr(
                    markdown_module, "_MAX_REPLACEMENT_PAYLOAD_BYTES", cap
                )
            return result

    client = StylePayloadLimitClient(
        revisions=["rev-2", "rev-3", "rev-4"], documents=documents
    )
    backup = client_module.make_recovery_backup(
        client, VALID_ID, tmp_path / "recovery"
    )

    with pytest.raises(DocsMCPError) as caught:
        client_module._run_table_phases(
            client,
            document_id=VALID_ID,
            model=model,
            tab_id="t.selected",
            revision="rev-1",
            backup=backup,
            profile=profile,
        )

    assert caught.value.code == "partial_write_requires_recovery"
    assert caught.value.as_result()["error"]["phase"] == "table_cell_styles"  # type: ignore[index]
    assert len(client.batch_calls) == 2
    assert backup.path.is_dir()


def test_task8_phase_revision_no_tables_is_a_verified_noop(tmp_path: Path) -> None:
    client = TablePhaseClient(revisions=[], documents=[])
    model = DocumentModel("plain\n", (), (), (), ())
    recovery_root = tmp_path / "recovery"

    assert client_module._run_table_phases(
        client,
        document_id=VALID_ID,
        model=model,
        tab_id="t.selected",
        revision="rev-1",
        backup=None,
    ) == "rev-1"
    assert client.calls == []
    assert client.batch_calls == []
    assert not recovery_root.exists()


@pytest.mark.parametrize(
    "tamper",
    (
        "missing_directory",
        "missing_file",
        "mismatched_mapping",
        "permissive_root",
        "permissive_directory",
        "permissive_file",
        "symlink_file",
        "hardlinked_file",
        "wrong_owner",
    ),
)
def test_task8_phase_rejects_unusable_recovery_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    client = TablePhaseClient(
        revisions=["rev-2", "rev-3", "rev-4"],
        documents=[
            _task8_phase_document("rev-2", 100),
            _task8_phase_document("rev-3", 100),
        ],
    )
    case_root = tmp_path / tamper
    case_root.mkdir()
    backup = client_module.make_recovery_backup(
        client, VALID_ID, case_root / "recovery"
    )
    supplied_backup = backup
    outside = tmp_path / f"{tamper}-outside"

    if tamper == "missing_directory":
        backup.text_path.unlink()
        backup.docx_path.unlink()
        backup.path.rmdir()
    elif tamper == "missing_file":
        backup.text_path.unlink()
    elif tamper == "mismatched_mapping":
        outside.write_bytes(b"MISMATCHED_MAPPING_CANARY")
        outside.chmod(0o600)
        supplied_backup = client_module.RecoveryBackup(
            path=backup.path,
            text_path=outside,
            docx_path=backup.docx_path,
        )
    elif tamper == "permissive_root":
        backup.path.parent.chmod(0o755)
    elif tamper == "permissive_directory":
        backup.path.chmod(0o755)
    elif tamper == "permissive_file":
        backup.text_path.chmod(0o644)
    elif tamper == "symlink_file":
        outside.write_bytes(b"SYMLINK_TARGET_CANARY")
        outside.chmod(0o600)
        backup.text_path.unlink()
        backup.text_path.symlink_to(outside)
    elif tamper == "hardlinked_file":
        os.link(backup.text_path, outside)
    elif tamper == "wrong_owner":
        getuid = getattr(os, "getuid", None)
        if getuid is None:
            pytest.skip("POSIX ownership checks are unavailable")
        current_uid = getuid()
        monkeypatch.setattr(client_module.os, "getuid", lambda: current_uid + 1)
    else:
        raise AssertionError(f"unknown tamper case: {tamper}")

    with pytest.raises(DocsMCPError) as caught:
        client_module._run_table_phases(
            client,
            document_id=VALID_ID,
            model=_task8_phase_model(),
            tab_id="t.selected",
            revision="rev-1",
            backup=supplied_backup,
        )

    error = caught.value
    assert error.code == "google_unavailable"
    assert client.batch_calls == []
    assert error.__context__ is None
    assert error.__cause__ is None
    assert_error_sanitized(
        error,
        "RECOVERY_TEXT_CANARY",
        "RECOVERY_DOCX_CANARY",
        "MISMATCHED_MAPPING_CANARY",
        "SYMLINK_TARGET_CANARY",
    )


_TASK9_WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_TASK9_DOCX_CANARIES = (
    "DOCX_HEADING_CANARY",
    "DOCX_BODY_CANARY",
    "DOCX_TABLE_CANARY",
)


def _task9_docx_ppr(
    *, bidi: bool = True, alignment: str = "right", indent: bool = True
) -> str:
    values = []
    if bidi:
        values.append("<w:bidi/>")
    values.append(f'<w:jc w:val="{alignment}"/>')
    if indent:
        values.append('<w:ind w:right="0"/>')
    return "<w:pPr>" + "".join(values) + "</w:pPr>"


def _task9_docx_rpr(*, font: bool = True, bold: bool = False) -> str:
    values = []
    if font:
        values.append(
            '<w:rFonts w:ascii="Vazirmatn" w:hAnsi="Vazirmatn" '
            'w:cs="Vazirmatn"/>'
        )
    if bold:
        values.append("<w:b/>")
    return "<w:rPr>" + "".join(values) + "</w:rPr>"


def _task9_docx_bytes(
    *,
    body_bidi: bool = True,
    body_alignment: str = "right",
    body_indent: bool = True,
    body_font: bool = True,
    heading_bold: bool = True,
    document_xml: str | None = None,
    duplicate_document: bool = False,
    omit_styles: bool = False,
) -> bytes:
    base_ppr = _task9_docx_ppr()
    base_rpr = _task9_docx_rpr()
    heading_rpr = _task9_docx_rpr(font=False, bold=heading_bold)
    styles_xml = f"""
<w:styles xmlns:w="{_TASK9_WORD_NAMESPACE}">
  <w:style w:type="paragraph" w:styleId="BaseRTL">
    <w:name w:val="Base RTL"/>
    {base_ppr}
    {base_rpr}
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/>
    <w:basedOn w:val="BaseRTL"/>
    {heading_rpr}
  </w:style>
</w:styles>
""".strip()
    if document_xml is None:
        body_ppr = _task9_docx_ppr(
            bidi=body_bidi,
            alignment=body_alignment,
            indent=body_indent,
        )
        body_rpr = _task9_docx_rpr(font=body_font)
        cell_ppr = _task9_docx_ppr()
        cell_rpr = _task9_docx_rpr()
        document_xml = f"""
<w:document xmlns:w="{_TASK9_WORD_NAMESPACE}">
  <w:body>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>{_TASK9_DOCX_CANARIES[0]}</w:t></w:r>
    </w:p>
    <w:p>
      {body_ppr}
      <w:r>{body_rpr}<w:t>{_TASK9_DOCX_CANARIES[1]}</w:t></w:r>
    </w:p>
    <w:tbl><w:tr><w:tc>
      <w:p>
        {cell_ppr}
        <w:r>{cell_rpr}<w:t>{_TASK9_DOCX_CANARIES[2]}</w:t></w:r>
      </w:p>
    </w:tc></w:tr></w:tbl>
  </w:body>
</w:document>
""".strip()

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document_xml)
        if duplicate_document:
            archive.writestr("word/document.xml", document_xml)
        if not omit_styles:
            archive.writestr("word/styles.xml", styles_xml)
    return output.getvalue()


def test_task9_docx_verifier_has_exact_signature() -> None:
    verifier = client_module.verify_persian_docx
    parameters = tuple(inspect.signature(verifier).parameters.values())

    assert tuple(parameter.name for parameter in parameters) == ("data",)
    assert parameters[0].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert parameters[0].default is inspect.Parameter.empty
    assert parameters[0].annotation is bytes
    assert inspect.signature(verifier).return_annotation == dict[
        str, int | bool | list[str]
    ]


def test_task9_docx_effective_direct_and_inherited_formatting_passes() -> None:
    result = client_module.verify_persian_docx(_task9_docx_bytes())

    assert result == {
        "valid": True,
        "paragraphs": 3,
        "bidi_paragraphs": 3,
        "right_aligned_paragraphs": 3,
        "right_indented_paragraphs": 3,
        "text_runs": 3,
        "vazirmatn_runs": 3,
        "heading_runs": 1,
        "bold_heading_runs": 1,
        "reasons": [],
    }
    public_value = json.dumps(result, ensure_ascii=False, sort_keys=True)
    for canary in _TASK9_DOCX_CANARIES:
        assert canary not in public_value


@pytest.mark.parametrize(
    ("overrides", "reason", "count_field", "expected_count"),
    (
        (
            {"body_bidi": False},
            "paragraph_bidi_missing",
            "bidi_paragraphs",
            2,
        ),
        (
            {"body_alignment": "left"},
            "paragraph_right_alignment_missing",
            "right_aligned_paragraphs",
            2,
        ),
        (
            {"body_indent": False},
            "paragraph_right_indent_missing",
            "right_indented_paragraphs",
            2,
        ),
        (
            {"body_font": False},
            "run_vazirmatn_missing",
            "vazirmatn_runs",
            2,
        ),
        (
            {"heading_bold": False},
            "heading_bold_missing",
            "bold_heading_runs",
            0,
        ),
    ),
)
def test_task9_docx_independent_formatting_failures_are_counted(
    overrides: dict[str, object],
    reason: str,
    count_field: str,
    expected_count: int,
) -> None:
    result = client_module.verify_persian_docx(
        _task9_docx_bytes(**overrides)  # type: ignore[arg-type]
    )

    assert result["valid"] is False
    assert result["reasons"] == [reason]
    assert result[count_field] == expected_count
    assert len(result["reasons"]) <= 5


def test_task9_docx_verifier_calls_defusedxml_for_both_required_parts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parse_calls = 0
    original_fromstring = client_module.DefusedElementTree.fromstring

    def recording_fromstring(data: bytes):
        nonlocal parse_calls
        parse_calls += 1
        return original_fromstring(data)

    monkeypatch.setattr(
        client_module.DefusedElementTree, "fromstring", recording_fromstring
    )

    result = client_module.verify_persian_docx(_task9_docx_bytes())

    assert result["valid"] is True
    assert parse_calls == 2


def test_task9_docx_verifier_rejects_duplicate_members_without_echo() -> None:
    with pytest.warns(UserWarning, match="Duplicate name"):
        payload = _task9_docx_bytes(duplicate_document=True)

    error = assert_error_code(
        "verification_failed", client_module.verify_persian_docx, payload
    )

    assert_error_sanitized(error, *_TASK9_DOCX_CANARIES)


def test_task9_docx_verifier_rejects_oversized_input_and_xml_without_echo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _task9_docx_bytes()
    monkeypatch.setattr(client_module, "_MAX_DOCX_BYTES", len(payload) - 1, raising=False)

    input_error = assert_error_code(
        "verification_failed", client_module.verify_persian_docx, payload
    )
    assert_error_sanitized(input_error, *_TASK9_DOCX_CANARIES)

    monkeypatch.setattr(client_module, "_MAX_DOCX_BYTES", len(payload))
    monkeypatch.setattr(client_module, "_MAX_DOCX_XML_BYTES", 64, raising=False)
    xml_error = assert_error_code(
        "verification_failed", client_module.verify_persian_docx, payload
    )
    assert_error_sanitized(xml_error, *_TASK9_DOCX_CANARIES)

    monkeypatch.setattr(client_module, "_MAX_DOCX_XML_BYTES", 8_000_000)
    monkeypatch.setattr(client_module, "_MAX_DOCX_XML_NODES", 5, raising=False)
    node_error = assert_error_code(
        "verification_failed", client_module.verify_persian_docx, payload
    )
    assert_error_sanitized(node_error, *_TASK9_DOCX_CANARIES)


def test_task9_docx_verifier_rejects_entity_xml_without_echo() -> None:
    entity_canary = "DOCX_ENTITY_CANARY"
    document_xml = f"""
<!DOCTYPE w:document [<!ENTITY leaked "{entity_canary}">]>
<w:document xmlns:w="{_TASK9_WORD_NAMESPACE}">
  <w:body><w:p><w:r><w:t>&leaked;</w:t></w:r></w:p></w:body>
</w:document>
""".strip()
    payload = _task9_docx_bytes(document_xml=document_xml)

    error = assert_error_code(
        "verification_failed", client_module.verify_persian_docx, payload
    )

    assert_error_sanitized(error, entity_canary, *_TASK9_DOCX_CANARIES)


def test_task9_docx_verifier_rejects_missing_required_part() -> None:
    payload = _task9_docx_bytes(omit_styles=True)

    error = assert_error_code(
        "verification_failed", client_module.verify_persian_docx, payload
    )

    assert_error_sanitized(error, *_TASK9_DOCX_CANARIES)


def _task9_default_styles_docx_bytes() -> bytes:
    paragraph_properties = _task9_docx_ppr()
    run_properties = _task9_docx_rpr()
    styles_xml = f"""
<w:styles xmlns:w="{_TASK9_WORD_NAMESPACE}">
  <w:docDefaults>
    <w:pPrDefault><w:pPr>
      <w:bidi w:val="0"/><w:jc w:val="left"/><w:ind w:right="24"/>
    </w:pPr></w:pPrDefault>
    <w:rPrDefault><w:rPr>
      <w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:cs="Arial"/>
    </w:rPr></w:rPrDefault>
  </w:docDefaults>
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Normal"/>
    {paragraph_properties}
  </w:style>
  <w:style w:type="character" w:default="1" w:styleId="DefaultParagraphFont">
    <w:name w:val="Default Paragraph Font"/>
    {run_properties}
  </w:style>
</w:styles>
""".strip()
    document_xml = f"""
<w:document xmlns:w="{_TASK9_WORD_NAMESPACE}">
  <w:body>
    <w:p><w:r><w:t>DEFAULT_STYLE_DOCX_CANARY</w:t></w:r></w:p>
  </w:body>
</w:document>
""".strip()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document_xml)
        archive.writestr("word/styles.xml", styles_xml)
    return output.getvalue()


def test_task9_docx_uses_default_styles_before_docdefaults() -> None:
    result = client_module.verify_persian_docx(
        _task9_default_styles_docx_bytes()
    )

    assert result == {
        "valid": True,
        "paragraphs": 1,
        "bidi_paragraphs": 1,
        "right_aligned_paragraphs": 1,
        "right_indented_paragraphs": 1,
        "text_runs": 1,
        "vazirmatn_runs": 1,
        "heading_runs": 0,
        "bold_heading_runs": 0,
        "reasons": [],
    }
    assert "DEFAULT_STYLE_DOCX_CANARY" not in repr(result)


_TASK10_SELECTED_TAB = "t.selected"


def _task10_text_element(content: str, start: int) -> tuple[dict, int]:
    end = start + utf16_length(content)
    return {
        "startIndex": start,
        "endIndex": end,
        "textRun": {"content": content},
    }, end


def _task10_paragraph(parts: tuple[str, ...], start: int) -> dict:
    elements: list[dict] = []
    index = start
    for part in parts:
        element, index = _task10_text_element(part, index)
        elements.append(element)
    return {
        "startIndex": start,
        "endIndex": index,
        "paragraph": {"elements": elements},
    }


def _task10_document(
    revision: str,
    *,
    paragraph_parts: tuple[str, ...],
    table_text: str | None = None,
) -> dict:
    selected_content = [_task10_paragraph(paragraph_parts, 1)]
    if table_text is not None:
        cell_paragraph = _task10_paragraph((table_text,), 100)
        selected_content.append(
            {
                "startIndex": 99,
                "endIndex": cell_paragraph["endIndex"] + 1,
                "table": {
                    "tableRows": [
                        {"tableCells": [{"content": [cell_paragraph]}]}
                    ]
                },
            }
        )
    return {
        "documentId": VALID_ID,
        "revisionId": revision,
        "tabs": [
            {
                "tabProperties": {
                    "tabId": "t.other",
                    "title": "Other",
                    "index": 0,
                },
                "documentTab": {
                    "body": {
                        "content": [_task10_paragraph(("قدیم قدیم\n",), 1)]
                    }
                },
                "childTabs": [],
            },
            {
                "tabProperties": {
                    "tabId": _TASK10_SELECTED_TAB,
                    "title": "Selected",
                    "index": 1,
                },
                "documentTab": {"body": {"content": selected_content}},
                "childTabs": [],
            },
        ],
    }


def _task10_document_with_segments(
    revision: str,
    *,
    body_text: str,
    header_text: str | None = None,
    footer_text: str | None = None,
    footnote_text: str | None = None,
) -> dict:
    document = _task10_document(
        revision,
        paragraph_parts=(body_text,),
    )
    document_tab = document["tabs"][1]["documentTab"]
    for field, id_field, segment_id, text in (
        ("headers", "headerId", "header.1", header_text),
        ("footers", "footerId", "footer.1", footer_text),
        ("footnotes", "footnoteId", "footnote.1", footnote_text),
    ):
        if text is not None:
            document_tab[field] = {
                segment_id: {
                    id_field: segment_id,
                    "content": [_task10_paragraph((text,), 0)],
                }
            }
    return document


class Task10Client:
    def __init__(
        self,
        documents: list[dict],
        responses: list[dict] | None = None,
    ) -> None:
        self.documents = list(documents)
        self.responses = [] if responses is None else list(responses)
        self.get_calls: list[str] = []
        self.batch_calls: list[tuple[str, list[dict], str]] = []

    def get_document(self, document_id: str) -> dict:
        self.get_calls.append(document_id)
        if not self.documents:
            raise AssertionError("unexpected get_document")
        return self.documents.pop(0)

    def batch_update(
        self, document_id: str, requests: list[dict], revision: str
    ) -> dict:
        self.batch_calls.append((document_id, requests, revision))
        if not self.responses:
            raise AssertionError("unexpected batch_update")
        return self.responses.pop(0)


def _task10_before_document() -> dict:
    return _task10_document(
        "rev-1",
        paragraph_parts=("پیش 😀 ", "قد", "یم و جدید\n"),
        table_text="قدیم\n",
    )


def _task10_after_document() -> dict:
    return _task10_document(
        "rev-2",
        paragraph_parts=("پیش 😀 جدید و جدید\n",),
        table_text="جدید\n",
    )


def _task10_success_response(count: int = 2) -> dict:
    return {
        "replies": [
            {"replaceAllText": {"occurrencesChanged": count}}
        ],
        "writeControl": {"requiredRevisionId": "rev-2"},
    }


def test_task10_auxiliary_segment_match_mismatch_fails_before_write() -> None:
    before = _task10_document_with_segments(
        "rev-1",
        body_text="SEGMENT_OLD_CANARY\n",
        header_text="SEGMENT_OLD_CANARY\n",
    )
    response = {
        "replies": [
            {"replaceAllText": {"occurrencesChanged": 2}},
        ],
        "writeControl": {"requiredRevisionId": "rev-2"},
    }
    client = Task10Client([before], [response])
    replacement = client_module.Replacement(
        "SEGMENT_OLD_CANARY", "SEGMENT_NEW_CANARY", 1
    )

    error = assert_error_code(
        "match_count_mismatch",
        client_module.apply_edits,
        client,
        VALID_ID,
        [replacement],
        _TASK10_SELECTED_TAB,
        "rev-1",
    )

    assert client.get_calls == [VALID_ID]
    assert client.batch_calls == []
    assert_error_sanitized(
        error,
        "SEGMENT_OLD_CANARY",
        "SEGMENT_NEW_CANARY",
    )


def test_task10_preview_covers_every_selected_tab_text_segment() -> None:
    before = _task10_document_with_segments(
        "rev-1",
        body_text="SEGMENT_OLD_CANARY\n",
        header_text="SEGMENT_OLD_CANARY\n",
        footer_text="SEGMENT_OLD_CANARY\n",
        footnote_text="SEGMENT_OLD_CANARY\n",
    )
    client = Task10Client([before])
    replacement = client_module.Replacement(
        "SEGMENT_OLD_CANARY", "SEGMENT_NEW_CANARY", 4
    )

    result = client_module.preview_edits(
        client,
        VALID_ID,
        [replacement],
        _TASK10_SELECTED_TAB,
        "rev-1",
    )

    replacement_result = result["replacements"]
    assert isinstance(replacement_result, list)
    assert replacement_result == [
        {
            "index": 0,
            "expected_count": 4,
            "actual_count": 4,
            "preexisting_new_count": 0,
            "ranges": [
                {
                    "start_index": 1,
                    "end_index": 1 + utf16_length("SEGMENT_OLD_CANARY"),
                },
                {
                    "segment_id": "header.1",
                    "start_index": 0,
                    "end_index": utf16_length("SEGMENT_OLD_CANARY"),
                },
                {
                    "segment_id": "footer.1",
                    "start_index": 0,
                    "end_index": utf16_length("SEGMENT_OLD_CANARY"),
                },
                {
                    "segment_id": "footnote.1",
                    "start_index": 0,
                    "end_index": utf16_length("SEGMENT_OLD_CANARY"),
                },
            ],
        }
    ]
    assert client.batch_calls == []
    assert "SEGMENT_OLD_CANARY" not in repr(result)
    assert "SEGMENT_NEW_CANARY" not in repr(result)


def test_task10_preview_covers_auxiliary_segment_in_nested_selected_tab() -> None:
    document = _task10_document_with_segments(
        "rev-1",
        body_text="NESTED_OLD_CANARY\n",
        header_text="NESTED_OLD_CANARY\n",
    )
    selected_tab = document["tabs"].pop(1)
    selected_tab["tabProperties"]["parentTabId"] = "t.other"
    document["tabs"][0]["childTabs"] = [selected_tab]
    client = Task10Client([document])
    replacement = client_module.Replacement(
        "NESTED_OLD_CANARY", "NESTED_NEW_CANARY", 2
    )

    result = client_module.preview_edits(
        client,
        VALID_ID,
        [replacement],
        _TASK10_SELECTED_TAB,
        "rev-1",
    )

    replacements = result["replacements"]
    assert isinstance(replacements, list)
    assert replacements[0]["actual_count"] == 2
    assert replacements[0]["ranges"] == [
        {
            "start_index": 1,
            "end_index": 1 + utf16_length("NESTED_OLD_CANARY"),
        },
        {
            "segment_id": "header.1",
            "start_index": 0,
            "end_index": utf16_length("NESTED_OLD_CANARY"),
        },
    ]
    assert client.batch_calls == []


def test_task10_apply_verifies_every_selected_tab_text_segment() -> None:
    before = _task10_document_with_segments(
        "rev-1",
        body_text="SEGMENT_OLD_CANARY\n",
        header_text="SEGMENT_OLD_CANARY\n",
        footer_text="SEGMENT_OLD_CANARY\n",
        footnote_text="SEGMENT_OLD_CANARY\n",
    )
    after = _task10_document_with_segments(
        "rev-2",
        body_text="SEGMENT_NEW_CANARY\n",
        header_text="SEGMENT_NEW_CANARY\n",
        footer_text="SEGMENT_NEW_CANARY\n",
        footnote_text="SEGMENT_NEW_CANARY\n",
    )
    client = Task10Client([before, after], [_task10_success_response(4)])
    replacement = client_module.Replacement(
        "SEGMENT_OLD_CANARY", "SEGMENT_NEW_CANARY", 4
    )

    result = client_module.apply_edits(
        client,
        VALID_ID,
        [replacement],
        _TASK10_SELECTED_TAB,
        "rev-1",
    )

    assert result["verified"] is True
    assert result["replacements"] == [
        {
            "index": 0,
            "expected_count": 4,
            "occurrences_changed": 4,
            "before_old_count": 4,
            "before_new_count": 0,
            "after_old_count": 0,
            "after_new_count": 4,
        }
    ]
    assert len(client.batch_calls) == 1


@pytest.mark.parametrize(
    ("field", "malformed"),
    (
        ("headers", ["SEGMENT_MAP_CANARY"]),
        ("footers", {"footer.1": "SEGMENT_ENTRY_CANARY"}),
        (
            "footnotes",
            {
                "footnote.1": {
                    "footnoteId": "SEGMENT_ID_CANARY",
                    "content": [],
                }
            },
        ),
    ),
)
def test_task10_malformed_auxiliary_segment_map_fails_closed(
    field: str,
    malformed: object,
) -> None:
    before = _task10_document_with_segments(
        "rev-1",
        body_text="BODY_OLD_CANARY\n",
    )
    before["tabs"][1]["documentTab"][field] = malformed
    client = Task10Client([before])
    replacement = client_module.Replacement(
        "BODY_OLD_CANARY", "BODY_NEW_CANARY", 1
    )

    error = assert_error_code(
        "google_unavailable",
        client_module.preview_edits,
        client,
        VALID_ID,
        [replacement],
        _TASK10_SELECTED_TAB,
        "rev-1",
    )

    assert client.batch_calls == []
    assert_error_sanitized(
        error,
        "SEGMENT_MAP_CANARY",
        "SEGMENT_ENTRY_CANARY",
        "SEGMENT_ID_CANARY",
        "BODY_OLD_CANARY",
        "BODY_NEW_CANARY",
    )


def test_task10_replacement_is_frozen_and_request_is_exact_tab_scoped() -> None:
    replacement = client_module.Replacement("قدیم", "جدید", 1)

    with pytest.raises(FrozenInstanceError):
        replacement.new_text = "تغییر"  # type: ignore[misc]

    requests = client_module.edit_requests([replacement], "t.2")

    assert requests == [
        {
            "replaceAllText": {
                "replaceText": "جدید",
                "containsText": {
                    "text": "قدیم",
                    "matchCase": True,
                    "searchByRegex": False,
                },
                "tabsCriteria": {"tabIds": ["t.2"]},
            }
        }
    ]
    assert all(
        set(request) == {"replaceAllText"} for request in requests
    )


@pytest.mark.parametrize(
    "replacements",
    (
        [
            ("DUPLICATE_OLD_CANARY", "new-a", 1),
            ("DUPLICATE_OLD_CANARY", "new-b", 1),
        ],
        [
            ("SUBSTRING_OLD_CANARY", "new-a", 1),
            ("prefix-SUBSTRING_OLD_CANARY-suffix", "new-b", 1),
        ],
        [
            ("PRODUCED_OLD_CANARY", "new-a", 1),
            ("other-old", "prefix-PRODUCED_OLD_CANARY-suffix", 1),
        ],
    ),
)
def test_task10_interfering_replacements_are_rejected_without_echo(
    replacements: list[tuple[str, str, int]],
) -> None:
    values = [client_module.Replacement(*value) for value in replacements]

    error = assert_error_code(
        "overlapping_edits", client_module.validate_noninterference, values
    )

    assert_error_sanitized(
        error,
        "DUPLICATE_OLD_CANARY",
        "SUBSTRING_OLD_CANARY",
        "PRODUCED_OLD_CANARY",
    )


def test_task10_replacement_batch_is_bounded_to_one_hundred() -> None:
    replacements = [
        client_module.Replacement(f"old-{index}", f"new-{index}", 1)
        for index in range(101)
    ]

    assert_error_code(
        "overlapping_edits",
        client_module.validate_noninterference,
        replacements,
    )


def test_task10_preview_reports_utf16_ranges_and_performs_zero_writes() -> None:
    client = Task10Client([_task10_before_document()])
    prefix = "پیش 😀 "
    first_start = 1 + utf16_length(prefix)
    replacement = client_module.Replacement("قدیم", "جدید", 2)

    result = client_module.preview_edits(
        client,
        VALID_ID,
        [replacement],
        _TASK10_SELECTED_TAB,
        "rev-1",
    )

    assert result == {
        "document_id": VALID_ID,
        "revision_id": "rev-1",
        "tab_id": _TASK10_SELECTED_TAB,
        "valid": True,
        "replacements": [
            {
                "index": 0,
                "expected_count": 2,
                "actual_count": 2,
                "preexisting_new_count": 1,
                "ranges": [
                    {
                        "start_index": first_start,
                        "end_index": first_start + utf16_length("قدیم"),
                    },
                    {
                        "start_index": 100,
                        "end_index": 100 + utf16_length("قدیم"),
                    },
                ],
            }
        ],
    }
    assert client.get_calls == [VALID_ID]
    assert client.batch_calls == []
    assert "قدیم" not in repr(result)
    assert "جدید" not in repr(result)


def test_task10_preview_count_mismatch_performs_zero_writes() -> None:
    client = Task10Client([_task10_before_document()])
    replacement = client_module.Replacement("قدیم", "جدید", 1)

    error = assert_error_code(
        "match_count_mismatch",
        client_module.preview_edits,
        client,
        VALID_ID,
        [replacement],
        _TASK10_SELECTED_TAB,
        "rev-1",
    )

    assert client.get_calls == [VALID_ID]
    assert client.batch_calls == []
    assert_error_sanitized(error, "قدیم", "جدید")


def test_task10_apply_stale_revision_performs_zero_writes() -> None:
    client = Task10Client([_task10_before_document()])
    replacement = client_module.Replacement("قدیم", "جدید", 2)

    error = assert_error_code(
        "stale_revision",
        client_module.apply_edits,
        client,
        VALID_ID,
        [replacement],
        _TASK10_SELECTED_TAB,
        "rev-stale",
    )

    assert client.get_calls == [VALID_ID]
    assert client.batch_calls == []
    assert_error_sanitized(error, "rev-stale", "rev-1", "قدیم", "جدید")


def test_task10_apply_uses_one_batch_and_verifies_preexisting_new_count() -> None:
    client = Task10Client(
        [_task10_before_document(), _task10_after_document()],
        [_task10_success_response()],
    )
    replacement = client_module.Replacement("قدیم", "جدید", 2)
    expected_requests = client_module.edit_requests(
        [replacement], _TASK10_SELECTED_TAB
    )

    result = client_module.apply_edits(
        client,
        VALID_ID,
        [replacement],
        _TASK10_SELECTED_TAB,
        "rev-1",
    )

    assert client.get_calls == [VALID_ID, VALID_ID]
    assert client.batch_calls == [
        (VALID_ID, expected_requests, "rev-1")
    ]
    assert result == {
        "document_id": VALID_ID,
        "before_revision_id": "rev-1",
        "after_revision_id": "rev-2",
        "tab_id": _TASK10_SELECTED_TAB,
        "verified": True,
        "replacements": [
            {
                "index": 0,
                "expected_count": 2,
                "occurrences_changed": 2,
                "before_old_count": 2,
                "before_new_count": 1,
                "after_old_count": 0,
                "after_new_count": 3,
            }
        ],
    }
    assert "قدیم" not in repr(result)
    assert "جدید" not in repr(result)
    assert all(
        set(request) == {"replaceAllText"}
        for _, requests, _ in client.batch_calls
        for request in requests
    )


@pytest.mark.parametrize(
    "response",
    (
        {"writeControl": {"requiredRevisionId": "rev-2"}},
        {
            "replies": [{}],
            "writeControl": {"requiredRevisionId": "rev-2"},
        },
        {
            "replies": [
                {"replaceAllText": {"occurrencesChanged": 1}}
            ],
            "writeControl": {"requiredRevisionId": "rev-2"},
        },
        {
            "replies": [
                {"replaceAllText": {"occurrencesChanged": True}}
            ],
            "writeControl": {"requiredRevisionId": "rev-2"},
        },
    ),
)
def test_task10_apply_rejects_missing_or_wrong_occurrence_response(
    response: dict,
) -> None:
    client = Task10Client([_task10_before_document()], [response])
    replacement = client_module.Replacement("قدیم", "جدید", 2)

    error = assert_error_code(
        "verification_failed",
        client_module.apply_edits,
        client,
        VALID_ID,
        [replacement],
        _TASK10_SELECTED_TAB,
        "rev-1",
    )

    assert client.get_calls == [VALID_ID]
    assert len(client.batch_calls) == 1
    assert_error_sanitized(error, "قدیم", "جدید")


def test_task10_apply_response_order_must_match_request_order() -> None:
    before = _task10_document(
        "rev-1",
        paragraph_parts=("first second second\n",),
    )
    response = {
        "replies": [
            {"replaceAllText": {"occurrencesChanged": 2}},
            {"replaceAllText": {"occurrencesChanged": 1}},
        ],
        "writeControl": {"requiredRevisionId": "rev-2"},
    }
    client = Task10Client([before], [response])
    replacements = [
        client_module.Replacement("first", "one", 1),
        client_module.Replacement("second", "two", 2),
    ]

    assert_error_code(
        "verification_failed",
        client_module.apply_edits,
        client,
        VALID_ID,
        replacements,
        _TASK10_SELECTED_TAB,
        "rev-1",
    )

    assert len(client.batch_calls) == 1
    assert client.get_calls == [VALID_ID]


@pytest.mark.parametrize(
    "after",
    (
        _task10_document(
            "rev-2",
            paragraph_parts=("پیش 😀 قدیم و جدید\n",),
            table_text="جدید\n",
        ),
        _task10_document(
            "rev-2",
            paragraph_parts=("پیش 😀 جدید و جدید\n",),
            table_text="بدون هدف\n",
        ),
        _task10_document(
            "rev-concurrent",
            paragraph_parts=("پیش 😀 جدید و جدید\n",),
            table_text="جدید\n",
        ),
    ),
)
def test_task10_apply_rejects_old_new_or_revision_readback_mismatch(
    after: dict,
) -> None:
    client = Task10Client(
        [_task10_before_document(), after],
        [_task10_success_response()],
    )
    replacement = client_module.Replacement("قدیم", "جدید", 2)

    error = assert_error_code(
        "verification_failed",
        client_module.apply_edits,
        client,
        VALID_ID,
        [replacement],
        _TASK10_SELECTED_TAB,
        "rev-1",
    )

    assert client.get_calls == [VALID_ID, VALID_ID]
    assert len(client.batch_calls) == 1
    assert_error_sanitized(error, "قدیم", "جدید", "rev-concurrent")


def test_task10_apply_requires_response_revision_to_advance() -> None:
    unchanged_revision_after = _task10_document(
        "rev-1",
        paragraph_parts=("پیش 😀 جدید و جدید\n",),
        table_text="جدید\n",
    )
    response = _task10_success_response()
    response["writeControl"]["requiredRevisionId"] = "rev-1"
    client = Task10Client(
        [_task10_before_document(), unchanged_revision_after],
        [response],
    )
    replacement = client_module.Replacement("قدیم", "جدید", 2)

    error = assert_error_code(
        "verification_failed",
        client_module.apply_edits,
        client,
        VALID_ID,
        [replacement],
        _TASK10_SELECTED_TAB,
        "rev-1",
    )

    assert client.get_calls == [VALID_ID]
    assert len(client.batch_calls) == 1
    assert_error_sanitized(error, "rev-1", "قدیم", "جدید")


def test_task10_apply_empty_new_text_verifies_deletion_without_empty_matches() -> None:
    before = _task10_document(
        "rev-1",
        paragraph_parts=("remove remove\n",),
    )
    after = _task10_document("rev-2", paragraph_parts=(" \n",))
    client = Task10Client(
        [before, after],
        [_task10_success_response()],
    )
    replacement = client_module.Replacement("remove", "", 2)

    result = client_module.apply_edits(
        client,
        VALID_ID,
        [replacement],
        _TASK10_SELECTED_TAB,
        "rev-1",
    )

    assert result["verified"] is True
    assert result["replacements"] == [
        {
            "index": 0,
            "expected_count": 2,
            "occurrences_changed": 2,
            "before_old_count": 2,
            "before_new_count": 0,
            "after_old_count": 0,
            "after_new_count": 0,
        }
    ]


def test_task10_multi_tab_without_selected_tab_fails_before_write() -> None:
    client = Task10Client([_task10_before_document()])
    replacement = client_module.Replacement("قدیم", "جدید", 2)

    assert_error_code(
        "multiple_tabs_require_tab_id",
        client_module.apply_edits,
        client,
        VALID_ID,
        [replacement],
        None,
        "rev-1",
    )

    assert client.get_calls == [VALID_ID]
    assert client.batch_calls == []


def _task10_document_with_selected_content(
    content: list[dict], revision: str = "rev-1"
) -> dict:
    document = _task10_document(
        revision,
        paragraph_parts=("placeholder\n",),
    )
    document["tabs"][1]["documentTab"]["body"]["content"] = content
    return document


def test_task10_contextual_batch_interference_fails_before_write() -> None:
    before = _task10_document(
        "rev-1",
        paragraph_parts=(
            "PREFIX_CANARY_SUFFIX JOIN_CANARY_SUFFIX\n",
        ),
    )
    response = {
        "replies": [
            {"replaceAllText": {"occurrencesChanged": 1}},
            {"replaceAllText": {"occurrencesChanged": 2}},
        ],
        "writeControl": {"requiredRevisionId": "rev-2"},
    }
    client = Task10Client([before], [response])
    replacements = [
        client_module.Replacement(
            "PREFIX_CANARY", "JOIN_CANARY", 1
        ),
        client_module.Replacement(
            "JOIN_CANARY_SUFFIX", "FINAL_CANARY", 1
        ),
    ]

    error = assert_error_code(
        "overlapping_edits",
        client_module.apply_edits,
        client,
        VALID_ID,
        replacements,
        _TASK10_SELECTED_TAB,
        "rev-1",
    )

    assert client.get_calls == [VALID_ID]
    assert client.batch_calls == []
    assert_error_sanitized(
        error,
        "PREFIX_CANARY_SUFFIX JOIN_CANARY_SUFFIX",
        "PREFIX_CANARY",
        "JOIN_CANARY",
        "JOIN_CANARY_SUFFIX",
        "FINAL_CANARY",
    )


@pytest.mark.parametrize(
    ("before_text", "after_text", "replacement_values", "after_new_counts"),
    (
        (
            "foobar\n",
            "foo\n",
            (("foobar", "foo", 1),),
            [1],
        ),
        (
            "A B\n",
            "foobar foo\n",
            (("A", "foobar", 1), ("B", "foo", 1)),
            [1, 2],
        ),
    ),
)
def test_task10_readback_uses_exact_simulated_new_counts(
    before_text: str,
    after_text: str,
    replacement_values: tuple[tuple[str, str, int], ...],
    after_new_counts: list[int],
) -> None:
    before = _task10_document("rev-1", paragraph_parts=(before_text,))
    after = _task10_document("rev-2", paragraph_parts=(after_text,))
    replacements = [
        client_module.Replacement(*value) for value in replacement_values
    ]
    response = {
        "replies": [
            {
                "replaceAllText": {
                    "occurrencesChanged": replacement.expected_count
                }
            }
            for replacement in replacements
        ],
        "writeControl": {"requiredRevisionId": "rev-2"},
    }
    client = Task10Client([before, after], [response])

    result = client_module.apply_edits(
        client,
        VALID_ID,
        replacements,
        _TASK10_SELECTED_TAB,
        "rev-1",
    )

    assert result["verified"] is True
    result_replacements = result["replacements"]
    assert isinstance(result_replacements, list)
    assert [
        entry["after_new_count"] for entry in result_replacements
    ] == after_new_counts
    assert len(client.batch_calls) == 1


def test_task10_successful_batch_preserves_literal_caller_order() -> None:
    before = _task10_document(
        "rev-1",
        paragraph_parts=("first second second\n",),
    )
    after = _task10_document(
        "rev-2",
        paragraph_parts=("one two two\n",),
    )
    response = {
        "replies": [
            {"replaceAllText": {"occurrencesChanged": 1}},
            {"replaceAllText": {"occurrencesChanged": 2}},
        ],
        "writeControl": {"requiredRevisionId": "rev-2"},
    }
    client = Task10Client([before, after], [response])
    replacements = [
        client_module.Replacement("first", "one", 1),
        client_module.Replacement("second", "two", 2),
    ]

    result = client_module.apply_edits(
        client,
        VALID_ID,
        replacements,
        _TASK10_SELECTED_TAB,
        "rev-1",
    )

    assert client.batch_calls == [
        (
            VALID_ID,
            [
                {
                    "replaceAllText": {
                        "replaceText": "one",
                        "containsText": {
                            "text": "first",
                            "matchCase": True,
                            "searchByRegex": False,
                        },
                        "tabsCriteria": {
                            "tabIds": [_TASK10_SELECTED_TAB]
                        },
                    }
                },
                {
                    "replaceAllText": {
                        "replaceText": "two",
                        "containsText": {
                            "text": "second",
                            "matchCase": True,
                            "searchByRegex": False,
                        },
                        "tabsCriteria": {
                            "tabIds": [_TASK10_SELECTED_TAB]
                        },
                    }
                },
            ],
            "rev-1",
        )
    ]
    result_replacements = result["replacements"]
    assert isinstance(result_replacements, list)
    assert [
        (entry["index"], entry["occurrences_changed"])
        for entry in result_replacements
    ] == [(0, 1), (1, 2)]


def _task10_boundary_contents() -> tuple[tuple[str, list[dict]], ...]:
    gap_left, _ = _task10_text_element("ab", 1)
    gap_right, _ = _task10_text_element("cd", 4)

    object_left, _ = _task10_text_element("ab", 1)
    object_right, _ = _task10_text_element("cd", 4)
    inline_object = {
        "startIndex": 3,
        "endIndex": 4,
        "inlineObjectElement": {"inlineObjectId": "object-boundary"},
    }

    paragraph_boundary = [
        _task10_paragraph(("ab",), 1),
        _task10_paragraph(("cd",), 3),
    ]
    table_boundary = [
        {
            "startIndex": 99,
            "endIndex": 105,
            "table": {
                "tableRows": [
                    {
                        "tableCells": [
                            {"content": [_task10_paragraph(("ab",), 100)]},
                            {"content": [_task10_paragraph(("cd",), 102)]},
                        ]
                    }
                ]
            },
        }
    ]
    return (
        (
            "index_gap",
            [{"paragraph": {"elements": [gap_left, gap_right]}}],
        ),
        (
            "inline_object",
            [
                {
                    "paragraph": {
                        "elements": [
                            object_left,
                            inline_object,
                            object_right,
                        ]
                    }
                }
            ],
        ),
        ("paragraph", paragraph_boundary),
        ("table_cell", table_boundary),
    )


@pytest.mark.parametrize(("boundary", "content"), _task10_boundary_contents())
def test_task10_preview_does_not_match_across_structural_boundary(
    boundary: str,
    content: list[dict],
) -> None:
    client = Task10Client([_task10_document_with_selected_content(content)])
    replacement = client_module.Replacement("abcd", "changed", 1)

    error = assert_error_code(
        "match_count_mismatch",
        client_module.preview_edits,
        client,
        VALID_ID,
        [replacement],
        _TASK10_SELECTED_TAB,
        "rev-1",
    )

    assert boundary in {"index_gap", "inline_object", "paragraph", "table_cell"}
    assert client.batch_calls == []
    assert_error_sanitized(error, "abcd", "changed", "object-boundary")


@pytest.mark.parametrize("shape", ("wide", "deep", "oversized", "malformed"))
def test_task10_edit_body_limits_fail_closed_before_write(
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    if shape == "wide":
        monkeypatch.setattr(client_module, "_MAX_RENDER_NODES", 3)
        content = [
            _task10_paragraph(("NODE_LIMIT_CANARY\n",), 1),
            _task10_paragraph(("second\n",), 30),
        ]
    elif shape == "deep":
        monkeypatch.setattr(client_module, "_MAX_RENDER_NODES", 5)
        nested: dict = _task10_paragraph(("DEEP_LIMIT_CANARY\n",), 100)
        for _ in range(10):
            nested = {
                "table": {
                    "tableRows": [
                        {"tableCells": [{"content": [nested]}]}
                    ]
                }
            }
        content = [nested]
    elif shape == "oversized":
        monkeypatch.setattr(client_module, "_MAX_RENDER_CHARS", 5)
        content = [_task10_paragraph(("CHAR_LIMIT_CANARY\n",), 1)]
    else:
        content = [{"paragraph": {"elements": ["MALFORMED_CANARY"]}}]

    client = Task10Client([_task10_document_with_selected_content(content)])
    replacement = client_module.Replacement("missing", "changed", 1)
    error = assert_error_code(
        "google_unavailable",
        client_module.apply_edits,
        client,
        VALID_ID,
        [replacement],
        _TASK10_SELECTED_TAB,
        "rev-1",
    )

    assert client.get_calls == [VALID_ID]
    assert client.batch_calls == []
    assert_error_sanitized(
        error,
        "NODE_LIMIT_CANARY",
        "DEEP_LIMIT_CANARY",
        "CHAR_LIMIT_CANARY",
        "MALFORMED_CANARY",
        "missing",
        "changed",
    )


def test_task10_oversized_run_is_rejected_before_utf16_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded: list[str] = []
    original_utf16_length = client_module.utf16_length

    def guarded_utf16_length(value: str) -> int:
        if len(value) > 5:
            encoded.append("OVERSIZED_UTF16_CANARY")
        return original_utf16_length(value)

    before = _task10_document(
        "rev-1",
        paragraph_parts=("OVERSIZED_RUN_CANARY\n",),
    )
    client = Task10Client([before])
    replacement = client_module.Replacement(
        "ABSENT_OLD_CANARY", "ABSENT_NEW_CANARY", 1
    )
    monkeypatch.setattr(client_module, "_MAX_RENDER_CHARS", 5)
    monkeypatch.setattr(client_module, "utf16_length", guarded_utf16_length)

    error = assert_error_code(
        "google_unavailable",
        client_module.preview_edits,
        client,
        VALID_ID,
        [replacement],
        _TASK10_SELECTED_TAB,
        "rev-1",
    )

    assert encoded == []
    assert client.get_calls == [VALID_ID]
    assert client.batch_calls == []
    assert_error_sanitized(
        error,
        "OVERSIZED_UTF16_CANARY",
        "OVERSIZED_RUN_CANARY",
        "ABSENT_OLD_CANARY",
        "ABSENT_NEW_CANARY",
    )


@pytest.mark.parametrize("sequence_kind", ("body", "elements", "rows", "cells"))
def test_task10_wide_sequences_are_consumed_only_to_the_node_limit(
    monkeypatch: pytest.MonkeyPatch,
    sequence_kind: str,
) -> None:
    class TrackingList(list):
        def __init__(self, values):
            super().__init__(values)
            self.accesses = 0
            self.bulk_calls = 0
            self.reversed_calls = 0

        def _item(self, index):
            self.accesses += 1
            if self.accesses > 8:
                raise AssertionError("WIDE_ACCESS_CANARY")
            return list.__getitem__(self, index)

        def __getitem__(self, index):
            if isinstance(index, slice):
                self.bulk_calls += 1
                return list.__getitem__(self, index)
            return self._item(index)

        def __iter__(self):
            for index in range(len(self)):
                yield self._item(index)

        def __reversed__(self):
            self.reversed_calls += 1
            for index in range(len(self) - 1, -1, -1):
                yield self._item(index)

    paragraph = _task10_paragraph(("wide\n",), 1)
    if sequence_kind == "body":
        tracked = TrackingList([paragraph] * 200)
        content = tracked
    elif sequence_kind == "elements":
        elements = [
            _task10_text_element("w", index)[0]
            for index in range(1, 201)
        ]
        tracked = TrackingList(elements)
        content = [{"paragraph": {"elements": tracked}}]
    elif sequence_kind == "rows":
        row = {"tableCells": [{"content": [paragraph]}]}
        tracked = TrackingList([row] * 200)
        content = [{"table": {"tableRows": tracked}}]
    else:
        cell = {"content": [paragraph]}
        tracked = TrackingList([cell] * 200)
        content = [
            {"table": {"tableRows": [{"tableCells": tracked}]}}
        ]

    before = _task10_document_with_selected_content(content)
    client = Task10Client([before])
    replacement = client_module.Replacement(
        "WIDE_OLD_CANARY", "WIDE_NEW_CANARY", 1
    )
    monkeypatch.setattr(client_module, "_MAX_RENDER_NODES", 3)

    error = assert_error_code(
        "google_unavailable",
        client_module.apply_edits,
        client,
        VALID_ID,
        [replacement],
        _TASK10_SELECTED_TAB,
        "rev-1",
    )

    assert tracked.accesses <= 3
    assert tracked.bulk_calls == 0
    assert tracked.reversed_calls == 0
    assert client.get_calls == [VALID_ID]
    assert client.batch_calls == []
    assert_error_sanitized(
        error,
        "WIDE_ACCESS_CANARY",
        "WIDE_OLD_CANARY",
        "WIDE_NEW_CANARY",
    )


def test_task10_simulated_expansion_is_bounded_before_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replace_calls: list[tuple[str, str, int]] = []

    class TrackingText(str):
        def replace(
            self,
            old: str,
            new: str,
            count: SupportsIndex = -1,
            /,
        ) -> str:
            replace_calls.append((old, new, int(count)))
            return super().replace(old, new, count)

    original_segment = client_module._EditSegment

    def tracking_segment(*args: Any, **kwargs: Any):
        segment = original_segment(*args, **kwargs)
        object.__setattr__(segment, "text", TrackingText(segment.text))
        return segment

    monkeypatch.setattr(client_module, "_EditSegment", tracking_segment)
    monkeypatch.setattr(client_module, "_MAX_RENDER_CHARS", 10)
    before = _task10_document("rev-1", paragraph_parts=("a\n",))
    after = _task10_document(
        "rev-2",
        paragraph_parts=("EXPANSION_CANARY\n",),
    )
    response = {
        "replies": [
            {"replaceAllText": {"occurrencesChanged": 1}},
        ],
        "writeControl": {"requiredRevisionId": "rev-2"},
    }
    client = Task10Client([before, after], [response])
    replacement = client_module.Replacement(
        "a", "EXPANSION_CANARY", 1
    )

    error = assert_error_code(
        "overlapping_edits",
        client_module.apply_edits,
        client,
        VALID_ID,
        [replacement],
        _TASK10_SELECTED_TAB,
        "rev-1",
    )

    assert client.get_calls == [VALID_ID]
    assert client.batch_calls == []
    assert replace_calls == []
    assert_error_sanitized(error, "EXPANSION_CANARY")


def test_task11_service_has_exact_public_api() -> None:
    service_type = client_module.GoogleDocsService
    public_methods = {
        name
        for name, value in vars(service_type).items()
        if not name.startswith("_") and callable(value)
    }

    assert public_methods == {"read", "create", "replace_markdown", "edit_text"}
    assert tuple(inspect.signature(service_type).parameters) == (
        "client",
        "recovery_root",
    )
    expected_parameters = {
        "read": ("self", "document", "tab_id", "start", "max_chars"),
        "create": ("self", "title", "markdown", "format_profile"),
        "replace_markdown": (
            "self",
            "document",
            "markdown",
            "expected_revision_id",
            "tab_id",
            "format_profile",
        ),
        "edit_text": (
            "self",
            "document",
            "replacements",
            "expected_revision_id",
            "tab_id",
            "apply",
        ),
    }
    for name, parameters in expected_parameters.items():
        assert tuple(inspect.signature(getattr(service_type, name)).parameters) == parameters
    read_parameters = inspect.signature(service_type.read).parameters
    assert read_parameters["tab_id"].default is None
    assert read_parameters["start"].default == 0
    assert read_parameters["max_chars"].default == 30_000
    create_parameters = inspect.signature(service_type.create).parameters
    assert create_parameters["markdown"].default == ""
    assert create_parameters["format_profile"].default == "persian"
    replace_parameters = inspect.signature(service_type.replace_markdown).parameters
    assert (
        replace_parameters["expected_revision_id"].default
        is inspect.Parameter.empty
    )
    assert replace_parameters["tab_id"].default is None
    assert replace_parameters["format_profile"].default == "persian"
    edit_parameters = inspect.signature(service_type.edit_text).parameters
    assert edit_parameters["expected_revision_id"].default is inspect.Parameter.empty
    assert edit_parameters["tab_id"].default is None
    assert edit_parameters["apply"].default is False


def test_task10_actual_match_overflow_stops_at_bounded_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    original_utf16_index = client_module.utf16_index

    def counted_utf16_index(value: str, index: int) -> int:
        nonlocal calls
        calls += 1
        return original_utf16_index(value, index)

    monkeypatch.setattr(client_module, "utf16_index", counted_utf16_index)
    before = _task10_document(
        "rev-1",
        paragraph_parts=("x" * 200 + "\n",),
    )
    client = Task10Client([before])
    replacement = client_module.Replacement("x", "y", 100)

    assert_error_code(
        "match_count_mismatch",
        client_module.apply_edits,
        client,
        VALID_ID,
        [replacement],
        _TASK10_SELECTED_TAB,
        "rev-1",
    )

    assert calls == 202
    assert client.batch_calls == []


_TASK11_NATIVE_MIME = "application/vnd.google-apps.document"
_TASK11_DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_TASK11_TEXT_MIME = "text/plain"
_TASK11_URL = f"https://docs.google.com/document/d/{VALID_ID}/edit"


def _task11_metadata(*, mime_type: str = _TASK11_NATIVE_MIME) -> dict:
    return {
        "id": VALID_ID,
        "name": "Task 11 document",
        "mimeType": mime_type,
        "modifiedTime": "2026-09-02T11:30:00.000Z",
        "version": "17",
        "webViewLink": _TASK11_URL,
    }


def _task11_document(
    revision: str,
    text: str,
    *,
    tab_id: str = "t.selected",
    title: str = "Selected",
) -> dict:
    return {
        "documentId": VALID_ID,
        "title": "Task 11 document",
        "revisionId": revision,
        "tabs": [
            {
                "tabProperties": {"tabId": tab_id, "title": title},
                "documentTab": {
                    "body": {"content": [_task10_paragraph((text,), 1)]}
                },
                "childTabs": [],
            }
        ],
    }


def _task11_with_persian_api_styles(document: dict) -> dict:
    """Opt explicit positive/DOCX fixtures into valid API formatting."""
    def apply(value: object) -> None:
        if isinstance(value, dict):
            if "paragraph" in value:
                paragraph = value["paragraph"]
                paragraph.setdefault("paragraphStyle", {}).update({
                    "direction": "RIGHT_TO_LEFT",
                    "alignment": "END",
                    "indentStart": {"magnitude": 0, "unit": "PT"},
                    "indentEnd": {"magnitude": 0, "unit": "PT"},
                })
                for element in paragraph["elements"]:
                    if "textRun" in element:
                        element["textRun"].setdefault("textStyle", {}).update({
                            "weightedFontFamily": {"fontFamily": "Vazirmatn"},
                        })
            for nested in value.values():
                apply(nested)
        elif isinstance(value, list):
            for nested in value:
                apply(nested)

    apply(document)
    return document


class Task11Client:
    def __init__(
        self,
        *,
        metadata: dict | None = None,
        documents: list[dict] | None = None,
        batch_revisions: list[str] | None = None,
        exports: dict[str, list[bytes]] | None = None,
    ) -> None:
        self.metadata = _task11_metadata() if metadata is None else metadata
        self.documents = [] if documents is None else list(documents)
        self.batch_revisions = (
            [] if batch_revisions is None else list(batch_revisions)
        )
        self.exports = (
            {} if exports is None else {key: list(value) for key, value in exports.items()}
        )
        self.events: list[str] = []
        self.create_calls: list[str] = []
        self.batch_calls: list[tuple[str, list[dict], str]] = []
        self.export_calls: list[tuple[str, str]] = []
        self.delete_calls: list[str] = []

    def create_document(self, title: str) -> str:
        self.events.append("create")
        self.create_calls.append(title)
        return VALID_ID

    def drive_metadata(self, document_id: str) -> dict:
        assert document_id == VALID_ID
        self.events.append("metadata")
        return dict(self.metadata)

    def get_document(self, document_id: str) -> dict:
        assert document_id == VALID_ID
        self.events.append("get_document")
        if not self.documents:
            raise AssertionError("TASK11_UNEXPECTED_DOCUMENT_READ")
        return self.documents.pop(0)

    def batch_update(
        self, document_id: str, requests_body: list[dict], revision: str
    ) -> dict:
        assert document_id == VALID_ID
        self.events.append("batch_update")
        self.batch_calls.append((document_id, requests_body, revision))
        if not self.batch_revisions:
            raise AssertionError("TASK11_UNEXPECTED_BATCH")
        return {
            "writeControl": {
                "requiredRevisionId": self.batch_revisions.pop(0)
            }
        }

    def export_file(self, document_id: str, mime_type: str) -> bytes:
        assert document_id == VALID_ID
        self.events.append(f"export:{mime_type}")
        self.export_calls.append((document_id, mime_type))
        payloads = self.exports.get(mime_type, [])
        if not payloads:
            raise AssertionError("TASK11_UNEXPECTED_EXPORT")
        return payloads.pop(0)

    def delete_file(self, document_id: str) -> None:
        assert document_id == VALID_ID
        self.events.append("delete")
        self.delete_calls.append(document_id)


def _task11_service(client: Task11Client, recovery_root: Path):
    return client_module.GoogleDocsService(
        client, recovery_root  # type: ignore[arg-type]
    )


class Task11EditClient(Task10Client):
    def drive_metadata(self, document_id: str) -> dict:
        assert document_id == VALID_ID
        return _task11_metadata()


@pytest.mark.parametrize("operation", ["create", "replace"])
@pytest.mark.parametrize("profile", ["persian", "plain"])
def test_task14_service_propagates_profile_to_heading_semantics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, profile: str) -> None:
    after = _task11_document("rev-2", "عنوان 😀\n")
    paragraph = after["tabs"][0]["documentTab"]["body"]["content"][0]["paragraph"]
    paragraph["paragraphStyle"] = {"namedStyleType": "HEADING_2"}
    for element in paragraph["elements"]:
        element["textRun"]["textStyle"] = {"bold": profile == "persian"}
    if profile == "persian":
        _task11_with_persian_api_styles(after)
    before = _task11_document("rev-1", "\n")
    client = Task11Client(documents=([before] if operation == "create" else []) + [before, after], batch_revisions=["rev-2"], exports={_TASK11_DOCX_MIME: [_task9_docx_bytes()]})
    service = _task11_service(client, tmp_path / "recovery")
    # A required keyword proves both preflight and readback pass profile explicitly.
    original = markdown_module.candidate_semantic
    profiles = []
    def candidate(model: DocumentModel, *, profile: str) -> dict:
        profiles.append(profile)
        return original(model, profile=profile)
    monkeypatch.setattr(markdown_module, "candidate_semantic", candidate)
    if operation == "create":
        result = service.create("Heading", "## عنوان 😀", profile)
    else:
        result = service.replace_markdown(VALID_ID, "## عنوان 😀", "rev-1", format_profile=profile)
    assert result["verified"] is True
    assert profiles == [profile] * (3 if operation == "create" else 2)
    assert len(client.batch_calls) == 1
    assert client.batch_calls[0][2] == "rev-1"
    assert result["semantic"]["sha256"] == markdown_module.semantic_sha256(markdown_module.remote_semantic(after["tabs"][0]["documentTab"]["body"]))


@pytest.mark.parametrize("operation", ["create", "replace"])
@pytest.mark.parametrize("overflow", ["cell_profiles", "structure", "initial"])
def test_task14_service_preflights_counts_before_first_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, overflow: str) -> None:
    client = Task11Client(documents=[_task11_document("rev-1", "\n")])
    service = _task11_service(client, tmp_path / "recovery")
    if overflow == "cell_profiles":
        model = _task8_multi_table_model(width=2, include_text=False)
        cap = 4  # Each table fits; aggregate 8 profile requests do not.
    elif overflow == "structure":
        model = _task8_multi_table_model(width=1, include_text=False)
        cap = 3
    else:
        model = markdown_module.parse_markdown("## Heading")
        cap = 4
    monkeypatch.setattr(markdown_module, "parse_markdown", lambda _: model)
    monkeypatch.setattr(markdown_module, "_MAX_REPLACEMENT_REQUESTS", cap)
    if operation == "create":
        assert_error_code("invalid_markdown", service.create, "Bounded", "input", "persian")
    else:
        assert_error_code("invalid_markdown", service.replace_markdown, VALID_ID, "input", "rev-1", None, "persian")
    assert client.create_calls == []
    assert client.batch_calls == []
    assert client.export_calls == []


@pytest.mark.parametrize("profile", ["persian", "plain"])
def test_task14_table_phases_use_fresh_ranges_and_profile(tmp_path: Path, profile: str) -> None:
    client = TablePhaseClient(revisions=["rev-2", "rev-3", "rev-4"], documents=[_task8_phase_document("rev-2", 100), _task8_phase_document("rev-3", 137)])
    backup = client_module.make_recovery_backup(client, VALID_ID, tmp_path / "recovery")
    revision = client_module._run_table_phases(client, document_id=VALID_ID, model=_task8_phase_model(), tab_id="t.selected", revision="rev-1", backup=backup, cleanup_backup=False, profile=profile)
    assert revision == "rev-4"
    assert [r for _, r in client.batch_calls] == ["rev-1", "rev-2", "rev-3"]
    assert client.batch_calls[1][0][0]["insertText"]["location"]["index"] == 100
    styles = client.batch_calls[2][0]
    if profile == "persian":
        assert styles[0]["updateParagraphStyle"]["range"] == {"startIndex": 137, "endIndex": 142, "tabId": "t.selected"}
        assert styles[1]["updateTextStyle"]["fields"] == "weightedFontFamily"
        assert len(styles) == 4
    else:
        assert len(styles) == 2
    assert styles[-2]["updateTextStyle"]["range"] == {"startIndex": 140, "endIndex": 141, "tabId": "t.selected"}
    assert styles[-1]["updateTextStyle"]["range"] == {"startIndex": 137, "endIndex": 139, "tabId": "t.selected"}
    assert backup.path.is_dir()


def test_task11_read_operation_returns_bounded_native_metadata_and_page(
    tmp_path: Path,
) -> None:
    client = Task11Client(documents=[_task11_document("rev-17", "Hello world\n")])
    service = _task11_service(client, tmp_path / "recovery")

    result = service.read(VALID_ID, start=6, max_chars=5)

    assert result == {
        "ok": True,
        "document_id": VALID_ID,
        "document_url": _TASK11_URL,
        "name": "Task 11 document",
        "mime_type": _TASK11_NATIVE_MIME,
        "modified_time": "2026-09-02T11:30:00.000Z",
        "version": "17",
        "revision_id": "rev-17",
        "tabs": [
            {
                "tab_id": "t.selected",
                "title": "Selected",
                "parent_tab_id": None,
            }
        ],
        "tab_id": "t.selected",
        "content": "world",
        "start": 6,
        "end": 11,
        "total_chars": 12,
        "next_start": 11,
        "outline": [],
        "verified": True,
    }
    assert client.events == ["metadata", "get_document"]
    assert client.create_calls == []
    assert client.batch_calls == []
    assert client.export_calls == []
    assert client.delete_calls == []


def test_task11_read_defaults_to_thirty_thousand_character_page(
    tmp_path: Path,
) -> None:
    text = "x" * 30_005 + "\n"
    client = Task11Client(documents=[_task11_document("rev-17", text)])
    service = _task11_service(client, tmp_path / "recovery")

    result = service.read(VALID_ID)

    assert result["content"] == "x" * 30_000
    assert result["start"] == 0
    assert result["end"] == 30_000
    assert result["total_chars"] == 30_006
    assert result["next_start"] == 30_000


def test_task11_read_rejects_wrong_document_link_before_docs_read(
    tmp_path: Path,
) -> None:
    metadata = _task11_metadata()
    metadata["webViewLink"] = (
        "https://docs.google.com/document/d/otherDEF_456-uvw/edit"
    )
    client = Task11Client(
        metadata=metadata,
        documents=[_task11_document("rev-1", "Hello\n")],
    )
    service = _task11_service(client, tmp_path / "recovery")

    error = assert_error_code("google_unavailable", service.read, VALID_ID)

    assert client.events == ["metadata"]
    assert client.batch_calls == []
    assert_error_sanitized(error, "otherDEF_456-uvw")


def _task11_multi_tab_document(revision: str) -> dict:
    document = _task11_document(revision, "first\n", tab_id="t.first", title="First")
    document["tabs"].append(
        _task11_document(
            revision, "second\n", tab_id="t.second", title="Second"
        )["tabs"][0]
    )
    return document


def _task11_table_document(revision: str) -> dict:
    def cell(text: str) -> dict:
        return {"content": [_task10_paragraph((text + "\n",), 1)]}

    return {
        "documentId": VALID_ID,
        "title": "Task 11 document",
        "revisionId": revision,
        "tabs": [
            {
                "tabProperties": {"tabId": "t.selected", "title": "Selected"},
                "documentTab": {
                    "body": {
                        "content": [
                            {
                                "table": {
                                    "tableRows": [
                                        {"tableCells": [cell("A")]},
                                        {"tableCells": [cell("B")]},
                                    ]
                                }
                            }
                        ]
                    }
                },
                "childTabs": [],
            }
        ],
    }


def _task11_expected_semantic(markdown: str) -> dict[str, object]:
    semantic = markdown_module.candidate_semantic(
        markdown_module.parse_markdown(markdown)
    )
    return {
        "schema": semantic["schema"],
        "block_count": len(semantic["blocks"]),
        "sha256": markdown_module.semantic_sha256(semantic),
    }


def test_task11_read_operation_without_multitab_id_returns_metadata_only(
    tmp_path: Path,
) -> None:
    client = Task11Client(documents=[_task11_multi_tab_document("rev-17")])
    service = _task11_service(client, tmp_path / "recovery")

    result = service.read(VALID_ID)

    assert result["tabs"] == [
        {"tab_id": "t.first", "title": "First", "parent_tab_id": None},
        {"tab_id": "t.second", "title": "Second", "parent_tab_id": None},
    ]
    assert not {"content", "outline", "start", "end", "next_start"} & set(result)
    assert result["verified"] is True
    assert client.batch_calls == []


@pytest.mark.parametrize(
    ("title", "markdown", "format_profile", "error_code"),
    [
        pytest.param("", "", "persian", "invalid_title", id="title"),
        pytest.param(
            "Created document",
            "before\x00after",
            "persian",
            "invalid_markdown",
            id="markdown",
        ),
        pytest.param(
            "Created document",
            "",
            "PROFILE_CANARY",
            "invalid_input",
            id="format-profile",
        ),
    ],
)
def test_task11_create_rejects_invalid_local_input_before_remote_creation(
    tmp_path: Path,
    title: str,
    markdown: str,
    format_profile: str,
    error_code: str,
) -> None:
    client = Task11Client()
    service = _task11_service(client, tmp_path / "recovery")

    error = assert_error_code(
        error_code,
        service.create,
        title,
        markdown,
        format_profile,
    )

    assert client.events == []
    assert client.create_calls == []
    assert client.batch_calls == []
    assert_error_sanitized(
        error,
        *(value for value in (markdown, format_profile) if value),
    )


def test_task11_create_completes_semantic_preflight_before_remote_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = Task11Client()
    service = _task11_service(client, tmp_path / "recovery")
    preflight_calls = 0

    def fail_preflight(value: object) -> str:
        nonlocal preflight_calls
        preflight_calls += 1
        assert isinstance(value, dict)
        raise DocsMCPError(
            "invalid_markdown",
            "Markdown semantic preflight failed.",
        )

    monkeypatch.setattr(markdown_module, "semantic_sha256", fail_preflight)

    assert_error_code(
        "invalid_markdown",
        service.create,
        "Created document",
        "SEMANTIC_PREFLIGHT_CANARY",
        "plain",
    )

    assert preflight_calls == 1
    assert client.events == []
    assert client.create_calls == []
    assert client.batch_calls == []


def test_task11_create_operation_with_empty_markdown_does_not_write(
    tmp_path: Path,
) -> None:
    client = Task11Client(documents=[_task11_document("rev-1", "\n")])
    service = _task11_service(client, tmp_path / "recovery")

    result = service.create("Created document", "", "persian")

    assert result == {
        "ok": True,
        "document_id": VALID_ID,
        "document_url": _TASK11_URL,
        "revision_id": "rev-1",
        "tab_id": "t.selected",
        "semantic": _task11_expected_semantic(""),
        "format_profile": "persian",
        "formatting": None,
        "verified": True,
    }
    assert client.events == ["create", "metadata", "get_document"]
    assert client.create_calls == ["Created document"]
    assert client.batch_calls == []
    assert client.export_calls == []
    assert client.delete_calls == []


def test_task11_create_operation_with_markdown_renders_and_verifies(
    tmp_path: Path,
) -> None:
    client = Task11Client(
        documents=[
            _task11_document("rev-1", "\n"),
            _task11_document("rev-1", "\n"),
            _task11_document("rev-2", "Hello\n"),
        ],
        batch_revisions=["rev-2"],
    )
    service = _task11_service(client, tmp_path / "recovery")

    result = service.create("Created document", "Hello", "plain")

    assert result == {
        "ok": True,
        "document_id": VALID_ID,
        "document_url": _TASK11_URL,
        "revision_id": "rev-2",
        "tab_id": "t.selected",
        "semantic": _task11_expected_semantic("Hello"),
        "format_profile": "plain",
        "formatting": None,
        "verified": True,
    }
    assert client.create_calls == ["Created document"]
    assert len(client.batch_calls) == 1
    assert client.batch_calls[0][2] == "rev-1"
    assert client.delete_calls == []


def test_task11_failed_initial_render_retains_new_document_id_without_echo(
    tmp_path: Path,
) -> None:
    markdown_canary = "INITIAL_MARKDOWN_CANARY"
    remote_canary = "INITIAL_REMOTE_CANARY"
    client = Task11Client(
        documents=[
            _task11_document("rev-1", "\n"),
            _task11_document("rev-1", "\n"),
            _task11_document("rev-2", remote_canary + "\n"),
        ],
        batch_revisions=["rev-2"],
    )
    service = _task11_service(client, tmp_path / "recovery")

    error = assert_error_code(
        "verification_failed",
        service.create,
        "Created document",
        markdown_canary,
        "plain",
    )

    assert error.as_result() == {
        "ok": False,
        "error": {
            "code": "verification_failed",
            "message": (
                "The new Google document was retained because its initial publication "
                "could not be verified."
            ),
            "retryable": False,
            "document_id": VALID_ID,
            "document_url": _TASK11_URL,
            "retained_for_diagnosis": True,
            "failure_code": "verification_failed",
        },
    }
    assert client.delete_calls == []
    assert_error_sanitized(error, markdown_canary, remote_canary)


def test_task11_failed_initial_table_render_returns_recovery_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table_markdown = "| A |\n| --- |\n| B |"
    client = Task11Client(
        documents=[
            _task11_document("rev-1", "\n"),
            _task11_document("rev-1", "\n"),
        ],
        batch_revisions=["rev-2"],
        exports={
            _TASK11_TEXT_MIME: [b"empty-before"],
            _TASK11_DOCX_MIME: [b"empty-before-docx"],
        },
    )
    service = _task11_service(client, tmp_path / "recovery")

    def fail_phases(
        phase_client: object,
        document_id: str,
        tab_id: str,
        model: DocumentModel,
        revision: str,
        backup: object,
        *,
        cleanup_backup: bool = True,
        profile: str,
    ) -> str:
        assert profile == "plain"
        assert phase_client is client
        assert document_id == VALID_ID
        assert tab_id == "t.selected"
        assert model.tables
        assert revision == "rev-2"
        assert isinstance(backup, client_module.RecoveryBackup)
        assert cleanup_backup is False
        raise client_module._PartialWriteError(
            phase="cell_text",
            revision_id=revision,
            recovery_path=backup.path,
        )

    monkeypatch.setattr(client_module, "_run_table_phases", fail_phases)

    error = assert_error_code(
        "verification_failed",
        service.create,
        "Retained table",
        table_markdown,
        "plain",
    )

    result = error.as_result()
    details = result["error"]
    assert isinstance(details, dict)
    assert details["document_id"] == VALID_ID
    assert details["failure_code"] == "partial_write_requires_recovery"
    assert details["phase"] == "cell_text"
    assert details["revision_id"] == "rev-2"
    recovery_value = details["recovery_path"]
    assert isinstance(recovery_value, str)
    recovery_path = Path(recovery_value)
    assert recovery_path.is_dir()
    assert (recovery_path / "document.txt").read_bytes() == b"empty-before"
    assert (recovery_path / "document.docx").read_bytes() == b"empty-before-docx"
    assert client.delete_calls == []
    assert_error_sanitized(error, table_markdown)


def test_task11_replace_operation_rejects_office_mime_before_docs_or_write(
    tmp_path: Path,
) -> None:
    client = Task11Client(
        metadata=_task11_metadata(mime_type=_TASK11_DOCX_MIME),
    )
    service = _task11_service(client, tmp_path / "recovery")

    error = assert_error_code(
        "unsupported_office_file",
        service.replace_markdown,
        VALID_ID,
        "OFFICE_MARKDOWN_CANARY",
        "rev-1",
        None,
        "plain",
    )

    assert client.events == ["metadata"]
    assert client.batch_calls == []
    assert client.export_calls == []
    assert not (tmp_path / "recovery").exists()
    assert_error_sanitized(error, "OFFICE_MARKDOWN_CANARY", _TASK11_DOCX_MIME)


def test_task11_replace_operation_requires_tab_id_before_backup_or_write(
    tmp_path: Path,
) -> None:
    client = Task11Client(documents=[_task11_multi_tab_document("rev-1")])
    service = _task11_service(client, tmp_path / "recovery")

    error = assert_error_code(
        "multiple_tabs_require_tab_id",
        service.replace_markdown,
        VALID_ID,
        "MULTITAB_MARKDOWN_CANARY",
        "rev-1",
        None,
        "plain",
    )

    assert client.events == ["metadata", "get_document"]
    assert client.batch_calls == []
    assert client.export_calls == []
    assert not (tmp_path / "recovery").exists()
    assert_error_sanitized(error, "MULTITAB_MARKDOWN_CANARY")


def test_task11_replace_operation_rejects_stale_revision_before_backup_or_write(
    tmp_path: Path,
) -> None:
    client = Task11Client(documents=[_task11_document("rev-current", "Before\n")])
    service = _task11_service(client, tmp_path / "recovery")

    error = assert_error_code(
        "stale_revision",
        service.replace_markdown,
        VALID_ID,
        "STALE_MARKDOWN_CANARY",
        "rev-stale",
        None,
        "plain",
    )

    assert client.batch_calls == []
    assert client.export_calls == []
    assert not (tmp_path / "recovery").exists()
    assert_error_sanitized(error, "STALE_MARKDOWN_CANARY", "rev-current")


def test_task11_replace_operation_without_tables_uses_one_batch_and_verifies(
    tmp_path: Path,
) -> None:
    client = Task11Client(
        documents=[
            _task11_document("rev-1", "Before\n"),
            _task11_document("rev-2", "Hello\n"),
        ],
        batch_revisions=["rev-2"],
    )
    service = _task11_service(client, tmp_path / "recovery")

    result = service.replace_markdown(
        VALID_ID, "Hello", "rev-1", None, "plain"
    )

    assert result == {
        "ok": True,
        "document_id": VALID_ID,
        "document_url": _TASK11_URL,
        "tab_id": "t.selected",
        "before_revision_id": "rev-1",
        "after_revision_id": "rev-2",
        "semantic": _task11_expected_semantic("Hello"),
        "format_profile": "plain",
        "formatting": None,
        "verified": True,
    }
    assert len(client.batch_calls) == 1
    assert client.batch_calls[0][2] == "rev-1"
    assert client.export_calls == []
    assert not (tmp_path / "recovery").exists()


def test_task11_replace_operation_rejects_nonadvancing_response_before_readback(
    tmp_path: Path,
) -> None:
    client = Task11Client(
        documents=[_task11_document("rev-1", "Before\n")],
        batch_revisions=["rev-1"],
    )
    service = _task11_service(client, tmp_path / "recovery")

    error = assert_error_code(
        "verification_failed",
        service.replace_markdown,
        VALID_ID,
        "REVISION_MARKDOWN_CANARY",
        "rev-1",
        None,
        "plain",
    )

    assert len(client.batch_calls) == 1
    assert client.events.count("get_document") == 1
    assert_error_sanitized(error, "REVISION_MARKDOWN_CANARY")


def test_task11_replace_operation_rejects_readback_revision_mismatch(
    tmp_path: Path,
) -> None:
    client = Task11Client(
        documents=[
            _task11_document("rev-1", "Before\n"),
            _task11_document("rev-other", "Hello\n"),
        ],
        batch_revisions=["rev-2"],
    )
    service = _task11_service(client, tmp_path / "recovery")

    error = assert_error_code(
        "verification_failed",
        service.replace_markdown,
        VALID_ID,
        "Hello",
        "rev-1",
        None,
        "plain",
    )

    assert len(client.batch_calls) == 1
    assert_error_sanitized(error, "rev-other")


def test_task11_replace_semantic_mismatch_is_sanitized_verification_failure(
    tmp_path: Path,
) -> None:
    markdown_canary = "SEMANTIC_MARKDOWN_CANARY"
    remote_canary = "SEMANTIC_REMOTE_CANARY"
    client = Task11Client(
        documents=[
            _task11_document("rev-1", "Before\n"),
            _task11_document("rev-2", remote_canary + "\n"),
        ],
        batch_revisions=["rev-2"],
    )
    service = _task11_service(client, tmp_path / "recovery")

    error = assert_error_code(
        "verification_failed",
        service.replace_markdown,
        VALID_ID,
        markdown_canary,
        "rev-1",
        None,
        "plain",
    )

    assert len(client.batch_calls) == 1
    assert_error_sanitized(error, markdown_canary, remote_canary)


@pytest.mark.parametrize(
    ("docx_payload", "expected_valid"),
    (
        (_task9_docx_bytes(), True),
        (_task9_docx_bytes(body_bidi=False), False),
    ),
    ids=("valid", "invalid"),
)
def test_task11_persian_replace_invokes_docx_verifier(
    tmp_path: Path,
    docx_payload: bytes,
    expected_valid: bool,
) -> None:
    client = Task11Client(
        documents=[
            _task11_document("rev-1", "Before\n"),
            _task11_with_persian_api_styles(_task11_document("rev-2", "سلام\n")),
        ],
        batch_revisions=["rev-2"],
        exports={_TASK11_DOCX_MIME: [docx_payload]},
    )
    service = _task11_service(client, tmp_path / "recovery")

    if expected_valid:
        result = service.replace_markdown(
            VALID_ID, "سلام", "rev-1", None, "persian"
        )
        assert result["formatting"] == client_module.verify_persian_docx(
            docx_payload
        )
        assert result["verified"] is True
    else:
        error = assert_error_code(
            "verification_failed",
            service.replace_markdown,
            VALID_ID,
            "سلام",
            "rev-1",
            None,
            "persian",
        )
        assert_error_sanitized(error, *_TASK9_DOCX_CANARIES)
    assert client.export_calls == [(VALID_ID, _TASK11_DOCX_MIME)]


def test_task11_table_phase_can_defer_recovery_cleanup(
    tmp_path: Path,
) -> None:
    client = TablePhaseClient(
        revisions=["rev-2", "rev-3", "rev-4"],
        documents=[
            _task8_phase_document("rev-2", 100),
            _task8_phase_document("rev-3", 100),
            _task8_phase_document("rev-4", 100),
        ],
    )
    backup = client_module.make_recovery_backup(client, VALID_ID, tmp_path)

    final_revision = client_module._run_table_phases(
        client,
        document_id=VALID_ID,
        tab_id="t.selected",
        model=_task8_phase_model(),
        revision="rev-1",
        backup=backup,
        cleanup_backup=False,
    )

    assert final_revision == "rev-4"
    assert backup.path.is_dir()
    assert backup.text_path.is_file()
    assert backup.docx_path.is_file()


def test_task11_table_replace_retains_recovery_on_semantic_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table_markdown = "| A |\n| --- |\n| B |"
    client = Task11Client(
        documents=[
            _task11_document("rev-1", "Before\n"),
            _task11_document("rev-5", "TABLE_REMOTE_CANARY\n"),
        ],
        batch_revisions=["rev-2"],
        exports={
            _TASK11_TEXT_MIME: [b"before"],
            _TASK11_DOCX_MIME: [b"backup-docx"],
        },
    )
    service = _task11_service(client, tmp_path / "recovery")
    phase_calls: list[tuple[str, bool]] = []

    def run_phases(
        phase_client: object,
        document_id: str,
        tab_id: str,
        model: DocumentModel,
        revision: str,
        backup: object,
        *,
        cleanup_backup: bool = True,
        profile: str,
    ) -> str:
        assert profile == "plain"
        assert phase_client is client
        assert document_id == VALID_ID
        assert tab_id == "t.selected"
        assert model.tables
        assert revision == "rev-2"
        assert isinstance(backup, client_module.RecoveryBackup)
        assert backup.path.is_dir()
        phase_calls.append((revision, cleanup_backup))
        return "rev-5"

    monkeypatch.setattr(client_module, "_run_table_phases", run_phases)

    error = assert_error_code(
        "partial_write_requires_recovery",
        service.replace_markdown,
        VALID_ID,
        table_markdown,
        "rev-1",
        None,
        "plain",
    )

    result = error.as_result()
    details = result["error"]
    assert isinstance(details, dict)
    assert details["phase"] == "semantic_verification"
    assert details["revision_id"] == "rev-5"
    recovery_value = details["recovery_path"]
    assert isinstance(recovery_value, str)
    recovery_path = Path(recovery_value)
    assert recovery_path.is_dir()
    assert (recovery_path / "document.txt").read_bytes() == b"before"
    assert (recovery_path / "document.docx").read_bytes() == b"backup-docx"
    assert phase_calls == [("rev-2", False)]
    assert client.delete_calls == []
    assert_error_sanitized(error, table_markdown, "TABLE_REMOTE_CANARY")


def test_task11_table_replace_retains_recovery_until_persian_docx_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table_markdown = "| A |\n| --- |\n| B |"
    invalid_docx = b"TABLE_FORMAT_DOCX_CANARY"
    client = Task11Client(
        documents=[
            _task11_document("rev-1", "Before\n"),
            _task11_with_persian_api_styles(_task11_table_document("rev-5")),
        ],
        batch_revisions=["rev-2"],
        exports={
            _TASK11_TEXT_MIME: [b"before"],
            _TASK11_DOCX_MIME: [b"backup-docx", invalid_docx],
        },
    )
    service = _task11_service(client, tmp_path / "recovery")
    recovery_paths: list[Path] = []

    def run_phases(
        phase_client: object,
        document_id: str,
        tab_id: str,
        model: DocumentModel,
        revision: str,
        backup: object,
        *,
        cleanup_backup: bool = True,
        profile: str,
    ) -> str:
        assert profile == "persian"
        assert phase_client is client
        assert document_id == VALID_ID
        assert tab_id == "t.selected"
        assert model.tables
        assert revision == "rev-2"
        assert isinstance(backup, client_module.RecoveryBackup)
        assert cleanup_backup is False
        recovery_paths.append(backup.path)
        return "rev-5"

    monkeypatch.setattr(client_module, "_run_table_phases", run_phases)

    error = assert_error_code(
        "partial_write_requires_recovery",
        service.replace_markdown,
        VALID_ID,
        table_markdown,
        "rev-1",
        None,
        "persian",
    )

    details = error.as_result()["error"]
    assert isinstance(details, dict)
    assert details["phase"] == "format_verification"
    assert details["revision_id"] == "rev-5"
    assert len(recovery_paths) == 1
    recovery_path = recovery_paths[0]
    assert details["recovery_path"] == str(recovery_path)
    assert recovery_path.is_dir()
    assert (recovery_path / "document.txt").read_bytes() == b"before"
    assert (recovery_path / "document.docx").read_bytes() == b"backup-docx"
    assert client.export_calls == [
        (VALID_ID, _TASK11_TEXT_MIME),
        (VALID_ID, _TASK11_DOCX_MIME),
        (VALID_ID, _TASK11_DOCX_MIME),
    ]
    assert_error_sanitized(
        error,
        table_markdown,
        invalid_docx.decode("ascii"),
    )


def test_task11_table_replace_deletes_recovery_only_after_full_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table_markdown = "| A |\n| --- |\n| B |"
    client = Task11Client(
        documents=[
            _task11_document("rev-1", "Before\n"),
            _task11_table_document("rev-5"),
        ],
        batch_revisions=["rev-2"],
        exports={
            _TASK11_TEXT_MIME: [b"before"],
            _TASK11_DOCX_MIME: [b"backup-docx"],
        },
    )
    service = _task11_service(client, tmp_path / "recovery")
    recovery_paths: list[Path] = []

    def run_phases(
        phase_client: object,
        document_id: str,
        tab_id: str,
        model: DocumentModel,
        revision: str,
        backup: object,
        *,
        cleanup_backup: bool = True,
        profile: str,
    ) -> str:
        assert profile == "plain"
        assert phase_client is client
        assert document_id == VALID_ID
        assert tab_id == "t.selected"
        assert model.tables
        assert revision == "rev-2"
        assert isinstance(backup, client_module.RecoveryBackup)
        assert cleanup_backup is False
        recovery_paths.append(backup.path)
        return "rev-5"

    monkeypatch.setattr(client_module, "_run_table_phases", run_phases)

    result = service.replace_markdown(
        VALID_ID, table_markdown, "rev-1", None, "plain"
    )

    assert result["semantic"] == _task11_expected_semantic(table_markdown)
    assert result["verified"] is True
    assert len(recovery_paths) == 1
    assert not recovery_paths[0].exists()


def test_task11_edit_rejects_office_mime_before_docs_read_or_write(
    tmp_path: Path,
) -> None:
    client = Task11Client(
        metadata=_task11_metadata(
            mime_type=(
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            )
        ),
        documents=[_task10_document("rev-1", paragraph_parts=("alpha\n",))],
    )
    service = _task11_service(client, tmp_path / "recovery")

    error = assert_error_code(
        "unsupported_office_file",
        service.edit_text,
        VALID_ID,
        [client_module.Replacement("alpha", "beta", 1)],
        "rev-1",
    )

    assert client.events == ["metadata"]
    assert client.batch_calls == []
    assert_error_sanitized(error, "alpha", "beta")


def test_task11_edit_operation_delegates_preview_and_apply(
    tmp_path: Path,
) -> None:
    before = _task10_document(
        "rev-1",
        paragraph_parts=("alpha alpha\n",),
    )
    after = _task10_document(
        "rev-2",
        paragraph_parts=("beta beta\n",),
    )
    replacement = client_module.Replacement("alpha", "beta", 2)

    preview_client = Task11EditClient([before])
    preview_service = client_module.GoogleDocsService(
        preview_client, tmp_path / "preview"  # type: ignore[arg-type]
    )
    preview = preview_service.edit_text(
        VALID_ID,
        [replacement],
        "rev-1",
        _TASK10_SELECTED_TAB,
    )
    assert preview["valid"] is True
    assert preview["revision_id"] == "rev-1"
    assert preview["ok"] is True
    assert preview["verified"] is True
    assert preview_client.batch_calls == []

    apply_client = Task11EditClient(
        [before, after],
        [_task10_success_response()],
    )
    apply_service = client_module.GoogleDocsService(
        apply_client, tmp_path / "apply"  # type: ignore[arg-type]
    )
    applied = apply_service.edit_text(
        VALID_ID,
        [replacement],
        "rev-1",
        _TASK10_SELECTED_TAB,
        True,
    )
    assert applied["before_revision_id"] == "rev-1"
    assert applied["after_revision_id"] == "rev-2"
    assert applied["ok"] is True
    assert applied["verified"] is True
    assert len(apply_client.batch_calls) == 1
