# Migration Plan — `podcast_transcribe.py`

**Status:** draft (not swapped). Live file `~/cos-pipeline/podcast_transcribe.py` is untouched.
**Draft location:** `~/cos-pipeline/_subscription/migrations/podcast_transcribe.draft.py`

---

## 1. Diff summary

One function modified: `generate_memo()` (lines ~373-424 in live file).

- **Removed:** raw `requests.post` to `api.anthropic.com/v1/messages` with manual `prompt-caching-2024-07-31` beta header.
- **Removed:** the inline `body` dict with two-block user content (MEMO_PREAMBLE cached + dynamic uncached).
- **Added:** lazy import of `cached_client.complete` from `_subscription/`.
- **Added:** call to `complete(user_query=MEMO_PREAMBLE, source_content=dynamic, model="claude-sonnet-4-6", max_tokens=2048)`.
- **Adapted:** `log_usage` call to read fields from `result["usage"]` (Anthropic SDK Usage object) instead of raw `resp_json`.

No other functions touched. `clean_memo`, `_extract_one_liner`, `MEMO_PREAMBLE`, `MEMO_DYNAMIC_TEMPLATE` all unchanged.

## 2. What's actually different at the wire

| Aspect | Original | Migrated |
|---|---|---|
| HTTP path | raw `requests.post` | Anthropic Python SDK |
| System prompt | empty (no `system=`) | 4,234-token static core (sections 1-5 of `system_prompt_v1.md`) |
| Cache strategy | one breakpoint inside user message (MEMO_PREAMBLE ~700 tokens cached) | two breakpoints in `system=` array (4,234-token core + 1,638-token bundle), both cached |
| MEMO_PREAMBLE | cached in user message | uncached in `user_query` slot (~700 tokens per call, fresh) |
| Bundle context | none | injected via `system_prompt_v1.md` section 6 |
| API key resolution | `ANTHROPIC_API_KEY` env via `_secrets.load_secret` (same) | identical (`cached_client._load_api_key` does the same lookup) |

## 3. Test commands before swap

```bash
# 1. Dry-run the draft against a known transcript file you already have a memo for.
#    Save the original memo first as a baseline.
cd ~/cos-pipeline
python3 -c "
import sys, datetime
sys.path.insert(0, '_subscription/migrations')
from podcast_transcribe import generate_memo
# Use a small transcript fixture from the Drive folder, ~3-4k words.
transcript = open('/path/to/known_transcript.txt').read()
memo, summary = generate_memo('Catalyst', 'Test episode',
                              datetime.datetime(2026, 5, 1), transcript)
print('SUMMARY:', summary)
print('MEMO LENGTH:', len(memo))
print('---')
print(memo[:2000])
"

# 2. Diff the new memo against the baseline. Look for:
#    - Section headings preserved (ONE-SENTENCE SUMMARY, THE CORE ARGUMENT, etc.)
#    - No markdown leakage (clean_memo handles this)
#    - Bullet character is `•` not `-`
#    - Substantive content quality (subjective; eyeball)

# 3. Verify cache_telemetry.jsonl shows expected pattern after 2 sequential runs:
tail -2 ~/cos-pipeline/_subscription/cache_telemetry.jsonl
# First run: cache_creation > 0, cache_read = 0 (or = previously-cached static core).
# Second run: cache_read >= 4,000.
```

## 4. Expected cost impact

Per-call cost from MEASUREMENT_REPORT.md (`podcast_snippet` × Sonnet 4.6):
- Cached: ~$0.0264/call (post-warm-up)
- Uncached baseline: ~$0.0334/call
- Savings: ~21%

Original script's per-call cost (estimated, since the only cached portion was MEMO_PREAMBLE ~700 tokens):
- ~$0.025/call (Sonnet 4.6, 40k transcript + 2k output, with ~700 token cache hit)

**Net cost change:** within ~5% noise band. The migration is **not a cost win** for podcast_transcribe — it's an architectural unification win. The static core's value here is consistency: every memo now reflects the firm-list and classifier context, which should reduce false positives in the briefing pipeline downstream.

## 5. Rollback plan

Pre-migration backup is implicit — `git log` of `~/cos-pipeline/podcast_transcribe.py` will show the prior version. To rollback after a swap:

```bash
cd ~/cos-pipeline
# Find the commit immediately before the swap
git log --oneline podcast_transcribe.py | head -3
# Hard-restore from that commit (replace HASH)
git checkout <PRE_SWAP_HASH> -- podcast_transcribe.py
git commit -m "rollback podcast_transcribe migration"
```

Or, if the swap was a single commit, `git revert <SWAP_HASH>`.

For belt-and-suspenders, before the swap, take an explicit local copy:

```bash
cp ~/cos-pipeline/podcast_transcribe.py ~/cos-pipeline/podcast_transcribe.py.pre-migration-bak
```

## 6. The exact 1-command swap

Once you've reviewed the draft and run the test commands above:

```bash
cp ~/cos-pipeline/podcast_transcribe.py ~/cos-pipeline/podcast_transcribe.py.pre-migration-bak && \
cp ~/cos-pipeline/_subscription/migrations/podcast_transcribe.draft.py ~/cos-pipeline/podcast_transcribe.py && \
cd ~/cos-pipeline && git add podcast_transcribe.py && \
git commit -m "podcast_transcribe: route memo generation through cached static-core client"
```

Note: this also leaves `podcast_transcribe.py.pre-migration-bak` in the working tree (untracked) for instant rollback.
