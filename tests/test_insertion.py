"""Independent synthetic fixtures for exact, non-destructive insertion."""
import asyncio
from copy import deepcopy
from pathlib import Path

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from google_docs_mcp import server
from google_docs_mcp.client import DocsMCPError, GoogleDocsService


DOC = "synthetic_doc_123"
STYLE = {
    "direction": "RIGHT_TO_LEFT", "alignment": "START",
    "indentStart": {"magnitude": 0, "unit": "PT"},
    "indentEnd": {"magnitude": 0, "unit": "PT"},
}
FONT = {"weightedFontFamily": {"fontFamily": "Vazirmatn"}}


def width(text):
    return len(text.encode("utf-16-le")) // 2


def body(text, start=1):
    content = []
    for line in text.splitlines(keepends=True):
        end = start + width(line)
        content.append({"startIndex": start, "endIndex": end, "paragraph": {
            "paragraphStyle": deepcopy(STYLE),
            "elements": [{"startIndex": start, "endIndex": end,
                          "textRun": {"content": line, "textStyle": deepcopy(FONT)}}],
        }})
        start = end
    return {"content": content}


def document(text, revision="rev-1"):
    return {"documentId": DOC, "revisionId": revision, "tabs": [{
        "tabProperties": {"tabId": "tab-1", "title": "Selected"},
        "documentTab": {"body": body(text)},
    }]}


class Client:
    def __init__(self, before, after=None):
        self.before = before
        self.after = after
        self.events = []
        self.writes = []
        self.response_revision = "rev-2"

    def drive_metadata(self, document_id):
        self.events.append("metadata")
        return {"id": DOC, "name": "Synthetic", "mimeType": "application/vnd.google-apps.document",
                "modifiedTime": "2026-01-01T00:00:00Z", "version": "1",
                "webViewLink": f"https://docs.google.com/document/d/{DOC}/edit"}

    def get_document(self, document_id):
        self.events.append("read")
        return deepcopy(self.after if self.writes else self.before)

    def batch_update(self, document_id, requests, revision, *, retry_safe=True):
        assert retry_safe is False
        self.events.append("write")
        self.writes.append((document_id, deepcopy(requests), revision))
        return {"writeControl": {"requiredRevisionId": self.response_revision},
                "replies": [{} for _ in requests]}


def insert(client, **kwargs):
    return GoogleDocsService(client, Path("unused-recovery")).insert_text(
        DOC, kwargs.pop("text", "جدید🧪"), kwargs.pop("expected_revision_id", "rev-1"), **kwargs
    )


@pytest.mark.parametrize("position,anchor,index,expected", [
    ("start", None, 1, "جدید🧪سلام 🧪 هدف\nبعد\n"),
    ("end", None, 16, "سلام 🧪 هدف\nبعدجدید🧪\n"),
    ("before", "هدف", 9, "سلام 🧪 جدید🧪هدف\nبعد\n"),
    ("after", "هدف", 12, "سلام 🧪 هدفجدید🧪\nبعد\n"),
])
def test_four_positions_preview_then_atomic_apply(position, anchor, index, expected):
    original = document("سلام 🧪 هدف\nبعد\n")
    client = Client(original, document(expected, "rev-2"))
    preview = insert(client, position=position, anchor_text=anchor, format_profile="plain")
    assert preview["ok"] and preview["valid"] and preview["applied"] is False
    assert preview["index"] == index
    assert preview["revision_id"] == "rev-1"
    assert client.writes == []
    result = insert(client, position=position, anchor_text=anchor, format_profile="plain", apply=True)
    assert result["verified"] and result["applied"] is True
    assert result["before_revision_id"] == "rev-1"
    assert result["after_revision_id"] == "rev-2"
    assert client.writes == [(DOC, [{"insertText": {
        "location": {"index": index, "tabId": "tab-1"}, "text": "جدید🧪",
    }}], "rev-1")]
    assert client.before == original
    assert client.events == ["metadata", "read", "metadata", "read", "write", "read"]


def test_blank_document_and_literal_markdown_newlines():
    text = "# literal\n**not bold**\n"
    client = Client(document("\n"), document(text + "\n", "rev-2"))
    assert insert(client, text=text, apply=True, format_profile="plain")["verified"]
    assert client.writes[0][1][0]["insertText"]["text"] == text


def test_anchor_matches_across_style_runs_but_not_paragraphs():
    before = document("target\n")
    paragraph = before["tabs"][0]["documentTab"]["body"]["content"][0]["paragraph"]
    paragraph["elements"] = [
        {"startIndex": 1, "endIndex": 4, "textRun": {"content": "tar"}},
        {"startIndex": 4, "endIndex": 8, "textRun": {"content": "get\n"}},
    ]
    client = Client(before, document("target!\n", "rev-2"))
    assert insert(client, text="!", position="after", anchor_text="target", apply=True,
                  format_profile="plain")["verified"]


@pytest.mark.parametrize("kwargs,code", [
    ({"text": ""}, "invalid_input"),
    ({"text": "\x00"}, "invalid_input"),
    ({"text": "\r"}, "invalid_input"),
    ({"text": "\ue000"}, "invalid_input"),
    ({"text": "\ud800"}, "invalid_input"),
    ({"text": "x" * 500001}, "invalid_input"),
    ({"text": 1}, "invalid_input"),
    ({"position": "middle"}, "invalid_input"),
    ({"position": []}, "invalid_input"),
    ({"position": "before"}, "invalid_input"),
    ({"position": "after", "anchor_text": ""}, "invalid_input"),
    ({"position": "before", "anchor_text": "a\nb"}, "invalid_input"),
    ({"position": "end", "anchor_text": "target"}, "invalid_input"),
    ({"position": "start", "anchor_text": "target"}, "invalid_input"),
    ({"format_profile": []}, "invalid_input"),
    ({"apply": "true"}, "invalid_input"),
    ({"expected_revision_id": ""}, "invalid_input"),
    ({"tab_id": 1}, "invalid_input"),
])
def test_invalid_inputs_rejected_before_transport(kwargs, code):
    client = Client(document("target\n"))
    with pytest.raises(DocsMCPError) as error:
        insert(client, **kwargs)
    assert error.value.code == code
    assert client.events == []


@pytest.mark.parametrize("text,anchor", [("target target\n", "target"), ("aaa\n", "aa"),
                                         ("absent\n", "target")])
def test_ambiguous_overlapping_or_absent_anchor_fails_without_writing(text, anchor):
    client = Client(document(text))
    with pytest.raises(DocsMCPError) as error:
        insert(client, position="after", anchor_text=anchor, apply=True)
    assert error.value.code == "anchor_match_mismatch"
    assert client.writes == []


def test_stale_revision_and_multitab_require_explicit_target():
    before = document("target\n")
    before["tabs"].append({"tabProperties": {"tabId": "tab-2", "title": "Other"},
                           "documentTab": {"body": body("target\n")}})
    client = Client(before)
    for kwargs, code in [({"expected_revision_id": "stale"}, "stale_revision"),
                         ({}, "multiple_tabs_require_tab_id"),
                         ({"tab_id": "unknown"}, "tab_not_found")]:
        with pytest.raises(DocsMCPError) as error:
            insert(client, apply=True, **kwargs)
        assert error.value.code == code
        assert client.writes == []
    result = insert(client, position="before", anchor_text="target", tab_id="tab-1")
    assert result["index"] == 1


def test_auxiliary_anchor_does_not_target_header():
    before = document("body\n")
    before["tabs"][0]["documentTab"]["headers"] = {
        "header-1": {"headerId": "header-1", **body("target\n", start=0)}
    }
    client = Client(before)
    with pytest.raises(DocsMCPError) as error:
        insert(client, position="after", anchor_text="target", apply=True)
    assert error.value.code == "anchor_match_mismatch"
    assert client.writes == []


def test_table_cell_anchor_preserves_body_gaps_and_following_text():
    def table_document(cell_text, revision):
        result = document("\n", revision)
        content = result["tabs"][0]["documentTab"]["body"]["content"]
        end = 5 + width(cell_text)
        content.append({"startIndex": 2, "endIndex": end + 2, "table": {"tableRows": [
            {"tableCells": [body(cell_text, start=5)]}
        ]}})
        content.extend(body("tail\n", start=end + 2)["content"])
        return result
    client = Client(table_document("target\n", "rev-1"), table_document("target!\n", "rev-2"))
    result = insert(client, text="!", position="after", anchor_text="target",
                    format_profile="plain", apply=True)
    assert result["verified"] and result["index"] == 11


@pytest.mark.parametrize("after", [document("wrong\n", "rev-2"), document("target!\n", "wrong"),
                                  document("!target\n", "rev-2"), document("target\n", "rev-2")])
def test_readback_must_match_location_old_text_and_revision(after):
    client = Client(document("target\n"), after)
    with pytest.raises(DocsMCPError) as error:
        insert(client, text="!", position="after", anchor_text="target", format_profile="plain", apply=True)
    assert error.value.code == "verification_failed"
    assert len(client.writes) == 1


def test_nonadvancing_revision_is_not_success_or_retried():
    client = Client(document("target\n"))
    client.response_revision = "rev-1"
    with pytest.raises(DocsMCPError) as error:
        insert(client, apply=True)
    assert error.value.code == "verification_failed"
    assert len(client.writes) == 1


def test_persian_requests_are_scoped_and_independently_verified():
    client = Client(document("abc\nuntouched\n"), document("aجدید🧪bc\nuntouched\n", "rev-2"))
    result = insert(client, position="after", anchor_text="a", apply=True)
    assert result["formatting_verified"] is True
    requests = client.writes[0][1]
    assert len(requests) == 3
    paragraph = requests[1]["updateParagraphStyle"]
    assert paragraph["paragraphStyle"] == STYLE
    assert paragraph["fields"] == "direction,alignment,indentStart,indentEnd"
    assert paragraph["range"] == {"startIndex": 2, "endIndex": 8, "tabId": "tab-1"}
    assert requests[2]["updateTextStyle"] == {
        "range": {"startIndex": 2, "endIndex": 8, "tabId": "tab-1"},
        "textStyle": FONT, "fields": "weightedFontFamily",
    }


@pytest.mark.parametrize("missing", ["direction", "alignment", "indentStart", "indentEnd", "font"])
def test_persian_readback_rejects_each_missing_setting(missing):
    after = document("a!bc\n", "rev-2")
    paragraph = after["tabs"][0]["documentTab"]["body"]["content"][0]["paragraph"]
    if missing == "font":
        paragraph["elements"][0]["textRun"]["textStyle"] = {}
    else:
        paragraph["paragraphStyle"].pop(missing)
    client = Client(document("abc\n"), after)
    with pytest.raises(DocsMCPError) as error:
        insert(client, text="!", position="after", anchor_text="a", apply=True)
    assert error.value.code == "verification_failed"


def test_insert_mcp_schema_defaults_and_forwarding(monkeypatch):
    calls = []
    class Service:
        def insert_text(self, *args, **kwargs):
            calls.append((args, kwargs))
            return {"ok": True}
    monkeypatch.setattr(server, "_get_service", lambda: Service())
    async def exercise():
        tools = {tool.name: tool for tool in await server.mcp.list_tools()}
        assert "docs_insert_text" in tools
        schema = tools["docs_insert_text"].inputSchema
        assert set(schema["properties"]) == {"document", "text", "expected_revision_id", "position",
                                             "anchor_text", "tab_id", "format_profile", "apply"}
        assert set(schema["required"]) == {"document", "text", "expected_revision_id"}
        assert schema["properties"]["position"]["enum"] == ["start", "end", "before", "after"]
        args = {"document": DOC, "text": "new", "expected_revision_id": "rev-1"}
        result = await server.mcp.call_tool("docs_insert_text", args)
        assert result[1] == {"ok": True}
        for invalid in ({"apply": "true"}, {"position": "middle"}, {"text": 42}):
            with pytest.raises(ToolError):
                await server.mcp.call_tool("docs_insert_text", {**args, **invalid})
    asyncio.run(exercise())
    assert calls == [((DOC, "new", "rev-1"), {"position": "end", "anchor_text": None,
                                           "tab_id": None, "format_profile": "persian", "apply": False})]


def test_insertion_preserves_nonbold_heading_without_demanding_new_bold_style():
    before, after = document("abc\n"), document("a!bc\n", "rev-2")
    for doc in (before, after):
        paragraph = doc["tabs"][0]["documentTab"]["body"]["content"][0]["paragraph"]
        paragraph["paragraphStyle"]["namedStyleType"] = "HEADING_2"
        paragraph["elements"][0]["textRun"]["textStyle"]["bold"] = False
    client = Client(before, after)
    assert insert(client, text="!", position="after", anchor_text="a", apply=True)["verified"]


def test_persian_insertion_ignores_unrelated_table_of_contents_formatting():
    before, after = document("abc\n"), document("a!bc\n", "rev-2")
    for doc, start in ((before, 5), (after, 6)):
        content = doc["tabs"][0]["documentTab"]["body"]["content"]
        toc = body("TOC\n", start)
        toc["content"][0]["paragraph"].pop("paragraphStyle")
        content.append({"startIndex": start, "endIndex": start + 4, "tableOfContents": toc})
        content.extend(body("\n", start + 4)["content"])
    client = Client(before, after)
    assert insert(client, text="!", position="after", anchor_text="a", apply=True)["verified"]


def test_anchor_before_inline_object_can_insert_without_replacing_the_object():
    def with_image(text, revision):
        doc = document(text + "\n", revision)
        paragraph = doc["tabs"][0]["documentTab"]["body"]["content"][0]
        end = 1 + width(text)
        paragraph["endIndex"] = end + 2
        paragraph["paragraph"]["elements"] = [
            {"startIndex": 1, "endIndex": end, "textRun": {"content": text, "textStyle": deepcopy(FONT)}},
            {"startIndex": end, "endIndex": end + 1, "inlineObjectElement": {"inlineObjectId": "synthetic-image"}},
            {"startIndex": end + 1, "endIndex": end + 2,
             "textRun": {"content": "\n", "textStyle": deepcopy(FONT)}},
        ]
        return doc
    client = Client(with_image("target", "rev-1"), with_image("target!", "rev-2"))
    assert insert(client, text="!", position="after", anchor_text="target", apply=True)["verified"]


@pytest.mark.parametrize("failure", ["timeout", 503, 429])
def test_insertion_does_not_retry_uncertain_http_writes(monkeypatch, failure):
    import requests
    from types import SimpleNamespace
    from google_docs_mcp.client import GoogleDocsClient

    transport_calls = []
    fake = Client(document("target\n"))
    def request(method, url, **kwargs):
        transport_calls.append(method)
        if method == "GET":
            payload = fake.before if "/documents/" in url else fake.drive_metadata(DOC)
            return SimpleNamespace(status_code=200, json=lambda: payload)
        if failure == "timeout":
            raise requests.Timeout("SYNTHETIC_PRIVATE_PROVIDER_ERROR")
        return SimpleNamespace(status_code=failure, headers={})
    monkeypatch.setattr("google_docs_mcp.client.time.sleep", lambda *_: None)
    client = GoogleDocsClient(SimpleNamespace(request=request))
    with pytest.raises(DocsMCPError) as error:
        insert(client, text="!", apply=True, format_profile="plain")
    assert "SYNTHETIC_PRIVATE" not in str(error.value)
    assert transport_calls == ["GET", "GET", "POST"]


@pytest.mark.parametrize("profile", ["persian", "plain"])
def test_start_insertion_when_first_paragraph_begins_with_inline_image(profile):
    def with_image(prefix, revision):
        doc = document("\n", revision)
        node = doc["tabs"][0]["documentTab"]["body"]["content"][0]
        index = 1 + width(prefix)
        node["endIndex"] = index + 2
        node["paragraph"]["elements"] = ([
            {"startIndex": 1, "endIndex": index,
             "textRun": {"content": prefix, "textStyle": deepcopy(FONT)}}
        ] if prefix else []) + [
            {"startIndex": index, "endIndex": index + 1,
             "inlineObjectElement": {"inlineObjectId": "synthetic-image"}},
            {"startIndex": index + 1, "endIndex": index + 2,
             "textRun": {"content": "\n", "textStyle": deepcopy(FONT)}},
        ]
        return doc
    client = Client(with_image("", "rev-1"), with_image("🧪", "rev-2"))
    preview = insert(client, text="🧪", position="start", format_profile=profile)
    assert preview["valid"] and preview["index"] == 1 and client.writes == []
    result = insert(client, text="🧪", position="start", format_profile=profile, apply=True)
    assert result["verified"] and result["index"] == 1


@pytest.mark.parametrize("fault", ["missing_elements", "empty_elements", "boolean_start", "float_start",
                                  "nontext_gap", "missing_newline", "wrong_paragraph_end", "unknown_element"])
@pytest.mark.parametrize("apply", [False, True])
def test_image_first_boundary_rejects_malformed_paragraph_before_write(fault, apply):
    before = document("\n")
    node = before["tabs"][0]["documentTab"]["body"]["content"][0]
    node["endIndex"] = 3
    node["paragraph"]["elements"] = [
        {"startIndex": 1, "endIndex": 2, "inlineObjectElement": {"inlineObjectId": "synthetic-image"}},
        {"startIndex": 2, "endIndex": 3, "textRun": {"content": "\n"}},
    ]
    if fault == "missing_elements":
        node["paragraph"] = {}
    elif fault == "empty_elements":
        node["paragraph"]["elements"] = []
    elif fault == "boolean_start":
        node["startIndex"] = True
    elif fault == "float_start":
        node["startIndex"] = 1.0
    elif fault == "nontext_gap":
        node["paragraph"]["elements"][0]["endIndex"] = 1
    elif fault == "missing_newline":
        node["paragraph"]["elements"][1]["textRun"]["content"] = "x"
    elif fault == "wrong_paragraph_end":
        node["endIndex"] = 4
    else:
        node["paragraph"]["elements"][0].pop("inlineObjectElement")
    after = deepcopy(before)
    after["revisionId"] = "rev-2"
    after["tabs"][0]["documentTab"]["body"]["content"][0]["endIndex"] += 2
    client = Client(before, after)
    with pytest.raises(DocsMCPError):
        insert(client, text="🧪", position="start", format_profile="plain", apply=apply)
    assert client.writes == []


def test_insertion_plan_cannot_omit_requested_text_without_segments():
    from google_docs_mcp.client import _insertion_snapshot

    with pytest.raises(DocsMCPError):
        _insertion_snapshot((), index=1, text="🧪")
