import io
import os
import re
import secrets
import stat
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
from defusedxml import ElementTree as DefusedElementTree
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials


DOC_ID_RE = re.compile(r"[A-Za-z0-9_-]{10,256}")
_DOCUMENT_PATH_PREFIX = "/document/d/"
_RECOVERY_TEXT_MIME = "text/plain"
_RECOVERY_DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_RECOVERY_PREFIX = "recovery-"
_RECOVERY_NAME_RE = re.compile(
    r"recovery-[0-9]{8}T[0-9]{6}\.[0-9]{6}Z-[0-9a-f]{16}"
)
_RECOVERY_FILENAMES = ("document.txt", "document.docx")
_RECOVERY_CREATE_ATTEMPTS = 8
_DOCX_DOCUMENT_MEMBER = "word/document.xml"
_DOCX_STYLES_MEMBER = "word/styles.xml"
_MAX_DOCX_BYTES = 32_000_000
_MAX_DOCX_MEMBERS = 4_096
_MAX_DOCX_XML_BYTES = 8_000_000
_MAX_DOCX_XML_NODES = 100_000
_MAX_DOCX_STYLE_DEPTH = 64
_WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_DOCX_REASON_ORDER = (
    "paragraph_bidi_missing",
    "paragraph_right_alignment_missing",
    "paragraph_right_indent_missing",
    "run_vazirmatn_missing",
    "heading_bold_missing",
)


class DocsMCPError(RuntimeError):
    def __init__(
        self, code: str, message: str, *, retryable: bool = False
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = retryable

    def as_result(self) -> dict[str, object]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
            },
        }


def _credential_storage_failed() -> DocsMCPError:
    return DocsMCPError(
        "credential_storage_failed",
        "Google credential storage could not be updated.",
    )


def _google_needs_reauth() -> DocsMCPError:
    return DocsMCPError(
        "google_needs_reauth",
        "Google authorization is unavailable. Reconnect Google Docs.",
    )


def default_token_path() -> Path:
    return Path.home() / ".hermes/google_token.json"


def atomic_write_private(path: Path, data: str) -> None:
    path = Path(path)
    temporary_path: Path | None = None
    temporary_identity: tuple[int, int] | None = None
    fd: int | None = None
    replaced = False
    storage_failed = False

    try:
        if not isinstance(data, str):
            raise TypeError
        payload = data.encode("utf-8")

        required_flag_names = (
            "O_WRONLY",
            "O_CREAT",
            "O_EXCL",
            "O_CLOEXEC",
            "O_NOFOLLOW",
        )
        required_flags = 0
        for flag_name in required_flag_names:
            flag = getattr(os, flag_name, 0)
            if not isinstance(flag, int) or isinstance(flag, bool) or flag == 0:
                raise OSError
            required_flags |= flag

        parent_metadata = os.lstat(path.parent)
        if not stat.S_ISDIR(parent_metadata.st_mode):
            raise OSError

        try:
            target_metadata = os.lstat(path)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(target_metadata.st_mode):
                raise OSError

        temporary_path = path.parent / (
            f".{path.name}.{secrets.token_hex(16)}.tmp"
        )
        fd = os.open(temporary_path, required_flags, 0o600)
        opened_metadata = os.fstat(fd)
        if not stat.S_ISREG(opened_metadata.st_mode):
            raise OSError
        temporary_identity = (opened_metadata.st_dev, opened_metadata.st_ino)

        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written <= 0:
                raise OSError
            offset += written
        os.fsync(fd)

        descriptor_to_close = fd
        fd = None
        os.close(descriptor_to_close)

        os.replace(temporary_path, path)
        replaced = True
        os.chmod(path, 0o600)
    except Exception:
        storage_failed = True
    finally:
        if fd is not None:
            descriptor_to_close = fd
            fd = None
            try:
                os.close(descriptor_to_close)
            except OSError:
                pass

        if temporary_path is not None and not replaced:
            try:
                visible_metadata = os.lstat(temporary_path)
            except OSError:
                pass
            else:
                visible_identity = (
                    visible_metadata.st_dev,
                    visible_metadata.st_ino,
                )
                if (
                    temporary_identity is not None
                    and visible_identity == temporary_identity
                    and stat.S_ISREG(visible_metadata.st_mode)
                ):
                    try:
                        os.unlink(temporary_path)
                    except OSError:
                        pass

    if storage_failed:
        raise _credential_storage_failed()


def load_credentials(path: Path | None = None) -> Credentials:
    token_path: Path | None = None
    preflight_failed = False
    try:
        token_path = default_token_path() if path is None else Path(path)
        token_metadata = os.lstat(token_path)
        if not stat.S_ISREG(token_metadata.st_mode):
            raise OSError

        if os.name == "posix":
            getuid = getattr(os, "getuid", None)
            if getuid is None or token_metadata.st_uid != getuid():
                raise OSError

        os.chmod(token_path, 0o600)
    except Exception:
        preflight_failed = True

    if preflight_failed or token_path is None:
        raise _google_needs_reauth()

    refreshed_json: str | None = None
    credentials: Credentials | None = None
    credential_processing_failed = False
    try:
        credentials = Credentials.from_authorized_user_file(str(token_path))
        if credentials.expired:
            if not credentials.refresh_token:
                raise ValueError
            credentials.refresh(Request())
            if not credentials.valid:
                raise ValueError
            refreshed_json = credentials.to_json()
            if not isinstance(refreshed_json, str):
                raise TypeError
        elif not credentials.valid:
            raise ValueError
    except Exception:
        credential_processing_failed = True

    if credential_processing_failed or credentials is None:
        raise _google_needs_reauth()

    if refreshed_json is not None:
        atomic_write_private(token_path, refreshed_json)
    return credentials


def _invalid_document_reference() -> DocsMCPError:
    return DocsMCPError(
        "invalid_document_reference",
        "Document reference must be a canonical Google Docs URL or valid ID.",
    )


def parse_document_id(value: object) -> str:
    if not isinstance(value, str):
        raise _invalid_document_reference()
    if any(character.isspace() or not character.isprintable() for character in value):
        raise _invalid_document_reference()
    if DOC_ID_RE.fullmatch(value):
        return value

    parsed = None
    parse_failed = False
    try:
        parsed = urlsplit(value)
    except (UnicodeError, ValueError):
        parse_failed = True

    if parse_failed or parsed is None:
        raise _invalid_document_reference()

    if parsed.scheme != "https" or parsed.netloc != "docs.google.com":
        raise _invalid_document_reference()
    if not parsed.path.startswith(_DOCUMENT_PATH_PREFIX):
        raise _invalid_document_reference()
    if "%" in parsed.path or "\\" in parsed.path:
        raise _invalid_document_reference()
    if any(segment in {".", ".."} for segment in parsed.path.split("/")):
        raise _invalid_document_reference()

    remainder = parsed.path[len(_DOCUMENT_PATH_PREFIX) :]
    document_id, separator, _suffix = remainder.partition("/")
    if not DOC_ID_RE.fullmatch(document_id):
        raise _invalid_document_reference()
    if separator not in {"", "/"}:
        raise _invalid_document_reference()
    return document_id


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def utf16_index(text: str, codepoint_offset: object) -> int:
    if (
        not isinstance(codepoint_offset, int)
        or isinstance(codepoint_offset, bool)
        or not 0 <= codepoint_offset <= len(text)
    ):
        raise DocsMCPError(
            "invalid_utf16_offset",
            "Codepoint offset must be an integer within the text bounds.",
        )
    return utf16_length(text[:codepoint_offset])


def validate_max_chars(value: object) -> int:
    if value is None:
        return 30_000
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= 100_000
    ):
        raise DocsMCPError(
            "invalid_max_chars",
            "max_chars must be an integer from 1 through 100000.",
        )
    return value


def validate_title(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value) > 200
    ):
        raise DocsMCPError(
            "invalid_title",
            "Title must be a non-blank string of at most 200 characters without NUL.",
        )
    return value


def validate_markdown(value: object) -> str:
    if not isinstance(value, str) or "\x00" in value or len(value) > 500_000:
        raise DocsMCPError(
            "invalid_markdown",
            "Markdown must be a string of at most 500000 characters without NUL.",
        )
    return value


def validate_replacement_count(value: object) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= 100
    ):
        raise DocsMCPError(
            "invalid_replacement_count",
            "Replacement count must be an integer from 1 through 100.",
        )
    return value


def validate_old_text(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise DocsMCPError(
            "invalid_old_text",
            "old_text must be a non-empty string without NUL.",
        )
    return value


def validate_new_text(value: object) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise DocsMCPError(
            "invalid_new_text",
            "new_text must be a string without NUL.",
        )
    return value


def _permission_denied() -> DocsMCPError:
    return DocsMCPError(
        "permission_denied",
        "Google Docs permission was denied.",
    )


def _document_not_found() -> DocsMCPError:
    return DocsMCPError(
        "document_not_found",
        "The requested Google document was not found.",
    )


def _rate_limited() -> DocsMCPError:
    return DocsMCPError(
        "rate_limited",
        "Google request rate limit was reached. Try again later.",
        retryable=True,
    )


def _google_unavailable(*, retryable: bool = False) -> DocsMCPError:
    return DocsMCPError(
        "google_unavailable",
        "Google services are unavailable.",
        retryable=retryable,
    )


@dataclass(frozen=True)
class TabView:
    tab_id: str
    title: str
    parent_tab_id: str | None
    body: dict


@dataclass
class _RenderBuffer:
    parts: list[str]
    length: int = 0


_MAX_TAB_NODES = 100_000
_MAX_RENDER_NODES = 100_000
_MAX_RENDER_CHARS = 5_000_000
_HEADING_LEVELS = {f"HEADING_{level}": level for level in range(1, 7)}
_KNOWN_NON_TEXT_KINDS = (
    "inlineObjectElement",
    "footnoteReference",
    "horizontalRule",
    "pageBreak",
    "columnBreak",
    "equation",
    "autoText",
    "richLink",
    "person",
    "rubricChip",
    "sectionBreak",
)


def _tab_not_found() -> DocsMCPError:
    return DocsMCPError(
        "tab_not_found",
        "The requested document tab was not found.",
    )


def _flatten_tabs(tabs: list[dict]) -> list[TabView]:
    if not isinstance(tabs, list):
        raise TypeError

    flattened: list[TabView] = []
    stack: list[tuple] = [("tab_sequence", tabs, 0)]
    visited = 0
    while stack:
        event = stack.pop()
        event_type = event[0]
        if event_type == "tab_sequence":
            sequence, index = event[1], event[2]
            if index < len(sequence):
                stack.append(("tab_sequence", sequence, index + 1))
                stack.append(("enter_tab", sequence[index]))
            continue

        tab = event[1]
        visited += 1
        if visited > _MAX_TAB_NODES:
            raise _google_unavailable()
        if not isinstance(tab, dict):
            raise TypeError

        properties = tab.get("tabProperties")
        document_tab = tab.get("documentTab")
        if not isinstance(properties, dict) or not isinstance(document_tab, dict):
            raise TypeError

        tab_id = properties.get("tabId")
        title = properties.get("title")
        parent_tab_id = properties.get("parentTabId")
        body = document_tab.get("body")
        if (
            not isinstance(tab_id, str)
            or not isinstance(title, str)
            or (parent_tab_id is not None and not isinstance(parent_tab_id, str))
            or not isinstance(body, dict)
        ):
            raise TypeError

        flattened.append(TabView(tab_id, title, parent_tab_id, body))
        child_tabs = tab.get("childTabs", [])
        if not isinstance(child_tabs, list):
            raise TypeError
        stack.append(("tab_sequence", child_tabs, 0))
    return flattened


def flatten_tabs(tabs: list[dict]) -> list[TabView]:
    result: list[TabView] | None = None
    failed = False
    try:
        result = _flatten_tabs(tabs)
    except DocsMCPError:
        raise
    except Exception:
        failed = True

    if failed or result is None:
        raise _google_unavailable()
    return result


def _select_tab(document: dict, tab_id: str | None) -> TabView | None:
    if not isinstance(document, dict):
        raise TypeError

    raw_tabs = document.get("tabs")
    if raw_tabs is None:
        body = document.get("body")
        if body is None:
            if tab_id is not None:
                raise _tab_not_found()
            return None
        if not isinstance(body, dict):
            raise TypeError
        if tab_id is not None:
            raise _tab_not_found()
        title = document.get("title", "")
        if not isinstance(title, str):
            raise TypeError
        return TabView("", title, None, body)

    if not isinstance(raw_tabs, list):
        raise TypeError
    tabs = flatten_tabs(raw_tabs)
    if tab_id is None:
        return tabs[0] if len(tabs) == 1 else None
    for tab in tabs:
        if tab.tab_id == tab_id:
            return tab
    raise _tab_not_found()


def select_tab(document: dict, tab_id: str | None) -> TabView | None:
    result: TabView | None = None
    failed = False
    try:
        result = _select_tab(document, tab_id)
    except DocsMCPError:
        raise
    except Exception:
        failed = True

    if failed:
        raise _google_unavailable()
    return result


def _render_marker(node: dict) -> str:
    for kind in _KNOWN_NON_TEXT_KINDS:
        if kind in node:
            return f"⟦NON_TEXT:{kind}⟧"
    return "⟦NON_TEXT:unknown⟧"


def _append_render(
    buffer: _RenderBuffer,
    text: str,
    active_chars: list[int],
) -> None:
    if not text:
        return
    next_length = active_chars[0] + len(text)
    if next_length > _MAX_RENDER_CHARS:
        raise _google_unavailable()
    buffer.parts.append(text)
    buffer.length += len(text)
    active_chars[0] = next_length


def _take_render(buffer: _RenderBuffer, active_chars: list[int]) -> str:
    text = "".join(buffer.parts)
    active_chars[0] -= buffer.length
    buffer.parts.clear()
    buffer.length = 0
    return text


def _buffer_endswith_newline(buffer: _RenderBuffer) -> bool:
    for part in reversed(buffer.parts):
        if part:
            return part.endswith("\n")
    return False


def _render_table(
    rows: list[list[_RenderBuffer]],
    target: _RenderBuffer,
    active_chars: list[int],
) -> None:
    width = max((len(row) for row in rows), default=0)
    if width == 0:
        return

    prefix = ""
    if target.length and not _buffer_endswith_newline(target):
        prefix = "\n"

    cell_buffer_chars = 0
    trimmed_newlines = 0
    for row in rows:
        for cell in row:
            cell_buffer_chars += cell.length
            if cell.length and _buffer_endswith_newline(cell):
                trimmed_newlines += 1

    available_chars = min(
        _MAX_RENDER_CHARS,
        _MAX_RENDER_CHARS - (active_chars[0] - cell_buffer_chars),
    )
    minimum_length = (
        len(prefix)
        + (len(rows) * ((3 * width) + 2))
        + (6 * width)
        + 2
        + cell_buffer_chars
        - trimmed_newlines
    )
    if minimum_length > available_chars:
        raise _google_unavailable()

    lines: list[str] = []
    rendered_length = 0

    def append_line(cells: list[str]) -> None:
        nonlocal rendered_length
        line_length = 4 + sum(len(cell) for cell in cells) + (3 * (len(cells) - 1))
        next_length = rendered_length + line_length + 1
        if len(prefix) + next_length > available_chars:
            raise _google_unavailable()
        rendered_length = next_length
        lines.append("| " + " | ".join(cells) + " |")

    for index, row in enumerate(rows):
        normalized: list[str] = []
        for cell in row:
            text = _take_render(cell, active_chars)
            if text.endswith("\n"):
                text = text[:-1]
            expanded_length = len(text) + text.count("|") + (3 * text.count("\n"))
            if expanded_length > _MAX_RENDER_CHARS:
                raise _google_unavailable()
            normalized.append(text.replace("|", "\\|").replace("\n", "<br>"))
        normalized.extend([""] * (width - len(normalized)))
        append_line(normalized)
        if index == 0:
            append_line(["---"] * width)

    table_text = prefix + "\n".join(lines) + "\n"
    _append_render(target, table_text, active_chars)


def _render_body(body: dict) -> tuple[str, list[dict]]:
    root = _RenderBuffer([])
    outline: list[dict] = []
    active_chars = [0]
    visited = 0
    stack: list[tuple] = [("enter_body", body, root)]

    while stack:
        event = stack.pop()
        event_type = event[0]
        if event_type.startswith("enter_"):
            visited += 1
            if visited > _MAX_RENDER_NODES:
                raise _google_unavailable()

        if event_type == "structural_sequence":
            sequence, index, target = event[1], event[2], event[3]
            if index < len(sequence):
                stack.append(("structural_sequence", sequence, index + 1, target))
                stack.append(("enter_structural", sequence[index], target))

        elif event_type == "paragraph_element_sequence":
            sequence, index, target = event[1], event[2], event[3]
            if index < len(sequence):
                stack.append(
                    ("paragraph_element_sequence", sequence, index + 1, target)
                )
                stack.append(("enter_paragraph_element", sequence[index], target))

        elif event_type == "table_row_sequence":
            sequence, index, rendered_rows = event[1], event[2], event[3]
            if index < len(sequence):
                stack.append(
                    ("table_row_sequence", sequence, index + 1, rendered_rows)
                )
                stack.append(("enter_table_row", sequence[index], rendered_rows))

        elif event_type == "table_cell_sequence":
            sequence, index, rendered_cells = event[1], event[2], event[3]
            if index < len(sequence):
                stack.append(
                    ("table_cell_sequence", sequence, index + 1, rendered_cells)
                )
                stack.append(("enter_table_cell", sequence[index], rendered_cells))

        elif event_type == "enter_body":
            container, target = event[1], event[2]
            if not isinstance(container, dict):
                raise TypeError
            content = container.get("content", [])
            if not isinstance(content, list):
                raise TypeError
            stack.append(("exit_body",))
            stack.append(("structural_sequence", content, 0, target))

        elif event_type == "enter_structural":
            element, target = event[1], event[2]
            if not isinstance(element, dict):
                raise TypeError

            if "paragraph" in element:
                paragraph = element["paragraph"]
                if not isinstance(paragraph, dict):
                    raise TypeError
                elements = paragraph.get("elements", [])
                style = paragraph.get("paragraphStyle", {})
                if not isinstance(elements, list) or not isinstance(style, dict):
                    raise TypeError
                paragraph_buffer = _RenderBuffer([])
                stack.append(
                    (
                        "exit_paragraph",
                        paragraph_buffer,
                        target,
                        style.get("namedStyleType"),
                    )
                )
                stack.append(
                    ("paragraph_element_sequence", elements, 0, paragraph_buffer)
                )
            elif "table" in element:
                table = element["table"]
                if not isinstance(table, dict):
                    raise TypeError
                table_rows = table.get("tableRows", [])
                if not isinstance(table_rows, list):
                    raise TypeError
                rendered_rows: list[list[_RenderBuffer]] = []
                stack.append(("exit_table", rendered_rows, target))
                stack.append(("table_row_sequence", table_rows, 0, rendered_rows))
            elif "tableOfContents" in element:
                table_of_contents = element["tableOfContents"]
                if not isinstance(table_of_contents, dict):
                    raise TypeError
                content = table_of_contents.get("content", [])
                if not isinstance(content, list):
                    raise TypeError
                stack.append(("exit_table_of_contents",))
                stack.append(("structural_sequence", content, 0, target))
            else:
                _append_render(target, _render_marker(element) + "\n", active_chars)

        elif event_type == "enter_paragraph_element":
            element, target = event[1], event[2]
            if not isinstance(element, dict):
                raise TypeError
            if "textRun" in element:
                text_run = element["textRun"]
                if not isinstance(text_run, dict):
                    raise TypeError
                content = text_run.get("content")
                if not isinstance(content, str):
                    raise TypeError
                _append_render(target, content, active_chars)
            else:
                _append_render(target, _render_marker(element), active_chars)

        elif event_type == "exit_paragraph":
            paragraph_buffer, target, style = event[1], event[2], event[3]
            text = _take_render(paragraph_buffer, active_chars)
            outline_text = text[:-1] if text.endswith("\n") else text
            level = _HEADING_LEVELS.get(style)
            if level is not None:
                outline.append({"level": level, "text": outline_text})
            if not text.endswith("\n"):
                text += "\n"
            _append_render(target, text, active_chars)

        elif event_type == "enter_table_row":
            row, rendered_rows = event[1], event[2]
            if not isinstance(row, dict):
                raise TypeError
            cells = row.get("tableCells", [])
            if not isinstance(cells, list):
                raise TypeError
            rendered_cells: list[_RenderBuffer] = []
            rendered_rows.append(rendered_cells)
            stack.append(("exit_table_row",))
            stack.append(("table_cell_sequence", cells, 0, rendered_cells))

        elif event_type == "enter_table_cell":
            cell, rendered_cells = event[1], event[2]
            if not isinstance(cell, dict):
                raise TypeError
            content = cell.get("content", [])
            if not isinstance(content, list):
                raise TypeError
            cell_buffer = _RenderBuffer([])
            rendered_cells.append(cell_buffer)
            stack.append(("exit_table_cell",))
            stack.append(("structural_sequence", content, 0, cell_buffer))

        elif event_type == "exit_table":
            _render_table(event[1], event[2], active_chars)

    return _take_render(root, active_chars), outline


def render_body(body: dict) -> tuple[str, list[dict]]:
    result: tuple[str, list[dict]] | None = None
    failed = False
    try:
        result = _render_body(body)
    except DocsMCPError:
        raise
    except Exception:
        failed = True

    if failed or result is None:
        raise _google_unavailable()
    return result


def paginate(text: str, start: int, max_chars: int) -> dict:
    if not isinstance(text, str):
        raise DocsMCPError("invalid_text", "text must be a string.")
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not 0 <= start <= len(text)
    ):
        raise DocsMCPError(
            "invalid_start",
            "start must be an integer within the text bounds.",
        )
    page_size = validate_max_chars(max_chars)
    total_chars = len(text)
    end = min(start + page_size, total_chars)
    return {
        "content": text[start:end],
        "start": start,
        "end": end,
        "total_chars": total_chars,
        "next_start": end if end < total_chars else None,
    }


@dataclass(frozen=True)
class RecoveryBackup:
    path: Path
    text_path: Path
    docx_path: Path


def _recovery_unavailable() -> DocsMCPError:
    return DocsMCPError(
        "google_unavailable",
        "A private recovery backup could not be prepared.",
    )


def _prepare_recovery_root(root: Path, *, create: bool) -> Path:
    root = Path(root)
    try:
        metadata = os.lstat(root)
    except FileNotFoundError:
        if not create:
            raise
        parent_metadata = os.lstat(root.parent)
        if not stat.S_ISDIR(parent_metadata.st_mode):
            raise OSError
        os.mkdir(root, 0o700)
        metadata = os.lstat(root)
    if not stat.S_ISDIR(metadata.st_mode):
        raise OSError
    if os.name == "posix":
        getuid = getattr(os, "getuid", None)
        if getuid is None or metadata.st_uid != getuid():
            raise OSError
    identity = (metadata.st_dev, metadata.st_ino)
    os.chmod(root, 0o700)
    final_metadata = os.lstat(root)
    if (
        not stat.S_ISDIR(final_metadata.st_mode)
        or (final_metadata.st_dev, final_metadata.st_ino) != identity
        or stat.S_IMODE(final_metadata.st_mode) != 0o700
    ):
        raise OSError
    return root


def _write_recovery_bytes(path: Path, payload: bytes) -> None:
    required_flags = 0
    for name in ("O_WRONLY", "O_CREAT", "O_EXCL", "O_CLOEXEC", "O_NOFOLLOW"):
        flag = getattr(os, name, 0)
        if not isinstance(flag, int) or isinstance(flag, bool) or flag == 0:
            raise OSError
        required_flags |= flag

    fd = os.open(path, required_flags, 0o600)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError
        os.fchmod(fd, 0o600)
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written <= 0:
                raise OSError
            offset += written
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_recovery_directory(path: Path) -> None:
    read_only = getattr(os, "O_RDONLY", None)
    if not isinstance(read_only, int) or isinstance(read_only, bool):
        raise OSError
    required_flags = read_only
    for name in ("O_DIRECTORY", "O_CLOEXEC", "O_NOFOLLOW"):
        flag = getattr(os, name, 0)
        if not isinstance(flag, int) or isinstance(flag, bool) or flag == 0:
            raise OSError
        required_flags |= flag
    fd = os.open(path, required_flags)
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError
        os.fsync(fd)
    finally:
        os.close(fd)


def _recovery_backup_is_usable(backup: object) -> bool:
    try:
        if not isinstance(backup, RecoveryBackup):
            return False
        paths = (backup.path, backup.text_path, backup.docx_path)
        if not all(isinstance(path, Path) for path in paths):
            return False
        if (
            backup.text_path != backup.path / _RECOVERY_FILENAMES[0]
            or backup.docx_path != backup.path / _RECOVERY_FILENAMES[1]
            or _RECOVERY_NAME_RE.fullmatch(backup.path.name) is None
        ):
            return False

        root_metadata = os.lstat(backup.path.parent)
        backup_metadata = os.lstat(backup.path)
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or stat.S_IMODE(root_metadata.st_mode) != 0o700
            or not stat.S_ISDIR(backup_metadata.st_mode)
            or stat.S_IMODE(backup_metadata.st_mode) != 0o700
        ):
            return False

        owner: int | None = None
        if os.name == "posix":
            getuid = getattr(os, "getuid", None)
            if getuid is None:
                return False
            owner = getuid()
            if root_metadata.st_uid != owner or backup_metadata.st_uid != owner:
                return False

        entries = {entry.name for entry in os.scandir(backup.path)}
        if entries != set(_RECOVERY_FILENAMES):
            return False
        for child in (backup.text_path, backup.docx_path):
            child_metadata = os.lstat(child)
            if (
                not stat.S_ISREG(child_metadata.st_mode)
                or stat.S_IMODE(child_metadata.st_mode) != 0o600
                or child_metadata.st_nlink != 1
                or (owner is not None and child_metadata.st_uid != owner)
            ):
                return False
    except Exception:
        return False
    return True


def _remove_recovery_directory(path: Path, *, allow_partial: bool) -> bool:
    try:
        metadata = os.lstat(path)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or _RECOVERY_NAME_RE.fullmatch(path.name) is None
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            return False
        owner: int | None = None
        if os.name == "posix":
            getuid = getattr(os, "getuid", None)
            if getuid is None:
                return False
            owner = getuid()
            if metadata.st_uid != owner:
                return False
        entries = {entry.name for entry in os.scandir(path)}
        expected = set(_RECOVERY_FILENAMES)
        if not entries <= expected or (not allow_partial and entries != expected):
            return False
        for name in sorted(entries):
            child = path / name
            child_metadata = os.lstat(child)
            if (
                not stat.S_ISREG(child_metadata.st_mode)
                or stat.S_IMODE(child_metadata.st_mode) != 0o600
                or child_metadata.st_nlink != 1
                or (owner is not None and child_metadata.st_uid != owner)
            ):
                return False
        for name in sorted(entries):
            os.unlink(path / name)
        os.rmdir(path)
    except OSError:
        return False
    return True


def make_recovery_backup(
    client: Any, document_id: str, root: Path
) -> RecoveryBackup:
    root_failed = False
    try:
        root_path = _prepare_recovery_root(root, create=True)
    except Exception:
        root_failed = True
        root_path = Path(root)
    if root_failed:
        raise _recovery_unavailable()

    export_failed = False
    text_payload: object = None
    docx_payload: object = None
    try:
        text_payload = client.export_file(document_id, _RECOVERY_TEXT_MIME)
        docx_payload = client.export_file(document_id, _RECOVERY_DOCX_MIME)
    except DocsMCPError:
        raise
    except Exception:
        export_failed = True
    if (
        export_failed
        or not isinstance(text_payload, bytes)
        or not isinstance(docx_payload, bytes)
    ):
        raise _recovery_unavailable()

    backup_path: Path | None = None
    backup: RecoveryBackup | None = None
    completed = False
    storage_failed = False
    try:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        for _ in range(_RECOVERY_CREATE_ATTEMPTS):
            candidate = root_path / (
                f"{_RECOVERY_PREFIX}{timestamp}-{secrets.token_hex(8)}"
            )
            try:
                os.mkdir(candidate, 0o700)
            except FileExistsError:
                continue
            backup_path = candidate
            break
        if backup_path is None:
            raise OSError
        os.chmod(backup_path, 0o700)
        backup = RecoveryBackup(
            path=backup_path,
            text_path=backup_path / _RECOVERY_FILENAMES[0],
            docx_path=backup_path / _RECOVERY_FILENAMES[1],
        )
        _write_recovery_bytes(backup.text_path, text_payload)
        _write_recovery_bytes(backup.docx_path, docx_payload)
        _fsync_recovery_directory(backup.path)
        _fsync_recovery_directory(root_path)
        _fsync_recovery_directory(root_path.parent)
        if (
            stat.S_IMODE(os.lstat(backup.path).st_mode) != 0o700
            or stat.S_IMODE(os.lstat(backup.text_path).st_mode) != 0o600
            or stat.S_IMODE(os.lstat(backup.docx_path).st_mode) != 0o600
        ):
            raise OSError
        completed = True
    except Exception:
        storage_failed = True
    finally:
        if backup_path is not None and not completed:
            _remove_recovery_directory(backup_path, allow_partial=True)
    if storage_failed or backup is None:
        raise _recovery_unavailable()
    return backup


def purge_old_recovery(root: Path, now: datetime) -> list[Path]:
    if (
        not isinstance(now, datetime)
        or now.tzinfo is None
        or now.utcoffset() is None
    ):
        raise ValueError("now must be a timezone-aware datetime.")
    root = Path(root)
    try:
        os.lstat(root)
    except FileNotFoundError:
        return []

    root_failed = False
    try:
        root = _prepare_recovery_root(root, create=False)
        entries = sorted(os.scandir(root), key=lambda entry: entry.name)
    except Exception:
        root_failed = True
        entries = []
    if root_failed:
        raise _recovery_unavailable()

    cutoff = (now.astimezone(timezone.utc) - timedelta(days=7)).timestamp()
    removed: list[Path] = []
    for entry in entries:
        try:
            metadata = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if (
            not entry.name.startswith(_RECOVERY_PREFIX)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_mtime >= cutoff
        ):
            continue
        path = root / entry.name
        if _remove_recovery_directory(path, allow_partial=True):
            removed.append(path)
    return removed


@dataclass(frozen=True)
class _DocxStyle:
    style_id: str
    style_type: str
    name: str
    based_on: str | None
    paragraph_properties: Any | None
    run_properties: Any | None


def _word(name: str) -> str:
    return f"{{{_WORD_NAMESPACE}}}{name}"


def _word_attribute(element: Any, name: str) -> str | None:
    value = element.get(_word(name))
    if value is not None and not isinstance(value, str):
        raise TypeError
    return value


def _docx_verification_error() -> DocsMCPError:
    return DocsMCPError(
        "verification_failed",
        "Persian DOCX formatting could not be verified.",
    )


def _read_docx_member(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo
) -> bytes:
    if (
        info.is_dir()
        or info.flag_bits & 0x1
        or not isinstance(info.file_size, int)
        or info.file_size < 0
        or info.file_size > _MAX_DOCX_XML_BYTES
    ):
        raise ValueError
    with archive.open(info, "r") as stream:
        payload = stream.read(_MAX_DOCX_XML_BYTES + 1)
    if len(payload) > _MAX_DOCX_XML_BYTES or len(payload) != info.file_size:
        raise ValueError
    return payload


def _read_docx_parts(data: bytes) -> tuple[bytes, bytes]:
    if type(data) is not bytes or not data or len(data) > _MAX_DOCX_BYTES:
        raise TypeError
    with zipfile.ZipFile(io.BytesIO(data), "r") as archive:
        infos = archive.infolist()
        if not infos or len(infos) > _MAX_DOCX_MEMBERS:
            raise ValueError
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise ValueError
        members = {info.filename: info for info in infos}
        if set((_DOCX_DOCUMENT_MEMBER, _DOCX_STYLES_MEMBER)) - set(members):
            raise ValueError
        document_xml = _read_docx_member(
            archive, members[_DOCX_DOCUMENT_MEMBER]
        )
        styles_xml = _read_docx_member(archive, members[_DOCX_STYLES_MEMBER])
    return document_xml, styles_xml


def _parse_docx_xml(payload: bytes, expected_root: str) -> Any:
    root = DefusedElementTree.fromstring(payload)
    if root.tag != _word(expected_root):
        raise ValueError
    nodes = 0
    for _ in root.iter():
        nodes += 1
        if nodes > _MAX_DOCX_XML_NODES:
            raise ValueError
    return root


def _docx_styles(
    root: Any,
) -> tuple[
    dict[str, _DocxStyle],
    Any | None,
    Any | None,
    dict[str, str],
]:
    styles: dict[str, _DocxStyle] = {}
    default_styles: dict[str, str] = {}
    for element in root.findall(_word("style")):
        style_id = _word_attribute(element, "styleId")
        style_type = _word_attribute(element, "type")
        if not style_id or not style_type or style_id in styles:
            raise ValueError
        default_value = _word_attribute(element, "default")
        if default_value is not None:
            normalized_default = default_value.casefold()
            if normalized_default not in {"true", "false", "1", "0", "on", "off"}:
                raise ValueError
            if normalized_default in {"true", "1", "on"} and style_type in {
                "paragraph",
                "character",
            }:
                if style_type in default_styles:
                    raise ValueError
                default_styles[style_type] = style_id
        name_element = element.find(_word("name"))
        name = (
            _word_attribute(name_element, "val")
            if name_element is not None
            else style_id
        )
        if not name:
            raise ValueError
        based_on_element = element.find(_word("basedOn"))
        based_on = (
            _word_attribute(based_on_element, "val")
            if based_on_element is not None
            else None
        )
        styles[style_id] = _DocxStyle(
            style_id=style_id,
            style_type=style_type,
            name=name,
            based_on=based_on,
            paragraph_properties=element.find(_word("pPr")),
            run_properties=element.find(_word("rPr")),
        )

    doc_defaults = root.find(_word("docDefaults"))
    paragraph_default = None
    run_default = None
    if doc_defaults is not None:
        paragraph_default = doc_defaults.find(
            f"{_word('pPrDefault')}/{_word('pPr')}"
        )
        run_default = doc_defaults.find(f"{_word('rPrDefault')}/{_word('rPr')}")
    return styles, paragraph_default, run_default, default_styles


def _docx_style_chain(
    style_id: str | None,
    styles: dict[str, _DocxStyle],
    expected_type: str,
) -> list[_DocxStyle]:
    if style_id is None:
        return []
    chain: list[_DocxStyle] = []
    seen: set[str] = set()
    current = style_id
    while current is not None:
        if current in seen or len(chain) >= _MAX_DOCX_STYLE_DEPTH:
            raise ValueError
        seen.add(current)
        style = styles.get(current)
        if style is None or style.style_type != expected_type:
            raise ValueError
        chain.append(style)
        current = style.based_on
    return chain


def _docx_style_id(properties: Any | None, child_name: str) -> str | None:
    if properties is None:
        return None
    child = properties.find(_word(child_name))
    if child is None:
        return None
    value = _word_attribute(child, "val")
    if not value:
        raise ValueError
    return value


def _docx_first_property(sources: list[Any | None], name: str) -> Any | None:
    for source in sources:
        if source is None:
            continue
        child = source.find(_word(name))
        if child is not None:
            return child
    return None


def _docx_first_property_attribute(
    sources: list[Any | None], property_name: str, attribute_name: str
) -> str | None:
    for source in sources:
        if source is None:
            continue
        child = source.find(_word(property_name))
        if child is None:
            continue
        value = _word_attribute(child, attribute_name)
        if value is not None:
            return value
    return None


def _docx_boolean_property(sources: list[Any | None], name: str) -> bool:
    element = _docx_first_property(sources, name)
    if element is None:
        return False
    value = _word_attribute(element, "val")
    if value is None:
        return True
    normalized = value.casefold()
    if normalized in {"true", "1", "on"}:
        return True
    if normalized in {"false", "0", "off"}:
        return False
    raise ValueError


def _docx_has_vazirmatn(sources: list[Any | None]) -> bool:
    values = [
        value
        for attribute in ("ascii", "hAnsi", "eastAsia", "cs")
        if (
            value := _docx_first_property_attribute(
                sources, "rFonts", attribute
            )
        )
        is not None
    ]
    return bool(values) and all(value.casefold() == "vazirmatn" for value in values)


def _docx_is_heading(chain: list[_DocxStyle]) -> bool:
    return any(
        value.casefold().replace(" ", "").startswith("heading")
        for style in chain
        for value in (style.style_id, style.name)
    )


def _docx_run_has_text(run: Any) -> bool:
    return any(
        isinstance(element.text, str) and bool(element.text.strip())
        for element in run.iter(_word("t"))
    )


def _verify_persian_docx(data: bytes) -> dict[str, int | bool | list[str]]:
    document_payload, styles_payload = _read_docx_parts(data)
    document_root = _parse_docx_xml(document_payload, "document")
    styles_root = _parse_docx_xml(styles_payload, "styles")
    styles, paragraph_default, run_default, default_styles = _docx_styles(
        styles_root
    )

    paragraphs = 0
    bidi_paragraphs = 0
    right_aligned_paragraphs = 0
    right_indented_paragraphs = 0
    text_runs = 0
    vazirmatn_runs = 0
    heading_runs = 0
    bold_heading_runs = 0

    for paragraph in document_root.iter(_word("p")):
        runs = [
            run
            for run in paragraph.iter(_word("r"))
            if _docx_run_has_text(run)
        ]
        if not runs:
            continue
        paragraphs += 1
        direct_paragraph = paragraph.find(_word("pPr"))
        paragraph_style_id = _docx_style_id(direct_paragraph, "pStyle")
        if paragraph_style_id is None:
            paragraph_style_id = default_styles.get("paragraph")
        paragraph_chain = _docx_style_chain(
            paragraph_style_id, styles, "paragraph"
        )
        paragraph_sources = [
            direct_paragraph,
            *(style.paragraph_properties for style in paragraph_chain),
            paragraph_default,
        ]
        bidi = _docx_boolean_property(paragraph_sources, "bidi")
        if bidi:
            bidi_paragraphs += 1
        alignment = _docx_first_property_attribute(
            paragraph_sources, "jc", "val"
        )
        # OOXML bidi reverses paragraph justification, including inherited jc.
        # Google exports native RTL+START as bidi+left, not bidi+right.
        right_values = {"left", "start"} if bidi else {"right", "end"}
        if alignment is not None and alignment.casefold() in right_values:
            right_aligned_paragraphs += 1
        right_indent = _docx_first_property_attribute(
            paragraph_sources, "ind", "right"
        )
        if right_indent is not None and int(right_indent) == 0:
            right_indented_paragraphs += 1

        is_heading = _docx_is_heading(paragraph_chain)
        for run in runs:
            text_runs += 1
            direct_run = run.find(_word("rPr"))
            character_style_id = _docx_style_id(direct_run, "rStyle")
            if character_style_id is None:
                character_style_id = default_styles.get("character")
            character_chain = _docx_style_chain(
                character_style_id, styles, "character"
            )
            run_sources = [
                direct_run,
                *(style.run_properties for style in character_chain),
                *(style.run_properties for style in paragraph_chain),
                run_default,
            ]
            if _docx_has_vazirmatn(run_sources):
                vazirmatn_runs += 1
            if is_heading:
                heading_runs += 1
                if _docx_boolean_property(run_sources, "b"):
                    bold_heading_runs += 1

    reasons_by_condition = {
        "paragraph_bidi_missing": bidi_paragraphs != paragraphs,
        "paragraph_right_alignment_missing": (
            right_aligned_paragraphs != paragraphs
        ),
        "paragraph_right_indent_missing": (
            right_indented_paragraphs != paragraphs
        ),
        "run_vazirmatn_missing": vazirmatn_runs != text_runs,
        "heading_bold_missing": bold_heading_runs != heading_runs,
    }
    reasons = [
        reason for reason in _DOCX_REASON_ORDER if reasons_by_condition[reason]
    ]
    return {
        "valid": not reasons,
        "paragraphs": paragraphs,
        "bidi_paragraphs": bidi_paragraphs,
        "right_aligned_paragraphs": right_aligned_paragraphs,
        "right_indented_paragraphs": right_indented_paragraphs,
        "text_runs": text_runs,
        "vazirmatn_runs": vazirmatn_runs,
        "heading_runs": heading_runs,
        "bold_heading_runs": bold_heading_runs,
        "reasons": reasons,
    }


def verify_persian_docx(data: bytes) -> dict[str, int | bool | list[str]]:
    result: dict[str, int | bool | list[str]] | None = None
    failed = False
    try:
        result = _verify_persian_docx(data)
    except Exception:
        failed = True
    if failed or result is None:
        raise _docx_verification_error()
    return result


class GoogleDocsClient:
    DOCS_BASE = "https://docs.googleapis.com/v1"
    DRIVE_BASE = "https://www.googleapis.com/drive/v3"
    RETRYABLE = {408, 429, 500, 502, 503, 504}

    _TIMEOUT = (20, 180)
    _MAX_ATTEMPTS = 5
    _DRIVE_FIELDS = "id,name,mimeType,modifiedTime,version,webViewLink"

    def __init__(self, session: Any) -> None:
        self._session = session

    @staticmethod
    def _retry_delay(response: Any, fallback: int) -> int:
        retry_after: object = None
        header_failed = False
        try:
            retry_after = response.headers.get("Retry-After")
        except Exception:
            header_failed = True

        if header_failed or not isinstance(retry_after, str):
            return fallback
        candidate = retry_after.strip()
        if not candidate or any(
            character not in "0123456789" for character in candidate
        ):
            return fallback

        normalized = candidate.lstrip("0") or "0"
        if len(normalized) > 2:
            return 60
        return min(int(normalized), 60)

    def _request(
        self,
        method: str,
        url: str,
        *,
        retry_safe: bool = True,
        **kwargs: object,
    ) -> Any:
        request_kwargs = dict(kwargs)
        request_kwargs["timeout"] = self._TIMEOUT

        for attempt in range(self._MAX_ATTEMPTS):
            response: Any = None
            network_failed = False
            request_failed = False
            try:
                response = self._session.request(method, url, **request_kwargs)
            except (requests.ConnectionError, requests.Timeout):
                network_failed = True
            except Exception:
                request_failed = True

            if request_failed:
                raise _google_unavailable()

            if network_failed:
                if not retry_safe:
                    raise _google_unavailable()
                if attempt == self._MAX_ATTEMPTS - 1:
                    raise _google_unavailable(retryable=True)
                time.sleep(2**attempt)
                continue

            status: object = None
            status_failed = False
            try:
                status = response.status_code
            except Exception:
                status_failed = True

            if (
                status_failed
                or not isinstance(status, int)
                or isinstance(status, bool)
            ):
                raise _google_unavailable()

            if 200 <= status < 300:
                return response
            if status == 401:
                raise _google_needs_reauth()
            if status == 403:
                raise _permission_denied()
            if status == 404:
                raise _document_not_found()
            if status not in self.RETRYABLE:
                raise _google_unavailable()
            if not retry_safe:
                if status == 429:
                    raise _rate_limited()
                raise _google_unavailable()
            if attempt == self._MAX_ATTEMPTS - 1:
                if status == 429:
                    raise _rate_limited()
                raise _google_unavailable(retryable=True)

            time.sleep(self._retry_delay(response, 2**attempt))

        raise _google_unavailable(retryable=True)

    @staticmethod
    def _json_dict(response: Any) -> dict:
        payload: object = None
        decoding_failed = False
        try:
            payload = response.json()
        except Exception:
            decoding_failed = True

        if decoding_failed or not isinstance(payload, dict):
            raise _google_unavailable()
        return payload

    def get_document(self, document_id: str) -> dict:
        normalized_id = parse_document_id(document_id)
        response = self._request(
            "GET",
            f"{self.DOCS_BASE}/documents/{normalized_id}",
            params={"includeTabsContent": "true"},
        )
        return self._json_dict(response)

    def create_document(self, title: str) -> str:
        normalized_title = validate_title(title)
        response = self._request(
            "POST",
            f"{self.DOCS_BASE}/documents",
            retry_safe=False,
            json={"title": normalized_title},
        )
        payload = self._json_dict(response)

        document_id: object = None
        shape_failed = False
        try:
            document_id = payload.get("documentId")
        except Exception:
            shape_failed = True
        if shape_failed or not isinstance(document_id, str) or not document_id:
            raise _google_unavailable()
        return document_id

    def batch_update(
        self,
        document_id: str,
        requests: list[dict],
        revision: str,
        *,
        retry_safe: bool = True,
    ) -> dict:
        normalized_id = parse_document_id(document_id)
        if not requests:
            return {}
        response = self._request(
            "POST",
            f"{self.DOCS_BASE}/documents/{normalized_id}:batchUpdate",
            retry_safe=retry_safe,
            json={
                "requests": requests,
                "writeControl": {"requiredRevisionId": revision},
            },
        )
        return self._json_dict(response)

    def drive_metadata(self, document_id: str) -> dict:
        normalized_id = parse_document_id(document_id)
        response = self._request(
            "GET",
            f"{self.DRIVE_BASE}/files/{normalized_id}",
            params={"fields": self._DRIVE_FIELDS},
        )
        return self._json_dict(response)

    def export_file(self, document_id: str, mime_type: str) -> bytes:
        normalized_id = parse_document_id(document_id)
        response = self._request(
            "GET",
            f"{self.DRIVE_BASE}/files/{normalized_id}/export",
            params={"mimeType": mime_type},
        )

        content: object = None
        content_failed = False
        try:
            content = response.content
        except Exception:
            content_failed = True
        if content_failed or not isinstance(content, bytes):
            raise _google_unavailable()
        return content

    def delete_file(self, document_id: str) -> None:
        normalized_id = parse_document_id(document_id)
        self._request(
            "DELETE",
            f"{self.DRIVE_BASE}/files/{normalized_id}",
        )


class _PartialWriteError(DocsMCPError):
    def __init__(
        self,
        *,
        phase: str,
        revision_id: str,
        recovery_path: Path,
    ) -> None:
        super().__init__(
            "partial_write_requires_recovery",
            "Google Docs was partially updated; manual recovery is required.",
        )
        self.phase = phase
        self.revision_id = revision_id
        self.recovery_path = recovery_path

    def as_result(self) -> dict[str, object]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
                "phase": self.phase,
                "revision_id": self.revision_id,
                "recovery_path": str(self.recovery_path),
                "recovery_action": (
                    "Restore document.txt or document.docx from recovery_path, "
                    "then delete that directory."
                ),
            },
        }


class _InitialRenderError(DocsMCPError):
    def __init__(
        self,
        *,
        document_id: str,
        document_url: str,
        failure_code: str,
        recovery_path: Path | None = None,
        phase: str | None = None,
        revision_id: str | None = None,
    ) -> None:
        super().__init__(
            "verification_failed",
            "The new Google document was retained because its initial publication could not be verified.",
        )
        if (recovery_path is None) != (phase is None) or (phase is None) != (
            revision_id is None
        ):
            raise ValueError("Incomplete recovery details.")
        self.document_id = document_id
        self.document_url = document_url
        self.failure_code = failure_code
        self.recovery_path = recovery_path
        self.phase = phase
        self.revision_id = revision_id

    def as_result(self) -> dict[str, object]:
        details: dict[str, object] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "document_id": self.document_id,
            "document_url": self.document_url,
            "retained_for_diagnosis": True,
            "failure_code": self.failure_code,
        }
        if self.recovery_path is not None:
            details.update(
                {
                    "phase": self.phase,
                    "revision_id": self.revision_id,
                    "recovery_path": str(self.recovery_path),
                    "recovery_action": (
                        "Restore document.txt or document.docx from recovery_path, "
                        "then delete that directory."
                    ),
                }
            )
        return {
            "ok": False,
            "error": details,
        }


def _response_revision(response: object) -> str:
    if not isinstance(response, dict):
        raise _google_unavailable()
    write_control = response.get("writeControl")
    if not isinstance(write_control, dict):
        raise _google_unavailable()
    revision = write_control.get("requiredRevisionId")
    if not isinstance(revision, str) or not revision:
        raise _google_unavailable()
    return revision


@dataclass(frozen=True)
class Replacement:
    old_text: str
    new_text: str
    expected_count: int = 1

    def __post_init__(self) -> None:
        validate_old_text(self.old_text)
        validate_new_text(self.new_text)
        validate_replacement_count(self.expected_count)


@dataclass(frozen=True)
class _EditSegment:
    segment_id: str
    start_index: int
    text: str


@dataclass(frozen=True)
class _EditCounts:
    new_count: int
    ranges: tuple[tuple[str, int, int], ...]


_EDIT_AUXILIARY_SEGMENTS = (
    ("headers", "headerId"),
    ("footers", "footerId"),
    ("footnotes", "footnoteId"),
)

_MAX_REPLACEMENTS = 100


def _overlapping_edits() -> DocsMCPError:
    return DocsMCPError(
        "overlapping_edits",
        "Text replacements overlap or can interfere with each other.",
    )


def _match_count_mismatch() -> DocsMCPError:
    return DocsMCPError(
        "match_count_mismatch",
        "A replacement match count differs from its expected count.",
    )


def _edit_verification_failed() -> DocsMCPError:
    return DocsMCPError(
        "verification_failed",
        "Google Docs targeted edit readback could not be verified.",
    )


def _multiple_tabs_require_tab_id() -> DocsMCPError:
    return DocsMCPError(
        "multiple_tabs_require_tab_id",
        "A tab ID is required when the Google document has multiple tabs.",
    )


def validate_noninterference(replacements: list[Replacement]) -> None:
    if (
        type(replacements) is not list
        or not 1 <= len(replacements) <= _MAX_REPLACEMENTS
        or any(type(replacement) is not Replacement for replacement in replacements)
    ):
        raise _overlapping_edits()

    old_texts = [replacement.old_text for replacement in replacements]
    for index, left in enumerate(old_texts):
        for right in old_texts[index + 1 :]:
            if left in right or right in left:
                raise _overlapping_edits()
    for replacement in replacements:
        if any(old_text in replacement.new_text for old_text in old_texts):
            raise _overlapping_edits()


def edit_requests(
    replacements: list[Replacement], tab_id: str
) -> list[dict]:
    validate_noninterference(replacements)
    if not isinstance(tab_id, str) or not tab_id or "\x00" in tab_id:
        raise _tab_not_found()
    return [
        {
            "replaceAllText": {
                "replaceText": replacement.new_text,
                "containsText": {
                    "text": replacement.old_text,
                    "matchCase": True,
                    "searchByRegex": False,
                },
                "tabsCriteria": {"tabIds": [tab_id]},
            }
        }
        for replacement in replacements
    ]


def _append_edit_paragraph_segments(
    paragraph: dict,
    segment_id: str,
    segments: list[_EditSegment],
    budget: list[int],
) -> None:
    elements = paragraph.get("elements", [])
    if not isinstance(elements, list):
        raise TypeError

    start_index: int | None = None
    end_index: int | None = None
    parts: list[str] = []

    def flush() -> None:
        nonlocal start_index, end_index
        if start_index is not None and parts:
            segments.append(
                _EditSegment(segment_id, start_index, "".join(parts))
            )
        start_index = None
        end_index = None
        parts.clear()

    for element in elements:
        budget[0] += 1
        if budget[0] > _MAX_RENDER_NODES or not isinstance(element, dict):
            raise TypeError
        text_run = element.get("textRun")
        if text_run is None:
            flush()
            continue
        if not isinstance(text_run, dict):
            raise TypeError
        content = text_run.get("content")
        # Google omits zero-valued startIndex in auxiliary segments.
        # Keep body indices explicit and validate the full UTF-16 span below.
        run_start = element.get("startIndex", 0 if segment_id else None)
        run_end = element.get("endIndex")
        if (
            not isinstance(content, str)
            or not isinstance(run_start, int)
            or isinstance(run_start, bool)
            or not isinstance(run_end, int)
            or isinstance(run_end, bool)
            or run_start < 0
            or run_end < run_start
        ):
            raise TypeError
        content_length = len(content)
        if content_length > _MAX_RENDER_CHARS - budget[1]:
            raise TypeError
        if run_end - run_start != utf16_length(content):
            raise TypeError

        budget[1] += content_length
        if start_index is None or end_index != run_start:
            flush()
            start_index = run_start
        parts.append(content)
        end_index = run_end
    flush()


def _append_edit_container_segments(
    container: dict,
    segment_id: str,
    segments: list[_EditSegment],
    budget: list[int],
) -> None:
    if not isinstance(container, dict):
        raise TypeError
    stack: list[tuple] = [("container", container)]

    while stack:
        event = stack.pop()
        kind = event[0]
        if kind == "sequence":
            sequence, index, next_kind = event[1], event[2], event[3]
            if index < len(sequence):
                stack.append(("sequence", sequence, index + 1, next_kind))
                stack.append((next_kind, sequence[index]))
            continue

        budget[0] += 1
        if budget[0] > _MAX_RENDER_NODES:
            raise TypeError
        value = event[1]
        if not isinstance(value, dict):
            raise TypeError

        if kind == "container":
            content = value.get("content", [])
            if not isinstance(content, list):
                raise TypeError
            stack.append(("sequence", content, 0, "structural"))
        elif kind == "structural":
            paragraph = value.get("paragraph")
            if paragraph is not None:
                if not isinstance(paragraph, dict):
                    raise TypeError
                _append_edit_paragraph_segments(
                    paragraph,
                    segment_id,
                    segments,
                    budget,
                )
                continue
            table = value.get("table")
            if table is not None:
                if not isinstance(table, dict):
                    raise TypeError
                rows = table.get("tableRows", [])
                if not isinstance(rows, list):
                    raise TypeError
                stack.append(("sequence", rows, 0, "table_row"))
                continue
            table_of_contents = value.get("tableOfContents")
            if table_of_contents is not None:
                if not isinstance(table_of_contents, dict):
                    raise TypeError
                stack.append(("container", table_of_contents))
        elif kind == "table_row":
            cells = value.get("tableCells", [])
            if not isinstance(cells, list):
                raise TypeError
            stack.append(("sequence", cells, 0, "container"))
        else:
            raise TypeError


def _document_tab_for_id(document: dict, tab_id: str) -> dict:
    raw_tabs = document.get("tabs")
    if not isinstance(raw_tabs, list):
        raise TypeError

    visited = 0
    stack: list[tuple] = [("tab_sequence", raw_tabs, 0)]
    while stack:
        event = stack.pop()
        kind = event[0]
        if kind == "tab_sequence":
            sequence, index = event[1], event[2]
            if index < len(sequence):
                stack.append(("tab_sequence", sequence, index + 1))
                stack.append(("tab", sequence[index]))
            continue
        if kind != "tab":
            raise TypeError

        visited += 1
        if visited > _MAX_TAB_NODES:
            raise TypeError
        tab = event[1]
        if not isinstance(tab, dict):
            raise TypeError
        properties = tab.get("tabProperties")
        document_tab = tab.get("documentTab")
        child_tabs = tab.get("childTabs", [])
        if (
            not isinstance(properties, dict)
            or not isinstance(document_tab, dict)
            or not isinstance(child_tabs, list)
        ):
            raise TypeError
        current_tab_id = properties.get("tabId")
        if not isinstance(current_tab_id, str):
            raise TypeError
        if current_tab_id == tab_id:
            return document_tab
        stack.append(("tab_sequence", child_tabs, 0))
    raise TypeError


def _tab_edit_containers(
    document_tab: dict,
) -> tuple[tuple[str, dict], ...]:
    if not isinstance(document_tab, dict):
        raise TypeError
    body = document_tab.get("body")
    if not isinstance(body, dict):
        raise TypeError

    containers: list[tuple[str, dict]] = [("", body)]
    seen_segment_ids = {""}
    remaining = _MAX_RENDER_NODES - 1
    for map_name, id_name in _EDIT_AUXILIARY_SEGMENTS:
        segment_map = document_tab.get(map_name, {})
        if not isinstance(segment_map, dict):
            raise TypeError
        if len(segment_map) > remaining:
            raise TypeError
        remaining -= len(segment_map)
        for segment_id in sorted(segment_map):
            segment = segment_map[segment_id]
            if (
                not isinstance(segment_id, str)
                or not segment_id
                or "\x00" in segment_id
                or segment_id in seen_segment_ids
                or not isinstance(segment, dict)
                or segment.get(id_name) != segment_id
            ):
                raise TypeError
            seen_segment_ids.add(segment_id)
            containers.append((segment_id, segment))
    return tuple(containers)


def _collect_edit_segments(document_tab: dict) -> tuple[_EditSegment, ...]:
    segments: list[_EditSegment] = []
    budget = [0, 0]
    for segment_id, container in _tab_edit_containers(document_tab):
        _append_edit_container_segments(
            container,
            segment_id,
            segments,
            budget,
        )
    return tuple(segments)


def _edit_segments(
    document: dict,
    tab: TabView,
) -> tuple[_EditSegment, ...]:
    result: tuple[_EditSegment, ...] | None = None
    failed = False
    try:
        document_tab = _document_tab_for_id(document, tab.tab_id)
        if document_tab.get("body") is not tab.body:
            raise TypeError
        result = _collect_edit_segments(document_tab)
    except Exception:
        failed = True
    if failed or result is None:
        raise _google_unavailable()
    return result


def _edit_ranges(
    segments: tuple[_EditSegment, ...], text: str
) -> tuple[tuple[str, int, int], ...]:
    ranges: list[tuple[str, int, int]] = []
    for segment in segments:
        offset = 0
        while len(ranges) <= _MAX_REPLACEMENTS:
            match = segment.text.find(text, offset)
            if match < 0:
                break
            ranges.append(
                (
                    segment.segment_id,
                    segment.start_index + utf16_index(segment.text, match),
                    segment.start_index
                    + utf16_index(segment.text, match + len(text)),
                )
            )
            offset = match + len(text)
    return tuple(ranges)


def _edit_text_count(
    segments: tuple[_EditSegment, ...], text: str
) -> int:
    if not text:
        return 0
    return sum(segment.text.count(text) for segment in segments)


def _simulate_edit_segments(
    segments: tuple[_EditSegment, ...],
    replacements: list[Replacement],
) -> tuple[_EditSegment, ...]:
    simulated = segments
    for replacement in replacements:
        if (
            _edit_text_count(simulated, replacement.old_text)
            != replacement.expected_count
        ):
            raise _overlapping_edits()

        next_segments: list[_EditSegment] = []
        projected_chars = 0
        for segment in simulated:
            occurrences = segment.text.count(replacement.old_text)
            projected_chars += len(segment.text) + occurrences * (
                len(replacement.new_text) - len(replacement.old_text)
            )
            if projected_chars > _MAX_RENDER_CHARS:
                raise _overlapping_edits()
            next_segments.append(
                _EditSegment(
                    segment_id=segment.segment_id,
                    start_index=segment.start_index,
                    text=segment.text.replace(
                        replacement.old_text, replacement.new_text
                    ),
                )
            )
        simulated = tuple(next_segments)

    if any(
        _edit_text_count(simulated, replacement.old_text)
        for replacement in replacements
    ):
        raise _overlapping_edits()
    return simulated


def _edit_document_state(
    client: Any,
    document_id: str,
    replacements: list[Replacement],
    tab_id: str | None,
    expected_revision_id: str,
) -> tuple[
    str,
    str,
    tuple[_EditSegment, ...],
    tuple[_EditCounts, ...],
    tuple[int, ...],
]:
    document = client.get_document(document_id)
    if not isinstance(document, dict):
        raise _google_unavailable()
    revision = document.get("revisionId")
    if (
        not isinstance(revision, str)
        or not revision
        or not isinstance(expected_revision_id, str)
        or not expected_revision_id
        or revision != expected_revision_id
    ):
        raise DocsMCPError(
            "stale_revision",
            "The Google document revision does not match the expected revision.",
        )

    selected = select_tab(document, tab_id)
    if selected is None:
        raise _multiple_tabs_require_tab_id()
    if not selected.tab_id:
        raise _tab_not_found()
    segments = _edit_segments(document, selected)
    counts: list[_EditCounts] = []
    for replacement in replacements:
        ranges = _edit_ranges(segments, replacement.old_text)
        if len(ranges) != replacement.expected_count:
            raise _match_count_mismatch()
        counts.append(
            _EditCounts(
                ranges=ranges,
                new_count=_edit_text_count(segments, replacement.new_text),
            )
        )
    simulated = _simulate_edit_segments(segments, replacements)
    expected_after_new_counts = tuple(
        _edit_text_count(simulated, replacement.new_text)
        for replacement in replacements
    )
    return (
        revision,
        selected.tab_id,
        segments,
        tuple(counts),
        expected_after_new_counts,
    )


def _edit_range_result(edit_range: tuple[str, int, int]) -> dict[str, object]:
    segment_id, start_index, end_index = edit_range
    result: dict[str, object] = {
        "start_index": start_index,
        "end_index": end_index,
    }
    if segment_id:
        result["segment_id"] = segment_id
    return result


def preview_edits(
    client: Any,
    document_id: str,
    replacements: list[Replacement],
    tab_id: str | None,
    expected_revision_id: str,
) -> dict[str, object]:
    normalized_id = parse_document_id(document_id)
    validate_noninterference(replacements)
    (
        revision,
        selected_tab_id,
        _segments,
        counts,
        _expected_after_new_counts,
    ) = _edit_document_state(
        client,
        normalized_id,
        replacements,
        tab_id,
        expected_revision_id,
    )
    return {
        "document_id": normalized_id,
        "revision_id": revision,
        "tab_id": selected_tab_id,
        "valid": True,
        "replacements": [
            {
                "index": index,
                "expected_count": replacement.expected_count,
                "actual_count": len(item.ranges),
                "preexisting_new_count": item.new_count,
                "ranges": [
                    _edit_range_result(edit_range)
                    for edit_range in item.ranges
                ],
            }
            for index, (replacement, item) in enumerate(
                zip(replacements, counts, strict=True)
            )
        ],
    }


def _edit_response_counts(
    response: object, replacements: list[Replacement]
) -> tuple[str, tuple[int, ...]]:
    if not isinstance(response, dict):
        raise _edit_verification_failed()
    replies = response.get("replies")
    write_control = response.get("writeControl")
    if (
        not isinstance(replies, list)
        or len(replies) != len(replacements)
        or not isinstance(write_control, dict)
    ):
        raise _edit_verification_failed()
    revision = write_control.get("requiredRevisionId")
    if not isinstance(revision, str) or not revision:
        raise _edit_verification_failed()

    changed_counts: list[int] = []
    for reply, replacement in zip(replies, replacements, strict=True):
        if not isinstance(reply, dict):
            raise _edit_verification_failed()
        replace_result = reply.get("replaceAllText")
        if not isinstance(replace_result, dict):
            raise _edit_verification_failed()
        changed = replace_result.get("occurrencesChanged")
        if (
            not isinstance(changed, int)
            or isinstance(changed, bool)
            or changed != replacement.expected_count
        ):
            raise _edit_verification_failed()
        changed_counts.append(changed)
    return revision, tuple(changed_counts)


def _edit_readback_counts(
    document: object,
    tab_id: str,
    revision: str,
    replacements: list[Replacement],
) -> tuple[tuple[int, int], ...]:
    if not isinstance(document, dict) or document.get("revisionId") != revision:
        raise _edit_verification_failed()
    selected = select_tab(document, tab_id)
    if selected is None or selected.tab_id != tab_id:
        raise _edit_verification_failed()
    segments = _edit_segments(document, selected)
    return tuple(
        (
            len(_edit_ranges(segments, replacement.old_text)),
            _edit_text_count(segments, replacement.new_text),
        )
        for replacement in replacements
    )


def apply_edits(
    client: Any,
    document_id: str,
    replacements: list[Replacement],
    tab_id: str | None,
    expected_revision_id: str,
) -> dict[str, object]:
    normalized_id = parse_document_id(document_id)
    validate_noninterference(replacements)
    (
        revision,
        selected_tab_id,
        _segments,
        before_counts,
        expected_after_new_counts,
    ) = _edit_document_state(
        client,
        normalized_id,
        replacements,
        tab_id,
        expected_revision_id,
    )
    requests = edit_requests(replacements, selected_tab_id)
    response = client.batch_update(normalized_id, requests, revision)
    after_revision, changed_counts = _edit_response_counts(response, replacements)
    if after_revision == revision:
        raise _edit_verification_failed()
    after_document = client.get_document(normalized_id)
    after_counts = _edit_readback_counts(
        after_document,
        selected_tab_id,
        after_revision,
        replacements,
    )

    for after, expected_new_count in zip(
        after_counts, expected_after_new_counts, strict=True
    ):
        after_old_count, after_new_count = after
        if after_old_count != 0 or after_new_count != expected_new_count:
            raise _edit_verification_failed()

    return {
        "document_id": normalized_id,
        "before_revision_id": revision,
        "after_revision_id": after_revision,
        "tab_id": selected_tab_id,
        "verified": True,
        "replacements": [
            {
                "index": index,
                "expected_count": replacement.expected_count,
                "occurrences_changed": changed,
                "before_old_count": len(before.ranges),
                "before_new_count": before.new_count,
                "after_old_count": after[0],
                "after_new_count": after[1],
            }
            for index, (replacement, before, after, changed) in enumerate(
                zip(
                    replacements,
                    before_counts,
                    after_counts,
                    changed_counts,
                    strict=True,
                )
            )
        ],
    }


def _table_nodes_for_tab(
    document: dict, tab_id: str, *, expected_count: int
) -> tuple[dict, ...]:
    selected = select_tab(document, tab_id)
    if selected is None:
        raise _tab_not_found()
    content = selected.body.get("content")
    if not isinstance(content, list):
        raise _google_unavailable()
    nodes: list[dict] = []
    for element in content:
        if not isinstance(element, dict):
            raise _google_unavailable()
        if "table" not in element:
            continue
        node = dict(element)
        node["tabId"] = tab_id
        nodes.append(node)
    if len(nodes) != expected_count:
        raise DocsMCPError(
            "verification_failed",
            "Google Docs table readback does not match the Markdown model.",
        )
    return tuple(nodes)


def _read_phase_tables(
    client: Any,
    document_id: str,
    tab_id: str,
    revision: str,
    expected_count: int,
) -> tuple[dict, ...]:
    document = client.get_document(document_id)
    if not isinstance(document, dict) or document.get("revisionId") != revision:
        raise DocsMCPError(
            "stale_revision",
            "The Google document changed during table rendering.",
        )
    return _table_nodes_for_tab(
        document, tab_id, expected_count=expected_count
    )


def _run_table_phases(
    client: Any,
    *,
    document_id: str,
    model: Any,
    tab_id: str,
    revision: str,
    backup: RecoveryBackup | None,
    cleanup_backup: bool = True,
    profile: str = "plain",
) -> str:
    if not getattr(model, "tables", ()):
        return revision
    if not isinstance(backup, RecoveryBackup) or not _recovery_backup_is_usable(
        backup
    ):
        raise _recovery_unavailable()

    from . import markdown as markdown_module

    structure_requests = markdown_module.insert_table_structure_requests(
        model, tab_id
    )
    markdown_module._validate_table_phase_request_counts(model, profile)
    phase = "table_structure"
    failed = False
    try:
        response = client.batch_update(document_id, structure_requests, revision)
        revision = _response_revision(response)
        phase = "table_structure_readback"
        table_nodes = _read_phase_tables(
            client,
            document_id,
            tab_id,
            revision,
            len(model.tables),
        )

        cell_requests: list[dict] = []
        for table, table_node in zip(model.tables, table_nodes, strict=True):
            cell_requests.extend(
                markdown_module.table_cell_insert_requests(
                    table_node, table.rows
                )
            )
        cell_requests.sort(
            key=lambda request: request["insertText"]["location"]["index"],
            reverse=True,
        )
        if cell_requests:
            phase = "table_cell_text"
            cell_requests = markdown_module._enforce_request_plan(cell_requests)
            response = client.batch_update(document_id, cell_requests, revision)
            revision = _response_revision(response)
            phase = "table_cell_readback"
            table_nodes = _read_phase_tables(
                client,
                document_id,
                tab_id,
                revision,
                len(model.tables),
            )

        style_requests: list[dict] = []
        for table, table_node in zip(model.tables, table_nodes, strict=True):
            style_requests.extend(
                markdown_module.table_cell_style_requests(
                    table_node, table.rows, profile
                )
            )
        if style_requests:
            phase = "table_cell_styles"
            style_requests = markdown_module._enforce_request_plan(style_requests)
            response = client.batch_update(document_id, style_requests, revision)
            revision = _response_revision(response)
    except Exception:
        failed = True

    if failed:
        raise _PartialWriteError(
            phase=phase,
            revision_id=revision,
            recovery_path=backup.path,
        )
    if cleanup_backup and not _remove_recovery_directory(
        backup.path, allow_partial=False
    ):
        raise _PartialWriteError(
            phase="recovery_cleanup",
            revision_id=revision,
            recovery_path=backup.path,
        )
    return revision


_NATIVE_DOCUMENT_MIME = "application/vnd.google-apps.document"


def _service_metadata(client: GoogleDocsClient, document_id: str) -> dict:
    metadata: dict | None = None
    failed = False
    try:
        value = client.drive_metadata(document_id)
        if isinstance(value, dict):
            metadata = value
        else:
            failed = True
    except DocsMCPError:
        raise
    except Exception:
        failed = True

    if failed or metadata is None:
        raise _google_unavailable()
    required = (
        metadata.get("id"),
        metadata.get("name"),
        metadata.get("mimeType"),
        metadata.get("modifiedTime"),
        metadata.get("version"),
        metadata.get("webViewLink"),
    )
    if (
        required[0] != document_id
        or any(not isinstance(value, str) or not value for value in required[1:])
        or any("\x00" in value for value in required if isinstance(value, str))
    ):
        raise _google_unavailable()

    link_matches = False
    try:
        link_matches = parse_document_id(required[5]) == document_id
    except DocsMCPError:
        pass
    if not link_matches:
        raise _google_unavailable()
    return metadata


def _service_document(
    client: GoogleDocsClient, document_id: str, *, require_revision: bool = True
) -> dict:
    value: dict | None = None
    failed = False
    try:
        result = client.get_document(document_id)
        if isinstance(result, dict):
            value = result
        else:
            failed = True
    except DocsMCPError:
        raise
    except Exception:
        failed = True
    if (
        failed
        or value is None
        or value.get("documentId") != document_id
        or (
            (require_revision or "revisionId" in value)
            and (
                not isinstance(value.get("revisionId"), str)
                or not value["revisionId"]
            )
        )
    ):
        raise _google_unavailable()
    return value


def _service_tabs(document: dict) -> list[TabView]:
    raw_tabs = document.get("tabs")
    if raw_tabs is None:
        selected = select_tab(document, None)
        return [] if selected is None else [selected]
    return flatten_tabs(raw_tabs)


def _tab_result(tab: TabView) -> dict[str, str | None]:
    return {
        "tab_id": tab.tab_id,
        "title": tab.title,
        "parent_tab_id": tab.parent_tab_id,
    }


def _require_native_document(metadata: dict) -> None:
    if metadata.get("mimeType") != _NATIVE_DOCUMENT_MIME:
        raise DocsMCPError(
            "unsupported_office_file",
            "Only native Google Docs documents are supported.",
        )


def _validate_service_revision(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1_000
        or "\x00" in value
    ):
        raise DocsMCPError(
            "invalid_input",
            "expected_revision_id must be a non-empty revision identifier.",
        )
    return value


def _validate_service_profile(value: object) -> str:
    if value not in {"persian", "plain"}:
        raise DocsMCPError(
            "invalid_input",
            "format_profile must be persian or plain.",
        )
    assert isinstance(value, str)
    return value


def _service_end_index(body: dict) -> int:
    content = body.get("content")
    if not isinstance(content, list) or not content:
        raise _google_unavailable()
    final = content[-1]
    if not isinstance(final, dict):
        raise _google_unavailable()
    end_index = final.get("endIndex")
    if (
        not isinstance(end_index, int)
        or isinstance(end_index, bool)
        or end_index < 2
    ):
        raise _google_unavailable()
    return end_index


def _service_batch_revision(
    client: GoogleDocsClient,
    document_id: str,
    requests: list[dict],
    revision_id: str,
    *,
    retry_safe: bool = True,
) -> str:
    response: dict | None = None
    failed = False
    try:
        if retry_safe:
            value = client.batch_update(document_id, requests, revision_id)
        else:
            value = client.batch_update(document_id, requests, revision_id, retry_safe=False)
        if isinstance(value, dict):
            response = value
        else:
            failed = True
    except DocsMCPError:
        raise
    except Exception:
        failed = True
    if failed or response is None:
        raise _google_unavailable()
    next_revision = _response_revision(response)
    if next_revision == revision_id:
        raise DocsMCPError(
            "verification_failed",
            "Google Docs did not report a new revision after the write.",
        )
    return next_revision


def _service_semantic_result(
    model: Any, body: dict, profile: str = "plain"
) -> dict[str, object]:
    from . import markdown as markdown_module

    candidate = markdown_module.candidate_semantic(model, profile=profile)
    remote = markdown_module.remote_semantic(body)
    candidate_hash = markdown_module.semantic_sha256(candidate)
    if markdown_module.semantic_sha256(remote) != candidate_hash:
        raise DocsMCPError(
            "verification_failed",
            "Google Docs content verification failed.",
        )
    blocks = candidate.get("blocks")
    schema = candidate.get("schema")
    if (
        not isinstance(blocks, list)
        or not isinstance(schema, int)
        or isinstance(schema, bool)
    ):
        raise DocsMCPError(
            "verification_failed",
            "Google Docs content verification failed.",
        )
    return {
        "schema": schema,
        "block_count": len(blocks),
        "sha256": candidate_hash,
    }


def _verify_persian_api(
    body: dict, *, start_index: int | None = None, end_index: int | None = None
) -> None:
    """Check the same revision's body/cells, independently of DOCX export."""
    failed = False
    try:
        content = body["content"]
        if not isinstance(content, list):
            raise ValueError
        # Iterator continuations bound width as well as depth. The semantic
        # verifier already rejects unsupported content before this check.
        stack = [(iter(content), "content")]
        visited = 0
        while stack:
            iterator, kind = stack[-1]
            try:
                node = next(iterator)
            except StopIteration:
                stack.pop()
                continue
            visited += 1
            if visited > _MAX_RENDER_NODES or not isinstance(node, dict):
                raise ValueError
            if kind == "row":
                children, child_kind = node["tableCells"], "cell"
            elif kind == "cell":
                children, child_kind = node["content"], "content"
            elif "table" in node:
                children, child_kind = node["table"]["tableRows"], "row"
            elif "paragraph" in node:
                paragraph = node["paragraph"]
                elements = paragraph["elements"]
                if not isinstance(elements, list) or not elements:
                    raise ValueError
                if start_index is not None and end_index is not None:
                    elements = [
                        element for element in elements
                        if element["startIndex"] < end_index
                        and element["endIndex"] > start_index
                    ]
                    if not elements:
                        continue
                style = paragraph["paragraphStyle"]
                if style.get("direction") != "RIGHT_TO_LEFT" or style.get("alignment") != "START":
                    raise ValueError
                for name in ("indentStart", "indentEnd"):
                    indent = style[name]
                    magnitude = indent.get("magnitude", 0)
                    if (type(magnitude) not in (int, float) or magnitude != 0 or indent.get("unit") != "PT"):
                        raise ValueError
                for element in elements:
                    visited += 1
                    if visited > _MAX_RENDER_NODES:
                        raise ValueError
                    run = element["textRun"]
                    text_style = run["textStyle"]
                    if text_style["weightedFontFamily"].get("fontFamily") != "Vazirmatn":
                        raise ValueError
                    if (start_index is None and style.get("namedStyleType") in _HEADING_LEVELS
                            and run["content"].strip()
                            and text_style.get("bold") is not True):
                        raise ValueError
                continue
            elif start_index is not None and "tableOfContents" in node:
                children, child_kind = node["tableOfContents"]["content"], "content"
            elif "sectionBreak" in node:
                continue
            else:
                raise ValueError
            if not isinstance(children, list):
                raise ValueError
            stack.append((iter(children), child_kind))
    except Exception:
        failed = True
    if failed:
        raise DocsMCPError(
            "verification_failed", "Google Docs API formatting verification failed."
        )


def _service_format_result(
    client: GoogleDocsClient,
    document_id: str,
    format_profile: str,
    body: dict,
    *,
    candidate_has_text: bool,
) -> dict[str, bool | int | list[str]] | None:
    if format_profile == "plain":
        return None
    # Semantic readback already matched; empty replacement emits no styles.
    if candidate_has_text:
        _verify_persian_api(body)
    result = verify_persian_docx(
        client.export_file(document_id, _RECOVERY_DOCX_MIME)
    )
    if result.get("valid") is not True:
        raise DocsMCPError(
            "verification_failed",
            "Google Docs formatting verification failed.",
        )
    return result


def _canonical_document_url(document_id: str) -> str:
    return f"https://docs.google.com/document/d/{document_id}/edit"


def _service_semantic_readback(
    client: GoogleDocsClient,
    document_id: str,
    tab_id: str,
    revision_id: str,
    model: Any,
    profile: str,
) -> tuple[dict[str, object], dict]:
    document = _service_document(client, document_id)
    if document["revisionId"] != revision_id:
        raise DocsMCPError(
            "verification_failed",
            "Google Docs revision verification failed.",
        )
    selected = select_tab(document, tab_id)
    if selected is None:
        raise DocsMCPError(
            "verification_failed",
            "Google Docs tab verification failed.",
        )
    return _service_semantic_result(model, selected.body, profile), selected.body


def _invalid_insertion() -> DocsMCPError:
    return DocsMCPError(
        "invalid_input", "Invalid text insertion arguments or unsupported insertion boundary."
    )


def _validate_insertion(
    text: object, position: object, anchor_text: object,
    tab_id: object, format_profile: object, apply: object,
) -> None:
    # Google strips these controls/private-use characters during insertText.
    # Reject rather than silently normalize an exact-text write.
    if (
        not isinstance(text, str) or not 1 <= len(text) <= 500_000
        or re.search(r"[\x00-\x08\x0b-\x1f\ud800-\udfff\ue000-\uf8ff]", text)
        or not isinstance(position, str) or position not in {"start", "end", "before", "after"}
        or not isinstance(format_profile, str) or format_profile not in {"persian", "plain"}
        or type(apply) is not bool
        or (tab_id is not None and (not isinstance(tab_id, str) or not tab_id or "\x00" in tab_id))
    ):
        raise _invalid_insertion()
    if position in {"before", "after"}:
        if (
            not isinstance(anchor_text, str) or not 1 <= len(anchor_text) <= 500_000
            or re.search(r"[\x00-\x1f\ud800-\udfff]", anchor_text)
        ):
            raise _invalid_insertion()
    elif anchor_text is not None:
        raise _invalid_insertion()


def _insertion_segments(body: dict) -> tuple[_EditSegment, ...]:
    result = None
    try:
        # Deliberately exclude headers, footers, and footnotes from targeting.
        result = _collect_edit_segments({"body": body})
    except Exception:
        pass
    if result is None:
        raise _google_unavailable()
    return result


def _is_first_paragraph_boundary(node: dict) -> bool:
    """Validate the fallback for a paragraph beginning with non-text content."""
    start, end = node.get("startIndex"), node.get("endIndex")
    paragraph = node.get("paragraph")
    if type(start) is not int or start != 1 or type(end) is not int or end < 2:
        return False
    if not isinstance(paragraph, dict):
        return False
    elements = paragraph.get("elements")
    if not isinstance(elements, list) or not elements:
        return False
    cursor = start
    for element in elements:
        if not isinstance(element, dict):
            return False
        left, right = element.get("startIndex"), element.get("endIndex")
        if type(left) is not int or type(right) is not int or left != cursor or not left < right <= end:
            return False
        if not any(isinstance(element.get(kind), dict) for kind in ("textRun", *_KNOWN_NON_TEXT_KINDS)):
            return False
        cursor = right
    terminal = elements[-1].get("textRun")
    return (
        cursor == end and isinstance(terminal, dict)
        and isinstance(terminal.get("content"), str) and terminal["content"].endswith("\n")
    )


def _insertion_index(
    body: dict, segments: tuple[_EditSegment, ...], position: str, anchor_text: str | None
) -> int:
    if position == "start":
        index = 1
    elif position == "end":
        index = _service_end_index(body) - 1
    else:
        assert anchor_text is not None
        matches: list[int] = []
        for segment in segments:
            offset = 0
            while True:
                match = segment.text.find(anchor_text, offset)
                if match < 0:
                    break
                end = match + len(anchor_text) if position == "after" else match
                matches.append(segment.start_index + utf16_index(segment.text, end))
                if len(matches) > 1:
                    break
                # Count overlapping matches too: 'aa' in 'aaa' is ambiguous.
                offset = match + 1
            if len(matches) > 1:
                break
        if len(matches) != 1:
            raise DocsMCPError(
                "anchor_match_mismatch", "The selected body must contain exactly one anchor match."
            )
        index = matches[0]
    text_boundary = any(
        segment.start_index <= index <= segment.start_index + utf16_length(segment.text)
        for segment in segments
    )
    # An image-first paragraph still has a legal insertion boundary at 1.
    paragraph_start = position == "start" and any(
        _is_first_paragraph_boundary(node)
        for node in body["content"]
    )
    if not 1 <= index < _service_end_index(body) or not (text_boundary or paragraph_start):
        raise _invalid_insertion()
    return index


def _insertion_snapshot(
    segments: tuple[_EditSegment, ...], *, index: int | None = None, text: str = ""
) -> tuple[tuple[int, bytes], ...]:
    """Compare indexed text, allowing Google's paragraph/style-run splitting.

    Gaps for tables and inline objects remain significant. Byte slicing uses
    UTF-16 boundaries already resolved from the exact anchor, not user indices.
    """
    if sum(len(segment.text) for segment in segments) + len(text) > _MAX_RENDER_CHARS:
        raise _invalid_insertion()
    if index is not None and segments and index < segments[0].start_index:
        # Insert before a leading inline object; retain the object's index gap.
        segments = (_EditSegment("", index, ""), *segments)
    inserted = text.encode("utf-16-le")
    delta = len(inserted) // 2
    groups: list[tuple[int, bytes]] = []
    parts: list[bytes] = []
    group_start = previous_end = 0
    insertion_done = False
    for segment in segments:
        start = segment.start_index
        payload = segment.text.encode("utf-16-le")
        if index is not None:
            if not insertion_done and start <= index <= start + len(payload) // 2:
                offset = (index - start) * 2
                payload = payload[:offset] + inserted + payload[offset:]
                insertion_done = True
            elif start >= index:
                start += delta
        if parts and start < previous_end:
            raise _google_unavailable()
        if not parts or start != previous_end:
            if parts:
                groups.append((group_start, b"".join(parts)))
            group_start = start
            parts = []
        parts.append(payload)
        previous_end = start + len(payload) // 2
    if parts:
        groups.append((group_start, b"".join(parts)))
    if index is not None and not insertion_done:
        raise _invalid_insertion()
    return tuple(groups)


class GoogleDocsService:
    def __init__(self, client: GoogleDocsClient, recovery_root: Path) -> None:
        self._client = client
        self._recovery_root = Path(recovery_root)

    def read(
        self,
        document: str,
        tab_id: str | None = None,
        start: int = 0,
        max_chars: int = 30_000,
    ) -> dict[str, object]:
        document_id = parse_document_id(document)
        validate_max_chars(max_chars)
        metadata = _service_metadata(self._client, document_id)
        _require_native_document(metadata)
        # Google omits revisionId for viewers/commenters. Only reads may
        # accept that omission; mutation paths retain the strict default.
        raw_document = _service_document(
            self._client, document_id, require_revision=False
        )
        tabs = _service_tabs(raw_document)
        selected = select_tab(raw_document, tab_id)
        result: dict[str, object] = {
            "ok": True,
            "document_id": document_id,
            "document_url": metadata["webViewLink"],
            "name": metadata["name"],
            "mime_type": metadata["mimeType"],
            "modified_time": metadata["modifiedTime"],
            "version": metadata["version"],
            "revision_id": raw_document.get("revisionId"),
            "tabs": [_tab_result(tab) for tab in tabs],
            "verified": True,
        }
        if selected is None:
            return result
        text, outline = render_body(selected.body)
        result["tab_id"] = selected.tab_id
        result.update(paginate(text, start, max_chars))
        result["outline"] = outline
        from .editing_common import table_inventory
        result["tables"] = table_inventory(selected.body)
        from .read_metadata import read_metadata
        result.update(read_metadata(raw_document, selected.tab_id))
        return result

    def create(
        self,
        title: str,
        markdown: str = "",
        format_profile: str = "persian",
    ) -> dict[str, object]:
        from . import markdown as markdown_module

        normalized_title = validate_title(title)
        profile = _validate_service_profile(format_profile)
        model = markdown_module.parse_markdown(markdown)
        markdown_module.semantic_sha256(
            markdown_module.candidate_semantic(model, profile=profile)
        )

        # Validate initial and aggregate table work before creating the document.
        # The new tab ID is unknown; complete real payload caps run again below.
        markdown_module.replacement_requests(model, 2, "", profile)
        markdown_module._validate_table_phase_request_counts(model, profile)

        document_id = parse_document_id(
            self._client.create_document(normalized_title)
        )
        document_url = _canonical_document_url(document_id)
        result: dict[str, object] | None = None
        failure_code: str | None = None
        recovery_path: Path | None = None
        failure_phase: str | None = None
        failure_revision: str | None = None
        try:
            metadata = _service_metadata(self._client, document_id)
            _require_native_document(metadata)
            document_url = metadata["webViewLink"]
            created_document = _service_document(self._client, document_id)
            selected = select_tab(created_document, None)
            if selected is None or not selected.tab_id:
                raise DocsMCPError(
                    "verification_failed",
                    "The new Google document tab could not be verified.",
                )
            if markdown:
                replaced = self.replace_markdown(
                    document_id,
                    markdown,
                    created_document["revisionId"],
                    tab_id=selected.tab_id,
                    format_profile=profile,
                )
                result = {
                    "ok": True,
                    "document_id": document_id,
                    "document_url": document_url,
                    "revision_id": replaced["after_revision_id"],
                    "tab_id": replaced["tab_id"],
                    "semantic": replaced["semantic"],
                    "format_profile": profile,
                    "formatting": replaced["formatting"],
                    "verified": True,
                }
            else:
                result = {
                    "ok": True,
                    "document_id": document_id,
                    "document_url": document_url,
                    "revision_id": created_document["revisionId"],
                    "tab_id": selected.tab_id,
                    "semantic": _service_semantic_result(model, selected.body, profile),
                    "format_profile": profile,
                    "formatting": None,
                    "verified": True,
                }
        except DocsMCPError as error:
            failure_code = error.code
            if isinstance(error, _PartialWriteError):
                recovery_path = error.recovery_path
                failure_phase = error.phase
                failure_revision = error.revision_id
        except Exception:
            failure_code = "google_unavailable"

        if failure_code is not None or result is None:
            raise _InitialRenderError(
                document_id=document_id,
                document_url=document_url,
                failure_code=failure_code or "google_unavailable",
                recovery_path=recovery_path,
                phase=failure_phase,
                revision_id=failure_revision,
            )
        return result

    def replace_markdown(
        self,
        document: str,
        markdown: str,
        expected_revision_id: str,
        tab_id: str | None = None,
        format_profile: str = "persian",
    ) -> dict[str, object]:
        from . import markdown as markdown_module

        document_id = parse_document_id(document)
        revision_before = _validate_service_revision(expected_revision_id)
        profile = _validate_service_profile(format_profile)
        model = markdown_module.parse_markdown(markdown)
        markdown_module.semantic_sha256(
            markdown_module.candidate_semantic(model, profile=profile)
        )

        metadata = _service_metadata(self._client, document_id)
        _require_native_document(metadata)
        source_document = _service_document(self._client, document_id)
        if source_document["revisionId"] != revision_before:
            raise DocsMCPError(
                "stale_revision",
                "The Google document revision changed before the write.",
            )
        selected = select_tab(source_document, tab_id)
        if selected is None:
            raise _multiple_tabs_require_tab_id()
        if not selected.tab_id:
            raise _tab_not_found()

        requests = markdown_module.replacement_requests(
            model,
            _service_end_index(selected.body),
            selected.tab_id,
            profile,
        )
        if model.tables:
            markdown_module._validate_table_phase_request_counts(model, profile)

        backup: RecoveryBackup | None = None
        if model.tables:
            backup = make_recovery_backup(
                self._client,
                document_id,
                self._recovery_root,
            )

        revision_after = revision_before
        if requests:
            initial_failed = False
            try:
                revision_after = _service_batch_revision(
                    self._client,
                    document_id,
                    requests,
                    revision_before,
                )
            except DocsMCPError:
                if backup is None:
                    raise
                initial_failed = True
            if initial_failed:
                assert backup is not None
                raise _PartialWriteError(
                    phase="initial_content",
                    revision_id=revision_before,
                    recovery_path=backup.path,
                )

        if model.tables:
            assert backup is not None
            revision_after = _run_table_phases(
                self._client,
                document_id=document_id,
                tab_id=selected.tab_id,
                model=model,
                revision=revision_after,
                backup=backup,
                cleanup_backup=False,
                profile=profile,
            )

        semantic: dict[str, object] | None = None
        readback_body: dict = {}
        formatting: dict[str, bool | int | list[str]] | None = None
        semantic_failed = False
        try:
            semantic, readback_body = _service_semantic_readback(
                self._client,
                document_id,
                selected.tab_id,
                revision_after,
                model,
                profile,
            )
        except DocsMCPError:
            if backup is None:
                raise
            semantic_failed = True
        if semantic_failed:
            assert backup is not None
            raise _PartialWriteError(
                phase="semantic_verification",
                revision_id=revision_after,
                recovery_path=backup.path,
            )

        format_failed = False
        try:
            formatting = _service_format_result(
                self._client,
                document_id,
                profile,
                readback_body,
                candidate_has_text=bool(model.text),
            )
        except DocsMCPError:
            if backup is None:
                raise
            format_failed = True
        if format_failed:
            assert backup is not None
            raise _PartialWriteError(
                phase="format_verification",
                revision_id=revision_after,
                recovery_path=backup.path,
            )

        if backup is not None and not _remove_recovery_directory(
            backup.path,
            allow_partial=False,
        ):
            raise _PartialWriteError(
                phase="recovery_cleanup",
                revision_id=revision_after,
                recovery_path=backup.path,
            )
        assert semantic is not None
        return {
            "ok": True,
            "document_id": document_id,
            "document_url": metadata["webViewLink"],
            "tab_id": selected.tab_id,
            "before_revision_id": revision_before,
            "after_revision_id": revision_after,
            "semantic": semantic,
            "format_profile": profile,
            "formatting": formatting,
            "verified": True,
        }

    def insert_text(
        self,
        document: str,
        text: str,
        expected_revision_id: str,
        position: str = "end",
        anchor_text: str | None = None,
        tab_id: str | None = None,
        format_profile: str = "persian",
        apply: bool = False,
    ) -> dict[str, object]:
        from . import markdown as markdown_module

        document_id = parse_document_id(document)
        revision = _validate_service_revision(expected_revision_id)
        _validate_insertion(text, position, anchor_text, tab_id, format_profile, apply)
        metadata = _service_metadata(self._client, document_id)
        _require_native_document(metadata)
        before = _service_document(self._client, document_id)
        if before["revisionId"] != revision:
            raise DocsMCPError(
                "stale_revision", "The Google document revision does not match the expected revision."
            )
        selected = select_tab(before, tab_id)
        if selected is None:
            raise _multiple_tabs_require_tab_id()
        if not selected.tab_id:
            raise _tab_not_found()
        segments = _insertion_segments(selected.body)
        index = _insertion_index(selected.body, segments, position, anchor_text)
        expected = _insertion_snapshot(segments, index=index, text=text)
        end_index = index + utf16_length(text)
        requests = [{"insertText": {
            "location": {"index": index, "tabId": selected.tab_id}, "text": text,
        }}]
        if format_profile == "persian":
            paragraph = markdown_module.paragraph_style_request(end_index, selected.tab_id)
            assert paragraph is not None
            paragraph["updateParagraphStyle"]["range"]["startIndex"] = index
            requests.extend([paragraph, {"updateTextStyle": {
                "range": {"startIndex": index, "endIndex": end_index, "tabId": selected.tab_id},
                "textStyle": {"weightedFontFamily": {"fontFamily": "Vazirmatn"}},
                "fields": "weightedFontFamily",
            }}])
        markdown_module._enforce_request_plan(requests)
        result: dict[str, object] = {
            "ok": True, "document_id": document_id, "document_url": metadata["webViewLink"],
            "tab_id": selected.tab_id, "position": position, "index": index,
            "inserted_utf16_length": end_index - index, "format_profile": format_profile,
            "applied": apply,
        }
        if not apply:
            return {**result, "revision_id": revision, "valid": True}
        after_revision = _service_batch_revision(
            self._client, document_id, requests, revision, retry_safe=False
        )
        after = _service_document(self._client, document_id)
        after_tab = select_tab(after, selected.tab_id)
        if (
            after["revisionId"] != after_revision or after_tab is None
            or _insertion_snapshot(_insertion_segments(after_tab.body)) != expected
            or _service_end_index(after_tab.body) != _service_end_index(selected.body) + utf16_length(text)
        ):
            raise DocsMCPError("verification_failed", "Google Docs insertion readback could not be verified.")
        if format_profile == "persian":
            _verify_persian_api(after_tab.body, start_index=index, end_index=end_index)
        return {
            **result, "before_revision_id": revision, "after_revision_id": after_revision,
            "verified": True, "formatting_verified": format_profile == "persian",
        }

    def edit_text(
        self,
        document: str,
        replacements: list[Replacement],
        expected_revision_id: str,
        tab_id: str | None = None,
        apply: bool = False,
    ) -> dict[str, object]:
        document_id = parse_document_id(document)
        revision_id = _validate_service_revision(expected_revision_id)
        metadata = _service_metadata(self._client, document_id)
        _require_native_document(metadata)
        if apply:
            result = apply_edits(
                client=self._client,
                document_id=document_id,
                replacements=replacements,
                tab_id=tab_id,
                expected_revision_id=revision_id,
            )
        else:
            result = preview_edits(
                client=self._client,
                document_id=document_id,
                replacements=replacements,
                tab_id=tab_id,
                expected_revision_id=revision_id,
            )
        return {
            "ok": True,
            **result,
            "document_url": metadata["webViewLink"],
            "verified": True,
        }
