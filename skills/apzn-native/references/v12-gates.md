# ADHD native gates v0.1 (adapted from upstream v1.2)
All commands are performed by the agent through normal Codex tools, not instructions the user must keep typing. Use the exact installed Python/root paths; PowerShell uses `& "APZN_PYTHON" "APZN_ROOT/adhd.py" ...`.

## Plan
Submit `native plan --session SESSION --workspace WORKSPACE --payload-file PLAN.json` after begin. Alternatively put a `plan` object in begin. This is mandatory for nontrivial runs; not necessary for a short direct answer. Example, replace every field with actual evidence and decisions:
```json
{"objective":"Actual requested result","approach":"Chosen path and concise reason","alternatives":["Existing tool versus new implementation; why chosen"],"risks":["Concrete uncertainty and mitigation"],"preflight":["Actual inputs, dependency availability and baseline checks"],"verification":"Actual acceptance commands / every-page render inspection","steps":[{"id":"S1","action":"Inspect source and establish baseline","depends_on":[],"requirements":["R1"]},{"id":"S2","action":"Implement and independently verify","depends_on":["S1"],"requirements":["R1","R2"]}]}
```
Every criterion must appear in at least one step. Dependencies reference earlier steps. After user amendment/sync-intent, update the plan too. The gate checks structure/coverage, not whether the model genuinely reasoned well. Parent shell writes cannot all be classified as implementation, so the gate directly blocks delegated implementation and final candidates without plans, not every possible write.

## Protected originals and document constraints
Begin may add:
```json
{"protected_inputs":["input/template.docx"],"documents":[{"path":"output/final.docx","max_pages":2},{"path":"output/slides.pptx","editable_required":true}]}
```
Use ONLY actual current limits. `exact_pages`, `max_chars`, `min_chars`, `count_whitespace` supported; character limits apply to the complete extracted document, NOT an unspecified Abstract. For a section limit, extract that exact section to a text artifact and verify its characters separately. Document count includes headers/notes when extracted. No inference of layout from character count.

Render each final document into a new folder with `adhd.py doc render FINAL --out RENDER_FOLDER`. HWP/HWPX need native Hancom `--backend hancom` or an existing skill that produces the same real render manifest. A missing renderer blocks visual approval. The final candidate additionally includes:
```json
{"document_evidence":[{"path":"output/final.docx","render_manifest":"output/render/render.json"}]}
```
The controller checks all image/PDF hashes and adds them to the candidate snapshot. Verifier MUST open every page image and return, alongside the original verdict fields:
```json
{"document_reviews":[{"path":"output/final.docx","render_sha256":"HASH_FROM_CANDIDATE","inspected_pages":[1,2],"pass":true,"evidence":"Actual checks: no clipping/overlap, font hierarchy, tables, required sections and template fit"}]}
```
A claimed page list is not a machine proof of visual correctness. The separate verifier must really inspect those images. Any subsequent edit invalidates the review.
