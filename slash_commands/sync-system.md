---
description: One command to flush all canonical-source edits to derived views — runs sync_registry + sync_learnings + sync_system_docs. Use after editing drive-docs.yaml, LEARNINGS-LEDGER.yaml, or any system doc.
---

# /sync-system — flush all canonical edits

When you've edited drive-docs.yaml, LEARNINGS-LEDGER.yaml, or any of the
narrative system docs (README/SYSTEM-REFERENCE/USER-MANUAL/SKILLS-CATALOG),
run this to push everything downstream in one shot.

## STEP 1 — Parse args

`$ARGUMENTS` may be empty (full sync) or one of:
- `dry-run` — show what WOULD change, no writes
- `registry` — only sync_registry.py (drive-docs.yaml → GAS + local_file_router)
- `learnings` — only sync_learnings.py (LEARNINGS-LEDGER → CLAUDE.md + Drive)
- `docs` — only sync_system_docs.py (markdown → Drive gdocs)

Default with no args: run all three with --apply (plus --push for GAS, --push-drive for learnings).

## STEP 2 — Pre-check

```bash
python3 ~/cos-pipeline/tools/coordination.py status
```

Confirm no stale locks. If any, suggest `clear-stale` first.

## STEP 3 — Run the syncs (in order — registry first, then ledger, then docs)

### 3a — drive-docs.yaml → GAS + local_file_router

```bash
python3 ~/cos-pipeline/tools/sync_registry.py --apply --push
```

Pushes regenerated `tc_config.gs/getDeals()` + `drive_organizer.gs/DEAL_FOLDERS`
via `clasp push`. Reads `local_file_router.py` DEALS dict fresh on next daemon
cycle (no clasp needed — pure Python).

### 3b — LEARNINGS-LEDGER → CLAUDE.md + Drive gdocs

```bash
python3 ~/cos-pipeline/tools/sync_learnings.py --apply --push-drive
```

Regenerates the `## UNIVERSAL RULES` section in `~/.claude/CLAUDE.md` between
sentinel markers. Pushes Yoni Personal Context + Practice Patterns gdocs via
Deal Sync Writer (edit-in-place, I11).

### 3c — Narrative system docs → Drive gdocs

```bash
python3 ~/cos-pipeline/tools/sync_system_docs.py --apply
```

Pushes any markdown file modified since last sync to its registered gdoc
(via mirror_path in drive-docs.yaml reference_docs section).

## STEP 4 — Verify

```bash
python3 ~/cos-pipeline/tools/coordination.py status
```

Confirm new `last_run` entries for sync_registry, sync_learnings,
sync_system_docs. All present = success.

Then run `/check-system` to confirm no regressions.

## STEP 5 — Optional: kick claude.ai project instructions

If learnings changed and you want immediate propagation to deal projects
(rather than waiting for the 24h dash-state-hook cycle):

```bash
python3 ~/cos-pipeline/tools/sync_learnings.py --apply --push-now
```

This invalidates the project-instructions sync state file so the next
dash-state-hook fire pushes Chrome MCP updates to all 6+ deal projects.

## OUTSTANDING REQUESTS
(per OR1)
