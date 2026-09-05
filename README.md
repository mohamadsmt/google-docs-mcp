# Google Docs MCP for Hermes

A small local **stdio MCP server** for native Google Docs: bounded reading, private document creation, Markdown publication, and exact text edits. Existing-document writes require the Docs revision you reviewed and perform readback checks. The server is launched by Hermes; it does not expose an HTTP listener or a general Google API proxy.

**MIT licensed · Python 3.12 · macOS/POSIX-oriented · Google OAuth required**

[Installation](#install-python-312-locked-physically-installed-non-editable) · [Authentication](#authentication-and-privacy-boundary) · [Hermes setup](#register-in-hermes) · [Security](SECURITY.md)

This is a community project, not an official Google or Nous Research product. The documented launcher requires Bash and a POSIX-style virtual environment; native Windows is not supported by these instructions. Persian formatting is the default; choose `format_profile="plain"` for documents that should not receive Persian formatting. There is no bundled OAuth client, login command, or hosted service.

## Exactly five tools

| Tool | Public arguments | Behavior |
| --- | --- | --- |
| `docs_read` | `document`, `tab_id=null`, `start=0`, `max_chars=30000` | Return metadata, tabs, the current Docs revision, and bounded readable content when a tab is selected. |
| `docs_create` | `title`, `markdown=""`, `format_profile="persian"` | Create a private native Google document; render and verify nonempty Markdown. |
| `docs_replace_markdown` | `document`, `markdown`, `expected_revision_id`, `tab_id=null`, `format_profile="persian"` | Replace the selected tab's body while retaining the document ID. This is destructive, not an append or merge. |
| `docs_edit_text` | `document`, `replacements`, `expected_revision_id`, `tab_id=null`, `apply=false` | Preview exact replacements; write only with `apply=true`. |
| `docs_insert_text` | `document`, `text`, `expected_revision_id`, `position="end"`, `anchor_text=null`, `tab_id=null`, `format_profile="persian"`, `apply=false` | Preview or insert literal text at the start/end or before/after one exact anchor, without replacing the tab. |

A replacement is `{"old_text":"…","new_text":"…","expected_count":1}`. Only `persian` and `plain` are valid profile names. Booleans and integer controls use strict MCP input types: pass JSON `false`/`true` and integers, not strings.

### Input limits and document references

- `document` is an ID matching `[A-Za-z0-9_-]{10,256}`, or an HTTPS URL on exactly `docs.google.com` with a `/document/d/<id>/…` path. Whitespace, nonprinting characters, URL credentials/ports, encoded path components, and traversal components are not accepted. The server does not fetch arbitrary user-supplied URLs.
- `title`: nonblank, at most 200 characters, no NUL.
- `markdown`: at most 500000 characters, no NUL. Rendering also has request-count, payload-byte, and semantic-processing limits; a short but excessively fragmented input can still fail.
- `max_chars`: 1–100000, default 30000. `start` must fall within the rendered content. Pagination uses character offsets, **not Google Docs UTF-16 indices**.
- `replacements`: 1–100 entries; each `expected_count` is 1–100. `old_text` must be nonempty; `new_text` may be empty to delete text. Neither may contain NUL.
- Insertion `text`: 1–500000 characters; literal text, not Markdown. Tabs/newlines and whitespace are preserved; no separator is added automatically. C0 controls except tab/newline, surrogate code points, and BMP private-use characters are rejected rather than allowing Google to strip them. Anchors are 1–500000 characters in a single contiguous paragraph text span; control characters and surrogate code points are rejected.
- `expected_revision_id`: a nonempty Docs revision identifier, at most 1000 characters, no NUL. Copy it from a fresh read; it is not a Drive `version`.

## Markdown subset and non-goals

This is a deliberately small renderer, not a CommonMark/GFM implementation or a lossless document conversion system.

| Input | Rendered behavior |
| --- | --- |
| `# Heading` through `###### Heading` | Native heading levels 1–6; the heading marker must start the line and be followed by whitespace. |
| `**bold**` | Explicit bold emphasis, including in supported link labels and table cells. |
| `[label](https://example.com)` or `[label](http://example.com)` | HTTP(S)-only link formatting; recognized destinations start with lowercase `http://` or `https://` and contain no whitespace. Links are not fetched or reputation-checked. |
| `- item` | A text bullet `• item`, not a native Google list object. |
| `- [ ] item`, `- [x] item`, `- [X] item` | Text checkbox glyphs `☐` / `☑`, not interactive Google checkboxes. |
| Pipe tables with a header and separator row | Native tables with text, bold, and supported links in cells. Header and separator widths must match; shorter data rows are padded to the widest row. Rows must begin with `|`; `\|` represents a literal pipe in a cell. Separator alignment colons do not set cell alignment. |
| Persian, mixed Persian/English, emoji and other astral characters | Unicode text is preserved; Google Docs edit/style ranges are calculated in **UTF-16 code units**, including offsets after emoji. |

Example supported Markdown:

```markdown
# یادداشت 🧪

متن **پررنگ** و [پیوند](https://example.com).

- گزینه
- [ ] کار باز
- [x] کار انجام‌شده

| موضوع | وضعیت |
| --- | --- |
| **آزمون** | آماده |
```

Additional normalization matters when preparing a write:

- A leading block delimited by `---` is removed as frontmatter; its metadata is not applied to Google Docs.
- Single-backtick inline code becomes literal text without backticks; no monospace/code style is applied.
- Obsidian `[[target|alias]]` becomes alias text; without an alias, the section after `#`, or otherwise the target, is displayed. It is not a linked-note integration.
- Backslash escapes are consumed; line endings are normalized. Unmatched or unsupported constructs are not universally rejected or preserved verbatim. They pass through the line/inline parser, so supported syntax inside them can still be interpreted.
- Italics, underscore-based bold, strikethrough, ordered/nested/native lists, blockquotes, fenced-code semantics, HTML rendering, images, and rich embeds are not supported features. Image-like syntax does not upload an image; its link portion may still be parsed. A fenced block does not protect internal headings or emphasis from parsing.
- Non-HTTP(S) link destinations are not turned into links. Reserved internal table-marker syntax is rejected with `invalid_markdown`.
- Emitted body and table-cell text containing characters that Google `insertText` strips (U+0000–U+0008, U+000C–U+001F, U+E000–U+F8FF), or surrogate code points, is rejected before creation/replacement. This does not change CRLF normalization, ignored frontmatter, or valid Persian half-spaces/emoji.
- Nonempty Markdown replacement removes inherited native bullets/numbering before applying the selected profile. Markdown bullets/checklists remain literal glyphs. Semantic verification rejects unexpected native lists, including empty paragraphs and table cells. Empty-model replacement retains its no-style-write behavior; this is not a general native-list editing tool.

There are no public tools for arbitrary `batchUpdate`, arbitrary HTTP, sharing/permission changes, deletion, comments, suggestions, named-range workflows, Office conversion, image upload, or free-form table/section/header/footer editing. The internal cleanup/export helpers are not MCP tools. `docs_edit_text` can count and replace matching existing text within the selected tab's body/table cells and auxiliary header/footer/footnote segments; that is not a layout or segment-authoring API.

### Why not Drive's native Markdown conversion?

Google Drive officially supports importing `text/markdown` to `application/vnd.google-apps.document`. This server still uses its explicit Docs request renderer: Drive import-update replaces the entire document, not just the selected tab, and the importer is not output-equivalent to our documented subset or Persian profile. A synthetic live characterization produced native headings, bold text, links, lists and a table, but no explicit RTL paragraphs or Vazirmatn runs. This is not a claim that native import can never be useful; it needs a separately defined import contract rather than a silent backend swap.

See [the API audit](docs/native-api-audit.md) for all Request categories, adoption decisions and evidence. The opt-in characterization and inherited-list regressions reuse the existing live-test cleanup helpers:

```bash
env -u PYTHONPATH -u PYTHONHOME RUN_GOOGLE_DOCS_MCP_LIVE=1 \
  .venv/bin/python -m pytest tests/test_native_api.py -v -s
```

Run only after explicitly authorizing private synthetic temporary Google documents and their deletion. A passing characterization proves conversion and cleanup, not equality with the current renderer. No import method or general-purpose API proxy is exposed through MCP.

## Read first, then write with the revision you reviewed

All IDs, tabs, revisions, and result excerpts below are **synthetic examples**, not live output. `abcDEF_123-xyz` is a syntactically valid example ID; replace it with your own authorized document reference. Each JSON object is a tool's arguments unless explicitly labeled as a response excerpt.

### Create privately

Call `docs_create`:

```json
{
  "title": "نمونهٔ خصوصی",
  "markdown": "وضعیت: پیش‌نویس 🧪",
  "format_profile": "persian"
}
```

The creation request does not grant sharing permissions or create an `anyone` permission. **Private-by-default creation is not a general permission-management API or a continuous audit of account/organization sharing policy.** Changing access remains a separate Google Drive/Docs action.

Creation returns `document_id`, `document_url`, `revision_id`, `tab_id`, `semantic`, `format_profile`, `formatting`, and `verified`, with `ok=true` on success. Empty Markdown creates a blank document and returns `formatting=null`; it does not demonstrate that later manually added text has been formatted.

If initial publication fails after creation, the new document is retained for diagnosis, not silently deleted. Its error includes `document_id`, `document_url`, `retained_for_diagnosis=true`, and `failure_code`, with recovery details when available. Inspect that document before retrying creation to avoid duplicates.

### Read the chosen tab

Call `docs_read`:

```json
{
  "document": "abcDEF_123-xyz",
  "tab_id": "tab_example",
  "start": 0,
  "max_chars": 30000
}
```

Synthetic response excerpt — not the complete response:

```json
{
  "ok": true,
  "document_id": "abcDEF_123-xyz",
  "revision_id": "revision_example_1",
  "tab_id": "tab_example",
  "content": "وضعیت: پیش‌نویس 🧪\n",
  "next_start": null,
  "verified": true
}
```

The readable body key is **`content`, not `text`**. Selected-tab responses also include `start`, `end`, `total_chars`, and `outline`. Follow `next_start` with the same document and tab until it is `null`; if revisions change between pages, reread rather than treating the pages as one snapshot.

**Privacy boundary:** `max_chars` limits only the returned `content` page, not the complete response. The selected tab's entire heading `outline`, document metadata, and tab inventory are returned separately and are not restricted to that page. Even a one-character page can reveal headings outside the requested range. Do not use pagination as a total-output limit or as permission to disclose only one excerpt.

Metadata fields are `document_id`, `document_url`, `name`, `mime_type`, `modified_time`, `version`, and `revision_id`. Each `tabs` entry contains `tab_id`, `title`, and `parent_tab_id`.

Reading produces plain readable paragraph text, a separate heading outline, and Markdown-like pipe tables. It does not reconstruct all source Markdown or preserve every style. Unsupported non-text elements are represented with markers such as `⟦NON_TEXT:inlineObjectElement⟧`. Do not feed readback blindly into a full replacement when the document contains content the renderer cannot reproduce.

### Preview and apply an exact edit

Call `docs_edit_text` with the revision from that read:

```json
{
  "document": "abcDEF_123-xyz",
  "tab_id": "tab_example",
  "expected_revision_id": "revision_example_1",
  "replacements": [
    {
      "old_text": "پیش‌نویس",
      "new_text": "آماده",
      "expected_count": 1
    }
  ],
  "apply": false
}
```

Preview performs no document mutation. A valid result includes `revision_id`, `valid=true`, and replacement entries containing `expected_count`, `actual_count`, `preexisting_new_count`, and `ranges`. Range entries use `start_index`/`end_index` in UTF-16 units, with `segment_id` when applicable. These are not offsets into the rendered `content` string.

After reviewing the preview, call `docs_edit_text`:

```json
{
  "document": "abcDEF_123-xyz",
  "tab_id": "tab_example",
  "expected_revision_id": "revision_example_1",
  "replacements": [
    {
      "old_text": "پیش‌نویس",
      "new_text": "آماده",
      "expected_count": 1
    }
  ],
  "apply": true
}
```

Apply rechecks the live revision and match counts; preview does not reserve the document. Matching is case-sensitive and non-regex. Duplicate, overlapping, substring-related, or otherwise interfering replacement sets are rejected, including replacements that create another operation's old text. The selected tab's auxiliary segments can affect counts even when absent from the readable body.

An applied result reports `before_revision_id`, `after_revision_id`, and per-operation `occurrences_changed`, before/after old-text counts, and before/after new-text counts. All replacements use one revision-guarded `batchUpdate`. This path sends no additional formatting requests; Google Docs handles existing formatting during `replaceAllText`.

### Insert text without replacing existing content

After a fresh `docs_read`, call `docs_insert_text`. This synthetic example previews a new paragraph at the end of a selected tab:

```json
{
  "document": "abcDEF_123-xyz",
  "text": "\nیادداشت تکمیلی: این بند به سند موجود اضافه می‌شود.\n",
  "expected_revision_id": "revision_example_1",
  "position": "end",
  "anchor_text": null,
  "tab_id": "tab_example",
  "format_profile": "persian",
  "apply": false
}
```

- `start` inserts at body index 1; `end` inserts immediately before the mandatory final newline. Both forbid `anchor_text`.
- `before` / `after` require one exact, case-sensitive `anchor_text` match within a contiguous paragraph text span in the selected body or a table cell. Styled runs can form one match; an anchor cannot span paragraphs, cells, or inline objects. Repeated matches (including overlapping matches) fail with `anchor_match_mismatch`; provide a longer unique anchor rather than choosing an arbitrary occurrence.
- Headers, footers, and footnotes are not insertion targets. The other tabs are never searched to resolve an anchor.
- `text` is literal: `#`, `**`, and pipe syntax do not create headings, emphasis, or tables. Include your own spaces and `\n` separators. For example, inserting `note` after `word` produces `wordnote`, not `word note`.
- Preview returns `applied=false`, `valid=true`, the reviewed `revision_id`, resolved UTF-16 `index`, and `inserted_utf16_length`. It does not change the document or reserve the position. Apply the same arguments with `apply=true`; the live revision and anchor are checked again.
- Apply sends one atomic batch: `insertText` plus scoped styles when Persian is selected. It does not delete or replace existing content. The result includes `applied=true`, before/after revision IDs, and `verified=true` only after exact indexed body-text readback. `formatting_verified=true` additionally means the affected paragraph settings and inserted font passed API readback; insertion does not perform a whole-document DOCX audit.
- `persian` sets RTL, explicit Right alignment, and right-side indentation on paragraphs touched by inserted text, and Vazirmatn on the inserted text. Existing text sharing those paragraphs shares their paragraph settings. Unrelated paragraphs are not reformatted. `plain` sends no formatting requests and inherits Google Docs formatting.

A stale revision must be reread, not silently replaced with a fresh token. A transport or verification failure may occur after a successful write; reread before retrying to avoid duplicate insertion. This tool is not a Markdown merge or a layout-authoring API.

### Replace Markdown on the same document ID

Read again after the edit:

```json
{
  "document": "abcDEF_123-xyz",
  "tab_id": "tab_example"
}
```

Suppose that read returns the synthetic revision `revision_example_2`. Call `docs_replace_markdown` with that exact value:

```json
{
  "document": "abcDEF_123-xyz",
  "tab_id": "tab_example",
  "expected_revision_id": "revision_example_2",
  "markdown": "# یادداشت تازه 🧪\n\nوضعیت: **آماده**\n\n- [ ] بازبینی نهایی\n",
  "format_profile": "persian"
}
```

This replaces the selected tab's body, including content absent from the candidate; it does not preserve old unsupported objects or perform a content merge. Empty Markdown clears the body while leaving the required terminal paragraph and its inherited styles. An already-empty body needs no mutation; semantic readback still runs and the revision may remain unchanged. A successful result includes `before_revision_id`, `after_revision_id`, `semantic` (`schema`, `block_count`, `sha256`), `format_profile`, `formatting`, and `verified=true`.

A non-table replacement uses one atomic mutation batch. Tables require several guarded phases with intermediate readbacks for actual cell indices; the complete table operation is **not** one transaction. Each phase consumes the revision produced by the preceding phase. Recovery exports remain until semantic/format verification and cleanup finish.

**On a stale revision, stop and reread.** Review the new content before forming a new write. Never substitute a newer revision into an old destructive request just to force it through. The server uses `requiredRevisionId`, not collaborator-style `targetRevisionId`; Drive `version` is only diagnostic metadata. A Google-side conflict after the preflight can surface as a sanitized upstream/verification error rather than `stale_revision`; reread after any uncertain write outcome.

### Multi-tab documents

- For a single tab, omission of `tab_id` selects that tab automatically.
- For multiple tabs, `docs_read` without `tab_id` returns metadata and the tab inventory only — no `content`, pagination, or selected-tab outline. Select a returned tab ID and read again.
- All existing-document write tools require an explicit `tab_id` for a multi-tab document. They fail closed with `multiple_tabs_require_tab_id` rather than writing to the first tab. An unknown tab produces `tab_not_found`.
- `tab_id` is a separate argument: a URL fragment does not choose the write target. Revisions guard the document, not just an isolated tab.

## Persian and plain profiles

`persian` is the default for create and Markdown replacement. The profile contract applies these fields independently:

```json
{
  "direction": "RIGHT_TO_LEFT",
  "alignment": "END",
  "indentStart": {"magnitude": 0, "unit": "PT"},
  "indentEnd": {"magnitude": 0, "unit": "PT"},
  "weightedFontFamily": {"fontFamily": "Vazirmatn"}
}
```

RTL alone is not proof of right alignment, zero indentation, or the font. `END` is intentional: the DOCX check expects physical right alignment. In Google API readback, an omitted zero `magnitude` can mean protobuf zero; the indent object and `unit="PT"` still matter.

The formatting contract is:

- Persian headings retain their native level and receive explicit `Vazirmatn` and `bold=true`, even without `**…**`. Expected heading semantics account for this profile-specific bold.
- Body text, text bullets/checklists, mixed-language paragraphs, and all table-cell paragraphs — including empty/padded cells — receive the Persian formatting contract. Explicit body/table bold and link spans still need to match the candidate.
- `plain` renders semantic Markdown styles only: heading levels and explicit `**bold**`/links. It does not impose Persian direction, alignment, font, or blanket heading bold. It is not a command to force LTR or strip all document formatting.
- Replacement resets inherited heading/bold/link state before applying the candidate, so an old heading is not allowed to leak into a new ordinary paragraph. Plain mode does not gain Persian formatting as a side effect of this reset.
- `docs_edit_text` has no profile parameter and does not re-render or reformat the document.

The runtime write path checks semantic readback and, for nonempty Persian Markdown replacement, independently checks the selected tab's API styles and exported DOCX formatting. API style checks use the same revision-checked body as semantic verification. An empty candidate has no new formatting to verify through the API; semantic and Persian DOCX checks still run. This exception does not exempt empty/padded cells or blank paragraphs within nonempty publication. Live acceptance also independently inspects both channels. DOCX export is document-wide, whereas the write target is one tab: unrelated content in other tabs can affect document-wide format verification. `formatting=null` in plain mode means the Persian format checks are skipped, not that a Persian check passed. No automatic language detection is provided.

## Typed errors and recovery decisions

Service failures use this shape; MCP schema-validation failures may instead be rejected by the SDK before the service runs:

```json
{
  "ok": false,
  "error": {
    "code": "stale_revision",
    "message": "The Google document revision changed before the write.",
    "retryable": false
  }
}
```

| Code | Action |
| --- | --- |
| `invalid_document_reference` | Supply an unchanged valid ID or canonical Google Docs URL. |
| `invalid_title`, `invalid_markdown`, `invalid_max_chars`, `invalid_start`, `invalid_input` | Correct the rejected field or reduce the request. `invalid_markdown` also covers reserved markers and rendering-plan limits. |
| `invalid_old_text`, `invalid_new_text`, `invalid_replacement_count` | Fix the exact replacement or its count. |
| `google_needs_reauth` | Reauthenticate through the **existing Google OAuth flow** that provisions the token used by this installation, then reload/restart the MCP connection. This package has no new login subcommand. |
| `credential_storage_failed` | Resolve private token-storage/atomic-write permissions safely; do not print or manually paste credentials into chat. |
| `permission_denied` | Check document access and granted Google scopes; this is distinct from an expired/missing authorization. |
| `unsupported_office_file` | Use a native Google Doc. Office import/conversion is outside this MCP. |
| `document_not_found` | Check the reference and authorized account; the document may be deleted or unavailable. |
| `multiple_tabs_require_tab_id`, `tab_not_found` | Read the tab inventory and select the intended tab explicitly. |
| `stale_revision` | Reread and review changes before preparing a new write. |
| `match_count_mismatch` | Review all selected-tab matches and set an intentional `expected_count`. |
| `overlapping_edits` | Split/reformulate interfering replacements; do not bypass the check. |
| `rate_limited`, `google_unavailable` | Inspect `retryable`; wait when appropriate. Reread before retrying any uncertain mutation. A recovery-backup preparation failure uses `google_unavailable`, not a public `recovery_unavailable` code. |
| `verification_failed` | The outcome was not verified; a write may already have occurred. Read back and diagnose. A failed initial creation may include a retained-document handle. |
| `partial_write_requires_recovery` | Stop writing and inspect the retained recovery exports and current document. Do not blindly retry or roll back over collaborator changes. |

Internal text/offset helpers also define `invalid_text` and `invalid_utf16_offset`; there are no public raw-text-offset tools.

Retry is bounded for safe-to-retry network failures and HTTP 408/429/500/502/503/504: up to five attempts, with exponential waits and bounded `Retry-After`. Non-idempotent document creation is not automatically retried. A retryable flag is not proof that repeating a whole user workflow is safe.

### Recovery exports and retention

Before the first mutation of a replacement whose candidate contains tables, the server exports the current document as TXT and DOCX into:

```text
~/.hermes/google-docs-mcp-recovery/
  recovery-<timestamp>-<random>/
    document.txt
    document.docx
```

The root and per-backup directories use mode **0700**; export files use **0600**. These exports contain private document content and are not encrypted by this package. Backups are not made for every targeted edit or every non-table replacement; they are not a general backup/version-history service.

On full table-write success the backup is removed. On partial failure the error includes `phase`, `revision_id`, `recovery_path`, and `recovery_action`. The supplied revision is a recovery diagnostic, not an invitation to retry. If initial creation wraps the failure, inspect its recovery details and `failure_code` too. Automatic rollback is deliberately absent because it could overwrite a collaborator's edit.

Startup cleanup removes recognized, safely owned recovery directories older than **seven days**, based on their modification time. It is not a daily daemon: if the MCP does not start, cleanup is delayed. Unrecognized or unsafe entries are skipped. Keep a private copy elsewhere if you need a longer retention period.

For manual recovery, open the exact returned directory locally, inspect `document.txt` and `document.docx`, and reconcile/restore the intended content using Google Docs/Drive after reviewing current changes. Once recovery is complete, delete **only those two files in that exact directory**, then remove the now-empty directory. Use your file manager or an explicitly checked, single-directory operation. Do not recursively delete `~/.hermes`, sweep all `recovery-*` entries, or delete a live document based on a shared title prefix. Do not paste exports or live recovery handles into public reports.

## Authentication and privacy boundary

The server reuses `~/.hermes/google_token.json`. It does not copy that token into the repository and has no credential arguments in its public tools. Token loading requires a regular, user-owned file on POSIX; the loader enforces mode 0600, refreshes when possible, and writes refreshed credentials atomically with mode 0600.

The existing OAuth client-secret file, `~/.hermes/google_client_secret.json`, must also be private. Harden file modes without displaying their contents:

```bash
chmod 600 ~/.hermes/google_token.json ~/.hermes/google_client_secret.json
```

Run your Google OAuth setup/reauthentication flow if authorization is missing, invalid, expired without a usable refresh token, or rejected by Google. No package-specific authentication command is supplied here. The client-secret file belongs to that setup; the MCP runtime loads the authorized-user token.

### First-time OAuth setup

If you do not already have a compatible token, provisioning one is a separate prerequisite, not something `uv sync` or MCP registration performs:

1. Follow Google's [Python Docs quickstart](https://developers.google.com/workspace/docs/api/quickstart/python) to create your own Google Cloud project, configure its OAuth consent screen and a **Desktop app** OAuth client. Enable **both the Google Docs API and Google Drive API**. For a personal account using an External app in testing, add yourself as a test user.
2. Run that OAuth setup in a separate private directory and environment, **not this checkout or its `.venv`**. Keep your downloaded client configuration at `~/.hermes/google_client_secret.json` with mode `0600`; adjust the quickstart's client-file path accordingly. Never commit the download.
3. Choose scopes for your actual workflow; the quickstart's read-only Docs scope alone cannot support this server's writes and Drive exports. For editing existing authorized documents, `https://www.googleapis.com/auth/documents` plus `https://www.googleapis.com/auth/drive.readonly` covers Docs writes and Drive metadata/exports. These are broad account grants: review Google's [Drive scope guidance](https://developers.google.com/workspace/drive/api/guides/api-specific-auth). `drive.file` is a narrower option for app-created/explicitly app-authorized files, but pasting an arbitrary existing document URL does **not** authorize it for that scope; this project has no Google Picker. The opt-in live harness also needs permission to delete its own temporary file.
4. Complete browser consent with your own account. Configure your OAuth setup to persist the authorized-user JSON at `~/.hermes/google_token.json` with mode `0600`, rather than leaving `token.json` in a public checkout. The runtime uses `google.oauth2.credentials.Credentials.from_authorized_user_file`: it expects an authorized-user token (including client ID, client secret and refresh token), **not** the downloaded `installed` client configuration, a service-account key, or an API key. Never print or paste these values.
5. Restart the MCP and try `docs_read` on a document you own. A successful tool listing does not test Google authorization. If you change scopes, repeat consent through your OAuth setup; editing the `scopes` field in a token file does not grant permissions.

The token and recovery paths are currently fixed under `Path.home() / ".hermes"`; setting `HERMES_HOME` or selecting a Hermes profile does not relocate them. Run under the intended OS account. You can install and run the offline tests without any Google credentials.

Use an account authorized for the intended documents and Docs/Drive operations. Existing OAuth grants may be broader than the five-tool surface; this server constrains its operations, not the token's global privileges. Keep both files out of source control, tool examples, logs, and config literals. Do not share full environment/config dumps to diagnose startup.

The package has no telemetry and sanitizes service errors rather than returning raw Google responses or credentials. Document content is still deliberately returned by `docs_read` and sent to Google for writes; Hermes/session retention and storage backups are separate from this package's recovery cleanup policy.

## Install: Python 3.12, locked, physically installed, non-editable

Prerequisites: `uv`, Python 3.12 (or permission for `uv` to install it), the existing Google OAuth setup, and Hermes with MCP support. The project requires Python `>=3.12,<3.13`; use its isolated environment, not the Hermes application's Python environment. The project pins `mcp[cli]==1.29.1` and locks dependencies in `uv.lock`.

Clone the repository, then install from the checkout (choose another location if preferred):

```bash
mkdir -p ~/Documents
git clone https://github.com/mohamadsmt/google-docs-mcp.git ~/Documents/google-docs-mcp
cd ~/Documents/google-docs-mcp
uv python install 3.12
env -u PYTHONPATH -u PYTHONHOME uv sync \
  --python 3.12 --locked --all-groups --no-editable \
  --reinstall-package google-docs-mcp --link-mode copy
```

`--no-editable` is required: `.venv` must contain the full physical package, not a source-tree pointer. `--reinstall-package google-docs-mcp` refreshes the local project even when its version has not changed; `--link-mode copy` avoids relying on cache-linked package files. Do not replace this procedure with editable installation or `uv run pytest`, which can re-sync the project into an editable environment.

The canonical launcher is `scripts/run-mcp`. It locates the checkout-relative `.venv/bin/google-docs-mcp`, unsets `PYTHONPATH` and `PYTHONHOME` for that process, and executes the installed console entrypoint. This prevents Hermes-host Python packages from shadowing the project's dependencies; it does not modify Hermes's environment/config. Launch through this wrapper, not a source import or the Hermes interpreter.

### Check installation freshness without loading credentials

From the checkout, compare the physical installed Python files with source:

```bash
env -u PYTHONPATH -u PYTHONHOME .venv/bin/python - <<'PY'
import importlib.metadata
import json
from pathlib import Path
import sysconfig

source = Path("src/google_docs_mcp").resolve()
installed = Path(sysconfig.get_path("purelib")) / "google_docs_mcp"
assert installed.is_dir() and not installed.is_symlink()
source_files = {p.relative_to(source) for p in source.rglob("*.py")}
installed_files = {p.relative_to(installed) for p in installed.rglob("*.py")}
assert source_files and installed_files == source_files
for relative in source_files:
    target = installed / relative
    assert target.is_file() and not target.is_symlink()
    assert target.read_bytes() == (source / relative).read_bytes(), relative
metadata = importlib.metadata.distribution("google-docs-mcp")
direct_url = json.loads(metadata.read_text("direct_url.json") or "{}")
assert direct_url.get("dir_info", {}).get("editable", False) is False
print("physical installed source matches checkout; non-editable metadata")
PY
```

This is a source/install consistency check, not a substitute for wheel inspection, real stdio tests, or final acceptance. Its printed line is the command's success message, not a pre-recorded result.

If source bytes are **proven stale after a forced reinstall**, retry the same locked physical installation with an isolated fresh cache rather than clearing unrelated global caches:

```bash
fresh_cache="$(mktemp -d "${TMPDIR:-/tmp}/google-docs-mcp-uv-cache.XXXXXX")"
env -u PYTHONPATH -u PYTHONHOME UV_CACHE_DIR="$fresh_cache" uv sync \
  --python 3.12 --locked --all-groups --no-editable \
  --reinstall-package google-docs-mcp --link-mode copy
```

Repeat the consistency check and offline tests. Keep that cache path private and remove only that exact temporary directory once no process needs it; a global cache purge or broad recursive deletion is not required.

## Tests and updates

### Offline tests

Use the installed project interpreter directly. Explicitly remove the live-test opt-in from the environment:

```bash
cd ~/Documents/google-docs-mcp
(
  unset RUN_GOOGLE_DOCS_MCP_LIVE
  env -u PYTHONPATH -u PYTHONHOME .venv/bin/python -m pytest -v
)
```

For the real-protocol offline checks specifically:

```bash
env -u PYTHONPATH -u PYTHONHOME .venv/bin/python -m pytest \
  tests/test_mcp_stdio.py tests/test_entrypoint.py -v
```

These commands are instructions to run, not claims about accepted test counts. Discovery/protocol checks alone do not prove that a Google write succeeded.

### Build and update

After reviewing and applying a source update to this checkout, reinstall it physically before testing or restarting the MCP. Dependency changes must be reviewed together with the lockfile; do not silently regenerate the lock during deployment.

```bash
cd ~/Documents/google-docs-mcp
env -u PYTHONPATH -u PYTHONHOME uv sync \
  --python 3.12 --locked --all-groups --no-editable \
  --reinstall-package google-docs-mcp --link-mode copy
(
  unset RUN_GOOGLE_DOCS_MCP_LIVE
  env -u PYTHONPATH -u PYTHONHOME .venv/bin/python -m pytest -v
)
build_output="$(mktemp -d "${TMPDIR:-/tmp}/google-docs-mcp-build.XXXXXX")"
env -u PYTHONPATH -u PYTHONHOME uv build --out-dir "$build_output"
```

Repeat the source/install consistency check. Fresh wheel/sdist output still needs archive and isolated-install verification for release acceptance. Reload the running MCP connection after a successful update; replacing files does not update code already imported by the old process.

### Opt-in live acceptance — creates and deletes a document

Only run this when you authorize real Google API writes and the final source has been installed non-editably:

```bash
cd ~/Documents/google-docs-mcp
env -u PYTHONPATH -u PYTHONHOME RUN_GOOGLE_DOCS_MCP_LIVE=1 \
  .venv/bin/python -m pytest tests/test_live_google.py::test_live_google_insertions_through_mcp -v
```

The live test uses real MCP `ClientSession.call_tool` calls through `scripts/run-mcp`. It creates a tiny private temporary document with synthetic Persian/English/emoji content, reads it, proves preview is non-mutating, applies an edit with independent readback, then injects an **external sentinel edit**. A stale replacement must fail without erasing that sentinel. It then replaces with a fresh revision, checks semantics and Persian API/DOCX formatting, deletes only the run's temporary document, and checks 404. Privacy is independently checked through bounded Drive permission readback in the harness; permission management is not exposed by the MCP.

The insertion acceptance above additionally exercises all four insertion positions on that same run-owned document, including a table-cell anchor and mixed Persian/English/emoji content. Each position proves non-mutating preview, independent exact text/revision readback, and stale-revision rejection; missing and ambiguous anchors must also leave the document unchanged. Persian API/DOCX checks after insertion require direction, right alignment, right indentation, and Vazirmatn; they do not impose Markdown's additional bold-heading rule on literal inserted text. Create/replace checks retain that stricter publication rule. The separate `test_live_google_docs_end_to_end_through_mcp` retains the original create/edit/replace regression; running the entire module creates and deletes one temporary document per test.

The harness attempts cleanup on failures and interrupts, but successful cleanup is an assertion to verify, not a promise that survives every network failure or forced process termination. It keeps a private per-run journal when the created document cannot be resolved safely. Resolve only that exact run's artifact/document; do not prefix-sweep other documents or recovery backups. Keep live IDs, URLs, credentials, and private content out of published test reports. A skipped test is not live acceptance, and old evidence does not validate changed code.

## Register in Hermes

Hermes must use an **absolute launcher path** because its working directory and shell expansion cannot be assumed. Run the registration command from your checkout root; `$(pwd)` supplies your actual path. The YAML below uses an explicit placeholder that you must replace with that same absolute path.

First inspect existing server names:

```bash
hermes mcp list
```

If `google_docs` is absent, use the official CLI and confirm enabling it:

```bash
printf 'Y\n' | hermes mcp add google_docs \
  --command "$(pwd)/scripts/run-mcp" \
  --connect-timeout 30
```

If it already exists, inspect that entry and update its existing launcher/policy rather than creating a duplicate. Set explicit policy fields:

```bash
hermes config set --force mcp_servers.google_docs.timeout 180
hermes config set --force mcp_servers.google_docs.connect_timeout 30
hermes config set --force mcp_servers.google_docs.sampling.enabled false
hermes config set --force mcp_servers.google_docs.tools.include '["docs_read","docs_create","docs_replace_markdown","docs_edit_text","docs_insert_text"]'
```

The resulting `~/.hermes/config.yaml` entry must match this policy; merge only this server entry, not the whole config:

```yaml
mcp_servers:
  google_docs:
    command: "/absolute/path/to/google-docs-mcp/scripts/run-mcp"
    args: []
    timeout: 180
    connect_timeout: 30
    sampling:
      enabled: false
    tools:
      include:
        - docs_read
        - docs_create
        - docs_replace_markdown
        - docs_edit_text
        - docs_insert_text
```

No credential literals or secret environment values belong in this entry. Read back the exact target entry locally after registration: confirm the launcher, empty args, timeout 180, connect timeout 30, disabled sampling, and exactly the five allowlisted names. Check an existing entry for exclusions or other policy that could prevent those tools from being exposed. Share only safe policy fields and environment **key names**, never values or a full config dump.

```bash
hermes mcp list
hermes mcp test google_docs
```

Require the server to be enabled, connection/discovery to succeed, and exactly the five expected tools to be discovered. `hermes mcp test` proves discovery, not Google write correctness. The opt-in insertion acceptance separately checks all five tool paths against Google; offline tests alone are not proof of a live insertion.

After registration or an update, use **`/reload-mcp`** in the running Hermes chat, or start a fresh Hermes chat/process. Then verify the tool inventory. An existing long-lived session is not automatically proven to have new schemas merely because config was saved or a separate CLI test passed. Hermes normally prefixes server tools as `mcp_google_docs_<tool_name>`; use the actual discovered inventory in your client.

Hermes references: [MCP integration and reload](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp), [CLI reference](https://hermes-agent.nousresearch.com/docs/reference/cli-commands). The commands above are operating instructions, not a substitute for fresh acceptance evidence after an update.

## Contributing and publication hygiene

Open an issue with a synthetic reproduction, or submit a focused pull request with offline test evidence. Do not attach real document content, IDs/URLs, recovery exports, OAuth files, environment dumps, or screenshots containing private data. See [SECURITY.md](SECURITY.md) for sensitive reports.

`.gitignore` excludes common credentials, local agent state, exports, recovery folders, build output and internal planning notes. It does **not** remove already tracked files or Git history, and it cannot recognize every secret filename. Before pushing, inspect `git diff --cached` and run a secret scanner such as `gitleaks git . --log-opts="--all" --redact`. Scan new/untracked candidate files too. Never force-add ignored credentials or artifacts. Source distributions use an explicit include list; inspect both wheel and sdist before distributing them. Examples and test fixtures must remain synthetic.

## License

[MIT](LICENSE) — Copyright (c) 2026 Mohamad Takalloo. Third-party dependencies retain their own licenses.
