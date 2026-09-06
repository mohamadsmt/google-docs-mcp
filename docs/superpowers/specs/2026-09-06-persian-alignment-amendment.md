# Approved amendment: physical Persian right alignment

Status: user explicitly authorized unified correction after the visual failure was disclosed.

This amendment supersedes every END/right-alignment assertion in the prior project design, insertion design and structured-editing design. Preserve all other contracts, existing tool signatures and scopes. No pre-existing user document may be mutated during delivery.

## Authoritative facts

Google Docs discovery ParagraphStyle.alignment defines START as left for LTR and right otherwise; END is right for LTR and left otherwise. Source: https://docs.googleapis.com/$discovery/rest?version=v1 and https://developers.google.com/workspace/docs/api/reference/rest/v1/documents#ParagraphStyle .

A real synthetic Google PDF at source commit b3f7ab37b70b84752462a8fdd7c3c029aafef699 visibly placed short Persian headings, list text and table-cell contents at the left. All 1064 automated tests (including live API/DOCX tests) passed: those old oracles repeated the incorrect alignment contract and are not proof of physical alignment.

## Required correction

1. Use explicit RTL + START for physical Right in every Persian create/replace/insert/section/table/format path. Explicit English remains LTR + START (Left). Preserve independent indent and font semantics.
2. Convert positive API fixtures to START and negative alignment sentinels to END; do not erase corruption tests.
3. Characterize real Google START/END exports before revising any DOCX oracle. Separate native Docs visual alignment from exported OOXML markup; never relabel a physical-layout test from a raw token alone.
4. Keep tests fail-closed and preserve text, bold, links, native lists and non-target objects.
5. Run fresh canonical physical-install acceptance with exact source/install identity, complete sdist test manifest including authorized live tests, plus native PDF visual inspection of short headings, list items, mixed language and table cells.
6. Finish independent specification PASS followed by quality/security APPROVED on the exact frozen final commit. Update only default-profile google_docs tool allowlist to the nine tools and read back/test discovery. Commit locally, no push.

If Google export behavior contradicts the native Docs physical alignment, label and test the representations separately rather than reverting a visually correct native profile to satisfy a stale exporter assumption.
