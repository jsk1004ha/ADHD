---
name: apzn-documents
description: Main workflow for faithful editing and authoring of PPTX, DOCX, HWP, HWPX and PDF. Preserve original templates, use existing document skills, inspect structure, edit native objects, render and independently inspect every final page.
---
# Documents are a main deliverable, not a code export
First read the current template/guide/source versions. Write down required sections, exact numerical limits, font/spacing/table/image roles, editable-object demands and what must remain unchanged. Do not extrapolate a previous 2-page or 1300-character assignment to this one. Never replace a named original with a same-topic summary. Plan before editing.

Route the user's already installed skills (document/PDF/slides/HWPX/Office) before coding a new converter. Run `APZN_PYTHON APZN_ROOT/adhd.py doc capabilities` and `doc inspect SOURCE`. Prefer programmatic extraction for text and image rendering for layout. OCR is last resort. Existing fonts are used locally and never redistributed.

## Editors
- PPTX/DOCX: existing native object-aware skills or python-pptx/python-docx for authoring; preserve headers, masters, notes, relationships, fields and formatting. Do not rasterize editable text/tables/diagrams into one image when the user needs objects. Font changes are actual requested style edits, not plain text replacement.
- HWPX: valid OWPML ZIP, not renamed DOCX. Prefer installed Hancom/HWPX skill. Exact local XML text edits preserve unmodified ZIP members; schema and native rendering still needed.
- HWP binary: use installed hwpkit or native Hancom, not raw ZIP editing. Adapter is optional and Windows/native fidelity is not proven by a library save. If unavailable, provide a clearly labelled alternate format and disclose HWP remains unfinished; never return a renamed ZIP.
- PDF: use installed PDF tools / PyMuPDF / pypdf for requested merge, annotation, form and page tasks; for faithful substantial text reflow, edit the source document and re-export when available. Complex PDF reflow is not a universal editable-DOCX conversion. Sensitive redactions require irreversible removal and re-extraction checks, not black rectangles.

For a narrow exact text edit: inspect returns `text_nodes` with part/node/text. Use `doc edit SOURCE --out NEW --payload-file EDITS.json` with `[{"part":"actual.xml","node":42,"before":"exact old text","after":"requested text"}]`. Only exact native text nodes; cross-run replacements need a specialist editor. Original and existing outputs are never overwritten. Keep edit receipt. For binary HWP use `doc edit-hwp` only when hwpkit is installed and matches its documented API.

## Final gate
Protect original inputs in begin. Write final files to new paths. Run `doc render FINAL --out NEW_RENDER_DIR`; native HWP/HWPX requires `--backend hancom` on Windows with installed Hancom and pywin32 (never bypass its security prompts). Rendering conversion is isolated per run; operating-system sandbox remains necessary for untrusted documents.

Inspect EVERY page/slide at usable zoom: clipping, overlap, glyphs, tables, page breaks, mixed Korean/Latin font fallbacks, diagrams, headers/footers, spacing and real user limits. Edit and rerender the final file when needed. Manifest images are not visual approval. Submit document_evidence and obtain independent verifier document_reviews per v12-gates.md. Verify actual final file hash and all page images, not a stale preview. Final response lists produced formats and actual unresolved format/fidelity limits.
