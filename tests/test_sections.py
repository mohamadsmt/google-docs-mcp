"""Independent raw Google fixtures, not a simulation of emitted requests."""
from copy import deepcopy
import importlib
import importlib.util
from pathlib import Path

import pytest

from google_docs_mcp.client import DocsMCPError

DOC = "synthetic_sections_123"
TAB = "t1"
URL = "https://example.com/x"
TRANSPORT_CANARY = "SECTIONS_TRANSPORT_SECRET_CANARY_7ec910fa"
PERSIAN = {"direction": "RIGHT_TO_LEFT", "alignment": "END",
           "indentStart": {"magnitude": 0, "unit": "PT"},
           "indentEnd": {"magnitude": 0, "unit": "PT"}}
FONT = {"weightedFontFamily": {"fontFamily": "Vazirmatn"}}


def edit(client, root, markdown="New", **kwargs):
    assert importlib.util.find_spec("google_docs_mcp.sections") is not None, "section editing is missing"
    return importlib.import_module("google_docs_mcp.sections").edit_section(
        client, root, kwargs.pop("document", DOC), markdown,
        kwargs.pop("expected_revision_id", "r1"), **kwargs)


def p(text, *, level=None, runs=None, style=None, bullet=None, persian=False):
    ps = {"namedStyleType": f"HEADING_{level}" if level else "NORMAL_TEXT"}
    ps.update(PERSIAN if persian else {})
    ps.update(style or {})
    elements = []
    for value, ts in (runs if runs is not None else [(text, {})]):
        ts = {**(FONT if persian else {}), **ts}
        elements.append({"textRun": {"content": value, "textStyle": ts}})
    paragraph = {"paragraphStyle": ps, "elements": elements}
    if bullet is not None:
        paragraph["bullet"] = bullet
    return {"paragraph": paragraph}


def table(rows, *, persian=False):
    cells = []
    for row in rows:
        cells.append({"tableCells": [{"tableCellStyle": {"rowSpan": 1, "columnSpan": 1},
            "content": [p(value + "\n", persian=persian) if isinstance(value, str) else value]}
            for value in row]})
    return {"table": {"rows": len(rows), "columns": len(rows[0]), "tableRows": cells}}


def object_paragraph():
    return {"paragraph": {"paragraphStyle": {"namedStyleType": "NORMAL_TEXT", "keepWithNext": True},
        "elements": [{"inlineObjectElement": {"inlineObjectId": "image-unchanged"}},
                     {"textRun": {"content": "\n", "textStyle": {"italic": True}}}]}}


def indexed(nodes, start=1):
    """Fixture authoring only: assign UTF-16 text indexes and Docs structural gaps."""
    # Each occurrence represents a distinct remote node, even when a fixture
    # reuses one template (deepcopy of the whole list preserves those aliases).
    nodes = [deepcopy(node) for node in nodes]
    cursor = start
    for node in nodes:
        node["startIndex"] = cursor
        if "paragraph" in node:
            for run in node["paragraph"]["elements"]:
                run["startIndex"] = cursor
                cursor += (len(run["textRun"]["content"].encode("utf-16-le")) // 2
                           if "textRun" in run else 1)
                run["endIndex"] = cursor
        elif "table" in node:
            cursor += 1
            for row in node["table"]["tableRows"]:
                row["startIndex"] = cursor
                cursor += 1
                for cell in row["tableCells"]:
                    cell["startIndex"] = cursor
                    cursor += 1
                    cell["content"], cursor = indexed(cell["content"], cursor)
                    cell["endIndex"] = cursor
                row["endIndex"] = cursor
            cursor += 1
        else:
            cursor += 1
        node["endIndex"] = cursor
    return nodes, cursor


def test_indexed_fixture_reused_paragraphs_have_independent_coordinates():
    blank = p("\n")
    nodes, end = indexed([blank, blank])
    assert nodes[0] is not nodes[1]
    assert [(node["startIndex"], node["endIndex"]) for node in nodes] == [(1, 2), (2, 3)]
    assert end == 3
    assert "startIndex" not in blank


def doc(nodes, revision="r1", other=False):
    content, _ = indexed(nodes)
    content.insert(0, {"endIndex": 1, "sectionBreak": {"sectionStyle": {}}})
    value = {"documentId": DOC, "revisionId": revision, "tabs": [{
        "tabProperties": {"tabId": TAB, "title": "Selected", "index": 0},
        "documentTab": {"body": {"content": content},
            "lists": {"list1": {"listProperties": {"nestingLevels": [{"glyphType": "DECIMAL"}]}}},
            "inlineObjects": {"image-unchanged": {"objectId": "image-unchanged"}}}}]}
    if other:
        value["tabs"].append({"tabProperties": {"tabId": "t2", "title": "Other", "index": 1},
                             "documentTab": {"body": {"content": indexed([p("Other\n")])[0]}}})
    return value


class Client:
    def __init__(self, before, *after, fail_batch=None, fail_export=False):
        self.documents = [before, *after]
        self.reads = self.metadata = 0
        self.batches = []
        self.events = []
        self.fail_batch = fail_batch
        self.fail_export = fail_export

    def drive_metadata(self, document_id):
        self.metadata += 1
        return {"id": document_id, "name": "Synthetic", "mimeType": "application/vnd.google-apps.document",
                "modifiedTime": "2026-01-01T00:00:00Z", "version": "1",
                "webViewLink": f"https://docs.google.com/document/d/{document_id}/edit"}

    def get_document(self, document_id):
        self.reads += 1
        self.events.append("read")
        assert self.documents, "unexpected extra read"
        return deepcopy(self.documents.pop(0))

    def batch_update(self, document_id, requests, revision, *, retry_safe):
        assert retry_safe is False
        self.events.append("write")
        self.batches.append((deepcopy(requests), revision))
        if len(self.batches) == self.fail_batch:
            raise TimeoutError(TRANSPORT_CANARY)
        return {"writeControl": {"requiredRevisionId": f"r{len(self.batches) + 1}"}}

    def export_file(self, document_id, mime):
        self.events.append("export")
        if self.fail_export:
            raise OSError("private export details")
        return b"synthetic recovery bytes"


@pytest.mark.parametrize("kwargs", [
    {"action": "wrong"}, {"action": []}, {"position": []}, {"position": "middle"},
    {"apply": 1}, {"format_profile": []}, {"format_profile": "english"},
    {"heading_text": "Title"}, {"position": "before"}, {"position": "after", "anchor_text": 1},
    {"anchor_text": "unused"}, {"action": "replace"},
    {"action": "replace", "heading_text": "Title", "position": "start"},
    {"action": "replace", "heading_text": "Title", "anchor_text": "bad"},
    {"action": "replace", "heading_text": "Title\n"},
    {"position": "before", "anchor_text": "hello\n"},
    {"tab_id": "bad\x00"}, {"tab_id": []}, {"expected_revision_id": ""},
    {"document": " synthetic_sections_123"},
])
def test_bad_arguments_before_any_io(tmp_path, kwargs):
    client = Client(doc([p("Old\n")]))
    with pytest.raises(DocsMCPError):
        edit(client, tmp_path, **kwargs)
    assert client.reads == client.metadata == 0
    assert client.events == []


@pytest.mark.parametrize("markdown", [None, [], "bad\x00", "bad\ud800", "⟦TABLE-0001⟧", "x" * 500001],
                         ids=["none", "list", "control", "surrogate", "reserved", "oversized"])
def test_invalid_markdown_before_io(tmp_path, markdown):
    client = Client(doc([p("Old\n")]))
    with pytest.raises(DocsMCPError):
        edit(client, tmp_path, markdown)
    assert client.reads == client.metadata == 0


@pytest.mark.parametrize("position,anchor,index", [
    ("start", None, 1), ("end", None, 14), ("before", "Two", 7), ("after", "Two", 11),
])
def test_preview_four_boundaries_no_writes_or_exports(tmp_path, position, anchor, index):
    client = Client(doc([p("One😀\n"), p("Two\n"), p("End\n")]))
    result = edit(client, tmp_path, position=position, anchor_text=anchor)
    # UTF-16: One😀\n occupies six units, so Two begins at 7.
    assert result["scope"] == {"start_index": index, "end_index": index}
    assert result["applied"] is False and result["revision_id"] == "r1"
    assert client.events == ["read"]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("anchor", ["ne", "Absent", "Repeat"])
def test_anchor_requires_unique_full_top_level_paragraph(tmp_path, anchor):
    client = Client(doc([p("One\n"), p("Repeat\n"), p("Repeat\n"), table([["Absent"]]), p("\n")]))
    with pytest.raises(DocsMCPError) as error:
        edit(client, tmp_path, position="before", anchor_text=anchor)
    assert error.value.code == "anchor_match_mismatch"
    assert not client.batches


def test_insert_rebases_unicode_styles_and_preserves_list_and_object(tmp_path):
    prefix = [p("Keep😀\n", level=1, runs=[("Keep😀\n", {"bold": True})]), object_paragraph()]
    suffix = [p("List\n", bullet={"listId": "list1", "nestingLevel": 0}), p("End\n")]
    candidate = p("Hi😀 bold link\n", runs=[("Hi😀 ", {"bold": False}),
        ("bold", {"bold": True}), (" ", {"bold": False}),
        ("link", {"bold": False, "link": {"url": URL}}), ("\n", {"bold": False})])
    client = Client(doc(prefix + suffix, other=True), doc(prefix + [candidate] + suffix, "r2", other=True))
    result = edit(client, tmp_path, f"Hi😀 **bold** [link]({URL})", position="before",
                  anchor_text="List", tab_id=TAB, format_profile="plain", apply=True)
    assert result["verified"] and result["after_revision_id"] == "r2"
    requests, revision = client.batches[0]
    assert revision == "r1"
    assert requests[0] == {"insertText": {"location": {"index": 10, "tabId": TAB}, "text": "Hi😀 bold link\n"}}
    bold = [r["updateTextStyle"] for r in requests if r.get("updateTextStyle", {}).get("textStyle") == {"bold": True}]
    assert bold[0]["range"] == {"startIndex": 15, "endIndex": 19, "tabId": TAB}
    assert not any("deleteContentRange" in request for request in requests)
    assert all(r.get("range", {}).get("startIndex", 10) >= 10 for request in requests for r in request.values())


@pytest.mark.parametrize("position,anchor,after", [
    ("start", None, [p("New\n"), p("Old\n")]),
    ("before", "Old", [p("New\n"), p("Old\n")]),
    ("after", "Old", [p("Old\n"), p("New\n"), p("\n")]),
    ("end", None, [p("Old\n"), p("New\n"), p("\n")]),
])
def test_insert_actual_readback_each_boundary(tmp_path, position, anchor, after):
    client = Client(doc([p("Old\n")]), doc(after, "r2"))
    result = edit(client, tmp_path, position=position, anchor_text=anchor, format_profile="plain", apply=True)
    assert result["verified"]
    text = client.batches[0][0][0]["insertText"]["text"]
    assert text == ("\nNew\n" if position in {"end", "after"} else "New\n")


@pytest.mark.parametrize("markdown,inserted", [("Fresh", [p("Fresh\n")]), ("", [])])
def test_replace_preserves_heading_and_next_peer_removes_children(tmp_path, markdown, inserted):
    heading = p("Target\n", level=2)
    prefix = [p("Top\n", level=1), heading]
    suffix = [p("Next\n", level=2), object_paragraph(), p("Tail\n")]
    before = prefix + [p("Old\n"), p("Child\n", level=3), p("Details\n")] + suffix
    client = Client(doc(before), doc(prefix + inserted + suffix, "r2"))
    result = edit(client, tmp_path, markdown, action="replace", heading_text="Target", format_profile="plain", apply=True)
    assert result["verified"]
    assert client.batches[0][0][0] == {"deleteContentRange": {"range": {"startIndex": 12, "endIndex": 30, "tabId": TAB}}}


@pytest.mark.parametrize("before,after,markdown", [
    ([p("Title\n", level=1), p("Old\n")], [p("Title\n", level=1), p("New\n"), p("\n")], "New"),
    ([p("Title\n", level=1), p("Old\n")], [p("Title\n", level=1), p("\n")], ""),
    ([p("Title\n", level=1)], [p("Title\n", level=1), p("New\n"), p("\n", level=1)], "New"),
])
def test_terminal_section_retains_mandatory_newline(tmp_path, before, after, markdown):
    client = Client(doc(before), doc(after, "r2"))
    result = edit(client, tmp_path, markdown, action="replace", heading_text="Title", format_profile="plain", apply=True)
    assert result["verified"]
    original_end = doc(before)["tabs"][0]["documentTab"]["body"]["content"][-1]["endIndex"]
    for request in client.batches[0][0]:
        if "deleteContentRange" in request:
            assert request["deleteContentRange"]["range"]["endIndex"] < original_end


def test_empty_insert_and_already_empty_section_are_noops(tmp_path):
    before = doc([p("Title\n", level=1)])
    for kwargs in ({}, {"action": "replace", "heading_text": "Title"}):
        client = Client(before)
        result = edit(client, tmp_path, "", apply=True, **kwargs)
        assert result["verified"] and result["after_revision_id"] == "r1"
        assert not client.batches


@pytest.mark.parametrize("kind", ["missing", "duplicate", "not_native", "unsupported"])
def test_replacement_rejects_bad_heading_or_destructive_objects(tmp_path, kind):
    nodes = [p("Title\n", level=None if kind == "not_native" else 1), object_paragraph(), p("End\n", level=1)]
    if kind == "duplicate":
        nodes.insert(1, p("Title\n", level=2))
    client = Client(doc(nodes))
    with pytest.raises(DocsMCPError):
        edit(client, tmp_path, action="replace", heading_text="Missing" if kind == "missing" else "Title", apply=True)
    assert not client.batches


@pytest.mark.parametrize("damage", ["text", "bold", "extra_blank", "heading", "outside_style", "outside_object", "other_tab", "revision"])
def test_wrong_readback_never_claims_success(tmp_path, damage):
    prefix = [object_paragraph()]
    suffix = [p("Next\n", level=1), p("End\n")]
    before = doc(prefix + suffix, other=True)
    after = doc(prefix + [p("New\n")] + suffix, "r2", other=True)
    nodes = after["tabs"][0]["documentTab"]["body"]["content"]
    if damage == "text":
        nodes[2]["paragraph"]["elements"][0]["textRun"]["content"] = "Bad\n"
    elif damage == "bold":
        nodes[2]["paragraph"]["elements"][0]["textRun"]["textStyle"]["bold"] = True
    elif damage == "extra_blank":
        after = doc(prefix + [p("New\n"), p("\n")] + suffix, "r2", other=True)
    elif damage == "heading":
        nodes[2]["paragraph"]["paragraphStyle"]["namedStyleType"] = "HEADING_2"
    elif damage == "outside_style":
        nodes[3]["paragraph"]["paragraphStyle"]["keepWithNext"] = False
    elif damage == "outside_object":
        nodes[1]["paragraph"]["elements"][0]["inlineObjectElement"]["inlineObjectId"] = "changed"
    elif damage == "other_tab":
        after["tabs"][1]["tabProperties"]["title"] = "Changed"
    else:
        after["revisionId"] = "wrong"
    client = Client(before, after)
    with pytest.raises(DocsMCPError) as error:
        edit(client, tmp_path, position="before", anchor_text="Next", format_profile="plain", tab_id=TAB, apply=True)
    assert error.value.code == "verification_failed"
    assert len(client.batches) == 1


def test_persian_checks_only_new_scope(tmp_path):
    old = [p("English\n", level=1)]
    new = p("عنوان\n", level=2, runs=[("عنوان", {"bold": True}), ("\n", {"bold": False})], persian=True)
    client = Client(doc(old), doc([new] + old, "r2"))
    result = edit(client, tmp_path, "## عنوان", position="start", apply=True)
    assert result["formatting_verified"] and client.events == ["read", "write", "read"]


def table_scenario(*, final_damage=None, persian=False, replace=False):
    old_table = table([["Existing"]])
    prefix = [p("Target\n", level=1)]
    suffix = [p("Next\n", level=1), old_table, object_paragraph(), p("End\n")]
    before = doc(prefix + ([p("Old\n")] if replace else []) + suffix)
    line = p("Intro😀\n", persian=persian)
    marker = p("⟦TABLE-0001⟧\n", persian=persian)
    blank = p("\n", persian=persian)
    initial = doc(prefix + [line, marker] + suffix, "r2")
    structure = doc(prefix + [line, blank, table([["", ""], ["", ""]]), blank] + suffix, "r3")
    text = doc(prefix + [line, blank, table([["😀B", "link"], ["last", ""]]), blank] + suffix, "r4")
    rich = p("😀B\n", runs=[("😀B", {"bold": True}), ("\n", {"bold": False})], persian=persian)
    linked = p("link\n", runs=[("link", {"link": {"url": URL}}), ("\n", {})], persian=persian)
    final = doc(prefix + [line, blank, table([[rich, linked], ["last", ""]], persian=persian), blank] + suffix, "r5")
    if final_damage == "cell":
        final = doc(prefix + [line, blank, table([["wrong", "link"], ["last", ""]]), blank] + suffix, "r5")
    if final_damage == "suffix":
        final["tabs"][0]["documentTab"]["body"]["content"][-2]["paragraph"]["elements"][0]["inlineObjectElement"]["inlineObjectId"] = "wrong"
    source = f"Intro😀\n| **😀B** | [link]({URL}) |\n| --- | --- |\n| last |"
    return source, [before, initial, structure, text, final]


@pytest.mark.parametrize("replace,persian", [(False, False), (True, True)])
def test_native_table_publication_actual_indices_revisions_and_recovery_cleanup(tmp_path, replace, persian):
    source, frames = table_scenario(replace=replace, persian=persian)
    client = Client(*frames)
    args = {"action": "replace", "heading_text": "Target"} if replace else {"position": "before", "anchor_text": "Next"}
    result = edit(client, tmp_path, source, format_profile="persian" if persian else "plain", apply=True, **args)
    assert result["verified"] and result["after_revision_id"] == "r5"
    assert [revision for _, revision in client.batches] == ["r1", "r2", "r3", "r4"]
    assert client.events[:4] == ["read", "export", "export", "write"]
    assert list(tmp_path.iterdir()) == []
    structural = client.batches[1][0]
    assert structural == [
        {"deleteContentRange": {"range": {"startIndex": 16, "endIndex": 28, "tabId": TAB}}},
        {"insertTable": {"rows": 2, "columns": 2, "location": {"index": 16, "tabId": TAB}}},
    ]
    raw_table = next(n for n in frames[2]["tabs"][0]["documentTab"]["body"]["content"] if "table" in n)
    cells = [cell for row in raw_table["table"]["tableRows"] for cell in row["tableCells"]]
    indices = [cell["content"][0]["startIndex"] for cell in cells]
    requests = client.batches[2][0]
    assert [r["insertText"]["location"]["index"] for r in requests] == indices[2::-1]
    filled = next(n for n in frames[3]["tabs"][0]["documentTab"]["body"]["content"] if "table" in n)
    base = filled["table"]["tableRows"][0]["tableCells"][0]["content"][0]["startIndex"]
    bold = next(r["updateTextStyle"] for r in client.batches[3][0] if r.get("updateTextStyle", {}).get("textStyle") == {"bold": True})
    assert bold["range"] == {"startIndex": base, "endIndex": base + 3, "tabId": TAB}


@pytest.mark.parametrize("fail_batch", [1, 2, 3, 4])
def test_uncertain_table_phase_keeps_recovery_and_never_retries(tmp_path, fail_batch):
    source, frames = table_scenario()
    client = Client(*frames[:fail_batch], fail_batch=fail_batch)
    with pytest.raises(DocsMCPError) as error:
        edit(client, tmp_path, source, position="before", anchor_text="Next", format_profile="plain", apply=True)
    assert error.value.code == "partial_write_requires_recovery"
    detail = error.value.as_result()["error"]
    assert isinstance(detail, dict)
    assert detail["revision_id"] == f"r{fail_batch}"
    assert TRANSPORT_CANARY not in str(detail)
    recovery = Path(detail["recovery_path"])
    assert (recovery / "document.txt").read_bytes() == b"synthetic recovery bytes"
    assert (recovery / "document.docx").exists()
    assert len(client.batches) == fail_batch


@pytest.mark.parametrize("damage", ["cell", "suffix"])
def test_table_verification_failure_retains_backup(tmp_path, damage):
    source, frames = table_scenario(final_damage=damage)
    client = Client(*frames)
    with pytest.raises(DocsMCPError) as error:
        edit(client, tmp_path, source, position="before", anchor_text="Next", format_profile="plain", apply=True)
    assert error.value.code == "partial_write_requires_recovery"
    detail = error.value.as_result()["error"]
    assert isinstance(detail, dict)
    assert detail["phase"] == "final_verification"
    assert detail["revision_id"] == "r5"
    assert len(client.batches) == 4
    assert len(list(tmp_path.iterdir())) == 1


def test_bad_structure_readback_stops_before_cell_mutation(tmp_path):
    source, frames = table_scenario()
    structure = frames[2]
    native = next(n["table"] for n in structure["tabs"][0]["documentTab"]["body"]["content"] if "table" in n)
    native["tableRows"][0]["tableCells"][0]["tableCellStyle"]["columnSpan"] = 2
    client = Client(*frames[:3])
    with pytest.raises(DocsMCPError) as error:
        edit(client, tmp_path, source, position="before", anchor_text="Next", format_profile="plain", apply=True)
    assert error.value.code == "partial_write_requires_recovery"
    assert len(client.batches) == 2


def test_table_preview_and_failed_export_never_mutate(tmp_path):
    source, frames = table_scenario()
    client = Client(frames[0])
    result = edit(client, tmp_path, source, position="before", anchor_text="Next")
    assert result["table_count"] == 1 and client.events == ["read"]
    client = Client(frames[0], fail_export=True)
    with pytest.raises(DocsMCPError):
        edit(client, tmp_path, source, position="before", anchor_text="Next", apply=True)
    assert not client.batches


@pytest.mark.parametrize("case", ["stale", "multitab", "bad_index", "bad_text_index", "bad_body"])
def test_preflight_rejects_stale_ambiguous_and_malformed_remote(tmp_path, case):
    before = doc([p("Old\n")], other=case == "multitab")
    if case == "stale":
        before["revisionId"] = "r0"
    body = before["tabs"][0]["documentTab"]["body"]
    if case == "bad_index":
        body["content"][-1]["startIndex"] = True
    if case == "bad_text_index":
        body["content"][-1]["paragraph"]["elements"][0]["endIndex"] = 100
    if case == "bad_body":
        body["content"] = None
    client = Client(before)
    with pytest.raises(DocsMCPError):
        edit(client, tmp_path, apply=True)
    assert not client.batches
