#!/usr/bin/env python3
"""check_l0050.py — Rule L0050: no invented surnames/firms from email fragments.

When the extractor labels a contact off a partial signal (an email
handle, a domain prefix), it sometimes invents a surname or firm name
that doesn't appear anywhere in the source content. Hard to detect
statically, but a strong heuristic exists: a `who` value that is a
single capitalized token AND that token appears nowhere else in the
item's `what` / `source` / `context` is almost certainly invented.

Status:
  pass — 0 suspicious
  warn — 1-3 suspicious
  fail — >3 suspicious
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

HOME = Path.home()
DASHBOARD_DATA = HOME / "dashboards" / "data" / "compiled" / "dashboard-data.json"

# Allowlist common short labels that are not "invented" (principals,
# team members, generic non-identifying labels).
_ALLOWLIST = {
    "yoni", "mark", "nik", "tbd", "team", "self", "internal",
    "unknown", "n/a", "tcip", "tomac",
}

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z\-']{1,}")


def _is_single_capitalized(who: str) -> bool:
    parts = who.strip().split()
    if len(parts) != 1:
        return False
    tok = parts[0]
    return len(tok) >= 3 and tok[0].isupper() and tok.isalpha()


def _context_text(item: dict) -> str:
    fields = ("what", "source", "context", "note", "deal", "workstream")
    return " ".join(str(item.get(f) or "") for f in fields)


def run() -> dict[str, Any]:
    if not DASHBOARD_DATA.exists():
        return {
            "name": "L0050: no invented surnames/firms",
            "rule_ref": "L0050",
            "status": "warn",
            "summary": f"dashboard-data.json not present: {DASHBOARD_DATA}",
            "details": {"path": str(DASHBOARD_DATA)},
        }

    try:
        data = json.loads(DASHBOARD_DATA.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "name": "L0050: no invented surnames/firms",
            "rule_ref": "L0050",
            "status": "fail",
            "summary": f"dashboard-data.json unreadable: {exc}",
            "details": {"error": str(exc)},
        }

    suspicious: list[dict[str, Any]] = []
    total_scanned = 0

    for section in ("followUps", "awaitingExternal"):
        items = data.get(section) or []
        for it in items:
            if not isinstance(it, dict):
                continue
            who = (it.get("who") or "").strip()
            if not who:
                continue
            if who.lower() in _ALLOWLIST:
                continue
            if not _is_single_capitalized(who):
                continue
            total_scanned += 1
            ctx_text = _context_text(it).lower()
            # If the token appears in the item's other fields, it's
            # supported by context — not invented.
            if who.lower() in ctx_text:
                continue
            suspicious.append({
                "section": section,
                "id": it.get("id"),
                "who": who,
                "what": (it.get("what") or it.get("note") or "")[:120],
                "source": (it.get("source") or "")[:80],
            })

    n = len(suspicious)
    if n == 0:
        status = "pass"
    elif n <= 3:
        status = "warn"
    else:
        status = "fail"

    return {
        "name": "L0050: no invented surnames/firms",
        "rule_ref": "L0050",
        "status": status,
        "summary": (
            f"L0050: {n} suspicious single-token `who` value(s) with no "
            f"supporting context (scanned {total_scanned} candidates)"
        ),
        "details": {
            "suspicious_count": n,
            "candidates_scanned": total_scanned,
            "suspicious": suspicious[:20],
            "allowlist_size": len(_ALLOWLIST),
        },
    }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
