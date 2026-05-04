# Track C (E1) — cos-dashboard-fetch tomac→deals — SUMMARY

Completed by Phase 2 sub-agent, run 2 (2026-05-03). Persisted by parent.

## Files produced (all NEW, zero live edits)

- `~/cos-pipeline/next/track-C/cos-dashboard-fetch.py.next` (290 lines, delta-instructions, 10 patch chunks)
- `~/cos-pipeline/next/track-C/server-routes.delta.md` (174 lines, 8 surgical patches with REPLACE/WITH + line numbers)
- `~/cos-pipeline/next/track-C/cos-dashboard-fetch.diff.md` (247 lines, 12 annotated REPLACE/WITH chunks)
- `~/cos-pipeline/tests/test_cos_dashboard_fetch_next.py` (285 lines, 22 tests — **all green** in 0.004s)
- 7 entries appended to `DECISIONS.md` Run 2 judgment log

## Key findings / judgment calls

1. **Brief discrepancy resolved (not a contradiction):** brief asks for `_TOMAC_CONFIG_PATH` and `window.__TOMAC_CONFIG__` rename in `cos-dashboard-fetch.py.next`. Verified via grep — both symbols live exclusively in `cos-dashboard-server.py` (lines 380, 926). Patches placed in `server-routes.delta.md`.
2. **Delta-instructions format chosen** for fetch.py.next (not full rewrite): the literal `'tomac'` string overloads 4 distinct concepts across 40 sites. Per-site annotated patches prevent silent cross-contamination.
3. **`/tomac` → `/deals/` 301 retarget** (was `/tomac-cove/`) per PLAN E1.6. **User-visible URL change — needs principal approval before merge.** Flagged medium-risk.
4. **Back-compat aliases** for 1 release written for 8 identifiers, each tagged `# remove next release`.
5. **`_OWNER_REJECT`** retains hardcoded generic first-name list — these are cross-tenant common-noise filters, while tenant-identity names now come from `firm_context.yaml :: principal.name + team[].name + owner_whitelist`.

## Deferrals (need human review)

1. Apply order: fold into existing `cos-dashboard-server.py.next` (Phase 1.7) vs. apply as sequenced second pass.
2. `/tomac` redirect change — user-visible bookmark impact.
3. React bundle in `~/dashboards/app/templates/` still reads `window.__TOMAC_CONFIG__` — alias preserved; bundle rebuild is separate.
4. Downstream writers (capture pipeline, envelope writer) may still write `workstream: 'tomac'` — `_is_deal_ws()` accepts both during back-compat window; sweep recommended before alias removal.

## Verification commands

```bash
python3 ~/cos-pipeline/tests/test_cos_dashboard_fetch_next.py
ls -la ~/cos-pipeline/next/track-C/
stat -f "%m %N" ~/cos-pipeline/cos-dashboard-fetch.py ~/cos-pipeline/cos-dashboard-server.py
grep -E "GPEcictr24200|saxe\.mark@gmail|msaxe@gmail" ~/cos-pipeline/next/track-C/* ~/cos-pipeline/tests/test_cos_dashboard_fetch_next.py
```

## HARD RULES sweep (passed)

- No live `.py` modified
- No `~/dashboards/app/templates/*.html` touched
- No LaunchAgents, credentials, or live API calls
- No git push, no launchctl
