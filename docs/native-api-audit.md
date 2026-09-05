# Native Google API audit

Reviewed 2026-09-05 / 1405-06-14.

## Decision

Retain the existing five-tool API and explicit Docs renderer. Add `deleteParagraphBullets` to nonempty replacement and reject Google-stripped emitted text before remote operations. Do not silently swap creation to Drive import, add a generic Request proxy, change existing Persian profile values, or weaken revision/tab/occurrence guards.

## Real Drive Markdown characterization

`about.importFormats` advertised `text/markdown` → `application/vnd.google-apps.document`. A real multipart `files.create` on a private synthetic document produced:

| Property | Observed |
| --- | --- |
| Native heading-1 paragraphs | 1 |
| Bold text runs | 4 |
| Linked text runs | 2 |
| Native-list paragraphs | 2 |
| Native tables | 1 |
| Explicit RTL paragraphs | 0 |
| Vazirmatn text runs | 0 |
| Semantics equal to this project's subset | false |

These are observed direct API values for this fixture, not universal importer fidelity claims or a performance benchmark. Import works, but produces different semantics and needs Persian post-formatting. Existing creation supports Docs writes plus Drive read-only metadata/export grants; Drive import additionally requires an appropriate Drive write grant. Import-update replaces the entire document, not just one selected tab, and does not provide the current Docs `requiredRevisionId` contract. A separate richer import workflow could be useful later; this audit does not expose one through MCP.

## Adopted corrections

1. The live numbered-list regression failed on both `plain` and `persian` before the change: replacing with ordinary text/literal-glyph list left native list metadata behind. `deleteParagraphBullets` now clears it inside the same revision-guarded batch. It precedes profile styles because deletion can add nesting indentation. Both profiles passed the same real regression after the change.
2. Semantic readback now marks unexpected native lists as unsupported in body and table cells, including empty paragraphs, without emitting list IDs. Empty-model replacement keeps its no-style-write behavior; an unremoved native list cannot be falsely certified as equivalent empty output.
3. Emitted inline body/cell text rejects U+0000–U+0008, U+000C–U+001F, U+E000–U+F8FF and surrogate code points with sanitized `invalid_markdown`, before create/replace I/O. This prevents Google's stripping from shifting UTF-16 style ranges. Ignored frontmatter, CRLF normalization, valid tabs/newlines, Persian ZWNJ and emoji remain supported.

No dependency, tool-schema, sharing, existing-document, insertion or exact-edit changes were needed.

## Complete Request inventory

The supplied live HTML has **48** union members. Public discovery revision **20260901** has **40**; the eight HTML-only collaboration entries are classified below. Inventory coverage was compared programmatically: every HTML member appears exactly once.

| Category | Request members | Decision |
| --- | --- | --- |
| Already used; retain | `replaceAllText`, `insertText`, `updateTextStyle`, `updateParagraphStyle`, `deleteContentRange`, `insertTable` | Exact tab scoping, literal case-sensitive replacement, UTF-16 ranges, field-mask resets and real table-index readback already implement the current workflows. |
| Adopted narrowly | `deleteParagraphBullets` | Remove inherited native lists during nonempty Markdown replacement, before final profile styling. One additional subrequest in the existing batch; no additional network round trip. |
| Not adopted: list authoring | `createParagraphBullets` | Changes the documented literal-glyph bullet/checklist representation; leading tabs can be removed and shift indexes. |
| Not adopted: named ranges | `createNamedRange`, `deleteNamedRange`, `replaceNamedRangeContent` | Adds lifecycle/identity semantics; not a substitute for exact occurrence counts and required revision guards. |
| Deferred: table layout/editing | `insertTableRow`, `insertTableColumn`, `deleteTableRow`, `deleteTableColumn`, `updateTableColumnProperties`, `updateTableCellStyle`, `updateTableRowStyle`, `mergeTableCells`, `unmergeTableCells`, `pinTableHeaderRows` | No requested row/column editing, merge, custom layout or repeated-header workflow. These do not replace text/paragraph styles inside cells. |
| Deferred: objects/chips | `insertInlineImage`, `replaceImage`, `deletePositionedObject`, `insertPerson`, `insertRichLink`, `insertDate` | Not part of current Markdown subset. Image fetching/hosting and rich-chip semantics need their own scoped design. |
| Deferred: layout/templates/segments | `insertPageBreak`, `updateDocumentStyle`, `createHeader`, `createFooter`, `createFootnote`, `updateSectionStyle`, `insertSectionBreak`, `deleteHeader`, `deleteFooter`, `updateNamedStyle` | No current layout/template authoring requirement. Named-style defaults add inheritance concerns rather than replacing explicit affected-paragraph formatting. |
| Deferred: tab authoring | `addDocumentTab`, `deleteTab`, `updateDocumentTabProperties` | Current tools select existing tabs; they do not manage tab lifecycle. |
| Out of scope: collaboration; HTML only | `insertComment`, `addCommentReply`, `updateCommentPost`, `deleteComment`, `deleteCommentReply`, `acceptSuggestion`, `rejectSuggestion`, `deleteSuggestion` | Present in the HTML reference but absent from the fetched public discovery schema; do not infer deployability from HTML alone. No requested comment/suggestion mutation workflow. |

## Other alternatives retained as-is

- `insertText.endOfSegmentLocation` is valid, but does not eliminate the index computation required by insertion preview, affected-range formatting and independent readback. Changing only the wire location would not simplify this path.
- Keep `requiredRevisionId`; `targetRevisionId` merges collaborator changes and would weaken the existing exact-version contract.
- Keep `tabsCriteria` plus `matchCase=true` and `searchByRegex=false` for replacements; regex mode is not useful for literal edits.
- Keep table phase readbacks. `insertTable` introduces structural offsets; inferred indexes would be less reliable.
- Field masks already reset inherited `bold`/`link` and order named styles, fonts and emphasis intentionally. Combining requests only to reduce a subrequest, without removing a network round trip, did not justify risking those semantics.

## Evidence and limitations

- Canonical baseline suite: 667 passed, 2 live tests skipped.
- New focused RED run: 31 failed, 1 compatibility characterization passed; failures demonstrated absent cleanup, invisible bullet metadata and absent input guards.
- After the corrective delta: 490 focused parser/client tests passed.
- Fresh native API live run: 3 passed; import characterization plus list replacement in both profiles. Every run-owned document was verified private, deleted by exact ID and independently checked absent via Drive metadata. No existing user document was selected or modified.
- Final complete manifest suite: 699 passed, 5 live tests skipped. The separately authorized live runs passed all three native-API tests and the full MCP insertion acceptance (which also covers create/read/edit/replace, stale revisions, Persian API/DOCX checks and exact cleanup). A live characterization PASS does not mean importer/renderer equivalence.
- The canonical physical non-editable installation was rebuilt and every installed source file matched the checkout byte-for-byte. Wheel/sdist builds succeeded; wheel modules matched source and the sdist contained the new test/audit without local duplicate copies.
- Generic `pytest` at baseline also collected three Git-ignored local ` 2.py` copies and failed an obsolete skipped-count assertion in `test_live_harness 2.py`. Final canonical testing uses the complete explicit sdist test manifest, without modifying/deleting those copies.

## Re-run

Offline: run all `tests/*.py` files explicitly named in `tool.hatch.build.targets.sdist.only-include`, with `RUN_GOOGLE_DOCS_MCP_LIVE` unset and the physically installed project interpreter.

Live (explicit synthetic-write/deletion authorization required):

```bash
env -u PYTHONPATH -u PYTHONHOME RUN_GOOGLE_DOCS_MCP_LIVE=1 \
  .venv/bin/python -m pytest tests/test_native_api.py -v -s
```

The new tests reuse `test_live_google` privacy checks, per-run journal, exact-title/time/owner reconciliation and deletion verification. They are tests, not an alternate publisher or public API proxy.

## Official sources

- [Docs Request reference](https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/request)
- [Docs batchUpdate and WriteControl](https://developers.google.com/workspace/docs/api/reference/rest/v1/documents/batchUpdate)
- [Public Docs discovery](https://docs.googleapis.com/$discovery/rest?version=v1)
- [Drive import/conversion guide](https://developers.google.com/workspace/drive/api/guides/manage-uploads#import-docs)
- [Drive about/importFormats](https://developers.google.com/workspace/drive/api/reference/rest/v3/about)
