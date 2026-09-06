"""Revision-guarded tab creation and property edits with exact tree readback."""
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .client import (
    DocsMCPError, _google_unavailable, _require_native_document,
    _response_revision, _service_document, _service_end_index,
    _service_metadata, _validate_service_revision, parse_document_id,
)
from .editing_common import invalid, verification_failed
from .markdown import _enforce_request_plan


_MAX_TABS = 100_000


def _identifier(value: Any) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= 1000
            and all(c.isprintable() and not c.isspace() for c in value))


def _title(value: Any) -> bool:
    return (isinstance(value, str) and bool(value.strip())
            and len(value) <= 200 and "\x00" not in value
            and not any(0xD800 <= ord(c) <= 0xDFFF for c in value))


def _properties(value: Any) -> dict:
    """Materialize only API scalar defaults, without discarding other fields."""
    if not isinstance(value, dict):
        raise _google_unavailable()
    result = deepcopy(value)
    if (not _identifier(result.get("tabId"))
            or not isinstance(result.get("title"), str)):
        raise _google_unavailable()
    for name, default in (("parentTabId", ""), ("index", 0),
                          ("nestingLevel", 0), ("iconEmoji", "")):
        result.setdefault(name, default)
    if (result["parentTabId"] != "" and not _identifier(result["parentTabId"])):
        raise _google_unavailable()
    if (type(result["index"]) is not int or result["index"] < 0
            or type(result["nestingLevel"]) is not int or result["nestingLevel"] < 0
            or not isinstance(result["iconEmoji"], str)):
        raise _google_unavailable()
    return result


@dataclass
class _Tree:
    document: dict
    payloads: dict[str, dict]
    children: dict[str, list[str]]


def _tree(document: dict) -> _Tree:
    """Validate actual ordering, parentage, depth and identity before planning."""
    try:
        roots = document.get("tabs")
        if not isinstance(roots, list) or not roots:
            raise _google_unavailable()
        result = _Tree(
            deepcopy({k: v for k, v in document.items() if k not in {"tabs", "revisionId"}}),
            {}, {"": []},
        )
        pending = [(iter(enumerate(roots)), "", 0)]
        while pending:
            iterator, parent, depth = pending[-1]
            try:
                index, node = next(iterator)
            except StopIteration:
                pending.pop()
                continue
            if not isinstance(node, dict) or len(result.payloads) >= _MAX_TABS:
                raise _google_unavailable()
            props = _properties(node.get("tabProperties"))
            tab_id = props["tabId"]
            if (tab_id in result.payloads or props["parentTabId"] != parent
                    or props["index"] != index or props["nestingLevel"] != depth):
                raise _google_unavailable()
            children = node.get("childTabs", [])
            content = node.get("documentTab")
            if not isinstance(children, list) or not isinstance(content, dict):
                raise _google_unavailable()
            _service_end_index(content["body"])
            payload = deepcopy({k: v for k, v in node.items() if k != "childTabs"})
            payload["tabProperties"] = props
            result.payloads[tab_id] = payload
            result.children[parent].append(tab_id)
            result.children[tab_id] = []
            pending.append((iter(enumerate(children)), tab_id, depth + 1))
        return result
    except DocsMCPError:
        raise
    except Exception:
        pass
    raise _google_unavailable()


def _relocate(tree: _Tree, tab_id: str, parent: str, index: int) -> None:
    """Apply the intended sibling shifts and all descendant depth changes."""
    props = tree.payloads[tab_id]["tabProperties"]
    old_parent = props["parentTabId"]
    tree.children[old_parent].remove(tab_id)
    tree.children[parent].insert(index, tab_id)
    props["parentTabId"] = parent
    for siblings in (tree.children[old_parent], tree.children[parent]):
        for position, sibling in enumerate(siblings):
            tree.payloads[sibling]["tabProperties"]["index"] = position
    depth = tree.payloads[parent]["tabProperties"]["nestingLevel"] + 1 if parent else 0
    pending = [(tab_id, depth)]
    while pending:
        child, level = pending.pop()
        tree.payloads[child]["tabProperties"]["nestingLevel"] = level
        pending.extend((descendant, level + 1) for descendant in tree.children[child])


def _batch(client: Any, document_id: str, requests: list[dict], revision: str) -> tuple[dict, str]:
    response = None
    try:
        # The raw reply is needed to attest the server-assigned new tab identity.
        response = client.batch_update(document_id, requests, revision, retry_safe=False)
    except DocsMCPError as error:
        raise DocsMCPError(error.code, error.message, retryable=False) from None
    except Exception:
        pass
    if response is None:
        raise _google_unavailable()
    try:
        next_revision = _validate_service_revision(_response_revision(response))
        if next_revision == revision:
            raise verification_failed()
        if response.get("documentId", document_id) != document_id:
            raise verification_failed()
        replies = response.get("replies")
        if not isinstance(replies, list) or len(replies) != 1 or not isinstance(replies[0], dict):
            raise verification_failed()
        return replies[0], next_revision
    except Exception:
        raise verification_failed() from None


def _verify_create(expected: _Tree, actual: _Tree, reply: dict, title: str,
                   parent: str, index: int) -> str:
    added = reply.get("addDocumentTab")
    if set(reply) != {"addDocumentTab"} or not isinstance(added, dict):
        raise verification_failed()
    properties = _properties(added.get("tabProperties"))
    tab_id = properties["tabId"]
    depth = expected.payloads[parent]["tabProperties"]["nestingLevel"] + 1 if parent else 0
    if (tab_id in expected.payloads or tab_id not in actual.payloads
            or properties["title"] != title or properties["parentTabId"] != parent
            or properties["index"] != index or properties["nestingLevel"] != depth
            or actual.payloads[tab_id]["tabProperties"] != properties
            or actual.children[tab_id]):
        raise verification_failed()
    expected.children[parent].insert(index, tab_id)
    expected.children[tab_id] = []
    # A new tab's default styles are server-owned. Existing payloads are not.
    expected.payloads[tab_id] = deepcopy(actual.payloads[tab_id])
    for position, sibling in enumerate(expected.children[parent]):
        expected.payloads[sibling]["tabProperties"]["index"] = position
    return tab_id


def manage_tab(client: Any, document: str, expected_revision_id: str,
               action: str, tab_id: str | None = None, title: str | None = None,
               parent_tab_id: str | None = None, index: int | None = None,
               apply: bool = False) -> dict[str, Any]:
    """Preview or apply create/rename/move; deletion is deliberately unsupported."""
    if (type(apply) is not bool or not isinstance(action, str)
            or action not in {"create", "rename", "move"}
            or (tab_id is not None and not _identifier(tab_id))
            or (parent_tab_id is not None and not _identifier(parent_tab_id))
            or (index is not None and (type(index) is not int or index < 0))):
        raise invalid()
    if action == "create":
        if tab_id is not None or not _title(title):
            raise invalid()
    elif action == "rename":
        if tab_id is None or not _title(title) or parent_tab_id is not None or index is not None:
            raise invalid()
    elif tab_id is None or title is not None or index is None:
        raise invalid()

    document_id = parse_document_id(document)
    revision = _validate_service_revision(expected_revision_id)
    metadata = _service_metadata(client, document_id)
    _require_native_document(metadata)
    before = _service_document(client, document_id)
    if before["revisionId"] != revision:
        raise DocsMCPError("stale_revision", "The Google document revision changed before the write.")
    expected = _tree(before)
    if tab_id is not None and tab_id not in expected.payloads:
        raise invalid("The tab_id must identify an existing tab.")
    parent = parent_tab_id if parent_tab_id is not None else ""
    if parent and parent not in expected.payloads:
        raise invalid("The parent_tab_id must identify an existing tab.")

    no_op = False
    if action == "create":
        siblings = expected.children[parent]
        index = len(siblings) if index is None else index
        if index > len(siblings):
            raise invalid()
        requests = [{"addDocumentTab": {"tabProperties": {
            "title": title, "parentTabId": parent, "index": index}}}]
    else:
        assert tab_id is not None  # Validated for both existing-tab actions.
        props = expected.payloads[tab_id]["tabProperties"]
        if action == "rename":
            parent, index = props["parentTabId"], props["index"]
            no_op = props["title"] == title
            props["title"] = title
            requests = [{"updateDocumentTabProperties": {
                "tabProperties": {"tabId": tab_id, "title": title}, "fields": "title"}}]
        else:
            assert index is not None  # Required by move argument validation.
            ancestor = parent
            while ancestor:
                if ancestor == tab_id:
                    raise invalid("A tab cannot be moved below itself or its descendants.")
                ancestor = expected.payloads[ancestor]["tabProperties"]["parentTabId"]
            available = len(expected.children[parent]) - (props["parentTabId"] == parent)
            if index > available:
                raise invalid()
            title = props["title"]
            no_op = props["parentTabId"] == parent and props["index"] == index
            _relocate(expected, tab_id, parent, index)
            requests = [{"updateDocumentTabProperties": {
                "tabProperties": {"tabId": tab_id, "parentTabId": parent, "index": index},
                "fields": "parentTabId,index"}}]
    _enforce_request_plan(requests)
    result = {
        "ok": True, "document_id": document_id, "document_url": metadata["webViewLink"],
        "action": action, "tab": {"tab_id": tab_id, "title": title,
                                  "parent_tab_id": parent or None, "index": index},
        "applied": apply, "no_op": no_op,
    }
    if no_op:
        return {**result, "revision_id": revision, "verified": True}
    if not apply:
        return {**result, "revision_id": revision, "valid": True}

    reply, next_revision = _batch(client, document_id, requests, revision)
    try:
        after = _service_document(client, document_id)
        if after["revisionId"] != next_revision:
            raise verification_failed()
        actual = _tree(after)
        if action == "create":
            assert isinstance(title, str) and isinstance(index, int)
            new_id = _verify_create(expected, actual, reply, title, parent, index)
            result["tab"]["tab_id"] = new_id
        elif reply:
            raise verification_failed()
        if expected != actual:
            raise verification_failed()
    except Exception:
        raise verification_failed() from None
    return {**result, "before_revision_id": revision, "after_revision_id": next_revision,
            "verified": True}
