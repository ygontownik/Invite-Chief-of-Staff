# MODEL_ROUTER.md — architecture + cutover plan for `_model_router.py`

**Status:** built, tested (13/13 unittest), call-site migration deferred until Track B merges (PLAN_v3.1 §C4).

---

## What it is

Single entry point — `call_claude(task_type, system, messages, ...)` — that every Claude-using script in the COS pipeline will eventually route through. It owns three concerns:

1. **Model selection** — picks Opus 4.7 / Sonnet 4.6 / Haiku 4.5 based on `task_type` per CLAUDE.md "Per-pass model assignments" table.
2. **Mode dispatch** — routes the call to the Anthropic SDK (`mode='api'`), to the subscription path (`mode='subscription'`, deferred per CSPIKE), or refuses (`mode='daemon'`).
3. **Cost discipline** — wraps the firm-context preamble in `cache_control={'type':'ephemeral'}`, writes per-call JSONL to `~/cos-pipeline/data-<tenant>/costs/YYYY-MM-DD.jsonl`, enforces `max_daily_usd` soft caps and 3x hard stops.

It is the only place model IDs and pricing should live. Call sites pass intent (`task_type='pass2_pipeline_analyst'`); the router resolves implementation.

---

## How call sites will plug in (Track C4 — DEFERRED)

Per PLAN_v3.1 §C4, do NOT migrate call sites until Track B (hardcoded ID excision) merges. Track B touches the same files (`cos_personal_briefing.py`, `cos_gmail_mini_v2.py`, `cos_capture_pipeline.py`) and a parallel rewrite would conflict.

When the cutover happens, every existing direct `client.messages.create(...)` becomes:

```python
# BEFORE
from anthropic import Anthropic
client = Anthropic()
resp = client.messages.create(
    model="claude-opus-4-7",
    max_tokens=4096,
    system=firm_context_preamble,
    messages=[...],
)

# AFTER
from _model_router import call_claude
result = call_claude(
    task_type="pass2_pipeline_analyst",
    system=firm_context_preamble,
    messages=[...],
)
text = result["text"]
```

Migration order (safest -> riskiest):
1. New code first — anything written in Track G (briefing migration spike) goes through the router.
2. `podcast_transcribe.py` — already `mode: api`, low blast radius.
3. Deal-pipeline 3-pass scripts — these benefit most (Pass 2 = Opus, Pass 3 = Sonnet, both 4096).
4. `cos_personal_briefing.py` — last, after CSPIKE GREEN since this is currently the highest-volume subscription consumer.

---

## Route resolution precedence

Highest -> lowest. First match wins.

1. **Per-tenant override** — `~/cos-pipeline-config-<tenant>/model_router.yaml :: routes.<task_type>` (PLAN §J multi-tenant). Lets the guinea-pig instance pin everything to Sonnet without editing code.
2. **routines.yaml** — `package` and `mode` fields. `package` selects the model bucket; `mode` picks subscription/api/daemon.
3. **CLAUDE.md per-pass keys** — `pass1_source_scanner`, `pass2_pipeline_analyst`, `pass3_ic_memo` get the explicit Opus/Sonnet + max_tokens from the table.
4. **CLAUDE.md package defaults** — `capture/briefing/research` -> Sonnet 4.6 / 2048; `deals` -> Opus 4.7 / 4096; `server/infra` -> Haiku 4.5 / 1024.
5. **Overall default** — Sonnet 4.6 / 2048.

`source` field on the returned `ModelRoute` says which layer matched. Useful for debugging "why did this call hit Opus."

---

## Per-tenant override schema

`~/cos-pipeline-config-<tenant>/model_router.yaml`:

```yaml
# Pin specific task_types to specific models for this tenant.
routes:
  pass2_pipeline_analyst:
    model: claude-sonnet-4-6   # cheap-mode override
    max_tokens: 2048
  briefing-morning:
    mode: api                  # force api even if subscription is GREEN

# Per-task daily soft caps in USD. Hard stop is 3x.
quotas:
  pass2_pipeline_analyst: 10.0
  podcast-transcribe-daily: 2.0
```

Both blocks optional. Tomac instance can run with no file at all (uses CLAUDE.md defaults). Guinea-pig instance ships with a stub that pins everything to Sonnet for cost control until usage profile stabilizes.

---

## Cost-tracking schema

One JSONL row per Claude call. Path: `~/cos-pipeline/data-<tenant>/costs/YYYY-MM-DD.jsonl`. Parent dirs auto-created.

Row shape:

```json
{
  "ts": "2026-05-02T14:23:11.482301+00:00",
  "task_type": "pass2_pipeline_analyst",
  "model": "claude-opus-4-7",
  "input_tokens": 12543,
  "output_tokens": 1832,
  "cached_input_tokens": 8200,
  "mode": "api",
  "est_usd": 0.21743
}
```

Cost math (matches `costs.py` PRICING table):
- Fresh input billed at full rate (Opus $15/M, Sonnet $3/M, Haiku $0.80/M).
- Cached input billed at 10% of input rate (`_CACHE_READ_FACTOR`).
- Output billed at full output rate (Opus $75/M, Sonnet $15/M, Haiku $4/M).

Subscription mode (when GREEN) writes a row with `est_usd: 0` and `mode: 'subscription'` so the dashboard can show "subscription saved $X.XX today vs api equivalent."

---

## Cache discipline (PLAN C3)

When `cache=True` (default), the `system` parameter is normalized to a single text block with `cache_control={'type':'ephemeral'}`. This is the firm-context preamble — the same ~3-5K tokens of firm/principal/team/owner-whitelist data prepended to every call. Cache hit -> 90% discount.

If the caller already passes `system` as a list of blocks (advanced use), the first block gets `cache_control` attached if missing. Existing `cache_control` on the caller's blocks is left alone.

`cache=False` -> raw string passthrough, no `cache_control`. Used by tests and any caller who explicitly doesn't want caching (rare).

---

## Quota enforcement (PLAN C6)

Every `mode='api'` call:
1. Sums today's `est_usd` for this `task_type` from the JSONL.
2. If `>= max_daily_usd`: writes a WARN line to stderr (does not block).
3. If `>= 3 * max_daily_usd`: raises `QuotaExceeded` BEFORE making the API call.

`max_daily_usd` source precedence:
- tenant override `quotas.<task_type>` (highest)
- `routines.yaml` row's `max_daily_usd` field
- module default `_DEFAULT_MAX_DAILY_USD = 5.00` (lowest)

Subscription and daemon modes bypass the quota check (no per-call $ to count).

---

## Cutover plan (post-CSPIKE)

**Phase 1 — router available, no migrations** (this commit). Router exists at `~/cos-pipeline/_model_router.py`. Tests pass. No call site uses it yet. Self-test prints the routing table.

**Phase 2 — Track B merges.** Hardcoded IDs out of `cos_personal_briefing.py` etc. Now safe to touch those files for router migration.

**Phase 3 — CSPIKE decision.**
- GREEN: remove `NotImplementedError` branch, wire chosen subscription path (CLI or Agent SDK), keep `mode='api'` only where needed (podcast/usage-report).
- RED: force-flip every `mode: subscription` in routines.yaml to `mode: api`; raise quota caps on briefing/deals; ship api-only.

**Phase 4 — call site migrations** (Track C4). One file at a time, in the order listed in "How call sites will plug in." Each migration:
1. Replace direct `Anthropic()` instantiation with `call_claude()` import.
2. Drop hardcoded model strings — caller passes `task_type` instead.
3. Verify costs.json shows the routine reporting via the new path.
4. Wait one full schedule cycle (24h or weekly) before moving to next file.

**Phase 5 — deprecate `_usage.py` direct usage logging** once every call goes through the router. Router's JSONL replaces `~/dashboards/data/anthropic-usage.jsonl`. `costs.py` adds a reader for the new path.

---

## Testing

`python3 _model_router.py --dry-run` — prints the resolved route for every known task_type without making any API call. Used as a smoke test.

`python3 _model_router_test.py` — 13 unittest cases covering routing precedence, subscription NotImplementedError, daemon ValueError, api dispatch with cache_control attachment, JSONL writing, soft-cap warning, hard-stop quota exception, and cache discount math. Mocks the anthropic SDK; no network. Writes cost JSONL into a tempdir; never touches live `data-tomac/`.

---

## What this router intentionally does NOT do

- **Streaming.** All callers use `messages.create` synchronously today; if/when streaming is needed, add a parallel `call_claude_stream()` rather than overloading the same function.
- **Tool use / function calling.** Out of scope for the router. Pass `tools=[...]` directly via the SDK; route through `mode='api'` only.
- **Retry on transient errors.** Anthropic SDK has its own retries. Router does not double-retry.
- **Routing across providers.** Anthropic only. If lane 2 ever wants a non-Anthropic backend, build a separate provider-router; do not cram it in here.
