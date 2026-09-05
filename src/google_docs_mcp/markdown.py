import hashlib
import json
import re
from dataclasses import dataclass

from . import client
from .client import DocsMCPError, validate_markdown


@dataclass(frozen=True)
class TextRange:
    start: int
    end: int


@dataclass(frozen=True)
class HeadingRange(TextRange):
    level: int


@dataclass(frozen=True)
class LinkRange(TextRange):
    url: str


@dataclass(frozen=True)
class InlineContent:
    text: str
    bold: tuple[TextRange, ...] = ()
    links: tuple[LinkRange, ...] = ()


@dataclass(frozen=True)
class TableBlock:
    marker: str
    rows: tuple[tuple[InlineContent, ...], ...]


@dataclass(frozen=True)
class DocumentModel:
    text: str
    headings: tuple[HeadingRange, ...]
    bold: tuple[TextRange, ...]
    links: tuple[LinkRange, ...]
    tables: tuple[TableBlock, ...]


_HEADING_RE = re.compile(r"^(#{1,6})[^\S\n]+(.*)$")
_SEPARATOR_RE = re.compile(r":?-{3,}:?")
_RESERVED_TABLE_MARKER_RE = re.compile(r"⟦TABLE-[0-9]{4,}⟧")
_RESERVED_TABLE_MARKER_MESSAGE = "Markdown contains a reserved table marker."
_INVALID_TABLE_MARKER_STATE_MESSAGE = "Markdown table marker state is invalid."
_INVALID_TABLE_MODEL_MESSAGE = "Markdown table structure is invalid."
_TABLE_READBACK_MISMATCH_MESSAGE = (
    "Google Docs table readback does not match the Markdown model."
)
_MAX_REPLACEMENT_REQUESTS = 10_000
_MAX_REPLACEMENT_PAYLOAD_BYTES = 8_000_000
_MAX_SEMANTIC_NODES = 100_000
_MAX_SEMANTIC_CHARS = 5_000_000
_MAX_SEMANTIC_HASH_BYTES = 8_000_000
_SEMANTIC_UNSUPPORTED_KINDS = (
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
    "tableOfContents",
    "table",
    "paragraph",
    "sectionBreak",
)
_TOO_MANY_FORMATTING_REQUESTS_MESSAGE = (
    "Markdown produces too many formatting requests."
)


def _reserved_table_marker_error() -> DocsMCPError:
    return DocsMCPError("invalid_markdown", _RESERVED_TABLE_MARKER_MESSAGE)


def _invalid_table_marker_state_error() -> DocsMCPError:
    return DocsMCPError("invalid_markdown", _INVALID_TABLE_MARKER_STATE_MESSAGE)


def _invalid_table_model_error() -> DocsMCPError:
    return DocsMCPError("invalid_markdown", _INVALID_TABLE_MODEL_MESSAGE)


def _table_readback_mismatch_error() -> DocsMCPError:
    return DocsMCPError("verification_failed", _TABLE_READBACK_MISMATCH_MESSAGE)


def _validate_table_marker_state(model: DocumentModel) -> None:
    text_markers = tuple(
        match.group(0) for match in _RESERVED_TABLE_MARKER_RE.finditer(model.text)
    )
    table_markers = tuple(table.marker for table in model.tables)
    if (
        any(
            not isinstance(marker, str)
            or _RESERVED_TABLE_MARKER_RE.fullmatch(marker) is None
            for marker in table_markers
        )
        or len(set(table_markers)) != len(table_markers)
        or text_markers != table_markers
    ):
        raise _invalid_table_marker_state_error()


def _too_many_formatting_requests_error() -> DocsMCPError:
    return DocsMCPError(
        "invalid_markdown", _TOO_MANY_FORMATTING_REQUESTS_MESSAGE
    )


def _enforce_request_count(count: int) -> None:
    if count > _MAX_REPLACEMENT_REQUESTS:
        raise _too_many_formatting_requests_error()


def _enforce_request_payload(requests: list[dict]) -> list[dict]:
    payload = json.dumps(
        requests, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    if len(payload) > _MAX_REPLACEMENT_PAYLOAD_BYTES:
        raise _too_many_formatting_requests_error()
    return requests


def _enforce_request_plan(requests: list[dict]) -> list[dict]:
    _enforce_request_count(len(requests))
    return _enforce_request_payload(requests)


def _validate_utf16_offset(text: str, codepoint_offset: object) -> None:
    if (
        not isinstance(codepoint_offset, int)
        or isinstance(codepoint_offset, bool)
        or not 0 <= codepoint_offset <= len(text)
    ):
        client.utf16_index(text, codepoint_offset)


def _utf16_boundary_map(text: str) -> list[int]:
    boundaries = [0]
    units = 0
    for character in text:
        units += len(character.encode("utf-16-le")) // 2
        boundaries.append(units)
    return boundaries


def _span_generates_request(span: TextRange) -> bool:
    if (
        not isinstance(span.start, int)
        or isinstance(span.start, bool)
        or not isinstance(span.end, int)
        or isinstance(span.end, bool)
    ):
        return True
    return span.end > span.start


def _inline_request_count(model: DocumentModel) -> int:
    return (
        sum(_span_generates_request(heading) for heading in model.headings)
        + sum(_span_generates_request(span) for span in model.bold)
        + sum(_span_generates_request(link) for link in model.links)
    )


def _heading_request_count(model: DocumentModel) -> int:
    return sum(_span_generates_request(heading) for heading in model.headings)


def _is_single_backtick(source: str, index: int) -> bool:
    return (
        source[index] == "`"
        and (index == 0 or source[index - 1] != "`")
        and (index + 1 == len(source) or source[index + 1] != "`")
    )


def _find_code_close(source: str, start: int, end: int) -> int | None:
    index = start + 1
    while index < end:
        if _is_single_backtick(source, index):
            return index
        index += 1
    return None


def _unescape(source: str) -> str:
    parts: list[str] = []
    index = 0
    while index < len(source):
        if source[index] == "\\" and index + 1 < len(source):
            parts.append(source[index + 1])
            index += 2
        else:
            parts.append(source[index])
            index += 1
    return "".join(parts)


def _match_link_at(
    source: str, start: int, end: int
) -> tuple[tuple[int, int, str, int] | None, int]:
    if source[start] != "[" or source.startswith("[[", start):
        return None, start + 1

    index = start + 1
    while index < end:
        if source[index] == "\\" and index + 1 < end:
            index += 2
            continue
        if source[index] == "`" and _is_single_backtick(source, index):
            code_close = _find_code_close(source, index, end)
            if code_close is not None:
                index = code_close + 1
                continue
        if source[index] != "]":
            index += 1
            continue
        if index + 1 >= end or source[index + 1] != "(":
            return None, index + 1

        url_start = index + 2
        url_end = url_start
        while url_end < end:
            if source[url_end] == "\\" and url_end + 1 < end:
                url_end += 2
                continue
            if source[url_end] == ")":
                raw_url = source[url_start:url_end]
                url = _unescape(raw_url)
                if url.startswith("http://"):
                    suffix = url[len("http://") :]
                elif url.startswith("https://"):
                    suffix = url[len("https://") :]
                else:
                    return None, url_end + 1
                if not suffix or any(character.isspace() for character in url):
                    return None, url_end + 1
                return (start + 1, index, url, url_end + 1), url_end + 1
            url_end += 1
        return None, end
    return None, end


def _find_bold_close(source: str, start: int, end: int) -> int | None:
    index = start + 2
    link_retry_from = index
    while index + 1 < end:
        if source[index] == "\\" and index + 1 < end:
            index += 2
            continue
        if source[index] == "`" and _is_single_backtick(source, index):
            code_close = _find_code_close(source, index, end)
            if code_close is not None:
                index = code_close + 1
                continue
        if source.startswith("**", index):
            return index
        if source[index] == "[" and index >= link_retry_from:
            link, link_retry_from = _match_link_at(source, index, end)
            if link is not None:
                index = link[3]
                continue
        index += 1
    return None


def _first_unescaped(source: str, delimiter: str) -> int | None:
    index = 0
    while index < len(source):
        if source[index] == "\\" and index + 1 < len(source):
            index += 2
            continue
        if source[index] == delimiter:
            return index
        index += 1
    return None


def _match_obsidian_at(source: str, start: int, end: int) -> tuple[str, int] | None:
    if not source.startswith("[[", start):
        return None

    index = start + 2
    while index + 1 < end:
        if source[index] == "\\" and index + 1 < end:
            index += 2
            continue
        if source.startswith("]]", index):
            target = source[start + 2 : index]
            pipe = _first_unescaped(target, "|")
            if pipe is not None:
                display = target[pipe + 1 :]
            else:
                section = _first_unescaped(target, "#")
                display = target[section + 1 :] if section is not None else target
            return _unescape(display), index + 2
        index += 1
    return None


def _parse_inline(source: str) -> InlineContent:
    output_parts: list[str] = []
    output_length = 0
    bold: list[TextRange] = []
    links: list[LinkRange] = []
    tasks: list[tuple[str, int, int, str]] = [
        ("segment", 0, len(source), ""),
    ]

    while tasks:
        kind, start, end, payload = tasks.pop()
        if kind == "bold":
            bold.append(TextRange(start, output_length))
            continue
        if kind == "link":
            links.append(LinkRange(start, output_length, payload))
            continue

        index = start
        link_retry_from = index
        obsidian_can_match = True
        while index < end:
            character = source[index]
            if character == "\\":
                if index + 1 < end:
                    output_parts.append(source[index + 1])
                    output_length += 1
                    index += 2
                else:
                    output_parts.append(character)
                    output_length += 1
                    index += 1
                continue

            if character == "`" and _is_single_backtick(source, index):
                code_close = _find_code_close(source, index, end)
                if code_close is not None:
                    literal = source[index + 1 : code_close]
                    output_parts.append(literal)
                    output_length += len(literal)
                    index = code_close + 1
                    continue

            if obsidian_can_match and source.startswith("[[", index):
                obsidian = _match_obsidian_at(source, index, end)
                if obsidian is not None:
                    display, after = obsidian
                    output_parts.append(display)
                    output_length += len(display)
                    index = after
                    continue
                obsidian_can_match = False

            if source.startswith("**", index):
                bold_close = _find_bold_close(source, index, end)
                if bold_close is not None:
                    after = bold_close + 2
                    if after < end:
                        tasks.append(("segment", after, end, ""))
                    tasks.append(("bold", output_length, 0, ""))
                    if index + 2 < bold_close:
                        tasks.append(("segment", index + 2, bold_close, ""))
                    break
                output_parts.append("**")
                output_length += 2
                index += 2
                continue

            if character == "[" and index >= link_retry_from:
                link, link_retry_from = _match_link_at(source, index, end)
                if link is not None:
                    label_start, label_end, url, after = link
                    if after < end:
                        tasks.append(("segment", after, end, ""))
                    tasks.append(("link", output_length, 0, url))
                    if label_start < label_end:
                        tasks.append(("segment", label_start, label_end, ""))
                    break

            output_parts.append(character)
            output_length += 1
            index += 1

    text = "".join(output_parts)
    if _RESERVED_TABLE_MARKER_RE.search(text) is not None:
        raise _reserved_table_marker_error()

    return InlineContent(
        text=text,
        bold=tuple(sorted(bold, key=lambda item: (item.start, item.end))),
        links=tuple(sorted(links, key=lambda item: (item.start, item.end, item.url))),
    )


def _split_table_row(line: str) -> tuple[str, ...]:
    cells: list[str] = []
    current: list[str] = []
    index = 1
    while index < len(line):
        if line[index] == "\\" and index + 1 < len(line):
            current.extend((line[index], line[index + 1]))
            index += 2
            continue
        if line[index] == "|":
            cells.append("".join(current).strip())
            current = []
            index += 1
            continue
        current.append(line[index])
        index += 1

    if current or not line.endswith("|"):
        cells.append("".join(current).strip())
    return tuple(cells)


def _table_at(
    lines: list[str], start: int
) -> tuple[tuple[tuple[InlineContent, ...], ...], int] | None:
    if (
        not lines[start].startswith("|")
        or start + 1 >= len(lines)
        or not lines[start + 1].startswith("|")
    ):
        return None

    header = _split_table_row(lines[start])
    separator = _split_table_row(lines[start + 1])
    if (
        not header
        or len(header) != len(separator)
        or not separator
        or any(_SEPARATOR_RE.fullmatch(cell) is None for cell in separator)
    ):
        return None

    rows: list[tuple[InlineContent, ...]] = [
        tuple(_parse_inline(cell) for cell in header)
    ]
    index = start + 2
    while index < len(lines) and lines[index].startswith("|"):
        rows.append(tuple(_parse_inline(cell) for cell in _split_table_row(lines[index])))
        index += 1
    return tuple(rows), index


def _logical_lines(source: str) -> list[str]:
    if not source:
        return []
    lines = source.split("\n")
    if source.endswith("\n"):
        lines.pop()
    return lines


def parse_markdown(markdown: str) -> DocumentModel:
    source = validate_markdown(markdown)
    if _RESERVED_TABLE_MARKER_RE.search(source) is not None:
        raise _reserved_table_marker_error()

    normalized = source.replace("\r\n", "\n").replace("\r", "\n")
    lines = _logical_lines(normalized)
    if lines and lines[0].strip() == "---":
        closing_frontmatter = next(
            (index for index in range(1, len(lines)) if lines[index].strip() == "---"),
            None,
        )
        if closing_frontmatter is not None:
            lines = lines[closing_frontmatter + 1 :]

    output_parts: list[str] = []
    output_offset = 0
    headings: list[HeadingRange] = []
    bold: list[TextRange] = []
    links: list[LinkRange] = []
    tables: list[TableBlock] = []

    line_index = 0
    while line_index < len(lines):
        table = _table_at(lines, line_index)
        if table is not None:
            rows, line_index = table
            marker = f"⟦TABLE-{len(tables) + 1:04d}⟧"
            tables.append(TableBlock(marker, rows))
            output_parts.extend((marker, "\n"))
            output_offset += len(marker) + 1
            continue

        line = lines[line_index]
        heading_match = _HEADING_RE.fullmatch(line)
        heading_level: int | None = None
        if heading_match is not None:
            heading_level = len(heading_match.group(1))
            line = heading_match.group(2)
        elif line.startswith("- [ ] "):
            line = "☐ " + line[len("- [ ] ") :]
        elif line.startswith("- [x] ") or line.startswith("- [X] "):
            line = "☑ " + line[len("- [x] ") :]
        elif line.startswith("- "):
            line = "• " + line[len("- ") :]

        inline = _parse_inline(line)
        if heading_level is not None:
            headings.append(
                HeadingRange(output_offset, output_offset + len(inline.text), heading_level)
            )
        bold.extend(
            TextRange(output_offset + span.start, output_offset + span.end)
            for span in inline.bold
        )
        links.extend(
            LinkRange(
                output_offset + span.start,
                output_offset + span.end,
                span.url,
            )
            for span in inline.links
        )
        output_parts.extend((inline.text, "\n"))
        output_offset += len(inline.text) + 1
        line_index += 1

    return DocumentModel(
        text="".join(output_parts),
        headings=tuple(headings),
        bold=tuple(bold),
        links=tuple(links),
        tables=tuple(tables),
    )


@dataclass
class _SemanticBudget:
    nodes: int = 0
    chars: int = 0

    def add_nodes(self, count: int = 1) -> None:
        if count < 0 or count > _MAX_SEMANTIC_NODES - self.nodes:
            raise ValueError
        self.nodes += count

    def add_text(self, text: str) -> None:
        if not isinstance(text, str) or len(text) > _MAX_SEMANTIC_CHARS - self.chars:
            raise ValueError
        self.chars += len(text)


@dataclass
class _SemanticTextRun:
    parts: list[str]
    bold: bool
    link: str | None


_SEMANTIC_RUN = _SemanticTextRun | dict[str, str]


def _semantic_verification_error() -> DocsMCPError:
    return DocsMCPError(
        "verification_failed",
        "Google Docs semantic readback could not be verified.",
    )


def _append_semantic_text(
    runs: list[_SEMANTIC_RUN],
    text: str,
    *,
    bold: bool,
    link: str | None,
    budget: _SemanticBudget,
    count_text: bool = True,
) -> None:
    if not isinstance(text, str) or not isinstance(bold, bool):
        raise TypeError
    if link is not None and (not isinstance(link, str) or not link):
        raise TypeError
    if not text:
        return
    if count_text:
        budget.add_text(text)
    if (
        runs
        and isinstance(runs[-1], _SemanticTextRun)
        and runs[-1].bold is bold
        and runs[-1].link == link
    ):
        runs[-1].parts.append(text)
        return
    budget.add_nodes()
    runs.append(_SemanticTextRun([text], bold, link))


def _append_unsupported_run(
    runs: list[_SEMANTIC_RUN], marker: str, budget: _SemanticBudget
) -> None:
    budget.add_nodes()
    runs.append({"unsupported": marker})


def _finish_semantic_runs(runs: list[_SEMANTIC_RUN]) -> list[dict]:
    result: list[dict] = []
    for run in runs:
        if isinstance(run, _SemanticTextRun):
            result.append(
                {
                    "text": "".join(run.parts),
                    "bold": run.bold,
                    "link": run.link,
                }
            )
        else:
            result.append(dict(run))
    return result


def _unsupported_marker(node: dict, scope: str) -> str:
    for kind in _SEMANTIC_UNSUPPORTED_KINDS:
        if kind in node:
            return f"{scope}:{kind}"
    return f"{scope}:unknown"


def _styled_semantic_runs(
    text: str,
    bold_spans: tuple[TextRange, ...],
    link_spans: tuple[LinkRange, ...],
    budget: _SemanticBudget,
    *,
    base: int = 0,
) -> list[dict]:
    if (
        not isinstance(text, str)
        or not isinstance(bold_spans, tuple)
        or not isinstance(link_spans, tuple)
        or not isinstance(base, int)
        or isinstance(base, bool)
    ):
        raise TypeError

    boundaries = {0, len(text)}
    bold_events: dict[int, int] = {}
    link_starts: dict[int, list[str]] = {}
    link_ends: dict[int, list[str]] = {}
    for span in bold_spans:
        if (
            type(span) is not TextRange
            or not isinstance(span.start, int)
            or isinstance(span.start, bool)
            or not isinstance(span.end, int)
            or isinstance(span.end, bool)
            or not base <= span.start <= span.end <= base + len(text)
        ):
            raise TypeError
        budget.add_nodes()
        left = span.start - base
        right = span.end - base
        boundaries.update((left, right))
        if right > left:
            bold_events[left] = bold_events.get(left, 0) + 1
            bold_events[right] = bold_events.get(right, 0) - 1

    for span in link_spans:
        if (
            type(span) is not LinkRange
            or not isinstance(span.start, int)
            or isinstance(span.start, bool)
            or not isinstance(span.end, int)
            or isinstance(span.end, bool)
            or not isinstance(span.url, str)
            or not span.url
            or not base <= span.start <= span.end <= base + len(text)
        ):
            raise TypeError
        budget.add_text(span.url)
        budget.add_nodes()
        left = span.start - base
        right = span.end - base
        boundaries.update((left, right))
        if right > left:
            link_starts.setdefault(left, []).append(span.url)
            link_ends.setdefault(right, []).append(span.url)

    active_bold = 0
    active_links: dict[str, int] = {}
    runs: list[_SEMANTIC_RUN] = []
    ordered = sorted(boundaries)
    for left, right in zip(ordered, ordered[1:]):
        active_bold += bold_events.get(left, 0)
        if active_bold < 0:
            raise ValueError
        for url in link_ends.get(left, ()):
            remaining = active_links.get(url, 0) - 1
            if remaining < 0:
                raise ValueError
            if remaining == 0:
                active_links.pop(url, None)
            else:
                active_links[url] = remaining
        for url in link_starts.get(left, ()):
            active_links[url] = active_links.get(url, 0) + 1
        if len(active_links) > 1:
            raise ValueError
        _append_semantic_text(
            runs,
            text[left:right],
            bold=active_bold > 0,
            link=next(iter(active_links), None),
            budget=budget,
        )
    return _finish_semantic_runs(runs)


def _semantic_lines(text: str) -> list[tuple[str, int, int]]:
    lines: list[tuple[str, int, int]] = []
    offset = 0
    for chunk in text.splitlines(keepends=True):
        line = chunk[:-1] if chunk.endswith("\n") else chunk
        lines.append((line, offset, offset + len(line)))
        offset += len(chunk)
    return lines


def _assign_model_spans(
    lines: list[tuple[str, int, int]],
    spans: tuple[TextRange, ...],
    span_type: type[TextRange],
) -> list[list[TextRange]]:
    if not isinstance(spans, tuple):
        raise TypeError
    validated: list[TextRange] = []
    for span in spans:
        if (
            type(span) is not span_type
            or not isinstance(span.start, int)
            or isinstance(span.start, bool)
            or not isinstance(span.end, int)
            or isinstance(span.end, bool)
            or not 0 <= span.start <= span.end
        ):
            raise TypeError
        validated.append(span)

    assigned: list[list[TextRange]] = [[] for _ in lines]
    line_index = 0
    for span in sorted(validated, key=lambda item: (item.start, item.end)):
        while line_index < len(lines) and span.start > lines[line_index][2]:
            line_index += 1
        if line_index >= len(lines):
            raise ValueError
        _, start, end = lines[line_index]
        if span.start < start or span.end > end:
            raise ValueError
        assigned[line_index].append(span)
    return assigned


def _candidate_table_semantic(
    table: TableBlock, budget: _SemanticBudget
) -> dict:
    if type(table) is not TableBlock or not isinstance(table.rows, tuple) or not table.rows:
        raise TypeError
    if any(not isinstance(row, tuple) or not row for row in table.rows):
        raise TypeError
    columns = max(len(row) for row in table.rows)
    budget.add_nodes(1 + len(table.rows) + (len(table.rows) * columns))
    padded = _pad_table_rows(table.rows)
    return {
        "type": "table",
        "rows": [
            [
                {
                    "runs": _styled_semantic_runs(
                        cell.text,
                        cell.bold,
                        cell.links,
                        budget,
                    )
                }
                for cell in row
            ]
            for row in padded
        ],
    }


def _candidate_semantic(model: DocumentModel, profile: str) -> dict:
    if profile not in {"plain", "persian"}:
        raise ValueError
    if (
        type(model) is not DocumentModel
        or not isinstance(model.text, str)
        or len(model.text) > _MAX_SEMANTIC_CHARS
        or not isinstance(model.headings, tuple)
        or not isinstance(model.bold, tuple)
        or not isinstance(model.links, tuple)
        or not isinstance(model.tables, tuple)
    ):
        raise TypeError

    budget = _SemanticBudget()
    for collection in (model.headings, model.bold, model.links, model.tables):
        budget.add_nodes(len(collection))
    _validate_table_marker_state(model)

    lines = _semantic_lines(model.text)
    budget.add_nodes(len(lines))
    headings_by_line = _assign_model_spans(lines, model.headings, HeadingRange)
    bold_by_line = _assign_model_spans(lines, model.bold, TextRange)
    links_by_line = _assign_model_spans(lines, model.links, LinkRange)

    tables: dict[str, TableBlock] = {}
    for table in model.tables:
        if type(table) is not TableBlock or not isinstance(table.marker, str):
            raise TypeError
        tables[table.marker] = table

    blocks: list[dict] = []
    for index, (text, start, end) in enumerate(lines):
        heading_spans = headings_by_line[index]
        bold_spans = bold_by_line[index]
        link_spans = links_by_line[index]
        table = tables.get(text)
        if table is not None:
            if heading_spans or bold_spans or link_spans:
                raise ValueError
            blocks.append(_candidate_table_semantic(table, budget))
            continue

        if len(heading_spans) > 1:
            raise ValueError
        heading: int | None = None
        if heading_spans:
            heading_span = heading_spans[0]
            if (
                type(heading_span) is not HeadingRange
                or heading_span.start != start
                or heading_span.end != end
                or not isinstance(heading_span.level, int)
                or isinstance(heading_span.level, bool)
                or not 1 <= heading_span.level <= 6
            ):
                raise TypeError
            heading = heading_span.level
            if profile == "persian" and text:
                bold_spans = [*bold_spans, TextRange(start, end)]
        if not text:
            if bold_spans or link_spans:
                raise ValueError
            continue

        typed_links = tuple(
            span for span in link_spans if type(span) is LinkRange
        )
        if len(typed_links) != len(link_spans):
            raise TypeError
        blocks.append(
            {
                "type": "paragraph",
                "heading": heading,
                "runs": _styled_semantic_runs(
                    text,
                    tuple(bold_spans),
                    typed_links,
                    budget,
                    base=start,
                ),
            }
        )
    return {"schema": 2, "blocks": blocks}


def candidate_semantic(model: DocumentModel, profile: str = "plain") -> dict:
    result: dict | None = None
    failed = False
    try:
        result = _candidate_semantic(model, profile)
    except Exception:
        failed = True
    if failed or result is None:
        raise _semantic_verification_error()
    return result


def _remote_text_runs(
    paragraph: dict, budget: _SemanticBudget, *, scope: str
) -> list[dict]:
    if not isinstance(paragraph, dict):
        raise TypeError
    elements = paragraph.get("elements", [])
    if not isinstance(elements, list):
        raise TypeError

    runs: list[_SEMANTIC_RUN] = []
    for element in elements:
        budget.add_nodes()
        if not isinstance(element, dict):
            raise TypeError
        payload_keys = set(element) - {"startIndex", "endIndex"}
        if payload_keys != {"textRun"}:
            _append_unsupported_run(
                runs, _unsupported_marker(element, scope), budget
            )
            continue

        text_run = element["textRun"]
        if not isinstance(text_run, dict):
            raise TypeError
        content = text_run.get("content")
        style = text_run.get("textStyle", {})
        if not isinstance(content, str) or not isinstance(style, dict):
            raise TypeError
        bold = style.get("bold", False)
        if not isinstance(bold, bool):
            raise TypeError
        link_value = style.get("link")
        link: str | None = None
        if link_value is not None:
            if not isinstance(link_value, dict):
                raise TypeError
            link = link_value.get("url")
            if not isinstance(link, str) or not link:
                raise TypeError
            budget.add_text(link)
        _append_semantic_text(
            runs,
            content,
            bold=bold,
            link=link,
            budget=budget,
        )

    result = _finish_semantic_runs(runs)
    if result and "text" in result[-1] and result[-1]["text"].endswith("\n"):
        result[-1]["text"] = result[-1]["text"][:-1]
        if not result[-1]["text"]:
            result.pop()
    return result


def _remote_heading(paragraph: dict) -> int | str | None:
    style = paragraph.get("paragraphStyle", {})
    if not isinstance(style, dict):
        raise TypeError
    named_style = style.get("namedStyleType", "NORMAL_TEXT")
    if not isinstance(named_style, str):
        raise TypeError
    if named_style == "NORMAL_TEXT":
        return None
    if named_style.startswith("HEADING_"):
        level_text = named_style.removeprefix("HEADING_")
        if level_text in {str(level) for level in range(1, 7)}:
            return int(level_text)
    return named_style


def _append_finished_runs(
    target: list[_SEMANTIC_RUN],
    source: list[dict],
    budget: _SemanticBudget,
) -> None:
    for run in source:
        if set(run) == {"text", "bold", "link"}:
            _append_semantic_text(
                target,
                run["text"],
                bold=run["bold"],
                link=run["link"],
                budget=budget,
                count_text=False,
            )
        elif set(run) == {"unsupported"} and isinstance(
            run["unsupported"], str
        ):
            budget.add_nodes()
            target.append({"unsupported": run["unsupported"]})
        else:
            raise TypeError


def _remote_cell_semantic(cell: dict, budget: _SemanticBudget) -> dict:
    if not isinstance(cell, dict):
        raise TypeError
    content = cell.get("content", [])
    if not isinstance(content, list):
        raise TypeError

    combined: list[_SEMANTIC_RUN] = []
    has_segment = False
    for node in content:
        budget.add_nodes()
        if not isinstance(node, dict):
            raise TypeError
        payload_keys = set(node) - {"startIndex", "endIndex"}
        if payload_keys == {"paragraph"}:
            segment = _remote_text_runs(
                node["paragraph"], budget, scope="paragraph"
            )
        else:
            segment = [{"unsupported": _unsupported_marker(node, "cell")}]
        if not segment:
            continue
        if has_segment:
            _append_semantic_text(
                combined,
                "\n",
                bold=False,
                link=None,
                budget=budget,
            )
        _append_finished_runs(combined, segment, budget)
        has_segment = True
    return {"runs": _finish_semantic_runs(combined)}


def _remote_table_semantic(table: dict, budget: _SemanticBudget) -> dict:
    if not isinstance(table, dict):
        raise TypeError
    rows = table.get("tableRows", [])
    if not isinstance(rows, list):
        raise TypeError
    semantic_rows: list[list[dict]] = []
    for row in rows:
        budget.add_nodes()
        if not isinstance(row, dict):
            raise TypeError
        cells = row.get("tableCells", [])
        if not isinstance(cells, list):
            raise TypeError
        semantic_row: list[dict] = []
        for cell in cells:
            budget.add_nodes()
            semantic_row.append(_remote_cell_semantic(cell, budget))
        semantic_rows.append(semantic_row)
    return {"type": "table", "rows": semantic_rows}


def _remote_semantic(body: dict) -> dict:
    if not isinstance(body, dict):
        raise TypeError
    content = body.get("content")
    if not isinstance(content, list):
        raise TypeError

    budget = _SemanticBudget()
    blocks: list[dict] = []
    for node in content:
        budget.add_nodes()
        if not isinstance(node, dict):
            raise TypeError
        payload_keys = set(node) - {"startIndex", "endIndex"}
        if payload_keys == {"sectionBreak"}:
            continue
        if payload_keys == {"paragraph"}:
            paragraph = node["paragraph"]
            runs = _remote_text_runs(paragraph, budget, scope="paragraph")
            if not runs:
                continue
            blocks.append(
                {
                    "type": "paragraph",
                    "heading": _remote_heading(paragraph),
                    "runs": runs,
                }
            )
            continue
        if payload_keys == {"table"}:
            blocks.append(_remote_table_semantic(node["table"], budget))
            continue
        budget.add_nodes()
        blocks.append(
            {
                "type": "unsupported",
                "marker": _unsupported_marker(node, "structural"),
            }
        )
    return {"schema": 2, "blocks": blocks}


def remote_semantic(body: dict) -> dict:
    result: dict | None = None
    failed = False
    try:
        result = _remote_semantic(body)
    except Exception:
        failed = True
    if failed or result is None:
        raise _semantic_verification_error()
    return result


def semantic_sha256(value: dict) -> str:
    digest: str | None = None
    failed = False
    try:
        if not isinstance(value, dict):
            raise TypeError
        encoder = json.JSONEncoder(
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        hasher = hashlib.sha256()
        payload_size = 0
        for chunk in encoder.iterencode(value):
            if len(chunk) > _MAX_SEMANTIC_HASH_BYTES - payload_size:
                raise ValueError
            encoded = chunk.encode("utf-8")
            if len(encoded) > _MAX_SEMANTIC_HASH_BYTES - payload_size:
                raise ValueError
            hasher.update(encoded)
            payload_size += len(encoded)
        digest = hasher.hexdigest()
    except Exception:
        failed = True
    if failed or digest is None:
        raise _semantic_verification_error()
    return digest


def _pad_table_rows(
    rows: tuple[tuple[InlineContent, ...], ...],
) -> tuple[tuple[InlineContent, ...], ...]:
    if (
        not isinstance(rows, tuple)
        or not rows
        or any(not isinstance(row, tuple) or not row for row in rows)
        or any(not isinstance(cell, InlineContent) for row in rows for cell in row)
    ):
        raise _invalid_table_model_error()
    columns = max(len(row) for row in rows)
    return tuple(
        row + (InlineContent(""),) * (columns - len(row)) for row in rows
    )


def _validate_table_phase_request_counts(
    model: DocumentModel, profile: str = "plain"
) -> None:
    _enforce_request_count(2 * len(model.tables))
    cell_text_count = 0
    cell_style_count = 0
    for table in model.tables:
        for row in _pad_table_rows(table.rows):
            for cell in row:
                cell_text_count += bool(cell.text)
                if profile == "persian":
                    cell_style_count += 2
                cell_style_count += sum(
                    _span_generates_request(span)
                    for span in (*cell.bold, *cell.links)
                )
    _enforce_request_count(cell_text_count)
    _enforce_request_count(cell_style_count)


def _table_cell_entries(
    table_node: dict,
    rows: tuple[tuple[InlineContent, ...], ...],
) -> tuple[str, tuple[tuple[int, InlineContent], ...]]:
    if not isinstance(table_node, dict):
        raise _table_readback_mismatch_error()
    tab_id = table_node.get("tabId")
    table = table_node.get("table")
    padded_rows = _pad_table_rows(rows)
    if not isinstance(tab_id, str) or not tab_id or not isinstance(table, dict):
        raise _table_readback_mismatch_error()
    table_rows = table.get("tableRows")
    if not isinstance(table_rows, list) or len(table_rows) != len(padded_rows):
        raise _table_readback_mismatch_error()

    entries: list[tuple[int, InlineContent]] = []
    seen_indexes: set[int] = set()
    for row_node, model_row in zip(table_rows, padded_rows, strict=True):
        if not isinstance(row_node, dict):
            raise _table_readback_mismatch_error()
        cells = row_node.get("tableCells")
        if not isinstance(cells, list) or len(cells) != len(model_row):
            raise _table_readback_mismatch_error()
        for cell_node, cell in zip(cells, model_row, strict=True):
            if not isinstance(cell_node, dict):
                raise _table_readback_mismatch_error()
            content = cell_node.get("content")
            if not isinstance(content, list):
                raise _table_readback_mismatch_error()
            paragraph_indexes = [
                element.get("startIndex")
                for element in content
                if isinstance(element, dict) and "paragraph" in element
            ]
            if len(paragraph_indexes) != 1:
                raise _table_readback_mismatch_error()
            start_index = paragraph_indexes[0]
            if (
                not isinstance(start_index, int)
                or isinstance(start_index, bool)
                or start_index < 1
                or start_index in seen_indexes
            ):
                raise _table_readback_mismatch_error()
            seen_indexes.add(start_index)
            entries.append((start_index, cell))
    return tab_id, tuple(entries)


def insert_table_structure_requests(
    model: DocumentModel, tab_id: str
) -> list[dict]:
    _validate_table_marker_state(model)
    request_count = 2 * len(model.tables)
    _enforce_request_count(request_count)
    if not model.tables:
        return _enforce_request_payload([])

    marker_matches = tuple(_RESERVED_TABLE_MARKER_RE.finditer(model.text))
    boundaries = _utf16_boundary_map(model.text)
    tables: list[
        tuple[int, str, tuple[tuple[InlineContent, ...], ...]]
    ] = []
    for match, table in zip(marker_matches, model.tables, strict=True):
        padded_rows = _pad_table_rows(table.rows)
        tables.append((1 + boundaries[match.start()], table.marker, padded_rows))

    requests: list[dict] = []
    for index, marker, rows in sorted(tables, key=lambda item: item[0], reverse=True):
        requests.extend(
            (
                {
                    "deleteContentRange": {
                        "range": {
                            "startIndex": index,
                            "endIndex": index + client.utf16_length(marker),
                            "tabId": tab_id,
                        }
                    }
                },
                {
                    "insertTable": {
                        "rows": len(rows),
                        "columns": len(rows[0]),
                        "location": {"index": index, "tabId": tab_id},
                    }
                },
            )
        )
    return _enforce_request_payload(requests)


def table_cell_insert_requests(
    table_node: dict,
    rows: tuple[tuple[InlineContent, ...], ...],
) -> list[dict]:
    tab_id, entries = _table_cell_entries(table_node, rows)
    request_count = sum(bool(cell.text) for _, cell in entries)
    _enforce_request_count(request_count)
    requests = [
        {
            "insertText": {
                "location": {"index": index, "tabId": tab_id},
                "text": cell.text,
            }
        }
        for index, cell in sorted(entries, key=lambda item: item[0], reverse=True)
        if cell.text
    ]
    return _enforce_request_payload(requests)


def table_cell_style_requests(
    table_node: dict,
    rows: tuple[tuple[InlineContent, ...], ...],
    profile: str = "plain",
) -> list[dict]:
    if profile not in {"persian", "plain"}:
        raise ValueError("Unsupported table profile.")
    tab_id, entries = _table_cell_entries(table_node, rows)
    request_count = sum(
        sum(_span_generates_request(span) for span in cell.bold)
        + sum(_span_generates_request(link) for link in cell.links)
        + (2 if profile == "persian" else 0)
        for _, cell in entries
    )
    _enforce_request_count(request_count)

    requests: list[dict] = []
    for base_index, cell in entries:
        for span in (*cell.bold, *cell.links):
            if not _span_generates_request(span):
                continue
            _validate_utf16_offset(cell.text, span.start)
            _validate_utf16_offset(cell.text, span.end)
        if profile == "persian":
            # Include the paragraph terminator so empty/padded cells get a
            # nonempty range too. These indexes come from post-insertion readback.
            cell_end = base_index + client.utf16_length(cell.text) + 1
            paragraph = paragraph_style_request(cell_end, tab_id)
            assert paragraph is not None
            paragraph["updateParagraphStyle"]["range"]["startIndex"] = base_index
            requests.append(paragraph)
            requests.append(
                {
                    "updateTextStyle": {
                        "range": {
                            "startIndex": base_index,
                            "endIndex": cell_end,
                            "tabId": tab_id,
                        },
                        "textStyle": {
                            "weightedFontFamily": {"fontFamily": "Vazirmatn"}
                        },
                        "fields": "weightedFontFamily",
                    }
                }
            )
        if not cell.bold and not cell.links:
            continue
        boundaries = _utf16_boundary_map(cell.text)
        for span in cell.bold:
            if span.end <= span.start:
                continue
            requests.append(
                {
                    "updateTextStyle": {
                        "range": {
                            "startIndex": base_index + boundaries[span.start],
                            "endIndex": base_index + boundaries[span.end],
                            "tabId": tab_id,
                        },
                        "textStyle": {"bold": True},
                        "fields": "bold",
                    }
                }
            )
        for link in cell.links:
            if link.end <= link.start:
                continue
            requests.append(
                {
                    "updateTextStyle": {
                        "range": {
                            "startIndex": base_index + boundaries[link.start],
                            "endIndex": base_index + boundaries[link.end],
                            "tabId": tab_id,
                        },
                        "textStyle": {"link": {"url": link.url}},
                        "fields": "link",
                    }
                }
            )
    return _enforce_request_payload(requests)


def inline_style_requests(model: DocumentModel, tab_id: str) -> list[dict]:
    request_count = _inline_request_count(model)
    _enforce_request_count(request_count)

    for span in (*model.headings, *model.bold, *model.links):
        if not _span_generates_request(span):
            continue
        _validate_utf16_offset(model.text, span.start)
        _validate_utf16_offset(model.text, span.end)

    requests: list[dict] = []
    if request_count == 0:
        return _enforce_request_payload(requests)

    boundaries = _utf16_boundary_map(model.text)
    for heading in model.headings:
        if heading.end <= heading.start:
            continue
        requests.append(
            {
                "updateParagraphStyle": {
                    "range": {
                        "startIndex": 1 + boundaries[heading.start],
                        "endIndex": 1 + boundaries[heading.end],
                        "tabId": tab_id,
                    },
                    "paragraphStyle": {
                        "namedStyleType": f"HEADING_{heading.level}"
                    },
                    "fields": "namedStyleType",
                }
            }
        )

    for span in model.bold:
        if span.end <= span.start:
            continue
        requests.append(
            {
                "updateTextStyle": {
                    "range": {
                        "startIndex": 1 + boundaries[span.start],
                        "endIndex": 1 + boundaries[span.end],
                        "tabId": tab_id,
                    },
                    "textStyle": {"bold": True},
                    "fields": "bold",
                }
            }
        )

    for link in model.links:
        if link.end <= link.start:
            continue
        requests.append(
            {
                "updateTextStyle": {
                    "range": {
                        "startIndex": 1 + boundaries[link.start],
                        "endIndex": 1 + boundaries[link.end],
                        "tabId": tab_id,
                    },
                    "textStyle": {"link": {"url": link.url}},
                    "fields": "link",
                }
            }
        )

    return _enforce_request_payload(requests)


def paragraph_style_request(end_index: int, tab_id: str) -> dict | None:
    if end_index <= 1:
        return None
    return {
        "updateParagraphStyle": {
            "range": {
                "startIndex": 1,
                "endIndex": end_index,
                "tabId": tab_id,
            },
            "paragraphStyle": {
                "direction": "RIGHT_TO_LEFT",
                "alignment": "END",
                "indentStart": {"magnitude": 0, "unit": "PT"},
                "indentEnd": {"magnitude": 0, "unit": "PT"},
            },
            "fields": "direction,alignment,indentStart,indentEnd",
        }
    }


def heading_font_requests(model: DocumentModel, tab_id: str) -> list[dict]:
    request_count = _heading_request_count(model)
    _enforce_request_count(request_count)

    for heading in model.headings:
        if not _span_generates_request(heading):
            continue
        _validate_utf16_offset(model.text, heading.start)
        _validate_utf16_offset(model.text, heading.end)

    requests: list[dict] = []
    if request_count == 0:
        return _enforce_request_payload(requests)

    boundaries = _utf16_boundary_map(model.text)
    for heading in model.headings:
        if heading.end <= heading.start:
            continue
        requests.append(
            {
                "updateTextStyle": {
                    "range": {
                        "startIndex": 1 + boundaries[heading.start],
                        "endIndex": 1 + boundaries[heading.end],
                        "tabId": tab_id,
                    },
                    "textStyle": {
                        "weightedFontFamily": {"fontFamily": "Vazirmatn"},
                        "bold": True,
                    },
                    "fields": "weightedFontFamily,bold",
                }
            }
        )
    return _enforce_request_payload(requests)


def replacement_requests(
    model: DocumentModel,
    end_index: int,
    tab_id: str,
    profile: str = "persian",
) -> list[dict]:
    if profile not in {"persian", "plain"}:
        raise ValueError("Unsupported replacement profile.")
    if (
        not isinstance(end_index, int)
        or isinstance(end_index, bool)
        or end_index < 2
    ):
        raise ValueError("Invalid document end index.")
    _validate_table_marker_state(model)

    insert_text = model.text[:-1] if model.text.endswith("\n") else model.text
    inline_count = _inline_request_count(model)
    request_count = (
        (1 if end_index > 2 else 0)
        + (1 if insert_text else 0)
        + inline_count
        + (2 if model.text else 0)
    )
    if profile == "persian" and model.text:
        request_count += 2 + _heading_request_count(model)
    _enforce_request_count(request_count)
    inline_requests = inline_style_requests(model, tab_id)

    requests: list[dict] = []
    if end_index > 2:
        requests.append(
            {
                "deleteContentRange": {
                    "range": {
                        "startIndex": 1,
                        "endIndex": end_index - 1,
                        "tabId": tab_id,
                    }
                }
            }
        )

    if not model.text:
        return _enforce_request_payload(requests)

    if insert_text:
        requests.append(
            {
                "insertText": {
                    "location": {"index": 1, "tabId": tab_id},
                    "text": insert_text,
                }
            }
        )
    # Named styles can reset paragraph/font; font changes can reset bold.
    # Apply named styles first and explicit emphasis only after final defaults.
    content_end = 1 + client.utf16_length(model.text)
    content_range = {"startIndex": 1, "endIndex": content_end, "tabId": tab_id}
    requests.append({
        "updateParagraphStyle": {
            "range": dict(content_range),
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
            "fields": "namedStyleType",
        }
    })
    requests.extend(r for r in inline_requests if "updateParagraphStyle" in r)
    requests.append({
        "updateTextStyle": {
            "range": dict(content_range),
            "textStyle": {"bold": False},
            "fields": "bold,link",
        }
    })
    if profile == "plain":
        requests.extend(r for r in inline_requests if "updateTextStyle" in r)
        return _enforce_request_payload(requests)

    requests.append(
        {
            "updateTextStyle": {
                "range": {
                    "startIndex": 1,
                    "endIndex": content_end,
                    "tabId": tab_id,
                },
                "textStyle": {
                    "weightedFontFamily": {"fontFamily": "Vazirmatn"}
                },
                "fields": "weightedFontFamily",
            }
        }
    )
    paragraph_request = paragraph_style_request(content_end, tab_id)
    if paragraph_request is not None:
        requests.append(paragraph_request)
    requests.extend(heading_font_requests(model, tab_id))
    requests.extend(r for r in inline_requests if "updateTextStyle" in r)
    return _enforce_request_payload(requests)


__all__ = [
    "DocumentModel",
    "HeadingRange",
    "InlineContent",
    "LinkRange",
    "TableBlock",
    "TextRange",
    "candidate_semantic",
    "heading_font_requests",
    "inline_style_requests",
    "insert_table_structure_requests",
    "paragraph_style_request",
    "parse_markdown",
    "remote_semantic",
    "replacement_requests",
    "semantic_sha256",
    "table_cell_insert_requests",
    "table_cell_style_requests",
]
