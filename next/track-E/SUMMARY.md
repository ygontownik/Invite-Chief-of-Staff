# Track E (L follow-up) — heartbeat productionization — SUMMARY

Completed by Phase 2 sub-agent, run 2 (2026-05-03). Persisted by parent.

## Files delivered

| Path | Purpose |
|---|---|
| `~/cos-pipeline/next/track-E/server-heartbeat.delta.md` | REPLACE/WITH patch for `cos-dashboard-server.py` adding owner-only `GET /admin/heartbeat`. Anchors: lines 1808–1820 + insert-before 1950 |
| `~/cos-pipeline/next/track-E/oauth_expiry.py.next` | Read-only OAuth expiry probe. JWT, JSON (Google `expiry`, MS variants, channel `expiration`), pickled `google.oauth2.credentials.Credentials` with mtime+90d fallback when SDK absent |
| `~/cos-pipeline/next/track-E/heartbeat.plist.next` | LaunchAgent text (NOT installed). Label `com.cos.tomac.heartbeat`. Runs every 600s. Logs to `~/dashboards/logs/heartbeat.{stdout,stderr}` |
| `~/cos-pipeline/tests/test_oauth_expiry.py` | 13 unittests, all green (1 conditional skip if SDK absent) |

## Test result

`Ran 13 tests in 0.045s — OK (skipped=1)`. Skip is the SDK-absent fallback test.

## Key decisions

- **E-L3-01** `/admin/heartbeat` slots inside existing `/admin` branch (server.py line 1808). Owner-only auth at line 1809 covers it for free.
- **E-L3-02** Missing cache returns `200 {"status":"uninitialized","hint":"run heartbeat.py --write-state","tenant":"<slug>"}`.
- **E-L3-03** Tenant slug derived from `self.server.server_port` via `{7777:'tomac', 7778:'re-dev'}` per C6.
- **E-L3-04** Stale-cache annotation at age > 4× plist interval (40 min); annotation only.
- **E-L4-01** OAuth expiry is standalone module (HEARTBEAT.md §7 single-file privacy boundary).
- **E-L4-02** Token bodies read and immediately discarded; only `{path, exp_iso, days_until_expiry, status, method}` returned.
- **E-L4-03** Pickle path requires `google.oauth2.credentials`; absent → fall back to `mtime + 90d`.
- **E-L4-04** All file opens `'r'` or `'rb'`. Zero write paths.
- **E-L4-05** `__main__` always exits 0 (avoid launchd retry noise).
- **E-PLIST-01** `StartInterval=600` (sub-hour freshness).
- **E-PLIST-02** `RunAtLoad=true`, `KeepAlive=false`.

## Deferrals

- `cos-dashboard-server.py` not modified — apply REPLACE/WITH morning-of.
- Plist not installed.
- `oauth_expiry.py.next` not yet wired into `heartbeat.py` — needs HEARTBEAT.md §7 "credentials" key added to JSON schema.
- Admin-tab HTML belongs to Phase 1.7 morning HTML strip swap.
- `heartbeat_email.py` "Credentials expiring soon" section not updated.

## Verification commands

```bash
python3 -c "import ast; ast.parse(open('$HOME/cos-pipeline/next/track-E/oauth_expiry.py.next').read())"
python3 -c "import xml.etree.ElementTree as ET; ET.parse('$HOME/cos-pipeline/next/track-E/heartbeat.plist.next')"
python3 ~/cos-pipeline/tests/test_oauth_expiry.py
python3 ~/cos-pipeline/next/track-E/oauth_expiry.py.next
sed -n '1808,1825p' ~/cos-pipeline/cos-dashboard-server.py
```

## HARD-RULES sweep (passed)

- No `~/credentials/*` modified (read-only)
- No `~/dashboards/app/templates/*.html` touched
- No `~/Library/LaunchAgents/*` touched
- No `launchctl`, no API call, no email, no git push
- `cos-dashboard-server.py` not modified — only `.delta.md`
- All NEW files
