"""_auth_watchdog.py — shared credential health checker.

Call check_all() at the top of any pipeline script.  Returns a dict of
per-credential health and writes ~/credentials/auth_health.json.
Never raises — on failure the credential is marked 'failed' and the
caller can skip auth-dependent sections gracefully.

Three credentials tracked:
  google      — token.json (COS OAuth) + gdrive_token.pickle (Drive API)
  chrome_cdp  — isolated Chrome profile, CDP on localhost:9222
  jefferies   — Playwright session cookies in jef_auth.json
"""
from __future__ import annotations

import json
import pickle
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CREDS = Path.home() / 'credentials'
HEALTH_FILE = CREDS / 'auth_health.json'


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── individual checks ──────────────────────────────────────────────────────────

def _check_google() -> dict:
    """Check token.json (COS pipeline OAuth) and gdrive_token.pickle (Drive API)."""
    issues: list[str] = []
    worst = 'ok'

    # --- token.json ---
    token_path = CREDS / 'token.json'
    try:
        data = json.loads(token_path.read_text())
        if not data.get('refresh_token'):
            issues.append('token.json: no refresh_token — re-auth required')
            worst = 'failed'
        else:
            expiry = data.get('expiry', '')
            if expiry:
                try:
                    exp_dt = datetime.fromisoformat(expiry.replace('Z', '+00:00'))
                    if exp_dt < datetime.now(timezone.utc):
                        issues.append(f'token.json: access token expired at {expiry}')
                        if worst == 'ok':
                            worst = 'stale'
                except Exception:
                    pass
    except FileNotFoundError:
        issues.append('token.json: file not found')
        worst = 'failed'
    except Exception as e:
        issues.append(f'token.json: {e}')
        worst = 'failed'

    # --- gdrive_token.pickle ---
    pickle_path = CREDS / 'gdrive_token.pickle'
    try:
        if not pickle_path.exists():
            issues.append('gdrive_token.pickle: file not found')
            worst = 'failed'
        else:
            with open(pickle_path, 'rb') as f:
                creds = pickle.load(f)
            if hasattr(creds, 'valid') and not creds.valid:
                has_rt = bool(getattr(creds, 'refresh_token', None))
                if has_rt:
                    issues.append('gdrive_token.pickle: access token expired, has refresh_token')
                    if worst == 'ok':
                        worst = 'stale'
                else:
                    issues.append('gdrive_token.pickle: token invalid, no refresh_token — re-auth required')
                    worst = 'failed'
    except Exception as e:
        issues.append(f'gdrive_token.pickle: {e}')
        worst = 'failed'

    return {
        'status': worst,
        'last_error': '; '.join(issues) if issues else None,
    }


def _check_chrome_cdp() -> dict:
    """Ping Chrome DevTools Protocol endpoint on localhost:9222."""
    try:
        with urllib.request.urlopen('http://localhost:9222/json', timeout=3) as r:
            data = json.loads(r.read())
            if isinstance(data, list):
                return {'status': 'ok', 'last_error': None}
            return {'status': 'stale', 'last_error': 'CDP /json returned unexpected format'}
    except OSError as e:
        return {'status': 'failed', 'last_error': f'CDP unreachable: {e}'}
    except Exception as e:
        return {'status': 'failed', 'last_error': str(e)}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Prevent urllib from following redirects so we can inspect the Location header."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def _check_jefferies() -> dict:
    """Load jef_auth.json cookies and make a live request to content.jefferies.com."""
    jef_path = CREDS / 'jef_auth.json'
    try:
        data = json.loads(jef_path.read_text())
        cookies = data.get('cookies', [])
        if not cookies:
            return {'status': 'failed', 'last_error': 'jef_auth.json has no cookies'}

        # Build Cookie header from all jefferies.com-scoped cookies
        cookie_str = '; '.join(
            f"{c['name']}={c['value']}"
            for c in cookies
            if 'jefferies.com' in c.get('domain', '')
        )
        if not cookie_str:
            return {'status': 'failed', 'last_error': 'no jefferies.com cookies in jef_auth.json'}

        req = urllib.request.Request(
            'https://content.jefferies.com',
            headers={
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
                'Cookie': cookie_str,
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            },
            method='GET',
        )
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(req, timeout=10) as resp:
                if resp.status == 200:
                    return {'status': 'ok', 'last_error': None}
                return {'status': 'stale', 'last_error': f'HTTP {resp.status}'}
        except urllib.error.HTTPError as e:
            loc = ''
            if hasattr(e, 'headers'):
                loc = e.headers.get('Location', '') or ''
            loc_lower = loc.lower()
            if e.code in (301, 302, 303, 307, 308):
                # Redirect to login/SSO = session expired
                if any(kw in loc_lower for kw in ('login', 'auth', 'sso', 'signin')):
                    return {'status': 'failed', 'last_error': f'Redirected to auth ({e.code}): {loc[:80]}'}
                # Redirect still within jefferies.com = normal (HTTPS upgrade etc.)
                if 'jefferies.com' in loc_lower:
                    return {'status': 'ok', 'last_error': None}
                return {'status': 'stale', 'last_error': f'HTTP {e.code} → {loc[:80] or "(no location)"}'}
            return {'status': 'stale', 'last_error': f'HTTP {e.code}'}
        except OSError as e:
            # Network down — degrade to stale (not failed) since cookies may still be valid
            return {'status': 'stale', 'last_error': f'network unreachable (offline check skipped): {e}'}

    except FileNotFoundError:
        return {'status': 'failed', 'last_error': 'jef_auth.json not found'}
    except Exception as e:
        return {'status': 'failed', 'last_error': str(e)}


# ── public API ─────────────────────────────────────────────────────────────────

def check_all(write: bool = True) -> dict:
    """Run all credential checks. Writes ~/credentials/auth_health.json by default.

    Returns the health dict. Never raises.
    """
    now = _now_iso()
    checks = {
        'google':      _check_google,
        'chrome_cdp':  _check_chrome_cdp,
        'jefferies':   _check_jefferies,
    }
    health: dict = {}
    for key, fn in checks.items():
        try:
            result = fn()
        except Exception as e:
            result = {'status': 'failed', 'last_error': f'watchdog internal error: {e}'}
        health[key] = {
            'status':       result.get('status', 'failed'),
            'last_checked': now,
            'last_error':   result.get('last_error'),
        }

    if write:
        try:
            HEALTH_FILE.write_text(json.dumps(health, indent=2))
        except Exception:
            pass

    return health


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    result = check_all()
    sym = {'ok': '✓', 'stale': '~', 'failed': '✗'}
    for k, v in result.items():
        line = f'{sym.get(v["status"], "?")} {k}: {v["status"]}'
        if v.get('last_error'):
            line += f' — {v["last_error"]}'
        print(line)
