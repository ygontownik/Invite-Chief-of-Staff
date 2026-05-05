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
        # 2026-05-04: also scan followUps[] and awaitingExternal[] for items
        # whose `who` / `counterparty` mentions the deal — take max addedDate.
        # This auto-derives last_activity from the broader signal set, so a
        # deal touched by an email/transcript yesterday updates even if the
        # tomac doc hasn't been re-parsed yet.
        deal_tokens = [t for t in (
            (d.get('name') or '').lower(),
            (d.get('ticker') or '').lower(),
            (d.get('id') or '').lower(),
        ) if t]
        signal_dates = []
        for fu in (dash.get('followUps') or []):
            who = (fu.get('who') or '').lower()
            what = (fu.get('what') or '').lower()
            if any(t in who or t in what for t in deal_tokens):
                ad = (fu.get('addedDate') or '').strip()
                if _DATE_RE.match(ad):
                    signal_dates.append(ad)
        for ae in (dash.get('awaitingExternal') or []):
            cp = (ae.get('counterparty') or '').lower()
            content = (ae.get('content') or '').lower()
            if any(t in cp or t in content for t in deal_tokens):
                ad = (ae.get('addedDate') or '').strip()
                if _DATE_RE.match(ad):
                    signal_dates.append(ad)
        signal_dates.sort()
        signal_max = signal_dates[-1] if signal_dates else ''
        # Prefer the most-recent of (latestUpdate, signal_max) over deal.md
        # hand-edited last_activity.
        candidates = [x for x in (fresh_date, signal_max) if _DATE_RE.match(x)]
        if candidates:
            best = max(candidates)
            cur_la = (d.get('last_activity') or '').strip()
            if not _DATE_RE.match(cur_la) or best > cur_la:
                d['last_activity'] = best
            cur_lu = (d.get('last_updated') or '').strip()
            if not _DATE_RE.match(cur_lu) or best > cur_lu:
                d['last_updated'] = best
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


def _assert_no_orphan_deal_dirs() -> None:
    """G4 silent assertion (codified 2026-05-04): every data/deals/<TICKER>/
    directory MUST contain deal.md, actions.md, LPs.md, TERMS.md. Missing
    files signal a half-created deal which would render incompletely on the
    dashboard. Logs a single line per orphan to stderr and continues — does
    NOT block compile, since compile may itself be the recovery path."""
    if not DEAL_SYSTEM.exists():
        return
    required = {'deal.md', 'actions.md', 'LPs.md', 'TERMS.md'}
    for d in sorted(DEAL_SYSTEM.iterdir()):
        if not d.is_dir() or d.name.startswith('_'):
            continue
        present = {f.name for f in d.iterdir() if f.is_file()}
        missing = required - present
        if missing:
            print(
                f'  G4 ORPHAN data/deals/{d.name}/ — missing: {sorted(missing)}',
                file=sys.stderr,
            )


def _gc_stale_tombstones() -> None:
    """R2 silent garbage-collect (codified 2026-05-04): tombstoned items in
    data/user-state/deletions.json that no longer match any current
    awaitingExternal[]/followUps[]/personal-items[] for ≥90 days are moved
    to an archive. Keeps deletions.json from growing unboundedly. Silent."""
    import datetime as _dt
    deletions_path = _ROOT / 'data' / 'user-state' / 'deletions.json'
    archive_path   = _ROOT / 'data' / 'user-state' / 'deletions-archive.json'
    dash_path      = COMPILED_DIR / 'dashboard-data.json'
    personal_path  = _ROOT / 'data' / 'user-state' / 'personal-items.json'
    if not deletions_path.exists():
        return
    try:
        dels = json.loads(deletions_path.read_text())
    except Exception:
        return
    items = dels.get('deletions') or []
    if not items:
        return
    # Build a Set of all currently-live IDs across consumers of the tombstone.
    live_ids = set()
    try:
        if dash_path.exists():
            d = json.loads(dash_path.read_text())
            for arr in ('awaitingExternal', 'followUps', 'dealIntel', 'originationInbox'):
                for it in (d.get(arr) or []):
                    iid = it.get('id')
                    if iid: live_ids.add(iid)
    except Exception:
        pass
    try:
        if personal_path.exists():
            p = json.loads(personal_path.read_text())
            for it in (p.get('items') or []):
                # personal-items may compute id client-side; attempt _djb2 if present
                iid = it.get('id')
                if iid: live_ids.add(iid)
    except Exception:
        pass

    now = _dt.datetime.now(_dt.UTC)
    keep = []
    archive = []
    try:
        existing_archive = json.loads(archive_path.read_text()) if archive_path.exists() else {'deletions': []}
    except Exception:
        existing_archive = {'deletions': []}
    for entry in items:
        iid = entry.get('id')
        if not iid: continue
        if iid in live_ids:
            keep.append(entry)
            continue
        # No live match — check age. Format: 2026-05-04T16:30:00Z
        try:
            ts_str = (entry.get('deleted_at') or '').rstrip('Z')
            ts = _dt.datetime.fromisoformat(ts_str).replace(tzinfo=_dt.UTC)
            age_days = (now - ts).days
        except Exception:
            keep.append(entry)
            continue
        if age_days >= 90:
            archive.append(entry)
        else:
            keep.append(entry)
    if archive:
        existing_archive['deletions'].extend(archive)
        archive_path.write_text(json.dumps(existing_archive, indent=2))
        dels['deletions'] = keep
        deletions_path.write_text(json.dumps(dels, indent=2))
        print(f'  R2 GC: {len(archive)} stale tombstone(s) archived', file=sys.stderr)


def _emit_g5_candidates_sidecar() -> None:
    """G5 silent inverse audit (codified 2026-05-04): firms surfacing in
    dashboard-data.json (followUps[]/awaitingExternal[]) ≥3 times in the last
    14 days that don't appear in any curated config land in a
    `candidatesAwaitingTriage:` sidecar at data/compiled/g5-candidates.json.
    Inspected by analyst passes. Silent — no user-facing surface."""
    import datetime as _dt, re as _re
    dash_path = COMPILED_DIR / 'dashboard-data.json'
    deal_cfg  = _ROOT / 'config' / 'deal-config.yaml'
    recr_cfg  = _ROOT / 'config' / 'recruit-config.yaml'
    out_path  = COMPILED_DIR / 'g5-candidates.json'
    if not dash_path.exists():
        return
    try:
        d = json.loads(dash_path.read_text())
    except Exception:
        return

    # Build the "known firms" set from configs + deal directories.
    # Plus alias needles (so "gideon powell" → Cholla resolves), team members
    # (so "Mark Saxe" doesn't surface as a candidate firm), and peer-firm
    # denylist (so "LS Power" / "Stonepeak" don't either).
    known = set()
    fc_path = Path.home() / 'cos-pipeline-config' / 'firm_context.yaml'
    try:
        import yaml as _yaml
        if deal_cfg.exists():
            cfg = _yaml.safe_load(deal_cfg.read_text()) or {}
            for sec in ('liveDeals','dealOrigination','capitalRaisingAdvisors','prospectiveInvestors','dormantInvestors'):
                for r in (cfg.get(sec) or []):
                    n = (r.get('name') or '').strip().lower()
                    if n: known.add(n)
        if recr_cfg.exists():
            rcfg = _yaml.safe_load(recr_cfg.read_text()) or {}
            for bucket in ('inDiscussion','waitingToHear','doIChase','dormant'):
                for r in (rcfg.get('priorityTargets', {}).get(bucket) or []):
                    n = (r.get('name') or '').strip().lower()
                    if n: known.add(n)
            for r in (rcfg.get('recruiters') or []):
                n = (r.get('firm') or '').strip().lower()
                if n: known.add(n)
        # Alias needles + peer firms + team members from firm_context.yaml
        if fc_path.exists():
            fc = _yaml.safe_load(fc_path.read_text()) or {}
            for entry in (fc.get('counterparty_aliases') or []):
                for needle in (entry.get('needles') or []):
                    if needle: known.add(needle.lower())
                if entry.get('canonical'):
                    known.add(entry['canonical'].lower())
            for peer in (fc.get('peer_firms') or []):
                if peer: known.add(peer.lower())
            # Team members — full names + first names
            for tm in (fc.get('team') or []):
                nm = (tm.get('name') or '').lower().strip()
                if nm:
                    known.add(nm)
                    first = nm.split()[0] if nm else ''
                    if first: known.add(first)
            principal = (fc.get('principal') or {}).get('name') or ''
            if principal:
                known.add(principal.lower())
                p_first = principal.lower().split()[0] if principal else ''
                if p_first: known.add(p_first)
    except Exception:
        pass
    if DEAL_SYSTEM.exists():
        for ddir in DEAL_SYSTEM.iterdir():
            if ddir.is_dir() and not ddir.name.startswith('_'):
                known.add(ddir.name.lower())
    # Pull the broader peer/GP denylist from cos-dashboard-fetch.py (it's
    # the most maintained list of peer firms; firm_context.yaml peer_firms
    # is a curated subset).
    try:
        import sys as _sys
        _fetch_dir = str(Path(__file__).parent.parent if (Path(__file__).parent.name == 'compile') else Path(__file__).parent)
        # cos-dashboard-fetch.py lives at ~/cos-pipeline/
        _cp_root = Path.home() / 'cos-pipeline'
        if _cp_root.exists() and str(_cp_root) not in _sys.path:
            _sys.path.insert(0, str(_cp_root))
        # Module name has a hyphen — load by file path.
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location('_cdf', _cp_root / 'cos-dashboard-fetch.py')
        if _spec and _spec.loader:
            try:
                _cdf = _ilu.module_from_spec(_spec)
                _spec.loader.exec_module(_cdf)
                for p in (getattr(_cdf, '_PEER_GP_DENYLIST', set()) or []):
                    known.add(p.lower())
            except Exception:
                pass
    except Exception:
        pass

    # Count firm mentions in followUps[] + awaitingExternal[] within last 14 days.
    today = _dt.date.today()
    cutoff = (today - _dt.timedelta(days=14)).isoformat()
    counts = {}
    for fu in (d.get('followUps') or []):
        if (fu.get('addedDate') or '') < cutoff: continue
        who = (fu.get('who') or '').strip()
        if not who: continue
        # Take the firm part — first part before "/" or "—"
        firm = _re.split(r'\s*[—–\/|,]\s*', who)[0].strip()
        if firm:
            counts[firm.lower()] = counts.get(firm.lower(), 0) + 1
    for ae in (d.get('awaitingExternal') or []):
        if (ae.get('addedDate') or '') < cutoff: continue
        cp = (ae.get('counterparty') or '').strip()
        if not cp: continue
        firm = _re.split(r'\s*[—–\/|,]\s*', cp)[0].strip()
        if firm:
            counts[firm.lower()] = counts.get(firm.lower(), 0) + 1

    # Service-provider denylist — counsel/accounting firms etc. that appear in
    # transcripts but aren't deals/LPs. Extend as new ones surface.
    SERVICE_DENY_SUBSTRINGS = (
        'legal group', 'law group', 'pllc', 'attorneys', 'counsel',
        'accountants', 'tax services',
    )

    def _looks_like_person(s: str) -> bool:
        """Heuristic: 2-word string with no firm keyword and both words start
        with a capital letter (in the original) → probably a person name."""
        parts = s.split()
        if len(parts) != 2: return False
        firm_kws = ('capital', 'partners', 'management', 'investments', 'group',
                    'corp', 'inc', 'llc', 'holdings', 'ventures', 'fund',
                    'equity', 'bank', 'sandler', 'sachs', 'finance', 'energy',
                    'industries', 'infrastructure')
        if any(k in s for k in firm_kws): return False
        return True

    # Candidates: firms with ≥3 mentions, NOT in known set, NOT obviously a person.
    candidates = []
    for firm, n in counts.items():
        if n < 3: continue
        # Substring-match against the known set (alias needles are substrings)
        if any(k in firm or firm in k for k in known if k):
            continue
        # Service providers (legal, accounting) are not dashboard candidates.
        if any(s in firm for s in SERVICE_DENY_SUBSTRINGS):
            continue
        # Skip single-word names that look like a person (heuristic).
        if len(firm.split()) == 1 and len(firm) < 12:
            continue
        if _looks_like_person(firm):
            continue
        candidates.append({'firm': firm, 'mentions_14d': n})
    candidates.sort(key=lambda x: -x['mentions_14d'])
    payload = {
        'updated_at': _dt.datetime.now(_dt.UTC).isoformat(timespec='seconds').replace('+00:00','Z'),
        'window_days': 14,
        'rule': 'G5 inverse audit — firms mentioned >= 3x but not in any curated config',
        'candidates': candidates,
    }
    out_path.write_text(json.dumps(payload, indent=2))
    if candidates:
        print(f'  G5 candidates: {len(candidates)} firm(s) need triage → {out_path.name}', file=sys.stderr)


def _compute_deal_readthroughs() -> int:
    """U2 silent join (codified 2026-05-04): for each deal in
    deal-system-data.json, scan dashboard-data.json > marketCommentary
    for items that mention the deal — by canonical name, alias needle,
    sector keyword, geography keyword. Attach matches as
    `recent_readthroughs[]` on each deal so deal cards (and the daily
    briefing) can call out market intel relevant to that deal.

    Today only reads from marketCommentary. Future: extend to read
    from podcast memos and Jefferies/GS research summaries once those
    pipelines emit structured intel records (see U2 deferred TODO in
    dash_corrections.md). Returns count of (deal, readthrough) pairs
    written. Silent — no user-visible warnings."""
    import re as _re
    dash_path = COMPILED_DIR / 'dashboard-data.json'
    deal_path = DATA_DST
    if not dash_path.exists() or not deal_path.exists():
        return 0
    try:
        dash = json.loads(dash_path.read_text())
        deal_doc = json.loads(deal_path.read_text())
    except Exception:
        return 0

    mc = dash.get('marketCommentary')
    if isinstance(mc, list):
        mc = mc[0] if mc else None
    if not isinstance(mc, dict):
        return 0
    mc_date = mc.get('date') or ''

    # Build alias map: canonical → list of needle substrings (lowercased).
    fc_path = Path.home() / 'cos-pipeline-config' / 'firm_context.yaml'
    canonical_to_needles: dict = {}
    try:
        import yaml as _yaml
        if fc_path.exists():
            fc = _yaml.safe_load(fc_path.read_text()) or {}
            for entry in (fc.get('counterparty_aliases') or []):
                canon = (entry.get('canonical') or '').strip()
                if not canon: continue
                canonical_to_needles[canon.lower()] = [
                    (n or '').lower() for n in (entry.get('needles') or [])
                ] + [canon.lower()]
    except Exception:
        pass

    def _tokens_for_deal(d: dict) -> dict:
        """Return matching tokens for a deal: name/ticker/id substrings,
        sector keywords, geography keywords, alias needles, and tagline
        keywords. Tagline often carries deal-distinctive terms (e.g.
        'hyperscale', 'salt-cavern') absent from the structured fields."""
        name = (d.get('name') or '').lower()
        ticker = (d.get('ticker') or '').lower()
        did = (d.get('id') or '').lower()
        sector = (d.get('sector') or '').lower()
        geo = (d.get('geography') or '').lower()
        tagline = (d.get('tagline') or '').lower()
        # Try to match this deal to a canonical via its name/id
        alias_needles = []
        for canon_low, needles in canonical_to_needles.items():
            if canon_low in name or any(n in name for n in needles if n):
                alias_needles = needles
                break
            if did and (canon_low == did or did in canon_low):
                alias_needles = needles
                break
        sector_tokens = [t for t in _re.split(r'[\s/,&\-]+', sector) if len(t) > 3]
        geo_tokens = [t for t in _re.split(r'[\s/,&\-(\)]+', geo) if len(t) > 2]
        # Tagline tokens — strip stopwords and short words
        tagline_tokens = [t for t in _re.split(r'[\s/,&\-—.;:]+', tagline) if len(t) >= 5]
        return {
            'name_tokens':  [t for t in (name, ticker, did) if t],
            'alias_needles': [n for n in alias_needles if n and len(n) >= 3],
            'sector_tokens': sector_tokens,
            'geo_tokens': geo_tokens,
            'tagline_tokens': tagline_tokens,
        }

    # Build a denylist of generic tokens that match too broadly. Anything
    # that fires on >=20% of a typical market brief is too generic.
    DENY_TOKENS = {'energy', 'power', 'gas', 'storage', 'data', 'infra', 'infrastructure',
                   'capital', 'partners', 'national', 'national)', 'multi-site',
                   'commercial', 'fund', 'corp', 'inc', 'llc', 'global', 'group',
                   'phone', 'phoenix', 'angeles', 'vegas', 'cities', 'usa', 'us',
                   'land', 'site', 'sites', 'project', 'platform', 'portfolio',
                   'phase', 'phase1', 'phase2', 'btm', 'utility', 'tier',
                   'operating', 'origin', 'originated', 'multi', 'natural',
                   'center', 'centers', 'pipeline', 'pipelines', 'demand',
                   'asset', 'assets', 'market', 'markets', 'company', 'firm',
                   'deal', 'deals', 'private', 'public', 'state', 'federal',
                   # Generic business / financial words that surface in
                   # taglines and pollute readthrough matches.
                   'product', 'products', 'committed', 'commit', 'commits',
                   'equity', 'debt', 'investment', 'investor', 'investors',
                   'strategy', 'strategic', 'operator', 'operators', 'agreed',
                   'agreement', 'bridge', 'leverage', 'returns', 'thesis',
                   'sourcing', 'diligence', 'advisory', 'memo', 'closing',
                   'pre-fid', 'post-fid', 'capex', 'opex', 'ebitda', 'revenue',
                   'income', 'value', 'valuation', 'pricing', 'priced',
                   'building', 'building-up', 'build-up', 'rolling', 'rolling-up'}

    def _filter_tokens(tokens):
        return [t for t in tokens if t and t not in DENY_TOKENS and len(t) >= 4]

    pairs = 0
    for d in (deal_doc.get('deals') or []):
        if not isinstance(d, dict):
            continue
        toks = _tokens_for_deal(d)
        # Prioritize alias needles (firm-name precision) over sector/geo tokens
        # (broader, prone to false positives).
        primary = set(_filter_tokens(toks['name_tokens'] + toks['alias_needles']))
        secondary = set(_filter_tokens(toks['sector_tokens'] + toks['geo_tokens'] + toks['tagline_tokens']))

        readthroughs = []
        seen_items = set()
        for sec in (mc.get('sections') or []):
            sec_title = sec.get('title') or ''
            for item in (sec.get('items') or []):
                if not isinstance(item, str): continue
                item_low = item.lower()
                # Match logic:
                #   primary hit (firm name / alias)        → high confidence
                #   single secondary hit ≥6 chars          → medium confidence
                #   ≥2 secondary hits                      → medium confidence
                # Single short secondary hit alone = too noisy; require ≥6 chars
                # for solo matches.
                primary_hits = [t for t in primary if t in item_low]
                secondary_hits = [t for t in secondary if t in item_low]
                solo_strong = any(len(t) >= 6 for t in secondary_hits)
                if not primary_hits and len(secondary_hits) < 2 and not solo_strong:
                    continue
                # Dedup the same item appearing under multiple sections (rare)
                key = item[:100]
                if key in seen_items: continue
                seen_items.add(key)
                match_reason = (
                    'firm/alias match: ' + ', '.join(sorted(primary_hits)[:3])
                    if primary_hits
                    else 'sector/geo match: ' + ', '.join(sorted(secondary_hits)[:3])
                )
                readthroughs.append({
                    'source': 'marketCommentary',
                    'date': mc_date,
                    'section': sec_title,
                    'text': item if len(item) <= 280 else item[:277] + '...',
                    'match_reason': match_reason,
                    'confidence': 'high' if primary_hits else 'medium',
                })
        # Cap per deal to keep dashboard surfaces lean
        readthroughs.sort(key=lambda r: (r['confidence'] != 'high', r.get('section')))
        d['recent_readthroughs'] = readthroughs[:5]
        pairs += len(readthroughs[:5])

    deal_path.write_text(json.dumps(deal_doc, indent=2, ensure_ascii=False))
    if pairs:
        print(f'  readthroughs: {pairs} (deal × intel-item) pair(s) computed', flush=True)
    return pairs


def _compute_deal_logs() -> int:
    """V1 silent auto-log (codified 2026-05-04): append per-deal entries
    to `data/deals/<TICKER>/log.json` from extraction signal. NO manual
    process — every relevant followUp / awaitingExternal / dealIntel
    item is auto-tagged to the deal it touches and appended to that
    deal's chronological log.

    Idempotency via stable djb2 id per (who, what, date). Already-logged
    ids are tracked in the same file so re-runs don't duplicate.

    Output: `recent_log[]` on each deal in deal-system-data.json (last
    20 entries, newest first). The briefing handler renders the latest
    1-3 entries per deal as the "Recent Activity" feed.

    Returns count of new entries appended."""
    import re as _re, hashlib as _hl, datetime as _dt
    dash_path = COMPILED_DIR / 'dashboard-data.json'
    deal_path = DATA_DST
    if not dash_path.exists() or not deal_path.exists():
        return 0
    if not DEAL_SYSTEM.exists():
        return 0
    try:
        dash = json.loads(dash_path.read_text())
        deal_doc = json.loads(deal_path.read_text())
    except Exception:
        return 0

    def _djb2(s: str) -> str:
        h = 5381
        for c in s:
            h = ((h << 5) + h) ^ ord(c)
        return format(h & 0xFFFFFFFF, '08x')

    # Build alias-needles map for each deal-system deal (so we know which
    # signal items match which deal).
    fc_path = Path.home() / 'cos-pipeline-config-tomac' / 'firm_context.yaml'
    if not fc_path.exists():
        fc_path = Path.home() / 'cos-pipeline-config' / 'firm_context.yaml'
    canonical_to_needles: dict = {}
    try:
        import yaml as _yaml
        if fc_path.exists():
            fc = _yaml.safe_load(fc_path.read_text()) or {}
            for entry in (fc.get('counterparty_aliases') or []):
                canon = (entry.get('canonical') or '').strip()
                if not canon: continue
                needles = [(n or '').lower() for n in (entry.get('needles') or [])]
                needles.append(canon.lower())
                canonical_to_needles[canon.lower()] = needles
    except Exception:
        pass

    def _deal_tokens(d: dict) -> list:
        """Return all lowercase token substrings to match an item to this deal."""
        tokens = []
        for f in ('name', 'ticker', 'id'):
            v = (d.get(f) or '').lower()
            if v: tokens.append(v)
        # Find canonical match → use all needles
        n = (d.get('name') or '').lower()
        nid = (d.get('id') or '').lower()
        for canon_low, needles in canonical_to_needles.items():
            if canon_low in n or canon_low == nid or any(nd and nd in n for nd in needles):
                tokens.extend(needles)
                break
        # Filter out tokens that are too short or too generic
        return [t for t in set(tokens) if t and len(t) >= 3]

    today = _dt.date.today().isoformat()
    cutoff_30d = (_dt.date.today() - _dt.timedelta(days=30)).isoformat()
    new_entries_total = 0

    for d in (deal_doc.get('deals') or []):
        if not isinstance(d, dict):
            continue
        did = d.get('id') or ''
        if not did:
            continue
        deal_dir = DEAL_SYSTEM / did
        if not deal_dir.is_dir():
            continue
        log_path = deal_dir / 'log.json'

        # Load existing log + seen-id set
        try:
            existing = json.loads(log_path.read_text()) if log_path.exists() else {}
        except Exception:
            existing = {}
        entries = existing.get('entries') or []
        seen = {e.get('id') for e in entries if e.get('id')}

        tokens = _deal_tokens(d)

        # Scan signal sources
        candidate_pairs = []
        for fu in (dash.get('followUps') or []):
            if (fu.get('what') or '').startswith('[RESOLVED]'): continue
            who = (fu.get('who') or '')
            what = (fu.get('what') or '')
            text_low = (who + ' ' + what).lower()
            if not any(t in text_low for t in tokens): continue
            added = (fu.get('addedDate') or fu.get('when') or '')[:10]
            if not _re.match(r'^\d{4}-\d{2}-\d{2}$', added): continue
            if added < cutoff_30d: continue
            iid = _djb2('fu|' + who + '|' + what[:80] + '|' + added)
            if iid in seen: continue
            candidate_pairs.append({
                'id': iid, 'date': added, 'source': 'followup',
                'who': who[:80], 'what': what[:280],
            })

        for ae in (dash.get('awaitingExternal') or []):
            cp = (ae.get('counterparty') or '')
            content = (ae.get('content') or '')
            text_low = (cp + ' ' + content).lower()
            if not any(t in text_low for t in tokens): continue
            added = (ae.get('addedDate') or '')[:10]
            if not _re.match(r'^\d{4}-\d{2}-\d{2}$', added): continue
            if added < cutoff_30d: continue
            iid = _djb2('ae|' + cp + '|' + content[:80] + '|' + added)
            if iid in seen: continue
            candidate_pairs.append({
                'id': iid, 'date': added, 'source': 'awaitingExternal',
                'who': cp[:80], 'what': content[:280],
            })

        for it in ((dash.get('dealIntel') or []) + (dash.get('originationInbox') or [])):
            content = (it.get('content') or '')
            ctx = (it.get('context') or '')
            text_low = (content + ' ' + ctx).lower()
            if not any(t in text_low for t in tokens): continue
            added = (it.get('addedDate') or '')[:10]
            if not _re.match(r'^\d{4}-\d{2}-\d{2}$', added): continue
            if added < cutoff_30d: continue
            iid = _djb2('intel|' + ctx[:40] + '|' + content[:80] + '|' + added)
            if iid in seen: continue
            candidate_pairs.append({
                'id': iid, 'date': added, 'source': 'intel',
                'who': ctx[:80], 'what': content[:280],
            })

        if candidate_pairs:
            # Append + cap rolling window at 200 entries
            entries.extend(candidate_pairs)
            entries.sort(key=lambda e: e.get('date',''), reverse=True)
            entries = entries[:200]
            existing['deal_id'] = did
            existing['entries'] = entries
            existing['updated_at'] = _dt.datetime.now(_dt.UTC).isoformat(timespec='seconds').replace('+00:00','Z')
            log_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
            new_entries_total += len(candidate_pairs)

        # Surface recent log on the deal record (top 5 by date desc)
        d['recent_log'] = sorted(
            entries, key=lambda e: e.get('date',''), reverse=True
        )[:5]

    deal_doc['deals'] = [d for d in (deal_doc.get('deals') or []) if isinstance(d, dict)]
    deal_path.write_text(json.dumps(deal_doc, indent=2, ensure_ascii=False))
    if new_entries_total:
        print(f'  V1 deal-log: appended {new_entries_total} new entries across deals', flush=True)
    return new_entries_total


def main():
    t0 = time.time()
    COMPILED_DIR.mkdir(parents=True, exist_ok=True)

    # ── Step 0: silent invariants (G4 orphan dirs, R2 stale-tombstone GC,
    # G5 inverse-audit candidates sidecar). Codified 2026-05-04. Each runs
    # silently — no user-facing alerts. Findings land in stderr or sidecar
    # files for analyst-pass review.
    _assert_no_orphan_deal_dirs()
    _gc_stale_tombstones()

    # ── Step 1: compile deal data ────────────────────────────────
    ok = run_step(
        'Compiling deal data',
        [sys.executable, str(_HERE / 'compile-dashboard.py')],
    )
    if not ok:
        sys.exit(1)

    # G5 runs after Step 1 so dashboard-data.json is fresh.
    _emit_g5_candidates_sidecar()

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

    # ── Step 3.5b: compute deal readthroughs from marketCommentary
    # (codified 2026-05-04 — U2 generic rule). Silent — writes
    # `recent_readthroughs[]` array onto each deal in deal-system-data.json.
    print('→ Computing deal readthroughs from market intel...', flush=True)
    _compute_deal_readthroughs()

    # ── Step 3.5c: auto-append per-deal activity log from extraction
    # signal (codified 2026-05-04 — V1 generic rule). Silent — writes
    # entries to data/deals/<TICKER>/log.json + recent_log[] onto each
    # deal record. NO manual log maintenance.
    print('→ Appending per-deal activity log from extraction signal...', flush=True)
    _compute_deal_logs()

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
