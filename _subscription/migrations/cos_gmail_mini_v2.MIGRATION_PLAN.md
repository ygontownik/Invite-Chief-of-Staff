# Migration Plan — `cos_gmail_mini_v2.py`

**Status:** draft (not swapped). Live file `~/cos-pipeline/cos_gmail_mini_v2.py` is untouched.
**Draft location:** `~/cos-pipeline/_subscription/migrations/cos_gmail_mini_v2.draft.py`

---

## 1. Diff summary

Two functions modified:

### `haiku_triage()` (lines ~584-622 in live file)
- **Removed:** local `import anthropic` + `client = anthropic.Anthropic(...)` setup.
- **Removed:** `client.messages.create(model="claude-haiku-4-5-20251001", system=TRIAGE_SYSTEM, ...)`.
- **Added:** lazy import of `cached_client.complete`.
- **Changed:** TRIAGE_SYSTEM is concatenated into `user_query` (carries the JSON-output contract; static core doesn't enforce it).
- **Adapted:** `log_usage` reads `result["usage"]` shape.

### `sonnet_enrich()` (lines ~674-733 in live file)
- Same pattern. Category-specific enrichment system prompts (DEAL/RECRUIT/ACTION) move into `user_query`.
- Otherwise identical structure swap.

No other functions touched. `_parse_json_response`, the JSON parsing, the category routing, the email-fetch logic — all unchanged.

## 2. What's actually different at the wire

| Aspect | Original | Migrated |
|---|---|---|
| Per-call system prompt | TRIAGE_SYSTEM (~80 tokens) or DEAL/RECRUIT/ACTION_ENRICHMENT_SYSTEM (~80-200 tokens) | 4,234-token static core (sections 1-5) + 1,638-token Tomac bundle |
| Cache | none (small system prompts) | both segments cached on first call; hit on subsequent calls in 5-min window |
| User message | email content only | original system prompt + email content (slightly larger) |

## 3. Cost analysis — the load-bearing concern

### `haiku_triage` — likely cost regression

Haiku 4.5: $0.80/M input, $4/M output.

| | Original | Migrated (cache miss) | Migrated (cache hit) |
|---|---|---|---|
| System tokens | ~80 (uncached) | ~80 (concatenated into user_query) | same |
| Cached prefix | 0 | 0 | 4,234 (read at 0.10× = 423 effective) |
| Cache write tokens | 0 | 4,234 (at 1.25× = 5,293 effective) | 0 |
| User message tokens | ~300 | ~380 (TRIAGE_SYSTEM moved here) | ~380 |
| Output tokens | ~80 | ~80 | ~80 |
| **Per-call cost** | **~$0.0006** | **~$0.0048** | **~$0.0009** |

After the cache warms, a migrated Haiku call is **~50% more expensive** than the original. Per Tomac's volume (180 runs/month × ~30 emails ≈ 5,400 triage calls/month), that's roughly $1.60 → $4.80/month. Small in absolute terms, but a real regression.

**Recommendation:** keep `haiku_triage` on the original path. The triage workload is exactly the kind that should bypass the static core — the classifier doesn't need investor doctrine to decide DEAL vs RECRUIT vs IGNORE.

### `sonnet_enrich` — small improvement, larger upside on quality

Sonnet 4.6: $3/M input, $15/M output.

| | Original | Migrated (cache hit) |
|---|---|---|
| System tokens | ~150 (uncached) | ~150 (in user_query) |
| Cached prefix | 0 | 4,234 (read at 0.10× = 423 effective) |
| User message | ~600 | ~750 |
| Output | ~200 | ~200 |
| **Per-call cost** | **~$0.0054** | **~$0.0061** |

Roughly flat on cost, but the static core's tracked-firms list and people context should improve `counterparty` and `recruiter` field accuracy on the JSON output. Worth migrating.

## 4. Test commands before swap

```bash
# 1. Smoke-test haiku_triage on a known email.
cd ~/cos-pipeline
python3 -c "
import sys
sys.path.insert(0, '_subscription/migrations')
from cos_gmail_mini_v2 import haiku_triage
sample = {
    'id': 'test-1',
    'from': 'jane@searchpartners.com',
    'subject': 'MD role at Tier 1 infra GP',
    'date': '2026-05-03',
    'body': 'Hi Yoni, confidential search for an MD, Power & Utilities at a top-tier GP. Open to chat?',
}
print(haiku_triage(sample))
"
# Expect: category=RECRUIT, confidence>=0.7

# 2. Smoke-test sonnet_enrich on the same email with category=RECRUIT.
python3 -c "
import sys
sys.path.insert(0, '_subscription/migrations')
from cos_gmail_mini_v2 import sonnet_enrich
sample = { ... same as above ... }
print(sonnet_enrich(sample, 'RECRUIT'))
"
# Expect: JSON with firm, role, recruiter, stage, action_summary fields populated.

# 3. Verify cache_telemetry.jsonl shows two distinct cache reads after the second call.
tail -4 ~/cos-pipeline/_subscription/cache_telemetry.jsonl
```

## 5. Expected cost impact (full pipeline, monthly)

Assuming 180 runs × ~30 emails/run = 5,400 triage calls + ~500 enrichment calls/month:

| | Original | Migrated (haiku stays) | Migrated (both swap) |
|---|---|---|---|
| Haiku triage | $1.60/mo | $1.60/mo | $4.80/mo |
| Sonnet enrich | $2.70/mo | $3.05/mo | $3.05/mo |
| **Total** | **$4.30/mo** | **$4.65/mo** | **$7.85/mo** |

Recommended path (`haiku stays`) is roughly cost-flat with a quality upside on enrichment.

## 6. Rollback plan

```bash
cd ~/cos-pipeline
git log --oneline cos_gmail_mini_v2.py | head -3
git checkout <PRE_SWAP_HASH> -- cos_gmail_mini_v2.py
git commit -m "rollback cos_gmail_mini_v2 migration"
```

Backup before swap:

```bash
cp ~/cos-pipeline/cos_gmail_mini_v2.py ~/cos-pipeline/cos_gmail_mini_v2.py.pre-migration-bak
```

## 7. The exact swap commands

### Option A — swap both functions (per the M5 instruction)

```bash
cp ~/cos-pipeline/cos_gmail_mini_v2.py ~/cos-pipeline/cos_gmail_mini_v2.py.pre-migration-bak && \
cp ~/cos-pipeline/_subscription/migrations/cos_gmail_mini_v2.draft.py ~/cos-pipeline/cos_gmail_mini_v2.py && \
cd ~/cos-pipeline && git add cos_gmail_mini_v2.py && \
git commit -m "cos_gmail_mini_v2: route haiku_triage and sonnet_enrich through cached static-core client"
```

### Option B — swap only sonnet_enrich (recommended; preserves Haiku cost profile)

This requires editing the draft to revert `haiku_triage` back to its original form before swapping. Easiest manual flow:

1. Open `~/cos-pipeline/_subscription/migrations/cos_gmail_mini_v2.draft.py`
2. Restore the original `haiku_triage` body from `~/cos-pipeline/cos_gmail_mini_v2.py` (lines 584-622)
3. Then run the swap commands from Option A

Or, leave the draft as-is (both swapped) and accept the modest Haiku cost increase as the price of architectural consistency. The choice is judgment, not code.
