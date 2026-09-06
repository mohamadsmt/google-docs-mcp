"""Synthetic trees and readbacks; no API emulator derives expected changes."""
from copy import deepcopy
import importlib

import pytest

from google_docs_mcp.client import DocsMCPError

DOC = "synthetic_doc_123"


def tab(tab_id, title, index, parent="", depth=0):
    text = f"{title}🧪\n"
    end = 1 + len(text.encode("utf-16-le")) // 2
    return {"tabProperties": {"tabId": tab_id, "title": title, "index": index,
                              "parentTabId": parent, "nestingLevel": depth, "iconEmoji": "🧪"},
            "documentTab": {"body": {"content": [{"endIndex": 1, "sectionBreak": {"sectionStyle": {}}},
                {"startIndex": 1, "endIndex": end, "paragraph": {
                    "paragraphStyle": {"namedStyleType": "HEADING_1"},
                    "elements": [{"startIndex": 1, "endIndex": end, "textRun": {
                        "content": text, "textStyle": {"bold": True, "link": {"url": "https://example.org/"}}}}]}}]},
                "footnotes": {"f1": {"content": []}}, "lists": {"l1": {"listProperties": {}}}}}


def sample():
    a, b, c = tab("a", "Alpha", 0), tab("b", "Beta", 1), tab("c", "Gamma", 2)
    a["childTabs"] = [tab("a1", "Child", 0, "a", 1), tab("a2", "Sibling", 1, "a", 1)]
    a["childTabs"][0]["childTabs"] = [tab("leaf", "Leaf", 0, "a1", 2)]
    return {"documentId": DOC, "revisionId": "r1", "title": "Synthetic", "tabs": [a, b, c]}


def created(parent=None, index=None):
    after = sample()
    properties = {"tabId": "new", "title": "New 🧪", "parentTabId": parent or "", "index": index if index is not None else (3 if parent is None else 2),
                  "nestingLevel": 0 if parent is None else 1}
    new = {"tabProperties": deepcopy(properties), "documentTab": {"body": {"content": [
        {"endIndex": 1, "sectionBreak": {"sectionStyle": {}}},
        {"startIndex": 1, "endIndex": 2, "paragraph": {"elements": [{"startIndex": 1, "endIndex": 2, "textRun": {"content": "\n"}}]}}]}}}
    if parent is None:
        if index is None:
            after["tabs"].append(new)
        else:
            after["tabs"].insert(1, new)
            after["tabs"][2]["tabProperties"]["index"] = 2
            after["tabs"][3]["tabProperties"]["index"] = 3
    else:
        after["tabs"][0]["childTabs"].append(new)
    after["revisionId"] = "r2"
    response = {"writeControl": {"requiredRevisionId": "r2"}, "replies": [{"addDocumentTab": {"tabProperties": properties}}]}
    return after, response


def renamed():
    after = sample()
    after["tabs"][0]["childTabs"][0]["tabProperties"]["title"] = "Renamed 🧪"
    after["revisionId"] = "r2"
    return after


def moved(kind):
    after = sample()
    if kind == "reorder":
        a, b, c = after["tabs"]
        after["tabs"] = [c, a, b]
        c["tabProperties"]["index"] = 0
        a["tabProperties"]["index"] = 1
        b["tabProperties"]["index"] = 2
    elif kind == "reparent":
        a1, a2 = after["tabs"][0]["childTabs"]
        after["tabs"][0]["childTabs"] = [a2]
        a2["tabProperties"]["index"] = 0
        after["tabs"][1]["childTabs"] = [a1]
        a1["tabProperties"]["parentTabId"] = "b"
    elif kind == "root":
        a1, a2 = after["tabs"][0]["childTabs"]
        after["tabs"][0]["childTabs"] = [a2]
        a2["tabProperties"]["index"] = 0
        after["tabs"].append(a1)
        a1["tabProperties"].update({"index": 3, "parentTabId": "", "nestingLevel": 0})
        a1["childTabs"][0]["tabProperties"]["nestingLevel"] = 1
    elif kind == "child_reorder":
        a1, a2 = after["tabs"][0]["childTabs"]
        after["tabs"][0]["childTabs"] = [a2, a1]
        a2["tabProperties"]["index"] = 0
        a1["tabProperties"]["index"] = 1
    after["revisionId"] = "r2"
    return after


class Client:
    def __init__(self, before=None, after=None, response=None, failure=None):
        self.before = sample() if before is None else before
        self.after = after
        self.response = response if response is not None else {"writeControl": {"requiredRevisionId": "r2"}, "replies": [{}]}
        self.failure = failure
        self.writes = []

    def drive_metadata(self, document_id):
        return {"id": document_id, "name": "Synthetic", "mimeType": "application/vnd.google-apps.document",
                "modifiedTime": "2026-01-01T00:00:00Z", "version": "1",
                "webViewLink": f"https://docs.google.com/document/d/{document_id}/edit"}

    def get_document(self, document_id):
        return deepcopy(self.after if self.writes else self.before)

    def batch_update(self, document_id, requests, revision, *, retry_safe=True):
        self.writes.append((document_id, deepcopy(requests), revision, retry_safe))
        if self.failure:
            raise self.failure
        return deepcopy(self.response)


def run(client, action="create", **kwargs):
    return importlib.import_module("google_docs_mcp.tabs").manage_tab(client, DOC, kwargs.pop("expected_revision_id", "r1"), action, **kwargs)


@pytest.mark.parametrize("parent,index", [(None, None), (None, 1), ("a", None)])
def test_create_preview_and_apply_exact_identity_and_sibling_shifts(parent, index):
    after, response = created(parent, index)
    client = Client(after=after, response=response)
    preview = run(client, title="New 🧪", parent_tab_id=parent, index=index)
    resolved_index = index if index is not None else (3 if parent is None else 2)
    assert preview["tab"] == {"tab_id": None, "title": "New 🧪", "parent_tab_id": parent, "index": resolved_index}
    assert preview["applied"] is False and not client.writes
    result = run(client, title="New 🧪", parent_tab_id=parent, index=index, apply=True)
    assert result["verified"] is True and result["tab"]["tab_id"] == "new"
    assert result["after_revision_id"] == "r2"
    assert client.writes == [(DOC, [{"addDocumentTab": {"tabProperties": {
        "title": "New 🧪", "parentTabId": parent or "", "index": resolved_index}}}], "r1", False)]


def test_rename_nested_tab_preserves_subtree_and_body_with_title_only_mask():
    client = Client(after=renamed())
    preview = run(client, "rename", tab_id="a1", title="Renamed 🧪")
    assert preview["tab"] == {"tab_id": "a1", "title": "Renamed 🧪", "parent_tab_id": "a", "index": 0}
    assert not client.writes
    result = run(client, "rename", tab_id="a1", title="Renamed 🧪", apply=True)
    assert result["verified"] is True
    assert client.writes == [(DOC, [{"updateDocumentTabProperties": {
        "tabProperties": {"tabId": "a1", "title": "Renamed 🧪"}, "fields": "title"}}], "r1", False)]


@pytest.mark.parametrize("kind,tab_id,parent,index", [
    ("reorder", "c", None, 0), ("reparent", "a1", "b", 0),
    ("root", "a1", None, 3), ("child_reorder", "a1", "a", 1)])
def test_move_preview_and_apply_exact_reorder_reparent_and_descendant_depth(kind, tab_id, parent, index):
    client = Client(after=moved(kind))
    preview = run(client, "move", tab_id=tab_id, parent_tab_id=parent, index=index)
    assert preview["tab"]["index"] == index and preview["tab"]["parent_tab_id"] == parent
    assert not client.writes
    result = run(client, "move", tab_id=tab_id, parent_tab_id=parent, index=index, apply=True)
    assert result["verified"] is True
    assert client.writes == [(DOC, [{"updateDocumentTabProperties": {
        "tabProperties": {"tabId": tab_id, "parentTabId": parent or "", "index": index},
        "fields": "parentTabId,index"}}], "r1", False)]


@pytest.mark.parametrize("action,args", [("rename", {"tab_id": "a1", "title": "Child"}),
    ("move", {"tab_id": "a1", "parent_tab_id": "a", "index": 0}), ("move", {"tab_id": "b", "index": 1})])
def test_compliant_operation_is_verified_noop(action, args):
    client = Client()
    result = run(client, action, apply=True, **args)
    assert result["verified"] is True and result["no_op"] is True and result["revision_id"] == "r1"
    assert not client.writes


@pytest.mark.parametrize("action,args", [
    ("delete", {"tab_id": "a"}), (None, {}), ([], {}),
    ("create", {}), ("create", {"title": "   "}), ("create", {"title": "x" * 201}),
    ("create", {"title": "new\x00"}), ("create", {"title": 1}),
    ("create", {"title": "New", "tab_id": "a"}),
    ("create", {"title": "New", "parent_tab_id": "missing"}),
    ("create", {"title": "New", "parent_tab_id": ""}),
    ("create", {"title": "New", "index": 4}),
    ("create", {"title": "New", "index": True}),
    ("create", {"title": "New", "index": 0.0}),
    ("rename", {"title": "New"}), ("rename", {"tab_id": "a"}),
    ("rename", {"tab_id": "a", "title": "New", "parent_tab_id": "a"}),
    ("rename", {"tab_id": "a", "title": "New", "index": 0}),
    ("rename", {"tab_id": "missing", "title": "New"}),
    ("move", {"tab_id": "a"}), ("move", {"index": 0}),
    ("move", {"tab_id": "a", "index": 0, "title": "New"}),
    ("move", {"tab_id": "a", "index": 3}), # Destination after source removal has only two entries.
    ("move", {"tab_id": "a", "index": -1}), ("move", {"tab_id": "a", "index": "0"}),
    ("move", {"tab_id": "a", "index": False}),
    ("move", {"tab_id": "a", "index": 0, "parent_tab_id": "a"}),
    ("move", {"tab_id": "a", "index": 0, "parent_tab_id": "leaf"}),
    ("move", {"tab_id": "a", "index": 0, "parent_tab_id": "missing"}),
    ("move", {"tab_id": [], "index": 0}),
    ("create", {"title": "New", "apply": 1}),
])
def test_invalid_actions_and_irrelevant_parameters_never_write(action, args):
    client = Client()
    with pytest.raises(DocsMCPError):
        run(client, action, **args)
    assert not client.writes


@pytest.mark.parametrize("action,args", [("create", {"title": "New"}), ("rename", {"tab_id": "a", "title": "New"}),
    ("move", {"tab_id": "b", "index": 0})])
def test_stale_revision_blocks_every_action(action, args):
    client = Client()
    with pytest.raises(DocsMCPError) as error:
        run(client, action, expected_revision_id="stale", apply=True, **args)
    assert error.value.code == "stale_revision" and not client.writes


@pytest.mark.parametrize("corruption", ["wrong_id", "extra_tab", "title", "parent", "index", "body", "icon", "revision", "sibling", "tree"])
def test_create_exact_readback_rejects_wrong_identity_or_preservation(corruption):
    after, response = created()
    new = after["tabs"][3]
    if corruption == "wrong_id":
        new["tabProperties"]["tabId"] = "wrong"
    elif corruption == "extra_tab":
        after["tabs"].append(tab("extra", "Extra", 4))
    elif corruption in {"title", "parent", "index"}:
        key = {"title": "title", "parent": "parentTabId", "index": "index"}[corruption]
        new["tabProperties"][key] = 0 if corruption == "index" else "wrong"
    elif corruption == "body":
        after["tabs"][1]["documentTab"]["body"] = {}
    elif corruption == "icon":
        after["tabs"][1]["tabProperties"]["iconEmoji"] = "🚨"
    elif corruption == "revision":
        after["revisionId"] = "r3"
    elif corruption == "sibling":
        after["tabs"][1]["tabProperties"]["index"] = 2
    else:
        after["tabs"][0]["childTabs"].reverse()
    with pytest.raises(DocsMCPError):
        run(Client(after=after, response=response), title="New 🧪", apply=True)


@pytest.mark.parametrize("kind,action,args", [("rename", "rename", {"tab_id": "a1", "title": "Renamed 🧪"}),
    ("reorder", "move", {"tab_id": "c", "index": 0}), ("reparent", "move", {"tab_id": "a1", "parent_tab_id": "b", "index": 0}),
    ("root", "move", {"tab_id": "a1", "index": 3})])
@pytest.mark.parametrize("damage", ["unchanged", "body", "other_property", "document_property"])
def test_rename_and_move_verify_exact_change_and_all_existing_payloads(kind, action, args, damage):
    after = renamed() if kind == "rename" else moved(kind)
    if damage == "unchanged":
        after = sample()
        after["revisionId"] = "r2"
    elif damage == "body":
        after["tabs"][0]["documentTab"]["footnotes"] = {}
    elif damage == "other_property":
        after["tabs"][0]["tabProperties"]["iconEmoji"] = "🚨"
    else:
        after["title"] = "wrong"
    with pytest.raises(DocsMCPError) as error:
        run(Client(after=after), action, apply=True, **args)
    assert error.value.code == "verification_failed"


@pytest.mark.parametrize("corruption", ["missing", "empty", "duplicate", "title", "parent", "index", "replies", "revision"])
def test_create_requires_valid_exact_add_reply(corruption):
    after, response = created()
    props = response["replies"][0]["addDocumentTab"]["tabProperties"]
    if corruption == "missing":
        response["replies"] = [{}]
    elif corruption == "empty":
        props["tabId"] = ""
    elif corruption == "duplicate":
        props["tabId"] = "a"
    elif corruption in {"title", "parent", "index"}:
        props[{"title": "title", "parent": "parentTabId", "index": "index"}[corruption]] = 0 if corruption == "index" else "wrong"
    elif corruption == "replies":
        response["replies"].append({})
    else:
        response["writeControl"]["requiredRevisionId"] = "r1"
    client = Client(after=after, response=response)
    with pytest.raises(DocsMCPError):
        run(client, title="New 🧪", apply=True)
    assert len(client.writes) == 1


@pytest.mark.parametrize("malformed", ["duplicate", "parent", "index", "children", "empty", "depth"])
def test_malformed_remote_tree_never_writes(malformed):
    before = sample()
    if malformed == "duplicate":
        before["tabs"][1]["tabProperties"]["tabId"] = "a"
    elif malformed == "parent":
        before["tabs"][0]["childTabs"][0]["tabProperties"]["parentTabId"] = "c"
    elif malformed == "index":
        before["tabs"][1]["tabProperties"]["index"] = True
    elif malformed == "children":
        before["tabs"][0]["childTabs"] = None
    elif malformed == "empty":
        before["tabs"] = []
    else:
        before["tabs"][0]["childTabs"][0]["tabProperties"]["nestingLevel"] = 0
    client = Client(before=before)
    with pytest.raises(DocsMCPError):
        run(client, title="New", apply=True)
    assert not client.writes


@pytest.mark.parametrize("action,args", [("create", {"title": "New"}), ("rename", {"tab_id": "a", "title": "New"}),
    ("move", {"tab_id": "b", "index": 0})])
def test_uncertain_remote_failure_sanitized_and_never_retried(action, args):
    client = Client(failure=RuntimeError("secret_token"))
    with pytest.raises(DocsMCPError) as error:
        run(client, action, apply=True, **args)
    assert "secret_token" not in str(error.value) and error.value.retryable is False
    assert len(client.writes) == 1 and client.writes[0][3] is False
