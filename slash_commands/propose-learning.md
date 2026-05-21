---
description: Capture a new behavioral learning. Drafts a LEARNINGS-LEDGER.yaml entry interactively, then runs sync_learnings.py to propagate. Use whenever you realize "going forward, X" or "always Y" or "never Z".
---

# /propose-learning — capture a new behavioral rule

When you've just learned something durable about how the work should run —
a rule, a pattern, a preference, a hard-won lesson — invoke this to capture
it into the canonical [LEARNINGS-LEDGER.yaml](file:///Users/ygontownik/dashboards/docs/LEARNINGS-LEDGER.yaml)
without losing it to info overload.

## STEP 1 — Read the current ledger

```bash
python3 -c "
import yaml
d = yaml.safe_load(open('/Users/ygontownik/dashboards/docs/LEARNINGS-LEDGER.yaml'))
print(f'Current next_id: {d[\"meta\"][\"next_id\"]}')
print(f'Active learnings: {sum(1 for L in d[\"learnings\"] if L.get(\"status\",\"active\")==\"active\")}')
print()
print('Recent additions (last 5):')
for L in d['learnings'][-5:]:
    print(f'  {L[\"id\"]}  {L.get(\"rule_code\",\"—\"):4}  {L[\"title\"][:60]}')
"
```

## STEP 2 — Gather the learning

Ask Yoni for these inputs (or infer from session context if obvious):

1. **Title** — short, descriptive (e.g., "Edit-in-place for instruction-referenced files")
2. **Domain** — one of: `universal`, `dashboard`, `deal`, `cos_pipeline`, `drive`, `financial_modeling`, `personal`, `meta`
3. **Rule text** — the actual behavioral instruction. Be specific. Multi-line if needed.
4. **Applies to** — context tags (e.g., `[pipeline_code, new_features]`)
5. **Confidence** — `high` (confirmed pattern) | `medium` (single strong observation) | `low` (hypothesis)
6. **Rule code (optional)** — assign if it's a universal rule worth promoting to `~/.claude/CLAUDE.md` (e.g., AB1, CC1, EP1). Use existing convention.
7. **Enforced by (optional)** — mechanism (e.g., "pre-commit lint", "Drive Organizer Phase 5", "code review")
8. **Source** — session ID or descriptive context

If the user just describes the learning without structure, infer + present
back: "I'll draft this as L00XX, domain X, confidence X. Confirm or correct."

## STEP 3 — Draft the YAML block

```yaml
  - id: L00XX                  # use meta.next_id
    rule_code: <CODE>          # optional
    title: <title>
    domain: <domain>
    confidence: <high|medium|low>
    learned: <YYYY-MM-DD>      # today
    source_file: session-<session-id-or-context>
    rule: |
      <multi-line rule text>
    applies_to: [<tags>]
    enforced_by: <mechanism>   # optional
    status: active
```

Show Yoni the draft, ask "OK to append?".

## STEP 4 — Append to ledger + bump next_id

If approved:

```bash
python3 << 'PYEOF'
import yaml
from pathlib import Path
P = Path("/Users/ygontownik/dashboards/docs/LEARNINGS-LEDGER.yaml")
docs = yaml.safe_load(P.read_text())
new_entry = {
    "id": "L00XX",  # the next_id value
    "rule_code": "...",  # if applicable; else delete this key
    "title": "...",
    "domain": "...",
    "confidence": "...",
    "learned": "YYYY-MM-DD",
    "source_file": "...",
    "rule": "...",
    "applies_to": [...],
    "status": "active",
}
docs["learnings"].append(new_entry)
# Bump next_id (e.g., L0040 -> L0041)
current = int(docs["meta"]["next_id"][1:])
docs["meta"]["next_id"] = f"L{current+1:04d}"
P.write_text(yaml.safe_dump(docs, sort_keys=False, default_flow_style=False, width=120))
print(f"Appended {new_entry['id']}; next_id now {docs['meta']['next_id']}")
PYEOF
```

## STEP 5 — Propagate

```bash
python3 ~/cos-pipeline/tools/sync_learnings.py --apply --push-drive
```

This regenerates:
- `~/.claude/CLAUDE.md` (UNIVERSAL RULES section between sentinel markers, if rule-coded)
- `LEARNINGS-INDEX.md`
- `MEMORY.md`
- Yoni Personal Context + Practice Patterns gdocs (via Deal Sync Writer setContent)

claude.ai project instructions auto-pick up on the next 24h dash-state-hook
cycle. To force immediate, add `--push-now`.

## STEP 6 — Report

Tell Yoni:
- New entry: L00XX + title
- Domain
- Where it now lives (CLAUDE.md regenerated section + Drive gdocs)
- When claude.ai projects will pick it up (within 24h, or now if --push-now)

## OUTSTANDING REQUESTS
(per OR1)
