# research-fetch / research-process — Consolidation Plan

**Track:** PLAN v3.1 Track H
**Status:** Paper-only as of 2026-05-02. No crons flipped, no SKILLs deleted, no LaunchAgents modified.
**Scope:** Collapse 6 legacy SKILLs (4 fetch + 2 process) → 2 vendor-parameterized SKILLs.

---

## Mapping (legacy → consolidated)

| Legacy SKILL | New invocation |
|---|---|
| `gs-research-daily-download` | `research-fetch --vendor=gs` |
| `jefferies-pdf-downloader`   | `research-fetch --vendor=jefferies` |
| `rbn-daily-sync`             | `research-fetch --vendor=rbn` |
| `peakload-weekly-sync`       | `research-fetch --vendor=peakload` |
| `gs-research-pdf-processor`  | `research-process --vendor=gs` *(invoked by fetch)* |
| `jefferies-pdf-processor`    | `research-process --vendor=jefferies` *(invoked by fetch)* |

The two RBN/PeakLoad legacy SKILLs already do their own write step inline; the new
`research-process --vendor=rbn|peakload` lifts that logic out of the legacy script and
runs it via the generic processor.

---

## Migration phases

### Phase 0 — Paper (NOW, 2026-05-02)
- [x] New SKILLs written under `~/cos-pipeline/skills/`
- [x] Vendor YAMLs written under `~/cos-pipeline-config-tomac/research-vendors/`
- [x] `~/cos-pipeline-config-re-dev/research-vendors/` left empty (PLAN H4 — re-dev gets zero research vendors by default)
- [ ] No cron edits, no LaunchAgent edits, no deletions

### Phase 1 — Shadow (day +1 to day +2)
- [ ] Manually invoke `research-fetch --vendor=gs --list` against today's GS alert mailbox; diff results against legacy SKILL's discovery output. Repeat for jefferies, rbn, peakload.
- [ ] Verify dedup trackers under `~/credentials/processed_research_<vendor>.json` populate without colliding with legacy trackers (`gs_processed_files.json`, `processed_files.json`, `jef_downloaded.json`).
- [ ] Verify staging directory `~/cos-pipeline/data-tomac/research/<vendor>/inbox/` exists and is writable.

### Phase 2 — Parallel run (day +3 to day +9, the 1-week soak)
- [ ] Add a SECOND cron entry for each vendor that runs the new SKILL 30 min after the legacy SKILL. Both write to separate inboxes and separate trackers.
- [ ] Compare outputs daily: same N items discovered? Same memo quality? Same Doc append behavior?
- [ ] Track failures in a soak log at `~/cos-pipeline/logs-tomac/research-soak.log`.

### Phase 3 — Cutover (day +10, only after 7 clean days)
- [ ] Disable the legacy SKILL's LaunchAgent: `launchctl unload ~/Library/LaunchAgents/com.yoni.claude-task.<legacy>.plist` (per vendor). Do NOT delete the plist file yet.
- [ ] Promote the parallel cron to the primary slot.
- [ ] Verify one full successful run per vendor on the new SKILL alone.

### Phase 4 — Cleanup (day +14, only after 7 clean cutover days)
- [ ] Delete legacy SKILL directories at `~/dashboards/scheduled-tasks/{gs-research-daily-download,gs-research-pdf-processor,jefferies-pdf-downloader,jefferies-pdf-processor,rbn-daily-sync,peakload-weekly-sync}/`.
- [ ] Remove legacy LaunchAgent plists.
- [ ] Update `~/cos-pipeline/routines.yaml` to reflect the consolidated SKILL inventory (drop the 6 legacy `rename_to: research-fetch-*` rows; add `research-fetch` and `research-process`).

This matches PLAN v3.1 H5: "Delete old SKILLs after 1 week of clean parallel runs."

---

## Cron schedule mapping (planned, not yet applied)

| Vendor    | Legacy schedule          | New schedule (identical) |
|-----------|--------------------------|--------------------------|
| gs        | Daily ~06:30 ET          | Daily 06:30 ET — `research-fetch --vendor=gs` |
| jefferies | Weekly (verify day/time) | Same — `research-fetch --vendor=jefferies` |
| rbn       | Daily weekday morning    | Same — `research-fetch --vendor=rbn` |
| peakload  | Sunday 14:00 ET          | Same — `research-fetch --vendor=peakload` |

`research-process` is invoked transitively by `research-fetch` and does NOT get its own cron.

---

## Re-dev tenant (PLAN H4)

`~/cos-pipeline-config-re-dev/research-vendors/` is intentionally empty. Re-dev (real estate
developer guinea pig) gets zero research vendors by default — neither GS nor Jefferies nor
RBN nor PeakLoad is relevant to that domain. If a real-estate-relevant research vendor
becomes available later, add a YAML there; the SKILL needs no changes.

---

## Rollback steps

If Phase 2 or Phase 3 surfaces a regression:

1. **Phase 2 rollback (no production impact):** Just stop running the parallel cron. Legacy SKILL keeps running. Investigate and re-attempt parallel run.

2. **Phase 3 rollback (post-cutover):**
   - Re-load legacy LaunchAgents: `launchctl load ~/Library/LaunchAgents/com.yoni.claude-task.<legacy>.plist`
   - Disable new SKILL cron entries.
   - Restore the legacy dedup trackers from the snapshot taken in PLAN A-fast.1
     (`~/cos-pipeline-backup-20260502.tar.gz`).
   - Open a CONTRADICTION_FOUND.md entry, halt Track H, debug.

3. **Phase 4 rollback (post-cleanup):** Restore legacy SKILL directories from the backup tarball; restore plists; reload LaunchAgents. The 1-week soak after cutover is the safety margin that makes this phase recoverable.

---

## Risks and watch-items

- **Tracker collision.** New trackers MUST NOT share a path with legacy trackers (`gs_processed_files.json`, `processed_files.json`, `jef_downloaded.json`). New scheme: `processed_research_<vendor>.json`. Verified in vendor YAMLs.
- **Drive folder collision.** Legacy SKILLs upload PDFs to specific Drive folders (`1jzoqxREz_KYV6aIfpL8ZGnUAxS7arjDg` for GS, `1ITQg3-Xer26WjsEI5ifd-GD96WsVWzGc` for Jefferies). The new fetch SKILL stages locally first; uploads to Drive happen in the process step (or are skipped — the master Doc is the canonical surface). Confirm in Phase 1 whether Drive uploads stay in flow or are dropped.
- **GS sector classification.** Legacy `gs-research-pdf-processor` routes to one of 6 sector docs via a Gemini classification call. New `research-process` must replicate this routing — `gs.yaml :: process.sector_doc_keys` lists the candidates; the prompt classifies and the writer resolves the sector key against `firm_context.yaml :: google_docs.<key>`.
- **GS auth expiry.** Elevated session depends on a fresh GS alert email arriving every ~3 days. Manual recovery path unchanged from legacy.
- **Jefferies auth expiry.** Manual `--setup` flow on the legacy downloader still required; new SKILL emits the same alert and exits.
- **Apps Script writer (`~/write_to_doc.py`).** Legacy `rbn-daily-sync` shells out to this. The new processor must either keep using it or reimplement HTML→Doc insertion natively. Decide in Phase 1.

---

## Open questions

1. Should the new processor keep uploading PDFs to the legacy Drive staging folders, or is the master Doc the only output? (Affects the `drive_staging_folder_id` field in vendor YAMLs.)
2. Should `research-process` use the same Gemini-based extraction the legacy processors use, or move to Claude (per CLAUDE.md per-pass model assignments)? Default in YAMLs is Claude `claude-sonnet-4-6`; verify quality in Phase 1 against legacy Gemini outputs.
3. Does PeakLoad need a memo at all, or just CSV upload? Legacy SKILL only uploads the CSV; the memo summary is a new behavior introduced here. Decide before Phase 3.

Resolve all three in Phase 1 shadow. None block Phase 0 paper delivery.
