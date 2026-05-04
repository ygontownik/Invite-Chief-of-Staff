# Track A (PLAN B1–B7) — ID excision .next files — SUMMARY

Completed by Phase 2 sub-agent, run 2 (2026-05-03). Persisted by parent.

## Files created (all NEW, under `~/cos-pipeline/`)

| File | Lines | Replaces |
|---|---:|---|
| `cos_personal_briefing.py.next` | 367 | **B1**: 5 hardcoded DOC_* IDs (61–65) → `_fc.load_drive_docs()` + fail-loud `_require_doc()`; system prompt rebuilt from firm_context |
| `cos_gmail_mini_v2.py.next` | 1151 | **B2**: DEFAULT_CONFIG.docs (4 IDs) + research_senders (5 IDs) emptied; `load_config()` rewritten to source from firm_config.json with drive-docs.yaml fallback |
| `cos_capture_pipeline.py.next` | 615 | **B3**: `_LOG_DIR` per-tenant via `_resolve_log_dir()` (tenant_slug / firm.short_name; default `tomac`) — replaces `~/tomac-cove-pipeline/logs` |
| `_entity_normalizer.py.next` | 244 | **B4**: VAGUE_PATTERNS first regex `(yoni|mark|nik)` → built from `owner_whitelist + team[].name + principal.name` |
| `podcast_transcribe.py.next` | 1348 | **B5**: 5-entry hardcoded RSS fallback → stderr WARNING + `{}` |
| `setup_keychain.sh.next` | 146 | **B6**: `SERVICE_PREFIX` resolves from firm_config.json :: keychain_service_prefix |
| `setup_launchagents.sh.next` | 202 | **B6**: 4-path search now includes `~/cos-pipeline-config-tomac/` |
| `MIGRATION_B7.md` | 117 | **B7**: paper migration plan + rollback for the data symlink |
| `BIDX_DIFF.md` | 125 | line-by-line diff of each .next vs live |
| `tests/_track_b_helpers.py` | 119 | shared tenant config builder |
| `tests/test_cos_personal_briefing.py` | 39 | B1 |
| `tests/test_cos_gmail_mini_v2.py` | 41 | B2 |
| `tests/test_cos_capture_pipeline.py` | 34 | B3 |
| `tests/test__entity_normalizer.py` | 42 | B4 |
| `tests/test_podcast_transcribe.py` | 37 | B5 |
| `tests/test_setup_keychain.py` | 65 | B6 |

## Test results

All 7 unit test files pass: 7 OK total. Run with `python3 ~/cos-pipeline/tests/test_<name>.py`.

## Decisions appended to DECISIONS.md (Run 2 judgment log)

1. Lazy `_fc.load_firm_context()` inside helpers, not at module import — preserves `--help` UX.
2. **B1**: doc IDs sourced via `_fc.load_drive_docs()` (drive-docs.yaml) rather than `firm_context.yaml :: google_docs`. **Harmonization vs C8** (which declares google_docs canonical) — recommend morning task to populate google_docs and migrate `_fc` to read from there with drive-docs.yaml fallback.
3. **B2**: kept `deal_keywords`/`recruit_keywords` lists — out of scope for B2; belongs to PLAN E1.3.
4. **B3**: default tenant slug `tomac` for backwards compatibility.
5. **B4**: generic `\w+` regex fallback when firm_context absent — preserves heuristic.
6. **B6**: `setup_launchagents.sh.next` prepends `~/cos-pipeline-config-tomac/firm_config.json` to candidate paths.

## Contradictions

None. PLAN B1–B7 line ranges all matched live code as cited.

## Deferrals / blockers

- **B2 partial**: tenant-specific keyword lists belong to PLAN E1.3.
- **B4 verification**: PLAN B4 calls out a possible `~/dashboards/routines/process/_entity_normalizer.py` copy. Spot-check left for morning.
- **B7 not actuated**: paper migration only per HARD RULES.
- **drive-docs.yaml location**: lives at `~/dashboards/config/drive-docs.yaml`; proper home is `~/cos-pipeline-config-<slug>/drive-docs.yaml` (move = morning task).
- **C8 harmonization** (`google_docs` vs `drive-docs.yaml`): see decision #2.

## Privacy sweep

`grep -nEi 'GPEcictr24200|saxe\.mark@gmail|msaxe@gmail|03kLgctRftjCUmrM'` over all 7 `.next` files: clean (zero hits).

## Recommended morning merge order

1. **B6** (lowest blast radius)
2. **B4** (pure regex; cheap revert)
3. **B3** (legacy `~/tomac-cove-pipeline/logs` symlink ahead of swap)
4. **B5** (verify `firm_config.json :: podcast_feeds` populated)
5. **B1** (verify drive-docs.yaml has all 5 keys)
6. **B2** (verify `firm_config.json :: docs` has all 4 keys)
7. **B7** (data-symlink migration; ~30s downtime; runbook in MIGRATION_B7.md)
