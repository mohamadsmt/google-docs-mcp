"""Synthetic scoped-editing contracts independent of Google transport."""
from copy import deepcopy
import importlib

import pytest

from google_docs_mcp.client import DocsMCPError


def common():
    return importlib.import_module("google_docs_mcp.editing_common")


def paragraph(text, start, level=None):
    end = start + len(text.encode("utf-16-le")) // 2
    style = {"namedStyleType": f"HEADING_{level}"} if level else {}
    return {"startIndex": start, "endIndex": end, "paragraph": {
        "paragraphStyle": style, "elements": [{"startIndex": start, "endIndex": end,
        "textRun": {"content": text}}]}}


def sample():
    return {"documentId": "synthetic_doc_123", "revisionId": "r1", "tabs": [{"tabProperties": {"tabId": "t1", "title": "One"},
        "documentTab": {"body": {"content": [paragraph("Title\n", 1, 1),
        paragraph("Text🧪\n", 7), paragraph("Child\n", 14, 2), paragraph("Next\n", 20, 1),
        paragraph("\n", 25)]}}}]}


class Client:
    def __init__(self, doc):
        self.doc = doc

    def drive_metadata(self, document_id):
        return {"id": document_id, "name": "Synthetic", "mimeType": "application/vnd.google-apps.document",
                "modifiedTime": "2026-01-01T00:00:00Z", "version": "1",
                "webViewLink": f"https://docs.google.com/document/d/{document_id}/edit"}

    def get_document(self, document_id):
        return deepcopy(self.doc)


def test_prepare_and_heading_boundaries():
    ctx = common().prepare(Client(sample()), "synthetic_doc_123", "r1")
    assert ctx.selected.tab_id == "t1"
    assert ctx.revision == "r1"
    assert common().heading_range(ctx.selected.body, "Title") == (7, 20)
    assert common().heading_range(ctx.selected.body, "Child", include_heading=True) == (14, 20)
    assert common().heading_range(ctx.selected.body, "Next") == (25, 25)


@pytest.mark.parametrize("heading", ["Missing", "", None])
def test_bad_heading_is_rejected(heading):
    with pytest.raises(DocsMCPError):
        common().heading_range(sample()["tabs"][0]["documentTab"]["body"], heading)


def test_stale_and_multiple_tabs_reject():
    with pytest.raises(DocsMCPError, match="revision"):
        common().prepare(Client(sample()), "synthetic_doc_123", "stale")
    doc = sample()
    second = deepcopy(doc["tabs"][0])
    second["tabProperties"]["tabId"] = "t2"
    doc["tabs"].append(second)
    with pytest.raises(DocsMCPError) as error:
        common().prepare(Client(doc), "synthetic_doc_123", "r1")
    assert error.value.code == "multiple_tabs_require_tab_id"


def test_checked_readback_rejects_changed_other_tab():
    doc = sample()
    second = deepcopy(doc["tabs"][0])
    second["tabProperties"]["tabId"] = "t2"
    doc["tabs"].append(second)
    ctx = common().prepare(Client(doc), "synthetic_doc_123", "r1", "t1")
    after = deepcopy(doc)
    after["revisionId"] = "r2"
    assert common().checked_readback(Client(after), ctx, "r2")[1].tab_id == "t1"
    after["tabs"][1]["documentTab"]["body"]["content"][0]["paragraph"]["elements"][0]["textRun"]["content"] = "bad\n"
    with pytest.raises(DocsMCPError):
        common().checked_readback(Client(after), ctx, "r2")


def test_without_indices_preserves_styles_objects_and_coalesces_runs():
    one = paragraph("ab\n", 1)
    two = deepcopy(one)
    two["paragraph"]["elements"] = [
        {"startIndex": 99, "endIndex": 100, "textRun": {"content": "a"}},
        {"startIndex": 100, "endIndex": 102, "textRun": {"content": "b\n"}},
    ]
    assert common().without_indices(one) == common().without_indices(two)
    two["paragraph"]["elements"][0]["textRun"]["textStyle"] = {"bold": True}
    assert common().without_indices(one) != common().without_indices(two)


def test_table_inventory_reports_no_cell_text():
    cell: dict = {"content": [paragraph("secret\n", 5)]}
    table = {"table": {"rows": 1, "columns": 1, "tableRows": [{"tableCells": [cell]}]}}
    result = common().table_inventory({"content": [table]})
    assert result == [{"table_index": 0, "rows": 1, "columns": 1, "editable": True}]
    cell["tableCellStyle"] = {"columnSpan": 2}
    assert common().table_inventory({"content": [table]})[0]["editable"] is False
