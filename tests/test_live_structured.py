"""Explicit-opt-in real Google acceptance for scoped structured editing.

Only run-owned private synthetic documents are selected. The existing test
journal, identity reconciliation, permission and verified cleanup path is reused.
"""
import asyncio
from copy import deepcopy
from datetime import timedelta
import os
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from google_docs_mcp.client import DocsMCPError
import test_live_google as live
from test_native_api import google_session, _temporary_document


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_GOOGLE_DOCS_MCP_LIVE") != "1",
    reason="Live structured writes require explicit synthetic-document authorization.",
)

_SEED = """# محفوظ 🧪

متن **محفوظ** و [پیوند](https://example.com/preserve).

- مورد فهرست

# هدف

متن قدیمی

## فرزند

جزئیات قدیمی

# پایان

پاراگراف مرجع

| بیرون | مقدار |
| --- | --- |
| ثابت | داده |
"""
_REPLACEMENT = """متن **جدید 🧪** و [منبع](https://example.com/new).

| شاخص | مقدار |
| --- | --- |
| خرید | **پنج** |
"""


def _tabs(document):
    result = {}
    def visit(items, parent=None):
        for index, tab in enumerate(items):
            props = tab["tabProperties"]
            result[props["tabId"]] = (props["title"], parent, index, tab["documentTab"])
            visit(tab.get("childTabs", []), props["tabId"])
    visit(document["tabs"])
    return result


def _body(document, tab_id):
    return _tabs(document)[tab_id][3]["body"]


def _text(value):
    if isinstance(value, dict):
        if "textRun" in value:
            return value["textRun"].get("content", "")
        return "".join(_text(v) for v in value.values())
    if isinstance(value, list):
        return "".join(_text(v) for v in value)
    return ""


def _tables(document, tab_id):
    return [e["table"] for e in _body(document, tab_id)["content"] if "table" in e]


def _cells(table):
    return [[_text(cell["content"]) for cell in row["tableCells"]] for row in table["tableRows"]]


def _check(value, stage, reason="acceptance check failed"):
    live._require(bool(value), f"{stage}: {reason}")


def _check_layout(body, *, direction, alignment, indent):
    paragraphs = list(live._paragraphs(body["content"]))
    _check(paragraphs, "Persian API verification")
    for paragraph in paragraphs:
        style = paragraph.get("paragraphStyle", {})
        _check(style.get("direction") == direction, "Persian API verification")
        _check(style.get("alignment") == alignment, "Persian API verification")
        # Docs indentation is logical: indentStart is the physical right in RTL.
        start_indent, end_indent = (indent, 0) if direction == "RIGHT_TO_LEFT" else (0, indent)
        _check(style.get("indentStart", {}).get("magnitude", 0) == start_indent, "Persian API verification")
        _check(style.get("indentEnd", {}).get("magnitude", 0) == end_indent, "Persian API verification")
        if direction == "RIGHT_TO_LEFT":
            for run in live._text_runs(paragraph):
                _check(run.get("textStyle", {}).get("weightedFontFamily", {}).get("fontFamily") == "Vazirmatn",
                       "Persian API verification")


async def _exercise(session, client, document_id, tmp_path):
    first = client.get_document(document_id)
    tab_id = next(iter(_tabs(first)))
    seed = await live._call(session, "docs_replace_markdown", {
        "document": document_id, "tab_id": tab_id, "expected_revision_id": first["revisionId"],
        "markdown": _SEED, "format_profile": "plain",
    })
    live._require_ok(seed, "replace")
    operations = []

    async def mutate(name, *, target=tab_id, **args):
        before = client.get_document(document_id)
        arguments = {"document": document_id, "expected_revision_id": before["revisionId"], **args}
        if name != "docs_manage_tab":
            arguments["tab_id"] = target
        preview = await live._call(session, name, {**arguments, "apply": False})
        live._require_ok(preview, "preview")
        _check(client.get_document(document_id) == before, "preview", "preview mutated document")
        result = await live._call(session, name, {**arguments, "apply": True})
        live._require_ok(result, "apply")
        _check(result.get("verified") is True, "apply", "format not verified")
        after = client.get_document(document_id)
        _check(result.get("after_revision_id", result.get("revision_id")) == after["revisionId"],
               "apply", "revision readback mismatch")
        if before["revisionId"] != after["revisionId"]:
            stale = await live._call(session, name, {**arguments, "apply": True})
            live._require_error(stale, "stale_revision", "stale guard")
            _check(client.get_document(document_id) == after, "stale guard", "rejected operation mutated document")
        operations.append(name + ":" + args.get("action", args.get("format_profile", "")))
        return result, before, after

    for position, anchor in [("start", None), ("end", None),
                             ("before", "پاراگراف مرجع"), ("after", "پاراگراف مرجع")]:
        marker = f"افزوده {position} 🧪"
        _, before, after = await mutate("docs_edit_section", action="insert", position=position,
                                       anchor_text=anchor, markdown=marker, format_profile="persian")
        _check(marker in _text(_body(after, tab_id)), "apply", "text readback mismatch")
        _check(_text(_body(before, tab_id)).replace("\n", "") in
               _text(_body(after, tab_id)).replace(marker, "").replace("\n", ""),
               "apply", "text readback mismatch")

    _, before, after = await mutate("docs_edit_section", action="replace", heading_text="هدف",
                                   markdown=_REPLACEMENT, format_profile="persian")
    text = _text(_body(after, tab_id))
    _check("متن قدیمی" not in text and "جزئیات قدیمی" not in text and "متن جدید 🧪" in text,
           "apply", "text readback mismatch")
    _check("محفوظ" in text and "پایان" in text and "پاراگراف مرجع" in text, "apply")
    _check(len(_tables(after, tab_id)) == 2 and _cells(_tables(after, tab_id)[1]) ==
           _cells(_tables(before, tab_id)[0]), "apply")
    inventory = await live._call(session, "docs_read", {"document": document_id, "tab_id": tab_id})
    _check(inventory.get("tables") == [
        {"table_index": 0, "rows": 2, "columns": 2, "editable": True},
        {"table_index": 1, "rows": 2, "columns": 2, "editable": True}], "docs_read")

    _, before, after = await mutate("docs_edit_table", action="set_cell", table_index=0,
                                   row_index=1, column_index=1, markdown="**هفت 🧪** و [لینک](https://example.com/cell)")
    _check(_cells(_tables(after, tab_id)[0])[1][1].strip() == "هفت 🧪 و لینک", "apply")
    cell = _tables(after, tab_id)[0]["tableRows"][1]["tableCells"][1]
    runs = [run for paragraph in live._paragraphs(cell["content"]) for run in live._text_runs(paragraph)]
    _check(any("هفت" in r.get("content", "") and r.get("textStyle", {}).get("bold") is True for r in runs), "apply")
    _check(any(r.get("textStyle", {}).get("link", {}).get("url") == "https://example.com/cell" for r in runs), "apply")
    for action, selectors, dimensions in [
        ("insert_row", {"row_index": 1, "side": "before"}, (3, 2)),
        ("delete_row", {"row_index": 1}, (2, 2)),
        ("insert_column", {"column_index": 1, "side": "after"}, (2, 3)),
        ("delete_column", {"column_index": 2}, (2, 2)),
    ]:
        _, before, after = await mutate("docs_edit_table", action=action, table_index=0, **selectors)
        table = _tables(after, tab_id)[0]
        _check((table["rows"], table["columns"]) == dimensions, "apply")
        _check(_cells(_tables(before, tab_id)[1]) == _cells(_tables(after, tab_id)[1]), "apply")
    await mutate("docs_edit_table", action="set_cell", table_index=0, row_index=1, column_index=1, markdown="")

    _, before, after = await mutate("docs_format", format_profile="persian", right_indent_pt=12)
    _check(_text(_body(before, tab_id)) == _text(_body(after, tab_id)), "apply", "text readback mismatch")
    _check_layout(_body(after, tab_id), direction="RIGHT_TO_LEFT", alignment="END", indent=12)
    _, before, after = await mutate("docs_format", format_profile="persian", right_indent_pt=12)
    _check(before["revisionId"] == after["revisionId"], "apply", "preview mutated document")
    await mutate("docs_format", heading_text="پایان", format_profile="english", right_indent_pt=0)
    await mutate("docs_format", heading_text="پایان", format_profile="persian", right_indent_pt=12)

    root_content = deepcopy(_body(client.get_document(document_id), tab_id))
    created, _, after = await mutate("docs_manage_tab", action="create", title="آرشیو")
    archive_id = created.get("tab", {}).get("tab_id")
    _check(isinstance(archive_id, str) and archive_id != tab_id and archive_id in _tabs(after), "apply")
    await mutate("docs_manage_tab", action="rename", tab_id=archive_id, title="آرشیو تازه")
    child, _, after = await mutate("docs_manage_tab", action="create", title="زیرتب", parent_tab_id=archive_id)
    child_id = child.get("tab", {}).get("tab_id")
    _check(_tabs(after)[child_id][1] == archive_id, "apply")
    await mutate("docs_manage_tab", action="move", tab_id=child_id, parent_tab_id=None, index=0)
    await mutate("docs_manage_tab", action="move", tab_id=child_id, parent_tab_id=archive_id, index=0)
    _, _, after = await mutate("docs_manage_tab", action="move", tab_id=archive_id, index=0)
    _check(_tabs(after)[archive_id][2] == 0 and _body(after, tab_id) == root_content, "apply")

    for name, args, code in [
        ("docs_manage_tab", {"action": "move", "tab_id": archive_id, "parent_tab_id": child_id, "index": 0}, "invalid_input"),
        ("docs_edit_section", {"action": "replace", "heading_text": "مفقود", "markdown": "x", "tab_id": tab_id}, "heading_match_mismatch"),
    ]:
        before = client.get_document(document_id)
        rejected = await live._call(session, name, {"document": document_id, "expected_revision_id": before["revisionId"],
                                                    "apply": True, **args})
        live._require_error(rejected, code, "stale guard")
        _check(client.get_document(document_id) == before, "stale guard", "rejected operation mutated document")

    # Multi-tab omission must not select a default for body mutation.
    before = client.get_document(document_id)
    rejected = await live._call(session, "docs_format", {"document": document_id,
                                "expected_revision_id": before["revisionId"], "apply": True})
    live._require_error(rejected, "multiple_tabs_require_tab_id", "stale guard")
    _check(client.get_document(document_id) == before, "stale guard")

    # A real committed first phase with an injected later failure proves recovery
    # against actual Google indices without inducing a provider/network outage.
    from google_docs_mcp.tables import edit_table
    class FailSecondBatch:
        def __init__(self):
            self.batches = 0
        def __getattr__(self, name):
            return getattr(client, name)
        def batch_update(self, *args, **kwargs):
            self.batches += 1
            if self.batches == 2:
                raise DocsMCPError("google_unavailable", "Synthetic phase interruption.")
            return client.batch_update(*args, **kwargs)
    proxy = FailSecondBatch()
    try:
        edit_table(proxy, tmp_path / "recovery", document_id, before["revisionId"], "insert_row", 0,
                   row_index=0, side="after", tab_id=tab_id, apply=True)
    except DocsMCPError as error:
        _check(error.code == "partial_write_requires_recovery", "recovery")
        _check(proxy.batches == 2, "recovery")
        _check(any((tmp_path / "recovery").glob("recovery-*/document.docx")), "recovery")
    else:
        live._fail("recovery: acceptance check failed")
    await mutate("docs_format", format_profile="persian", right_indent_pt=12)
    # Export is test-only; no new public export API.
    pdf = client.export_file(document_id, "application/pdf")
    _check(pdf.startswith(b"%PDF"), "DOCX verification")
    destination = tmp_path / "structured-persian.pdf"
    destination.write_bytes(pdf)
    destination.chmod(0o600)
    return operations


def test_live_structured_editing_through_mcp(google_session, tmp_path, capfd, caplog, record_property):
    authorized, client = google_session
    outcome = live._Outcome()
    operations = []
    try:
        with _temporary_document(authorized, client, client.create_document) as document_id:
            async def run():
                params = StdioServerParameters(command=str(live._ROOT / "scripts/run-mcp"))
                with open(os.devnull, "w") as errlog:
                    async with stdio_client(params, errlog=errlog) as (read, write):
                        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=240)) as session:  # type: ignore[arg-type]
                            await session.initialize()
                            return await _exercise(session, client, document_id, tmp_path)
            operations = asyncio.run(run())
    except BaseException as error:
        outcome.capture(error)
    capfd.readouterr()
    caplog.clear()
    live._finish(outcome)
    record_property("structured_actions", ",".join(operations))
    record_property("private_run_document_cleanup", "verified")
