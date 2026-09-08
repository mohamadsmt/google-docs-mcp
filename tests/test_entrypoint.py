import asyncio
import hashlib
import os
import shutil
import stat
import subprocess
import tarfile
import zipfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WRAPPER = PROJECT_ROOT / "scripts/run-mcp"
EXPECTED_TOOLS = [
    "docs_read",
    "docs_create",
    "docs_replace_markdown",
    "docs_edit_text",
    "docs_insert_text",
    "docs_edit_section",
    "docs_format",
    "docs_manage_tab",
    "docs_edit_table",
    "docs_export",
    "docs_insert_image",
]
EXPECTED_WRAPPER = """#!/bin/bash
set -euo pipefail
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
unset PYTHONPATH PYTHONHOME
exec "$ROOT/.venv/bin/google-docs-mcp" "$@"
"""
EXPECTED_PACKAGE_MODULES = {
    "google_docs_mcp/__init__.py",
    "google_docs_mcp/client.py",
    "google_docs_mcp/markdown.py",
    "google_docs_mcp/server.py",
    "google_docs_mcp/editing_common.py",
    "google_docs_mcp/sections.py",
    "google_docs_mcp/formatting.py",
    "google_docs_mcp/tabs.py",
    "google_docs_mcp/tables.py",
    "google_docs_mcp/read_metadata.py",
    "google_docs_mcp/exports.py",
    "google_docs_mcp/images.py",
}


@dataclass(frozen=True)
class InstalledArtifact:
    entrypoint: Path
    installed_package: Path
    project_root: Path
    python: Path
    wheel: Path
    wheel_manifest: dict[str, str]


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_environment(home: Path) -> dict[str, str]:
    environment = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL", "TERM", "TMPDIR")
        if key in os.environ
    }
    environment["HOME"] = str(home)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _run_checked(
    argv: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout: float,
) -> None:
    try:
        completed = subprocess.run(
            tuple(argv),
            cwd=cwd,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("bounded packaging command timed out")
    assert completed.returncode == 0


def _installed_manifest(package_root: Path) -> dict[str, str]:
    return {
        f"google_docs_mcp/{path.relative_to(package_root).as_posix()}": (
            _sha256_bytes(path.read_bytes())
        )
        for path in sorted(package_root.rglob("*.py"))
        if "__pycache__" not in path.parts
    }


def _assert_installed_manifest(artifact: InstalledArtifact) -> None:
    assert _installed_manifest(artifact.installed_package) == artifact.wheel_manifest


def _assert_installed_import_origins(
    artifact: InstalledArtifact,
    scratch_root: Path,
) -> None:
    scratch_root.mkdir(mode=0o700)
    home = scratch_root / "home"
    home.mkdir(mode=0o700)
    result_path = scratch_root / "origins.txt"
    probe_path = scratch_root / "origin_probe.py"
    probe_path.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "import google_docs_mcp\n"
        "import google_docs_mcp.server\n"
        "origins = (\n"
        "    Path(google_docs_mcp.__file__).resolve(),\n"
        "    Path(google_docs_mcp.server.__file__).resolve(),\n"
        ")\n"
        "Path(os.environ['ORIGIN_RESULT']).write_text(\n"
        "    '\\n'.join(map(str, origins)) + '\\n', encoding='utf-8'\n"
        ")\n",
        encoding="utf-8",
    )
    environment = _safe_environment(home)
    environment["ORIGIN_RESULT"] = str(result_path)
    _run_checked(
        [str(artifact.python), str(probe_path)],
        cwd=scratch_root,
        environment=environment,
        timeout=30,
    )
    encoded = result_path.read_bytes()
    assert len(encoded) <= 4096
    origins = encoded.decode("utf-8", errors="strict").splitlines()
    assert origins == [
        str((artifact.installed_package / "__init__.py").resolve()),
        str((artifact.installed_package / "server.py").resolve()),
    ]


async def _list_tools(
    *,
    command: Path,
    cwd: Path,
    environment: dict[str, str],
    stderr_path: Path,
) -> tuple[list[str], str]:
    with stderr_path.open("w+", encoding="utf-8") as stderr:
        parameters = StdioServerParameters(
            command=str(command),
            args=[],
            env=dict(environment),
            cwd=cwd,
        )
        async with stdio_client(parameters, errlog=stderr) as (reader, writer):
            async with ClientSession(
                reader,
                writer,
                read_timeout_seconds=timedelta(seconds=10),
            ) as session:
                await session.initialize()
                names = [tool.name for tool in (await session.list_tools()).tools]
        stderr.flush()
        stderr.seek(0)
        stderr_text = stderr.read()
    return names, stderr_text


@pytest.fixture(scope="module")
def installed_artifact(tmp_path_factory: pytest.TempPathFactory) -> InstalledArtifact:
    root = tmp_path_factory.mktemp("installed-entrypoint")
    build_root = root / "build"
    wheel_root = root / "wheel"
    staged_project = root / "staged-project"
    venv = staged_project / ".venv"
    home = root / "home"
    cache = root / "uv-cache"
    for path in (build_root, wheel_root, staged_project, home, cache):
        path.mkdir(mode=0o700)

    uv = shutil.which("uv")
    assert uv is not None
    environment = _safe_environment(home)
    environment["UV_CACHE_DIR"] = str(cache)
    environment["UV_LINK_MODE"] = "copy"
    environment["UV_NO_PROGRESS"] = "1"

    _run_checked(
        [uv, "build", "--wheel", "--out-dir", str(wheel_root)],
        cwd=PROJECT_ROOT,
        environment=environment,
        timeout=120,
    )
    wheels = sorted(wheel_root.glob("*.whl"))
    assert len(wheels) == 1
    wheel = wheels[0]
    with zipfile.ZipFile(wheel) as archive:
        members = archive.namelist()
        assert len(members) == len(set(members))
        payload_members = {name for name in members if ".dist-info/" not in name}
        assert payload_members == EXPECTED_PACKAGE_MODULES
        wheel_manifest = {
            name: _sha256_bytes(archive.read(name))
            for name in EXPECTED_PACKAGE_MODULES
        }

    _run_checked(
        [uv, "venv", "--python", "3.12", str(venv)],
        cwd=build_root,
        environment=environment,
        timeout=120,
    )
    _run_checked(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(venv / "bin/python"),
            str(wheel),
        ],
        cwd=build_root,
        environment=environment,
        timeout=300,
    )

    site_packages = sorted((venv / "lib").glob("python*/site-packages"))
    assert len(site_packages) == 1
    installed_package = site_packages[0] / "google_docs_mcp"
    assert installed_package.is_dir()
    entrypoint = venv / "bin/google-docs-mcp"
    assert entrypoint.is_file()
    assert os.access(entrypoint, os.X_OK)

    artifact = InstalledArtifact(
        entrypoint=entrypoint,
        installed_package=installed_package,
        project_root=staged_project,
        python=venv / "bin/python",
        wheel=wheel,
        wheel_manifest=wheel_manifest,
    )
    _assert_installed_manifest(artifact)
    return artifact


def test_sdist_contains_only_distributable_files(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    output = tmp_path / "sdist"
    home = tmp_path / "home"
    cache = tmp_path / "uv-cache"
    for path in (checkout, output, home, cache):
        path.mkdir(mode=0o700)

    # Copy deliberate public inputs, never the working tree or local handoffs.
    public_files = {f"src/{name}" for name in EXPECTED_PACKAGE_MODULES} | {
        "tests/test_entrypoint.py",
        "scripts/run-mcp",
        "README.md",
        "LICENSE",
        "SECURITY.md",
        "pyproject.toml",
        "uv.lock",
        ".gitignore",
        ".python-version",
    }
    for name in sorted(public_files):
        source = PROJECT_ROOT / name
        assert source.is_file() and not source.is_symlink()
        destination = checkout / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    for name in (
        ".hermes/handoffs/synthetic-private-handoff.md",
        "synthetic-local-note.txt",
        "docs/plans/synthetic-private-plan.md",
        "docs/superpowers/specs/synthetic-private-design.md",
        "tests/test_private_local_copy.py",
        "src/google_docs_mcp/private_local_copy.py",
        ".env",
        "google_token.json",
        "google_client_secret.json",
        ".artifacts/synthetic-report.json",
        "exports/document.txt",
    ):
        private_file = checkout / name
        private_file.parent.mkdir(parents=True, exist_ok=True)
        private_file.write_text("Synthetic private canary only.\n", encoding="utf-8")

    uv = shutil.which("uv")
    assert uv is not None
    environment = _safe_environment(home)
    environment["UV_CACHE_DIR"] = str(cache)
    environment["UV_LINK_MODE"] = "copy"
    environment["UV_NO_PROGRESS"] = "1"
    _run_checked(
        [uv, "build", "--sdist", "--wheel", "--out-dir", str(output)],
        cwd=checkout,
        environment=environment,
        timeout=120,
    )
    sdists = sorted(output.glob("*.tar.gz"))
    assert len(sdists) == 1
    with tarfile.open(sdists[0], "r:gz") as archive:
        members = archive.getnames()
    assert len(members) == len(set(members))
    assert len({name.partition("/")[0] for name in members}) == 1
    payload_members = {name.partition("/")[2] for name in members}
    assert payload_members == public_files | {"PKG-INFO"}

    wheels = sorted(output.glob("*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as archive:
        wheel_members = archive.namelist()
    assert len(wheel_members) == len(set(wheel_members))
    assert {
        name for name in wheel_members if ".dist-info/" not in name
    } == EXPECTED_PACKAGE_MODULES


def test_launcher_matches_exact_isolation_contract() -> None:
    assert WRAPPER.is_file(), "scripts/run-mcp is missing"
    assert WRAPPER.read_text(encoding="utf-8") == EXPECTED_WRAPPER
    assert stat.S_IMODE(WRAPPER.stat().st_mode) == 0o755


def test_wheel_contains_each_package_module_once(
    installed_artifact: InstalledArtifact,
) -> None:
    assert installed_artifact.wheel.is_file()
    assert set(installed_artifact.wheel_manifest) == EXPECTED_PACKAGE_MODULES
    _assert_installed_manifest(installed_artifact)


def test_installed_imports_resolve_to_physical_wheel_package(
    installed_artifact: InstalledArtifact,
    tmp_path: Path,
) -> None:
    _assert_installed_import_origins(installed_artifact, tmp_path / "origin-probe")


def test_installed_console_initializes_twice_outside_checkout(
    installed_artifact: InstalledArtifact,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside-checkout"
    home = tmp_path / "home"
    outside.mkdir(mode=0o700)
    home.mkdir(mode=0o700)
    assert PROJECT_ROOT not in outside.parents
    environment = _safe_environment(home)

    for attempt in range(2):
        names, stderr_text = asyncio.run(
            _list_tools(
                command=installed_artifact.entrypoint,
                cwd=outside,
                environment=environment,
                stderr_path=tmp_path / f"direct-stderr-{attempt}.txt",
            )
        )
        assert names == EXPECTED_TOOLS
        assert stderr_text == ""
        _assert_installed_manifest(installed_artifact)
        _assert_installed_import_origins(
            installed_artifact,
            tmp_path / f"direct-origin-{attempt}",
        )


def test_relocated_wrapper_ignores_hostile_python_environment(
    installed_artifact: InstalledArtifact,
    tmp_path: Path,
) -> None:
    scripts = installed_artifact.project_root / "scripts"
    scripts.mkdir(mode=0o700)
    relocated_wrapper = scripts / "run-mcp"
    shutil.copy2(WRAPPER, relocated_wrapper)
    assert stat.S_IMODE(relocated_wrapper.stat().st_mode) == 0o755

    hostile_pythonpath = tmp_path / "hostile-pythonpath"
    hostile_package = hostile_pythonpath / "mcp"
    hostile_package.mkdir(parents=True, mode=0o700)
    hostile_package.joinpath("__init__.py").write_text(
        'raise RuntimeError("HOSTILE_PYTHONPATH_CANARY")\n',
        encoding="utf-8",
    )
    hostile_pythonhome = tmp_path / "hostile-pythonhome"
    hostile_pythonhome.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    environment = _safe_environment(home)
    environment["PYTHONPATH"] = str(hostile_pythonpath)
    environment["PYTHONHOME"] = str(hostile_pythonhome)

    names, stderr_text = asyncio.run(
        _list_tools(
            command=relocated_wrapper,
            cwd=outside,
            environment=environment,
            stderr_path=tmp_path / "wrapper-stderr.txt",
        )
    )
    assert names == EXPECTED_TOOLS
    assert stderr_text == ""
    assert "HOSTILE_PYTHONPATH_CANARY" not in stderr_text
    _assert_installed_manifest(installed_artifact)
    _assert_installed_import_origins(
        installed_artifact,
        tmp_path / "wrapper-origin",
    )
