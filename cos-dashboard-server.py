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

Kept alive by LaunchAgent: com.yoni.cosdashboard.plist
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
    return ips

_OWN_HOST_IPS = _resolve_own_host_ips()

def _sessions_path():
    return Path(__file__).parent.parent / 'data' / 'user-state' / 'sessions.json'

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
    slug = os.environ.get('COS_TENANT_SLUG', 'tomac')
    candidates.append(Path.home() / f'cos-pipeline-config-{slug}' / 'config' / 'deal-config.yaml')
    candidates.append(Path(__file__).parent.parent / 'config' / 'deal-config.yaml')
    # Legacy fallback (one-release back-compat — remove next major release)
    candidates.append(Path(__file__).parent.parent / 'config' / 'tomac-config.yaml')
    for p in candidates:
        try:
            if p.exists():
                return p
        except Exception:
            continue
    return candidates[-1]   # may not exist; loader will catch the error

_DEAL_CONFIG_PATH = _resolve_deal_config_path()
# Back-compat alias — legacy callers may still reference _TOMAC_CONFIG_PATH.
# Will be removed in the release after this one.
_TOMAC_CONFIG_PATH = _DEAL_CONFIG_PATH

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
    Renamed from _load_tomac_config in PLAN E1.4. Falls back to the legacy
    tomac-config.yaml path for one release; see _resolve_deal_config_path()."""
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

# Back-compat alias — remove in next major release.
_load_tomac_config = _load_deal_config


def _assert_cross_config_dedup() -> None:
    """Cross-config dedup invariant: an entity in deal-config.yaml MUST NOT
    also exist in recruit-config.yaml. Logs a stderr warning per overlap.

    Documented exception: an entry in `recruit-config.yaml >
    priorityTargets.inDiscussion` whose name contains "(CURRENT ROLE)" is the
    principal's career anchor and intentionally tracked there even if it
    matches a deal-config name (e.g. the firm Yoni co-founds is also a
    recruiting touch-point). Codified 2026-05-04 — see dash_corrections.md.
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
    Returns (all_succeeded, list_of_repos_granted)."""
    REPO_MAP = {
        'tc_team':    [
            ('ygontownik/Read-Tomac-Deal-Pipeline', 'read'),
            ('ygontownik/Invite-Chief-of-Staff',    'read'),
        ],
        'subscriber': [
            ('ygontownik/Invite-Chief-of-Staff',    'read'),
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
        if has_cos_setup:
            repo_lines_plain = '\n'.join(f'  • {r}' for r in github_repos)
            repo_lines_html  = ''.join(f'<li style="font-family:monospace;font-size:12px">{r}</li>' for r in github_repos)
            ONBOARD_URL = 'https://ygontownik.github.io/Dashboard/onboard.html'
            BOOTSTRAP_CMD = 'curl -fsSL https://ygontownik.github.io/Dashboard/bootstrap.sh | bash'
            cos_plain = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
YOUR OWN DASHBOARD
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IMPORTANT: Your dashboard and pipeline run on your own Mac — not on Yoni's server.
Yoni has no access to your data or API keys. Content is processed through
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
       • Wait for gdrive_credentials.json (Yoni will send separately)
       • Open Google sign-in → click Allow
       • Launch your dashboard at http://localhost:7777 (on your Mac)

Note: macOS will ask "allow access to keychain?" during setup — click Always Allow each time.
"""
            cos_html = f"""
<div style="border-top:2px solid #1b2d45;padding-top:20px;margin-top:20px">
  <div style="font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:#8c8378;margin-bottom:10px">Your own dashboard</div>
  <div style="background:#eef4ff;border:1px solid #c7d9f5;border-radius:8px;padding:14px 16px;margin-bottom:16px;font-size:13px;color:#1b2d45;line-height:1.6">
    <strong>Your dashboard and pipeline run on your own Mac</strong> — not on Yoni's server. Yoni has no access to your data or API keys. Content is processed through standard cloud APIs (Anthropic, Google Drive, AssemblyAI) that you control and pay for.<br><br>
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
    <li>The installer will ask for your GitHub token, open Anthropic console for your API key, set your dashboard login, wait for <code style="background:#f0ece4;padding:1px 4px;border-radius:3px">gdrive_credentials.json</code> from Yoni, then launch your dashboard at <code style="background:#f0ece4;padding:1px 4px;border-radius:3px">http://localhost:7777</code>.</li>
  </ol>
  <div style="background:#fffbeb;border:1px solid #fde68a;border-radius:6px;padding:10px 14px;font-size:12px;color:#92400e">
    <strong>macOS keychain:</strong> If macOS asks "allow access to keychain?" during setup — click <strong>Always Allow</strong> each time. This lets background tasks read your API keys without prompting.
  </div>
</div>"""

        plain = f"""Hi {first},

You've been given access to the Tomac Cove dashboard.

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
install Tailscale first, then let Yoni know your Tailscale email.

  Laptop: https://tailscale.com/download — install, sign in, tell Yoni
  iPhone: Tailscale from the App Store — sign in with the same email
  Same WiFi: No Tailscale needed — the URL above works directly.
{cos_plain}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Questions? Reply to this email.
"""
        html = f"""<div style="font-family:-apple-system,sans-serif;max-width:560px;color:#1a1a1a;line-height:1.5">
<p>Hi {first},</p>
<p>You've been given access to the Tomac Cove dashboard.</p>

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
  <p style="font-size:14px;margin:0 0 12px">The dashboard runs on a private server. To reach it from anywhere, install <strong>Tailscale</strong> first, then ask Yoni to add your email to the network.</p>

  <div style="margin-bottom:14px">
    <div style="font-weight:600;font-size:13px;margin-bottom:4px">💻 Laptop (Mac or Windows)</div>
    <ol style="margin:0;padding-left:18px;font-size:13px;color:#333">
      <li>Go to <a href="https://tailscale.com/download" style="color:#1b2d45">tailscale.com/download</a> and install</li>
      <li>Sign in with your email</li>
      <li>Let Yoni know your Tailscale email so he can add you</li>
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

  <p style="font-size:13px;color:#666;margin:0"><em>On the same WiFi as Yoni's Mac Mini? No Tailscale needed — the URL works directly.</em></p>
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
try:
    import _firm_context as _fc_srv
    _FC_CTX = _fc_srv.load_firm_context()
except Exception as _e:
    _fc_srv = None
    _FC_CTX = {}


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
# Back-compat alias — remove next release after callers migrate.
TOMAC_DATA          = DEAL_PIPELINE_DATA
BRIEFING_DASHBOARD  = _HERE / 'templates' / 'briefing-dashboard.html'
BRIEFING_MD         = _ROOT / 'data' / 'compiled' / 'deal-briefing-latest.md'
ALL_DASHBOARD       = _HERE / 'templates' / 'all-dashboard.html'
TILES_CONFIG        = _ROOT / 'config' / 'dashboard-tiles.yaml'
FIRM_CONFIG_PATH    = Path.home() / 'cos-pipeline' / 'firm_config.json'
TC_BUILD            = _HERE / 'tomac-cove-build'
SHARED_STATIC       = _HERE / 'static'               # shared design-system.css + assets
TOPNAV_PARTIAL      = _HERE / 'templates' / '_topnav.html'
DEAL_SYSTEM_DATA    = _ROOT / 'data' / 'compiled' / 'deal-system-data.json'
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
OTTER_SCRIPT         = str(_ROOT / 'routines' / 'process' / 'cos_otter_backfill.py')
RESOLVER_SCRIPT      = str(_ROOT / 'routines' / 'process' / 'cos_email_resolver.py')
SWEEP_SCRIPT         = str(_ROOT / 'routines' / 'process' / '_resolved_row_sweep.py')
WARMUP_INTERVAL_MIN  = 10   # auto-fetch every N minutes in background

# ── per-user JSON filter (F-now.3, feature-flagged) ─────────────────
# When PER_USER_FILTER_ENABLED is true, /data responses for non-owner users are
# filtered against ~/cos-pipeline-config-<TENANT>/users/<email>/preferences.json
# before being returned. Owner sees the full payload. If prefs file missing,
# behavior falls back to the legacy tier-based filter (no harm).
PER_USER_FILTER_ENABLED = os.environ.get('PER_USER_FILTER_ENABLED', '0') == '1'
COS_TENANT_SLUG         = os.environ.get('COS_TENANT_SLUG', 'tomac')
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
    return _bucket_cfg_cache['cfg'] or {
        'rules': [], 'default_bucket': 'General / Other',
        'default_owner': 'Yoni', 'owner_prefixes': [],
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
    return cfg.get('default_owner', 'Yoni')

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
    if 'window.__DELETIONS__' not in html:
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
        cp_aliases = _fc_srv.cp_aliases(_FC_CTX) if _fc_srv else []
    except Exception:
        cp_aliases = []
    firm_ctx_public = _firm_context_public(_FC_CTX)
    return (
        '<script>'
        '(function(){'
        'window.__TOPICS_INITIAL__ = ' + json.dumps(topics_initial) + ';'
        'window.__ORDER_INITIAL__ = ' + json.dumps(order_initial) + ';'
        'window.__BUILD_BACKLOG_INITIAL__ = ' + json.dumps(build_backlog_initial) + ';'
        'window.__PERSONAL_ITEMS_INITIAL__ = ' + json.dumps(personal_items_initial) + ';'
        'window.__RECRUIT_CONFIG__ = ' + json.dumps(recruit_config) + ';'
        # Canonical name — JS bundles should read window.__DEAL_CONFIG__.
        'window.__DEAL_CONFIG__ = ' + json.dumps(deal_config) + ';'
        # Back-compat alias — pre-rename React bundle still references
        # window.__TOMAC_CONFIG__. Leave for one release; remove after the
        # bundle in ~/dashboards/app/templates/* is rebuilt against __DEAL_CONFIG__.
        'window.__TOMAC_CONFIG__ = window.__DEAL_CONFIG__;'
        # Firm identity (principal name, team, firm name) — single source for
        # tenant-personalized strings in templates. See _firm_context_public()
        # for the schema (counterparty_aliases / draft_voice are kept server-side).
        'window.__FIRM_CONTEXT__ = ' + json.dumps(firm_ctx_public) + ';'
        'window.__USER_ROLE__ = ' + json.dumps(user) + ';'
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

# Small overlay script shown only on /tomac-cove/?deal=... — renders a
# floating "← Back" link so users returning from /, /deals/ or /briefing/
# can get back to their prior dashboard without hitting browser back.
# Lives here (not in the React bundle) so we don't need to rebuild the
# pre-compiled React artifact.
_DRAWER_BACK_SHIM = '''
<script>
(function () {
  if (!/^\\/tomac-cove\\/?$/.test(location.pathname)) return;
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


def _warmup_in_background():
    """Warmup sequence on every cycle:
      1. Prune corrections-queue (inline, <1ms)
      2. compile + (resolver → fetch → sweep) run in parallel threads.
         Resolver runs before fetch so same-cycle resolutions land in the data.
         Sweep runs after fetch on the freshly-compiled JSON — pure local I/O,
         no network, <100ms. All three complete well before the next /refresh.
    """
    _prune_corrections_queue()

    def _resolver_then_fetch():
        _run_email_resolver()
        _run_fetch()
        _run_sweep()  # prune resolved/stale items from the fresh dashboard-data.json

    t_chain   = threading.Thread(target=_resolver_then_fetch, daemon=True, name='warmup-chain')
    t_compile = threading.Thread(target=_run_compile,          daemon=True, name='warmup-compile')
    t_chain.start()
    t_compile.start()
    return t_chain

def _auto_warmup_loop():
    """Background loop: warm cache immediately on startup, then every N minutes."""
    # Initial warmup — delay 5s to let server finish starting
    time.sleep(5)
    _run_fetch()
    # Recurring warmup
    while True:
        time.sleep(WARMUP_INTERVAL_MIN * 60)
        _run_fetch()

# ── Routines (Claude scheduled-task health) ────────────────
# Reads ~/Library/LaunchAgents/com.yoni.claude-task.*.plist + per-task
# logs at ~/dashboards/logs/claude-tasks/<task>.{stdout,stderr,run}.log.
# Wrapper at ~/dashboards/scripts/run-claude-task.sh writes deterministic
# BEGIN ... END banners we parse to extract per-run history.
ROUTINES_LAUNCHAGENTS = Path.home() / 'Library' / 'LaunchAgents'
ROUTINES_LOG_DIR      = Path.home() / 'dashboards' / 'logs' / 'claude-tasks'
ROUTINES_SKILL_DIR    = Path.home() / '.claude' / 'scheduled-tasks'
ROUTINES_LABEL_PREFIX = 'com.yoni.claude-task.'
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
            'skill_path':        str(ROUTINES_SKILL_DIR / name / 'SKILL.md'),
            'log_paths': {
                'stdout': str(ROUTINES_LOG_DIR / f'{stem}.stdout.log'),
                'stderr': str(ROUTINES_LOG_DIR / f'{stem}.stderr.log'),
                'run':    str(ROUTINES_LOG_DIR / f'{stem}.run.log'),
            },
        }
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
        # supporting paths for React app
        if any('/tomac-cove' in t for t in (u.get('tiles') or [])):
            allowed_prefixes.update(['/static', '/dashboard-data.json'])
        # /deals/ deps
        if any('/deals' in t for t in (u.get('tiles') or [])):
            allowed_prefixes.update(['/deals', '/deals/', '/data',
                                     '/deals/data.json', '/tomac/data.json'])
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
            self.send_header('WWW-Authenticate', 'Basic realm="Tomac Cove Dashboard"')
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
            if TOMAC_DATA.exists():
                self._serve_file(TOMAC_DATA, 'application/json')
            else:
                self.send_json(404, {'error': 'deal-pipeline-data.json not yet generated'})
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
        elif self.path == '/tomac' or self.path == '/tomac/':
            # Legacy route — kept as 301 for one release per PLAN E1.6.
            # Routes to consolidated /deals/ view (sourcing + pipeline).
            # Remove this elif in the release after this one.
            self.send_response(301)
            self.send_header('Location', '/deals/')
            self.end_headers()
        # TODO(E1, next release): remove /tomac/data.json — use /deals/data.json.
        elif self.path == '/tomac/data.json':
            # Backward-compat data endpoint — same payload now read from /deals/.
            if TOMAC_DATA.exists():
                self._serve_file(TOMAC_DATA, 'application/json')
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
        elif self.path == '/cache-status':
            # Quick endpoint to check how fresh the cache is. Also surfaces
            # per-section age so the UI can flag stale tiles even when the
            # global fetch ran recently (e.g. an OAuth-scoped sub-fetch
            # silently returned empty and preserved a stale prior value).
            status = {'ok': False, 'fetchedAt': None, 'ageMin': None, 'sections': {}}
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
            flash = None
            if 'ftype' in qs and 'fmsg' in qs:
                flash = (qs['ftype'][0], qs['fmsg'][0])
            self._handle_admin(flash=flash)
        elif self.path.split('?')[0] == '/batch-jobs':
            if user != 'owner':
                self._send_403(); return
            self.send_json(200, {'jobs': _batch_jobs_data()})
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
        elif self.path.split('?')[0] in ('/tomac-cove', '/tomac-cove/'):
            self._serve_html_template(COS_DASHBOARD, user)
        elif self.path.startswith('/api/auth-health'):
            # Credential health — owner-only.
            user = self._authenticate()
            if user is None:
                self._send_401()
                return
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
        else:
            self.send_response(404); self.end_headers()

    def _handle_all(self, user):
        """Unified landing page — renders tiles visible to the user's tier.
        Hides the Tomac Cove alt view (per CEO) and the Admin console from
        the visual grid; those remain reachable by direct URL."""
        HIDDEN_IN_ALL = {'tomac', 'admin'}
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
        html = (html.replace('{{USER}}', user)
                    .replace('{{TILE_COUNT}}', str(len(tiles)))
                    .replace('{{TILES}}', '\n'.join(cards)))
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
        # Tenant from server port. Default tomac (port 7777).
        port = getattr(self.server, 'server_port', 7777)
        tenant = {7777: 'tomac', 7778: 're-dev'}.get(port, 'tomac')
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
        body.append('<style>'
                    'body{font:14px/1.45 -apple-system,BlinkMacSystemFont,sans-serif;'
                    'max-width:1100px;margin:24px auto;padding:0 16px;color:#222}'
                    'h1{font-size:20px;margin:0 0 4px}'
                    '.sub{color:#666;margin-bottom:18px}'
                    '.tot{background:#f5f6f7;padding:10px 14px;border-radius:6px;margin:12px 0;'
                    'display:flex;gap:24px;font-size:13px}'
                    '.tot b{font-size:16px}'
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
        html = (html.replace('{{DASHBOARD_SECTIONS}}', dashboard_sections)
                    .replace('{{USERS_TABLE}}', table)
                    .replace('{{FLASH}}', flash_html)
                    .replace('{{ALL_TILES_JSON}}', tiles_for_js)
                    .replace('{{ADMIN_TABS_JSON}}', admtabs_for_js)
                    .replace('{{SCHEDULED_CALLS_JSON}}', json.dumps(sched_calls))
                    .replace('{{PROCESSED_CALLS_JSON}}', json.dumps(proc_calls))
                    .replace('{{RUN_STATE_JSON}}', json.dumps(run_state_slim))
                    .replace('{{DELETIONS_PANEL}}', deletions_panel))
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
            'tomac':            state.get('tomac',            []),
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

    def _handle_briefing_intel(self):
        """GET /briefing/intel.json — daily briefing synopsis from compiled dashboard-data.json.
        Shape: {synopsis: {captureSummary: {date, tomac, recruiting, other, actionItems}}, fetchedAt}"""
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
            payload = {
                'synopsis': synopsis,
                'marketCommentary': latest_market,
                'date': data.get('today', ''),
                'fullText': full_text,
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
<title>Sign in — Tomac Cove</title>
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
  <h1>Tomac Cove Dashboard</h1>
  {error_block}
  <form method="post" action="/login">
    <input type="hidden" name="next" value="{next_path}">
    <label for="u">Username</label>
    <input id="u" name="username" type="text" autocomplete="username" autofocus required>
    <label for="p">Password</label>
    <input id="p" name="password" type="password" autocomplete="current-password" required>
    <button type="submit">Sign in</button>
  </form>
  <div class="brand">Tomac Cove &nbsp;·&nbsp; Private</div>
</div>
</body>
</html>"""

    def _serve_login_page(self, error: str = '', next_path: str = '/'):
        error_block = f'<div class="err">{error}</div>' if error else ''
        html = self._LOGIN_PAGE.replace('{error_block}', error_block).replace('{next_path}', next_path)
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
        """Serve an HTML string; optionally inject shared design-system + topnav."""
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
            self.end_headers()
        else:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
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
        self._serve_html(html, inject_chrome=True, user=user)

    def do_POST(self):
        # ── unauthenticated routes ──────────────────────────────
        if self.path == '/login':
            self._handle_login_post()
            return
        # POSTs must come from localhost OR be authenticated as owner.
        # Routines on the Mac curl localhost directly (no auth needed);
        # remote browsers must present owner credentials.
        if not self._is_localhost():
            user = self._authenticate()
            if user is None:
                self._send_401()
                return
            if user != 'owner':
                self._send_403()
                return
        if self.path == '/refresh':
            self._handle_refresh()
        elif self.path == '/warmup':
            self._handle_warmup()
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
        elif self.path == '/item/undelete':
            self._handle_item_undelete()
        elif self.path == '/topics/save':
            self._handle_topics_save()
        elif self.path == '/order/save':
            self._handle_order_save()
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
        elif self.path == '/admin/schedule-dial-in':
            if not self._is_localhost():
                user = self._authenticate()
                if user != 'owner':
                    self._send_403(); return
            self._handle_admin_schedule_dial_in()
        else:
            self.send_json(404, {'ok': False, 'error': 'not found'})

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

        # ── Register with focused-exploration Railway recording pipeline ──
        # This triggers Twilio to dial in and record, then transcribe + upload to Drive.
        # Only attempt if call_recording is enabled in firm_context.yaml — subscribers
        # without Twilio don't have a Railway endpoint and shouldn't see the warning.
        _call_recording_enabled = bool((_FC_CTX or {}).get('call_recording', False))
        _railway_registered = False
        if phone and _call_recording_enabled:
            try:
                import urllib.request as _urlreq
                _sh, _sm = int(time_str.split(':')[0]), int(time_str.split(':')[1])
                _start_12 = f'{_sh % 12 or 12}:{_sm:02d} {"AM" if _sh < 12 else "PM"}'
                _end_12   = f'{end_dt.hour % 12 or 12}:{end_dt.minute:02d} {"AM" if end_dt.hour < 12 else "PM"}'
                _body = json.dumps({
                    'label':     title,
                    'number':    phone,
                    'code':      access_code or '',
                    'pin':       pin or '',
                    'date':      date_str,
                    'startTime': _start_12,
                    'endTime':   _end_12,
                }).encode()
                _req = _urlreq.Request(
                    'https://focused-exploration-production.up.railway.app/api/calls',
                    data=_body,
                    headers={'Content-Type': 'application/json'},
                    method='POST',
                )
                with _urlreq.urlopen(_req, timeout=15):
                    _railway_registered = True
            except Exception:
                pass  # Railway failure doesn't block the local launchd schedule

        detail = f'{date_str} at {time_str} ({dur_min} min)'
        if phone:
            detail += f' · dial {phone}' + (f' PIN {pin}' if pin else '')
        _pipeline_note = ' Twilio recording pipeline armed.' if _railway_registered else (' (Railway pipeline not reached — check internet)' if (phone and _call_recording_enabled) else '')
        self._redirect_admin(('ok', f'Scheduled "{title}" for {detail}. Recording will start automatically.{_pipeline_note}'))

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
        """Run deal-dashboard-refresh.py to inject fresh JSON into the deals HTML file."""
        try:
            result = subprocess.run(
                [sys.executable, DEAL_REFRESH_SCRIPT],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                self.send_json(200, {'ok': True, 'log': result.stdout})
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
            STATE_PATH.write_text(json.dumps(cur, indent=2))
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

        Phase 2 (fetch + compile in parallel): read Follow-ups/Recruiting/Tomac Docs +
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

        # Tomac deals  →  Status / Dealflow
        _diff_list('Status', 'Tomac Cove → Dealflow',
                   live.get('tomac', []), proposed.get('tomac', []),
                   'name', 'name')

        # LP data  →  Status / Fundraising
        _diff_list('Status', 'Tomac Cove → Fundraising',
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
                'tab': 'Status', 'subtab': 'Tomac Cove → Fundraising',
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
