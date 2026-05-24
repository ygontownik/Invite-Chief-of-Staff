---
description: Scrape claude.ai deal-project chats for ---DEAL-INTEL--- blocks and route them into per-deal log.json. Block-only — never full transcripts.
argument-hint: "[deal_id | all]"
---

# /capture-deal-chats — auto-capture DEAL-INTEL blocks from claude.ai

For each TCIP deal project on claude.ai with a `project_url` in
`~/cos-pipeline/tools/sync-state.json`, walk the chat list, open any
chats with new messages since last capture, scrape ONLY the
`---DEAL-INTEL---` blocks (not the surrounding chat text), pipe them
through `intel_capture.py parse-stdin` to route into the right deal's
`~/dashboards/data/deals/<deal>/log.json`.

`/deal-sync` then folds them into status + master brief on next cycle.

This is the claude.ai counterpart to the Stop hook's
`run_intel_capture_scan()` (which handles Claude Code transcripts).
Two surfaces, one block format, one target log.

---

## STEP 0 — Parse argument

`$ARGUMENTS` is `<deal_id>` or `all` (default: `all`).

---

## STEP 1 — Load registry + capture state

```bash
python3 - <<'EOF'
import json, os
with open('/Users/ygontownik/cos-pipeline/tools/sync-state.json') as f:
    ss = json.load(f)
state_path = os.path.expanduser('~/dashboards/data/chat_capture_state.json')
state = json.loads(open(state_path).read()) if os.path.exists(state_path) else {}

DEAL = "$DEAL_ID_OR_ALL"
if DEAL == "all":
    targets = [(k, v["project_url"], state.get(k, {}))
               for k, v in ss.items() if v.get("project_url")]
else:
    if DEAL not in ss or not ss[DEAL].get("project_url"):
        import sys; sys.exit(f"deal {DEAL} not found")
    targets = [(DEAL, ss[DEAL]["project_url"], state.get(DEAL, {}))]
print(json.dumps([{"deal_id": t[0], "url": t[1],
                   "last_capture": t[2].get("last_capture"),
                   "captured_chat_ids": t[2].get("captured_chat_ids", [])}
                  for t in targets], indent=2))
EOF
```

Parse the result. You now have, per target: `deal_id`, project `url`,
`last_capture` ISO timestamp (or null), and `captured_chat_ids` set.

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

4. Pipe through intel_capture (extracts ONLY DEAL-INTEL blocks):

```bash
echo "<full chat text>" | python3 ~/cos-pipeline/tools/intel_capture.py parse-stdin
```

The helper finds `---DEAL-INTEL---` ... `---END-DEAL-INTEL---` blocks,
parses each, validates `deal:` is in the registry, appends to the
right `log.json`. The chat text outside blocks is discarded — we never
store full chat transcripts.

5. Record this chat as captured:
   - Compute `chat_id = djb2(chat_url)` or `djb2(chat_title + first_message_excerpt)` — whatever's stable
   - Add to `captured_chat_ids` for this deal in state

6. Click back/projects link to return to the project page for the
next chat:

```
mcp__claude-in-chrome__navigate({ url: project_url, tabId })
```

(Faster than browser back; avoids state-replay bugs.)

### 3e. Update state file

After processing all chats for this deal, write back:

```python
import json, os
from datetime import datetime
state_path = os.path.expanduser('~/dashboards/data/chat_capture_state.json')
state = json.loads(open(state_path).read()) if os.path.exists(state_path) else {}
state[deal_id] = {
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
{deal_id}: {N} chats scraped, {M} DEAL-INTEL blocks routed (via intel_capture)
{deal_id}: skipped — no chats / no new messages
...

Routing details: ~/dashboards/logs/intel_capture_errors.log
```

---

## RULES (non-negotiable)

- **Block-only.** Never store or upload full chat text. Only the
  parsed `---DEAL-INTEL---` block contents reach `log.json`. Chat
  text outside blocks is discarded by `intel_capture.py parse-stdin`.
- **Idempotent.** Re-scraping a chat is safe — the block dedup at
  helper-side (id collision) prevents double-routing.
- **Block-only is privacy.** `log.json` is in a private repo
  (`Private-Yoni-Dashboard`), but full chat content stays on
  claude.ai. We export the structured intel only.
- **Per-deal failures don't abort the run.** Log + continue.
- **Chrome not connected → exit cleanly.** Don't retry; the periodic
  trigger will fire again.

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
