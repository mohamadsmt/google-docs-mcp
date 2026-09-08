"""Native PDF/DOCX exports with bounded validation and private local delivery.

Live characterization attests all root and child tabs in both native formats.
Drive files.export has no selector: only all_tabs is supported. This is a
stable-source observation, not a Google revision-conditional export transaction.
Static symlinks are rejected; hostile unrelated same-UID writers are out of scope.
"""
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import stat
import struct
import zipfile

from defusedxml import ElementTree

from .client import (DocsMCPError, _require_native_document, _service_metadata,
                     _service_tabs, parse_document_id, _google_needs_reauth,
                     _permission_denied, _document_not_found, _rate_limited)

MIME_TYPES = {"pdf": "application/pdf", "docx":
              "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
MAX_EXPORT_BYTES = 10_000_000
_MAX_MEMBERS = 4096
_MAX_EXPANDED = 32_000_000
_MAX_XML = 8_000_000
_WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MESSAGES = {
    "invalid_input": "format must be exactly pdf or docx.",
    "unsupported_export_scope": "Native export supports only all_tabs; individual tabs cannot be selected.",
    "google_unavailable": "The Google document could not be exported.",
    "invalid_export": "Google returned an invalid or unsupported export file.",
    "export_too_large": "The export exceeds the 10000000-byte limit.",
    "source_changed": "The source changed during export. Read it again before retrying.",
    "export_storage_unavailable": "A private export file could not be safely created.",
    "unsupported_office_file": "Only native Google Docs documents are supported.",
}


_GOOGLE_ERRORS = {"google_needs_reauth": _google_needs_reauth,
                  "permission_denied": _permission_denied,
                  "document_not_found": _document_not_found,
                  "rate_limited": _rate_limited}


def _error(code):
    if code in _GOOGLE_ERRORS:
        return _GOOGLE_ERRORS[code]()
    return DocsMCPError(code, _MESSAGES[code])


def _document_snapshot(client, document_id):
    value = client.get_document(document_id)
    if not isinstance(value, dict) or value.get("documentId") != document_id:
        raise _error("google_unavailable")
    revision = value.get("revisionId")
    if revision is not None and (not isinstance(revision, str) or not revision
                                 or len(revision) > 1000 or "\x00" in revision):
        raise _error("google_unavailable")
    # Bound traversal before copying/flattening. Do not use expiring contentUri
    # bearer links as source identity; all persistent content/style is compared.
    pending = [value]
    seen = set()
    nodes = size = 0
    while pending:
        node = pending.pop()
        nodes += 1
        if nodes > 100_000:
            raise _error("google_unavailable")
        if isinstance(node, (dict, list)):
            if id(node) in seen:
                raise _error("google_unavailable")
            seen.add(id(node))
            pending.extend(node.values() if isinstance(node, dict) else node)
        elif isinstance(node, str):
            size += len(node)
            if size > 32_000_000:
                raise _error("google_unavailable")
    stable = deepcopy(value)
    pending = [stable]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            node.pop("contentUri", None)
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    stable["revisionId"] = revision
    tabs = _service_tabs(stable)
    tab_ids = [tab.tab_id for tab in tabs]
    if not tabs or len(tabs) > 1000 or len(set(tab_ids)) != len(tab_ids):
        raise _error("google_unavailable")
    return revision, tab_ids, json.dumps(stable, sort_keys=True, ensure_ascii=True)


def _download(client, document_id, mime):
    # export_file() eagerly reads .content. Reuse the canonical authenticated
    # request/endpoint, but stream here so the local bound precedes allocation.
    response = client._request("GET", f"{client.DRIVE_BASE}/files/{document_id}/export",
                               params={"mimeType": mime}, stream=True, retry_safe=False)
    try:
        if response.headers.get("Content-Type", "").split(";", 1)[0].strip() != mime:
            raise _error("invalid_export")
        length = response.headers.get("Content-Length")
        if length is not None:
            if not isinstance(length, str) or not re.fullmatch(r"[0-9]{1,12}", length):
                raise _error("invalid_export")
            if int(length) > MAX_EXPORT_BYTES:
                raise _error("export_too_large")
        output = bytearray()
        for count, chunk in enumerate(response.iter_content(chunk_size=65536)):
            if count >= 2048 or not isinstance(chunk, bytes):
                raise _error("invalid_export")
            if len(output) + len(chunk) > MAX_EXPORT_BYTES:
                raise _error("export_too_large")
            output.extend(chunk)
        if not output or (length is not None and int(length) != len(output)):
            raise _error("invalid_export")
        return bytes(output)
    finally:
        response.close()


def _check_pdf(payload):
    # Bounded structural envelope, not a PDF renderer or malware scanner.
    if not re.match(rb"%PDF-[12]\.[0-9][\r\n]", payload):
        raise _error("invalid_export")
    match = re.search(rb"startxref\s+([0-9]{1,10})\s+%%EOF\s*\Z", payload[-1024:])
    if match is None:
        raise _error("invalid_export")
    offset = int(match[1])
    if offset >= len(payload) or not (payload[offset:offset + 4] == b"xref"
            or re.match(rb"[0-9]+\s+[0-9]+\s+obj", payload[offset:offset + 64])):
        raise _error("invalid_export")
    if any(not re.search(rb"/Type\s*/" + kind + rb"\b", payload)
           for kind in (b"Catalog", b"Pages", b"Page")):
        raise _error("invalid_export")


def _check_docx(payload):
    # Preflight the central directory before ZipFile allocates its member list.
    end = payload.rfind(b"PK\x05\x06", max(0, len(payload) - 65557))
    if end < 0 or len(payload) < end + 22 or not payload.startswith(b"PK\x03\x04"):
        raise _error("invalid_export")
    _, disk, cd_disk, disk_count, count, cd_size, cd_offset, comment = struct.unpack_from("<4s4H2LH", payload, end)
    if (disk or cd_disk or disk_count != count or not 2 <= count <= _MAX_MEMBERS
            or cd_size > 1_000_000 or cd_offset + cd_size != end
            or end + 22 + comment != len(payload)):
        raise _error("invalid_export")
    # Do not trust the advertised member count: walk the bounded raw directory.
    cursor = cd_offset
    actual = 0
    while cursor < end:
        if payload[cursor:cursor + 4] != b"PK\x01\x02" or cursor + 46 > end:
            raise _error("invalid_export")
        name_len, extra_len, comment_len = struct.unpack_from("<3H", payload, cursor + 28)
        cursor += 46 + name_len + extra_len + comment_len
        actual += 1
        if actual > _MAX_MEMBERS:
            raise _error("invalid_export")
    if cursor != end or actual != count:
        raise _error("invalid_export")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = archive.infolist()
        names = [item.filename for item in members]
        if (len(set(names)) != len(names) or sum(item.file_size for item in members) > _MAX_EXPANDED
                or "word/document.xml" not in names or "[Content_Types].xml" not in names):
            raise _error("invalid_export")
        xml = {}
        for item in members:
            if (item.flag_bits & 1 or item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                    or item.file_size < 0):
                raise _error("invalid_export")
            required = item.filename in {"word/document.xml", "[Content_Types].xml"}
            if required and item.file_size > _MAX_XML:
                raise _error("invalid_export")
            parts = bytearray()
            consumed = 0
            with archive.open(item) as stream:
                while chunk := stream.read(65536):
                    consumed += len(chunk)
                    if consumed > item.file_size or consumed > _MAX_EXPANDED:
                        raise _error("invalid_export")
                    if required:
                        parts.extend(chunk)
            if required:
                # defusedxml rejects entities, external references and DTDs.
                root = ElementTree.fromstring(bytes(parts), forbid_dtd=True)
                if sum(1 for _ in root.iter()) > 100_000:
                    raise _error("invalid_export")
                xml[item.filename] = root
        document = xml["word/document.xml"]
        if document.tag != _WORD + "document" or document.find(_WORD + "body") is None:
            raise _error("invalid_export")
        types = xml["[Content_Types].xml"]
        if not any(item.get("PartName") == "/word/document.xml" and item.get("ContentType") ==
                   "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
                   for item in types):
            raise _error("invalid_export")


def _root_fd(root):
    flags = os.O_RDONLY
    for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC"):
        flag = getattr(os, name, 0)
        if type(flag) is not int or not flag:
            raise OSError
        flags |= flag
    if not root.is_absolute() or ".." in root.parts or root == Path("/"):
        raise OSError
    fd = os.open("/", flags)
    try:
        for part in root.parts[1:]:
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                os.mkdir(part, 0o700, dir_fd=fd)
                child = os.open(part, flags, dir_fd=fd)
            old, fd = fd, child
            os.close(old)
        metadata = os.fstat(fd)
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise OSError
        return fd
    except BaseException:
        os.close(fd)
        raise


def _publish(root, payload, format):
    root = Path(root)
    token = secrets.token_hex(16)
    if not isinstance(token, str) or re.fullmatch(r"[a-f0-9]{32}", token) is None:
        raise OSError
    name = "export-" + token + "." + format
    path = str(root / name)
    fd = root_fd = None
    owned = None
    completed = False
    try:
        root_fd = _root_fd(root)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(name, flags, 0o600, dir_fd=root_fd)
        metadata = os.fstat(fd)
        owned = (metadata.st_dev, metadata.st_ino)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError
        os.fchmod(fd, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError
            view = view[written:]
        os.fsync(fd)
        closing, fd = fd, None
        os.close(closing)
        os.fsync(root_fd)
        current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if ((current.st_dev, current.st_ino) != owned or current.st_size != len(payload)
                or stat.S_IMODE(current.st_mode) != 0o600 or current.st_nlink != 1):
            raise OSError
        completed = True
        return path
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if owned is not None and not completed:
            try:
                current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == owned:
                    os.unlink(name, dir_fd=root_fd)
            except OSError:
                pass
        if root_fd is not None:
            os.close(root_fd)


def export_document(client, export_root, *, document, format="pdf", scope="all_tabs"):
    """Export every native tab; create a retained 0600 file in the configured root.

    Raises constant DocsMCPError values; no caller-controlled destination/name,
    local conversion, upstream reconstruction, overwrite or export auto-cleanup.
    A null viewer revision is reported honestly and guarded by Drive metadata
    and persistent document readback instead of a fabricated revision.
    """
    if not isinstance(format, str) or format not in MIME_TYPES:
        raise _error("invalid_input")
    if not isinstance(scope, str) or scope != "all_tabs":
        raise _error("unsupported_export_scope")
    document_id = parse_document_id(document)
    phase = "google_unavailable"
    try:
        metadata = deepcopy(_service_metadata(client, document_id))
        _require_native_document(metadata)
        before = _document_snapshot(client, document_id)
        payload = _download(client, document_id, MIME_TYPES[format])
        phase = "invalid_export"
        (_check_pdf if format == "pdf" else _check_docx)(payload)
        phase = "source_changed"
        after = _document_snapshot(client, document_id)
        final_metadata = _service_metadata(client, document_id)
        if before != after or metadata != final_metadata:
            raise _error("source_changed")
        result = {"ok": True, "verified": True, "format": format,
                  "mime_type": MIME_TYPES[format], "bytes": len(payload),
                  "sha256": hashlib.sha256(payload).hexdigest(), "document_id": document_id,
                  "document_url": metadata["webViewLink"], "revision_id": before[0],
                  "scope": "all_tabs", "tab_ids": before[1]}
        phase = "export_storage_unavailable"
        result["path"] = _publish(export_root, payload, format)
        return result
    except DocsMCPError as error:
        # Rebuild even known errors; an upstream exception may contain private
        # response text in its message/args. Never reflect unknown codes.
        code = error.code if error.code in _MESSAGES or error.code in _GOOGLE_ERRORS else phase
    except Exception:
        code = phase
    raise _error(code) from None
