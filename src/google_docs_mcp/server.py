from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from google.auth.transport.requests import AuthorizedSession
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field, StrictBool, StrictInt

from .client import (
    DocsMCPError,
    GoogleDocsClient,
    GoogleDocsService,
    Replacement,
    load_credentials,
    purge_old_recovery,
)


mcp = FastMCP("google-docs", log_level="ERROR")

_RECOVERY_ROOT = Path.home() / ".hermes/google-docs-mcp-recovery"
_service: GoogleDocsService | None = None
_recovery_purged = False

_READ_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
_WRITE_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
)


class ReplacementInput(BaseModel):
    old_text: str
    new_text: str
    expected_count: StrictInt = Field(default=1, ge=1)


def _prepare_runtime() -> None:
    global _recovery_purged
    if not _recovery_purged:
        purge_old_recovery(_RECOVERY_ROOT, datetime.now(timezone.utc))
        _recovery_purged = True


def _get_service() -> GoogleDocsService:
    global _service
    if _service is None:
        _prepare_runtime()
        credentials = load_credentials()
        session = AuthorizedSession(credentials)
        _service = GoogleDocsService(GoogleDocsClient(session), _RECOVERY_ROOT)
    return _service


def _unexpected_error_result() -> dict[str, object]:
    return DocsMCPError(
        "google_unavailable",
        "Google services are unavailable.",
    ).as_result()


def _call_service(
    operation: Callable[[], dict[str, object]],
) -> dict[str, object]:
    try:
        return operation()
    except DocsMCPError as error:
        return error.as_result()
    except Exception:
        return _unexpected_error_result()


@mcp.tool(
    name="docs_read",
    structured_output=True,
    annotations=_READ_ANNOTATIONS,
)
def docs_read(
    document: str,
    tab_id: str | None = None,
    start: StrictInt = 0,
    max_chars: StrictInt = 30_000,
) -> dict[str, object]:
    """Read bounded content, tab metadata, and the current Docs revision."""
    return _call_service(
        lambda: _get_service().read(
            document,
            tab_id=tab_id,
            start=start,
            max_chars=max_chars,
        )
    )


@mcp.tool(
    name="docs_create",
    structured_output=True,
    annotations=_WRITE_ANNOTATIONS,
)
def docs_create(
    title: str,
    markdown: str = "",
    format_profile: Literal["persian", "plain"] = "persian",
) -> dict[str, object]:
    """Create a private native Google document and verify its initial content."""
    return _call_service(
        lambda: _get_service().create(
            title,
            markdown=markdown,
            format_profile=format_profile,
        )
    )


@mcp.tool(
    name="docs_replace_markdown",
    structured_output=True,
    annotations=_WRITE_ANNOTATIONS,
)
def docs_replace_markdown(
    document: str,
    markdown: str,
    expected_revision_id: str,
    tab_id: str | None = None,
    format_profile: Literal["persian", "plain"] = "persian",
) -> dict[str, object]:
    """Replace one document tab from Markdown under an exact revision guard."""
    return _call_service(
        lambda: _get_service().replace_markdown(
            document,
            markdown,
            expected_revision_id,
            tab_id=tab_id,
            format_profile=format_profile,
        )
    )


@mcp.tool(
    name="docs_edit_text",
    structured_output=True,
    annotations=_WRITE_ANNOTATIONS,
)
def docs_edit_text(
    document: str,
    replacements: list[ReplacementInput],
    expected_revision_id: str,
    tab_id: str | None = None,
    apply: StrictBool = False,
) -> dict[str, object]:
    """Preview or apply exact, revision-guarded replacements in one tab."""

    def operation() -> dict[str, object]:
        typed_replacements = [
            Replacement(
                replacement.old_text,
                replacement.new_text,
                replacement.expected_count,
            )
            for replacement in replacements
        ]
        return _get_service().edit_text(
            document,
            typed_replacements,
            expected_revision_id,
            tab_id=tab_id,
            apply=apply,
        )

    return _call_service(operation)


@mcp.tool(
    name="docs_insert_text",
    structured_output=True,
    annotations=_WRITE_ANNOTATIONS,
)
def docs_insert_text(
    document: str,
    text: str,
    expected_revision_id: str,
    position: Literal["start", "end", "before", "after"] = "end",
    anchor_text: str | None = None,
    tab_id: str | None = None,
    format_profile: Literal["persian", "plain"] = "persian",
    apply: StrictBool = False,
) -> dict[str, object]:
    """Preview/apply plain-text insertion without replacing the tab. Before/after
    require one exact paragraph anchor; start/end forbid it. No separators are
    added. Persian styles touch inserted text/paragraphs; plain inherits styles.
    """
    return _call_service(
        lambda: _get_service().insert_text(
            document, text, expected_revision_id, position=position,
            anchor_text=anchor_text, tab_id=tab_id, format_profile=format_profile, apply=apply,
        )
    )


def main() -> None:
    try:
        _prepare_runtime()
    except Exception:
        raise SystemExit(1) from None
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
