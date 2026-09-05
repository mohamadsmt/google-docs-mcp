from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest

import test_live_google as live


def test_api_formatting_accepts_proto_json_zero_omission() -> None:
    paragraphs = []
    for index in range(8):
        paragraphs.append({"paragraph": {
            "paragraphStyle": {
                "direction": "RIGHT_TO_LEFT", "alignment": "END",
                "indentStart": {"unit": "PT"}, "indentEnd": {"unit": "PT"},
                "namedStyleType": "HEADING_1" if index == 0 else "NORMAL_TEXT",
            },
            "elements": [{"textRun": {"content": "synthetic", "textStyle": {
                "weightedFontFamily": {"fontFamily": "Vazirmatn"}, "bold": True,
            }}}],
        }})
    document = {"tabs": [{"documentTab": {"body": {"content": paragraphs}},
                          "tabProperties": {"tabId": "test-tab", "title": "Synthetic"}}]}
    client = SimpleNamespace(get_document=lambda _: document)
    live._assert_api_persian_formatting(cast(Any, client), "synthetic_id", "test-tab")


@pytest.mark.parametrize("extra", [
    {"type": "user", "role": "reader"},
    {"type": "user", "role": "writer"},
    {"type": "unknown", "role": "owner"},
])
def test_private_means_only_owner_permissions(extra: dict) -> None:
    result = {"permissions": [{"type": "user", "role": "owner"}, extra]}
    session = SimpleNamespace(get=lambda *args, **kwargs: SimpleNamespace(
        status_code=200, json=lambda: result,
    ))
    with pytest.raises(live._CheckFailed, match="privacy verification"):
        live._assert_private(cast(Any, session), "synthetic_id")


def test_privacy_rejects_incomplete_permission_listing() -> None:
    result = {"permissions": [{"type": "user", "role": "owner"}],
              "nextPageToken": "SYNTHETIC_CANARY"}
    calls = []

    def get(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status_code=200, json=lambda: result)

    with pytest.raises(live._CheckFailed, match="privacy verification"):
        live._assert_private(cast(Any, SimpleNamespace(get=get)), "synthetic_id")
    assert "nextPageToken" in calls[0]["params"]["fields"]


def test_diagnostic_does_not_echo_unknown_enum_shaped_content() -> None:
    canary = "synthetic_secret_canary"
    with pytest.raises(live._CheckFailed) as error:
        live._require_ok({"ok": False, "error": {
            "code": canary, "failure_code": canary, "phase": canary,
        }}, "docs_create")
    assert canary not in str(error.value)


def test_expected_semantics_overlay_only_heading_and_coalesce() -> None:
    markdown = "# A **B** [C](https://example.com)\n\nD **E**\n\n| H | I |\n| --- | --- |\n| **J** | [K](https://example.com) |\n| L |"
    raw = live.candidate_semantic(live.parse_markdown(markdown))
    expected = live._expected_semantic(markdown)
    assert expected["blocks"][0] == {
        "type": "paragraph", "heading": 1,
        "runs": [{"text": "A B ", "bold": True, "link": None},
                 {"text": "C", "bold": True, "link": "https://example.com"}],
    }
    assert expected["blocks"][1:] == raw["blocks"][1:]
    assert raw["blocks"][0]["runs"][0]["bold"] is False
    assert expected["blocks"][-1]["rows"][-1][-1] == {"runs": []}


@pytest.mark.parametrize("markdown", [live._INITIAL_MARKDOWN, live._FINAL_MARKDOWN], ids=["create", "replace"])
def test_live_fixtures_cover_cell_emphasis_links_and_empty_padding(markdown) -> None:
    semantic = live.candidate_semantic(live.parse_markdown(markdown))
    table = next(block for block in semantic["blocks"] if block["type"] == "table")
    cells = [cell for row in table["rows"] for cell in row]
    assert any(not cell["runs"] for cell in cells)
    assert any(run["bold"] for cell in cells for run in cell["runs"])
    assert any(run["link"] for cell in cells for run in cell["runs"])
    paragraph = semantic["blocks"][1]
    assert "🧪" in paragraph["runs"][0]["text"]


_START = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
_TITLE = "TEMP — Hermes Google Docs MCP acceptance — 20260905T120000.000000Z"


def _file(**changes):
    return {"id": "synthetic_doc_123", "name": _TITLE,
            "createdTime": "2026-09-05T12:00:01Z", "ownedByMe": True,
            "mimeType": live._NATIVE_DOCUMENT_MIME, **changes}


def _listing(files, **changes):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=200, json=lambda: {"files": files, **changes})

    return SimpleNamespace(get=get), calls


def test_reconcile_exact_name_and_bounded_creation_window() -> None:
    session, calls = _listing([_file()])
    assert live._reconcile_created(session, _TITLE, _START, _START + timedelta(seconds=10)) == "synthetic_doc_123"
    assert len(calls) == 1
    query = calls[0][1]["params"]["q"]
    assert "name = '" + _TITLE + "'" in query
    assert "createdTime >=" in query and "createdTime <=" in query
    assert "name contains" not in query
    assert calls[0][1]["params"]["pageSize"] == 100
    assert calls[0][1]["timeout"] == (20, 180)


@pytest.mark.parametrize("files,extra", [
    ([_file(), _file(id="synthetic_other_123")], {}),
    ([_file()], {"nextPageToken": "synthetic_private_page"}),
    ([_file(createdTime="malformed")], {}),
    ([_file(createdTime="2026-09-05T12:00:01")], {}),
])
def test_reconcile_rejects_ambiguity_incomplete_and_invalid_time(files, extra) -> None:
    session, _ = _listing(files, **extra)
    with pytest.raises(live._CheckFailed, match="recovery"):
        live._reconcile_created(session, _TITLE, _START, _START + timedelta(seconds=10))


@pytest.mark.parametrize("changes", [
    {"name": _TITLE + " other"},
    {"createdTime": "2026-09-05T11:59:59Z"},
    {"createdTime": "2026-09-05T12:00:11Z"},
    {"ownedByMe": False},
    {"mimeType": "text/plain"},
    {"id": "short"},
])
def test_reconcile_never_claims_near_matches(changes) -> None:
    session, _ = _listing([_file(**changes)])
    assert live._reconcile_created(session, _TITLE, _START, _START + timedelta(seconds=10)) is None


def test_failure_report_preserves_primary_and_cleanup_without_group_output() -> None:
    report = live._Outcome()
    try:
        live._require_ok({"ok": False, "error": {"code": "verification_failed",
                         "phase": "table_cell_styles"}}, "docs_create")
    except Exception as error:
        report.capture(ExceptionGroup("synthetic_secret_group", [error, ValueError("synthetic_secret")]))
    report.capture(RuntimeError("synthetic_cleanup_secret"), cleanup=True)
    with pytest.raises(pytest.fail.Exception) as failed:
        live._finish(report)
    message = str(failed.value)
    assert "docs_create" in message and "code=verification_failed" in message
    assert "phase=table_cell_styles" in message and "cleanup:" in message
    assert "synthetic" not in message
    assert "ExceptionGroup" not in message
    assert failed.value.pytrace is False
    assert failed.value.__context__ is None


@pytest.mark.parametrize("during_cleanup", [False, True])
def test_interrupt_wins_without_masking_and_keeps_cleanup_note(during_cleanup) -> None:
    report = live._Outcome()
    interrupt = KeyboardInterrupt()
    report.capture(ValueError("synthetic_primary_secret"))
    report.capture(interrupt, cleanup=during_cleanup)
    report.capture(RuntimeError("synthetic_cleanup_secret"), cleanup=True)
    with pytest.raises(KeyboardInterrupt) as caught:
        live._finish(report)
    assert caught.value is interrupt
    assert any("cleanup:" in note for note in caught.value.__notes__)
    assert all("synthetic" not in note for note in caught.value.__notes__)


def test_journal_private_minimal_and_only_owned_file_removed(tmp_path) -> None:
    journal = live._create_journal(_TITLE, _START, directory=tmp_path)
    unrelated = tmp_path / "unrelated.json"
    unrelated.write_text("do not remove")
    assert journal.path.stat().st_mode & 0o777 == 0o600
    assert json.loads(journal.path.read_text()) == {"title": _TITLE, "started_at": _START.isoformat()}
    journal.remove()
    assert not journal.path.exists()
    assert unrelated.read_text() == "do not remove"


def test_journal_replaced_path_is_not_removed(tmp_path) -> None:
    journal = live._create_journal(_TITLE, _START, directory=tmp_path)
    journal.path.rename(tmp_path / "original")
    journal.path.write_text("replacement")
    with pytest.raises(live._CheckFailed, match="recovery"):
        journal.remove()
    assert journal.path.read_text() == "replacement"


@pytest.mark.parametrize("interrupted", [False, True])
@pytest.mark.parametrize("resolution", ["found", "none", "ambiguous", "delete-error"])
def test_lost_create_result_reconciles_only_after_process_close(monkeypatch, tmp_path, interrupted, resolution) -> None:
    events = []
    interrupt = KeyboardInterrupt()
    journals = []
    original_create_journal = live._create_journal

    def journal(title, started):
        result = original_create_journal(title, started, directory=tmp_path)
        journals.append(result)
        return result

    @asynccontextmanager
    async def transport(*args, **kwargs):
        try:
            yield (None, None)
        finally:
            events.append("process closed")

    class Session:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def initialize(self):
            pass

        async def call_tool(self, name, arguments):
            events.append(name)
            raise interrupt if interrupted else RuntimeError("synthetic_transport_secret")

    def reconcile(*args):
        assert "process closed" in events
        events.append("reconcile")
        if resolution == "ambiguous":
            live._fail("recovery: ambiguous listing")
        return None if resolution == "none" else "synthetic_doc_123"

    def delete(_):
        events.append("delete")
        if resolution == "delete-error":
            raise RuntimeError("synthetic_delete_secret")

    client = SimpleNamespace(delete_file=delete)
    monkeypatch.setattr(live, "load_credentials", lambda: None)
    monkeypatch.setattr(live, "AuthorizedSession", lambda _: SimpleNamespace(close=lambda: events.append("auth closed")))
    monkeypatch.setattr(live, "GoogleDocsClient", lambda _: client)
    monkeypatch.setattr(live, "stdio_client", transport)
    monkeypatch.setattr(live, "ClientSession", Session)
    monkeypatch.setattr(live, "_create_journal", journal)
    monkeypatch.setattr(live, "_reconcile_created", reconcile)
    monkeypatch.setattr(live, "_assert_deleted_directly", lambda *_: events.append("direct 404"))
    report = asyncio.run(live._run_live_acceptance())
    expected = ["docs_create", "process closed", "reconcile"]
    if resolution in {"found", "delete-error"}:
        expected.append("delete")
    if resolution == "found":
        expected.append("direct 404")
    assert events == expected + ["auth closed"]
    assert journals[0].path.exists() is (resolution != "found")
    assert (report.cleanup is not None) is (resolution != "found")
    with pytest.raises(KeyboardInterrupt if interrupted else pytest.fail.Exception) as failed:
        live._finish(report)
    assert "synthetic_transport_secret" not in str(failed.value)


@pytest.mark.parametrize("fault", [None, "preview-content", "preview-revision", "apply-content",
                                    "apply-revision", "create-semantic", "replace-semantic"])
def test_acceptance_uses_content_and_independent_readbacks(monkeypatch, tmp_path, fault) -> None:
    events = []
    state = {"content": live._INITIAL_MARKDOWN, "revision": "revision1", "deleted": False, "reads": 0}
    original_journal = live._create_journal
    monkeypatch.setattr(live, "_create_journal", lambda title, started: original_journal(title, started, directory=tmp_path))

    def semantic(markdown):
        expected = live._expected_semantic(markdown)
        return {"sha256": live.semantic_sha256(expected), "block_count": len(expected["blocks"])}

    @asynccontextmanager
    async def transport(*args, **kwargs):
        try:
            yield (None, None)
        finally:
            events.append("closed")

    class Session:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def initialize(self):
            pass

        async def call_tool(self, name, arguments):
            events.append(name)
            result = {"ok": True, "verified": True, "document_id": "synthetic_doc_123", "tab_id": "tab1"}
            if name == "docs_create":
                state["title"] = arguments["title"]
                result.update(revision_id="revision1", format_profile="persian", semantic=semantic(live._INITIAL_MARKDOWN))
                if fault == "create-semantic":
                    result["semantic"]["sha256"] = "incorrect"
            elif name == "docs_read":
                if state["deleted"]:
                    result = {"ok": False, "error": {"code": "document_not_found"}}
                else:
                    state["reads"] += 1
                    result.update(content=state["content"], revision_id=state["revision"],
                                  name=state["title"], mime_type=live._NATIVE_DOCUMENT_MIME)
                    if state["reads"] == 2:
                        if fault == "preview-content":
                            result["content"] = "synthetic_mutation"
                        if fault == "preview-revision":
                            result["revision_id"] = "different"
                    if state["reads"] == 3:
                        if fault == "apply-content":
                            result["content"] = live._INITIAL_MARKDOWN
                        if fault == "apply-revision":
                            result["revision_id"] = "revision1"
            elif name == "docs_edit_text":
                assert arguments["expected_revision_id"] == "revision1"
                assert arguments["replacements"][0]["expected_count"] == 1
                if arguments["apply"] is False:
                    result["replacements"] = [{"actual_count": 1, "expected_count": 1}]
                else:
                    state["content"] = state["content"].replace(live._INITIAL_ANCHOR, live._EDITED_ANCHOR)
                    state["revision"] = "revision2"
                    result.update(after_revision_id="revision2", replacements=[{
                        "occurrences_changed": 1, "after_old_count": 0, "after_new_count": 1}])
            elif name == "docs_replace_markdown":
                if arguments["expected_revision_id"] == "revision2":
                    result = {"ok": False, "error": {"code": "stale_revision"}}
                else:
                    assert arguments["expected_revision_id"] == "revision3"
                    state["content"] = live._FINAL_MARKDOWN
                    state["revision"] = "revision4"
                    result.update(before_revision_id="revision3", after_revision_id="revision4", semantic=semantic(live._FINAL_MARKDOWN))
                    if fault == "replace-semantic":
                        result["semantic"]["sha256"] = "incorrect"
            else:
                raise AssertionError("unexpected tool")
            return SimpleNamespace(isError=False, structuredContent=result)

    def external_edit(*args):
        events.append("external")
        state["content"] += "\n" + live._SENTINEL
        state["revision"] = "revision3"

    def delete(*args):
        events.append("delete")
        state["deleted"] = True

    client = SimpleNamespace(delete_file=delete, batch_update=external_edit,
                             get_document=lambda _: {"revisionId": state["revision"]})
    monkeypatch.setattr(live, "load_credentials", lambda: None)
    monkeypatch.setattr(live, "AuthorizedSession", lambda _: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(live, "GoogleDocsClient", lambda _: client)
    monkeypatch.setattr(live, "stdio_client", transport)
    monkeypatch.setattr(live, "ClientSession", Session)
    monkeypatch.setattr(live, "_assert_private", lambda *_: events.append("private"))
    monkeypatch.setattr(live, "_assert_api_persian_formatting", lambda *_: events.append("API"))
    monkeypatch.setattr(live, "_assert_docx_formatting", lambda *_: events.append("DOCX"))
    monkeypatch.setattr(live, "_assert_deleted_directly", lambda *_: events.append("direct 404"))
    report = asyncio.run(live._run_live_acceptance())
    assert state["deleted"] is True
    assert report.cleanup is None
    assert not list(tmp_path.iterdir())
    assert events[-4:] == ["delete", "direct 404", "docs_read", "closed"]
    if fault is not None:
        with pytest.raises(pytest.fail.Exception):
            live._finish(report)
    else:
        live._finish(report)
        assert events == ["docs_create", "DOCX", "private", "API", "docs_read",
                          "docs_edit_text", "docs_read", "docs_edit_text", "docs_read",
                          "external", "docs_replace_markdown", "docs_read",
                          "docs_replace_markdown", "DOCX", "docs_read", "API",
                          "delete", "direct 404", "docs_read", "closed"]


@pytest.mark.parametrize("missing", [None, "paragraphStyle", "font"])
def test_api_formatting_checks_empty_table_cells(missing) -> None:
    style = {"direction": "RIGHT_TO_LEFT", "alignment": "END", "indentStart": {"unit": "PT"},
             "indentEnd": {"unit": "PT"}, "namedStyleType": "HEADING_1"}
    text_style = {"weightedFontFamily": {"fontFamily": "Vazirmatn"}, "bold": True}
    body = [{"paragraph": {"paragraphStyle": style, "elements": [{"textRun": {
        "content": "synthetic", "textStyle": text_style}}]}} for _ in range(8)]
    # An empty heading cell requires the font, not bold on its newline.
    empty = {"paragraphStyle": style, "elements": [{"textRun": {"content": "\n", "textStyle": {
        "weightedFontFamily": {"fontFamily": "Vazirmatn"}}}}]}
    if missing == "paragraphStyle":
        empty.pop("paragraphStyle")
    elif missing == "font":
        empty["elements"] = [{"textRun": {"content": "\n", "textStyle": {}}}]
    body.append({"table": {"tableRows": [{"tableCells": [{"content": [{"paragraph": empty}]}]}]}})
    document = {"tabs": [{"tabProperties": {"tabId": "tab1", "title": "Synthetic"}, "documentTab": {"body": {"content": body}}}]}
    client = SimpleNamespace(get_document=lambda _: document)
    if missing is None:
        live._assert_api_persian_formatting(client, "synthetic_doc_123", "tab1")
    else:
        with pytest.raises(live._CheckFailed, match="Persian API verification"):
            live._assert_api_persian_formatting(client, "synthetic_doc_123", "tab1")


def test_live_gate_skips_without_authorization_or_transport(monkeypatch) -> None:
    import os
    import subprocess
    import sys

    environment = dict(os.environ)
    environment.pop("RUN_GOOGLE_DOCS_MCP_LIVE", None)
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    # A real fresh interpreter proves import-time gating, not a patched flag.
    script = """import sys, pytest
sys.path.insert(0, 'tests')
import test_live_google as live
def forbidden(*args, **kwargs):
    raise AssertionError('offline gate performed I/O')
live.load_credentials = forbidden
live.AuthorizedSession = forbidden
live.stdio_client = forbidden
raise SystemExit(pytest.main(['tests/test_live_google.py', '-o', 'addopts=', '-p', 'no:cacheprovider']))
"""
    result = subprocess.run([sys.executable, "-c", script], cwd=live._ROOT,
                            env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0
    assert "1 skipped" in result.stdout
    assert "offline gate performed I/O" not in result.stdout + result.stderr
