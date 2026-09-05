from importlib.metadata import version


def test_package_version_is_installed() -> None:
    import google_docs_mcp

    assert google_docs_mcp.__version__ == "0.1.0"
    assert version("google-docs-mcp") == "0.1.0"
