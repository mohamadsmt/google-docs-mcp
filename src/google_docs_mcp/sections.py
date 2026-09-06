"""Revision-guarded Markdown publication in a bounded body range.

Never round-trip the lossy document read view. The existing renderer's index-1
requests are rebased; cell indexes are obtained from each actual readback.
Every retained paragraph/run/object is compared, including its original styles.
"""
from copy import deepcopy
from pathlib import Path
import re
from typing import Any

from . import markdown as md
from .client import (
    DocsMCPError, _PartialWriteError, _google_unavailable,
    _remove_recovery_directory, _service_batch_revision, _service_end_index,
    _verify_persian_api, make_recovery_backup, utf16_length,
)
from .editing_common import (
    EditContext, checked_readback, heading_range, invalid, paragraph_text,
    prepare, verification_failed, without_indices,
)


_SELECTOR_CONTROLS = re.compile(r"[\x00-\x1f\ud800-\udfff]")


def _validate(action, position, anchor, heading, tab_id, profile, apply):
    if (not isinstance(action, str) or action not in {"insert", "replace"}
            or not isinstance(position, str) or position not in {"start", "end", "before", "after"}
            or not isinstance(profile, str) or profile not in {"plain", "persian"}
            or type(apply) is not bool):
        raise invalid()
    for value, cap in ((anchor, 500_000), (heading, 500_000), (tab_id, 1000)):
        if value is not None and (not isinstance(value, str) or not value
                                 or len(value) > cap or _SELECTOR_CONTROLS.search(value)):
            raise invalid()
    if action == "replace":
        if heading is None or position != "end" or anchor is not None:
            raise invalid()
    elif heading is not None or ((position in {"before", "after"}) != (anchor is not None)):
        raise invalid()


def _bounds(node, *, section=False):
    start = node.get("startIndex", 0 if section else None)
    end = node.get("endIndex")
    if type(start) is not int or type(end) is not int or not 0 <= start < end:
        raise ValueError
    return start, end


def _validate_paragraph(node):
    start, end = _bounds(node)
    paragraph = node["paragraph"]
    if not isinstance(paragraph, dict) or not isinstance(paragraph.get("paragraphStyle", {}), dict):
        raise ValueError
    elements = paragraph.get("elements")
    if not isinstance(elements, list) or not elements:
        raise ValueError
    cursor = start
    for element in elements:
        left, right = _bounds(element)
        if left != cursor or right > end:
            raise ValueError
        if "textRun" in element:
            run = element["textRun"]
            if (not isinstance(run, dict) or not isinstance(run.get("content"), str)
                    or not isinstance(run.get("textStyle", {}), dict)
                    or utf16_length(run["content"]) != right - left):
                raise ValueError
        cursor = right
    terminal = elements[-1].get("textRun", {}).get("content", "")
    if cursor != end or not terminal.endswith("\n"):
        raise ValueError


def _body_end(body):
    """Fail closed on malformed coordinates before they can become a request."""
    try:
        end = _service_end_index(body)
        content = body["content"]
        if len(content) > 100_000:
            raise ValueError
        cursor = 1
        for i, node in enumerate(content):
            section = i == 0 and "sectionBreak" in node
            left, right = _bounds(node, section=section)
            if section:
                if left != 0 or right != 1:
                    raise ValueError
                continue
            if left != cursor:
                raise ValueError
            if "paragraph" in node:
                _validate_paragraph(node)
            cursor = right
        if cursor != end or "paragraph" not in content[-1]:
            raise ValueError
        return end
    except DocsMCPError:
        raise
    except Exception:
        raise _google_unavailable() from None


def _slice(body, start, end):
    """Index-aware structural slice, only splitting text at UTF-16 boundaries.

    Partial paragraph metadata is retained: a terminal newline remains an
    outside-scope object with the original paragraph and text styles.
    """
    result = []
    for original in body["content"]:
        left, right = _bounds(original, section="sectionBreak" in original)
        if left >= end or right <= start:
            continue
        if start <= left and right <= end:
            result.append(deepcopy(original))
            continue
        if "paragraph" not in original:
            raise verification_failed()
        node = deepcopy(original)
        elements = []
        for original_run in node["paragraph"]["elements"]:
            a, b = _bounds(original_run)
            lo, hi = max(a, start), min(b, end)
            if hi <= lo:
                continue
            run = deepcopy(original_run)
            if lo != a or hi != b:
                if "textRun" not in run:
                    raise verification_failed()
                try:
                    data = run["textRun"]["content"].encode("utf-16-le")
                    run["textRun"]["content"] = data[2 * (lo - a):2 * (hi - a)].decode("utf-16-le")
                except (UnicodeError, KeyError, TypeError):
                    raise verification_failed() from None
            run.update(startIndex=lo, endIndex=hi)
            elements.append(run)
        node.update(startIndex=max(left, start), endIndex=min(right, end))
        node["paragraph"]["elements"] = elements
        result.append(node)
    return {"content": result}


def _resolve(body, action, position, anchor, heading):
    terminal = _body_end(body) - 1
    if action == "replace":
        start, end = heading_range(body, heading)
    else:
        if position == "start":
            start = 1
        elif position == "end":
            start = terminal
        else:
            matches = [n for n in body["content"] if "paragraph" in n and paragraph_text(n) == anchor]
            if len(matches) != 1:
                raise DocsMCPError("anchor_match_mismatch", "The anchor must match exactly one full top-level paragraph.")
            start = matches[0]["startIndex"] if position == "before" else min(matches[0]["endIndex"], terminal)
        end = start
    # Only the final paragraph can require a split, immediately before its
    # undeletable newline. All other boundaries are whole-paragraph boundaries.
    final = body["content"][-1]
    separator = start == terminal and final["startIndex"] < terminal
    if not separator and not any("paragraph" in n and start in (n["startIndex"], n["endIndex"])
                                 for n in body["content"]):
        raise invalid("The selected location is not a paragraph boundary.")
    return start, end, separator


def _table_cells(node):
    """Validate an unmerged, non-nested, indexed table before cell requests."""
    try:
        start, end = _bounds(node)
        table = node["table"]
        rows, columns = table["rows"], table["columns"]
        if type(rows) is not int or type(columns) is not int or rows < 1 or columns < 1 or rows * columns > 100_000:
            raise ValueError
        if len(table["tableRows"]) != rows:
            raise ValueError
        result = []
        cursor = start
        for row in table["tableRows"]:
            if len(row["tableCells"]) != columns:
                raise ValueError
            for cell in row["tableCells"]:
                style = cell.get("tableCellStyle", {})
                for key in ("rowSpan", "columnSpan"):
                    if type(style.get(key, 1)) is not int or style.get(key, 1) != 1:
                        raise ValueError
                paragraphs = cell["content"]
                if not isinstance(paragraphs, list) or not paragraphs:
                    raise ValueError
                for paragraph in paragraphs:
                    _validate_paragraph(paragraph)
                    a, b = _bounds(paragraph)
                    if not cursor < a < b <= end:
                        raise ValueError
                    cursor = b - 1
                result.append(paragraphs)
        return rows, columns, result
    except Exception:
        raise verification_failed() from None


def _supported_target(body):
    """Destruction is permitted for text/lists and ordinary native tables only."""
    pending = list(body["content"])
    while pending:
        node = pending.pop()
        keys = set(node) - {"startIndex", "endIndex"}
        if keys == {"table"}:
            _, _, cells = _table_cells(node)
            for paragraphs in cells:
                pending.extend(paragraphs)
        elif keys == {"paragraph"}:
            paragraph = node["paragraph"]
            if set(paragraph) - {"paragraphStyle", "elements", "bullet"}:
                raise invalid("Unsupported content in the destructive section.")
            for run in paragraph["elements"]:
                if (set(run) - {"startIndex", "endIndex"} != {"textRun"}
                        or set(run["textRun"]) - {"content", "textStyle"}):
                    raise invalid("Unsupported content in the destructive section.")
        else:
            raise invalid("Unsupported content in the destructive section.")


def _rebase(requests, offset):
    """Rebase only emitted location/range coordinates, never model offsets."""
    requests = deepcopy(requests)
    for request in requests:
        payload = next(iter(request.values()))
        for field in ("location", "range"):
            for name in ("index", "startIndex", "endIndex"):
                if name in payload.get(field, {}):
                    payload[field][name] += offset
    return md._enforce_request_plan(requests)


def _initial_requests(model, start, end, separator, tab_id, profile):
    text = ("\n" if separator and model.text else "") + model.text
    requests = []
    if end > start:
        requests.append({"deleteContentRange": {"range": {
            "startIndex": start, "endIndex": end, "tabId": tab_id}}})
    if not text:
        return md._enforce_request_plan(requests)
    requests.append({"insertText": {"location": {"index": start, "tabId": tab_id}, "text": text}})
    # end_index=2 prevents the whole-tab renderer from emitting deletion.
    # Supply our full terminating newline, rather than stealing a suffix newline.
    styles = [r for r in md.replacement_requests(model, 2, tab_id, profile)
              if "insertText" not in r]
    requests.extend(_rebase(styles, start + int(separator) - 1))
    return md._enforce_request_plan(requests)


def _expected_layout(model, stage, separator):
    tables = {t.marker: t for t in model.tables}
    result = [("paragraph", "\n")] if separator and model.text else []
    for line in model.text.split("\n")[:-1]:
        table = tables.get(line)
        if table is None or stage == "initial":
            result.append(("paragraph", line + "\n"))
        else:
            values = tuple(tuple("" if stage == "structure" else cell.text for cell in row)
                           for row in md._pad_table_rows(table.rows))
            # InsertTable adds a newline before itself; the marker paragraph's
            # original newline remains after it. Both separators are deliberate.
            result.extend([("paragraph", "\n"), ("table", values), ("paragraph", "\n")])
    return result


def _actual_layout(body):
    result = []
    for node in body["content"]:
        if "paragraph" in node:
            paragraph = node["paragraph"]
            if ("bullet" in paragraph or set(paragraph) - {"paragraphStyle", "elements"}
                    or any(set(r) - {"startIndex", "endIndex"} != {"textRun"}
                           for r in paragraph["elements"])):
                raise verification_failed()
            result.append(("paragraph", "".join(r["textRun"]["content"] for r in paragraph["elements"])))
        elif "table" in node:
            rows, columns, cells = _table_cells(node)
            values = []
            for paragraphs in cells:
                if len(paragraphs) != 1:
                    raise verification_failed()
                layout = _actual_layout({"content": paragraphs})
                if len(layout) != 1 or layout[0][0] != "paragraph" or not layout[0][1].endswith("\n"):
                    raise verification_failed()
                values.append(layout[0][1][:-1])
            result.append(("table", tuple(tuple(values[i * columns:(i + 1) * columns]) for i in range(rows))))
        else:
            raise verification_failed()
    return result


def _expected_semantic(model, profile, stage):
    expected = md.candidate_semantic(model, profile)
    tables = iter(model.tables)
    for i, block in enumerate(expected["blocks"]):
        if block["type"] != "table":
            continue
        table = next(tables)
        if stage == "initial":
            expected["blocks"][i] = {"type": "paragraph", "heading": None,
                "runs": [{"text": table.marker, "bold": False, "link": None}]}
        elif stage in {"structure", "text"}:
            for row, cells in zip(block["rows"], md._pad_table_rows(table.rows), strict=True):
                for remote, cell in zip(row, cells, strict=True):
                    remote["runs"] = ([{"text": cell.text, "bold": False, "link": None}]
                                      if stage == "text" and cell.text else [])
    return expected


def _read_scope(client, context, revision, start, end, model, profile, separator, stage):
    try:
        _, selected = checked_readback(client, context, revision)
        after_end = _body_end(selected.body)
        before_end = _service_end_index(context.selected.body)
        new_end = end + after_end - before_end
        if new_end < start or new_end >= after_end:
            raise verification_failed()
        before_body, after_body = context.selected.body, selected.body
        if ({k: v for k, v in before_body.items() if k != "content"}
                != {k: v for k, v in after_body.items() if k != "content"}):
            raise verification_failed()
        for b, a in ((_slice(before_body, 0, start), _slice(after_body, 0, start)),
                     (_slice(before_body, end, before_end), _slice(after_body, new_end, after_end))):
            if without_indices(b) != without_indices(a):
                raise verification_failed()
        scoped = _slice(after_body, start, new_end)
        # A leading separator belongs to the preserved last paragraph, possibly
        # a heading/list/object paragraph; do not impose candidate formatting.
        candidate = {"content": scoped["content"][1:]} if separator and model.text else scoped
        expected = _expected_layout(model, stage, False)
        if separator and model.text:
            lead = scoped["content"][0]
            if paragraph_text(lead) != "":
                raise verification_failed()
        if _actual_layout(candidate) != expected:
            raise verification_failed()
        if md.remote_semantic(candidate) != _expected_semantic(model, profile, stage):
            raise verification_failed()
        if profile == "persian" and stage in {"initial", "final"} and model.text:
            _verify_persian_api(candidate)
        tables = tuple({**n, "tabId": context.selected.tab_id} for n in candidate["content"] if "table" in n)
        return tables, new_end
    except Exception:
        raise verification_failed() from None


def edit_section(client: Any, recovery_root: Path, document: str, markdown: str,
                 expected_revision_id: str, action: str = "insert", position: str = "end",
                 anchor_text: str | None = None, heading_text: str | None = None,
                 tab_id: str | None = None, format_profile: str = "persian",
                 apply: bool = False) -> dict[str, object]:
    """Preview or apply one scoped Markdown edit; never retry an uncertain write.

    Each emitted Markdown paragraph has its own newline. At the end of a
    nonempty final paragraph, insert a leading separator and retain the original
    mandatory newline as a trailing empty paragraph with its original style.
    Empty Markdown emits no separator and clears only a replacement range.
    """
    _validate(action, position, anchor_text, heading_text, tab_id, format_profile, apply)
    model = md.parse_markdown(markdown)
    semantic = md.candidate_semantic(model, format_profile)
    candidate_hash = md.semantic_sha256(semantic)
    # Validate bounded renderer plans before document/recovery I/O as well as
    # after actual coordinate rebasing. No provider defaults decide write scope.
    _initial_requests(model, 1, 1, False, "preflight", format_profile)
    md.insert_table_structure_requests(model, "preflight")
    md._validate_table_phase_request_counts(model, format_profile)
    context = prepare(client, document, expected_revision_id, tab_id)
    start, end, separator = _resolve(context.selected.body, action, position, anchor_text, heading_text)
    if end > start:
        _supported_target(_slice(context.selected.body, start, end))
    requests = _initial_requests(model, start, end, separator, context.selected.tab_id, format_profile)
    structure = _rebase(md.insert_table_structure_requests(model, context.selected.tab_id),
                        start + int(separator) - 1)
    result = {
        "ok": True, "document_id": context.document_id, "document_url": context.document_url,
        "tab_id": context.selected.tab_id, "action": action, "position": position,
        "scope": {"start_index": start, "end_index": end},
        "format_profile": format_profile, "table_count": len(model.tables),
        "leading_separator": bool(separator and model.text),
        "terminal_newline_retained": True, "applied": apply,
    }
    if not apply:
        return {**result, "revision_id": context.revision, "valid": True}
    revision = context.revision
    backup = make_recovery_backup(client, context.document_id, recovery_root) if model.tables else None
    phase = "initial_content"
    new_end = end
    try:
        if requests:
            revision = _service_batch_revision(client, context.document_id, requests, revision, retry_safe=False)
            phase = "initial_readback"
            _, new_end = _read_scope(client, context, revision, start, end, model,
                                     format_profile, separator, "initial")
        if structure:
            phase = "table_structure"
            revision = _service_batch_revision(client, context.document_id, structure, revision, retry_safe=False)
            phase = "table_structure_readback"
            tables, new_end = _read_scope(client, context, revision, start, end, model,
                                          format_profile, separator, "structure")
            cells = []
            for table, node in zip(model.tables, tables, strict=True):
                cells.extend(md.table_cell_insert_requests(node, table.rows))
            cells.sort(key=lambda r: r["insertText"]["location"]["index"], reverse=True)
            if cells:
                phase = "table_cell_text"
                revision = _service_batch_revision(client, context.document_id,
                    md._enforce_request_plan(cells), revision, retry_safe=False)
                phase = "table_cell_readback"
                tables, new_end = _read_scope(client, context, revision, start, end, model,
                                              format_profile, separator, "text")
            styles = []
            for table, node in zip(model.tables, tables, strict=True):
                styles.extend(md.table_cell_style_requests(node, table.rows, format_profile))
            if styles:
                phase = "table_cell_styles"
                revision = _service_batch_revision(client, context.document_id,
                    md._enforce_request_plan(styles), revision, retry_safe=False)
                phase = "final_verification"
                _, new_end = _read_scope(client, context, revision, start, end, model,
                                         format_profile, separator, "final")
            # With no styling requests, the text/structure check is also final:
            # there are no model bold/link ranges or Persian defaults to apply.
    except Exception as error:
        if backup is not None:
            raise _PartialWriteError(phase=phase, revision_id=revision, recovery_path=backup.path) from None
        if isinstance(error, DocsMCPError):
            raise
        raise verification_failed() from None
    if backup is not None and not _remove_recovery_directory(backup.path, allow_partial=False):
        raise _PartialWriteError(phase="recovery_cleanup", revision_id=revision, recovery_path=backup.path)
    return {**result, "before_revision_id": context.revision, "after_revision_id": revision,
            "after_scope": {"start_index": start, "end_index": new_end},
            "semantic": {"schema": semantic["schema"], "block_count": len(semantic["blocks"]),
                         "sha256": candidate_hash},
            "verified": True, "formatting_verified": format_profile == "persian"}


__all__ = ["edit_section"]
