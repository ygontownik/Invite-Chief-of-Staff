# Track D (F-now) — per-user preferences — SUMMARY

Completed by Phase 2 sub-agent, run 2 (2026-05-03). Persisted by parent.

## Files written

| Path | Status |
|------|--------|
| `~/cos-pipeline/next/track-D/preferences.schema.json` | created |
| `~/cos-pipeline/next/track-D/example-preferences.json` | created |
| `~/cos-pipeline/users_migrate.py.next` | created |
| `~/cos-pipeline/next/track-D/server-data-filter.delta.md` | created |
| `~/cos-pipeline/tests/test_users_migrate.py` | created |

All test suite green (6/6). No live runtime files modified. Migration script dry-run succeeds against real data.

## Verification

- `python3 ~/cos-pipeline/tests/test_users_migrate.py` → `Ran 6 tests in 0.005s OK`
- `python3 ~/cos-pipeline/users_migrate.py.next -v` → `would_write=1` for `ygontownik@gmail.com` (84 hidden items derived from existing `_dismissedFollowUps` + `_dismissedEmailIds`)

## Key findings

1. **Live `dashboard-data.json` does NOT contain `_pinnedItems`/`_hiddenItems`** — keys reserved by F-now for future use. Migrator extracts when present, otherwise empty defaults. What IS present today: `_dismissedFollowUps` (81), `_dismissedEmailIds` (3), `_stageOverrides`, `_docCache`, `_sectionTimestamps`. First two routed to owner's prefs.
2. **Live `users.json` has only one user** (`ygontownik@gmail.com`). Mark Saxe's record comes later (Track G onboarding wizard). Mark filtering test uses synthetic data.
3. **`/data` endpoint at `cos-dashboard-server.py:2312` does not currently take a `user` arg.** Patch threads `user` from `do_GET` (line 1700) into `_handle_data(user=user)` at call site (line 1767). Owner short-circuits to identity.
4. **`hiddenItems` uses prefixed IDs** (`followUp:xxx`, `emailQueue:xxx`, `transcript:xxx`) — heterogeneous types in one flat list.

## Decisions logged

- Theme enum restricted to `paper | cream | high-contrast` per global "no dark backgrounds" memory rule.
- Hard policy strip (`recruiting`, `personalActions`, `briefingLog`) runs independently of `tilesVisible` so misconfigured prefs cannot leak owner-only sections.
- Idempotency via field-equality (preserves prior `lastUpdated`) → zero-diff re-runs.
- Feature-flagged rollout via env var `PER_USER_FILTER_ENABLED` (default off) — patch is byte-identical to today's behavior until flipped.

## Gates for morning review

- **ACTION-001 (High):** Apply 3 hunks in `server-data-filter.delta.md` → `cos-dashboard-server.py.next`, then `python3 -m py_compile`.
- **ACTION-002 (High):** Run `users_migrate.py.next --apply -v` after reviewing dry-run output.
- **ACTION-003 (Low):** Decide whether tile-id → data-key map should live in `dashboard-tiles.yaml` rather than hardcoded in the filter.
