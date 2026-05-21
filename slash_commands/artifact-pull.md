---
description: Walk each TCIP deal's claude.ai project via Chrome MCP, download any new artifacts (jsx/html/pptx/pdf) to ~/Downloads. local_file_router.py routes them to deal _Outputs/ by alias.
argument-hint: "[deal_id | all]"
---

# /artifact-pull — auto-pull new claude.ai artifacts per deal

For each TCIP deal in `drive-docs.yaml` with a `project_url`, open the
project on claude.ai, walk its chats, and download any **new artifacts**
(jsx/html/tsx/pptx/pdf/md/docx) since the deal's last pull. Artifacts
land in `~/Downloads`; [`local_file_router.py`](../tools/local_file_router.py)
(running every 30s) routes them by deal alias to the deal's `_Outputs/`.

Companion to [`/capture-deal-chats`](capture-deal-chats.md) — chat-capture
pulls `---DEAL-INTEL---` blocks (data); artifact-pull pulls the rendered
files (artifacts). Same Chrome MCP profile, same 4h cadence (fired by
[`dash-state-hook.py:run_artifact_pull()`](../tools/dash-state-hook.py)).

State: `~/credentials/processed_artifacts.json`
Log:   `~/dashboards/logs/artifact_pull.log`

---

## STEP 0 — Ensure real Chrome (mandatory)

Per the global rule, every Chrome-using SKILL must run this preflight.
chrome-devtools-mcp will silently fall back to a Cloudflare-blocked
profile if real Chrome isn't reachable on :9222.

```bash
bash ~/credentials/ensure_real_chrome.sh
```

If it exits non-zero: STOP. Tell Yoni "ensure_real_chrome.sh failed —
check ~/.chrome-mcp-profile and port 9222" and exit cleanly. The
periodic trigger will retry in 4h.

**Known Cloudflare caveat (2026-05-20):** the `/capture-deal-chats`
production runs surfaced that Chrome DevTools Protocol automation can
trigger Cloudflare's bot detection on claude.ai even on the real
Chrome profile. If the page returns a Cloudflare challenge instead of
the project view, fall back to direct claude.ai API calls using the
session cookie extracted from the real Chrome profile (see
[`/capture-deal-chats`](capture-deal-chats.md) — same workaround). Log
the fall-back in `~/dashboards/logs/artifact_pull.log` and continue.

---

## STEP 1 — Parse argument

`$ARGUMENTS` is `<deal_id>` or `all` (default: `all`).

---

## STEP 2 — Load registry + per-deal state

```bash
python3 - <<'PYEOF'
import json, yaml, os
from pathlib import Path

DRIVE_DOCS  = Path.home() / "dashboards/config/drive-docs.yaml"
STATE_PATH  = Path.home() / "credentials/processed_artifacts.json"

cfg = yaml.safe_load(DRIVE_DOCS.read_text())
deal_docs = cfg.get("deal_docs", {})
state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}

ARG = "$ARGUMENTS".strip() or "all"
if ARG == "all":
    targets = [(k, v["project_url"], state.get(k, {}))
               for k, v in deal_docs.items() if v.get("project_url")]
else:
    if ARG not in deal_docs or not deal_docs[ARG].get("project_url"):
        import sys; sys.exit(f"deal {ARG} has no project_url in drive-docs.yaml")
    targets = [(ARG, deal_docs[ARG]["project_url"], state.get(ARG, {}))]

print(json.dumps([{
    "deal_id":  t[0],
    "url":      t[1],
    "last_pull": t[2].get("last_pull"),
    "downloaded_ids": t[2].get("downloaded_artifact_ids", []),
} for t in targets], indent=2))
PYEOF
```

You now have, per target: `deal_id`, `url`, `last_pull` ISO timestamp
(or null on first run), and the `downloaded_ids` set (for dedup).

---

## STEP 3 — Open a Chrome page

```
mcp__plugin_chrome-devtools-mcp_chrome-devtools__list_pages
```

If at least one page is open, pick the first (or any non-extension
page). Otherwise:

```
mcp__plugin_chrome-devtools-mcp_chrome-devtools__new_page({ url: "about:blank" })
```

Use the returned `pageIdx` for all subsequent navigation.

---

## STEP 4 — For each target deal

### 4a. Navigate to the project

```
mcp__plugin_chrome-devtools-mcp_chrome-devtools__navigate_page({ url: <project_url>, pageIdx })
mcp__plugin_chrome-devtools-mcp_chrome-devtools__wait_for({ text: "Project", timeoutMs: 8000, pageIdx })
```

If navigation 404s or the project no longer exists: log "project not
found" and skip to the next deal.

### 4b. Snapshot the chat list

```
mcp__plugin_chrome-devtools-mcp_chrome-devtools__take_snapshot({ pageIdx })
```

Look at the structured output. The deal project page lists recent chats
below the input field — each entry shows a title + "Last message X ago".
Build a list `[{title, ref, relative_time}, ...]`.

If the chat list is empty ("Start a chat to keep conversations
organized"): nothing to pull, bump `last_pull` for this deal, continue.

### 4c. Filter chats by recency

Same heuristic as `/capture-deal-chats`:
- If `relative_time` says "X minutes/hours ago" and X hours < hours
  since `last_pull` → newer than last pull, enter.
- If "X days ago" and X days ≥ days since `last_pull` → skip.
- First-ever pull (`last_pull` is null) → enter every chat.

When in doubt, enter — the `downloaded_ids` dedup means we won't
re-download the same artifact twice.

### 4d. Per chat: open + find artifacts

For each chat to enter:

1. Click the chat title using its `ref`:

   ```
   mcp__plugin_chrome-devtools-mcp_chrome-devtools__click({ ref: <chat_ref>, pageIdx })
   mcp__plugin_chrome-devtools-mcp_chrome-devtools__wait_for({ text: "Claude", timeoutMs: 6000, pageIdx })
   ```

2. Snapshot the chat:

   ```
   mcp__plugin_chrome-devtools-mcp_chrome-devtools__take_snapshot({ pageIdx })
   ```

3. Locate artifact entries. claude.ai renders artifacts inline with a
   header containing the artifact title and a kebab/three-dot menu that
   exposes "Copy", "Download as <ext>", "Share". Look in the snapshot
   for elements matching:
   - role: button or menuitem
   - text containing "Download" (case-insensitive)
   - OR an `<a>` with `download` attribute / href ending in
     `.jsx|.tsx|.html|.pdf|.docx|.xlsx|.pptx|.md|.txt`

   For each candidate artifact, derive a stable `artifact_id`:

   ```python
   import hashlib
   artifact_id = hashlib.sha1(
       f"{deal_id}|{chat_id}|{artifact_title}".encode()
   ).hexdigest()[:16]
   ```

   If `artifact_id` is already in `downloaded_ids` for this deal, SKIP it.

4. For each NEW artifact:
   - Click the kebab/menu trigger to open the menu (if needed)
   - Click the "Download" item
   - The browser fires a download to `~/Downloads` (Chrome's default;
     no save-as dialog because the profile is configured for it)
   - Add `artifact_id` to `downloaded_ids` for this deal
   - Wait ~1s between downloads to keep Chrome's download manager happy

5. Navigate back to the project page for the next chat:

   ```
   mcp__plugin_chrome-devtools-mcp_chrome-devtools__navigate_page({ url: <project_url>, pageIdx })
   ```

### 4e. After all chats for this deal

Update the state object in memory:

```python
deal_state["last_pull"] = "<now-iso>"
deal_state["downloaded_artifact_ids"] = sorted(set(downloaded_ids))
```

DO NOT write yet — accumulate all deals first, single locked write at end.

---

## STEP 5 — Persist state (single locked write)

After all deals are done, write the merged state under a coordination
lock. This prevents races with future concurrent runs.

```bash
python3 - <<'PYEOF'
import json, sys, os
from pathlib import Path
from datetime import datetime

# Make coordination importable (it lives in tools/)
sys.path.insert(0, str(Path.home() / "cos-pipeline/tools"))
from coordination import lock

STATE_PATH = Path.home() / "credentials/processed_artifacts.json"
existing = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}

# Merge in updates produced during this run.
# Replace this stub with the per-deal updates accumulated in step 4e.
updates = {
    # "cholla": {"last_pull": "...", "downloaded_artifact_ids": [...]},
}
existing.update(updates)

with lock("processed-artifacts", holder="artifact-pull-skill", ttl_seconds=30):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(existing, indent=2))
    tmp.replace(STATE_PATH)
print("processed_artifacts.json updated")
PYEOF
```

---

## STEP 6 — Final summary

```
ARTIFACT-PULL COMPLETE
======================
{deal_id}: {N} new artifacts downloaded ({M} skipped as already-pulled)
{deal_id}: skipped — no new chats since {last_pull}
...

Downloads landed in ~/Downloads — local_file_router.py will route
within 30s. Watch routing in: ~/dashboards/logs/local_file_router.log
```

---

## RULES (non-negotiable)

- **Step 0 is mandatory.** Skipping `ensure_real_chrome.sh` will silently
  attach to the Cloudflare-blocked fallback profile and every download
  will 403.
- **Dedup by `artifact_id`.** Re-running the skill must be idempotent.
  Never re-download an artifact whose id is in `downloaded_ids` for that
  deal.
- **Per-deal failures don't abort the run.** Log + continue.
- **Single locked write at the end.** Don't write the state file inside
  the per-deal loop — a crash mid-loop would leave it half-updated. One
  write under `coordination.lock("processed-artifacts")`.
- **No artifact body parsing.** This skill never reads or transforms
  the downloaded file content. Routing + classification is
  `local_file_router.py`'s job.

---

## ERROR HANDLING

- Chrome not reachable on :9222 → step 0 fails → exit cleanly.
- Project URL 404 → log, skip deal, continue.
- Chat list snapshot fails → log, skip deal, continue.
- Per-artifact download click fails → mark NOT downloaded (will retry
  next 4h cycle), continue to next artifact.
- State write fails (lock timeout, disk full) → log, exit non-zero so
  the Stop hook can flag it.
