"""Formatting-only repairs, scoped to a tab body or a native heading section."""
from copy import deepcopy
import math
from typing import Any, Iterator

from .client import DocsMCPError, _google_unavailable, _service_batch_revision, _service_end_index
from .editing_common import checked_readback, heading_range, invalid, prepare, verification_failed, without_indices
from .markdown import _enforce_request_plan


_PARAGRAPH_FIELDS = ("direction", "alignment", "indentStart", "indentEnd")
_MAX_NODES = 100_000


def _paragraphs(body: dict) -> Iterator[dict]:
    """Walk cell/TOC paragraphs without flattening away native objects."""
    content = body.get("content")
    if not isinstance(content, list) or not content:
        raise _google_unavailable()
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
        if visited > _MAX_NODES or not isinstance(node, dict):
            raise _google_unavailable()
        if kind == "row":
            children, child_kind = node["tableCells"], "cell"
        elif kind == "cell":
            children, child_kind = node["content"], "content"
        elif "paragraph" in node:
            start, end = node.get("startIndex"), node.get("endIndex")
            if type(start) is not int or type(end) is not int or not 1 <= start < end:
                raise _google_unavailable()
            paragraph = node["paragraph"]
            if not isinstance(paragraph, dict) or not isinstance(paragraph.get("paragraphStyle", {}), dict):
                raise _google_unavailable()
            elements = paragraph.get("elements")
            if not isinstance(elements, list) or not elements:
                raise _google_unavailable()
            previous_end = start
            for element in elements:
                visited += 1
                if visited > _MAX_NODES or not isinstance(element, dict):
                    raise _google_unavailable()
                rs, re = element.get("startIndex"), element.get("endIndex")
                if type(rs) is not int or type(re) is not int or not previous_end == rs < re <= end:
                    raise _google_unavailable()
                previous_end = re
                if "textRun" in element:
                    run = element["textRun"]
                    if (not isinstance(run, dict) or not isinstance(run.get("content"), str)
                            or not isinstance(run.get("textStyle", {}), dict)
                            or len(run["content"].encode("utf-16-le")) // 2 != re - rs):
                        raise _google_unavailable()
                    if not isinstance(run.get("textStyle", {}).get("weightedFontFamily", {}), dict):
                        raise _google_unavailable()
            if previous_end != end:
                raise _google_unavailable()
            yield node
            continue
        elif "table" in node:
            children, child_kind = node["table"]["tableRows"], "row"
        elif "tableOfContents" in node:
            children, child_kind = node["tableOfContents"]["content"], "content"
        else:
            # Non-text structural elements are retained verbatim.
            continue
        if not isinstance(children, list):
            raise _google_unavailable()
        stack.append((iter(children), child_kind))


def _selected_paragraphs(body: dict, start: int, end: int) -> list[dict]:
    try:
        result = []
        for node in _paragraphs(body):
            if node["startIndex"] < end and node["endIndex"] > start:
                if node["startIndex"] < start or node["endIndex"] > end:
                    raise _google_unavailable()
                result.append(node)
        if not result:
            raise _google_unavailable()
        return result
    except DocsMCPError:
        raise
    except Exception:
        pass
    raise _google_unavailable()


def _matches(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        return (isinstance(actual, dict) and actual.get("unit") == "PT"
                and type(actual.get("magnitude", 0)) in (int, float)
                and actual.get("magnitude", 0) == expected["magnitude"])
    return actual == expected


def _plan(nodes: list[dict], tab_id: str, styles: dict, persian: bool) -> tuple[dict, list[dict]]:
    counts = {name: 0 for name in (*_PARAGRAPH_FIELDS, "fontFamily")}
    requests = []
    for node in nodes:
        paragraph = node["paragraph"]
        actual = paragraph.get("paragraphStyle", {})
        changes = {name: value for name, value in styles.items() if not _matches(actual.get(name), value)}
        for name in changes:
            counts[name] += 1
        if changes:
            requests.append({"updateParagraphStyle": {
                "range": {"tabId": tab_id, "startIndex": node["startIndex"], "endIndex": node["endIndex"]},
                "paragraphStyle": changes, "fields": ",".join(changes)}})
        if not persian:
            continue
        for element in paragraph["elements"]:
            run = element.get("textRun")
            if run is None:
                continue
            if run.get("textStyle", {}).get("weightedFontFamily", {}).get("fontFamily") != "Vazirmatn":
                counts["fontFamily"] += 1
                requests.append({"updateTextStyle": {
                    "range": {"tabId": tab_id, "startIndex": element["startIndex"], "endIndex": element["endIndex"]},
                    "textStyle": {"weightedFontFamily": {"fontFamily": "Vazirmatn"}},
                    # Preserve explicit weight, bold, links, and all other styles.
                    "fields": "weightedFontFamily.fontFamily"}})
    return counts, requests


def _snapshot(document: dict, tab_id: str, start: int, end: int, persian: bool) -> dict:
    copy = deepcopy(document)
    copy.pop("revisionId", None)
    pending = list(copy["tabs"])
    while pending:
        tab = pending.pop()
        pending.extend(tab.get("childTabs", []))
        if tab["tabProperties"]["tabId"] != tab_id:
            continue
        for node in _selected_paragraphs(tab["documentTab"]["body"], start, end):
            paragraph = node["paragraph"]
            style = paragraph.get("paragraphStyle", {})
            for name in _PARAGRAPH_FIELDS:
                style.pop(name, None)
            if not style:
                paragraph.pop("paragraphStyle", None)
            if persian:
                for element in paragraph["elements"]:
                    if "textRun" not in element:
                        continue
                    run = element["textRun"]
                    text_style = run.get("textStyle", {})
                    family = text_style.get("weightedFontFamily", {})
                    family.pop("fontFamily", None)
                    if not family:
                        text_style.pop("weightedFontFamily", None)
                    if not text_style:
                        run.pop("textStyle", None)
    return without_indices(copy)


def format_document(client: Any, document: str, expected_revision_id: str,
                    tab_id: str | None = None, heading_text: str | None = None,
                    format_profile: str = "persian", right_indent_pt: float = 0,
                    apply: bool = False) -> dict[str, Any]:
    """Preview or apply only layout/font repairs at the reviewed revision."""
    if (type(apply) is not bool or not isinstance(format_profile, str)
            or format_profile not in {"persian", "english"}
            or type(right_indent_pt) not in (int, float)
            or not 0 <= right_indent_pt <= 144 or not math.isfinite(right_indent_pt)
            or (heading_text is not None and (not isinstance(heading_text, str)
                or not heading_text or len(heading_text) > 500_000 or "\x00" in heading_text))):
        raise invalid()
    context = prepare(client, document, expected_revision_id, tab_id)
    body = context.selected.body
    terminal = _service_end_index(body)
    start, end = 1, terminal
    if heading_text is not None:
        start, end = heading_range(body, heading_text, include_heading=True)
        if end == terminal - 1:
            # Style updates, unlike deletions, may include the mandatory newline.
            end = terminal
    nodes = _selected_paragraphs(body, start, end)
    persian = format_profile == "persian"
    styles = {
        "direction": "RIGHT_TO_LEFT" if persian else "LEFT_TO_RIGHT",
        "alignment": "END" if persian else "START",
        # API indents are logical; the public parameter is physical right.
        "indentStart": {"magnitude": right_indent_pt if persian else 0, "unit": "PT"},
        "indentEnd": {"magnitude": 0 if persian else right_indent_pt, "unit": "PT"},
    }
    counts, requests = _plan(nodes, context.selected.tab_id, styles, persian)
    _enforce_request_plan(requests)
    result = {
        "ok": True, "document_id": context.document_id, "document_url": context.document_url,
        "tab_id": context.selected.tab_id, "format_profile": format_profile, "right_indent_pt": right_indent_pt,
        "scope": {"tab_id": context.selected.tab_id, "heading_text": heading_text,
                  "start_index": start, "end_index": end},
        "mismatches": counts, "no_op": not requests, "applied": apply,
    }
    if not requests:
        return {**result, "revision_id": context.revision, "verified": True}
    if not apply:
        return {**result, "revision_id": context.revision, "valid": True}
    expected_snapshot = _snapshot(context.before, context.selected.tab_id, start, end, persian)
    revision = _service_batch_revision(client, context.document_id, requests, context.revision, retry_safe=False)
    after, selected = checked_readback(client, context, revision)
    try:
        after_nodes = _selected_paragraphs(selected.body, start, end)
        remaining, _ = _plan(after_nodes, selected.tab_id, styles, persian)
        if (any(remaining.values())
                or _snapshot(after, selected.tab_id, start, end, persian) != expected_snapshot):
            raise verification_failed()
    except Exception:
        raise verification_failed() from None
    return {**result, "before_revision_id": context.revision, "after_revision_id": revision,
            "verified": True, "formatting_verified": True}
