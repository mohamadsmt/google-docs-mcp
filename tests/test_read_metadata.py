"""Read projections preserve content while exposing non-secret source metadata."""
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import pytest

from google_docs_mcp.client import DocsMCPError, GoogleDocsService
from test_insertion import Client, DOC, document


def run(text, start, link=None, **style):
    if link is not None:
        style["link"] = link
    return {"startIndex": start, "endIndex": start + len(text.encode("utf-16-le")) // 2,
            "textRun": {"content": text, "textStyle": style}}


def read(raw, **kwargs) -> Any:
    return GoogleDocsService(cast(Any, Client(raw)), Path("unused")).read(DOC, **kwargs)


def test_links_keep_destinations_and_utf16_spans_without_changing_content():
    raw = document("منبع 🧪 پژوهش\n")
    elements = raw["tabs"][0]["documentTab"]["body"]["content"][0]["paragraph"]["elements"]
    elements[:] = [run("منبع 🧪 ", 1), run("پژ", 9, {"url": "https://example.com/source?a=1&b=2"}),
                   run("وهش", 11, {"url": "https://example.com/source?a=1&b=2"}, bold=True),
                   run("\n", 14)]
    result = read(raw)
    assert result["content"] == "منبع 🧪 پژوهش\n"
    assert result["links"] == [{"text": "پژوهش", "target": {"url": "https://example.com/source?a=1&b=2"},
                                "tab_id": "tab-1", "start_index": 9, "end_index": 14}]
    assert result["images"] == []


def test_repeated_links_are_not_deduped_and_metadata_is_whole_selected_body():
    raw = document("first\nsecond\n")
    paragraphs = raw["tabs"][0]["documentTab"]["body"]["content"]
    for paragraph in paragraphs:
        paragraph["paragraph"]["elements"][0]["textRun"]["textStyle"]["link"] = {"url": "https://example.com/same"}
    result = read(raw, max_chars=1)
    assert result["content"] == "f"
    assert [link["text"] for link in result["links"]] == ["first\n", "second\n"]
    assert result["metadata_scope"] == "selected_tab_body"


@pytest.mark.parametrize("target", [
    {"heading": {"id": "heading-1", "tabId": "tab-1"}},
    {"bookmark": {"id": "bookmark-1", "tabId": "tab-1"}},
    {"tabId": "tab-2"}, {"headingId": "heading-legacy"},
    {"bookmarkId": "bookmark-legacy"}, {"url": "mailto:example@example.com"},
])
def test_internal_and_external_link_values_are_preserved_not_rewritten(target):
    raw = document("target\n")
    raw["tabs"][0]["documentTab"]["body"]["content"][0]["paragraph"]["elements"] = [run("target\n", 1, target)]
    assert read(raw)["links"][0]["target"] == target


def test_table_cell_link_and_missing_source_index_are_explicit():
    raw = document("\n")
    table = {"rows": 1, "columns": 1, "tableRows": [{"tableCells": [{"content": [{
        "paragraph": {"elements": [{"textRun": {"content": "cell\n", "textStyle": {
            "link": {"url": "https://example.com/table"}}}}]}}]}]}]}
    raw["tabs"][0]["documentTab"]["body"]["content"].append({"table": table})
    link = read(raw)["links"][0]
    assert link == {"text": "cell\n", "target": {"url": "https://example.com/table"},
                    "tab_id": "tab-1", "start_index": None, "end_index": None}


def test_images_return_object_metadata_but_never_temporary_content_uri():
    raw = document("before\nafter\n")
    tab = raw["tabs"][0]["documentTab"]
    first = tab["body"]["content"][0]["paragraph"]
    first["elements"].insert(0, {"startIndex": 1, "endIndex": 2,
                               "inlineObjectElement": {"inlineObjectId": "image-1"}})
    first["positionedObjectIds"] = ["positioned-1"]
    embedded = {"title": "نمودار", "description": "توضیح", "size": {
        "width": {"magnitude": 100, "unit": "PT"}, "height": {"magnitude": 50, "unit": "PT"}},
        "imageProperties": {"sourceUri": "https://example.com/chart.png",
                            "contentUri": "https://temporary.example/SECRET-CONTENT-TOKEN"}}
    tab["inlineObjects"] = {"image-1": {"inlineObjectProperties": {"embeddedObject": embedded}}}
    tab["positionedObjects"] = {"positioned-1": {"positionedObjectProperties": {"embeddedObject": embedded}}}
    result = read(raw)
    assert len(result["images"]) == 2
    assert result["images"][0] == {"object_id": "image-1", "kind": "inline", "tab_id": "tab-1",
        "start_index": 1, "end_index": 2, "title": "نمودار", "description": "توضیح",
        "size": embedded["size"], "source_uri": "https://example.com/chart.png"}
    assert result["images"][1]["kind"] == "positioned"
    assert result["images"][1]["object_id"] == "positioned-1"
    assert "SECRET-CONTENT-TOKEN" not in repr(result)
    assert "contentUri" not in repr(result)


def test_drawing_marker_without_image_properties_is_not_claimed_as_image():
    raw = document("\n")
    tab = raw["tabs"][0]["documentTab"]
    tab["body"]["content"][0]["paragraph"]["elements"].insert(0, {"inlineObjectElement": {"inlineObjectId": "drawing"}})
    tab["inlineObjects"] = {"drawing": {"inlineObjectProperties": {"embeddedObject": {"title": "drawing"}}}}
    assert read(raw)["images"] == []


def test_selected_tab_only_no_auxiliary_segments_and_reader_revision_optional():
    raw = document("selected\n")
    other = deepcopy(raw["tabs"][0])
    other["tabProperties"]["tabId"] = "other"
    other["documentTab"]["body"]["content"][0]["paragraph"]["elements"] = [run("hidden\n", 1, {"url": "https://example.com/hidden"})]
    raw["tabs"].append(other)
    raw.pop("revisionId")
    raw["tabs"][0]["documentTab"]["footnotes"] = {"secret": {"content": other["documentTab"]["body"]["content"]}}
    metadata_only = read(raw)
    assert "links" not in metadata_only and "images" not in metadata_only
    result = read(raw, tab_id="tab-1")
    assert result["revision_id"] is None and result["links"] == []
    assert "hidden" not in result["content"]


@pytest.mark.parametrize("bad_link", [[], {"url": []}, {"unknown": "secret"},
                                        {"heading": {"id": "x", "unexpected": "secret"}}])
def test_malformed_link_metadata_fails_closed_without_echo(bad_link):
    raw = document("target\n")
    raw["tabs"][0]["documentTab"]["body"]["content"][0]["paragraph"]["elements"] = [run("target", 1, bad_link)]
    with pytest.raises(DocsMCPError) as caught:
        read(raw)
    assert caught.value.code == "google_unavailable"
    assert "secret" not in str(caught.value)


def test_metadata_budget_is_explicit_and_not_silent_truncation():
    raw = document("\n")
    raw["tabs"][0]["documentTab"]["body"]["content"][0]["paragraph"]["elements"] = [
        run("x", i + 1, {"url": f"https://example.com/{i}"}) for i in range(10001)]
    with pytest.raises(DocsMCPError) as caught:
        read(raw)
    assert caught.value.code == "read_metadata_limit"


@pytest.mark.parametrize("limit", ["characters", "nodes"])
def test_independent_metadata_character_and_node_budgets(limit):
    from google_docs_mcp.read_metadata import read_metadata
    raw = document("\n")
    body = raw["tabs"][0]["documentTab"]["body"]
    if limit == "characters":
        body["content"][0]["paragraph"]["elements"] = [run("x" * 2_000_001, 1, {"url": "https://example.com"})]
    else:
        body["content"] = [{"paragraph": {"elements": []}} for _ in range(100_001)]
    with pytest.raises(DocsMCPError) as caught:
        read_metadata(raw, "tab-1")
    assert caught.value.code == "read_metadata_limit"
