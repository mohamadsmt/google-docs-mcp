"""Exact-revision edits of one top-level, unmerged Google Docs table.

Cell input is deliberately single-paragraph inline Markdown. Structure and text
writes are read back before deriving any new-cell formatting indices. No phase
is transport-retryable; uncertain writes retain the existing recovery exports.
"""
from copy import deepcopy
from pathlib import Path
import re
from typing import Any

from . import markdown as md
from .client import (
    DocsMCPError, _PartialWriteError, _remove_recovery_directory,
    _service_batch_revision, make_recovery_backup, utf16_length, validate_markdown,
)
from .editing_common import (
    EditContext, checked_readback, invalid, prepare, verification_failed, without_indices,
)

_ACTIONS = {"set_cell", "insert_row", "insert_column", "delete_row", "delete_column"}
_MAX_CELLS = 100_000


def _integer(value: object, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _arguments(action: str, table_index: int, row_index: int | None,
               column_index: int | None, markdown: str | None, side: str | None,
               profile: str, apply: bool) -> md.InlineContent | None:
    if (not isinstance(action, str) or action not in _ACTIONS
            or not _integer(table_index) or type(apply) is not bool
            or not isinstance(profile, str) or profile not in {"persian", "plain"}):
        raise invalid()
    if action == "set_cell":
        if not _integer(row_index) or not _integer(column_index) or side is not None:
            raise invalid()
        source = validate_markdown(markdown)
        # Do not silently flatten block Markdown or invoke the parser's extra
        # Obsidian/code syntax. Literal line breaks are not supported in v1.
        if (re.search(r"[\r\n\v\f\u0085\u2028\u2029]", source)
                or re.match(r"^\s*(?:#{1,6}\s|[-+*]\s|\d+[.)]\s|>|\|)", source)
                or re.search(r"(?<!\\)(?:`|\[\[|!\[)", source)):
            raise DocsMCPError("invalid_markdown", "Table cells accept single-paragraph inline text, bold and HTTP(S) links only.")
        return md._parse_inline(source)
    is_row = action.endswith("row")
    required, irrelevant = (row_index, column_index) if is_row else (column_index, row_index)
    if not _integer(required) or irrelevant is not None or markdown is not None:
        raise invalid()
    if action.startswith("insert"):
        if not isinstance(side, str) or side not in {"before", "after"}:
            raise invalid()
    elif side is not None:
        raise invalid()
    return None


def _unsupported() -> DocsMCPError:
    return DocsMCPError("unsupported_table", "The target must be a well-formed, unmerged table without nested tables.")


def _bounds(node: dict) -> tuple[int, int]:
    start, end = node.get("startIndex"), node.get("endIndex")
    if not _integer(start, 1) or not _integer(end, 1) or end <= start:
        raise _unsupported()
    return start, end


def _validate_table(node: dict) -> None:
    """Validate remote indices and shape without flattening unrelated objects."""
    try:
        start, end = _bounds(node)
        table = node["table"]
        rows, columns = table["rows"], table["columns"]
        if (not _integer(rows, 1) or not _integer(columns, 1)
                or rows * columns > _MAX_CELLS):
            raise _unsupported()
        table_rows = table["tableRows"]
        if not isinstance(table_rows, list) or len(table_rows) != rows:
            raise _unsupported()
        previous = start
        for row in table_rows:
            cells = row["tableCells"]
            if not isinstance(cells, list) or len(cells) != columns:
                raise _unsupported()
            for cell in cells:
                cell_start, cell_end = _bounds(cell)
                if cell_start < previous or cell_end > end:
                    raise _unsupported()
                previous = cell_end
                style = cell.get("tableCellStyle", {})
                if not isinstance(style, dict):
                    raise _unsupported()
                for key in ("rowSpan", "columnSpan"):
                    if type(style.get(key, 1)) is not int or style.get(key, 1) != 1:
                        raise _unsupported()
                content = cell["content"]
                if not isinstance(content, list) or not content or len(content) > _MAX_CELLS:
                    raise _unsupported()
                paragraph_end = cell_start
                for element in content:
                    p_start, p_end = _bounds(element)
                    if "table" in element or p_start < paragraph_end or p_end > cell_end:
                        raise _unsupported()
                    paragraph_end = p_end
                    if "paragraph" not in element:
                        raise _unsupported()
                    paragraph = element["paragraph"]
                    runs = paragraph["elements"]
                    if not isinstance(runs, list) or not runs or len(runs) > _MAX_CELLS:
                        raise _unsupported()
                    run_end = p_start
                    for run in runs:
                        r_start, r_end = _bounds(run)
                        if r_start != run_end or r_end > p_end:
                            raise _unsupported()
                        run_end = r_end
                        if "textRun" in run:
                            text = run["textRun"]["content"]
                            if not isinstance(text, str) or utf16_length(text) != r_end - r_start:
                                raise _unsupported()
                    if run_end != p_end:
                        raise _unsupported()
                if paragraph_end != cell_end:
                    raise _unsupported()
        properties = table.get("tableStyle", {}).get("tableColumnProperties")
        if properties is not None and (not isinstance(properties, list) or len(properties) != columns):
            raise _unsupported()
    except DocsMCPError:
        raise
    except (KeyError, TypeError, AttributeError, UnicodeError, ValueError):
        raise _unsupported() from None


def _target(body: dict, table_index: int) -> tuple[int, dict]:
    content = body.get("content")
    if not isinstance(content, list) or any(not isinstance(node, dict) for node in content):
        raise _unsupported()
    tables = [(i, node) for i, node in enumerate(content) if "table" in node]
    if table_index >= len(tables):
        raise invalid("table_index is outside the selected tab's top-level tables.")
    offset, node = tables[table_index]
    _validate_table(node)
    return offset, node


def _cell(node: dict, row: int, column: int) -> dict:
    return node["table"]["tableRows"][row]["tableCells"][column]


def _text(cell: dict) -> str:
    pieces = []
    for node in cell["content"]:
        if set(node) - {"startIndex", "endIndex"} != {"paragraph"}:
            raise _unsupported()
        paragraph = node["paragraph"]
        if set(paragraph) - {"elements", "paragraphStyle", "bullet"}:
            raise _unsupported()
        paragraph_parts = []
        for run in paragraph["elements"]:
            if set(run) - {"startIndex", "endIndex"} != {"textRun"}:
                raise _unsupported()
            if set(run["textRun"]) - {"content", "textStyle"}:
                raise _unsupported()
            paragraph_parts.append(run["textRun"]["content"])
        value = "".join(paragraph_parts)
        if not value.endswith("\n") or "\n" in value[:-1]:
            raise _unsupported()
        pieces.append(value)
    return "".join(pieces)


def _except(value: dict, *keys: str) -> dict:
    return {key: item for key, item in value.items() if key not in keys}


def _equal(before: Any, after: Any) -> None:
    if without_indices(before) != without_indices(after):
        raise verification_failed()


def _axis_map(count: int, action: str, axis: str, position: int) -> list[int | None]:
    result: list[int | None] = list(range(count))
    if action == f"insert_{axis}":
        result.insert(position, None)
    elif action == f"delete_{axis}":
        result.pop(position)
    return result


def _verify_structure(before_body: dict, after_body: dict, table_index: int,
                      action: str, row_index: int | None, column_index: int | None,
                      position: int) -> tuple[dict, list[tuple[int, int]]]:
    before_offset, before_node = _target(before_body, table_index)
    after_offset, after_node = _target(after_body, table_index)
    if before_offset != after_offset:
        raise verification_failed()
    _equal(_except(before_body, "content"), _except(after_body, "content"))
    b_content, a_content = before_body["content"], after_body["content"]
    _equal(b_content[:before_offset], a_content[:after_offset])
    _equal(b_content[before_offset + 1:], a_content[after_offset + 1:])
    _equal(_except(before_node, "table"), _except(after_node, "table"))
    b, a = before_node["table"], after_node["table"]
    row_map = _axis_map(b["rows"], action, "row", position)
    col_map = _axis_map(b["columns"], action, "column", position)
    if a["rows"] != len(row_map) or a["columns"] != len(col_map):
        raise verification_failed()
    _equal(_except(b, "rows", "columns", "tableRows", "tableStyle"),
           _except(a, "rows", "columns", "tableRows", "tableStyle"))
    b_style, a_style = b.get("tableStyle", {}), a.get("tableStyle", {})
    _equal(_except(b_style, "tableColumnProperties"), _except(a_style, "tableColumnProperties"))
    b_props, a_props = b_style.get("tableColumnProperties"), a_style.get("tableColumnProperties")
    if b_props is None or a_props is None:
        _equal(b_props, a_props)
    else:
        for new_column, old_column in enumerate(col_map):
            if old_column is not None:
                _equal(b_props[old_column], a_props[new_column])
    changed = []
    for new_row, old_row in enumerate(row_map):
        if old_row is not None:
            _equal(_except(b["tableRows"][old_row], "tableCells"),
                   _except(a["tableRows"][new_row], "tableCells"))
        for new_col, old_col in enumerate(col_map):
            after_cell = _cell(after_node, new_row, new_col)
            if old_row is None or old_col is None:
                changed.append((new_row, new_col))
                if _text(after_cell) != "\n" or len(after_cell["content"]) != 1:
                    raise verification_failed()
            elif action == "set_cell" and (new_row, new_col) == (row_index, column_index):
                changed.append((new_row, new_col))
                _equal(_except(_cell(before_node, old_row, old_col), "content"),
                       _except(after_cell, "content"))
            else:
                _equal(_cell(before_node, old_row, old_col), after_cell)
    return after_node, changed


def _inserted_column_position(before: dict, after: dict, table_index: int,
                              column_index: int) -> int:
    # insertRight is physical; Docs cell arrays can use the opposite logical
    # ordering. Only accept a unique adjacent empty column whose removal restores
    # every original cell/style/width and all outside content exactly.
    matches = []
    for position in (column_index, column_index + 1):
        try:
            _verify_structure(before, after, table_index, "insert_column", None, column_index, position)
        except DocsMCPError:
            continue
        matches.append(position)
    if len(matches) != 1:
        raise verification_failed()
    return matches[0]


def _readback(client: Any, context: EditContext, revision: str) -> dict:
    raw, selected = checked_readback(client, context, revision)
    # Common checks tabs; root document/named styles are also outside our scope.
    _equal(_except(context.before, "tabs", "revisionId"), _except(raw, "tabs", "revisionId"))
    return selected.body


def _cell_style_requests(cell: dict, inline: md.InlineContent, profile: str,
                         tab_id: str, *, reset: bool) -> list[dict]:
    start = cell["content"][0]["startIndex"]
    end = cell["content"][-1]["endIndex"]
    cell_range = {"startIndex": start, "endIndex": end, "tabId": tab_id}
    requests = []
    if reset:
        requests.extend([
            {"deleteParagraphBullets": {"range": dict(cell_range)}},
            {"updateParagraphStyle": {"range": dict(cell_range),
                                      "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                                      "fields": "namedStyleType"}},
            {"updateTextStyle": {"range": dict(cell_range), "textStyle": {"bold": False},
                                 "fields": "bold,link"}},
        ])
    # Project only the target cell, retaining its real readback index. The
    # existing renderer otherwise styles every cell in the supplied table.
    projection = {"tabId": tab_id, "table": {"tableRows": [{"tableCells": [cell]}]}}
    requests.extend(md.table_cell_style_requests(projection, ((inline,),), profile))
    return md._enforce_request_plan(requests)


def _verify_cell(cell: dict, inline: md.InlineContent, profile: str, *, reset: bool) -> None:
    if len(cell["content"]) != 1 or _text(cell) != inline.text + "\n":
        raise verification_failed()
    paragraph = cell["content"][0]["paragraph"]
    if reset:
        model = md.DocumentModel(inline.text + "\n", (), inline.bold, inline.links, ())
        if md.remote_semantic({"content": cell["content"]}) != md.candidate_semantic(model, "plain"):
            raise verification_failed()
        if paragraph.get("paragraphStyle", {}).get("namedStyleType", "NORMAL_TEXT") != "NORMAL_TEXT" or "bullet" in paragraph:
            raise verification_failed()
    if profile == "persian":
        style = paragraph.get("paragraphStyle", {})
        if style.get("direction") != "RIGHT_TO_LEFT" or style.get("alignment") != "START":
            raise verification_failed()
        for field in ("indentStart", "indentEnd"):
            dimension = style.get(field, {})
            if dimension.get("magnitude", 0) != 0 or dimension.get("unit") != "PT":
                raise verification_failed()
        for run in paragraph["elements"]:
            font = run["textRun"].get("textStyle", {}).get("weightedFontFamily", {})
            if font.get("fontFamily") != "Vazirmatn":
                raise verification_failed()


def _structure_request(node: dict, tab_id: str, action: str, row: int | None,
                       column: int | None, side: str | None) -> dict:
    name = {"insert_row": "insertTableRow", "insert_column": "insertTableColumn",
            "delete_row": "deleteTableRow", "delete_column": "deleteTableColumn"}[action]
    value = {"tableCellLocation": {"tableStartLocation": {"index": node["startIndex"], "tabId": tab_id},
                                   "rowIndex": row if row is not None else 0,
                                   "columnIndex": column if column is not None else 0}}
    if action.startswith("insert"):
        value["insertBelow" if action.endswith("row") else "insertRight"] = side == "after"
    return {name: value}


def edit_table(client: Any, recovery_root: Path, document: str, expected_revision_id: str,
               action: str, table_index: int, row_index: int | None = None,
               column_index: int | None = None, markdown: str | None = None,
               side: str | None = None, tab_id: str | None = None,
               format_profile: str = "persian", apply: bool = False) -> dict[str, object]:
    """Preview or apply exactly one cell, row, or physical column operation."""
    inline = _arguments(action, table_index, row_index, column_index, markdown, side, format_profile, apply)
    context = prepare(client, document, expected_revision_id, tab_id)
    _, node = _target(context.selected.body, table_index)
    rows, columns = node["table"]["rows"], node["table"]["columns"]
    if ((row_index is not None and row_index >= rows)
            or (column_index is not None and column_index >= columns)):
        raise invalid("The row or column selector is outside the target table.")
    if (action == "delete_row" and rows == 1) or (action == "delete_column" and columns == 1):
        raise invalid("Deleting the last row or column would delete the table.")
    selected_tab_id = context.selected.tab_id
    if action == "set_cell":
        assert inline is not None and row_index is not None and column_index is not None
        target_cell = _cell(node, row_index, column_index)
        _text(target_cell)
        start = target_cell["content"][0]["startIndex"]
        end = target_cell["content"][-1]["endIndex"] - 1
        requests = []
        if end > start:
            requests.append({"deleteContentRange": {"range": {"startIndex": start, "endIndex": end, "tabId": selected_tab_id}}})
        if inline.text:
            requests.append({"insertText": {"location": {"index": start, "tabId": selected_tab_id}, "text": inline.text}})
        position = 0
        # Bound future formatting before any mutation as well as per phase.
        model = md.DocumentModel(inline.text + "\n", (), inline.bold, inline.links, ())
        md._enforce_request_plan(md.inline_style_requests(model, selected_tab_id) + [{}] * 5)
    else:
        requests = [_structure_request(node, selected_tab_id, action, row_index, column_index, side)]
        index = row_index if action.endswith("row") else column_index
        assert index is not None
        position = index + (1 if action.startswith("insert") and side == "after" else 0)
        if action.startswith("insert") and format_profile == "persian":
            md._enforce_request_count(2 * (columns if action.endswith("row") else rows))
    requests = md._enforce_request_plan(requests)
    scope = {"table_index": table_index, "rows": rows, "columns": columns}
    if row_index is not None:
        scope["row_index"] = row_index
    if column_index is not None:
        scope["column_index"] = column_index
    result: dict[str, object] = {
        "ok": True, "document_id": context.document_id, "document_url": context.document_url,
        "tab_id": selected_tab_id, "action": action, "scope": scope, "side": side,
        "before_revision_id": context.revision, "format_profile": format_profile,
        "applied": False,
    }
    if not apply:
        return result

    backup = make_recovery_backup(client, context.document_id, recovery_root)
    revision = context.revision
    phase = "table_cell_text" if action == "set_cell" else "table_structure"
    failed = False
    try:
        if requests:
            revision = _service_batch_revision(client, context.document_id, requests, revision, retry_safe=False)
            phase += "_readback"
            body = _readback(client, context, revision)
        else:
            body = context.selected.body
        if action == "insert_column":
            assert column_index is not None  # Required by the argument validator.
            position = _inserted_column_position(context.selected.body, body, table_index, column_index)
            scope["inserted_column_index"] = position
        current, changed = _verify_structure(context.selected.body, body, table_index,
                                             action, row_index, column_index, position)
        if inline is not None:
            target_cell = _cell(current, row_index, column_index)
            if len(target_cell["content"]) != 1 or _text(target_cell) != inline.text + "\n":
                raise verification_failed()
        styles = []
        for r, c in changed:
            styles.extend(_cell_style_requests(_cell(current, r, c), inline or md.InlineContent(""),
                                               format_profile, selected_tab_id, reset=action == "set_cell"))
        styles = md._enforce_request_plan(styles)
        if styles:
            phase = "table_cell_styles"
            revision = _service_batch_revision(client, context.document_id, styles, revision, retry_safe=False)
            phase = "table_cell_styles_readback"
            final_body = _readback(client, context, revision)
            final, final_changed = _verify_structure(context.selected.body, final_body, table_index,
                                                     action, row_index, column_index, position)
            _equal(changed, final_changed)
            # Row/cell properties newly created by Google are now known. No
            # formatting request may mutate them during the second phase.
            b_table, a_table = deepcopy(current), deepcopy(final)
            for r, c in changed:
                _cell(b_table, r, c).pop("content")
                _cell(a_table, r, c).pop("content")
            _equal(b_table, a_table)
            current = final
        phase = "table_verification"
        for r, c in changed:
            _verify_cell(_cell(current, r, c), inline or md.InlineContent(""), format_profile,
                         reset=action == "set_cell")
    except Exception:
        failed = True
    if failed:
        raise _PartialWriteError(phase=phase, revision_id=revision, recovery_path=backup.path)
    if not _remove_recovery_directory(backup.path, allow_partial=False):
        raise _PartialWriteError(phase="recovery_cleanup", revision_id=revision, recovery_path=backup.path)
    result.update({"applied": True, "verified": True, "after_revision_id": revision})
    return result


__all__ = ["edit_table"]
