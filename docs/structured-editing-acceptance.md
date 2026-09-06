# Structured-editing acceptance evidence

Date: 2026-09-06 / 1405-06-15.

## Scope and approved correction

The four new tools are `docs_edit_section`, `docs_format`, `docs_manage_tab`, and `docs_edit_table`; the existing five tools remain available. The authoritative user-facing contracts are in [structured-editing.md](structured-editing.md). The user-approved [alignment amendment](superpowers/specs/2026-09-06-persian-alignment-amendment.md) supersedes the old END/right-alignment assumption, not the original tool signatures or mutation boundaries.

No pre-existing user document was selected for a write. Live tests use only private run-owned synthetic documents; exact-ID deletion and independent deletion readback are mandatory cleanup, including on failure. No push was performed. Enabling the tools does not migrate or reformat any existing document.

## Executed evidence before candidate freeze

- RED: new regression tests rejected the old Persian API/profile behavior and old bidi-unaware DOCX justification oracle: 6 failed, 4 passed, 1 opt-in skipped.
- Fresh canonical Python 3.12 non-editable `.venv`, locked dependencies, clean Python environment without ambient `PYTHONPATH`/`PYTHONHOME`.
- Byte equality checked for all nine source/install modules.
- Complete explicit sdist test manifest, offline: **1068 passed, 7 opt-in skipped**.
- Complete manifest with `RUN_GOOGLE_DOCS_MCP_LIVE=1`, fresh output directory: **1075 passed, 0 failed, 0 skipped**, 264.81 seconds.
- `python -m build`: wheel and sdist built successfully. Wheel contains exactly the nine expected Python modules with source-identical bytes; sdist contains the complete declared manifest including the new alignment tests, without ignored sync-conflict copies.
- `git diff --check`: passed.

The live structured scenario invokes the installed MCP through stdio: all four section insertion positions, section replacement with native table, cell replace/clear, row and column insertion/deletion, Persian/English formatting, formatting no-op, tab creation/rename/reordering/reparenting, and preservation checks. Stale/ambiguous/unsupported targets and partial-write recovery have separate rejection/recovery checks. Full tool inventory/schema/forwarding and strict argument validation are covered offline.

The independent START/END characterization wrote identical synthetic text with both alignments. Google readback and actual PDF confirmed RTL/START = physical Right and RTL/END = physical Left. Actual DOCX exports were respectively `w:bidi` + `w:jc=left` and `w:bidi` + `w:jc=right`. The revised oracle resolves effective inherited bidi and justification together, with an eight-case RTL/LTR matrix.

## Visual acceptance

Native Google PDF rendered locally, inspected directly:

- `structured-persian.pdf`, page 6: short Persian headings, literal bullet, mixed Persian/English lines and text in both native tables are visibly right-aligned; bold/link text and right indentation are retained, without visible clipping.
- Page 7: final mixed-language insertion is visibly right-aligned, without clipping. This page is a continuation paragraph, not a table.
- `rtl-start.pdf`: short heading and Persian/English line visibly align on their right edge. The paired END export demonstrates the opposite behavior.

The seven-page structured PDF includes Google's generated tab divider pages; those are not body paragraphs rewritten by the formatting operation. Visual verification is distinct from API/DOCX checks; the old passing automated suite was not proof of correct appearance.

## Frozen local provenance

- Source-module manifest SHA-256 (sorted path→file-SHA JSON): `707c7557d41d51058671ed6b503cae87aa09d3ed9a6edef073208071923e92d2`.
- Complete live JUnit: run directory `gdocs-alignment-live-15jnetsl`; SHA-256 `31328a3a0420c7cebf9314dff5c3f0482f559a07c0d2ca74d786a08b7df9f089`.
- Structured native PDF SHA-256: `858c98e1b2e6858193e6f703321199c8fb07264472520feb68cc48fb07cacfba`.
- Rendered images: `.artifacts/alignment-final-visual-8zoro0w0/`.
- Wheel: `.artifacts/alignment-build.56grVZ/google_docs_mcp-0.1.0-py3-none-any.whl`; SHA-256 `9846d8cc4a5e4e5b6cc0489bfac20b401041ed006f60a62af3b91d242c2fe989`.
- Sdist: `.artifacts/alignment-build.56grVZ/google_docs_mcp-0.1.0.tar.gz`; SHA-256 `68c8806b5b8181394c2f34b1bbe1e10e78f0a7a40e390cc369529be35df4872c`.

Failed diagnostic runs were not resumed or substituted for this evidence. A run with pytest basetemp accidentally inside the checkout failed the intentional outside-checkout launcher assertion; the successful acceptance above uses a fresh OS-temporary directory outside the checkout.

Independent specification and quality/security verdicts are separate release gates bound to the candidate commit containing this report. A final post-commit reinstall/retest and default-profile allowlist readback/discovery are recorded separately by the releasing agent; this pre-freeze report does not claim those later steps happened already. Existing Hermes chats require `/reload-mcp` or a fresh chat to acquire changed registration.
