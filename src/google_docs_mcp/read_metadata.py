"""Bounded source metadata for reads; never follows links or returns contentUri."""
import math
from typing import Any

from .client import DocsMCPError, _google_unavailable


_MAX_ITEMS = 10_000
_MAX_CHARS = 2_000_000
_MAX_NODES = 100_000


def _limit() -> DocsMCPError:
    return DocsMCPError("read_metadata_limit", "The selected tab exceeds the read metadata limit.")


def _text(value: Any, budget: list[int]) -> str:
    if not isinstance(value, str):
        raise TypeError
    budget[0] += len(value)
    if budget[0] > _MAX_CHARS:
        raise _limit()
    # Reject malformed Unicode without changing exact source URLs or text.
    value.encode("utf-8")
    return value


def _target(value: Any, budget: list[int]) -> dict:
    if not isinstance(value, dict) or len(value) != 1:
        raise TypeError
    kind, target = next(iter(value.items()))
    if kind in {"url", "tabId", "headingId", "bookmarkId"}:
        return {kind: _text(target, budget)}
    if kind not in {"heading", "bookmark"} or not isinstance(target, dict):
        raise TypeError
    if not target or not target.keys() <= {"id", "tabId"} or "id" not in target:
        raise TypeError
    return {kind: {key: _text(item, budget) for key, item in target.items()}}


def _position(node: dict, tab_id: str) -> dict:
    start, end = node.get("startIndex"), node.get("endIndex")
    for value in (start, end):
        if value is not None and (type(value) is not int or value < 0):
            raise TypeError
    if start is not None and end is not None and end < start:
        raise TypeError
    return {"tab_id": tab_id, "start_index": start, "end_index": end}


def _document_tab(document: dict, tab_id: str) -> dict:
    if "tabs" not in document:
        return document
    pending = list(reversed(document["tabs"]))
    selected = None
    visited = 0
    while pending:
        visited += 1
        if visited > _MAX_NODES or len(pending) > _MAX_NODES:
            raise _limit()
        tab = pending.pop()
        if tab["tabProperties"]["tabId"] == tab_id:
            if selected is not None:
                raise TypeError
            selected = tab["documentTab"]
        pending.extend(reversed(tab.get("childTabs", [])))
    if not isinstance(selected, dict):
        raise TypeError
    return selected


def _image(tab: dict, object_id: Any, kind: str, location: dict,
           budget: list[int]) -> dict | None:
    object_id = _text(object_id, budget)
    objects = tab.get(kind + "Objects", {})
    if not isinstance(objects, dict):
        raise TypeError
    obj = objects.get(object_id)
    # A non-text marker remains visible even when Google supplies no resolvable
    # image metadata (for example a drawing). Do not claim it is an image.
    if obj is None:
        return None
    embedded = obj.get(kind + "ObjectProperties", {}).get("embeddedObject", {})
    if "imageProperties" not in embedded:
        return None
    properties = embedded["imageProperties"]
    if not isinstance(properties, dict):
        raise TypeError
    size = embedded.get("size", {})
    if not isinstance(size, dict) or not size.keys() <= {"width", "height"}:
        raise TypeError
    projected_size = {}
    for key, dimension in size.items():
        if not isinstance(dimension, dict) or not dimension.keys() <= {"magnitude", "unit"}:
            raise TypeError
        magnitude = dimension.get("magnitude", 0)
        unit = dimension.get("unit", "PT")
        if (type(magnitude) not in {int, float} or not math.isfinite(magnitude)
                or magnitude < 0 or unit != "PT"):
            raise TypeError
        projected_size[key] = {"magnitude": magnitude, "unit": unit}
    return {"object_id": object_id, "kind": kind, **location,
            "title": _text(embedded.get("title", ""), budget),
            "description": _text(embedded.get("description", ""), budget),
            "size": projected_size, "source_uri": _text(properties.get("sourceUri", ""), budget)}


def _project(document: dict, tab_id: str) -> dict:
    tab = _document_tab(document, tab_id)
    links: list[dict] = []
    images: list[dict] = []
    budget = [0]
    pending = list(reversed(tab["body"].get("content", [])))
    visited = 0
    while pending:
        visited += 1
        if visited > _MAX_NODES or len(pending) > _MAX_NODES:
            raise _limit()
        node = pending.pop()
        if not isinstance(node, dict):
            raise TypeError
        if "paragraph" in node:
            paragraph = node["paragraph"]
            previous = None
            for element in paragraph.get("elements", []):
                visited += 1
                if visited > _MAX_NODES:
                    raise _limit()
                run = element.get("textRun")
                if run is not None:
                    style = run.get("textStyle", {})
                    if not isinstance(style, dict):
                        raise TypeError
                    if "link" in style:
                        link = {"text": [_text(run["content"], budget)],
                                "target": _target(style["link"], budget), **_position(element, tab_id)}
                        if (previous is not None and previous["target"] == link["target"]
                                and link["start_index"] is not None
                                and previous["end_index"] == link["start_index"]):
                            previous["text"].extend(link["text"])
                            previous["end_index"] = link["end_index"]
                        else:
                            links.append(link)
                            previous = link
                    else:
                        previous = None
                else:
                    previous = None
                    inline = element.get("inlineObjectElement")
                    if inline is not None:
                        image = _image(tab, inline["inlineObjectId"], "inline", _position(element, tab_id), budget)
                        if image is not None:
                            images.append(image)
                if len(links) + len(images) > _MAX_ITEMS:
                    raise _limit()
            positioned = paragraph.get("positionedObjectIds", [])
            if not isinstance(positioned, list):
                raise TypeError
            for object_id in positioned:
                visited += 1
                if visited > _MAX_NODES:
                    raise _limit()
                image = _image(tab, object_id, "positioned", _position(node, tab_id), budget)
                if image is not None:
                    images.append(image)
                if len(links) + len(images) > _MAX_ITEMS:
                    raise _limit()
        elif "table" in node:
            for row in reversed(node["table"].get("tableRows", [])):
                for cell in reversed(row.get("tableCells", [])):
                    pending.extend(reversed(cell.get("content", [])))
                    if len(pending) > _MAX_NODES:
                        raise _limit()
        elif "tableOfContents" in node:
            pending.extend(reversed(node["tableOfContents"].get("content", [])))
    for link in links:
        link["text"] = "".join(link["text"])
    return {"links": links, "images": images, "metadata_scope": "selected_tab_body"}


def read_metadata(document: dict, tab_id: str) -> dict:
    """Return whole-selected-body inventories, independent of text pagination."""
    failed = False
    try:
        return _project(document, tab_id)
    except DocsMCPError:
        raise
    except Exception:
        failed = True
    if failed:
        raise _google_unavailable()
    raise _google_unavailable()
