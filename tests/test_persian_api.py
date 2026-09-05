"""Automatic service verification must reject API/DOCX disagreement."""
from copy import deepcopy
from pathlib import Path

import pytest

from google_docs_mcp.client import DocsMCPError
from test_client import (
    Task11Client,
    VALID_ID,
    _TASK11_DOCX_MIME,
    _task9_docx_bytes,
    _task11_document,
    _task11_service,
)


def _blank_docx(*, omit_styles=False):
    return _task9_docx_bytes(
        document_xml=(
            '<w:document xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main"><w:body><w:p/></w:body></w:document>'
        ),
        omit_styles=omit_styles,
    )


@pytest.mark.parametrize("profile", ["persian", "plain"])
@pytest.mark.parametrize("before_text", ["\n", "Before\n"], ids=["noop", "delete"])
def test_empty_candidate_preserves_no_style_write_boundary(tmp_path, profile, before_text):
    revision_after = "rev-1" if before_text == "\n" else "rev-2"
    client = Task11Client(
        documents=[
            _task11_document("rev-1", before_text),
            _task11_document(revision_after, "\n"),
        ],
        batch_revisions=[] if revision_after == "rev-1" else [revision_after],
        exports={_TASK11_DOCX_MIME: [_blank_docx()]},
    )
    result = _task11_service(client, tmp_path / "recovery").replace_markdown(
        VALID_ID, "", "rev-1", format_profile=profile
    )
    assert result["verified"] is True
    semantic = result["semantic"]
    assert isinstance(semantic, dict)
    assert semantic["block_count"] == 0
    assert result["before_revision_id"] == "rev-1"
    assert result["after_revision_id"] == revision_after
    expected_events = ["metadata", "get_document"]
    if before_text == "\n":
        assert client.batch_calls == []
    else:
        assert client.batch_calls == [(VALID_ID, [{"deleteContentRange": {"range": {
            "startIndex": 1, "endIndex": 7, "tabId": "t.selected",
        }}}], "rev-1")]
        expected_events.append("batch_update")
    expected_events.append("get_document")
    if profile == "persian":
        formatting = result["formatting"]
        assert isinstance(formatting, dict)
        assert formatting["valid"] is True
        assert formatting["paragraphs"] == 0
        assert client.export_calls == [(VALID_ID, _TASK11_DOCX_MIME)]
        expected_events.append(f"export:{_TASK11_DOCX_MIME}")
    else:
        assert result["formatting"] is None
        assert client.export_calls == []
    assert client.events == expected_events


@pytest.mark.parametrize("before_text", ["\n", "Before\n"], ids=["noop", "delete"])
def test_empty_candidate_still_rejects_invalid_blank_docx(tmp_path, before_text):
    revision_after = "rev-1" if before_text == "\n" else "rev-2"
    client = Task11Client(
        documents=[
            _task11_document("rev-1", before_text),
            _task11_document(revision_after, "\n"),
        ],
        batch_revisions=[] if revision_after == "rev-1" else [revision_after],
        exports={_TASK11_DOCX_MIME: [_blank_docx(omit_styles=True)]},
    )
    with pytest.raises(DocsMCPError) as caught:
        _task11_service(client, tmp_path / "recovery").replace_markdown(
            VALID_ID, "", "rev-1", format_profile="persian"
        )
    assert caught.value.code == "verification_failed"
    assert client.export_calls == [(VALID_ID, _TASK11_DOCX_MIME)]


@pytest.mark.parametrize("profile", ["persian", "plain"])
@pytest.mark.parametrize("before_text", ["\n", "Before\n"], ids=["noop", "delete"])
def test_empty_candidate_still_rejects_semantic_mismatch(tmp_path, profile, before_text):
    revision_after = "rev-1" if before_text == "\n" else "rev-2"
    client = Task11Client(
        documents=[
            _task11_document("rev-1", before_text),
            _task11_document(revision_after, "Unexpected\n"),
        ],
        batch_revisions=[] if revision_after == "rev-1" else [revision_after],
        exports={_TASK11_DOCX_MIME: [_blank_docx()]},
    )
    with pytest.raises(DocsMCPError, match="content verification") as caught:
        _task11_service(client, tmp_path / "recovery").replace_markdown(
            VALID_ID, "", "rev-1", format_profile=profile
        )
    assert caught.value.code == "verification_failed"
    assert client.export_calls == []


def _formatted_document():
    document = _task11_document("rev-2", "سلام\n")
    paragraph = document["tabs"][0]["documentTab"]["body"]["content"][0]["paragraph"]
    paragraph["paragraphStyle"] = {
        "namedStyleType": "NORMAL_TEXT",
        "direction": "RIGHT_TO_LEFT",
        "alignment": "END",
        "indentStart": {"unit": "PT"},
        "indentEnd": {"magnitude": 0, "unit": "PT"},
    }
    for element in paragraph["elements"]:
        element["textRun"]["textStyle"] = {
            "weightedFontFamily": {"fontFamily": "Vazirmatn"},
        }
    return document, paragraph


@pytest.mark.parametrize("field", ["direction", "alignment", "indentStart", "indentEnd", "font"])
def test_persian_service_rejects_bad_api_style_even_with_valid_docx(tmp_path: Path, field: str):
    after, paragraph = _formatted_document()
    if field == "font":
        paragraph["elements"][0]["textRun"]["textStyle"]["weightedFontFamily"] = {"fontFamily": "Arial"}
    else:
        paragraph["paragraphStyle"].pop(field)
    client = Task11Client(
        documents=[_task11_document("rev-1", "Before\n"), after],
        batch_revisions=["rev-2"],
        exports={_TASK11_DOCX_MIME: [_task9_docx_bytes()]},
    )
    with pytest.raises(DocsMCPError) as caught:
        _task11_service(client, tmp_path / "recovery").replace_markdown(
            VALID_ID, "سلام", "rev-1", format_profile="persian"
        )
    assert caught.value.code == "verification_failed"
    assert "سلام" not in str(caught.value)


def test_persian_service_accepts_independent_api_fields_and_proto_zero(tmp_path: Path):
    after, _ = _formatted_document()
    before = deepcopy(after)
    client = Task11Client(
        documents=[_task11_document("rev-1", "Before\n"), after],
        batch_revisions=["rev-2"],
        exports={_TASK11_DOCX_MIME: [_task9_docx_bytes()]},
    )
    result = _task11_service(client, tmp_path / "recovery").replace_markdown(
        VALID_ID, "سلام", "rev-1", format_profile="persian"
    )
    assert result["verified"] is True
    assert after == before
    assert client.events.count("get_document") == 2


@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("field", [None, "direction", "alignment", "indentStart", "indentEnd", "font"])
def test_api_verifier_checks_table_cells_independently(empty, field):
    import google_docs_mcp.client as module

    after, paragraph = _formatted_document()
    body = after["tabs"][0]["documentTab"]["body"]
    cell_paragraph = deepcopy(paragraph)
    if empty:
        cell_paragraph["elements"][0]["textRun"]["content"] = "\n"
    if field == "font":
        cell_paragraph["elements"][0]["textRun"]["textStyle"].pop("weightedFontFamily")
    elif field is not None:
        cell_paragraph["paragraphStyle"].pop(field)
    body["content"].append({"table": {"tableRows": [{"tableCells": [
        {"content": [{"paragraph": cell_paragraph}]}
    ]}]}})
    if field is None:
        module._verify_persian_api(body)
    else:
        with pytest.raises(DocsMCPError, match="API formatting"):
            module._verify_persian_api(body)


@pytest.mark.parametrize("bad", [False, True, "0", 1, None])
def test_api_verifier_rejects_invalid_indent_magnitude(bad):
    import google_docs_mcp.client as module

    after, paragraph = _formatted_document()
    paragraph["paragraphStyle"]["indentStart"]["magnitude"] = bad
    with pytest.raises(DocsMCPError, match="API formatting"):
        module._verify_persian_api(after["tabs"][0]["documentTab"]["body"])


def test_api_verifier_does_not_treat_null_as_end_of_sequence():
    import google_docs_mcp.client as module

    with pytest.raises(DocsMCPError, match="API formatting"):
        module._verify_persian_api({"content": [None]})


def test_api_verifier_is_node_bounded_and_sanitized(monkeypatch):
    import google_docs_mcp.client as module

    after, _ = _formatted_document()
    monkeypatch.setattr(module, "_MAX_RENDER_NODES", 1)
    with pytest.raises(DocsMCPError) as caught:
        module._verify_persian_api(after["tabs"][0]["documentTab"]["body"])
    assert caught.value.code == "verification_failed"
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("bold", [True, False])
def test_api_verifier_checks_heading_bold(bold):
    import google_docs_mcp.client as module

    after, paragraph = _formatted_document()
    paragraph["paragraphStyle"]["namedStyleType"] = "HEADING_2"
    paragraph["elements"][0]["textRun"]["textStyle"]["bold"] = bold
    body = after["tabs"][0]["documentTab"]["body"]
    if bold:
        module._verify_persian_api(body)
    else:
        with pytest.raises(DocsMCPError, match="API formatting"):
            module._verify_persian_api(body)
