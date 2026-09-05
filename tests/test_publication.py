import os
import subprocess
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_gitignore_excludes_private_and_generated_files(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_bytes((ROOT / ".gitignore").read_bytes())
    environment = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL", "TMPDIR")
        if key in os.environ
    }
    environment.update(HOME=str(tmp_path), GIT_CONFIG_NOSYSTEM="1")
    subprocess.run(
        ["git", "init", "--quiet", str(tmp_path)],
        env=environment,
        check=True,
        timeout=10,
    )
    private_paths = {
        ".env", ".env.local", "nested/.env.production",
        "google_token.json", "google_token.json.bak", "nested/token.json",
        "credentials.json", "google_client_secret.json", "client_secret_demo.json",
        "service-account.json", "secrets/local.json", "private.pem", "private.key",
        "auth.json", ".hermes/state.db", ".artifacts/report.json",
        "google-docs-mcp-recovery/recovery-example/document.docx",
        "recovery-example/document.txt", "exports/document.txt", "test.log",
        "local.sqlite", ".DS_Store", ".venv/bin/python", "dist/package.whl",
        "docs/plans/local.md", "docs/superpowers/specs/local.md",
    }
    public_paths = {
        ".env.example", "README.md", "LICENSE", "SECURITY.md", "pyproject.toml",
        "uv.lock", "src/google_docs_mcp/client.py", "tests/test_client.py",
        "scripts/run-mcp", "docs/authentication.md",
    }
    paths = sorted(private_paths | public_paths)
    result = subprocess.run(
        ["git", "-c", "core.excludesFile=/dev/null", "check-ignore", "--no-index", "--stdin"],
        input="\n".join(paths) + "\n",
        text=True,
        capture_output=True,
        cwd=tmp_path,
        env=environment,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0
    assert set(result.stdout.splitlines()) == private_paths


def test_distribution_metadata_limits_publication_scope() -> None:
    configuration = tomllib.loads((ROOT / "pyproject.toml").read_text())
    project = configuration["project"]
    assert project.get("license") == "MIT"
    assert project.get("license-files") == ["LICENSE"]
    assert project.get("readme") == "README.md"
    included = set(configuration["tool"]["hatch"]["build"]["targets"]["sdist"]["only-include"])
    assert {"LICENSE", "SECURITY.md", "README.md"} <= included
    assert not {"docs", "tests", "src/google_docs_mcp", ".hermes", ".env", ".artifacts"} & included
