#!/usr/bin/env python3
"""
check_auth_surfaces.py
======================

Surfaces the state of every authentication surface the operator's routines
depend on, so "which routines will block today" is tracked on the health
snapshot instead of discovered at 7am when a routine fails.

Reads ~/credentials/auth_health.json, which is refreshed every 30 min by
the auth-watchdog LaunchAgent (_auth_watchdog.py). Surfaces:

  google     — token.json + gdrive_token.pickle (auto-healed; should be ok)
  chrome_cdp — Chrome DevTools on :9222 (NotebookLM / RBN / GS all need it)
  jefferies  — jef_auth.json Playwright cookies (NOT auto-healable; manual
               `jefferies_downloader.py --setup` re-login when it expires)
  goldman    — GS Marquee trustedLink precondition (NOT auto-healable; needs
               a fresh gs-portal alert email to warm the session)

Why this matters: google/token scopes self-heal, but the browser/cookie
sessions (jefferies, goldman, chrome) CANNOT be auto-restored — when they
expire a human must re-login. This check turns a silent 7am failure into a
visible warn the evening before.

Status:
  - "fail" if auth_health.json is missing, or any surface is 'failed'.
  - "warn" if the file is stale (watchdog not running) or any surface is 'stale'.
  - "pass" if every surface is 'ok' and the file is fresh.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HEALTH = Path.home() / "credentials" / "auth_health.json"
NAME = "auth-surfaces: per-routine login health"
RULE_REF = "RUNBOOK.md :: auth surfaces (token / chrome / jefferies / goldman)"

# auth_health.json is written every 30 min; flag stale past 2x that window.
STALE_AFTER_SEC = 2 * 1800

# Human-readable remediation per surface — shown when a surface is degraded.
REMEDIATION = {
    "google": "token auto-heals; if 'failed', run python3 ~/credentials/oauth_reauth.py",
    "chrome_cdp": "run ~/credentials/ensure_real_chrome.sh (Chrome must be up on :9222)",
    "jefferies": "re-login: python3 ~/credentials/jefferies_downloader.py --setup",
    "goldman": "ensure a fresh gs-portal alert email exists, or re-warm Marquee in Chrome",
}


def run() -> dict[str, Any]:
    if not HEALTH.exists():
        return {
            "name": NAME, "rule_ref": RULE_REF, "status": "fail",
            "summary": "auth_health.json missing — auth-watchdog has never run",
            "details": {"path": str(HEALTH),
                        "fix": "load the auth-watchdog LaunchAgent"},
        }
    try:
        data = json.loads(HEALTH.read_text())
    except Exception as e:
        return {"name": NAME, "rule_ref": RULE_REF, "status": "fail",
                "summary": f"auth_health.json unreadable: {e}", "details": {}}

    # Freshness: oldest last_checked across surfaces.
    now = datetime.now(timezone.utc)
    oldest_age = None
    for v in data.values():
        ts = v.get("last_checked")
        if not ts:
            continue
        try:
            age = (now - datetime.fromisoformat(ts)).total_seconds()
            oldest_age = age if oldest_age is None else max(oldest_age, age)
        except Exception:
            pass

    stale_file = oldest_age is not None and oldest_age > STALE_AFTER_SEC

    failed, degraded, surfaces = [], [], {}
    for key, v in data.items():
        st = v.get("status", "failed")
        surfaces[key] = st
        if st == "failed":
            failed.append(f"{key} ({v.get('last_error') or 'failed'}) → {REMEDIATION.get(key, '')}")
        elif st == "stale":
            degraded.append(f"{key} ({v.get('last_error') or 'stale'}) → {REMEDIATION.get(key, '')}")

    if failed:
        return {
            "name": NAME, "rule_ref": RULE_REF, "status": "fail",
            "summary": f"{len(failed)} auth surface(s) DOWN — routines will block: "
                       + "; ".join(f.split(' → ')[0] for f in failed),
            "details": {"surfaces": surfaces, "failed": failed, "degraded": degraded,
                        "age_hours": round((oldest_age or 0) / 3600, 1)},
        }
    if stale_file or degraded:
        bits = []
        if stale_file:
            bits.append(f"watchdog stale ({round((oldest_age or 0)/3600,1)}h old)")
        if degraded:
            bits.append(f"{len(degraded)} surface(s) aging: "
                        + "; ".join(d.split(' → ')[0] for d in degraded))
        return {
            "name": NAME, "rule_ref": RULE_REF, "status": "warn",
            "summary": " · ".join(bits),
            "details": {"surfaces": surfaces, "degraded": degraded,
                        "age_hours": round((oldest_age or 0) / 3600, 1)},
        }
    return {
        "name": NAME, "rule_ref": RULE_REF, "status": "pass",
        "summary": f"all {len(surfaces)} auth surfaces ok ({', '.join(surfaces)})",
        "details": {"surfaces": surfaces, "age_hours": round((oldest_age or 0) / 3600, 1)},
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
