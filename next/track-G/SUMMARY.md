# Track G — costs/quota dashboard tile — SUMMARY

Completed by Phase 2 sub-agent, run 2 (2026-05-03). Persisted by parent.

## Files delivered

- `~/cos-pipeline/next/track-G/costs_aggregator.py` — pure-stdlib aggregator (`aggregate_costs`, `format_for_tile`, runnable `__main__`)
- `~/cos-pipeline/next/track-G/fetch.delta.md` — REPLACE/WITH patches for `cos-dashboard-fetch.py` (Track C merges first; anchors are stable comment strings)
- `~/cos-pipeline/next/track-G/cos-dashboard-tile.html.delta.md` — tile HTML + `renderCostsTile()` JS for `cos-dashboard.template.html` (NOT live `.html`)
- `~/cos-pipeline/tests/test_costs_aggregator.py` — 5 unittests, all pass

## Key decisions

1. **Schema reality check.** Brief asks for `pass_name`/`routine_name` rollups, but `_model_router.py::_record_cost` writes `task_type` only (verified live). Aggregator reads `pass_name`/`routine_name` if present (forward-compat), else falls back to `task_type` for both.
2. **Subscription rows count toward `lines_read` but contribute $0** — matches MODEL_ROUTER.md line 115.
3. **Lookback filter on filename first** (`YYYY-MM-DD.jsonl`), per-row `ts` defense-in-depth.
4. **Missing-dir = empty result, never raises.** Tile renders harmlessly on day-zero or new tenant.
5. **Light UI only** — uses `var(--paper, #fdfaf3)` and existing slate/navy palette per global rule.
6. **No coupling to Track C order.** fetch.delta.md anchors on stable strings, not line numbers.

## Deferrals

- Soft/hard cap visualization (needs stable per-routine cap accessor)
- Subscription "saved vs API" comparison (needs api-equivalent estimate per subscription row)
- Live `cos-dashboard.html` edit (template only, per hard rules)
- Multi-tenant side-by-side render (`COS_TENANT` env, default `tomac`)

## Verification (executed)

```
python3 ~/cos-pipeline/tests/test_costs_aggregator.py
→ Ran 5 tests in 0.003s — OK

python3 ~/cos-pipeline/next/track-G/costs_aggregator.py
→ {"total_usd": 0.0, ...}  (tomac costs/ dir doesn't exist yet — expected)
```

After fetch.delta.md is applied:
```
python3 ~/cos-pipeline/cos-dashboard-fetch.py --dry-run \
  | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin).get('costs'), indent=2))"
```

NO ACTIONS REQUIRED
