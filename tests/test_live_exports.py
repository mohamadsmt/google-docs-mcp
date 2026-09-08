"""Opt-in native tab-coverage characterization on private run-owned documents."""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import zipfile

from defusedxml import ElementTree
import pytest

from google_docs_mcp.client import DocsMCPError, select_tab
from test_native_api import _temporary_document, google_session
import test_live_google as live


def _stable_export(client, root, *, document, format):
    from google_docs_mcp.exports import export_document

    # Newly written Docs content can precede Drive version/modifiedTime updates.
    # A real characterization caught this drift while Docs revision stayed fixed.
    # Only the synthetic test retries fresh reads; production must reject drift.
    for attempt in range(3):
        try:
            return export_document(client, root, document=document, format=format)
        except DocsMCPError as error:
            if error.code != "source_changed" or attempt == 2:
                raise
            print("NATIVE_EXPORT_FIXTURE Drive_metadata_settling_retry")

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_GOOGLE_DOCS_MCP_LIVE") != "1",
    reason="Live Google writes require explicit authorization.",
)
MIMES = {"pdf": "application/pdf", "docx":
         "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
MARKERS = {"first_root": "HERMESFIRSTROOTMARKER", "second_root": "HERMESSECONDROOTMARKER",
           "child": "HERMESCHILDTABMARKER"}


def _export_text(payload, format):
    if format == "docx":
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            root = ElementTree.fromstring(archive.read("word/document.xml"))
        return "".join(root.itertext())
    executable = shutil.which("gs")
    if executable is None:
        pytest.skip("Ghostscript required for independent native PDF text characterization")
    result = subprocess.run(
        [executable, "-dSAFER", "-dBATCH", "-dNOPAUSE", "-q", "-sDEVICE=txtwrite",
         "-sOutputFile=-", "-"], input=payload, capture_output=True, timeout=60,
        check=False,
    )
    live._require(result.returncode == 0, "semantic verification: acceptance check failed")
    # Ghostscript txtwrite can emit CESU-8 surrogate pairs for valid astral
    # glyphs. Recombine complete pairs; malformed UTF-8/lone surrogates still fail.
    return result.stdout.decode("utf-8", "surrogatepass").encode("utf-16", "surrogatepass").decode("utf-16")


def test_live_native_export_tab_coverage(google_session, record_property, tmp_path):
    # Check the local oracle before any creation side effect.
    if shutil.which("gs") is None:
        pytest.skip("Ghostscript required for independent native PDF text characterization")
    session, client = google_session
    summary = {}
    with _temporary_document(session, client, client.create_document) as document_id:
        document = client.get_document(document_id)
        first = select_tab(document, None).tab_id
        reply = client.batch_update(document_id, [
            {"addDocumentTab": {"tabProperties": {"title": "Second root", "index": 1}}},
            {"addDocumentTab": {"tabProperties": {
                "title": "Child", "parentTabId": first, "index": 0}}},
        ], document["revisionId"], retry_safe=False)
        second, child = [item["addDocumentTab"]["tabProperties"]["tabId"] for item in reply["replies"]]
        ids = {"first_root": first, "second_root": second, "child": child}
        document = client.get_document(document_id)
        client.batch_update(document_id, [
            {"insertText": {"location": {"index": 1, "tabId": ids[key]}, "text": marker}}
            for key, marker in MARKERS.items()
        ], document["revisionId"], retry_safe=False)
        verified = client.get_document(document_id)
        for key, tab_id in ids.items():
            selected = select_tab(verified, tab_id)
            text = "".join(run["content"] for paragraph in live._paragraphs(selected.body["content"])
                           for run in live._text_runs(paragraph))
            live._require(text.strip() == MARKERS[key], "semantic verification: text readback mismatch")
        for format, mime in MIMES.items():
            payload = client.export_file(document_id, mime)
            text = _export_text(payload, format)
            summary[format] = {"bytes": len(payload), "coverage": {
                key: marker in text for key, marker in MARKERS.items()}}
            live._require(all(summary[format]["coverage"].values()),
                          "semantic verification: acceptance check failed")
            result = _stable_export(client, tmp_path.resolve() / "exports",
                                    document=document_id, format=format)
            module_text = _export_text(Path(result["path"]).read_bytes(), format)
            live._require(all(marker in module_text for marker in MARKERS.values()),
                          "semantic verification: text readback mismatch")
            live._require(result["tab_ids"] == [first, child, second]
                          and result["scope"] == "all_tabs", "semantic verification: acceptance check failed")
            summary[format]["module_all_tabs_verified"] = True
        live._assert_private(session, document_id)
        # Synthetic identity is evidence only; never an input/deletion authority.
        summary["document_id"] = document_id
        summary["tab_ids"] = ids
    # The context returns only after DELETE + direct GET 404 and journal removal.
    summary["cleanup"] = "run_owned_deleted_direct_404_verified_journal_removed"
    record_property("native_export_characterization", json.dumps(summary, sort_keys=True))
    print("NATIVE_EXPORT_CHARACTERIZATION " + json.dumps(summary, sort_keys=True))


def test_live_export_module_single_tab(google_session, tmp_path: Path):
    if shutil.which("gs") is None:
        pytest.skip("Ghostscript required for independent native PDF text characterization")
    session, client = google_session
    with _temporary_document(session, client, client.create_document) as document_id:
        before = client.get_document(document_id)
        tab_id = select_tab(before, None).tab_id
        client.batch_update(document_id, [{"insertText": {
            "location": {"index": 1, "tabId": tab_id}, "text": MARKERS["first_root"]}}],
            before["revisionId"], retry_safe=False)
        root = tmp_path.resolve() / "exports"
        for format in MIMES:
            result = _stable_export(client, root, document=document_id, format=format)
            live._require(result["ok"] is True and result["verified"] is True,
                          "semantic verification: acceptance check failed")
            live._require(result["scope"] == "all_tabs" and result["tab_ids"] == [tab_id],
                          "semantic verification: acceptance check failed")
            live._require(MARKERS["first_root"] in _export_text(Path(result["path"]).read_bytes(), format),
                          "semantic verification: text readback mismatch")
    print("NATIVE_EXPORT_MODULE_CLEANUP " + document_id
          + " run_owned_deleted_direct_404_verified_journal_removed")
