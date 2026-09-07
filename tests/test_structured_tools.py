"""The four structured operations remain thin, typed MCP adapters."""
import asyncio
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from google_docs_mcp import server
from google_docs_mcp.client import DocsMCPError, GoogleDocsService


CASES = [
    ("docs_edit_section", "sections", "edit_section", {"markdown": "**hello**"},
     {"action": "insert", "position": "end", "anchor_text": None, "heading_text": None,
      "tab_id": None, "format_profile": "persian", "apply": False}, True),
    ("docs_format", "formatting", "format_document", {},
     {"tab_id": None, "heading_text": None, "format_profile": "persian", "right_indent_pt": 0,
      "apply": False}, False),
    ("docs_manage_tab", "tabs", "manage_tab", {"action": "create", "title": "New"},
     {"tab_id": None, "parent_tab_id": None, "index": None, "apply": False}, False),
    ("docs_edit_table", "tables", "edit_table", {"action": "set_cell", "table_index": 0,
     "row_index": 0, "column_index": 1, "markdown": "value"},
     {"side": None, "tab_id": None, "format_profile": "persian", "apply": False}, True),
]


def invoke(name, arguments):
    result = asyncio.run(server.mcp.call_tool(name, arguments))
    assert isinstance(result, tuple) and isinstance(result[1], dict)
    return result[1]


@pytest.mark.parametrize("name,module,function,arguments,defaults,recovery", CASES)
def test_structured_adapter_forwards_every_argument(monkeypatch, name, module, function, arguments, defaults, recovery):
    calls = []
    stub = ModuleType(f"google_docs_mcp.{module}")
    setattr(stub, function, lambda *args, **kwargs: calls.append((args, kwargs)) or {"ok": True, "test": name})
    monkeypatch.setitem(sys.modules, stub.__name__, stub)
    client = object()
    service = SimpleNamespace(_client=client, _recovery_root=Path("synthetic-recovery"))
    monkeypatch.setattr(server, "_get_service", lambda: service)
    supplied = {"document": "synthetic_doc_123", "expected_revision_id": "revision", **arguments}
    assert invoke(name, supplied) == {"ok": True, "test": name}
    assert calls == [((client, Path("synthetic-recovery")) if recovery else (client,),
                      {**supplied, **defaults})]


@pytest.mark.parametrize("name,module,function,arguments,defaults,recovery", CASES)
def test_structured_adapters_redact_unexpected_failures(monkeypatch, name, module, function, arguments, defaults, recovery):
    stub = ModuleType(f"google_docs_mcp.{module}")
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic_sensitive_canary")
    setattr(stub, function, fail)
    monkeypatch.setitem(sys.modules, stub.__name__, stub)
    monkeypatch.setattr(server, "_get_service", lambda: SimpleNamespace(_client=None, _recovery_root=Path("x")))
    result = invoke(name, {"document": "synthetic_doc_123", "expected_revision_id": "r1", **arguments})
    assert result["error"]["code"] == "google_unavailable"
    assert "synthetic_sensitive_canary" not in str(result)


@pytest.mark.parametrize("name,args", [
    ("docs_edit_section", {"markdown": "text", "apply": "true"}),
    ("docs_edit_section", {"markdown": "text", "position": "middle"}),
    ("docs_format", {"right_indent_pt": True}),
    ("docs_format", {"right_indent_pt": "12"}),
    ("docs_format", {"right_indent_pt": float("nan")}),
    ("docs_format", {"right_indent_pt": 145}),
    ("docs_format", {"format_profile": "plain"}),
    ("docs_manage_tab", {"action": "delete", "tab_id": "t1"}),
    ("docs_manage_tab", {"action": "move", "index": "1"}),
    ("docs_edit_table", {"action": "set_cell", "table_index": True}),
    ("docs_edit_table", {"action": "merge", "table_index": 0}),
])
def test_structured_schemas_reject_invalid_types_before_service(monkeypatch, name, args):
    assert name in {tool.name for tool in asyncio.run(server.mcp.list_tools())}
    def fail():
        pytest.fail("schema validation must precede service")
    monkeypatch.setattr(server, "_get_service", fail)
    with pytest.raises(ToolError):
        invoke(name, {"document": "synthetic_doc_123", "expected_revision_id": "r1", **args})


def test_read_adds_nontext_table_inventory_without_changing_content():
    from test_editing_common import Client, sample
    doc = sample()
    result = GoogleDocsService(cast(Any, Client(doc)), Path("unused")).read("synthetic_doc_123")
    assert result["tables"] == []
    assert "Title" in str(result["content"])
    assert result["outline"]


@pytest.mark.parametrize("tab_id", [None, "t1", "t2"])
def test_read_missing_revision_preserves_exact_multitab_result(tab_id, monkeypatch, tmp_path):
    """Google omits revisionId for viewers/commenters, not document content."""
    from copy import deepcopy
    from test_editing_common import Client, paragraph, sample

    doc = sample()
    second = deepcopy(doc["tabs"][0])
    second["tabProperties"] = {"tabId": "t2", "title": "Selected"}
    second["documentTab"]["body"]["content"] = [paragraph("انتخاب🧪\n", 1)]
    doc["tabs"].append(second)
    client = Client(doc)
    service = GoogleDocsService(cast(Any, client), tmp_path / "unused")
    monkeypatch.setattr(server, "_get_service", lambda: service)
    args = {"document": "synthetic_doc_123", "tab_id": tab_id, "start": 1, "max_chars": 3}
    expected = invoke("docs_read", args)
    assert expected["ok"] is True
    assert expected["revision_id"] == "r1"
    if tab_id is not None:
        assert expected["tab_id"] == tab_id
        assert len(expected["content"]) == 3
    del doc["revisionId"]
    expected["revision_id"] = None
    assert invoke("docs_read", args) == expected
    assert not (tmp_path / "unused").exists()


@pytest.mark.parametrize("damage", ["null_revision", "empty_revision", "numeric_revision", "wrong_id"])
def test_read_still_rejects_malformed_document_identity_or_revision(damage, monkeypatch, tmp_path):
    from test_editing_common import Client, sample

    doc = sample()
    if damage == "wrong_id":
        doc["documentId"] = "another_doc_123"
        del doc["revisionId"]
    else:
        doc["revisionId"] = {"null_revision": None, "empty_revision": "", "numeric_revision": 123}[damage]
    service = GoogleDocsService(cast(Any, Client(doc)), tmp_path / "unused")
    monkeypatch.setattr(server, "_get_service", lambda: service)
    result = invoke("docs_read", {"document": "synthetic_doc_123", "tab_id": "t1"})
    assert result["ok"] is False
    assert result["error"]["code"] == "google_unavailable"
    assert "content" not in result


@pytest.mark.parametrize("name,extra", [
    ("docs_replace_markdown", {"markdown": "replacement", "format_profile": "plain"}),
    ("docs_edit_text", {"replacements": [{"old_text": "Title", "new_text": "New", "expected_count": 1}]}),
    ("docs_insert_text", {"text": "inserted", "format_profile": "plain"}),
    ("docs_edit_section", {"markdown": "inserted", "format_profile": "plain"}),
    ("docs_format", {"format_profile": "english"}),
    ("docs_manage_tab", {"action": "create", "title": "New"}),
    ("docs_edit_table", {"action": "insert_row", "table_index": 0, "row_index": 0, "side": "after"}),
])
@pytest.mark.parametrize("apply", [False, True])
def test_missing_revision_keeps_every_existing_document_write_closed(name, extra, apply, monkeypatch, tmp_path):
    from copy import deepcopy
    from test_editing_common import Client, sample

    doc = sample()
    del doc["revisionId"]
    before = deepcopy(doc)
    calls = []

    class NoMutationClient(Client):
        def batch_update(self, *args, **kwargs):
            calls.append("batch_update")
            raise AssertionError("must reject before mutation")

        def export_file(self, *args, **kwargs):
            calls.append("export_file")
            raise AssertionError("must reject before recovery preparation")

    service = GoogleDocsService(cast(Any, NoMutationClient(doc)), tmp_path / "unused")
    monkeypatch.setattr(server, "_get_service", lambda: service)
    args = {"document": "synthetic_doc_123", "tab_id": "t1", "expected_revision_id": "r1", **extra}
    if name == "docs_manage_tab":
        args["tab_id"] = None  # Creation has no existing target tab.
    if name != "docs_replace_markdown":
        args["apply"] = apply
    result = invoke(name, args)
    assert result["ok"] is False
    expected_code = "stale_revision" if name == "docs_edit_text" else "google_unavailable"
    assert result["error"]["code"] == expected_code
    assert calls == []
    assert doc == before
    assert not (tmp_path / "unused").exists()
