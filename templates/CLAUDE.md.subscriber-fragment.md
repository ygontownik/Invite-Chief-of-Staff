# --- COS pipeline rules (auto-appended by setup.sh) ---
# Don't edit between markers; setup.sh re-applies on update.
# <COS-PIPELINE-RULES-START>

## System-map preflight (Rule SM1) — conditional

**When proposing new architecture, new pipelines, or changes that touch more than 2 files in `~/cos-pipeline/`, `~/cos-pipeline-config-*/`, or your data dir (`~/dashboards/` or `$COS_DATA_DIR`), read `~/dashboards/docs/SYSTEM-MAP.md` first** (or `$COS_DATA_DIR/docs/SYSTEM-MAP.md`).

For routine work — running existing scripts, single-file edits, config tweaks, debugging a known component — skip the preflight. The map is ~14KB; loading it on every CC session is wasteful.

When the preflight applies (architectural / multi-file / new-pipeline work):
1. Read `SYSTEM-MAP.md` (full file).
2. Read `~/cos-pipeline/docs/CLAUDE.md` if relevant.
3. Trace the data path for the artifact in question through the existing writers/readers shown in the map.
4. THEN propose. Only propose new pipelines after confirming no existing one already does the work.

## Deal-intel emission (Rule DI1)

**When discussing any registered deal in any session — Claude Code OR claude.ai project — emit `---DEAL-INTEL---` blocks throughout the session whenever a non-trivial new fact, decision, counterparty intel, or action item lands.**

Registered deals are listed in `~/cos-pipeline/tools/deal-system-data.json`. Emit one block per discrete intel item. The Stop hook scans transcripts for these blocks and routes them to the correct deal's `log.json` automatically. `/deal-sync` then folds them into the deal's status + master brief on the next cycle.

Block format (canonical):

```
---DEAL-INTEL---
deal: <deal_id>
date: YYYY-MM-DD
title: <one-line>
summary: <1-2 sentences>
facts:
  - <fact, with numbers + named entities>
counterparties:
  - <name (firm)> -- <new info>
actions:
  - <date>: <verb-first action> [@owner]
---END-DEAL-INTEL---
```

Don't emit empty blocks or trivial mentions. Multiple blocks per session are fine (one per discrete topic).

# <COS-PIPELINE-RULES-END>
