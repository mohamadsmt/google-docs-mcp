"""Independent scripted Google structures, not a production-request simulator."""
from copy import deepcopy
import importlib
from pathlib import Path

import pytest

from google_docs_mcp.client import DocsMCPError


DOC = "synthetic_table_doc_123"
LINK = "https://example.com/سلام"
PERSIAN = {
    "direction": "RIGHT_TO_LEFT", "alignment": "START",
    "indentStart": {"magnitude": 0, "unit": "PT"},
    "indentEnd": {"magnitude": 0, "unit": "PT"},
}
FONT = {"weightedFontFamily": {"fontFamily": "Vazirmatn"}}


def edit(*args, **kwargs):
    return importlib.import_module("google_docs_mcp.tables").edit_table(*args, **kwargs)


def units(text):
    return len(text.encode("utf-16-le")) // 2


def cell(text, *, runs=None, persian=False, normal=False):
    style = {"namedStyleType": "NORMAL_TEXT" if normal else "NORMAL_TEXT",
             "direction": "LEFT_TO_RIGHT", "alignment": "START"}
    if persian:
        style.update(deepcopy(PERSIAN))
    if runs is None:
        runs = [(text + "\n", {"italic": True})]
    if persian:
        runs = [(t, {**s, **FONT}) for t, s in runs]
    return {"paragraphStyle": style, "runs": deepcopy(runs)}


def paragraph(start, value):
    cursor = start
    elements = []
    for text, style in value["runs"]:
        end = cursor + units(text)
        elements.append({"startIndex": cursor, "endIndex": end,
                         "textRun": {"content": text, "textStyle": deepcopy(style)}})
        cursor = end
    return {"startIndex": start, "endIndex": cursor,
            "paragraph": {"elements": elements,
                          "paragraphStyle": deepcopy(value["paragraphStyle"])}}


def grid():
    return [[cell("A🧪", runs=[("A🧪\n", {"bold": True, "link": {"url": LINK}})]), cell("ب")],
            [cell("C"), cell("D")]]


def table(start, values, row_ids=None, widths=None):
    """Assign actual UTF-16 positions from independently specified cell values."""
    cursor = start + 1
    rows = []
    row_ids = row_ids if row_ids is not None else list(range(len(values)))
    widths = widths if widths is not None else [70, 90][:len(values[0])]
    for row_id, values_row in zip(row_ids, values, strict=True):
        row_start = cursor
        cursor += 1
        cells = []
        for value in values_row:
            start_cell = cursor
            p = paragraph(cursor + 1, value)
            cursor = p["endIndex"]
            cells.append({"startIndex": start_cell, "endIndex": cursor,
                          "tableCellStyle": {"rowSpan": 1, "columnSpan": 1,
                                             "paddingTop": {"magnitude": 5, "unit": "PT"}},
                          "content": [p]})
        rows.append({"startIndex": row_start, "endIndex": cursor,
                     "tableRowStyle": {"minRowHeight": {"magnitude": 10 + row_id, "unit": "PT"}},
                     "tableCells": cells})
    return {"startIndex": start, "endIndex": cursor, "table": {
        "rows": len(values), "columns": len(values[0]), "tableRows": rows,
        "tableStyle": {"tableColumnProperties": [
            {"widthType": "FIXED_WIDTH", "columnWidth": {"magnitude": w, "unit": "PT"}}
            for w in widths]}}}


def document(values=None, *, revision="r1", row_ids=None, widths=None, second_tab=False):
    values = grid() if values is None else values
    prefix = paragraph(1, cell("Outside🧪"))
    prefix["paragraph"]["bullet"] = {"listId": "keep-list", "nestingLevel": 1}
    first = table(prefix["endIndex"], [[cell("Other table")]], widths=[40])
    target = table(first["endIndex"], values, row_ids, widths)
    suffix = paragraph(target["endIndex"], cell("Suffix"))
    # An unsupported object outside scope must survive, not block the edit.
    suffix["paragraph"]["elements"].append({"inlineObjectElement": {"inlineObjectId": "keep-object"}})
    doc = {"documentId": DOC, "revisionId": revision, "title": "Synthetic",
           "documentStyle": {"background": {"color": {"color": {"rgbColor": {"red": 1}}}}},
           "tabs": [{"tabProperties": {"tabId": "t1", "title": "Main"},
                     "documentTab": {"body": {"content": [prefix, first, target, suffix]},
                                     "lists": {"keep-list": {"listProperties": {}}}}}]}
    if second_tab:
        doc["tabs"].append({"tabProperties": {"tabId": "t2", "title": "Other"},
                            "documentTab": {"body": {"content": [paragraph(1, cell("Untouched"))]}}})
    return doc


def target(doc):
    return doc["tabs"][0]["documentTab"]["body"]["content"][2]


def target_cell(doc, row=0, col=0):
    return target(doc)["table"]["tableRows"][row]["tableCells"][col]


class Client:
    def __init__(self, *documents, fail_batch=None, response=None, export_error=False):
        self.documents = list(documents)
        self.reads = 0
        self.batches = []
        self.exports = []
        self.fail_batch = fail_batch
        self.response = response
        self.export_error = export_error

    def drive_metadata(self, document_id):
        return {"id": document_id, "name": "Synthetic", "mimeType": "application/vnd.google-apps.document",
                "modifiedTime": "2026-01-01T00:00:00Z", "version": "1",
                "webViewLink": f"https://docs.google.com/document/d/{document_id}/edit"}

    def get_document(self, document_id):
        assert document_id == DOC
        assert self.reads < len(self.documents), "Unexpected additional document read"
        result = deepcopy(self.documents[self.reads])
        self.reads += 1
        return result

    def batch_update(self, document_id, requests, revision_id, *, retry_safe):
        assert document_id == DOC
        self.batches.append((deepcopy(requests), revision_id, retry_safe))
        if len(self.batches) == self.fail_batch:
            raise TimeoutError("secret URL and transport payload must not escape")
        return self.response if self.response is not None else {
            "writeControl": {"requiredRevisionId": f"r{len(self.batches) + 1}"}}

    def export_file(self, document_id, mime_type):
        self.exports.append((document_id, mime_type))
        if self.export_error:
            raise OSError("secret export error")
        return b"synthetic recovery bytes"


def invoke(client, tmp_path, **kwargs):
    args = {"action": "set_cell", "table_index": 1, "row_index": 0,
            "column_index": 0, "markdown": "new", "apply": False}
    args.update(kwargs)
    return edit(client, tmp_path / "recovery", DOC, "r1", **args)


def assert_recovery(error, tmp_path, phase=None):
    assert error.value.code == "partial_write_requires_recovery"
    details = error.value.as_result()["error"]
    if phase:
        assert details["phase"] == phase
    path = Path(details["recovery_path"])
    assert path.is_relative_to(tmp_path)
    assert (path / "document.txt").read_bytes() == b"synthetic recovery bytes"
    assert (path / "document.docx").read_bytes() == b"synthetic recovery bytes"
    assert "secret" not in str(details)
    assert details["retryable"] is False


def test_preview_resolves_second_table_without_exports_or_writes(tmp_path):
    client = Client(document())
    result = invoke(client, tmp_path)
    assert result["ok"] is True and result["applied"] is False
    assert result["action"] == "set_cell"
    assert result["tab_id"] == "t1"
    assert result["scope"] == {"table_index": 1, "rows": 2, "columns": 2,
                               "row_index": 0, "column_index": 0}
    assert client.batches == client.exports == []
    assert not (tmp_path / "recovery").exists()


@pytest.mark.parametrize("updates", [
    {"action": "bogus"}, {"action": []}, {"action": None}, {"table_index": True},
    {"table_index": -1}, {"table_index": 1.0}, {"table_index": "1"},
    {"row_index": True}, {"row_index": -1}, {"row_index": 2}, {"row_index": None},
    {"column_index": 2}, {"column_index": False}, {"column_index": 0.0},
    {"column_index": None}, {"markdown": None}, {"markdown": 1},
    {"side": "before"}, {"apply": 1}, {"apply": "false"},
    {"format_profile": "english"}, {"format_profile": []},
    {"table_index": 7}, {"tab_id": "unknown"},
])
def test_invalid_set_arguments_fail_without_writes(tmp_path, updates):
    client = Client(document())
    with pytest.raises(DocsMCPError):
        invoke(client, tmp_path, **updates)
    assert client.batches == client.exports == []


@pytest.mark.parametrize("action,selectors", [
    ("insert_row", {"row_index": 0, "side": "before"}),
    ("insert_column", {"column_index": 0, "side": "after"}),
    ("delete_row", {"row_index": 0}), ("delete_column", {"column_index": 0}),
])
def test_structural_actions_strict_combinations(tmp_path, action, selectors):
    defaults = {"row_index": None, "column_index": None, "markdown": None, "side": None}
    valid = {**defaults, **selectors, "action": action}
    assert invoke(Client(document()), tmp_path, **valid)["applied"] is False
    invalid = [{"markdown": ""}]
    irrelevant = "column_index" if action.endswith("row") else "row_index"
    required = "row_index" if action.endswith("row") else "column_index"
    invalid += [{irrelevant: 0}, {required: None}, {required: True}, {required: 2}]
    invalid += [{"side": None}, {"side": "left"}, {"side": []}] if action.startswith("insert") else [{"side": "before"}]
    for change in invalid:
        client = Client(document())
        with pytest.raises(DocsMCPError):
            invoke(client, tmp_path, **{**valid, **change})
        assert client.batches == client.exports == []


@pytest.mark.parametrize("markdown", ["# Heading", "| a | b |\n| --- | --- |", "one\ntwo", "one\r\ntwo",
                                     "- list", "```code```", "`code`", "[[note]]", "![image](https://x.test)",
                                     "x\x00", "x\ue000", "x\ud800", "⟦TABLE-0001⟧", "x" * 500001])
def test_non_inline_or_unsafe_markdown_rejected(tmp_path, markdown):
    client = Client(document())
    with pytest.raises(DocsMCPError):
        invoke(client, tmp_path, markdown=markdown)
    assert client.batches == client.exports == []


@pytest.mark.parametrize("kind", ["row_span", "column_span", "nested", "ragged", "rows_bool", "columns_mismatch",
                                   "cells_wrong_type", "content_missing", "index_bool", "wrong_run_length"])
def test_unsupported_or_malformed_target_rejected_before_export(tmp_path, kind):
    doc = document()
    node = target(doc)
    c = target_cell(doc)
    if kind == "row_span": c["tableCellStyle"]["rowSpan"] = 2
    if kind == "column_span": c["tableCellStyle"]["columnSpan"] = 2
    if kind == "nested": c["content"].append({"table": {"rows": 1}})
    if kind == "ragged": node["table"]["tableRows"][1]["tableCells"].pop()
    if kind == "rows_bool": node["table"]["rows"] = True
    if kind == "columns_mismatch": node["table"]["columns"] = 3
    if kind == "cells_wrong_type": node["table"]["tableRows"][0]["tableCells"] = None
    if kind == "content_missing": c["content"] = []
    if kind == "index_bool": c["content"][0]["startIndex"] = True
    if kind == "wrong_run_length": c["content"][0]["elements"] = []  # replaced below
    if kind == "wrong_run_length": c["content"][0]["paragraph"]["elements"][0]["endIndex"] += 1
    client = Client(doc)
    with pytest.raises(DocsMCPError):
        invoke(client, tmp_path, apply=True)
    assert client.batches == client.exports == []


@pytest.mark.parametrize("action,values,selectors", [
    ("delete_row", [grid()[0]], {"row_index": 0}),
    ("delete_column", [[grid()[0][0]], [grid()[1][0]]], {"column_index": 0}),
])
def test_cannot_delete_last_dimension(tmp_path, action, values, selectors):
    client = Client(document(values, widths=[70] if action.endswith("column") else [70, 90]))
    with pytest.raises(DocsMCPError):
        invoke(client, tmp_path, action=action, markdown=None, apply=True,
               **{"row_index": None, "column_index": None, **selectors})
    assert client.batches == client.exports == []


def test_stale_and_multi_tab_selection(tmp_path):
    client = Client(document(revision="changed"))
    with pytest.raises(DocsMCPError) as error:
        invoke(client, tmp_path, apply=True)
    assert error.value.code == "stale_revision"
    client = Client(document(second_tab=True))
    with pytest.raises(DocsMCPError) as error:
        invoke(client, tmp_path)
    assert error.value.code == "multiple_tabs_require_tab_id"
    assert invoke(Client(document(second_tab=True)), tmp_path, tab_id="t1")["tab_id"] == "t1"
    assert client.batches == client.exports == []


def set_documents(text="نو🧪 link", *, persian=True, second_tab=False):
    unstyled = grid()
    unstyled[0][0] = cell(text)
    styled = grid()
    runs = [("نو🧪", {"bold": True}), (" ", {"bold": False}),
            ("link", {"bold": False, "link": {"url": LINK}}), ("\n", {"bold": False})]
    if text == "": runs = [("\n", {"bold": False})]
    styled[0][0] = cell(text, runs=runs, persian=persian, normal=True)
    return (document(second_tab=second_tab), document(unstyled, revision="r2", second_tab=second_tab),
            document(styled, revision="r3", second_tab=second_tab))


@pytest.mark.parametrize("profile", ["persian", "plain"])
def test_set_cell_unicode_bold_link_payload_and_exact_readback(tmp_path, profile):
    docs = set_documents(persian=profile == "persian", second_tab=True)
    client = Client(*docs)
    result = invoke(client, tmp_path, markdown=f"**نو🧪** [link]({LINK})", format_profile=profile,
                    apply=True, tab_id="t1")
    assert result["verified"] is True and result["applied"] is True
    assert result["before_revision_id"] == "r1" and result["after_revision_id"] == "r3"
    start = target_cell(docs[0])["content"][0]["startIndex"]
    assert client.batches[0] == ([
        {"deleteContentRange": {"range": {"startIndex": start, "endIndex": start + 3, "tabId": "t1"}}},
        {"insertText": {"location": {"index": start, "tabId": "t1"}, "text": "نو🧪 link"}},
    ], "r1", False)
    styles, revision, retry = client.batches[1]
    assert (revision, retry) == ("r2", False)
    text_styles = [x["updateTextStyle"] for x in styles if "updateTextStyle" in x]
    assert {"range": {"startIndex": start, "endIndex": start + 4, "tabId": "t1"},
            "textStyle": {"bold": True}, "fields": "bold"} in text_styles
    assert {"range": {"startIndex": start + 5, "endIndex": start + 9, "tabId": "t1"},
            "textStyle": {"link": {"url": LINK}}, "fields": "link"} in text_styles
    assert any(x["fields"] == "bold,link" and x["textStyle"] == {"bold": False} for x in text_styles)
    for request in styles:
        value = next(iter(request.values()))
        assert value["range"]["startIndex"] == start or value["range"]["startIndex"] == start + 5
        assert value["range"]["endIndex"] <= start + 10
        assert "*" not in value.get("fields", "")
    assert len(client.exports) == 2 and client.reads == 3
    assert list((tmp_path / "recovery").iterdir()) == []


def test_empty_set_retains_mandatory_paragraph(tmp_path):
    docs = set_documents(text="")
    client = Client(*docs)
    assert invoke(client, tmp_path, markdown="", apply=True)["verified"]
    assert list(client.batches[0][0][0]) == ["deleteContentRange"]
    assert len(client.batches[0][0]) == 1
    assert any(x.get("updateTextStyle", {}).get("fields") == "weightedFontFamily" for x in client.batches[1][0])


@pytest.mark.parametrize("action,side", [("insert_row", "before"), ("insert_row", "after"),
                                         ("insert_column", "before"), ("insert_column", "after")])
@pytest.mark.parametrize("profile", ["persian", "plain"])
@pytest.mark.parametrize("reverse_column_order", [False, True])
def test_insert_dimension_styles_only_new_cells_from_actual_indices(tmp_path, action, side, profile, reverse_column_order):
    position = 0 if side == "before" else 1
    # Live RTL readback places insertRight before the reference in the logical
    # cell array. Physical side is requested, not inferred from array order.
    if action == "insert_column" and reverse_column_order:
        position = 1 - position
    new = grid()
    row_ids, widths = [0, 1], [70, 90]
    if action == "insert_row":
        new.insert(position, [cell(""), cell("")])
        row_ids.insert(position, 9)
        coordinates = [(position, 0), (position, 1)]
    else:
        for row in new: row.insert(position, cell(""))
        widths.insert(position, 120)
        coordinates = [(0, position), (1, position)]
    styled = deepcopy(new)
    for r, c in coordinates:
        styled[r][c] = cell("", persian=profile == "persian")
    before = document()
    middle = document(new, revision="r2", row_ids=row_ids, widths=widths)
    final = document(styled, revision="r3", row_ids=row_ids, widths=widths)
    client = Client(before, middle, final) if profile == "persian" else Client(before, middle)
    selectors = {"row_index": 0 if action.endswith("row") else None,
                 "column_index": 0 if action.endswith("column") else None}
    result = invoke(client, tmp_path, action=action, markdown=None, side=side, format_profile=profile,
                    apply=True, **selectors)
    assert result["verified"]
    key = "insertTableRow" if action.endswith("row") else "insertTableColumn"
    option = "insertBelow" if action.endswith("row") else "insertRight"
    assert client.batches[0] == ([{key: {"tableCellLocation": {
        "tableStartLocation": {"index": target(before)["startIndex"], "tabId": "t1"},
        "rowIndex": 0, "columnIndex": 0}, option: side == "after"}}], "r1", False)
    if profile == "persian":
        assert len(client.batches) == 2
        expected_ranges = [{"startIndex": target_cell(middle, r, c)["content"][0]["startIndex"],
                            "endIndex": target_cell(middle, r, c)["content"][0]["endIndex"], "tabId": "t1"}
                           for r, c in coordinates]
        assert len(client.batches[1][0]) == 4
        for req in client.batches[1][0]:
            value = next(iter(req.values()))
            assert value["range"] in expected_ranges
            if "updateParagraphStyle" in req:
                assert value["paragraphStyle"] == PERSIAN
                assert value["fields"] == "direction,alignment,indentStart,indentEnd"
            else:
                assert value["textStyle"] == FONT and value["fields"] == "weightedFontFamily"
        assert client.batches[1][1:] == ("r2", False)
    else:
        assert len(client.batches) == 1
    assert list((tmp_path / "recovery").iterdir()) == []


def test_ambiguous_column_identity_stops_before_cell_styling(tmp_path):
    before = document([[cell(""), cell("")], [cell(""), cell("")]], widths=[70, 70])
    after = document([[cell(""), cell(""), cell("")], [cell(""), cell(""), cell("")]],
                     widths=[70, 70, 70], revision="r2")
    client = Client(before, after)
    with pytest.raises(DocsMCPError) as error:
        invoke(client, tmp_path, action="insert_column", row_index=None, column_index=0,
               markdown=None, side="after", format_profile="plain", apply=True)
    assert_recovery(error, tmp_path)
    assert len(client.batches) == 1


@pytest.mark.parametrize("action,index", [("delete_row", 0), ("delete_row", 1), ("delete_column", 0), ("delete_column", 1)])
def test_delete_dimension_preserves_shifted_identities_and_styles(tmp_path, action, index):
    values = grid()
    row_ids, widths = [0, 1], [70, 90]
    if action.endswith("row"):
        values.pop(index)
        row_ids.pop(index)
    else:
        for row in values: row.pop(index)
        widths.pop(index)
    before = document(second_tab=True)
    after = document(values, revision="r2", row_ids=row_ids, widths=widths, second_tab=True)
    client = Client(before, after)
    result = invoke(client, tmp_path, action=action, markdown=None, apply=True, tab_id="t1",
                    row_index=index if action.endswith("row") else None,
                    column_index=index if action.endswith("column") else None)
    assert result["verified"] and result["after_revision_id"] == "r2"
    key = "deleteTableRow" if action.endswith("row") else "deleteTableColumn"
    assert client.batches == [([{key: {"tableCellLocation": {
        "tableStartLocation": {"index": target(before)["startIndex"], "tabId": "t1"},
        "rowIndex": index if action.endswith("row") else 0,
        "columnIndex": index if action.endswith("column") else 0}}}], "r1", False)]


@pytest.mark.parametrize("corruption", ["target_text", "bold", "link", "paragraph_style", "font", "outside_text",
                                       "outside_style", "unrelated_cell", "other_tab", "document_style", "revision", "row_style"])
def test_set_final_corruption_retains_recovery(tmp_path, corruption):
    docs = list(set_documents(second_tab=True))
    after = docs[-1]
    p = target_cell(after)["content"][0]["paragraph"]
    if corruption == "target_text": p["elements"][0]["textRun"]["content"] = "خر🧪"
    if corruption == "bold": p["elements"][0]["textRun"]["textStyle"]["bold"] = False
    if corruption == "link": p["elements"][2]["textRun"]["textStyle"]["link"] = {"url": "https://wrong.test"}
    if corruption == "paragraph_style": p["paragraphStyle"]["alignment"] = "END"
    if corruption == "font": p["elements"][0]["textRun"]["textStyle"]["weightedFontFamily"] = {"fontFamily": "Arial"}
    body = after["tabs"][0]["documentTab"]["body"]
    if corruption == "outside_text": body["content"][3]["paragraph"]["elements"][0]["textRun"]["content"] = "Damage\n"
    if corruption == "outside_style": body["content"][0]["paragraph"]["bullet"]["listId"] = "changed"
    if corruption == "unrelated_cell": target_cell(after, 1, 1)["content"][0]["paragraph"]["elements"][0]["textRun"]["textStyle"]["italic"] = False
    if corruption == "other_tab": after["tabs"][1]["tabProperties"]["title"] = "Changed"
    if corruption == "document_style": after["documentStyle"] = {}
    if corruption == "revision": after["revisionId"] = "interloper"
    if corruption == "row_style": target(after)["table"]["tableRows"][0]["tableRowStyle"] = {}
    client = Client(*docs)
    with pytest.raises(DocsMCPError) as error:
        invoke(client, tmp_path, markdown=f"**نو🧪** [link]({LINK})", apply=True, tab_id="t1")
    assert_recovery(error, tmp_path)
    assert len(client.batches) == 2


@pytest.mark.parametrize("corruption", ["dimensions", "identity", "nonempty", "cell_style", "column_width"])
def test_insert_structural_corruption_stops_before_styling(tmp_path, corruption):
    values = [grid()[0], [cell(""), cell("")], grid()[1]]
    after = document(values, revision="r2", row_ids=[0, 9, 1])
    if corruption == "dimensions": target(after)["table"]["rows"] = 2
    if corruption == "identity": target(after)["table"]["tableRows"][0], target(after)["table"]["tableRows"][2] = target(after)["table"]["tableRows"][2], target(after)["table"]["tableRows"][0]
    if corruption == "nonempty": target_cell(after, 1, 0)["content"][0]["paragraph"]["elements"][0]["textRun"]["content"] = "x\n"
    if corruption == "cell_style": target_cell(after, 2, 1)["tableCellStyle"]["paddingTop"]["magnitude"] = 99
    if corruption == "column_width": target(after)["table"]["tableStyle"]["tableColumnProperties"][1]["columnWidth"]["magnitude"] = 3
    client = Client(document(), after)
    with pytest.raises(DocsMCPError) as error:
        invoke(client, tmp_path, action="insert_row", row_index=0, column_index=None,
               markdown=None, side="after", apply=True)
    assert_recovery(error, tmp_path)
    assert len(client.batches) == 1


@pytest.mark.parametrize("batch", [1, 2])
def test_uncertain_transport_not_retried_and_recovery_retained(tmp_path, batch):
    client = Client(*set_documents(), fail_batch=batch)
    with pytest.raises(DocsMCPError) as error:
        invoke(client, tmp_path, markdown=f"**نو🧪** [link]({LINK})", apply=True)
    assert_recovery(error, tmp_path)
    assert len(client.batches) == batch
    assert all(retry is False for _, _, retry in client.batches)
    assert error.value.as_result()["error"]["revision_id"] == ("r1" if batch == 1 else "r2")


@pytest.mark.parametrize("response", [{}, {"writeControl": {"requiredRevisionId": "r1"}}])
def test_bad_batch_revision_is_uncertain(tmp_path, response):
    client = Client(document(), response=response)
    with pytest.raises(DocsMCPError) as error:
        invoke(client, tmp_path, apply=True)
    assert_recovery(error, tmp_path)
    assert len(client.batches) == 1


def test_export_failure_prevents_mutation(tmp_path):
    client = Client(document(), export_error=True)
    with pytest.raises(DocsMCPError) as error:
        invoke(client, tmp_path, apply=True)
    assert error.value.code == "google_unavailable"
    assert "recovery backup" in error.value.message
    assert client.batches == []
