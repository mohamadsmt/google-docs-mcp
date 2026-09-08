"""Bounded exports, source stability and private no-clobber storage."""
from copy import deepcopy
import hashlib
import importlib
import io
import os
from pathlib import Path
import struct
import zipfile

import pytest

from google_docs_mcp.client import DocsMCPError, GoogleDocsClient

DOC = "synthetic_document_123456"
MIME_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
SECRET = "UPSTREAM_SECRET_URI_OR_PATH"


def pdf():
    prefix = (b"%PDF-1.7\n1 0 obj<</Type /Catalog /Pages 2 0 R>>endobj\n"
              b"2 0 obj<</Type /Pages /Kids [3 0 R] /Count 1>>endobj\n"
              b"3 0 obj<</Type /Page /Parent 2 0 R>>endobj\n")
    return prefix + b"xref\n0 1\n0000000000 65535 f\ntrailer<</Root 1 0 R>>\nstartxref\n" + str(len(prefix)).encode() + b"\n%%EOF\n"


def docx(*, xml=None, extra=None):
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr("word/document.xml", xml or '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p/></w:body></w:document>')
        for key, value in (extra or {}).items():
            archive.writestr(key, value)
    return target.getvalue()


class Response:
    def __init__(self, payload, mime):
        self.payload = payload
        self.headers = {"Content-Type": mime, "Content-Length": str(len(payload))}
        self.closed = False
        self.reads = 0

    @property
    def content(self):
        raise AssertionError("Must stream, not buffer an unbounded response")

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.payload), chunk_size):
            self.reads += 1
            yield self.payload[offset:offset + chunk_size]

    def close(self):
        self.closed = True


class Client:
    DRIVE_BASE = GoogleDocsClient.DRIVE_BASE

    def __init__(self, format="pdf"):
        self.metadata = {"id": DOC, "name": "Private synthetic", "mimeType": "application/vnd.google-apps.document",
                         "modifiedTime": "2026-09-08T00:00:00Z", "version": "7",
                         "webViewLink": "https://docs.google.com/document/d/" + DOC + "/edit"}
        self.document = {"documentId": DOC, "revisionId": "revision7", "tabs": [self.tab("t.0")]}
        self.response = Response(pdf() if format == "pdf" else docx(),
                                 "application/pdf" if format == "pdf" else MIME_DOCX)
        self.calls = []
        self.on_export = lambda: None

    @staticmethod
    def tab(tab_id):
        return {"tabProperties": {"tabId": tab_id, "title": "Synthetic"},
                "documentTab": {"body": {"content": [{"endIndex": 2, "paragraph": {"elements": [
                    {"startIndex": 1, "endIndex": 2, "textRun": {"content": "\n"}}]}}]}}}

    def drive_metadata(self, document_id):
        self.calls.append("metadata")
        assert document_id == DOC
        return deepcopy(self.metadata)

    def get_document(self, document_id):
        self.calls.append("document")
        assert document_id == DOC
        return deepcopy(self.document)

    def _request(self, method, url, **kwargs):
        self.calls.append("export")
        assert method == "GET" and url == self.DRIVE_BASE + "/files/" + DOC + "/export"
        assert kwargs["stream"] is True
        assert kwargs["params"] == {"mimeType": self.response.headers["Content-Type"]}
        self.on_export()
        return self.response


def test_export_module_available():
    assert importlib.util.find_spec("google_docs_mcp.exports") is not None, "native export module is missing"


@pytest.fixture
def module():
    return importlib.import_module("google_docs_mcp.exports")


@pytest.fixture
def root(tmp_path):
    return tmp_path.resolve() / "exports"


@pytest.mark.parametrize("format", ["pdf", "docx"])
def test_private_verified_native_export(module, root, format):
    client = Client(format)
    result = module.export_document(client, root, document=DOC, format=format)
    path = Path(result["path"])
    assert path.is_absolute() and path.parent == root
    assert result == {"ok": True, "verified": True, "path": str(path), "format": format,
                      "mime_type": client.response.headers["Content-Type"], "bytes": len(client.response.payload),
                      "sha256": hashlib.sha256(client.response.payload).hexdigest(), "document_id": DOC,
                      "document_url": client.metadata["webViewLink"], "revision_id": "revision7",
                      "scope": "all_tabs", "tab_ids": ["t.0"]}
    assert path.read_bytes() == client.response.payload
    assert root.stat().st_mode & 0o7777 == 0o700
    assert path.stat().st_mode & 0o7777 == 0o600
    assert client.calls == ["metadata", "document", "export", "document", "metadata"]
    assert client.response.closed


@pytest.mark.parametrize("format", ["PDF", "doc", "txt", "../pdf", "pdf ", None, [], True])
def test_exact_format_no_side_effects(module, root, format):
    client = Client()
    with pytest.raises(DocsMCPError) as caught:
        module.export_document(client, root, document=DOC, format=format)
    assert caught.value.code == "invalid_input" and not client.calls and not root.exists()


@pytest.mark.parametrize("scope", ["first_tab", "tab", "", None, [], True])
def test_native_all_tabs_has_no_tab_selector(module, root, scope):
    client = Client()
    with pytest.raises(DocsMCPError) as caught:
        module.export_document(client, root, document=DOC, scope=scope)
    assert caught.value.code == "unsupported_export_scope" and not client.calls


def test_native_export_reports_root_and_child_tabs(module, root):
    client = Client()
    client.document["tabs"][0]["childTabs"] = [client.tab("child")]
    client.document["tabs"].append(client.tab("second"))
    result = module.export_document(client, root, document=DOC)
    assert result["tab_ids"] == ["t.0", "child", "second"]


@pytest.mark.parametrize("explicit_null", [False, True])
def test_reader_without_revision_uses_metadata_guard(module, root, explicit_null):
    client = Client()
    client.document.pop("revisionId")
    if explicit_null:
        client.document["revisionId"] = None
    assert module.export_document(client, root, document=DOC)["revision_id"] is None


@pytest.mark.parametrize("field", ["version", "modifiedTime", "name", "mimeType", "id", "webViewLink", "revisionId", "tabs", "body"])
def test_source_drift_never_publishes(module, root, field):
    client = Client()
    def change():
        if field == "revisionId":
            client.document[field] = "changed"
        elif field == "tabs":
            client.document["tabs"].append(client.tab("new"))
        elif field == "body":
            client.document["tabs"][0]["documentTab"]["body"]["content"][0]["endIndex"] = 99
        else:
            client.metadata[field] = "changed"
    client.on_export = change
    with pytest.raises(DocsMCPError):
        module.export_document(client, root, document=DOC)
    assert not root.exists() and client.response.closed


@pytest.mark.parametrize("revision", [None, ""])
def test_revision_disappearing_fails_closed(module, root, revision):
    client = Client()
    client.on_export = lambda: client.document.update(revisionId=revision)
    with pytest.raises(DocsMCPError):
        module.export_document(client, root, document=DOC)
    assert not root.exists()


@pytest.mark.parametrize("payload", [b"", b"<html>error</html>", b"%PDF-1.7\n%%EOF", pdf()[:-10]])
def test_malformed_pdf(module, root, payload):
    client = Client()
    client.response = Response(payload, "application/pdf")
    with pytest.raises(DocsMCPError) as caught:
        module.export_document(client, root, document=DOC)
    assert caught.value.code == "invalid_export" and not root.exists() and client.response.closed


@pytest.mark.parametrize("payload", [b"PK\x03\x04invalid", docx(xml="<wrong/>"),
    docx(xml='<!DOCTYPE x [<!ENTITY a "SECRET">]><x>&a;</x>'),
    docx(extra={"bomb": b"0" * 32_000_001})])
def test_malformed_or_expanding_docx(module, root, payload):
    client = Client("docx")
    client.response = Response(payload, MIME_DOCX)
    with pytest.raises(DocsMCPError) as caught:
        module.export_document(client, root, document=DOC, format="docx")
    assert caught.value.code == "invalid_export" and not root.exists() and client.response.closed


@pytest.mark.parametrize("advertised", [True, False])
def test_10mb_limit_before_publication(module, root, advertised):
    client = Client()
    client.response = Response(b"a" * 10_000_001, "application/pdf")
    if not advertised:
        client.response.headers.pop("Content-Length")
    with pytest.raises(DocsMCPError) as caught:
        module.export_document(client, root, document=DOC)
    assert caught.value.code == "export_too_large" and not root.exists() and client.response.closed
    if advertised:
        assert client.response.reads == 0


@pytest.mark.parametrize("kind", ["symlink", "ancestor_symlink", "file", "permissive"])
def test_unsafe_root_preserves_existing_objects(module, root, kind):
    target = root.parent / "target"
    target.mkdir(mode=0o700)
    sentinel = target / "sentinel"
    sentinel.write_bytes(b"untouched")
    if kind == "symlink":
        root.symlink_to(target, target_is_directory=True)
    elif kind == "ancestor_symlink":
        root.symlink_to(target, target_is_directory=True)
        root = root / "leaf"
    elif kind == "file":
        root.write_bytes(b"untouched")
    else:
        root.mkdir(mode=0o755)
        root.chmod(0o755)
    with pytest.raises(DocsMCPError) as caught:
        module.export_document(Client(), root, document=DOC)
    assert caught.value.code == "export_storage_unavailable"
    assert sentinel.read_bytes() == b"untouched" and target.stat().st_mode & 0o777 == 0o700
    if kind == "permissive":
        assert root.stat().st_mode & 0o777 == 0o755


def test_nested_creation_restrictive_even_under_umask_zero(module, root):
    old = os.umask(0)
    try:
        module.export_document(Client(), root / "nested", document=DOC)
    finally:
        os.umask(old)
    assert root.stat().st_mode & 0o777 == 0o700
    assert (root / "nested").stat().st_mode & 0o777 == 0o700


def test_collision_never_changes_existing_artifact(module, root, monkeypatch):
    monkeypatch.setattr(module.secrets, "token_hex", lambda n: "a" * (n * 2))
    result = module.export_document(Client(), root, document=DOC)
    path = Path(result["path"])
    metadata, payload = path.stat(), path.read_bytes()
    with pytest.raises(DocsMCPError) as caught:
        module.export_document(Client(), root, document=DOC)
    assert caught.value.code == "export_storage_unavailable"
    assert list(root.iterdir()) == [path]
    assert path.read_bytes() == payload and path.stat().st_ino == metadata.st_ino


@pytest.mark.parametrize("seam", ["write", "fsync"])
def test_failed_owned_write_cleaned_without_touching_existing(module, root, monkeypatch, seam):
    root.mkdir(mode=0o700)
    sentinel = root / "old.pdf"
    sentinel.write_bytes(b"preserve")
    def fail(*a, **kw):
        raise OSError(SECRET)
    monkeypatch.setattr(module.os, seam, fail)
    with pytest.raises(DocsMCPError) as caught:
        module.export_document(Client(), root, document=DOC)
    assert caught.value.code == "export_storage_unavailable" and SECRET not in str(caught.value)
    assert list(root.iterdir()) == [sentinel] and sentinel.read_bytes() == b"preserve"


@pytest.mark.parametrize("seam", ["drive_metadata", "get_document", "_request"])
def test_upstream_errors_constant_no_echo(module, root, seam):
    client = Client()
    def fail(*args, **kwargs):
        raise DocsMCPError(SECRET, SECRET)
    setattr(client, seam, fail)
    with pytest.raises(DocsMCPError) as caught:
        module.export_document(client, root, document=DOC)
    assert caught.value.code == "google_unavailable" and SECRET not in str(caught.value)
    assert not root.exists()


@pytest.mark.parametrize("code", ["google_needs_reauth", "permission_denied", "document_not_found", "rate_limited"])
@pytest.mark.parametrize("seam", ["drive_metadata", "get_document", "_request"])
def test_actionable_google_errors_survive_export_redaction(module, root, seam, code):
    client = Client()
    def fail(*args, **kwargs):
        raise DocsMCPError(code, SECRET, retryable=code == "rate_limited")
    setattr(client, seam, fail)
    with pytest.raises(DocsMCPError) as caught:
        module.export_document(client, root, document=DOC)
    assert caught.value.code == code
    assert SECRET not in str(caught.value) and caught.value.__context__ is None
    assert not root.exists()
