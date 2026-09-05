"""Opt-in native API characterization; never touches existing documents."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import uuid

import pytest
from google.auth.transport.requests import AuthorizedSession

from google_docs_mcp.client import (
    DocsMCPError,
    GoogleDocsClient,
    GoogleDocsService,
    load_credentials,
    select_tab,
    utf16_length,
)
from google_docs_mcp.markdown import candidate_semantic, parse_markdown, remote_semantic
import test_live_google as live


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_GOOGLE_DOCS_MCP_LIVE") != "1",
    reason="Live Google writes require explicit authorization.",
)

_IMPORT_MARKDOWN = """# عنوان آزمایشی 🧪

متن **پررنگ** و [پیوند](https://example.com/native-api).

- گزینه
- [x] انجام‌شده

| ستون | مقدار |
| --- | --- |
| **ردیف** | [داده](https://example.com/native-cell) |
"""


@pytest.fixture
def google_session():
    try:
        credentials = load_credentials()
    except DocsMCPError as error:
        pytest.fail("Google authorization unavailable: " + error.code, pytrace=False)
    with AuthorizedSession(credentials) as session:
        yield session, GoogleDocsClient(session)


@contextmanager
def _temporary_document(session, client, create):
    started = datetime.now(timezone.utc)
    title = "TEMP — Hermes native API audit — " + uuid.uuid4().hex
    journal = live._create_journal(title, started)
    document_id = None
    attempted = False
    outcome = live._Outcome()
    try:
        attempted = True
        document_id = live._valid_document_id(create(title))
        if document_id is None:
            live._fail("docs_create: invalid document ID")
        live._assert_private(session, document_id)
        yield document_id
    except BaseException as error:
        outcome.capture(error)
    finally:
        try:
            if document_id is None and attempted:
                document_id = live._reconcile_created(
                    session, title, started, datetime.now(timezone.utc)
                )
                live._require(document_id is not None,
                              "recovery: run document unresolved; private journal retained")
            if document_id is not None:
                live._delete_and_verify(client, document_id)
            journal.remove()
        except BaseException as error:
            outcome.capture(error, cleanup=True)
        live._finish(outcome)


def test_live_drive_markdown_import_characterization(google_session, record_property):
    session, client = google_session
    response = client._request(
        "GET", client.DRIVE_BASE + "/about", params={"fields": "importFormats"}
    )
    supported = response.json().get("importFormats", {}).get("text/markdown", [])
    assert live._NATIVE_DOCUMENT_MIME in supported

    def create(title):
        boundary = "hermes_native_" + uuid.uuid4().hex
        metadata = json.dumps({"name": title, "mimeType": live._NATIVE_DOCUMENT_MIME})
        payload = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{metadata}\r\n--{boundary}\r\nContent-Type: text/markdown; charset=UTF-8\r\n\r\n"
            f"{_IMPORT_MARKDOWN}\r\n--{boundary}--\r\n"
        ).encode("utf-8")
        response = client._request(
            "POST", "https://www.googleapis.com/upload/drive/v3/files",
            retry_safe=False,
            params={"uploadType": "multipart", "fields": "id"},
            headers={"Content-Type": f"multipart/related; boundary={boundary}"},
            data=payload,
        )
        return response.json().get("id")

    with _temporary_document(session, client, create) as document_id:
        document = client.get_document(document_id)
        selected = select_tab(document, None)
        if selected is None:
            live._fail("docs_read: missing tab")
        paragraphs = list(live._paragraphs(selected.body["content"]))
        runs = [run for paragraph in paragraphs for run in live._text_runs(paragraph)]
        summary = {
            "native_markdown_supported": True,
            "headings": sum(p.get("paragraphStyle", {}).get("namedStyleType") == "HEADING_1"
                            for p in paragraphs),
            "native_list_paragraphs": sum("bullet" in p for p in paragraphs),
            "tables": sum("table" in node for node in selected.body["content"]),
            "bold_runs": sum(r.get("textStyle", {}).get("bold") is True for r in runs),
            "linked_runs": sum("link" in r.get("textStyle", {}) for r in runs),
            "rtl_paragraphs": sum(p.get("paragraphStyle", {}).get("direction") == "RIGHT_TO_LEFT"
                                  for p in paragraphs),
            "vazirmatn_runs": sum(r.get("textStyle", {}).get("weightedFontFamily", {}).get("fontFamily") == "Vazirmatn"
                                  for r in runs),
            "current_subset_semantics_equal": remote_semantic(selected.body) == candidate_semantic(parse_markdown(_IMPORT_MARKDOWN)),
        }
        live._require(summary["headings"] == 1 and summary["bold_runs"] > 0
                      and summary["linked_runs"] > 0, "semantic verification: conversion failed")
        record_property("native_import_characterization", json.dumps(summary, sort_keys=True))
        print("NATIVE_IMPORT_CHARACTERIZATION " + json.dumps(summary, sort_keys=True))


@pytest.mark.parametrize("profile", ["plain", "persian"])
def test_live_replacement_removes_native_lists(google_session, tmp_path: Path, profile):
    session, client = google_session
    with _temporary_document(session, client, client.create_document) as document_id:
        document = client.get_document(document_id)
        selected = select_tab(document, None)
        if selected is None:
            live._fail("docs_read: missing tab")
        tab_id = selected.tab_id
        seed = "Old list\nSecond item"
        client.batch_update(document_id, [
            {"insertText": {"location": {"index": 1, "tabId": tab_id}, "text": seed}},
            {"createParagraphBullets": {"range": {"startIndex": 1, "endIndex": 1 + utf16_length(seed), "tabId": tab_id},
                                         "bulletPreset": "NUMBERED_DECIMAL_ALPHA_ROMAN"}},
        ], document["revisionId"], retry_safe=False)
        before = client.get_document(document_id)
        selected = select_tab(before, tab_id)
        if selected is None:
            live._fail("docs_read: missing tab")
        live._require(any("bullet" in p for p in live._paragraphs(selected.body["content"])),
                      "semantic verification: fixture not unique")
        result = GoogleDocsService(client, tmp_path / "recovery").replace_markdown(
            document_id, "متن جدید 🧪\n\n- گزینه", before["revisionId"], tab_id, profile
        )
        live._require_ok(result, "docs_replace_markdown")
        after = client.get_document(document_id)
        selected = select_tab(after, tab_id)
        if selected is None:
            live._fail("docs_read: missing tab")
        live._require(not any("bullet" in p for p in live._paragraphs(selected.body["content"])),
                      "semantic verification: native list survived replacement")
