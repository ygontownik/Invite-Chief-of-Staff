#!/usr/bin/env python3
"""
deal-system-compile.py
────────────────────────────────────────────────────────────
Compiles the firm's deal system (reads Deals/*.md + Excel profit models)
and syncs outputs to the dashboard server.

What it does (fast — all local, no Google APIs, ~3–5 sec end-to-end):
  1. python3 scripts/compile-dashboard.py  → dashboard/dashboard-data.json
  2. python3 scripts/cos-briefing.py       → dashboard/cos-briefing-latest.md
  3. Copy compiled JSON to ~/tomac-cove-pipeline/dashboard/deal-system-data.json
     (this is where cos-dashboard-fetch.py reads it to embed into the CoS dashboard)
  4. Copy briefing MD to ~/tomac-cove-pipeline/dashboard/deal-briefing-latest.md
  5. POST /refresh-deals  → fast HTML inject into Deal Pipeline Dashboard.html
  6. POST /warmup         → triggers CoS data re-fetch so dealPortfolio is embedded

Called by:
  - cos-dashboard-server.py  POST /compile-deals  (on-demand from dashboard)
  - cos-dashboard-server.py  _warmup_in_background()  (in parallel with every warmup)
  - ~/.claude/scheduled-tasks/tomac-deal-compile/SKILL.md  (daily scheduled task)
  - Direct:  python3 ~/tomac-cove-pipeline/deal-system-compile.py

Output paths:
  ~/tomac-cove-pipeline/dashboard/deal-system-data.json   ← read by cos-dashboard-fetch.py
  ~/tomac-cove-pipeline/dashboard/deal-briefing-latest.md ← for reference
"""
import json, shutil, subprocess, sys, time, urllib.request
from pathlib import Path

# New home layout (post-consolidation):
#   ~/dashboards/data/deals/           ← authoritative deal sources (YAML + xlsx)
#   ~/dashboards/routines/compile/     ← this script + compile-dashboard.py + cos-briefing.py
#   ~/dashboards/data/compiled/        ← JSON + briefing MD consumed by dashboards
_ROOT        = Path.home() / 'dashboards'            # ~/dashboards/ (hardcoded — symlink breaks __file__ inference)
_HERE        = _ROOT / 'routines' / 'compile'        # ~/dashboards/routines/compile/
DEAL_SYSTEM  = _ROOT / 'data' / 'deals'              # source-of-truth deal folders
COMPILED_DIR = _ROOT / 'data' / 'compiled'           # dashboard-consumed outputs

# compile-dashboard.py writes its intermediate products into data/compiled/
# (previously written into tomac-deal-system/dashboard/). Source and destination
# are identical now — no copy needed, but kept for back-compat if the scripts are
# invoked standalone.
DATA_SRC     = COMPILED_DIR / 'deal-system-data.json'
BRIEFING_SRC = COMPILED_DIR / 'deal-briefing-latest.md'
DATA_DST     = COMPILED_DIR / 'deal-system-data.json'
BRIEFING_DST = COMPILED_DIR / 'deal-briefing-latest.md'

SERVER_URL = 'http://localhost:7777'


def _post(path: str, timeout: int = 5) -> bool:
    try:
        urllib.request.urlopen(f'{SERVER_URL}{path}', data=b'', timeout=timeout)
        return True
    except Exception as e:
        print(f'  (server ping {path} failed: {e})')
        return False


def run_step(label: str, cmd: list, cwd=None) -> bool:
    print(f'→ {label}...', flush=True)
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if result.returncode != 0:
        print(f'  FAILED (exit {result.returncode})', file=sys.stderr)
        if result.stderr.strip():
            print(f'  {result.stderr.strip()[:300]}', file=sys.stderr)
        return False
    if result.stdout.strip():
        for line in result.stdout.strip().splitlines():
            print(f'  {line}')
    return True


# ── Cross-pollination overlay ──────────────────────────────────
# Fresh deal signal (transcripts + emails) lives in dashboard-data.json
# under the `tomac` array, written by cos-dashboard-fetch.py from Drive.
# Authoritative deal records live in deal-system-data.json under `deals`,
# written by compile-dashboard.py from data/deals/<ticker>/deal.md.
# Without an overlay, the markdown side ages while the transcript side
# ticks fresh — same deal looks alive on the Status page (consumes
# tomac[]) and dead on the React portfolio (consumes deal-system).
# This overlay is one-way: read tomac[] → augment deals[] in place.
# Never writes back to dashboard-data.json.
_DATE_RE = __import__('re').compile(r'^\d{4}-\d{2}-\d{2}$')

def _norm_name(s: str) -> str:
    """Lowercase + alphanumeric-only fingerprint for fuzzy name match."""
    return ''.join(c.lower() for c in (s or '') if c.isalnum())

def overlay_fresh_signals() -> int:
    """Read dashboard-data.json `tomac[]`; for each deal in
    deal-system-data.json `deals[]`, if a tomac entry matches by name or
    ticker (normalized, with substring fallback), update last_activity /
    last_updated when the tomac date is fresher (lexicographic ISO date
    compare) and copy `signalCount` / `freshSignal` into additive
    `_signal_count` / `_fresh_signal` fields. Returns count of overlaid
    deals. Silent no-op if either file is missing or unparseable."""
    dash_path = COMPILED_DIR / 'dashboard-data.json'
    deal_path = DATA_DST
    if not dash_path.exists() or not deal_path.exists():
        return 0
    try:
        dash = json.loads(dash_path.read_text())
        deal_doc = json.loads(deal_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f'  overlay: skipped — {e}', file=sys.stderr)
        return 0

    tomac = dash.get('tomac') or []
    if not isinstance(tomac, list):
        return 0
    by_name: dict = {}
    for t in tomac:
        if not isinstance(t, dict):
            continue
        n = _norm_name(t.get('name'))
        if n:
            by_name[n] = t
    if not by_name:
        return 0

    deals = deal_doc.get('deals') or []
    overlaid = 0
    for d in deals:
        if not isinstance(d, dict):
            continue
        # Try exact match on name → ticker → substring either direction.
        candidates = [d.get('name'), d.get('ticker'), d.get('id')]
        match = None
        for c in candidates:
            n = _norm_name(c)
            if n and n in by_name:
                match = by_name[n]
                break
        if match is None:
            for c in candidates:
                n = _norm_name(c)
                if not n:
                    continue
                for tk, tv in by_name.items():
                    if n in tk or tk in n:
                        match = tv
                        break
                if match:
                    break
        if match is None:
            continue

        latest = match.get('latestUpdate') or {}
        fresh_date = (latest.get('date') or '').strip()
        if _DATE_RE.match(fresh_date):
            cur_la = (d.get('last_activity') or '').strip()
            if not _DATE_RE.match(cur_la) or fresh_date > cur_la:
                d['last_activity'] = fresh_date
            cur_lu = (d.get('last_updated') or '').strip()
            if not _DATE_RE.match(cur_lu) or fresh_date > cur_lu:
                d['last_updated'] = fresh_date
        if 'signalCount' in match:
            try:
                d['_signal_count'] = int(match.get('signalCount') or 0)
            except (TypeError, ValueError):
                d['_signal_count'] = 0
        if 'freshSignal' in match:
            d['_fresh_signal'] = bool(match.get('freshSignal'))
        overlaid += 1

    if overlaid:
        deal_path.write_text(json.dumps(deal_doc, indent=2))
    return overlaid


# ── Manual overrides for deal tile fields ──────────────────────
# Three fields on each deal are user-editable (potential_partner,
# deck_url, model_url). Compile must (a) ensure they exist as null on
# every deal so the renderers can rely on the keys, and (b) overlay any
# values the user has entered via the dashboard UI. Override file lives
# under data/user-state/ so upstream sync never touches it.
DEAL_OVERRIDE_FIELDS = ('potential_partner', 'deck_url', 'model_url')
DEAL_OVERRIDE_PATH   = _ROOT / 'data' / 'user-state' / 'deal-overrides.json'

def apply_deal_overrides() -> int:
    """Ensure every deal carries the three override fields (default null);
    apply user-saved overrides from data/user-state/deal-overrides.json.
    Returns count of deals where any field was set non-null. Idempotent."""
    deal_path = DATA_DST
    if not deal_path.exists():
        return 0
    try:
        deal_doc = json.loads(deal_path.read_text())
    except (json.JSONDecodeError, OSError):
        return 0

    overrides: dict = {}
    if DEAL_OVERRIDE_PATH.exists():
        try:
            overrides = json.loads(DEAL_OVERRIDE_PATH.read_text()) or {}
        except (json.JSONDecodeError, OSError):
            overrides = {}

    deals = deal_doc.get('deals') or []
    set_count = 0
    for d in deals:
        if not isinstance(d, dict):
            continue
        # Default missing fields to null so renderers see consistent shape.
        for f in DEAL_OVERRIDE_FIELDS:
            if f not in d:
                d[f] = None
        # Apply user overrides keyed by ticker (lowercase) or id.
        keys = [str(d.get('ticker') or '').lower(), str(d.get('id') or '').lower()]
        ov = None
        for k in keys:
            if k and k in overrides and isinstance(overrides[k], dict):
                ov = overrides[k]
                break
        if ov:
            for f in DEAL_OVERRIDE_FIELDS:
                if f in ov:
                    d[f] = ov[f]
            if any(d.get(f) for f in DEAL_OVERRIDE_FIELDS):
                set_count += 1

    deal_path.write_text(json.dumps(deal_doc, indent=2))
    return set_count


def main():
    t0 = time.time()
    COMPILED_DIR.mkdir(parents=True, exist_ok=True)

    # ── Step 1: compile deal data ────────────────────────────────
    ok = run_step(
        'Compiling deal data',
        [sys.executable, str(_HERE / 'compile-dashboard.py')],
    )
    if not ok:
        sys.exit(1)

    # ── Step 2: generate CoS briefing ───────────────────────────
    run_step(
        'Generating deal briefing',
        [sys.executable, str(_HERE / 'cos-briefing.py')],
    )

    # Steps 3 (copy src→dst) is a no-op — the children now write directly to
    # COMPILED_DIR. Keep a soft existence check for observability.
    if not DATA_DST.exists():
        print(f'  WARNING: {DATA_DST} not found — compile-dashboard.py may have failed', file=sys.stderr)

    # ── Step 3.5: cross-pollinate fresh transcript/email signals ──
    # Read-only against dashboard-data.json; mutates deal-system-data.json
    # in place. Fixes the Cholla case where Status shows fresh and the
    # React portfolio shows stale because they're fed by different
    # compilers.
    print('→ Overlaying fresh signals from dashboard-data.json...', flush=True)
    n = overlay_fresh_signals()
    print(f'  overlaid {n} deal(s)' if n else '  no overlay (no matching tomac entries)')

    # ── Step 3.6: ensure schema defaults + apply user overrides ──
    # potential_partner, deck_url, model_url are user-editable via the
    # dashboard. This step both guarantees the keys exist on every deal
    # (so renderers don't crash) and re-applies any overrides the user
    # saved through POST /deal/override since the last compile.
    print('→ Applying deal-tile overrides (partner, deck, model)...', flush=True)
    n_ov = apply_deal_overrides()
    print(f'  {n_ov} deal(s) carry user overrides')

    # ── Step 4: push deal HTML inject ───────────────────────────
    print('→ Refreshing Deal Pipeline Dashboard...', flush=True)
    _post('/refresh-deals')

    # ── Step 5: kick warmup so CoS data embeds dealPortfolio ────
    # Non-blocking: server does this in background; we don't wait.
    _post('/warmup', timeout=3)

    # ── Summary ─────────────────────────────────────────────────
    elapsed = time.time() - t0
    try:
        data = json.loads(DATA_DST.read_text())
        p = data.get('portfolio', {})
        deals = data.get('deals', [])
        crit  = p.get('total_critical_open', 0)
        total = p.get('total_open_actions', 0)
        mid   = sum(d.get('tcip_profit', {}).get('total', {}).get('mid', 0) for d in deals)
        print(
            f'\n✅  Deal compile done in {elapsed:.1f}s\n'
            f'   {p.get("total_deals", 0)} deals · avg health {p.get("avg_health", 0)}/100 · '
            f'{total} actions ({crit} critical) · portfolio mid ${mid/1e6:.0f}M\n'
            f'   {" ".join(d.get("ticker","?") + "=" + str(d.get("health","?")) for d in deals)}',
            flush=True,
        )
    except Exception:
        print(f'✅  Deal compile done in {elapsed:.1f}s', flush=True)


if __name__ == '__main__':
    main()
