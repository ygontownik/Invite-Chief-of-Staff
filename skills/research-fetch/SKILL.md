---
name: research-fetch
description: Generic vendor-parameterized research fetcher. Replaces (after 1-week soak) the four legacy fetch SKILLs (gs-research-daily-download, jefferies-pdf-downloader, rbn-daily-sync, peakload-weekly-sync).
---

# research-fetch

Single SKILL parameterized by `--vendor=<name>`. Loads vendor-specific configuration from
`~/cos-pipeline-config-<tenant>/research-vendors/<vendor>.yaml` and executes a generic
discover → download → stage → process → mark-processed flow.

This SKILL replaces, after the 1-week soak window defined in PLAN v3.1 H5, the four legacy
per-vendor fetch SKILLs at `~/dashboards/scheduled-tasks/`:

- `gs-research-daily-download`
- `jefferies-pdf-downloader`
- `rbn-daily-sync`
- `peakload-weekly-sync`

The legacy SKILLs MUST NOT be deleted before the soak window passes (PLAN H5 hard rule).

---

## Inputs (CLI flags)

```
--vendor=<name>          Required. One of: gs | jefferies | rbn | peakload (or any vendor
                         YAML present at ~/cos-pipeline-config-<tenant>/research-vendors/).
--tenant=<short>         Optional. Defaults to the active tenant (tomac).
--force                  Optional. Re-process items already in dedup tracker.
--list                   Optional. Show what WOULD be fetched, take no action.
--backfill[=N]           Optional. First-run pull historical N-day window (default 7).
```

Re-dev tenant gets zero research vendors by default (PLAN H4); the directory
`~/cos-pipeline-config-re-dev/research-vendors/` is intentionally empty.

---

## Generic flow

1. **Resolve config.** Read `~/cos-pipeline-config-<tenant>/research-vendors/<vendor>.yaml`.
   If `enabled: false`, log and exit 0. If file missing, exit 2 with a clear error.

2. **Discover new items** using `fetch.method`:

   - `gmail-search` — search Gmail with `fetch.source` query (e.g. GS alert emails),
     extract trustedLinks/PDF URLs from message bodies. Pull credentials from Keychain
     under `fetch.credentials_keychain_key` if set.
   - `imap` — connect to the IMAP folder at `fetch.source`.
   - `rss` — fetch the RSS feed at `fetch.source`.
   - `playwright-scrape` — headed/headless browse to the URL at `fetch.source`,
     authenticated via stored auth state if `fetch.credentials_keychain_key` is present.

3. **Dedup against the tracker** at `dedup_tracker` (default
   `~/credentials/processed_research_<vendor>.json`). Each entry is keyed by a stable
   per-vendor unique ID (UUID, message-id, GUID, file hash). Skip items whose ID is
   already present unless `--force`.

4. **Download** each new item to the per-vendor inbox:

   ```
   ~/cos-pipeline/data-<tenant>/research/<vendor>/inbox/
   ```

   Filename: sanitized title + native extension (.pdf, .html, .csv) — never the raw UUID.
   Per-item error isolation: log + continue on failure (PLAN coding default).

5. **Invoke the process step.** Call the `research-process` SKILL with
   `--vendor=<vendor> --tenant=<tenant>`. The process SKILL pops every file out of the
   inbox, extracts text, drafts a six-section memo, and appends to the master Doc.

6. **Mark processed.** After the process step succeeds for an item, append its unique ID
   plus a UTC timestamp to the dedup tracker. On process failure, do NOT mark processed
   — the next run will retry.

7. **Summarize.** Print one-line summary: `<vendor>: N new | N processed | N failed | N skipped`.

---

## Vendor invocation examples

### GS Marquee (daily, gmail-search)
```bash
research-fetch --vendor=gs
```
Reads `gs.yaml`. Searches Gmail for `from:gs-portal-emails@alerts.publishing.gs.com newer_than:7d`,
warms the elevated session via a trustedLink, scans the My Content DOM and today's alert emails,
filters out non-energy/telecom/Europe/Japan/China reports, downloads PDFs via `browser_cookie3`
+ `requests`, stages into `data-tomac/research/gs/inbox/`, then invokes the processor.
Dedup tracker: `~/credentials/processed_research_gs.json`.

### Jefferies (weekly, playwright-scrape)
```bash
research-fetch --vendor=jefferies
```
Reads `jefferies.yaml`. Loads saved auth at `~/credentials/jef_auth.json`, queries the Jefferies
research portal for analysts Dumoulin-Smith / Zimbardo / Ailani, downloads new PDFs into
`data-tomac/research/jefferies/inbox/`. On `Session may have expired` → email alert and exit.
Dedup tracker: `~/credentials/processed_research_jefferies.json`.

### RBN Energy (daily, playwright-scrape — public RSS surface)
```bash
research-fetch --vendor=rbn
```
Reads `rbn.yaml`. Navigates `https://rbnenergy.com`, finds the most recent free article,
extracts text + inline images, saves as HTML to `data-tomac/research/rbn/inbox/`, then
invokes the processor (which converts HTML to memo and appends to the RBN Daily Archive Doc).
Dedup tracker: `~/credentials/processed_research_rbn.json`.

### PeakLoad M&A (weekly, playwright-scrape via Next.js server action)
```bash
research-fetch --vendor=peakload
```
Reads `peakload.yaml`. Logs into `peakload.com` with stored credentials, paginates the deals
API (100/page), writes a CSV to `data-tomac/research/peakload/inbox/`, then invokes the
processor (which uploads the CSV and appends a deals summary memo).
Dedup tracker: `~/credentials/processed_research_peakload.json`.

---

## Error handling

- Per-item isolation: one failure must never stop a batch.
- All credentials read from macOS Keychain under prefix `cos-pipeline-<tenant>` (per
  Decision C9). Never hardcode.
- If an entire vendor fails (no items found, auth expired, source down): log, send a single
  alert email to `principal.email` from `firm_context.yaml`, exit 1. Do not retry inside the
  SKILL — the next cron run handles it.

## Run summary template

```
=== research-fetch SUMMARY ===
Vendor:  <name>
Tenant:  <short>
Date:    YYYY-MM-DD
Source:  <fetch.method>:<fetch.source-summary>

Discovered:        N
Already processed: N
New (queued):      N
Downloaded:        N ok / N failed
Processed:         N ok / N failed (delegated to research-process)
Marked processed:  N
```

---

## Migration note

This SKILL is paper-only until Track H4 cron flip. See
`~/cos-pipeline/skills/research-fetch/CONSOLIDATION_PLAN.md` for the migration plan,
soak window, and rollback steps.
