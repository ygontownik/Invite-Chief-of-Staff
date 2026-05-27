---
description: Scrape claude.ai project chats for ---DEAL-INTEL--- and ---NDA-LESSONS--- blocks and route them to the right handlers. Block-only — never full transcripts.
argument-hint: "[deal_id | nda | all]"
---

# /capture-deal-chats — auto-capture blocks from all claude.ai projects

For every registered claude.ai project (TCIP deals **and** the NDA Review
project), walk the chat list, open any chats with new messages since last
capture, scrape ONLY structured blocks, and route them:

- `---DEAL-INTEL---` → `intel_capture.py parse-stdin` → deal `log.json`
- `---NDA-LESSONS---` → `nda_log_processor.py parse-stdin` → NDA Reviewer doc

`/deal-sync` folds DEAL-INTEL into status + master brief on next cycle.
`nda_log_processor` merges NDA-LESSONS into §3/§7/§9 of the NDA Reviewer doc.

This is the claude.ai counterpart to the Stop hook's
`run_intel_capture_scan()` (which handles Claude Code transcripts).
Two surfaces, one block format, one target each.

---

## STEP 0 — Parse argument

`$ARGUMENTS` is `<deal_id>`, `nda`, or `all` (default: `all`).

---

## STEP 1 — Load registry + capture state

Builds a unified target list from ALL registered `project_url` sources:
`sync-state.json` (TCIP deals) and `drive-docs.yaml` (NDA project, etc.).

```bash
python3 - <<'EOF'
import json, os, sys
from pathlib import Path
import yaml

# --- TCIP deals from sync-state.json ---
ss_path = Path('/Users/ygontownik/cos-pipeline/tools/sync-state.json')
ss = json.loads(ss_path.read_text()) if ss_path.exists() else {}

# --- Non-deal projects from drive-docs.yaml ---
dy_path = Path.home() / 'cos-pipeline-config-tomac/drive-docs.yaml'
dy = yaml.safe_load(dy_path.read_text()) if dy_path.exists() else {}

state_path = Path.home() / 'dashboards/data/chat_capture_state.json'
state = json.loads(state_path.read_text()) if state_path.exists() else {}

DEAL = "$DEAL_ID_OR_ALL"

targets = []

if DEAL in ("all", "nda"):
    nda = dy.get("nda_review", {})
    nda_url = nda.get("project_instructions", {}).get("project_url", "")
    if nda_url:
        targets.append({
            "project_id": "nda",
            "url": nda_url,
            "block_types": ["NDA-LESSONS"],
            "last_capture": state.get("nda", {}).get("last_capture"),
            "captured_chat_ids": state.get("nda", {}).get("captured_chat_ids", []),
        })

if DEAL != "nda":
    for k, v in ss.items():
        if not v.get("project_url"):
            continue
        if DEAL not in ("all",) and DEAL != k:
            continue
        targets.append({
            "project_id": k,
            "url": v["project_url"],
            "block_types": ["DEAL-INTEL"],
            "last_capture": state.get(k, {}).get("last_capture"),
            "captured_chat_ids": state.get(k, {}).get("captured_chat_ids", []),
        })

if not targets:
    sys.exit(f"No matching projects found for argument: {DEAL!r}")

print(json.dumps(targets, indent=2))
EOF
```

Parse the result. Each entry has: `project_id`, `url`, `block_types`,
`last_capture` (ISO or null), `captured_chat_ids` set.

---

## STEP 2 — Load Chrome MCP

```
ToolSearch({ query: "claude-in-chrome", max_results: 30 })
```

Then verify a browser is connected:

```
mcp__claude-in-chrome__list_connected_browsers
```

If no browser, stop and tell Yoni: "The Chrome extension is not
connected. Open Chrome and confirm the Claude in Chrome extension is
active, then re-run."

Get tab context:

```
mcp__claude-in-chrome__tabs_context_mcp({ createIfEmpty: true })
```

Use the returned `tabId` for all subsequent browser actions.

---

## STEP 3 — For each target, walk the chat list

### 3a. Open the project page

```
mcp__claude-in-chrome__navigate({ url: project_url, tabId })
```

Wait 4 seconds for the page to load.

### 3b. Get the chat list

The deal project page lists recent chats below the input field, each
with a title and a "Last message X ago" timestamp. Read the page:

```
mcp__claude-in-chrome__read_page({ tabId, filter: "interactive" })
```

Extract the chat list — each entry is a clickable element with title
+ relative timestamp. Build a list of `{title, ref, relative_time}`.

If the page shows "Start a chat to keep conversations organized" (no
prior chats), skip this deal — `last_capture` gets bumped, nothing
to do.

### 3c. Decide which chats to scrape

For each chat in the list:
- If `chat_id` (derive from chat URL or title hash) is in
  `captured_chat_ids` AND `relative_time` indicates no new messages
  since last_capture → skip.
- Otherwise → scrape.

Heuristics for "new messages": if the chat says "Last message N
minutes/hours ago" and N hours < hours-since-last_capture, it's been
updated. If "X days ago" and X >= days-since-last_capture, no update.

When in doubt, scrape — the block-id dedup downstream means re-scraping
is idempotent (helper rejects duplicate ids).

### 3d. For each chat to scrape

1. Click the chat title (use the `ref` from read_page):

```
mcp__claude-in-chrome__computer({ action: "left_click", tabId, ref: <chat_ref> })
```

2. Wait 4 seconds for chat to load.

3. Get full chat text:

```
mcp__claude-in-chrome__get_page_text({ tabId })
```

4. Route by block type. Scan the text for each block type the project
   registers. For each type found, pipe to the correct handler:

**`---DEAL-INTEL---` blocks** (TCIP deal projects):
```bash
echo "<full chat text>" | python3 ~/cos-pipeline/tools/intel_capture.py parse-stdin
```
Finds `---DEAL-INTEL---` ... `---END-DEAL-INTEL---` blocks, validates
`deal:` against the registry, appends to the right `log.json`.

**`---NDA-LESSONS---` blocks** (NDA Review project):
```bash
echo "<full chat text>" | python3 ~/cos-pipeline/tools/nda_log_processor.py parse-stdin
```
Finds `---NDA-LESSONS---` ... `---END-NDA-LESSONS---` blocks, deduplicates
by SHA256 hash (state: `~/dashboards/data/nda_lessons_state.json`), and
merges DEAL-LOG-ROW into §7, FRAMEWORK-UPDATE into §3, LANGUAGE-UPDATE
into §9 of the NDA Reviewer doc via an LLM-supervised rewrite.

Both handlers discard chat text outside their respective blocks — we never
store full chat transcripts (Rule DS1).

5. Record this chat as captured:
   - Compute `chat_id = djb2(chat_url)` or `djb2(chat_title + first_message_excerpt)` — whatever's stable
   - Add to `captured_chat_ids` for this project in state

6. Click back/projects link to return to the project page for the
next chat:

```
mcp__claude-in-chrome__navigate({ url: project_url, tabId })
```

(Faster than browser back; avoids state-replay bugs.)

### 3e. Update state file

After processing all chats for this project, write back:

```python
import json, os
from datetime import datetime
state_path = os.path.expanduser('~/dashboards/data/chat_capture_state.json')
state = json.loads(open(state_path).read()) if os.path.exists(state_path) else {}
state[project_id] = {
    "last_capture": datetime.now().isoformat() + "Z",
    "captured_chat_ids": sorted(set(captured_chat_ids))
}
open(state_path, 'w').write(json.dumps(state, indent=2))
```

---

## STEP 4 — Final summary

```
CAPTURE-DEAL-CHATS COMPLETE
============================
{project_id}: {N} chats scraped, {M} DEAL-INTEL blocks routed (intel_capture)
nda:          {N} chats scraped, {M} NDA-LESSONS blocks merged (nda_log_processor)
{project_id}: skipped — no chats / no new messages
...

Routing details:
  DEAL-INTEL errors → ~/dashboards/logs/intel_capture_errors.log
  NDA-LESSONS state → ~/dashboards/data/nda_lessons_state.json
```

---

## DEAL-INTEL block types

The standard DEAL-INTEL block routes intel to a single deal:

```
---DEAL-INTEL---
deal: pngts
date: 2026-05-25
title: Pipeline tariff update
summary: FERC approved new tariff structure for PNGTS Phase 2.
facts:
  - Tariff effective 2026-07-01
counterparties:
  - Mark Mitchell (TC Energy) — supportive of Phase 2
---END-DEAL-INTEL---
```

### Block type: cross_deal_link

When a strategic insight connects two or more deals, emit a JSON-body block:

```
---DEAL-INTEL---
{"type": "cross_deal_link",
 "deals": ["pngts", "unitil"],
 "frame": "PNGTS could acquire Unitil for synergistic EBITDA",
 "rationale": "1 sentence — why this connection matters strategically",
 "evidence": "1-2 lines of supporting context from this chat"}
---END-DEAL-INTEL---
```

Routes the same intel entry to EACH deal's log.json (with source="intel"
and a `cross_deal_link_partner` field set to the other deal slug(s)).
Jane's per-deal jane_brief.md will pick it up from log.json under both
deals and synthesize the connection at the portfolio layer.

Rules for cross_deal_link blocks:
- `deals` must list ≥2 registered deal slugs (from deal-system-data.json)
- `frame` is the one-sentence strategic insight
- `rationale` explains why the connection matters (one sentence)
- `evidence` provides supporting context from this chat (1-2 lines)
- Routing is bidirectional: each deal gets an entry with `cross_deal_link_partner`
  set to the other slug(s)
- Unknown slugs are skipped with a warning; known slugs still receive entries

---

## RULES (non-negotiable)

- **Block-only.** Never store or upload full chat text. Only parsed
  block contents (`---DEAL-INTEL---`, `---NDA-LESSONS---`) reach their
  handlers. Chat text outside blocks is discarded. (Rule DS1)
- **Idempotent.** Re-scraping a chat is safe — block dedup at the
  handler level (SHA256 / id collision) prevents double-routing.
- **Block-only is privacy.** `log.json` and the NDA Reviewer doc are
  in private storage; full chat content stays on claude.ai.
- **Per-project failures don't abort the run.** Log + continue.
- **Chrome not connected → exit cleanly.** Don't retry; the periodic
  trigger will fire again.
- **New project_url auto-discovered.** Adding a `project_url` to
  `drive-docs.yaml` or `sync-state.json` is sufficient — no code
  changes needed to include a new project in the next run.

---

## ERROR HANDLING

- Chrome extension not connected → stop, log, exit.
- Project URL 404 (project deleted) → log, skip deal, continue.
- Chat list scrape fails → log, skip deal, continue.
- intel_capture parse failure → already logged to
  `~/dashboards/logs/intel_capture_errors.log`. Continue.
- Per-chat scrape times out → mark chat NOT captured (will retry
  next run). Continue to next.

---

## POST-HOOK — Phase J artifact ingestion (Claude Max, free)

After the chat scrape completes, run Phase J ingestion so any
artifacts that `artifact-pull` downloaded as part of the chat capture
get extracted into structured DEAL-INTEL + proposed-followups +
entity_mentions BEFORE the next /wrap or synthesis tick reads them.

```bash
# 1. Walk ~/Downloads/_Routed/<slug>/*.md, dedup-aware per artifact.
python3 ~/cos-pipeline/cos_artifact_ingest.py

# 2. Auto-apply ≥0.95-confidence staged followups to Drive Follow-ups.
python3 ~/cos-pipeline/cos_followup_applier.py
```

100% Claude Max via `_claude_dispatch` per CC1. Per-artifact dedup
via `data/deals/<slug>/artifacts.json`. Graceful fallback: any
failure exits 0; the 30-min LaunchAgent
(`com.cospipeline.tomac.artifact-ingest`) catches up next tick.

Design: `~/dashboards/docs/DESIGN-phase-J-artifact-ingest.md`.
