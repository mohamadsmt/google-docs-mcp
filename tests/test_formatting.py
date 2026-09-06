"""Independent fixtures for formatting-only, exact-revision repairs."""
from copy import deepcopy
import importlib

import pytest

from google_docs_mcp.client import DocsMCPError

DOC = "synthetic_doc_123"


def paragraph(text, start, heading=None):
    end = start + len(text.encode("utf-16-le")) // 2
    return {"startIndex": start, "endIndex": end, "paragraph": {
        "paragraphStyle": {"namedStyleType": heading or "NORMAL_TEXT", "spaceAbove": {"magnitude": 6, "unit": "PT"}},
        "elements": [{"startIndex": start, "endIndex": end, "textRun": {
            "content": text, "textStyle": {"bold": False, "weightedFontFamily": {"fontFamily": "Arial", "weight": 400}}}}]}}


def sample():
    linked = paragraph("A🧪 B\n", 15)
    linked["paragraph"]["elements"][0]["textRun"]["textStyle"].update(
        {"bold": True, "link": {"url": "https://example.org/"}})
    linked["paragraph"]["bullet"] = {"listId": "list1", "nestingLevel": 1}
    obj = paragraph("body\n", 41)
    obj["paragraph"]["elements"] = [
        {"startIndex": 41, "endIndex": 42, "textRun": {"content": "b", "textStyle": {"italic": True}}},
        {"startIndex": 42, "endIndex": 43, "inlineObjectElement": {"inlineObjectId": "obj1"}},
        {"startIndex": 43, "endIndex": 46, "textRun": {"content": "dy\n"}}]
    body = {"content": [
        {"endIndex": 1, "sectionBreak": {"sectionStyle": {}}},
        paragraph("Before\n", 1), paragraph("Target\n", 8, "HEADING_1"), linked,
        {"startIndex": 21, "endIndex": 35, "table": {"rows": 1, "columns": 2, "tableRows": [{
            "tableCells": [{"content": [paragraph("\n", 24)], "tableCellStyle": {"paddingLeft": {"magnitude": 9, "unit": "PT"}}},
                           {"content": [paragraph("  x  \n", 27)]}]}]}},
        paragraph("Child\n", 35, "HEADING_2"), obj,
        paragraph("Next\n", 46, "HEADING_1"), paragraph("\n", 51)]}
    tab = {"tabProperties": {"tabId": "t1", "title": "One", "index": 0},
           "documentTab": {"body": body, "lists": {"list1": {"listProperties": {"nestingLevels": [{"glyphType": "DECIMAL"}]}}},
                           "inlineObjects": {"obj1": {"objectId": "obj1"}}, "headers": {"h1": {"content": []}}}}
    other = {"tabProperties": {"tabId": "t2", "title": "Two", "index": 1},
             "documentTab": {"body": {"content": [paragraph("untouched\n", 1)]}}}
    return {"documentId": DOC, "revisionId": "r1", "title": "Synthetic", "tabs": [tab, other]}


def paragraphs(doc):
    # Deliberately fixture-specific; not the implementation's traversal.
    content = doc["tabs"][0]["documentTab"]["body"]["content"]
    return [content[1], content[2], content[3],
            content[4]["table"]["tableRows"][0]["tableCells"][0]["content"][0],
            content[4]["table"]["tableRows"][0]["tableCells"][1]["content"][0],
            content[5], content[6], content[7], content[8]]


def repaired(before, profile="persian", indent=0, section=False):
    after = deepcopy(before)
    after["revisionId"] = "r2"
    for node in paragraphs(after):
        if section and not 8 <= node["startIndex"] < 46:
            continue
        node["paragraph"]["paragraphStyle"].update({
            "direction": "RIGHT_TO_LEFT" if profile == "persian" else "LEFT_TO_RIGHT",
            "alignment": "START",
            "indentStart": {"magnitude": indent if profile == "persian" else 0, "unit": "PT"},
            "indentEnd": {"magnitude": 0 if profile == "persian" else indent, "unit": "PT"}})
        if profile == "persian":
            for element in node["paragraph"]["elements"]:
                if "textRun" in element:
                    element["textRun"].setdefault("textStyle", {}).setdefault("weightedFontFamily", {})["fontFamily"] = "Vazirmatn"
    return after


class Client:
    def __init__(self, before, after=None, response=None, failure=None):
        self.before, self.after = before, after
        self.response = response if response is not None else {"writeControl": {"requiredRevisionId": "r2"}}
        self.failure = failure
        self.writes = []
        self.reads = 0

    def drive_metadata(self, document_id):
        return {"id": document_id, "name": "Synthetic", "mimeType": "application/vnd.google-apps.document",
                "modifiedTime": "2026-01-01T00:00:00Z", "version": "1",
                "webViewLink": f"https://docs.google.com/document/d/{document_id}/edit"}

    def get_document(self, document_id):
        self.reads += 1
        return deepcopy(self.after if self.writes else self.before)

    def batch_update(self, document_id, requests, revision, *, retry_safe=True):
        self.writes.append((document_id, deepcopy(requests), revision, retry_safe))
        if self.failure:
            raise self.failure
        return deepcopy(self.response)


def run(client, **kwargs):
    options = {"tab_id": "t1", **kwargs}
    return importlib.import_module("google_docs_mcp.formatting").format_document(client, DOC, options.pop("expected_revision_id", "r1"), **options)


def test_preview_counts_each_mismatch_including_blank_cells_without_writes():
    client = Client(sample())
    result = run(client, heading_text="Target", right_indent_pt=24)
    assert result["applied"] is False
    assert result["scope"] == {"tab_id": "t1", "heading_text": "Target", "start_index": 8, "end_index": 46}
    assert result["mismatches"] == {"direction": 6, "alignment": 6, "indentStart": 6, "indentEnd": 6, "fontFamily": 7}
    assert result["no_op"] is False
    assert client.writes == []


@pytest.mark.parametrize("profile,indent", [("persian", 24), ("english", 72.5), ("persian", 144)])
def test_apply_payload_is_only_scoped_style_fields_and_verifies_independent_readback(profile, indent):
    before = sample()
    client = Client(before, repaired(before, profile, indent, section=True))
    result = run(client, heading_text="Target", format_profile=profile, right_indent_pt=indent, apply=True)
    assert result["verified"] is True and result["after_revision_id"] == "r2"
    assert len(client.writes) == 1
    doc, requests, revision, retry_safe = client.writes[0]
    assert (doc, revision, retry_safe) == (DOC, "r1", False)
    assert requests
    for request in requests:
        assert set(request) <= {"updateParagraphStyle", "updateTextStyle"}
        value = next(iter(request.values()))
        assert value["range"]["tabId"] == "t1"
        assert 8 <= value["range"]["startIndex"] < value["range"]["endIndex"] <= 46
        if "updateParagraphStyle" in request:
            assert value["fields"] == "direction,alignment,indentStart,indentEnd"
            assert value["paragraphStyle"] == {
                "direction": "RIGHT_TO_LEFT" if profile == "persian" else "LEFT_TO_RIGHT",
                "alignment": "START",
                "indentStart": {"magnitude": indent if profile == "persian" else 0, "unit": "PT"},
                "indentEnd": {"magnitude": 0 if profile == "persian" else indent, "unit": "PT"}}
        else:
            assert profile == "persian"
            assert value["fields"] == "weightedFontFamily,bold"
            original = next(e["textRun"] for n in paragraphs(before) for e in n["paragraph"]["elements"]
                            if e["startIndex"] == value["range"]["startIndex"])
            assert value["textStyle"] == {"weightedFontFamily": {"fontFamily": "Vazirmatn", "weight": 400},
                                          "bold": original.get("textStyle", {}).get("bold", False)}
    assert before == sample()


@pytest.mark.parametrize("field", ["direction", "alignment", "indentStart", "indentEnd", "fontFamily"])
def test_each_field_repaired_independently_and_compliant_fields_still_verified(field):
    good = repaired(sample())
    good["revisionId"] = "r1"
    before = deepcopy(good)
    paragraph = paragraphs(before)[2]["paragraph"]
    if field == "fontFamily":
        paragraph["elements"][0]["textRun"]["textStyle"]["weightedFontFamily"]["fontFamily"] = "Arial"
    else:
        paragraph["paragraphStyle"].pop(field)
    after = deepcopy(good)
    after["revisionId"] = "r2"
    client = Client(before, after)
    assert run(client, apply=True)["verified"] is True
    requests = client.writes[0][1]
    assert len(requests) == 1
    assert next(iter(requests[0].values()))["fields"] == ("weightedFontFamily,bold" if field == "fontFamily" else field)
    other = "alignment" if field != "alignment" else "direction"
    paragraphs(after)[2]["paragraph"]["paragraphStyle"].pop(other)
    with pytest.raises(DocsMCPError) as error:
        run(Client(before, after), apply=True)
    assert error.value.code == "verification_failed"


@pytest.mark.parametrize("profile", ["persian", "english"])
def test_compliant_whole_tab_is_verified_noop_even_for_final_blank_paragraph(profile):
    before = repaired(sample(), profile=profile)
    before["revisionId"] = "r1"
    client = Client(before)
    result = run(client, format_profile=profile, apply=True)
    assert result["no_op"] is True and result["verified"] is True
    assert not any(result["mismatches"].values())
    assert result["revision_id"] == "r1"
    assert client.writes == []


@pytest.mark.parametrize("inherited_weight", [None, 400, 600])
def test_font_repair_preserves_resolved_weight_and_accepts_materialized_default(inherited_weight):
    before = sample()
    paragraphs(before)[0]["paragraph"]["elements"][0]["textRun"]["textStyle"].pop("weightedFontFamily")
    if inherited_weight is not None:
        before["tabs"][0]["documentTab"]["namedStyles"] = {"styles": [{
            "namedStyleType": "NORMAL_TEXT", "textStyle": {
                "weightedFontFamily": {"fontFamily": "Arial", "weight": inherited_weight}}}]}
    after = repaired(before)
    for old, new in zip(paragraphs(before), paragraphs(after), strict=True):
        for a, b in zip(old["paragraph"]["elements"], new["paragraph"]["elements"], strict=True):
            if "textRun" in a and "weightedFontFamily" not in a["textRun"].get("textStyle", {}):
                b["textRun"]["textStyle"]["weightedFontFamily"]["weight"] = inherited_weight or 400
    client = Client(before, after)
    assert run(client, apply=True)["verified"]
    request = next(r["updateTextStyle"] for r in client.writes[0][1]
                   if "updateTextStyle" in r and r["updateTextStyle"]["range"]["startIndex"] == 1)
    assert request["textStyle"]["weightedFontFamily"] == {
        "fontFamily": "Vazirmatn", "weight": inherited_weight or 400}
    paragraphs(after)[0]["paragraph"]["elements"][0]["textRun"]["textStyle"]["weightedFontFamily"]["weight"] = 900
    with pytest.raises(DocsMCPError):
        run(Client(before, after), apply=True)


@pytest.mark.parametrize("explicit,inherited", [(True, False), (False, True), (None, True), (None, False)])
def test_font_change_explicitly_reapplies_resolved_bold(explicit, inherited):
    before = sample()
    style = paragraphs(before)[0]["paragraph"]["elements"][0]["textRun"]["textStyle"]
    if explicit is None:
        style.pop("bold")
    else:
        style["bold"] = explicit
    before["tabs"][0]["documentTab"]["namedStyles"] = {"styles": [{
        "namedStyleType": "NORMAL_TEXT", "textStyle": {"bold": inherited}}]}
    after = repaired(before)
    expected = inherited if explicit is None else explicit
    paragraphs(after)[0]["paragraph"]["elements"][0]["textRun"]["textStyle"]["bold"] = expected
    client = Client(before, after)
    assert run(client, apply=True)["verified"]
    request = next(r["updateTextStyle"] for r in client.writes[0][1]
                   if "updateTextStyle" in r and r["updateTextStyle"]["range"]["startIndex"] == 1)
    assert request["fields"] == "weightedFontFamily,bold"
    assert request["textStyle"]["bold"] is expected


@pytest.mark.parametrize("kwargs", [
    {"right_indent_pt": x} for x in [True, "2", -1, 145, float("nan"), float("inf"), None, 10**1000]
] + [{"format_profile": x} for x in ["plain", "Persian", None, []]]
  + [{"apply": x} for x in [1, "true", None]]
  + [{"heading_text": x} for x in ["", [], 123]]
  + [{"tab_id": x} for x in ["", [], "missing"]])
def test_strict_invalid_inputs_never_write(kwargs):
    client = Client(sample())
    with pytest.raises(DocsMCPError):
        run(client, **kwargs)
    assert not client.writes


@pytest.mark.parametrize("kwargs,code", [({"expected_revision_id": "stale"}, "stale_revision"),
    ({"tab_id": None}, "multiple_tabs_require_tab_id"), ({"heading_text": "missing"}, "heading_match_mismatch")])
def test_scope_and_revision_fail_closed(kwargs, code):
    client = Client(sample())
    with pytest.raises(DocsMCPError) as error:
        run(client, apply=True, **kwargs)
    assert error.value.code == code and not client.writes


def test_ambiguous_heading_never_writes():
    before = sample()
    paragraphs(before)[-2]["paragraph"]["elements"][0]["textRun"]["content"] = "Target\n"
    client = Client(before)
    with pytest.raises(DocsMCPError) as error:
        run(client, heading_text="Target", apply=True)
    assert error.value.code == "heading_match_mismatch" and not client.writes


@pytest.mark.parametrize("corruption", ["text", "bold", "link", "heading", "list", "object", "padding", "other_scope", "other_tab", "header", "revision"])
def test_incorrect_readback_rejects_unrelated_damage(corruption):
    before = sample()
    after = repaired(before, section=True)
    target = paragraphs(after)[2]["paragraph"]
    if corruption == "text":
        target["elements"][0]["textRun"]["content"] = "changed\n"
    elif corruption in {"bold", "link"}:
        target["elements"][0]["textRun"]["textStyle"].pop(corruption)
    elif corruption == "heading":
        paragraphs(after)[1]["paragraph"]["paragraphStyle"]["namedStyleType"] = "NORMAL_TEXT"
    elif corruption == "list":
        target["bullet"]["listId"] = "other"
    elif corruption == "object":
        paragraphs(after)[6]["paragraph"]["elements"][1]["inlineObjectElement"]["inlineObjectId"] = "other"
    elif corruption == "padding":
        after["tabs"][0]["documentTab"]["body"]["content"][4]["table"]["tableRows"][0]["tableCells"][0]["tableCellStyle"] = {}
    elif corruption == "other_scope":
        paragraphs(after)[0]["paragraph"]["paragraphStyle"]["direction"] = "RIGHT_TO_LEFT"
    elif corruption == "other_tab":
        after["tabs"][1]["tabProperties"]["title"] = "other"
    elif corruption == "header":
        after["tabs"][0]["documentTab"]["headers"] = {}
    else:
        after["revisionId"] = "r3"
    with pytest.raises(DocsMCPError) as error:
        run(Client(before, after), heading_text="Target", apply=True)
    assert error.value.code == "verification_failed"


@pytest.mark.parametrize("response", [{}, {"writeControl": {"requiredRevisionId": "r1"}}, {"writeControl": None}])
def test_bad_batch_response_is_not_retried(response):
    client = Client(sample(), repaired(sample()), response=response)
    with pytest.raises(DocsMCPError):
        run(client, apply=True)
    assert len(client.writes) == 1


def test_uncertain_transport_is_sanitized_and_not_retried():
    client = Client(sample(), failure=RuntimeError("secret_token"))
    with pytest.raises(DocsMCPError) as error:
        run(client, apply=True)
    assert "secret_token" not in str(error.value)
    assert error.value.retryable is False and len(client.writes) == 1


@pytest.mark.parametrize("malformed", [None, {"content": []}, {"content": [None]}, {"content": [{"endIndex": 5, "paragraph": {"elements": None}}]}])
def test_malformed_remote_body_is_sanitized_without_mutation(malformed):
    before = sample()
    before["tabs"][0]["documentTab"]["body"] = malformed
    client = Client(before)
    with pytest.raises(DocsMCPError):
        run(client, apply=True)
    assert not client.writes
