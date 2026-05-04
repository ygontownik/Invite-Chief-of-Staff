# Track G — fetch.delta.md

Patches to `~/cos-pipeline/cos-dashboard-fetch.py` (live path) to add a top-level
`costs` key to the assembled `dashboard-data.json` (which is what `/data` serves).

## Merge order with Track C

**Track C (server/fetch refactor) lands first.** Track C is producing
`cos-dashboard-fetch.py.next`. This Track G delta describes line ranges in the
**current live** `cos-dashboard-fetch.py` (last inspected: rev with
`STATE_PATH` at line 26 and the `live_data = { ... }` dict closing on line 2096
followed by deal-portfolio embed at lines 2098–2111).

When merging, apply the equivalent change against whichever `cos-dashboard-fetch.py`
is current after Track C lands — the anchor strings are stable across both:
the `# ── Embed deal system portfolio` comment and the `merged = {**state, **live_data}`
line. Anchor on those, not the line numbers.

---

## Patch 1: import the aggregator (top of file, with other imports)

**REPLACE** (around line 26, after `STATE_PATH` is defined):

```python
STATE_PATH = _ROOT / 'data' / 'compiled' / 'dashboard-data.json'
```

**WITH**:

```python
STATE_PATH = _ROOT / 'data' / 'compiled' / 'dashboard-data.json'

# Track G — costs/quota tile aggregator. Pure-stdlib, no I/O at import time.
import importlib.util as _ilu
_costs_mod_path = Path(os.path.expanduser('~/cos-pipeline/next/track-G/costs_aggregator.py'))
if _costs_mod_path.exists():
    _spec = _ilu.spec_from_file_location('costs_aggregator', _costs_mod_path)
    _costs_mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_costs_mod)
else:
    _costs_mod = None
```

(When Track G graduates out of `next/`, replace with a normal `from costs_aggregator import ...`.)

## Patch 2: embed `costs` into `live_data` before the merge

**INSERT BEFORE** the comment block at line 2113:

```python
    # Merge: live data wins over stale cached values; curated fields from state are preserved.
```

**INSERT THIS BLOCK**:

```python
    # ── Embed costs/quota rollup (Track G) ────────────────────────────────────
    # Reads ~/cos-pipeline/data-<tenant>/costs/*.jsonl. Pure local I/O, ~1ms.
    # Tile shape consumed by renderCostsTile(data) in cos-dashboard.template.html.
    if _costs_mod is not None:
        try:
            _tenant = os.environ.get('COS_TENANT', 'tomac')
            _agg = _costs_mod.aggregate_costs(_tenant, lookback_days=30)
            live_data['costs'] = _costs_mod.format_for_tile(_agg, top_n=5)
            print(f'cos-dashboard-fetch: costs tile — '
                  f'${_agg["total_usd"]:.2f} over {_agg["lines_read"]} calls '
                  f'({_agg["jsonl_files_seen"]} files)', file=sys.stderr)
        except Exception as _e:
            print(f'cos-dashboard-fetch: costs tile embed failed: {_e}', file=sys.stderr)
            live_data['costs'] = {
                'summary': 'cost data unavailable', 'totalUsd': 0.0, 'lookbackDays': 30,
                'topModels': [], 'topPasses': [], 'topRoutines': [], 'dailyChart': [],
                'filesSeen': 0, 'linesRead': 0,
            }
```

## Verification after merge

```bash
# Dry-run the fetch and confirm the costs key is present
python3 ~/cos-pipeline/cos-dashboard-fetch.py --dry-run | python3 -c \
  "import json,sys; d=json.load(sys.stdin); print(json.dumps(d.get('costs'), indent=2))"
```

Expected: a dict with `summary`, `topModels`, etc. — empty rollups are fine
until the router begins writing to `~/cos-pipeline/data-tomac/costs/`.

## Notes

- **No live file modified by this track.** The patches above are documented for
  the maintainer (or Track C follow-up) to apply.
- The aggregator never raises; the try/except is belt-and-suspenders.
- Tenant is sourced from `COS_TENANT` env var with a `tomac` default — matches
  the convention used elsewhere in the pipeline.
