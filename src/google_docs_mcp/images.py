"""Public-HTTPS inline images; Google, not this process, fetches the URI.

Lexical URL checks are not DNS pinning or redirect vetting of Google's fetch.
Google enforces PNG/JPEG/GIF, <50 MB and <=25 megapixels. Requested dimensions
are an aspect-ratio-preserving bounding box; no local image download occurs.
"""
from __future__ import annotations

from copy import deepcopy
import ipaddress
import math
import re
from typing import Any
import unicodedata
from urllib.parse import unquote, urlsplit

from .client import (
    DocsMCPError, _insertion_index, _insertion_segments, _response_revision,
    _service_document, _validate_insertion, utf16_length,
    _document_not_found, _google_needs_reauth, _google_unavailable,
    _permission_denied, _rate_limited,
)
from .editing_common import invalid, prepare
from .markdown import _enforce_request_plan, paragraph_style_request

MAX_IMAGE_URI_BYTES = 2048
MAX_IMAGE_DIMENSION_PT = 1440
_MAX_NODES = 100_000


def _validate_uri(value: object) -> None:
    bad = False
    try:
        if not isinstance(value, str) or not 1 <= len(value.encode("utf-8")) <= MAX_IMAGE_URI_BYTES:
            raise ValueError
        if any(c.isspace() or unicodedata.category(c).startswith("C") for c in value) or "\\" in value:
            raise ValueError
        if re.search(r"%(?![0-9a-fA-F]{2})", value):
            raise ValueError
        decoded = unquote(value, errors="strict")
        if any(c.isspace() or unicodedata.category(c).startswith("C") for c in decoded):
            raise ValueError
        parsed = urlsplit(value)
        host = parsed.hostname
        if (parsed.scheme != "https" or not host or parsed.username is not None
                or parsed.password is not None or parsed.fragment or "%" in parsed.netloc
                or not parsed.netloc.isascii()):
            raise ValueError
        # Preserve source URI bytes; do not normalize malformed/empty ports.
        authority_host = f"[{host}]" if ":" in host else host
        if parsed.netloc.lower() not in (authority_host, authority_host + ":443"):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            labels = host.split(".")
            if (len(host) > 253 or len(labels) < 2
                    or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", labels[-1])
                    or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                           for label in labels)
                    or labels[-1] in {"localhost", "local", "internal", "lan", "home", "onion", "invalid", "test"}
                    or host == "home.arpa" or host.endswith(".home.arpa")):
                raise ValueError
        else:
            if not address.is_global or address.is_multicast:
                raise ValueError
            if isinstance(address, ipaddress.IPv6Address):
                if address.ipv4_mapped or address.sixtofour or address.teredo:
                    raise ValueError
    except (ValueError, UnicodeError, TypeError):
        bad = True
    if bad:
        raise invalid("image_uri must be a public HTTPS URI of at most 2048 UTF-8 bytes, without credentials or unsafe host/port syntax.")


def _validate_dimension(value: object) -> None:
    if value is not None and (not isinstance(value, (int, float)) or type(value) not in (int, float)
                              or not 0 < value <= MAX_IMAGE_DIMENSION_PT
                              or not math.isfinite(value)):
        raise invalid("Image dimensions must be finite positive numbers no greater than 1440 PT.")


def _safe_copy(value: Any) -> Any:
    """Bound traversal before deep copy, dropping only volatile bearer image URIs."""
    visited = 0
    def copy(node: Any, depth: int = 0) -> Any:
        nonlocal visited
        visited += 1
        if visited > _MAX_NODES or depth > 100:
            raise ValueError
        if isinstance(node, dict):
            return {k: copy(v, depth + 1) for k, v in node.items() if k != "contentUri"}
        if isinstance(node, list):
            return [copy(v, depth + 1) for v in node]
        if node is None or type(node) in (str, int, float, bool):
            return node
        raise ValueError
    return copy(value)


def _selected_payload(document: dict, tab_id: str) -> dict:
    pending = list(document["tabs"])
    matches = []
    seen = set()
    while pending:
        tab = pending.pop()
        identity = tab["tabProperties"]["tabId"]
        if identity in seen:
            raise ValueError
        seen.add(identity)
        if identity == tab_id:
            matches.append(tab["documentTab"])
        pending.extend(tab.get("childTabs", []))
    if len(matches) != 1:
        raise ValueError
    return matches[0]


def _paragraphs(body: dict):
    # Targets only body and table-cell paragraphs, never TOC or auxiliary segments.
    pending = [iter(body["content"])]
    while pending:
        try:
            node = next(pending[-1])
        except StopIteration:
            pending.pop()
            continue
        if "paragraph" in node:
            yield node
        elif "table" in node:
            for row in reversed(node["table"]["tableRows"]):
                for cell in reversed(row["tableCells"]):
                    pending.append(iter(cell["content"]))


def _paragraph_at(body: dict, index: int) -> dict:
    matches = [p for p in _paragraphs(body) if type(p.get("startIndex")) is int
               and type(p.get("endIndex")) is int and p["startIndex"] <= index < p["endIndex"]]
    if len(matches) != 1:
        raise ValueError
    node = matches[0]
    elements = node["paragraph"]["elements"]
    cursor = node["startIndex"]
    for element in elements:
        left, right = element["startIndex"], element["endIndex"]
        if type(left) is not int or type(right) is not int or left != cursor or right <= left:
            raise ValueError
        if "textRun" in element and utf16_length(element["textRun"]["content"]) != right - left:
            raise ValueError
        cursor = right
    if (cursor != node["endIndex"] or not elements
            or not elements[-1].get("textRun", {}).get("content", "").endswith("\n")):
        raise ValueError
    return node


def _coalesce(value: Any) -> Any:
    """Ignore legal text-run splits, retaining indices, styles and all other data."""
    if isinstance(value, dict):
        return {k: _coalesce(v) for k, v in value.items()}
    if not isinstance(value, list):
        return value
    result = []
    for original in value:
        item = _coalesce(original)
        if result and isinstance(item, dict) and isinstance(result[-1], dict):
            previous = result[-1]
            if ("textRun" in previous and "textRun" in item
                    and previous.get("endIndex") == item.get("startIndex")
                    and {k: v for k, v in previous.items() if k not in {"startIndex", "endIndex", "textRun"}}
                    == {k: v for k, v in item.items() if k not in {"startIndex", "endIndex", "textRun"}}
                    and {k: v for k, v in previous["textRun"].items() if k != "content"}
                    == {k: v for k, v in item["textRun"].items() if k != "content"}):
                previous["textRun"]["content"] += item["textRun"]["content"]
                previous["endIndex"] = item["endIndex"]
                continue
        result.append(item)
    return result


def _unshift(value: Any, index: int) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"startIndex", "endIndex"}:
                if type(item) is not int:
                    raise ValueError
                if item > index:
                    value[key] -= 1
            elif isinstance(item, (dict, list)):
                _unshift(item, index)
    elif isinstance(value, list):
        for item in value:
            _unshift(item, index)


def _dimensions(obj: dict, width_pt: float | None, height_pt: float | None) -> dict:
    embedded = obj["inlineObjectProperties"]["embeddedObject"]
    if not isinstance(embedded["imageProperties"], dict):
        raise ValueError
    size = embedded["size"]
    actual = {}
    for name in ("width", "height"):
        dimension = size[name]
        number = dimension["magnitude"]
        if (type(number) not in (int, float) or not 0 < number <= 1_000_000
                or not math.isfinite(number) or dimension["unit"] != "PT"):
            raise ValueError
        actual[name] = number
    # Google preserves intrinsic aspect ratio. Without fetching original pixels,
    # we can verify the documented fitted box, not independently infer that ratio.
    requested = {k: v for k, v in (("width", width_pt), ("height", height_pt)) if v is not None}
    close = [math.isclose(actual[k], v, rel_tol=1e-5, abs_tol=0.01) for k, v in requested.items()]
    if (any(actual[k] > v and not math.isclose(actual[k], v, rel_tol=1e-5, abs_tol=0.01)
            for k, v in requested.items()) or (requested and not any(close))):
        raise ValueError
    return actual


def _body_named_ranges(selected: dict, tab_id: str) -> list[dict]:
    """Select body-coordinate ranges without altering header/other-tab ranges."""
    groups = selected.get("namedRanges", {})
    if not isinstance(groups, dict):
        raise ValueError
    result = []
    for group in groups.values():
        if not isinstance(group, dict) or not isinstance(group.get("namedRanges"), list):
            raise ValueError
        for named in group["namedRanges"]:
            if not isinstance(named, dict) or not isinstance(named.get("ranges"), list):
                raise ValueError
            for area in named["ranges"]:
                if not isinstance(area, dict) or area.keys() - {"startIndex", "endIndex", "segmentId", "tabId"}:
                    raise ValueError
                start, end = area.get("startIndex", 0), area.get("endIndex")
                if type(start) is not int or start < 0 or (end is not None and (type(end) is not int or end < start)):
                    raise ValueError
                if area.get("segmentId") in (None, "") and area.get("tabId") in (None, tab_id):
                    result.append(area)
    return result


def _verify(after: dict, expected: dict, *, tab_id: str, index: int, image_id: str,
            revision: str, width_pt: float | None, height_pt: float | None,
            format_profile: str) -> dict:
    pending = [expected]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            for key in ("inlineObjects", "positionedObjects"):
                if image_id in value.get(key, {}):
                    raise ValueError
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    restored = _safe_copy(after)
    if restored["revisionId"] != revision:
        raise ValueError
    selected = _selected_payload(restored, tab_id)
    previous = _selected_payload(expected, tab_id)
    objects = selected["inlineObjects"]
    old_objects = previous.get("inlineObjects", {})
    if image_id in old_objects or objects.keys() != old_objects.keys() | {image_id}:
        raise ValueError
    dimensions = _dimensions(objects[image_id], width_pt, height_pt)
    node = _paragraph_at(selected["body"], index)
    if format_profile == "persian":
        style = node["paragraph"]["paragraphStyle"]
        if style.get("direction") != "RIGHT_TO_LEFT" or style.get("alignment") != "START":
            raise ValueError
        for name in ("indentStart", "indentEnd"):
            indent = style[name]
            number = indent.get("magnitude", 0)
            if type(number) not in (int, float) or number != 0 or indent.get("unit") != "PT":
                raise ValueError
            # Google can omit scalar zero, but not the logical indent field.
            indent["magnitude"] = 0
    elements = node["paragraph"]["elements"]
    hits = [e for p in _paragraphs(selected["body"]) for e in p["paragraph"]["elements"]
            if e.get("inlineObjectElement", {}).get("inlineObjectId") == image_id]
    if len(hits) != 1 or hits[0]["startIndex"] != index or hits[0]["endIndex"] != index + 1:
        raise ValueError
    elements.remove(hits[0])
    del objects[image_id]
    if "inlineObjects" not in previous:
        del selected["inlineObjects"]
    _unshift(selected["body"], index)
    # Named ranges are siblings of body, but their body coordinates move too.
    # Reverse only the one-unit insertion; identity/coverage/other segments must
    # still match in the complete document comparison below.
    for area in _body_named_ranges(selected, tab_id):
        _unshift(area, index)
    restored["revisionId"] = expected["revisionId"]
    if _coalesce(restored) != _coalesce(expected):
        raise ValueError
    return dimensions


def insert_image(client: Any, *, document: str, image_uri: str, expected_revision_id: str,
                 position: str = "end", anchor_text: str | None = None, tab_id: str | None = None,
                 width_pt: float | None = None, height_pt: float | None = None,
                 format_profile: str = "persian", apply: bool = False) -> dict[str, object]:
    """Preview by default; one non-replayed batch, then object-aware readback."""
    _validate_uri(image_uri)
    _validate_dimension(width_pt)
    _validate_dimension(height_pt)
    _validate_insertion("image", position, anchor_text, tab_id, format_profile, apply)
    context = prepare(client, document, expected_revision_id, tab_id)
    selected = context.selected
    index = _insertion_index(selected.body, _insertion_segments(selected.body), position, anchor_text)
    failed = False
    expected: dict[str, Any] = {}
    paragraph: dict[str, Any] = {}
    try:
        expected = _safe_copy(context.before)
        target = _selected_payload(expected, selected.tab_id)
        _body_named_ranges(target, selected.tab_id)
        paragraph = _paragraph_at(target["body"], index)["paragraph"]
        if not isinstance(target.get("inlineObjects", {}), dict):
            raise ValueError
    except Exception:
        failed = True
    if failed:
        raise invalid("The selected image insertion boundary or document structure is unsupported.")
    location = {"index": index, "tabId": selected.tab_id}
    image_request: dict[str, object] = {"uri": image_uri, "location": location}
    size = {k: {"magnitude": v, "unit": "PT"}
            for k, v in (("width", width_pt), ("height", height_pt)) if v is not None}
    if size:
        image_request["objectSize"] = size
    requests = [{"insertInlineImage": image_request}]
    if format_profile == "persian":
        style = paragraph_style_request(index + 1, selected.tab_id)
        assert style is not None
        style["updateParagraphStyle"]["range"]["startIndex"] = index
        requests.append(style)
        paragraph.setdefault("paragraphStyle", {}).update(deepcopy(style["updateParagraphStyle"]["paragraphStyle"]))
    _enforce_request_plan(requests)
    result: dict[str, object] = {
        "ok": True, "document_id": context.document_id, "document_url": context.document_url,
        "tab_id": selected.tab_id, "position": position, "index": index,
        "range": {"startIndex": index, "endIndex": index + 1, "tabId": selected.tab_id},
        "inserted_utf16_length": 1, "format_profile": format_profile, "applied": apply,
    }
    if not apply:
        return {**result, "revision_id": context.revision, "valid": True}
    # We need both the revision and insertion reply; _service_batch_revision
    # intentionally projects away replies, so use its revision parser directly.
    failure_code = "google_unavailable"
    failure_message = ""
    failure_retryable = False
    revision = image_id = ""
    dimensions: dict[str, float] = {}
    try:
        response = client.batch_update(context.document_id, requests, context.revision, retry_safe=False)
        failure_code = "verification_failed"
        revision = _response_revision(response)
        if revision == context.revision:
            raise ValueError
        replies = response["replies"]
        if not isinstance(replies, list) or len(replies) != len(requests):
            raise ValueError
        image_id = replies[0]["insertInlineImage"]["objectId"]
        if not isinstance(image_id, str) or not image_id or len(image_id) > 1000:
            raise ValueError
        after = _service_document(client, context.document_id)
        dimensions = _verify(after, expected, tab_id=selected.tab_id, index=index, image_id=image_id,
                             revision=revision, width_pt=width_pt, height_pt=height_pt,
                             format_profile=format_profile)
    except DocsMCPError as error:
        # Preserve canonical actionable codes (especially expired authorization),
        # but reconstruct messages rather than trusting an upstream exception.
        safe_errors = {
            "google_needs_reauth": _google_needs_reauth,
            "permission_denied": _permission_denied,
            "document_not_found": _document_not_found,
            "rate_limited": _rate_limited,
            "google_unavailable": _google_unavailable,
        }
        if error.code in safe_errors:
            safe = safe_errors[error.code]()
            failure_code = safe.code
            failure_message = safe.message + " "
            failure_retryable = error.retryable is True
        failed = True
    except Exception:
        failed = True
    if failed:
        # Raise outside the handler: upstream messages/exception chains can contain
        # private URLs. A write may already have succeeded; never replay it here.
        raise DocsMCPError(failure_code, failure_message + "Image insertion may have changed the document but could not be verified. Read the document afresh before attempting another write; do not replay automatically.", retryable=failure_retryable)
    return {**result, "before_revision_id": context.revision, "after_revision_id": revision,
            "image_id": image_id, "dimensions_pt": dimensions, "verified": True,
            "formatting_verified": format_profile == "persian", "dimensions_verified": True}
