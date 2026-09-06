# Scoped structured editing

These four tools extend the original five without changing literal insertion, exact replacement, or whole-tab publication semantics. They are narrow operations, not a general Docs `batchUpdate` proxy. All existing-document writes require `expected_revision_id` from a fresh read and default to `apply=false` (read-only preview).

## Common workflow

1. `docs_read` the exact document/tab. For multi-tab documents, explicitly select a returned tab ID.
2. Select the heading, paragraph or table at that revision. `table_index`, `row_index` and `column_index` are zero-based; table_index is the ordinal among top-level tables in the selected tab, not an API character index.
3. Preview with `apply=false`, review the resolved scope, then repeat with `apply=true` and the same revision. Preview does not reserve the document.
4. Inspect the verified result. After any uncertain write outcome, reread before retrying; never substitute a fresh revision into an old destructive payload blindly.

New mutation batches disable automatic transport retries and use `requiredRevisionId`. Multi-phase operations may have already changed the document when a later phase fails. Their private TXT/DOCX recovery exports are retained for diagnosis; they are not an automatic rollback mechanism. Successful scoped checks do not certify the formatting of unrelated document content.

## `docs_edit_section`

Arguments: `document`, `markdown`, `expected_revision_id`, `action="insert"`, `position="end"`, `anchor_text=null`, `heading_text=null`, `tab_id=null`, `format_profile="persian"`, `apply=false`.

- `insert`: start/end of the selected body, or before/after one exact full top-level paragraph. Supply the paragraph text without its final newline. Unlike `docs_insert_text`, this tool inserts rendered Markdown at paragraph boundaries, not inside a word/run/cell.
- `replace`: supply the exact unique native heading text. The heading itself is retained. Its following content, including lower-level subheadings and tables, is replaced up to the next equal-or-higher heading. Use the default position and omit anchor_text. Empty Markdown clears that section, not the heading or other sections.
- Supports the existing renderer's headings, bold, HTTP(S) links, literal bullet/checklist glyphs and native tables. It is not a full GFM implementation.
- Missing/ambiguous headings or paragraph anchors and unsupported destructive targets are rejected. Content outside the selected range is not republished from the lossy read view.
- New paragraphs and their separators are deliberate structural output, unlike literal insertion which never adds separators. Table cells use actual post-insertion API indices and guarded phase readbacks.

Synthetic preview (replace identifiers/revision with your reviewed values):

```json
{
  "document": "synthetic_doc_123",
  "tab_id": "tab_example",
  "expected_revision_id": "reviewed_revision",
  "action": "replace",
  "heading_text": "شاخص‌ها",
  "markdown": "مقدار **به‌روز**\n\n| شاخص | هدف |\n| --- | --- |\n| نمونه | پنج |",
  "format_profile": "persian",
  "apply": false
}
```

## `docs_format`

Arguments: `document`, `expected_revision_id`, `tab_id=null`, `heading_text=null`, `format_profile="persian"`, `right_indent_pt=0`, `apply=false`.

Omit heading_text to select the whole tab body; otherwise select that heading and its section. Table-cell paragraphs in scope are included. No text replacement is performed; links, bold, native heading/list/object structure are retained.

- `persian`: RTL, explicit `START` alignment (physical Right), explicit zero left indent, requested physical right indent and Vazirmatn font. This supersedes the incorrect old END alignment contract for all Persian profiles; no existing document is automatically reformatted.
- `english`: explicit LTR/`START` (Left), explicit zero left indent and requested physical right indent; existing fonts are retained.
- right_indent_pt must be a finite JSON number from 0 through 144 points. Booleans and numeric strings are not accepted.
- An already compliant selection is a verified no-op; no write is sent.
- This operation does not change what the original `persian`/`plain` create/replace/insert profiles mean. In particular, `plain` is still not an explicit English formatting command.

## `docs_manage_tab`

Arguments: `document`, `expected_revision_id`, `action`, `tab_id=null`, `title=null`, `parent_tab_id=null`, `index=null`, `apply=false`.

- `create`: title required, tab_id forbidden. No parent means root level; no index means append among that parent's children.
- `rename`: existing tab_id and new title required; parent and index forbidden.
- `move`: existing tab_id and destination index required; title forbidden. No parent means root level. Index refers to the destination siblings **after removing the source tab**. A tab cannot become its own ancestor.
- Titles are nonblank and limited to 200 characters. These operations do not delete tabs or rewrite their contents.
- Readback verifies the exact resulting tab identity/title/parent/order and preservation of existing contents. Sibling positions shift only as required by the chosen operation.

## `docs_edit_table`

Arguments: `document`, `expected_revision_id`, `action`, `table_index`, `row_index=null`, `column_index=null`, `markdown=null`, `side=null`, `tab_id=null`, `format_profile="persian"`, `apply=false`.

`docs_read` now includes a selected-tab `tables` inventory with `table_index`, `rows`, `columns`, and `editable`. The inventory contains no extra cell text. Like the outline, it describes the selected tab independently of the paginated content excerpt.

| Action | Required selectors/content | Behavior |
| --- | --- | --- |
| `set_cell` | row_index, column_index, markdown | Replace one cell's content using supported inline text/bold/links; empty string clears text while retaining the mandatory cell paragraph. |
| `insert_row` | row_index, side=`before`/`after` | Insert one empty row relative to the selected row. |
| `insert_column` | column_index, side=`before`/`after` | Insert one empty column; before/after means physical left/right. |
| `delete_row` | row_index | Delete one row, never the last remaining row. |
| `delete_column` | column_index | Delete one column, never the last remaining column. |

Extraneous selectors/content are rejected, not ignored. Merged and nested target tables are unsupported and rejected before mutation: Google can expand merged-cell deletions to multiple rows/columns, and deleting the last row/column deletes the entire table. The tool intentionally does neither. Unrelated cells, other tables and other tabs remain outside the write scope.

New empty cells receive the requested profile. `plain` does not impose Persian layout. Structural changes may need separate structure/style batches and recovery exports; they are not advertised as a whole-operation transaction.

Column before/after is a physical side; the logical cell-array order can differ in RTL tables. Apply reports `scope.inserted_column_index` from verified readback, not arithmetic on the reference index. If indistinguishable adjacent empty columns prevent unique identification, the operation stops before cell styling and retains recovery exports; it never guesses which existing cell to reformat.

## Verification and real acceptance

Offline tests cover strict inputs, preview/nonmutation, stale revisions, Unicode indices, exact payloads, preserved scopes, corrupted readback and partial-write recovery. Run the complete explicit sdist test manifest rather than ignored local sync-conflict test copies.

With explicit authorization for private temporary synthetic Google documents and their deletion:

```bash
env -u PYTHONPATH -u PYTHONHOME RUN_GOOGLE_DOCS_MCP_LIVE=1 \
  .venv/bin/python -m pytest tests/test_live_structured.py -v
```

The harness reuses existing permission checks, exclusive run journal, exact-ID cleanup and independent deletion verification. It exercises the new MCP tools, uses a real first phase plus an injected later failure for recovery, and exports a synthetic PDF into pytest's private temporary workspace for visual inspection. It never selects pre-existing user documents. A skipped test is not live acceptance.
