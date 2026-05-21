---
description: Re-run the TCIP workflow audit — coordination + system_health + reference integrity + delta-since-last-audit
argument-hint: "[--full | --quick]"
---

# /workflow-audit — re-run the TCIP/CoS workflow audit

Comprehensive health snapshot + delta vs. prior audit. The seed audit
(`WORKFLOW-AUDIT-2026-05-20.md`) is too big to regenerate from scratch each
invocation — this skill runs targeted health checks and produces a focused
delta report.

`$ARGUMENTS` controls scope:
- `--full` (default if empty): all 6 steps below
- `--quick`: snapshot only (skip step 2 prior-audit comparison; inline deltas)

## STEP 1 — Snapshot (run in parallel)

Run these four commands in parallel via four Bash tool calls in one message:

```bash
python3 ~/cos-pipeline/tools/coordination.py status
```

```bash
python3 ~/cos-pipeline/tools/system_health.py
```

```bash
python3 ~/cos-pipeline/tools/reference_integrity_audit.py
```

```bash
bash ~/dashboards/scripts/verify-system.sh | tail -20
```

Capture for each: PASS/FAIL/WARN counts, named failures, last-run timestamps
of key scripts, tenant-leak hits (hard vs allow-listed), Drive ID resolution
failures.

## STEP 2 — Compute deltas (skip if `--quick`)

Find the most recent prior audit:

```bash
ls -t ~/dashboards/docs/WORKFLOW-AUDIT-*.md | head -1
```

Read it. Extract its findings. Compare to today's snapshot. Build deltas in
these categories:

1. **Health checks that flipped** — was PASS, now FAIL/WARN; or recovered
2. **New stale coordination state** — locks older than 24h that weren't stale before
3. **Pipeline scripts last-run > 48h** that should run more often (podcast,
   deal-sync, capture-deal-chats, artifact-pull, briefing)
4. **Tenant-leak count** — must be 0 hard hits; allow-listed only OK
5. **GitHub commit counts since last audit** per repo:
   ```bash
   for repo in ~/cos-pipeline ~/dashboards ~/cos-pipeline-config-tomac; do
     echo "=== $repo ==="
     git -C "$repo" log --since="<prior-audit-date>" --oneline | wc -l
   done
   ```

## STEP 3 — Outstanding requests audit

Find the latest session handoff:

```bash
ls -t ~/dashboards/docs/SESSION-HANDOFF-*.md | head -1
```

Read it. Extract the outstanding items list. For each item, verify whether
it's still outstanding by checking:
- `git log --all --since="<handoff-date>" --grep="<keyword>"` per repo
- File presence (`ls` for any file the item said would be created)
- Recent commits touching the relevant area

Produce a "still open" vs "done" diff. For "done" items, cite the commit SHA
or file path as evidence.

## STEP 4 — Output

Write the report to:

```
~/dashboards/docs/WORKFLOW-AUDIT-<YYYY-MM-DD>.md
```

Use today's absolute date (Rule AB1 — no relative phrasing anywhere). Use
this structure:

```markdown
# Workflow Audit — <YYYY-MM-DD>

Mode: --full | --quick
Prior audit: <path to previous WORKFLOW-AUDIT-*.md>

## § Snapshot

Coordination     : <N processes / M stale locks>
system_health    : PASS|FAIL · <N> checks · <X> warns · <Y> fails
Reference audit  : <PASS|FAIL · resolution count>
verify-system    : <PASS|FAIL summary>

Key last-run timestamps:
- sync_registry         : <ISO ts>
- sync_learnings        : <ISO ts>
- sync_system_docs      : <ISO ts>
- deal-sync             : <ISO ts>
- capture-deal-chats    : <ISO ts>
- briefing              : <ISO ts>

## § Deltas since previous audit (<prior-date>)

| Category | Was | Now | Notes |
|---|---|---|---|
| <check name>  | PASS | FAIL | <file/line> |
| Tenant leaks  | 0    | 0    | allow-listed only |
| Commits: cos-pipeline | — | N | <one-line theme> |
| ...           |      |      |      |

## § Outstanding requests status

Source: <path to SESSION-HANDOFF used>

| Item | Status | Evidence |
|---|---|---|
| <verb-first item> | DONE | commit <sha> |
| <verb-first item> | OPEN | no commits / file absent |
| ...               |      |          |

## § Recommended next 3 actions

1. <verb-first one-liner>
2. <verb-first one-liner>
3. <verb-first one-liner>
```

## STEP 5 — Mirror to Drive (conditional)

Check `~/cos-pipeline-config-tomac/drive-docs.yaml` (or whichever tenant
config the active session uses — never hardcode) for a `workflow_audit` key:

```bash
grep -n "^workflow_audit:\|  workflow_audit:" ~/cos-pipeline-config-tomac/drive-docs.yaml
```

If present, push via Deal Sync Writer setContent (Rule EP1 — edit in place
on the registered fileId):

```bash
python3 ~/cos-pipeline/tools/deal_sync_writer.py \
  --doc-key workflow_audit \
  --content-file ~/dashboards/docs/WORKFLOW-AUDIT-<YYYY-MM-DD>.md
```

If NOT present, append a TODO line to the local file and skip the push:

```
> TODO: register `workflow_audit` key in drive-docs.yaml to enable Drive mirror.
```

Do NOT auto-create a gdoc ID. Yoni registers it manually.

## STEP 6 — OUTSTANDING REQUESTS

Per Rule OR1, append the OUTSTANDING REQUESTS block at the end of the
session response, listing each still-open item from the report's
"Outstanding requests status" table (verb-first, one line each). If
everything is closed, write "All requests in this session addressed."

## Notes

- All paths use `~/` or absolute paths — skill works regardless of cwd
- Multi-tenant safe — no hardcoded tenant slug; resolve config path from
  active session context (Rule PD1)
- Use today's ISO date everywhere (Rule AB1 — no "today", "yesterday")
