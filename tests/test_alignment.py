"""Independent alignment contracts and opt-in Google export characterization."""
import os
from io import BytesIO
from zipfile import ZipFile
from defusedxml import ElementTree

import pytest

from google_docs_mcp.client import DocsMCPError, _verify_persian_api, utf16_length, verify_persian_docx
from google_docs_mcp.markdown import paragraph_style_request
from test_native_api import google_session, _temporary_document
import test_live_google as live


def test_persian_profile_requires_line_start_for_physical_right():
    request = paragraph_style_request(8, "synthetic-tab")
    assert request is not None
    assert request["updateParagraphStyle"]["paragraphStyle"]["direction"] == "RIGHT_TO_LEFT"
    assert request["updateParagraphStyle"]["paragraphStyle"]["alignment"] == "START"


def test_rtl_line_end_is_not_physical_right():
    body = {"content": [{"startIndex": 1, "endIndex": 3, "paragraph": {
        "paragraphStyle": {"direction": "RIGHT_TO_LEFT", "alignment": "END",
                           "indentStart": {"unit": "PT"}, "indentEnd": {"unit": "PT"}},
        "elements": [{"startIndex": 1, "endIndex": 3, "textRun": {
            "content": "x\n", "textStyle": {"weightedFontFamily": {"fontFamily": "Vazirmatn"}}}}],
    }}]}
    with pytest.raises(DocsMCPError):
        _verify_persian_api(body)


@pytest.mark.parametrize("bidi,alignment,count", [
    (True, "left", 1), (True, "right", 0), (True, "start", 1), (True, "end", 0),
    (False, "left", 0), (False, "right", 1), (False, "start", 0), (False, "end", 1),
])
def test_docx_justification_is_resolved_with_effective_bidi(bidi, alignment, count):
    from test_client import _task9_docx_bytes, _task9_docx_ppr, _task9_docx_rpr, _TASK9_WORD_NAMESPACE
    xml = (f'<w:document xmlns:w="{_TASK9_WORD_NAMESPACE}"><w:body><w:p>'
           + _task9_docx_ppr(bidi=bidi, alignment=alignment)
           + '<w:r>' + _task9_docx_rpr() + '<w:t>synthetic</w:t></w:r></w:p></w:body></w:document>')
    result = verify_persian_docx(_task9_docx_bytes(document_xml=xml))
    assert result["right_aligned_paragraphs"] == count


@pytest.mark.skipif(os.getenv("RUN_GOOGLE_DOCS_MCP_LIVE") != "1", reason="Synthetic Google writes require opt-in.")
def test_live_rtl_alignment_export_characterization(google_session, tmp_path, record_property):
    session, client = google_session
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with _temporary_document(session, client, client.create_document) as document_id:
        document = client.get_document(document_id)
        tab_id = document["tabs"][0]["tabProperties"]["tabId"]
        text = "عنوان کوتاه\nمتن فارسی و English\n"
        area = {"startIndex": 1, "endIndex": 1 + utf16_length(text), "tabId": tab_id}
        client.batch_update(document_id, [
            {"insertText": {"location": {"index": 1, "tabId": tab_id}, "text": text}},
            {"updateTextStyle": {"range": area, "textStyle": {"weightedFontFamily": {"fontFamily": "Vazirmatn"}},
                                 "fields": "weightedFontFamily"}},
        ], document["revisionId"], retry_safe=False)
        for alignment in ("START", "END"):
            before = client.get_document(document_id)
            client.batch_update(document_id, [{"updateParagraphStyle": {
                "range": area, "paragraphStyle": {"direction": "RIGHT_TO_LEFT", "alignment": alignment,
                    "indentStart": {"magnitude": 0, "unit": "PT"}, "indentEnd": {"magnitude": 0, "unit": "PT"}},
                "fields": "direction,alignment,indentStart,indentEnd"}}], before["revisionId"], retry_safe=False)
            after = client.get_document(document_id)
            paragraphs = list(live._paragraphs(after["tabs"][0]["documentTab"]["body"]["content"]))
            live._require(len(paragraphs) >= 2 and all(
                p["paragraphStyle"]["alignment"] == alignment and
                p["paragraphStyle"]["direction"] == "RIGHT_TO_LEFT" for p in paragraphs[:2]),
                          "Persian API verification: acceptance check failed")
            for extension, mime in (("pdf", "application/pdf"),
                                    ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")):
                payload = client.export_file(document_id, mime)
                path = tmp_path / ("rtl-" + alignment.lower() + "." + extension)
                with path.open("xb") as stream:
                    path.chmod(0o600)
                    stream.write(payload)
                if extension == "docx":
                    verification = verify_persian_docx(payload)
                    live._require(verification["paragraphs"] == verification["bidi_paragraphs"] == 2
                                  and verification["right_aligned_paragraphs"] == (2 if alignment == "START" else 0),
                                  "DOCX verification: acceptance check failed")
                    with ZipFile(BytesIO(payload)) as archive:
                        root = ElementTree.fromstring(archive.read("word/document.xml"))
                    values = sorted({n.get("{" + ns["w"] + "}val", "") for n in root.findall(".//w:jc", ns)})
                    live._require(bool(values) and all(v in {"left", "right", "start", "end"} for v in values),
                                  "DOCX verification: acceptance check failed")
                    record_property("rtl_" + alignment.lower() + "_export_jc", ",".join(values))
    record_property("private_run_document_cleanup", "verified")
