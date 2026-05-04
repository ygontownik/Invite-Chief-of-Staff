# API Optimization Reference
## COS Pipeline — Caching, Routing, and Cost Architecture

> Last updated: 2026-05-04
> Covers work across multiple sessions. Use this as the starting point for any further changes.

---

## The Core Problem We Solved

Every scheduled routine (capture pipeline, personal briefing, podcast memos) was running as a **Claude Code SKILL session** — meaning each run started a full Claude Code agent, loaded the session, and then called the API. This billed at full metered rates *plus* session overhead on every fire.

The fix: **direct Python scripts calling the Anthropic API via `cached_client.py`**, with the investor identity and Tomac bundle pre-cached in system prompt blocks. No Claude Code involved. Each routine is now a simple LaunchAgent → bash runner → python3 script.

---

## What Changed: File by File

### `_subscription/cached_client.py`
The central SDK wrapper. All production routines call this instead of making raw API calls.

**What it does:**
- Reads `system_prompt_v1.md` and splits it on two `<!-- CACHE_BREAKPOINT_N -->` markers into three segments
- Sends segments 1 and 2 with `cache_control: {"type": "ephemeral"}` — these are cached by Anthropic
- Accepts an optional `routine_prompt` (fourth cached block — see Third Breakpoint below)
- Logs every call to `cache_telemetry.jsonl` for cost tracking

**Key functions:**
```
complete(user_query, source_content, tenant_bundle, model, max_tokens, routine_prompt)
submit_batch(requests, routine, tenant_bundle, model, max_tokens, routine_prompt)
retrieve_batch(batch_id) → results or None if still pending
load_pending_batches(routine) → list of unwritten pending jobs
mark_batch_written(batch_id)
```

**Batch state sidecar:** `~/credentials/pending_batches.json`

---

### `system_prompt_v1.md` (280 lines)
The static core that rides the cache. Contains:
- Lines 1–229: Investor identity, memo structure, action extraction rules, firm list, analytical lenses (Yoni's CLAUDE.md §1–5)
- `<!-- CACHE_BREAKPOINT_1 -->` at line 229
- Lines 230–274: Tomac Cove tenant bundle (sectors, deal pipeline, key people, top themes)
- `<!-- CACHE_BREAKPOINT_2 -->` at line 274
- Lines 275–280: Variable slot (not cached)

**Cached prefix size: ~4,131 tokens.** At Sonnet rates: $0.0012 to read vs $0.0124 to send uncached.

---

### `cos_capture_pipeline.py` — migrated 2026-05-04
*Was:* SKILL wrapper → Claude Code session → raw urllib Anthropic call
*Now:* Direct Python → `cached_client.complete()`

Key change in `call_claude()`:
```python
result = complete(
    user_query="",
    source_content=user_payload,      # emails + calendar + docs (~10K–25K tokens, always full price)
    tenant_bundle="",
    routine_prompt=system_prompt,     # capture ruleset + JSON schema (~1,800 tokens, now cached)
    model="claude-sonnet-4-6",
    max_tokens=8192,
)
```

**LaunchAgent:** `com.tomaccove.cos-capture-pipeline` → `cos-capture-pipeline-runner.sh` → runs at 7:22am M–F

---

### `cos_personal_briefing.py` — migrated 2026-05-04
*Was:* SKILL wrapper → Claude Code session → raw urllib Anthropic call with full identity preamble in system prompt
*Now:* Direct Python → `cached_client.complete()`

Key change in `call_claude()`:
```python
result = complete(
    user_query="",
    source_content=source_content,    # follow-ups + recruiting + pipeline + market docs (~20K tokens)
    tenant_bundle="",
    routine_prompt=format_prompt,     # briefing section template (~300 tokens, now cached)
    model="claude-sonnet-4-6",
    max_tokens=2048,
)
```

**LaunchAgent:** `com.tomaccove.cos-personal-briefing` → `cos-personal-briefing-runner.sh` → runs at 7:51am M–F

---

### `podcast_transcribe.py` — migrated prior session, refined 2026-05-04
*Was:* SKILL wrapper → Claude Code session → raw urllib Anthropic call
*Now:* Direct Python → `cached_client.complete()` (sync) or `cached_client.submit_batch()` (async)

Key changes:
- `max_tokens` raised from 2,048 → 4,096 (memos were truncating mid-sentence at the old cap)
- `MEMO_PREAMBLE` moved to `routine_prompt` (third cached block)
- `--batch` flag: submits memo to Batch API instead of waiting synchronously (50% off)
- `--retrieve-batches` flag: picks up pending batch results and writes memos to Google Docs

```python
result = complete(
    user_query="",
    source_content=dynamic,           # show + title + transcript (~10K tokens, always full price)
    tenant_bundle="",
    routine_prompt=MEMO_PREAMBLE,     # 6-section memo format rules (~300 tokens, now cached)
    model="claude-sonnet-4-6",
    max_tokens=4096,
)
```

**LaunchAgents (two, replacing one):**
- `com.tomaccove.podcast-transcribe-daily` → `podcast-transcribe-runner.sh` → 2:00am (was 5:00am SKILL)
  - Transcribes audio via AssemblyAI, submits Claude batch
- `com.tomaccove.podcast-retrieve` → `podcast-retrieve-runner.sh` → 5:00am (new)
  - Retrieves batch results, writes memos to Google Docs

---

### `cos_gmail_mini_v2.py` — migrated prior session
`sonnet_enrich()` routes through `cached_client.complete()` with max_tokens=512.
Gmail reads stay on `gmail_mini_token.pickle`; Google Docs writes use `token.json` (fix for 403 scope error).

---

## The Four-Layer Cache Architecture

Every production call now has this structure on the wire:

```
SYSTEM BLOCK 1 — Investor identity, memo structure, action rules, firm list
                 ~4,131 tokens | cache_control: ephemeral | ✅ CACHED
                 <!-- CACHE_BREAKPOINT_1 -->

SYSTEM BLOCK 2 — Tomac Cove tenant bundle (sectors, pipeline, people, themes)
                 ~400 tokens   | cache_control: ephemeral | ✅ CACHED
                 <!-- CACHE_BREAKPOINT_2 -->

SYSTEM BLOCK 3 — Static tail of system_prompt_v1.md
                 ~50 tokens    | no cache_control         | full price

SYSTEM BLOCK 4 — Per-routine format prompt (MEMO_PREAMBLE / capture ruleset /
                 briefing template) — only present when routine_prompt is set
                 ~300–1,800 tokens | cache_control: ephemeral | ✅ CACHED

USER MESSAGE   — "Today's date: ... \n\nSource content:\n[docs/emails/transcript]"
                 ~10,000–25,000 tokens | always full price (changes every run)
```

**The source documents (user message) will always be full price.** They change every run and cannot be cached. This is unavoidable and represents 60–85% of input cost for the data-heavy routines.

---

## Batch API

For routines where results don't need to be immediate, the Batch API gives a flat **50% discount** on all tokens.

**Candidates (implemented):**
- Podcast memos — submitted at 2am, retrieved at 5am, well before 7:51am briefing

**Not candidates (too time-sensitive):**
- Capture pipeline — output feeds the 7:51am briefing (29-min window, can't queue)
- Personal briefing — read immediately on wake-up
- Gmail-mini — fires every 2h, must respond within the window

**Force sync from dashboard:** Admin → Routines tab → Delayed Jobs card (top of page). Shows any pending batch results with a "Force sync now" button. Calls `podcast_transcribe.py --retrieve-batches` in the background.

---

## Cache Economics

**Anthropic pricing (Sonnet 4.6, as of May 2026):**

| Token type | $/M tokens |
|------------|------------|
| Standard input | $3.00 |
| Cache write (first call) | $3.75 (+25%) |
| Cache read (subsequent calls) | $0.30 (−90%) |
| Output | $15.00 |
| Batch input (all types × 0.5) | 50% off |

**The personal-use reality:** Daily routines run once at fixed times, always cold-starting the cache (5-minute TTL has expired). Cache writes cost ~$0.003/run *more* than uncached. Across three routines five days/week: **~$2.40/year** overhead. Not worth special-casing.

**The subscriber-scale payoff:** With N concurrent subscribers running the same routine within the same 5-minute window, subscriber 1 pays the write cost; subscribers 2–N pay 10% of normal on the cached blocks. At 10 subscribers the overhead flips to savings. At 100, the savings on the static core alone are ~$0.011 per user per call vs $0.124 without caching.

**Telemetry to date (87 calls, all testing + dry-runs):**
- Cost with caching: $1.74
- Cost without cache would have been: $2.53
- Saved: $0.78 (31%) — from rapid-fire test sessions where cache was warm

---

## The "Timestamp Trap" (Avoided)

`Today's date:` is in the **user message**, not the system prompt. If it were in the system prompt, the cache would invalidate every minute. This is the most common caching mistake. It's handled correctly in `cached_client.py`:

```python
user_content = f"Today's date: {today}\n\nSource content:\n{source_content}"
```

---

## LaunchAgent Map — Old vs New

| Routine | Old plist | New plist | Runner script |
|---------|-----------|-----------|---------------|
| COS capture | `com.yoni.claude-task.cos-capture-pipeline` | `com.tomaccove.cos-capture-pipeline` | `cos-capture-pipeline-runner.sh` |
| Personal briefing | `com.yoni.claude-task.cos-personal-briefing` | `com.tomaccove.cos-personal-briefing` | `cos-personal-briefing-runner.sh` |
| Podcast (transcribe) | `com.yoni.claude-task.podcast-transcribe-daily` | `com.tomaccove.podcast-transcribe-daily` | `podcast-transcribe-runner.sh` |
| Podcast (retrieve) | *(didn't exist)* | `com.tomaccove.podcast-retrieve` | `podcast-retrieve-runner.sh` |

All runner scripts are in `~/dashboards/scripts/`. All use `/opt/homebrew/bin/python3` (3.14) — **not** `/usr/bin/python3` (3.9, too old for `dict | None` syntax in `_firm_context.py`).

---

## What's Still Pending

| Item | Impact | Notes |
|------|--------|-------|
| EVAL_PASS3.md blind scoring | Decide Sonnet vs Opus for IC memos | User action: read `_subscription/EVAL_PASS3.md`, score A vs B, check answer key |
| Pass 3 IC memo migration | Move IC memo from SKILL to direct Python | Currently still `SKILL_pass3_ic_memo.md` — Batch API would apply here too |
| Cloudflare Worker proxy | Subscriber-key → Yoni-key mapping for Tier 2b | Phase 3 of subscription product |

---

## If You Need to Modify the Cache Architecture

**To change what's cached in the static core:**
Edit `_subscription/system_prompt_v1.md`. The two `<!-- CACHE_BREAKPOINT_N -->` markers control the split. Everything before `CACHE_BREAKPOINT_1` is Block 1; between the two markers is Block 2 (tenant bundle); after `CACHE_BREAKPOINT_2` is Block 3 (not cached).

**To change the per-routine format prompt (fourth block):**
Edit the `routine_prompt=` argument in the relevant script's `complete()` call. For podcast it's `MEMO_PREAMBLE` (constant at top of `podcast_transcribe.py`). For capture it's the `build_system_prompt(ctx)` return value. For briefing it's `_build_briefing_format_prompt()`.

**To add a new routine:**
1. Write the script, call `cached_client.complete()` with `routine_prompt=<your stable ruleset>`
2. Create a runner script in `~/dashboards/scripts/`
3. Create a `com.tomaccove.<name>.plist` in `~/Library/LaunchAgents/`
4. `launchctl bootstrap "gui/$(id -u)" <plist path>`

**To check if caching is working:**
```bash
tail -5 ~/cos-pipeline/_subscription/cache_telemetry.jsonl | python3 -c "
import json,sys
for l in sys.stdin:
    r=json.loads(l)
    if 'event' in r: continue
    total=r['cache_creation_tokens']+r['cache_read_tokens']+r['uncached_input_tokens']
    print(f'create={r[\"cache_creation_tokens\"]} read={r[\"cache_read_tokens\"]} uncached={r[\"uncached_input_tokens\"]} ({round(r[\"cache_read_tokens\"]/total*100,1) if total else 0}% from cache)')
"
```
Cache reads will be 0% on daily cold-start runs. That's expected. Non-zero reads mean multiple calls happened within the same 5-minute window.
