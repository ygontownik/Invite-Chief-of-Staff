---
description: One-command system health snapshot — coordination state + system_health.py + reference integrity. "Is everything OK?"
---

# /check-system — system health snapshot

Run the three canonical diagnostic commands in sequence and present a single
status summary. Use when you want to know if the system is healthy without
chasing 3 separate commands.

## STEP 1 — Coordination state

```bash
python3 ~/cos-pipeline/tools/coordination.py status
```

Read the output. Capture:
- Any process currently running (none expected at idle)
- Any stale locks (none expected; if any, surface them)
- Last successful run timestamps for key scripts (sync_registry, sync_learnings, sync_system_docs, log_compaction, etc.)

## STEP 2 — System health

```bash
python3 ~/cos-pipeline/tools/system_health.py
```

Read the output (`system_health: PASS|FAIL · N checks · X warns · Y fails`).
If FAIL or WARN, read the JSON detail:

```bash
python3 -c "
import json
r = json.load(open('/Users/ygontownik/dashboards/data/system-health/latest.json'))
print(f'Overall: {r[\"overall\"]}, counts: {r[\"counts\"]}')
print()
print('Failed checks:')
for c in r['checks']:
    if c['status'] == 'fail':
        print(f'  ✗ {c[\"name\"]}: {c.get(\"summary\",\"?\")}')
print()
print('Warnings:')
for c in r['checks']:
    if c['status'] == 'warn':
        print(f'  ⚠ {c[\"name\"]}: {c.get(\"summary\",\"?\")}')
"
```

## STEP 3 — Reference integrity (if not run recently)

If the last system_health output is older than 24h, run:

```bash
python3 ~/cos-pipeline/tools/reference_integrity_audit.py --dry-run 2>&1 | tail -20
```

This validates that every Drive ID in drive-docs.yaml resolves, and that
claude.ai project instructions reference IDs that match the registry.

## STEP 4 — Synthesize a single status line

Present back to Yoni:

```
SYSTEM CHECK — <ISO timestamp>

  ✅ N passes / ⚠ M warns / ✗ K fails
  Last sync_registry:    <time>
  Last sync_learnings:   <time>
  Last sync_system_docs: <time>

  Knowns (always present, not blockers):
    - launchagents: <known reason if relevant>
    - past-due deal actions: <count> (workstream signal)

  Action required (if any):
    - <specific fail with file/line>
```

If all green, just: "All clean. Last full sync N hours ago."

If anything actionable, flag it concisely and offer to investigate.

## OUTSTANDING REQUESTS
(per universal rule OR1 — list any flagged items at end of session)
