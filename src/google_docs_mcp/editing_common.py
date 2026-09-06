"""Shared exact-revision scope helpers for structured editing."""
from dataclasses import dataclass
from typing import Any

from .client import (
    DocsMCPError, TabView, _multiple_tabs_require_tab_id, _require_native_document,
    _service_document, _service_end_index, _service_metadata, _service_tabs,
    _validate_service_revision, parse_document_id, select_tab,
)


def invalid(message: str = "Invalid structured editing input.") -> DocsMCPError:
    return DocsMCPError("invalid_input", message)


def verification_failed() -> DocsMCPError:
    return DocsMCPError("verification_failed", "The scoped Google Docs change could not be verified.")


@dataclass(frozen=True)
class EditContext:
    document_id: str
    document_url: str
    revision: str
    before: dict
    selected: TabView


def prepare(client: Any, document: str, expected_revision_id: str,
            tab_id: str | None = None) -> EditContext:
    document_id = parse_document_id(document)
    revision = _validate_service_revision(expected_revision_id)
    if tab_id is not None and (not isinstance(tab_id, str) or not tab_id or len(tab_id) > 1000):
        raise invalid()
    metadata = _service_metadata(client, document_id)
    _require_native_document(metadata)
    before = _service_document(client, document_id)
    if before["revisionId"] != revision:
        raise DocsMCPError("stale_revision", "The Google document revision changed before the write.")
    selected = select_tab(before, tab_id)
    if selected is None:
        raise _multiple_tabs_require_tab_id()
    if not selected.tab_id:
        raise invalid()
    return EditContext(document_id, metadata["webViewLink"], revision, before, selected)


def without_indices(value: Any) -> Any:
    """Compare shifted structure without erasing text, styles, or object identity."""
    if isinstance(value, dict):
        return {k: without_indices(v) for k, v in value.items() if k not in {"startIndex", "endIndex"}}
    if isinstance(value, list):
        result: list[Any] = []
        for original in value:
            item = without_indices(original)
            if (result and isinstance(item, dict) and isinstance(result[-1], dict)
                    and isinstance(item.get("textRun"), dict)
                    and isinstance(result[-1].get("textRun"), dict)):
                previous = result[-1]
                other = {k: v for k, v in item.items() if k != "textRun"}
                previous_other = {k: v for k, v in previous.items() if k != "textRun"}
                run = item["textRun"]
                previous_run = previous["textRun"]
                if (other == previous_other
                        and {k: v for k, v in run.items() if k != "content"}
                        == {k: v for k, v in previous_run.items() if k != "content"}
                        and isinstance(run.get("content"), str)
                        and isinstance(previous_run.get("content"), str)):
                    previous_run["content"] += run["content"]
                    continue
            result.append(item)
        return result
    return value


def _tab_payloads(document: dict) -> dict[str, dict]:
    result: dict[str, dict] = {}
    pending = list(document.get("tabs", []))
    while pending:
        tab = pending.pop()
        result[tab["tabProperties"]["tabId"]] = {
            k: v for k, v in tab.items() if k != "childTabs"
        }
        pending.extend(tab.get("childTabs", []))
    return result


def checked_readback(client: Any, context: EditContext, revision: str) -> tuple[dict, TabView]:
    after = _service_document(client, context.document_id)
    if after["revisionId"] != revision:
        raise verification_failed()
    selected = select_tab(after, context.selected.tab_id)
    if selected is None:
        raise verification_failed()
    _service_tabs(after)
    before_tabs, after_tabs = _tab_payloads(context.before), _tab_payloads(after)
    if before_tabs.keys() != after_tabs.keys():
        raise verification_failed()
    for tab_id, before in before_tabs.items():
        if tab_id != context.selected.tab_id and before != after_tabs[tab_id]:
            raise verification_failed()
    before_selected = before_tabs[context.selected.tab_id]
    after_selected = after_tabs[context.selected.tab_id]
    # This helper permits body edits only, never headers/footers/footnotes or tab properties.
    for key in before_selected.keys() | after_selected.keys():
        if key == "documentTab":
            b = {k: v for k, v in before_selected[key].items() if k != "body"}
            a = {k: v for k, v in after_selected[key].items() if k != "body"}
            if b != a:
                raise verification_failed()
        elif before_selected.get(key) != after_selected.get(key):
            raise verification_failed()
    return after, selected


def paragraph_text(element: dict) -> str | None:
    paragraph = element.get("paragraph")
    if not isinstance(paragraph, dict):
        return None
    pieces = []
    for run in paragraph.get("elements", []):
        text = run.get("textRun", {}).get("content")
        if not isinstance(text, str):
            return None
        pieces.append(text)
    return "".join(pieces).removesuffix("\n")


def heading_range(body: dict, heading_text: str, include_heading: bool = False) -> tuple[int, int]:
    if not isinstance(heading_text, str) or not heading_text or len(heading_text) > 500_000:
        raise invalid()
    headings = []
    for element in body.get("content", []):
        name = element.get("paragraph", {}).get("paragraphStyle", {}).get("namedStyleType", "")
        if name in {f"HEADING_{n}" for n in range(1, 7)}:
            headings.append((element, int(name[-1])))
    matches = [(i, e, level) for i, (e, level) in enumerate(headings)
               if paragraph_text(e) == heading_text]
    if len(matches) != 1:
        raise DocsMCPError("heading_match_mismatch", "The heading must match exactly once in the selected body.")
    offset, heading, level = matches[0]
    end = _service_end_index(body) - 1
    for element, other_level in headings[offset + 1:]:
        if other_level <= level:
            end = element["startIndex"]
            break
    start = heading["startIndex"] if include_heading else heading["endIndex"]
    # A heading can itself contain the document's mandatory terminal newline.
    return min(start, end), end


def table_inventory(body: dict) -> list[dict]:
    result = []
    for element in body.get("content", []):
        table = element.get("table")
        if not isinstance(table, dict):
            continue
        rows, columns = table.get("rows"), table.get("columns")
        editable = type(rows) is int and type(columns) is int and rows > 0 and columns > 0
        table_rows = table.get("tableRows", [])
        editable = editable and len(table_rows) == rows
        for row in table_rows:
            cells = row.get("tableCells", [])
            editable = editable and len(cells) == columns
            for cell in cells:
                style = cell.get("tableCellStyle", {})
                editable = editable and style.get("rowSpan", 1) == 1 and style.get("columnSpan", 1) == 1
                editable = editable and all("table" not in node for node in cell.get("content", []))
        result.append({"table_index": len(result), "rows": rows, "columns": columns,
                       "editable": bool(editable)})
    return result
