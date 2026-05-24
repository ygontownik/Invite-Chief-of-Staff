#!/usr/bin/env python3
"""
cos-dashboard-fetch.py
Fetches Google Docs + Calendar + Gmail → parses → writes ~/docs/dashboard-data.json.

This is the SLOW path (1-2 sec, hits Google APIs in parallel).
Run in the background by:
  - cos-dashboard-server.py auto-warmup thread (every 20 min)
  - POST /warmup endpoint
  - Any scheduled task that writes to the follow-ups / recruiting / pipeline docs
  - cos-personal-briefing (daily 7:51am)

cos-dashboard-refresh.py (the FAST path) reads the JSON this writes and injects HTML instantly.
"""
import json, os, pickle, re, sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

_HERE      = Path(__file__).parent             # ~/dashboards/app/
_ROOT      = _HERE.parent                                 # ~/dashboards/
CREDS_PATH        = Path.home() / 'credentials/token.json'         # shared union token (drive + documents + mail + calendar.readonly)
GDRIVE_PICKLE_PATH = Path.home() / 'credentials/gdrive_token.pickle' # drive + documents
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

# Firm context — loads firm_context.yaml for config that varies by team
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _firm_context as _fc
_CTX     = _fc.load_firm_context()
_FCONFIG = _fc.load_firm_config()                          # firm_config.json
MY_EMAIL = (_fc._principal(_CTX).get('email') or '')

# Drive doc IDs — prefer firm_context.yaml :: google_docs (canonical
# per DECISIONS.md C8), fall back to firm_config.json :: docs (legacy).
def _doc_id(key, default=''):
    gdocs = (_CTX.get('google_docs') or {})
    if key in gdocs:
        return gdocs[key]
    # If google_docs section exists but this key is missing, the firm's
    # context is incomplete — warn loudly rather than silently reading
    # a fallback doc ID that may belong to a different subscriber.
    if gdocs and default:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            'FETCH: firm_context.yaml has google_docs but missing key %r — '
            'falling back to hardcoded default %r. Add this key to firm_context.yaml.',
            key, default
        )
    legacy = (_FCONFIG.get('docs') or {})
    return legacy.get(key, default)

DOC_IDS = {
    'followups':     _doc_id('followups',     '10leX26u8n3XkoCHzg7SDwLUodVX2CqKjvXcSJ-KAsCY'),
    'recruiting':    _doc_id('recruiting',    '1ZnTCVoA0ID7XTDFy27yDnrEVhBqx75kaTg_QXFq4eXA'),
    'deal_pipeline': _doc_id('pipeline',      '1LHorixPs8ppwSvQzGfA_B6609YZA8dSpR4rmppENzpc'),
    'briefing_log':  _doc_id('briefing_log',  '14wE3L6ZRsjhhx2psRKbaHS5i0kgEoteWYZusqETiAZ0'),
    'daily_market':  _doc_id('daily_market',  '1UZ1t4bhgzll5VcAuP3Mj1CyYb-4xjgmbUK1xg6oUS_k'),
    'people':        _doc_id('people',        '1F3MjRAoAOWYLXiwXEYQYpu1tJjdx7-4iZtAHSyyL3Hg'),
}
# (Legacy alias key removed — no callers remain that reference it.)

# Owner whitelist — loaded from firm_context.yaml. Falls back to
# owner_whitelist; if absent, derives from principal.name + team[].name.
# Used to reject person-only counterparties from being mis-classified
# as deal-shaped firms (line 849-857 region in live file).
def _firm_owner_reject_set(ctx):
    owners = ctx.get('owner_whitelist') or []
    if not owners:
        owners = []
        p = (ctx.get('principal') or {}).get('name', '')
        if p: owners.append(p)
        for m in (ctx.get('team') or []):
            n = m.get('name', '')
            if n: owners.append(n)
    out = set()
    for o in owners:
        if not o: continue
        out.add(o.lower())
        # Add first-name token (e.g. "Mark Saxe" → "mark") so a raw
        # counterparty like "mark" still rejects.
        first = o.split()[0].lower() if o.split() else ''
        if first: out.add(first)
    return out

# Deal/recruit keyword sets — loaded from firm_config.json.
# Per DECISIONS.md C13, the canonical source is
# ~/cos-pipeline/domains/<domain>/config.yaml; firm_config.json acts
# as the resolved per-tenant copy that the loader merges. We read from
# _FCONFIG (which respects $COS_CONFIG_DIR) and fall back to a minimal
# generic set if the field is missing.
_DEFAULT_DEAL_KEYWORDS = (
    'term sheet', 'loi', 'letter of intent', ' nda ', 'diligence',
    'investment committee', ' ic ', 'closing', 'co-invest', 'co invest',
)
_DEFAULT_RECRUIT_KEYWORDS = (
    'resume', ' cv ', 'recruiting', 'recruiter', 'interview',
    'shortlist', 'longlist', 'offer letter', 'compensation',
    'job description', ' jd ', 'open role', 'opportunity',
)

def _firm_deal_keywords(fcfg):
    raw = fcfg.get('deal_keywords') or list(_DEFAULT_DEAL_KEYWORDS)
    aliases = (fcfg.get('counterparty_aliases') or {})
    canon = []
    if isinstance(aliases, dict):
        for v in aliases.values():
            if isinstance(v, str):
                canon.append(v)
            elif isinstance(v, dict) and v.get('canonical'):
                canon.append(v['canonical'])
    elif isinstance(aliases, list):
        for entry in aliases:
            if isinstance(entry, dict) and entry.get('canonical'):
                canon.append(entry['canonical'])
    return {str(k).lower() for k in (raw + canon) if k}

def _firm_recruit_keywords(fcfg):
    raw = fcfg.get('recruit_keywords') or list(_DEFAULT_RECRUIT_KEYWORDS)
    return {str(k).lower() for k in raw if k}

def _is_deal_ws(ws):
    """True if a workstream code refers to deal/pipeline activity.
    Accepts the canonical 'deals' code plus a legacy alias for one release."""
    return ws in ('deals', 'tomac')  # noqa: tenant-leak (legacy workstream alias)

# ── Services ──────────────────────────────────────────────
def _gdrive_creds():
    """Load gdrive_token.pickle (drive + documents scopes)."""
    with open(GDRIVE_PICKLE_PATH, 'rb') as f:
        return pickle.load(f)

def get_services():
    # Docs/Drive use gdrive_token.pickle (drive + documents scopes).
    # Calendar uses token.json (calendar.readonly scope).
    docs_svc = build('docs',     'v1', credentials=_gdrive_creds())
    cal_creds = Credentials.from_authorized_user_file(str(CREDS_PATH))
    cal_svc  = build('calendar', 'v3', credentials=cal_creds)
    return docs_svc, cal_svc

def _fetch_doc_worker(doc_id, doc_cache):
    """Thread-safe doc fetch: creates its own service instances per thread."""
    creds     = _gdrive_creds()
    docs_svc  = build('docs',  'v1', credentials=creds)
    drive_svc = build('drive', 'v3', credentials=creds)
    return get_doc_text_cached(docs_svc, drive_svc, doc_id, doc_cache)

def get_doc_text(docs_svc, doc_id):
    doc = docs_svc.documents().get(documentId=doc_id).execute()
    parts = []
    for el in doc.get('body', {}).get('content', []):
        if 'paragraph' in el:
            for pe in el['paragraph'].get('elements', []):
                if 'textRun' in pe:
                    parts.append(pe['textRun']['content'])
    return ''.join(parts)

_DOC_CACHE_TTL_MIN = 30   # for docs where Drive metadata access is denied (scope limitation)

def get_doc_text_cached(docs_svc, drive_svc, doc_id, doc_cache):
    """Fetch doc text, using cache when the doc hasn't changed since last warmup.

    Two cache strategies:
    1. Exact match (preferred): Drive files.get() returns modifiedTime in ~60ms.
       If it matches the cached value, skip the full documents.get() (~400-700ms).
    2. TTL fallback: Many docs use 'drive.file' scope which only allows metadata
       access for app-created files. For other docs, modifiedTime is unavailable.
       In that case, use a time-based TTL (30 min) — sufficient because docs are
       only written by pipeline runs that trigger warmup explicitly afterward.

    doc_cache is the '_docCache' dict from dashboard-data.json, mutated in place.
    Returns the doc text string.
    """
    try:
        meta     = drive_svc.files().get(fileId=doc_id, fields='modifiedTime').execute()
        mod_time = meta.get('modifiedTime', '')
    except Exception:
        mod_time = ''

    cached = doc_cache.get(doc_id, {})

    # Strategy 1: exact modifiedTime match
    if mod_time and mod_time == cached.get('modifiedTime') and cached.get('text'):
        return cached['text']   # ← cache hit

    # Strategy 2: TTL fallback when modifiedTime unavailable (Drive scope limitation)
    if not mod_time and cached.get('text') and cached.get('cachedAt'):
        try:
            age_min = (datetime.now() - datetime.fromisoformat(cached['cachedAt'])).total_seconds() / 60
            if age_min < _DOC_CACHE_TTL_MIN:
                return cached['text']   # ← TTL cache hit
        except Exception:
            pass

    # Cache miss — fetch full content
    text = get_doc_text(docs_svc, doc_id)
    doc_cache[doc_id] = {'modifiedTime': mod_time, 'cachedAt': datetime.now().isoformat(), 'text': text}
    return text


# ── Gmail activity ─────────────────────────────────────────
# Keyword sets sourced from firm_config.json (was hardcoded — see DELTA 3 of
# track-C/cos-dashboard-fetch.py.next). Helpers defined above near DOC_IDS.
_DEAL_KEYS    = _firm_deal_keywords(_FCONFIG)
_RECRUIT_KEYS = _firm_recruit_keywords(_FCONFIG)
_OWNER_REJECT = _firm_owner_reject_set(_CTX)

def _header(headers_list, name):
    name_l = name.lower()
    for h in headers_list:
        if h.get('name', '').lower() == name_l:
            return h.get('value', '')
    return ''

def _display_name(raw_addr):
    """Extract display name from 'Full Name <email@>' or return username."""
    m = re.match(r'^"?([^<"]+)"?\s*<', raw_addr or '')
    if m:
        return m.group(1).strip().strip('"')
    at = (raw_addr or '').split('@')[0]
    return at.replace('.', ' ').title() if at else raw_addr or ''

def get_gmail_activity(gmail_svc):
    """Fetch last 48h email signals relevant to dashboard.

    Strategy for speed:
      1. One messages.list() call with a broad 48h query → up to 20 IDs  (~100ms)
      2. One batch HTTP request for all message metadata               (~150ms)
      Total added latency: ~250ms (runs in parallel with doc fetches)

    Returns list of signal dicts for recentActivity feed.
    """
    if gmail_svc is None:
        return []

    results = []
    today     = datetime.now().strftime('%Y-%m-%d')
    yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')

    try:
        # Broad query: anything from/to me in last 48h that touches recruiting or deal keywords.
        # Gmail search is server-side so this is fast even on a large inbox.
        # Gmail server-side query — built from firm_config keywords so
        # adding a counterparty in firm_config.json doesn't require code
        # edits. We bound to the top ~20 strongest terms to keep the URL
        # under Gmail's query length cap (~3KB safe).
        deal_terms = [k for k in sorted(_DEAL_KEYS, key=len, reverse=True) if len(k) >= 4][:15]
        recruit_terms = [k for k in sorted(_RECRUIT_KEYS, key=len, reverse=True) if len(k) >= 4][:10]
        # Quote multi-word terms; strip embedded spaces for single-word.
        def _q(t):
            t = t.strip()
            return f'"{t}"' if ' ' in t else t
        subj = ' OR '.join(_q(t) for t in (deal_terms + recruit_terms)) or '"deal"'
        query = f'newer_than:2d (from:me OR subject:({subj}))'
        list_resp = gmail_svc.users().messages().list(
            userId='me', q=query, maxResults=20,
            fields='messages(id)'
        ).execute()

        msg_ids = [m['id'] for m in list_resp.get('messages', [])]
        if not msg_ids:
            return []

        # Batch-fetch metadata for all IDs in a single HTTP round trip
        fetched = {}
        def _cb(request_id, response, exception):
            if exception is None and response:
                fetched[request_id] = response

        batch = gmail_svc.new_batch_http_request(callback=_cb)
        for mid in msg_ids:
            batch.add(
                gmail_svc.users().messages().get(
                    userId='me', id=mid,
                    format='metadata',
                    metadataHeaders=['From', 'To', 'Subject', 'Date'],
                    fields='id,snippet,internalDate,labelIds,payload/headers',
                ),
                request_id=mid,
            )
        batch.execute()

        for mid, msg in fetched.items():
            hdrs      = msg.get('payload', {}).get('headers', [])
            from_raw  = _header(hdrs, 'From')
            to_raw    = _header(hdrs, 'To')
            subject   = _header(hdrs, 'Subject') or '(no subject)'
            snippet   = msg.get('snippet', '')
            labels    = msg.get('labelIds', [])

            direction = 'sent' if 'SENT' in labels else 'received'
            counterparty_raw = to_raw if direction == 'sent' else from_raw
            counterparty = _display_name(counterparty_raw)

            # Parse date
            ts = int(msg.get('internalDate', 0)) / 1000
            dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
            date_str  = dt.strftime('%Y-%m-%d')
            time_str  = dt.strftime('%-I:%M%p').lower()
            date_label = 'Today' if date_str == today else 'Yesterday' if date_str == yesterday else date_str

            # Classify
            combined = (subject + ' ' + from_raw + ' ' + to_raw + ' ' + snippet).lower()

            workstream, signal = None, None
            if any(k in combined for k in _RECRUIT_KEYS):
                workstream = 'job'
                if 'resume' in combined or ' cv ' in combined or 'curriculum vitae' in combined:
                    signal = 'Resume sent' if direction == 'sent' else 'Resume requested'
                elif 'offer' in combined:
                    signal = 'Offer / comp'
                elif 'interview' in combined:
                    signal = 'Interview'
                elif 'shortlist' in combined or 'longlist' in combined:
                    signal = 'Shortlist update'
                elif 'job description' in combined or ' jd ' in combined:
                    signal = 'JD received' if direction == 'received' else 'JD sent'
                else:
                    signal = 'Recruiting'
            elif any(k in combined for k in _DEAL_KEYS):
                workstream = 'tomac'  # noqa: tenant-leak (legacy workstream key — preserved for back-compat with frontend)
                if 'term sheet' in combined or ' loi ' in combined or 'letter of intent' in combined:
                    signal = 'Term sheet / LOI'
                elif ' nda ' in combined:
                    signal = 'NDA'
                elif 'diligence' in combined:
                    signal = 'Diligence'
                else:
                    signal = 'Deal email'
            else:
                continue  # Not relevant enough to surface

            title = f"{'→' if direction == 'sent' else '←'} {counterparty} — {subject[:60]}"
            results.append({
                'type':        'email',
                'direction':   direction,
                'signal':      signal,
                'workstream':  workstream,
                'subject':     subject,
                'counterparty': counterparty,
                'snippet':     snippet[:120],
                'date':        date_str,
                'time':        time_str,
                'dateLabel':   date_label,
                'title':       title,
                'summary':     f"{signal} · {time_str}",
                'category':    f"Email · {signal}",
                'color':       'blue' if workstream == 'job' else 'green',
            })

    except Exception as e:
        print(f'Gmail activity error: {e}', file=sys.stderr)

    # Newest first
    results.sort(key=lambda x: x.get('date', '') + x.get('time', ''), reverse=True)
    return results

# ── Parsers ───────────────────────────────────────────────
def _merge_awaiting(pipeline_items, doc_items):
    """
    Merge envelope-writer-authored awaitingExternal items with doc-authored
    [waiting] rows, de-duplicating on (counterparty, content[:60]).
    Doc rows take precedence when a collision exists (human-maintained).
    """
    seen = set()
    out = []
    for item in list(doc_items) + list(pipeline_items):
        cp = (item.get('counterparty') or '').strip().lower()
        content = (item.get('content') or '').strip().lower()[:60]
        key = (cp, content)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


import re as _re_src
_DASH_PAD = _re_src.compile(r'^─+\s*|\s*─+$')

def _promote_source_ref(items):
    """Promote source_ref fields to top-level source/addedDate so the UI can
    display provenance without reading nested objects. Fills only if missing."""
    for item in items:
        ref = item.get('source_ref') or {}
        if not item.get('addedDate') and ref.get('date'):
            item['addedDate'] = ref['date']
        if not item.get('source') and ref.get('title'):
            clean = _DASH_PAD.sub('', ref['title']).strip()
            clean = _re_src.sub(r'_[Oo]tter_[Aa]i', '', clean)
            clean = _re_src.sub(r'\.(txt|docx?)$', '', clean, flags=_re_src.IGNORECASE).strip()
            src_type = ref.get('type', 'call')
            item['source'] = f'{src_type} — {clean}' if clean else src_type
    return items


# Workflow-stage supersession: pairs of (upstream_pattern, downstream_pattern).
# When the SAME canonical counterparty has both an upstream-stage item and a
# downstream-stage item AND the downstream item's addedDate >= upstream's,
# the upstream is auto-retired (logged to stderr) and dropped from awaiting.
# Codified 2026-05-04 per dash_corrections.md.
_WORKFLOW_PAIRS = [
    # NDA draft / send / issue / finalize → NDA review / redline / counter / execute
    # 2026-05-04: broadened upstream pattern to catch "Finalize and send NDA"
    # (was requiring literal "draft NDA" / "send draft NDA"). Also covers
    # "issue NDA", "deliver NDA", "stand up NDA" as the upstream-stage verbs.
    (_re_src.compile(
        r'\b(draft|deliver|send|finaliz[ae]|issue|stand[\s-]?up|prepar(e|ing))\s+(?:and\s+\w+\s+)?(?:the\s+)?(?:mutual\s+|draft\s+|new\s+)?nda\b',
        _re_src.IGNORECASE,
     ),
     _re_src.compile(
        r'\b(review|accept|counter|respond\s+to|redline|execute|sign|countersign)\s+\w*\s*nda\b|nda\s+redline\b',
        _re_src.IGNORECASE,
     )),
    # Calendar invite → meeting confirmation/recap
    (_re_src.compile(r'\b(send|share)\s+(calendar|cal|zoom)\s+inv', _re_src.IGNORECASE),
     _re_src.compile(r'\b(confirm|recap|summarize)\s+(the\s+)?(call|meeting|catch[\s-]?up)\b', _re_src.IGNORECASE)),
    # Teaser request → teaser delivered (we received materials)
    (_re_src.compile(r'\b(send|deliver)\s+(the\s+)?(teaser|cim|materials)', _re_src.IGNORECASE),
     _re_src.compile(r'\b(review|accept|respond\s+to)\s+(the\s+)?(teaser|cim|materials)', _re_src.IGNORECASE)),
]

def _supersede_workflow_stages(items):
    """Drop upstream-stage awaiting items when a downstream-stage item exists for
    the same canonical counterparty with newer addedDate.

    Logs each supersession to stderr. See dash_corrections.md (2026-05-04).
    """
    if not items:
        return items
    # Group by canonical counterparty
    by_cp = {}
    for i, item in enumerate(items):
        cp = _normalize_cp(item.get('counterparty') or '')
        by_cp.setdefault(cp.lower(), []).append((i, item))

    drop_idx = set()
    for cp_low, group in by_cp.items():
        if not cp_low or len(group) < 2:
            continue
        for upstream_re, downstream_re in _WORKFLOW_PAIRS:
            ups = [(i, x) for (i, x) in group if upstream_re.search((x.get('content') or '') + ' ' + (x.get('what') or ''))]
            dns = [(i, x) for (i, x) in group if downstream_re.search((x.get('content') or '') + ' ' + (x.get('what') or ''))]
            if not ups or not dns:
                continue
            latest_dn_added = max((x.get('addedDate') or '') for (_, x) in dns)
            for (ui, ux) in ups:
                ux_added = ux.get('addedDate') or ''
                if latest_dn_added and ux_added and latest_dn_added >= ux_added:
                    print(
                        f'cos-dashboard-fetch: superseded upstream item for {cp_low!r}: '
                        f'{(ux.get("content") or "")[:80]!r} (downstream addedDate {latest_dn_added})',
                        file=sys.stderr,
                    )
                    drop_idx.add(ui)
    return [it for i, it in enumerate(items) if i not in drop_idx]


# "next week" / "later this week" / "early next week" — frozen-in-time references
# that go stale silently. Replaced with explicit "week of YYYY-MM-DD" computed
# from the item's addedDate so the user can see the actual week being referenced.
# Codified 2026-05-04 per dash_corrections.md.
_NEXT_WEEK_PATTERNS = [
    (_re_src.compile(r'\bnext\s+week\b', _re_src.IGNORECASE), 7),
    (_re_src.compile(r'\bthis\s+week\b', _re_src.IGNORECASE), 0),
    (_re_src.compile(r'\bearly\s+next\s+week\b', _re_src.IGNORECASE), 7),
    (_re_src.compile(r'\bearly\s+this\s+week\b', _re_src.IGNORECASE), 0),
    (_re_src.compile(r'\blate\s+next\s+week\b', _re_src.IGNORECASE), 10),
    (_re_src.compile(r'\blater\s+this\s+week\b', _re_src.IGNORECASE), 3),
    (_re_src.compile(r'\bend\s+of\s+the\s+week\b', _re_src.IGNORECASE), 3),
    (_re_src.compile(r'\bend\s+of\s+next\s+week\b', _re_src.IGNORECASE), 10),
]

# Day-name + M/D forms — "Wed 4/29", "Friday 5/1", "Mon 5/12". Codified
# 2026-05-05 (rule AB1) — extractor often emits these because they appear
# verbatim in the source email. They read stale once the date passes
# even when the action is still valid. This second-layer auto-correct
# rewrites them to YYYY-MM-DD by inferring the year from the item's
# addedDate (if M/D resolves to a month <= addedDate's month, we assume
# same year; otherwise flip to the year that makes the date plausible).
_DAY_NAMES = r'(?:mon|tue|wed|thu|fri|sat|sun)(?:day|s)?'
_DAY_DATE_PAT = _re_src.compile(
    rf'\b{_DAY_NAMES}\.?\s+(\d{{1,2}})/(\d{{1,2}})(?:/(\d{{2,4}}))?\b',
    _re_src.IGNORECASE,
)
# Bare M/D without a day name (e.g. "by 5/12", "before 4/30") — broader,
# higher false-positive risk. Restricted to forms preceded by date-action
# words to avoid clobbering ratios / scores / page numbers.
_BARE_MD_PAT = _re_src.compile(
    r'\b(?:by|before|due|on|until|through|circa|circle\s+back\s+by)\s+'
    r'(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b',
    _re_src.IGNORECASE,
)
_TOMORROW_PAT = _re_src.compile(r'\btomorrow\b', _re_src.IGNORECASE)
_TODAY_PAT    = _re_src.compile(r'\b(?:today|EOD)\b', _re_src.IGNORECASE)


def _resolve_md_to_iso(month: int, day: int, year_hint, base_date):
    """Resolve a (month, day, optional 2-or-4-digit year) tuple against
    the item's addedDate, returning ISO YYYY-MM-DD. If year is not
    given, pick the year that places the date closest to base_date —
    typically same year if M/D is in the future or recent past, else
    next year for clearly-forward references."""
    from datetime import date as _date
    try:
        if year_hint:
            y = int(year_hint)
            if y < 100: y += 2000
        else:
            y = base_date.year
            try:
                candidate = _date(y, month, day)
            except ValueError:
                return None
            # If the candidate is more than 6 months in the past, assume
            # the writer meant next year.
            if (base_date - candidate).days > 180:
                y += 1
            elif (candidate - base_date).days > 365:
                y -= 1
        return _date(y, month, day).isoformat()
    except (ValueError, TypeError):
        return None


def _materialize_next_week(items):
    """Replace relative-time phrases with absolute YYYY-MM-DD references.
    Codifies rule AB1 (Absolute Dates Only) at the post-extraction layer.

    Patterns covered (idempotent — once replaced, patterns no longer match):
      - "next week" / "early next week" / "late next week" → "week of YYYY-MM-DD"
      - "later this week" / "end of the week" → "week of YYYY-MM-DD"
      - "tomorrow" → "YYYY-MM-DD" (addedDate + 1)
      - "today" / "EOD" → "YYYY-MM-DD" (addedDate)
      - "Wed 4/29" / "Friday 5/1" → "YYYY-MM-DD"
      - "by 5/12" / "due 4/30" → "by YYYY-MM-DD" (preserves verb)

    Operates on `content` and `what` fields. Reads `addedDate` to anchor
    the resolution. If no addedDate, falls through unchanged.
    """
    from datetime import date as _date, timedelta as _td
    for item in items:
        # 2026-05-05 (rule AB1 follow-up): dealIntel + originationInbox
        # items often lack a top-level addedDate — the date lives at
        # source_ref.date. Without this fallback, the materialize pass
        # silently no-ops on those buckets and relative phrasings
        # ("this week", "by 5/12") survive into the rendered dashboard.
        # Same fallback pattern as _compute_deal_logs() at line ~935.
        added_raw = item.get('addedDate') or ''
        if not added_raw or len(added_raw) < 10:
            sref = item.get('source_ref') or {}
            if isinstance(sref, dict):
                added_raw = (sref.get('date') or '')[:10]
        if not added_raw or len(added_raw) < 10:
            continue
        try:
            base = _date.fromisoformat(added_raw[:10])
        except ValueError:
            continue
        for fld in ('content', 'what'):
            v = item.get(fld)
            if not isinstance(v, str) or not v:
                continue
            new = v
            # 1. Week-bucket phrases → "week of YYYY-MM-DD"
            for pat, days in _NEXT_WEEK_PATTERNS:
                target = base + _td(days=days)
                target = target + _td(days=(7 - target.weekday()) % 7)
                new = pat.sub(f'week of {target.isoformat()}', new)
            # 2. "tomorrow" → addedDate + 1
            if _TOMORROW_PAT.search(new):
                new = _TOMORROW_PAT.sub((base + _td(days=1)).isoformat(), new)
            # 3. "today" / "EOD" → addedDate
            if _TODAY_PAT.search(new):
                new = _TODAY_PAT.sub(base.isoformat(), new)
            # 4. "Wed 4/29" / "Friday 5/1" → YYYY-MM-DD
            def _day_date_sub(m):
                iso = _resolve_md_to_iso(int(m.group(1)), int(m.group(2)), m.group(3), base)
                return iso or m.group(0)
            new = _DAY_DATE_PAT.sub(_day_date_sub, new)
            # 5. "by 5/12" / "due 4/30" → "by YYYY-MM-DD" (verb preserved)
            def _bare_md_sub(m):
                iso = _resolve_md_to_iso(int(m.group(1)), int(m.group(2)), m.group(3), base)
                if not iso: return m.group(0)
                # Reinsert the leading verb (split off in the regex)
                verb = m.group(0).split(None, 1)[0]
                return f'{verb} {iso}'
            new = _BARE_MD_PAT.sub(_bare_md_sub, new)
            if new != v:
                item[fld] = new
    return items


_STALE_EVENT_PATTERNS = _re_src.compile(
    r'\b(conference|summit|forum|symposium|register|registration|rsvp|'
    r'attend|attending|attendance|sign[\s-]?up|enroll|'
    r'propose\s+times?|send\s+(calendar|cal)\s+invit\w*|calendar\s+inv|'
    r'schedule\s+(a\s+)?(call|meeting|time|intro|separate)|'
    # 2026-05-05: broaden — "Schedule separate intro between X and Y" /
    # "Intro Jeff K. to Apogee and schedule separate meeting" / "Schedule
    # one-month follow-up meeting" all left Apogee + Berkman items hanging.
    r'schedule\s+(separate|one[\s-]?month|follow[\s-]?up|next)|'
    r'(intro|introduce)\s+\w+\s+(to|and)\s+\w+\s+(and\s+)?schedule|'
    r'reschedule|book\s+(a\s+)?(slot|time|meeting|call)|'
    # "Confirm availability" was the surviving phrase across 5 of the 5
    # items the user flagged 2026-05-05 (Apogee 3×, Black Mountain 1×).
    # Add it as a first-class event marker.
    r'confirm\s+(availability|call\s+time|meeting\s+time|in.person|the\s+meeting)|'
    r'in.person\s+meeting|ranch\s+visit|site\s+visit|'
    r'text\s+\w+\s+schedule|schedule\s+for\s+next\s+week|'
    r'send\s+(calendar|cal)\s+invit\w*|hold\s+(calendar|cal)\s+invit\w*|'
    r'zoom\s+(calendar|cal)\s+invit\w*|calendar\s+invite\s+for\s+\w+\s+\w+\s+\d|'
    # 2026-05-04: broadened to catch slash/and-separated forms and noun-style
    # scheduling references — see dash_corrections.md
    r'meeting\s+(day|date)?[\s/]*(and\s+)?(time|location|logistics)|'
    r'meeting\s+day\s+and\s+location|'
    r'confirm\s+\w+\s+\d+/\d+\s+(meeting|call|intro)|'
    r'intro\s+(call|meeting)|live\s+call|connect\s+live|'
    r'catch[\s-]?up\s+(call|meeting|chat)|'
    r'host\s+\w*\s*catch[\s-]?up|'
    r'reconnect\s+catch[\s-]?up)\b',
    _re_src.IGNORECASE,
)

def _classify_awaiting_category(items):
    """Tag each awaiting item with `category: 'scheduling' | 'substantive'`.

    Scheduling: calendar invites, intro calls, conference registration,
    'confirm availability', 'schedule X', reschedule — items whose
    completion is "the meeting got booked / happened." User wants these
    grouped separately because they're high-volume but low-substance —
    once the call lands on the calendar, the item is dead.

    Substantive: sending materials (Uber lease, term sheets), commitments
    ($X capital, signed NDA), decisions, structural diligence asks.
    These are the actual deal-progress items the user needs to see.

    Classifier: same regex used by _auto_expire_stale_events. If content
    matches, it's scheduling; otherwise substantive. Codified 2026-05-05
    per user request: 'awaiting external should be broken between call
    schedules and actual steps to be taken.'
    """
    for item in items:
        content = (item.get('content') or '') + ' ' + (item.get('what') or '')
        if _STALE_EVENT_PATTERNS.search(content):
            item['category'] = 'scheduling'
        else:
            item['category'] = 'substantive'
    return items


def _drop_team_member_counterparties(items):
    """Awaiting External must only show items waiting on EXTERNAL parties.
    If after canonicalization the counterparty resolves to a team member
    (principal or team[]), drop the item — it's not "awaiting external,"
    it's an internal handoff that belongs in followUps or team-actions.

    Also catches items whose RAW counterparty was a team member but no
    content-alias matched (so re-derivation didn't fire). These leak
    through with the team member's name as the visible label and
    confuse the user — the dashboard claims it's waiting on an external
    party when really it's waiting on someone on the team.

    Codified 2026-05-05 per user request: 'this should be specific to
    non-team members actions ... should NOT include team members.'
    """
    fc_team = []
    try:
        principal_name = (_CTX.get('principal') or {}).get('name') or ''
        if principal_name: fc_team.append(principal_name.lower())
        for t in (_CTX.get('team') or []):
            n = (t.get('name') or '').lower()
            if n: fc_team.append(n)
            # First-token form too — many doc rows just say "Mark"
            if n: fc_team.append(n.split()[0])
    except Exception:
        pass
    # Add tenant firm shortname / principal first-name fallbacks
    try:
        fname = (_CTX.get('firm') or {}).get('short_name') or ''
        if fname: fc_team.append(fname.lower())
        fname2 = (_CTX.get('firm') or {}).get('name') or ''
        if fname2: fc_team.append(fname2.lower())
    except Exception:
        pass
    fc_team = [t for t in fc_team if t]
    if not fc_team:
        return items  # no firm context loaded — pass through unchanged

    kept = []
    for item in items:
        cp_low = (item.get('counterparty') or '').strip().lower()
        if not cp_low:
            kept.append(item); continue
        # Match if the canonical counterparty IS a team member or the
        # firm name itself. Whole-word match against the lowered cp
        # string so "Markus Acme" doesn't trip on "mark."
        is_team = False
        for needle in fc_team:
            # Anchor: cp_low equals needle, starts with needle+space/punct,
            # or ends with space/punct+needle. This avoids matching needle
            # as a substring of an unrelated counterparty.
            if cp_low == needle: is_team = True; break
            if cp_low.startswith(needle + ' ') or cp_low.startswith(needle + ','): is_team = True; break
            if cp_low.endswith(' ' + needle): is_team = True; break
            if (' ' + needle + ' ') in cp_low: is_team = True; break
        if is_team:
            print(
                f'cos-dashboard-fetch: dropping team-member awaiting item '
                f'(cp={item.get("counterparty","")!r}): '
                f'{(item.get("content","") or "")[:80]!r}',
                file=sys.stderr,
            )
            continue
        kept.append(item)
    return kept


def _redupe_after_canonicalization(items):
    """Second-pass dedup AFTER _rederive_counterparty has canonicalized
    counterparties. The initial _merge_awaiting dedup uses (cp_raw,
    content[:60]) as key, so items that re-derive to the same canonical
    don't collapse there.

    This pass dedupes on (canonical_cp, content_stem) where content_stem
    is the lowered content with stop-words and verb-tense variants
    collapsed — e.g. "Send draft Uber lease" / "Share Uber lease draft"
    / "Deliver Uber lease draft" all stem to the same key.
    Keeps the FIRST occurrence (which is the doc-authored row when
    present, since _merge_awaiting puts those first).
    """
    import re as _re
    _STOP = set('a an the to for of and or with on in at by from is be was'.split())
    _VERB_NORM = {
        'send': 'send', 'sent': 'send', 'sending': 'send',
        'share': 'send', 'shared': 'send', 'sharing': 'send',
        'deliver': 'send', 'delivered': 'send', 'delivering': 'send',
        'circulate': 'send', 'circulated': 'send', 'circulating': 'send',
        'forward': 'send', 'forwarded': 'send', 'forwarding': 'send',
        'pass': 'send',
        'follow': 'follow', 'followed': 'follow', 'following': 'follow',
        'confirm': 'confirm', 'confirmed': 'confirm', 'confirming': 'confirm',
    }
    def _stem(text: str) -> str:
        text = (text or '').lower()
        text = _re.sub(r'[^\w\s]', ' ', text)
        toks = []
        for t in text.split():
            if t in _STOP: continue
            if len(t) < 3: continue
            toks.append(_VERB_NORM.get(t, t))
        # First 6 stable tokens make a strong key
        return ' '.join(toks[:6])
    seen = set()
    out = []
    for item in items:
        cp = (item.get('counterparty') or '').strip().lower()
        stem = _stem(item.get('content') or '')
        key = (cp, stem)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)

    # ── Thread-ID dedup ───────────────────────────────────────────────────
    # The same Gmail thread can produce multiple extraction results with
    # different wording (e.g. "confirm meeting slot" vs "confirm call time").
    # After canonicalization these share the same counterparty but differ
    # enough in content that the stem dedup above misses them.
    # Keep only the richest item (longest content) per thread_id.
    # 2026-05-12: added after iSquared thread 19e02cdfce993a2a produced 3 items.
    by_thread: dict = {}
    for item in out:
        tid = (item.get('source_ref') or {}).get('thread_id') or ''
        if not tid:
            continue
        existing = by_thread.get(tid)
        if existing is None or len(item.get('content') or '') > len(existing.get('content') or ''):
            by_thread[tid] = item

    if by_thread:
        thread_keep = set(id(v) for v in by_thread.values())
        before_t = len(out)
        out = [
            item for item in out
            if not (item.get('source_ref') or {}).get('thread_id')
            or id(item) in thread_keep
        ]
        dropped_t = before_t - len(out)
        if dropped_t:
            print(f'_redupe_after_canonicalization: collapsed {dropped_t} same-thread dup(s)',
                  file=sys.stderr)

    return out


def _rederive_counterparty(items):
    """Re-derive `counterparty` from content+context aliases.

    The extractor often sets counterparty to the email sender (e.g.
    an intro broker forwarding a thread about a different deal),
    even when the substantive subject of the action is a DIFFERENT
    counterparty. This pass scans the content + context fields for
    counterparty_alias needles and, when one matches, overrides
    `counterparty` with the canonical. Original raw value preserved
    in `counterparty_raw` for diagnostics.

    Lookup order per item:
      1. content + context tokens → first matching alias canonical wins
      2. fallback: existing counterparty (run through _normalize_cp,
         which strips trailing person names and applies aliases on
         the original counterparty string)

    Codified 2026-05-05 — the dashboard was rendering ~25 substantive
    deal-subject items under the intro broker's name because the
    extractor used the inbound sender's firm as counterparty.
    """
    for item in items:
        raw = (item.get('counterparty') or '').strip()
        if not raw:
            continue
        item.setdefault('counterparty_raw', raw)
        # Build search haystack from substantive fields. context is
        # usually the cleanest single signal.
        haystack = ' '.join([
            str(item.get('context')   or ''),
            str(item.get('content')   or ''),
            str(item.get('what')      or ''),
        ]).lower()
        # Walk aliases in registration order; first hit wins. _CP_ALIASES
        # is [(needle_lc, canonical), ...] from firm_context.
        chosen = None
        for needle, canon in _CP_ALIASES:
            if needle and needle in haystack:
                chosen = canon
                break
        if chosen and chosen.lower() != raw.lower():
            item['counterparty'] = chosen
            continue
        # Fallback: normalize the existing counterparty (strip person,
        # apply aliases on the cp string itself).
        norm = _normalize_cp(raw)
        if norm and norm != raw:
            item['counterparty'] = norm
    return items


def _auto_expire_stale_events(items):
    """Drop stale/resolved awaitingExternal items.

    Two classes are removed:
    1. [RESOLVED] items — action was completed; should not appear in awaiting list.
    2. One-time-event items (conference, RSVP, scheduling, site/ranch visit, etc.)
       whose due/addedDate is more than 7 days in the past. Items with no date are kept.
    3. Any item whose due date is more than 14 days in the past, regardless of
       content shape — "if it was due two weeks ago and is still showing,
       the human dropped it." Catches stale non-event commitments that
       slip past the event-pattern filter (codified 2026-05-05).
    """
    from datetime import date as _date
    today = _date.today()
    kept = []
    for item in items:
        content = (item.get('content') or '') + ' ' + (item.get('what') or '')

        # Class 1: completed items — never show in awaiting list
        if content.lstrip().startswith('[RESOLVED]'):
            print(
                f'cos-dashboard-fetch: dropping [RESOLVED] awaiting item: '
                f'{content[:80].strip()!r}',
                file=sys.stderr,
            )
            continue

        # Class 3 (codified 2026-05-05, tightened 2026-05-21): hard 3-day
        # past-due cutoff. If due is >3 days in the past, drop regardless of
        # content shape. The 14-day grace originally codified here let a 65%
        # backlog accumulate (39/60 items past-due, mostly Otter-call
        # extractions that nothing ever marked resolved). 3 days = enough
        # slack for a late reply but tight enough that ghosts don't bloat
        # the tile. Items with no due date fall through to the event filter.
        due_raw_global = item.get('due') or ''
        if due_raw_global and len(due_raw_global) >= 10:
            try:
                d_global = _date.fromisoformat(due_raw_global[:10])
                if (today - d_global).days > 3:
                    print(
                        f'cos-dashboard-fetch: hard-expire 3d+ past-due item '
                        f'({due_raw_global[:10]}, {(today - d_global).days}d ago): '
                        f'{content[:80].strip()!r}',
                        file=sys.stderr,
                    )
                    continue
            except ValueError:
                pass

        # Class 2: one-time-event patterns with a past date
        if not _STALE_EVENT_PATTERNS.search(content):
            kept.append(item)
            continue
        due_raw = item.get('due') or item.get('addedDate') or ''
        if not due_raw or len(due_raw) < 10:
            kept.append(item)
            continue
        try:
            item_date = _date.fromisoformat(due_raw[:10])
        except ValueError:
            kept.append(item)
            continue
        days_past = (today - item_date).days
        # 2026-05-05: tightened from 7 → 0 day grace past due. Calls /
        # conferences / scheduled meetings are time-bound — once the
        # due date passes (today + 1 onward), the event has happened
        # or been missed and the item is dead. The user reported 5
        # specific items lingering with due=yesterday despite the
        # underlying event already occurring (e.g. calls, conferences, meetings
        # with past due dates). >=1 day past now drops, while
        # day-of (days_past==0) still renders so the user sees the
        # reminder on the day itself.
        if days_past >= 1:
            print(
                f'cos-dashboard-fetch: auto-expired stale event item '
                f'({due_raw[:10]}, {days_past}d ago): '
                f'{content[:80].strip()!r}',
                file=sys.stderr,
            )
            continue
        kept.append(item)
    return kept


def parse_followups(text):
    # Workstream codes: 'deals' is the canonical code (pre-E1 code was a tenant-specific name).
    # Display labels come from firm_context.yaml :: workstream_categories.
    _ws_deal_label = (_CTX.get('workstream_categories') or {}).get('deal') or _fc.workstream_deal(_CTX) or 'Deals'
    ws_map = {'Job Search': 'job', _ws_deal_label: 'deals', 'Personal': 'personal'}
    # Back-compat alias — accept the legacy label (pre-E1 config used firm name as label)
    ws_map.setdefault('Tomac Cove', 'deals')  # noqa: tenant-leak — backward-compat label
    today = datetime.now().strftime('%Y-%m-%d')

    # Drive doc IDs for source hyperlinking — keyed by lowercase fragment.
    # Doc IDs come from DOC_IDS (which reads firm_context.yaml :: google_docs)
    # so they are tenant-specific and never hardcoded here.
    _dp_url  = f'https://docs.google.com/document/d/{DOC_IDS["deal_pipeline"]}/edit'  if DOC_IDS.get("deal_pipeline")  else ''
    _rec_url = f'https://docs.google.com/document/d/{DOC_IDS["recruiting"]}/edit'     if DOC_IDS.get("recruiting")     else ''
    SOURCE_LINKS = {
        'gmail':      ('https://mail.google.com/mail/u/0/#search/from%3A' +
                       (MY_EMAIL.replace('@', '%40') if MY_EMAIL else '')) if MY_EMAIL else 'https://mail.google.com/mail/u/0/',
        'email':      'https://mail.google.com/mail/u/0/',
        'pipeline':   _dp_url,
        'recruiting': _rec_url,
        'calendar':   'https://calendar.google.com/',
        'otter':      'https://drive.google.com/drive/folders/1zJly0cCiqsbZ3umYBXse7nYE7tUpFGOr',
        'transcript': 'https://drive.google.com/drive/folders/1zJly0cCiqsbZ3umYBXse7nYE7tUpFGOr',
        'call':       'https://drive.google.com/drive/folders/1jYntgSVBsW5-5rdx18TeZhHRsI9xT74p',
    }

    def source_url(src_raw, linked_to):
        """Return a URL for the source field — specific doc/search when possible."""
        import urllib.parse
        lt = linked_to.strip()
        lt_lower = lt.lower()
        src_lower = src_raw.lower()

        # ── Google Doc sources (specific doc IDs) ──
        # Route to deal-pipeline doc when the linked_to text mentions the
        # pipeline or any deal keyword (loaded from firm_context via _DEAL_KEYS).
        if _dp_url and (any(k in lt_lower for k in ['pipeline', 'weekly docket', 'weekly call'])
                        or any(k in lt_lower for k in _DEAL_KEYS)):
            return _dp_url
        # Route to recruiting doc when linked_to matches any recruit keyword
        # (loaded from firm_config.json via _RECRUIT_KEYS).
        if _rec_url and any(k in lt_lower for k in _RECRUIT_KEYS):
            return _rec_url

        # ── Otter/transcript — link to specific subfolder ──
        if any(k in src_lower for k in _RECRUIT_KEYS):
            return 'https://drive.google.com/drive/folders/1tMEGofeqzfF93YhPCyGe0dgJj8tzdRlF'  # Otter/Recruiting
        if any(k in src_lower for k in _DEAL_KEYS):
            return 'https://drive.google.com/drive/folders/1pHmuq_TfLY46GDg0BzRIwrq57ictIT5S'  # Otter/TC
        if 'transcript' in src_lower or 'otter' in src_lower:
            return 'https://drive.google.com/drive/folders/1dt-s-D1SWaTrpIEsi0GiBAu1BCQCoPGq'  # Otter/Other
        if 'call recording' in src_lower or ('call' in src_lower and 'recording' in src_lower):
            return 'https://drive.google.com/drive/folders/1jYntgSVBsW5-5rdx18TeZhHRsI9xT74p'  # Call Recordings

        # ── Gmail — build search URL from linked_to subject line ──
        if lt and any(k in lt_lower for k in ['re:', 'fwd:', 'my resume', 'intro', 'reaching out',
                                               'quick intro', 'connecting', 'open position', 'opportunity']):
            q = urllib.parse.quote(f'subject:"{lt}"')
            return f'https://mail.google.com/mail/u/0/#search/{q}'
        if any(k in src_lower for k in ['gmail', 'email']):
            if lt:
                q = urllib.parse.quote(f'"{lt}"')
                return f'https://mail.google.com/mail/u/0/#search/{q}'
            return 'https://mail.google.com/mail/u/0/'

        # ── Calendar ──
        if 'calendar' in src_lower:
            return 'https://calendar.google.com/'

        # ── Fallback: check SOURCE_LINKS keywords ──
        for key, url in SOURCE_LINKS.items():
            if key in src_lower:
                return url
        return ''

    results = []
    awaiting = []  # Rows tagged [waiting] / [awaiting] — route to awaitingExternal[]
    import re as _re_fu
    _WAIT_TAG = _re_fu.compile(r'^\s*\[\s*(waiting|awaiting)(?:\s+on\s+[^\]]*)?\s*\]\s*',
                               _re_fu.IGNORECASE)
    # HISTORICAL guard — rows from calls older than 7 days are tagged
    # [HISTORICAL — call YYYY-MM-DD] by cos_otter_backfill.py so the dashboard
    # / action layer can filter them out. They remain in the Follow-ups doc
    # for archival but never surface as live action items.
    _HIST_TAG = _re_fu.compile(r'^\s*\[\s*HISTORICAL\b', _re_fu.IGNORECASE)
    for line in text.split('\n'):
        line = line.strip()
        if not line.startswith('|') or '---|' in line:
            continue
        parts = [p.strip() for p in line.split('|') if p.strip()]
        if len(parts) < 5 or not parts[0].isdigit():
            continue
        _, who, what, due, ws_raw = parts[0], parts[1], parts[2], parts[3], parts[4]
        src           = parts[5] if len(parts) > 5 else ''
        linked_to     = parts[6] if len(parts) > 6 else ''
        context       = parts[7] if len(parts) > 7 else ''
        dashboard_path = parts[8] if len(parts) > 8 else ''
        due_clean = due if due and due != 'TBD' else ''

        # ── [HISTORICAL] tag — skip rows from stale calls (>7d old) ─────────
        # The Follow-ups doc retains the row for archival; dashboard hides it.
        # Two detection paths:
        #   1. Forward-going: cos_otter_backfill.py prepends [HISTORICAL — call YYYY-MM-DD] to `what`
        #   2. Retroactive: source column contains "call — YYYY-MM-DD" where date > 7d ago
        if _HIST_TAG.match(what):
            continue
        # Retroactive guard — match "call — YYYY-MM-DD..." source pattern
        try:
            _src_date_match = _re_fu.search(r'call\s*[—\-]\s*(\d{4}-\d{2}-\d{2})', src or '')
            if _src_date_match:
                _src_date = _src_date_match.group(1)
                if _src_date < (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d'):
                    continue
        except Exception:
            pass

        # ── [waiting] tag support (ROUTING-SPEC §4.1) ──────────────────────
        # A row prefixed with [waiting] or [awaiting on X] is a third-party
        # commitment Yoni is chasing — routes to awaitingExternal[] card
        # rather than the followUps list. Strip the tag from the content.
        wait_match = _WAIT_TAG.match(what)
        if wait_match:
            what_clean = _WAIT_TAG.sub('', what).strip()
            awaiting.append({
                'content_type':   'awaiting_external',
                'owner':          'external',
                'counterparty':   who,
                'parent_id':      '',
                'due':            due_clean,
                'context':        context,
                'dashboard_path': dashboard_path,
                'content':        what_clean,
                'source_ref': {
                    'type':    'manual',
                    'title':   'Follow-ups doc',
                    'doc_url': 'https://docs.google.com/document/d/10leX26u8n3XkoCHzg7SDwLUodVX2CqKjvXcSJ-KAsCY/edit',
                    'date':    today,
                },
                'urgent':         bool(due_clean and due_clean <= today),
            })
            continue

        results.append({
            'who':           who,
            'what':          what,
            'due':           due_clean,
            'workstream':    ws_map.get(ws_raw, ws_raw.lower()),
            'source':        src,
            'sourceUrl':     source_url(src, linked_to),
            'linkedTo':      linked_to,
            'context':       context,
            'dashboardPath': dashboard_path,
            'urgent':        bool(due_clean and due_clean <= today),
        })
    return results, awaiting

def parse_section(text, skip_headings):
    sections = []
    current_head, current_lines = None, []
    for line in text.split('\n'):
        if line.startswith('## '):
            if current_head and current_head not in skip_headings:
                sections.append((current_head, current_lines))
            current_head = line[3:].strip()
            current_lines = []
        elif current_head is not None:
            current_lines.append(line)
    if current_head and current_head not in skip_headings:
        sections.append((current_head, current_lines))
    return sections

def field(lines, key):
    prefix = f'- **{key}:**'
    for l in lines:
        s = l.strip()
        if s.startswith(prefix):
            return s[len(prefix):].strip()
    return ''

def parse_recruiting(text):
    results = []
    for heading, lines in parse_section(text, {'Template', 'Recruiting Pipeline'}):
        stage = field(lines, 'Stage') or 'Outreach'
        if stage == 'Closed':
            continue
        results.append({
            'name':       field(lines, 'Firm') or heading,
            'stage':      stage,
            'contact':    field(lines, 'Key contacts'),
            'next':       field(lines, 'Next step'),
            'lastAction': field(lines, 'Last action'),
            'note':       '',
        })
    return results[:25]

def parse_deal_pipeline(text):
    """Parse the deal-pipeline doc into deal cards.
    Renamed from parse_deal_doc in PLAN E1.1. Old name kept as alias."""
    results = []
    _deal_label = (_CTX.get('workstream_categories') or {}).get('deal') or _fc.workstream_deal(_CTX) or 'Deals'
    skip = {f'{_deal_label} — Deal Pipeline', 'Template'}
    # Dated log/journal headers are section artifacts, not deals.
    # Matches "Update Log — 2026-04-14", "Update Log - 2026-04-14", "Log 2026-04-14", etc.
    _LOG_HEADER_RE = re.compile(r'^(update\s+)?log\b', re.IGNORECASE)
    for heading, lines in parse_section(text, skip):
        if heading and _LOG_HEADER_RE.match(heading.strip()):
            continue
        stage = field(lines, 'Stage') or 'Sourcing'
        if stage in ('Closed', 'Pass'):
            continue
        eval_lines, history = [], []
        in_eval = in_hist = False
        for l in lines:
            s = l.strip()
            if s == '- **Eval notes:**':  in_eval = True;  in_hist = False; continue
            if s == '- **History:**':      in_hist = True;  in_eval = False; continue
            if re.match(r'- \*\*\w', s) and s != '- **':
                in_eval = in_hist = False
            if in_eval and s.startswith('- '):
                eval_lines.append(s[2:])
            elif in_hist and s.startswith('- '):
                m = re.match(r'(\d{4}-\d{2}-\d{2}):\s*(.*)', s[2:])
                if m:
                    history.append({'date': m.group(1), 'text': m.group(2)})
        thesis = ' '.join(eval_lines[:2]) if eval_lines else ''
        results.append({
            'name':     heading,
            'stage':    stage,
            'sector':   field(lines, 'Sector'),
            'size':     field(lines, 'Est. EV'),
            'source':   field(lines, 'Source'),
            'contacts': field(lines, 'Key contacts'),
            'thesis':   thesis,
            'nextStep': field(lines, 'Next step'),
            'notes':    history,
        })
    return results

# Back-compat alias — kept for 1 release so any external caller
# importing parse_tomac still works. Remove in next major release.
parse_tomac = parse_deal_pipeline  # noqa: tenant-leak — backward-compat alias, remove in next major

# ── Deal-card freshest-signal overlay (Gap B) ─────────────────────────────
# Static nextStep from the doc goes stale the moment a call or email
# produces new commitments. We overlay it with the earliest-due open
# my_action / awaiting_external that references this deal, and we expose a
# `latestUpdate` line for a "Latest: …" strip at the top of the card.
_DEAL_STOPWORDS = {
    'the','a','an','and','or','of','for','in','on','to','at','by','with',
    'deal','call','site','project','energy','hub','corp','inc','llc','lp',
    'co','cove','capital','partners','fund','holdings','group','company',
    'via','update','log',
}

def _deal_tokens(name):
    import re as _re
    toks = {t for t in _re.findall(r'[A-Za-z0-9]+', (name or '').lower()) if len(t) >= 3}
    return toks - _DEAL_STOPWORDS

def _item_mentions_deal(item, deal_tokens):
    """True if any string field of `item` contains at least one deal token."""
    if not deal_tokens:
        return False
    hay = ' '.join(str(item.get(k, '') or '') for k in (
        'who','what','counterparty','content','context','parent_id','source','dashboard_path'
    )).lower()
    return any(t in hay for t in deal_tokens)

def _overlay_freshest_signal(deals, followups, envelope_items, today_str):
    """For each deal, find the earliest-due open follow-up or awaiting_external
    that mentions it; overlay nextStep; expose latestUpdate + freshSignal.

    Preserves the original doc-parsed nextStep as `nextStepDoc`. Conservative:
    only overlays when a real matching item exists; otherwise leaves the
    doc version untouched.
    """
    if not deals:
        return deals
    # Index envelope items by deal tokens (one pass over envelope)
    for deal in deals:
        name = deal.get('name', '')
        if not name or name.startswith('Update Log'):
            continue
        tokens = _deal_tokens(name)
        if not tokens:
            continue

        # Collect candidate signals
        matched_fus = []
        for fu in followups or []:
            if fu.get('workstream') not in (None, '', 'tomac', 'deals'):  # noqa: tenant-leak — 'tomac' is backward-compat workstream code
                continue
            if _item_mentions_deal(fu, tokens):
                matched_fus.append(fu)
        matched_env = [it for it in envelope_items or []
                       if _item_mentions_deal(it, tokens)
                       and it.get('content_type') in ('my_action','awaiting_external','deal_takeaway','origination_idea')]

        # Pick freshest: most-recently-added fundraising-relevant action wins.
        # Rationale: a stale overdue ranch-visit reminder shouldn't mask today's
        # fundraising-focus action captured from a fresh call. When a deal has
        # multiple actions with the same addedDate (common — a call generates a
        # batch of items all stamped today), prefer ones that contain strong
        # fundraising / diligence terms (FEA, LC, teaser, CIM, term sheet,
        # structure, refund, raise, post, IRA credit, milestone, bridge) over
        # scheduling / meeting-logistics actions.
        _FUNDRAISING_HINT = (
            'fea','teaser','cim','term sheet',' lc ','letter of credit','post',  # noqa: tenant-leak
            'milestone','refund','refundable','bridge','raise','anchor','tcip',  # noqa: tenant-leak
            'ic memo','ira','credit','structure','phase 2','2,000 mw','400 mw',  # noqa: tenant-leak
            '150 mw','oncor','pclr','capital','fundraise','fundraising',  # noqa: tenant-leak
        )
        _SCHEDULING_HINT = (
            'ranch visit','schedule','reschedule','meeting','in-person',
            'dates','alternative dates','coordinate','confirm via',
        )
        def _fundraising_weight(text):
            t = (text or '').lower()
            score = 0
            for kw in _FUNDRAISING_HINT:
                if kw in t: score += 1
            for kw in _SCHEDULING_HINT:
                if kw in t: score -= 1
            return score

        def _sort_key_action(x):
            added = x.get('addedDate') or ''
            due = x.get('due') or '9999-12-31'
            weight = _fundraising_weight(x.get('what',''))
            # Sort ASC: prefer rows *with* addedDate, then most-recent addedDate,
            # then *higher* fundraising weight (negate), then earliest due.
            return (added == '', tuple(-ord(c) for c in added), -weight, due)

        # [RESOLVED] prefix means the action was completed — never surface as nextStep
        actions = [f for f in matched_fus
                   if f.get('what') and not (f.get('what') or '').startswith('[RESOLVED]')]
        aw_all  = [e for e in matched_env if e.get('content_type') == 'awaiting_external'
                   and not (e.get('content') or '').startswith('[RESOLVED]')]
        # Prefer open (future-due or undated) awaiting items; fall back to overdue if nothing open
        aw_open = [e for e in aw_all if not e.get('due') or e.get('due') >= today_str]
        aw      = aw_open if aw_open else aw_all
        best_action = sorted(actions, key=_sort_key_action)[0] if actions else None
        best_await  = min(aw, key=lambda e: (e.get('due') or '9999-12-31')) if aw else None

        # Preserve original doc-parsed nextStep; clear if already resolved
        raw_ns = deal.get('nextStep', '') or ''
        deal['nextStepDoc'] = raw_ns
        if raw_ns.startswith('[RESOLVED]'):
            deal['nextStep'] = ''

        # Build "Latest: …" one-liner from best available signal
        latest_txt  = ''
        latest_date = ''
        latest_kind = ''
        if best_action:
            latest_txt  = best_action.get('what', '')[:200]
            latest_date = best_action.get('addedDate') or best_action.get('due') or ''
            latest_kind = 'action'
        elif best_await:
            latest_txt  = best_await.get('content', '')[:200]
            latest_date = best_await.get('due') or ''
            latest_kind = 'awaiting'
        elif matched_env:
            # fall back to most-recent deal_takeaway / origination_idea
            it = matched_env[0]
            latest_txt  = (it.get('content') or '')[:200]
            latest_kind = it.get('content_type', 'intel')

        if latest_txt:
            deal['latestUpdate'] = {
                'text': latest_txt,
                'date': latest_date,
                'kind': latest_kind,
            }
            # Overlay nextStep only if we have a concrete action/awaiting item
            if best_action:
                deal['nextStep']    = best_action.get('what', '') or deal['nextStepDoc']
                deal['freshSignal'] = True
            elif best_await:
                deal['nextStep']    = f"(awaiting) {best_await.get('content','')}"
                deal['freshSignal'] = True
            else:
                deal['freshSignal'] = False
        else:
            deal['freshSignal'] = False

        deal['signalCount'] = len(actions) + len(matched_env)
    return deals

# ── Gap A: auto-promote an origination cluster to a Tomac deal ──────────
# User rule: "once a deal has conversation about raising funds for them it
# becomes a real deal." We scan the envelope arrays for counterparties that
# aren't already in the Tomac deal list, and where the associated content
# contains explicit, *deal-scoped* fundraising signals — then we synthesize
# a Deal entry so it surfaces on the dashboard at its first
# appearance instead of waiting for a manual doc edit.
#
# Predicate is deliberately narrow — loose heuristics here flood the deal
# list with LP names, colleague first names, and advisory-firm references.
# Requirements:
#   (1) Source = envelope items only (origination_idea | deal_takeaway |
#       awaiting_external). Followups are owner-side action tracking and
#       are not promotion evidence.
#   (2) Counterparty must be deal-shaped — either contain a firm suffix
#       (Capital, Partners, Energy, Solutions, Ventures, Digital, LLC,
#       Corp, Inc, LP, Holdings, Co) or include a " / " / " — " joiner
#       suggesting "Firm / Person" form. Pure first names are rejected.
#   (3) Cluster must contain ≥1 *strong* fundraising term (deposit, FEA,
#       CIM, teaser, term sheet, bid, bridge equity, anchor, raise, check
#       size, JV, project finance) — "capital"/"fund" alone don't count.
#   (4) Cluster size ≥ 3 items (signal, not a single mention).
#   (5) Counterparty is not already in Tomac deal names and not in lpData.
_STRONG_FUNDRAISING = (
    'deposit', 'fea', 'cim', 'teaser', 'term sheet', 'bridge equity',
    'anchor capital', 'raise', 'check size', 'project finance',
    'refund', 'tcip anchor', 'tcip participation', 'binding bid',  # noqa: tenant-leak (deal-fundraise vocab corpus)
    'ic memo', 'ic approval', 'bridge to equity', 'equity bridge',
    'joint venture', ' jv ', 'mezz', 'preferred equity',
)
_FIRM_SUFFIXES = (
    'capital', 'partners', 'energy', 'solutions', 'ventures', 'digital',
    'power', 'infrastructure', 'holdings', 'group', 'corp', 'inc',
    'llc', 'lp', 'co ', 'company', 'fund', 'ltd', 'plc', 'hub',
    'systems', 'networks', 'fiber', 'midstream', 'gas', 'resources',
)

def _has_strong_fundraising_signal(item):
    hay = ' '.join(str(item.get(k, '') or '') for k in (
        'what','counterparty','content','context','dashboard_path'
    )).lower()
    return any(t in hay for t in _STRONG_FUNDRAISING)

    # Counterparty aliases — different spellings of the same deal/firm should
    # collapse to one key so the auto-promoter doesn't create parallel deal
    # rows and the awaiting-external UI doesn't show the same deal twice.
    # Keyed by *lowercased full raw counterparty* (after _normalize_cp splits
    # on separators, we also check the raw string contents).
# Counterparty aliases — loaded from firm_context.yaml (counterparty_aliases section).
# Edit firm_context.yaml to add/change aliases; do not hardcode deal or person names here.
_CP_ALIASES = _fc.cp_aliases(_CTX)

def _normalize_cp(name):
    """Normalize a counterparty string for grouping. Splits on separators
    and keeps the firm/org half, dropping trailing person names. Applies
    a small alias table so alternative spellings of the same deal collapse
    to a canonical name."""
    import re as _re
    raw = (name or '').strip()
    low = raw.lower()
    for needle, canon in _CP_ALIASES:
        if needle in low:
            return canon
    base = _re.split(r'\s*[—–/|,]\s*', raw, maxsplit=1)[0]
    base = _re.sub(r'\s*\(.*?\)\s*$', '', base).strip()
    return base

_PEER_GP_DENYLIST = {  # noqa: tenant-leak (peer GP / firm denylist — generic infra-PE peer firms; subscribers can extend via firm_context.yaml :: peer_firms[])
    'arclight','arclight capital','arclight capital partners',
    'stonepeak','stonepeak partners','stonepeak infrastructure',
    'i squared','i squared capital','ecp','energy capital partners',
    'quantum','quantum capital','quantum energy partners',  # noqa: tenant-leak
    'kkr','kkr infra','kkr infrastructure',
    'tpg','tpg rise climate','brookfield','brookfield infra',
    'blackstone','blackstone infra','blackrock','blackrock infra','msip',
    'ls power','nuveen','nuveen infrastructure','ridgewood',  # noqa: tenant-leak
    'pennybacker','pennybacker capital','lockfront','walker lockfront',
    'capstone','vinson elkins','perkins coie','v&e','v and e','v&amp;e',
    'cologix','track capital','mercuria','gcm','grosvenor',  # noqa: tenant-leak
    'encore','oncor',  # utilities/counterparties referenced around deals
    'apollo','carlyle','eqt','antin','global infrastructure partners','gip',
    'macquarie','ontario teachers','ontario teachers pension',
    'industry funds management','ifm','cdpq','caisse',
    'goldman sachs','morgan stanley','jefferies','jpmorgan',
    'fit ventures','thunderhead','thunderhead dg',  # noqa: tenant-leak
    # Government counterparties / advisors — not deals
    'export-import bank of the united states','exim','exim bank',
    'doe','department of energy','dfc','osc',
    # Publications / research sources
    'bank street group llc','bank street group','bank street',
    'rbn energy','utility dive','capstone dc','capstone','substack',
    # Financing / capital partners (not themselves deals)
    'shorebridge capital','shorebridge','hamilton lane','cliffwater',
    'new york life','manulife',
    # Event / forum co-hosts — deal *context* but not deals
    'black mountain',
    # Parse artifacts / placeholders
    'unknown','template','tbd','n/a','update log','log',
}

def _is_deal_shaped_cp(cp, raw_cp=None):
    """True if the counterparty looks like a firm/asset/brand.

    Accept shapes:
      - Raw counterparty contains a separator (— – / ,) AND firm-half is
        ≥3 chars: treats "<deal> — <principal>" as deal-shaped (firm half
        "<deal>"). This is the dominant pattern for origination items.
      - Firm-suffix match (Capital, Partners, Energy, etc.)
      - Multi-word (≥2 tokens) AND ≥7 chars AND not a Title Case two-name.
    Reject common first-name stems AND known LP/peer GPs outright.
    """
    if not cp or len(cp) < 3:
        return False
    low = cp.lower().strip()
    REJECT_NAMES = _OWNER_REJECT | {
        # Generic single-name common-noise tokens — keep these
        # tenant-agnostic; they reject any "First-name only" raw cp.
        'kevin','tim','dan','david','brian','joey','andrew','mike','john','matt',
        'chris','tom','paul','bob','rob','steve','sam','pete','will','greg',
        'ian','ryan','max','joe','adam','alex','ben','ed',
        'frank','gary','henry','james','kate','laura','lisa','molly','nate',
        'oscar','peter','rick','scott','ted','victor','walter',
    }
    if low in REJECT_NAMES:
        return False
    # Reject known LP / peer GPs / advisors / law firms / counterparty utils —
    # these show up in deal conversations but are not themselves deals.
    if low in _PEER_GP_DENYLIST:
        return False
    # Also reject if the normalized name starts with a peer GP name (handles
    # "ArcLight Capital Partners" variants).
    for peer in _PEER_GP_DENYLIST:
        if low == peer or low.startswith(peer + ' '):
            return False

    # Shape 1: raw cp had a separator with person on the other side.
    # We've already stripped everything after the separator in _normalize_cp,
    # so detect this by looking at the raw form.
    if raw_cp:
        if any(sep in raw_cp for sep in (' — ', ' – ', '—', '–', '/', ',')):
            return len(cp) >= 3

    has_suffix = any(s in low for s in _FIRM_SUFFIXES)
    if has_suffix:
        return True

    tokens = cp.split()
    # Reject pure "First Last" where both tokens look like given names
    if len(tokens) == 2 and all(
        t[:1].isupper() and t[1:].islower() for t in tokens if len(t) > 1
    ):
        return False
    return len(tokens) >= 2 and len(cp) >= 7

def _auto_promote_origination(existing_deals, followups, envelope_items, today_str, lp_names=None):
    """Synthesize deal entries for counterparties with deal-scoped
    fundraising evidence that aren't already deals. Returns
    (augmented_deals, promoted_names)."""
    if not existing_deals:
        existing_deals = []
    existing_names = {_normalize_cp(d.get('name','')).lower() for d in existing_deals if d.get('name')}
    existing_names.discard('')
    lp_set = {(l.get('name','') or '').lower() for l in (lp_names or [])}
    lp_set.discard('')

    # Cluster envelope items only (not follow-ups) by normalized counterparty
    from collections import defaultdict
    clusters = defaultdict(list)
    for it in envelope_items or []:
        ct = it.get('content_type')
        if ct not in ('origination_idea','deal_takeaway','awaiting_external'):
            continue
        raw_cp = it.get('counterparty') or it.get('parent_id') or ''
        cp = _normalize_cp(raw_cp)
        if not cp: continue
        cp_low = cp.lower()
        if cp_low in existing_names: continue
        if cp_low in lp_set: continue
        if not _is_deal_shaped_cp(cp, raw_cp=raw_cp): continue
        clusters[cp].append(('env', it))
    # Narrow dedup: if a cluster's normalized name appears verbatim (case-
    # insensitive) inside another cluster's *raw* counterparty strings, they
    # are likely the same deal under two spellings (e.g. "AlphaEnergy" appears
    # inside "AlphaEnergy / DealX — Jane Doe"). Merge the smaller into
    # the larger.
    def _raw_cps(items):
        return [str((it.get('counterparty') or it.get('parent_id') or '')).lower() for _, it in items]

    names_by_size = sorted(clusters.keys(), key=lambda n: -len(clusters[n]))
    for a in list(names_by_size):
        if a not in clusters: continue
        a_low = a.lower()
        for b in list(clusters.keys()):
            if b == a or b not in clusters: continue
            b_raws = _raw_cps(clusters[b])
            if any(a_low in r for r in b_raws) or any(b.lower() in r for r in _raw_cps(clusters[a])):
                # Merge the smaller name's items into the larger
                big, small = (a, b) if len(clusters[a]) >= len(clusters[b]) else (b, a)
                if big in clusters and small in clusters and big != small:
                    clusters[big].extend(clusters[small])
                    del clusters[small]

    # Associate matching followups to each surviving cluster (for nextStep)
    for cp in list(clusters.keys()):
        tokens = _deal_tokens(cp)
        # Also pull tokens from items' parent_id / context so followups that
        # mention the asset (e.g. "Big South Dallas") match a cluster keyed
        # on the firm ("DealX").
        import re as _re
        for _, it in clusters[cp]:
            for field_name in ('parent_id','context'):
                for tok in _re.findall(r'[a-z0-9]+', str(it.get(field_name,'') or '').lower()):
                    if len(tok) >= 5 and tok not in _DEAL_STOPWORDS:
                        tokens.add(tok)
        for fu in followups or []:
            if not _is_deal_ws(fu.get('workstream')): continue
            if _item_mentions_deal(fu, tokens):
                clusters[cp].append(('fu', fu))

    promoted_names = []
    for cp, items in clusters.items():
        # Require ≥3 pieces of evidence AND ≥1 strong fundraising signal AND
        # ≥1 origination_idea OR awaiting_external (i.e. a call/email touch
        # on this counterparty — not just a research deal_takeaway)
        if len(items) < 3:
            continue
        if not any(_has_strong_fundraising_signal(x) for _, x in items):
            continue
        env_items_only = [x for k, x in items if k == 'env']
        has_touch = any(
            x.get('content_type') in ('origination_idea','awaiting_external')
            for x in env_items_only
        )
        if not has_touch:
            continue

        # Pick the freshest action for nextStep
        fu_items  = [x for k, x in items if k == 'fu']
        env_items = [x for k, x in items if k == 'env']
        aw        = [e for e in env_items if e.get('content_type') == 'awaiting_external']
        ori       = [e for e in env_items if e.get('content_type') == 'origination_idea']
        tak       = [e for e in env_items if e.get('content_type') == 'deal_takeaway']
        status    = [e for e in env_items if e.get('content_type') == 'status_update']

        next_step = ''
        latest_txt = ''
        latest_kind = ''
        latest_date = ''
        if fu_items:
            # Prefer most recently ADDED action as nextStep. An old
            # overdue action with a past due date shouldn't dominate
            # over a fresh action captured today — yesterday's ranch-
            # visit reminder was masking today's Oncor FEA fundraising
            # focus on a specific deal, for example. Sort by addedDate desc,
            # fall back to due date asc when addedDate is missing.
            def _fu_sort_key(f):
                added = f.get('addedDate') or ''
                due   = f.get('due') or '9999-12-31'
                # Negative string trick: sort by (-added, due). Python
                # can't negate strings, so we flip with a tuple of
                # (inverse added, due).
                return (added == '', -len(added), tuple(-ord(c) for c in added), due)
            top = sorted(fu_items, key=_fu_sort_key)[0]
            next_step = top.get('what','')
            latest_txt = top.get('what','')
            latest_date = top.get('addedDate') or top.get('due','')
            latest_kind = 'action'
        elif aw:
            top = min(aw, key=lambda e: (e.get('due') or '9999-12-31'))
            next_step = f"(awaiting) {top.get('content','')}"
            latest_txt = top.get('content','')
            latest_date = top.get('due','')
            latest_kind = 'awaiting'

        # Thesis: synthesize from origination_idea first, else deal_takeaway
        thesis_bits = [o.get('content','') for o in (ori + tak)][:2]
        thesis = ' '.join(t for t in thesis_bits if t)[:500]

        # Best guess at sector / size from origination_idea context
        sector = ''
        for o in ori:
            ctx = o.get('context','')
            if ctx:
                # context format from the extractor is "<kind> — <detail>"
                sector = ctx.split('—')[-1].strip()[:80]
                break
        if not sector and tak:
            sector = (tak[0].get('context','').split('—')[-1] or '').strip()[:80]

        notes = []
        for s in status[-5:]:
            content = s.get('content','')
            if content:
                notes.append({'date': today_str, 'text': content[:280]})

        synth = {
            'name':     cp,
            'stage':    'Sourcing / Auto',
            'sector':   sector,
            'size':     '',
            'source':   'origination — transcript/email ingest (auto-promoted on fundraising signal)',
            'contacts': '',
            'thesis':   thesis,
            'nextStep': next_step,
            'nextStepDoc': '',
            'notes':    notes,
            'latestUpdate': {'text': latest_txt, 'date': latest_date, 'kind': latest_kind} if latest_txt else None,
            'freshSignal': True,
            'signalCount': len(items),
            '_synthesized': True,
            '_promotedAt':  today_str,
        }
        existing_deals.append(synth)
        promoted_names.append(cp)

    return existing_deals, promoted_names

def parse_lp_data(text):
    """Extract LP Investor Intel sections from the deal pipeline doc.

    Looks for blocks starting with '══' containing 'LP INVESTOR INTEL' or
    '## LP' headings, then parses each named investor entry.

    Returns list of dicts:
      { name, status, statusColor, fit, notes, approach, source }
    """
    STATUS_COLORS = {
        'active':     'green',
        'qualified':  'blue',
        'hold':       'yellow',
        'long-term':  'gray',
        'unknown':    'gray',
        'key resource': 'purple',
        'priority':   'green',
    }

    lps = []

    # Find the LP INVESTOR INTEL block.
    # Structure: ══ separator line, then "LP INVESTOR INTEL..." title line, then ══,
    # then content entries until the next ══ closing separator.
    lines = text.split('\n')
    in_lp_block = False
    lp_lines = []
    passed_second_sep = False
    for l in lines:
        if not in_lp_block:
            if 'LP INVESTOR INTEL' in l:
                in_lp_block = True
                passed_second_sep = False
            continue
        # Skip the closing ══ line immediately after the title
        if not passed_second_sep and l.startswith('══'):
            passed_second_sep = True
            continue
        # End block at the next ══ line after content has started
        if passed_second_sep and l.startswith('══'):
            break
        lp_lines.append(l)

    if not lp_lines:
        return []

    # Parse each investor entry — separated by bold name headers (**Name**)
    # Format: **Name** followed by bullet lines with - key: value or - STATUS: ...
    current_name = None
    current_lines = []

    def flush(name, lines_):
        if not name:
            return
        notes_parts, approach_parts = [], []
        status_raw = 'Unknown'
        fit = ''
        source = ''
        in_notes = False
        for l in lines_:
            s = l.strip()
            if s.startswith('- STATUS:'):
                status_raw = s[9:].strip()
                in_notes = False
            elif s.startswith('- Approach:') or s.startswith('- approach:'):
                approach_parts.append(s.split(':', 1)[1].strip())
                in_notes = False
            elif s.startswith('- Fit:') or s.startswith('- fit:'):
                fit = s.split(':', 1)[1].strip()
                in_notes = False
            elif s.startswith('- Source:') or s.startswith('- source:'):
                source = s.split(':', 1)[1].strip()
                in_notes = False
            elif s.startswith('- ') and s != '- ':
                notes_parts.append(s[2:])

        # Derive color from status text
        status_lower = status_raw.lower()
        color = 'gray'
        for key, val in STATUS_COLORS.items():
            if key in status_lower:
                color = val
                break

        # Extract clean status label (everything before ' —')
        status_label = status_raw.split('—')[0].strip()

        lps.append({
            'name':        name,
            'status':      status_label,
            'statusColor': color,
            'fit':         fit,
            'notes':       ' '.join(notes_parts[:3]),   # first 3 bullet points as notes
            'approach':    ' '.join(approach_parts),
            'source':      source,
        })

    for l in lp_lines:
        s = l.strip()
        # Bold name header: **Name** or **Name / Firm**
        m = re.match(r'^\*\*(.+?)\*\*\s*$', s)
        if m:
            flush(current_name, current_lines)
            current_name = m.group(1).strip()
            current_lines = []
        elif current_name:
            current_lines.append(l)

    flush(current_name, current_lines)
    return lps


def parse_fundraising_strategy(text):
    """Extract high-level fundraising strategy notes from the pipeline doc.

    Looks for Goldman / capital formation call notes and returns a summary dict.
    """
    strategy = {
        'approach':    '',
        'currentFocus': '',
        'lpTargetPool': '',
        'timeline':    '',
        'competitive': [],
    }

    lines = text.split('\n')

    # Look for CAPITAL FORMATION STRATEGY section
    in_cap = False
    cap_lines = []
    for l in lines:
        if 'CAPITAL FORMATION STRATEGY' in l:
            in_cap = True
            continue
        if in_cap:
            if l.startswith('─') or l.startswith('═') or (l.startswith('[20') and in_cap):
                if cap_lines:
                    break
            cap_lines.append(l.strip())

    strategy['approach'] = ' '.join([l for l in cap_lines if l][:4])

    # Competitive intel
    for l in lines:
        if 'Nova Hampton' in l and ('1.5B' in l or '$1.5' in l or 'billion' in l.lower()):
            strategy['competitive'].append('Nova Hampton: ~$1.5B raise (Oct 2025 start, co-invest → fund)')
        if 'Tallvine' in l and ('1.5B' in l or '$1.5' in l or 'debut' in l.lower()):
            strategy['competitive'].append('Tallvine: $1.5B debut fund (infra spinout, active raise)')

    return strategy


def parse_briefing_log(text):
    """Extract the most recent Capture Summary and Briefing from the Personal Briefing Log.

    Returns a dict:
      {
        'captureSummary': { 'date': 'YYYY-MM-DD', 'deals': [...], 'recruiting': [...], 'other': [...], 'actionItems': [...] },
        'lastBriefingDate': 'YYYY-MM-DD',
        'lastBriefingSnippet': '...',
      }
    """
    result = {
        'captureSummary': None,
        'lastBriefingDate': '',
        'lastBriefingSnippet': '',
    }

    # Find the most recent ## Capture Summary section
    capture_start = None
    capture_date = ''
    lines = text.split('\n')
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith('## Capture Summary'):
            capture_start = i
            # Extract date from heading e.g. "## Capture Summary — 2026-04-14"
            m = re.search(r'(\d{4}-\d{2}-\d{2})', lines[i])
            if m:
                capture_date = m.group(1)
            break

    if capture_start is not None:
        section_lines = []
        for j in range(capture_start + 1, len(lines)):
            if lines[j].startswith('## ') and j > capture_start:
                break
            section_lines.append(lines[j])

        deal_items, recruiting_items, other_items, action_items = [], [], [], []
        current_bucket = None
        for l in section_lines:
            s = l.strip()
            _ws_deal_calls = f"### {_fc.workstream_deal(_CTX)} Calls"
            if s in (_ws_deal_calls, '### Tomac Cove Calls'):  # noqa: tenant-leak — backward-compat header
                current_bucket = deal_items; continue
            if s == '### Recruiting Calls':        current_bucket = recruiting_items; continue
            if s == '### Other Calls':             current_bucket = other_items; continue
            if s == '### Key Action Items From Calls': current_bucket = action_items; continue
            if s.startswith('- ') and current_bucket is not None:
                current_bucket.append(s[2:])

        result['captureSummary'] = {
            'date': capture_date,
            'deals': deal_items,
            'recruiting': recruiting_items,
            'other': other_items,
            'actionItems': action_items,
        }

    # Noise patterns — skip lines that are call transcript chatter, not briefing content
    _NOISE_RE = re.compile(
        r'^(i need|hold on|just a|one second|give me a|sorry|thanks|ok[,.]|okay|'
        r'sure[,.]|yeah[,.]|yes[,.]|no[,.]|right[,.]|got it|sounds good|let me|'
        r'hmm|uh |so\s|we\'re|SPEAKER_\d|you know)',
        re.IGNORECASE,
    )

    # Find the most recent ## Personal Briefing section for snippet
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith('## Personal Briefing'):
            m = re.search(r'(\d{4}-\d{2}-\d{2})', lines[i])
            if m:
                result['lastBriefingDate'] = m.group(1)

            # Prefer the ### Today's Priorities sub-section — most actionable content
            priorities_start = None
            market_intel_start = None
            for j in range(i + 1, min(i + 300, len(lines))):
                if re.match(r'^###\s+(Today|Priorities)', lines[j], re.IGNORECASE):
                    priorities_start = j + 1
                if re.match(r'^###\s+Market', lines[j], re.IGNORECASE):
                    market_intel_start = j + 1
                if lines[j].startswith('## ') and j > i:
                    break   # reached the next top-level section — stop

            start = priorities_start if priorities_start else (i + 1)

            snippet_parts = []
            for j in range(start, min(start + 60, len(lines))):
                stripped = lines[j].strip()
                if stripped.startswith('## '):
                    break  # next top-level section
                if stripped.startswith('### ') and j > start:
                    break  # stop at next sub-section (keep to Today's Priorities only)
                if stripped and not _NOISE_RE.search(stripped):
                    snippet_parts.append(stripped)
                if len(' '.join(snippet_parts)) >= 1500:
                    break

            # Also pull first 2 bullets from Market Intelligence section if present
            market_snippet = []
            if market_intel_start:
                for j in range(market_intel_start, min(market_intel_start + 20, len(lines))):
                    stripped = lines[j].strip()
                    if stripped.startswith('#'):
                        break
                    if stripped.startswith('- ') or stripped.startswith('• '):
                        content = stripped.lstrip('-• ').strip()
                        if len(content) > 20 and not _NOISE_RE.search(content):
                            market_snippet.append(content)
                    if len(market_snippet) >= 2:
                        break

            combined = ' '.join(snippet_parts)
            if market_snippet:
                combined += ' | MARKET: ' + ' · '.join(market_snippet)
            result['lastBriefingSnippet'] = combined[:1800]
            break

    return result


# Month name → number for Daily Market Update date parsing
_MONTH_MAP = {
    'January':1,'February':2,'March':3,'April':4,'May':5,'June':6,
    'July':7,'August':8,'September':9,'October':10,'November':11,'December':12,
}

def parse_market_commentary(text):
    """Extract last-48h entries from the Daily Market Update doc.

    Doc structure per entry:
      April DD, YYYY — Daily Market Briefing
      **KEY TAKEAWAY:** one-sentence thesis
      1\\. US DIGITAL INFRA & ENERGY BOTTLENECKS
        **BoldHeader:** paragraph content...
      2\\. REGULATORY FRICTION ...
      ...

    Returns list of:
      { 'date': 'YYYY-MM-DD', 'takeaway': str, 'sections': [ {'title': str, 'items': [str]} ] }
    Only includes entries dated within the last 48 hours.
    """
    today     = datetime.now().strftime('%Y-%m-%d')
    two_days_ago = (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d')

    def _parse_month_date(s):
        """'April 14, 2026 ...' → '2026-04-14' or None."""
        m = re.match(
            r'(January|February|March|April|May|June|July|August|September|October|November|December)'
            r'\s+(\d{1,2}),\s+(\d{4})',
            s,
        )
        if not m:
            return None
        mon, day, yr = m.group(1), int(m.group(2)), int(m.group(3))
        return f"{yr:04d}-{_MONTH_MAP[mon]:02d}-{day:02d}"

    # Skip lines that are TOC entries (contain bookmark anchors)
    _SKIP_RE = re.compile(r'#bookmark=|^\[.*\]\(#')
    # Strip markdown bold/italic/links
    _CLEAN_RE = re.compile(r'\*{1,2}|\\+\.|(\[([^\]]+)\]\([^)]+\))')

    def clean(s):
        s = _CLEAN_RE.sub(lambda m: m.group(2) if m.group(2) else '', s)
        return s.strip()

    lines       = text.split('\n')
    all_entries = []
    cur_date    = None
    cur_takeaway = ''
    cur_sections = []
    cur_sec_title = ''
    cur_sec_items = []
    in_entry    = False

    def _flush_sec():
        if cur_sec_items:
            cur_sections.append({'title': cur_sec_title, 'items': cur_sec_items[:3]})

    def _flush_entry():
        nonlocal cur_date, cur_takeaway, cur_sections, cur_sec_title, cur_sec_items, in_entry
        if cur_date and cur_date >= two_days_ago:
            _flush_sec()
            if cur_takeaway or cur_sections:
                all_entries.append({
                    'date':     cur_date,
                    'takeaway': cur_takeaway,
                    'sections': cur_sections[:5],
                })
        cur_date = None; cur_takeaway = ''; cur_sections = []
        cur_sec_title = ''; cur_sec_items = []; in_entry = False

    # Strip a leading bullet/dash marker. Handles "• ", "◦ ", "- ", "* ", "– "
    # plus optional surrounding whitespace. Returns (stripped_line, was_bullet).
    _BULLET_RE = re.compile(r'^[•◦*\-–]+\s+')
    def _strip_bullet(s):
        m = _BULLET_RE.match(s)
        if m:
            return s[m.end():], True
        return s, False

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        # Date heading: "April 14, 2026 — Daily Market Briefing"
        # Must check before stripping bullets — date lines never start with a bullet.
        d = _parse_month_date(line)
        if d and re.search(r'(briefing|update|market)', line, re.IGNORECASE):
            _flush_entry()
            if d >= two_days_ago:
                cur_date  = d
                in_entry  = True
            continue

        if not in_entry:
            continue

        # KEY TAKEAWAY line  (plain text from Docs API — no ** markers)
        if 'KEY TAKEAWAY' in line.upper():
            kt = re.sub(r'KEY TAKEAWAY[:\s]+', '', line, flags=re.IGNORECASE)
            # Strip trailing date suffix e.g. "... deals. 2026-04-14 — Daily Briefing"
            kt = re.sub(r'\s+\d{4}-\d{2}-\d{2}\s*—.*$', '', kt)
            cur_takeaway = kt.strip()[:350]
            continue

        # Numbered section header: "1. US DIGITAL INFRA..."  (plain text — no backslash)
        m = re.match(r'^(\d+)\.\s+([A-Z].{5,})', line)
        if m:
            _flush_sec()
            cur_sec_title = m.group(2).strip()
            cur_sec_items = []
            continue

        # Item: try the bullet/dash format first (current briefing convention),
        # then fall back to the legacy "Title: content" colon format. Either
        # way, populate cur_sec_items as a single string for downstream
        # readthrough matching in _compute_deal_readthroughs().
        stripped, was_bullet = _strip_bullet(line)
        if was_bullet and cur_sec_title and len(stripped) >= 20:
            content = stripped
            # Strip trailing source citation e.g. "[Jefferies — General]"
            content = re.sub(r'\s*\[[^\]]{3,}\]\.?$', '', content).strip()
            # Cap at 280 chars — matches downstream truncation in
            # _compute_deal_readthroughs (kept generous for token matching).
            cur_sec_items.append(content[:280])
            continue

        # Legacy bold-header format: "Title: content" (plain text)
        m = re.match(r'^([A-Z][^.:\n]{4,70}):\s{1,2}(.{40,})', line)
        if m and cur_sec_title:
            title   = m.group(1).strip()
            content = m.group(2).strip()
            content = re.sub(r'\s*\[[^\]]{3,}\]\.?$', '', content)
            first = (content.split('.')[0] + '.') if '.' in content else content
            cur_sec_items.append(f"{title}: {first[:160]}")

    _flush_entry()
    return all_entries


def parse_recent_activity(briefing_data, followups, recruiting, deal_list, today, market_entries=None):
    """Synthesize a last-48h activity feed from all sources.

    Returns list of dicts:
      { type, category, date, dateLabel, title, summary, color }
    Sorted newest-first.
    """
    from datetime import datetime, timedelta
    yesterday = (datetime.strptime(today, '%Y-%m-%d') - timedelta(days=1)).strftime('%Y-%m-%d')
    two_days_ago = (datetime.strptime(today, '%Y-%m-%d') - timedelta(days=2)).strftime('%Y-%m-%d')

    def date_label(d):
        if d == today:     return 'Today'
        if d == yesterday: return 'Yesterday'
        return d

    entries = []

    # ── Capture summary — call notes written by pipeline ──────────────────
    cap = briefing_data.get('captureSummary') if briefing_data else None
    if cap and cap.get('date', '') >= two_days_ago:
        cap_date = cap.get('date', today)
        for item in (cap.get('deals') or cap.get('tomac') or []):  # noqa: tenant-leak — backward-compat key
            # Parse "Title (date): summary" format
            m = re.match(r'^(.+?)\s*\(([^)]+)\):\s*(.+)$', item)
            title   = m.group(1).strip() if m else item[:60]
            summary = m.group(3).strip() if m else item
            _ws_deal = _fc.workstream_deal(_CTX) or 'Deal'
            entries.append({'type': 'call', 'category': f'{_ws_deal} Call', 'workstream': 'deals',
                            'date': cap_date, 'dateLabel': date_label(cap_date),
                            'title': title, 'summary': summary, 'color': 'green'})
        for item in (cap.get('recruiting') or []):
            m = re.match(r'^(.+?)\s*\(([^)]+)\):\s*(.+)$', item)
            title   = m.group(1).strip() if m else item[:60]
            summary = m.group(3).strip() if m else item
            entries.append({'type': 'call', 'category': 'Recruiting Call', 'workstream': 'job',
                            'date': cap_date, 'dateLabel': date_label(cap_date),
                            'title': title, 'summary': summary, 'color': 'blue'})
        for item in (cap.get('other') or []):
            m = re.match(r'^(.+?)\s*\(([^)]+)\):\s*(.+)$', item)
            title   = m.group(1).strip() if m else item[:60]
            summary = m.group(3).strip() if m else item
            entries.append({'type': 'call', 'category': 'Call / Meeting', 'workstream': 'all',
                            'date': cap_date, 'dateLabel': date_label(cap_date),
                            'title': title, 'summary': summary, 'color': 'purple'})
        for item in (cap.get('actionItems') or [])[:6]:
            entries.append({'type': 'action', 'category': 'New Action Item', 'workstream': 'all',
                            'date': cap_date, 'dateLabel': date_label(cap_date),
                            'title': item[:80], 'summary': '', 'color': 'amber'})

    # ── New follow-ups added in last 48h ──────────────────────────────────
    for fu in followups:
        added = fu.get('addedDate', '')
        if added and added >= two_days_ago:
            who  = fu.get('who', '')
            what = fu.get('what', '')
            ws   = fu.get('workstream', '')
            cat  = ('New Follow-up · Deals' if _is_deal_ws(ws)
                    else 'New Follow-up · Recruiting' if ws == 'job'
                    else 'New Follow-up')
            color = 'green' if _is_deal_ws(ws) else 'blue' if ws == 'job' else 'amber'
            title = f"{who} — {what[:60]}" if who else what[:80]
            entries.append({'type': 'followup', 'category': cat, 'workstream': ws or 'all',
                            'date': added, 'dateLabel': date_label(added),
                            'title': title, 'summary': '', 'color': color})

    # ── Recruiting moves (lastAction in last 48h) ─────────────────────────
    for r in recruiting:
        la = (r.get('lastAction') or '')
        la_date = la[:10] if len(la) >= 10 else ''
        # Guard: skip freetext lastAction values (e.g. "to reach out", "Potential meeting...")
        if la_date and not re.match(r'\d{4}-\d{2}-\d{2}', la_date):
            la_date = ''
        if la_date and la_date >= two_days_ago:
            stage = r.get('stage', '')
            contact = r.get('contact', '')
            nxt = r.get('next', '') or ''
            summary = nxt[:120] if nxt and nxt.lower() not in ('unknown', '') else ''
            entries.append({'type': 'recruiting', 'category': 'Recruiting Activity', 'workstream': 'job',
                            'date': la_date, 'dateLabel': date_label(la_date),
                            'title': f"{r['name']} ({contact}) · {stage}",
                            'summary': summary, 'color': 'blue'})

    # ── Deal movement (recent entries in deal list) ───────────────────────
    for d in deal_list:
        notes_list = d.get('notes') or []
        # notes is a list of {'date': ..., 'text': ...} dicts
        for test_date in (today, yesterday):
            matching = [n for n in notes_list if isinstance(n, dict) and n.get('date', '') == test_date]
            if matching:
                summary_text = matching[0].get('text', '')[:150].strip()
                entries.append({'type': 'deal', 'category': 'Deal Activity', 'workstream': 'deals',
                                'date': test_date, 'dateLabel': date_label(test_date),
                                'title': f"{d.get('name', 'Deal')} — {d.get('stage', '')}",
                                'summary': summary_text, 'color': 'green'})
                break

    # ── Market commentary from Daily Market Update ────────────────────────────
    # Surfaces the KEY TAKEAWAY + top section items from the most recent entry.
    # Capped at 1 takeaway + 5 section items to avoid flooding the feed.
    for mc in (market_entries or []):
        d_mc = mc.get('date', '')
        if not d_mc or d_mc < two_days_ago:
            continue
        dl_mc = date_label(d_mc)
        # KEY TAKEAWAY — shown as a single prominent entry
        if mc.get('takeaway'):
            entries.append({
                'type':      'market',
                'category':  'Market Intelligence',
                'workstream': 'all',
                'date':      d_mc,
                'dateLabel': dl_mc,
                'title':     mc['takeaway'][:350],
                'summary':   '',
                'color':     'violet',
            })
        # Section items — top 5 bullets across all sections
        item_count = 0
        for sec in mc.get('sections', []):
            sec_label = sec.get('title', '')[:35]
            for item in sec.get('items', []):
                if item_count >= 5:
                    break
                entries.append({
                    'type':      'market',
                    'category':  f"Market · {sec_label}",
                    'workstream': 'all',
                    'date':      d_mc,
                    'dateLabel': dl_mc,
                    'title':     item[:150],
                    'summary':   '',
                    'color':     'violet',
                })
                item_count += 1
            if item_count >= 5:
                break

    # Sort newest-first, cap at 40
    entries.sort(key=lambda x: x.get('date', ''), reverse=True)
    return entries[:40]


# Matches the same patterns as call_scheduler.py VIDEO_RE
_VIDEO_RE = re.compile(
    r"teams\.microsoft\.com|zoom\.us|meet\.google\.com|webex\.com|"
    r"gotomeeting\.com|whereby\.com|bluejeans\.com|"
    r"passcode|meeting\s+id|access\s+code|conference\s+(id|call)|dial[-\s]in",
    re.IGNORECASE,
)
_SCHEDULED_PATH = Path.home() / 'recordings/calls/.scheduled_meetings.json'

def _load_scheduled_ids():
    """Return set of meeting IDs already registered with launchd for recording."""
    try:
        data = json.loads(_SCHEDULED_PATH.read_text())
        return set(data.keys())
    except Exception:
        return set()

def _is_video(ev):
    """Return True if the calendar event has a video/phone conference link."""
    searchable = ' '.join(filter(None, [
        ev.get('summary', ''),
        ev.get('description', ''),
        ev.get('location', ''),
        ev.get('hangoutLink', ''),
        str(ev.get('conferenceData', {})),
    ]))
    if _VIDEO_RE.search(searchable):
        return True
    for ep in ev.get('conferenceData', {}).get('entryPoints', []):
        if ep.get('entryPointType') in ('video', 'phone'):
            return True
    return False

def get_calendar(cal_svc):
    now = datetime.now(timezone.utc)
    try:
        items = cal_svc.events().list(
            calendarId='primary',
            timeMin=now.isoformat(),
            timeMax=(now + timedelta(days=7)).isoformat(),
            maxResults=30, singleEvents=True, orderBy='startTime'
        ).execute().get('items', [])
    except Exception as e:
        print(f'Calendar error: {e}', file=sys.stderr)
        return [], []

    scheduled_ids = _load_scheduled_ids()
    today = now.date()
    day_buckets = {}
    upcoming = []

    for ev in items:
        start = ev.get('start', {})
        dt_str = start.get('dateTime', start.get('date', ''))
        if 'T' in dt_str:
            dt = datetime.fromisoformat(dt_str.replace('Z', '+00:00')).astimezone()
            day = dt.date()
            time_str = dt.strftime('%-I:%M%p').lower().rstrip('m') + 'm'
        else:
            day = datetime.fromisoformat(dt_str).date()
            time_str = 'all day'

        title = ev.get('summary', '(no title)')
        tl = title.lower()
        # Attendee emails — used as a secondary classification signal so
        # Deal calls with ambiguous titles ("Castleton sync") don't
        # fall into recruiting just because Reinova is keyed there.
        attendee_blob = ' '.join(
            ((a.get('email') or '') + ' ' + (a.get('displayName') or ''))
            for a in ev.get('attendees', [])
        ).lower()
        hay = tl + ' ' + attendee_blob
        # Deal-activity keywords from firm_config (was hardcoded TOMAC_KEYS).
        # Recruiting keywords from firm_config (was hardcoded JOB_KEYS).
        DEAL_KEYS = _DEAL_KEYS
        JOB_KEYS  = _RECRUIT_KEYS
        # Deals win ties — investment activity is the priority classification.
        ws = ('deals' if any(k in hay for k in DEAL_KEYS)
              else 'job' if any(k in hay for k in JOB_KEYS)
              else 'personal')

        # Recording indicators
        ev_id       = f"gcal_{ev.get('id', '')}"
        has_video   = _is_video(ev)
        is_scheduled = ev_id in scheduled_ids   # launchd job already registered
        will_record  = has_video                 # will be auto-recorded when scheduler runs

        # Attendees — names + emails, excluding self. Useful for SMS
        # correlation (text from someone on the calendar that day routes
        # to that workstream/deal).
        ev_attendees = [{
            'name':  a.get('displayName', '').strip(),
            'email': a.get('email', '').strip().lower(),
        } for a in ev.get('attendees', []) if not a.get('self')]

        delta = (day - today).days
        day_lbl = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][day.weekday()] + f' {day.month}/{day.day}'
        if delta not in day_buckets:
            day_buckets[delta] = {'label': day_lbl, 'isToday': delta == 0, 'events': []}
        day_buckets[delta]['events'].append({
            'time':        time_str,
            'title':       title,
            'workstream':  ws,
            'willRecord':  will_record,
            'isScheduled': is_scheduled,
            'date':        day.isoformat(),
            'start':       dt_str,
            'attendees':   ev_attendees,
        })

        if delta <= 3:
            attendees = [a.get('displayName', a.get('email','')) for a in ev.get('attendees',[]) if not a.get('self')]
            upcoming.append({
                'when':        f'{day_lbl} · {time_str}',
                'who':         title,
                'with':        ' · '.join(attendees[:3]),
                'workstream':  ws,
                'willRecord':  will_record,
                'isScheduled': is_scheduled,
            })

    days_out = []
    for i in range(7):
        d = today + timedelta(days=i)
        days_out.append(day_buckets.get(i, {
            'label': ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][d.weekday()] + f' {d.month}/{d.day}',
            'isToday': i == 0, 'events': []
        }))
    return days_out, upcoming[:6]

# ── Run state (pre-computed AI artifacts) ─────────────────
RUN_STATE_PATH = _ROOT / 'data' / 'compiled' / 'cos-run-state.json'

# Otter AI Drive folder IDs — checked for unprocessed transcripts
OTTER_FOLDER_IDS = [
    '1pHmuq_TfLY46GDg0BzRIwrq57ictIT5S',   # Deal folder  # noqa: tenant-leak — Drive ID comment
    '1tMEGofeqzfF93YhPCyGe0dgJj8tzdRlF',   # Recruiting
    '1dt-s-D1SWaTrpIEsi0GiBAu1BCQCoPGq',   # Other
]

def load_run_state():
    """Read cos-run-state.json written by scheduled AI pipeline runs."""
    try:
        return json.loads(RUN_STATE_PATH.read_text())
    except Exception:
        return {'lastFullRunAt': None, 'lastMiniRunAt': None,
                'emailQueue': [], 'processedTranscripts': [], 'runHistory': []}

def get_unprocessed_transcripts(drive_svc, last_run_at):
    """Query Drive for Otter transcript files modified after last full AI run.
    Returns list of {title, folder, modifiedTime} for files not yet processed.
    Only runs if we have a lastFullRunAt timestamp to compare against.
    """
    if not last_run_at:
        return []
    try:
        unprocessed = []
        for folder_id in OTTER_FOLDER_IDS:
            folder_name = {
                '1pHmuq_TfLY46GDg0BzRIwrq57ictIT5S': _fc.workstream_deal(_CTX) or 'Deal',
                '1tMEGofeqzfF93YhPCyGe0dgJj8tzdRlF': 'Recruiting',
                '1dt-s-D1SWaTrpIEsi0GiBAu1BCQCoPGq': 'Other',
            }.get(folder_id, folder_id)
            # Accept Google Docs AND uploaded text/docx files — Otter.ai can export either
            q = (f"'{folder_id}' in parents "
                 f"and modifiedTime > '{last_run_at}' "
                 f"and (mimeType = 'application/vnd.google-apps.document' "
                 f"     or mimeType = 'text/plain' "
                 f"     or mimeType = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document') "
                 f"and trashed = false")
            results = drive_svc.files().list(
                q=q, fields='files(id,name,modifiedTime)', pageSize=10
            ).execute().get('files', [])
            for f in results:
                unprocessed.append({
                    'title':        f.get('name', ''),
                    'folder':       folder_name,
                    'modifiedTime': f.get('modifiedTime', ''),
                    'fileId':       f.get('id', ''),
                })
        return unprocessed
    except Exception as e:
        print(f'Unprocessed transcript check error: {e}', file=sys.stderr)
        return []

# ── Main ──────────────────────────────────────────────────
def main(dry_run: bool = False):
    t0 = datetime.now()
    print('cos-dashboard-fetch: connecting to Google APIs...', file=sys.stderr)
    docs_svc, cal_svc = get_services()

    # Refresh the People-directory cache from the People/CRM Google Doc.
    # Used by _envelope_writer counterparty inference. Daily refresh is
    # plenty — the doc itself is human-curated and changes slowly.
    try:
        from _envelope_writer import refresh_people_directory_from_doc
        n = refresh_people_directory_from_doc(DOC_IDS.get('people'))
        if n:
            print(f'cos-dashboard-fetch: people directory refreshed ({n} entries)',
                  file=sys.stderr)
    except Exception as e:
        print(f'cos-dashboard-fetch: people directory refresh skipped ({e})',
              file=sys.stderr)

    # Load pre-computed AI artifacts from scheduled pipeline runs
    run_state = load_run_state()
    last_full_run = run_state.get('lastFullRunAt')

    # Load existing state so we can use the doc content cache + carry forward
    # calendar/gmail data written by the daily capture pipeline.
    state = {}
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH) as f:
                state = json.load(f)
        except Exception:
            pass

    # Doc content cache — avoids re-fetching unchanged docs (biggest speed win)
    doc_cache = state.get('_docCache', {})

    print('cos-dashboard-fetch: fetching 5 Google Docs + Calendar in parallel...', file=sys.stderr)
    with ThreadPoolExecutor(max_workers=6) as ex:
        fu_f  = ex.submit(_fetch_doc_worker, DOC_IDS['followups'],    doc_cache)
        rec_f = ex.submit(_fetch_doc_worker, DOC_IDS['recruiting'],   doc_cache)
        deal_f = ex.submit(_fetch_doc_worker, DOC_IDS['deal_pipeline'], doc_cache)
        log_f = ex.submit(_fetch_doc_worker, DOC_IDS['briefing_log'], doc_cache)
        mkt_f = ex.submit(_fetch_doc_worker, DOC_IDS['daily_market'], doc_cache)
        cal_f = ex.submit(get_calendar, cal_svc)
        followups_text      = fu_f.result()
        recruiting_text     = rec_f.result()
        deal_pipeline_text  = deal_f.result()
        log_text            = log_f.result()
        market_text         = mkt_f.result()
        calendar, upcoming  = cal_f.result()

    # Gmail: not fetched here — Gmail.readonly scope not on this token and polling
    # every 20 min would be wasteful. Capture pipeline (7:29am) handles Gmail via MCP.
    email_activity          = []
    unprocessed_transcripts = []

    print('cos-dashboard-fetch: parsing...', file=sys.stderr)
    followups, awaiting_from_doc = parse_followups(followups_text)
    recruiting           = parse_recruiting(recruiting_text)
    deals                = parse_deal_pipeline(deal_pipeline_text)
    lp_data              = parse_lp_data(deal_pipeline_text)
    fundraising_strategy = parse_fundraising_strategy(deal_pipeline_text)
    briefing_data        = parse_briefing_log(log_text)
    market_entries       = parse_market_commentary(market_text)

    # ── Apply email-resolutions: mark items completed via email/calendar ──
    # email-resolutions.json is written by cos_email_resolver.py (runs before
    # this fetch in the warmup chain). Items with a resolution get [RESOLVED]
    # prepended so _resolved_row_sweep.py removes them after a 2-day grace.
    _email_res_path = _ROOT / "data" / "user-state" / "email-resolutions.json"
    try:
        _email_resolutions = json.loads(_email_res_path.read_text()) if _email_res_path.exists() else {}
    except Exception:
        _email_resolutions = {}

    def _djb2_fetch(s):
        h = 5381
        for c in s:
            h = ((h << 5) + h) ^ ord(c)
        return format(h & 0xFFFFFFFF, "08x")

    def _item_hash(item, who_key="who", what_key="what"):
        who  = item.get(who_key, item.get("counterparty", item.get("owner", "")))
        what = item.get(what_key, item.get("content", ""))
        return _djb2_fetch(who + "|" + what[:60])

    _resolved_count = 0
    for fu in followups:
        if _item_hash(fu) in _email_resolutions:
            if not fu.get("what", "").startswith("[RESOLVED]"):
                fu["what"] = "[RESOLVED] " + fu.get("what", "")
                _resolved_count += 1
    for ae in awaiting_from_doc:
        if _item_hash(ae, who_key="counterparty", what_key="content") in _email_resolutions:
            if not ae.get("content", "").startswith("[RESOLVED]"):
                ae["content"] = "[RESOLVED] " + ae.get("content", "")
                _resolved_count += 1
    if _resolved_count:
        print(f'cos-dashboard-fetch: {_resolved_count} item(s) marked [RESOLVED] via email-resolutions')

    recent_activity      = parse_recent_activity(
        briefing_data, followups, recruiting, deals,
        t0.strftime('%Y-%m-%d'),
        market_entries=market_entries,
    )

    # ── Stamp addedDate on newly appeared follow-up items ─────────────────
    # Compare current parsed list to previous state to detect new rows.
    # Preserves existing addedDate for items already seen (so the 48h "New" badge
    # persists correctly across multiple warmup cycles).
    today_str = t0.strftime('%Y-%m-%d')
    prev_fus  = state.get('followUps', [])
    prev_keys = {(f.get('who', ''), f.get('what', '')[:60]): f.get('addedDate', '') for f in prev_fus}
    for fu in followups:
        k = (fu.get('who', ''), fu.get('what', '')[:60])
        if k in prev_keys:
            if prev_keys[k]:
                fu['addedDate'] = prev_keys[k]   # carry forward existing date
        else:
            fu['addedDate'] = today_str           # genuinely new row

    # Merge email signals into recent activity feed (newest-first, cap at 40 total)
    if email_activity:
        combined = email_activity + recent_activity
        combined.sort(key=lambda x: x.get('date', '') + x.get('time', ''), reverse=True)
        recent_activity = combined[:40]
        print(f'cos-dashboard-fetch: {len(email_activity)} email signal(s) merged into activity feed')

    # Preserve manually-managed recruiting entries not present in the doc-parsed list.
    parsed_names = {r['name'] for r in recruiting}
    for r in state.get('recruiting', {}).get('active', []):
        if r['name'] not in parsed_names and (r.get('contacts') or r.get('_manual')):
            recruiting.append(r)

    # Preserve manually-added follow-up items (_manual: true).
    parsed_fu_keys = {(f.get('who',''), f.get('what','')[:40]) for f in followups}
    manual_fus = []
    for fu in state.get('followUps', []):
        if fu.get('_manual'):
            key = (fu.get('who',''), fu.get('what','')[:40])
            if key not in parsed_fu_keys:
                manual_fus.append(fu)
    if manual_fus:
        followups = manual_fus + followups

    # Preserve envelope my_action items written directly by _envelope_writer.append_items().
    # These are written to dashboard-data.json with content_type="my_action" but NOT to the
    # Follow-ups Google Doc, so parse_followups() above silently drops them on every fetch.
    # Re-merge any that aren't already covered by the doc parse.
    envelope_fu_keys = {(f.get('who',''), f.get('what','')[:40]) for f in followups}
    envelope_fus = []
    for fu in state.get('followUps', []):
        if fu.get('content_type') == 'my_action':
            what = (fu.get('content') or fu.get('what') or '')
            who  = (fu.get('owner') or fu.get('who') or '')
            key  = (who, what[:40])
            if key not in envelope_fu_keys:
                normalised = dict(fu)
                if not normalised.get('who'):
                    normalised['who'] = who
                if not normalised.get('what'):
                    normalised['what'] = what
                envelope_fus.append(normalised)
    if envelope_fus:
        followups = followups + envelope_fus
        print(f'cos-dashboard-fetch: {len(envelope_fus)} envelope my_action item(s) re-merged')

    # Apply manual workstream/category overrides — persists dashboard drag-and-drop moves across refreshes.
    # Written by dashboard UI as: dashboard-data.json._workstreamOverrides = { "who|what_prefix": "newWorkstream" }
    ws_overrides = state.get('_workstreamOverrides', {})
    if ws_overrides:
        for fu in followups:
            key = f"{fu.get('who','')}|{fu.get('what','')[:40]}"
            if key in ws_overrides:
                fu['workstream'] = ws_overrides[key]

    # Apply manual stage overrides for deals — persists dashboard stage moves.
    # Written by dashboard UI as: dashboard-data.json._stageOverrides = { "deal_name": "newStage" }
    stage_overrides = state.get('_stageOverrides', {})
    if stage_overrides:
        for deal in deals:
            if deal.get('name') in stage_overrides:
                deal['stage'] = stage_overrides[deal['name']]
        for rec in recruiting:
            if rec.get('name') in stage_overrides:
                rec['stage'] = stage_overrides[rec['name']]

    # Apply manual pinned/hidden flags — persists items pinned to top or hidden from dashboard.
    # Written by dashboard UI as: dashboard-data.json._pinnedItems = ["who|what_prefix", ...]
    #                              dashboard-data.json._hiddenItems = ["who|what_prefix", ...]
    pinned_keys = set(state.get('_pinnedItems', []))
    hidden_keys = set(state.get('_hiddenItems', []))
    if pinned_keys or hidden_keys:
        pinned, regular, hidden = [], [], []
        for fu in followups:
            key = f"{fu.get('who','')}|{fu.get('what','')[:40]}"
            if key in hidden_keys:
                fu['_hidden'] = True
                hidden.append(fu)
            elif key in pinned_keys:
                fu['_pinned'] = True
                pinned.append(fu)
            else:
                regular.append(fu)
        followups = pinned + regular  # hidden items stripped from active list

    # Strip server-side dismissed follow-ups (persisted by dismissAction() via POST /patch).
    # Key format mirrors JS actionKey(): "who|due" with quotes removed.
    dismissed_fu_keys = set(state.get('_dismissedFollowUps', []))
    if dismissed_fu_keys:
        before = len(followups)
        followups = [
            fu for fu in followups
            if (fu.get('who', '') + '|' + fu.get('due', '')).replace('"', '') not in dismissed_fu_keys
        ]
        dropped = before - len(followups)
        if dropped:
            print(f'cos-dashboard-fetch: filtered {dropped} dismissed follow-up(s)')

    # ── Spurious-action suppression ───────────────────────────────────────
    # The transcript extractor occasionally manufactures "actions" from things
    # Yoni said he *could* do but the counterparty never accepted. These get
    # filtered by content regex before they hit the dashboard so the user
    # doesn't have to dismiss them one at a time. Also filters envelope
    # awaiting_external / takeaway items that restate the same non-action.
    _SPURIOUS_ACTION_PATTERNS = [
        # Spurious-action pattern: principal offered to ping ex-colleague on
        # the Oncor board; counterparty never confirmed. Not a real action.
        re.compile(r'(ping|reach\s*out|call|contact).{0,40}(ex[-\s]?colleague|ex[-\s]?yoni).{0,40}oncor\s*board', re.I),  # noqa: tenant-leak
        re.compile(r'(ping|reach\s*out|call|contact).{0,60}oncor\s*board.{0,60}(pressure[-\s]?test|likelihood|color|read)', re.I),
    ]
    def _is_spurious(text):
        t = text or ''
        return any(p.search(t) for p in _SPURIOUS_ACTION_PATTERNS)

    before_fu = len(followups)
    followups = [fu for fu in followups if not _is_spurious(fu.get('what',''))]
    dropped_fu = before_fu - len(followups)
    if dropped_fu:
        print(f'cos-dashboard-fetch: suppressed {dropped_fu} spurious follow-up(s)')

    # ── Follow-up near-duplicate suppression ─────────────────────────────
    # Transcript processors can emit the same action twice with minor wording
    # differences or inconsistent who-attribution (first name vs full name).
    # Dedup on (normalized_who, content_stem_8tok), keeping first occurrence.
    # 2026-05-12: added after a deal action appeared twice from
    # the same weekly call due to name normalization mismatch.
    _PRINCIPAL_FIRST = (_fc.principal_first_name(_CTX) or '').lower()
    _PRINCIPAL_FULL  = ((_CTX.get('principal') or {}).get('name') or '').lower()

    def _norm_who_fu(who_str):
        w = (who_str or '').strip().lower()
        if _PRINCIPAL_FIRST and _PRINCIPAL_FIRST in w:
            return _PRINCIPAL_FIRST
        return w

    import re as _re_fu
    _STOP_FU = set('a an the to for of and or with on in at by from is be was i my'.split())
    def _stem_fu(text):
        t = (text or '').lower()
        t = _re_fu.sub(r'[^\w\s]', ' ', t)
        toks = [tok for tok in t.split() if tok not in _STOP_FU and len(tok) >= 3]
        return ' '.join(toks[:8])

    # Pass A: exact stem key — catches identical or near-identical wording
    seen_fu: set = set()
    deduped_fus = []
    for fu in followups:
        key = (_norm_who_fu(fu.get('who', '')), _stem_fu(fu.get('what', '')))
        if key in seen_fu:
            print(f'cos-dashboard-fetch: deduped near-dup follow-up (stem): {fu.get("what","")[:60]!r}')
            continue
        seen_fu.add(key)
        deduped_fus.append(fu)

    # Pass B: same-transcript dedup — catches reworded actions from the same call.
    # When two items share (normalized_who, source, linkedTo) keep the richer one
    # (longer `what`). linkedTo is the specific doc/transcript URL, so this key
    # uniquely identifies "same person, same call" without needing content similarity.
    # 2026-05-12: added after a deal action appeared twice from
    # the same weekly call with different wording, foiling the stem dedup.
    by_transcript: dict = {}
    for fu in deduped_fus:
        lt = (fu.get('linkedTo') or '').strip()
        if not lt:
            continue
        nw = _norm_who_fu(fu.get('who', ''))
        src = (fu.get('source') or '').strip().lower()
        tkey = (nw, src, lt)
        existing = by_transcript.get(tkey)
        if existing is None or len(fu.get('what') or '') > len(existing.get('what') or ''):
            by_transcript[tkey] = fu

    if by_transcript:
        trans_keep = set(id(v) for v in by_transcript.values())
        before_t = len(deduped_fus)
        deduped_fus = [
            fu for fu in deduped_fus
            if not ((fu.get('linkedTo') or '').strip())
            or id(fu) in trans_keep
        ]
        dropped_t = before_t - len(deduped_fus)
        if dropped_t:
            print(f'cos-dashboard-fetch: deduped {dropped_t} same-transcript follow-up(s)')

    if len(deduped_fus) < len(followups):
        print(f'cos-dashboard-fetch: deduped {len(followups)-len(deduped_fus)} follow-up(s) total')
    followups = deduped_fus

    for key in ('awaitingExternal', 'dealIntel', 'originationInbox', 'statusUpdates'):
        arr = state.get(key, []) or []
        filtered = [it for it in arr
                    if not _is_spurious((it.get('content') or '') + ' ' + (it.get('counterparty') or ''))]
        if len(filtered) != len(arr):
            print(f'cos-dashboard-fetch: suppressed {len(arr)-len(filtered)} spurious {key} item(s)')
            state[key] = filtered

    # HISTORICAL guard — items from calls older than 7 days are tagged either
    # with [HISTORICAL prefix on content or historical:true. Filter them from
    # all live action arrays so the dashboard never surfaces them as actionable.
    def _is_historical(it):
        if not isinstance(it, dict):
            return False
        if it.get('historical') is True:
            return True
        for fld in ('content', 'what', 'key_feedback', 'feedback'):
            v = it.get(fld) or ''
            if isinstance(v, str) and v.lstrip().lower().startswith('[historical'):
                return True
        return False

    for key in ('awaitingExternal', 'dealIntel', 'originationInbox', 'statusUpdates', 'followUps'):
        arr = state.get(key, []) or []
        filtered = [it for it in arr if not _is_historical(it)]
        if len(filtered) != len(arr):
            print(f'cos-dashboard-fetch: filtered {len(arr)-len(filtered)} historical {key} item(s)')
            state[key] = filtered

    now = datetime.now()

    # Read today's content tracker (new articles/reports detected by track_new_substack_articles.py)
    tracker_path = Path(f'/tmp/new_substack_articles_{now.strftime("%Y-%m-%d")}.json')
    content_tracker = None
    if tracker_path.exists():
        try:
            content_tracker = json.loads(tracker_path.read_text())
        except Exception:
            pass

    # Build pipeline status summary from run state
    run_history   = run_state.get('runHistory', [])
    email_queue   = run_state.get('emailQueue', [])
    ready_drafts  = [e for e in email_queue if e.get('status') == 'DRAFT_READY']
    pipeline_status = {
        'lastFullRunAt':   last_full_run,
        'lastMiniRunAt':   run_state.get('lastMiniRunAt'),
        'lastFetchAt':     run_state.get('lastFetchAt'),   # stamped by this script on every run
        'lastRunSummary':  run_history[-1] if run_history else None,
        'draftsReady':     len(ready_drafts),
        'unprocessedTranscripts': len(unprocessed_transcripts),
    }

    # ── Gap A: auto-promote origination clusters to tracked deals ───────────
    # When a counterparty not in the deal doc shows up with follow-ups or
    # envelope items containing explicit fundraising signals, synthesize a
    # Deal entry so it surfaces before a manual doc edit.
    _deals_envelope = state.get('awaitingExternal', []) + state.get('dealIntel', []) + state.get('originationInbox', []) + state.get('statusUpdates', [])
    deals, _promoted = _auto_promote_origination(deals, followups, _deals_envelope, today_str, lp_names=lp_data)
    if _promoted:
        print(f'cos-dashboard-fetch: auto-promoted {len(_promoted)} origination→deals: {", ".join(_promoted)}', file=sys.stderr)

    # ── Gap B: overlay freshest signal onto each deal's nextStep ─────
    # The deal doc's "Next step" field is static; it goes stale as soon as
    # a call or email produces new commitments. For each deal, pick the
    # earliest-due open my_action or awaiting_external that references the
    # deal (by name token overlap on who/counterparty/content), and if that
    # signal is fresher than the doc version, overlay it. Always expose the
    # doc version as `nextStepDoc` and the freshness flag as `freshSignal`
    # so the UI can badge "updated today".
    deals = _overlay_freshest_signal(deals, followups, _deals_envelope, today_str)

    # ── Per-section last_refreshed tracking ───────────────────────────────
    # For each major section, record the timestamp of the most recent fetch
    # that returned non-empty content. If a section comes back empty (likely
    # transient: token expiry, API timeout), preserve the prior timestamp so
    # the UI can flag it as stale rather than appearing freshly empty.
    prior_ts = state.get('_sectionTimestamps', {}) if isinstance(state, dict) else {}
    now_iso = now.isoformat()

    def _ts(section_name: str, value) -> str:
        # Truthy = update; empty/None = preserve prior (or now if never seen).
        if value:
            return now_iso
        return prior_ts.get(section_name, now_iso)

    section_timestamps = {
        'followUps':        _ts('followUps',        followups),
        'upcomingCalls':    _ts('upcomingCalls',    upcoming),
        'deals':            _ts('deals',            deals),
        # Back-compat — write old workstream key as a duplicate timestamp for 1 release
        # so consumers still on the old key see fresh data. Remove next release.
        'tomac':            _ts('deals',            deals),  # noqa: tenant-leak — backward-compat, remove next release
        'recruiting':       _ts('recruiting',       recruiting),
        'calendar':         _ts('calendar',         calendar),
        'briefingSynopsis': _ts('briefingSynopsis', briefing_data),
        'lpData':           _ts('lpData',           lp_data),
        'fundraising':      _ts('fundraising',      fundraising_strategy),
        'marketCommentary': _ts('marketCommentary', market_entries),
        'recentActivity':   _ts('recentActivity',   recent_activity),
        'emailActivity':    now_iso,                              # always current — gmail-mini owns email via its own LaunchAgent
        'emailQueue':       _ts('emailQueue',       email_queue),
        'contentTracker':   _ts('contentTracker',   content_tracker),
    }

    live_data = {
        'fetchedAt':        now.isoformat(),
        '_sectionTimestamps': section_timestamps,
        'today':            now.strftime('%Y-%m-%d'),
        '_docCache':        doc_cache,   # persisted across warmups for modifiedTime skip
        'threeDays':        (now + timedelta(days=3)).strftime('%Y-%m-%d'),
        'upcomingCalls':    upcoming,
        # 2026-05-05 (rule AB1): also run _materialize_next_week on
        # followUps so any "tomorrow" / "next week" / "Wed 4/29" /
        # "Friday 5/1" relative phrasing the LLM extractor missed gets
        # rewritten to absolute YYYY-MM-DD against the row's addedDate.
        # Same coverage as awaitingExternal — same audit catches both.
        'followUps':        _materialize_next_week(followups),
        # Raw text of Drive Follow-ups doc — used by gap_detector (Phase I) to
        # cross-reference which entities are already covered. Empty string if
        # doc fetch failed; gap_detector degrades gracefully on empty input.
        'followUpsRaw':     followups_text or "",
        'deals':            deals,
        # Back-compat duplicate (read-only mirror) — remove next release.
        'tomac':            deals,  # noqa: tenant-leak — backward-compat key, remove next release
        'recruiting': {
            'active':   recruiting,
            'archived': state.get('recruiting', {}).get('archived', []),
        },
        'calendar':         calendar,
        'briefingSynopsis': briefing_data,
        'lpData':           lp_data,
        'fundraising':      fundraising_strategy,
        'marketCommentary': market_entries,
        'recentActivity':   recent_activity,
        'emailActivity':    email_activity,
        'gmailScanned':     now.isoformat(),
        # Pre-computed AI artifacts from scheduled pipeline runs
        'emailQueue':                email_queue,          # drafts ready to review/send
        'unprocessedTranscripts':    unprocessed_transcripts,  # new transcripts since last AI run
        'pipelineStatus':            pipeline_status,      # last run time + summary counts
        'pipelineRunHistory':        run_history[-7:],     # last 7 runs
        'contentTracker':            content_tracker,      # new articles from track_new_substack_articles.py
        # ── Routing v2 arrays (Phase 1 of ROUTING-SPEC-2026-04-21) ──────────────
        # Populated by the envelope writer in routines/process/_envelope_writer.py;
        # consumed by UI in Phase 3. Preserved across warmups via state merge below.
        # Merge doc-authored [waiting] rows with pipeline-authored envelope items.
        # Dedupe by (counterparty, content[:60]) so a doc row and pipeline row
        # referencing the same commitment collapse.
        'awaitingExternal':  _classify_awaiting_category(
                                 _auto_expire_stale_events(
                                 _supersede_workflow_stages(
                                 _redupe_after_canonicalization(
                                 _drop_team_member_counterparties(
                                 _materialize_next_week(
                                 _rederive_counterparty(
                                 _promote_source_ref(_merge_awaiting(
                                 state.get('awaitingExternal', []), awaiting_from_doc))))))))),
        # Rule AB1: same materialize sweep as awaitingExternal/followUps so
        # relative phrasing in deal-intel and origination-inbox content
        # gets resolved to YYYY-MM-DD before render.
        'dealIntel':         _materialize_next_week(state.get('dealIntel', [])),
        'originationInbox':  _materialize_next_week(state.get('originationInbox', [])),
        'themes':            state.get('themes',            []),
        'routingExceptions': state.get('routingExceptions', []),
    }

    # ── Embed deal system portfolio (pre-compiled by deal-system-compile.py) ──────
    # Reads local deal-system-data.json — no Google APIs, ~1ms.
    # Contains: portfolio rollup (health, actions, profit mid) + per-deal structured data.
    # Written by ~/cos-pipeline/deal-system-compile.py, which runs in parallel
    # with this fetch on every warmup. CoS dashboard uses this for the deal health panel.
    _deal_sys_path = _ROOT / 'data' / 'compiled' / 'deal-system-data.json'
    if _deal_sys_path.exists():
        try:
            live_data['dealPortfolio'] = json.loads(_deal_sys_path.read_text())
            p = live_data['dealPortfolio'].get('portfolio', {})
            print(f'cos-dashboard-fetch: deal portfolio embedded — '
                  f'{p.get("total_deals","?")} deals, health {p.get("avg_health","?")}', file=sys.stderr)
        except Exception as _e:
            print(f'cos-dashboard-fetch: deal portfolio embed failed: {_e}', file=sys.stderr)

    # ── Embed costs/quota rollup (Track G) ────────────────────────────────────
    # Reads ~/cos-pipeline/data-<tenant>/costs/*.jsonl. Pure local I/O, ~1ms.
    # Tile shape consumed by renderCostsTile(data) in cos-dashboard.template.html.
    if _costs_mod is not None:
        try:
            _tenant = os.environ.get('COS_TENANT', '')
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

    # --- Tier 1 synthesis: rank actionable items across HQ + Personal sources.
    # No LLM. Lives inline so synthesis recomputes every cache refresh.
    try:
        import sys as _sys
        _sys.path.insert(0, str(_ROOT / "app"))
        from lib.prioritize import synthesize_tier1
        _deal_sys_path = _ROOT / "data" / "compiled" / "deal-system-data.json"
        _deal_sys = json.loads(_deal_sys_path.read_text()) if _deal_sys_path.exists() else {}
        _synth = synthesize_tier1(live_data, _deal_sys)
        # Preserve any Tier 2 prose already in state (only Tier 1 refreshes here).
        # Must include both legacy aliases (prose/worthNoticing/clusters) AND the
        # surface-specific keys added in Phase H split (prose_hq/prose_personal/etc.)
        # — omitting the new keys was causing dashboard-fetch to wipe them on warmup.
        _prev_synth = (state.get("prioritySynthesis") or {})
        _TIER2_PRESERVE = (
            "prose", "worthNoticing", "clusters", "tier2GeneratedAt",
            "prose_hq", "worthNoticing_hq", "clusters_hq",
            "prose_personal", "worthNoticing_personal", "clusters_personal",
            "ruleApplications",
        )
        for k in _TIER2_PRESERVE:
            if k in _prev_synth:
                _synth[k] = _prev_synth[k]

        # --- Tier 1.5: gap detection (Phase I) ----------------------------
        # Cross-references per-deal entity_mentions.json (Phase J output) +
        # actions.md + log.json + calendar against curated Follow-ups doc +
        # dashboard surfaces. Entities present in source but absent in
        # curated → gaps[]. No LLM. Defensive — any failure leaves gaps[]
        # empty rather than breaking the cache refresh.
        try:
            from lib.gap_detector import run as _detect_gaps
            import yaml as _yaml
            _weights_path = _ROOT / "config" / "synthesis-weights.yaml"
            _weights = (_yaml.safe_load(_weights_path.read_text())
                        if _weights_path.exists() else {}) or {}
            _followups_text = live_data.get("followUpsRaw", "") or ""
            _calendar = live_data.get("upcomingCalls", []) or []
            _deal_config = (_deal_sys.get("deals") or [])
            _gaps = _detect_gaps(
                dashboard_data=live_data,
                followups_text=_followups_text,
                calendar_events=_calendar,
                deal_config=_deal_config,
                weights=_weights,
            )
            _synth["gaps"] = _gaps
        except Exception as _ge:
            print(f"gap_detector warning: {_ge}", file=sys.stderr)
            _synth["gaps"] = _synth.get("gaps", [])

        live_data["prioritySynthesis"] = _synth
    except Exception as e:
        print(f"prioritize warning: {e}", file=sys.stderr)
        # Non-fatal: continue without synthesis rather than failing the whole fetch.

    # Merge: live data wins over stale cached values; curated fields from state are preserved.
    # CRITICAL: never overwrite manual override maps — they are written by the dashboard UI
    # and must survive every refresh cycle.
    merged = {**state, **live_data}
    for preserve_key in ('_workstreamOverrides', '_stageOverrides', '_pinnedItems', '_hiddenItems', '_dismissedFollowUps', '_dismissedEmailIds', 'prioritySynthesis'):
        if preserve_key in state and preserve_key not in live_data:
            merged[preserve_key] = state[preserve_key]

    # `generatedAt` is a *display-only* field formatted for the HTML banner —
    # cos-dashboard-refresh.py and the GET /data endpoint compute it fresh from
    # `fetchedAt` on every serve. Persisting a stale copy here would surface as
    # a misleading timestamp in the dashboard until the next /warmup. Drop it.
    merged.pop('generatedAt', None)
    if dry_run:
        # --dry-run: emit the merged state as JSON to stdout; do NOT write the file.
        # The server's /sync-preview endpoint uses this to diff against the live state.
        print(json.dumps(merged, indent=2, ensure_ascii=False))
    else:
        _tmp = STATE_PATH.with_suffix('.tmp')
        _tmp.write_text(json.dumps(merged, indent=2, ensure_ascii=False))
        os.replace(_tmp, STATE_PATH)
        # Stamp lastFetchAt so the dashboard freshness badge reflects doc-read time,
        # not just the heavier Gmail-scan pipeline's lastFullRunAt.
        try:
            rs = json.loads(RUN_STATE_PATH.read_text()) if RUN_STATE_PATH.exists() else {}
            rs['lastFetchAt'] = datetime.utcnow().isoformat()
            RUN_STATE_PATH.write_text(json.dumps(rs, indent=2))
        except Exception:
            pass

    elapsed = (datetime.now() - t0).total_seconds()
    dest = 'stdout (dry-run)' if dry_run else str(STATE_PATH)
    print(f'cos-dashboard-fetch: done in {elapsed:.1f}s → {dest}', file=sys.stderr)

if __name__ == '__main__':
    import argparse as _ap
    _p = _ap.ArgumentParser(add_help=False)
    _p.add_argument('--dry-run', action='store_true',
                    help='Compute the merged state but print to stdout instead of writing dashboard-data.json')
    _args, _ = _p.parse_known_args()
    main(dry_run=_args.dry_run)
