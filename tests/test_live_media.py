"""Opt-in installed-MCP acceptance for links, public images and native exports."""
import asyncio
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import stat

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from google_docs_mcp.client import DocsMCPError, select_tab
from test_native_api import _temporary_document, google_session
from test_live_exports import _export_text
import test_live_google as live


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_GOOGLE_DOCS_MCP_LIVE") != "1",
    reason="Live media acceptance requires explicit synthetic-document authorization.",
)
IMAGE = "https://www.gstatic.com/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png"
SEED = """# عنوان کوتاه فارسی

HERMESMEDIAROOTMARKER

متن فارسی و English 🧪 با [منبع اصلی](https://example.com/media-source).

جای تصویر 🧪

- مورد فهرست فارسی

| موضوع | منبع |
| --- | --- |
| نمودار | [لینک جدول](https://example.com/media-table) |
"""


async def _exercise(session, client, document_id, tmp_path):
    # require_verified checks the result contract only; it never configures TLS.
    async def call(name, args, *, require_verified=True):
        result = await live._call(session, name, {"document": document_id, **args})
        if result.get("ok") is not True:
            print("LIVE_MEDIA_ERROR " + json.dumps({"tool": name, "error": result.get("error")}, ensure_ascii=True))
        if require_verified:
            live._require_ok(result, "apply")
        else:
            live._require(result.get("ok") is True, "preview: invalid preview")
        return result

    before = client.get_document(document_id)
    tab_id = select_tab(before, None).tab_id
    await call("docs_replace_markdown", {"markdown": SEED, "expected_revision_id": before["revisionId"],
                                         "tab_id": tab_id, "format_profile": "persian"})
    read = await call("docs_read", {"tab_id": tab_id, "max_chars": 1})
    links = {item["text"]: item["target"] for item in read["links"]}
    live._require(links == {"منبع اصلی": {"url": "https://example.com/media-source"},
                           "لینک جدول": {"url": "https://example.com/media-table"}},
                  "semantic verification: text readback mismatch")
    live._require(read["images"] == [] and len(read["content"]) == 1,
                  "semantic verification: acceptance check failed")
    before = client.get_document(document_id)
    # Google mechanically moves body named-range coordinates during insertion.
    # Seed one after the image anchor so real verification exercises that path.
    target = next(item for item in read["links"] if item["text"] == "لینک جدول")
    client.batch_update(document_id, [{"createNamedRange": {"name": "HERMES_MEDIA_RANGE",
        "range": {"startIndex": target["start_index"], "endIndex": target["end_index"],
                  "tabId": tab_id}}}], before["revisionId"], retry_safe=False)
    before = client.get_document(document_id)
    args = {"image_uri": IMAGE, "expected_revision_id": before["revisionId"], "tab_id": tab_id,
            "position": "after", "anchor_text": "جای تصویر 🧪", "width_pt": 120,
            "height_pt": 120, "format_profile": "persian"}
    preview = await call("docs_insert_image", args, require_verified=False)
    live._require(preview["applied"] is False and preview["valid"] is True, "preview: invalid preview")
    live._require(client.get_document(document_id) == before, "preview: preview mutated document")
    inserted = await call("docs_insert_image", {**args, "apply": True})
    live._require(inserted["verified"] is True and inserted["formatting_verified"] is True,
                  "apply: format not verified")
    raw = client.get_document(document_id)
    source_revision = raw["revisionId"]
    read = await call("docs_read", {"tab_id": tab_id})
    live._require([item["object_id"] for item in read["images"]] == [inserted["image_id"]],
                  "semantic verification: acceptance check failed")
    live._require(read["images"][0]["start_index"] == preview["index"],
                  "semantic verification: acceptance check failed")
    live._require("contentUri" not in json.dumps(read), "privacy verification: acceptance check failed")
    for arguments, code in [({**args, "apply": True}, "stale_revision"),
                            ({**args, "expected_revision_id": source_revision,
                              "image_uri": "https://127.0.0.1/image.png", "apply": True}, "invalid_input")]:
        rejected = await live._call(session, "docs_insert_image", {"document": document_id, **arguments})
        live._require_error(rejected, code, "stale guard")
        live._require(client.get_document(document_id)["revisionId"] == source_revision,
                      "stale guard: rejected operation mutated document")

    # Lost-success response: a real one-effect write, deterministic transport
    # interruption, and explicit readback recovery; never replay the operation.
    from google_docs_mcp.images import insert_image
    class LostResponse:
        writes = 0
        def __getattr__(self, name):
            return getattr(client, name)
        def batch_update(self, *args, **kwargs):
            self.writes += 1
            client.batch_update(*args, **kwargs)
            raise DocsMCPError("google_unavailable", "Synthetic lost response.")
    proxy = LostResponse()
    try:
        insert_image(proxy, document=document_id, image_uri=IMAGE, expected_revision_id=source_revision,
                     tab_id=tab_id, width_pt=80, position="end", apply=True)
    except DocsMCPError as error:
        live._require(error.code == "google_unavailable" and proxy.writes == 1,
                      "recovery: acceptance check failed")
    else:
        live._fail("recovery: acceptance check failed")
    recovered = await call("docs_read", {"tab_id": tab_id})
    live._require(len(recovered["images"]) == 2, "recovery: acceptance check failed")
    live._require(len(recovered["links"]) == 2 and "جای تصویر 🧪" in recovered["content"],
                  "recovery: text readback mismatch")

    # Native export must retain root AND child tab content, links and images.
    created = await call("docs_manage_tab", {"expected_revision_id": recovered["revision_id"],
        "action": "create", "title": "پیوست آزمایشی", "parent_tab_id": tab_id, "apply": True})
    child_id = created["tab"]["tab_id"]
    revision = client.get_document(document_id)["revisionId"]
    await call("docs_insert_text", {"text": "HERMESMEDIACHILDMARKER\n", "expected_revision_id": revision,
                                   "tab_id": child_id, "format_profile": "persian", "apply": True})
    final_revision = client.get_document(document_id)["revisionId"]
    await call("docs_insert_image", {"image_uri": IMAGE, "expected_revision_id": final_revision,
        "tab_id": tab_id, "position": "after", "anchor_text": "نمودار", "width_pt": 60,
        "format_profile": "persian", "apply": True})
    final_read = await call("docs_read", {"tab_id": tab_id})
    live._require(len(final_read["images"]) == 3 and len(final_read["links"]) == 2,
                  "semantic verification: acceptance check failed")
    final_revision = client.get_document(document_id)["revisionId"]
    exports = []
    for format in ("pdf", "docx"):
        result = await call("docs_export", {"format": format, "scope": "all_tabs"})
        path = Path(result["path"])
        payload = path.read_bytes()
        live._require(stat.S_IMODE(path.stat().st_mode) == 0o600, "privacy verification: acceptance check failed")
        live._require(hashlib.sha256(payload).hexdigest() == result["sha256"],
                      "semantic verification: acceptance check failed")
        text = _export_text(payload, format)
        live._require("HERMESMEDIAROOTMARKER" in text and "HERMESMEDIACHILDMARKER" in text,
                      "semantic verification: text readback mismatch")
        live._require(set(result["tab_ids"]) == {tab_id, child_id} and result["scope"] == "all_tabs",
                      "semantic verification: acceptance check failed")
        if format == "docx":
            import io
            import zipfile
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                media = [name for name in archive.namelist() if name.startswith("word/media/")]
                live._require(bool(media), "DOCX verification: acceptance check failed")
        exports.append({"format": format, "path": str(path), "sha256": result["sha256"], "bytes": len(payload)})
    live._require(client.get_document(document_id)["revisionId"] == final_revision,
                  "semantic verification: revision readback mismatch")
    return {"exports": exports, "links": 2, "images": 3,
            "image_preview_apply_stale_invalid_recovery": True, "tab_coverage": "root_and_child"}


def test_live_media_through_installed_mcp(google_session, tmp_path, record_property):
    import shutil
    if shutil.which("gs") is None:
        pytest.skip("Ghostscript required for independent PDF verification")
    authorized, client = google_session
    with _temporary_document(authorized, client, client.create_document) as document_id:
        async def run():
            params = StdioServerParameters(command=str(live._ROOT / "scripts/run-mcp"))
            with open(os.devnull, "w") as errlog:
                async with stdio_client(params, errlog=errlog) as (reader, writer):
                    async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=240)) as session:
                        await session.initialize()
                        return await _exercise(session, client, document_id, tmp_path)
        summary = asyncio.run(run())
        live._assert_private(authorized, document_id)
        summary["document_id"] = document_id
    summary["private_document_deleted_and_404_verified"] = True
    record_property("media_acceptance", json.dumps(summary, sort_keys=True))
    print("LIVE_MEDIA_ACCEPTANCE " + json.dumps(summary, sort_keys=True))
