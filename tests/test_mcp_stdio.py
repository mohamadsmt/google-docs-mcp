import asyncio
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


EXPECTED_TOOLS = [
    "docs_read",
    "docs_create",
    "docs_replace_markdown",
    "docs_edit_text",
]


def _server_environment(home: Path) -> dict[str, str]:
    environment = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL", "TERM", "TMPDIR")
        if key in os.environ
    }
    environment["HOME"] = str(home)
    environment["PYTHONPATH"] = str(
        Path(__file__).resolve().parents[1] / "src"
    )
    return environment


def test_stdio_server_initializes_and_lists_exact_tools(tmp_path: Path) -> None:
    recovery_root = tmp_path / ".hermes/google-docs-mcp-recovery"
    recovery_root.mkdir(parents=True, mode=0o700)
    recovery_root.chmod(0o700)
    stale_recovery = (
        recovery_root
        / "recovery-20000101T000000.000000Z-0000000000000000"
    )
    stale_recovery.mkdir(mode=0o700)
    for name in ("document.txt", "document.docx"):
        recovery_file = stale_recovery / name
        recovery_file.write_bytes(b"")
        recovery_file.chmod(0o600)
    os.utime(stale_recovery, (1, 1))

    async def exercise_server() -> tuple[list[str], str]:
        stderr_path = tmp_path / "server-stderr.log"
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "google_docs_mcp.server"],
            env=_server_environment(tmp_path),
            cwd=tmp_path,
        )
        with stderr_path.open("w+", encoding="utf-8") as stderr:
            async with stdio_client(params, errlog=stderr) as (read, write):
                async with ClientSession(
                    read,
                    write,
                    read_timeout_seconds=timedelta(seconds=10),
                ) as session:
                    await session.initialize()
                    tools = await session.list_tools()
            stderr.flush()
            stderr.seek(0)
            stderr_text = stderr.read()
        return [tool.name for tool in tools.tools], stderr_text

    tool_names, stderr = asyncio.run(exercise_server())

    assert tool_names == EXPECTED_TOOLS
    assert not stale_recovery.exists()
    assert "Traceback" not in stderr
    assert "google_needs_reauth" not in stderr


def test_stdio_startup_purge_failure_exits_without_raw_traceback(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    hermes_root = home / ".hermes"
    hermes_root.mkdir(parents=True)
    blocked_recovery_root = hermes_root / "google-docs-mcp-recovery"
    blocked_recovery_root.write_bytes(b"CANARY_STARTUP_PURGE_SECRET")
    stdout_path = tmp_path / "server-stdout.log"
    stderr_path = tmp_path / "server-stderr.log"

    with (
        stdout_path.open("wb") as stdout,
        stderr_path.open("wb") as stderr,
    ):
        completed = subprocess.run(
            [sys.executable, "-m", "google_docs_mcp.server"],
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            cwd=tmp_path,
            env=_server_environment(home),
            check=False,
            timeout=10,
        )

    assert completed.returncode != 0
    assert stdout_path.read_bytes() == b""
    assert stderr_path.read_bytes() == b""
