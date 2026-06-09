#!/usr/bin/env python3
"""
check_token_scopes.py
=====================

Guards the shared OAuth token (~/credentials/token.json) against silent
scope-narrowing.

BACKGROUND. ~24 pipeline scripts read AND write token.json, and each
carries its own narrow SCOPES list (drive-only, documents-only,
gmail.send-only, calendar.readonly-only). When any refreshes the token,
google-auth stamps ITS scope list back onto the shared file — starving
every other consumer. Symptom: the daily briefing's Step-0 scope check
aborts because token.json claims, e.g., drive-only. The grant on Google's
side is unchanged; only the local `scopes` field is corrupted.

The canonical union lives in ~/credentials/token_union_scopes.json
(captured from a real reauth). heal_token_scopes.py restores the field
when it drifts. This check reports the drift so it shows up on the health
snapshot instead of as a dead briefing.

Status:
  - "fail" if token.json is missing, unreadable, or has no refresh_token
    (genuinely needs reauth).
  - "warn" if the scopes field is narrower than the pinned union (a writer
    narrowed it; heal_token_scopes.py will restore it on next run, but it
    means a narrowing writer is active and worth fixing at source).
  - "pass" if scopes cover the union, or "warn" if no pin exists yet.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CREDS = Path.home() / "credentials"
TOKEN = CREDS / "token.json"
PIN = CREDS / "token_union_scopes.json"

NAME = "token-scopes: shared OAuth union integrity"
RULE_REF = "CLAUDE.md :: 'Never narrow scopes on the shared token.json'"


def run() -> dict[str, Any]:
    if not PIN.exists():
        return {
            "name": NAME, "rule_ref": RULE_REF, "status": "warn",
            "summary": "no token_union_scopes.json pin yet — run heal_token_scopes.py setup",
            "details": {"pin": str(PIN)},
        }
    try:
        union = set(json.loads(PIN.read_text()).get("scopes", []))
    except Exception as e:
        return {"name": NAME, "rule_ref": RULE_REF, "status": "warn",
                "summary": f"pin unreadable: {e}", "details": {"pin": str(PIN)}}

    if not TOKEN.exists():
        return {"name": NAME, "rule_ref": RULE_REF, "status": "fail",
                "summary": "token.json missing — reauth required (oauth_reauth.py)",
                "details": {"token": str(TOKEN)}}
    try:
        data = json.loads(TOKEN.read_text())
    except Exception as e:
        return {"name": NAME, "rule_ref": RULE_REF, "status": "fail",
                "summary": f"token.json unreadable: {e}", "details": {}}

    if not data.get("refresh_token"):
        return {"name": NAME, "rule_ref": RULE_REF, "status": "fail",
                "summary": "token.json has no refresh_token — reauth required",
                "details": {}}

    current = set(data.get("scopes", []))
    missing = sorted(union - current)
    if missing:
        return {
            "name": NAME, "rule_ref": RULE_REF, "status": "warn",
            "summary": f"token scopes narrowed — missing {len(missing)} "
                       f"(heal restores on next run; a narrowing writer is active)",
            "details": {"missing": missing, "current": sorted(current),
                        "union": sorted(union)},
        }
    return {
        "name": NAME, "rule_ref": RULE_REF, "status": "pass",
        "summary": f"token covers full union ({len(union)} scopes)",
        "details": {"scopes": sorted(current)},
    }


if __name__ == "__main__":
    import json as _j
    print(_j.dumps(run(), indent=2))
