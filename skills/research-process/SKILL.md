---
name: research-process
description: Generic vendor-parameterized research processor. Pops items from a vendor's inbox, extracts text, drafts a six-section investor memo via Claude, and appends to the vendor's master Google Doc.
---

# research-process

Single SKILL parameterized by `--vendor=<name>`. Companion to `research-fetch`. Replaces
(after 1-week soak per PLAN H5) the legacy `gs-research-pdf-processor` and
`jefferies-pdf-processor` SKILLs, and absorbs the per-vendor write-to-doc logic that was
previously inline in `rbn-daily-sync` and `peakload-weekly-sync`.

---

## Inputs (CLI flags)

```
--vendor=<name>          Required.
--tenant=<short>         Optional. Defaults to active tenant.
--max-items=<N>          Optional. Limit per run (default: process whole inbox).
--dry-run                Optional. Extract + draft memo, print to stdout, do NOT write Doc.
```

---

## Generic flow

1. **Resolve config.** Read `~/cos-pipeline-config-<tenant>/research-vendors/<vendor>.yaml`.

2. **Pop next inbox item** from `~/cos-pipeline/data-<tenant>/research/<vendor>/inbox/`.
   Process oldest-first (mtime ascending). If empty, exit 0.

3. **Extract text** per `process.extraction`:

   - `pdfminer` — use `pdfminer.six.high_level.extract_text`. Cap at
     `process.max_pages_per_pdf` pages (default 80).
   - `pymupdf` — use `fitz` (faster, better for image-heavy reports).
   - `html-to-md` — strip nav/footer/paywall blocks, convert to markdown.
   - `text-passthrough` — read as UTF-8 (CSV, TXT).

4. **Draft a six-section investor memo** via the Claude API (model from
   `process.memo_model`, falling back to the default in `routines.yaml`). Prompt enforces
   the CLAUDE.md six-section structure:

   1. THE CORE ARGUMENT
   2. POINTS OF CONSENSUS
   3. POINTS OF DISAGREEMENT OR TENSION
   4. OPEN QUESTIONS AND UNRESOLVED ISSUES
   5. WHAT YOU WOULD NEED TO FORM A VIEW
   6. KEY NAMES AND FIRMS
   7. ACTION ITEMS (machine-readable block per CLAUDE.md)

   Default model: `claude-sonnet-4-6` (CLAUDE.md per-pass table — Pass 3 IC Memo
   Production). Override per-vendor via `process.memo_model`. Max tokens: 4096.

5. **Resolve master Doc.** Look up `firm_context.yaml :: google_docs.<process.master_doc_key>`.
   Fail loud if missing (PLAN B1 rule — never hardcode Doc IDs).

6. **Append to the master Doc** following the per-vendor convention from CLAUDE.md
   (HEADING_2 entry title with date, then the six-section memo, then a `═══` separator).
   For RBN, prepend instead of append (most-recent-first per existing convention).

7. **Mark processed.** Append a record to the vendor's dedup tracker
   (`dedup_tracker` from the YAML — same file `research-fetch` writes to, but the
   `processed_at` field is filled in by this step). Move the inbox file to
   `~/cos-pipeline/data-<tenant>/research/<vendor>/done/YYYY/MM/`.

8. **Summarize.** Print one-line status per item; print final count.

---

## Per-vendor processing notes

### gs
- `extraction: pdfminer` (Marquee PDFs are text-extractable).
- Classify Type A (company-specific) vs Type B (sector/thematic) before memo generation;
  pass classification into the prompt so the memo emphasizes the right axis.
- Master Doc: routes to one of GS_Macro_Market / GS_Energy / GS_Technology / GS_Telecom /
  GS_Power_Utilities / GS_General. Use a sub-key under `google_docs.gs.<sector>` and have
  the prompt classify which.

### jefferies
- `extraction: pdfminer`.
- Master Doc: `firm_context.yaml :: google_docs.jefferies` (single doc, sector subdivisions
  inside).

### rbn
- `extraction: html-to-md`. Article HTML already includes inline image alt text — preserve.
- Master Doc: `firm_context.yaml :: google_docs.rbn`.
- Insertion order: prepend (most-recent-first) per existing convention.
- Skip silently if the article URL is already in the doc (writer-side dedup remains).

### peakload
- `extraction: text-passthrough` (CSV).
- Master Doc: `firm_context.yaml :: google_docs.peakload` (or upload CSV directly to Drive
  file ID per YAML; memo summarizes new deals since last run).

---

## Error handling

- Per-item isolation. On extraction or API failure, leave the file in `inbox/` so the next
  run retries. Log error with full context.
- Claude API: 90s timeout (per CLAUDE.md). On 529 / overloaded → exponential backoff up to
  3 attempts, then leave for next run.
- Doc write failure: do NOT mark processed. Next run will redo the memo (idempotency on
  the Doc side handled by writer-level dedup checking title/URL).

## Run summary template

```
=== research-process SUMMARY ===
Vendor:  <name>
Tenant:  <short>
Items in inbox at start: N
Processed OK:  N
Failed:        N
Doc updated:   <doc title>  (<doc id>)
Memo model:    <model>
```
