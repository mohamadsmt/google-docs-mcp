from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from dataclasses import dataclass
from stat import S_ISREG
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Never, cast

import pytest
from google.auth.transport.requests import AuthorizedSession
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from google_docs_mcp.client import (
    DocsMCPError,
    GoogleDocsClient,
    load_credentials,
    select_tab,
    verify_persian_docx,
)
from google_docs_mcp.markdown import (
    candidate_semantic,
    parse_markdown,
    semantic_sha256,
)


_LIVE_ENABLED = os.getenv("RUN_GOOGLE_DOCS_MCP_LIVE") == "1"
_ROOT = Path(__file__).resolve().parents[1]
_DOCUMENT_ID_RE = re.compile(r"[A-Za-z0-9_-]{10,256}")
_SAFE_DIAGNOSTICS = {
    "code": frozenset({"verification_failed", "partial_write_requires_recovery",
                       "google_needs_reauth", "google_unavailable", "permission_denied",
                       "rate_limited", "recovery_unavailable", "stale_revision"}),
    "failure_code": frozenset({"verification_failed", "partial_write_requires_recovery",
                               "google_needs_reauth", "google_unavailable",
                               "permission_denied", "recovery_unavailable"}),
    "phase": frozenset({"initial_content", "table_structure", "table_structure_readback",
                        "table_cell_text", "table_cell_readback", "table_cell_styles",
                        "semantic_verification", "format_verification", "recovery_cleanup"}),
}
_NATIVE_DOCUMENT_MIME = "application/vnd.google-apps.document"

_INITIAL_ANCHOR = "نشان اولیهٔ پذیرش"
_EDITED_ANCHOR = "نشان ویرایش‌شدهٔ پذیرش"
_FINAL_ANCHOR = "نشان نهایی پذیرش"
_SENTINEL = "EXTERNAL_SENTINEL_HERMES_MCP"

_INITIAL_MARKDOWN = f"""# پذیرش زندهٔ گوگل داکس 🧪

🧪 این خط ترکیبی فارسی و English شامل {_INITIAL_ANCHOR} و **متن پررنگ** و [پیوند آزمون](https://example.com/hermes-live) است.

- [ ] مورد چک‌لیست
- مورد فهرست

| ستون | مقدار |
| --- | --- |
| **ردیف** | [داده](https://example.com/cell-initial) |
| سلول خالی |  |
| سلول padded |
"""

_FINAL_MARKDOWN = f"""# نتیجهٔ نهایی پذیرش 🧪

🧪 این خط ترکیبی فارسی و English شامل {_FINAL_ANCHOR} و **متن نهایی پررنگ** و [پیوند نهایی](https://example.com/hermes-final) است.

- [x] چک‌لیست نهایی
- گزینهٔ نهایی

| معیار | وضعیت |
| --- | --- |
| **مسیر MCP** | [تأیید شد](https://example.com/cell-final) |
| سلول خالی نهایی |  |
| سلول padded نهایی |
"""


_STAGES = frozenset({
    "live setup", "live acceptance", "docs_create", "docs_read", "docs_read initial",
    "docs_read preview", "docs_read applied", "docs_read sentinel", "docs_read final",
    "docs_edit_text", "docs_edit_text preview", "docs_edit_text apply",
    "docs_replace_markdown", "docs_replace_markdown stale", "privacy verification",
    "Persian API verification", "DOCX verification", "edit verification", "preview",
    "apply", "sentinel edit", "sentinel read", "stale guard", "replace", "final read",
    "cleanup", "cleanup MCP read", "recovery", "semantic verification",
    "docs_insert_text", "insertion preview", "insertion apply", "insertion stale", "insertion anchor",
})
_REASONS = frozenset({
    "acceptance check failed", "typed operation failed", "MCP transport failed",
    "MCP protocol error", "delete failed", "direct 404 verification failed",
    "document still exists", "ambiguous listing", "incomplete listing",
    "invalid creation time", "run document unresolved; private journal retained",
    "journal identity changed", "unexpected failure", "interrupted",
    "text readback mismatch", "revision readback mismatch", "revision unchanged",
    "invalid preview", "preview mutated document", "terminal newline missing",
    "fixture not unique", "not applied", "format not verified", "revision missing",
    "rejected operation mutated document",
})


class _CheckFailed(Exception):
    """Only allowlisted constants may cross the live test's output boundary."""

    def __init__(self, message: str):
        stage, _, detail = message.partition(": ")
        stage = stage if stage in _STAGES else "live acceptance"
        reason = detail if detail in _REASONS else "acceptance check failed"
        diagnostics = []
        for token in detail.split():
            key, _, value = token.partition("=")
            if key in _SAFE_DIAGNOSTICS and value in _SAFE_DIAGNOSTICS[key]:
                diagnostics.append(f"{key}={value}")
        super().__init__(f"{stage}: {reason}" + (" " + " ".join(diagnostics) if diagnostics else ""))
        self.transport_failed = detail in {"MCP transport failed", "MCP protocol error"}


@dataclass
class _Outcome:
    primary: str | None = None
    cleanup: str | None = None
    interrupt: BaseException | None = None

    def capture(self, error: BaseException, *, cleanup: bool = False) -> None:
        if isinstance(error, BaseExceptionGroup):
            # Keep the known check before generic transport/group failures.
            leaves = list(error.exceptions)
            leaves.sort(key=lambda item: not isinstance(item, _CheckFailed))
            for leaf in leaves:
                self.capture(leaf, cleanup=cleanup)
            return
        if not isinstance(error, Exception):
            if self.interrupt is None:
                self.interrupt = error
            message = "live acceptance: interrupted"
        else:
            message = str(error) if isinstance(error, _CheckFailed) else "live acceptance: unexpected failure"
        if cleanup:
            if self.cleanup is None:
                self.cleanup = "cleanup: " + message
        elif self.primary is None:
            self.primary = message


def _finish(outcome: _Outcome) -> None:
    messages = [value for value in (outcome.primary, outcome.cleanup) if value]
    if outcome.interrupt is not None:
        for message in messages:
            outcome.interrupt.add_note(message)
        raise outcome.interrupt.with_traceback(None) from None
    if messages:
        pytest.fail("; ".join(messages), pytrace=False)


def _fail(message: str) -> Never:
    raise _CheckFailed(message) from None


def _expected_semantic(markdown: str) -> dict[str, Any]:
    # Independent test oracle: raw parser semantics, not production profile overlay.
    expected = candidate_semantic(parse_markdown(markdown))
    for block in expected["blocks"]:
        if block["type"] != "paragraph" or block.get("heading") is None:
            continue
        runs: list[dict[str, Any]] = []
        for original in block["runs"]:
            run = {**original, "bold": True}
            if runs and runs[-1]["link"] == run["link"]:
                runs[-1]["text"] += run["text"]
            else:
                runs.append(run)
        block["runs"] = runs
    return expected


def _assert_semantic(payload: dict[str, Any], markdown: str) -> None:
    expected = _expected_semantic(markdown)
    semantic = payload.get("semantic")
    _require(isinstance(semantic, dict), "semantic verification: result missing")
    semantic = cast(dict[str, Any], semantic)
    _require(semantic.get("sha256") == semantic_sha256(expected), "semantic verification: digest mismatch")
    _require(type(semantic.get("block_count")) is int and semantic["block_count"] == len(expected["blocks"]),
             "semantic verification: block count mismatch")


@dataclass(frozen=True)
class _Journal:
    path: Path
    identity: tuple[int, int]

    def remove(self) -> None:
        current = self.path.lstat()
        _require(S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == self.identity,
                 "recovery: journal identity changed")
        self.path.unlink()


def _create_journal(title: str, started: datetime, *, directory: Path | None = None) -> _Journal:
    # mkstemp is exclusive and 0600. Never inspect or sweep the shared recovery root.
    fd, path = tempfile.mkstemp(prefix="hermes-google-docs-mcp-acceptance-", suffix=".json", dir=directory)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        metadata = os.fstat(stream.fileno())
        json.dump({"title": title, "started_at": started.isoformat()}, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    return _Journal(Path(path), (metadata.st_dev, metadata.st_ino))


def _reconcile_created(authorized: AuthorizedSession, title: str, started: datetime,
                       stopped: datetime) -> str | None:
    _require(started.tzinfo is not None and stopped.tzinfo is not None and started <= stopped,
             "recovery: invalid creation time")
    escaped_title = title.replace("\\", "\\\\").replace("'", "\\'")
    response = authorized.get(
        f"{GoogleDocsClient.DRIVE_BASE}/files",
        params={"q": f"name = '{escaped_title}' and createdTime >= '{started.isoformat()}' "
                     f"and createdTime <= '{stopped.isoformat()}' and trashed = false "
                     f"and mimeType = '{_NATIVE_DOCUMENT_MIME}' and 'me' in owners",
                "fields": "nextPageToken,files(id,name,createdTime,mimeType,ownedByMe)",
                "pageSize": 100, "spaces": "drive"},
        timeout=(20, 180),
    )
    _require(200 <= response.status_code < 300, "recovery: listing failed")
    payload = response.json()
    _require(isinstance(payload, dict), "recovery: malformed listing")
    _require(not payload.get("nextPageToken"), "recovery: incomplete listing")
    files = payload.get("files")
    _require(isinstance(files, list) and len(files) <= 100, "recovery: malformed listing")
    matches = []
    for item in files:
        _require(isinstance(item, dict), "recovery: malformed listing")
        try:
            created = datetime.fromisoformat(item["createdTime"].replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError, AttributeError):
            _fail("recovery: invalid creation time")
        _require(created.tzinfo is not None, "recovery: invalid creation time")
        document_id = _valid_document_id(item.get("id"))
        if (item.get("name") == title and started <= created <= stopped
                and item.get("ownedByMe") is True and item.get("mimeType") == _NATIVE_DOCUMENT_MIME
                and document_id is not None):
            matches.append(document_id)
    _require(len(matches) <= 1, "recovery: ambiguous listing")
    return matches[0] if matches else None


def _require(condition: bool, message: str) -> None:
    if not condition:
        _fail(message)


def _require_string(value: object, message: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(message)
    return value


def _valid_document_id(value: object) -> str | None:
    if isinstance(value, str) and _DOCUMENT_ID_RE.fullmatch(value):
        return value
    return None


def _document_id_from_create(payload: dict[str, Any]) -> str | None:
    document_id = _valid_document_id(payload.get("document_id"))
    if document_id is not None:
        return document_id
    error = payload.get("error")
    if isinstance(error, dict):
        return _valid_document_id(error.get("document_id"))
    return None


def _payload(result: Any, operation: str) -> dict[str, Any]:
    _require(result.isError is False, f"{operation}: MCP protocol error")
    payload = result.structuredContent
    _require(isinstance(payload, dict), f"{operation}: missing structured result")
    return payload


async def _call(
    session: ClientSession,
    name: str,
    arguments: dict[str, object],
) -> dict[str, Any]:
    try:
        result = await session.call_tool(name, arguments=arguments)
    except Exception:
        _fail(f"{name}: MCP transport failed")
    return _payload(result, name)


def _require_ok(payload: dict[str, Any], operation: str) -> None:
    if payload.get("ok") is not True:
        error = payload.get("error")
        error = error if isinstance(error, dict) else {}
        diagnostics = []
        for key in ("code", "failure_code", "phase"):
            value = error.get(key)
            if isinstance(value, str) and value in _SAFE_DIAGNOSTICS[key]:
                diagnostics.append(f"{key}={value}")
        suffix = " " + " ".join(diagnostics) if diagnostics else ""
        _fail(f"{operation}: typed operation failed{suffix}")
    _require(payload.get("verified") is True, f"{operation}: verification missing")


def _require_error(payload: dict[str, Any], code: str, operation: str) -> None:
    _require(payload.get("ok") is False, f"{operation}: expected typed failure")
    error = payload.get("error")
    _require(isinstance(error, dict), f"{operation}: missing typed error")
    error = cast(dict[str, Any], error)
    _require(error.get("code") == code, f"{operation}: unexpected error code")


def _paragraphs(content: object, *, include_empty: bool = True) -> Iterator[dict[str, Any]]:
    if not isinstance(content, list):
        return
    for structural_element in content:
        if not isinstance(structural_element, dict):
            continue
        paragraph = structural_element.get("paragraph")
        if isinstance(paragraph, dict) and (include_empty or _text_runs(paragraph)):
            yield paragraph
        table = structural_element.get("table")
        if isinstance(table, dict):
            rows = table.get("tableRows")
            if isinstance(rows, list):
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    cells = row.get("tableCells")
                    if not isinstance(cells, list):
                        continue
                    for cell in cells:
                        if isinstance(cell, dict):
                            yield from _paragraphs(cell.get("content"))
        table_of_contents = structural_element.get("tableOfContents")
        if isinstance(table_of_contents, dict):
            yield from _paragraphs(table_of_contents.get("content"), include_empty=include_empty)


def _text_runs(paragraph: dict[str, Any], *, include_empty: bool = False) -> list[dict[str, Any]]:
    elements = paragraph.get("elements")
    if not isinstance(elements, list):
        return []
    runs: list[dict[str, Any]] = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        text_run = element.get("textRun")
        if not isinstance(text_run, dict):
            continue
        content = text_run.get("content")
        if isinstance(content, str) and (content if include_empty else content.strip()):
            runs.append(text_run)
    return runs


def _assert_private(authorized: AuthorizedSession, document_id: str) -> None:
    status = 0
    data: object = None
    try:
        response = authorized.get(
            f"{GoogleDocsClient.DRIVE_BASE}/files/{document_id}/permissions",
            params={
                "fields": "nextPageToken,permissions(type,role,allowFileDiscovery)",
                "pageSize": 100,
                "supportsAllDrives": "false",
            },
            timeout=(20, 180),
        )
        status = response.status_code
        data = response.json() if 200 <= status < 300 else None
    except Exception:
        _fail("privacy verification: bounded Drive read failed")
    _require(200 <= status < 300, "privacy verification: Drive read failed")
    _require(isinstance(data, dict), "privacy verification: malformed result")
    data = cast(dict[str, Any], data)
    _require(not data.get("nextPageToken"), "privacy verification: incomplete listing")
    permissions = data.get("permissions")
    _require(isinstance(permissions, list), "privacy verification: no permissions")
    permissions = cast(list[object], permissions)
    _require(bool(permissions), "privacy verification: owner permission missing")
    owner_found = False
    for permission in permissions:
        _require(isinstance(permission, dict), "privacy verification: malformed permission")
        permission = cast(dict[str, Any], permission)
        permission_type = permission.get("type")
        role = permission.get("role")
        _require(
            permission_type == "user" and role == "owner",
            "privacy verification: non-private permission present",
        )
        if permission_type == "user" and role == "owner":
            owner_found = True
    _require(owner_found, "privacy verification: owner permission missing")


def _assert_api_persian_formatting(
    cleanup: GoogleDocsClient,
    document_id: str,
    tab_id: str,
    *,
    require_heading_bold: bool = True,
) -> None:
    selected: Any = None
    try:
        document = cleanup.get_document(document_id)
        selected = select_tab(document, tab_id)
    except Exception:
        _fail("Persian API verification: document read failed")
    _require(selected is not None, "Persian API verification: tab missing")
    # Ignore blank body separators, but retain every table-cell paragraph.
    paragraphs = list(_paragraphs(selected.body.get("content"), include_empty=False))
    _require(len(paragraphs) >= 7, "Persian API verification: coverage too narrow")

    heading_count = 0
    text_run_count = 0
    for paragraph in paragraphs:
        paragraph_style = paragraph.get("paragraphStyle")
        _require(
            isinstance(paragraph_style, dict),
            "Persian API verification: paragraph style missing",
        )
        paragraph_style = cast(dict[str, Any], paragraph_style)
        _require(
            paragraph_style.get("direction") == "RIGHT_TO_LEFT",
            "Persian API verification: RTL missing",
        )
        _require(
            paragraph_style.get("alignment") == "END",
            "Persian API verification: visual Right alignment missing",
        )
        for indent_name in ("indentStart", "indentEnd"):
            indent = paragraph_style.get(indent_name)
            _require(
                isinstance(indent, dict),
                "Persian API verification: independent right indent missing",
            )
            indent = cast(dict[str, Any], indent)
            _require(
                indent.get("magnitude", 0) == 0 and indent.get("unit") == "PT",
                "Persian API verification: unexpected indent",
            )

        content_runs = _text_runs(paragraph)
        runs = content_runs or _text_runs(paragraph, include_empty=True)
        _require(bool(runs), "Persian API verification: run coverage missing")
        text_run_count += len(runs)
        for text_run in runs:
            text_style = text_run.get("textStyle")
            _require(
                isinstance(text_style, dict),
                "Persian API verification: text style missing",
            )
            text_style = cast(dict[str, Any], text_style)
            font = text_style.get("weightedFontFamily")
            _require(
                isinstance(font, dict) and font.get("fontFamily") == "Vazirmatn",
                "Persian API verification: Vazirmatn missing",
            )

        named_style = paragraph_style.get("namedStyleType")
        if isinstance(named_style, str) and named_style.startswith("HEADING_"):
            heading_count += 1
            for text_run in content_runs if require_heading_bold else ():
                text_style = text_run.get("textStyle")
                _require(
                    isinstance(text_style, dict) and text_style.get("bold") is True,
                    "Persian API verification: heading bold missing",
                )

    _require(heading_count >= 1, "Persian API verification: heading missing")
    _require(text_run_count >= len(paragraphs), "Persian API verification: run coverage missing")


def _assert_docx_formatting(formatting: object, *, require_heading_bold: bool = True) -> None:
    _require(isinstance(formatting, dict), "DOCX verification: result missing")
    formatting = cast(dict[str, Any], formatting)
    if require_heading_bold:
        _require(formatting.get("valid") is True, "DOCX verification: invalid")
        _require(formatting.get("reasons") == [], "DOCX verification: reasons present")
    else:
        # Literal insertion promises paragraph direction/alignment/indent and
        # font, not Markdown's additional bold-heading publication policy.
        reasons = formatting.get("reasons")
        _require(reasons in ([], ["heading_bold_missing"]), "DOCX verification: reasons present")
        _require(formatting.get("valid") is (not reasons), "DOCX verification: invalid")
    paragraphs = formatting.get("paragraphs")
    text_runs = formatting.get("text_runs")
    heading_runs = formatting.get("heading_runs")
    _require(
        isinstance(paragraphs, int) and not isinstance(paragraphs, bool) and paragraphs > 0,
        "DOCX verification: paragraph count invalid",
    )
    _require(
        formatting.get("bidi_paragraphs") == paragraphs,
        "DOCX verification: bidi coverage incomplete",
    )
    _require(
        formatting.get("right_aligned_paragraphs") == paragraphs,
        "DOCX verification: right alignment incomplete",
    )
    _require(
        formatting.get("right_indented_paragraphs") == paragraphs,
        "DOCX verification: right indent incomplete",
    )
    _require(
        isinstance(text_runs, int) and not isinstance(text_runs, bool) and text_runs > 0,
        "DOCX verification: text run count invalid",
    )
    _require(
        formatting.get("vazirmatn_runs") == text_runs,
        "DOCX verification: Vazirmatn coverage incomplete",
    )
    _require(
        isinstance(heading_runs, int)
        and not isinstance(heading_runs, bool)
        and heading_runs > 0,
        "DOCX verification: heading coverage missing",
    )
    if require_heading_bold:
        _require(
            formatting.get("bold_heading_runs") == heading_runs,
            "DOCX verification: bold heading coverage incomplete",
        )


def _require_single_replacement(payload: dict[str, Any]) -> dict[str, Any]:
    replacements = payload.get("replacements")
    _require(
        isinstance(replacements, list) and len(replacements) == 1,
        "edit verification: replacement result missing",
    )
    replacements = cast(list[object], replacements)
    replacement = replacements[0]
    _require(isinstance(replacement, dict), "edit verification: malformed result")
    return cast(dict[str, Any], replacement)


def _assert_deleted_directly(cleanup: GoogleDocsClient, document_id: str) -> None:
    try:
        cleanup.drive_metadata(document_id)
    except DocsMCPError as error:
        _require(error.code == "document_not_found", "cleanup: unexpected direct error")
    except Exception:
        _fail("cleanup: direct 404 verification failed")
    else:
        _fail("cleanup: document still exists")


def _delete_and_verify(cleanup: GoogleDocsClient, document_id: str) -> None:
    try:
        cleanup.delete_file(document_id)
    except DocsMCPError as error:
        _require(error.code == "document_not_found", "cleanup: delete failed")
    except Exception:
        _fail("cleanup: delete failed")
    _assert_deleted_directly(cleanup, document_id)


async def _exercise_insertions(
    session: ClientSession, document_id: str, tab_id: str, initial: dict[str, Any]
) -> None:
    """Exercise literal insertions using the same run-owned temporary document."""
    text = _require_string(initial.get("content"), "insertion apply: missing text")
    revision = _require_string(initial.get("revision_id"), "insertion apply: missing revision")
    cases = (
        ("start", None, "شروع🧪\n", "persian"),
        ("end", None, "\nپایان🧪", "plain"),
        ("before", _FINAL_ANCHOR, "پیش‌متن🧪 ", "persian"),
        ("after", "تأیید شد", " تکمیل🧪", "plain"),
    )
    for position, anchor, addition, profile in cases:
        arguments = {
            "document": document_id, "tab_id": tab_id, "text": addition,
            "expected_revision_id": revision, "position": position,
            "anchor_text": anchor, "format_profile": profile, "apply": False,
        }
        preview = await _call(session, "docs_insert_text", arguments)
        if preview.get("ok") is not True:
            _require_ok(preview, "insertion preview")
        _require(preview.get("valid") is True and preview.get("applied") is False,
                 "insertion preview: invalid preview")
        read_preview = await _call(session, "docs_read", {"document": document_id, "tab_id": tab_id})
        _require_ok(read_preview, "docs_read preview")
        _require(read_preview.get("content") == text and read_preview.get("revision_id") == revision,
                 "insertion preview: preview mutated document")
        if position == "start":
            # docs_read renders the index-0 sectionBreak as a visible marker.
            # Literal insertion at API index 1 belongs after that marker.
            prefix = "⟦NON_TEXT:sectionBreak⟧\n"
            if not text.startswith(prefix):
                prefix = ""
            expected = prefix + addition + text[len(prefix):]
        elif position == "end":
            _require(text.endswith("\n"), "insertion apply: terminal newline missing")
            expected = text[:-1] + addition + "\n"
        else:
            _require(isinstance(anchor, str) and text.count(anchor) == 1,
                     "insertion anchor: fixture not unique")
            anchor = cast(str, anchor)
            replacement = addition + anchor if position == "before" else anchor + addition
            expected = text.replace(anchor, replacement)
        applied = await _call(session, "docs_insert_text", {**arguments, "apply": True})
        _require_ok(applied, "insertion apply")
        _require(applied.get("applied") is True, "insertion apply: not applied")
        if profile == "persian":
            _require(applied.get("formatting_verified") is True, "insertion apply: format not verified")
        next_revision = _require_string(applied.get("after_revision_id"), "insertion apply: revision missing")
        _require(next_revision != revision, "insertion apply: revision unchanged")
        stale = await _call(session, "docs_insert_text", {**arguments, "apply": True})
        _require_error(stale, "stale_revision", "insertion stale")
        read_applied = await _call(session, "docs_read", {"document": document_id, "tab_id": tab_id})
        _require_ok(read_applied, "docs_read applied")
        _require(read_applied.get("content") == expected, "insertion apply: text readback mismatch")
        _require(read_applied.get("revision_id") == next_revision, "insertion apply: revision readback mismatch")
        text, revision = expected, next_revision
    for anchor in ("SYNTHETIC_MISSING_INSERT_ANCHOR", "🧪"):
        rejected = await _call(session, "docs_insert_text", {
            "document": document_id, "tab_id": tab_id, "text": "should not appear",
            "expected_revision_id": revision, "position": "after", "anchor_text": anchor,
            "format_profile": "plain", "apply": True,
        })
        _require_error(rejected, "anchor_match_mismatch", "insertion anchor")
        read_rejected = await _call(session, "docs_read", {"document": document_id, "tab_id": tab_id})
        _require_ok(read_rejected, "docs_read applied")
        _require(read_rejected.get("content") == text and read_rejected.get("revision_id") == revision,
                 "insertion anchor: rejected operation mutated document")


async def _run_live_acceptance(*, include_insertions: bool = False) -> _Outcome:
    outcome = _Outcome()
    started = datetime.now(timezone.utc)
    title = "TEMP — Hermes Google Docs MCP acceptance — " + started.strftime("%Y%m%dT%H%M%S.%fZ")
    document_id: str | None = None
    journal: _Journal | None = None
    create_attempted = False
    session_alive = True
    deleted = False
    authorized: AuthorizedSession | None = None
    cleanup: GoogleDocsClient | None = None
    try:
        credentials = load_credentials()
        authorized = AuthorizedSession(credentials)
        cleanup = GoogleDocsClient(authorized)
    except BaseException as error:
        outcome.capture(error)
        if authorized is not None:
            try:
                authorized.close()
            except BaseException as close_error:
                outcome.capture(close_error, cleanup=True)
        return outcome
    authorized = cast(AuthorizedSession, authorized)
    cleanup = cast(GoogleDocsClient, cleanup)

    server = StdioServerParameters(command=str(_ROOT / "scripts/run-mcp"))
    errlog = open(os.devnull, "w")
    try:
        async with stdio_client(server, errlog=errlog) as (read, write):
            async with ClientSession(
                read,
                write,
                read_timeout_seconds=timedelta(seconds=240),  # type: ignore[arg-type]
            ) as session:
                await session.initialize()
                try:
                    journal = _create_journal(title, started)
                    create_attempted = True
                    created = await _call(
                        session,
                        "docs_create",
                        {
                            "title": title,
                            "markdown": _INITIAL_MARKDOWN,
                            "format_profile": "persian",
                        },
                    )
                    document_id = _document_id_from_create(created)
                    _require(document_id is not None, "docs_create: document ID missing")
                    document_id = cast(str, document_id)
                    _require_ok(created, "docs_create")
                    tab_id = _require_string(
                        created.get("tab_id"), "docs_create: tab ID missing"
                    )
                    _require_string(
                        created.get("revision_id"), "docs_create: revision missing"
                    )
                    _require(created.get("format_profile") == "persian", "docs_create: profile mismatch")
                    _assert_semantic(created, _INITIAL_MARKDOWN)
                    _assert_docx_formatting(created.get("formatting"))
                    _assert_private(authorized, document_id)
                    _assert_api_persian_formatting(cleanup, document_id, tab_id)

                    read_initial = await _call(
                        session,
                        "docs_read",
                        {"document": document_id, "tab_id": tab_id},
                    )
                    _require_ok(read_initial, "docs_read initial")
                    _require(read_initial.get("mime_type") == _NATIVE_DOCUMENT_MIME, "docs_read: MIME mismatch")
                    _require(read_initial.get("name") == title, "docs_read: title mismatch")
                    _require(read_initial.get("tab_id") == tab_id, "docs_read: tab mismatch")
                    initial_read_revision = _require_string(
                        read_initial.get("revision_id"), "docs_read: revision missing"
                    )
                    initial_text = _require_string(
                        read_initial.get("content"), "docs_read: text missing"
                    )
                    for anchor in (_INITIAL_ANCHOR, "English", "مورد چک‌لیست", "ستون", "داده"):
                        _require(anchor in initial_text, "docs_read: semantic anchor missing")

                    preview = await _call(
                        session,
                        "docs_edit_text",
                        {
                            "document": document_id,
                            "tab_id": tab_id,
                            "expected_revision_id": initial_read_revision,
                            "replacements": [
                                {
                                    "old_text": _INITIAL_ANCHOR,
                                    "new_text": _EDITED_ANCHOR,
                                    "expected_count": 1,
                                }
                            ],
                            "apply": False,
                        },
                    )
                    _require_ok(preview, "docs_edit_text preview")
                    preview_item = _require_single_replacement(preview)
                    _require(preview_item.get("actual_count") == 1, "preview: exact count mismatch")
                    _require(preview_item.get("expected_count") == 1, "preview: guard mismatch")

                    read_preview = await _call(session, "docs_read", {"document": document_id, "tab_id": tab_id})
                    _require_ok(read_preview, "docs_read preview")
                    _require(read_preview.get("content") == initial_text, "preview: content mutated")
                    _require(read_preview.get("revision_id") == initial_read_revision, "preview: revision mutated")

                    applied = await _call(
                        session,
                        "docs_edit_text",
                        {
                            "document": document_id,
                            "tab_id": tab_id,
                            "expected_revision_id": initial_read_revision,
                            "replacements": [
                                {
                                    "old_text": _INITIAL_ANCHOR,
                                    "new_text": _EDITED_ANCHOR,
                                    "expected_count": 1,
                                }
                            ],
                            "apply": True,
                        },
                    )
                    _require_ok(applied, "docs_edit_text apply")
                    applied_item = _require_single_replacement(applied)
                    _require(applied_item.get("occurrences_changed") == 1, "apply: occurrence count mismatch")
                    _require(applied_item.get("after_old_count") == 0, "apply: old text remains")
                    _require(applied_item.get("after_new_count") == 1, "apply: new text missing")
                    applied_revision = _require_string(
                        applied.get("after_revision_id"), "apply: revision missing"
                    )

                    read_applied = await _call(session, "docs_read", {"document": document_id, "tab_id": tab_id})
                    _require_ok(read_applied, "docs_read applied")
                    _require(read_applied.get("content") == initial_text.replace(_INITIAL_ANCHOR, _EDITED_ANCHOR),
                             "apply: independent content mismatch")
                    _require(read_applied.get("revision_id") == applied_revision, "apply: independent revision mismatch")
                    _require(applied_revision != initial_read_revision, "apply: revision unchanged")

                    sentinel_document: dict[str, Any] | None = None
                    try:
                        cleanup.batch_update(
                            document_id,
                            [
                                {
                                    "insertText": {
                                        "endOfSegmentLocation": {"tabId": tab_id},
                                        "text": "\n" + _SENTINEL,
                                    }
                                }
                            ],
                            applied_revision,
                        )
                        sentinel_document = cleanup.get_document(document_id)
                    except Exception:
                        _fail("sentinel edit: external write failed")
                    _require(
                        isinstance(sentinel_document, dict),
                        "sentinel edit: document read missing",
                    )
                    sentinel_document = cast(dict[str, Any], sentinel_document)
                    sentinel_revision = _require_string(
                        sentinel_document.get("revisionId"),
                        "sentinel edit: revision missing",
                    )
                    _require(sentinel_revision != applied_revision, "sentinel edit: revision unchanged")

                    stale = await _call(
                        session,
                        "docs_replace_markdown",
                        {
                            "document": document_id,
                            "tab_id": tab_id,
                            "expected_revision_id": applied_revision,
                            "markdown": _FINAL_MARKDOWN,
                            "format_profile": "persian",
                        },
                    )
                    _require_error(stale, "stale_revision", "docs_replace_markdown stale")

                    read_sentinel = await _call(
                        session,
                        "docs_read",
                        {"document": document_id, "tab_id": tab_id},
                    )
                    _require_ok(read_sentinel, "docs_read sentinel")
                    sentinel_text = _require_string(
                        read_sentinel.get("content"), "sentinel read: text missing"
                    )
                    _require(_SENTINEL in sentinel_text, "stale guard: sentinel overwritten")
                    _require(_EDITED_ANCHOR in sentinel_text, "stale guard: prior edit overwritten")
                    fresh_revision = _require_string(
                        read_sentinel.get("revision_id"),
                        "sentinel read: revision missing",
                    )
                    _require(fresh_revision == sentinel_revision, "sentinel read: revision mismatch")

                    replaced = await _call(
                        session,
                        "docs_replace_markdown",
                        {
                            "document": document_id,
                            "tab_id": tab_id,
                            "expected_revision_id": fresh_revision,
                            "markdown": _FINAL_MARKDOWN,
                            "format_profile": "persian",
                        },
                    )
                    _require_ok(replaced, "docs_replace_markdown")
                    _require(replaced.get("document_id") == document_id, "replace: document ID changed")
                    _require(replaced.get("tab_id") == tab_id, "replace: tab changed")
                    _require(replaced.get("before_revision_id") == fresh_revision, "replace: revision guard mismatch")
                    after_revision = _require_string(
                        replaced.get("after_revision_id"),
                        "replace: new revision missing",
                    )
                    _require(after_revision != fresh_revision, "replace: revision unchanged")

                    _assert_semantic(replaced, _FINAL_MARKDOWN)
                    _assert_docx_formatting(replaced.get("formatting"))

                    final_read = await _call(
                        session,
                        "docs_read",
                        {"document": document_id, "tab_id": tab_id},
                    )
                    _require_ok(final_read, "docs_read final")
                    _require(final_read.get("revision_id") == after_revision, "final read: revision mismatch")
                    final_text = _require_string(
                        final_read.get("content"), "final read: text missing"
                    )
                    for anchor in (_FINAL_ANCHOR, "English", "چک‌لیست نهایی", "معیار", "تأیید شد"):
                        _require(anchor in final_text, "final read: semantic anchor missing")
                    _require(_SENTINEL not in final_text, "final read: sentinel not replaced")
                    _require(_EDITED_ANCHOR not in final_text, "final read: old content remains")
                    if include_insertions:
                        await _exercise_insertions(session, document_id, tab_id, final_read)
                        _assert_docx_formatting(verify_persian_docx(cleanup.export_file(
                            document_id,
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        )), require_heading_bold=False)
                    formatting_options = {"require_heading_bold": False} if include_insertions else {}
                    _assert_api_persian_formatting(cleanup, document_id, tab_id, **formatting_options)
                except BaseException as error:
                    outcome.capture(error)
                    session_alive = isinstance(error, _CheckFailed) and not error.transport_failed
                finally:
                    if document_id is not None and session_alive:
                        try:
                            _delete_and_verify(cleanup, document_id)
                            deleted = True
                            deleted_read = await _call(session, "docs_read", {"document": document_id})
                            _require_error(deleted_read, "document_not_found", "cleanup MCP read")
                        except BaseException as error:
                            outcome.capture(error, cleanup=True)
    except BaseException as error:
        outcome.capture(error)
    finally:
        # The SDK process/transport has closed before unknown-result reconciliation.
        try:
            if create_attempted and not deleted:
                if document_id is None:
                    document_id = _reconcile_created(authorized, title, started, datetime.now(timezone.utc))
                _require(document_id is not None, "recovery: run document unresolved; private journal retained")
                _delete_and_verify(cleanup, cast(str, document_id))
                deleted = True
        except BaseException as error:
            outcome.capture(error, cleanup=True)
        if journal is not None and deleted:
            try:
                journal.remove()
            except BaseException as error:
                outcome.capture(error, cleanup=True)
        try:
            authorized.close()
        except BaseException as error:
            outcome.capture(error, cleanup=True)
        errlog.close()
    return outcome


@pytest.mark.skipif(
    not _LIVE_ENABLED,
    reason="set RUN_GOOGLE_DOCS_MCP_LIVE=1 for destructive Google acceptance",
)
def test_live_google_docs_end_to_end_through_mcp(capfd, caplog) -> None:
    outcome = _Outcome()
    try:
        outcome = asyncio.run(_run_live_acceptance())
    except BaseException as error:
        outcome.capture(error)
    # Never publish captured child/provider output, even when the test fails.
    capfd.readouterr()
    caplog.clear()
    _finish(outcome)


@pytest.mark.skipif(
    not _LIVE_ENABLED,
    reason="set RUN_GOOGLE_DOCS_MCP_LIVE=1 for destructive Google insertion acceptance",
)
def test_live_google_insertions_through_mcp(capfd, caplog) -> None:
    outcome = _Outcome()
    try:
        outcome = asyncio.run(_run_live_acceptance(include_insertions=True))
    except BaseException as error:
        outcome.capture(error)
    capfd.readouterr()
    caplog.clear()
    _finish(outcome)
