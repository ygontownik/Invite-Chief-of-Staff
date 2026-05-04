# server-data-filter.delta.md

Per-user filter layer for `cos-dashboard-server.py` `/data` endpoint. Adds a NEW
code path under feature flag `PER_USER_FILTER_ENABLED` (env var, default off).
Owner is unaffected. Non-owner users get a filtered response that drops
`recruiting`, `personalActions`, `briefingLog` and any tile-restricted sections.

All line numbers below reference the live file at the snapshot read this run
(2026-05-03). Re-verify before patching.

---

## Patch 1 — module-level feature flag + helper (NEW code, additive)

Insert immediately after the existing module-level constants block near
`TOMAC_DATA = _ROOT / 'data' / 'compiled' / 'deal-pipeline-data.json'`
(around line 673–700).

REPLACE: (none — pure insertion)

WITH:
```python
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
```

---

## Patch 2 — thread `user` into `_handle_data` call site

Live line 1766–1767:

REPLACE:
```python
        elif self.path == '/data':
            self._handle_data()
```

WITH:
```python
        elif self.path == '/data':
            self._handle_data(user=user)
```

---

## Patch 3 — accept `user` in `_handle_data` and apply filter

Live line 2312:

REPLACE:
```python
    def _handle_data(self):
        """GET /data — returns the slim dashboard data JSON used by in-place refresh.
        Same fields as cos-dashboard-refresh.py injects into the HTML, so the browser
        can update window.DATA in-place without a full page reload.
        """
```

WITH:
```python
    def _handle_data(self, user: str = 'owner'):
        """GET /data — returns the slim dashboard data JSON used by in-place refresh.
        Same fields as cos-dashboard-refresh.py injects into the HTML, so the browser
        can update window.DATA in-place without a full page reload.

        When PER_USER_FILTER_ENABLED is on and `user` != 'owner', the response is
        filtered through `_filter_data_for_user()` (drops recruiting / personal /
        briefing-log sections plus tilesVisible-restricted keys, and removes
        hiddenItems IDs from list sections). See server-data-filter.delta.md.
        """
```

Live line 2394 (final line of `_handle_data`):

REPLACE:
```python
        self.send_json(200, data)
```

WITH:
```python
        data = _filter_data_for_user(data, user)
        self.send_json(200, data)
```

---

## Rollout sequence

1. Apply patches 1-3 in `cos-dashboard-server.py.next` (NOT the live file).
2. Run `users_migrate.py.next --apply` once to populate preferences.json files.
3. Manual check: with `PER_USER_FILTER_ENABLED=0` (default), behavior is
   byte-identical to today (verified by diffing `/data` responses pre/post).
4. Flip `PER_USER_FILTER_ENABLED=1` in the LaunchAgent plist; restart server.
5. Log in as Mark (non-owner) -> verify `recruiting`, `personalActions`,
   `briefingLog` are absent from `/data` payload.
6. Log in as owner -> verify full payload still present.

## Reverse-out

Set `PER_USER_FILTER_ENABLED=0` and restart. Code path is bypassed; no data
changes are required to revert.
