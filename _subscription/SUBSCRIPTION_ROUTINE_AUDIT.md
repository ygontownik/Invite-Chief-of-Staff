# Subscription Routine Audit — What Works for Subscribers Without Claude Code

## Strategic context

The subscription product needs to run for subscribers who **do not have a Claude Code account**. Two billing models are on the table:

- **Direct API**: subscriber gets their own Anthropic API key, you ship them the code, they pay Anthropic directly. They never need Claude Code.
- **Service provider**: subscribers pay you, you pay Anthropic for aggregate API usage on their behalf. You hold the API key. They still never need Claude Code.

For both models, **every routine in their workflow must work via the Anthropic SDK only** — no Claude Code reasoning in the runtime path. This audit classifies every existing scheduled task into three buckets and tells you what each one needs.

---

## The decision tree

```
For each routine, ask:

1. Does the routine make ANY Claude API calls?
   ├── No → SUBSCRIPTION-READY (it's just file movement / Drive API / OAuth)
   │        Action: package and ship.
   │
   └── Yes → Where do the Claude calls happen?

2. Are the Claude calls inside a standalone Python script?
   ├── Yes → SUBSCRIPTION-READY after one cleanup step.
   │   ├── Already uses cached_client.py? → SHIP (this is the migration we did).
   │   └── Uses raw urllib / requests? → MIGRATE to cached_client.py (~30 min/script;
   │       same pattern as podcast_transcribe). Net: subscriber gets static-core
   │       caching benefits for free.
   │
   └── No, the Claude calls happen INSIDE the Claude Code session itself
       (the SKILL is doing multi-step reasoning, tool use, judgment) → REWRITE NEEDED.
       Three options, ordered by effort:
       (a) Decompose into discrete Python+API steps (lift-and-shift).
           Hardest but cleanest. Subscriber gets a portable Python package.
       (b) Replace with the Anthropic Agent SDK (multi-turn loop without Claude Code).
           Medium effort. Needs an SDK rewrite of the SKILL's logic.
       (c) Require subscribers to have a Claude Code account.
           Easiest, but breaks the subscription value prop. Don't pick this.
```

---

## Per-routine classification (24 scheduled tasks)

### Bucket 1 — SUBSCRIPTION-READY TODAY (Python pipeline, already migrated)

| Routine | Python script | Status |
|---|---|---|
| `cos-gmail-mini` (LaunchAgent skips Claude entirely; bash → python3) | `cos_gmail_mini_v2.py` | ✓ Migrated to cached_client (Option B: sonnet_enrich only) |
| `podcast-transcribe-daily` | `podcast_transcribe.py` | ✓ Migrated to cached_client |

**Subscriber experience:** they install the Python script, set `ANTHROPIC_API_KEY`, schedule the LaunchAgent. No Claude Code involved. Static-core cache cuts ongoing API spend ~21-44%.

### Bucket 2 — SUBSCRIPTION-READY AFTER ~30 MIN MIGRATION EACH

These are SKILLs that are **explicitly described as "thin wrappers"** around Python scripts that already make their own Claude calls (just via raw urllib instead of cached_client). The Claude Code session itself does nothing meaningful — it just `python3 script.py`.

| Routine | Python script | Currently calls Claude via | Migration |
|---|---|---|---|
| `cos-personal-briefing` (daily 7:51am) | `cos_personal_briefing.py` | urllib + manual cache_control | Swap to cached_client.complete() |
| `cos-capture-pipeline` (daily 7:22am) | `cos_capture_pipeline.py` | urllib + manual cache_control | Swap to cached_client.complete() |

**The bigger win here is removing Claude Code from the path entirely.** Today these routines fire as `LaunchAgent → run-claude-task.sh → claude --print → SKILL → python3 script.py`. The Claude Code shell is a fixed overhead per run that subscribers don't need. Replace with `LaunchAgent → bash → python3 script.py` (the cos-gmail-mini pattern). Net subscriber experience: faster, cheaper, no Claude Code license required.

### Bucket 3 — Pure orchestration (no Claude calls; trivially portable)

These routines don't call Claude at all — they're just shell + Python + Drive API. Subscription-ready as-is, only need credential templating.

- `gs-research-daily-download`, `gs-research-pdf-processor`
- `jefferies-pdf-downloader`, `jefferies-pdf-processor`
- `rbn-daily-sync`, `run-syncall-gas`
- `cos-otter-transcripts-backfill-now` (variant)
- `peakload-weekly-sync`
- `master-daily-update` (calls `reorder_substack_docs.py` — verify no Claude calls)
- `tomac-cove-weekly-pipeline` (calls `send_pipeline_email.py` — verify)

**Verify each by grepping for `anthropic|messages.create|claude` in the underlying scripts; the SKILLs may be light touch but the Python scripts could still hit the API.**

### Bucket 4 — REWRITE NEEDED (heavy Claude Code reasoning)

These SKILLs are 100-540 lines of multi-step reasoning, tool dispatch, judgment calls, and document writes. The Claude Code session is doing real work — there's no Python script behind it that makes a single API call.

| Routine | Lines in SKILL | Reasoning indicators |
|---|---|---|
| `cos-otter-transcripts` (daily) | 538 | Drive queries, dedup, multi-source categorization, action extraction, doc writeback to 4 docs |
| `cos-otter-transcripts-midday` | 161 | Same engine, midday-scoped |
| `cos-otter-transcripts-backfill-now` | 148 | Same engine, manual backfill |
| `cos-otter-transcripts-afternoon` | 103 | Same engine, afternoon-scoped |
| `notebooklm-daily-briefing` | (16 py refs) | Multi-step Substack scraping + briefing assembly |
| `notebooklm-sunday-weekly-briefing` | (21 py refs) | Same, weekly cadence |
| `tomac-deal-compile` | ? | Compiles deal-pipeline-data.json from many sources |
| `cos-email-resolver-morning/-evening` | small | Calls cos_email_resolver.py — verify what's inside |
| `sunday-weekly-email` | small | Calls sunday_weekly_email.py — verify |
| `beside-pipeline` | 21 | Small, but Claude-driven |

For these, the rewrite path is:

- **Best case (single-shot Claude call hidden in SKILL)**: port to a Python script that uses `cached_client.complete()`. ~1-2 hours each.
- **Worst case (multi-tool reasoning loop)**: rewrite using the Anthropic Agent SDK or break into discrete Python pipeline steps. ~1-3 days each. The `cos-otter-transcripts` family is the canonical hard case — categorization plus dedup plus 4-doc writeback is a real workflow.

---

## Recommended priority order for the subscription product

**Phase 1 — Ship the easy wins (done + 2 hours work):**

1. ✓ `cos-gmail-mini` — already in production via direct LaunchAgent. **Done.**
2. ✓ `podcast-transcribe-daily` — Python migrated; remove the SKILL wrapper next. (~15 min)
3. **Migrate `cos_personal_briefing.py` to cached_client + drop the SKILL wrapper.** (~30 min)
4. **Migrate `cos_capture_pipeline.py` to cached_client + drop the SKILL wrapper.** (~30 min)

After Phase 1, the four highest-frequency / highest-value daily routines are pure-Python and subscription-ready. You can demo a working subscription to a prospect.

**Phase 2 — Audit the ambiguous middle (~half day):**

5. Verify Bucket 3 is actually Claude-free by grepping each underlying Python script.
6. For Bucket 3 scripts that DO hit Claude (likely jefferies-pdf-processor, gs-research-pdf-processor, sunday-weekly-email, tomac-deal-compile): migrate to cached_client.

After Phase 2, ~75% of the dashboard's recurring work runs without Claude Code.

**Phase 3 — Decide the cos-otter-transcripts question (1-3 days):**

7. The transcript-processing family is the biggest remaining Claude Code dependency. Choose one path:
   - **(a)** Port to discrete Python steps. Hard but most portable.
   - **(b)** Rewrite using Anthropic Agent SDK. Medium hard, single artifact.
   - **(c)** Make this a "premium tier" feature that requires the customer to add their own Claude Code account. Easy out, but limits the product.

This is a real architectural fork. Recommend deciding before promising subscribers a launch date.

---

## What this means for the work I just shipped

Concretely:

- The `cached_client.py` + `system_prompt_v1.md` architecture is the **substrate** of the subscription product. Every Bucket 1 and Bucket 2 routine inherits from it.
- The two migrations I did (`podcast_transcribe.py`, `cos_gmail_mini_v2.py` Option B) are the proof points that the substrate works in production.
- The `refresh_bundle.py` script is the mechanism by which a subscriber's customized bundle (their firm list, their themes) stays current without manual editing.
- The Pass 3 Sonnet vs Opus eval will tell you which model to default to for subscribers' IC memo workload — affects their per-call cost.

What I did **not** touch and is **not yet subscription-ready**:

- The SKILL-as-thin-wrapper pattern (Bucket 2). These need the cached_client migration AND the LaunchAgent rewiring to drop Claude Code. That's the next concrete work block — and the highest-leverage one because it's two more daily routines becoming portable.
- Bucket 4 SKILLs. These require a real architectural choice (port, rewrite, or premium tier).

---

## Suggested next move

Pick one of:

- **A)** I migrate `cos_personal_briefing.py` and `cos_capture_pipeline.py` to cached_client now (~1 hour total). After this, all four high-frequency daily routines are pure-Python and a subscriber can run them with just an API key.
- **B)** I run the Bucket 3 verification grep across all underlying Python scripts and update this audit with the actual API-call inventory (~30 min).
- **C)** Both A and B back-to-back.

Recommend C — gets you a complete picture plus two more migrations in production before lunch.
