"""Narrow media/export adapters retain typed preview and closed error surfaces."""
import asyncio
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from google_docs_mcp import server


def test_pdf_text_oracle_preserves_ghostscript_surrogate_pairs(monkeypatch):
    import test_live_exports as oracle
    from types import SimpleNamespace
    monkeypatch.setattr(oracle.shutil, "which", lambda name: "gs")
    output = "marker \ud83e\uddea".encode("utf-8", "surrogatepass")
    monkeypatch.setattr(oracle.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=output))
    assert oracle._export_text(b"synthetic PDF input", "pdf") == "marker 🧪"


CASES = [
    ("docs_export", "exports", "export_document", {"document": "synthetic_doc_123"},
     {"format": "pdf", "scope": "all_tabs"}),
    ("docs_insert_image", "images", "insert_image",
     {"document": "synthetic_doc_123", "image_uri": "https://example.com/image.png", "expected_revision_id": "r1"},
     {"position": "end", "anchor_text": None, "tab_id": None, "width_pt": None,
      "height_pt": None, "format_profile": "persian", "apply": False}),
]


def invoke(name, arguments):
    result = asyncio.run(server.mcp.call_tool(name, arguments))
    assert isinstance(result, tuple) and isinstance(result[1], dict)
    return result[1]


@pytest.mark.parametrize("name,module,function,args,defaults", CASES)
def test_adapters_forward_exact_arguments(monkeypatch, name, module, function, args, defaults):
    calls = []
    stub = ModuleType(f"google_docs_mcp.{module}")
    setattr(stub, function, lambda *a, **kw: calls.append((a, kw)) or {"ok": True})
    monkeypatch.setitem(sys.modules, stub.__name__, stub)
    client = object()
    monkeypatch.setattr(server, "_get_service", lambda: SimpleNamespace(_client=client))
    assert invoke(name, args) == {"ok": True}
    assert calls[0][1] == {**args, **defaults}
    assert calls[0][0] == ((client, Path.home() / ".hermes/google-docs-mcp-exports")
                         if name == "docs_export" else (client,))


@pytest.mark.parametrize("name,module,function,args,defaults", CASES)
def test_adapters_sanitize_unknown_errors(monkeypatch, name, module, function, args, defaults):
    stub = ModuleType(f"google_docs_mcp.{module}")
    def fail(*a, **kw):
        raise RuntimeError("PRIVATE_CANARY")
    setattr(stub, function, fail)
    monkeypatch.setitem(sys.modules, stub.__name__, stub)
    monkeypatch.setattr(server, "_get_service", lambda: SimpleNamespace(_client=None))
    result = invoke(name, args)
    assert result["error"]["code"] == "google_unavailable"
    assert "PRIVATE_CANARY" not in str(result)


@pytest.mark.parametrize("name,args", [
    ("docs_export", {"format": "html"}),
    ("docs_export", {"scope": "selected_tab"}),
    ("docs_insert_image", {"apply": "true"}),
    ("docs_insert_image", {"width_pt": True}),
    ("docs_insert_image", {"height_pt": "25"}),
    ("docs_insert_image", {"width_pt": float("nan")}),
    ("docs_insert_image", {"width_pt": 0}),
    ("docs_insert_image", {"position": "middle"}),
])
def test_schema_rejects_bad_types_before_service(monkeypatch, name, args):
    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    assert name in tools
    monkeypatch.setattr(server, "_get_service", lambda: pytest.fail("must reject before service"))
    supplied = {"document": "synthetic_doc_123"}
    if name == "docs_insert_image":
        supplied.update(image_uri="https://example.com/image.png", expected_revision_id="r1")
    with pytest.raises(ToolError):
        invoke(name, {**supplied, **args})


def test_export_annotation_accounts_for_local_artifact_write():
    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    assert tools["docs_export"].annotations.model_dump(exclude_none=True) == {
        "readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True}
