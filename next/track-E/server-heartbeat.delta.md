# server-heartbeat.delta.md — additive `/admin/heartbeat` route

Track L follow-up (Run 2 / Track E namespace).
Target file: `~/cos-pipeline/cos-dashboard-server.py` (live; **do not edit tonight**).
File length at time of design: 3764 lines.
This delta is paper-only — describes the additive route to be merged morning-of.

## Contract (per DECISION C1 + HEARTBEAT.md §6 L3)

- Route: `GET /admin/heartbeat`
- Owner-only auth (same gate as the rest of `/admin/*`).
- Reads `~/cos-pipeline/data-<tenant>/heartbeat.json` (the tenant slug derived from the listening port per DECISION C6: 7777 → `tomac`, 7778 → `re-dev`).
- Returns `application/json` body == raw cache file. **Never** mutates cache.
- If cache file does not exist: return `200` with `{"status":"uninitialized","hint":"run heartbeat.py --write-state","tenant":"<slug>"}` so the admin tab can render an empty state instead of breaking.
- If cache file exists but is older than 4 × heartbeat run interval (>40 min by default): include a top-level `"warning":"cache stale (Xh)"` field but still serve.
- Cache-Control: `no-store` (admin diagnostic — must never be intermediated).

## Where it slots in

The `do_GET` dispatcher already routes any path starting with `/admin` through an owner gate at lines **1808–1824**:

```
1808        elif self.path.startswith('/admin'):
1809            if user != 'owner':
1810                self._send_403(); return
1811            from urllib.parse import urlparse, parse_qs
1812            parsed = urlparse(self.path)
1813            qs = parse_qs(parsed.query)
1814            if parsed.path in ('/admin/spend', '/admin/spend/'):
1815                ...
1819                self._handle_admin_spend(days=days)
1820                return
1821            flash = None
1822            ...
1824            self._handle_admin(flash=flash)
```

The new branch goes inside this block so the owner-gate at line 1809 covers it for free. No new auth code needed.

## REPLACE / WITH patch

Live-file line numbers refer to the snapshot used for this delta (3764 lines, sha visible via `wc -l ~/cos-pipeline/cos-dashboard-server.py`).

### Patch 1 — add the route inside the `/admin` block

```
REPLACE  (lines 1814–1820, exact)
            if parsed.path in ('/admin/spend', '/admin/spend/'):
                try:
                    days = max(1, min(int(qs.get('days', ['7'])[0]), 90))
                except Exception:
                    days = 7
                self._handle_admin_spend(days=days)
                return

WITH
            if parsed.path in ('/admin/spend', '/admin/spend/'):
                try:
                    days = max(1, min(int(qs.get('days', ['7'])[0]), 90))
                except Exception:
                    days = 7
                self._handle_admin_spend(days=days)
                return
            if parsed.path in ('/admin/heartbeat', '/admin/heartbeat/'):
                self._handle_admin_heartbeat()
                return
```

### Patch 2 — add the handler method

Insert directly above `_handle_admin_spend` (currently line 1950) so heartbeat sits next to the other `_handle_admin_*` methods:

```
INSERT BEFORE  (line 1950)
    def _handle_admin_spend(self, days: int = 7):

WITH (prepend)
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

```

## Auth note

The owner check at live line 1809 (`if user != 'owner': self._send_403()`) is the only auth surface. The new handler must remain *inside* that branch — do not lift it to a sibling `elif` outside `/admin`, or it loses owner gating.

## Non-goals (deferred)

- Admin-tab HTML rendering (HEARTBEAT.md §6 step 2: pill bar + collapsible table) — paper for the morning HTML swap pass; not in this delta.
- POST `/admin/heartbeat/refresh` to trigger `heartbeat.py --write-state` — defer; the L LaunchAgent (`heartbeat.plist.next`) will keep the cache fresh.
- Per-tenant routing for non-tomac ports — covered by the dict literal; extend when re-dev tenant goes live.

## Verification (post-merge, morning)

```
curl -s -u owner:<pw> http://localhost:7777/admin/heartbeat | jq .status
# expect "uninitialized" until heartbeat.py --write-state runs once
python3 ~/cos-pipeline/heartbeat.py --tenant tomac --write-state
curl -s -u owner:<pw> http://localhost:7777/admin/heartbeat | jq '.tenant, (.routines|length)'
# expect "tomac", >0
curl -s http://localhost:7777/admin/heartbeat -o /dev/null -w '%{http_code}\n'
# expect 403 (no auth) — proves owner gate covers route
```
