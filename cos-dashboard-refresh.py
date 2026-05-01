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
"""
import json, subprocess, sys
from datetime import datetime, timedelta
from pathlib import Path

_HERE               = Path(__file__).resolve().parent    # ~/dashboards/app/
_ROOT               = _HERE.parent                       # ~/dashboards/
DASHBOARD_PATH      = _HERE / 'templates' / 'cos-dashboard.html'
STATE_PATH          = _ROOT / 'data' / 'compiled' / 'dashboard-data.json'
FETCH_SCRIPT        = _HERE / 'cos-dashboard-fetch.py'
FUNDRAISING_PATH    = _ROOT / 'data' / 'user-state' / 'fundraising.json'
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
def clean_follow_ups(fus: list, cutoff_days: int = 14, dismissed_set: set = None) -> tuple[list, int]:
    """
    Remove dirty items from followUps every refresh so pipeline re-runs can't
    re-introduce them. Rules applied in order:

    0. Dismissed set — drop items user explicitly dismissed via the UI (stable who|what[:40] key)
    1. Date filter   — drop items whose due date is older than cutoff_days
    2. Yoni self-ref — drop where who=='Yoni' (or similar) with no external party
    3. Blocklist     — specific who+what patterns we never want
    4. Dedup         — keep first occurrence of each (who.lower, what[:60].lower) key
    """
    from datetime import date as _date

    today = _date.today()
    cutoff = today - timedelta(days=cutoff_days)
    _dismissed = dismissed_set or set()

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

    # ── 2. Yoni self-ref patterns ──
    YONI_NAMES = {'yoni', 'yoni gontownik', 'yoni / tomac cove'}
    def _is_yoni_self_ref(f):
        who = f.get('who', '').strip().lower()
        return who in YONI_NAMES

    # ── 3a. Name normalizer — applied before blocklist and dedup ──────────────────
    # Each entry: (who_substr_match, new_who)  — rewrites 'who' field in-place
    # Use this to canonicalize messy LLM-generated names that keep coming back from re-fetches.
    WHO_NORMALIZE = [
        ('john stinebach',                     'John Stinebaugh / Reinova Partners'),
        ('fit ventures team',                  'FIT Ventures'),
        ('fit ventures / brian becker',        'FIT Ventures'),
        ('one search — rebecca',               'One Search Partners'),
        ('castleton recruiter',                'Castleton Commodities'),
        ('gideon powell / cholla',             'Gideon Powell / Cholla Digital'),
        ('cholla digital — gideon',            'Cholla Digital / Gideon Powell'),
        ('hudson bay/sander',                  'Hudson Bay / Sander Gerber'),
        ('hudson bay / sander',                'Hudson Bay / Sander Gerber'),
        ('diana and sydney',                   'Diana / Sydney (Goldman)'),
        ('leaf / piper sandler syndication',   'Lee / Piper Sandler Syndication'),
        ('thomas cooper (piper maddox)',        'Thomas Cooper / Piper Maddox'),
        ('mark saxe / brennan zaunbrecher (',   'Mark Saxe / Brennan Zaunbrecher'),
    ]
    WHAT_NORMALIZE = [
        # (who_substr, old_what_substr, new_what) — only rewrites if who matches
        ('ben chouake', 'katie behnke', 'Follow up on Kurt Alme intro via Katie Behnke — action in Katie\'s court'),
        ('railway', 'build failures', 'Resolve 6 consecutive build failures on call-webhook — conference call transcription pipeline may be down'),
        ('castleton commodities', 'decide whether to update on mercuria', 'Decide whether to inform Castleton of Mercuria departure'),
        ('castleton commodities', 'proactively disclose', 'Decide whether to proactively disclose Mercuria departure to Castleton recruiter'),
        ('ansel', 'grab coffee or beer with ansel', 'Grab coffee — Ansel led Walker buying 5% of RD front-to-back; sharp deal-market read for Tomac Cove'),
        ('fit ventures', 'cell tower', 'Provide detailed information on cell tower portfolio deal structure and tenant data'),
        ('fit ventures', 'thunderhead', 'Share Thunderhead equipment financing project details'),
        ('hudson bay', 'consider introduction', 'Intro Sander Gerber once Black Bayou deal has clarity'),
        ('mark saxe', 'call with mark + brennan', 'Call Mark + Brennan: Tue Apr 28 11am CT proposed — topic: Thunderhead DG equipment finance equity structure'),
        ('diana', 'capital formation meeting for next week', 'Schedule capital formation meeting'),
        ('gideon powell / cholla', 'propose 2-3 alternative', 'Propose alternate dates for Dallas ranch visit — target Apr 28–May 2'),
    ]
    def _normalize(f):
        f = dict(f)
        who_l = f.get('who', '').lower().strip()
        for pat, new_who in WHO_NORMALIZE:
            if pat in who_l:
                f['who'] = new_who
                who_l = new_who.lower()
                break
        what_l = f.get('what', '').lower().strip()
        for w_pat, a_pat, new_what in WHAT_NORMALIZE:
            if (not w_pat or w_pat in who_l) and (a_pat in what_l):
                f['what'] = new_what
                break
        return f

    # ── 3. Blocklist ──────────────────────────────────────────────────────────────
    # Each entry: (who_substr, what_substr)          — substring match on both fields
    #         OR: (who_substr, what_substr, 'exact') — what must match exactly (stripped)
    #             (use 'exact' when the bad version is a short subset of a good version)
    BLOCKLIST = [
        # Boris / Cameron Prairie
        ('boris', 'cameron prairie'),
        # Generic LP strategy notes
        ('tomac cove lp outreach', ''),
        # Vague / first-name-only contacts
        ('azraelli contact', ''),
        ('', 'reach out to school contact'),
        ('tony', 'towers data'),
        ('brian/infrastructure partners', ''),
        ('austin guy', ''),
        # Yoni self-ref Tanmay follow-ups
        ('', 'follow up with tanmay kumar (greg network advisor) to activate intro to ansel'),
        ('', 'follow up with tanmay kumar (greg network advisor) to activate intro to leaf'),
        ('', 'text unnamed advisor from greg network call'),
        # Tanmay Kumar near-dups — drop all grab beer variants
        ('tanmay kumar', 'grab beer'),
        ('tanmay kumar', 'text and connect him with rob'),
        ('tanmay kumar', 'follow up to get intro to ansel'),
        ('tanmay kumar', 'follow up to get intro to leaf'),
        ('tanmay kumar', 'follow up to activate intro to ansel'),
        ('tanmay kumar', 'follow up to activate intro to leaf'),
        ('tanmay kumar', 'text to connect him with rob'),
        # Rohan at ISG — inferior dup of "Rohan / ISG"
        ('rohan at isg', ''),
        # FIT Ventures — "schedule + attend" call is dismissed
        ('fit ventures', 'schedule'),
        # Tower Portfolio — dismissed
        ('tower portfolio', 'follow up on diligence package'),
    ]
    def _is_blocklisted(f):
        who  = f.get('who',  '').lower().strip()
        what = f.get('what', '').lower().strip()
        for entry in BLOCKLIST:
            w_pat, a_pat = entry[0], entry[1]
            exact = len(entry) > 2 and entry[2] == 'exact'
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
        if _is_yoni_self_ref(f):
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
    """Normalize firm names, contacts, and lastAction text for recruiting entries."""
    FIRM_NORM = {
        'quantum':                    'Quantum Capital',
        'hartree':                    'Hartree Partners',
        'citadel':                    'Citadel',
        'CARLYLE':                    'Carlyle',
        'BROOKFIELD':                 'Brookfield',
        'Nofar':                      'Nofar Energy',
        'HIG':                        'H.I.G. Capital',
        'ARCLIGHT':                   'ArcLight',
        'future standard / post oak': 'Future Standard / Post Oak',
        'bennet cogden':              'Bennett Cogden',
        'One Search':                 'One Search Partners',
        'Govt -':                     'Govt — OSC',
        'DHR':                        'DHR Global',
        'Digital Bridge':             'DigitalBridge',
        'I Squared':                  'I Squared Capital',
    }
    CONTACT_NORM = {
        'ANDREW EHRLICKMAN':                    'Andrew Ehrlickman',
        'ANDREW BRANNAN':                       'Andrew Brannan',
        'ed pallesan OR paul grum':             'Ed Pallesan / Paul Grum',
        'Nadav Barkan or OFER AYANNAY':         'Nadav Barkan / Ofer Ayalon',
        'pangea recruiting':                    'Pangea Recruiting',
        'scott levy':                           'Scott Levy',
        'jay rubenstein':                       'Jay Rubenstein',
        'tyler kopp':                           'Tyler Kopp',
        'steven sonnenstein':                   'Steven Sonnenstein',
        'Jennifer Skylakos / amanda yaffa':     'Jennifer Skylakos / Amanda Yaffa',
        'jennifer Skylakos / amanda yaffa':     'Jennifer Skylakos / Amanda Yaffa',
        'Office of Strategic Capital':          'David Lorch / OSC',
        'Dan Mccarthy':                         'Dan McCarthy',
    }
    LAST_ACTION_NORM = {
        'quantum':      'Manish: role reports to a senior of Ben Daniel; VP level too junior. Awaiting Doug.',
        'CARLYLE':      'Role pending deal close; 2 hires joining first. Digital lead seat not backfilled — deals underperforming.',
        'BROOKFIELD':   'No on Infra AI Head. Andrew Ehrlickman checking with Nadav on alternate path.',
        'Nofar':        'Andrew Ehrlickman checking with Nadav Barkan.',
        'HIG':          'Reached out to Ed Pallesan / Paul Grum.',
        'ARCLIGHT':     'Reached out.',
        'Apollo':       'Manish checking on origination talent role.',
        'future standard / post oak': 'Manish flagging: exploring if expanding.',
        'Blackstone':   'Checked in with Jeremy Smilovitz — awaiting response.',
        'citadel':      'Returns Mar 30 — follow up pending.',
        'TPG':          'Note: also acquired Peppertree — possible fit.',
        'Govt -':       'Sent updated resume to David Lorch.',
    }
    NEXT_NORM = {
        'Barton Partnership': 'Discuss: I Squared (Singapore + US), Apollo (origination talent), pensions.',
    }
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
def main():
    t0 = datetime.now()

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
        'tomac':            state.get('tomac',            []),
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
    }

    # ── Inject into HTML ──
    if not DASHBOARD_PATH.exists():
        print(f'ERROR: dashboard not found at {DASHBOARD_PATH}', file=sys.stderr)
        sys.exit(1)

    html = DASHBOARD_PATH.read_text()
    s, e = find_data_block(html)
    if s is None:
        print('ERROR: could not find DATA block in dashboard HTML', file=sys.stderr)
        sys.exit(1)

    data_js = 'const DATA = ' + json.dumps(data, indent=2, ensure_ascii=False) + '; // __END_DATA__'
    DASHBOARD_PATH.write_text(html[:s] + data_js + html[e:])

    elapsed = (datetime.now() - t0).total_seconds() * 1000
    source  = f'cache ({age_label(age)})' if age is not None else 'live fetch'
    print(f'Dashboard refreshed in {elapsed:.0f}ms from {source} → {DASHBOARD_PATH}')

if __name__ == '__main__':
    main()
