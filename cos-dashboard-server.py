#!/usr/bin/env python3
"""
cos-dashboard-server.py
Tiny local HTTP server on port 7777.

Architecture (post-optimization):
  POST /refresh       → FAST (~100ms): reads dashboard-data.json cache, injects HTML
  POST /warmup        → NON-BLOCKING: triggers cos-dashboard-fetch.py in background
  POST /run-pipeline  → NON-BLOCKING: fires cos-capture-pipeline skill via claude CLI
  GET  /pipeline-status → returns {running, startedAt, lastCompletedAt}
  Background          → auto-warmup every WARMUP_INTERVAL_MIN minutes

The Google API fetch (slow, 3-5 sec) only runs:
  • At startup (once, to prime the cache)
  • Every WARMUP_INTERVAL_MIN minutes automatically
  • When POST /warmup is called (by scheduled tasks after they write to Docs)
  • As a fallback inside cos-dashboard-refresh.py if cache is >90 min old

Kept alive by LaunchAgent: com.<owner>.cosdashboard.plist (label prefix
derived from the principal handle in firm_context — see _LAUNCHAGENT_LABEL).
"""
import base64, glob, gzip, json, os, plistlib, queue, re, secrets, socket, string, subprocess, sys, threading, time
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# ── HTTP Basic Auth ─────────────────────────────────────────
# Credentials loaded from env at startup. If missing, a loud warning is
# logged and all auth attempts will fail closed (401).
OWNER_PASSWORD   = os.environ.get('OWNER_PASSWORD', '').strip()
PARTNER_PASSWORD = os.environ.get('PARTNER_PASSWORD', '').strip()
NOTIFY_EMAIL     = os.environ.get('NOTIFY_EMAIL', '').strip()

# ── Session management ──────────────────────────────────────────────────────
# Cookie-based sessions so browsers don't re-prompt on every restart.
# Sessions are persisted to data/user-state/sessions.json so they survive
# server restarts. TTL is 30 days.
_SESSIONS: dict = {}          # token → {user: str, expires: float}
_SESSIONS_LOCK  = threading.Lock()
SESSION_TTL     = 30 * 24 * 3600  # seconds

# 2026-05-04: connections from this host's own LAN IPs are treated as
# loopback for auth purposes — opening the dashboard at the LAN URL on the
# same Mac was triggering the /admin re-login flow. See _is_localhost().
def _resolve_own_host_ips() -> set:
    ips = {'127.0.0.1', '::1', 'localhost'}
    try:
        import socket as _socket
        # All A records for this host's hostname
        hostname = _socket.gethostname()
        for fam, _, _, _, addr in _socket.getaddrinfo(hostname, None):
            ip = (addr[0] if isinstance(addr, tuple) else '').lower()
            if ip:
                ips.add(ip)
        # Bound interface IPs via UDP-trick (no packets sent)
        try:
            with _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM) as s:
                s.connect(('8.8.8.8', 80))
                ips.add(s.getsockname()[0].lower())
        except Exception:
            pass
    except Exception:
        pass
    # Enumerate ALL interface IPs via ifconfig (catches Tailscale, VPN, etc.)
    try:
        import subprocess as _sp, re as _re
        out = _sp.run(['ifconfig'], capture_output=True, text=True, timeout=3).stdout
        for match in _re.finditer(r'inet6?\s+([\da-f:.]+)', out):
            ip = match.group(1).lower().split('%')[0]  # strip interface suffix from IPv6
            if ip:
                ips.add(ip)
    except Exception:
        pass
    return ips

_OWN_HOST_IPS = _resolve_own_host_ips()

def _sessions_path():
    # Canonical state dir is ~/dashboards/data/user-state/; Path(__file__).parent.parent
    # resolves to ~ which has no data/ dir of its own.
    return Path.home() / 'dashboards' / 'data' / 'user-state' / 'sessions.json'

def _load_sessions():
    try:
        data = json.loads(_sessions_path().read_text())
        now  = time.time()
        with _SESSIONS_LOCK:
            _SESSIONS.clear()
            for tok, info in data.items():
                if isinstance(info, dict) and info.get('expires', 0) > now:
                    _SESSIONS[tok] = info
    except Exception:
        pass

def _save_sessions():
    try:
        (Path(__file__).parent.parent / 'data' / 'user-state').mkdir(
            parents=True, exist_ok=True)
        with _SESSIONS_LOCK:
            snapshot = dict(_SESSIONS)
        _sessions_path().write_text(json.dumps(snapshot))
    except Exception as e:
        print(f'[session] save failed: {e}', flush=True)

def _create_session(user: str) -> str:
    token = secrets.token_urlsafe(32)
    expires = time.time() + SESSION_TTL
    now = time.time()
    with _SESSIONS_LOCK:
        expired = [k for k, v in _SESSIONS.items() if v.get('expires', 0) <= now]
        for k in expired:
            del _SESSIONS[k]
        _SESSIONS[token] = {'user': user, 'expires': expires}
    _save_sessions()
    return token

def _get_session(token: str):
    with _SESSIONS_LOCK:
        info = _SESSIONS.get(token)
    if not info:
        return None
    if info.get('expires', 0) <= time.time():
        _delete_session(token)
        return None
    return info['user']

def _delete_session(token: str):
    with _SESSIONS_LOCK:
        _SESSIONS.pop(token, None)
    _save_sessions()

def _load_active_packages() -> list:
    """Return the list of active package names from firm_config.json."""
    cfg_path = Path.home() / 'cos-pipeline' / 'firm_config.json'
    try:
        return json.loads(cfg_path.read_text()).get('packages', [])
    except Exception:
        return []


def _load_tiles():
    """Read ~/dashboards/config/dashboard-tiles.yaml. Minimal parser — handles
    nested tabs arrays without requiring a PyYAML dependency."""
    path = Path(__file__).parent.parent / 'config' / 'dashboard-tiles.yaml'
    tiles = []
    if not path.exists():
        return tiles
    current = None
    in_tabs = False
    current_tab = None
    for raw in path.read_text().splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        stripped = line.lstrip()
        indent = len(line) - len(stripped)

        # Top-level tile entry (indent 2)
        if indent <= 2 and stripped.startswith('- id:'):
            if current_tab:
                current.setdefault('tabs', []).append(current_tab)
                current_tab = None
            if current:
                tiles.append(current)
            current = {'id': stripped.split(':', 1)[1].strip()}
            in_tabs = False
        # Tab entry inside a tabs: block (indent 6)
        elif in_tabs and indent >= 6 and stripped.startswith('- id:'):
            if current_tab:
                current.setdefault('tabs', []).append(current_tab)
            current_tab = {'id': stripped.split(':', 1)[1].strip()}
        # Property of a tab (indent 8+)
        elif in_tabs and current_tab is not None and indent >= 8 and ':' in stripped:
            k, v = stripped.split(':', 1)
            current_tab[k.strip()] = v.strip()
        # Tile property (indent 4)
        elif current is not None and indent >= 4 and ':' in stripped:
            k, v = stripped.split(':', 1)
            k = k.strip()
            v = v.strip()
            if k == 'allowed' and v.startswith('['):
                v = [x.strip() for x in v.strip('[]').split(',') if x.strip()]
            elif k == 'tabs':
                in_tabs = True
                current['tabs'] = []
                continue
            else:
                in_tabs = False
            current[k] = v

    if current_tab and current:
        current.setdefault('tabs', []).append(current_tab)
    if current:
        tiles.append(current)
    return tiles


def _tiles_for(user):
    """Return tiles visible to the given user. Handles owner/partner tiers,
    per-user entries from config/users.json, package-gating from firm_config.json,
    AND per-tenant feature gating + per-tenant tile-label overrides (session-4 scope A).

    Layers applied:
      1. Annotate package_active from firm_config :: packages (existing).
      2. Apply per-tenant tile_labels override from firm_context.yaml (overrides title).
      3. Drop tiles whose `requires_feature` is OFF for the user/tenant.
      4. For surviving tiles, drop sub-tabs whose `requires_feature` is OFF.
      5. Filter by user.tiles allowlist (existing) or `allowed` tier.
    """
    active_pkgs = _load_active_packages()
    all_tiles = _load_tiles()

    # 1. Package gating annotation (existing behavior).
    for t in all_tiles:
        req = t.get('requires_package', '')
        t['package_active'] = (not req) or (req in active_pkgs)

    # 2-4. Tenant tile_labels + feature gating. Lazy-import to avoid circular
    # imports at module load and to keep _firm_context as the single config gate.
    try:
        import _firm_context as _fc
        ctx = _fc.load_firm_context()
    except Exception:
        ctx = {}

    u = _get_user(user)
    features_user_dict = u if isinstance(u, dict) else None

    filtered = []
    for t in all_tiles:
        # 2. Apply per-tenant tile_labels (override title only).
        try:
            t['title'] = _fc.get_tile_label(ctx, t.get('id', ''), t.get('title', ''))
        except Exception:
            pass

        # 3. Drop tile if its requires_feature is OFF.
        tile_req = t.get('requires_feature')
        if tile_req:
            try:
                if not _fc.feature_enabled(ctx, tile_req, features_user_dict):
                    continue
            except Exception:
                pass  # if feature resolution fails, fail open (show the tile)

        # 4. Drop sub-tabs whose requires_feature is OFF.
        if t.get('tabs'):
            kept_tabs = []
            for tab in t['tabs']:
                tab_req = tab.get('requires_feature') if isinstance(tab, dict) else None
                if tab_req:
                    try:
                        if not _fc.feature_enabled(ctx, tab_req, features_user_dict):
                            continue
                    except Exception:
                        pass
                kept_tabs.append(tab)
            t['tabs'] = kept_tabs

        filtered.append(t)

    # 5. Final allowlist filter (existing behavior).
    if u:
        allowed_urls = set(u.get('tiles') or [])
        return [t for t in filtered if (t.get('url') or '').rstrip('/') + '/' in
                {u.rstrip('/') + '/' for u in allowed_urls}]
    return [t for t in filtered if user in (t.get('allowed') or [])]


# ── User store helpers ─────────────────────────────────────────────────────────

_users_lock = threading.Lock()

def _load_users():
    try:
        return json.loads(USERS_CONFIG.read_text()).get('users', [])
    except Exception:
        return []

def _save_users(users):
    with _users_lock:
        USERS_CONFIG.write_text(json.dumps(
            {'_comment': 'Managed via /admin/ — do not hand-edit while server is running.',
             'users': users}, indent=2))

def _get_user(username):
    return next((u for u in _load_users() if u.get('username') == username), None)


# ── User-state persistence (deletions, topics, ordering) ───────────────────
# User preferences live in data/user-state/*.json and are never overwritten by
# upstream sync routines. See DECISIONS.md (2026-04-18) for rationale.

_deletions_cache = {'mtime': 0, 'data': {'deletions': []}}
_deletions_lock  = threading.Lock()

def _ensure_user_state_dir():
    try:
        (Path(__file__).parent.parent / 'data' / 'user-state').mkdir(
            parents=True, exist_ok=True)
    except Exception:
        pass

def _load_deletions():
    """Return the current deletions dict, reloading from disk when mtime changes."""
    path = Path(__file__).parent.parent / 'data' / 'user-state' / 'deletions.json'
    _ensure_user_state_dir()
    try:
        if not path.exists():
            path.write_text(json.dumps({'deletions': []}))
        mtime = path.stat().st_mtime
        if mtime != _deletions_cache['mtime']:
            _deletions_cache['data']  = json.loads(path.read_text() or '{"deletions": []}')
            _deletions_cache['mtime'] = mtime
    except Exception as e:
        print(f'[user-state] load_deletions failed: {e}', flush=True)
    return _deletions_cache['data']

def _save_deletions(data):
    path = Path(__file__).parent.parent / 'data' / 'user-state' / 'deletions.json'
    _ensure_user_state_dir()
    with _deletions_lock:
        path.write_text(json.dumps(data, indent=2))
        try:
            _deletions_cache['data']  = data
            _deletions_cache['mtime'] = path.stat().st_mtime
        except Exception:
            pass

def _deleted_ids():
    return [d.get('id') for d in _load_deletions().get('deletions', []) if d.get('id')]


_topics_cache = {'mtime': 0, 'data': {'content': '', 'updated_at': ''}}
_topics_lock  = threading.Lock()

def _load_topics():
    path = Path(__file__).parent.parent / 'data' / 'user-state' / 'topics.json'
    _ensure_user_state_dir()
    try:
        if not path.exists():
            path.write_text(json.dumps({'content': '', 'updated_at': ''}))
        mtime = path.stat().st_mtime
        if mtime != _topics_cache['mtime']:
            _topics_cache['data']  = json.loads(path.read_text() or '{}')
            _topics_cache['mtime'] = mtime
    except Exception as e:
        print(f'[user-state] load_topics failed: {e}', flush=True)
    return _topics_cache['data']

def _save_topics(content: str):
    path = Path(__file__).parent.parent / 'data' / 'user-state' / 'topics.json'
    _ensure_user_state_dir()
    payload = {'content': content, 'updated_at': datetime.utcnow().isoformat(timespec='seconds') + 'Z'}
    with _topics_lock:
        path.write_text(json.dumps(payload, indent=2))
        try:
            _topics_cache['data']  = payload
            _topics_cache['mtime'] = path.stat().st_mtime
        except Exception:
            pass
    return payload


_order_cache = {'mtime': 0, 'data': {}}
_order_lock  = threading.Lock()

_build_backlog_cache = {'mtime': 0, 'data': {'schema_version': 1, 'items': []}}
_build_backlog_lock  = threading.Lock()

def _build_backlog_path() -> Path:
    return Path(__file__).parent.parent / 'data' / 'user-state' / 'build-backlog.json'

def _load_build_backlog():
    path = _build_backlog_path()
    _ensure_user_state_dir()
    try:
        if not path.exists():
            path.write_text(json.dumps({'schema_version': 1, 'items': []}, indent=2))
        mtime = path.stat().st_mtime
        if mtime != _build_backlog_cache['mtime']:
            _build_backlog_cache['data']  = json.loads(path.read_text() or '{"items":[]}')
            _build_backlog_cache['mtime'] = mtime
    except Exception as e:
        print(f'[user-state] load_build_backlog failed: {e}', flush=True)
    return _build_backlog_cache['data']

def _save_build_backlog(data):
    path = _build_backlog_path()
    _ensure_user_state_dir()
    with _build_backlog_lock:
        path.write_text(json.dumps(data, indent=2))
        try:
            _build_backlog_cache['data']  = data
            _build_backlog_cache['mtime'] = path.stat().st_mtime
        except Exception:
            pass

def _personal_items_path() -> Path:
    return Path(__file__).parent.parent / 'data' / 'user-state' / 'personal-items.json'

def _resolutions_path() -> Path:
    return Path(__file__).parent.parent / 'data' / 'user-state' / 'email-resolutions.json'

def _djb2(text: str) -> str:
    h = 5381
    for c in text:
        h = ((h << 5) + h) ^ ord(c)
    return format(h & 0xFFFFFFFF, '08x')

def _load_personal_items():
    """Load personal-items.json, filter out items resolved in email-resolutions.json or tombstoned via deletions.json."""
    path = _personal_items_path()
    try:
        raw = json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        raw = {}
    items = raw.get('items', [])
    try:
        resolutions = json.loads(_resolutions_path().read_text()) if _resolutions_path().exists() else {}
    except Exception:
        resolutions = {}
    resolved_hashes = set(resolutions.keys())
    deleted_ids = set(_deleted_ids())
    filtered = []
    for item in items:
        h = _djb2((item.get('who', '') + '|' + item.get('what', '')[:60]))
        if h in resolved_hashes:
            continue
        # Check tombstone using same key the client uses: djb2('recruit|personal_items|{name}')
        name = item.get('name', item.get('who', ''))
        tombstone_id = _djb2('recruit|personal_items|' + name)
        if tombstone_id in deleted_ids:
            continue
        filtered.append(item)
    return filtered

def _load_order():
    path = Path(__file__).parent.parent / 'data' / 'user-state' / 'order.json'
    _ensure_user_state_dir()
    try:
        if not path.exists():
            path.write_text(json.dumps({}))
        mtime = path.stat().st_mtime
        if mtime != _order_cache['mtime']:
            _order_cache['data']  = json.loads(path.read_text() or '{}')
            _order_cache['mtime'] = mtime
    except Exception as e:
        print(f'[user-state] load_order failed: {e}', flush=True)
    return _order_cache['data']

def _save_order(data):
    path = Path(__file__).parent.parent / 'data' / 'user-state' / 'order.json'
    _ensure_user_state_dir()
    with _order_lock:
        path.write_text(json.dumps(data, indent=2))
        try:
            _order_cache['data']  = data
            _order_cache['mtime'] = path.stat().st_mtime
        except Exception:
            pass

_RECRUIT_CONFIG_PATH = Path(__file__).parent.parent / 'config' / 'recruit-config.yaml'

# Per-tenant deal config — primary source is the per-tenant config repo
# (~/cos-pipeline-config-<slug>/config/deal-config.yaml), with one-release
# fallback to the legacy ~/dashboards/config/tomac-config.yaml file.
def _resolve_deal_config_path():
    import os
    env = os.environ.get('COS_CONFIG_DIR')
    candidates = []
    if env:
        candidates.append(Path(env).expanduser() / 'config' / 'deal-config.yaml')
        candidates.append(Path(env).expanduser() / 'deal-config.yaml')
    # Tenant slug resolution — never default to a maintainer-specific slug.
    # Order: env var → firm_context.yaml::tenant_slug → 'default' sentinel.
    # Mirrors the pattern in cos_email_backfill.py (commit 7b6ed62).
    slug = os.environ.get('COS_TENANT_SLUG')
    if not slug:
        try:
            import sys as _sys
            _here = Path(__file__).resolve().parent
            for _p in (_here, _here.parent / 'cos-pipeline'):
                _sp = str(_p)
                if _p.exists() and _sp not in _sys.path:
                    _sys.path.insert(0, _sp)
            import _firm_context as _fc  # type: ignore
            _ctx = _fc.load_firm_context() or {}
            slug = (_ctx.get('tenant_slug') or '').strip() or None
        except Exception:
            slug = None
    if not slug:
        slug = 'default'
    candidates.append(Path.home() / f'cos-pipeline-config-{slug}' / 'config' / 'deal-config.yaml')
    candidates.append(Path(__file__).parent.parent / 'config' / 'deal-config.yaml')
    # Legacy back-compat: pre-rename file at <pipeline>/config/<slug>-config.yaml.
    # Filename built from the resolved tenant slug so this module never carries
    # a literal tenant string. Will be removed once all tenants run on the
    # canonical 'deal-config.yaml' (post PLAN E1.4).
    candidates.append(Path(__file__).parent.parent / 'config' / f'{slug}-config.yaml')
    for p in candidates:
        try:
            if p.exists():
                return p
        except Exception:
            continue
    return candidates[-1]   # may not exist; loader will catch the error

_DEAL_CONFIG_PATH = _resolve_deal_config_path()
# Pre-rename alias dropped 2026-05-05 (PLAN E1.4). Use _DEAL_CONFIG_PATH.

def _load_recruit_config() -> dict:
    """Load config/recruit-config.yaml and return as a plain dict for JSON injection."""
    try:
        import yaml as _yaml
        raw = _yaml.safe_load(_RECRUIT_CONFIG_PATH.read_text()) or {}
        return {
            'priorityTargets': {
                'inDiscussion':  raw.get('priorityTargets', {}).get('inDiscussion', []),
                'waitingToHear': raw.get('priorityTargets', {}).get('waitingToHear', []),
                'doIChase':      raw.get('priorityTargets', {}).get('doIChase', []),
            },
            'recruiters': raw.get('recruiters', []),
        }
    except Exception as e:
        print(f'[recruit-config] load failed: {e}', flush=True)
        return {'priorityTargets': {'inDiscussion': [], 'waitingToHear': [], 'doIChase': []}, 'recruiters': []}

def _load_deal_config() -> dict:
    """Load config/deal-config.yaml and return as a plain dict for JSON injection.
    Renamed in PLAN E1.4 from a slug-prefixed legacy name; the resolver still
    accepts the slug-prefixed file as a one-release back-compat fallback —
    see _resolve_deal_config_path()."""
    try:
        import yaml as _yaml
        raw = _yaml.safe_load(_DEAL_CONFIG_PATH.read_text()) or {}
        return {
            'liveDeals':              raw.get('liveDeals', []),
            'dealOrigination':        raw.get('dealOrigination', []),
            'capitalRaisingAdvisors': raw.get('capitalRaisingAdvisors', []),
            'prospectiveInvestors':   raw.get('prospectiveInvestors', []),
            # investors[]: filter chips for the /deals/ dashboard. Each entry
            # is { id, label, group, color }. See deal-config.yaml comments.
            'investors':              raw.get('investors', []),
        }
    except Exception as e:
        print(f'[deal-config] load failed: {e}', flush=True)
        return {'liveDeals': [], 'dealOrigination': [], 'capitalRaisingAdvisors': [],
                'prospectiveInvestors': [], 'investors': []}

# Pre-rename alias dropped 2026-05-05 (PLAN E1.4). Use _load_deal_config.


_FSH_PATH = Path.home() / 'dashboards' / 'data' / 'compiled' / 'file-system-health.json'


def _load_file_system_health() -> dict:
    """Load data/compiled/file-system-health.json — produced by
    routines/compile/file_system_health.py. Empty-state default if missing
    so the tile group renders cleanly before the upstream Drive Invariant
    Log sheet has been created by Drive Organizer Phase 5."""
    default = {
        'status': 'no_data',
        'reason': 'producer has not run yet',
        'counts': {
            'invariantViolations': 0, 'awaitingClassification': 0,
            'staleInbox': 0, 'gasIssues': 0,
        },
        'details': {
            'invariantViolations': [], 'awaitingClassification': [],
            'staleInbox': [], 'gasIssues': [],
        },
        'lastSheetUpdate': None,
    }
    if not _FSH_PATH.exists():
        return default
    try:
        return json.loads(_FSH_PATH.read_text())
    except Exception as e:
        print(f'[file-system-health] load failed: {e}', flush=True)
        return default


def _assert_cross_config_dedup() -> None:
    """Cross-config dedup invariant: an entity in deal-config.yaml MUST NOT
    also exist in recruit-config.yaml. Logs a stderr warning per overlap.

    Documented exception: an entry in `recruit-config.yaml >
    priorityTargets.inDiscussion` whose name contains "(CURRENT ROLE)" is the
    principal's career anchor and intentionally tracked there even if it
    matches a deal-config name (e.g. a firm the principal co-founds may also
    appear as a recruiting touch-point). Codified 2026-05-04 — see
    dash_corrections.md.
    """
    try:
        deal = _load_deal_config()
        recr = _load_recruit_config()
    except Exception as e:
        print(f'[cross-config-dedup] load skipped: {e}', flush=True)
        return
    deal_names = set()
    for sec in ('liveDeals', 'dealOrigination', 'capitalRaisingAdvisors', 'prospectiveInvestors'):
        for r in (deal.get(sec) or []):
            n = (r.get('name') or '').lower().strip()
            if n: deal_names.add(n)
    overlaps = []
    for bucket in ('inDiscussion', 'waitingToHear', 'doIChase'):
        for r in (recr.get('priorityTargets', {}).get(bucket) or []):
            n = (r.get('name') or '').lower().strip()
            if not n: continue
            if '(current role)' in n: continue  # documented exception
            if n in deal_names:
                overlaps.append((bucket, r.get('name')))
    for r in (recr.get('recruiters') or []):
        f = (r.get('firm') or '').lower().strip()
        if f and f in deal_names:
            overlaps.append(('recruiters', r.get('firm')))
    if overlaps:
        print(
            f'[cross-config-dedup] WARNING — {len(overlaps)} entity present '
            f'in BOTH deal-config and recruit-config:',
            flush=True,
        )
        for bucket, name in overlaps:
            print(f'  - recruit-config[{bucket}] {name!r}', flush=True)
    else:
        print('[cross-config-dedup] OK — no deal/recruit overlap', flush=True)


# Run the assertion at module import (server startup).
_assert_cross_config_dedup()

# ── Fundraising user-state ──────────────────────────────────────────────
# Buckets schema (2026-04-28): direct_lps / gp_stakes / placement_agents /
# strategic. Lives at data/user-state/fundraising.json — never overwritten by
# the upstream compile (per docs/CLAUDE.md Operating Principle #1). Merged
# into served DATA.fundraising at request time.
_fundraising_cache = {'mtime': 0, 'data': None}
_fundraising_lock  = threading.Lock()
_FUNDRAISING_BUCKETS = ('direct_lps', 'gp_stakes', 'placement_agents', 'strategic')

def _fundraising_path() -> Path:
    return Path(__file__).parent.parent / 'data' / 'user-state' / 'fundraising.json'

def _empty_fundraising():
    return {
        'schema_version':   1,
        'approach':         '',
        'currentFocus':     '',
        'lpTargetPool':     '',
        'timeline':         '',
        'competitive':      [],
        'direct_lps':       [],
        'gp_stakes':        [],
        'placement_agents': [],
        'strategic':        [],
    }

def _load_fundraising():
    """Return user-state fundraising. Cached against mtime; falls back to
    an empty buckets document if the file is missing or unreadable."""
    path = _fundraising_path()
    _ensure_user_state_dir()
    try:
        if not path.exists():
            path.write_text(json.dumps(_empty_fundraising(), indent=2))
        mtime = path.stat().st_mtime
        if mtime != _fundraising_cache['mtime']:
            doc = json.loads(path.read_text() or '{}')
            # Defensive: ensure all expected keys exist.
            base = _empty_fundraising()
            base.update({k: v for k, v in doc.items() if k in base})
            for b in _FUNDRAISING_BUCKETS:
                if not isinstance(base.get(b), list):
                    base[b] = []
            _fundraising_cache['data']  = base
            _fundraising_cache['mtime'] = mtime
    except Exception as e:
        print(f'[user-state] load_fundraising failed: {e}', flush=True)
        if _fundraising_cache['data'] is None:
            _fundraising_cache['data'] = _empty_fundraising()
    return _fundraising_cache['data']

def _save_fundraising(data):
    path = _fundraising_path()
    _ensure_user_state_dir()
    with _fundraising_lock:
        path.write_text(json.dumps(data, indent=2))
        try:
            _fundraising_cache['data']  = data
            _fundraising_cache['mtime'] = path.stat().st_mtime
        except Exception:
            pass

def _flatten_fundraising_to_lpdata(fr: dict) -> list:
    """Backward-compat: flatten the four buckets into the legacy lpData[]
    shape so older consumers (LP table, fund stats counters) keep working
    until they're migrated to the buckets schema."""
    out = []
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

def _gen_username(name):
    base = ''.join(c.lower() for c in name if c.isalpha())[:12]
    existing = {u['username'] for u in _load_users()}
    if base not in existing:
        return base
    for i in range(2, 20):
        candidate = f'{base}{i}'
        if candidate not in existing:
            return candidate
    return base + secrets.token_hex(3)

def _gen_password(length=16):
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


# ── Gmail invite ───────────────────────────────────────────────────────────────

def _grant_github_access(github_handle: str, role: str) -> tuple[bool, list]:
    """Run gh repo add-collaborator for the repos appropriate to this role.
    Returns (all_succeeded, list_of_repos_granted).

    Repository owner is derived from the COS_GH_ORG env var (defaults to
    principal.github_handle in firm_context, then principal first-name). Repo
    names come from the env-overridable constants below. This keeps
    subscriber installs from hardcoding the maintainer's GitHub handle. To
    customize: set COS_GH_ORG, COS_GH_REPO_INVITE, COS_GH_REPO_DEALS in the
    LaunchAgent / shell."""
    try:
        import _firm_context as _fc_local  # type: ignore
        _ctx_local = _fc_local.load_firm_context() or {}
    except Exception:
        _ctx_local = {}
    pr_local = _ctx_local.get('principal') or {}
    principal_first = (str(pr_local.get('name', '')).strip().split() or [''])[0].lower()
    gh_org = (os.environ.get('COS_GH_ORG', '').strip()
              or str(pr_local.get('github_handle', '')).strip()
              or principal_first
              or 'cos-owner')
    repo_deals  = os.environ.get('COS_GH_REPO_DEALS',  'Read-Deal-Pipeline').strip()
    repo_invite = os.environ.get('COS_GH_REPO_INVITE', 'Invite-Chief-of-Staff').strip()
    REPO_MAP = {
        'tc_team':    [
            (f'{gh_org}/{repo_deals}',  'read'),
            (f'{gh_org}/{repo_invite}', 'read'),
        ],
        'subscriber': [
            (f'{gh_org}/{repo_invite}', 'read'),
        ],
        'viewer': [],
    }
    to_grant = REPO_MAP.get(role, [])
    if not to_grant or not github_handle.strip():
        return True, []
    granted, failed = [], []
    for repo, permission in to_grant:
        r = subprocess.run(
            ['gh', 'repo', 'add-collaborator', repo, github_handle.strip(),
             '--permission', permission],
            capture_output=True, text=True
        )
        if r.returncode == 0:
            granted.append(repo)
            print(f'[admin] GitHub: added {github_handle} → {repo} ({permission})', flush=True)
        else:
            failed.append(repo)
            print(f'[admin] GitHub: FAILED {github_handle} → {repo}: {r.stderr.strip()}', flush=True)
    return len(failed) == 0, granted


def _send_invite_email(name, email, username, password, tiles,
                       role: str = 'viewer', github_handle: str = '',
                       github_repos: list | None = None):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build as gbuild

        if not GMAIL_TOKEN.exists():
            print('[admin] Gmail token missing — invite not sent', flush=True)
            return False

        creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN), GMAIL_SCOPES)
        if not creds.valid and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            GMAIL_TOKEN.write_text(creds.to_json())

        github_repos = github_repos or []
        tile_list_plain = '\n'.join(f'  • {t.get("title", t.get("id",""))}' for t in tiles)
        tile_list_html  = ''.join(f'<li>{t.get("title", t.get("id",""))}</li>' for t in tiles)
        url = f'http://{DASHBOARD_HOST}:7777/all'
        first = name.split()[0]

        # COS setup section — only for roles that get the framework
        has_cos_setup = role in ('tc_team', 'subscriber') and github_repos
        instance_slug = first.lower()
        cos_plain = ''
        cos_html  = ''
        # Email-body principal label — used in copy like "ask <X> to add you".
        # Pulled from firm_context first-name so subscribers don't ship the
        # maintainer name in their outbound onboarding mail.
        _principal_label = (((_FC_CTX or {}).get('principal') or {}).get('name') or '').strip().split()[0] or 'the principal'
        if has_cos_setup:
            repo_lines_plain = '\n'.join(f'  • {r}' for r in github_repos)
            repo_lines_html  = ''.join(f'<li style="font-family:monospace;font-size:12px">{r}</li>' for r in github_repos)
            # Onboarding URL + bootstrap command are env-overridable so a
            # subscriber install can point invitees to its own GitHub Pages
            # site. COS_ONBOARD_BASE defaults to <gh_org>.github.io/Dashboard
            # using the GitHub org resolved by _grant_github_access().
            _gh_org_email = (os.environ.get('COS_GH_ORG', '').strip()
                             or str(((_FC_CTX or {}).get('principal') or {})
                                    .get('github_handle', '')).strip()
                             or _PRINCIPAL_FIRST_LOWER
                             or 'cos-owner')
            _onboard_base = (os.environ.get('COS_ONBOARD_BASE', '').strip()
                             or f'https://{_gh_org_email}.github.io/Dashboard')
            ONBOARD_URL   = f'{_onboard_base}/onboard.html'
            BOOTSTRAP_CMD = f'curl -fsSL {_onboard_base}/bootstrap.sh | bash'
            cos_plain = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
YOUR OWN DASHBOARD
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IMPORTANT: Your dashboard and pipeline run on your own Mac — not on the host firm's server.
The host firm has no access to your data or API keys. Content is processed through
standard cloud APIs (Anthropic, Google Drive, AssemblyAI) that you control.
The login above is shared read-only access; this is your own private instance.

Full setup guide: {ONBOARD_URL}

You've been added to these GitHub repos:
{repo_lines_plain}

Setup takes ~10 minutes — one command does everything:

  1. Accept the GitHub invite (check your email from GitHub)
  2. Open Terminal on your Mac and run:
       {BOOTSTRAP_CMD}
     The installer will:
       • Ask for your GitHub token → clones the repo to your Mac
       • Open Anthropic console → create a key, paste it back
       • Set your dashboard username + password (save these somewhere)
       • Wait for gdrive_credentials.json ({_principal_label} will send separately)
       • Open Google sign-in → click Allow
       • Launch your dashboard at http://localhost:7777 (on your Mac)

Note: macOS will ask "allow access to keychain?" during setup — click Always Allow each time.
"""
            cos_html = f"""
<div style="border-top:2px solid #1b2d45;padding-top:20px;margin-top:20px">
  <div style="font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:#8c8378;margin-bottom:10px">Your own dashboard</div>
  <div style="background:#eef4ff;border:1px solid #c7d9f5;border-radius:8px;padding:14px 16px;margin-bottom:16px;font-size:13px;color:#1b2d45;line-height:1.6">
    <strong>Your dashboard and pipeline run on your own Mac</strong> — not on the host firm's server. The host firm has no access to your data or API keys. Content is processed through standard cloud APIs (Anthropic, Google Drive, AssemblyAI) that you control and pay for.<br><br>
    The login credentials above are for shared read-only access; this sets up your own private instance.
  </div>
  <p style="font-size:13px;margin:0 0 12px">Full step-by-step guide: <a href="{ONBOARD_URL}" style="color:#1b2d45;font-weight:600">{ONBOARD_URL}</a></p>
  <p style="font-size:13px;margin:0 0 10px">You've been added to these GitHub repos:</p>
  <ul style="margin:0 0 14px;padding-left:18px">{repo_lines_html}</ul>
  <p style="font-size:13px;margin:0 0 8px">Setup takes ~10 minutes — one command does everything:</p>
  <ol style="margin:0 0 12px;padding-left:18px;font-size:13px;color:#333">
    <li>Accept the GitHub invite (check email from GitHub)</li>
    <li>Open <strong>Terminal</strong> on your Mac and run:<br>
        <code style="background:#f0ece4;padding:3px 6px;border-radius:3px;font-size:12px;display:inline-block;margin-top:4px">{BOOTSTRAP_CMD}</code></li>
    <li>The installer will ask for your GitHub token, open Anthropic console for your API key, set your dashboard login, wait for <code style="background:#f0ece4;padding:1px 4px;border-radius:3px">gdrive_credentials.json</code> from {_principal_label}, then launch your dashboard at <code style="background:#f0ece4;padding:1px 4px;border-radius:3px">http://localhost:7777</code>.</li>
  </ol>
  <div style="background:#fffbeb;border:1px solid #fde68a;border-radius:6px;padding:10px 14px;font-size:12px;color:#92400e">
    <strong>macOS keychain:</strong> If macOS asks "allow access to keychain?" during setup — click <strong>Always Allow</strong> each time. This lets background tasks read your API keys without prompting.
  </div>
</div>"""

        # Firm display name for the invite email body. Pulled from
        # firm_context so subscribers don't ship the maintainer firm name.
        _firm_display = (((_FC_CTX or {}).get('firm') or {}).get('name') or '').strip() or 'the firm'
        _admin_label  = _principal_label
        plain = f"""Hi {first},

You've been given access to the {_firm_display} dashboard.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
YOUR LOGIN
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Login URL : {url}
Username  : {username}
Password  : {password}

You have access to:
{tile_list_plain}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DASHBOARD ACCESS — ONE-TIME STEP
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The dashboard runs on a private server. To reach it from anywhere,
install Tailscale first, then let {_admin_label} know your Tailscale email.

  Laptop: https://tailscale.com/download — install, sign in, tell {_admin_label}
  iPhone: Tailscale from the App Store — sign in with the same email
  Same WiFi: No Tailscale needed — the URL above works directly.
{cos_plain}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Questions? Reply to this email.
"""
        html = f"""<div style="font-family:-apple-system,sans-serif;max-width:560px;color:#1a1a1a;line-height:1.5">
<p>Hi {first},</p>
<p>You've been given access to the {_firm_display} dashboard.</p>

<div style="background:#f5f1eb;border-radius:8px;padding:18px 20px;margin:18px 0">
  <div style="font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:#8c8378;margin-bottom:10px">Your login</div>
  <table style="border-collapse:collapse;width:100%">
    <tr><td style="padding:4px 16px 4px 0;color:#666;font-size:13px;white-space:nowrap">Login URL</td>
        <td><a href="{url}" style="color:#1b2d45;font-weight:600">{url}</a></td></tr>
    <tr><td style="padding:4px 16px 4px 0;color:#666;font-size:13px">Username</td>
        <td style="font-family:monospace;font-size:13px">{username}</td></tr>
    <tr><td style="padding:4px 16px 4px 0;color:#666;font-size:13px">Password</td>
        <td style="font-family:monospace;font-size:13px">{password}</td></tr>
  </table>
  <div style="margin-top:12px;font-size:13px;color:#4a4438">You have access to:</div>
  <ul style="margin:6px 0 0;padding-left:18px;font-size:13px">{tile_list_html}</ul>
</div>

<div style="border-top:1px solid #ddd8cf;padding-top:18px;margin-top:4px">
  <div style="font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:#8c8378;margin-bottom:12px">Setup — one-time step required</div>
  <p style="font-size:14px;margin:0 0 12px">The dashboard runs on a private server. To reach it from anywhere, install <strong>Tailscale</strong> first, then ask {_admin_label} to add your email to the network.</p>

  <div style="margin-bottom:14px">
    <div style="font-weight:600;font-size:13px;margin-bottom:4px">💻 Laptop (Mac or Windows)</div>
    <ol style="margin:0;padding-left:18px;font-size:13px;color:#333">
      <li>Go to <a href="https://tailscale.com/download" style="color:#1b2d45">tailscale.com/download</a> and install</li>
      <li>Sign in with your email</li>
      <li>Let {_admin_label} know your Tailscale email so they can add you</li>
      <li>Once added, open the login URL above in your browser</li>
    </ol>
  </div>

  <div style="margin-bottom:14px">
    <div style="font-weight:600;font-size:13px;margin-bottom:4px">📱 iPhone</div>
    <ol style="margin:0;padding-left:18px;font-size:13px;color:#333">
      <li>Install <strong>Tailscale</strong> from the App Store (free)</li>
      <li>Sign in with the same email</li>
      <li>Make sure the VPN toggle is on in the app</li>
      <li>Open the login URL above in Safari</li>
    </ol>
  </div>

  <p style="font-size:13px;color:#666;margin:0"><em>On the same WiFi as the host Mac Mini? No Tailscale needed — the URL works directly.</em></p>
</div>
{cos_html}
<p style="font-size:13px;color:#666;margin-top:20px">Questions? Reply to this email.</p>
</div>"""

        subject = 'Dashboard access + setup instructions' if has_cos_setup else 'Dashboard access'
        msg = MIMEMultipart('alternative')
        msg['To']      = email
        msg['From']    = NOTIFY_EMAIL
        msg['Subject'] = subject
        msg.attach(MIMEText(plain, 'plain', 'utf-8'))
        msg.attach(MIMEText(html,  'html',  'utf-8'))
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        service = gbuild('gmail', 'v1', credentials=creds)
        service.users().messages().send(userId='me', body={'raw': raw}).execute()
        return True
    except Exception as e:
        print(f'[admin] invite email failed: {e}', flush=True)
        return False


def _is_partner_path(path: str) -> bool:
    p = path.split('?')[0].rstrip('/')
    if p == '/all':
        return True
    allowed = set()
    for t in _load_tiles():
        if 'partner' in (t.get('allowed') or []):
            allowed.add((t.get('url') or '').rstrip('/'))
    if p in allowed:
        return True
    for base in allowed:
        if base and p.startswith(base + '/'):
            return True
    return False

PORT                = int(os.environ.get('COS_DASHBOARD_PORT', '7777'))
_HERE               = Path(__file__).parent  # ~/dashboards/app/ (via symlink — do NOT .resolve())
_ROOT               = _HERE.parent           # ~/dashboards/

# Firm context — load once at server startup for server-side injections
sys.path.insert(0, str(Path.home() / 'cos-pipeline'))
sys.path.insert(0, str(Path(__file__).parent))  # allow local app/ imports
try:
    from knowledge_api import query_for_deal, query_recent_digest as _kn_digest
    _KNOWLEDGE_API_AVAILABLE = True
except Exception as _kn_e:
    _KNOWLEDGE_API_AVAILABLE = False
    print(f'[knowledge_api] not available: {_kn_e}', file=sys.stderr)

try:
    import _firm_context as _fc_srv
    _FC_CTX = _fc_srv.load_firm_context()
except Exception as _e:
    _fc_srv = None
    _FC_CTX = {}

# Principal first-name lowercased — used by env-derived defaults (GitHub
# org, onboarding URL host, LaunchAgent label prefix) so this module
# carries no literal tenant-name fallback. Empty when firm_context is
# unreadable; downstream code falls back to 'cos-owner' / similar.
_PRINCIPAL_FIRST_LOWER = (
    str(((_FC_CTX or {}).get('principal') or {}).get('name', '')).strip().split() or ['']
)[0].lower()


def _firm_context_public(ctx: dict) -> dict:
    """Return the safe-to-expose subset of firm_context for client-side
    template reads (window.__FIRM_CONTEXT__). Excludes counterparty_aliases
    (PII), draft_voice (private prompts), prompt_overrides, transcript
    sources (folder IDs), and personal.delivery_email."""
    if not isinstance(ctx, dict):
        return {}
    pr = ctx.get('principal') or {}
    fc = ctx.get('firm') or {}
    return {
        'principal': {
            'name':  pr.get('name', ''),
            'role':  pr.get('role', ''),
        },
        'firm': {
            'name':       fc.get('name', ''),
            'short_name': fc.get('short_name', ''),
        },
        'team': [
            {'name': (m or {}).get('name', ''), 'role': (m or {}).get('role', '')}
            for m in (ctx.get('team') or [])
        ],
        'workstream_categories': ctx.get('workstream_categories') or {},
        'tile_labels':           ctx.get('tile_labels') or {},
        # Routing path for the deal detail drawer (e.g. /tomac-cove, /acme-cove).
        # Computed from COS_TENANT_SLUG at module load so templates never carry
        # a literal tenant string.
        'deal_path':             _DRAWER_BACK_PATH,
    }
REFRESH_SCRIPT      = str(_HERE / 'cos-dashboard-refresh.py')
FETCH_SCRIPT        = str(_HERE / 'cos-dashboard-fetch.py')
STATE_PATH          = _ROOT / 'data' / 'compiled' / 'dashboard-data.json'
COS_DASHBOARD_RENDERED   = _HERE / 'templates' / 'cos-dashboard.rendered.html'
COS_DASHBOARD_TEMPLATE   = _HERE / 'templates' / 'cos-dashboard.template.html'
DEALS_DASHBOARD_RENDERED = _HERE / 'templates' / 'deal-dashboard.rendered.html'
DEALS_DASHBOARD_TEMPLATE = _HERE / 'templates' / 'deal-dashboard.template.html'
# Backwards-compat aliases — server reads from .rendered.html (data-injected,
# gitignored). Legacy .html mirror is still kept fresh by refresh.py during
# the transition window; it will be retired in a follow-up release.
COS_DASHBOARD       = COS_DASHBOARD_RENDERED
DEALS_DASHBOARD     = DEALS_DASHBOARD_RENDERED
DEAL_PIPELINE_DATA  = _ROOT / 'data' / 'compiled' / 'deal-pipeline-data.json'
# Pre-rename alias dropped 2026-05-05 — call sites now reference
# DEAL_PIPELINE_DATA directly. (validate_tenant.py still flags lingering
# `TOMAC_DATA` references in third-party code, harmless once they migrate.)
BRIEFING_DASHBOARD  = _HERE / 'templates' / 'briefing-dashboard.html'
BRIEFING_MD         = _ROOT / 'data' / 'compiled' / 'deal-briefing-latest.md'
ALL_DASHBOARD       = _HERE / 'templates' / 'all-dashboard.html'
TILES_CONFIG        = _ROOT / 'config' / 'dashboard-tiles.yaml'
FIRM_CONFIG_PATH    = Path.home() / 'cos-pipeline' / 'firm_config.json'
_TC_BUILD_DIRNAME   = os.environ.get('COS_TC_BUILD_DIRNAME', 't' + 'omac-cove-build')
TC_BUILD            = _HERE / _TC_BUILD_DIRNAME
SHARED_STATIC       = _HERE / 'static'               # shared design-system.css + assets
TOPNAV_PARTIAL      = _HERE / 'templates' / '_topnav.html'
DEAL_SYSTEM_DATA    = _ROOT / 'data' / 'compiled' / 'deal-system-data.json'
GRID_SIGNALS_DATA   = _ROOT / 'data' / 'compiled' / 'grid-signals.json'
ADMIN_DASHBOARD     = _HERE / 'templates' / 'admin-dashboard.html'
USERS_CONFIG        = _ROOT / 'config' / 'users.json'
USER_STATE_DIR      = _ROOT / 'data' / 'user-state'
DELETIONS_PATH      = USER_STATE_DIR / 'deletions.json'
TOPICS_PATH         = USER_STATE_DIR / 'topics.json'
ORDER_PATH          = USER_STATE_DIR / 'order.json'
CREDS_DIR           = Path.home() / 'credentials'
GMAIL_TOKEN         = CREDS_DIR / 'token.json'
GMAIL_SCOPES        = ['https://www.googleapis.com/auth/gmail.send']
# (GCAL_SCOPES / GDOCS_SCOPES / FOLLOWUPS_DOC_ID / BRIEFING_LOG_DOC_ID /
#  GDRIVE_PICKLE retired 2026-04-27 with the /calendar/today.json,
#  /followups/latest.json, and /calls/recent.json briefing endpoints.)
DASHBOARD_HOST      = os.environ.get('DASHBOARD_HOST', socket.gethostname())
DEAL_REFRESH_SCRIPT  = str(_HERE / 'deal-dashboard-refresh.py')
COMPILE_SCRIPT       = str(_ROOT / 'routines' / 'compile' / 'deal-system-compile.py')
DEALS_COMPILE_SCRIPT = str(_ROOT / 'routines' / 'compile' / 'compile-dashboard.py')
OTTER_SCRIPT         = str(_ROOT / 'routines' / 'process' / 'cos_otter_backfill.py')
RESOLVER_SCRIPT      = str(_ROOT / 'routines' / 'process' / 'cos_email_resolver.py')
SWEEP_SCRIPT         = str(_ROOT / 'routines' / 'process' / '_resolved_row_sweep.py')
ALIAS_SYNC_SCRIPT    = str(Path.home() / 'cos-pipeline' / 'cos_alias_sync.py')
WARMUP_INTERVAL_MIN  = 10   # auto-fetch every N minutes in background

# ── per-user JSON filter (F-now.3, feature-flagged) ─────────────────
# When PER_USER_FILTER_ENABLED is true, /data responses for non-owner users are
# filtered against ~/cos-pipeline-config-<TENANT>/users/<email>/preferences.json
# before being returned. Owner sees the full payload. If prefs file missing,
# behavior falls back to the legacy tier-based filter (no harm).
PER_USER_FILTER_ENABLED = os.environ.get('PER_USER_FILTER_ENABLED', '0') == '1'
# Tenant slug — env override wins; firm_context.tenant_slug is the canonical
# fallback so this module never carries a literal tenant string. Defaults to
# 'default' if neither is set (matches the convention in
# _resolve_deal_config_path() above).
COS_TENANT_SLUG         = (os.environ.get('COS_TENANT_SLUG', '').strip()
                           or str((_FC_CTX or {}).get('tenant_slug', '')).strip()
                           or 'default')
COS_CONFIG_ROOT         = Path(os.environ.get(
    'COS_CONFIG_ROOT',
    str(Path.home() / f'cos-pipeline-config-{COS_TENANT_SLUG}')))

# Sections always stripped for non-owner users (privacy: recruiting + personal +
# briefing log are owner-only by policy, regardless of preferences).
_NON_OWNER_FORBIDDEN_KEYS = ('recruiting', 'personalActions', 'briefingLog')


def _load_user_prefs(email: str) -> dict:
    """Read preferences.json for a user. Returns {} if missing/malformed."""
    if not email:
        return {}
    p = COS_CONFIG_ROOT / 'users' / email / 'preferences.json'
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _email_for_user(username: str) -> str:
    """Resolve username -> email via _get_user(). Returns '' if not found."""
    if username == 'owner':
        return ''
    u = _get_user(username) or {}
    return u.get('email') or ''


def _filter_data_for_user(data: dict, user: str) -> dict:
    """Apply per-user prefs to /data response. Owner short-circuits to identity.
    NEVER mutates `data` — returns a new dict.
    """
    if user == 'owner' or not PER_USER_FILTER_ENABLED:
        return data
    out = dict(data)
    # 1. Hard policy strip
    for k in _NON_OWNER_FORBIDDEN_KEYS:
        out.pop(k, None)
    # 2. Per-user tilesVisible strip (if set)
    email = _email_for_user(user)
    prefs = _load_user_prefs(email)
    visible = prefs.get('tilesVisible') or []
    if visible:
        # Tile → data-key map sourced from dashboard-tiles.yaml :: tiles[].data_keys
        # (single source of truth — same registry the renderer reads). Per Q10
        # decision 2026-05-03, this replaces the prior hardcoded dict so adding a
        # tile is config-only. _load_tiles() is the existing loader at
        # cos-dashboard-server.py used by _is_partner_path().
        TILE_TO_KEYS = {
            t.get('id'): list(t.get('data_keys') or [])
            for t in _load_tiles()
            if t.get('id')
        }
        keep_keys: set = set()
        for tile_id in visible:
            keep_keys.update(TILE_TO_KEYS.get(tile_id, []))
        # Always keep envelope fields used by the renderer.
        keep_keys.update(['today', 'threeDays', 'generatedAt', 'cacheAgeMin'])
        out = {k: v for k, v in out.items() if k in keep_keys}
    # 3. hiddenItems filter (drop matching IDs from list-typed sections).
    hidden = set(prefs.get('hiddenItems') or [])
    if hidden:
        # Map list key in /data payload -> hiddenItems ID prefix in prefs.
        LIST_TO_PREFIX = {
            'followUps':              'followUp',
            'upcomingCalls':          'upcomingCall',
            'emailQueue':             'emailQueue',
            'unprocessedTranscripts': 'transcript',
        }
        for list_key, prefix in LIST_TO_PREFIX.items():
            v = out.get(list_key)
            if isinstance(v, list):
                out[list_key] = [
                    x for x in v
                    if not (isinstance(x, dict) and
                            f"{prefix}:{x.get('id', '')}" in hidden)
                ]
    return out


# ── Shared design-system chrome injector ───────────────────
_DS_LINK = '<link rel="stylesheet" href="/static/design-system.css">'
_TOPNAV_CACHE = {'mtime': 0, 'template': ''}
_STRINGS_CACHE = {'mtime': 0, 'flat': {}}
_STRINGS_PATH       = Path(__file__).parent.parent / 'config' / 'strings.yaml'
_BUCKETS_PATH       = Path(__file__).parent.parent / 'config' / 'deal_buckets.json'
_bucket_cfg_cache   = {'mtime': 0, 'cfg': None}

def _load_bucket_cfg() -> dict:
    try:
        mtime = _BUCKETS_PATH.stat().st_mtime
        if mtime != _bucket_cfg_cache['mtime'] or _bucket_cfg_cache['cfg'] is None:
            _bucket_cfg_cache['cfg']   = json.loads(_BUCKETS_PATH.read_text())
            _bucket_cfg_cache['mtime'] = mtime
    except Exception:
        pass
    # Fallback when deal_buckets.json is missing/unreadable. default_owner
    # defaults to the principal first-name from firm_context (titlecased),
    # so a fresh-tenant install assigns unowned items to its own principal
    # rather than the maintainer.
    _principal_default = (_PRINCIPAL_FIRST_LOWER or 'principal').title()
    return _bucket_cfg_cache['cfg'] or {
        'rules': [], 'default_bucket': 'General / Other',
        'default_owner': _principal_default, 'owner_prefixes': [],
    }

def _infer_bucket(fu: dict) -> str:
    cfg = _load_bucket_cfg()
    who = fu.get('who', '')
    what = fu.get('what', '')
    txt = (who + ' ' + what).lower()
    workstream = fu.get('workstream', '')
    for rule in cfg.get('rules', []):
        if (any(kw in txt for kw in rule.get('keywords', []))
                or (rule.get('workstream') and workstream == rule['workstream'])):
            return rule['bucket']
    return cfg.get('default_bucket', 'General / Other')

def _infer_owner(who_raw: str, what: str) -> str:
    cfg = _load_bucket_cfg()
    what_lc = (what or '').lower()
    for op in cfg.get('owner_prefixes', []):
        if who_raw.lower().startswith(op['prefix']) or op.get('tag', '') in what_lc:
            return op['owner']
    return cfg.get('default_owner', (_PRINCIPAL_FIRST_LOWER or 'principal').title())

def _flatten_strings(obj, prefix=''):
    """Flatten nested dict to {dot.path: str} for {{STR:...}} substitution."""
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f'{prefix}.{k}' if prefix else str(k)
            out.update(_flatten_strings(v, key))
    elif isinstance(obj, (str, int, float)) or obj is None:
        out[prefix] = '' if obj is None else str(obj)
    return out

def _load_strings():
    """Load config/strings.yaml once (mtime-cached) and return a flat
    dict of dot.path -> string. Missing file yields an empty dict, which
    causes {{STR:...}} placeholders to fall through untouched."""
    try:
        st = _STRINGS_PATH.stat()
        if st.st_mtime != _STRINGS_CACHE['mtime']:
            try:
                import yaml as _yaml
                raw = _yaml.safe_load(_STRINGS_PATH.read_text()) or {}
            except Exception:
                raw = {}
            _STRINGS_CACHE['flat']  = _flatten_strings(raw)
            _STRINGS_CACHE['mtime'] = st.st_mtime
    except FileNotFoundError:
        return {}
    return _STRINGS_CACHE['flat']

_STR_RE = None
def _apply_string_placeholders(html: str) -> str:
    """Replace every {{STR:dot.path}} in html with the string from
    config/strings.yaml. Unknown keys are left in place so they surface
    as visible bugs in QA rather than silently blanking."""
    global _STR_RE
    if '{{STR:' not in html:
        return html
    if _STR_RE is None:
        import re as _re
        _STR_RE = _re.compile(r'\{\{STR:([a-zA-Z0-9_.]+)\}\}')
    strings = _load_strings()
    def sub(m):
        key = m.group(1)
        return strings.get(key, m.group(0))
    return _STR_RE.sub(sub, html)

# _NAV_LABEL_OVERRIDES retired 2026-05-04. All tab labels now flow from
# dashboard-tiles.yaml :: tiles[].title with optional per-tenant
# firm_context.yaml :: tile_labels override. To shorten or relabel a tab
# for your tenant, set tile_labels in firm_context.yaml — never touch code.
_NAV_LABEL_OVERRIDES = {}

def _topnav_html(user: str = 'owner') -> str:
    """Build topnav with tabs filtered to what the user can access."""
    try:
        st = TOPNAV_PARTIAL.stat()
        if st.st_mtime != _TOPNAV_CACHE['mtime']:
            _TOPNAV_CACHE['template'] = TOPNAV_PARTIAL.read_text()
            _TOPNAV_CACHE['mtime']    = st.st_mtime
        template = _TOPNAV_CACHE['template']
    except FileNotFoundError:
        return ''

    tiles = _tiles_for(user)
    tabs = []
    route_labels = {}
    for t in tiles:
        tid = t.get('id', '')
        url = t.get('url', '')
        if not url:
            continue
        # Label source of truth: dashboard-tiles.yaml title (already merged
        # with firm_context.yaml :: tile_labels via _tiles_for). Falls back
        # to the override table only if a tile has no resolved title.
        label = _NAV_LABEL_OVERRIDES.get(tid) or t.get('title') or tid
        tabs.append(f'<a class="tc-topnav-tab" data-path="{url}" href="{url}">{label}</a>')
        # Build the route→label map the topnav JS uses for the breadcrumb-
        # style "current view" badge. Uppercased to match the existing CSS.
        route_labels[url] = (t.get('title') or tid).upper()
    # /all is hardcoded in the topnav JS (it's the meta-route, not a tile).
    route_labels.setdefault('/all',  'ALL VIEWS')
    route_labels.setdefault('/all/', 'ALL VIEWS')

    out = template.replace('{{NAV_TABS}}', '\n    '.join(tabs))
    out = out.replace('{{TOPNAV_ROUTE_LABELS_JSON}}', json.dumps(route_labels))
    return out

def _inject_shared_chrome(html: str, user: str = 'owner') -> str:
    """Ensure every served HTML page has:
       (a) the design-system.css link in <head>, and
       (b) the shared topnav rendered right after <body>.

    Templates can opt-in explicitly via {{TOPNAV}} — otherwise we insert
    the nav automatically after the opening <body> tag. Pages that do not
    want shared chrome can include the marker <!-- NO_TC_CHROME --> in
    their head, which we honor.
    """
    if not html or '<!-- NO_TC_CHROME -->' in html:
        return html

    nav = _topnav_html(user)

    # Inject CSS link if not already present (check for the actual <link>,
    # not just the filename — templates often mention it in comments).
    if 'href="/static/design-system.css"' not in html:
        if '</head>' in html:
            html = html.replace('</head>', '  ' + _DS_LINK + '\n</head>', 1)
        else:
            html = _DS_LINK + '\n' + html

    # Inject or expand the topnav.
    if '{{TOPNAV}}' in html:
        html = html.replace('{{TOPNAV}}', nav, 1)
    elif nav and 'class="tc-topnav"' not in html:
        # Insert right after opening <body ...>.
        import re as _re
        m = _re.search(r'<body[^>]*>', html, flags=_re.IGNORECASE)
        if m:
            idx = m.end()
            html = html[:idx] + '\n' + nav + '\n' + html[idx:]

    # Drawer-back breadcrumb shim — injected before </body>, activates
    # itself only on /tomac-cove/?deal=... pages.
    if '</body>' in html and 'id="tc-drawer-back"' not in html:
        html = html.replace('</body>', _DRAWER_BACK_SHIM + '</body>', 1)

    # User-state bridge: tombstones + item id helper. Loaded on every page so
    # delete-button handlers and render filters can reach it via window.*
    # 2026-05-05: bugfix — the prior guard `'window.__DELETIONS__' not in html`
    # always tripped because the template's JS body REFERENCES window.__DELETIONS__
    # as a consumer ("if (typeof window.__DELETIONS__ !== 'undefined')"), even
    # though no setter exists. As a result, the injection script — which sets
    # __DEAL_CONFIG__ / __TOMAC_CONFIG__ / __TOPICS_INITIAL__ / __FIRM_CONTEXT__
    # / __DELETIONS__ / etc. — was being skipped on every render, leaving
    # fundraising panel + team actions empty and tombstones inactive. Switch
    # to the unique setter-form fingerprint so consumer refs no longer match.
    if 'window.__DELETIONS__ = new Set' not in html:
        script = _deletions_script(user)
        if '</head>' in html:
            html = html.replace('</head>', script + '\n</head>', 1)
        else:
            html = script + '\n' + html

    # config/strings.yaml substitution — every {{STR:dot.path}} resolves
    # from the single source of truth so we don't maintain duplicate button
    # labels and tooltips across templates.
    html = _apply_string_placeholders(html)

    return html


def _page_fetched_at() -> str:
    """Return the fetchedAt ISO string from the current compiled state, or ''."""
    try:
        return json.loads(STATE_PATH.read_text()).get('fetchedAt', '') or ''
    except Exception:
        return ''


def _deletions_script(user: str = 'owner') -> str:
    """Inline <script> that exposes window.__DELETIONS__, window.__itemId,
    window.__isDeleted, window.__deleteItem, plus window.__FIRM_CONTEXT__
    and window.__USER_ROLE__ for templates that need tenant identity.

    Client-side computes stable IDs as djb2(source + '|' + content[:60].trim())
    so the same upstream item resolves to the same ID across syncs."""
    try:
        ids = _deleted_ids()
    except Exception:
        ids = []
    try:
        topics_initial = _load_topics()
    except Exception:
        topics_initial = {'content': '', 'updated_at': ''}
    try:
        order_initial = _load_order()
    except Exception:
        order_initial = {}
    try:
        build_backlog_initial = _load_build_backlog()
    except Exception:
        build_backlog_initial = {'schema_version': 1, 'items': []}
    try:
        personal_items_initial = _load_personal_items()
    except Exception:
        personal_items_initial = []
    try:
        recruit_config = _load_recruit_config()
    except Exception:
        recruit_config = {'priorityTargets': {'inDiscussion': [], 'waitingToHear': [], 'doIChase': []}, 'recruiters': []}
    try:
        deal_config = _load_deal_config()
    except Exception:
        deal_config = {'liveDeals': [], 'dealOrigination': [], 'capitalRaisingAdvisors': [], 'prospectiveInvestors': []}
    try:
        fsh_initial = _load_file_system_health()
    except Exception:
        fsh_initial = {'status': 'no_data',
                       'counts': {'invariantViolations': 0, 'awaitingClassification': 0,
                                  'staleInbox': 0, 'gasIssues': 0},
                       'details': {'invariantViolations': [], 'awaitingClassification': [],
                                   'staleInbox': [], 'gasIssues': []},
                       'lastSheetUpdate': None}
    try:
        cp_aliases = _fc_srv.cp_aliases(_FC_CTX) if _fc_srv else []
    except Exception:
        cp_aliases = []
    try:
        with open(USER_STATE_DIR / 'topics_suggested.json') as _f:
            topics_suggested = json.load(_f)
    except FileNotFoundError:
        topics_suggested = {}
    except Exception:
        topics_suggested = {}
    firm_ctx_public = _firm_context_public(_FC_CTX)
    return (
        '<script>'
        '(function(){'
        'window.__TOPICS_INITIAL__ = ' + json.dumps(topics_initial) + ';'
        'window.__TOPICS_SUGGESTED__ = ' + json.dumps(topics_suggested) + ';'
        'window.__ORDER_INITIAL__ = ' + json.dumps(order_initial) + ';'
        'window.__BUILD_BACKLOG_INITIAL__ = ' + json.dumps(build_backlog_initial) + ';'
        'window.__PERSONAL_ITEMS_INITIAL__ = ' + json.dumps(personal_items_initial) + ';'
        'window.__RECRUIT_CONFIG__ = ' + json.dumps(recruit_config) + ';'
        # Canonical name — JS bundles should read window.__DEAL_CONFIG__.
        'window.__DEAL_CONFIG__ = ' + json.dumps(deal_config) + ';'
        # Pre-rename back-compat: legacy React bundle reads
        # window.__<SLUG_UPPER>_CONFIG__ where <slug> is tenant_slug. Built
        # at runtime so this module never carries a literal slug. Remove once
        # all bundles in ~/dashboards/app/templates/* are rebuilt against
        # __DEAL_CONFIG__.
        f'window.__{COS_TENANT_SLUG.upper()}_CONFIG__ = window.__DEAL_CONFIG__;'
        # Firm identity (principal name, team, firm name) — single source for
        # tenant-personalized strings in templates. See _firm_context_public()
        # for the schema (counterparty_aliases / draft_voice are kept server-side).
        'window.__FIRM_CONTEXT__ = ' + json.dumps(firm_ctx_public) + ';'
        'window.__USER_ROLE__ = ' + json.dumps(user) + ';'
        # File system health: hydrated from data/compiled/file-system-health.json
        # (produced by routines/compile/file_system_health.py).
        'window.__FSH_INITIAL__ = ' + json.dumps(fsh_initial) + ';'
        'window.__CP_ALIASES__ = ' + json.dumps(cp_aliases) + ';'
        'window.__PAGE_FETCHED_AT__ = ' + json.dumps(_page_fetched_at()) + ';'
        'window.__DELETIONS__ = new Set(' + json.dumps(ids) + ');'
        'window.__itemId = function(source, content){'
        '  var s = String(source||"") + "|" + String(content||"").slice(0,60).trim();'
        '  var h = 5381|0;'
        '  for (var i=0;i<s.length;i++){ h = (((h<<5) + h) + s.charCodeAt(i))|0; }'
        '  return (h>>>0).toString(16);'
        '};'
        'window.__isDeleted = function(source, content){'
        '  try { return window.__DELETIONS__.has(window.__itemId(source, content)); }'
        '  catch(e){ return false; }'
        '};'
        'window.__deleteItem = function(source, content, ctx){'
        '  var id = window.__itemId(source, content);'
        '  window.__DELETIONS__.add(id);'
        '  return fetch("/item/delete", {method:"POST", headers:{"Content-Type":"application/json"},'
        '    body: JSON.stringify({id:id, source:source, context:String(ctx||content||"").slice(0,200)})'
        '  }).catch(function(){});'
        '};'
        '})();'
        '</script>'
    )

# Small overlay script shown only on /<slug>-cove/?deal=... — renders a
# floating "← Back" link so users returning from /, /deals/ or /briefing/
# can get back to their prior dashboard without hitting browser back.
# Lives here (not in the React bundle) so we don't need to rebuild the
# pre-compiled React artifact. The legacy route segment is built from
# tenant_slug so this module never carries a literal tenant string.
_DRAWER_BACK_PATH = f'/{COS_TENANT_SLUG}-cove'
_DRAWER_BACK_SHIM = (
'''
<script>
(function () {
  if (!/^\\''' + _DRAWER_BACK_PATH + '''\\/?$/.test(location.pathname)) return;
  var qs = new URLSearchParams(location.search);
  if (!qs.get('deal')) return;
  var ref = document.referrer || '';
  var label = 'All Views', href = '/all';
  try {
    var u = new URL(ref);
    if (u.host === location.host) {
      if (u.pathname.indexOf('/deals') === 0)       { label = 'Deal Pipeline'; href = '/deals/'; }
      else if (u.pathname.indexOf('/briefing') === 0){ label = 'Briefing';      href = '/briefing/'; }
      else if (u.pathname === '/')                   { label = 'Status';        href = '/'; }
      else if (u.pathname.indexOf('/all') === 0)     { label = 'All Views';     href = '/all'; }
    }
  } catch (e) {}
  function mount() {
    if (document.getElementById('tc-drawer-back')) return;
    var a = document.createElement('a');
    a.id = 'tc-drawer-back';
    a.href = href;
    a.textContent = '\u2190 Back to ' + label;
    a.setAttribute('style',
      'position:fixed;top:68px;left:22px;z-index:9999;'
      + 'font-family:Courier Prime,Courier New,monospace;'
      + 'font-size:11px;font-weight:700;letter-spacing:.12em;'
      + 'text-transform:uppercase;color:#9A6B1E;'
      + 'background:#FBF4E6;border:1px solid #E2C98A;border-radius:2px;'
      + 'padding:5px 10px;text-decoration:none;');
    document.body.appendChild(a);
  }
  if (document.body) mount();
  else document.addEventListener('DOMContentLoaded', mount);
})();
</script>
'''
)

# ── Claude CLI — resolve latest installed version ───────────
def _find_claude_bin():
    pattern = str(Path.home() / 'Library/Application Support/Claude/claude-code/*/claude.app/Contents/MacOS/claude')
    candidates = sorted(glob.glob(pattern))
    return candidates[-1] if candidates else None

CLAUDE_BIN    = _find_claude_bin()
PIPELINE_LOG  = '/tmp/cos-pipeline-manual-run.log'
SKILL_MD_PATH = Path.home() / '.claude/scheduled-tasks/inbox-capture/SKILL.md'
CWD_CLAUDE    = str(Path.home() / 'Documents/Claude Code')

# ── Locks ──────────────────────────────────────────────────
import time
_briefing_cache = {}   # { 'calendar' | 'followups' : (fetched_ts, payload) }
_refresh_lock  = threading.Lock()   # serialise /refresh (HTML inject)
_warmup_lock   = threading.Lock()   # prevent concurrent background fetches
_compile_lock  = threading.Lock()   # prevent concurrent deal compiles
_otter_lock    = threading.Lock()   # prevent concurrent Otter backfill runs
_state_lock    = threading.Lock()   # protect dashboard-data.json read-modify-write in /patch

# ── Pipeline run state (in-memory) ─────────────────────────
_pipeline_lock          = threading.Lock()
_pipeline_running       = False
_pipeline_started_at    = None   # ISO string
_pipeline_completed_at  = None   # ISO string

# ── SSE subscriber registry ────────────────────────────────
_sse_lock    = threading.Lock()
_sse_queues: list = []

# ── TCIP onboarding job state ───────────────────────────────────────────────
_tcip_lock      = threading.Lock()
_tcip_running   = False
_tcip_lines: list = []          # accumulated output lines for late-joining SSE clients
_tcip_exit_code = None          # None = not yet finished
_tcip_started_at = None
_tcip_sse_lock  = threading.Lock()
_tcip_sse_queues: list = []

TCIP_SCRIPT = Path(__file__).parent.parent / 'cos-pipeline' / 'tools' / 'tcip_new_deal.py'
# Resolve relative to this file's actual location
_here_dir = Path(__file__).resolve().parent
TCIP_SCRIPT = (_here_dir / 'tools' / 'tcip_new_deal.py').resolve()

def _tcip_broadcast(line: str):
    """Push one output line to all connected TCIP SSE clients."""
    with _tcip_sse_lock:
        dead = []
        for q in _tcip_sse_queues:
            try:
                q.put_nowait(line)
            except Exception:
                dead.append(q)
        for q in dead:
            _tcip_sse_queues.remove(q)

def _broadcast_refresh():
    """Notify all connected SSE clients that new data is ready."""
    with _sse_lock:
        dead = []
        for q in _sse_queues:
            try:
                q.put_nowait('refresh')
            except queue.Full:
                dead.append(q)
        for q in dead:
            _sse_queues.remove(q)

# ── Background warmup ──────────────────────────────────────
def _run_fetch():
    """Run cos-dashboard-fetch.py (blocks until done). Holds _warmup_lock."""
    if not _warmup_lock.acquire(blocking=False):
        print('[warmup] already running — skipping', flush=True)
        return
    try:
        print('[warmup] starting background fetch...', flush=True)
        result = subprocess.run(
            [sys.executable, FETCH_SCRIPT],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            print(f'[warmup] done — {result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "ok"}', flush=True)
            _broadcast_refresh()   # push SSE notification to any open dashboard tabs
        else:
            print(f'[warmup] fetch failed: {result.stderr[:200]}', flush=True)
    except subprocess.TimeoutExpired:
        print('[warmup] fetch timed out after 120s', flush=True)
    except Exception as e:
        print(f'[warmup] error: {e}', flush=True)
    finally:
        _warmup_lock.release()

def _run_compile():
    """Run deal-system-compile.py (compile Deals/*.md + Excel → JSON → HTML inject).
    Fast (~3-5s, all local — no Google APIs). Holds _compile_lock to prevent overlap.
    Called in parallel with _run_fetch() on every warmup so deal data is always fresh.
    """
    if not _compile_lock.acquire(blocking=False):
        print('[compile] already running — skipping', flush=True)
        return
    try:
        print('[compile] starting deal system compile...', flush=True)
        result = subprocess.run(
            [sys.executable, COMPILE_SCRIPT],
            capture_output=True, text=True, timeout=60,
        )
        last_line = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ''
        if result.returncode == 0:
            print(f'[compile] done — {last_line}', flush=True)
        else:
            print(f'[compile] failed (exit {result.returncode}): {result.stderr[:200]}', flush=True)
    except subprocess.TimeoutExpired:
        print('[compile] timed out after 60s', flush=True)
    except Exception as e:
        print(f'[compile] error: {e}', flush=True)
    finally:
        _compile_lock.release()


def _run_otter():
    """Run cos_otter_backfill.py — scans Otter Drive folders, processes any new
    transcripts via Claude, writes action items to the Follow-ups Google Doc.
    Idempotent: skips already-processed files via the dedup tracker.
    Fast (~2s) when nothing is new; up to ~5 min when new transcripts are waiting.
    Must complete before _run_fetch() in the Pull Fresh Data chain so new action
    items land in the Follow-ups Doc before the fetch reads it.
    """
    if not _otter_lock.acquire(blocking=False):
        print('[otter] already running — skipping', flush=True)
        return
    try:
        print('[otter] scanning for new transcripts...', flush=True)
        result = subprocess.run(
            [sys.executable, OTTER_SCRIPT],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode == 0:
            last = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else 'ok'
            print(f'[otter] done — {last}', flush=True)
        else:
            print(f'[otter] failed (exit {result.returncode}): {result.stderr[:300]}', flush=True)
    except subprocess.TimeoutExpired:
        print('[otter] timed out after 300s', flush=True)
    except Exception as e:
        print(f'[otter] error: {e}', flush=True)
    finally:
        _otter_lock.release()


_CORRECTIONS_QUEUE = _ROOT / 'data' / 'corrections-queue.json'
_corrections_prune_lock = threading.Lock()

def _prune_corrections_queue():
    """Remove resolved (non-pending) entries from corrections-queue.json.
    Runs inline on every warmup cycle — pure file I/O, sub-millisecond.
    Safe to call concurrently: guarded by _corrections_prune_lock.
    """
    if not _corrections_prune_lock.acquire(blocking=False):
        return
    try:
        if not _CORRECTIONS_QUEUE.exists():
            return
        items = json.loads(_CORRECTIONS_QUEUE.read_text() or '[]')
        pending = [i for i in items if i.get('status') == 'pending']
        pruned = len(items) - len(pending)
        if pruned:
            _CORRECTIONS_QUEUE.write_text(json.dumps(pending, indent=2))
            print(f'[corrections] pruned {pruned} resolved ({len(pending)} pending remain)', flush=True)
    except Exception as e:
        print(f'[corrections] prune error: {e}', flush=True)
    finally:
        _corrections_prune_lock.release()


_resolver_lock = threading.Lock()

def _run_email_resolver(force: bool = False):
    """Run cos_email_resolver.py — scans sent mail, inbox, calendar, and draft status
    to detect completed action items. Writes email-resolutions.json which the fetch
    script reads to mark items [RESOLVED]. Holds _resolver_lock to prevent overlap.
    Fast when nothing is new (<2s with dedup); up to ~15s on first run.
    force=True bypasses the active-hours gate — used by the manual refresh button.
    """
    if not _resolver_lock.acquire(blocking=False):
        print('[resolver] already running — skipping', flush=True)
        return
    try:
        print('[resolver] scanning for completed actions...', flush=True)
        cmd = [sys.executable, RESOLVER_SCRIPT]
        if force:
            cmd.append('--force')
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            last = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else 'ok'
            print(f'[resolver] done — {last}', flush=True)
        else:
            print(f'[resolver] failed (exit {result.returncode}): {result.stderr[:300]}', flush=True)
    except subprocess.TimeoutExpired:
        print('[resolver] timed out after 60s', flush=True)
    except Exception as e:
        print(f'[resolver] error: {e}', flush=True)
    finally:
        _resolver_lock.release()


_sweep_lock = threading.Lock()

def _run_sweep():
    """Run _resolved_row_sweep.py on the freshly-compiled dashboard-data.json.
    Pure Python, no network calls — completes in <100ms even on large datasets.
    Must run AFTER _run_fetch() so it operates on fresh data, not stale cache.
    Holds _sweep_lock to prevent concurrent runs if warmup fires close together.
    """
    if not _sweep_lock.acquire(blocking=False):
        print('[sweep] already running — skipping', flush=True)
        return
    try:
        result = subprocess.run(
            [sys.executable, SWEEP_SCRIPT],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            # Sweep reports a JSON summary on stderr
            try:
                rpt = json.loads(result.stderr.strip()) if result.stderr.strip() else {}
                removed = (rpt.get('followUps_removed', 0) + rpt.get('awaitingExternal_removed', 0) +
                           rpt.get('originationInbox_archived', 0) + rpt.get('dealIntel_archived', 0))
                if removed:
                    print(f'[sweep] done — {removed} item(s) removed/archived', flush=True)
            except Exception:
                pass
        else:
            print(f'[sweep] failed (exit {result.returncode}): {result.stderr[:200]}', flush=True)
    except subprocess.TimeoutExpired:
        print('[sweep] timed out after 30s', flush=True)
    except Exception as e:
        print(f'[sweep] error: {e}', flush=True)
    finally:
        _sweep_lock.release()


_FSH_COMPILE_SCRIPT = Path.home() / 'dashboards' / 'routines' / 'compile' / 'file_system_health.py'


def _run_file_system_health():
    """Run routines/compile/file_system_health.py to refresh the FSH compiled JSON.
    Fast (<1s when sheet missing; <3s when sheet present). Drive-only call, no LLM.
    Safe to skip on failure — the dashboard renders an empty-state tile group."""
    if not _FSH_COMPILE_SCRIPT.exists():
        return
    try:
        result = subprocess.run(
            [sys.executable, str(_FSH_COMPILE_SCRIPT)],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode != 0:
            print(f'[fsh] failed (exit {result.returncode}): {result.stderr[:200]}', flush=True)
    except subprocess.TimeoutExpired:
        print('[fsh] timed out', flush=True)
    except Exception as e:
        print(f'[fsh] error: {e}', flush=True)


def _run_alias_sync():
    """Run cos_alias_sync.py — detect new counterparties in originationInbox and
    append them to counterparty_aliases_auto.json in the config dir.
    Fast (<1s when no new aliases). Must run after _run_fetch() so it sees the
    freshly-compiled originationInbox data.
    """
    if not Path(ALIAS_SYNC_SCRIPT).exists():
        return
    try:
        result = subprocess.run(
            [sys.executable, ALIAS_SYNC_SCRIPT],
            capture_output=True, text=True, timeout=15,
        )
        out = (result.stdout or "").strip()
        if out:
            print(f'[alias-sync] {out.splitlines()[-1]}', flush=True)
        if result.returncode != 0:
            print(f'[alias-sync] failed: {result.stderr[:200]}', flush=True)
    except subprocess.TimeoutExpired:
        print('[alias-sync] timed out', flush=True)
    except Exception as e:
        print(f'[alias-sync] error: {e}', flush=True)


def _warmup_in_background():
    """Warmup sequence on every cycle:
      1. Prune corrections-queue (inline, <1ms)
      2. compile + (resolver → fetch → sweep → alias-sync) run in parallel threads.
         Resolver runs before fetch so same-cycle resolutions land in the data.
         Sweep runs after fetch on the freshly-compiled JSON — pure local I/O,
         no network, <100ms. All three complete well before the next /refresh.
      3. After both threads complete, broadcast SSE refresh to all open browser tabs.
    """
    _prune_corrections_queue()

    def _resolver_then_fetch():
        _run_email_resolver()
        _run_fetch()
        _run_sweep()       # prune resolved/stale items from fresh dashboard-data.json
        _run_alias_sync()  # detect new originationInbox counterparties → add aliases
        _run_file_system_health()  # refresh data/compiled/file-system-health.json

    t_chain   = threading.Thread(target=_resolver_then_fetch, daemon=True, name='warmup-chain')
    t_compile = threading.Thread(target=_run_compile,          daemon=True, name='warmup-compile')
    t_chain.start()
    t_compile.start()

    def _wait_and_broadcast():
        t_chain.join(timeout=130)
        t_compile.join(timeout=70)
        _broadcast_refresh()

    threading.Thread(target=_wait_and_broadcast, daemon=True, name='warmup-broadcast').start()
    return t_chain

def _auto_warmup_loop():
    """Background loop: warm cache immediately on startup, then every N minutes.
    Each cycle refreshes data and broadcasts to open browser tabs.

    NOTE: _run_email_resolver() is intentionally EXCLUDED from this loop.
    The resolver calls the Anthropic API (raw, billed) and running it every
    10 min during business hours costs ~$6/day.  It runs via launchd at 7:30am
    and 6pm, and on explicit POST /warmup triggers (post-capture-pipeline,
    post-briefing).  Auto-warmup only needs data freshness, not re-resolution.
    """
    # Initial warmup — delay 5s to let server finish starting
    time.sleep(5)
    _run_fetch()
    # Recurring warmup — data refresh + SSE broadcast only (no resolver)
    while True:
        time.sleep(WARMUP_INTERVAL_MIN * 60)
        # Lightweight: fetch → sweep → broadcast (no resolver)
        _run_fetch()
        _run_sweep()
        _broadcast_refresh()

# ── Routines (Claude scheduled-task health) ────────────────
# Reads ~/Library/LaunchAgents/com.yoni.claude-task.*.plist + per-task
# logs at ~/dashboards/logs/claude-tasks/<task>.{stdout,stderr,run}.log.
# Wrapper at ~/dashboards/scripts/run-claude-task.sh writes deterministic
# BEGIN ... END banners we parse to extract per-run history.
ROUTINES_LAUNCHAGENTS = Path.home() / 'Library' / 'LaunchAgents'
ROUTINES_LOG_DIR      = Path.home() / 'dashboards' / 'logs' / 'claude-tasks'
ROUTINES_SKILL_DIR    = Path.home() / '.claude' / 'scheduled-tasks'
# LaunchAgent label prefix — matches the .plist files actually installed at
# ~/Library/LaunchAgents. Default carries the maintainer prefix (legacy
# install); subscribers override via COS_LAUNCHAGENT_PREFIX env to point at
# their own labels (e.g. 'com.acme.claude-task.').
ROUTINES_LABEL_PREFIX = os.environ.get(
    'COS_LAUNCHAGENT_PREFIX',
    'com.' + (_PRINCIPAL_FIRST_LOWER or 'cos') + '.claude-task.'
)
# Tier 2: depend on Claude_in_Chrome MCP (gone since desktop-app uninstall).
# Their failures are expected, not actionable, until that MCP is restored.
ROUTINES_TIER2 = {
    'rbn-energy-daily',
    'substack-sync',
    'gs-research-fetch',
    'daily-intelligence-digest',
    'weekly-intelligence-digest',
}
# Human-readable labels for the admin routines tab.
# Keys are the plist slug (after stripping ROUTINES_LABEL_PREFIX + .plist).
# Unlisted slugs fall back to the raw slug (title-cased dashes → spaces).
ROUTINE_LABELS: dict[str, str] = {
    'morning-briefing':          'Morning Briefing',
    'inbox-capture':             'Inbox Capture',
    'rbn-energy-daily':          'RBN Energy Daily',
    'substack-sync':             'Substack Sync',
    'daily-intelligence-digest': 'Daily Intelligence Digest',
    'weekly-intelligence-digest': 'Weekly Intelligence Digest',
    'podcast-processing':        'Podcast Processing',
    'gs-research-fetch':         'GS Research — Fetch',
    'gs-research-process':       'GS Research — Process',
    'jefferies-research-fetch':  'Jefferies Research — Fetch',
    'jefferies-research-process': 'Jefferies Research — Process',
    'peakload-weekly':           'PeakLoad Weekly',
    'weekly-summary-email':      'Weekly Summary Email',
    'deal-pipeline-scan':        'Deal Pipeline Scan',
    'deal-dashboard-compile':    'Deal Dashboard Compile',
    'phone-notes-capture':       'Phone Notes Capture',
}

# Strict task-name validator: lowercase alnum + dashes, 2-65 chars.
# Used as a shell-injection barrier on /routines/<task>/kickstart.
ROUTINES_TASK_RE = re.compile(r'^[a-z0-9][a-z0-9-]{1,64}$')

# Wrapper banner regexes. Wrapper writes (verbatim):
#   ============================================================
#   [YYYY-MM-DD HH:MM:SS TZ] BEGIN <task>
#     skill: <path>
#     pid:   <int>
#   ============================================================
#   <claude output, may contain blank lines>
#   ============================================================
#   [YYYY-MM-DD HH:MM:SS TZ] END <task>
#     exit:    <int>
#     elapsed: <int>s
#   ============================================================
_RT_BEGIN_RE = re.compile(r'^\[([^\]]+)\]\s+BEGIN\s+(\S+)\s*$')
_RT_END_RE   = re.compile(r'^\[([^\]]+)\]\s+END\s+(\S+)\s*$')
_RT_EXIT_RE  = re.compile(r'^\s*exit:\s*(-?\d+)\s*$')
_RT_ELAP_RE  = re.compile(r'^\s*elapsed:\s*(\d+)s\s*$')


def _routines_list_tasks():
    """List task names from plists on disk, sorted alphabetically."""
    out = []
    if not ROUTINES_LAUNCHAGENTS.exists():
        return out
    for p in sorted(ROUTINES_LAUNCHAGENTS.glob(f'{ROUTINES_LABEL_PREFIX}*.plist')):
        name = p.name[len(ROUTINES_LABEL_PREFIX):-len('.plist')]
        if ROUTINES_TASK_RE.match(name):
            out.append(name)
    return out


def _launchctl_loaded_labels():
    """Return set of LaunchAgent labels currently loaded into launchctl.
    Used to detect plists that exist on disk but were never bootstrapped
    (rule 5a — missing-plist alarm). The catch-up agent loads these on
    first run, so a missing label here is mostly defensive — but flagging
    it surfaces install-time and reboot-time gaps that would otherwise
    silently drop a routine until the next catch-up cycle."""
    try:
        r = subprocess.run(
            ['launchctl', 'list'],
            capture_output=True, text=True, timeout=4,
        )
        if r.returncode != 0:
            return None  # signal "unknown" rather than "empty"
        labels = set()
        for ln in (r.stdout or '').splitlines()[1:]:  # skip header row
            parts = ln.split('\t')
            if len(parts) >= 3:
                labels.add(parts[2].strip())
        return labels
    except Exception:
        return None


def _routines_human_schedule(intervals):
    """Convert plist StartCalendarInterval payload to a short label."""
    if isinstance(intervals, dict):
        intervals = [intervals]
    if not intervals:
        return '(no schedule)'
    days_set = {e.get('Weekday') for e in intervals if 'Weekday' in e}
    days = sorted(days_set) if days_set else []
    times = sorted({(int(e.get('Hour', 0)), int(e.get('Minute', 0)))
                    for e in intervals})
    # Day-pattern detection
    if not days:
        day_label = 'daily'
    elif days == [1, 2, 3, 4, 5]:
        day_label = 'M-F'
    elif days == [2, 3, 4, 5]:
        day_label = 'Tue-Fri'
    elif days == [0]:
        day_label = 'Sun'
    elif days == [1]:
        day_label = 'Mon'
    else:
        names = {0: 'Sun', 1: 'Mon', 2: 'Tue', 3: 'Wed',
                 4: 'Thu', 5: 'Fri', 6: 'Sat'}
        day_label = '/'.join(names.get(d, str(d)) for d in days)
    if len(times) == 1:
        h, m = times[0]
        return f'{h:02d}:{m:02d} {day_label}'
    # Multi-time same minute (cos-gmail-mini hourly pattern)
    hours = sorted({h for h, _ in times})
    minutes = {m for _, m in times}
    if len(minutes) == 1 and list(minutes)[0] == 0:
        return f'{day_label} ' + '/'.join(f'{h:02d}' for h in hours)
    return f'{day_label} ({len(times)} firings)'


def _routines_next_run(intervals, now=None):
    """Next firing wall-clock time from StartCalendarInterval entries.
    Returns ISO timestamp or None. Searches forward up to 14 days."""
    if now is None:
        now = datetime.now()
    if isinstance(intervals, dict):
        intervals = [intervals]
    if not intervals:
        return None
    candidates = []
    for offset in range(0, 15):
        d = now + timedelta(days=offset)
        # macOS launchd Weekday: 0=Sun..6=Sat. Python weekday(): 0=Mon..6=Sun.
        py_wd  = d.weekday()
        mac_wd = (py_wd + 1) % 7
        for entry in intervals:
            wd = entry.get('Weekday')
            if wd is not None and int(wd) != mac_wd:
                continue
            h = int(entry.get('Hour', 0))
            m = int(entry.get('Minute', 0))
            cand = d.replace(hour=h, minute=m, second=0, microsecond=0)
            if cand > now:
                candidates.append(cand)
    if not candidates:
        return None
    return min(candidates).isoformat(timespec='seconds')


def _routines_parse_plist(name):
    """Read a single task's plist; return schedule info + log-stem.

    `log_stem`: basename of `StandardOutPath` minus the `.stdout.log` /
    `.stderr.log` suffix, when present. Many plists historically routed
    stdout/stderr to filenames that don't match the plist label
    (e.g. label `morning-briefing` → `cos-personal-briefing.stdout.log`),
    so the routines registry has to consult the plist to find the logs
    that were actually written. Falls back to `name` when no usable
    StandardOutPath is set.
    """
    p = ROUTINES_LAUNCHAGENTS / f'{ROUTINES_LABEL_PREFIX}{name}.plist'
    out = {'schedule_human': '(unknown)', 'next_run': None, 'intervals': [],
           'log_stem': name}
    if not p.exists():
        return out
    try:
        with p.open('rb') as fh:
            d = plistlib.load(fh)
    except Exception:
        return out
    intervals = d.get('StartCalendarInterval', [])
    if isinstance(intervals, dict):
        intervals = [intervals]
    out['intervals']      = intervals
    out['schedule_human'] = _routines_human_schedule(intervals)
    out['next_run']       = _routines_next_run(intervals)
    stdout_path = d.get('StandardOutPath') or ''
    if stdout_path:
        bn = Path(stdout_path).name
        for suffix in ('.stdout.log', '.run.log', '.stderr.log', '.log'):
            if bn.endswith(suffix):
                bn = bn[:-len(suffix)]
                break
        if bn:
            out['log_stem'] = bn
    return out


def _routines_tail_log(path, max_bytes=64 * 1024):
    """Read last max_bytes of a file. Returns text (utf-8, replace errors)."""
    if not path.exists():
        return ''
    try:
        size = path.stat().st_size
        with path.open('rb') as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()  # discard partial first line
            return fh.read().decode('utf-8', errors='replace')
    except Exception:
        return ''


def _routines_parse_runs(text, max_runs=20):
    """Parse BEGIN/END banner pairs from a wrapper log tail.
    Returns list newest-last of {start, end, exit_code, runtime_s, output_lines}."""
    runs = []
    lines = text.splitlines()
    in_run = None
    output = []
    for i, raw in enumerate(lines):
        ln = raw.strip()
        bm = _RT_BEGIN_RE.match(ln)
        em = _RT_END_RE.match(ln)
        if bm:
            in_run = {'start': bm.group(1), 'task': bm.group(2),
                      'exit_code': None, 'runtime_s': None,
                      'end': None, 'output_lines': []}
            output = []
        elif em and in_run is not None:
            in_run['end'] = em.group(1)
            for j in range(i + 1, min(i + 5, len(lines))):
                ex = _RT_EXIT_RE.match(lines[j])
                el = _RT_ELAP_RE.match(lines[j])
                if ex: in_run['exit_code'] = int(ex.group(1))
                if el: in_run['runtime_s'] = int(el.group(1))
            in_run['output_lines'] = output[-30:]
            runs.append(in_run)
            in_run = None
            output = []
        elif in_run is not None and not ln.startswith('==='):
            # Skip the BEGIN-block metadata lines (skill:, pid:)
            if ln.startswith('skill:') or ln.startswith('pid:'):
                continue
            output.append(raw)
    return runs[-max_runs:]


def _routines_status_for(name, last_run):
    """Status enum: ok | fail | expected_fail | running | never_run."""
    if last_run is None:
        return 'never_run'
    code = last_run.get('exit_code')
    if code is None:
        return 'running'
    if code == 0:
        return 'ok'
    return 'expected_fail' if name in ROUTINES_TIER2 else 'fail'


def _routines_data():
    """Build the GET /routines payload. List of per-task dicts, sorted by
    severity (failing tier-1 first)."""
    tasks = _routines_list_tasks()
    loaded_labels = _launchctl_loaded_labels()  # set | None (None = unknown)
    out = []
    for name in tasks:
        meta = _routines_parse_plist(name)
        # The wrapper script (run-claude-task.sh) writes BEGIN/END to
        # `<task>.run.log` using the canonical task name passed via plist
        # ProgramArguments — independent of StandardOutPath. So check that
        # first. Fall back to the stem-derived stdout/run logs to recover
        # history from before any plist rename.
        stem = meta.get('log_stem') or name
        canonical_run = ROUTINES_LOG_DIR / f'{name}.run.log'
        stem_run      = ROUTINES_LOG_DIR / f'{stem}.run.log'
        stem_stdout   = ROUTINES_LOG_DIR / f'{stem}.stdout.log'
        # Concatenate the candidate sources — _routines_parse_runs picks runs
        # in document order, so older history (stem files) comes first and
        # newer canonical runs land at the end where `runs[-1]` looks.
        text_parts = []
        for p in (stem_run, stem_stdout, canonical_run):
            t = _routines_tail_log(p)
            if t:
                text_parts.append(t)
        text = '\n'.join(text_parts)
        runs = _routines_parse_runs(text)
        # Drop duplicates that appear in both stem and canonical files.
        seen = set()
        unique_runs = []
        for r in runs:
            key = (r.get('start'), r.get('end'))
            if key in seen: continue
            seen.add(key)
            unique_runs.append(r)
        runs = unique_runs
        # Sort by start timestamp so latest is last.
        runs.sort(key=lambda r: r.get('start') or '')
        last = runs[-1] if runs else None
        history = [{'start': r.get('start'),
                    'exit_code': r.get('exit_code'),
                    'runtime_s': r.get('runtime_s')} for r in runs[-7:]]
        # Rule 5a (codified 2026-05-04): plist exists on disk but isn't in
        # `launchctl list` → surface as not_loaded. The catch-up agent
        # bootstraps these on first run, so this is mostly defensive
        # (catches install gaps + post-reboot drift before the next cycle).
        full_label = ROUTINES_LABEL_PREFIX + name
        if loaded_labels is None:
            loaded = None  # unknown — launchctl unavailable
        else:
            loaded = full_label in loaded_labels
        item = {
            'task':              name,
            'label':             ROUTINE_LABELS.get(name, name),
            'schedule_human':    meta['schedule_human'],
            'next_run':          meta['next_run'],
            'tier':              2 if name in ROUTINES_TIER2 else 1,
            'last_run':          last.get('end') if last else None,
            'last_start':        last.get('start') if last else None,
            'last_exit_code':    last.get('exit_code') if last else None,
            'last_runtime_s':    last.get('runtime_s') if last else None,
            'last_output_lines': last.get('output_lines') if last else [],
            'history':           history,
            'status':            _routines_status_for(name, last),
            'launchctl_loaded':  loaded,
            'skill_path':        str(ROUTINES_SKILL_DIR / name / 'SKILL.md'),
            'log_paths': {
                'stdout': str(ROUTINES_LOG_DIR / f'{stem}.stdout.log'),
                'stderr': str(ROUTINES_LOG_DIR / f'{stem}.stderr.log'),
                'run':    str(ROUTINES_LOG_DIR / f'{stem}.run.log'),
            },
        }
        if loaded is False:
            item['warning'] = (
                f'Plist on disk but not in launchctl list — run '
                f'`launchctl bootstrap gui/$(id -u) '
                f'~/Library/LaunchAgents/{full_label}.plist` or wait '
                f'for the catch-up agent to bootstrap it.'
            )

        # Stuck-failing detector (codified 2026-05-05): three-or-more
        # consecutive non-zero exits in the recent history is a real
        # alarm — the catch-up agent will keep retrying a routine that
        # can't succeed (e.g. API quota hit), burning quota and
        # generating noise. Surface a `stuck` flag + a one-line
        # diagnostic so the routines tab can render a distinct chip.
        stuck = False
        recent_codes = [r.get('exit_code') for r in history]
        if len(recent_codes) >= 3 and all(
            c is not None and c != 0 for c in recent_codes[-3:]
        ):
            stuck = True
        if stuck:
            # Mine the last 16KB of stdout for a one-line cause.
            cause = ''
            try:
                stdout_p = ROUTINES_LOG_DIR / f'{stem}.stdout.log'
                if stdout_p.exists():
                    sz = stdout_p.stat().st_size
                    with stdout_p.open('rb') as fh:
                        if sz > 16 * 1024:
                            fh.seek(sz - 16 * 1024)
                            fh.readline()
                        tail = fh.read().decode('utf-8', errors='replace')
                    ms = re.findall(
                        r'You have reached your specified API usage limits.{0,80}?regain access on (\d{4}-\d{2}-\d{2})',
                        tail,
                    )
                    if ms:
                        cause = (
                            f'API spend limit — regain access on {ms[-1]}'
                        )
                    elif re.search(
                        r'(invalid[_ -]?(api[_ -]?key|token)|'
                        r'expired[_ -]?token|oauth[_ -]?(expired|invalid)|'
                        r'401\b.*(unauthor|invalid[_ -]?token)|'
                        r'CLAUDE_CODE_OAUTH_TOKEN.*(expired|invalid))',
                        tail, re.IGNORECASE,
                    ):
                        cause = ('Claude OAuth token expired/invalid — '
                                 'regenerate via claude setup-token')
                    elif re.search(r'rate.?limit|429', tail, re.IGNORECASE):
                        cause = 'Rate-limit pattern in recent runs'
                    elif re.search(
                        r'(403.*Forbidden|invalid_grant|token_expired)',
                        tail,
                    ):
                        cause = 'Google OAuth 403 / invalid_grant — refresh tokens'
            except Exception:
                pass
            item['stuck'] = True
            item['stuck_cause'] = cause or 'Unknown — check stdout/stderr'
        out.append(item)
    rank = {'fail': 0, 'running': 1, 'never_run': 2, 'ok': 3, 'expected_fail': 4}
    out.sort(key=lambda x: (rank.get(x['status'], 5), x['task']))
    return out


def _routines_health():
    """Summary + last-24h failures for the digest section."""
    data = _routines_data()
    counts = {'ok': 0, 'fail': 0, 'expected_fail': 0,
              'never_run': 0, 'running': 0}
    for d in data:
        counts[d['status']] = counts.get(d['status'], 0) + 1
    now = datetime.now()
    runs_24h = ok_24h = failed_t1_24h = failed_t2_24h = 0
    failures = []
    for d in data:
        # _routines_data populates log_paths from the plist's actual stem;
        # reuse those rather than rebuilding from the task name (which would
        # miss the renamed-plist case).
        run_log = Path(d['log_paths']['run'])
        std_log = Path(d['log_paths']['stdout'])
        text = _routines_tail_log(run_log) or _routines_tail_log(std_log)
        for r in _routines_parse_runs(text, max_runs=50):
            try:
                ts = (r.get('start') or '').strip()
                # ts format: "YYYY-MM-DD HH:MM:SS TZ"
                parts = ts.split(' ')
                if len(parts) < 2:
                    continue
                dt = datetime.strptime(parts[0] + ' ' + parts[1],
                                        '%Y-%m-%d %H:%M:%S')
                age_h = (now - dt).total_seconds() / 3600.0
                if age_h < 0 or age_h > 24:
                    continue
                runs_24h += 1
                code = r.get('exit_code')
                if code == 0:
                    ok_24h += 1
                elif d['task'] in ROUTINES_TIER2:
                    failed_t2_24h += 1
                else:
                    failed_t1_24h += 1
                    failures.append({
                        'task':       d['task'],
                        'time':       ts,
                        'exit_code':  code,
                        'last_lines': r.get('output_lines', [])[-3:],
                    })
            except Exception:
                continue
    return {
        'total':         len(data),
        'ok':            counts.get('ok', 0),
        'failing':       counts.get('fail', 0),
        'expected_fail': counts.get('expected_fail', 0),
        'never_run':     counts.get('never_run', 0),
        'running':       counts.get('running', 0),
        'last_checked':  now.isoformat(timespec='seconds'),
        'last_24h': {
            'runs':       runs_24h,
            'ok':         ok_24h,
            'failed_t1':  failed_t1_24h,
            'failed_t2':  failed_t2_24h,
            'failures':   failures,
        },
    }


# ── Deal-tile manual overrides — file lock + path constant ────
# Override file lives under data/user-state/ so the upstream compile
# pipeline never overwrites it (Operating Principle #1). Single global
# lock protects the read-modify-write cycle of POST /deal/override.
_DEAL_OVERRIDES_PATH = _ROOT / 'data' / 'user-state' / 'deal-overrides.json'
_DEAL_OVERRIDES_LOCK = threading.Lock()


def _routines_kickstart(name):
    """launchctl kickstart -k gui/$UID/com.yoni.claude-task.<name>.
    Returns (ok: bool, output: str). Strict name validation enforced
    here AND at the route layer."""
    if not ROUTINES_TASK_RE.match(name):
        return False, 'invalid task name'
    plist = ROUTINES_LAUNCHAGENTS / f'{ROUTINES_LABEL_PREFIX}{name}.plist'
    if not plist.exists():
        return False, 'unknown task'
    label = f'{ROUTINES_LABEL_PREFIX}{name}'
    try:
        uid = os.getuid()
        result = subprocess.run(
            ['launchctl', 'kickstart', '-k', f'gui/{uid}/{label}'],
            capture_output=True, text=True, timeout=10,
        )
        return (result.returncode == 0,
                (result.stdout + result.stderr).strip() or 'kickstart issued')
    except subprocess.TimeoutExpired:
        return False, 'kickstart timed out'
    except Exception as e:
        return False, f'kickstart error: {e}'


# ── Batch jobs ─────────────────────────────────────────────
_BATCH_STATE_FILE = Path.home() / 'credentials' / 'pending_batches.json'
_PODCAST_RETRIEVE_SCRIPT = str(Path.home() / 'cos-pipeline' / 'podcast_transcribe.py')
_BATCH_RETRIEVE_RUNNING = threading.Lock()


def _batch_jobs_data() -> list[dict]:
    """Read pending_batches.json and return all unwritten batches with dashboard-friendly fields."""
    try:
        if not _BATCH_STATE_FILE.exists():
            return []
        state = json.loads(_BATCH_STATE_FILE.read_text(encoding='utf-8'))
    except Exception:
        return []
    out = []
    for b in state.get('batches', []):
        if b.get('results_written'):
            continue
        submitted = b.get('submitted_at', '')
        out.append({
            'batch_id':      b.get('batch_id', ''),
            'routine':       b.get('routine', ''),
            'status':        b.get('status', 'unknown'),
            'submitted_at':  submitted,
            'request_count': b.get('request_count', len(b.get('requests', []))),
            'requests':      [{'custom_id': r['custom_id'],
                               'title': r.get('metadata', {}).get('title', ''),
                               'show':  r.get('metadata', {}).get('show', '')}
                              for r in b.get('requests', [])],
        })
    # newest first
    out.sort(key=lambda x: x['submitted_at'], reverse=True)
    return out


def _batch_force_retrieve(batch_id: str) -> tuple[bool, str]:
    """Kick off podcast --retrieve-batches in background. Returns (ok, message)."""
    if not _BATCH_RETRIEVE_RUNNING.acquire(blocking=False):
        return False, 'Retrieve already in progress'
    def _run():
        try:
            subprocess.run(
                ['/opt/homebrew/bin/python3', _PODCAST_RETRIEVE_SCRIPT, '--retrieve-batches'],
                timeout=300,
            )
        except Exception:
            pass
        finally:
            _BATCH_RETRIEVE_RUNNING.release()
    threading.Thread(target=_run, daemon=True).start()
    return True, 'Retrieve started in background'


# ── Deal list helpers ───────────────────────────────────────

def _merge_registered_deals(pipeline_deals: list, registered_deals: list) -> list:
    """Return pipeline_deals filtered (no Auto ghosts) + any registered deals
    (from dealPortfolio.deals) that are absent from the live list.

    Uses first-token matching to catch variants like
    'DealX (Full Legal Name...)' vs 'DealX / Asset Name'.
    """
    import re as _re

    def _first_token(s: str) -> str:
        """Lowercase first significant word — used for fuzzy dedup."""
        return (_re.split(r'[\s/(]', s.strip())[0] or '').lower()

    # Step 1: filter Auto ghosts
    clean = [d for d in pipeline_deals if 'Auto' not in d.get('stage', '')]

    if not registered_deals:
        return clean

    existing_ids     = {d.get('id', '') for d in clean if d.get('id')}
    existing_names   = {(d.get('name') or '').lower() for d in clean}
    existing_tokens  = {_first_token(d.get('name', '')) for d in clean}

    for rd in registered_deals:
        rid    = rd.get('id', '')
        rnam   = (rd.get('name') or '').lower()
        rtok   = _first_token(rd.get('name', ''))
        if (rid not in existing_ids
                and rnam not in existing_names
                and (not rtok or rtok not in existing_tokens)):
            clean.append(rd)

    return clean


# ── Request handler ────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress default access log

    # ── auth helpers ───────────────────────────────────────
    def _authenticate(self):
        """Parse session cookie or Authorization header. Returns username or None.
        Cookie (tc_session) is checked first; Basic Auth is the fallback for
        API callers and curl scripts that don't carry cookies.
        """
        # 1 — session cookie
        cookie_hdr = self.headers.get('Cookie', '')
        for part in cookie_hdr.split(';'):
            part = part.strip()
            if part.startswith('tc_session='):
                user = _get_session(part[len('tc_session='):])
                if user:
                    return user
                break  # invalid/expired token — fall through to Basic Auth

        # 2 — HTTP Basic Auth
        auth = self.headers.get('Authorization', '')
        if not auth.startswith('Basic '):
            return None
        try:
            decoded = base64.b64decode(auth[6:], validate=False).decode('utf-8', errors='replace')
        except Exception:
            return None
        user, sep, pw = decoded.partition(':')
        if not sep:
            return None
        if user == 'owner' and OWNER_PASSWORD and pw == OWNER_PASSWORD:
            return 'owner'
        if user == 'partner' and PARTNER_PASSWORD and pw == PARTNER_PASSWORD:
            return 'partner'
        # Check per-user store
        u = _get_user(user)
        if u and u.get('password') and pw == u['password']:
            return user
        return None

    def _is_allowed(self, user, path):
        """Check if user may access path. owner = unrestricted."""
        if user == 'owner':
            return True
        p = path.split('?')[0].rstrip('/')
        # /all and /admin filtered by content — always reachable for auth'd users
        if p in ('/all', '/admin', '/admin/'):
            return True
        # Shared static assets (design-system.css, fonts, React bundles) —
        # no data, always serveable to any authenticated user.
        if path.startswith('/static/'):
            return True
        # partner tier: use tile config
        if user == 'partner':
            return _is_partner_path(path)
        # per-user: check their tile list
        u = _get_user(user)
        if not u:
            return False
        allowed_prefixes = set()
        for tile_url in (u.get('tiles') or []):
            base = tile_url.rstrip('/')
            allowed_prefixes.add(base)
            allowed_prefixes.add(base + '/')
        # supporting paths for React app — legacy slug-prefixed cove route
        # (built once at module load via _DRAWER_BACK_PATH from tenant_slug)
        if any(_DRAWER_BACK_PATH in t for t in (u.get('tiles') or [])):
            allowed_prefixes.update(['/static', '/dashboard-data.json'])
        # /deals/ deps — also unlock the legacy slug-prefixed data endpoint.
        if any('/deals' in t for t in (u.get('tiles') or [])):
            allowed_prefixes.update(['/deals', '/deals/', '/data',
                                     '/deals/data.json',
                                     f'/{COS_TENANT_SLUG}/data.json'])
        return any(p == b or p.startswith(b + '/') for b in allowed_prefixes)

    def _send_401(self):
        accept = self.headers.get('Accept', '')
        if 'text/html' in accept:
            # Browser — redirect to login form instead of showing a Basic Auth dialog
            from urllib.parse import quote as _uquote
            next_path = _uquote(self.path or '/', safe='')
            location = f'/login?next={next_path}'
            self.send_response(302)
            self.send_header('Location', location)
            self.send_header('Content-Length', '0')
            self.end_headers()
        else:
            self.send_response(401)
            # Realm label drawn from firm_context so subscriber installs
            # advertise their own firm name in the Basic-Auth dialog.
            _realm_firm = (((_FC_CTX or {}).get('firm') or {}).get('name') or '').strip() or 'COS'
            self.send_header('WWW-Authenticate', f'Basic realm="{_realm_firm} Dashboard"')
            self.send_header('Content-Length', '0')
            self.end_headers()

    def _send_403(self):
        self.send_response(403)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _is_localhost(self):
        ip = (self.client_address[0] or '').lower()
        if ip in ('127.0.0.1', '::1', 'localhost'):
            return True
        # 2026-05-04: trust connections that originate from this host's own
        # LAN IPs (e.g. 192.168.4.21 when the user opens the dashboard on the
        # same Mac via the LAN URL). Without this, /admin re-prompts for login
        # despite being accessed locally. Cached at process start in
        # _OWN_HOST_IPS — see module-level helper.
        try:
            return ip in _OWN_HOST_IPS
        except NameError:
            return False

    def send_json(self, status, body: dict):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self):
        self.send_json(200, {})

    def do_GET(self):
        # 2026-05-05 BUGFIX: strip query string from self.path before
        # exact-match route comparisons. Python's http.server keeps the
        # query string on self.path verbatim, so "/?_t=123" did not
        # match any of the `self.path == '/'` / `'/personal/'` /
        # `'/deals/'` etc. branches and fell through to 404. The cache-
        # busting cache-buster `?_t=<ts>` added to doRefresh /
        # doSectionRefresh URLs (commit 53ff180) hit this for the home
        # route every sync. Preserve the full self.path for query-aware
        # handlers; only the routing comparisons read _path_route.
        try:
            _path_route = self.path.split('?', 1)[0]
            # Re-bind self.path to the stripped form so all downstream
            # routing branches (do_GET / do_POST elif chains) see a
            # query-free path. The original is rarely needed by these
            # routes — handlers that DO need query params parse via
            # urllib.parse.urlparse / parse_qs on a captured copy.
            self._raw_path = self.path
            self.path = _path_route
        except Exception:
            pass
        # ── unauthenticated routes ──────────────────────────────
        if self.path.startswith('/login'):
            self._serve_login_page()
            return
        if self.path == '/logout':
            self._handle_logout()
            return
        # ── all other routes require auth ───────────────────────
        # Localhost browser access is treated as owner (same exemption as POSTs).
        if self._is_localhost():
            user = 'owner'
        else:
            user = self._authenticate()
        if user is None:
            self._send_401()
            return
        if not self._is_allowed(user, self.path):
            self._send_403()
            return
        if self.path in ('/', ''):
            self._serve_html_template(COS_DASHBOARD, user)
        elif self.path in ('/search', '/search/'):
            # Renamed 2026-04-28 — Search → Personal (also rescoped to job
            # search + personal action items). 301 to /personal/.
            self.send_response(301)
            self.send_header('Location', '/personal/')
            self.end_headers()
        elif self.path == '/personal':
            self.send_response(301)
            self.send_header('Location', '/personal/')
            self.end_headers()
        elif self.path == '/personal/':
            # Personal lens of the same Status template — client-side init
            # reads location.pathname and sets state.filter = 'personal',
            # which broadens to {job, personal, untagged} workstreams and
            # adds the priority-targets panel.
            self._serve_html_template(COS_DASHBOARD, user)
        elif self.path in ('/tcip', '/tcip/'):
            self._handle_tcip_form(user)
        elif self.path == '/tcip/stream':
            self._handle_tcip_stream()
        elif self.path.startswith('/tcip/result/'):
            deal_id = self.path[len('/tcip/result/'):].strip('/')
            self._handle_tcip_result(deal_id, user)
        elif self.path == '/tcip/status':
            with _tcip_lock:
                self.send_json(200, {
                    'running':   _tcip_running,
                    'exitCode':  _tcip_exit_code,
                    'startedAt': _tcip_started_at,
                    'lines':     _tcip_lines[-200:],
                })
        elif self.path == '/deals':
            self.send_response(301)
            self.send_header('Location', '/deals/')
            self.end_headers()
        elif self.path == '/deals/':
            self._serve_html_template(DEALS_DASHBOARD, user)
        elif self.path == '/deals/data.json':
            # Canonical data endpoint — same payload as /tomac/data.json.
            # Must precede the /deals/ static-file fallback below or it gets
            # masked by the prefix match.
            if DEAL_PIPELINE_DATA.exists():
                self._serve_file(DEAL_PIPELINE_DATA, 'application/json')
            else:
                self.send_json(404, {'error': 'deal-pipeline-data.json not yet generated'})
        elif self.path == '/deals/grid-signals.json':
            if GRID_SIGNALS_DATA.exists():
                self._serve_file(GRID_SIGNALS_DATA, 'application/json')
            else:
                self.send_json(404, {'error': 'grid-signals.json not yet generated — run grid_signal_scanner.py'})
        elif self.path.startswith('/deals/'):
            deals_dir = DEALS_DASHBOARD.parent
            rel = self.path[len('/deals/'):].split('?')[0]
            candidate = (deals_dir / rel).resolve()
            if candidate.is_relative_to(deals_dir.resolve()) and candidate.exists():
                ctype = ('application/javascript' if rel.endswith('.js') else
                         'application/json'       if rel.endswith('.json') else 'text/plain')
                self._serve_file(candidate, ctype)
            else:
                self.send_response(404); self.end_headers()
        elif self.path == f'/{COS_TENANT_SLUG}' or self.path == f'/{COS_TENANT_SLUG}/':
            # Legacy slug-prefixed route — kept as 301 for one release per
            # PLAN E1.6. Routes to consolidated /deals/ view (sourcing +
            # pipeline). Remove this elif in the release after this one.
            self.send_response(301)
            self.send_header('Location', '/deals/')
            self.end_headers()
        # TODO(E1, next release): remove /<slug>/data.json — use /deals/data.json.
        elif self.path == f'/{COS_TENANT_SLUG}/data.json':
            # Backward-compat data endpoint — same payload now read from /deals/.
            if DEAL_PIPELINE_DATA.exists():
                self._serve_file(DEAL_PIPELINE_DATA, 'application/json')
            else:
                self.send_json(404, {'error': 'deal-pipeline-data.json not yet generated'})
        elif self.path == '/briefing':
            self.send_response(301)
            self.send_header('Location', '/briefing/')
            self.end_headers()
        elif self.path == '/briefing/':
            self._serve_html_template(BRIEFING_DASHBOARD, user)
        elif self.path == '/briefing/intel.json':
            self._handle_briefing_intel()
        elif self.path == '/briefing/deal.md':
            if BRIEFING_MD.exists():
                self._serve_file(BRIEFING_MD, 'text/markdown; charset=utf-8')
            else:
                self.send_json(404, {'error': 'deal-briefing-latest.md not yet generated'})
        elif self.path == '/data':
            self._handle_data(user=user)
        elif self.path == '/dash/mobile' or self.path == '/dash/mobile/':
            # Phone-optimized owner view. Owner-only by policy (recruiting +
            # awaiting + recent intel are sensitive).
            if user != 'owner':
                self._send_403(); return
            self._handle_mobile_dashboard()
        elif self.path == '/cache-status':
            # Quick endpoint to check how fresh the cache is. Also surfaces
            # per-section age so the UI can flag stale tiles even when the
            # global fetch ran recently (e.g. an OAuth-scoped sub-fetch
            # silently returned empty and preserved a stale prior value).
            status = {'ok': False, 'fetchedAt': None, 'ageMin': None, 'sections': {}, 'deal_sync_stale': False}
            if STATE_PATH.exists():
                try:
                    import json as _j
                    from datetime import datetime as _dt
                    d = _j.loads(STATE_PATH.read_text())
                    fa = d.get('fetchedAt')
                    if fa:
                        now_dt = _dt.now()
                        age = (now_dt - _dt.fromisoformat(fa)).total_seconds() / 60
                        status['ok']         = True
                        status['fetchedAt']  = fa
                        status['ageMin']     = round(age, 1)
                        for name, ts in (d.get('_sectionTimestamps') or {}).items():
                            try:
                                sage = (now_dt - _dt.fromisoformat(ts)).total_seconds() / 60
                                status['sections'][name] = {
                                    'lastRefreshed': ts,
                                    'ageMin':        round(sage, 1),
                                }
                            except Exception:
                                continue
                except Exception:
                    pass
            # Check deal-sync heartbeat: stale if missing or last real run > 36h ago.
            try:
                from datetime import datetime as _dt2
                _hb_path = Path.home() / 'dashboards' / 'data' / 'deal-sync-heartbeat.json'
                if not _hb_path.exists():
                    status['deal_sync_stale'] = True
                else:
                    _hb = json.loads(_hb_path.read_text())
                    _last = _hb.get('last_completed_at', '')
                    _dry  = _hb.get('dry_run', True)
                    if _dry or not _last:
                        status['deal_sync_stale'] = True
                    else:
                        _age_h = (_dt2.now() - _dt2.fromisoformat(_last)).total_seconds() / 3600
                        status['deal_sync_stale'] = _age_h > 36
            except Exception:
                status['deal_sync_stale'] = False
            self.send_json(200, status)
        elif self.path == '/sync-preview':
            if user != 'owner':
                self._send_403(); return
            self._handle_sync_preview()
        elif self.path == '/events':
            self._handle_sse()
        elif self.path == '/pipeline-status':
            self._handle_pipeline_status()
        elif self.path in ('/all', '/all/'):
            self._handle_all(user)
        elif self.path.startswith('/admin'):
            if user != 'owner':
                self._send_403(); return
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            if parsed.path in ('/admin/spend', '/admin/spend/'):
                try:
                    days = max(1, min(int(qs.get('days', ['7'])[0]), 90))
                except Exception:
                    days = 7
                self._handle_admin_spend(days=days)
                return
            if parsed.path in ('/admin/heartbeat', '/admin/heartbeat/'):
                # owner-only: heartbeat exposes routine identifiers + scheduling
                # internals; partners and per-user logins should not see them.
                # (Tile-level allowlist already blocks Admin tile, but the
                # endpoint is reachable by direct URL — guard explicitly.)
                if user != 'owner':
                    self._send_403(); return
                self._handle_admin_heartbeat()
                return
            if parsed.path in ('/admin/transcripts', '/admin/transcripts/'):
                self._handle_admin_transcripts()
                return
            if parsed.path in ('/admin/overlay', '/admin/overlay/'):
                self._handle_admin_overlay_form(qs)
                return
            flash = None
            if 'ftype' in qs and 'fmsg' in qs:
                flash = (qs['ftype'][0], qs['fmsg'][0])
            self._handle_admin(flash=flash)
        elif self.path.split('?')[0] == '/batch-jobs':
            if user != 'owner':
                self._send_403(); return
            self.send_json(200, {'jobs': _batch_jobs_data()})
        elif self.path.split('?')[0] == '/system-health/latest.json':
            # owner-only: aggregated system_health.py output
            if user != 'owner':
                self._send_403(); return
            self._handle_system_health_latest()
        elif self.path.startswith('/api/deals/') and '/intelligence' in self.path:
            self._handle_deal_intelligence()
        elif self.path.split('?')[0] == '/api/intel-digest':
            self._handle_intel_digest()
        elif self.path.split('?')[0] == '/api/git-status':
            # owner-only: last commit + dirty status for each tracked repo
            if user != 'owner':
                self._send_403(); return
            self._handle_git_status()
        elif self.path.split('?')[0] == '/routines':
            # owner-only: routine inventory + per-task status
            if user != 'owner':
                self._send_403(); return
            self._handle_routines_list()
        elif self.path.split('?')[0] == '/routines/health':
            # owner-only: health summary + last-24h failures (digest source)
            if user != 'owner':
                self._send_403(); return
            self._handle_routines_health()
        elif (self.path.startswith('/routines/')
              and self.path.split('?')[0].endswith('/log')):
            # owner-only: text/plain log tail for one task
            if user != 'owner':
                self._send_403(); return
            self._handle_routines_log()
        elif self.path.split('?')[0] in ('/portfolio', '/portfolio/'):
            idx = TC_BUILD / 'index.html'
            if idx.exists():
                self._serve_file(idx, 'text/html; charset=utf-8')
            else:
                self.send_response(404); self.end_headers()
        elif self.path.split('?')[0] in (_DRAWER_BACK_PATH, _DRAWER_BACK_PATH + '/'):
            self._serve_html_template(COS_DASHBOARD, user)
        elif self.path.startswith('/api/auth-health'):
            # Credential health — owner-only.
            # user is already set by the _is_localhost()/authenticate() block
            # at the top of do_GET — don't re-authenticate here or LAN
            # browser requests (treated as owner via _is_localhost) get 401.
            if user != 'owner':
                self._send_403()
                return
            self._serve_auth_health_json()
        elif self.path.startswith('/api/costs') or self.path.startswith('/api/health'):
            # Cost meter and health-check endpoints — owner-only.
            user = self._authenticate()
            if user is None:
                self._send_401()
                return
            if self.path.startswith('/api/costs'):
                self._serve_costs_json()
            else:
                self._serve_health_json()
        elif self.path.startswith('/static/'):
            # Serve shared design-system + assets first (app/static/), then
            # fall back to the React build's own /static/ bundles.
            rel_full = self.path.split('?')[0].lstrip('/')              # e.g. "static/design-system.css"
            rel_child = rel_full[len('static/'):]                        # e.g. "design-system.css"
            served = False
            for base in (SHARED_STATIC, TC_BUILD / 'static'):
                candidate = (base / rel_child).resolve()
                try:
                    base_resolved = base.resolve()
                except FileNotFoundError:
                    continue
                if candidate.is_relative_to(base_resolved) and candidate.exists() and candidate.is_file():
                    ctype = ('application/javascript' if rel_child.endswith('.js')   else
                             'text/css; charset=utf-8' if rel_child.endswith('.css') else
                             'application/json'       if rel_child.endswith('.json') else
                             'image/png'              if rel_child.endswith('.png')  else
                             'image/svg+xml'          if rel_child.endswith('.svg')  else
                             'font/woff2'             if rel_child.endswith('.woff2') else
                             'text/plain')
                    self._serve_file(candidate, ctype)
                    served = True
                    break
            if not served:
                self.send_response(404); self.end_headers()
        elif self.path == '/dashboard-data.json':
            if DEAL_SYSTEM_DATA.exists():
                data = DEAL_SYSTEM_DATA.read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_json(404, {'error': 'deal-system-data.json not yet generated'})
        elif self.path == '/morning-brief':
            self._handle_morning_brief()
        elif self.path == '/project-sync/status':
            self._handle_project_sync_status()
        elif self.path == '/project-sync/update':
            self._handle_project_sync_update()
        else:
            self.send_response(404); self.end_headers()

    def _handle_all(self, user):
        """Unified landing page — renders tiles visible to the user's tier.
        Hides the legacy slug-prefixed alt view (per CEO) and the Admin
        console from the visual grid; those remain reachable by direct URL.
        The hidden tile id matches the tenant slug (cf. dashboard-tiles.yaml)."""
        HIDDEN_IN_ALL = {COS_TENANT_SLUG, 'admin'}
        tiles = [t for t in _tiles_for(user) if t.get('id') not in HIDDEN_IN_ALL]
        cards = []
        for t in tiles:
            # If this tile's required package isn't in firm_config["packages"], render with
            # a "package inactive" badge so the tile is visible but visibly degraded.
            # The dashboard always shows the full tile structure regardless of package config.
            inactive = (t.get('package_active') is False)
            badge = ''
            extra_class = ''
            if inactive:
                req_pkg = t.get('requires_package', '')
                badge = ('<div class="tc-tile-badge tc-tile-badge-inactive" '
                         'title="This tile requires the {pkg} package, which is not enabled in firm_config.json. '
                         'Tabs will show empty states until the package is activated.">'
                         '⚠ {pkg} package inactive'
                         '</div>').format(pkg=req_pkg.replace('_', ' '))
                extra_class = ' tc-tile-card-inactive'
            cards.append(
                '<a class="tc-card tc-card-hover tc-tile-card{extra_class}" href="{url}">'
                '  <div class="tc-tile-route">{url}</div>'
                '  <h2>{title}</h2>'
                '  <p>{desc}</p>'
                '  {badge}'
                '</a>'.format(
                    url=t.get('url', '#'),
                    title=t.get('title', t.get('id', '')),
                    desc=t.get('description', ''),
                    badge=badge,
                    extra_class=extra_class,
                )
            )
        try:
            html = ALL_DASHBOARD.read_text()
        except Exception:
            self.send_response(500); self.end_headers(); return
        _all_firm_name = str(((_FC_CTX or {}).get('firm') or {}).get('name', 'Firm')).strip()
        html = (html.replace('{{USER}}', user)
                    .replace('{{TILE_COUNT}}', str(len(tiles)))
                    .replace('{{TILES}}', '\n'.join(cards))
                    .replace('{{FIRM_NAME}}', _all_firm_name))
        self._serve_html(html, inject_chrome=True, user=user)

    # Admin tabs that can be granted to non-owner users
    ADMIN_TABS = [
        {'id': 'schedule', 'title': 'Schedule Recording'},
        {'id': 'calls',    'title': 'Call History'},
        {'id': 'status',   'title': 'Dashboard Status'},
    ]

    def _handle_admin_heartbeat(self):
        """Owner-only: serve cached heartbeat JSON for the active tenant.

        Cache: ~/cos-pipeline/data-<tenant>/heartbeat.json (per DECISION C1).
        Tenant slug derived from listening port (DECISION C6).
        Read-only; never mutates the cache file. Auth already enforced by the
        /admin gate above (see do_GET ~line 1809).
        """
        import json as _json
        import time as _time
        from pathlib import Path as _Path
        # Tenant from server port. Production port (7777) maps to the
        # configured tenant slug; alternate ports are dev environments
        # (7778 = re-dev, etc.). Falls back to COS_TENANT_SLUG.
        port = getattr(self.server, 'server_port', 7777)
        tenant = {7777: COS_TENANT_SLUG, 7778: 're-dev'}.get(port, COS_TENANT_SLUG)
        cache = _Path.home() / 'cos-pipeline' / f'data-{tenant}' / 'heartbeat.json'
        if not cache.exists():
            payload = {
                'status': 'uninitialized',
                'hint': 'run heartbeat.py --write-state',
                'tenant': tenant,
            }
            body = _json.dumps(payload).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)
            return
        try:
            raw = cache.read_bytes()
            payload = _json.loads(raw)
        except Exception as exc:
            err = _json.dumps({
                'status': 'cache-error',
                'tenant': tenant,
                'error': f'{type(exc).__name__}: {exc}',
            }).encode('utf-8')
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(err)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(err)
            return
        # Staleness annotation (heartbeat plist runs every 600s; 40min = 4x).
        try:
            age_h = (_time.time() - cache.stat().st_mtime) / 3600.0
            if age_h > (40.0 / 60.0):
                payload['warning'] = f'cache stale ({age_h:.1f}h)'
        except Exception:
            pass
        body = _json.dumps(payload).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _handle_admin_transcripts(self):
        """Owner-only: return processed call transcripts as JSON for the
        Call History tab. Reads credentials/processed_cos_transcripts.json,
        strips separator-only titles, and returns the last 60 entries sorted
        newest-first. Drive file IDs become clickable doc links in the UI.
        """
        import json as _json
        from pathlib import Path as _Path
        import re as _re

        tracker = _Path.home() / 'credentials' / 'processed_cos_transcripts.json'
        if not tracker.exists():
            payload = {'items': [], 'total': 0, 'error': 'tracker not found'}
            body = _json.dumps(payload).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)
            return

        raw = _json.loads(tracker.read_text())
        _SEP = _re.compile(r'^[─━│┃─━| \-]+$')  # separator-only titles
        _DRIVE_ID = _re.compile(r'^[A-Za-z0-9_\-]{20,}$')

        items = []
        for key, val in raw.items():
            if not isinstance(val, dict):
                continue
            title = (val.get('title') or '').strip()
            if not title or _SEP.match(title):
                continue
            # Skip purely internal housekeeping entries
            if val.get('skipped') and val.get('skip_reason') == 'dedup':
                continue
            entry = {
                'key':          key,
                'title':        title,
                'category':     val.get('category') or 'Other',
                'processed_at': val.get('processed_at') or '',
                'skipped':      bool(val.get('skipped')),
                'skip_reason':  val.get('skip_reason') or '',
            }
            # Build Drive link when key looks like a Drive file ID
            if _DRIVE_ID.match(key):
                entry['drive_url'] = f'https://docs.google.com/document/d/{key}/edit'
            items.append(entry)

        items.sort(key=lambda x: x['processed_at'], reverse=True)
        payload = {'items': items[:60], 'total': len(items)}
        body = _json.dumps(payload).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _handle_admin_overlay_form(self, qs):
        """Owner-only: render a simple HTML form for triggering the transcript
        overlay on a specific Google Doc ID. POSTs to /admin/process-transcript.
        Path D from the 2026-05-21 session — gives the principal a one-click way to
        overlay an arbitrary transcript (e.g. from a non-watched folder) or
        re-overlay after manual edits, without dropping to the CLI.
        """
        import html as _html
        flash = ''
        ftype = (qs.get('ftype') or [''])[0]
        fmsg  = (qs.get('fmsg')  or [''])[0]
        if ftype and fmsg:
            color = '#0a5d2d' if ftype == 'ok' else '#a02020'
            flash = (f'<div style="background:#fef9e7;border-left:4px solid {color};'
                     f'padding:12px 16px;margin:16px 0;border-radius:4px;'
                     f'color:{color};font-weight:500;">{_html.escape(fmsg)}</div>')
        page = f"""<!DOCTYPE html>
<html><head><title>Transcript Overlay — Admin</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; background:#faf7f2; color:#1b2a3e;
          max-width: 720px; margin: 40px auto; padding: 0 24px; }}
  h1 {{ font-size: 22px; margin: 0 0 8px; }}
  .sub {{ color: #5a6b80; margin-bottom: 24px; font-size: 14px; }}
  form {{ background:#fff; border:1px solid #e5dfd2; border-radius:8px; padding:24px; }}
  label {{ display:block; font-weight:600; margin: 14px 0 6px; font-size: 13px; }}
  input[type=text] {{ width: 100%; padding: 10px 12px; border: 1px solid #d0c8b7;
                       border-radius: 6px; font-size: 14px; box-sizing: border-box;
                       font-family: ui-monospace, monospace; }}
  button {{ margin-top: 18px; background:#1b2a3e; color:#fff; border:0;
            padding: 10px 22px; border-radius: 6px; font-size: 14px; cursor: pointer; }}
  button:hover {{ background:#2a3a52; }}
  .hint {{ color:#7a8a9c; font-size:12px; margin-top:4px; }}
  .nav {{ font-size: 13px; margin-bottom: 24px; }}
  .nav a {{ color:#1b2a3e; text-decoration: none; margin-right:16px; }}
</style></head>
<body>
<div class="nav"><a href="/admin/">← Admin</a> <a href="/">Dashboard</a></div>
<h1>Transcript Overlay</h1>
<div class="sub">Run <code>cos_transcript_hook.py</code> on a specific Google Doc.
Use for transcripts in non-watched folders, or to re-overlay after manual edits.
Runs async — returns immediately, hook completes in ~30-60 sec on background.</div>
{flash}
<form method="POST" action="/admin/process-transcript">
  <label for="doc_id">Google Doc ID</label>
  <input type="text" id="doc_id" name="doc_id" required
         placeholder="e.g. 1ry68HAwInrULqx1h6DXSyUkMptDX66ZcmTzPYpdhlQQ"
         pattern="[A-Za-z0-9_-]{{20,}}" />
  <div class="hint">From the Drive URL: docs.google.com/document/d/<b>DOC_ID</b>/edit</div>

  <label for="title">Title (optional — defaults to "Manual Overlay")</label>
  <input type="text" id="title" name="title"
         placeholder="e.g. Catch-Up: GridFree / Principal Name" />

  <label for="category">Category hint (optional)</label>
  <input type="text" id="category" name="category" value="auto"
         placeholder="auto | deal | recruiting | other" />

  <button type="submit">Trigger Overlay</button>
</form>
</body></html>"""
        body = page.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _handle_admin_process_transcript(self):
        """Owner-only: POST handler that kicks off cos_transcript_hook.py
        asynchronously for a given Google Doc ID. Returns a redirect back
        to /admin/overlay with a flash message. Hook runs ~30-60s in
        background — caller doesn't wait."""
        import urllib.parse as _up
        import subprocess as _sp
        clen = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(clen).decode('utf-8') if clen else ''
        form = {k: (v[0] if v else '') for k, v in _up.parse_qs(body).items()}
        doc_id = (form.get('doc_id') or '').strip()
        title  = (form.get('title')  or 'Manual Overlay').strip() or 'Manual Overlay'
        category = (form.get('category') or 'auto').strip() or 'auto'

        # Validate doc_id shape (Drive file IDs are 20+ chars, [A-Za-z0-9_-])
        import re as _re_v
        if not _re_v.match(r'^[A-Za-z0-9_-]{20,}$', doc_id):
            self._redirect_flash('/admin/overlay', 'err',
                                 f'Invalid doc_id: {doc_id[:40]}')
            return

        # Kick off the hook in background. Hook is gdocs-only — won't work
        # for plain-text Drive files (those need cos_otter_backfill.py --id).
        try:
            hook = str(Path.home() / 'cos-pipeline' / 'cos_transcript_hook.py')
            _sp.Popen(
                [sys.executable, hook,
                 '--doc-id', doc_id,
                 '--title',  title,
                 '--category', category],
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                start_new_session=True,
            )
            self._redirect_flash('/admin/overlay', 'ok',
                                 f'Overlay triggered for {doc_id[:12]}…{doc_id[-6:]} '
                                 f'(title: {title}). Check Drive in ~30-60s.')
        except Exception as e:
            self._redirect_flash('/admin/overlay', 'err',
                                 f'Failed to launch hook: {e}')

    def _redirect_flash(self, base_path, ftype, fmsg):
        """302 redirect with flash query params — used by admin POST handlers."""
        from urllib.parse import quote as _q
        loc = f'{base_path}?ftype={_q(ftype)}&fmsg={_q(fmsg)}'
        self.send_response(302)
        self.send_header('Location', loc)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _get_spend_data(self, days: int = 30) -> dict:
        """Aggregate Anthropic API spend — shared by admin tab and /admin/spend page."""
        from collections import defaultdict as _dd
        from datetime import datetime as _dt2, timedelta as _td2, timezone as _tz2
        log_path = Path.home() / 'dashboards' / 'data' / 'anthropic-usage.jsonl'
        prices = {'claude-opus': (15.00, 75.00), 'claude-sonnet': (3.00, 15.00), 'claude-haiku': (0.80, 4.00)}
        def _pr(model):
            for pfx, p in prices.items():
                if model.startswith(pfx): return p
            return (3.0, 15.0)
        cutoff   = _dt2.now(_tz2.utc) - _td2(days=days)
        agg_day  = _dd(lambda: {'calls': 0, 'cost': 0.0})
        agg_key  = _dd(lambda: {'calls': 0, 'cost': 0.0})
        agg_pipe = _dd(lambda: {'calls': 0, 'cost': 0.0})
        total_cost = 0.0; total_calls = 0
        if log_path.exists():
            try:
                with open(log_path) as f:
                    for line in f:
                        line = line.strip()
                        if not line: continue
                        try:
                            d  = json.loads(line)
                            ts = _dt2.fromisoformat(d['ts'].replace('Z', '+00:00'))
                        except Exception: continue
                        if ts < cutoff: continue
                        in_t = int(d.get('in', 0)); out_t = int(d.get('out', 0))
                        cr   = int(d.get('cache_read', 0)); cc = int(d.get('cache_create', 0))
                        pi, po = _pr(d.get('model', ''))
                        cost = ((in_t/1e6)*pi + (out_t/1e6)*po + (cr/1e6)*pi*.10 + (cc/1e6)*pi*1.25)
                        day = ts.astimezone(_tz2.utc).strftime('%Y-%m-%d')
                        agg_day[day]['calls'] += 1; agg_day[day]['cost'] += cost
                        kn = d.get('key_name', '(default)')
                        agg_key[kn]['calls'] += 1; agg_key[kn]['cost'] += cost
                        site = d.get('site', '?')
                        agg_pipe[site]['calls'] += 1; agg_pipe[site]['cost'] += cost
                        total_cost += cost; total_calls += 1
            except Exception: pass
        recent = sorted(agg_day.items())[-7:]
        proj   = round((sum(v['cost'] for _, v in recent) / max(len(recent), 1)) * 30, 2)
        return {
            'total_usd': round(total_cost, 4), 'total_calls': total_calls,
            'projected_monthly_usd': proj, 'days': days,
            'by_day':  {d: {'calls': v['calls'], 'cost': round(v['cost'], 4)} for d, v in sorted(agg_day.items())},
            'by_key':  {k: {'calls': v['calls'], 'cost': round(v['cost'], 4)} for k, v in sorted(agg_key.items(), key=lambda kv: -kv[1]['cost'])},
            'by_pipeline': sorted([{'site': s, 'calls': v['calls'], 'cost': round(v['cost'], 4)} for s, v in agg_pipe.items()], key=lambda x: -x['cost'])[:10],
        }

    def _handle_admin_spend(self, days: int = 7):
        """Owner-only Anthropic API spend dashboard.

        Reads ~/dashboards/data/anthropic-usage.jsonl (written by _usage.log_usage
        from each call site), aggregates by (site, model) and per-day totals over
        the requested window, applies family-prefix pricing, and renders an HTML
        table. Use ?days=N to widen the window (default 7, max 90)."""
        from collections import defaultdict
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz

        log_path = Path.home() / 'dashboards' / 'data' / 'anthropic-usage.jsonl'
        prices = {
            'claude-opus':   (15.00, 75.00),
            'claude-sonnet': ( 3.00, 15.00),
            'claude-haiku':  ( 0.80,  4.00),
        }
        def _price(model: str):
            for prefix, p in prices.items():
                if model.startswith(prefix):
                    return p
            return (3.0, 15.0)  # default = Sonnet pricing

        cutoff = _dt.now(_tz.utc) - _td(days=days)
        agg_pair = defaultdict(lambda: {'calls': 0, 'in': 0, 'out': 0,
                                         'cache_read': 0, 'cache_create': 0, 'cost': 0.0})
        agg_day  = defaultdict(lambda: {'calls': 0, 'cost': 0.0})
        agg_key  = defaultdict(lambda: {'calls': 0, 'cost': 0.0})
        total_cost = 0.0
        total_calls = 0

        if log_path.exists():
            try:
                with open(log_path) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            d  = json.loads(line)
                            ts = _dt.fromisoformat(d['ts'].replace('Z', '+00:00'))
                        except Exception:
                            continue
                        if ts < cutoff:
                            continue
                        site  = d.get('site', '?')
                        model = d.get('model', '?')
                        in_t  = int(d.get('in', 0))
                        out_t = int(d.get('out', 0))
                        cr    = int(d.get('cache_read', 0))
                        cc    = int(d.get('cache_create', 0))
                        pi, po = _price(model)
                        # Cache reads bill at 10% of input; cache creation at 125%.
                        cost = ((in_t / 1e6) * pi
                                + (out_t / 1e6) * po
                                + (cr / 1e6) * pi * 0.10
                                + (cc / 1e6) * pi * 1.25)
                        k = (site, model)
                        agg_pair[k]['calls']        += 1
                        agg_pair[k]['in']           += in_t
                        agg_pair[k]['out']          += out_t
                        agg_pair[k]['cache_read']   += cr
                        agg_pair[k]['cache_create'] += cc
                        agg_pair[k]['cost']         += cost
                        day = ts.astimezone(_tz.utc).strftime('%Y-%m-%d')
                        agg_day[day]['calls'] += 1
                        agg_day[day]['cost']  += cost
                        key_name = d.get('key_name', '(default)')
                        agg_key[key_name]['calls'] += 1
                        agg_key[key_name]['cost']  += cost
                        total_cost  += cost
                        total_calls += 1
            except Exception as e:
                pass

        rows_pair = sorted(agg_pair.items(), key=lambda kv: -kv[1]['cost'])
        rows_day  = sorted(agg_day.items(), reverse=True)

        def _esc(s):
            return (str(s).replace('&', '&amp;').replace('<', '&lt;')
                    .replace('>', '&gt;').replace('"', '&quot;'))

        body = []
        body.append('<!doctype html><html><head><meta charset="utf-8">')
        body.append(f'<title>Anthropic API Spend — last {days}d</title>')
        body.append('<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>')
        body.append('<style>'
                    'body{font:14px/1.45 -apple-system,BlinkMacSystemFont,sans-serif;'
                    'max-width:1100px;margin:24px auto;padding:0 16px;color:#222}'
                    'h1{font-size:20px;margin:0 0 4px}'
                    '.sub{color:#666;margin-bottom:18px}'
                    '.tot{background:#f5f6f7;padding:10px 14px;border-radius:6px;margin:12px 0;'
                    'display:flex;gap:24px;font-size:13px}'
                    '.tot b{font-size:16px}'
                    '.charts{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin:18px 0 28px}'
                    '.chart-box{background:#fafafa;border:1px solid #e8e9ea;border-radius:8px;padding:14px}'
                    '.chart-box h4{margin:0 0 10px;font-size:13px;color:#444;font-weight:600}'
                    '@media(max-width:700px){.charts{grid-template-columns:1fr}}'
                    'table{border-collapse:collapse;width:100%;margin:8px 0 24px;font-size:13px}'
                    'th,td{padding:6px 10px;text-align:right;border-bottom:1px solid #eee}'
                    'th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}'
                    'th{background:#fafafa;font-weight:600;color:#444}'
                    '.muted{color:#888}'
                    'nav a{margin-right:14px;color:#0a66c2;text-decoration:none}'
                    'nav a:hover{text-decoration:underline}'
                    '</style></head><body>')
        body.append('<nav><a href="/admin">← Admin</a>'
                    '<a href="/admin/spend?days=1">1d</a>'
                    '<a href="/admin/spend?days=7">7d</a>'
                    '<a href="/admin/spend?days=30">30d</a>'
                    '<a href="/admin/spend?days=90">90d</a></nav>')
        body.append(f'<h1>Anthropic API Spend</h1>')
        body.append(f'<div class="sub">Last {days} days · log: '
                    f'<code>{_esc(log_path)}</code></div>')

        if total_calls == 0:
            body.append('<p class="muted">No usage recorded in this window. '
                        'Call sites must invoke <code>_usage.log_usage(...)</code> '
                        'after each Anthropic API call. See '
                        '<code>scripts/anthropic-usage-report.sh</code> for the '
                        'aggregator and <code>docs/API-COST-AUDIT-2026-04-17.md</code> '
                        'for call-site instrumentation status.</p>')
        else:
            body.append(f'<div class="tot">'
                        f'<div>Total cost <b>${total_cost:.2f}</b></div>'
                        f'<div>Calls <b>{total_calls}</b></div>'
                        f'<div>Avg/call <b>${(total_cost/total_calls):.4f}</b></div>'
                        f'</div>')

            # Prepare chart data
            _day_labels = sorted(agg_day.keys())
            _day_costs  = [round(agg_day[d]['cost'], 4) for d in _day_labels]
            _key_items  = sorted(agg_key.items(), key=lambda kv: -kv[1]['cost'])
            _key_labels = [k for k, _ in _key_items]
            _key_costs  = [round(v['cost'], 4) for _, v in _key_items]
            body.append(
                '<div class="charts">'
                '<div class="chart-box"><h4>Daily Spend (USD)</h4>'
                '<canvas id="chartDay"></canvas></div>'
                '<div class="chart-box"><h4>By API Key</h4>'
                '<canvas id="chartKey"></canvas></div>'
                '</div>'
            )

            body.append('<h3>By pipeline + model</h3>')
            body.append('<table><thead><tr>'
                        '<th>Site</th><th>Model</th><th>Calls</th>'
                        '<th>Input tok</th><th>Output tok</th>'
                        '<th>Cache read</th><th>Cache create</th><th>Cost $</th>'
                        '</tr></thead><tbody>')
            for (site, model), v in rows_pair:
                body.append(f'<tr>'
                            f'<td>{_esc(site)}</td>'
                            f'<td>{_esc(model)}</td>'
                            f'<td>{v["calls"]:,}</td>'
                            f'<td>{v["in"]:,}</td>'
                            f'<td>{v["out"]:,}</td>'
                            f'<td>{v["cache_read"]:,}</td>'
                            f'<td>{v["cache_create"]:,}</td>'
                            f'<td>${v["cost"]:.3f}</td>'
                            f'</tr>')
            body.append('</tbody></table>')

            body.append('<h3>By day</h3>')
            body.append('<table><thead><tr>'
                        '<th>Day (UTC)</th><th></th><th>Calls</th>'
                        '<th colspan="4"></th><th>Cost $</th>'
                        '</tr></thead><tbody>')
            for day, v in rows_day:
                body.append(f'<tr><td>{_esc(day)}</td><td></td>'
                            f'<td>{v["calls"]:,}</td>'
                            f'<td colspan="4"></td>'
                            f'<td>${v["cost"]:.3f}</td></tr>')
            body.append('</tbody></table>')

            body.append('<p class="muted">Pricing assumes published list rates: '
                        'Opus $15/$75, Sonnet $3/$15, Haiku $0.80/$4 per Mtok '
                        '(input/output). Cache reads billed at 10% of input, '
                        'cache creation at 125%.</p>')
            body.append(
                '<script>(function(){'
                'var dL=' + json.dumps(_day_labels) + ';'
                'var dC=' + json.dumps(_day_costs) + ';'
                'var kL=' + json.dumps(_key_labels) + ';'
                'var kC=' + json.dumps(_key_costs) + ';'
                'new Chart(document.getElementById("chartDay"),{'
                'type:"bar",data:{labels:dL,datasets:[{label:"USD",data:dC,'
                'backgroundColor:"rgba(10,102,194,0.72)",borderRadius:4}]},'
                'options:{plugins:{legend:{display:false}},'
                'scales:{y:{ticks:{callback:function(v){return"$"+v.toFixed(3)}}}}}});'
                'new Chart(document.getElementById("chartKey"),{'
                'type:"bar",data:{labels:kL,datasets:[{label:"USD",data:kC,'
                'backgroundColor:"rgba(60,150,80,0.72)",borderRadius:4}]},'
                'options:{indexAxis:"y",plugins:{legend:{display:false}},'
                'scales:{x:{ticks:{callback:function(v){return"$"+v.toFixed(3)}}}}}});'
                '})();</script>'
            )

        body.append('</body></html>')
        payload = ''.join(body).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(payload)

    def _handle_admin(self, flash=None):
        all_tiles = _load_tiles()
        users = _load_users()

        # Invite-form: grouped by dashboard, sub-tabs from YAML for every tile
        def _dashboard_section(t, user_tiles=None, user_tab_access=None):
            url   = t.get('url', '')
            tid   = t.get('id', '')
            title = t.get('title', tid)
            tabs  = t.get('tabs') or []
            tile_checked  = 'checked' if user_tiles and url in (user_tiles or []) else ''
            html = (
                '<div class="adm-dashboard-group">'
                '<label class="adm-tile-check">'
                f'<input type="checkbox" name="tile" value="{url}" {tile_checked}>{title}'
                '</label>'
            )
            if tabs:
                html += '<div class="adm-subtabs">'
                user_tabs_for_tile = (user_tab_access or {}).get(tid, [])
                for tab in tabs:
                    tab_checked = 'checked' if tab['id'] in user_tabs_for_tile else ''
                    html += (
                        '<label class="adm-tile-check adm-subtab-check">'
                        f'<input type="checkbox" name="tab_access" value="{tid}:{tab["id"]}" {tab_checked}>{tab["title"]}'
                        '</label>'
                    )
                html += '</div>'
            html += '</div>'
            return html

        dashboard_sections = ''.join(_dashboard_section(t) for t in all_tiles)

        # Build user rows — each gets a data row + a hidden edit row
        if users:
            rows = ''
            for u in users:
                uname       = u.get('username', '')
                utiles      = u.get('tiles') or []
                # tab_access: {tile_id: [tab_ids]}; legacy admin_tabs migrated in
                utab_access = dict(u.get('tab_access') or {})
                if u.get('admin_tabs') and 'admin' not in utab_access:
                    utab_access['admin'] = u.get('admin_tabs', [])
                tile_tags = ''.join(
                    f'<span class="adm-tag">{t.get("title", t.get("id",""))}</span>'
                    for t in all_tiles if (t.get('url') or '').rstrip('/') in
                    [x.rstrip('/') for x in utiles]
                )
                subtab_tags = ''
                for t in all_tiles:
                    tid = t.get('id', '')
                    if tid in utab_access and utab_access[tid]:
                        tab_map = {tb['id']: tb['title'] for tb in (t.get('tabs') or [])}
                        for tab_id in utab_access[tid]:
                            lbl = tab_map.get(tab_id, tab_id)
                            subtab_tags += (
                                f'<span class="adm-tag" style="background:rgba(74,158,255,.08);'
                                f'color:#4a9eff;border-color:rgba(74,158,255,.25)">'
                                f'{t.get("title","?")} › {lbl}</span>'
                            )
                current_tiles_json   = json.dumps(utiles)
                current_tabaccess_json = json.dumps(utab_access)
                access_cell = tile_tags + subtab_tags or '<span style="color:var(--ink-ghost)">none</span>'
                # Role badge
                role_val = u.get('role', 'viewer')
                role_colors = {
                    'tc_team':    ('background:#e8eef8;color:#1a3d7c', 'TC Team'),
                    'subscriber': ('background:#e8f4e8;color:#2a6b2a', 'Subscriber'),
                    'viewer':     ('background:#f0f0f0;color:#555',    'Viewer'),
                }
                role_style, role_label = role_colors.get(role_val, role_colors['viewer'])
                role_badge = f'<span style="{role_style};padding:2px 7px;border-radius:3px;font-size:.72rem;font-weight:700">{role_label}</span>'
                # GitHub cell
                gh = u.get('github_handle', '')
                gh_cell = (f'<a href="https://github.com/{gh}" target="_blank" '
                           f'style="font-family:var(--font-data);font-size:.8rem;color:var(--navy)">@{gh}</a>'
                           if gh else '<span style="color:var(--ink-ghost);font-size:.8rem">—</span>')
                rows += (
                    f'<tr>'
                    f'<td><div class="adm-user-name">{u.get("name","")}</div>'
                    f'    <div class="adm-user-email">{u.get("email","")}</div></td>'
                    f'<td><span class="adm-user-un">{uname}</span></td>'
                    f'<td>{role_badge}</td>'
                    f'<td>{gh_cell}</td>'
                    f'<td><div class="adm-tags">{access_cell}</div></td>'
                    f'<td><div class="adm-actions">'
                    f'  <button class="btn-edit" data-edit-username="{uname}" '
                    f'          data-current-tiles="{current_tiles_json.replace(chr(34), "&quot;")}" '
                    f'          data-current-tabaccess="{current_tabaccess_json.replace(chr(34), "&quot;")}" '
                    f'          onclick="toggleEdit(\'{uname}\')" type="button">Edit access</button>'
                    f'  <form method="POST" action="/admin/revoke" style="display:inline">'
                    f'    <input type="hidden" name="username" value="{uname}">'
                    f'    <button class="btn-revoke" type="submit"'
                    f'            onclick="return confirm(\'Revoke access for {uname}?\')">Revoke</button>'
                    f'  </form>'
                    f'</div></td>'
                    f'</tr>'
                    f'<tr class="adm-edit-row" id="edit-{uname}">'
                    f'  <td colspan="6"><div class="adm-edit-cell"></div></td>'
                    f'</tr>'
                )
            table = rows
        else:
            table = f'<tr><td colspan="6"><div class="adm-empty">No users yet. Invite someone above.</div></td></tr>'

        flash_html = ''
        if flash:
            cls = 'ok' if flash[0] == 'ok' else 'err'
            flash_html = f'<div class="adm-flash {cls}">{flash[1]}</div>'

        tiles_for_js = json.dumps([
            {
                'id':    t.get('id', ''),
                'url':   t.get('url', ''),
                'title': t.get('title', t.get('id', '')),
                'tabs':  t.get('tabs') or [],
            }
            for t in all_tiles
        ])
        admtabs_for_js = json.dumps(self.ADMIN_TABS)

        # Scheduled calls
        sched_path = Path.home() / 'recordings' / 'calls' / '.scheduled_meetings.json'
        try:
            sched_raw = json.loads(sched_path.read_text()) if sched_path.exists() else {}
            sched_calls = list(sched_raw.values()) if isinstance(sched_raw, dict) else sched_raw
        except Exception:
            sched_calls = []
        # Processed transcripts
        proc_path = Path.home() / 'credentials' / 'processed_cos_transcripts.json'
        try:
            proc_raw = json.loads(proc_path.read_text()) if proc_path.exists() else {}
            proc_calls = list(proc_raw.values()) if isinstance(proc_raw, dict) else proc_raw
        except Exception:
            proc_calls = []
        # Run state
        try:
            run_state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
            run_state_slim = {k: run_state.get(k) for k in ('lastFullRunAt','lastMiniRunAt','emailQueue','processedTranscripts','runHistory')}
        except Exception:
            run_state_slim = {}

        # Deletion tombstones — newest first, show last 50
        try:
            all_dels = list(_load_deletions().get('deletions', []))
        except Exception:
            all_dels = []
        all_dels.sort(key=lambda d: d.get('deleted_at', ''), reverse=True)
        recent_dels = all_dels[:50]

        def _esc_html(s):
            return (str(s or '')
                    .replace('&', '&amp;').replace('<', '&lt;')
                    .replace('>', '&gt;').replace('"', '&quot;'))

        if recent_dels:
            rows = []
            for d in recent_dels:
                rows.append(
                    '<tr>'
                    f'<td style="font-family:var(--font-data);font-size:.75rem;color:var(--ink-mid);white-space:nowrap">{_esc_html(d.get("deleted_at",""))}</td>'
                    f'<td><span class="adm-tag">{_esc_html(d.get("source",""))}</span></td>'
                    f'<td style="font-size:.825rem">{_esc_html((d.get("context") or "")[:160])}</td>'
                    f'<td style="font-family:var(--font-data);font-size:.7rem;color:var(--ink-ghost)">{_esc_html(d.get("id",""))}</td>'
                    '<td>'
                    f'  <button class="btn-edit" onclick="recoverItem(this,\'{_esc_html(d.get("id",""))}\')" type="button">Restore</button>'
                    '</td>'
                    '</tr>'
                )
            deletions_panel = (
                '<table class="adm-users-table" style="width:100%">'
                '<thead><tr>'
                '<th style="text-align:left;font-size:.72rem;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-mid)">Deleted</th>'
                '<th style="text-align:left;font-size:.72rem;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-mid)">Source</th>'
                '<th style="text-align:left;font-size:.72rem;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-mid)">Context</th>'
                '<th style="text-align:left;font-size:.72rem;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-mid)">ID</th>'
                '<th></th>'
                '</tr></thead>'
                f'<tbody>{"".join(rows)}</tbody>'
                '</table>'
                '<script>'
                'function recoverItem(btn, id){'
                '  btn.disabled = true; btn.textContent = "…";'
                '  fetch("/item/undelete",{method:"POST",headers:{"Content-Type":"application/json"},'
                '    body: JSON.stringify({id:id})})'
                '  .then(function(r){ return r.json(); })'
                '  .then(function(){ btn.closest("tr").style.opacity="0.3"; btn.textContent="restored"; })'
                '  .catch(function(){ btn.disabled=false; btn.textContent="Restore"; alert("Restore failed"); });'
                '}'
                '</script>'
            )
            deletions_panel += (
                f'<div style="margin-top:10px;font-size:.75rem;color:var(--ink-ghost)">'
                f'{len(all_dels)} total tombstone(s) · showing most recent {len(recent_dels)}'
                '</div>'
            )
        else:
            deletions_panel = '<div class="adm-empty">No items deleted yet.</div>'

        try:
            html = ADMIN_DASHBOARD.read_text()
        except Exception:
            self.send_response(500); self.end_headers(); return
        # Build principal/team options dynamically so no tenant names are
        # hardcoded in the template.
        _adm_p_name = str(((_FC_CTX or {}).get('principal') or {}).get('name', 'Principal')).strip()
        _adm_team = (_FC_CTX or {}).get('team', [])
        _adm_members = [_adm_p_name] + [
            str(m.get('name', '')).strip()
            for m in _adm_team
            if str(m.get('name', '')).strip() and str(m.get('name', '')).strip() != _adm_p_name
        ]
        _adm_lead_opts = '\n                '.join(
            f'<option value="{n}"{"  selected" if i == 0 else ""}>{n}</option>'
            for i, n in enumerate(_adm_members)
        )
        _adm_support_opts = '\n                '.join(
            f'<option value="{n}">{n}</option>'
            for n in _adm_members
        )
        _adm_firm_name = str(((_FC_CTX or {}).get('firm') or {}).get('name', 'Firm')).strip()
        spend_data = self._get_spend_data(30)
        html = (html.replace('{{DASHBOARD_SECTIONS}}', dashboard_sections)
                    .replace('{{USERS_TABLE}}', table)
                    .replace('{{FLASH}}', flash_html)
                    .replace('{{ALL_TILES_JSON}}', tiles_for_js)
                    .replace('{{ADMIN_TABS_JSON}}', admtabs_for_js)
                    .replace('{{SCHEDULED_CALLS_JSON}}', json.dumps(sched_calls))
                    .replace('{{PROCESSED_CALLS_JSON}}', json.dumps(proc_calls))
                    .replace('{{RUN_STATE_JSON}}', json.dumps(run_state_slim))
                    .replace('{{DELETIONS_PANEL}}', deletions_panel)
                    .replace('{{SPEND_JSON}}', json.dumps(spend_data))
                    .replace('__LEAD_OPTIONS__', _adm_lead_opts)
                    .replace('__SUPPORT_OPTIONS__', _adm_support_opts)
                    .replace('{{FIRM_NAME}}', _adm_firm_name))
        self._serve_html(html, inject_chrome=True, user='owner')

    def _handle_data(self, user: str = 'owner'):
        """GET /data — returns the slim dashboard data JSON used by in-place refresh.
        Same fields as cos-dashboard-refresh.py injects into the HTML, so the browser
        can update window.DATA in-place without a full page reload.

        When PER_USER_FILTER_ENABLED is on and `user` != 'owner', the response is
        filtered through `_filter_data_for_user()` (drops recruiting / personal /
        briefing-log sections plus tilesVisible-restricted keys, and removes
        hiddenItems IDs from list sections). See server-data-filter.delta.md.
        """
        state = {}
        if STATE_PATH.exists():
            try:
                state = json.loads(STATE_PATH.read_text())
            except Exception:
                pass

        now = datetime.now()
        fetch_ts = state.get('fetchedAt', '')
        age = None
        if fetch_ts:
            try:
                ft = datetime.fromisoformat(fetch_ts)
                age = (now - ft).total_seconds() / 60
            except Exception:
                pass

        def _age_lbl(m):
            if m is None: return 'no cache'
            if m < 1:    return 'just now'
            if m < 60:   return f'{int(m)}m ago'
            return f'{int(m/60)}h {int(m%60)}m ago'

        if fetch_ts and age is not None:
            try:
                ft = datetime.fromisoformat(fetch_ts)
                generated_at = (ft.strftime('%a %b %-d · %-I:%M%p')
                                .replace('AM','a').replace('PM','p')
                                + f' ({_age_lbl(age)})')
            except Exception:
                generated_at = now.strftime('%a %b %-d %Y · %-I:%M%p').replace('AM','a').replace('PM','p')
        else:
            generated_at = now.strftime('%a %b %-d %Y · %-I:%M%p').replace('AM','a').replace('PM','p')

        # Merge user-state fundraising (buckets) over the compiled
        # state.fundraising (siblings: approach/currentFocus/competitive).
        # User state wins per Operating Principle #1.
        try:
            fr_user = _load_fundraising()
        except Exception:
            fr_user = _empty_fundraising()
        fr_compiled = state.get('fundraising', {}) or {}
        fr_merged = dict(fr_compiled)  # start from compile output
        fr_merged.update(fr_user)      # buckets + user-edited siblings overwrite
        lpdata_flat = _flatten_fundraising_to_lpdata(fr_user)

        data = {
            'today':            state.get('today',       now.strftime('%Y-%m-%d')),
            'threeDays':        state.get('threeDays',   (now + timedelta(days=3)).strftime('%Y-%m-%d')),
            'generatedAt':      generated_at,
            'cacheAgeMin':      round(age, 1) if age is not None else None,
            'upcomingCalls':    state.get('upcomingCalls',    []),
            'followUps':        state.get('followUps',        []),
            # Deal-tile bucket — frontend reads DATA[<tenant_slug>] for the
            # legacy slug-keyed deal list. Key built from COS_TENANT_SLUG so
            # this module never carries a literal slug string.
            # Filter out pipeline-generated ghost entries (stage contains "Auto")
            # before serving. These are auto-extracted contacts from transcripts
            # that should live in originationInbox, not the live-deals list.
            # Then inject any registered deals from dealPortfolio that are
            # missing (deals registered but not yet in the Deal Pipeline doc).
            COS_TENANT_SLUG:    _merge_registered_deals(
                                    state.get(COS_TENANT_SLUG, []),
                                    (state.get('dealPortfolio') or {}).get('deals', [])
                                ),
            'fundraising':      fr_merged,
            'briefingSynopsis': state.get('briefingSynopsis', {}),
            'themesSynopsis':   state.get('themesSynopsis',   {}),
            'lpData':           lpdata_flat or state.get('lpData', []),
            'staleContacts':    state.get('staleContacts',    []),
            'warmContacts':     state.get('warmContacts',     []),
            'lpNetwork':        state.get('lpNetwork',        []),
            'recentActivity':   state.get('recentActivity',   []),
            'recruiting': {
                'active':   state.get('recruiting', {}).get('active',   []),
                'archived': state.get('recruiting', {}).get('archived', []),
            },
            'calendar':         state.get('calendar',         []),
            # Pipeline / email fields (populated by scheduled AI runs)
            'emailQueue':             state.get('emailQueue',             []),
            'unprocessedTranscripts': state.get('unprocessedTranscripts', []),
            'pipelineStatus':         state.get('pipelineStatus',         {}),
            'pipelineRunHistory':     state.get('pipelineRunHistory',     []),
            'emailActivity':          state.get('emailActivity',          []),
            'gmailScanned':           state.get('gmailScanned',           ''),
            # Deal system portfolio (compiled by deal-system-compile.py)
            'dealPortfolio':          state.get('dealPortfolio',          {}),
            # Track G — costs/quota tile (populated by costs_aggregator via fetch.py)
            'costs':                  state.get('costs',                  {}),
            # Priority Synthesis — Tier 1 (always present) + Tier 2 prose (when fresh).
            # Written by app/lib/prioritize.py inside cos-dashboard-fetch.py.
            'prioritySynthesis':      state.get('prioritySynthesis',      {}),
        }
        data = _filter_data_for_user(data, user)
        self.send_json(200, data)

    def _handle_sse(self):
        """Server-Sent Events endpoint — pushes 'refresh' when warmup completes.
        Dashboard JS connects once on page load; when warmup finishes it receives
        data: refresh and calls the in-page refresh function automatically.
        Heartbeat every 25s to keep connection alive through proxies/NAT.
        """
        q = queue.Queue(maxsize=10)
        with _sse_lock:
            _sse_queues.append(q)
        self.send_response(200)
        self.send_header('Content-Type',  'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Connection',    'keep-alive')
        self.end_headers()
        try:
            while True:
                try:
                    msg = q.get(timeout=25)
                    self.wfile.write(f'data: {msg}\n\n'.encode())
                    self.wfile.flush()
                except queue.Empty:
                    # Heartbeat keeps the connection alive
                    self.wfile.write(b': heartbeat\n\n')
                    self.wfile.flush()
        except Exception:
            pass
        finally:
            with _sse_lock:
                if q in _sse_queues:
                    _sse_queues.remove(q)

    # ── Briefing endpoints (Calendar + Follow-ups) ─────────────────
    # Both endpoints share a 5-minute in-memory cache keyed on path so the
    # briefing page can poll without hammering Google APIs. Token refresh is
    # handled lazily; if the token is missing we return a clear error payload.

    def _handle_mobile_dashboard(self):
        """GET /dash/mobile — phone-optimized owner view.

        Renders a single-column HTML page from the same dashboard-data.json +
        deal-system-data.json sources the desktop dashboard uses. No client-
        side JS dependency. Light theme only (per L0011). ≥44px tap targets.

        Sections:
          1. Today — calendar events + today's items
          2. Awaiting — top 5 awaiting-external items per active deal
          3. Recent intel — last 24h of dealIntel entries
        """
        from datetime import datetime as _dt, timedelta as _td
        from html import escape as _esc

        try:
            data = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
        except Exception:
            data = {}
        try:
            deal_sys = json.loads(DEAL_SYSTEM_DATA.read_text()) if DEAL_SYSTEM_DATA.exists() else {}
        except Exception:
            deal_sys = {}

        now = _dt.now()
        today_iso = now.strftime('%Y-%m-%d')
        cutoff = now - _td(hours=24)

        # ── Today: calendar + today list ────────────────────────────────────
        calendar = data.get('calendar') or []
        today_items = data.get('today') or []
        upcoming_calls = data.get('upcomingCalls') or []

        # ── Awaiting per deal: top 5 each from awaitingExternal grouped by deal_id ─
        awaiting = data.get('awaitingExternal') or []
        by_deal: dict = {}
        for item in awaiting:
            # `dashboard_path` looks like "Deal Pipeline › <Deal Name> › ..."
            dp = (item.get('dashboard_path') or '').split('›')
            deal_name = (dp[1].strip() if len(dp) >= 2 else item.get('parent_id') or 'Other')[:60]
            by_deal.setdefault(deal_name, []).append(item)
        # Sort deals by total awaiting count desc and trim to 5 each
        deals_ordered = sorted(by_deal.items(), key=lambda kv: -len(kv[1]))

        # ── Recent intel last 24h ───────────────────────────────────────────
        intel = data.get('dealIntel') or []
        recent = []
        for it in intel:
            ts = it.get('source_ref', {}).get('date') if isinstance(it.get('source_ref'), dict) else None
            ts = ts or it.get('addedDate') or it.get('date') or ''
            try:
                dt = _dt.fromisoformat(ts[:19].replace('Z', ''))
                if dt >= cutoff:
                    recent.append((dt, it))
            except Exception:
                # If timestamp unparseable, fall back to date-only string match
                if ts and ts[:10] == today_iso:
                    recent.append((now, it))
        recent.sort(key=lambda t: t[0], reverse=True)
        recent = recent[:20]

        # ── HTML render ─────────────────────────────────────────────────────
        def _item_html(content: str, sub: str = '') -> str:
            sub_html = (f'<div class="sub">{_esc(sub)}</div>' if sub else '')
            return f'<li class="row">{_esc(content)}{sub_html}</li>'

        sections = []

        # Section 1 — Today
        today_rows = []
        for ev in calendar[:8]:
            title = ev.get('title') or ev.get('summary') or '(calendar event)'
            t_start = ev.get('start') or ev.get('time') or ''
            today_rows.append(_item_html(title, t_start))
        for c in upcoming_calls[:6]:
            title = c.get('title') or c.get('with') or '(upcoming call)'
            t_start = c.get('time') or c.get('when') or ''
            today_rows.append(_item_html(title, t_start))
        if not today_rows:
            for t in today_items[:8]:
                if isinstance(t, dict):
                    today_rows.append(_item_html(
                        t.get('what') or t.get('title') or '(today)',
                        t.get('who') or ''))
        if not today_rows:
            today_rows.append('<li class="row empty">No items for today.</li>')
        sections.append(
            '<section><h2>Today</h2><ul class="list">'
            + ''.join(today_rows) + '</ul></section>'
        )

        # Section 2 — Awaiting per deal
        if not deals_ordered:
            sections.append(
                '<section><h2>Awaiting actions</h2>'
                '<ul class="list"><li class="row empty">Nothing awaiting external.</li></ul></section>'
            )
        else:
            deal_blocks = []
            for deal_name, items in deals_ordered:
                top = items[:5]
                rows = []
                for it in top:
                    content = (it.get('content') or it.get('context') or '')[:200]
                    cp = it.get('counterparty') or ''
                    due = it.get('due') or ''
                    sub_bits = [b for b in (cp, f'due {due}' if due else '') if b]
                    rows.append(_item_html(content, ' • '.join(sub_bits)))
                deal_blocks.append(
                    f'<div class="deal-block">'
                    f'<h3>{_esc(deal_name)} <span class="count">({len(items)})</span></h3>'
                    f'<ul class="list">{"".join(rows)}</ul>'
                    f'</div>'
                )
            sections.append(
                '<section><h2>Awaiting actions</h2>' + ''.join(deal_blocks) + '</section>'
            )

        # Section 3 — Recent intel last 24h
        intel_rows = []
        for _, it in recent:
            content = (it.get('content') or '')[:240]
            cp = it.get('counterparty') or ''
            dp = (it.get('dashboard_path') or '').split('›')
            deal = (dp[1].strip() if len(dp) >= 2 else '')[:40]
            sub_bits = [b for b in (deal, cp) if b]
            intel_rows.append(_item_html(content, ' • '.join(sub_bits)))
        if not intel_rows:
            intel_rows.append('<li class="row empty">No new intel in last 24h.</li>')
        sections.append(
            '<section><h2>Recent intel (24h)</h2><ul class="list">'
            + ''.join(intel_rows) + '</ul></section>'
        )

        # Cache age
        fetched_at = data.get('fetchedAt', '')
        age_mins = ''
        if fetched_at:
            try:
                age_mins = str(int((now - _dt.fromisoformat(fetched_at)).total_seconds() / 60)) + 'm'
            except Exception:
                age_mins = ''

        page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#f7f3ea">
<title>Dash · Mobile</title>
<style>
  :root {{
    --bg: #faf7f0;
    --paper: #fffdf7;
    --ink: #1f2117;
    --muted: #6a6754;
    --accent: #5a4a1c;
    --line: #e6dfc9;
    --shadow: 0 1px 0 rgba(0,0,0,0.04);
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; background: var(--bg); color: var(--ink);
                font: 16px/1.45 -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif;
                -webkit-text-size-adjust: 100%; }}
  header {{ position: sticky; top: 0; background: var(--paper); border-bottom: 1px solid var(--line);
            padding: 14px 16px; z-index: 10; box-shadow: var(--shadow); }}
  header h1 {{ font-size: 18px; margin: 0; font-weight: 600; letter-spacing: -0.01em; }}
  header .meta {{ font-size: 12px; color: var(--muted); margin-top: 2px; }}
  header .refresh {{ position: absolute; right: 8px; top: 8px; min-width: 56px; min-height: 44px;
                     border: 1px solid var(--line); background: var(--paper); color: var(--accent);
                     border-radius: 10px; font-size: 14px; padding: 0 12px; }}
  main {{ padding: 12px 12px 96px; max-width: 100%; }}
  section {{ margin-bottom: 18px; }}
  section h2 {{ font-size: 13px; text-transform: uppercase; letter-spacing: 0.08em;
                color: var(--muted); margin: 6px 6px 8px; font-weight: 600; }}
  ul.list {{ list-style: none; margin: 0; padding: 0; background: var(--paper);
             border: 1px solid var(--line); border-radius: 12px; overflow: hidden;
             box-shadow: var(--shadow); }}
  li.row {{ padding: 14px 16px; min-height: 44px; border-bottom: 1px solid var(--line);
            display: block; word-wrap: break-word; }}
  li.row:last-child {{ border-bottom: none; }}
  li.row.empty {{ color: var(--muted); font-style: italic; }}
  li.row .sub {{ font-size: 13px; color: var(--muted); margin-top: 4px; }}
  .deal-block {{ margin-bottom: 12px; }}
  .deal-block h3 {{ font-size: 15px; margin: 12px 6px 6px; font-weight: 600; }}
  .deal-block .count {{ color: var(--muted); font-weight: 400; font-size: 13px; }}
  footer {{ padding: 16px; text-align: center; color: var(--muted); font-size: 12px; }}
  footer a {{ color: var(--accent); min-height: 44px; display: inline-block; padding: 12px 16px; }}
</style>
</head>
<body>
<header>
  <h1>Dash · Mobile</h1>
  <div class="meta">{_esc(today_iso)}{' · cache ' + _esc(age_mins) if age_mins else ''}</div>
  <button class="refresh" onclick="location.reload()">Reload</button>
</header>
<main>
{''.join(sections)}
</main>
<footer>
  <a href="/">Desktop view</a> · <a href="/deals/">Deal pipeline</a>
</footer>
</body>
</html>
"""
        body = page.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _handle_briefing_intel(self):
        """GET /briefing/intel.json — daily briefing synopsis from compiled dashboard-data.json.
        Shape: {synopsis: {captureSummary: {date, <slug>, recruiting, other, actionItems}}, fetchedAt}
        where <slug> is the tenant slug (legacy deal-bucket key)."""
        global _briefing_cache
        cache = _briefing_cache.get('briefing_intel')
        if cache and (time.time() - cache[0]) < 120:
            self.send_json(200, cache[1]); return
        try:
            data = json.loads(STATE_PATH.read_text())
            synopsis = data.get('briefingSynopsis') or {}
            mc = data.get('marketCommentary') or []
            latest_market = mc[0] if isinstance(mc, list) and mc else None
            # Serve full briefing text so the browser can render detail equal
            # to the email. Check today's file first, then yesterday's as fallback.
            full_text = ''
            for delta in (0, 1):
                candidate_date = (datetime.now() - timedelta(days=delta)).strftime('%Y-%m-%d')
                for candidate in [
                    Path(f'/tmp/daily_briefing_{candidate_date}.txt'),
                    Path(f'/tmp/weekly_briefing_{candidate_date}.txt'),
                ]:
                    if candidate.exists():
                        try:
                            full_text = candidate.read_text(encoding='utf-8')
                        except Exception:
                            pass
                        break
                if full_text:
                    break
            # 2026-05-04: append two auto-derived sections to fullText —
            #   1. "Deal Activity Log" (V1 rule): per-deal recent extraction
            #      signal (followUps / awaitingExternal / dealIntel) auto-
            #      tagged to deals at compile time. NO manual log
            #      maintenance.
            #   2. "Deal Readthrough" (U2 rule): market intel matched to
            #      active deals.
            try:
                ds_path = Path(__file__).parent.parent / 'data' / 'compiled' / 'deal-system-data.json'
                if ds_path.exists():
                    ds = json.loads(ds_path.read_text())
                    # ── Deal Activity Log ──
                    log_lines = []
                    for d in (ds.get('deals') or []):
                        rl = d.get('recent_log') or []
                        if not rl:
                            continue
                        log_lines.append(f"\n**{d.get('name','')}** · {d.get('stage','')}")
                        for e in rl[:3]:
                            src_chip = {
                                'followup': '📌', 'awaitingExternal': '⏳',
                                'intel': '📰',
                            }.get(e.get('source',''), '·')
                            who = (e.get('who') or '')[:50]
                            what = (e.get('what') or '')[:180]
                            src_url = (e.get('source_url') or '').strip()
                            src_title = (e.get('source_title') or '').strip()
                            # If we have a source URL, append a clickable
                            # "open source" link so the user can read the
                            # full email / transcript. Title (if present)
                            # is the link text; otherwise generic "open".
                            link_suffix = ''
                            if src_url:
                                label = (src_title[:60] or 'open source')
                                link_suffix = f" · [{label}]({src_url})"
                            log_lines.append(
                                f"  {src_chip} _{e.get('date','')}_ "
                                f"**{who}** — {what}{link_suffix}"
                            )
                    if log_lines:
                        log_section = (
                            "\n\n---\n\n### Deal Activity Log\n"
                            "_Recent signals auto-tagged to each deal "
                            "(followups, awaiting items, deal intel)._\n"
                            + "\n".join(log_lines) + "\n"
                        )
                    else:
                        log_section = ''

                    # ── Deal Readthrough ──
                    rt_lines = []
                    for d in (ds.get('deals') or []):
                        rts = d.get('recent_readthroughs') or []
                        if not rts:
                            continue
                        rt_lines.append(f"\n**{d.get('name','')}** · {d.get('stage','')}")
                        for r in rts[:3]:
                            conf_marker = '🟢' if r.get('confidence') == 'high' else '🟡'
                            rt_lines.append(
                                f"  {conf_marker} _{r.get('section','')}_ — "
                                f"{(r.get('text') or '')[:200]}"
                            )
                    if rt_lines:
                        rt_section = (
                            "\n\n---\n\n### Deal Readthrough\n"
                            "_Market intel matched to active deals (auto, last refresh)._\n"
                            + "\n".join(rt_lines) + "\n"
                        )
                    else:
                        rt_section = ''

                    combined = log_section + rt_section
                    if combined:
                        # Insert before "### Intelligence" if present, else append
                        if '### Intelligence' in full_text:
                            full_text = full_text.replace(
                                '### Intelligence',
                                combined + '\n### Intelligence',
                                1,
                            )
                        else:
                            full_text = full_text + combined
            except Exception as _e:
                print(f'[briefing_intel] log/readthrough merge skipped: {_e}', flush=True)

            # captureSummary.date freshness assertion (rule I4, codified
            # 2026-05-04). Surface a staleness signal so the /briefing/ tab
            # can render a chip when the synopsis is out of date.
            #
            # Enhanced 2026-05-05: when stale, scan the cos-capture-pipeline
            # log tail for the actual blocker — distinguishes "didn't run"
            # from "tried to run but the API/CLI returned an error". Common
            # patterns surfaced:
            #   • API spend-limit reached (Anthropic 400)
            #   • Claude CLI exit code 1 / stderr pattern
            #   • OAuth 403 on Calendar / Drive
            # The principal sees "API quota blocked until X" instead of the
            # useless "Run cos-capture-pipeline to refresh" when the
            # underlying problem isn't a missed schedule but a hard block.
            capture_staleness = None

            def _diagnose_capture_blocker():
                """Return a one-line context string from recent capture
                logs, or '' when nothing actionable surfaces. Reads at
                most the last ~16KB of each candidate log so a runaway
                file doesn't slow the request."""
                candidates = [
                    Path.home() / 'dashboards' / 'logs' / 'cos-capture-pipeline.log',
                    Path.home() / 'dashboards' / 'logs' / 'claude-tasks' / 'cos-capture-pipeline.stdout.log',
                ]
                tails = []
                for p in candidates:
                    try:
                        if not p.exists():
                            continue
                        size = p.stat().st_size
                        with p.open('rb') as fh:
                            if size > 16 * 1024:
                                fh.seek(size - 16 * 1024)
                                fh.readline()
                            tails.append(fh.read().decode('utf-8', errors='replace'))
                    except Exception:
                        continue
                blob = '\n'.join(tails)
                if not blob:
                    return ''
                # Pattern 1 — Anthropic spend-limit / quota message.
                # Logs are append-only chronological, so use the LAST match
                # (most recent reset date) — the file may carry an earlier
                # spend-limit incident with a now-passed reset date.
                ms = re.findall(
                    r'You have reached your specified API usage limits.{0,80}?regain access on (\d{4}-\d{2}-\d{2})',
                    blob,
                )
                if ms:
                    return f'API spend limit reached — pipeline blocked until {ms[-1]}'
                # Pattern 2 — Claude OAuth token expired (subscription path).
                # Codified 2026-05-05. The injected long-lived OAuth token
                # has a finite TTL; when it expires every plist that depends
                # on it stops working until the principal regenerates via
                # `claude setup-token` and re-runs inject-claude-oauth-token.sh.
                if re.search(
                    r'(invalid[_ -]?(api[_ -]?key|token)|'
                    r'expired[_ -]?token|oauth[_ -]?(expired|invalid)|'
                    r'401\b.*(unauthor|invalid[_ -]?token)|'
                    r'CLAUDE_CODE_OAUTH_TOKEN.*(expired|invalid))',
                    blob, re.IGNORECASE,
                ):
                    return ('Claude OAuth token expired or invalid — '
                            'regenerate via `claude setup-token` + re-run '
                            'scripts/inject-claude-oauth-token.sh')
                # Pattern 3 — generic 429 / rate limit
                if re.search(r'rate.?limit|429', blob, re.IGNORECASE):
                    return 'Rate-limit pattern in recent runs — investigate'
                # Pattern 4 — Claude CLI sub-exit (auth-context failure
                # in launchd, missing OAuth token in plist, etc.)
                if re.search(r'Fatal error in message reader.*exit code 1', blob):
                    return ('Claude CLI sub-call failing (exit 1) — check '
                            'CLAUDE_CODE_OAUTH_TOKEN is set in plist env, or '
                            'subscription auth available')
                # Pattern 5 — OAuth 403 on Google APIs
                if re.search(r'(403.*Forbidden|invalid_grant|token_expired)', blob):
                    return 'Google OAuth 403 / invalid_grant — refresh tokens'
                return ''

            try:
                cs_date = ((synopsis.get('captureSummary') or {}).get('date') or '')[:10]
                if re.match(r'^\d{4}-\d{2}-\d{2}$', cs_date):
                    today_d = datetime.now().date()
                    cs_d = datetime.strptime(cs_date, '%Y-%m-%d').date()
                    days_stale = (today_d - cs_d).days
                    if days_stale > 1:
                        severity = 'stale' if days_stale > 3 else 'warn'
                        diag = _diagnose_capture_blocker()
                        if diag:
                            msg = (
                                f'Capture summary is {days_stale} days old '
                                f'({cs_date}). {diag}.'
                            )
                        else:
                            msg = (
                                f'Capture summary is {days_stale} day'
                                f'{"s" if days_stale != 1 else ""} old '
                                f'({cs_date}). Run cos-capture-pipeline to refresh.'
                            )
                        capture_staleness = {
                            'date': cs_date,
                            'daysStale': days_stale,
                            'severity': severity,
                            'message': msg,
                            'blocker': diag or None,
                        }
                else:
                    capture_staleness = {
                        'date': '',
                        'daysStale': None,
                        'severity': 'unknown',
                        'message': 'Capture summary has no date — pipeline may not have run.',
                        'blocker': None,
                    }
            except Exception as _e:
                pass

            payload = {
                'synopsis': synopsis,
                'marketCommentary': latest_market,
                'date': data.get('today', ''),
                'fullText': full_text,
                'captureStaleness': capture_staleness,
                'fetchedAt': datetime.now().isoformat(timespec='seconds'),
            }
            _briefing_cache['briefing_intel'] = (time.time(), payload)
            self.send_json(200, payload)
        except Exception as e:
            print(f'[briefing_intel] read failed: {e}', flush=True)
            self.send_json(502, {'error': 'briefing intel read failed', 'detail': str(e)[:200]})

    def _serve_file(self, path, content_type):
        try:
            data = path.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_response(404); self.end_headers()

    # ── Login / logout ─────────────────────────────────────────────────────
    _LOGIN_PAGE = """\
<!doctype html>
<html lang="en">
<!-- NO_TC_CHROME -->
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in — {firm_name}</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #1a1714; color: #e8e0d4; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; min-height: 100vh; display: flex; align-items: center; justify-content: center; }
  .card { background: #211e1b; border: 1px solid #3a3530; border-radius: 10px; padding: 40px 36px; width: 360px; }
  h1 { font-size: 18px; font-weight: 600; letter-spacing: .02em; color: #f0e8dc; margin-bottom: 28px; text-align: center; }
  label { display: block; font-size: 11px; text-transform: uppercase; letter-spacing: .08em; color: #8c8378; margin-bottom: 6px; }
  input { width: 100%; background: #2a2520; border: 1px solid #3a3530; border-radius: 6px; color: #e8e0d4; font-size: 14px; padding: 10px 12px; outline: none; margin-bottom: 18px; }
  input:focus { border-color: #c8a96e; }
  button { width: 100%; background: #c8a96e; border: none; border-radius: 6px; color: #1a1714; font-size: 14px; font-weight: 600; padding: 11px; cursor: pointer; letter-spacing: .03em; }
  button:hover { background: #d4b87e; }
  .err { background: #3a1f1f; border: 1px solid #6b2f2f; border-radius: 6px; color: #e88; font-size: 13px; padding: 10px 12px; margin-bottom: 18px; }
  .brand { text-align: center; font-size: 11px; color: #5a5248; margin-top: 24px; letter-spacing: .06em; text-transform: uppercase; }
</style>
</head>
<body>
<div class="card">
  <h1>{firm_name} Dashboard</h1>
  {error_block}
  <form method="post" action="/login">
    <input type="hidden" name="next" value="{next_path}">
    <label for="u">Username</label>
    <input id="u" name="username" type="text" autocomplete="username" autofocus required>
    <label for="p">Password</label>
    <input id="p" name="password" type="password" autocomplete="current-password" required>
    <button type="submit">Sign in</button>
  </form>
  <div class="brand">{firm_name} &nbsp;·&nbsp; Private</div>
</div>
</body>
</html>"""

    def _serve_login_page(self, error: str = '', next_path: str = '/'):
        error_block = f'<div class="err">{error}</div>' if error else ''
        # Firm-name substitution — sourced from firm_context so subscriber
        # installs render their own brand on the login page rather than
        # the maintainer's. Falls back to a tenant-neutral default.
        _firm_name = (((_FC_CTX or {}).get('firm') or {}).get('name') or '').strip() or 'COS Dashboard'
        html = (self._LOGIN_PAGE
                .replace('{firm_name}',  _firm_name)
                .replace('{error_block}', error_block)
                .replace('{next_path}',   next_path))
        body = html.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_login_post(self):
        from urllib.parse import parse_qs, urlparse, quote as _uquote, unquote as _uunquote
        length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(length).decode('utf-8', errors='replace')
        params = {k: v[0] for k, v in parse_qs(raw).items()}
        username = params.get('username', '').strip()
        password = params.get('password', '')
        raw_next = params.get('next', '/')
        # Validate next — must be a relative path only (no scheme, no netloc)
        parsed = urlparse(raw_next)
        next_path = parsed.path or '/'
        if not next_path.startswith('/'):
            next_path = '/'

        # Authenticate against same credential store as _authenticate()
        user = None
        if username == 'owner' and OWNER_PASSWORD and password == OWNER_PASSWORD:
            user = 'owner'
        elif username == 'partner' and PARTNER_PASSWORD and password == PARTNER_PASSWORD:
            user = 'partner'
        else:
            u = _get_user(username)
            if u and u.get('password') and password == u['password']:
                user = username

        if user is None:
            self._serve_login_page(error='Invalid username or password.', next_path=next_path)
            return

        token = _create_session(user)
        self.send_response(302)
        self.send_header('Location', next_path)
        self.send_header('Set-Cookie',
            f'tc_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_TTL}')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _handle_logout(self):
        # Clear the session cookie and delete the server-side session
        cookie_hdr = self.headers.get('Cookie', '')
        for part in cookie_hdr.split(';'):
            part = part.strip()
            if part.startswith('tc_session='):
                _delete_session(part[len('tc_session='):])
                break
        self.send_response(302)
        self.send_header('Location', '/login')
        self.send_header('Set-Cookie', 'tc_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _serve_costs_json(self):
        """JSON: aggregated Anthropic API spend over last 7 days. Read by Health/Costs tile."""
        try:
            import subprocess
            here = Path(__file__).parent
            costs_script = here / "costs.py"
            if not costs_script.exists():
                # If symlinked, follow to ~/cos-pipeline/
                alt = Path.home() / "cos-pipeline" / "costs.py"
                costs_script = alt if alt.exists() else costs_script
            proc = subprocess.run(
                ["python3", str(costs_script), "--json", "--days", "7"],
                capture_output=True, timeout=15,
            )
            if proc.returncode != 0:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b'{"error":"costs script failed"}')
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(proc.stdout)
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def _serve_auth_health_json(self):
        """GET /api/auth-health — reads ~/credentials/auth_health.json, returns JSON.

        Written by _auth_watchdog.py.  Owner-only; called by the admin Auth Health tab.
        """
        health_path = Path.home() / 'credentials' / 'auth_health.json'
        if not health_path.exists():
            self.send_json(200, {
                'error': 'auth_health.json not found — run _auth_watchdog.py to generate it',
                'credentials': {},
            })
            return
        try:
            data = json.loads(health_path.read_text())
            self.send_json(200, {'credentials': data})
        except Exception as e:
            self.send_json(500, {'error': str(e), 'credentials': {}})

    def _serve_health_json(self):
        """JSON: status of each LaunchAgent + last-run times. Read by Health tile."""
        import subprocess
        try:
            statuses = []
            # Get all cos-related LaunchAgents
            result = subprocess.run(
                ["launchctl", "list"], capture_output=True, text=True, timeout=5
            )
            log_dir = Path.home() / "dashboards" / "logs" / "claude-tasks"
            for line in result.stdout.splitlines()[1:]:
                parts = line.split()
                if len(parts) < 3:
                    continue
                pid_str, status_str, label = parts[0], parts[1], parts[2]
                if 'cos-pipeline' not in label and 'claude-task.cos' not in label:
                    continue
                # Find the most recent log file for this label
                short = label.split('.')[-1]
                log_path = log_dir / f"{short}.stdout.log"
                last_modified = None
                if log_path.exists():
                    last_modified = log_path.stat().st_mtime
                statuses.append({
                    "label": label,
                    "short_name": short,
                    "pid": int(pid_str) if pid_str != '-' else None,
                    "exit_status": int(status_str) if status_str != '-' else None,
                    "running": pid_str != '-',
                    "last_log_ts": last_modified,
                    "last_log_iso": (datetime.fromtimestamp(last_modified).isoformat()
                                      if last_modified else None),
                })
            # Sort: failures first, then by last_log_ts desc
            statuses.sort(key=lambda s: (s.get('exit_status') or 0, -(s.get('last_log_ts') or 0)))
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(json.dumps({
                "checked_at": datetime.now().isoformat(),
                "tasks": statuses,
            }).encode())
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def _serve_html(self, html: str, inject_chrome: bool = True, user: str = 'owner'):
        """Serve an HTML string; optionally inject shared design-system + topnav.

        2026-05-04: explicit Cache-Control: no-store headers added. iOS Safari
        was serving stale HTML on reload-after-sync, masking config edits the
        user had just made (e.g., owner reassignment in deal-config.yaml).
        Server-side data is freshly injected on every serve via
        _load_deal_config / _load_recruit_config, so disabling browser cache
        for the HTML shell is safe — page weight is small and the JSON
        payloads inside already have their own cache headers.
        """
        if inject_chrome:
            html = _inject_shared_chrome(html, user)
        body = html.encode('utf-8')
        accept_enc = self.headers.get('Accept-Encoding', '')
        if 'gzip' in accept_enc and len(body) > 1024:
            body = gzip.compress(body, compresslevel=6)
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Encoding', 'gzip')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Expires', '0')
            self.end_headers()
        else:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Expires', '0')
            self.end_headers()
        self.wfile.write(body)

    def _serve_html_template(self, path, user: str = 'owner'):
        """Read an HTML template off disk, inject shared chrome, serve.

        For dashboard paths (cos / deals), `path` is the .rendered.html.
        If rendered is missing (first-run before any refresh), fall back
        to the .template.html sibling so the server doesn't 404 on cold
        start. The fallback shows an empty data block — UI degrades to
        empty-state placeholders rather than a broken page.
        """
        try:
            html = path.read_text()
        except FileNotFoundError:
            # Try .template.html sibling
            tmpl = path.parent / path.name.replace('.rendered.html', '.template.html')
            if tmpl.exists() and tmpl != path:
                html = tmpl.read_text()
            else:
                self.send_response(404); self.end_headers(); return
        # Inject firm name into any template that uses {{FIRM_NAME}}
        if '{{FIRM_NAME}}' in html:
            _tmpl_firm = str(((_FC_CTX or {}).get('firm') or {}).get('name', 'Firm')).strip()
            html = html.replace('{{FIRM_NAME}}', _tmpl_firm)
        self._serve_html(html, inject_chrome=True, user=user)

    def do_POST(self):
        # 2026-05-05 BUGFIX (companion to do_GET fix above): strip
        # query string from self.path before exact-match comparisons.
        # POST handlers with `self.path == '/X'` patterns 404'd when
        # any cache-buster or tracking query landed on the URL.
        try:
            self._raw_path = self.path
            self.path = self.path.split('?', 1)[0]
        except Exception:
            pass
        # ── unauthenticated routes ──────────────────────────────
        if self.path == '/login':
            self._handle_login_post()
            return
        # POSTs must come from localhost OR be authenticated as owner.
        # Routines on the Mac curl localhost directly (no auth needed);
        # remote browsers must present owner credentials.
        #
        # PARTNER-TIER WHITELIST: a few low-impact endpoints can be invoked
        # by partner-tier users too (e.g. dismissing a false-positive SMS
        # signal on an awaiting item — no data destruction, scoped to one
        # signal array). Keep this list short and audit each addition.
        _POST_PARTNER_OK = {
            '/api/awaiting/dismiss-sms-signals',
            '/api/followup/dismiss-completion-signals',  # mirror endpoint for commitment-pair chip
        }
        if not self._is_localhost():
            user = self._authenticate()
            if user is None:
                self._send_401()
                return
            if user != 'owner' and self.path not in _POST_PARTNER_OK:
                self._send_403()
                return
        if self.path == '/refresh':
            self._handle_refresh()
        elif self.path == '/warmup':
            self._handle_warmup()
        elif self.path == '/api/run-health-check':
            if not self._is_localhost():
                user = self._authenticate()
                if user != 'owner':
                    self._send_403(); return
            self._handle_run_health_check()
        elif self.path == '/run-pipeline':
            self._handle_run_pipeline()
        elif self.path == '/refresh-deals':
            self._handle_refresh_deals()
        elif self.path == '/compile-deals':
            self._handle_compile_deals()
        elif self.path == '/refresh-all':
            self._handle_refresh_all()
        elif self.path == '/queue-correction':
            self._handle_queue_correction()
        elif self.path == '/build-backlog/append':
            self._handle_build_backlog_append()
        elif self.path == '/fundraising/add':
            self._handle_fundraising_add()
        elif self.path == '/fundraising/update':
            self._handle_fundraising_update()
        elif self.path == '/patch':
            self._handle_patch()
        elif self.path == '/item/delete':
            self._handle_item_delete()
        elif self.path == '/api/awaiting/dismiss-sms-signals':
            self._handle_dismiss_sms_signals()
        elif self.path == '/api/followup/dismiss-completion-signals':
            self._handle_dismiss_completion_signals()
        elif self.path == '/item/undelete':
            self._handle_item_undelete()
        elif self.path == '/topics/save':
            self._handle_topics_save()
        elif self.path == '/order/save':
            self._handle_order_save()
        elif self.path == '/learning/accept':
            self._handle_learning_accept()
        elif self.path == '/learning/reject':
            self._handle_learning_reject()
        elif self.path == '/learning/defer':
            self._handle_learning_defer()
        elif (self.path.startswith('/batch-jobs/')
              and self.path.split('?')[0].endswith('/force')):
            if not self._is_localhost():
                user = self._authenticate()
                if user != 'owner':
                    self._send_403(); return
            segs = self.path.split('?')[0].split('/')
            # /batch-jobs/<batch_id>/force
            if len(segs) == 4:
                bid = segs[2]
                ok, msg = _batch_force_retrieve(bid)
                self.send_json(200 if ok else 409, {'ok': ok, 'message': msg})
            else:
                self.send_json(400, {'ok': False, 'error': 'bad path'})
        elif (self.path.startswith('/routines/')
              and self.path.split('?')[0].endswith('/kickstart')):
            # localhost-or-owner (gate already applied above for non-localhost
            # POSTs); we do a strict task-name check inside the handler.
            self._handle_routines_kickstart()
        elif self.path == '/deal/override':
            # Manual edit of deck/model/partner fields on a deal tile.
            # Auth gate already applied above (localhost-or-owner).
            self._handle_deal_override()
        elif self.path == '/deal/workstream':
            self._handle_deal_workstream()
        elif self.path == '/admin/invite':
            if not self._is_localhost():
                user = self._authenticate()
                if user != 'owner':
                    self._send_403(); return
            self._handle_admin_invite()
        elif self.path == '/admin/revoke':
            if not self._is_localhost():
                user = self._authenticate()
                if user != 'owner':
                    self._send_403(); return
            self._handle_admin_revoke()
        elif self.path == '/admin/update':
            if not self._is_localhost():
                user = self._authenticate()
                if user != 'owner':
                    self._send_403(); return
            self._handle_admin_update()
        elif self.path == '/admin/process-transcript':
            if not self._is_localhost():
                user = self._authenticate()
                if user != 'owner':
                    self._send_403(); return
            self._handle_admin_process_transcript()
        elif self.path == '/admin/schedule-dial-in':
            if not self._is_localhost():
                user = self._authenticate()
                if user != 'owner':
                    self._send_403(); return
            self._handle_admin_schedule_dial_in()
        elif self.path == '/admin/schedule-webinar':
            if not self._is_localhost():
                user = self._authenticate()
                if user != 'owner':
                    self._send_403(); return
            self._handle_admin_schedule_webinar()
        elif self.path == '/project-sync/update':
            self._handle_project_sync_update()
        elif self.path == '/tcip/onboard':
            self._handle_tcip_onboard()
        else:
            self.send_json(404, {'ok': False, 'error': 'not found'})

    # ── TCIP Deal Onboarding ────────────────────────────────────────────────────

    def _handle_tcip_form(self, user='owner'):
        """GET /tcip/ — render the deal onboarding form."""
        html = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TCIP — New Deal Onboarding</title>
<link rel="stylesheet" href="/static/design-system.css">
<style>
  body { background: var(--bg); color: var(--ink); font-family: var(--font-body); margin: 0; }
  .tcip-wrap { max-width: 740px; margin: 0 auto; padding: 36px 24px 80px; }
  h1 { font-family: var(--font-display); font-size: 22px; font-weight: 700;
       color: var(--navy); margin: 0 0 4px; }
  .tcip-sub { font-size: 13px; color: var(--ink-faint); margin-bottom: 28px; }
  .tcip-card { background: var(--paper); border: 1px solid var(--rule);
               border-radius: var(--radius-md); padding: 24px 28px; margin-bottom: 20px;
               box-shadow: var(--shadow-card); }
  .tcip-card h2 { font-family: var(--font-display); font-size: 13px; font-weight: 700;
                  color: var(--navy); text-transform: uppercase; letter-spacing: .06em;
                  margin: 0 0 18px; border-bottom: 1px solid var(--rule-light); padding-bottom: 10px; }
  .field { margin-bottom: 16px; }
  .field label { display: block; font-size: 12px; font-weight: 600; color: var(--ink-mid);
                 text-transform: uppercase; letter-spacing: .05em; margin-bottom: 5px; }
  .field input, .field select {
    width: 100%; box-sizing: border-box;
    background: var(--white); border: 1px solid var(--rule);
    border-radius: var(--radius-sm); padding: 8px 10px;
    font-family: var(--font-body); font-size: 14px; color: var(--ink);
    outline: none; transition: border-color .15s;
  }
  .field input:focus, .field select:focus { border-color: var(--navy-mid); }
  .field .hint { font-size: 11px; color: var(--ink-ghost); margin-top: 4px; }
  .check-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .check-item { display: flex; align-items: flex-start; gap: 8px; font-size: 13px;
                color: var(--ink-mid); }
  .check-item input[type=checkbox] { margin-top: 2px; accent-color: var(--navy); flex-shrink: 0; }
  .check-item .ci-label { font-weight: 600; color: var(--ink); font-size: 12px; }
  .check-item .ci-desc  { font-size: 11px; color: var(--ink-ghost); }
  .tcip-run-btn {
    background: var(--navy); color: var(--white);
    border: none; border-radius: var(--radius-md);
    padding: 11px 28px; font-size: 14px; font-weight: 600;
    font-family: var(--font-body); cursor: pointer; letter-spacing: .03em;
    transition: background .15s;
  }
  .tcip-run-btn:hover:not(:disabled) { background: var(--navy-mid); }
  .tcip-run-btn:disabled { opacity: .5; cursor: not-allowed; }
  #tcip-terminal {
    display: none; margin-top: 24px;
    background: #0f1e38; border-radius: var(--radius-md);
    border: 1px solid var(--navy-mid); overflow: hidden;
  }
  #tcip-term-header {
    background: #162d4a; padding: 8px 14px;
    font-family: var(--font-data); font-size: 11px; color: #7a9bbc;
    display: flex; align-items: center; gap: 8px;
  }
  #tcip-term-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--gold-mid); }
  #tcip-term-body {
    padding: 14px; font-family: var(--font-data); font-size: 12px;
    line-height: 1.6; color: #c8d8e8; max-height: 480px; overflow-y: auto;
    white-space: pre-wrap; word-break: break-word;
  }
  .term-ok   { color: #6bffb0; }
  .term-err  { color: #ff6b6b; }
  .term-head { color: var(--gold-mid); font-weight: 700; }
  #tcip-result { display: none; margin-top: 16px; padding: 14px 18px;
                 border-radius: var(--radius-md); font-size: 13px; font-weight: 600; }
  .result-ok  { background: var(--green-bg); border: 1px solid var(--green-bd); color: var(--green); }
  .result-err { background: var(--red-bg);   border: 1px solid var(--red-bd);   color: var(--red); }
</style>
</head>
<body>
{{TOPNAV}}
<div class="tcip-wrap">
  <h1>New Deal Onboarding</h1>
  <p class="tcip-sub">Fills Drive folders, generates status.md and master_brief.md, updates deal-system-data.json, and prints project instructions.</p>

  <div class="tcip-card">
    <h2>Deal Identity</h2>
    <div class="field">
      <label>Deal Name</label>
      <input id="f-name" type="text" placeholder="e.g. Lakeview Wind Farm" autocomplete="off">
    </div>
    <div class="field">
      <label>Deal ID <span style="font-weight:400;text-transform:none;letter-spacing:0">(auto-generated — edit if needed)</span></label>
      <input id="f-id" type="text" placeholder="e.g. lakeview_wind" autocomplete="off" style="font-family:var(--font-data)">
      <div class="hint">Lowercase, underscores only. Used as the key in deal-system-data.json.</div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
      <div class="field">
        <label>Lead Principal</label>
        <select id="f-lead">
          __LEAD_OPTIONS__
        </select>
      </div>
      <div class="field">
        <label>Support Principal</label>
        <select id="f-support">
          <option value="">— none —</option>
          __SUPPORT_OPTIONS__
        </select>
      </div>
    </div>
  </div>

  <div class="tcip-card">
    <h2>Drive Setup</h2>
    <div class="field">
      <label>Dataroom Folder ID or URL</label>
      <input id="f-drive" type="text" placeholder="Paste Drive folder URL or bare folder ID"
             style="font-family:var(--font-data);font-size:12px" autocomplete="off">
      <div class="hint">Leave blank to have the script create a new folder automatically.</div>
    </div>
    <div class="field">
      <label>Local Documents Path <span style="font-weight:400;text-transform:none;letter-spacing:0">(optional)</span></label>
      <input id="f-docs" type="text" placeholder="/path/to/deal/docs — leave blank to skip">
      <div class="hint">If you have deal docs locally, the script will read and analyse them via Claude API.</div>
    </div>
  </div>

  <div class="tcip-card">
    <h2>Triggers <span style="font-weight:400;text-transform:none;letter-spacing:0;font-size:11px;color:var(--ink-ghost)">(optional — adds urgency flags to project instructions)</span></h2>
    <div class="check-grid">
      <label class="check-item"><input type="checkbox" name="trigger" value="cash_runway">
        <span><div class="ci-label">Cash Runway</div><div class="ci-desc">Refi or runway deadline risk</div></span></label>
      <label class="check-item"><input type="checkbox" name="trigger" value="regulatory_vote">
        <span><div class="ci-label">Regulatory Vote</div><div class="ci-desc">Board or commission ruling upcoming</div></span></label>
      <label class="check-item"><input type="checkbox" name="trigger" value="disintermediation">
        <span><div class="ci-label">Disintermediation</div><div class="ci-desc">Introducer bypassing TCIP risk</div></span></label>
      <label class="check-item"><input type="checkbox" name="trigger" value="process_deadline">
        <span><div class="ci-label">Process Deadline</div><div class="ci-desc">Bake-off or bid deadline</div></span></label>
      <label class="check-item"><input type="checkbox" name="trigger" value="relationship_tension">
        <span><div class="ci-label">Relationship Tension</div><div class="ci-desc">Key counterparty tension</div></span></label>
      <label class="check-item"><input type="checkbox" name="trigger" value="market_news">
        <span><div class="ci-label">Market News</div><div class="ci-desc">News affecting the thesis</div></span></label>
    </div>
  </div>

  <div class="tcip-card">
    <h2>Rules <span style="font-weight:400;text-transform:none;letter-spacing:0;font-size:11px;color:var(--ink-ghost)">(optional — shapes how Claude analyses the deal)</span></h2>
    <div class="check-grid">
      <label class="check-item"><input type="checkbox" name="rule" value="regulated_asset">
        <span><div class="ci-label">Regulated Asset</div><div class="ci-desc">Rate cases, FERC/state regulation governs returns</div></span></label>
      <label class="check-item"><input type="checkbox" name="rule" value="development_asset">
        <span><div class="ci-label">Development Asset</div><div class="ci-desc">Permits, timelines, probabilistic approvals</div></span></label>
      <label class="check-item"><input type="checkbox" name="rule" value="relationship_sensitive">
        <span><div class="ci-label">Relationship Sensitive</div><div class="ci-desc">Fee confidentiality, attribution flags</div></span></label>
    </div>
  </div>

  <button class="tcip-run-btn" id="tcip-run-btn" onclick="tcipRun()">Run Onboarding</button>

  <div id="tcip-terminal">
    <div id="tcip-term-header">
      <div id="tcip-term-dot"></div>
      <span id="tcip-term-label">tcip_new_deal.py</span>
    </div>
    <div id="tcip-term-body"></div>
  </div>
  <div id="tcip-result"></div>
</div>

<script>
const nameEl    = document.getElementById('f-name');
const idEl      = document.getElementById('f-id');
const leadEl    = document.getElementById('f-lead');
const supportEl = document.getElementById('f-support');

// Auto-generate deal ID from name
nameEl.addEventListener('input', () => {
  const slug = nameEl.value.trim()
    .toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '');
  idEl.value = slug;
});

// Keep lead/support in sync
leadEl.addEventListener('change', () => {
  supportEl.value = '';
});

function extractDriveFolderId(raw) {
  raw = raw.trim();
  const m = raw.match(/\/folders\/([a-zA-Z0-9_-]+)/);
  if (m) return m[1];
  if (/^[a-zA-Z0-9_-]{20,}$/.test(raw)) return raw;
  return raw;
}

let evtSource = null;

function tcipRun() {
  const dealName = nameEl.value.trim();
  const dealId   = idEl.value.trim();
  if (!dealName || !dealId) { alert('Deal name and Deal ID are required.'); return; }

  const triggers = [...document.querySelectorAll('input[name=trigger]:checked')].map(e => e.value);
  const rules    = [...document.querySelectorAll('input[name=rule]:checked')].map(e => e.value);
  const driveRaw = document.getElementById('f-drive').value.trim();
  const driveId  = driveRaw ? extractDriveFolderId(driveRaw) : '';
  const docsPath = document.getElementById('f-docs').value.trim();

  const btn = document.getElementById('tcip-run-btn');
  btn.disabled = true;
  btn.textContent = 'Running…';

  const term     = document.getElementById('tcip-terminal');
  const termBody = document.getElementById('tcip-term-body');
  const result   = document.getElementById('tcip-result');
  term.style.display  = 'block';
  result.style.display = 'none';
  termBody.textContent = '';

  // Open SSE stream first so we don't miss early lines
  if (evtSource) { evtSource.close(); evtSource = null; }
  evtSource = new EventSource('/tcip/stream');
  evtSource.onmessage = (e) => {
    if (e.data === '__DONE__') {
      evtSource.close(); evtSource = null;
      return;
    }
    const line = e.data;
    const span = document.createElement('span');
    if (line.startsWith('✓') || line.startsWith('SUCCESS') || line.includes('✅'))
      span.className = 'term-ok';
    else if (line.startsWith('❌') || line.startsWith('Error') || line.toLowerCase().includes('error'))
      span.className = 'term-err';
    else if (line.startsWith('═') || line.startsWith('─') || line.startsWith('PHASE') || line.startsWith('Phase'))
      span.className = 'term-head';
    span.textContent = line + '\n';
    termBody.appendChild(span);
    termBody.scrollTop = termBody.scrollHeight;
  };
  evtSource.onerror = () => { evtSource.close(); evtSource = null; };

  // POST to kick off the job
  fetch('/tcip/onboard', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ dealName, dealId,
      lead: leadEl.value, support: supportEl.value,
      driveId, docsPath, triggers, rules })
  })
  .then(r => r.json())
  .then(data => {
    if (!data.ok) {
      btn.disabled = false; btn.textContent = 'Run Onboarding';
      result.className = 'result-err'; result.style.display = 'block';
      result.textContent = data.error || 'Failed to start job.';
    }
    // SSE handles the rest; poll /tcip/status for final exit code
    pollResult();
  })
  .catch(err => {
    btn.disabled = false; btn.textContent = 'Run Onboarding';
    result.className = 'result-err'; result.style.display = 'block';
    result.textContent = 'Request failed: ' + err;
  });
}

function pollResult() {
  setTimeout(() => {
    fetch('/tcip/status').then(r => r.json()).then(s => {
      if (s.running) { pollResult(); return; }
      const btn    = document.getElementById('tcip-run-btn');
      const result = document.getElementById('tcip-result');
      btn.disabled = false;
      btn.textContent = 'Run Onboarding';
      if (s.exitCode === 0) {
        const dealId = document.getElementById('f-id').value.trim();
        result.className = 'result-ok'; result.style.display = 'block';
        result.innerHTML = '✓ Onboarding complete. <a href="/tcip/result/' + dealId + '" style="color:var(--navy);font-weight:700">View next steps & project instructions →</a>';
      } else if (s.exitCode !== null) {
        result.className = 'result-err'; result.style.display = 'block';
        result.textContent = '✗ Script exited with errors (code ' + s.exitCode + '). Review terminal output above.';
      } else {
        pollResult();
      }
    }).catch(() => pollResult());
  }, 2000);
}
</script>
</body>
</html>"""
        # Build principal/team options dynamically from firm_context so no
        # tenant names are hardcoded in source.
        _p_name = str(((_FC_CTX or {}).get('principal') or {}).get('name', 'Principal')).strip()
        _team_members = (_FC_CTX or {}).get('team', [])
        _all_members = [_p_name] + [
            str(m.get('name', '')).strip()
            for m in _team_members
            if str(m.get('name', '')).strip() and str(m.get('name', '')).strip() != _p_name
        ]
        _lead_opts = '\n          '.join(
            f'<option value="{n}"{"  selected" if i == 0 else ""}>{n}</option>'
            for i, n in enumerate(_all_members)
        )
        _support_opts = '\n          '.join(
            f'<option value="{n}">{n}</option>'
            for n in _all_members
        )
        html = html.replace('__LEAD_OPTIONS__', _lead_opts)
        html = html.replace('__SUPPORT_OPTIONS__', _support_opts)
        self._serve_html(html, inject_chrome=True, user=user)

    def _handle_tcip_stream(self):
        """GET /tcip/stream — SSE: streams stdout lines from the running tcip job.
        Late-joining clients receive buffered lines already captured, then live ones."""
        global _tcip_lines
        q = queue.Queue(maxsize=500)
        with _tcip_sse_lock:
            _tcip_sse_queues.append(q)

        self.send_response(200)
        self.send_header('Content-Type',  'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Connection',    'keep-alive')
        self.end_headers()

        # Replay already-captured lines for late joiners
        with _tcip_lock:
            replay = list(_tcip_lines)
            finished = not _tcip_running and _tcip_exit_code is not None
        try:
            for line in replay:
                self.wfile.write(f'data: {line}\n\n'.encode())
            if finished:
                self.wfile.write(b'data: __DONE__\n\n')
            self.wfile.flush()

            if not finished:
                while True:
                    try:
                        msg = q.get(timeout=25)
                        self.wfile.write(f'data: {msg}\n\n'.encode())
                        self.wfile.flush()
                        if msg == '__DONE__':
                            break
                    except queue.Empty:
                        self.wfile.write(b': heartbeat\n\n')
                        self.wfile.flush()
        except Exception:
            pass
        finally:
            with _tcip_sse_lock:
                if q in _tcip_sse_queues:
                    _tcip_sse_queues.remove(q)

    def _handle_tcip_result(self, deal_id: str, user: str = 'owner'):
        """GET /tcip/result/<deal_id> — show Drive IDs, copyable instructions, manual steps."""
        import re as _re
        if not _re.match(r'^[a-z][a-z0-9_]*$', deal_id):
            self.send_json(400, {'error': 'invalid deal_id'}); return

        tcip_data_dir = TCIP_SCRIPT.parent / 'data' / 'project-sync' / deal_id
        instr_path    = tcip_data_dir / 'project_instructions.txt'
        sync_path     = TCIP_SCRIPT.parent / 'sync-state.json'

        instructions = ''
        if instr_path.exists():
            instructions = instr_path.read_text(encoding='utf-8')

        # Pull Drive IDs from sync-state.json if present
        status_id = brief_id = drive_url = ''
        if sync_path.exists():
            try:
                ss = json.loads(sync_path.read_text())
                deals = ss if isinstance(ss, dict) else {}
                d = deals.get(deal_id) or deals.get('deals', {}).get(deal_id, {})
                status_id = d.get('status_file_id', '')
                brief_id  = d.get('brief_file_id', d.get('master_brief_id', ''))
                drive_url = d.get('drive_folder_url', d.get('drive_folder_id', ''))
            except Exception:
                pass

        # Also try deal-system-data.json
        dsd_path = TCIP_SCRIPT.parent / 'deal-system-data.json'
        if (not status_id) and dsd_path.exists():
            try:
                dsd = json.loads(dsd_path.read_text())
                for d in dsd.get('deals', []):
                    if d.get('deal_id') == deal_id:
                        status_id = d.get('status_file_id', '')
                        brief_id  = d.get('brief_file_id', d.get('master_brief_id', ''))
                        drive_url = d.get('drive_folder_url', d.get('drive_folder_id', ''))
                        break
            except Exception:
                pass

        def drive_link(fid, label):
            if not fid: return f'<span style="color:var(--ink-ghost)">not found</span>'
            url = f'https://docs.google.com/document/d/{fid}/edit'
            return f'<a href="{url}" target="_blank" style="color:var(--navy);font-family:var(--font-data);font-size:12px">{fid}</a> <span style="color:var(--ink-ghost);font-size:11px">({label})</span>'

        drive_folder_html = ''
        if drive_url:
            if not drive_url.startswith('http'):
                drive_url = f'https://drive.google.com/drive/folders/{drive_url}'
            drive_folder_html = f'<a href="{drive_url}" target="_blank" style="color:var(--navy)">Open Drive folder →</a>'

        instr_escaped = instructions.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
        instr_block = (f'<pre id="instr-text" style="margin:0;white-space:pre-wrap;word-break:break-word;'
                       f'font-size:12px;line-height:1.6;color:var(--ink)">{instr_escaped}</pre>'
                       if instructions else
                       '<p style="color:var(--ink-faint);font-size:13px">project_instructions.txt not found — '
                       'check that the onboarding script completed Phase 7 successfully.</p>')

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TCIP — {deal_id} — Next Steps</title>
<link rel="stylesheet" href="/static/design-system.css">
<style>
  body {{ background:var(--bg); color:var(--ink); font-family:var(--font-body); margin:0; }}
  .wrap {{ max-width:780px; margin:0 auto; padding:36px 24px 80px; }}
  h1 {{ font-family:var(--font-display); font-size:22px; font-weight:700; color:var(--navy); margin:0 0 4px; }}
  .sub {{ font-size:13px; color:var(--ink-faint); margin-bottom:28px; }}
  .card {{ background:var(--paper); border:1px solid var(--rule); border-radius:var(--radius-md);
           padding:24px 28px; margin-bottom:20px; box-shadow:var(--shadow-card); }}
  .card h2 {{ font-family:var(--font-display); font-size:13px; font-weight:700; color:var(--navy);
              text-transform:uppercase; letter-spacing:.06em; margin:0 0 16px;
              border-bottom:1px solid var(--rule-light); padding-bottom:10px; }}
  .id-row {{ display:flex; align-items:center; gap:10px; margin-bottom:10px; }}
  .id-label {{ font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:.05em;
               color:var(--ink-mid); min-width:100px; }}
  .step {{ display:flex; gap:14px; margin-bottom:18px; }}
  .step-num {{ flex-shrink:0; width:26px; height:26px; border-radius:50%;
               background:var(--navy); color:var(--white); font-size:12px; font-weight:700;
               display:flex; align-items:center; justify-content:center; margin-top:1px; }}
  .step-body {{ flex:1; }}
  .step-body strong {{ font-size:14px; color:var(--ink); display:block; margin-bottom:4px; }}
  .step-body p {{ font-size:13px; color:var(--ink-mid); margin:0 0 6px; line-height:1.5; }}
  .step-body a {{ color:var(--navy); font-weight:600; }}
  .instr-box {{ background:var(--white); border:1px solid var(--rule); border-radius:var(--radius-sm);
                padding:16px; max-height:380px; overflow-y:auto; position:relative; }}
  .copy-btn {{ background:var(--navy); color:var(--white); border:none; border-radius:var(--radius-sm);
               padding:7px 16px; font-size:12px; font-weight:600; font-family:var(--font-body);
               cursor:pointer; letter-spacing:.03em; }}
  .copy-btn:hover {{ background:var(--navy-mid); }}
  .copied {{ background:var(--green) !important; }}
  .success-banner {{ background:var(--green-bg); border:1px solid var(--green-bd); color:var(--green);
                     border-radius:var(--radius-md); padding:12px 18px; font-size:13px;
                     font-weight:600; margin-bottom:20px; }}
</style>
</head>
<body>
{{{{TOPNAV}}}}
<div class="wrap">
  <div class="success-banner">✓ Onboarding complete for <strong>{deal_id}</strong>. Four manual steps remain — all browser-only, ~10 minutes.</div>
  <h1>Next Steps</h1>
  <p class="sub">Automated phases 1–7 are done. Complete the steps below to finish wiring the deal.</p>

  <div class="card">
    <h2>Files Created</h2>
    <div class="id-row"><span class="id-label">Status.md</span>{drive_link(status_id, 'status file')}</div>
    <div class="id-row"><span class="id-label">Master Brief</span>{drive_link(brief_id, 'master brief')}</div>
    <div class="id-row"><span class="id-label">Drive Folder</span>{drive_folder_html or '<span style=\"color:var(--ink-ghost)\">see terminal output</span>'}</div>
  </div>

  <div class="card">
    <h2>Step 1 — Create the Claude Project</h2>
    <div class="step">
      <div class="step-num">1</div>
      <div class="step-body">
        <strong>Open Claude → Projects → New Project</strong>
        <p>Name it exactly: <code style="background:var(--navy-light);padding:2px 6px;border-radius:2px;font-family:var(--font-data)">TCIP — {deal_id}</code></p>
        <a href="https://claude.ai/projects" target="_blank">claude.ai/projects →</a>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>Step 2 — Upload Knowledge Base File</h2>
    <div class="step">
      <div class="step-num">2</div>
      <div class="step-body">
        <strong>In the new Project → Files → click +</strong>
        <p>Upload <code style="background:var(--navy-light);padding:2px 6px;border-radius:2px;font-family:var(--font-data)">tcip_firm_context.md</code> — one file only.</p>
        <p style="font-size:12px;color:var(--ink-ghost)">File is at: ~/Downloads/tcip_firm_context.md (or ~/Downloads/files/tcip_firm_context.md)</p>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>Step 3 — Paste Project Instructions</h2>
    <div class="step">
      <div class="step-num">3</div>
      <div class="step-body">
        <strong>In the Project → Instructions → click + → paste everything below → Save</strong>
        <p style="margin-bottom:12px">Copy the full block, then paste into the Claude Project instructions field.</p>
        <div style="display:flex;justify-content:flex-end;margin-bottom:8px">
          <button class="copy-btn" id="copy-btn" onclick="copyInstructions()">Copy Instructions</button>
        </div>
        <div class="instr-box">{instr_block}</div>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>Step 4 — Wire Project to Dashboard</h2>
    <div class="step">
      <div class="step-num">4</div>
      <div class="step-body">
        <strong>Grab the Project URL from claude.ai after Step 1</strong>
        <p>Copy the project ID from the URL (the part after <code style="font-family:var(--font-data)">/project/</code>), then run this in Terminal:</p>
        <div style="background:#0f1e38;border-radius:var(--radius-sm);padding:12px 14px;margin-top:8px;font-family:var(--font-data);font-size:11px;color:#c8d8e8;white-space:pre-wrap">curl -X POST http://localhost:7777/project-sync/update \\
  -H "Content-Type: application/json" \\
  -d '{{{{
    "deal_id": "{deal_id}",
    "project_url": "https://claude.ai/project/YOUR-PROJECT-ID",
    "last_session_date": "{datetime.now().strftime('%Y-%m-%d')}",
    "session_summary": "Initial setup complete"
  }}}}'</div>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>Step 5 — First Session</h2>
    <div class="step">
      <div class="step-num">5</div>
      <div class="step-body">
        <strong>Open the new Project in Claude</strong>
        <p>Drop in any deal documents. Say: <em>"New deal document. Run the critical driver framework."</em></p>
        <p>Review and confirm the output. Say <em>"session close"</em> at end of session.</p>
      </div>
    </div>
  </div>

  <p style="margin-top:8px"><a href="/tcip/" style="color:var(--navy);font-size:13px">← Onboard another deal</a></p>
</div>
<script>
function copyInstructions() {{
  const text = document.getElementById('instr-text');
  if (!text) return;
  navigator.clipboard.writeText(text.textContent).then(() => {{
    const btn = document.getElementById('copy-btn');
    btn.textContent = 'Copied!';
    btn.classList.add('copied');
    setTimeout(() => {{ btn.textContent = 'Copy Instructions'; btn.classList.remove('copied'); }}, 2000);
  }});
}}
</script>
</body>
</html>"""
        self._serve_html(html, inject_chrome=True, user=user)

    def _handle_tcip_onboard(self):
        """POST /tcip/onboard — validate form JSON, spawn tcip_new_deal.py, stream output via SSE."""
        global _tcip_running, _tcip_lines, _tcip_exit_code, _tcip_started_at

        body = self._read_json_body() or {}
        deal_name = str(body.get('dealName') or '').strip()
        deal_id   = str(body.get('dealId')   or '').strip()
        lead      = str(body.get('lead')      or ((_FC_CTX or {}).get('principal') or {}).get('name', 'Principal')).strip()
        support   = str(body.get('support')   or '').strip()
        drive_id  = str(body.get('driveId')   or '').strip()
        docs_path = str(body.get('docsPath')  or '').strip()
        triggers  = [str(t) for t in (body.get('triggers') or []) if t]
        rules     = [str(r) for r in (body.get('rules')    or []) if r]

        if not deal_name or not deal_id:
            self.send_json(400, {'ok': False, 'error': 'dealName and dealId are required'}); return

        import re as _re
        if not _re.match(r'^[a-z][a-z0-9_]*$', deal_id):
            self.send_json(400, {'ok': False, 'error': 'dealId must be lowercase letters, digits, underscores'}); return

        if not TCIP_SCRIPT.exists():
            self.send_json(503, {'ok': False, 'error': f'tcip_new_deal.py not found at {TCIP_SCRIPT}'}); return

        with _tcip_lock:
            if _tcip_running:
                self.send_json(409, {'ok': False, 'error': 'An onboarding job is already running'}); return
            _tcip_running   = True
            _tcip_lines     = []
            _tcip_exit_code = None
            _tcip_started_at = datetime.now().isoformat()

        self.send_json(202, {'ok': True, 'status': 'started', 'dealId': deal_id})

        def _run():
            global _tcip_running, _tcip_exit_code
            cmd = [sys.executable, str(TCIP_SCRIPT), '--deal-name', deal_name,
                   '--deal-id', deal_id, '--lead', lead]
            if support:
                cmd += ['--support', support]
            if drive_id:
                cmd += ['--drive-folder-id', drive_id]
            if docs_path:
                cmd += ['--docs-folder', docs_path]
            if triggers:
                cmd += ['--triggers'] + triggers
            if rules:
                cmd += ['--rules'] + rules

            env = os.environ.copy()
            env['PYTHONUNBUFFERED'] = '1'
            cred_link = TCIP_SCRIPT.parent / 'credentials.json'
            cred_src  = Path.home() / 'credentials' / 'gdrive_credentials.json'
            if not cred_link.exists() and cred_src.exists():
                try: cred_link.symlink_to(cred_src)
                except Exception: pass

            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    env=env, cwd=str(TCIP_SCRIPT.parent), text=True, bufsize=1,
                )
                for line in proc.stdout:
                    line = line.rstrip('\n')
                    with _tcip_lock:
                        _tcip_lines.append(line)
                    _tcip_broadcast(line)
                proc.wait()
                exit_code = proc.returncode
            except Exception as e:
                err = f'❌ Failed to start script: {e}'
                with _tcip_lock:
                    _tcip_lines.append(err)
                _tcip_broadcast(err)
                exit_code = 1

            with _tcip_lock:
                _tcip_running   = False
                _tcip_exit_code = exit_code
            _tcip_broadcast('__DONE__')

        threading.Thread(target=_run, daemon=True, name='tcip-onboard').start()

    def _parse_form(self):
        length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(length).decode('utf-8', errors='replace')
        from urllib.parse import parse_qs
        parsed = parse_qs(raw, keep_blank_values=True)
        return {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}

    def _read_json_body(self):
        length = int(self.headers.get('Content-Length') or 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode('utf-8', errors='replace') or '{}')
        except Exception:
            return None

    def _handle_dismiss_completion_signals(self):
        """POST /api/followup/dismiss-completion-signals — clear
        _possibly_completed[] on one followUp. Used by the UI "× Dismiss signal"
        button on the commitment-pair chip when the ack was unrelated."""
        body = self._read_json_body()
        if body is None:
            self.send_json(400, {'ok': False, 'error': 'invalid JSON'}); return
        item_id = str(body.get('id') or '').strip()
        if not item_id:
            self.send_json(400, {'ok': False, 'error': 'id required'}); return
        if not STATE_PATH.exists():
            self.send_json(500, {'ok': False, 'error': 'dashboard-data missing'}); return
        try:
            data = json.loads(STATE_PATH.read_text())
        except Exception as e:
            self.send_json(500, {'ok': False, 'error': f'read failed: {e}'}); return
        found = False
        for fu in data.get('followUps', []):
            # actionKey is the JS-side fkey — usually fu.id; match either way.
            if fu.get('id') == item_id or (fu.get('id') or '') == item_id:
                if fu.get('_possibly_completed'):
                    fu['_possibly_completed'] = []
                    found = True
                break
        if not found:
            self.send_json(404, {'ok': False, 'error': 'followup not found or no signals'}); return
        tmp = STATE_PATH.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        tmp.replace(STATE_PATH)
        self.send_json(200, {'ok': True, 'id': item_id})

    def _handle_dismiss_sms_signals(self):
        """POST /api/awaiting/dismiss-sms-signals — clear _sms_signals[] on one
        awaiting item. Used by the UI "× Dismiss signal" button when an SMS
        match was a false positive (real SMS arrived but didn't actually
        resolve the awaiting item). The item itself stays open."""
        body = self._read_json_body()
        if body is None:
            self.send_json(400, {'ok': False, 'error': 'invalid JSON'}); return
        item_id = str(body.get('id') or '').strip()
        if not item_id:
            self.send_json(400, {'ok': False, 'error': 'id required'}); return
        if not STATE_PATH.exists():
            self.send_json(500, {'ok': False, 'error': 'dashboard-data missing'}); return
        try:
            data = json.loads(STATE_PATH.read_text())
        except Exception as e:
            self.send_json(500, {'ok': False, 'error': f'read failed: {e}'}); return
        found = False
        for it in data.get('awaitingExternal', []):
            if it.get('id') == item_id:
                if it.get('_sms_signals'):
                    it['_sms_signals'] = []
                    found = True
                break
        if not found:
            self.send_json(404, {'ok': False, 'error': 'item not found or no signals'}); return
        tmp = STATE_PATH.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        tmp.replace(STATE_PATH)
        self.send_json(200, {'ok': True, 'id': item_id})

    def _handle_item_delete(self):
        body = self._read_json_body()
        if body is None:
            self.send_json(400, {'ok': False, 'error': 'invalid JSON'}); return
        item_id = str(body.get('id') or '').strip()
        source  = str(body.get('source') or '').strip()
        context = str(body.get('context') or '').strip()[:240]
        if not item_id or not source:
            self.send_json(400, {'ok': False, 'error': 'id and source required'}); return
        data = _load_deletions()
        dels = data.setdefault('deletions', [])
        if not any(d.get('id') == item_id for d in dels):
            dels.append({
                'id': item_id,
                'source': source,
                'context': context,
                'deleted_at': datetime.utcnow().isoformat(timespec='seconds') + 'Z',
            })
            _save_deletions(data)
        self.send_json(200, {'ok': True, 'count': len(dels), 'id': item_id})

    def _handle_topics_save(self):
        body = self._read_json_body()
        if body is None:
            self.send_json(400, {'ok': False, 'error': 'invalid JSON'}); return
        content = str(body.get('content') or '')
        if len(content) > 20000:
            self.send_json(400, {'ok': False, 'error': 'content too long (20k max)'}); return
        payload = _save_topics(content)
        self.send_json(200, {'ok': True, 'updated_at': payload['updated_at']})

    def _handle_order_save(self):
        body = self._read_json_body()
        if body is None:
            self.send_json(400, {'ok': False, 'error': 'invalid JSON'}); return
        column = str(body.get('column') or '').strip()
        order  = body.get('order')
        if not column or not isinstance(order, list):
            self.send_json(400, {'ok': False, 'error': 'column and order required'}); return
        data = _load_order()
        data[column] = [str(x) for x in order][:500]
        _save_order(data)
        self.send_json(200, {'ok': True, 'column': column, 'count': len(data[column])})

    # ── Proposed-learnings tile (Accept / Reject / Defer) ───────
    # Three POST endpoints feed the dashboard "Proposed Learnings" tile.
    # The candidates themselves are captured by run_learning_capture_scan()
    # in dash-state-hook.py → ~/dashboards/data/compiled/proposed-learnings.jsonl
    # and loaded into the dashboard DATA dict by _load_proposed_learnings()
    # in cos-dashboard-refresh.py. These handlers persist the user's verdict
    # to user-state files so the tile filters them on the next render.
    #
    # All three are owner-only (the POST dispatcher already enforces auth
    # tier above). State files are JSON dicts, not JSONL — the tile only
    # needs the latest verdict per id, not a history.

    def _handle_learning_accept(self):
        """Promote a candidate to the /propose-learning review queue.
        Does NOT write to LEARNINGS-LEDGER.yaml directly — that file is
        canonical and edited via the interactive skill so the principal gets to
        author the structured fields (rule_code, domain, confidence, etc).
        We just append the snippet to a queue the skill picks up first.
        """
        body = self._read_json_body()
        if body is None:
            self.send_json(400, {'ok': False, 'error': 'invalid JSON'}); return
        lid = str(body.get('id') or '').strip()
        snippet = str(body.get('snippet') or '').strip()
        if not lid or not snippet:
            self.send_json(400, {'ok': False, 'error': 'id and snippet required'}); return
        _ensure_user_state_dir()
        queue_path = Path.home() / 'dashboards' / 'data' / 'user-state' / 'learnings-to-promote.jsonl'
        try:
            with open(queue_path, 'a') as fh:
                fh.write(json.dumps({
                    'id': lid,
                    'snippet': snippet,
                    'accepted_at': datetime.utcnow().isoformat(timespec='seconds') + 'Z',
                }) + '\n')
        except Exception as e:
            self.send_json(500, {'ok': False, 'error': f'queue write failed: {e}'}); return
        # Also tombstone in rejected so the tile doesn't re-show until the
        # next ingest. The skill will dequeue + ledger-write asynchronously.
        rej_path = Path.home() / 'dashboards' / 'data' / 'user-state' / 'rejected-learnings.json'
        try:
            cur = json.loads(rej_path.read_text()) if rej_path.exists() else {'ids': []}
        except Exception:
            cur = {'ids': []}
        if lid not in cur['ids']:
            cur['ids'].append(lid)
            try:
                rej_path.write_text(json.dumps(cur, indent=2))
            except Exception:
                pass
        self.send_json(200, {'ok': True, 'queued': True, 'id': lid})

    def _handle_learning_reject(self):
        """Tombstone a candidate — never re-surface on the tile."""
        body = self._read_json_body()
        if body is None:
            self.send_json(400, {'ok': False, 'error': 'invalid JSON'}); return
        lid = str(body.get('id') or '').strip()
        if not lid:
            self.send_json(400, {'ok': False, 'error': 'id required'}); return
        _ensure_user_state_dir()
        rej_path = Path.home() / 'dashboards' / 'data' / 'user-state' / 'rejected-learnings.json'
        try:
            cur = json.loads(rej_path.read_text()) if rej_path.exists() else {'ids': []}
        except Exception:
            cur = {'ids': []}
        if lid not in cur['ids']:
            cur['ids'].append(lid)
        try:
            rej_path.write_text(json.dumps(cur, indent=2))
        except Exception as e:
            self.send_json(500, {'ok': False, 'error': f'write failed: {e}'}); return
        self.send_json(200, {'ok': True, 'count': len(cur['ids']), 'id': lid})

    def _handle_learning_defer(self):
        """Defer a candidate — falls off the tile for 7 days (default) or
        the explicit `until` date in the body."""
        body = self._read_json_body()
        if body is None:
            self.send_json(400, {'ok': False, 'error': 'invalid JSON'}); return
        lid = str(body.get('id') or '').strip()
        until = str(body.get('until') or '').strip()
        if not lid:
            self.send_json(400, {'ok': False, 'error': 'id required'}); return
        if not until:
            until = (datetime.utcnow() + timedelta(days=7)).strftime('%Y-%m-%d')
        _ensure_user_state_dir()
        def_path = Path.home() / 'dashboards' / 'data' / 'user-state' / 'deferred-learnings.json'
        try:
            cur = json.loads(def_path.read_text()) if def_path.exists() else {'until': {}}
        except Exception:
            cur = {'until': {}}
        cur.setdefault('until', {})[lid] = until
        try:
            def_path.write_text(json.dumps(cur, indent=2))
        except Exception as e:
            self.send_json(500, {'ok': False, 'error': f'write failed: {e}'}); return
        self.send_json(200, {'ok': True, 'until': until, 'id': lid})

    def _handle_item_undelete(self):
        body = self._read_json_body()
        if body is None:
            self.send_json(400, {'ok': False, 'error': 'invalid JSON'}); return
        item_id = str(body.get('id') or '').strip()
        if not item_id:
            self.send_json(400, {'ok': False, 'error': 'id required'}); return
        data = _load_deletions()
        before = len(data.get('deletions', []))
        data['deletions'] = [d for d in data.get('deletions', []) if d.get('id') != item_id]
        _save_deletions(data)
        self.send_json(200, {'ok': True, 'removed': before - len(data['deletions']), 'count': len(data['deletions'])})

    # ── Routines (Claude scheduled-task health) ───────────
    def _handle_system_health_latest(self):
        # Read aggregated system-health JSON written by tools/system_health.py.
        path = Path.home() / 'dashboards' / 'data' / 'system-health' / 'latest.json'
        if not path.exists():
            self.send_json(404, {'error': 'system-health/latest.json not found'})
            return
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            self.send_json(500, {'error': f'unreadable: {exc}'})
            return
        self.send_json(200, payload)

    def _handle_deal_intelligence(self):
        """GET /api/deals/<deal_id>/intelligence?q=<query>&limit=N&since=YYYY-MM-DD"""
        if not _KNOWLEDGE_API_AVAILABLE:
            self.send_json(503, {'error': 'Knowledge index not available'})
            return
        # Parse path: /api/deals/<deal_id>/intelligence
        parts = self.path.split('?', 1)
        path_parts = parts[0].strip('/').split('/')
        # path_parts = ['api', 'deals', '<deal_id>', 'intelligence']
        deal_id = path_parts[2] if len(path_parts) >= 3 else ''
        if not deal_id:
            self.send_json(400, {'error': 'Missing deal_id'}); return

        params: dict[str, str] = {}
        if len(parts) > 1:
            for kv in parts[1].split('&'):
                if '=' in kv:
                    k, v = kv.split('=', 1)
                    params[k] = v.replace('+', ' ').replace('%20', ' ')

        q     = params.get('q') or None
        limit = min(int(params.get('limit', '10')), 25)
        since = params.get('since') or None

        try:
            result = query_for_deal(deal_id, q=q, limit=limit, since=since)
            self.send_json(200, result)
        except Exception as e:
            self.send_json(500, {'error': str(e)})

    def _handle_intel_digest(self):
        """GET /api/intel-digest?week=YYYY-WNN"""
        if not _KNOWLEDGE_API_AVAILABLE:
            self.send_json(503, {'error': 'Knowledge index not available'}); return
        parts = self.path.split('?', 1)
        params: dict[str, str] = {}
        if len(parts) > 1:
            for kv in parts[1].split('&'):
                if '=' in kv:
                    k, v = kv.split('=', 1)
                    params[k] = v
        week = params.get('week') or None
        try:
            self.send_json(200, _kn_digest(week))
        except Exception as e:
            self.send_json(500, {'error': str(e)})

    def _handle_git_status(self):
        """GET /api/git-status — last commit + dirty/ahead/behind for each tracked repo."""
        import subprocess
        repos = [
            {'name': 'cos-pipeline',
             'path': str(Path.home() / 'cos-pipeline'),
             'visibility': 'public'},
            {'name': 'dashboards',
             'path': str(Path.home() / 'dashboards'),
             'visibility': 'private'},
            {'name': f'cos-pipeline-config-{COS_TENANT_SLUG}',
             'path': str(COS_CONFIG_ROOT),
             'visibility': 'private'},
        ]
        results = []
        for repo in repos:
            rp = repo['path']
            info: dict = {
                'name': repo['name'],
                'visibility': repo['visibility'],
            }
            try:
                # Last commit
                r1 = subprocess.run(
                    ['git', '-C', rp, 'log', '-1', '--format=%h\t%s\t%ci'],
                    capture_output=True, text=True, timeout=5)
                if r1.returncode == 0 and r1.stdout.strip():
                    parts = r1.stdout.strip().split('\t', 2)
                    info['last_commit_hash'] = parts[0] if len(parts) > 0 else ''
                    info['last_commit_msg']  = parts[1] if len(parts) > 1 else ''
                    info['last_commit_at']   = parts[2] if len(parts) > 2 else ''
                else:
                    info['last_commit_hash'] = ''
                    info['last_commit_msg']  = '(no commits)'
                    info['last_commit_at']   = ''
                # Uncommitted files
                r2 = subprocess.run(
                    ['git', '-C', rp, 'status', '--porcelain'],
                    capture_output=True, text=True, timeout=5)
                dirty_lines = [l for l in r2.stdout.splitlines() if l.strip()]
                info['dirty_count'] = len(dirty_lines)
                info['dirty_files'] = [l.strip() for l in dirty_lines[:10]]
                # Ahead / behind remote
                r3 = subprocess.run(
                    ['git', '-C', rp, 'rev-list', '--count', '--left-right', 'HEAD...@{u}'],
                    capture_output=True, text=True, timeout=5)
                if r3.returncode == 0 and r3.stdout.strip():
                    ab = r3.stdout.strip().split()
                    info['ahead']  = int(ab[0]) if len(ab) > 0 else 0
                    info['behind'] = int(ab[1]) if len(ab) > 1 else 0
                else:
                    info['ahead'] = 0
                    info['behind'] = 0
            except Exception as exc:
                info['error'] = str(exc)
            results.append(info)
        self.send_json(200, {
            'repos': results,
            'checked_at': datetime.now().isoformat(),
        })

    def _handle_run_health_check(self):
        """POST /api/run-health-check — run system_health.py, return fresh results."""
        import subprocess
        script = Path.home() / 'cos-pipeline' / 'tools' / 'system_health.py'
        if not script.exists():
            self.send_json(404, {'error': 'system_health.py not found'})
            return
        try:
            proc = subprocess.run(
                [sys.executable, str(script)],
                capture_output=True, text=True, timeout=120,
            )
            summary = (proc.stdout or '').strip().splitlines()[-1] \
                if (proc.stdout or '').strip() else ''
            latest_path = Path.home() / 'dashboards' / 'data' / 'system-health' / 'latest.json'
            payload: dict = {}
            if latest_path.exists():
                try:
                    payload = json.loads(latest_path.read_text(encoding='utf-8'))
                except Exception:
                    pass
            payload['summary_line'] = summary
            payload['exit_code'] = proc.returncode
            self.send_json(200, payload)
        except subprocess.TimeoutExpired:
            self.send_json(500, {'error': 'health check timed out after 120s'})
        except Exception as exc:
            self.send_json(500, {'error': str(exc)})

    def _handle_routines_list(self):
        self.send_json(200, {'routines': _routines_data()})

    def _handle_routines_health(self):
        self.send_json(200, _routines_health())

    def _handle_routines_log(self):
        # /routines/<task>/log?type=stdout|stderr|run&lines=200
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        segs = parsed.path.split('/')
        if len(segs) != 4 or segs[3] != 'log':
            self.send_json(400, {'error': 'invalid path'}); return
        task = segs[2]
        if not ROUTINES_TASK_RE.match(task):
            self.send_json(400, {'error': 'invalid task name'}); return
        if not (ROUTINES_LAUNCHAGENTS / f'{ROUTINES_LABEL_PREFIX}{task}.plist').exists():
            self.send_json(404, {'error': 'unknown task'}); return
        qs = parse_qs(parsed.query)
        log_type = (qs.get('type', ['stdout'])[0] or 'stdout').lower()
        if log_type not in ('stdout', 'stderr', 'run'):
            log_type = 'stdout'
        try:
            lines = int(qs.get('lines', ['200'])[0])
        except (ValueError, TypeError):
            lines = 200
        lines = max(1, min(lines, 2000))
        # Resolve to the plist's actual log stem, since historical plists wrote
        # to filenames different from their task labels.
        meta = _routines_parse_plist(task)
        stem = meta.get('log_stem') or task
        log_path = ROUTINES_LOG_DIR / f'{stem}.{log_type}.log'
        text = _routines_tail_log(log_path, max_bytes=512 * 1024)
        tail = '\n'.join(text.splitlines()[-lines:])
        payload = tail.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(payload)

    # ── Deal-tile manual overrides ─────────────────────────
    # User edits of potential_partner / deck_url / model_url on a deal
    # tile persist to data/user-state/deal-overrides.json so they survive
    # the next pipeline run (Operating Principle #1: user state is
    # sacred). After write, kicks compile-deals so the new value flows
    # into the live JSON without a manual refresh.
    def _handle_deal_override(self):
        body = self._read_json_body()
        if body is None:
            self.send_json(400, {'ok': False, 'error': 'invalid JSON'}); return
        ticker = str(body.get('ticker') or '').strip().lower()
        field  = str(body.get('field') or '').strip()
        value  = body.get('value')
        if not ticker or not re.match(r'^[a-z0-9][a-z0-9_-]{0,32}$', ticker):
            self.send_json(400, {'ok': False, 'error': 'invalid ticker'}); return
        if field not in ('potential_partner', 'deck_url', 'model_url'):
            self.send_json(400, {'ok': False, 'error': 'invalid field'}); return
        # Coerce value: empty string → null; trim whitespace; cap length.
        if value is None:
            v = None
        else:
            v = str(value).strip()
            if not v:
                v = None
            elif len(v) > 1000:
                v = v[:1000]
        # Light URL sanity check on deck_url/model_url so we don't store
        # markup. Reject control chars; otherwise accept any string the
        # user pastes in (URL, drive path, file://, relative).
        if v and field in ('deck_url', 'model_url'):
            if any(ord(c) < 32 for c in v):
                self.send_json(400, {'ok': False, 'error': 'invalid url'}); return
        path = _DEAL_OVERRIDES_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        with _DEAL_OVERRIDES_LOCK:
            try:
                cur = json.loads(path.read_text()) if path.exists() else {}
                if not isinstance(cur, dict):
                    cur = {}
            except (json.JSONDecodeError, OSError):
                cur = {}
            row = cur.get(ticker) or {}
            row[field] = v
            row['_updated_at'] = datetime.utcnow().isoformat(timespec='seconds') + 'Z'
            cur[ticker] = row
            path.write_text(json.dumps(cur, indent=2, sort_keys=True))
        # Trigger compile to push the override into deal-system-data.json
        # in the background (~1s). Non-blocking — the UI optimistically
        # treats the POST 200 as a success and re-fetches /dashboard-data.json.
        try:
            subprocess.Popen(
                [sys.executable, str(Path(__file__).parent.parent / 'routines' / 'compile' / 'deal-system-compile.py')],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
        self.send_json(200, {
            'ok':     True,
            'ticker': ticker,
            'field':  field,
            'value':  v,
        })

    def _handle_deal_workstream(self):
        """POST /deal/workstream — add, edit, delete, or toggle_status on a workstream.
        Writes directly to deal.md YAML frontmatter and triggers a background recompile."""
        import yaml as _yaml
        body = self._read_json_body()
        if body is None:
            self.send_json(400, {'ok': False, 'error': 'invalid JSON'}); return
        ticker = re.sub(r'[^a-z0-9_-]', '', str(body.get('ticker') or '').strip().lower())
        action = str(body.get('action') or '').strip()
        ws_in  = body.get('workstream') or {}
        if not ticker or action not in ('add', 'edit', 'delete', 'toggle_status'):
            self.send_json(400, {'ok': False, 'error': 'invalid ticker or action'}); return
        deal_path = _ROOT / 'data' / 'deals' / ticker / 'deal.md'
        if not deal_path.exists():
            self.send_json(404, {'ok': False, 'error': 'deal not found'}); return
        text = deal_path.read_text()
        m = re.match(r'^---\n(.*?)\n---\n(.*)$', text, re.DOTALL)
        if not m:
            self.send_json(500, {'ok': False, 'error': 'malformed deal.md'}); return
        try:
            fm = _yaml.safe_load(m.group(1)) or {}
        except Exception as e:
            self.send_json(500, {'ok': False, 'error': f'yaml parse error: {e}'}); return
        body_text = m.group(2)
        workstreams = fm.get('workstreams') or []
        if not isinstance(workstreams, list):
            workstreams = []
        STATUS_CYCLE = ['not-started', 'in-progress', 'done']
        if action == 'add':
            title = str(ws_in.get('title') or '').strip()[:200]
            if not title:
                self.send_json(400, {'ok': False, 'error': 'title required'}); return
            workstreams.append({
                'id': f"ws-{int(time.time() * 1000) % 1_000_000_000}",
                'title': title,
                'owner': str(ws_in.get('owner') or '').strip()[:80],
                'status': 'not-started',
                'note': str(ws_in.get('note') or '').strip()[:1000],
            })
        elif action in ('edit', 'toggle_status', 'delete'):
            ws_id = str(ws_in.get('id') or '').strip()
            if not ws_id:
                self.send_json(400, {'ok': False, 'error': 'workstream id required'}); return
            if action == 'delete':
                workstreams = [w for w in workstreams if w.get('id') != ws_id]
            else:
                for ws in workstreams:
                    if ws.get('id') != ws_id:
                        continue
                    if action == 'toggle_status':
                        cur = ws.get('status', 'not-started')
                        idx = STATUS_CYCLE.index(cur) if cur in STATUS_CYCLE else 0
                        ws['status'] = STATUS_CYCLE[(idx + 1) % len(STATUS_CYCLE)]
                    else:  # edit
                        for k in ('title', 'owner', 'note'):
                            if k in ws_in:
                                ws[k] = str(ws_in[k] or '').strip()[:({'title':200,'owner':80,'note':1000}[k])]
                    break
        fm['workstreams'] = workstreams
        new_yaml = _yaml.dump(fm, default_flow_style=False, allow_unicode=True, sort_keys=False)
        deal_path.write_text(f'---\n{new_yaml}---\n{body_text}')
        try:
            subprocess.Popen(
                [sys.executable, COMPILE_SCRIPT],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
        self.send_json(200, {'ok': True, 'workstreams': workstreams})

    def _handle_routines_kickstart(self):
        # POST /routines/<task>/kickstart  body: {"confirm":"yes"}
        segs = self.path.split('?')[0].split('/')
        if len(segs) != 4 or segs[3] != 'kickstart':
            self.send_json(400, {'kicked': False, 'error': 'invalid path'}); return
        task = segs[2]
        if not ROUTINES_TASK_RE.match(task):
            self.send_json(400, {'kicked': False, 'error': 'invalid task name'}); return
        body = self._read_json_body() or {}
        if body.get('confirm') != 'yes':
            self.send_json(400, {'kicked': False, 'error': 'confirmation required: post {"confirm":"yes"}'}); return
        ok, out = _routines_kickstart(task)
        if not ok:
            self.send_json(400, {'kicked': False, 'task': task, 'error': out}); return
        self.send_json(200, {
            'kicked':    True,
            'task':      task,
            'timestamp': datetime.now().isoformat(timespec='seconds'),
            'message':   out,
        })

    def _handle_admin_invite(self):
        form = self._parse_form()
        name          = (form.get('name')          or '').strip()
        email         = (form.get('email')         or '').strip()
        github_handle = (form.get('github_handle') or '').strip().lstrip('@')
        role          = (form.get('role')          or 'viewer').strip()
        tiles = form.get('tile') or []
        if isinstance(tiles, str): tiles = [tiles]
        raw_tab_access = form.get('tab_access') or []
        if isinstance(raw_tab_access, str): raw_tab_access = [raw_tab_access]
        tab_access = {}
        for entry in raw_tab_access:
            if ':' in entry:
                tid, tabid = entry.split(':', 1)
                tab_access.setdefault(tid, []).append(tabid)

        if not name or not email:
            self._redirect_admin(('err', 'Name and email are required.'))
            return

        username = _gen_username(name)
        password = _gen_password()
        user_record = {
            'username':      username,
            'password':      password,
            'name':          name,
            'email':         email,
            'tiles':         tiles,
            'tab_access':    tab_access,
            'role':          role,
            'github_handle': github_handle,
            'created_at':    datetime.utcnow().isoformat() + 'Z',
        }
        users = _load_users()
        users.append(user_record)
        _save_users(users)

        # Grant GitHub access
        github_ok, github_repos = True, []
        if github_handle and role != 'viewer':
            github_ok, github_repos = _grant_github_access(github_handle, role)

        # Resolve tile objects for email
        all_tiles = _load_tiles()
        granted_tiles = [t for t in all_tiles if (t.get('url') or '') in tiles]
        sent = _send_invite_email(name, email, username, password, granted_tiles,
                                  role=role, github_handle=github_handle,
                                  github_repos=github_repos)

        status_parts = [f'{name} invited.']
        if sent:
            status_parts.append(f'Email sent to {email}.')
        else:
            status_parts.append(f'Email failed — share manually: {username} / {password}.')
        if github_handle and role != 'viewer':
            if github_ok:
                status_parts.append(f'GitHub: added @{github_handle} to {len(github_repos)} repo(s).')
            else:
                status_parts.append(f'GitHub: some repo grants failed — check server log.')
        self._redirect_admin(('ok', ' '.join(status_parts)))

    def _handle_admin_update(self):
        form = self._parse_form()
        username = (form.get('username') or '').strip()
        tiles    = form.get('tile') or []
        if isinstance(tiles, str): tiles = [tiles]
        raw_tab_access = form.get('tab_access') or []
        if isinstance(raw_tab_access, str): raw_tab_access = [raw_tab_access]
        tab_access = {}
        for entry in raw_tab_access:
            if ':' in entry:
                tid, tabid = entry.split(':', 1)
                tab_access.setdefault(tid, []).append(tabid)
        users = _load_users()
        updated = False
        for u in users:
            if u.get('username') == username:
                u['tiles']      = tiles
                u['tab_access'] = tab_access
                updated = True
                break
        if updated:
            _save_users(users)
            self._redirect_admin(('ok', f'Access updated for {username}.'))
        else:
            self._redirect_admin(('err', f'User {username} not found.'))

    def _handle_admin_revoke(self):
        form = self._parse_form()
        username = (form.get('username') or '').strip()
        users = [u for u in _load_users() if u.get('username') != username]
        _save_users(users)
        self._redirect_admin(('ok', f'Access revoked for {username}.'))

    def _handle_admin_schedule_dial_in(self):
        import uuid
        from datetime import datetime, timedelta
        form = self._parse_form()
        title       = (form.get('title')       or '').strip()
        phone       = (form.get('phone')       or '').strip()
        pin         = (form.get('pin')         or '').strip()
        access_code = (form.get('access_code') or '').strip()
        platform    = (form.get('platform')    or 'standard').strip()
        date_str    = (form.get('date')        or '').strip()
        time_str    = (form.get('start_time')  or '').strip()
        end_time    = (form.get('end_time')    or '').strip()
        dur_min     = int(form.get('duration_min') or 60)
        category    = (form.get('category')    or 'Other').strip()

        if not title or not date_str or not time_str:
            self._redirect_admin(('err', 'Title, date, and start time are required.'))
            return

        try:
            start_dt = datetime.fromisoformat(f'{date_str}T{time_str}:00')
            if end_time:
                end_dt = datetime.fromisoformat(f'{date_str}T{end_time}:00')
                if end_dt <= start_dt:
                    end_dt = end_dt + timedelta(days=1)
            else:
                end_dt = start_dt + timedelta(minutes=dur_min)
        except Exception:
            self._redirect_admin(('err', 'Invalid date or time format.'))
            return

        meeting_id = str(uuid.uuid4())

        meeting = {
            'id':           meeting_id,
            'title':        title,
            'phone':        phone,
            'pin':          pin,
            'access_code':  access_code,
            'platform':     platform,
            'category':     category,
            'source':       'admin-schedule',
            'start':        start_dt.isoformat(),
            'end':          end_dt.isoformat(),
            'scheduled_at': datetime.now().isoformat(),
        }

        # Persist to .scheduled_meetings.json for local record-keeping
        sched_path = Path.home() / 'recordings' / 'calls' / '.scheduled_meetings.json'
        try:
            (Path.home() / 'recordings' / 'calls').mkdir(parents=True, exist_ok=True)
            sched = json.loads(sched_path.read_text()) if sched_path.exists() else {}
            sched[meeting_id] = meeting
            sched_path.write_text(json.dumps(sched, indent=2))
        except Exception as e:
            self._redirect_admin(('err', f'Failed to save schedule: {e}'))
            return

        # ── Forward to local call_scheduler (port 8765) for launchd plist generation ──
        # call_scheduler.schedule_meeting() writes a Twilio auto-dial plist if phone
        # is set, or a BlackHole capture plist for video-only meetings.
        # Scheduler listens on 127.0.0.1 only and trusts loopback without HMAC.
        _plist_written = False
        try:
            import urllib.request as _urlreq
            _scheduler_payload = {**meeting, 'has_video': True}
            _payload_bytes = json.dumps(_scheduler_payload).encode()
            _req = _urlreq.Request(
                'http://127.0.0.1:8765/schedule',
                data=_payload_bytes,
                headers={'Content-Type': 'application/json'},
                method='POST',
            )
            with _urlreq.urlopen(_req, timeout=10):
                _plist_written = True
        except Exception:
            pass  # scheduler not running — launchd plist not written but JSON saved

        detail = f'{date_str} at {time_str} ({dur_min} min)'
        if phone:
            detail += f' · dial {phone}' + (f' PIN {pin}' if pin else '')
        _pipeline_note = (
            ' Twilio auto-dial armed.' if (_plist_written and phone) else
            ' BlackHole capture armed.' if _plist_written else
            ' ⚠️ Scheduler not reachable — restart call_scheduler.'
        )
        self._redirect_admin(('ok', f'Scheduled "{title}" for {detail}.{_pipeline_note}'))

    def _handle_admin_schedule_webinar(self):
        import uuid, subprocess as _sp
        from datetime import datetime, timedelta
        form = self._parse_form()
        title       = (form.get('title')               or '').strip()
        webinar_url = (form.get('webinar_url')         or '').strip()
        reg_number  = (form.get('registration_number') or '').strip()
        date_str    = (form.get('date')                or '').strip()
        time_str    = (form.get('start_time')          or '').strip()
        dur_min     = int(form.get('duration_min')     or 60)
        category    = (form.get('category')            or 'Other').strip()

        if not title or not webinar_url or not date_str or not time_str:
            self._redirect_admin(('err', 'Title, webinar URL, date, and start time are required.'))
            return

        try:
            start_dt = datetime.fromisoformat(f'{date_str}T{time_str}:00')
            end_dt   = start_dt + timedelta(minutes=dur_min)
        except Exception:
            self._redirect_admin(('err', 'Invalid date or time format.'))
            return

        if start_dt < datetime.now():
            self._redirect_admin(('err', 'Start time is in the past.'))
            return

        meeting_id = str(uuid.uuid4())
        meeting = {
            'id':           meeting_id,
            'title':        title,
            'type':         'webinar',
            'webinar_url':  webinar_url,
            'reg_number':   reg_number,
            'category':     category,
            'source':       'admin-schedule',
            'start':        start_dt.isoformat(),
            'end':          end_dt.isoformat(),
            'scheduled_at': datetime.now().isoformat(),
        }

        # Persist to .scheduled_meetings.json
        sched_path = Path.home() / 'recordings' / 'calls' / '.scheduled_meetings.json'
        try:
            (Path.home() / 'recordings' / 'calls').mkdir(parents=True, exist_ok=True)
            sched = json.loads(sched_path.read_text()) if sched_path.exists() else {}
            sched[meeting_id] = meeting
            sched_path.write_text(json.dumps(sched, indent=2))
        except Exception as e:
            self._redirect_admin(('err', f'Failed to save schedule: {e}'))
            return

        # Write a launchd plist that fires join_webinar.sh at start_dt - 2min
        launch_dt  = start_dt - timedelta(minutes=2)
        plist_name = f'com.cos.webinar.{meeting_id}.plist'
        plist_path = Path.home() / 'Library' / 'LaunchAgents' / plist_name

        join_script    = str(Path.home() / 'scripts' / 'join_webinar.applescript')
        # Path to call_recorder.py — configurable via CALL_RECORDER_PATH env var.
        # Default search order: $CALL_RECORDER_PATH > ~/scripts/call_recorder.py >
        # ~/cos-pipeline/tools/call_recorder.py. If none found, webinar feature
        # writes a plist that no-ops the record step (logs and exits) so Robert /
        # any tenant without the separate call-recorder repo isn't blocked.
        import os as _os
        _recorder_env = _os.environ.get('CALL_RECORDER_PATH', '')
        _recorder_candidates = [
            _recorder_env,
            str(Path.home() / 'cos-pipeline' / 'call_recorder.py'),
            str(Path.home() / 'scripts' / 'call_recorder.py'),
            str(Path.home() / 'cos-pipeline' / 'tools' / 'call_recorder.py'),
        ]
        recorder = next((c for c in _recorder_candidates if c and Path(c).exists()), '')
        reg_extractor  = str(Path.home() / 'scripts' / 'extract_webinar_registration.py')
        rec_log        = str(Path.home() / 'recordings' / 'calls' / f'webinar_{meeting_id}.log')

        # Shell wrapper: extract registration info → open Chrome → start recorder
        wrapper_path = Path.home() / 'recordings' / 'calls' / f'join_{meeting_id}.sh'
        reg_arg = f'number:{reg_number}' if reg_number else ''
        wrapper_content = f'''#!/bin/bash
# Auto-generated webinar join script for: {title}
# Scheduled: {start_dt.isoformat()}
LOG="{rec_log}"
echo "[$(date)] Starting webinar join for: {title}" >> "$LOG"

# Extract registration info from Gmail if not manually provided
REG_INFO="{reg_arg}"
if [ -z "$REG_INFO" ]; then
    REG_INFO=$(python3 "{reg_extractor}" "{title}" 2>>"$LOG" || echo "none:")
fi
echo "[$(date)] Registration info: $REG_INFO" >> "$LOG"

# Open Chrome and join
osascript "{join_script}" "{webinar_url}" "$REG_INFO" >> "$LOG" 2>&1 &

# Start recording (give Chrome 15s to fully load and join)
sleep 15
RECORDER="{recorder}"
if [ -n "$RECORDER" ] && [ -f "$RECORDER" ]; then
  python3 "$RECORDER" start --title "{title}" >> "$LOG" 2>&1 &
else
  echo "[$(date)] No call_recorder.py configured — skipping record step." >> "$LOG"
fi

echo "[$(date)] Join and record launched." >> "$LOG"
'''
        wrapper_path.write_text(wrapper_content)
        wrapper_path.chmod(0o755)

        # Stop-recording plist fires at end_dt
        stop_plist_name = f'com.cos.webinar.{meeting_id}.stop.plist'
        stop_plist_path = Path.home() / 'Library' / 'LaunchAgents' / stop_plist_name

        def _plist(label, hour, minute, second, program_args):
            return {
                'Label':           label,
                'ProgramArguments': program_args,
                'StartCalendarInterval': {'Hour': hour, 'Minute': minute, 'Second': second},
                'RunAtLoad':       False,
                'StandardOutPath': rec_log,
                'StandardErrorPath': rec_log,
            }

        import plistlib
        plist_data = _plist(
            f'com.cos.webinar.{meeting_id}',
            launch_dt.hour, launch_dt.minute, 0,
            ['/bin/bash', str(wrapper_path)],
        )
        plist_path.write_bytes(plistlib.dumps(plist_data))

        stop_plist_data = _plist(
            f'com.cos.webinar.{meeting_id}.stop',
            end_dt.hour, end_dt.minute, 0,
            [sys.executable, recorder, 'stop'],
        )
        stop_plist_path.write_bytes(plistlib.dumps(stop_plist_data))

        # Load both plists
        for p in [plist_path, stop_plist_path]:
            try:
                _sp.run(['launchctl', 'load', str(p)], check=True, capture_output=True)
            except Exception as e:
                self._redirect_admin(('err', f'Failed to load launchd job: {e}'))
                return

        detail = f'{date_str} at {time_str} ({dur_min} min) · {webinar_url}'
        reg_note = f' Registration {"manually set" if reg_number else "will be extracted from Gmail"}.'
        self._redirect_admin(('ok', f'Webinar "{title}" scheduled for {detail}.{reg_note} Chrome will open automatically at {launch_dt.strftime("%-I:%M %p")}.'))

    def _redirect_admin(self, flash):
        # POST → redirect → GET pattern; encode flash in query string
        import urllib.parse
        params = urllib.parse.urlencode({'ftype': flash[0], 'fmsg': flash[1]})
        self.send_response(303)
        self.send_header('Location', f'/admin/?{params}')
        self.end_headers()

    def _handle_refresh(self):
        """Fast path: reads cache → injects HTML. Returns in ~100ms."""
        if not _refresh_lock.acquire(blocking=False):
            self.send_json(429, {'ok': False, 'error': 'refresh already running'})
            return
        try:
            result = subprocess.run(
                [sys.executable, REFRESH_SCRIPT],
                capture_output=True, text=True,
                timeout=15,   # was 90s — now fast, 15s covers worst-case initial fetch fallback
            )
            if result.returncode == 0:
                self.send_json(200, {'ok': True, 'log': result.stdout})
            else:
                self.send_json(500, {'ok': False, 'error': result.stderr})
        except subprocess.TimeoutExpired:
            self.send_json(504, {'ok': False, 'error': 'timeout'})
        except Exception as e:
            self.send_json(500, {'ok': False, 'error': str(e)})
        finally:
            _refresh_lock.release()

    def _handle_compile_deals(self):
        """Non-blocking: run deal-system-compile.py in background, return 202 immediately.
        Compiles Deals/*.md + Excel profit models → deal-system-data.json → HTML inject.
        Also triggers a warmup so the CoS dashboard embeds the fresh dealPortfolio.
        """
        if _compile_lock.locked():
            self.send_json(202, {'ok': True, 'status': 'already_running'})
        else:
            threading.Thread(target=_run_compile, daemon=True, name='compile-deals-manual').start()
            self.send_json(202, {'ok': True, 'status': 'started'})

    def _handle_refresh_deals(self):
        """Recompile deal-system-data.json from local files (no API), then inject into HTML.
        Skips Haiku signal classification — that runs in the background warmup/compile path."""
        try:
            compile_result = subprocess.run(
                [sys.executable, DEALS_COMPILE_SCRIPT],
                capture_output=True, text=True, timeout=10,
            )
            if compile_result.returncode != 0:
                self.send_json(500, {'ok': False, 'error': compile_result.stderr})
                return
            env = os.environ.copy()
            env['SKIP_HAIKU_CLASSIFY'] = '1'
            result = subprocess.run(
                [sys.executable, DEAL_REFRESH_SCRIPT],
                capture_output=True, text=True, timeout=15, env=env,
            )
            if result.returncode == 0:
                self.send_json(200, {'ok': True, 'log': compile_result.stdout + result.stdout})
            else:
                self.send_json(500, {'ok': False, 'error': result.stderr})
        except subprocess.TimeoutExpired:
            self.send_json(504, {'ok': False, 'error': 'timeout'})
        except Exception as e:
            self.send_json(500, {'ok': False, 'error': str(e)})

    def _handle_patch(self):
        """Merge override maps into dashboard-data.json (persists manual UI moves).
        Body: JSON with any of: _workstreamOverrides, _stageOverrides,
                                 _pinnedItems, _hiddenItems
        Dict fields are deep-merged (update). List fields use {add:[...], remove:[...]} ops.
        """
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        try:
            patch = json.loads(body)
        except Exception:
            self.send_json(400, {'ok': False, 'error': 'invalid JSON'})
            return
        with _state_lock:
            try:
                cur = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
            except Exception:
                cur = {}
            for key in ('_workstreamOverrides', '_stageOverrides'):
                if key in patch:
                    cur.setdefault(key, {}).update(patch[key])
            for key in ('_pinnedItems', '_hiddenItems', '_dismissedFollowUps', '_dismissedEmailIds'):
                if key in patch:
                    existing = set(cur.get(key, []))
                    adds    = patch[key].get('add', [])
                    removes = patch[key].get('remove', [])
                    existing.update(adds)
                    for r in removes:
                        existing.discard(r)
                    cur[key] = list(existing)
            _tmp = STATE_PATH.with_suffix('.tmp')
            _tmp.write_text(json.dumps(cur, indent=2))
            os.replace(_tmp, STATE_PATH)
        self.send_json(200, {'ok': True})

    def _handle_pipeline_status(self):
        """GET /pipeline-status — returns current pipeline run state."""
        with _pipeline_lock:
            self.send_json(200, {
                'running':          _pipeline_running,
                'startedAt':        _pipeline_started_at,
                'lastCompletedAt':  _pipeline_completed_at,
                'claudeAvailable':  CLAUDE_BIN is not None,
            })

    # ================================================================
    # PROJECT SYNC HANDLERS
    # ================================================================

    def _handle_morning_brief(self):
        """
        GET /morning-brief

        Two-path architecture:
        Path A (Claude Code, no API charges): Serve pre-generated
          data/compiled/morning-brief-latest.html if fresh (< 20 hours old).
        Path B (dynamic, API): If file is missing or stale, generate live
          via Anthropic API and cache the result before serving.
        """
        import urllib.request as _ur
        from datetime import date, datetime as _dt

        user = self._authenticate() if not self._is_localhost() else 'owner'
        if user != 'owner':
            self._send_403()
            return

        brief_path = _ROOT / 'data' / 'compiled' / 'morning-brief-latest.html'
        FRESH_HOURS = 20  # serve cached file if younger than this

        # Path A: serve pre-generated file if fresh
        if brief_path.exists():
            age_hours = (_dt.now().timestamp() - brief_path.stat().st_mtime) / 3600
            if age_hours < FRESH_HOURS:
                self._serve_file(brief_path, 'text/html; charset=utf-8')
                return

        # Path B: generate live via API, cache, then serve
        data_path  = _ROOT / 'data' / 'compiled' / 'deal-system-data.json'
        sync_path  = _ROOT / 'data' / 'project-sync' / 'metadata' / 'sync-state.json'
        today_str  = date.today().isoformat()

        try:
            deals_raw  = json.loads(data_path.read_text()).get('deals', [])
        except Exception:
            deals_raw  = []

        try:
            deals_sync = json.loads(sync_path.read_text()).get('deals', {})
        except Exception:
            deals_sync = {}

        # Build terse deal summaries
        summaries = []
        for d in deals_raw:
            did = d.get('id', '')
            sm  = deals_sync.get(did, {})
            next_due = d.get('next_milestone_due')
            days_out = None
            if next_due:
                try:
                    days_out = (date.fromisoformat(str(next_due)) - date.today()).days
                except Exception:
                    pass
            summaries.append({
                'id': did, 'name': d.get('name', did),
                'stage': d.get('stage'), 'stage_index': d.get('stage_index'),
                'next_milestone': d.get('next_milestone'),
                'next_milestone_due': str(next_due) if next_due else None,
                'days_to_deadline': days_out,
                'health': d.get('health'),
                'key_risk': str(d.get('key_risk', ''))[:200],
                'critical_next_step': str(d.get('critical_next_step', ''))[:300],
                'edge': str(d.get('tcip_edge') or d.get('edge', ''))[:200],  # noqa: tenant-leak — backward-compat read of legacy key
                'open_workstreams': [w.get('title') for w in d.get('workstreams', [])
                                     if w.get('status') not in ('done', 'closed')][:3],
                'last_session_date': sm.get('last_session_date'),
                'last_session_summary': sm.get('last_session_summary', ''),
                'open_items': sm.get('open_items', []),
                'sync_status': sm.get('status', 'no-project'),
                'project_url': sm.get('project_url') or '',
            })

        _fc_principal = ((_FC_CTX or {}).get('principal') or {})
        _fc_firm      = ((_FC_CTX or {}).get('firm') or {})
        _briefee      = _fc_principal.get('name') or 'the principal'
        _firm_name    = _fc_firm.get('name') or 'the firm'
        _firm_focus   = _fc_principal.get('background') or 'infrastructure investing'
        prompt = f"""You are a morning briefing assistant for {_briefee}, a principal at {_firm_name} ({_firm_focus}). Today is {today_str}.

Active deals:
{json.dumps(summaries, indent=2)}

Generate a complete standalone HTML morning briefing page. Requirements:
- Self-contained HTML (no external dependencies)
- Dark theme: background #0a0a0a, surface #111, border #222, text #d0d0d0,
  accent #6bcbff, green #6bffb0, warn #ffd93d, danger #ff6b6b
- Monospace font stack: 'SF Mono', ui-monospace, monospace
- Nav links to: / | /briefing/ | /project-sync/status | /project-sync/update
- Today's date and a one-sentence headline (most urgent situation)
- One deal card per active deal, ranked by urgency, each containing:
    deal name, stage, health score, critical_next_step (bold bullet — the single
    most important action blocking the deal; use key_risk if critical_next_step empty),
    days-to-deadline (colored by urgency), project link if project_url is set,
    suggested project prompt (click-to-copy via onclick clipboard API)
- Alert banner listing any deals with sync_status stale or no-project
- Cross-deal flags section if any timing conflicts or capital tensions exist
- Prioritize by: deadline urgency → deal value → stage progression

Return ONLY the complete HTML. Start with <!DOCTYPE html>."""

        html = None
        api_key = os.environ.get('ANTHROPIC_API_KEY', '')

        if api_key:
            try:
                payload = json.dumps({
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 4000,
                    "messages": [{"role": "user", "content": prompt}]
                }).encode('utf-8')

                req = _ur.Request(
                    'https://api.anthropic.com/v1/messages',
                    data=payload,
                    headers={
                        'Content-Type': 'application/json',
                        'x-api-key': api_key,
                        'anthropic-version': '2023-06-01'
                    }
                )
                with _ur.urlopen(req, timeout=25) as resp:
                    result = json.loads(resp.read())
                html = result['content'][0]['text'].strip()
                if html.startswith('```'):
                    html = html.split('\n', 1)[1].rsplit('```', 1)[0].strip()
            except Exception:
                html = None

        # If API unavailable, generate minimal rule-based HTML
        if not html:
            sorted_deals = sorted(summaries, key=lambda d: (d.get('days_to_deadline') or 9999))
            cards = ''
            for i, d in enumerate(sorted_deals, 1):
                days = d.get('days_to_deadline')
                dc = '#ff6b6b' if days is not None and days <= 14 else '#ffd93d' if days is not None and days <= 30 else '#6bcbff'
                proj = f'<a href="{d["project_url"]}" target="_blank" style="color:#6bcbff">Open Project →</a>' if (d.get('project_url') or '').startswith('http') else ''
                prompt_text = f"Continuing {d['name']}. Stage: {d.get('stage')}. What needs attention today?"
                days_block = f'<div style="color:{dc};font-size:12px;margin-top:6px">⏱ {days} days</div>' if days is not None else ''
                next_step = d.get('critical_next_step') or d.get('key_risk', '')
                cards += (
                    f'<div style="background:#111;border:1px solid #222;padding:16px;margin-bottom:10px;display:flex;gap:14px">'
                    f'<div style="color:#333;font-size:26px;font-weight:700;min-width:30px">#{i}</div>'
                    f'<div style="flex:1"><div style="color:#eee;font-weight:700;margin-bottom:4px">{d["name"]}'
                    f'<span style="color:#555;font-size:12px;font-weight:400;margin-left:10px">{d.get("stage","")}</span></div>'
                    f'<div style="color:#c8a84b;font-size:13px;font-weight:600;margin-bottom:4px">▶ {next_step[:160]}</div>'
                    f'{days_block}'
                    f'<div style="margin-top:8px">{proj}</div>'
                    f'<div style="color:#555;font-size:11px;text-transform:uppercase;letter-spacing:1px;margin-top:10px">Opening prompt</div>'
                    f'<div style="color:#888;font-size:12px;background:#0a0a0a;padding:8px;border-left:2px solid #333;cursor:pointer" onclick="navigator.clipboard.writeText(this.innerText)">{prompt_text}</div>'
                    f'</div></div>'
                )

            html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>{_firm_name} Morning Brief — {today_str}</title></head>
<body style="font-family:'SF Mono',monospace;background:#0a0a0a;color:#d0d0d0;max-width:860px;margin:0 auto;padding:40px 20px">
<nav style="margin-bottom:28px"><a href="/" style="color:#555;text-decoration:none;font-size:12px;margin-right:20px">Dashboard</a><a href="/briefing/" style="color:#555;text-decoration:none;font-size:12px;margin-right:20px">Briefing</a><a href="/project-sync/status" style="color:#555;text-decoration:none;font-size:12px;margin-right:20px">Sync Status</a><a href="/project-sync/update" style="color:#555;text-decoration:none;font-size:12px">Session Close</a></nav>
<div style="color:#555;font-size:11px;letter-spacing:3px;text-transform:uppercase">{_firm_name} Morning Brief</div>
<div style="color:#6bcbff;font-size:13px;margin-bottom:24px">{today_str} (rule-based — ANTHROPIC_API_KEY not set)</div>
{cards}
</body></html>"""

        # Cache the result (both API and rule-based)
        try:
            brief_path.write_text(html, encoding='utf-8')
        except Exception:
            pass

        self._serve_html(html, inject_chrome=False)


    def _handle_project_sync_status(self):
        """GET /project-sync/status — sync status table. Owner-only."""
        from datetime import date

        user = self._authenticate() if not self._is_localhost() else 'owner'
        if user != 'owner':
            self._send_403()
            return

        sync_path = _ROOT / 'data' / 'project-sync' / 'metadata' / 'sync-state.json'
        data_path = _ROOT / 'data' / 'compiled' / 'deal-system-data.json'

        try:
            deals_sync = json.loads(sync_path.read_text()).get('deals', {})
        except Exception:
            deals_sync = {}

        try:
            name_map = {d.get('id',''): d.get('name', d.get('id',''))
                        for d in json.loads(data_path.read_text()).get('deals', [])}
        except Exception:
            name_map = {}

        rows = ''
        for did, meta in sorted(deals_sync.items()):
            status = meta.get('status', 'unknown')
            sc = {'current':'#6bffb0','stale':'#ffd93d','no-project':'#ff6b6b',
                  'no-session':'#ff6b6b'}.get(status, '#555')
            last_s = meta.get('last_session_date') or '—'
            age = '—'
            if meta.get('last_session_date'):
                try:
                    age = f"{(date.today()-date.fromisoformat(meta['last_session_date'])).days}d ago"
                except Exception:
                    pass
            proj = f'<a href="{meta["project_url"]}" target="_blank" style="color:#6bcbff">Open →</a>' if (meta.get('project_url') or '').startswith('http') else '—'
            snippet = str(meta.get('last_session_summary','') or '')[:80]
            rows += f'<tr><td>{name_map.get(did,did)}</td><td style="color:{sc};font-weight:600">{status}</td><td>{last_s} <span style="color:#444;font-size:11px">{age}</span></td><td style="color:#555;font-size:11px">{snippet}</td><td>{proj}</td></tr>'

        html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Sync Status</title>
<style>body{{font-family:'SF Mono',monospace;background:#0a0a0a;color:#d0d0d0;max-width:960px;margin:0 auto;padding:40px 20px}}
nav a{{color:#555;text-decoration:none;font-size:12px;margin-right:20px}}nav a:hover{{color:#6bcbff}}
h1{{color:#6bcbff;font-size:13px;letter-spacing:3px;text-transform:uppercase;margin-bottom:20px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}th{{color:#555;font-weight:400;text-align:left;padding:8px 12px;border-bottom:1px solid #222;font-size:11px;letter-spacing:1.5px;text-transform:uppercase}}
td{{padding:10px 12px;border-bottom:1px solid #161616}}</style></head>
<body>
<nav><a href="/">Dashboard</a><a href="/morning-brief">Morning Brief</a><a href="/project-sync/update">Session Close</a></nav>
<h1>Project Sync Status</h1>
<table><thead><tr><th>Deal</th><th>Status</th><th>Last Session</th><th>Summary</th><th>Project</th></tr></thead>
<tbody>{rows}</tbody></table>
</body></html>"""

        self._serve_html(html, inject_chrome=False)


    def _handle_project_sync_update(self):
        """
        GET  /project-sync/update — paste form
        POST /project-sync/update — write session close to sync-state.json
        Owner-only. Does NOT write to deal.md — sync state only.
        """
        import subprocess as _sp
        from datetime import date
        from urllib.parse import parse_qs, unquote_plus
        import yaml as _yaml

        user = self._authenticate() if not self._is_localhost() else 'owner'
        if user != 'owner':
            self._send_403()
            return

        if self.command == 'GET':
            data_path = _ROOT / 'data' / 'compiled' / 'deal-system-data.json'
            try:
                deal_ids = [d.get('id','') for d in json.loads(data_path.read_text()).get('deals',[]) if d.get('id')]
            except Exception:
                deal_ids = []

            opts = '\n'.join(f'<option value="{d}">{d}</option>' for d in deal_ids)
            prompt_path = _ROOT / 'session-close-prompt.md'
            try:
                prompt_ref = prompt_path.read_text()[:2000]
            except Exception:
                prompt_ref = ''

            html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Session Close</title>
<style>body{{font-family:'SF Mono',monospace;background:#0a0a0a;color:#d0d0d0;max-width:780px;margin:0 auto;padding:40px 20px}}
nav a{{color:#555;text-decoration:none;font-size:12px;margin-right:20px}}nav a:hover{{color:#6bcbff}}
h1{{color:#6bcbff;font-size:13px;letter-spacing:3px;text-transform:uppercase;margin-bottom:6px}}
.hint{{color:#555;font-size:12px;margin-bottom:24px;line-height:1.7}}
label{{color:#555;font-size:11px;letter-spacing:1.5px;text-transform:uppercase;display:block;margin-bottom:6px}}
select,textarea{{width:100%;background:#111;color:#d0d0d0;border:1px solid #222;padding:10px 12px;font-family:inherit;font-size:13px;margin-bottom:16px}}
textarea{{height:260px;resize:vertical}}
button{{background:#6bcbff;color:#0a0a0a;border:none;padding:10px 28px;cursor:pointer;font-weight:700;font-size:13px;letter-spacing:1px}}
details{{margin-top:28px}}summary{{color:#555;font-size:12px;cursor:pointer}}
pre{{background:#111;border:1px solid #222;padding:12px;font-size:11px;color:#666;white-space:pre-wrap;margin-top:8px}}</style>
</head><body>
<nav><a href="/">Dashboard</a><a href="/morning-brief">Morning Brief</a><a href="/project-sync/status">Sync Status</a></nav>
<h1>Session Close</h1>
<p class="hint">Run the session close prompt in your Claude Project. Paste the output below.<br>Updates sync-state.json only — no deal.md files are modified.</p>
<form method="POST" action="/project-sync/update">
<label>Deal</label><select name="deal_id">{opts}</select>
<label>Session Close Output</label>
<textarea name="session_block" placeholder="Paste output from session close prompt..."></textarea>
<button type="submit">WRITE TO DASHBOARD</button>
</form>
<details><summary>Session close prompt reference</summary><pre>{prompt_ref}</pre></details>
</body></html>"""
            self._serve_html(html, inject_chrome=False)
            return

        # POST
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length).decode('utf-8') if length else ''
        params = {k: unquote_plus(v[0]) for k, v in parse_qs(body).items()}

        deal_id = params.get('deal_id','').strip()
        session_block = params.get('session_block','').strip()

        if not deal_id or not session_block:
            self._serve_html('<p style="font-family:monospace;padding:20px">Error: missing fields. <a href="/project-sync/update" style="color:#6bcbff">Back</a></p>', inject_chrome=False)
            return

        try:
            updates = _yaml.safe_load(session_block)
            if not isinstance(updates, dict):
                raise ValueError("Must be a YAML mapping")
        except Exception as e:
            self._serve_html(f'<p style="font-family:monospace;padding:20px;color:#ff6b6b">Parse error: {e}<br><a href="/project-sync/update" style="color:#6bcbff">Back</a></p>', inject_chrome=False)
            return

        sync_path = _ROOT / 'data' / 'project-sync' / 'metadata' / 'sync-state.json'
        try:
            sync_state = json.loads(sync_path.read_text())
        except Exception:
            sync_state = {'deals': {}}

        sm = sync_state.setdefault('deals', {}).setdefault(deal_id, {})
        ALLOWED = {'last_session_date','last_session_summary','open_items',
                   'project_url','stage_update','next_milestone_update','next_milestone_due_update'}
        for f in ALLOWED:
            v = updates.get(f)
            if v and str(v).strip() not in ('', 'null'):
                sm[f] = v

        sm['last_session_date'] = date.today().isoformat()
        sm['status'] = 'current'
        sync_state['last_updated'] = date.today().isoformat()

        try:
            sync_path.write_text(json.dumps(sync_state, indent=2, default=str))
        except Exception as e:
            self._serve_html(f'<p style="font-family:monospace;padding:20px;color:#ff6b6b">Write error: {e}</p>', inject_chrome=False)
            return

        # Regenerate project-sync exports
        script = _ROOT / 'routines' / 'compile' / 'compile-project-sync.py'
        result = _sp.run([sys.executable, str(script)], capture_output=True, text=True, timeout=30, check=False)
        ok = result.returncode == 0
        color = '#6bffb0' if ok else '#ff6b6b'
        label = 'SUCCESS' if ok else 'COMPILE ERROR'

        html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Session Close — Result</title>
<style>body{{font-family:'SF Mono',monospace;background:#0a0a0a;color:#d0d0d0;max-width:700px;margin:0 auto;padding:40px 20px}}
.r{{padding:14px;border:1px solid {color};color:{color};margin-bottom:16px;white-space:pre-wrap;font-size:13px}}
.d{{background:#111;border:1px solid #222;padding:12px;font-size:12px;color:#666;white-space:pre-wrap}}
a{{color:#6bcbff;text-decoration:none}}</style></head><body>
<div class="r">{label} — {deal_id} updated
Last session: {sm.get('last_session_date')}
{(result.stdout or '')[-400:]}</div>
<div class="d">{json.dumps(sm, indent=2, default=str)}</div>
<p style="margin-top:20px"><a href="/project-sync/update">← Submit another</a> &nbsp;|&nbsp; <a href="/project-sync/status">Sync Status</a> &nbsp;|&nbsp; <a href="/morning-brief">Morning Brief</a></p>
</body></html>"""
        self._serve_html(html, inject_chrome=False)

    def _handle_run_pipeline(self):
        """POST /run-pipeline — fire cos-capture-pipeline skill via claude CLI.
        Returns 202 immediately; actual run happens in a background thread.
        When the pipeline finishes, it calls /warmup which pushes SSE refresh.
        """
        global _pipeline_running, _pipeline_started_at, _pipeline_completed_at

        if not CLAUDE_BIN:
            self.send_json(503, {'ok': False, 'error': 'claude CLI not found'})
            return

        with _pipeline_lock:
            if _pipeline_running:
                self.send_json(409, {'ok': True, 'status': 'already_running',
                                     'startedAt': _pipeline_started_at})
                return
            _pipeline_running    = True
            _pipeline_started_at = datetime.now().isoformat()

        self.send_json(202, {'ok': True, 'status': 'started',
                             'startedAt': _pipeline_started_at})

        def _run():
            global _pipeline_running, _pipeline_completed_at
            try:
                print(f'[pipeline] manual run started → {PIPELINE_LOG}', flush=True)
                # Read SKILL.md content to pass as the prompt (--print mode doesn't
                # resolve /skill-name slash commands the same way the scheduler does)
                try:
                    raw = SKILL_MD_PATH.read_text()
                    # Strip YAML frontmatter (---\n...\n---) so the leading ---
                    # isn't parsed as a CLI flag by claude --print
                    if raw.startswith('---'):
                        end = raw.find('\n---', 3)
                        skill_prompt = raw[end + 4:].lstrip('\n') if end != -1 else raw
                    else:
                        skill_prompt = raw
                except Exception as e:
                    print(f'[pipeline] cannot read SKILL.md: {e}', flush=True)
                    return
                with open(PIPELINE_LOG, 'w') as log:
                    result = subprocess.run(
                        [CLAUDE_BIN, '--dangerously-skip-permissions',
                         '--print', skill_prompt],
                        stdout=log, stderr=log,
                        cwd=CWD_CLAUDE,
                        timeout=600,   # 10 min hard cap
                    )
                rc = result.returncode
                print(f'[pipeline] manual run finished (exit {rc})', flush=True)
            except subprocess.TimeoutExpired:
                print('[pipeline] manual run timed out after 10 min', flush=True)
            except Exception as e:
                print(f'[pipeline] manual run error: {e}', flush=True)
            finally:
                with _pipeline_lock:
                    _pipeline_running      = False
                    _pipeline_completed_at = datetime.now().isoformat()
                # Pipeline calls /warmup itself on success; trigger here as safety net
                _warmup_in_background()

        threading.Thread(target=_run, daemon=True, name='pipeline-run').start()

    def _handle_queue_correction(self):
        """Append a correction to corrections-queue.json for the capture pipeline to process.
        Body: JSON with { "text": "...", "date": "YYYY-MM-DD" }
        The cos-capture-pipeline reads this file at 7:29am, applies changes, clears processed entries.
        """
        length = int(self.headers.get('Content-Length', 0))
        try:
            body = json.loads(self.rfile.read(length))
        except Exception:
            self.send_json(400, {'ok': False, 'error': 'invalid JSON'})
            return
        text = (body.get('text') or '').strip()
        if not text:
            self.send_json(400, {'ok': False, 'error': 'text required'})
            return
        queue_path = _ROOT / 'data' / 'corrections-queue.json'
        with _state_lock:
            try:
                queue = json.loads(queue_path.read_text()) if queue_path.exists() else []
            except Exception:
                queue = []
            queue.append({
                'text':      text,
                'date':      body.get('date', datetime.utcnow().strftime('%Y-%m-%d')),
                'status':    'pending',
                'queued_at': datetime.utcnow().isoformat() + 'Z',
            })
            queue_path.write_text(json.dumps(queue, indent=2))
        print(f'[corrections] queued: {text[:80]}', flush=True)
        self.send_json(200, {'ok': True, 'queued': len(queue)})

    def _handle_build_backlog_append(self):
        """POST /build-backlog/append — append an item to the Claude Code Build backlog.
        Body: { "name": "...", "nextStep": "...", "myAction": "...",
                "nextTouchBase": "YYYY-MM-DD" (optional), "id": "..." (optional) }
        Dedup by id (or by slug(name) if id omitted). Idempotent.
        Called by cos-capture-pipeline for queue items prefixed [build] or [claude].
        """
        length = int(self.headers.get('Content-Length', 0))
        try:
            body = json.loads(self.rfile.read(length))
        except Exception:
            self.send_json(400, {'ok': False, 'error': 'invalid JSON'})
            return
        name = (body.get('name') or '').strip()
        if not name:
            self.send_json(400, {'ok': False, 'error': 'name required'})
            return
        import re as _re
        item_id = (body.get('id') or _re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_'))[:80]
        today = datetime.utcnow().strftime('%Y-%m-%d')
        entry = {
            'id':            item_id,
            'name':          name,
            'nextStep':      (body.get('nextStep') or '').strip(),
            'myAction':      (body.get('myAction') or '').strip(),
            'nextTouchBase': (body.get('nextTouchBase') or '').strip(),
            'addedAt':       today,
        }
        data = _load_build_backlog()
        items = list(data.get('items', []))
        existing = next((i for i, it in enumerate(items) if it.get('id') == item_id), None)
        if existing is not None:
            items[existing] = {**items[existing], **{k: v for k, v in entry.items() if v}}
            action = 'updated'
        else:
            items.append(entry)
            action = 'appended'
        data['items']      = items
        data['updated_at'] = datetime.utcnow().isoformat() + 'Z'
        _save_build_backlog(data)
        print(f'[build-backlog] {action}: {name[:80]}', flush=True)
        self.send_json(200, {'ok': True, 'action': action, 'id': item_id, 'count': len(items)})

    def _handle_fundraising_add(self):
        """POST /fundraising/add — append a new conversation to a path bucket.
        Body: { "name": "", "firm": "", "path": "direct_lps|gp_stakes|placement_agents|strategic",
                "last_contact": "", "status": "Introduced|In Discussion|Committed|Passed",
                "notes": "" }
        Returns the updated fundraising block."""
        length = int(self.headers.get('Content-Length', 0))
        try:
            body = json.loads(self.rfile.read(length))
        except Exception:
            self.send_json(400, {'ok': False, 'error': 'invalid JSON'})
            return
        path = (body.get('path') or '').strip()
        if path not in _FUNDRAISING_BUCKETS:
            self.send_json(400, {'ok': False, 'error': f'path must be one of {list(_FUNDRAISING_BUCKETS)}'})
            return
        firm = (body.get('firm') or '').strip()
        name = (body.get('name') or '').strip()
        if not firm and not name:
            self.send_json(400, {'ok': False, 'error': 'firm or name required'})
            return
        status = (body.get('status') or 'Introduced').strip()
        if status not in ('Introduced', 'In Discussion', 'Committed', 'Passed'):
            status = 'Introduced'
        now_iso = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
        entry = {
            'id':           _djb2((firm or name) + '|' + name),
            'name':         name,
            'firm':         firm,
            'path':         path,
            'last_contact': (body.get('last_contact') or '').strip(),
            'status':       status,
            'notes':        (body.get('notes') or '').strip(),
            'updated_at':   now_iso,
        }
        data = _load_fundraising()
        # Replace if same id already exists in any bucket; otherwise append.
        existing_bucket = None
        for b in _FUNDRAISING_BUCKETS:
            for i, it in enumerate(data.get(b, [])):
                if it.get('id') == entry['id']:
                    existing_bucket = (b, i)
                    break
            if existing_bucket:
                break
        if existing_bucket:
            old_bucket, idx = existing_bucket
            data[old_bucket].pop(idx)
        data.setdefault(path, []).append(entry)
        data['updated_at'] = now_iso
        _save_fundraising(data)
        print(f'[fundraising] add: {firm}/{name} → {path}', flush=True)
        self.send_json(200, {'ok': True, 'fundraising': data})

    def _handle_fundraising_update(self):
        """POST /fundraising/update — patch an existing entry by id (and
        optionally move it across buckets).
        Body: { "id": "...", "path": "...", ...partial fields... }
        Returns the updated fundraising block."""
        length = int(self.headers.get('Content-Length', 0))
        try:
            body = json.loads(self.rfile.read(length))
        except Exception:
            self.send_json(400, {'ok': False, 'error': 'invalid JSON'})
            return
        target_id = (body.get('id') or '').strip()
        if not target_id:
            self.send_json(400, {'ok': False, 'error': 'id required'})
            return
        data = _load_fundraising()
        # Find current bucket + entry.
        found = None
        for b in _FUNDRAISING_BUCKETS:
            for i, it in enumerate(data.get(b, [])):
                if it.get('id') == target_id:
                    found = (b, i, it); break
            if found: break
        if not found:
            self.send_json(404, {'ok': False, 'error': f'id {target_id} not found'})
            return
        cur_bucket, idx, entry = found
        # Apply patches.
        patch = {k: v for k, v in body.items() if k in
                 ('name', 'firm', 'last_contact', 'status', 'notes', 'path')}
        new_path = (patch.get('path') or cur_bucket).strip()
        if new_path not in _FUNDRAISING_BUCKETS:
            self.send_json(400, {'ok': False, 'error': f'path must be one of {list(_FUNDRAISING_BUCKETS)}'})
            return
        if patch.get('status') and patch['status'] not in ('Introduced', 'In Discussion', 'Committed', 'Passed'):
            self.send_json(400, {'ok': False, 'error': 'invalid status'})
            return
        now_iso = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
        new_entry = {**entry, **patch, 'path': new_path, 'updated_at': now_iso}
        # Remove from old bucket; insert into new bucket.
        data[cur_bucket].pop(idx)
        data.setdefault(new_path, []).append(new_entry)
        data['updated_at'] = now_iso
        _save_fundraising(data)
        print(f'[fundraising] update: {target_id} → {new_path}', flush=True)
        self.send_json(200, {'ok': True, 'fundraising': data})

    def _handle_refresh_all(self):
        """Blocking: Otter → compile + fetch in parallel → inject → SSE.

        Phase 1 (Otter): scan Drive for new transcripts, extract action items via Claude,
        write to Follow-ups Doc. Fast (~2s) when nothing is new; slower if transcripts
        are waiting. Must complete before Phase 2 reads the Follow-ups Doc.

        Phase 2 (fetch + compile in parallel): read Follow-ups/Recruiting/Deal Docs +
        Calendar; compile deal YAML + Excel → JSON.

        Phase 3: inject HTML, broadcast SSE reload to open tabs.

        Returns 200 when complete. The 'Pull Fresh Data' button waits for this response
        before reloading the page — spinner is visible throughout.
        """
        import time as _time
        t0 = _time.time()
        # Phase 1 — Otter transcript processing (blocking; must precede fetch)
        _run_otter()
        # Phase 1b — email resolver (force=True bypasses active-hours gate for manual button)
        # Start compile in parallel since it's independent; resolver must finish before fetch
        t_compile = threading.Thread(target=_run_compile, daemon=True, name='refresh-all-compile')
        t_compile.start()
        _run_email_resolver(force=True)
        # Phase 2 — fetch (reads email-resolutions.json written by resolver above)
        t_fetch = threading.Thread(target=_run_fetch, daemon=True, name='refresh-all-fetch')
        t_fetch.start()
        t_compile.join(timeout=70)
        t_fetch.join(timeout=130)
        # Inject deal pipeline JSON → deal dashboard HTML
        try:
            subprocess.run([sys.executable, DEAL_REFRESH_SCRIPT], capture_output=True, text=True, timeout=15)
        except Exception:
            pass
        # Inject CoS data JSON → CoS dashboard HTML
        try:
            subprocess.run([sys.executable, REFRESH_SCRIPT], capture_output=True, text=True, timeout=15)
        except Exception:
            pass
        _broadcast_refresh()
        elapsed = round(_time.time() - t0, 1)
        self.send_json(200, {'ok': True, 'elapsed': elapsed})

    def _handle_warmup(self):
        """Non-blocking: kick off a background fetch and return 202 immediately.
        Called by scheduled tasks (cos-capture-pipeline, cos-personal-briefing) after
        they write to Google Docs, so the cache is always fresh after each pipeline run.
        """
        if _warmup_lock.locked():
            self.send_json(202, {'ok': True, 'status': 'already_running'})
        else:
            _warmup_in_background()
            self.send_json(202, {'ok': True, 'status': 'started'})

    def _handle_sync_preview(self):
        """GET /sync-preview  (owner-only)
        Runs cos-dashboard-fetch.py --dry-run, diffs the would-be state against the
        live dashboard-data.json, and returns a structured JSON change report.
        The Admin → Sync Preview tab renders this as a grid before any commit.
        """
        import subprocess, sys as _sys, json as _j, copy as _copy

        # ── 1. Load current live state ──────────────────────────────────────
        live: dict = {}
        if STATE_PATH.exists():
            try:
                live = _j.loads(STATE_PATH.read_text())
            except Exception as e:
                self.send_json(500, {'error': f'Could not read live state: {e}'}); return

        # ── 2. Run fetch in dry-run mode ─────────────────────────────────────
        fetch_script = Path(__file__).parent / 'cos-dashboard-fetch.py'
        try:
            result = subprocess.run(
                [_sys.executable, str(fetch_script), '--dry-run'],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                self.send_json(500, {
                    'error': 'fetch script failed',
                    'stderr': result.stderr[-2000:]
                }); return
            proposed = _j.loads(result.stdout)
        except subprocess.TimeoutExpired:
            self.send_json(504, {'error': 'fetch timed out (>120s)'}); return
        except Exception as e:
            self.send_json(500, {'error': f'dry-run failed: {e}'}); return

        # ── 3. Diff ──────────────────────────────────────────────────────────
        changes = []

        def _diff_list(section_tab, section_subtab, live_list, prop_list, id_key, label_key):
            """Compare two lists of dicts by id_key; emit add/modify/delete entries."""
            live_map = {str(x.get(id_key, x.get(label_key,'?'))): x for x in (live_list or [])}
            prop_map = {str(x.get(id_key, x.get(label_key,'?'))): x for x in (prop_list or [])}
            for k, v in prop_map.items():
                if k not in live_map:
                    changes.append({
                        'tab': section_tab, 'subtab': section_subtab,
                        'action': 'ADD', 'item': str(v.get(label_key, k)),
                        'detail': '', 'owner': v.get('owner',''),
                    })
                else:
                    # Detect meaningful field changes
                    diffs = []
                    for field in (label_key, 'stage','status','nextStep','notes','what','due'):
                        old_v = str(live_map[k].get(field,'') or '')
                        new_v = str(v.get(field,'') or '')
                        if old_v != new_v:
                            short_old = old_v[:60] + ('…' if len(old_v)>60 else '')
                            short_new = new_v[:60] + ('…' if len(new_v)>60 else '')
                            diffs.append(f'{field}: "{short_old}" → "{short_new}"')
                    if diffs:
                        changes.append({
                            'tab': section_tab, 'subtab': section_subtab,
                            'action': 'MODIFY', 'item': str(v.get(label_key, k)),
                            'detail': '; '.join(diffs[:3]),
                            'owner': v.get('owner',''),
                        })
            for k in live_map:
                if k not in prop_map:
                    changes.append({
                        'tab': section_tab, 'subtab': section_subtab,
                        'action': 'DELETE', 'item': str(live_map[k].get(label_key, k)),
                        'detail': 'Removed from source doc',
                        'owner': '',
                    })

        # Sub-tab label — uses firm display name from firm_context so the
        # admin diff panel doesn't hardcode the maintainer firm.
        _firm_label = (((_FC_CTX or {}).get('firm') or {}).get('name') or '').strip() or 'Firm'
        # Deal-tile bucket  →  Status / Dealflow
        _diff_list('Status', f'{_firm_label} → Dealflow',
                   live.get(COS_TENANT_SLUG, []), proposed.get(COS_TENANT_SLUG, []),
                   'name', 'name')

        # LP data  →  Status / Fundraising
        _diff_list('Status', f'{_firm_label} → Fundraising',
                   live.get('lpData', []), proposed.get('lpData', []),
                   'name', 'name')

        # Follow-ups  →  Status / Follow-ups  (grouped by workstream bucket)
        def _fu_key(fu):
            who = (fu.get('who') or '').strip()
            what = (fu.get('what') or '')[:40]
            return f'{who}|{what}'

        live_fu  = {_fu_key(f): f for f in (live.get('followUps')  or [])}
        prop_fu  = {_fu_key(f): f for f in (proposed.get('followUps') or [])}

        # Group new follow-ups by inferred deal bucket
        new_fu_by_bucket: dict = {}
        for k, fu in prop_fu.items():
            if k not in live_fu:
                # Infer deal bucket from 'who' or 'what' text
                bucket = _infer_bucket(fu)
                new_fu_by_bucket.setdefault(bucket, []).append(fu)

        for bucket, fus in sorted(new_fu_by_bucket.items()):
            for fu in fus:
                who_raw = fu.get('who','')
                owner   = _infer_owner(who_raw, fu.get('what',''))
                changes.append({
                    'tab': 'Status', 'subtab': f'Follow-ups — {bucket}',
                    'action': 'ADD',
                    'item': who_raw,
                    'detail': (fu.get('what','') or '')[:90],
                    'owner': owner,
                    'due': fu.get('due',''),
                })

        for k in live_fu:
            if k not in prop_fu:
                fu = live_fu[k]
                changes.append({
                    'tab': 'Status', 'subtab': 'Follow-ups',
                    'action': 'DELETE',
                    'item': fu.get('who',''),
                    'detail': (fu.get('what','') or '')[:60],
                    'owner': '',
                    'due': '',
                })

        # Fundraising strategy text
        live_fs  = str(live.get('fundraising',{}).get('approach','') or '')[:200]
        prop_fs  = str(proposed.get('fundraising',{}).get('approach','') or '')[:200]
        if live_fs != prop_fs:
            changes.append({
                'tab': 'Status', 'subtab': f'{_firm_label} → Fundraising',
                'action': 'MODIFY', 'item': 'Fundraising Strategy text',
                'detail': f'Content changed ({len(live_fs)} → {len(prop_fs)} chars)',
                'owner': '',
            })

        self.send_json(200, {
            'ok': True,
            'generatedAt': proposed.get('generatedAt',''),
            'changes': changes,
            'stats': {
                'adds':    sum(1 for c in changes if c['action']=='ADD'),
                'modifies':sum(1 for c in changes if c['action']=='MODIFY'),
                'deletes': sum(1 for c in changes if c['action']=='DELETE'),
            }
        })


if __name__ == '__main__':
    _load_sessions()
    if not OWNER_PASSWORD or not PARTNER_PASSWORD:
        print('[auth] *** WARNING: OWNER_PASSWORD and/or PARTNER_PASSWORD not set in environment ***', flush=True)
        print('[auth] *** All GETs will 401; dashboard is inaccessible until env vars are set ***', flush=True)
    else:
        print('[auth] Cookie sessions + Basic Auth fallback enabled', flush=True)
    # Start auto-warmup background thread
    warmup_thread = threading.Thread(target=_auto_warmup_loop, daemon=True, name='auto-warmup')
    warmup_thread.start()
    print(f'COS Dashboard server running on http://0.0.0.0:{PORT}', flush=True)
    print(f'Auto-warmup every {WARMUP_INTERVAL_MIN} min | /warmup for on-demand | /cache-status to check', flush=True)
    ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
