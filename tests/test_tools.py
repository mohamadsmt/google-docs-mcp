import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from google_docs_mcp import server as server_module
from google_docs_mcp.client import DocsMCPError, GoogleDocsService, Replacement


EXPECTED_TOOLS = [
    "docs_read",
    "docs_create",
    "docs_replace_markdown",
    "docs_edit_text",
    "docs_insert_text",
]

EXPECTED_PROPERTIES = {
    "docs_insert_text": {
        "document", "text", "expected_revision_id", "position", "anchor_text",
        "tab_id", "format_profile", "apply",
    },
    "docs_read": {"document", "tab_id", "start", "max_chars"},
    "docs_create": {"title", "markdown", "format_profile"},
    "docs_replace_markdown": {
        "document",
        "markdown",
        "expected_revision_id",
        "tab_id",
        "format_profile",
    },
    "docs_edit_text": {
        "document",
        "replacements",
        "expected_revision_id",
        "tab_id",
        "apply",
    },
}

EXPECTED_REQUIRED = {
    "docs_insert_text": {"document", "text", "expected_revision_id"},
    "docs_read": {"document"},
    "docs_create": {"title"},
    "docs_replace_markdown": {
        "document",
        "markdown",
        "expected_revision_id",
    },
    "docs_edit_text": {"document", "replacements", "expected_revision_id"},
}

EXPECTED_DEFAULTS = {
    "docs_insert_text": {
        "position": "end", "anchor_text": None, "tab_id": None,
        "format_profile": "persian", "apply": False,
    },
    "docs_read": {"tab_id": None, "start": 0, "max_chars": 30_000},
    "docs_create": {"markdown": "", "format_profile": "persian"},
    "docs_replace_markdown": {
        "tab_id": None,
        "format_profile": "persian",
    },
    "docs_edit_text": {"tab_id": None, "apply": False},
}


async def _tools_by_name() -> dict[str, Any]:
    tools = await server_module.mcp.list_tools()
    assert [tool.name for tool in tools] == EXPECTED_TOOLS
    return {tool.name: tool for tool in tools}


async def _call_tool(name: str, arguments: dict[str, object]) -> dict[str, object]:
    result = await server_module.mcp.call_tool(name, arguments)
    assert isinstance(result, tuple)
    assert len(result) == 2
    structured = result[1]
    assert isinstance(structured, dict)
    return structured


def test_server_exposes_exact_tool_surface_and_closed_schemas() -> None:
    tools = asyncio.run(_tools_by_name())

    assert set(tools) == set(EXPECTED_TOOLS)
    forbidden_arguments = {"url", "method", "body", "headers", "header"}
    for name, tool in tools.items():
        schema = tool.inputSchema
        assert set(schema["properties"]) == EXPECTED_PROPERTIES[name]
        assert set(schema.get("required", [])) == EXPECTED_REQUIRED[name]
        assert forbidden_arguments.isdisjoint(schema["properties"])
        assert {
            parameter: schema["properties"][parameter]["default"]
            for parameter in EXPECTED_DEFAULTS[name]
        } == EXPECTED_DEFAULTS[name]
        assert tool.outputSchema == {
            "additionalProperties": True,
            "title": f"{name}DictOutput",
            "type": "object",
        }

    for name in ("docs_create", "docs_replace_markdown", "docs_insert_text"):
        profile = tools[name].inputSchema["properties"]["format_profile"]
        assert profile["enum"] == ["persian", "plain"]

    edit_schema = tools["docs_edit_text"].inputSchema
    replacement = edit_schema["$defs"]["ReplacementInput"]
    assert set(replacement["properties"]) == {
        "old_text",
        "new_text",
        "expected_count",
    }
    assert set(replacement["required"]) == {"old_text", "new_text"}
    assert replacement["properties"]["expected_count"]["default"] == 1
    assert replacement["properties"]["expected_count"]["minimum"] == 1


def test_server_annotations_fail_closed_for_the_exact_surface() -> None:
    tools = asyncio.run(_tools_by_name())

    assert tools["docs_read"].annotations.model_dump(exclude_none=True) == {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
    for name in EXPECTED_TOOLS[1:]:
        assert tools[name].annotations.model_dump(exclude_none=True) == {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": True,
        }


def test_list_tools_is_lazy_and_does_not_construct_service(monkeypatch) -> None:
    calls: list[str] = []

    def fail_if_called() -> GoogleDocsService:
        calls.append("service")
        raise AssertionError("service construction must be lazy")

    monkeypatch.setattr(server_module, "_get_service", fail_if_called)

    asyncio.run(server_module.mcp.list_tools())

    assert calls == []


def test_lazy_service_factory_purges_once_and_caches_service(monkeypatch) -> None:
    events: list[Any] = []
    credential = object()
    session = object()

    monkeypatch.setattr(server_module, "_service", None)
    monkeypatch.setattr(server_module, "_recovery_purged", False)
    monkeypatch.setattr(
        server_module,
        "purge_old_recovery",
        lambda root, now: events.append(("purge", root, now)),
    )
    monkeypatch.setattr(
        server_module,
        "load_credentials",
        lambda: events.append("credentials") or credential,
    )
    monkeypatch.setattr(
        server_module,
        "AuthorizedSession",
        lambda value: events.append(("session", value)) or session,
    )

    first = server_module._get_service()
    second = server_module._get_service()

    assert first is second
    assert isinstance(first, GoogleDocsService)
    assert first._client._session is session
    assert first._recovery_root == server_module._RECOVERY_ROOT
    assert events[0][0:2] == ("purge", server_module._RECOVERY_ROOT)
    purge_time = events[0][2]
    assert isinstance(purge_time, datetime)
    assert purge_time.tzinfo is timezone.utc
    assert events[1:] == ["credentials", ("session", credential)]


class RecordingService:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def read(self, *args: object, **kwargs: object) -> dict[str, object]:
        self.calls.append(("read", args, kwargs))
        return {"ok": True, "operation": "read"}

    def create(self, *args: object, **kwargs: object) -> dict[str, object]:
        self.calls.append(("create", args, kwargs))
        return {"ok": True, "operation": "create"}

    def replace_markdown(
        self, *args: object, **kwargs: object
    ) -> dict[str, object]:
        self.calls.append(("replace", args, kwargs))
        return {"ok": True, "operation": "replace"}

    def edit_text(self, *args: object, **kwargs: object) -> dict[str, object]:
        self.calls.append(("edit", args, kwargs))
        return {"ok": True, "operation": "edit"}


def test_tools_map_typed_inputs_to_service_without_expanding_surface(monkeypatch) -> None:
    service = RecordingService()
    monkeypatch.setattr(server_module, "_get_service", lambda: service)

    assert asyncio.run(
        _call_tool(
            "docs_read",
            {
                "document": "document123",
                "tab_id": "tab-1",
                "start": 7,
                "max_chars": 99,
            },
        )
    ) == {"ok": True, "operation": "read"}
    assert asyncio.run(
        _call_tool(
            "docs_create",
            {
                "title": "Title",
                "markdown": "# Body",
                "format_profile": "plain",
            },
        )
    ) == {"ok": True, "operation": "create"}
    assert asyncio.run(
        _call_tool(
            "docs_replace_markdown",
            {
                "document": "document123",
                "markdown": "Body",
                "expected_revision_id": "rev-1",
                "tab_id": "tab-1",
                "format_profile": "persian",
            },
        )
    ) == {"ok": True, "operation": "replace"}
    assert asyncio.run(
        _call_tool(
            "docs_edit_text",
            {
                "document": "document123",
                "replacements": [
                    {"old_text": "old", "new_text": "new", "expected_count": 2},
                    {
                        "old_text": "alpha",
                        "new_text": "beta",
                        "expected_count": 1,
                    },
                ],
                "expected_revision_id": "rev-1",
                "tab_id": "tab-1",
                "apply": True,
            },
        )
    ) == {"ok": True, "operation": "edit"}

    assert service.calls == [
        (
            "read",
            ("document123",),
            {"tab_id": "tab-1", "start": 7, "max_chars": 99},
        ),
        (
            "create",
            ("Title",),
            {"markdown": "# Body", "format_profile": "plain"},
        ),
        (
            "replace",
            ("document123", "Body", "rev-1"),
            {"tab_id": "tab-1", "format_profile": "persian"},
        ),
        (
            "edit",
            (
                "document123",
                [
                    Replacement("old", "new", 2),
                    Replacement("alpha", "beta", 1),
                ],
                "rev-1",
            ),
            {"tab_id": "tab-1", "apply": True},
        ),
    ]


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("docs_read", {"document": "document123", "start": "1"}),
        ("docs_read", {"document": "document123", "max_chars": True}),
        (
            "docs_edit_text",
            {
                "document": "document123",
                "expected_revision_id": "rev-1",
                "replacements": [
                    {
                        "old_text": "old",
                        "new_text": "new",
                        "expected_count": True,
                    }
                ],
            },
        ),
        (
            "docs_edit_text",
            {
                "document": "document123",
                "expected_revision_id": "rev-1",
                "replacements": [
                    {"old_text": "old", "new_text": "new", "expected_count": "1"}
                ],
            },
        ),
        (
            "docs_edit_text",
            {
                "document": "document123",
                "expected_revision_id": "rev-1",
                "replacements": [{"old_text": "old", "new_text": "new"}],
                "apply": 1,
            },
        ),
        (
            "docs_edit_text",
            {
                "document": "document123",
                "expected_revision_id": "rev-1",
                "replacements": [{"old_text": "old", "new_text": "new"}],
                "apply": "false",
            },
        ),
        (
            "docs_edit_text",
            {
                "document": "document123",
                "expected_revision_id": "rev-1",
                "replacements": [{"old_text": 7, "new_text": "new"}],
            },
        ),
        (
            "docs_edit_text",
            {
                "document": "document123",
                "expected_revision_id": "rev-1",
                "replacements": [{"old_text": "old", "new_text": 7}],
            },
        ),
    ],
)
def test_registry_rejects_type_coercion_before_service_dispatch(
    monkeypatch,
    tool_name: str,
    arguments: dict[str, object],
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        server_module,
        "_get_service",
        lambda: calls.append("service") or RecordingService(),
    )

    with pytest.raises(ToolError):
        asyncio.run(server_module.mcp.call_tool(tool_name, arguments))

    assert calls == []


def test_domain_service_error_returns_typed_json_without_traceback(monkeypatch) -> None:
    class FailingService:
        def read(self, *args: object, **kwargs: object) -> dict[str, object]:
            raise DocsMCPError(
                "permission_denied",
                "Google Docs access was denied.",
            )

    monkeypatch.setattr(server_module, "_get_service", lambda: FailingService())

    result = asyncio.run(_call_tool("docs_read", {"document": "document123"}))

    assert result == {
        "ok": False,
        "error": {
            "code": "permission_denied",
            "message": "Google Docs access was denied.",
            "retryable": False,
        },
    }
    assert "Traceback" not in repr(result)


def test_unexpected_service_error_is_typed_and_does_not_echo_details(monkeypatch) -> None:
    canary = "CANARY_UNEXPECTED_SERVICE_SECRET"

    class FailingService:
        def read(self, *args: object, **kwargs: object) -> dict[str, object]:
            raise RuntimeError(canary)

    monkeypatch.setattr(server_module, "_get_service", lambda: FailingService())

    result = asyncio.run(_call_tool("docs_read", {"document": "document123"}))

    assert result == {
        "ok": False,
        "error": {
            "code": "google_unavailable",
            "message": "Google services are unavailable.",
            "retryable": False,
        },
    }
    assert canary not in repr(result)
    assert "Traceback" not in repr(result)


def test_base_exception_is_not_swallowed_by_tool_error_mapping(monkeypatch) -> None:
    class StopSignal(BaseException):
        pass

    class StoppingService:
        def read(self, *args: object, **kwargs: object) -> dict[str, object]:
            raise StopSignal

    monkeypatch.setattr(server_module, "_get_service", lambda: StoppingService())

    with pytest.raises(StopSignal):
        asyncio.run(
            server_module.mcp.call_tool(
                "docs_read",
                {"document": "document123"},
            )
        )
