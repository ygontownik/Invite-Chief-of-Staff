#!/usr/bin/env python3
"""
cos-dashboard-refresh.py  —  FAST PATH (~100ms)

Reads pre-fetched data from ~/docs/dashboard-data.json and injects it into the
dashboard HTML. Does NOT call any Google APIs.

The slow Google API work happens in cos-dashboard-fetch.py, which runs in the
background (every 20 min via server thread + POST /warmup + scheduled tasks).

If the cache is missing or older than STALE_THRESHOLD_MIN, this script
triggers a background fetch and uses whatever cached data is available
(falling back to empty data only if no cache exists at all).

── TENANT CONFIG ─────────────────────────────────────────────────────
This script is tenant-agnostic. All firm/principal labels, follow-up
cleanup rules (self-ref names, who/what normalizers, blocklist), and
recruiting normalizers are loaded at runtime from:
  - firm_context.yaml          (principal/firm/team/owner_whitelist)
  - <config_dir>/config/dashboard-cleanup.yaml
                               (cleanup dictionaries; missing = no-op)
Both resolve via _firm_context._find_config_dir() so a new subscriber
with their own ~/cos-pipeline-config-<slug>/ inherits the same code path
without forking. The legacy deal-tile data key in the rendered DATA
dict is derived from `tenant_slug` to preserve the existing frontend
contract (cos-dashboard.template.html reads DATA[<slug>]); a rename to
a non-slug-keyed contract will follow the deal-system rename in a later
release.
"""
import json, subprocess, sys
from datetime import datetime, timedelta
from pathlib import Path

# ── Tenant config (firm_context.yaml + dashboard-cleanup.yaml) ────────
# All tenant-specific values flow from the config dir at module load.
# Falls back to safe empty defaults if either file is missing so a
# fresh-tenant install never crashes — it just gets no cleanup until
# the subscriber populates dashboard-cleanup.yaml.
_PIPELINE_DIR = Path.home() / 'cos-pipeline'
if str(_PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_DIR))
try:
    import _firm_context as _fc  # type: ignore
    _CTX             = _fc.load_firm_context() or {}
    _CONFIG_DIR      = _fc._find_config_dir()
except Exception:
    _CTX             = {}
    _CONFIG_DIR      = _PIPELINE_DIR

_PRINCIPAL_FIRST = ((_CTX.get('principal') or {}).get('name') or '').split()[0].lower() or 'principal'
_PRINCIPAL_FULL  = ((_CTX.get('principal') or {}).get('name') or '').strip().lower()
_FIRM_NAME       = ((_CTX.get('firm') or {}).get('name') or '').strip()
_FIRM_SHORT      = ((_CTX.get('firm') or {}).get('short_name') or '').strip() or _FIRM_NAME
_TENANT_SLUG     = (_CTX.get('tenant_slug') or '').strip().lower()
# Frontend data-contract key for the deal/dealflow tile. The browser
# bundle still reads DATA[<this key>] (see cos-dashboard.template.html).
# Held as a constant so this module references no literal tenant slug.
_DEAL_TILE_KEY   = _TENANT_SLUG or 'deals'

def _load_cleanup_cfg() -> dict:
    """Load tenant follow-up + recruiting normalization rules.
    Returns {} when the file is missing or unreadable — pipeline keeps
    running with no-op cleanup."""
    p = _CONFIG_DIR / 'config' / 'dashboard-cleanup.yaml'
    if not p.exists():
        return {}
    try:
        import yaml  # type: ignore
        return yaml.safe_load(p.read_text()) or {}
    except Exception:
        return {}

_CLEANUP_CFG = _load_cleanup_cfg()


def _merge_registered_deals(pipeline_deals: list, registered_deals: list) -> list:
    """Inject dealPortfolio.deals entries (rich, from deal-system-data.json) into
    the pipeline-doc deal tile when absent, filtering out Auto-promoted ghosts.
    Deduplication: exact id match, exact lowercased name match, or first-token match.
    """
    import re as _re
    def _first_token(s: str) -> str:
        return (_re.split(r'[\s/(]', s.strip())[0] or '').lower()
    clean = [d for d in pipeline_deals if 'Auto' not in d.get('stage', '')]
    if not registered_deals:
        return clean
    existing_ids    = {d.get('id', '') for d in clean if d.get('id')}
    existing_names  = {(d.get('name') or '').lower() for d in clean}
    existing_tokens = {_first_token(d.get('name', '')) for d in clean}
    for rd in registered_deals:
        rid  = rd.get('id', '')
        rnam = (rd.get('name') or '').lower()
        rtok = _first_token(rd.get('name', ''))
        if (rid not in existing_ids
                and rnam not in existing_names
                and (not rtok or rtok not in existing_tokens)):
            clean.append(rd)
    return clean


_HERE               = Path(__file__).parent    # ~/dashboards/app/
_ROOT               = _HERE.parent                       # ~/dashboards/
# HTML strip P2 paths (Track 1.7):
#  - .template.html — clean source, committed, no injected data block.
#  - .rendered.html — data-injected output, gitignored, served by the dashboard.
#  - .html         — legacy path; still kept fresh during transition as a rollback target.
#                     Will be retired once 7 days of clean .rendered.html operation pass.
DASHBOARD_TEMPLATE  = _HERE / 'templates' / 'cos-dashboard.template.html'
DASHBOARD_RENDERED  = _HERE / 'templates' / 'cos-dashboard.rendered.html'
DASHBOARD_PATH      = _HERE / 'templates' / 'cos-dashboard.html'
STATE_PATH          = _ROOT / 'data' / 'compiled' / 'dashboard-data.json'
FETCH_SCRIPT        = _HERE / 'cos-dashboard-fetch.py'
FUNDRAISING_PATH    = _ROOT / 'data' / 'user-state' / 'fundraising.json'
PROPOSED_LEARNINGS_PATH = _ROOT / 'data' / 'compiled' / 'proposed-learnings.jsonl'
REJECTED_LEARNINGS_PATH = _ROOT / 'data' / 'user-state' / 'rejected-learnings.json'
DEFERRED_LEARNINGS_PATH = _ROOT / 'data' / 'user-state' / 'deferred-learnings.json'
RULES_COMPLIANCE_PATH   = _ROOT / 'data' / 'compiled' / 'rules-compliance.json'
STALE_THRESHOLD_MIN = 90   # trigger background re-fetch if cache older than this

_FUNDRAISING_BUCKETS = ('direct_lps', 'gp_stakes', 'placement_agents', 'strategic')

def _load_user_fundraising():
    """User-state buckets (data/user-state/fundraising.json) — preferred
    source of truth for the fundraising block. Compile output is treated as
    fallback only. Returns None if user-state file is missing or unreadable."""
    if not FUNDRAISING_PATH.exists():
        return None
    try:
        return json.loads(FUNDRAISING_PATH.read_text())
    except Exception:
        return None

def _flatten_fundraising_to_lpdata(fr):
    """Backward-compat: flatten the four buckets into the legacy lpData[] shape."""
    out = []
    if not fr:
        return out
    for b in _FUNDRAISING_BUCKETS:
        for entry in fr.get(b, []) or []:
            firm = (entry.get('firm') or '').strip()
            name = (entry.get('name') or '').strip()
            display = f'{firm} / {name}' if firm and name else (firm or name or '')
            out.append({
                'name':        display,
                'firm':        firm,
                'contact':     name,
                'status':      entry.get('status', ''),
                'statusColor': '',
                'fit':         '',
                'notes':       entry.get('notes', ''),
                'approach':    '',
                'source':      '',
                'path':        entry.get('path', b),
                'last_contact':entry.get('last_contact', ''),
                'lastTouch':   entry.get('last_contact', ''),  # legacy alias
                'id':          entry.get('id', ''),
            })
    return out

# ── Proposed-learnings (capture-loop candidates) ───────────────
def _learning_id(snippet: str) -> str:
    """Stable 8-char hex ID from snippet text. Survives the next ingest so
    accept/reject/defer state in user-state/*.json keeps matching even if
    the producer re-emits the same candidate."""
    h = 5381
    for ch in (snippet or ''):
        h = ((h * 33) + ord(ch)) & 0xFFFFFFFF
    return f'{h:08x}'

def _is_substantive_learning(snippet: str) -> bool:
    """Drop the candidates that aren't reviewable rules.

    The producer (run_learning_capture_scan in dash-state-hook.py) matches
    'always X' / 'never X' / 'going forward, X' patterns and truncates at
    the first period — which catches `_claude_dispatch.py` mid-filename,
    inline-code endings, comment fragments, etc. Until the producer is
    tightened (out-of-scope for this tile), filter aggressively here so
    the user sees only candidates worth a yes/no decision.
    """
    s = (snippet or '').strip()
    if len(s) < 35:                                # too short to be a rule
        return False
    if len(s) > 240:                                # likely a paragraph blob, not a rule
        return False
    # Embedded newlines or code markers → not a rule, a paste fragment.
    if '\n' in s or '//' in s or '**:' in s or '*/' in s:
        return False
    # Mid-word truncations: ends with "X." where X is lowercase AND the
    # snippet contains no spaces in the last 8 chars → code-like.
    if s.endswith('.') and ' ' not in s[-8:]:
        return False
    # Transcript-fluff signals — verbal-tic patterns Claude never emits in
    # rule shape. These are almost always Otter/iMessage capture noise.
    fluff_signals = (', you know', 'I mean', 'kind of', 'sort of',
                     "I was interested", "I'm interested",
                     'gigawatt site', 'as I was saying', 'you know what')
    low = s.lower()
    if any(sig.lower() in low for sig in fluff_signals):
        return False
    # Must start with a recognizable rule shape — discard mid-sentence pickups.
    first_token = s.split()[0].lower() if s.split() else ''
    if first_token not in {'always', 'never', 'going', 'from', 'don\'t',
                           'do', 'use', 'prefer', 'require', 'avoid',
                           'the', 'we', 'all', 'every', 'no'}:
        return False
    # Require the first letter capitalized OR a clear imperative-rule shape.
    # Otter transcripts rarely capitalize; Claude rule-emissions almost always do.
    # Allow lowercase only when the snippet is unmistakably a rule (contains "—"
    # or starts with "always"/"never" followed by code-fence backticks).
    if s[0].islower() and not (' — ' in s or '`' in s[:20]):
        return False
    return True

def _load_rules_compliance() -> dict:
    """Read ~/dashboards/data/compiled/rules-compliance.json (written by
    ~/cos-pipeline/tools/rules_audit.py --apply). Returns a thin summary
    for the dashboard tile: counts + top 10 violations + top 10 paper-rule
    gaps. Returns {} on any failure — this is a soft surface, never a
    hard dependency.

    Schema served to the template:
      {
        ranAt: ISO string,
        totalRules: int,
        counts: {enforced, violated, warned, paper_rule, informational, deprecated},
        overallStatus: 'pass'|'warn'|'fail',
        violations: [{id, code, title, summary}, ...],   # top 10 by severity
        paperRules: [{id, code, title, enforced_by}, ...] # top 10 by recency
      }
    """
    if not RULES_COMPLIANCE_PATH.exists():
        return {}
    try:
        data = json.loads(RULES_COMPLIANCE_PATH.read_text())
    except Exception:
        return {}
    rules = data.get('rules') or []
    violations = [
        {
            'id':       r.get('id'),
            'code':     r.get('rule_code'),
            'title':    (r.get('title') or '')[:120],
            'summary':  (r.get('check_summary') or '')[:200],
            'module':   r.get('check_module'),
        }
        for r in rules if r.get('classification') == 'violated'
    ][:10]
    paper_rules = [
        {
            'id':          r.get('id'),
            'code':        r.get('rule_code'),
            'title':       (r.get('title') or '')[:120],
            'enforcedBy':  (r.get('enforced_by_field') or '')[:160],
        }
        for r in rules if r.get('classification') == 'paper_rule'
    ][:10]
    orphans = [
        {
            'filename':  o.get('filename'),
            'rule_ref':  o.get('rule_ref'),
            'status':    o.get('status'),
            'summary':   (o.get('summary') or '')[:200],
        }
        for o in (data.get('orphan_checks') or [])
    ][:10]
    return {
        'ranAt':         data.get('ran_at'),
        'totalRules':    data.get('total_rules', 0),
        'counts':        data.get('counts', {}),
        'overallStatus': data.get('overall_status', 'pass'),
        'nextActions':   data.get('next_actions', []),
        'violations':    violations,
        'paperRules':    paper_rules,
        'orphans':       orphans,
    }


def _load_proposed_learnings(today: str, cap: int = 20) -> list[dict]:
    """Read proposed-learnings.jsonl, de-dupe by snippet, filter out rejected
    and currently-deferred candidates, score by recency, return up to `cap`
    for the dashboard tile. Returns [] on any failure — this is a soft
    surface, never a hard dependency.
    """
    if not PROPOSED_LEARNINGS_PATH.exists():
        return []
    rejected = set()
    deferred = {}
    if REJECTED_LEARNINGS_PATH.exists():
        try:
            rejected = set(json.loads(REJECTED_LEARNINGS_PATH.read_text()).get('ids', []))
        except Exception:
            pass
    if DEFERRED_LEARNINGS_PATH.exists():
        try:
            deferred = json.loads(DEFERRED_LEARNINGS_PATH.read_text()).get('until', {})
        except Exception:
            pass
    # Read all → keep most-recent per snippet
    latest_by_snippet = {}
    try:
        with open(PROPOSED_LEARNINGS_PATH) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                snip = (rec.get('snippet') or '').strip()
                if not snip:
                    continue
                cap_at = rec.get('captured_at') or ''
                prev = latest_by_snippet.get(snip)
                if not prev or (cap_at and cap_at > (prev.get('captured_at') or '')):
                    latest_by_snippet[snip] = rec
    except Exception as e:
        print(f'Warning: could not read proposed-learnings.jsonl: {e}', file=sys.stderr)
        return []
    out = []
    for snip, rec in latest_by_snippet.items():
        if not _is_substantive_learning(snip):
            continue
        lid = _learning_id(snip)
        if lid in rejected:
            continue
        if lid in deferred and deferred[lid] > today:
            continue
        out.append({
            'id':           lid,
            'snippet':      snip,
            'capturedAt':   rec.get('captured_at', ''),
            'sessionId':    rec.get('session_id', ''),
        })
    out.sort(key=lambda r: r.get('capturedAt') or '', reverse=True)
    return out[:cap]

# ── Cache age helpers ──────────────────────────────────────
def cache_age_minutes(state: dict) -> float | None:
    """Return age of cache in minutes, or None if no fetchedAt."""
    fetched_at = state.get('fetchedAt', '')
    if not fetched_at:
        return None
    try:
        ft = datetime.fromisoformat(fetched_at)
        return (datetime.now() - ft).total_seconds() / 60
    except Exception:
        return None

def age_label(minutes: float | None) -> str:
    if minutes is None:
        return 'no cache'
    if minutes < 1:
        return 'just now'
    if minutes < 60:
        return f'{int(minutes)}m ago'
    return f'{int(minutes/60)}h {int(minutes%60)}m ago'

# ── HTML injection ─────────────────────────────────────────
def _check_inline_js_syntax(html: str) -> str | None:
    """Run `node --check` against every inline <script> block in `html`.

    Returns None if all blocks parse, or a printable error string identifying
    the first failing block. If `node` is not on PATH, returns None
    (fail-open — the dashboard refresh must not require a node install).

    Why: cos-dashboard.template.html is one giant <script>. A single bad
    escape (e.g. `'won\\'t'` instead of `'won\'t'`) throws SyntaxError at
    parse time, killing the React mount on every dashboard route while
    curl still returns HTTP 200 + the full payload. This gate catches the
    failure at render time instead of at user-eyeballs time.
    """
    import re, tempfile, shutil
    if not shutil.which('node'):
        return None
    scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
    for idx, src in enumerate(scripts):
        # Skip JSON-LD / non-JS script blocks; they have no parse risk for us.
        if not src.strip():
            continue
        with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False) as f:
            f.write(src)
            tmp_path = f.name
        try:
            r = subprocess.run(
                ['node', '--check', tmp_path],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode != 0:
                # node prints `<path>:<line>` then a caret line then the
                # SyntaxError. Trim absolute path to just the basename for
                # readability; keep the rest verbatim.
                err = r.stderr.replace(tmp_path, f'<inline-script-{idx}>')
                return err.strip()
        except subprocess.TimeoutExpired:
            return f'node --check timed out on inline script {idx}'
        finally:
            try: Path(tmp_path).unlink()
            except Exception: pass
    return None


def find_data_block(html: str):
    """Return (start_idx, end_idx) of the full 'const DATA = { ... }; // __END_DATA__' block.

    Uses the __END_DATA__ sentinel to find the true end — not just the closing brace.
    This prevents accumulation: each refresh correctly replaces the entire previous block
    (including any previously written sentinel text) rather than leaving stale copies behind.

    If multiple sentinels have accumulated (from earlier bug), scans past all of them.
    Falls back to brace counting if no sentinel exists (first-run or legacy file).
    """
    SENTINEL = '__END_DATA__'
    start = html.find('const DATA = ')
    if start == -1:
        return None, None

    # ── Strategy 1: sentinel-based (fast, correct after first sentinel-aware write) ──
    # Find the opening brace so we know where the data starts (we need 'start' for the
    # replace, but the end position is determined by the sentinel).
    brace_pos = html.find('{', start)
    if brace_pos == -1:
        return None, None

    # Walk forward past all accumulated sentinel copies to find the true end.
    search_from = brace_pos
    last_sentinel_end = None
    while True:
        pos = html.find(SENTINEL, search_from)
        if pos == -1:
            break
        candidate_end = pos + len(SENTINEL)
        # Only accept this sentinel if it's plausibly close to the data block
        # (within 10 chars of closing brace or another sentinel).
        last_sentinel_end = candidate_end
        search_from = candidate_end
        # Check whether the very next sentinel immediately follows (accumulated copies):
        # peek ahead — if the next few chars contain another sentinel start, keep going.
        gap = html[candidate_end:candidate_end + 30]
        if '; //' not in gap and SENTINEL not in gap:
            break  # this is the last one

    if last_sentinel_end is not None:
        return start, last_sentinel_end

    # ── Strategy 2: brace counting fallback (legacy / no sentinel yet) ──
    depth, i, in_str, sc = 0, brace_pos, False, None
    while i < len(html):
        c = html[i]
        if in_str:
            if c == '\\' and i + 1 < len(html):
                i += 2
                continue
            if c == sc:
                in_str = False
        else:
            if c in ('"', "'", '`'):
                in_str = True
                sc = c
            elif c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    return start, i + 1
        i += 1
    return None, None

# ── Follow-up cleanup (runs every refresh to survive pipeline re-runs) ────────
def clean_follow_ups(fus: list, cutoff_days: int = 5, dismissed_set: set = None) -> tuple[list, int]:
    """
    Remove dirty items from followUps every refresh so pipeline re-runs can't
    re-introduce them. Rules applied in order:

    0. Dismissed set    — drop items user explicitly dismissed via the UI
                          (stable who|what[:40] key)
    1. Date filter      — drop items whose due date is older than cutoff_days
                          (default 5d, tightened 2026-05-21 from 14d after
                          audit found 45 of 108 visible items >7d stale and
                          zero in the 14–30d band — the entire stale tail
                          sat in the 5–14d window)
    2. Self-ref filter  — drop where who matches the principal/team (exact)
                          (config: follow_ups.self_ref_names)
    3. Who-junk filter  — drop where who matches a junk substring
                          ("Speaker N", "Unnamed", "Unidentified", etc.)
                          (config: follow_ups.who_junk_patterns)
    4. Blocklist        — specific who+what patterns we never want
                          (config: follow_ups.blocklist)
    5. Dedup            — keep first occurrence of each
                          (who.lower, what[:60].lower) key

    All tenant-specific patterns (self-ref names, who-junk substrings,
    who/what normalize tables, blocklist) live in
    <config_dir>/config/dashboard-cleanup.yaml and are loaded at module
    import (_CLEANUP_CFG). Missing file = no-op cleanup.
    """
    from datetime import date as _date

    today = _date.today()
    cutoff = today - timedelta(days=cutoff_days)
    _dismissed = dismissed_set or set()

    fu_cfg = (_CLEANUP_CFG.get('follow_ups') or {}) if isinstance(_CLEANUP_CFG, dict) else {}

    # ── 0. Dismissed set — stable who|what[:40] key ──
    def _stable_key(f):
        return (f.get('who', '').lower().strip() + '|' +
                (f.get('what', '') or '').lower().strip()[:40])

    def _is_dismissed(f):
        return _stable_key(f) in _dismissed

    # ── 1. Date filter ──
    def _too_old(f):
        due = f.get('due', '')
        if not due:
            return False   # no due date = keep
        try:
            d = _date.fromisoformat(due)
            return d < cutoff
        except ValueError:
            return False

    # ── 2. Principal / self-ref drop ──
    # Pull self-ref names from cleanup config; fall back to {principal_first,
    # principal_full} from firm_context so a fresh tenant install with no
    # cleanup yaml still drops the obvious self-references.
    _self_ref_cfg = fu_cfg.get('self_ref_names') or []
    _self_refs = {str(n).lower().strip() for n in _self_ref_cfg if str(n).strip()}
    if _PRINCIPAL_FIRST and _PRINCIPAL_FIRST != 'principal':
        _self_refs.add(_PRINCIPAL_FIRST)
    if _PRINCIPAL_FULL:
        _self_refs.add(_PRINCIPAL_FULL)

    def _is_principal_self_ref(f):
        who = f.get('who', '').strip().lower()
        return who in _self_refs

    # ── 2b. Who-junk substring filter (config-driven) ──
    # Drops followups where who matches placeholder/junk text — e.g.
    # "Speaker 1 (unidentified)", "Unnamed ex-Centerbridge PM",
    # "EU power markets lawyer (to be identified)". Added 2026-05-21.
    _who_junk = [str(p).lower().strip()
                 for p in (fu_cfg.get('who_junk_patterns') or [])
                 if str(p).strip()]

    def _is_junk_who(f):
        who = (f.get('who', '') or '').strip().lower()
        if not who:
            return False
        return any(pat in who for pat in _who_junk)

    # ── 3a. Name normalizer (config-driven) ──
    # Each entry: {match: <who_substr>, replace: <canonical>}
    _who_norm = [(str(e.get('match','')).lower(), str(e.get('replace','')))
                 for e in (fu_cfg.get('who_normalize') or [])
                 if isinstance(e, dict) and e.get('match')]
    # Each entry: {who: <substr|empty>, match: <what_substr>, replace: <new>}
    _what_norm = [(str(e.get('who','')).lower(), str(e.get('match','')).lower(),
                   str(e.get('replace','')))
                  for e in (fu_cfg.get('what_normalize') or [])
                  if isinstance(e, dict) and e.get('match')]

    def _normalize(f):
        f = dict(f)
        who_l = f.get('who', '').lower().strip()
        for pat, new_who in _who_norm:
            if pat and pat in who_l:
                f['who'] = new_who
                who_l = new_who.lower()
                break
        what_l = f.get('what', '').lower().strip()
        for w_pat, a_pat, new_what in _what_norm:
            if (not w_pat or w_pat in who_l) and (a_pat in what_l):
                f['what'] = new_what
                break
        return f

    # ── 3. Blocklist (config-driven) ──
    # Each entry: {who: <substr>, what: <substr>, exact?: bool}.
    _blocklist = []
    for e in (fu_cfg.get('blocklist') or []):
        if not isinstance(e, dict):
            continue
        _blocklist.append((
            str(e.get('who','')).lower(),
            str(e.get('what','')).lower(),
            bool(e.get('exact', False)),
        ))

    def _is_blocklisted(f):
        who  = f.get('who',  '').lower().strip()
        what = f.get('what', '').lower().strip()
        for w_pat, a_pat, exact in _blocklist:
            who_ok  = (not w_pat) or (w_pat in who)
            what_ok = (what == a_pat) if exact else ((not a_pat) or (a_pat in what))
            if who_ok and what_ok:
                return True
        return False

    # ── 4. Dedup — keep first occurrence per (who, what[:60]) key ──
    seen = set()
    def _dedup_key(f):
        return (f.get('who','').lower().strip(), f.get('what','').lower().strip()[:60])

    kept = []
    removed = 0
    for f in fus:
        if _is_dismissed(f):
            removed += 1; continue
        if _too_old(f):
            removed += 1; continue
        if _is_principal_self_ref(f) and not f.get('_manual'):
            removed += 1; continue
        if _is_junk_who(f) and not f.get('_manual'):
            removed += 1; continue
        if _is_blocklisted(f):
            removed += 1; continue
        f = _normalize(f)          # clean names/text in-place before dedup
        k = _dedup_key(f)
        if k in seen:
            removed += 1; continue
        seen.add(k)
        kept.append(f)

    return kept, removed


# ── Recruiting normalizer (runs on every inject, survives fetch cycle) ─────────
def clean_recruiting(rec_active: list) -> list:
    """Normalize firm names, contacts, and lastAction text for recruiting entries.

    All firm/contact/last-action/next normalization tables live in
    <config_dir>/config/dashboard-cleanup.yaml :: recruiting.* — missing
    file = no-op (entries pass through untouched)."""
    rec_cfg = (_CLEANUP_CFG.get('recruiting') or {}) if isinstance(_CLEANUP_CFG, dict) else {}
    FIRM_NORM        = dict(rec_cfg.get('firm_normalize')        or {})
    CONTACT_NORM     = dict(rec_cfg.get('contact_normalize')     or {})
    LAST_ACTION_NORM = dict(rec_cfg.get('last_action_normalize') or {})
    NEXT_NORM        = dict(rec_cfg.get('next_normalize')        or {})
    out = []
    for r in rec_active:
        r = dict(r)
        name = r.get('name', '')
        # Firm name
        if name in FIRM_NORM:
            r['name'] = FIRM_NORM[name]
            name = r['name']
        # Contact
        c = r.get('contact', '')
        if c in CONTACT_NORM:
            r['contact'] = CONTACT_NORM[c]
        # lastAction text (only if value is raw note — don't overwrite clean dates/texts)
        la = r.get('lastAction', '')
        orig_name_for_la = next((k for k in LAST_ACTION_NORM if k in r.get('_orig_name', name)), None)
        if not orig_name_for_la:
            orig_name_for_la = next((k for k in LAST_ACTION_NORM if k == name or name.lower() == k.lower()), None)
        if orig_name_for_la and la and la.upper() == la:  # still ALL_CAPS = raw note
            r['lastAction'] = LAST_ACTION_NORM[orig_name_for_la]
        elif orig_name_for_la and la and la.lower().strip() in ('4', 'reached out', 'reach out', 'blackstone checkin',
                                                                  'manish checking on this', 'role dependent on deal getting done, 2 guys joining, will be after that. they never backfilled digital lead as deals are poor performers.',
                                                                  'he was checking with doug; per manish, role would be approved by senior to ben and partner alongside him, vp too junior',
                                                                  'returns march 30th, reach out then'):
            r['lastAction'] = LAST_ACTION_NORM[orig_name_for_la]
        # next text
        if name in NEXT_NORM and r.get('next', '').lower().startswith('ask about'):
            r['next'] = NEXT_NORM[name]
        out.append(r)
    return out


# ── Main ──────────────────────────────────────────────────
def assemble_data():
    """Load cached state and build the dashboard DATA dict.

    Extracted from main() as part of the HTML strip P2 refactor (see
    HTML_STRIP_RUNBOOK.md). This keeps the data-assembly logic in one
    place so the template/rendered split helper can call it directly
    without a subprocess hop and without duplicating ~120 lines.

    Returns (data, state) — `data` is the JSON-injectable dict; `state`
    is the cleaned cache (callers use it for staleness reporting and
    other downstream metadata).
    """
    # ── Load cache ──
    state = {}
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH) as f:
                state = json.load(f)
        except Exception as e:
            print(f'Warning: could not read state file: {e}', file=sys.stderr)

    age = cache_age_minutes(state)

    # ── Trigger background re-fetch if stale ──
    if age is None or age > STALE_THRESHOLD_MIN:
        reason = 'no cache' if age is None else f'cache is {int(age)}m old'
        print(f'Cache stale ({reason}) — triggering background fetch...')
        subprocess.Popen(
            [sys.executable, str(FETCH_SCRIPT)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # If we have no state at all, wait for the fetch to finish
        if age is None:
            print('No cache exists — waiting for initial fetch...')
            try:
                subprocess.run(
                    [sys.executable, str(FETCH_SCRIPT)],
                    timeout=60, check=True,
                )
                with open(STATE_PATH) as f:
                    state = json.load(f)
                age = 0.0
            except Exception as e:
                print(f'Initial fetch failed: {e}', file=sys.stderr)
                sys.exit(1)

    # ── Clean recruiting (normalizes names/contacts on every inject) ──
    raw_rec = state.get('recruiting', {}).get('active', [])
    clean_rec = clean_recruiting(raw_rec)
    if 'recruiting' not in state:
        state['recruiting'] = {}
    state['recruiting']['active'] = clean_rec

    # ── Filter email queue by server-side dismissed IDs ──
    dismissed_email_ids = set(state.get('_dismissedEmailIds', []))
    if dismissed_email_ids:
        raw_eq = state.get('emailQueue', [])
        state['emailQueue'] = [e for e in raw_eq if e.get('id') not in dismissed_email_ids]

    # ── Clean follow-ups (survives every pipeline re-run) ──
    raw_fus = state.get('followUps', [])
    dismissed_set = set(state.get('_dismissedFollowUps', []))
    clean_fus, n_removed = clean_follow_ups(raw_fus, dismissed_set=dismissed_set)
    if n_removed:
        state['followUps'] = clean_fus
        # Write cleaned data back so the JSON file itself stays tidy
        try:
            with open(STATE_PATH, 'w') as fh:
                json.dump(state, fh, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f'Warning: could not write cleaned state: {e}', file=sys.stderr)
        print(f'Cleanup: removed {n_removed} followUp item(s) (old/dupe/blocked) → {len(clean_fus)} remain')

    # ── Build DATA object from cache ──
    now = datetime.now()
    fetch_time_str = state.get('fetchedAt', '')
    if fetch_time_str:
        try:
            ft = datetime.fromisoformat(fetch_time_str)
            generated_at = (
                ft.strftime('%a %b %-d · %-I:%M%p').replace('AM','a').replace('PM','p')
                + f' ({age_label(age)})'
            )
        except Exception:
            generated_at = now.strftime('%a %b %-d %Y · %-I:%M%p').replace('AM','a').replace('PM','p')
    else:
        generated_at = now.strftime('%a %b %-d %Y · %-I:%M%p').replace('AM','a').replace('PM','p')

    data = {
        'today':            state.get('today',       now.strftime('%Y-%m-%d')),
        'threeDays':        state.get('threeDays',   (now + timedelta(days=3)).strftime('%Y-%m-%d')),
        'generatedAt':      generated_at,
        'cacheAgeMin':      round(age, 1) if age is not None else None,
        'upcomingCalls':    state.get('upcomingCalls',    []),
        'followUps':        clean_fus,
        # Deal-tile bucket (frontend reads DATA[<deal_tile_key>]). Key name
        # comes from tenant_slug so the field name doesn't hardcode any
        # tenant string in this module.
        _DEAL_TILE_KEY:     _merge_registered_deals(
                                state.get(_DEAL_TILE_KEY, []),
                                (state.get('dealPortfolio') or {}).get('deals', [])
                            ),
        # Fundraising block: user-state (buckets) wins over compile output
        # (siblings) per Operating Principle #1. Falls back to compile output
        # if user-state file is missing.
        'fundraising':      (lambda: (
            (lambda fr_user, fr_comp: ({**(fr_comp or {}), **fr_user} if fr_user else (fr_comp or {})))
            (_load_user_fundraising(), state.get('fundraising', {}))
        ))(),
        'briefingSynopsis': state.get('briefingSynopsis', {}),
        'themesSynopsis':   state.get('themesSynopsis',   {}),
        'lpData':           (_flatten_fundraising_to_lpdata(_load_user_fundraising())
                              or state.get('lpData', [])),
        'staleContacts':    state.get('staleContacts',    []),
        'warmContacts':     state.get('warmContacts',     []),
        'lpNetwork':        state.get('lpNetwork',        []),
        'recentActivity':   state.get('recentActivity',   []),
        'recruiting': {
            'active':           clean_rec,
            'archived':         state.get('recruiting', {}).get('archived', []),
            'priorityTargets':  state.get('recruiting', {}).get('priorityTargets',  {}),
            'recruiters':       state.get('recruiting', {}).get('recruiters',       []),
        },
        'calendar':         state.get('calendar',         []),
        # Pipeline / email fields (populated by scheduled AI runs)
        'emailQueue':             state.get('emailQueue',             []),
        'unprocessedTranscripts': state.get('unprocessedTranscripts', []),
        'pipelineStatus':         state.get('pipelineStatus',         {}),
        'pipelineRunHistory':     state.get('pipelineRunHistory',     []),
        'emailActivity':          state.get('emailActivity',          []),
        'gmailScanned':           state.get('gmailScanned',           ''),
        # Deal system portfolio (compiled by deal-system-compile.py, embedded by cos-dashboard-fetch.py)
        'dealPortfolio':          state.get('dealPortfolio',          {}),
        # ── Routing v2 envelope arrays (Phase 3 UI surface) ──────────────
        # Populated by routines/process/_envelope_writer.py; rendered by
        # buildAwaitingExternal() + buildIntelCard() in cos-dashboard.html.
        'awaitingExternal':       state.get('awaitingExternal',       []),
        'dealIntel':              state.get('dealIntel',              []),
        'originationInbox':       state.get('originationInbox',       []),
        'themes':                 state.get('themes',                 []),
        'routingExceptions':      state.get('routingExceptions',      []),
        # Proposed-learnings tile — candidates queued by
        # run_learning_capture_scan() in dash-state-hook.py. Dashboard
        # surfaces these for one-click accept/reject/defer; full structured
        # entry then happens via /propose-learning skill.
        'proposedLearnings':      _load_proposed_learnings(
                                       state.get('today', now.strftime('%Y-%m-%d'))
                                  ),
        # Rules-of-the-Road tile — written by `python3 ~/cos-pipeline/tools/
        # rules_audit.py --apply`. Soft surface: returns {} when the file is
        # missing or unreadable; the template hides the tile in that case.
        'rulesCompliance':        _load_rules_compliance(),
        # Priority Synthesis — Tier 1 (rule-based scoring) + Tier 2 (Claude prose).
        # Written by lib/prioritize.py (Tier 1) + synthesize_prose.py (Tier 2).
        # Must be included here so buildSynthesisPane() in the template can read
        # DATA.prioritySynthesis — omitting it leaves the pane permanently empty.
        'prioritySynthesis':      state.get('prioritySynthesis', {}),
    }

    # ── Phase I Tier 1.5 — gap_detector (Jane critic substrate) ──────────
    # Re-runs on every warmup/refresh using cached followUpsRaw + current
    # deal logs. Non-fatal — a broken gap_detector never crashes the render.
    # gap_detector.run() is also called in cos-dashboard-fetch.py (the slow
    # path that calls Drive APIs); this call gives fresh gap detection on
    # every refresh even between full fetches.
    # Uses _HERE / _ROOT (already module-level, symlink-aware — do NOT use
    # Path(__file__).resolve() as that breaks the dashboards/app path layout).
    try:
        _gd_app_dir = str(_HERE)  # ~/dashboards/app/ (symlink-aware)
        if _gd_app_dir not in sys.path:
            sys.path.insert(0, _gd_app_dir)
        from lib import gap_detector as _gap_detector
        import yaml as _yaml
        _gd_weights_path = _ROOT / 'config' / 'synthesis-weights.yaml'
        _gd_weights = (_yaml.safe_load(_gd_weights_path.read_text())
                       if _gd_weights_path.exists() else {}) or {}
        _gd_followups = state.get('followUpsRaw', '') or ''
        _gd_calendar = state.get('upcomingCalls', []) or []
        _gd_deal_sys_path = _ROOT / 'data' / 'compiled' / 'deal-system-data.json'
        _gd_deal_config = []
        if _gd_deal_sys_path.exists():
            try:
                _gd_deal_config = json.loads(_gd_deal_sys_path.read_text()).get('deals') or []
            except Exception:
                pass
        _gd_gaps = _gap_detector.run(
            dashboard_data=state,
            followups_text=_gd_followups,
            calendar_events=_gd_calendar,
            deal_config=_gd_deal_config,
            weights=_gd_weights,
        )
        data.setdefault('prioritySynthesis', {})['gaps'] = _gd_gaps
        if _gd_gaps:
            print(f'[gap_detector] {len(_gd_gaps)} gaps surfaced', flush=True)
    except Exception as _gap_err:
        print(f'[gap_detector] non-fatal: {_gap_err!r}', flush=True)
        data.setdefault('prioritySynthesis', {}).setdefault('gaps', [])

    return data, state


def main():
    t0 = datetime.now()

    data, state = assemble_data()
    age = cache_age_minutes(state)

    # ── Inject into HTML ──
    # Read from clean .template.html (committed, no injected data block).
    # Write to .rendered.html (gitignored, served by dashboard) AND mirror
    # to legacy .html for the transition window — see HTML_STRIP_RUNBOOK.
    if DASHBOARD_TEMPLATE.exists():
        source_path = DASHBOARD_TEMPLATE
    elif DASHBOARD_PATH.exists():
        source_path = DASHBOARD_PATH   # bootstrap case before .template.html exists
    else:
        print(f'ERROR: neither template nor legacy dashboard found '
              f'({DASHBOARD_TEMPLATE} / {DASHBOARD_PATH})', file=sys.stderr)
        sys.exit(1)

    html = source_path.read_text()
    s, e = find_data_block(html)
    if s is None:
        print(f'ERROR: could not find DATA block in {source_path.name}', file=sys.stderr)
        sys.exit(1)

    data_js = 'const DATA = ' + json.dumps(data, indent=2, ensure_ascii=False) + '; // __END_DATA__'
    rendered = html[:s] + data_js + html[e:]

    # Syntax-check the inline <script> blocks before overwriting the live
    # rendered file. 2026-05-21: a single backslash escape (`won\\'t`) in a
    # _showDismissToast literal threw SyntaxError at parse, killing the React
    # mount and blanking every dashboard route while curl still returned 200.
    # If `node` is unavailable we skip silently — refresh must not depend on
    # node being installed.
    syntax_err = _check_inline_js_syntax(rendered)
    if syntax_err:
        print(f'ERROR: rendered HTML has JS syntax error — NOT overwriting '
              f'{DASHBOARD_RENDERED.name}:\n{syntax_err}', file=sys.stderr)
        sys.exit(2)

    DASHBOARD_RENDERED.write_text(rendered)
    # Legacy mirror — keep .html fresh during transition so rollback by
    # reverting server.py constants stays a single-step undo.
    DASHBOARD_PATH.write_text(rendered)

    elapsed = (datetime.now() - t0).total_seconds() * 1000
    source  = f'cache ({age_label(age)})' if age is not None else 'live fetch'
    print(f'Dashboard refreshed in {elapsed:.0f}ms from {source} '
          f'→ {DASHBOARD_RENDERED.name} (+ legacy {DASHBOARD_PATH.name})')

if __name__ == '__main__':
    main()
