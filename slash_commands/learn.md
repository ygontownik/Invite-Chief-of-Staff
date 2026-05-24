---
name: learn
description: On-demand learning consolidation — the Dreaming equivalent for this local pipeline. Processes today's proposed-learnings.jsonl queue through Sonnet (structuring) + Opus (semantic dedup), auto-applies high-confidence rules to LEARNINGS-LEDGER.yaml, and propagates via sync_learnings.py. Use after a session where new patterns were established but you don't want to wait for the 2am nightly scan.
---

# /learn — On-demand learning consolidation

Fires `nightly_learning_scan.py --apply` synchronously. The proposed-learnings
queue accumulated by the stop hook throughout the day gets processed through
two LLM passes and high-confidence entries land in the ledger immediately.

## When to use

- You've had a session with clear new rules and want them in the ledger now,
  not waiting for 2am.
- Before `/wrap` — ensures any rules from this session are in the ledger before
  the wrap snapshot runs.
- After a `/propose-learning` that identified patterns you want to batch-confirm
  against existing rules.
- Any time you say "going forward, X" or "always Y" and want it captured.

## What it does

```bash
/opt/homebrew/bin/python3 ~/dashboards/routines/nightly_learning_scan.py --apply --verbose
```

Two-pass LLM processing per candidate in `proposed-learnings.jsonl`:
- **Pass A (Sonnet)** — Structures raw snippet → LEARNINGS-LEDGER schema. Filters
  non-rules (prose, code comments, descriptions of external tech we don't run).
- **Pass B (Opus)** — Semantic dedup check vs all active ledger entries. Scores
  add_confidence 0.0–1.0; threshold ≥ 0.85 auto-applies.

Followed by `sync_learnings.py --apply` to propagate → CLAUDE.md + MEMORY.md.

## What it does NOT do

- It does not replace `/propose-learning` for manual single-rule capture.
- It does not touch the queue entries below the 0.85 threshold — those stay
  for review (visible at `~/dashboards/data/compiled/proposed-learnings.jsonl`).
- It does not push to Drive (use `/propose-learning` with `--push-drive` for that).

## Cost

$0 marginal (routes via `_claude_dispatch` → Claude Max subscription).
~2–4 min for a typical 3–5 item queue (Sonnet + Opus per candidate).

## How to run

```bash
/opt/homebrew/bin/python3 ~/dashboards/routines/nightly_learning_scan.py --apply --verbose
```

Or dry-run first to preview decisions:

```bash
/opt/homebrew/bin/python3 ~/dashboards/routines/nightly_learning_scan.py --verbose
```

## After /learn fires

New learning IDs are logged. Run `/propose-learning` to assign rule_code values
to any auto-applied entries that deserve promotion to CLAUDE.md (they're added
to the ledger with `_auto_applied: true` and `_review_note` as a flag).

Check queue residuals:

```bash
cat ~/dashboards/data/compiled/proposed-learnings.jsonl
```

## Nightly cadence

The same script runs automatically at 2:00am daily via LaunchAgent
`com.cospipeline.tomac.nightly-learning-scan`. Use `/learn` only when you want
consolidation NOW rather than waiting for the overnight cycle.

## Architecture note

This is the local equivalent of Anthropic's "Dreaming" API (Managed Agents
research preview). Dreaming runs cloud-side on hosted session transcripts;
this runs locally on the same proposed-learnings.jsonl queue accumulated by
the Claude Code stop hook throughout the day. Same consolidation loop,
architecture-appropriate implementation.
